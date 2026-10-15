"""Staff confirmation and commit (Task 9.2; docs/STAFF_COMMIT.md; Demo Scope S5-S8;
Red-Team R-07, R-10, R-11, R-14; RFC: commit_change is triggered only by a confirmation
button, never by the LLM).

Deterministic: no model call happens here. A staff turn's messages that belong to 9.2 are
taken out before the staff buffer (split) and answered here (respond):

- a tap on a staff card or list: Save / Edit / Cancel of a create draft, Confirm / Cancel of
  an update draft, Undo, the "Kaunsi property?" pick, the "kis listing ki?" photo pick and
  the attach / undo confirmations (ids built by app.staff_cards);
- "#undo CODE" (a confirm card first: no write without a tap, rule 5);
- a message that replies to a staff card (message.context_wamid -> outbound_message ->
  staff_card, migration 017): photos attach to that draft (or, after a confirm, to that
  listing); text corrects that draft;
- the text that follows an Edit tap (staff_session.state, 10 minutes), unless it reads as a
  new listing description.

Every id a tap carries is client-supplied: the draft, event, listing or segment is re-read
under a row lock in the staff member's agency (FORCE RLS plus agency_id =
current_agency_id()) and re-validated (status, version, save blockers, update rules). A draft
or event remembers the message that closed it, so a retried turn re-sends the same reply and
a second tap writes nothing (a double tap is a no-op). Never log text, values or numbers.
"""

import dataclasses
import json
import logging
import re
from contextlib import contextmanager

from psycopg.types.json import Jsonb

from app import (
    cards,
    normalise,
    requirements,
    staff_buffer,
    staff_cards,
    staff_tools,
    whatsapp,
)
from app import staff_normalise as sn
from app.db import tenant_transaction

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
UNDO_BUTTON_MINUTES = 30  # R-11: the Undo button; "#undo CODE" has no time limit
EDIT_MINUTES = 10  # how long the text after an Edit tap is taken as the correction
RECENT_LISTING_DAYS = 7  # listings offered for a photo-only segment
ATTACH_TARGETS = 5  # drafts and listings each, in the "kis listing ki?" list
UNDOABLE = ("created", "updated", "status_changed")

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_VERSION = r"[0-9]{1,9}"
DRAFT_TAP = re.compile(rf"draft:(save|edit|cancel|confirm):({_UUID}):({_VERSION})")
UNDO_TAP = re.compile(rf"undo:({_UUID})")
UNDO_CONFIRM = re.compile(rf"undoc:(confirm|cancel):({_UUID}):({_VERSION})")
PICK = re.compile(r"pick:([A-Z0-9]{2,4}-[0-9]{3}):((?:p=[0-9]{1,13})?;?(?:s=[a-z_]{1,12})?)")
ATTACH = re.compile(rf"attach:({_UUID}):([dp]):({_UUID})")
ATTACH_CONFIRM = re.compile(rf"attachc:(confirm|cancel):({_UUID}):({_UUID}):({_VERSION})")
TAPS = (DRAFT_TAP, UNDO_TAP, UNDO_CONFIRM, PICK, ATTACH, ATTACH_CONFIRM)
UNDO_COMMAND = re.compile(r"\s*#\s*undo\s+#?([A-Za-z0-9]{2,4}-[0-9]{3})\s*", re.IGNORECASE)
CHANGE_STATUSES = frozenset({"active", "reserved", "on_hold", "sold", "rented", "withdrawn"})


@dataclasses.dataclass(frozen=True)
class Ctx:
    conn: object
    turn: object
    gazetteer: object  # None without the requirements service: facts lose place names
    city_code: str = "KHI"
    marla_sqft: object = 225


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


@contextmanager
def _tx(ctx):
    with tenant_transaction(ctx.conn, ctx.turn.agency_id):
        _set_timeout(ctx.conn)
        yield ctx.conn


def _notice(key, **params):
    return whatsapp.text(staff_cards.NOTICES[key].format(**params))


def _jsonb(value):
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=str))


def tap_of(message):
    """(pattern, match) of a 9.2 tap id, else None. Anything else falls through."""
    for pattern in TAPS:
        match = pattern.fullmatch(message.reply_id or "")
        if match:
            return pattern, match
    return None


def undo_command(message):
    match = UNDO_COMMAND.fullmatch(message.text or "") if message.type == "text" else None
    return match.group(1).upper() if match else None


# --- split: which messages of a staff turn are 9.2's -------------------------------------------

def card_refs(conn, wamids):
    """{quoted WAMID: (listing_draft_id, property_id)} for the staff cards among them.
    Inside a tenant transaction."""
    if not wamids:
        return {}
    rows = conn.execute(
        """
        SELECT o.provider_message_id, c.listing_draft_id, c.property_id
        FROM outbound_message o
        JOIN staff_card c ON c.agency_id = o.agency_id AND c.dedupe_key = o.dedupe_key
        WHERE o.agency_id = current_agency_id() AND o.provider_message_id = ANY(%s)
        """,
        (list(wamids),),
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def _pending_edit(conn, staff_id, lock=False):
    """The open Edit of this staff member ({draft_id, version}) while it is fresh, else None."""
    row = conn.execute(
        f"""
        SELECT state -> 'edit', (state -> 'edit' ->> 'until')::timestamptz > now()
        FROM staff_session
        WHERE agency_id = current_agency_id() AND staff_id = %s {"FOR UPDATE" if lock else ""}
        """,
        (staff_id,),
    ).fetchone()
    return row[0] if row and row[0] and row[1] else None


def _set_edit(conn, staff_id, edit):
    """Store (or, with None, clear) the pending Edit in staff_session.state."""
    value = (_jsonb(dict(edit, until=None)) if edit else None)
    conn.execute(
        """
        INSERT INTO staff_session (agency_id, staff_id, state)
        VALUES (current_agency_id(), %s, '{}'::jsonb)
        ON CONFLICT (agency_id, staff_id) DO NOTHING
        """,
        (staff_id,),
    )
    if edit is None:
        conn.execute(
            "UPDATE staff_session SET state = state - 'edit', updated_at = now(), "
            "last_activity_at = now() "
            "WHERE agency_id = current_agency_id() AND staff_id = %s", (staff_id,))
        return
    conn.execute(
        """
        UPDATE staff_session SET updated_at = now(), last_activity_at = now(),
            state = jsonb_set(state, '{edit}', %s::jsonb || jsonb_build_object(
                'until', now() + make_interval(mins => %s)))
        WHERE agency_id = current_agency_id() AND staff_id = %s
        """,
        (value, EDIT_MINUTES, staff_id),
    )


def split(conn, turn):
    """(mine, rest): the turn's messages 9.2 answers, and the ones left for the staff buffer
    and the engine, both in the turn's order. Idempotent for a resumed turn: a correction
    already applied to a draft is still recognised as one."""
    if turn.route != "staff" or turn.staff_id is None or not turn.messages:
        return [], list(turn.messages)
    with tenant_transaction(conn, turn.agency_id):
        _set_timeout(conn)
        refs = card_refs(conn, {m.context_wamid for m in turn.messages if m.context_wamid})
        photos = {row[0] for row in conn.execute(
            "SELECT message_id FROM inbound_media WHERE agency_id = current_agency_id() "
            "AND message_id = ANY(%s)", ([m.id for m in turn.messages],))}
        edit = _pending_edit(conn, turn.staff_id)
        applied = {row[0] for row in conn.execute(
            """
            SELECT e.value #>> '{}' FROM listing_draft d,
                LATERAL jsonb_array_elements(COALESCE(d.payload -> 'edits', '[]'::jsonb)) e
            WHERE d.agency_id = current_agency_id() AND d.staff_id = %s
              AND e.value #>> '{}' = ANY(%s)
            """,
            (turn.staff_id, [str(m.id) for m in turn.messages]),
        )}
    mine, rest, edit_taken = [], [], False
    for message in turn.messages:
        text = (message.text or "").strip()
        plain = message.type == "text" and bool(text) and not text.startswith("#")
        ref = refs.get(message.context_wamid)
        if (tap_of(message) or undo_command(message) or str(message.id) in applied
                or (ref and message.id in photos)  # photos for the card's draft or listing
                or (ref and ref[0] is not None and plain)):  # a correction of the card's draft
            mine.append(message)
        elif (edit and not edit_taken and plain
              # a new listing pasted meanwhile opens its own segment, never overwrites the
              # draft; a swipe-reply to the card still corrects it whatever it says
              and staff_buffer.classify_text(text) not in (staff_buffer.DONE,
                                                           staff_buffer.DESCRIPTION)):
            mine.append(message)
            edit_taken = True
        else:
            rest.append(message)
    return mine, rest


# --- respond --------------------------------------------------------------------------------

def respond(ctx, messages):
    """The payloads for the 9.2 messages of a staff turn, in order. Photos replying to the same
    card are attached together."""
    with _tx(ctx) as conn:
        refs = card_refs(conn, {m.context_wamid for m in messages if m.context_wamid})
        photos = {row[0]: row[1] for row in conn.execute(
            "SELECT message_id, id FROM inbound_media "
            "WHERE agency_id = current_agency_id() AND message_id = ANY(%s)",
            ([m.id for m in messages],))}
    items, grouped = [], {}
    for message in messages:
        if message.id in photos and message.context_wamid in refs:
            key = refs[message.context_wamid]
            if key not in grouped:
                grouped[key] = []
                items.append(("photos", key, grouped[key]))
            grouped[key].append((message, photos[message.id]))
        elif message.id not in photos:
            items.append(("one", message, refs.get(message.context_wamid)))
    out = []
    for item in items:
        if item[0] == "photos":
            out += _reply_photos(ctx, item[1], item[2])
        else:
            out += _one(ctx, item[1], item[2])
    return out


def _one(ctx, message, ref):
    found = tap_of(message)
    if found:
        pattern, match = found
        if pattern is DRAFT_TAP:
            return _draft_tap(ctx, message, match.group(1), match.group(2), int(match.group(3)))
        if pattern is UNDO_TAP:
            return _undo(ctx, message, match.group(1), button=True)
        if pattern is UNDO_CONFIRM:
            if match.group(1) == "cancel":
                return [_notice("staff_undo_cancelled")]
            return _undo(ctx, message, match.group(2), version=int(match.group(3)))
        if pattern is PICK:
            return _pick(ctx, message, match.group(1), match.group(2))
        if pattern is ATTACH:
            return _attach_pick(ctx, message, match.group(1), match.group(2), match.group(3))
        if match.group(1) == "cancel":
            return [_notice("staff_attach_cancelled")]
        return _attach_listing(ctx, message, match.group(2), match.group(3),
                               int(match.group(4)))
    code = undo_command(message)
    if code:
        return _undo_ask(ctx, code)
    draft_id = ref[0] if ref else None
    return _correction(ctx, message, draft_id)


# --- drafts: Save / Edit / Cancel / Confirm -------------------------------------------------

_DRAFT_COLUMNS = ("id", "staff_id", "kind", "status", "version", "payload",
                  "target_property_id", "base_property_version", "source_turn_id", "ordinal")


def _lock_draft(conn, draft_id):
    row = conn.execute(
        f"SELECT {', '.join(_DRAFT_COLUMNS)} FROM listing_draft "
        "WHERE agency_id = current_agency_id() AND id = %s FOR UPDATE",
        (draft_id,),
    ).fetchone()
    return dict(zip(_DRAFT_COLUMNS, row, strict=True)) if row else None


def _close_draft(conn, draft, status, done):
    conn.execute(
        """
        UPDATE listing_draft SET status = %s, closed_at = now(), updated_at = now(),
            payload = payload || jsonb_build_object('done', %s::jsonb)
        WHERE agency_id = current_agency_id() AND id = %s AND status = 'open'
        """,
        (status, _jsonb(done), draft["id"]),
    )


def _draft_tap(ctx, message, action, draft_id, version):
    with _tx(ctx) as conn:
        draft = _lock_draft(conn, draft_id)
        if draft is None or draft["staff_id"] != ctx.turn.staff_id:
            return [_notice("staff_card_closed")]
        wanted = {"save": "create", "edit": "create", "confirm": "update"}.get(action)
        if wanted is not None and draft["kind"] != wanted:
            return [_notice("staff_card_closed")]
        if draft["status"] != "open":
            return _closed_reply(draft, message)
        if draft["version"] != version:
            return [_notice("staff_card_stale")]
        if action == "cancel":
            _close_draft(conn, draft, "cancelled",
                         {"message_id": str(message.id), "action": "cancel"})
            edit = _pending_edit(conn, ctx.turn.staff_id, lock=True)
            if edit and edit.get("draft_id") == str(draft["id"]):
                _set_edit(conn, ctx.turn.staff_id, None)
            return [_notice("staff_draft_cancelled" if draft["kind"] == "create"
                            else "staff_update_cancelled")]
        if action == "edit":
            _set_edit(conn, ctx.turn.staff_id,
                      {"draft_id": str(draft["id"]), "version": draft["version"]})
            return [_notice("staff_edit_prompt")]
        if action == "save":
            return _save(ctx, conn, draft, message)
        return _confirm(ctx, conn, draft, message)


def _closed_reply(draft, message):
    """The answer to a tap on a closed draft: the same reply again when this very message
    closed it (a retried turn), else what happened to it. Nothing is written."""
    done = draft["payload"].get("done") or {}
    if done.get("message_id") == str(message.id):
        action = done.get("action")
        if action == "save":
            return [staff_cards.saved_reply(done["code"], done["event_id"])]
        if action == "confirm":
            return [staff_cards.updated_reply(draft["payload"]["target_code"],
                                              done["changes"], done["event_id"])]
        if action == "cancel":
            return [_notice("staff_draft_cancelled" if draft["kind"] == "create"
                            else "staff_update_cancelled")]
        if action == "stale":
            return [_notice("staff_update_stale")]
        return [_notice("staff_card_closed")]
    if draft["status"] == "saved" and draft["kind"] == "create":
        return [_notice("staff_already_saved", code=done.get("code", ""))]
    if draft["status"] == "saved":
        return [_notice("staff_already_updated")]
    if draft["status"] == "superseded":
        return [_notice("staff_card_stale")]
    return [_notice("staff_card_closed")]


def _blocked(blockers):
    labels = []
    for blocker in blockers:
        name = blocker.split(":", 1)[-1]
        label = staff_cards.FIELD_LABELS.get(name, name.replace("_", " "))
        if label not in labels:
            labels.append(label)
    return [_notice("staff_save_blocked", fields=", ".join(labels))]


def _allocate_code(conn, agency_id, prefix):
    """The next free code of a prefix, under an agency + prefix advisory lock held to the end
    of the transaction (two Saves of the same prefix run one after the other)."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (f"listing_code:{agency_id}:{prefix}",))
    codes = [row[0] for row in conn.execute(
        "SELECT listing_code FROM property WHERE agency_id = current_agency_id() "
        "AND listing_code LIKE %s", (f"{prefix}-%",))]
    return sn.next_code(prefix, codes)


def _source(conn, draft):
    """(source_text, source WAMIDs) of a draft: the text messages of its segment turn."""
    rows = conn.execute(
        """
        SELECT m.text_body, m.provider_message_id FROM conversation_turn t
        JOIN staff_segment_item i ON i.segment_id = t.staff_segment_id
            AND i.agency_id = t.agency_id
        JOIN message m ON m.id = i.message_id AND m.agency_id = i.agency_id
        WHERE t.agency_id = current_agency_id() AND t.id = %s
        ORDER BY i.position
        """,
        (draft["source_turn_id"],),
    ).fetchall() if draft["source_turn_id"] else []
    texts = [r[0] for r in rows if r[0]]
    return ("\n".join(texts) or None), ([r[1] for r in rows if r[1]] or None)


def _insert_media(conn, property_id, photos, start=0):
    """property_media rows for usable inbound photos (objects reused, EXIF already stripped),
    skipping bytes the listing already has. Returns the new ids in order."""
    have = {row[0] for row in conn.execute(
        "SELECT content_sha256 FROM property_media WHERE agency_id = current_agency_id() "
        "AND property_id = %s", (property_id,))}
    ids, order = [], start
    for photo in photos:
        if photo["content_sha256"] in have:
            continue
        have.add(photo["content_sha256"])
        ids.append(conn.execute(
            """
            INSERT INTO property_media (
                agency_id, property_id, original_key, wa_image_key, card_image_key,
                content_sha256, mime_type, sort_order, public_url_card, exif_stripped,
                source_inbound_media_id, kind
            )
            VALUES (current_agency_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'image')
            RETURNING id
            """,
            (property_id, photo["original_key"], photo["wa_image_key"],
             photo["card_image_key"], photo["content_sha256"], photo["mime_type"], order,
             photo["public_url_card"], photo["exif_stripped"], photo["id"]),
        ).fetchone()[0])
        order += 1
    return ids


def _value(fields, name):
    return (fields.get(name) or {}).get("value")


def _save(ctx, conn, draft, message):
    """Save a create draft: one transaction writes the listing (ACTIVE, so it is in
    v_public_listing at once), its photos and cover, the 'created' event, and closes the
    draft. The code is allocated here (the proposed one was advisory, 8.2 section 4.5)."""
    payload = draft["payload"]
    fields, conflicts = payload["fields"], payload["conflicts"]
    blockers = sn.save_blockers(fields, conflicts)  # never a stored flag (R-07)
    if blockers:
        return _blocked(blockers)
    location_id = fields["location"]["location_id"]
    place = (requirements.id_map(ctx.gazetteer).get(str(location_id))
             if ctx.gazetteer is not None else None)
    prefix = ctx.gazetteer.code_of(place) if place is not None else None
    if prefix is None:
        return [_notice("staff_no_code")]
    code = _allocate_code(conn, ctx.turn.agency_id, prefix)
    kind, size = _value(fields, "property_type"), fields["size"]
    features = {}
    if "description" in fields:
        clean, _ = sn.sanitise_description(fields["description"]["value"])  # again (R-36)
        if clean:
            features["description"] = clean
    if _value(fields, "parking_spaces") is not None:
        features["parking_spaces"] = _value(fields, "parking_spaces")
    source_text, wamids = _source(conn, draft)
    property_id = conn.execute(
        """
        INSERT INTO property (
            agency_id, listing_code, status, purpose, property_type, category, location_id,
            price_pkr, area_value, area_unit, area_marla_sqft, size_nominal_sqyd, bedrooms,
            bathrooms, construction_status, source_type, public_features, source_text,
            source_message_ids, created_by_staff_id, last_confirmed_at, is_demo
        )
        VALUES (
            current_agency_id(), %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, now(),
            -- an agency whose inventory is demo data keeps new listings marked as demo
            EXISTS (SELECT 1 FROM property WHERE agency_id = current_agency_id() AND is_demo)
        )
        RETURNING id
        """,
        (code, _value(fields, "purpose"), kind, normalise.category_of(kind), place,
         _value(fields, "price_pkr"), size["value"], size["unit"],
         size.get("marla_sqft") if size["unit"] in ("marla", "kanal") else None,
         size.get("nominal_sqyd"), _value(fields, "bedrooms"), _value(fields, "bathrooms"),
         _value(fields, "construction_status"), _value(fields, "source_type"),
         _jsonb(features), source_text, wamids, draft["staff_id"]),
    ).fetchone()[0]
    media = _insert_media(conn, property_id, staff_tools.draft_media(conn, draft["id"]))
    if media:
        conn.execute("UPDATE property SET cover_media_id = %s WHERE id = %s",
                     (media[0], property_id))
    snapshot = {name: _value(fields, name) for name in (
        "purpose", "property_type", "price_pkr", "bedrooms", "bathrooms",
        "construction_status", "source_type")}
    snapshot.update(location_id=str(place), area_value=size["value"], area_unit=size["unit"])
    event_id = conn.execute(
        """
        INSERT INTO property_event (agency_id, property_id, event_type, actor_staff_id,
                                    listing_draft_id, to_version, changes)
        VALUES (current_agency_id(), %s, 'created', %s, %s, 1, %s)
        RETURNING id
        """,
        (property_id, draft["staff_id"], draft["id"], _jsonb({
            "listing_code": code, "fields": snapshot, "photos": len(media),
            "message_id": str(message.id)})),
    ).fetchone()[0]
    _close_draft(conn, draft, "saved", {
        "message_id": str(message.id), "action": "save", "code": code,
        "property_id": str(property_id), "event_id": str(event_id)})
    logger.info("Draft %s saved as a listing with %d photo(s)", draft["id"], len(media))
    return [staff_cards.saved_reply(code, event_id)]


def _apply(conn, property_id, values, base_version):
    """Set columns of a listing with the optimistic version check; the new version or None."""
    sets = "".join(f"{column} = %s, " for column in values)
    row = conn.execute(
        f"UPDATE property SET {sets}version = version + 1, updated_at = now(), "
        "last_confirmed_at = now() "
        "WHERE agency_id = current_agency_id() AND id = %s AND version = %s RETURNING version",
        (*values.values(), property_id, base_version),
    ).fetchone()
    return row[0] if row else None


def _event_type(changes):
    return "status_changed" if set(changes) == {"status"} else "updated"


def _confirm(ctx, conn, draft, message):
    """Confirm an update draft: the listing must still be at the version the draft was made
    from (R-14), and the change is re-validated before it is written with its event."""
    payload = draft["payload"]
    facts = staff_tools.property_facts(conn, property_id=draft["target_property_id"],
                                       lock=True)
    if facts is None or facts["version"] != draft["base_property_version"]:
        _close_draft(conn, draft, "superseded",
                     {"message_id": str(message.id), "action": "stale"})
        return [_notice("staff_update_stale")]
    wanted = {field: change["to"] for field, change in payload["changes"].items()}
    checked = sn.check_update(facts, wanted)
    if "error" in checked:
        _close_draft(conn, draft, "superseded",
                     {"message_id": str(message.id), "action": "invalid"})
        reason = staff_cards.REASONS.get(checked["error"], staff_cards.REASONS["invalid_change"])
        return [_notice("staff_update_invalid", code=facts["code"], reason=reason)]
    changes = {field: {"from": c["from"], "to": c["to"]}
               for field, c in checked["changes"].items()}
    version = _apply(conn, facts["id"], {f: c["to"] for f, c in changes.items()},
                     facts["version"])
    event_id = conn.execute(
        """
        INSERT INTO property_event (agency_id, property_id, event_type, actor_staff_id,
                                    listing_draft_id, from_version, to_version, changes)
        VALUES (current_agency_id(), %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (facts["id"], _event_type(changes), draft["staff_id"], draft["id"], facts["version"],
         version, _jsonb({"fields": changes, "message_id": str(message.id)})),
    ).fetchone()[0]
    _close_draft(conn, draft, "saved", {
        "message_id": str(message.id), "action": "confirm", "changes": changes,
        "event_id": str(event_id)})
    logger.info("Update draft %s confirmed (%s)", draft["id"], ",".join(sorted(changes)))
    return [staff_cards.updated_reply(facts["code"], changes, event_id)]


# --- Edit: the free-text correction (deterministic) ------------------------------------------

def _correction(ctx, message, draft_id):
    """Apply a correction text to a draft: the one an Edit tap opened, or the draft whose card
    the text replies to. The draft gets version + 1, so its old buttons go stale."""
    text = (message.text or "").strip()
    if message.type != "text" or not text:
        return []
    listings = staff_tools.load_listings(ctx.conn, ctx.turn.agency_id)
    with _tx(ctx) as conn:
        edit = _pending_edit(conn, ctx.turn.staff_id, lock=True)
        if draft_id is None:
            draft_id = (edit or {}).get("draft_id")
        if draft_id is None:  # the Edit expired between split and here
            rows = conn.execute(
                "SELECT id FROM listing_draft WHERE agency_id = current_agency_id() "
                "AND staff_id = %s AND payload -> 'edits' ? %s",
                (ctx.turn.staff_id, str(message.id))).fetchall()
            if not rows:
                return [_notice("staff_edit_unclear")]
            draft_id = rows[0][0]
        draft = _lock_draft(conn, draft_id)
        usable = (draft is not None and draft["staff_id"] == ctx.turn.staff_id
                  and draft["kind"] == "create")
        if edit and edit.get("draft_id") == str(draft_id) and not (
                usable and draft["status"] == "open"):
            _set_edit(conn, ctx.turn.staff_id, None)  # its draft is gone: stop capturing texts
        if not usable:
            return [_notice("staff_card_closed")]
        applied = str(message.id) in (draft["payload"].get("edits") or [])
        if not applied:
            if draft["status"] != "open":
                return _closed_reply(draft, message)
            if ctx.gazetteer is None:
                return [_notice("staff_edit_unclear")]
            new = sn.apply_correction(draft["payload"], text, ctx.gazetteer,
                                      marla_sqft=ctx.marla_sqft, city_code=ctx.city_code,
                                      listings=listings)
            if new is None:
                return [_notice("staff_edit_unclear")]
            new["edits"] = [*(draft["payload"].get("edits") or []), str(message.id)]
            conn.execute(
                "UPDATE listing_draft SET payload = %s, version = version + 1, "
                "updated_at = now() WHERE agency_id = current_agency_id() AND id = %s",
                (_jsonb(new), draft["id"]))
            if edit and edit.get("draft_id") == str(draft["id"]):
                _set_edit(conn, ctx.turn.staff_id, None)
            logger.info("Draft %s corrected", draft["id"])
        stored, total = _stored_draft(conn, draft["id"])
    return [staff_tools.draft_card(ctx.conn, ctx.turn.agency_id, stored, total, ctx.gazetteer)]


def _stored_draft(conn, draft_id):
    row = conn.execute(
        """
        SELECT d.id, COALESCE(d.ordinal, 1), d.version, d.status, d.payload,
               (SELECT count(*) FROM listing_draft o WHERE o.agency_id = d.agency_id
                AND o.source_turn_id = d.source_turn_id)
        FROM listing_draft d WHERE d.agency_id = current_agency_id() AND d.id = %s
        """,
        (draft_id,),
    ).fetchone()
    return staff_tools.StoredDraft(*row[:5]), max(row[5], 1)


# --- Undo (R-11) ---------------------------------------------------------------------------

def _event(conn, event_id):
    row = conn.execute(
        """
        SELECT id, property_id, event_type, to_version, changes, actor_staff_id,
               created_at < now() - make_interval(mins => %s)
        FROM property_event WHERE agency_id = current_agency_id() AND id = %s
        """,
        (UNDO_BUTTON_MINUTES, event_id),
    ).fetchone()
    keys = ("id", "property_id", "event_type", "to_version", "changes", "actor", "expired")
    return dict(zip(keys, row, strict=True)) if row else None


def _undo_changes(event):
    """{field: {from, to}} the undo reverses (a created listing goes to withdrawn)."""
    if event["event_type"] == "created":
        return {}
    return event["changes"].get("fields") or {}


def _undo(ctx, message, event_id, *, button=False, version=None):
    """Reverse one event (button: within 30 minutes, by the staff member who made it;
    "#undo" confirm: at the version the card showed). Only when nothing changed the listing
    since (its version is still the event's); written as a new 'undone' event."""
    with _tx(ctx) as conn:
        event = _event(conn, event_id)
        if event is None or event["event_type"] not in UNDOABLE:
            return [_notice("staff_undo_none")]
        if button and event["actor"] != ctx.turn.staff_id:
            return [_notice("staff_card_closed")]
        done = conn.execute(
            "SELECT changes ->> 'message_id', changes FROM property_event "
            "WHERE agency_id = current_agency_id() AND undoes_event_id = %s", (event["id"],)
        ).fetchone()
        facts = staff_tools.property_facts(conn, property_id=event["property_id"], lock=True)
        if done is not None:
            if done[0] == str(message.id):
                return [staff_cards.undone_reply(facts["code"], event["event_type"],
                                                 _undo_changes(event))]
            return [_notice("staff_undo_done_before")]
        if button and event["expired"]:
            return [_notice("staff_undo_expired")]
        if facts["version"] != event["to_version"] or (
                version is not None and facts["version"] != version):
            return [_notice("staff_undo_later_change")]
        if event["event_type"] == "created":
            values = {"status": "withdrawn"}
            changes = {"status": {"from": facts["status"], "to": "withdrawn"}}
        else:
            values = {field: change["from"] for field, change in _undo_changes(event).items()}
            changes = {field: {"from": change["to"], "to": change["from"]}
                       for field, change in _undo_changes(event).items()}
        new_version = _apply(conn, facts["id"], values, facts["version"])
        conn.execute(
            """
            INSERT INTO property_event (agency_id, property_id, event_type, actor_staff_id,
                                        from_version, to_version, changes, undoes_event_id)
            VALUES (current_agency_id(), %s, 'undone', %s, %s, %s, %s, %s)
            """,
            (facts["id"], ctx.turn.staff_id, facts["version"], new_version,
             _jsonb({"fields": changes, "message_id": str(message.id)}), event["id"]),
        )
        logger.info("Event %s undone", event["id"])
        return [staff_cards.undone_reply(facts["code"], event["event_type"],
                                         _undo_changes(event))]


def _undo_ask(ctx, code):
    """"#undo CODE": the listing's latest change, shown on a confirm card (rule 5)."""
    with _tx(ctx) as conn:
        facts = staff_tools.property_facts(conn, code=code)
        if facts is None:
            return [_notice("staff_update_not_found", code=code)]
        row = conn.execute(
            """
            SELECT id FROM property_event
            WHERE agency_id = current_agency_id() AND property_id = %s
            ORDER BY to_version DESC, created_at DESC LIMIT 1
            """,
            (facts["id"],),
        ).fetchone()
        event = _event(conn, row[0]) if row else None
        if event is None or event["event_type"] not in UNDOABLE:
            return [_notice("staff_undo_none")]
        if event["to_version"] != facts["version"]:
            return [_notice("staff_undo_later_change")]
    return [staff_cards.undo_confirm_card(code, event["event_type"], _undo_changes(event),
                                          event["id"], facts["version"], facts, ctx.gazetteer)]


# --- the "Kaunsi property?" pick ----------------------------------------------------------

def decode_changes(encoded):
    """"p=78000000;s=sold" -> {"price_pkr": 78000000, "status": "sold"}; None when malformed."""
    changes = {}
    for part in filter(None, (encoded or "").split(";")):
        key, _, value = part.partition("=")
        if key == "p" and value.isdigit() and int(value) > 0:
            changes["price_pkr"] = int(value)
        elif key == "s" and value in CHANGE_STATUSES:
            changes["status"] = value
        else:
            return None
    return changes


def _pick(ctx, message, code, encoded):
    """A row of the candidate list: the listing (re-read in this agency) and the change the
    backend parsed from the staff text become an update draft and its Confirm card; nothing
    is written to the listing (R-11)."""
    changes = decode_changes(encoded)
    if not changes:
        return [_notice("staff_pick_no_change", code=code)]
    outcome = staff_tools.propose_update_draft(
        ctx.conn, ctx.turn, code, changes, {field: "list" for field in changes})
    if outcome.get("error") == "target_not_found":
        return [_notice("staff_update_not_found", code=code)]
    if "error" in outcome:
        reason = staff_cards.REASONS.get(outcome["error"], staff_cards.REASONS["invalid_change"])
        return [_notice("staff_update_invalid", code=code, reason=reason)]
    return [staff_tools.update_card(ctx.conn, ctx.turn.agency_id, outcome["draft"],
                                    ctx.gazetteer)]


# --- photos: photo-only segments and reply-to-card (R-10) -----------------------------------

def _draft_title(payload):
    code = payload.get("proposed_code")
    return f"Draft {code}" if code else "Draft (new)"


def _draft_description(payload, gazetteer):
    fields = payload["fields"]
    place = staff_cards.place_name((fields.get("location") or {}).get("location_id"),
                                   gazetteer)
    return " · ".join(x for x in (
        _value(fields, "property_type"), place,
        cards.price_text(_value(fields, "price_pkr"), _value(fields, "purpose"))) if x)


def attach_prompt(conn, turn, gazetteer):
    """The reply to a photo-only segment: a list of this staff member's open drafts and recent
    listings to attach the photos to (R-10), or None when there is none (the caller then
    asks for the listing's details, the 7.4 template)."""
    with tenant_transaction(conn, turn.agency_id):
        _set_timeout(conn)
        photos = len(staff_tools.segment_media(conn, turn.staff_segment_id))
        drafts = conn.execute(
            """
            SELECT id, payload FROM listing_draft
            WHERE agency_id = current_agency_id() AND staff_id = %s AND kind = 'create'
              AND status = 'open'
            ORDER BY created_at DESC LIMIT %s
            """,
            (turn.staff_id, ATTACH_TARGETS),
        ).fetchall()
        listings = conn.execute(
            """
            SELECT p.id, p.listing_code, p.property_type, p.location_id, p.price_pkr, p.purpose
            FROM property p
            WHERE p.agency_id = current_agency_id()
              AND p.status IN ('active', 'reserved', 'on_hold')
              AND p.updated_at > now() - make_interval(days => %s)
              AND (p.created_by_staff_id = %s OR EXISTS (
                  SELECT 1 FROM property_event e WHERE e.agency_id = p.agency_id
                  AND e.property_id = p.id AND e.actor_staff_id = %s))
            ORDER BY p.updated_at DESC LIMIT %s
            """,
            (RECENT_LISTING_DAYS, turn.staff_id, turn.staff_id, ATTACH_TARGETS),
        ).fetchall()
    targets = [("d", d[0], _draft_title(d[1]), _draft_description(d[1], gazetteer))
               for d in drafts]
    targets += [("p", p[0], p[1], " · ".join(x for x in (
        p[2], staff_cards.place_name(p[3], gazetteer), cards.price_text(p[4], p[5])) if x))
        for p in listings]
    if not targets or not photos:
        return None
    return [staff_cards.attach_list(turn.staff_segment_id, photos, targets)]


def _segment_photos(conn, staff_id, segment_id):
    """This staff member's flushed segment's usable photos, or None (not theirs / unknown)."""
    row = conn.execute(
        "SELECT 1 FROM staff_segment WHERE agency_id = current_agency_id() AND id = %s "
        "AND staff_id = %s AND status = 'flushed'", (segment_id, staff_id)).fetchone()
    return staff_tools.segment_media(conn, segment_id) if row else None


def _add_to_draft(ctx, conn, draft_id, media_ids, token):
    """Attach photos to an open create draft (no listing is written, so no confirm): its
    payload.photos grows, version + 1, and the card is sent again. token makes a retry a
    no-op. Returns the payloads."""
    draft = _lock_draft(conn, draft_id)
    if (draft is None or draft["staff_id"] != ctx.turn.staff_id or draft["kind"] != "create"
            or draft["status"] != "open"):
        return None
    payload = draft["payload"]
    if token not in (payload.get("attached") or []):
        current = [str(i) for i in payload.get("photos") or []]
        if payload.get("photos") is None:  # the photos it had by default stay on it
            current = [str(m["id"]) for m in staff_tools.draft_media(conn, draft["id"])]
        photos = current + [str(i) for i in media_ids if str(i) not in current]
        conn.execute(
            """
            UPDATE listing_draft SET version = version + 1, updated_at = now(),
                payload = payload || jsonb_build_object('photos', %s::jsonb,
                    'attached', COALESCE(payload -> 'attached', '[]'::jsonb) || %s::jsonb)
            WHERE agency_id = current_agency_id() AND id = %s
            """,
            (_jsonb(photos), _jsonb([token]), draft["id"]),
        )
        logger.info("Draft %s: %d photo(s) attached", draft["id"], len(media_ids))
    return draft["id"]


def _attach_pick(ctx, message, segment_id, kind, target_id):
    """A row of the photo-only list: a draft takes the photos at once; a saved listing gets a
    confirm card first (its customers would see the photos)."""
    with _tx(ctx) as conn:
        photos = _segment_photos(conn, ctx.turn.staff_id, segment_id)
        if not photos:
            return [_notice("staff_card_closed")]
        if kind == "d":
            draft_id = _add_to_draft(ctx, conn, target_id, [p["id"] for p in photos],
                                     f"segment:{segment_id}")
            if draft_id is None:
                return [_notice("staff_card_closed")]
            stored, total = _stored_draft(conn, draft_id)
        else:
            facts = staff_tools.property_facts(conn, property_id=target_id)
            if facts is None:
                return [_notice("staff_card_closed")]
    if kind == "d":
        return [staff_tools.draft_card(ctx.conn, ctx.turn.agency_id, stored, total,
                                       ctx.gazetteer)]
    return [staff_cards.attach_confirm_card(facts["code"], len(photos), segment_id, facts,
                                            ctx.gazetteer)]


def _attach_listing(ctx, message, source_id, property_id, version):
    """Confirm: add the photos (a segment's, or the ones a turn sent in reply to the listing's
    card) to a saved listing, with a 'media_added' event; the first becomes the cover when the
    listing has none. A retried or repeated confirm adds nothing."""
    with _tx(ctx) as conn:
        facts = staff_tools.property_facts(conn, property_id=property_id, lock=True)
        if facts is None:
            return [_notice("staff_card_closed")]
        done = conn.execute(
            "SELECT changes ->> 'message_id', changes -> 'media' FROM property_event "
            "WHERE agency_id = current_agency_id() AND property_id = %s "
            "AND event_type = 'media_added' AND changes ->> 'source_id' = %s",
            (property_id, source_id)).fetchone()
        if done is not None:
            if done[0] == str(message.id):
                return [staff_cards.attached_reply(facts["code"], len(done[1] or []))]
            return [_notice("staff_card_closed")]
        if facts["version"] != version:
            return [_notice("staff_card_stale")]
        photos = _segment_photos(conn, ctx.turn.staff_id, source_id)
        if photos is None:
            photos = _turn_photos(conn, ctx.turn.staff_id, source_id, property_id)
        if not photos:
            return [_notice("staff_card_closed")]
        start = conn.execute(
            "SELECT COALESCE(max(sort_order) + 1, 0) FROM property_media "
            "WHERE agency_id = current_agency_id() AND property_id = %s",
            (property_id,)).fetchone()[0]
        media = _insert_media(conn, property_id, photos, start)
        values = {}
        if media and conn.execute("SELECT cover_media_id IS NULL FROM property WHERE id = %s",
                                  (property_id,)).fetchone()[0]:
            values["cover_media_id"] = media[0]
        new_version = _apply(conn, property_id, values, facts["version"])
        conn.execute(
            """
            INSERT INTO property_event (agency_id, property_id, event_type, actor_staff_id,
                                        from_version, to_version, changes)
            VALUES (current_agency_id(), %s, 'media_added', %s, %s, %s, %s)
            """,
            (property_id, ctx.turn.staff_id, facts["version"], new_version,
             _jsonb({"media": [str(m) for m in media], "source_id": source_id,
                     "message_id": str(message.id)})),
        )
        logger.info("Listing %s: %d photo(s) added", property_id, len(media))
    return [staff_cards.attached_reply(facts["code"], len(media))]


def _turn_photos(conn, staff_id, turn_id, property_id):
    """The usable photos a staff turn sent in reply to one listing's card."""
    rows = conn.execute(
        """
        SELECT m.id FROM message m
        JOIN conversation_turn t ON t.id = m.turn_id AND t.agency_id = m.agency_id
        JOIN outbound_message o ON o.provider_message_id = m.context_wamid
            AND o.agency_id = m.agency_id
        JOIN staff_card c ON c.dedupe_key = o.dedupe_key AND c.agency_id = o.agency_id
        WHERE m.agency_id = current_agency_id() AND m.turn_id = %s AND t.staff_id = %s
          AND c.property_id = %s
        ORDER BY m.provider_timestamp NULLS LAST, m.created_at
        """,
        (turn_id, staff_id, property_id),
    ).fetchall()
    media = conn.execute(
        "SELECT id FROM inbound_media WHERE agency_id = current_agency_id() "
        "AND message_id = ANY(%s)", ([r[0] for r in rows],)).fetchall()
    return staff_tools.media_rows(conn, [r[0] for r in media])


def _reply_photos(ctx, ref, items):
    """Photos sent in reply to a staff card: to a draft card they attach at once; to a saved
    listing's card they get a confirm card."""
    draft_id, property_id = ref
    media_ids = [media_id for _, media_id in items]
    token = f"turn:{ctx.turn.id}"
    with _tx(ctx) as conn:
        if draft_id is not None:
            if _add_to_draft(ctx, conn, draft_id, media_ids, token) is None:
                return [_notice("staff_card_closed")]
            stored, total = _stored_draft(conn, draft_id)
        else:
            facts = staff_tools.property_facts(conn, property_id=property_id)
            if facts is None:
                return [_notice("staff_card_closed")]
    if draft_id is not None:
        return [staff_tools.draft_card(ctx.conn, ctx.turn.agency_id, stored, total,
                                       ctx.gazetteer, photos=len(stored.payload["photos"]))]

    return [staff_cards.attach_confirm_card(facts["code"], len(media_ids), str(ctx.turn.id),
                                            facts, ctx.gazetteer)]


# --- the reply-to-card map (migration 017) --------------------------------------------------

def card_targets(payloads):
    """[(seq, kind, id)] for the payloads that are staff cards: ('d', draft id) from a draft
    button, ('e', event id) from an Undo button, ('p', property id) from an attach confirm."""
    out = []
    for seq, payload in enumerate(payloads):
        if payload.get("type") != "interactive":
            continue
        interactive = payload["interactive"]
        if interactive.get("type") != "button":
            continue
        for button in interactive["action"]["buttons"]:
            reply_id = button["reply"]["id"]
            if m := DRAFT_TAP.fullmatch(reply_id):
                out.append((seq, "d", m.group(2)))
            elif m := UNDO_TAP.fullmatch(reply_id):
                out.append((seq, "e", m.group(1)))
            elif m := UNDO_CONFIRM.fullmatch(reply_id):
                out.append((seq, "e", m.group(2)))
            elif m := ATTACH_CONFIRM.fullmatch(reply_id):
                out.append((seq, "p", m.group(3)))
            else:
                continue
            break
    return out


def register_cards(conn, turn, payloads):
    """Record which draft or listing each staff card of a reply is about, under the key it is
    sent with (turn:{id}:{seq}), before it is sent. Idempotent."""
    targets = card_targets(payloads)
    if not targets or turn.route != "staff":
        return
    with tenant_transaction(conn, turn.agency_id):
        _set_timeout(conn)
        for seq, kind, target in targets:
            draft_id = property_id = None
            if kind == "d":
                draft_id = target
            elif kind == "p":
                property_id = target
            else:
                row = conn.execute(
                    "SELECT property_id FROM property_event "
                    "WHERE agency_id = current_agency_id() AND id = %s", (target,)).fetchone()
                if row is None:
                    continue
                property_id = row[0]
            conn.execute(
                """
                INSERT INTO staff_card (agency_id, dedupe_key, listing_draft_id, property_id)
                SELECT current_agency_id(), %s, %s, %s
                WHERE EXISTS (SELECT 1 FROM listing_draft WHERE id = %s)
                   OR EXISTS (SELECT 1 FROM property WHERE id = %s)
                ON CONFLICT (agency_id, dedupe_key) DO NOTHING
                """,
                (f"turn:{turn.id}:{seq}", draft_id, property_id, draft_id, property_id),
            )

