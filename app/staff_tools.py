"""Staff tools, database layer (Task 8.2; Task 9.2 cards with buttons).

Everything runs in the caller's agency: tenant_transaction sets the RLS context and
every statement also names current_agency_id(), so another agency's listings can
neither be compared with a draft nor take part in a code sequence. A staff turn's
create drafts are stored once under (agency_id, source_turn_id, ordinal) (migration 016);
nothing here writes a property or reserves a listing code: the cards carry Save / Edit /
Cancel / Confirm buttons, and only a tap, handled by app.staff_actions, commits (R-07, RFC:
commit_change is never an LLM tool).
Never log rows, payloads or message text.
"""

import json
from dataclasses import dataclass
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from app import ai_tools, staff_cards, staff_drafts, staff_normalise
from app.db import tenant_transaction

STATEMENT_TIMEOUT = "5s"
MAX_DRAFTS = 5


def load_listings(conn, agency_id):
    """The agency's listings, every status, as the rows staff_normalise compares: code,
    status, purpose, property_type, location_id, sqft (the nominal sq-yd bucket when there
    is one, else the actual size), price_pkr, bedrooms."""
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        rows = conn.execute(
            """
            SELECT listing_code, status, purpose, property_type, location_id,
                   COALESCE(size_nominal_sqyd * 9, area_sqft), price_pkr, bedrooms,
                   id, version
            FROM property
            WHERE agency_id = current_agency_id()
            ORDER BY listing_code
            """
        ).fetchall()
    return [{"code": r[0], "status": r[1], "purpose": r[2], "property_type": r[3],
             "location_id": r[4], "sqft": None if r[5] is None else Decimal(r[5]),
             "price_pkr": r[6], "bedrooms": r[7], "id": r[8], "version": r[9]}
            for r in rows]


def agency_settings(conn, agency_id):
    """(city_default, marla_sqft) of the agency."""
    with tenant_transaction(conn, agency_id):
        row = conn.execute(
            "SELECT city_default, marla_sqft FROM agency WHERE id = current_agency_id()"
        ).fetchone()
    return row if row else ("KHI", 225)


MEDIA_COLUMNS = ("id", "content_sha256", "mime_type", "original_key", "wa_image_key",
                 "card_image_key", "public_url_card", "exif_stripped")
_MEDIA_SELECT = ", ".join(f"m.{c}" for c in MEDIA_COLUMNS)


def segment_media(conn, segment_id):
    """The usable photos (stored, or a duplicate of a stored copy) of a segment, in order.
    Inside a tenant transaction."""
    rows = conn.execute(
        f"""
        SELECT {_MEDIA_SELECT} FROM staff_segment_item i
        JOIN inbound_media m ON m.message_id = i.message_id
            AND m.agency_id = current_agency_id()
        WHERE i.agency_id = current_agency_id() AND i.segment_id = %s AND i.is_photo
          AND m.status IN ('stored', 'duplicate')
        ORDER BY i.position
        """,
        (segment_id,),
    ).fetchall()
    return [dict(zip(MEDIA_COLUMNS, row, strict=True)) for row in rows]


def media_rows(conn, ids):
    """Usable inbound photos by id, in the order given (unknown or unusable ids dropped).
    Inside a tenant transaction."""
    if not ids:
        return []
    rows = conn.execute(
        f"""
        SELECT {_MEDIA_SELECT} FROM inbound_media m
        WHERE m.agency_id = current_agency_id() AND m.id = ANY(%s::uuid[])
          AND m.status IN ('stored', 'duplicate')
        """,
        ([str(i) for i in ids],),
    ).fetchall()
    found = {str(row[0]): dict(zip(MEDIA_COLUMNS, row, strict=True)) for row in rows}
    return [found[str(i)] for i in ids if str(i) in found]


def draft_media(conn, draft_id):
    """The photos a create draft carries: the ones attached to it (payload.photos, 9.2), else,
    when it is the only draft of its segment turn, all the segment's photos (8.2 section 6).
    Several drafts of one message never share photos by guess (R-08). Inside a tenant
    transaction."""
    row = conn.execute(
        """
        SELECT d.payload -> 'photos', t.staff_segment_id,
               (SELECT count(*) FROM listing_draft o WHERE o.agency_id = d.agency_id
                AND o.source_turn_id = d.source_turn_id)
        FROM listing_draft d
        LEFT JOIN conversation_turn t ON t.id = d.source_turn_id
            AND t.agency_id = d.agency_id
        WHERE d.agency_id = current_agency_id() AND d.id = %s AND d.kind = 'create'
        """,
        (draft_id,),
    ).fetchone()
    if row is None:
        return []
    attached, segment_id, siblings = row
    if attached is not None:
        return media_rows(conn, attached)
    if segment_id is not None and siblings == 1:
        return segment_media(conn, segment_id)
    return []


FACT_KEYS = ("id", "code", "status", "purpose", "property_type", "location_id", "area_value",
             "area_unit", "bedrooms", "bathrooms", "price_pkr", "version", "cover_url")


def property_facts(conn, code=None, property_id=None, *, lock=False):
    """The facts a staff card shows for a listing (any status) with its cover's public card
    URL, or None. lock: FOR UPDATE of the property row. Inside a tenant transaction."""
    where, value = ("p.listing_code = %s", code) if code else ("p.id = %s", property_id)
    row = conn.execute(
        f"""
        SELECT p.id, p.listing_code, p.status, p.purpose, p.property_type, p.location_id,
               p.area_value, p.area_unit, p.bedrooms, p.bathrooms, p.price_pkr, p.version,
               c.public_url_card
        FROM property p
        LEFT JOIN property_media c ON c.id = p.cover_media_id AND c.agency_id = p.agency_id
        WHERE p.agency_id = current_agency_id() AND {where}
        {"FOR UPDATE OF p" if lock else ""}
        """,
        (value,),
    ).fetchone()
    return dict(zip(FACT_KEYS, row, strict=True)) if row else None


@dataclass(frozen=True)
class StoredDraft:
    id: object
    ordinal: int
    version: int
    status: str
    payload: dict


def _stored(conn, turn_id, kind):
    rows = conn.execute(
        """
        SELECT id, ordinal, version, status, payload FROM listing_draft
        WHERE agency_id = current_agency_id() AND source_turn_id = %s AND kind = %s
        ORDER BY ordinal
        """,
        (turn_id, kind),
    ).fetchall()
    return [StoredDraft(*row) for row in rows]


def _lock_turn(conn, turn):
    """Lock the turn's row (its retries run one after the other) and check it is this
    agency's turn of this staff member. Inside a tenant transaction."""
    row = conn.execute(
        "SELECT staff_id FROM conversation_turn "
        "WHERE agency_id = current_agency_id() AND id = %s FOR UPDATE",
        (turn.id,),
    ).fetchone()
    if row is None:
        raise LookupError("turn not found")
    if row[0] != turn.staff_id:
        raise ValueError("the turn belongs to another staff member")


def save_drafts(conn, turn, drafts):
    """Store 1-5 create drafts of a staff turn in one tenant transaction and return
    ([StoredDraft], created). A turn that already has drafts returns them unchanged, whatever
    `drafts` now says: the model's second answer to a retried turn changes nothing.

    The turn row is locked first, so two retries of one turn run one after the other (the
    second finds the first's rows) and the unique index is only the last line of defence.
    """
    if turn.route != "staff" or turn.staff_id is None:
        raise ValueError("only a staff turn can draft listings")
    if not 1 <= len(drafts) <= MAX_DRAFTS:
        raise ValueError(f"1-{MAX_DRAFTS} drafts per turn")
    if [d.ordinal for d in drafts] != list(range(1, len(drafts) + 1)):
        raise ValueError("draft ordinals must run 1..n")
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        _lock_turn(conn, turn)
        existing = _stored(conn, turn.id, "create")
        if existing:
            return existing, False
        for draft in drafts:
            conn.execute(
                """
                INSERT INTO listing_draft (agency_id, staff_id, kind, source_turn_id,
                                           ordinal, payload)
                VALUES (current_agency_id(), %s, 'create', %s, %s, %s)
                """,
                (turn.staff_id, turn.id, draft.ordinal,
                 Jsonb(staff_normalise.to_payload(draft),
                       dumps=lambda value: json.dumps(value, default=str))),
            )
        return _stored(conn, turn.id, "create"), True


# --- update proposals (slice 4b) -------------------------------------------------------------

def _summary(row, gazetteer):
    place = gazetteer.places.get(row["location_id"]) if row["location_id"] is not None else None
    return {"code": row["code"], "status": row["status"], "type": row["property_type"],
            "purpose": row["purpose"], "price_pkr": row["price_pkr"],
            "location": place.display_name if place else None}


def _proposal(conn, turn, code, changes, raw):
    """One transaction: this turn's draft if it has one (even superseded), else supersede the
    staff member's open proposal for the listing and store the new one. Returns
    {"draft": StoredDraft, "created": bool} or {"error": code}."""
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        _lock_turn(conn, turn)
        existing = _stored(conn, turn.id, "update")
        if existing:
            return {"draft": existing[0], "created": False}
        row = conn.execute(
            "SELECT id, version, status, purpose, price_pkr FROM property "
            "WHERE agency_id = current_agency_id() AND listing_code = %s", (code,)
        ).fetchone()
        if row is None:
            return {"error": "target_not_found"}
        prop = {"id": row[0], "version": row[1], "status": row[2], "purpose": row[3],
                "price_pkr": row[4]}
        checked = staff_normalise.check_update(prop, changes)
        if "error" in checked:
            return checked
        payload = {"schema": 1, "target_code": code, "warnings": checked["warnings"],
                   "changes": {f: dict(c, raw=raw[f]) for f, c in checked["changes"].items()}}
        # a newer proposal replaces the older open one (R-14); the version bump makes the
        # old buttons stale
        conn.execute(
            "UPDATE listing_draft SET status = 'superseded', closed_at = now(), "
            "updated_at = now(), version = version + 1 "
            "WHERE agency_id = current_agency_id() AND staff_id = %s "
            "AND target_property_id = %s AND kind = 'update' AND status = 'open'",
            (turn.staff_id, prop["id"]))
        conn.execute(
            "INSERT INTO listing_draft (agency_id, staff_id, kind, target_property_id, "
            "base_property_version, source_turn_id, ordinal, payload) "
            "VALUES (current_agency_id(), %s, 'update', %s, %s, %s, 1, %s)",
            (turn.staff_id, prop["id"], prop["version"], turn.id,
             Jsonb(payload, dumps=lambda value: json.dumps(value, default=str))))
        return {"draft": _stored(conn, turn.id, "update")[0], "created": True}


def propose_update_draft(conn, turn, code, changes, raw):
    """_proposal with one retry: a concurrent proposal for the same staff member and listing
    can win the unique index (listing_draft_one_open_update_idx); the retry then supersedes it."""
    for attempt in (1, 2):
        try:
            return _proposal(conn, turn, code, changes, raw)
        except psycopg.errors.UniqueViolation:
            if attempt == 2:
                raise


class StaffBinding:
    """The staff tools' view of one staff turn: the connection, the turn, the gazetteer and
    what find_listings found, so propose_update accepts a code the staff member did not write
    only when it was the single candidate of this turn. No tool here changes a property."""

    def __init__(self, conn, turn, gazetteer, *, city_code="KHI", marla_sqft=225):
        self.conn, self.turn, self.gazetteer = conn, turn, gazetteer
        self.city_code, self.marla_sqft = city_code, marla_sqft
        self.found = None      # the last find_listings result: {"status", "candidates"}
        self.proposed = None   # the turn's proposal result (StoredDraft) once made

    def _fail(self, name, error, notice=None, codes=()):
        """An error result; the staff member still gets the reply `notice` names."""
        result = ai_tools._error(name, error)
        return ai_tools.ToolResult(name, False, result.response, error, notice,
                                   trusted_codes=frozenset(codes))

    def draft_listing(self, args, context):
        existing = self._stored("create")
        if existing:  # a retried turn: its own drafts, whatever the model says now
            return self._drafted(existing, False)
        bound = staff_drafts.bind_draft_listing(args, context.staff)
        notices = {"too_many_drafts": "staff_draft_too_many", "no_segment": None}
        if "error" in bound:
            return self._fail("draft_listing", bound["error"], notices.get(bound["error"]))
        if not any({"property_type", "size", "price", "location"} & set(d.spans)
                   for d in bound["drafts"]):
            return self._fail("draft_listing", "no_listing", "staff_draft_none")
        listings = load_listings(self.conn, self.turn.agency_id)
        drafts = staff_normalise.normalise_drafts(
            args, context.staff, self.gazetteer, marla_sqft=self.marla_sqft,
            city_code=self.city_code, listings=listings)["drafts"]
        stored, created = save_drafts(self.conn, self.turn, drafts)
        return self._drafted(stored, created)

    def _drafted(self, stored, created):
        payloads = tuple(draft_card(self.conn, self.turn.agency_id, d, len(stored),
                                    self.gazetteer) for d in stored)
        codes = frozenset().union(*(staff_cards.draft_codes(d) for d in stored))
        response = {"status": "drafted" if created else "existing", "drafts": [
            {"ordinal": d.ordinal, "complete": not d.payload["blockers"],
             "missing": d.payload["missing"], "conflicts": len(d.payload["conflicts"]),
             "duplicates": [x["code"] for x in d.payload["duplicates"] if "code" in x],
             "proposed_code": d.payload["proposed_code"]} for d in stored]}
        return ai_tools.ToolResult("draft_listing", True, response, payloads=payloads,
                                   trusted_codes=codes)

    def _place(self, location_id):
        ids = {str(place_id): place_id for place_id in self.gazetteer.places}
        return self.gazetteer.places[ids[str(location_id)]].display_name

    def _stored(self, kind):
        with tenant_transaction(self.conn, self.turn.agency_id):
            self.conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            return _stored(self.conn, self.turn.id, kind)

    def find_listings(self, args, context):
        result = staff_drafts.verify_find_listings(args, context.staff)
        if "error" in result:
            return ai_tools._error("find_listings", result["error"])
        found = staff_normalise.find_candidates(
            result["spans"], load_listings(self.conn, self.turn.agency_id), self.gazetteer,
            marla_sqft=self.marla_sqft, city_code=self.city_code)
        if found.get("error") == "location_unclear" and len(found["candidates"]) == 1:
            place = self._place(found["candidates"][0])  # one similar place: ask, don't assume
            return self._fail("find_listings", "location_unclear", ai_tools.Notice(
                "staff_find_confirm_place", (("place", place),)))
        if "error" in found:
            return self._fail("find_listings", found["error"], "staff_find_unclear")
        self.found = found
        summaries = [_summary(row, self.gazetteer) for row in found["candidates"]]
        response = {"status": found["status"], "candidates": summaries}
        codes = frozenset(s["code"] for s in summaries)
        if found["status"] in ("several", "too_many"):  # a choice for the staff member
            # The change the staff member asked for, read by the backend from their own text
            # (never from the model), rides on each row; the tap re-validates it (9.2).
            changes = staff_normalise.changes_in_text(context.staff.texts, result["spans"])
            return ai_tools.ToolResult(
                "find_listings", True, response, trusted_codes=codes,
                payloads=(staff_cards.candidates_reply(
                    summaries, changes, more=found["status"] == "too_many"),))
        notice = "staff_update_none" if found["status"] == "none" else None
        return ai_tools.ToolResult("find_listings", True, response, notice=notice,
                                   trusted_codes=codes)

    def propose_update(self, args, context):
        if self.proposed is not None:
            return self._fail("propose_update", "one_per_turn", "staff_one_at_a_time")
        existing = self._existing()
        if existing is not None:  # a retried turn: its own draft, whatever the model says now
            return self._result(existing, False)
        result = staff_drafts.verify_propose_update(args, context.staff)
        if "error" in result:
            return ai_tools._error("propose_update", result["error"])
        code = result["target"]
        refused = staff_normalise.resolve_target(result, self.found)
        if refused:
            return self._fail("propose_update", refused,
                              None if refused == "ambiguous_target"  # a list was already sent
                              else "staff_update_pick")
        parsed = staff_normalise.parse_changes(result["verified"])
        if "error" in parsed:
            return self._invalid(code, parsed["error"])
        outcome = propose_update_draft(self.conn, self.turn, code, parsed["changes"],
                                       parsed["raw"])
        if outcome.get("error") == "target_not_found":
            return self._fail("propose_update", "target_not_found",
                              ai_tools.Notice("staff_update_not_found", (("code", code),)),
                              codes=(code,))
        if "error" in outcome:
            return self._invalid(code, outcome["error"])
        self.proposed = outcome["draft"]
        return self._result(outcome["draft"], outcome["created"])

    def _existing(self):
        with tenant_transaction(self.conn, self.turn.agency_id):
            conn = self.conn
            conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            rows = _stored(conn, self.turn.id, "update")
        return rows[0] if rows else None

    def _invalid(self, code, error):
        reason = staff_cards.REASONS.get(error, staff_cards.REASONS["invalid_change"])
        return self._fail("propose_update", error,
                          ai_tools.Notice("staff_update_invalid",
                                          (("code", code), ("reason", reason))), codes=(code,))

    def _result(self, draft, created):
        response = {"status": "proposed" if created else "existing", "draft_id": str(draft.id),
                    "draft_status": draft.status, "target": draft.payload["target_code"],
                    "changes": draft.payload["changes"]}
        card = update_card(self.conn, self.turn.agency_id, draft, self.gazetteer)
        return ai_tools.ToolResult(
            "propose_update", True, response, payloads=(card,),
            trusted_codes=frozenset({draft.payload["target_code"]}))


def draft_card(conn, agency_id, stored, total, gazetteer, photos=None):
    """The card of a stored create draft with its photo count and cover (Task 9.2). photos:
    the count to show when photos were just attached and may still be downloading."""
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        media = draft_media(conn, stored.id)
    return staff_cards.draft_card(stored, total, gazetteer,
                                  len(media) if photos is None else photos,
                                  media[0]["public_url_card"] if media else None)


def update_card(conn, agency_id, stored, gazetteer):
    """The Confirm / Cancel card of a stored update draft with the target's facts and cover
    (R-11: the staff member sees which listing changes)."""
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        facts = property_facts(conn, code=stored.payload["target_code"])
    return staff_cards.update_card(stored, facts, gazetteer)
