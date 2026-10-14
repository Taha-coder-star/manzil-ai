"""Staff draft and candidate cards (Task 8.2 slice 4c): text only, built from STORED rows.

A card is a plain WhatsApp text message: no buttons, no Save, Edit, Cancel or Confirm
(those are Task 9.2, which also owns the cover image). Facts come from the stored draft
payload and the gazetteer's display names, never from the model. The wording below is
proposed and needs owner approval (docs/STAFF_EXTRACTION.md section 8); NOTICES are the
fixed sentences the engine adds to its approved templates.
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
WORDING = {
    "head": "Draft {n} of {total} · Proposed ID {code}",
    "head_no_code": "Draft {n} of {total}",
    "inferred": "* purpose price se samjha gaya",
    "missing": "Missing: {fields}. Likh kar bhejein.",
    "conflict": "Check - {label}: {values}?",
    "duplicate": "Milti-julti listing pehle se hai: {codes}.",
    "sanitised": "Description se {n} line hata di gayi (instruction jaisi).",
    "photos": "Photos: {n}",
    "photos_multi": "Photos ke liye is card ko reply karke photos bhejein.",
    "ready": "Sab zaroori maloomat mil gayi. (Review: Save abhi available nahi.)",
    "not_ready": "Save ke liye abhi maloomat poori nahi. (Review only.)",
    "update": "{code}: {changes}.",
    "update_note": "Review: tabdeeli abhi save nahi hui.",
    "unusual": "Qeemat purani qeemat se bohat alag hai - dobara check karein.",
    "pick_head": "Kaunsi property? {n} listings milin:",
    "pick_tail": "Listing code likh kar dobara bhejein, jaise #{code} sold.",
}
# Sentences the engine adds as approved templates (app.turn_engine.TEMPLATES).
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
    "staff_update_pick": "Kaunsi listing? Listing code likh kar dobara bhejein.",
    "staff_one_at_a_time": "Ek waqt mein sirf ek listing ki tabdeeli ho sakti hai.",
    "staff_find_unclear": ("Description samajh nahi aayi (jagah, type ya size). Listing code "
                           "ya saaf description likh kar bhejein."),
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


def _conflict_values(conflict):
    return " or ".join(f"'{v['raw']}'" for v in conflict["values"])


def draft_card(stored, total, gazetteer, photos):
    """One text payload for a stored create draft. photos: the segment's photo count (the
    card of a several-draft segment says photos are attached by replying to it)."""
    payload, fields = stored.payload, stored.payload["fields"]
    code = payload.get("proposed_code")
    lines = [WORDING["head"].format(n=stored.ordinal, total=total, code=code) if code
             else WORDING["head_no_code"].format(n=stored.ordinal, total=total)]
    kind, purpose = _value(fields.get("property_type")), _value(fields.get("purpose"))
    star = "*" if (fields.get("purpose") or {}).get("source") == "inferred" else ""
    if kind or purpose:
        lines.append(" · ".join(x for x in (
            (kind or "").upper(), f"{purpose.upper()}{star}" if purpose else "") if x))
    rooms = " / ".join(x for x in (
        f"{_value(fields['bedrooms'])} bed" if "bedrooms" in fields else None,
        f"{_value(fields['bathrooms'])} bath" if "bathrooms" in fields else None) if x)
    where = " · ".join(x for x in (
        _place(fields.get("location"), gazetteer),
        _size(fields["size"]) if "size" in fields else None, rooms or None) if x)
    if where:
        lines.append(where)
    price = cards.price_text(_value(fields.get("price_pkr")), purpose)
    source = SOURCES.get(_value(fields.get("source_type")))
    if price or source:
        lines.append(" · ".join(x for x in (price, source) if x))
    if "description" in fields:
        lines.append(f"Note: {fields['description']['value']}")
    if total == 1 and photos:
        lines.append(WORDING["photos"].format(n=photos))
    elif total > 1 and photos:
        lines.append(WORDING["photos_multi"])
    if star:
        lines.append(WORDING["inferred"])
    for conflict in payload["conflicts"]:
        lines.append(WORDING["conflict"].format(
            label=FIELD_LABELS.get(conflict["field"], conflict["field"]),
            values=_conflict_values(conflict)))
    if payload["missing"]:
        lines.append(WORDING["missing"].format(
            fields=", ".join(FIELD_LABELS.get(f, f) for f in payload["missing"])))
    if payload["duplicates"]:
        lines.append(WORDING["duplicate"].format(codes=", ".join(
            d["code"] if "code" in d else f"draft {d['draft']}" for d in payload["duplicates"])))
    if fields.get("description", {}).get("removed"):
        lines.append(WORDING["sanitised"].format(n=fields["description"]["removed"]))
    lines.append(WORDING["ready" if not payload["blockers"] else "not_ready"])
    return whatsapp.text("\n".join(lines))


def draft_codes(stored):
    """Listing codes a draft card shows (the engine trusts these in the reply)."""
    payload = stored.payload
    codes = {d["code"] for d in payload["duplicates"] if "code" in d}
    return codes | ({payload["proposed_code"]} if payload.get("proposed_code") else set())


def update_card(stored):
    payload = stored.payload
    parts = []
    for field, change in payload["changes"].items():
        if field == "price_pkr":
            old = cards.price_text(change["from"], None)
            new = cards.price_text(change["to"], None)
            parts.append(f"price {old} -> {new}")
        else:
            parts.append(f"status {change['from']} -> {change['to']}")
    lines = [WORDING["update"].format(code=payload["target_code"], changes="; ".join(parts))]
    if DRAFT_WARNING_NOTE in payload.get("warnings", ()):
        lines.append(WORDING["unusual"])
    lines.append(WORDING["update_note"])
    return whatsapp.text("\n".join(lines))


def candidates_reply(summaries):
    """A numbered text list of the listings a description matched (several, or too many)."""
    lines = [WORDING["pick_head"].format(n=len(summaries))]
    for number, item in enumerate(summaries, 1):
        lines.append(f"{number}. {item['code']} · {item['type']} · "
                     f"{item['location'] or '-'} · "
                     f"{cards.price_text(item['price_pkr'], item['purpose'])} · "
                     f"{item['status']}")
    lines.append(WORDING["pick_tail"].format(code=summaries[0]["code"]))
    return whatsapp.text("\n".join(lines))
