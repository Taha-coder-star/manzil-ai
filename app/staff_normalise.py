"""Staff extraction, normalisation (Task 8.2 slice 2; docs/STAFF_EXTRACTION.md
sections 3 and 4; Red-Team R-07, R-12, R-22, R-36).

Pure functions over the verified spans of app.staff_drafts: a rejected span gives no
value; values come from the existing normalisers and the location resolver; the draft's
own text is scanned independently for conflicting values. Nothing here reads or writes
the database. This is a separate module because app.requirements imports app.ai_tools,
which imports app.staff_drafts. Never log spans, values or message text.
"""

import re
from dataclasses import dataclass, replace
from decimal import Decimal

from app import location, normalise, requirements, staff_drafts
from app.staff_drafts import bind_draft_listing

# Bedrooms are required for these types: the CHECK property_active_publishable of
# migration 007 (a test reads the SQL).
BEDROOM_TYPES = frozenset({"house", "apartment", "penthouse", "portion"})
CORE_TYPES = frozenset({"house", "apartment", "penthouse", "portion", "plot", "shop", "office"})
# Required fields, in the order a blocker lists them; area_value is the size.
REQUIRED = ("purpose", "property_type", "location", "area_value", "price_pkr", "bedrooms")

# --- normalisation (slice 2) --------------------------------------------------------------

# Amounts introduced (or followed) by these words are not the asking price (R-12).
_NOT_ASKING = ("advance", "deposit", "token", "booking", "commission", "maintenance",
               "down payment", "downpayment", "installment", "instalment", "qist", "kist",
               "security")
_COVERED = ("covered", "built up", "built-up", "builtup", "constructed")
_DELIMITERS = re.compile(r"[,;\n()]|\.\s")
_ROOM = re.compile(
    r"(?P<n>\d+|[^\W\d_]+)\s*-?\s*(?:(?P<bed>beds?|bedrooms?|bd|br|kamr[ae]|kamray)"
    r"|(?P<bath>baths?|bathrooms?|washrooms?))\b", re.IGNORECASE)
_SOURCES = (
    (r"sub[\s-]*agent", "sub_agent"), (r"developer", "developer"),
    (r"owner[\s-]*direct|direct[\s-]*owner|owner\s+se\s+direct", "owner_direct"),
    (r"\bagent\b|\bdealer\b", "agent_listing"),
)
# shortcut: a run of 10+ digits (with spaces / dashes) is a phone number; a real price
# that long (100 crore +) would be dropped from a description, which is the safe side.
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s-]?){10,}(?!\d)")
_UNSAFE = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bignore\b.*\b(?:instructions?|previous|above|prior|rules)\b", r"\bdisregard\b",
    r"system\s*prompt", r"\byou\s+are\b", r"\btell\s+(?:the\s+)?customers?\b",
    r"\breveal\b", r"\bact\s+as\b", r"\b(?:assistant|system)\s*:", r"\bguarantee",
    r"final\s+price", r"negotiable\s+to", r"mil\s+jayega", r"no\s+commission",
    r"https?://|www\.", r"\S+@\S+\.\S+", r"```",
))
DESCRIPTION_MAX = 600


@dataclass(frozen=True)
class Draft:
    """One normalised draft: trusted values only, what is missing, open conflicts,
    warnings and the span fields that were rejected (they contribute nothing)."""
    ordinal: int
    source_message: int | None
    fields: dict
    missing: tuple
    conflicts: tuple
    warnings: tuple
    rejected: tuple
    duplicates: tuple = ()  # possible duplicates (a warning, never a blocker)
    proposed_code: str | None = None  # advisory; 9.2 allocates the real one

    def blockers(self):
        return save_blockers(self.fields, self.conflicts)


def _tail(text, start):
    return _DELIMITERS.split(text[:start])[-1].casefold()


def _head(text, end):
    words = _DELIMITERS.split(text[end:], 1)[0].casefold().split()
    return words[0] if words else ""


def _amounts(text):
    """[(pkr, raw)] asking-price candidates of a text."""
    out = []
    for m in normalise.find_money(text):
        around = _tail(text, m.start) + " " + _head(text, m.end)
        if not any(word in around for word in _NOT_ASKING):
            out.append((int(m.value.to_integral_value()), m.text))
    return out


def _areas(text, marla_sqft):
    """Area expressions with a stated unit; kanal + marla are one size (in marla).
    Covered / built-up areas are left out."""
    found, out, i = normalise.find_areas(text), [], 0
    while i < len(found):
        m = found[i]
        nxt = found[i + 1] if i + 1 < len(found) else None
        if (m.unit == "kanal" and nxt is not None and nxt.unit == "marla"
                and not text[m.end:nxt.start].strip()):
            value, unit, raw, i = m.value * 20 + nxt.value, "marla", text[m.start:nxt.end], i + 2
        else:
            value, unit, raw, i = m.value, m.unit, m.text, i + 1
        if any(word in _tail(text, m.start) for word in _COVERED):
            continue
        sqft = value * normalise.sqft_per_unit(unit, marla_sqft)
        out.append({"raw": raw, "value": value, "unit": unit, "sqft": sqft})
    return out


def find_rooms(text):
    """{"bed": [(raw, n)], "bath": [(raw, n)]}: counts stated with a bed / bath word."""
    out = {"bed": [], "bath": []}
    for m in _ROOM.finditer(text or ""):
        count = normalise.parse_number(m.group("n"))
        if count is not None and count == count.to_integral_value() and 0 <= count <= 20:
            out["bed" if m.group("bed") else "bath"].append((m.group(0), int(count)))
    return out


def parse_source(span):
    for pattern, value in _SOURCES:
        if re.search(pattern, span or "", re.IGNORECASE):
            return value
    return None


def sanitise_description(text):
    """(clean text, removed units). Units that address the assistant or the customer,
    promise a price or terms, or hold a phone number, link, e-mail or code fence are
    dropped (R-36); the rest is cut at DESCRIPTION_MAX on a word boundary."""
    keep, removed = [], []
    for unit in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        unit = unit.strip()
        if not unit:
            continue
        if _PHONE.search(unit) or any(p.search(unit) for p in _UNSAFE):
            removed.append(unit)
        else:
            keep.append(unit)
    clean = " ".join(" ".join(keep).split())
    if len(clean) > DESCRIPTION_MAX:
        clean = clean[:DESCRIPTION_MAX].rsplit(" ", 1)[0].rstrip()
    return clean, removed


def missing_fields(fields, conflicts=()):
    """The required fields a draft still lacks, from its trusted values only. A field
    in conflict is not missing: the conflict itself blocks."""
    kind = (fields.get("property_type") or {}).get("value")
    where = fields.get("location") or {}
    present = {
        "purpose": "purpose" in fields,
        "property_type": kind is not None,
        "location": where.get("status") == location.RESOLVED and "location_id" in where,
        "area_value": (fields.get("size") or {}).get("value") is not None,
        "price_pkr": (fields.get("price_pkr") or {}).get("value") is not None,
        "bedrooms": "bedrooms" in fields,
    }
    need = [f for f in REQUIRED if f != "bedrooms" or kind in BEDROOM_TYPES]
    disputed = {"area_value" if c["field"] == "size" else c["field"] for c in conflicts}
    return tuple(f for f in need if not present[f] and f not in disputed)


def save_blockers(fields, conflicts):
    """Why a draft cannot be saved: a missing required field, an open conflict, an
    out-of-coverage location. Recomputed from the values every time, never from a
    stored flag (R-07, R-12)."""
    blockers = [f"missing:{f}" for f in missing_fields(fields, conflicts)]
    blockers += [f"conflict:{c['field']}" for c in conflicts]
    if (fields.get("location") or {}).get("status") == location.OUT_OF_COVERAGE:
        blockers.append("out_of_coverage")
    return tuple(blockers)


def _conflict(field, pairs):
    """pairs: [(raw, value)]; the first raw seen for each distinct value is kept."""
    seen = {}
    for raw, value in pairs:
        seen.setdefault(value, raw)
    return {"field": field, "kind": "multiple_values",
            "values": [{"raw": raw, "value": value} for value, raw in seen.items()]}


def _distinct(pairs):
    return len({value for _, value in pairs}) > 1


def _normalise_location(span, gazetteer, city_code):
    result = location.resolve(span, None, gazetteer, city_code=city_code)
    field = {"status": result.status, "raw": span, "method": result.method}
    if result.status == location.RESOLVED:
        node = result.nodes[0]
        if len(result.nodes) == 1 and gazetteer.places[node].level != "city":
            field.update(location_id=node, mode=result.mode)
        else:
            field["reason"] = "too_broad" if len(result.nodes) == 1 else "ambiguous"
    elif result.status == location.AMBIGUOUS:
        field.update(reason="ambiguous", candidates=[
            {"location_id": c.location_id, "score": c.score} for c in result.candidates])
    elif result.status == location.OUT_OF_COVERAGE:
        field["reason"] = "out_of_coverage"
    else:
        field["reason"] = "unknown"
    return field


def _number(value):
    return int(value) if value == value.to_integral_value() else float(value)


def _normalise_size(span, kind, marla_sqft, texts):
    """(size field | None, conflict | None, warnings). A nominal figure beside its
    actual size (250 gaz, actual 272 gaz) is one size: the actual one is stored."""
    expressions = [a for t in texts for a in _areas(t, marla_sqft)]
    stated = _areas(span, marla_sqft) if span else []
    if len(stated) > 1:
        expressions += stated
    yards = [a for a in expressions if a["unit"] == "sq_yd"]
    actual = nominal = None
    for big in yards:
        for small in yards:
            if (small["value"] < big["value"]
                    and normalise.nominal_bucket(big["value"]) == small["value"]):
                actual, nominal = big, small
    distinct = {}
    for a in expressions:
        if a is not nominal:
            distinct.setdefault(int(a["sqft"].to_integral_value()), a["raw"])
    if len(distinct) > 1:
        return None, _conflict("size", [(raw, v) for v, raw in distinct.items()]), ()
    if not span:
        return None, None, ()
    if actual is not None and stated and stated[0] in (actual, nominal):
        area = normalise.parse_area(actual["raw"], property_type=kind, marla_sqft=marla_sqft)
    elif len(stated) == 1 and stated[0]["unit"] == "marla" and "kanal" in span.casefold():
        sqft = stated[0]["sqft"]  # kanal + marla, one size counted in marla
        area = normalise.Area(span, normalise.OK, value=stated[0]["value"], unit="marla",
                              sqft=int(sqft.to_integral_value()), unit_source="stated",
                              nominal_sqyd=normalise.nominal_bucket(sqft / 9), mode="target")
    else:
        area = normalise.parse_area(span, property_type=kind, marla_sqft=marla_sqft)
    if area.status != normalise.OK or area.mode != "target":
        return None, None, ()
    value, unit = area.value, area.unit
    if unit == "acre":
        value, unit = Decimal(area.sqft), "sq_ft"
    nominal_sqyd = int(nominal["value"]) if nominal is not None else area.nominal_sqyd
    basis = _number(Decimal(str(marla_sqft))) if unit in ("marla", "kanal") else None
    field = {"value": _number(value), "unit": unit, "sqft": area.sqft,
             "nominal_sqyd": nominal_sqyd, "raw": span, "unit_source": area.unit_source,
             "marla_sqft": basis}
    return field, None, (("unit_assumed",) if area.unit_source == "default" else ())


def normalise_draft(bound, gazetteer, *, marla_sqft=225, city_code="KHI", peers=None):
    """A BoundDraft -> Draft. Only verified spans give values (a rejected span gives
    none); the draft's own text is scanned independently for conflicting values (R-12).
    peers: prices of live listings of that purpose / type / place, for price_unusual."""
    spans, scan = bound.spans, bound.scan
    fields, conflicts, warnings = {}, [], []

    named = requirements.parse_types(spans.get("property_type", ""))
    kinds = ({k for t in scan for k in requirements.parse_types(t)} & CORE_TYPES) | set(named)
    kind = None
    if len(kinds) > 1:
        conflicts.append(_conflict("property_type", [(k, k) for k in sorted(kinds)]))
    elif len(named) == 1:
        kind = named[0]
        fields["property_type"] = {"value": kind, "raw": spans["property_type"]}

    if "location" in spans:
        fields["location"] = _normalise_location(spans["location"], gazetteer, city_code)

    size, clash, extra = _normalise_size(spans.get("size"), kind, marla_sqft, scan)
    if size:
        fields["size"] = size
    if clash:
        conflicts.append(clash)
    warnings += extra

    stated = _amounts(spans["price"]) if "price" in spans else []
    pairs = [(raw, pkr) for pkr, raw in stated]
    pairs += [(raw, pkr) for t in scan for pkr, raw in _amounts(t)]
    if _distinct(pairs):
        conflicts.append(_conflict("price_pkr", pairs))
    elif len({pkr for pkr, _ in stated}) == 1:
        fields["price_pkr"] = {"value": stated[0][0], "raw": spans["price"], "period": None}

    words = set().union(*(normalise.purposes_in(t) for t in scan))
    if len(words) > 1:
        conflicts.append(_conflict("purpose", [(w, w) for w in sorted(words)]))
    else:
        purpose = normalise.parse_purpose(spans["purpose"]) if "purpose" in spans else None
        if purpose is not None and purpose.status == normalise.OK:
            fields["purpose"] = {"value": purpose.value, "raw": spans["purpose"],
                                 "source": "text"}
        elif "price_pkr" in fields:
            guess = normalise.infer_purpose(fields["price_pkr"]["value"],
                                            normalise.category_of(kind))
            if guess.status == normalise.OK:
                fields["purpose"] = {"value": guess.value, "raw": None, "source": "inferred"}
                warnings.append("purpose_inferred")
    if "price_pkr" in fields and "purpose" in fields:
        fields["price_pkr"]["period"] = normalise.period_for(fields["purpose"]["value"])

    rooms = {k: [r for t in scan for r in find_rooms(t)[k]] for k in ("bed", "bath")}
    for key, field in (("bed", "bedrooms"), ("bath", "bathrooms")):
        if _distinct(rooms[key]):
            conflicts.append(_conflict(field, rooms[key]))
        elif field in spans:
            count = requirements.parse_count(spans[field])
            if count is not None and kind is not None and kind not in BEDROOM_TYPES:
                warnings.append("bedrooms_ignored")
            elif count is not None:
                fields[field] = {"value": count, "raw": spans[field]}

    if "construction" in spans:
        every = requirements.parse_vocab("construction", " ".join(scan)) or []
        named = requirements.parse_vocab("construction", spans["construction"]) or []
        if len(set(every) | set(named)) > 1:
            conflicts.append(_conflict("construction_status", [(v, v) for v in every]))
        elif named:
            fields["construction_status"] = {"value": named[0], "raw": spans["construction"]}
    if parse_source(spans.get("source")):
        fields["source_type"] = {"value": parse_source(spans["source"]), "raw": spans["source"]}
    if requirements.parse_count(spans.get("parking", "")) is not None:
        fields["parking_spaces"] = {"value": requirements.parse_count(spans["parking"]),
                                    "raw": spans["parking"]}
    if "description" in spans:
        clean, removed = sanitise_description(spans["description"])
        if removed:
            warnings.append("description_sanitised")
        if clean:
            fields["description"] = {"value": clean, "raw": spans["description"],
                                     "removed": len(removed)}

    price = (fields.get("price_pkr") or {}).get("value")
    if price and peers and not (requirements.PLAUSIBLE_LOW * min(peers)
                                <= price <= requirements.PLAUSIBLE_HIGH * max(peers)):
        warnings.append("price_unusual")
    return Draft(bound.ordinal, bound.source_message, fields,
                 missing_fields(fields, conflicts), tuple(conflicts), tuple(dict.fromkeys(warnings)), bound.rejected)


def normalise_drafts(args, staff, gazetteer, *, marla_sqft=225, city_code="KHI", peers=None,
                     listings=None):
    """draft_listing args -> {"drafts": [Draft]} or {"error": code} (spans as in slice 1).
    listings: the agency's own listing rows (load_listings); given, the drafts also get
    duplicate warnings and proposed codes."""
    result = bind_draft_listing(args, staff)
    if "error" in result:
        return result
    drafts = [normalise_draft(d, gazetteer, marla_sqft=marla_sqft, city_code=city_code,
                              peers=peers) for d in result["drafts"]]
    return {"drafts": annotate(drafts, listings, gazetteer) if listings is not None
            else drafts}


# --- duplicates and advisory codes (slice 3) -----------------------------------------------

# RFC section 5 step 4: "same location + size + price +- tolerance". Fixed here; the
# benchmark's ST-01 gold is NOT a reason to tune them (docs/STAFF_EXTRACTION.md, Q1).
DUP_SIZE = Decimal("0.10")
DUP_PRICE = Decimal("0.05")
LIVE_STATUSES = frozenset({"active", "reserved", "on_hold"})
FIRST_SEQ = 101  # the first code of a prefix, as in the demo data
_CODE_NUMBER = re.compile(r"^([A-Z0-9]+)-([0-9]+)$")


def _row_of(draft):
    """A draft in the shape of a listing row, or None when a compared field is missing
    (the duplicate check is then skipped, not answered "no duplicate")."""
    f = draft.fields
    node = (f.get("location") or {}).get("location_id")
    size, price = f.get("size"), (f.get("price_pkr") or {}).get("value")
    kind, purpose = (f.get("property_type") or {}).get("value"), (f.get("purpose") or {}).get("value")
    if not all((purpose, kind, node, size, price)):
        return None
    nominal = size.get("nominal_sqyd")
    return {"purpose": purpose, "property_type": kind, "location_id": node,
            "sqft": Decimal(nominal) * 9 if nominal else Decimal(str(size["sqft"])),
            "price_pkr": price, "bedrooms": (f.get("bedrooms") or {}).get("value")}


def _match(mine, row, gazetteer):
    """The reasons `row` is a possible duplicate of `mine` (all must hold), else []."""
    if (row["purpose"] != mine["purpose"] or row["property_type"] != mine["property_type"]
            or row["location_id"] is None or row["sqft"] is None or row["price_pkr"] is None):
        return []
    here, there = mine["location_id"], row["location_id"]
    if not (gazetteer.is_within(there, here) or gazetteer.is_within(here, there)):
        return []
    if abs(Decimal(str(row["sqft"])) - mine["sqft"]) > DUP_SIZE * mine["sqft"]:
        return []
    if abs(Decimal(row["price_pkr"]) - mine["price_pkr"]) > DUP_PRICE * mine["price_pkr"]:
        return []
    reasons = ["location", "size", "price"]
    if row["bedrooms"] is not None and mine["bedrooms"] is not None:
        if row["bedrooms"] != mine["bedrooms"]:
            return []
        reasons.append("beds")
    return reasons


def find_duplicates(draft, listings, gazetteer):
    """Possible duplicates of a draft among the agency's live listings (`listings` are the
    agency's own rows: code, status, purpose, property_type, location_id, sqft, price_pkr,
    bedrooms; sold, rented, withdrawn and archived ones are history)."""
    mine = _row_of(draft)
    if mine is None:
        return []
    found = []
    for row in listings:
        reasons = _match(mine, row, gazetteer) if row["status"] in LIVE_STATUSES else []
        if reasons:
            found.append({"code": row["code"], "status": row["status"], "reasons": reasons})
    return found


def proposed_codes(drafts, listing_codes, gazetteer):
    """{ordinal: code}: the nearest coded ancestor of each draft's location, then the
    highest number over EVERY listing code of the agency (any status) + 1, the first
    being FIRST_SEQ. Drafts of one segment are numbered one after the other. Advisory:
    nothing is reserved, and 9.2 allocates the real code at commit."""
    top = {}
    for code in listing_codes:
        m = _CODE_NUMBER.match(code or "")
        if m:
            top[m.group(1)] = max(top.get(m.group(1), 0), int(m.group(2)))
    out = {}
    for draft in drafts:
        node = (draft.fields.get("location") or {}).get("location_id")
        prefix = gazetteer.code_of(node) if node is not None else None
        if prefix is None:
            continue
        top[prefix] = max(top.get(prefix, 0), FIRST_SEQ - 1) + 1
        out[draft.ordinal] = f"{prefix}-{top[prefix]}"
    return out


def annotate(drafts, listings, gazetteer):
    """The drafts with their duplicate warnings (against the listings and against each
    other) and proposed codes. Duplicates never change the save blockers."""
    codes = proposed_codes(drafts, [row["code"] for row in listings], gazetteer)
    rows = {d.ordinal: _row_of(d) for d in drafts}
    out = []
    for draft in drafts:
        found = find_duplicates(draft, listings, gazetteer)
        for other in drafts:
            if other.ordinal != draft.ordinal and rows[draft.ordinal] and rows[other.ordinal]:
                reasons = _match(rows[draft.ordinal], rows[other.ordinal], gazetteer)
                if reasons:
                    found.append({"draft": other.ordinal, "reasons": reasons})
        out.append(replace(
            draft, duplicates=tuple(found), proposed_code=codes.get(draft.ordinal),
            warnings=draft.warnings + (("duplicate",) if found else ())))
    return out


def to_payload(draft):
    """The JSON stored in listing_draft.payload (schema 1): the normalised result and the
    save blockers, enough to rebuild a card from the stored row alone. Values only; no
    buttons, no Save action (9.2)."""
    return {"schema": 1, "fields": draft.fields, "missing": list(draft.missing),
            "conflicts": list(draft.conflicts), "warnings": list(draft.warnings),
            "duplicates": list(draft.duplicates), "proposed_code": draft.proposed_code,
            "rejected": list(draft.rejected), "blockers": list(draft.blockers()),
            "source_message": draft.source_message}


# --- find_listings and propose_update rules (slice 4b) ----------------------------------

FIND_SIZE = Decimal("0.05")   # a described size matches within 5 %: BTK-102 (235) is not "250"
FIND_PRICE = Decimal("0.10")
FIND_LIMIT = 10
_STATUS_WORDS = (
    (r"\bsold\b|\bbik\s*gay[ai]\b", "sold"), (r"\brent(?:ed)?\b", "rented"),
    (r"\breserv", "reserved"), (r"\bwithdraw|\bhata\b", "withdrawn"),
    (r"\bon[\s-]*hold\b|\bhold\b", "on_hold"), (r"\bavailable\b|\bactive\b|\bwapas\b", "active"),
)


def parse_status(span):
    """The one status a span names, else None (none, or two different ones)."""
    found = {value for pattern, value in _STATUS_WORDS
             if re.search(pattern, span or "", re.IGNORECASE)}
    return next(iter(found)) if len(found) == 1 else None


def _criterion_sqft(size):
    return Decimal(size["nominal_sqyd"]) * 9 if size.get("nominal_sqyd") else Decimal(
        str(size["sqft"]))


def find_candidates(criteria, rows, gazetteer, *, marla_sqft=225, city_code="KHI"):
    """Listings a staff description refers to. `criteria` are verified spans (code, location,
    property_type, purpose, size, price, bedrooms); rows are the agency's listings. A listing
    code is a direct target in any status; otherwise the live listings that match every
    criterion, active first, at most FIND_LIMIT. {"status": none | single | several |
    too_many | direct, "candidates": [row]} or {"error": ...}. Never picks one of several."""
    if "code" in criteria:
        match = staff_drafts.CODE.fullmatch(criteria["code"].strip())
        if match is None:
            return {"error": "invalid_code"}
        hit = [r for r in rows if r["code"] == match.group(1).upper()]
        return {"status": "direct" if hit else "none", "candidates": hit}
    node = kind = purpose = size = price = beds = None
    if "location" in criteria:
        field = _normalise_location(criteria["location"], gazetteer, city_code)
        node = field.get("location_id")
        if node is None and field["status"] == location.AMBIGUOUS \
                and len(field.get("candidates", ())) == 1:
            node = field["candidates"][0]["location_id"]  # one place, matched by similarity
        if node is None and field.get("reason") != "too_broad":
            return {"error": "location_unclear", "candidates": [
                c["location_id"] for c in field.get("candidates", ())]}
    if "property_type" in criteria:
        types = requirements.parse_types(criteria["property_type"])
        if len(types) != 1:
            return {"error": "type_unclear"}
        kind = types[0]
    if "purpose" in criteria:
        found = normalise.parse_purpose(criteria["purpose"])
        if found.status != normalise.OK:
            return {"error": "purpose_unclear"}
        purpose = found.value
    if "size" in criteria:
        size, _, _ = _normalise_size(criteria["size"], kind, marla_sqft, [criteria["size"]])
        if size is None:
            return {"error": "size_unclear"}
    if "price" in criteria:
        amounts = {pkr for pkr, _ in _amounts(criteria["price"])}
        if len(amounts) != 1:
            return {"error": "price_unclear"}
        price = amounts.pop()
    if "bedrooms" in criteria:
        beds = requirements.parse_count(criteria["bedrooms"])
        if beds is None:
            return {"error": "bedrooms_unclear"}
    wanted = _criterion_sqft(size) if size else None
    matches = []
    for row in rows:
        if row["status"] not in LIVE_STATUSES:
            continue
        if node is not None and not (row["location_id"] is not None
                                     and gazetteer.is_within(row["location_id"], node)):
            continue
        if kind is not None and row["property_type"] != kind:
            continue
        if purpose is not None and row["purpose"] != purpose:
            continue
        if wanted is not None and (row["sqft"] is None
                                   or abs(Decimal(str(row["sqft"])) - wanted) > FIND_SIZE * wanted):
            continue
        if price is not None and (row["price_pkr"] is None
                                  or abs(Decimal(row["price_pkr"]) - price) > FIND_PRICE * price):
            continue
        if beds is not None and row["bedrooms"] != beds:
            continue
        matches.append(row)
    matches.sort(key=lambda r: (r["status"] != "active", r["code"]))
    status = ("none" if not matches else "single" if len(matches) == 1
              else "too_many" if len(matches) > FIND_LIMIT else "several")
    return {"status": status, "candidates": matches[:FIND_LIMIT]}


def check_update(prop, changes):
    """Validate a proposed change of one listing: `prop` is its row (status, purpose,
    price_pkr), `changes` {"price_pkr": int, "status": str}. Returns {"changes": {field:
    {"from", "to"}}, "warnings": [...]} or {"error": code}. A live listing may move to any
    other status; a closed one only back to active; a sold listing must be a sale, a rented
    one a rent. Nothing is written here."""
    live = prop["status"] in LIVE_STATUSES
    out, warnings = {}, []
    if "price_pkr" in changes:
        new, old = changes["price_pkr"], prop["price_pkr"]
        if not isinstance(new, int) or new <= 0:
            return {"error": "bad_amount"}
        if new == old:
            return {"error": "no_change"}
        if not live:
            return {"error": "not_live"}
        if old and not (Decimal("0.3") <= Decimal(new) / Decimal(old) <= 3):
            warnings.append("price_unusual")
        out["price_pkr"] = {"from": old, "to": new}
    if "status" in changes:
        new = changes["status"]
        if new == prop["status"]:
            return {"error": "no_change"}
        if not live and new != "active":
            return {"error": "not_live"}
        if new == "sold" and prop["purpose"] != "sale":
            return {"error": "sold_needs_sale"}
        if new == "rented" and prop["purpose"] != "rent":
            return {"error": "rented_needs_rent"}
        out["status"] = {"from": prop["status"], "to": new}
    return {"changes": out, "warnings": warnings}
