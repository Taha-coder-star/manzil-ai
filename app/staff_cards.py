"""Staff draft, update and candidate cards (Task 8.2 slice 4c; Task 9.2), built from STORED
rows only.

A draft card is a reply-buttons message (Demo Scope S5): the cover as image header when the
draft has photos, English labels, the missing fields and conflicts marked with ⚠, and Save
(only when nothing blocks it), Edit and Cancel. Button ids carry draft_id:version
(docs/STAFF_COMMIT.md section 2), so an old card cannot act on a newer draft; app.staff_actions
re-checks everything a tap claims. Facts come from the stored draft payload, the property row
and the gazetteer's display names, never from the model. The wording is proposed and needs
owner approval (docs/STAFF_EXTRACTION.md section 8, docs/STAFF_COMMIT.md section 6); NOTICES are
the fixed sentences the engine and app.staff_actions send as approved templates.
"""

from app import cards, requirements, whatsapp

UNITS = {"sq_yd": "sq yd", "sq_ft": "sq ft", "marla": "marla", "kanal": "kanal"}
FIELD_LABELS = {
    "purpose": "Purpose", "property_type": "Type", "location": "Location",
    "area_value": "Size", "price_pkr": "Price", "bedrooms": "Bedrooms",
    "size": "Size", "bathrooms": "Bathrooms", "construction_status": "Construction",
}
SOURCES = {"owner_direct": "Owner direct", "sub_agent": "Sub agent",
           "developer": "Developer", "agent_listing": "Agent listing"}
STATUS_LABELS = {"active": "ACTIVE", "reserved": "RESERVED", "on_hold": "ON HOLD",
                 "sold": "SOLD", "rented": "RENTED", "withdrawn": "WITHDRAWN",
                 "archived": "ARCHIVED"}
NOTE = "📝 Note: "
WORDING = {
    "head": "📝 *Draft {n} of {total}* · Proposed ID *{code}*",
    "head_no_code": "📝 *Draft {n} of {total}*",
    "inferred": "ℹ️ Purpose price se samjha gaya",
    "missing": "⚠️ *Missing:* {fields}. Edit dabayein aur likh kar bhejein.",
    "conflict": "⚠️ *Check* - {label}: {values}?",
    "duplicate": "⚠️ Milti-julti listing pehle se hai: {codes}.",
    "sanitised": "Description se {n} line hata di gayi (instruction jaisi).",
    "photos": "📷 Photos: {n}",
    "photos_multi": "📷 Photos ke liye is card ko reply karke photos bhejein.",
    "ready": "✅ Sab zaroori maloomat mil gayi. *Save* dabayein.",
    "not_ready": "Save ke liye maloomat poori karein (*Edit*).",
    "update": "✏️ *{code}*: {changes}\n*Confirm?*",
    "unusual": "Qeemat purani qeemat se bohat alag hai - dobara check karein.",
    "price_unusual": "Qeemat is area ki aam qeemat se bohat alag hai - dobara check karein.",
    "place_hint": "Location: kya {places}? Edit dabayein aur poora naam likhein.",
    "pick_head": "Kaunsi property? {n} listings milin. List se chunein.",
    "pick_more": "Sirf pehli {n} dikhayi hain; area ya size likh kar chhota karein.",
    "saved": "✅ Saved as *{code}*. Listing ab active hai.",
    "updated": "✅ *{code}* updated: {changes}.",
    "marked": "✅ *{code}* marked *{status}*.",
    "undo_ask": "↩ *Undo {code}*: {changes}?",
    "undo_created": "listing hata di jayegi (WITHDRAWN)",
    "undone": "↩ Undo ho gaya: *{code}* {changes}.",
    "undone_created": "↩ Undo ho gaya: *{code}* ab WITHDRAWN hai.",
    "attach_head": "📷 Ye {n} photos kis listing ki hain? List se chunein.",
    "attach_ask": "📷 *{code}*: {n} photos add karein?",
    "attached": "✅ *{code}*: {n} photos add ho gayin.",
}
# Sentences the engine and app.staff_actions send as approved templates
# (app.turn_engine.TEMPLATES). {code} values come from database rows, never from the model.
NOTICES = {
    "staff_draft_too_many": ("Ek message mein 5 se zyada listings hain. Please 5 ya us se "
                             "kam listings ek saath bhejein."),
    "staff_draft_too_long": ("Message bohat lamba hai. Listings ko chhote messages mein "
                             "bhejein."),
    "staff_draft_none": ("Is message mein listing samajh nahi aayi. Type, area, size aur "
                         "price likh kar bhejein."),
    "staff_draft_note_unassigned": ("Aap ka note kisi ek listing se jura nahi hai; baad "
                                    "mein listing ko theek karte waqt likh dein."),
    "staff_update_none": "Is description se koi listing nahi mili.",
    "staff_update_not_found": "{code} nahi mili.",
    "staff_update_invalid": "{code}: {reason}.",
    "staff_find_confirm_place": ("Kya aap {place} keh rahe hain? Poora naam likh kar dobara "
                                 "bhejein."),
    "staff_update_pick": "Kaunsi listing? Listing code likh kar dobara bhejein.",
    "staff_one_at_a_time": "Ek waqt mein sirf ek listing ki tabdeeli ho sakti hai.",
    "staff_find_unclear": ("Description samajh nahi aayi (jagah, type ya size). Listing code "
                           "ya saaf description likh kar bhejein."),
    # Task 9.2 (wording pending owner approval).
    "staff_card_stale": "Ye card purana hai. Neeche wala naya card istemal karein.",
    "staff_card_closed": "Is card par ab koi kaam nahi ho sakta.",
    "staff_already_saved": "Ye draft pehle hi {code} ke naam se save ho chuka hai.",
    "staff_already_updated": "Ye tabdeeli pehle hi save ho chuki hai.",
    "staff_draft_cancelled": "Draft cancel ho gaya. Kuch save nahi hua.",
    "staff_update_cancelled": "Tabdeeli cancel ho gayi. Kuch save nahi hua.",
    "staff_save_blocked": "Save nahi ho saka. ⚠ Missing / check: {fields}. Edit dabayein.",
    "staff_no_code": ("Is location ke liye listing code nahi ban sakta. Location Edit karein "
                      "(phase / area ka naam)."),
    "staff_edit_prompt": ("Correction likh kar bhejein, jaise: price 7.5 crore, 5 bed, "
                          "500 gaz, location DHA Phase 7."),
    "staff_edit_unclear": ("Correction samajh nahi aayi. Aise likhein: price 7.5 crore, "
                           "5 bed, 500 gaz, location DHA Phase 7."),
    "staff_update_stale": ("Listing is dauran badal chuki hai; ye tabdeeli save nahi hui. "
                           "Dobara bhejein."),
    "staff_undo_expired": ("Undo button 30 minute tak chalta hai. Ab #undo aur listing code "
                           "likh kar bhejein."),
    "staff_undo_later_change": ("Is ke baad listing mein aur tabdeeli hui hai, is liye undo "
                                "nahi ho sakta."),
    "staff_undo_none": "Undo ke liye koi tabdeeli nahi mili.",
    "staff_undo_done_before": "Ye tabdeeli pehle hi undo ho chuki hai.",
    "staff_undo_cancelled": "Undo cancel ho gaya. Kuch nahi badla.",
    "staff_attach_cancelled": "Photos kisi listing mein add nahi ki gayin.",
    "staff_pick_no_change": ("Tabdeeli samajh nahi aayi. Listing code ke saath likhein, jaise "
                             "#{code} sold."),
}
REASONS = {
    "no_change": "ye tabdeeli pehle se wahi hai",
    "bad_amount": "raqam samajh nahi aayi",
    "not_live": "ye listing ab active nahi hai",
    "sold_needs_sale": "sold sirf sale listing ho sakti hai",
    "rented_needs_rent": "rented sirf rent listing ho sakti hai",
    "invalid_change": "tabdeeli samajh nahi aayi",
}
DRAFT_WARNING_NOTE = "price_unusual"
MAX_CANDIDATE_ROWS = whatsapp.LIST_ROWS_MAX


# --- button and row ids (docs/STAFF_COMMIT.md section 2; parsed by app.staff_actions) ----------

def draft_button(action, draft_id, version):
    return f"draft:{action}:{draft_id}:{version}"


def undo_button(event_id):
    return f"undo:{event_id}"


def undo_confirm_button(action, event_id, version):
    return f"undoc:{action}:{event_id}:{version}"


def attach_row(segment_id, kind, target_id):
    return f"attach:{segment_id}:{kind}:{target_id}"


def attach_confirm_button(action, segment_id, property_id, version):
    return f"attachc:{action}:{segment_id}:{property_id}:{version}"


def encode_changes(changes):
    """{"price_pkr": int, "status": str} -> "p=78000000;s=sold" (a pick row id)."""
    parts = []
    if "price_pkr" in changes:
        parts.append(f"p={int(changes['price_pkr'])}")
    if "status" in changes:
        parts.append(f"s={changes['status']}")
    return ";".join(parts)


def pick_row(code, changes):
    return f"pick:{code}:{encode_changes(changes)}"


# --- shared fact lines ---------------------------------------------------------------------

def _value(field, default=None):
    return (field or {}).get("value", default)


def _size(size):
    return f"{size['value']:g} {UNITS.get(size['unit'], size['unit'])}"


def _place(location, gazetteer):
    if not location or "location_id" not in location:
        return None
    ids = requirements.id_map(gazetteer)
    place = ids.get(str(location["location_id"]))
    return gazetteer.places[place].display_name if place is not None else location.get("raw")


def place_name(location_id, gazetteer):
    """The display name of a place, or None (no place, or no gazetteer loaded)."""
    if location_id is None or gazetteer is None:
        return None
    place = requirements.id_map(gazetteer).get(str(location_id))
    return gazetteer.places[place].display_name if place is not None else None


def _conflict_values(conflict):
    return " or ".join(f"'{v['raw']}'" for v in conflict["values"])


def _fit(lines, limit=whatsapp.INTERACTIVE_BODY_MAX):
    """Join the lines (an empty line is a blank line between sections, never two in a row
    or at an end); drop the optional Note line, then cut, to stay within the limit."""
    body = _join(lines)
    if len(body) > limit:
        body = _join([line for line in lines if not line.startswith(NOTE)])
    return body if len(body) <= limit else body[: limit - 1] + "…"


def _join(lines):
    out = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def listing_lines(facts, gazetteer):
    """Key facts of a property row (R-11: shown on a status-change card so a wrong pick is
    visible): type · purpose · status, then place · size · rooms, then price."""
    kind, purpose = facts.get("property_type"), facts.get("purpose")
    head = " · ".join(x for x in ((kind or "").upper(), (purpose or "").upper(),
                                   STATUS_LABELS.get(facts.get("status"), "")) if x)
    lines = [f"🏠 *{head}*" if head else ""]
    size = (f"{cards._number(facts['area_value'])} "
            f"{UNITS.get(facts['area_unit'], facts['area_unit'])}"
            if facts.get("area_value") is not None else None)
    rooms = " / ".join(x for x in (
        f"{facts['bedrooms']} bed" if facts.get("bedrooms") is not None else None,
        f"{facts['bathrooms']} bath" if facts.get("bathrooms") is not None else None) if x)
    where = " · ".join(x for x in (place_name(facts.get("location_id"), gazetteer), size,
                                    rooms or None) if x)
    if where:
        lines.append(f"📍 {where}")
    price = cards.price_text(facts.get("price_pkr"), purpose)
    if price:
        lines.append(f"💰 *{price}*")
    return [line for line in lines if line]


def _image(cover_url):
    return {"link": cover_url} if cover_url else None


def _buttons(body, buttons, cover_url=None):
    return whatsapp.reply_buttons(body, buttons, image_header=_image(cover_url))


# --- draft cards (create) ------------------------------------------------------------------

def draft_card(stored, total, gazetteer, photos, cover_url=None):
    """The card of a stored create draft. photos: the draft's photo count (with several drafts
    in one message the card says photos are attached by replying to it, unless some already
    are); cover_url: the public card image of its first photo, or None."""
    payload, fields = stored.payload, stored.payload["fields"]
    code = payload.get("proposed_code")
    lines = [WORDING["head"].format(n=stored.ordinal, total=total, code=code) if code
             else WORDING["head_no_code"].format(n=stored.ordinal, total=total)]
    kind, purpose = _value(fields.get("property_type")), _value(fields.get("purpose"))
    inferred = (fields.get("purpose") or {}).get("source") == "inferred"
    lines.append("")
    if kind or purpose:
        head = " · ".join(x for x in ((kind or "").upper(), (purpose or "").upper()) if x)
        lines.append(f"🏠 *{head}*" + (" (inferred)" if inferred and purpose else ""))
    rooms = " / ".join(x for x in (
        f"{_value(fields['bedrooms'])} bed" if "bedrooms" in fields else None,
        f"{_value(fields['bathrooms'])} bath" if "bathrooms" in fields else None) if x)
    where = " · ".join(x for x in (
        _place(fields.get("location"), gazetteer),
        _size(fields["size"]) if "size" in fields else None, rooms or None) if x)
    if where:
        lines.append(f"📍 {where}")
    price = cards.price_text(_value(fields.get("price_pkr")), purpose)
    source = SOURCES.get(_value(fields.get("source_type")))
    if price or source:
        lines.append("💰 " + " · ".join(x for x in (f"*{price}*" if price else None, source)
                                          if x))
    lines.append("")
    if "description" in fields:
        lines.append(f"{NOTE}{fields['description']['value']}")
    attached = payload.get("photos") is not None
    if photos and (total == 1 or attached):
        lines.append(WORDING["photos"].format(n=photos))
    elif total > 1:
        lines.append(WORDING["photos_multi"])
    lines.append("")
    if inferred:
        lines.append(WORDING["inferred"])
    for conflict in payload["conflicts"]:
        lines.append(WORDING["conflict"].format(
            label=FIELD_LABELS.get(conflict["field"], conflict["field"]),
            values=_conflict_values(conflict)))
    where_field = fields.get("location") or {}
    if where_field.get("candidates"):
        ids = requirements.id_map(gazetteer)
        names = [gazetteer.places[ids[str(c["location_id"])]].display_name
                 for c in where_field["candidates"] if str(c["location_id"]) in ids]
        if names:
            lines.append(WORDING["place_hint"].format(places=" ya ".join(names)))
    if payload["missing"]:
        lines.append(WORDING["missing"].format(
            fields=", ".join(FIELD_LABELS.get(f, f) for f in payload["missing"])))
    if "price_unusual" in payload.get("warnings", ()):
        lines.append(WORDING["price_unusual"])
    if payload["duplicates"]:
        lines.append(WORDING["duplicate"].format(codes=", ".join(
            d["code"] if "code" in d else f"draft {d['draft']}" for d in payload["duplicates"])))
    if fields.get("description", {}).get("removed"):
        lines.append(WORDING["sanitised"].format(n=fields["description"]["removed"]))
    ready = not payload["blockers"]
    lines += ["", WORDING["ready" if ready else "not_ready"]]
    buttons = [(draft_button("save", stored.id, stored.version), "Save")] if ready else []
    buttons += [(draft_button("edit", stored.id, stored.version), "Edit"),
                (draft_button("cancel", stored.id, stored.version), "Cancel")]
    return _buttons(_fit(lines), buttons, cover_url)


def draft_codes(stored):
    """Listing codes a draft card shows (the engine trusts these in the reply)."""
    payload = stored.payload
    codes = {d["code"] for d in payload["duplicates"] if "code" in d}
    return codes | ({payload["proposed_code"]} if payload.get("proposed_code") else set())


# --- update cards ------------------------------------------------------------------------

def change_text(changes):
    """'price PKR 8 crore -> PKR 7.8 crore; status active -> sold' from {field: {from, to}}."""
    parts = []
    for field, change in changes.items():
        if field == "price_pkr":
            parts.append(f"price {cards.price_text(change['from'], None)} → "
                         f"*{cards.price_text(change['to'], None)}*")
        else:
            parts.append(f"status {change['from']} → *{change['to']}*")
    return "; ".join(parts)


def update_card(stored, facts=None, gazetteer=None):
    """The Confirm / Cancel card of a stored update draft. facts: the target property row
    (with cover_url), shown so the staff member sees WHICH listing changes (R-11)."""
    payload = stored.payload
    lines = [WORDING["update"].format(code=payload["target_code"],
                                      changes=change_text(payload["changes"]))]
    if facts is not None:
        lines += [""] + listing_lines(facts, gazetteer)
    if DRAFT_WARNING_NOTE in payload.get("warnings", ()):
        lines.append(WORDING["unusual"])
    buttons = [(draft_button("confirm", stored.id, stored.version), "Confirm"),
               (draft_button("cancel", stored.id, stored.version), "Cancel")]
    return _buttons(_fit(lines), buttons, (facts or {}).get("cover_url"))


def _row_description(item):
    text = " · ".join(x for x in (
        item["type"], item["location"], cards.price_text(item["price_pkr"], item["purpose"]),
        item["status"]) if x)
    return text if len(text) <= whatsapp.ROW_DESCRIPTION_MAX else (
        text[: whatsapp.ROW_DESCRIPTION_MAX - 1] + "…")


def candidates_reply(summaries, changes=None, more=False):
    """A list message "Kaunsi property?" (<= 10 rows, R-11 / Demo Scope 4c). Each row id names
    the listing and the change parsed by the backend (pick:{CODE}:{p=...;s=...}); a tap is
    re-validated by app.staff_actions and only then becomes an update draft."""
    rows = [(pick_row(item["code"], changes or {}), item["code"], _row_description(item))
            for item in summaries[:MAX_CANDIDATE_ROWS]]
    body = WORDING["pick_head"].format(n=len(rows))
    if more:
        body += "\n" + WORDING["pick_more"].format(n=len(rows))
    return whatsapp.list_message(body, "Listings", [(None, rows)])


# --- commit replies (Task 9.2) --------------------------------------------------------------

def saved_reply(code, event_id):
    return _buttons(WORDING["saved"].format(code=code), [(undo_button(event_id), "Undo")])


def updated_reply(code, changes, event_id):
    if set(changes) == {"status"}:
        body = WORDING["marked"].format(
            code=code, status=STATUS_LABELS.get(changes["status"]["to"],
                                                changes["status"]["to"].upper()))
    else:
        body = WORDING["updated"].format(code=code, changes=change_text(changes))
    return _buttons(body, [(undo_button(event_id), "Undo")])


def _undo_changes(event_type, changes):
    if event_type == "created":
        return WORDING["undo_created"]
    return change_text({f: {"from": c["to"], "to": c["from"]} for f, c in changes.items()})


def undo_confirm_card(code, event_type, changes, event_id, version, facts, gazetteer):
    lines = [WORDING["undo_ask"].format(code=code, changes=_undo_changes(event_type, changes))]
    lines += [""] + listing_lines(facts, gazetteer)
    buttons = [(undo_confirm_button("confirm", event_id, version), "Confirm undo"),
               (undo_confirm_button("cancel", event_id, version), "Cancel")]
    return _buttons(_fit(lines), buttons, facts.get("cover_url"))


def undone_reply(code, event_type, changes):
    if event_type == "created":
        return whatsapp.text(WORDING["undone_created"].format(code=code))
    return whatsapp.text(WORDING["undone"].format(
        code=code, changes=_undo_changes(event_type, changes)))


def attach_list(segment_id, photos, targets):
    """"Ye photos kis listing ki hain?" (R-10): targets are (kind 'd' | 'p', id, title,
    description) of this staff member's open drafts and recent listings."""
    rows = [(attach_row(segment_id, kind, target_id), title[: whatsapp.ROW_TITLE_MAX],
             (description or "")[: whatsapp.ROW_DESCRIPTION_MAX] or None)
            for kind, target_id, title, description in targets[:MAX_CANDIDATE_ROWS]]
    return whatsapp.list_message(WORDING["attach_head"].format(n=photos), "Listings",
                                 [(None, rows)])


def attach_confirm_card(code, photos, segment_id, facts, gazetteer):
    lines = [WORDING["attach_ask"].format(code=code, n=photos), ""]
    lines += listing_lines(facts, gazetteer)
    buttons = [(attach_confirm_button("confirm", segment_id, facts["id"], facts["version"]),
                "Confirm"),
               (attach_confirm_button("cancel", segment_id, facts["id"], facts["version"]),
                "Cancel")]
    return _buttons(_fit(lines), buttons, facts.get("cover_url"))


def attached_reply(code, photos):
    return whatsapp.text(WORDING["attached"].format(code=code, n=photos))
