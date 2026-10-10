"""Task 5.3 benchmark set (bench/): offline validation of the R16 section 4 gold data.

The set is 30 customer texts, 30 customer voice notes (+ 5 unscored controls)
and 20 staff messages, each with the input, gold extraction (R17 requirement
slots), expected tool calls and expected outcome over the demo dataset
(demo/, Task 4.3). Format and recording protocol: docs/BENCHMARK.md.

The checks here keep the gold data honest: category and coverage counts from
R16, slot names and raw spans (R-22: spans are substrings of the user text or
transcript), customer tools vs staff tools, and expected result IDs against
the demo listings. The result checks use explicit, documented filters (R17
tolerances and the ladder bounds) to confirm that every expected ID fits the
request and that no clear match is missing. They are not the search engine
and must not be used as one.

    python -m app.benchmark [bench_dir]
"""

import json
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

from app import demo_dataset

BENCH_DIR = Path(__file__).resolve().parent.parent / "bench"
FILES = {
    "customer_text": "customer_text.json",
    "customer_voice": "customer_voice.json",
    "control": "voice_controls.json",
    "staff": "staff.json",
}
ID_PREFIXES = {"customer_text": "CT-", "customer_voice": "CV-", "control": "VC-", "staff": "ST-"}

# R16 4.1-4.3 make-up (category -> R16 group).
TEXT_GROUPS = {
    "english": "english", "roman_urdu": "roman_urdu", "mixed": "mixed",
    "urdu_script": "urdu_script", "incomplete": "incomplete_ambiguous",
    "ambiguous": "incomplete_ambiguous", "follow_up": "follow_up_handoff",
    "handoff": "follow_up_handoff",
}
TEXT_COUNTS = {"english": 5, "roman_urdu": 7, "mixed": 7, "urdu_script": 2,
               "incomplete_ambiguous": 5, "follow_up_handoff": 4}
VOICE_COUNTS = {"english": 5, "roman_urdu": 10, "mixed": 10, "ambiguous": 3, "handoff": 2}
VOICE_PAIRED = 12
CONTROL_COUNTS = {"silent": 1, "near_silent": 1, "music": 1, "other_language": 1,
                  "over_120_s": 1}
STAFF_COUNTS = {"new_full": 6, "new_missing": 3, "update_by_id": 4, "update_single_match": 2,
                "update_multiple_matches": 3, "bahria_marla": 2}
# R16 4.1 coverage targets across the 30 texts.
MIN_PRICES, MIN_UNITS, MIN_LOCATIONS = 12, 10, 15
MIN_PURPOSE_EXPLICIT, MIN_PURPOSE_INFERRED = 8, 4
PRICE_WORDS = ("lakh", "crore", "sawa", "saade", "dedh", "dhai", "lac", "cr")
# R16 4.2 recording matrix.
MIN_SPEAKERS = 6
PHONES = {"android_low", "android_mid", "iphone"}
NOISE_COUNTS = {"quiet": 15, "moderate": 10, "street": 5}
VOICE_SECONDS = (3, 45)
LANGUAGES = {"en", "roman_urdu", "mixed", "urdu_script"}

# Migration 006 requirement_state slots (R17) and patch ops.
SLOTS = {
    "purpose", "category", "property_types", "location", "budget", "previous_budget", "size",
    "beds_min", "baths_min", "construction", "possession", "investment", "soft_prefs",
    "unmapped_prefs", "excluded_property_ids",
}
OPS = {"set", "add", "remove", "clear", "relative"}
SOURCES = {"text", "voice", "inferred", "default"}
LOCATION_STATUSES = {"RESOLVED", "AMBIGUOUS", "UNKNOWN", "OUT_OF_COVERAGE"}
BUDGET_MODES = {"max", "around"}
PERIODS = {"sale": "one_time", "rent": "monthly"}
SQFT_PER_UNIT = {"sq_ft": 1, "sq_yd": 9, "marla": 225, "kanal": 4500}

# RFC tool sets: the LLM never calls commit_change (a button does).
CUSTOMER_TOOLS = {"update_requirements", "search_properties", "send_property_photos",
                  "request_handoff"}
STAFF_TOOLS = {"draft_listing", "find_listings", "propose_update", "list_my_listings"}
CUSTOMER_OUTCOMES = {"results", "no_match", "clarify", "photos", "handoff"}
CONTROL_OUTCOMES = {"fallback", "truncated"}
STAFF_OUTCOMES = {"draft", "update", "disambiguate"}

# R17 search tolerances; the ladder adds budget +10 %, size +/-20 %, neighbours, beds -1.
PRICE_TOLERANCE = Decimal("1.05")
PRICE_FLOOR = Decimal("0.9")
LADDER_BUDGET = Decimal("1.10")
SQYD_TYPES = {"house", "plot", "portion"}
SIZE_TOLERANCE = {True: Decimal("0.10"), False: Decimal("0.15")}
LADDER_SIZE = Decimal("0.20")

# Save minimum for a draft (CLAUDE.md section 9).
REQUIRED_FIELDS = ("purpose", "property_type", "location_key", "area_value", "price_pkr")
RESIDENTIAL = {"house", "portion", "apartment", "penthouse"}
DRAFT_FIELDS = {
    "purpose", "property_type", "location_key", "price_pkr", "area_value", "area_unit",
    "size_nominal_sqyd", "bedrooms", "bathrooms", "construction_status", "source_type",
    "parking_spaces",
}
UPDATE_FIELDS = {"price_pkr", "status"}
PHONE_LIKE = re.compile(r"\d{10,}")


class BenchmarkError(Exception):
    pass


def load(directory=BENCH_DIR):
    directory = Path(directory)
    data = {}
    for set_name, file_name in FILES.items():
        items = json.loads((directory / file_name).read_text("utf-8"))
        if not isinstance(items, list):
            raise BenchmarkError(f"{file_name} must be a JSON list")
        data[set_name] = items
    return data


def load_demo(directory=demo_dataset.DEMO_DIR):
    raw = demo_dataset.load(directory)
    errors = []
    locations = {row["key"]: row for row in raw["locations.csv"]}
    properties = {}
    for row in raw["properties.csv"]:
        item = demo_dataset.parse_property(row, errors)
        properties[item["listing_code"]] = item
    pairs = [(r["location_a_key"], r["location_b_key"]) for r in raw["location_neighbors.csv"]]
    if errors:
        raise BenchmarkError(f"demo dataset: {errors[0]}")
    return {"locations": locations, "properties": properties, "pairs": pairs}


# --- helpers -------------------------------------------------------------------------

def _spoken(item):
    """The text that raw spans must come from (R-22)."""
    if item["set"] in ("customer_voice", "control"):
        return (item.get("gold_transcript") or {}).get("roman") or ""
    return item.get("input") or ""


def _in_text(span, text):
    return isinstance(span, str) and span.strip() != "" and span.lower() in text.lower()


def area_code(locations, key):
    for node in demo_dataset.ancestors(locations, key):
        if locations[node]["code"]:
            return locations[node]["code"]
    return None


def _listing_sqft(listing):
    if listing["size_nominal_sqyd"] is not None:
        return Decimal(listing["size_nominal_sqyd"]) * 9
    if listing["area_value"] is None or listing["area_unit"] is None:
        return None
    return Decimal(listing["area_value"]) * SQFT_PER_UNIT[listing["area_unit"]]


def matches(demo, listing, search, *, relaxed):
    """Does a listing fit a search under the R17 tolerances (strict) or the ladder
    bounds (relaxed)? A consistency check for gold IDs, not a search engine."""
    if listing["status"] != "active" or listing["purpose"] != search["purpose"]:
        return False
    if listing["listing_code"] in search.get("exclude", ()):
        return False
    types = search.get("property_types")
    if types and listing["property_type"] not in types:
        return False
    nodes = set(search["locations"])
    if relaxed:
        for key in list(nodes):
            nodes |= demo_dataset.neighbours_of(demo["pairs"], key)
    if not demo_dataset._in_locations(demo["locations"], listing, nodes):
        return False
    price = Decimal(listing["price_pkr"])
    if search.get("budget_max") is not None:
        limit = Decimal(search["budget_max"]) * PRICE_TOLERANCE
        if price > limit * (LADDER_BUDGET if relaxed else 1):
            return False
    if search.get("budget_min") is not None and price < Decimal(search["budget_min"]) * PRICE_FLOOR:
        return False
    wanted = search.get("size_sqft")
    if search.get("size_sqyd") is not None:
        wanted = search["size_sqyd"] * 9
    if wanted is not None:
        actual = _listing_sqft(listing)
        tolerance = LADDER_SIZE if relaxed else SIZE_TOLERANCE[
            listing["property_type"] in SQYD_TYPES]
        if actual is None or abs(actual - wanted) > Decimal(wanted) * tolerance:
            return False
    beds = search.get("beds_min")
    return beds is None or (
        listing["bedrooms"] is not None and listing["bedrooms"] >= beds - (1 if relaxed else 0)
    )


# --- checks --------------------------------------------------------------------------

def check_counts(data, errors):
    ids = Counter(item.get("id") for items in data.values() for item in items)
    for item_id, count in ids.items():
        if count > 1:
            errors.append(f"duplicate id {item_id}")
    for set_name, items in data.items():
        for item in items:
            if item.get("set") != set_name or not str(item.get("id", "")).startswith(
                    ID_PREFIXES[set_name]):
                errors.append(f"{item.get('id')}: wrong set or id prefix for {set_name}")

    def compare(name, actual, expected):
        if actual != expected:
            errors.append(f"{name} counts {dict(actual)} != R16 {expected}")

    compare("customer_text", Counter(TEXT_GROUPS.get(i["category"], i["category"])
                                     for i in data["customer_text"]), TEXT_COUNTS)
    compare("customer_voice", Counter(i["category"] for i in data["customer_voice"]), VOICE_COUNTS)
    compare("control", Counter(i["category"] for i in data["control"]), CONTROL_COUNTS)
    compare("staff", Counter(i["category"] for i in data["staff"]), STAFF_COUNTS)
    for item in data["customer_text"] + data["customer_voice"]:
        if item.get("language") not in LANGUAGES:
            errors.append(f"{item['id']}: language {item.get('language')!r}")


def check_text_coverage(data, errors):
    texts = data["customer_text"]

    def having(slot):
        return [i for i in texts if (i["gold_extraction"].get(slot) or {}).get("raw")]

    prices, units, places = having("budget"), having("size"), having("location")
    explicit = [i for i in texts if (i["gold_extraction"].get("purpose") or {}).get("source") == "text"]
    inferred = [i for i in texts
                if (i["gold_extraction"].get("purpose") or {}).get("source") == "inferred"]
    for name, found, minimum in [
        ("prices", prices, MIN_PRICES), ("units", units, MIN_UNITS),
        ("locations", places, MIN_LOCATIONS), ("explicit purpose", explicit, MIN_PURPOSE_EXPLICIT),
        ("inferred purpose", inferred, MIN_PURPOSE_INFERRED),
    ]:
        if len(found) < minimum:
            errors.append(f"customer_text coverage: {len(found)} {name} < {minimum}")
    spans = " ".join(i["gold_extraction"]["budget"]["raw"].lower() for i in prices)
    for word in PRICE_WORDS:
        if not re.search(rf"(\d|\b){word}\b", spans):
            errors.append(f"customer_text coverage: no price written with {word!r}")
    kinds = {i["gold_extraction"]["size"]["unit"] for i in units}
    if kinds != set(SQFT_PER_UNIT):
        errors.append(f"customer_text coverage: units {sorted(kinds)} miss some of "
                      f"{sorted(SQFT_PER_UNIT)}")


def check_voice_matrix(data, errors):
    voice, texts = data["customer_voice"], {i["id"]: i for i in data["customer_text"]}
    paired = [i for i in voice if i.get("paired_with")]
    if len(paired) != VOICE_PAIRED:
        errors.append(f"customer_voice: {len(paired)} paired items != {VOICE_PAIRED}")
    for item in paired:
        if item["paired_with"] not in texts:
            errors.append(f"{item['id']}: paired_with {item['paired_with']} is not a text item")
    recordings = [i.get("recording") or {} for i in voice]
    if len({r.get("speaker") for r in recordings}) < MIN_SPEAKERS:
        errors.append(f"customer_voice: fewer than {MIN_SPEAKERS} speakers")
    if {r.get("phone") for r in recordings} != PHONES:
        errors.append(f"customer_voice: phones must be exactly {sorted(PHONES)}")
    noise = Counter(r.get("noise") for r in recordings)
    if noise != NOISE_COUNTS:
        errors.append(f"customer_voice: noise {dict(noise)} != {NOISE_COUNTS}")
    for item, rec in zip(voice, recordings, strict=True):
        seconds = rec.get("target_seconds")
        if not isinstance(seconds, int) or not VOICE_SECONDS[0] <= seconds <= VOICE_SECONDS[1]:
            errors.append(f"{item['id']}: target_seconds {seconds!r} outside {VOICE_SECONDS}")
        transcript = item.get("gold_transcript") or {}
        if not transcript.get("roman") or not transcript.get("native"):
            errors.append(f"{item['id']}: gold transcript needs roman and native script")
        if item.get("script") != transcript.get("roman"):
            errors.append(f"{item['id']}: script and roman transcript differ")
    for item in data["control"]:
        if item["gold_extraction"] or item["expected_tools"]:
            errors.append(f"{item['id']}: a control expects no requirements and no tools")
        rec = item.get("recording") or {}
        if rec.get("phone") not in PHONES or not rec.get("speaker"):
            errors.append(f"{item['id']}: recording needs a speaker and a phone class")


def check_slots(demo, item, errors):
    where, text = item["id"], _spoken(item)
    gx = item.get("gold_extraction") or {}
    for slot, value in gx.items():
        if slot not in SLOTS:
            errors.append(f"{where}: unknown slot {slot}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{where}: slot {slot} must be an object")
            continue
        raw = value.get("raw")
        if raw is not None and not _in_text(raw, text):
            errors.append(f"{where}: {slot} raw span {raw!r} is not in the input")
        if value.get("source") is not None and value["source"] not in SOURCES:
            errors.append(f"{where}: {slot} source {value['source']!r}")
        if value.get("op") is not None and value["op"] not in OPS:
            errors.append(f"{where}: {slot} op {value['op']!r}")
    purpose = gx.get("purpose")
    if purpose and (purpose.get("value") not in PERIODS or
                    (purpose.get("raw") is None) != (purpose.get("source") == "inferred")):
        errors.append(f"{where}: purpose must be sale / rent and inferred exactly when no raw span")
    location = gx.get("location")
    if location:
        status = location.get("status", "RESOLVED")
        keys, candidates = location.get("value") or [], location.get("candidates") or []
        if status not in LOCATION_STATUSES:
            errors.append(f"{where}: location status {status!r}")
        if status == "AMBIGUOUS" and (keys or len(candidates) < 2):
            errors.append(f"{where}: an AMBIGUOUS location has no value and >= 2 candidates")
        if status == "RESOLVED" and not keys:
            errors.append(f"{where}: a RESOLVED location needs a key")
        for key in keys + candidates:
            if key not in demo["locations"]:
                errors.append(f"{where}: unknown location {key}")
    budget = gx.get("budget")
    if budget and budget.get("op") != "relative":
        if budget.get("mode") not in BUDGET_MODES:
            errors.append(f"{where}: budget mode {budget.get('mode')!r}")
        lo, hi = budget.get("min"), budget.get("max")
        if hi is None or (lo is not None and lo > hi):
            errors.append(f"{where}: budget needs max >= min")
        if purpose and budget.get("period") != PERIODS.get(purpose.get("value")):
            errors.append(f"{where}: budget period does not follow purpose")
    size = gx.get("size")
    if size:
        unit = size.get("unit")
        if unit not in SQFT_PER_UNIT or not isinstance(size.get("value"), int | float):
            errors.append(f"{where}: size needs a number and a unit")
        elif size.get("sqft") != size["value"] * SQFT_PER_UNIT[unit]:
            errors.append(f"{where}: size sqft {size.get('sqft')} != {size['value']} {unit}")


def check_tools(item, errors):
    allowed = STAFF_TOOLS if item["set"] == "staff" else CUSTOMER_TOOLS
    text = _spoken(item) if item["set"] != "staff" else item["input"]
    for tool in item.get("expected_tools") or []:
        name = tool.get("name")
        if name not in allowed:
            errors.append(f"{item['id']}: tool {name!r} is not a {item['set']} tool")
        if name == "update_requirements":
            for op in tool["args"].get("patch") or []:
                if op.get("slot") not in SLOTS or op.get("op") not in OPS:
                    errors.append(f"{item['id']}: bad patch op {op}")
                if not _in_text(op.get("raw_span"), text):
                    errors.append(f"{item['id']}: patch span {op.get('raw_span')!r} not in input")


def _active_code(demo, code, extra=()):
    listing = demo["properties"].get(code)
    return code in extra or (listing is not None and listing["status"] == "active")


def check_customer_outcome(demo, item, by_id, errors):
    where, outcome = item["id"], item["expected_outcome"]
    kind = outcome.get("type")
    allowed = CONTROL_OUTCOMES if item["set"] == "control" else CUSTOMER_OUTCOMES
    if kind not in allowed:
        errors.append(f"{where}: outcome {kind!r}")
        return
    if kind in ("results", "no_match"):
        search = outcome.get("search") or {}
        for key in search.get("locations") or []:
            if key not in demo["locations"]:
                errors.append(f"{where}: unknown search location {key}")
        if not item.get("context"):
            gx = item["gold_extraction"]
            budget = gx.get("budget") or {}
            if (search.get("purpose") != (gx.get("purpose") or {}).get("value")
                    or search.get("locations") != (gx.get("location") or {}).get("value")
                    or search.get("budget_max") != budget.get("max")
                    or search.get("budget_min") != budget.get("min")):
                errors.append(f"{where}: outcome search does not follow the gold extraction")
        strict = {code for code, listing in demo["properties"].items()
                  if matches(demo, listing, search, relaxed=False)}
        if kind == "no_match":
            if strict:
                errors.append(f"{where}: no_match but {sorted(strict)} fit the request")
            return
        ids, extra = set(outcome.get("ids") or []), set(outcome.get("allowed") or [])
        if not ids or ids & extra:
            errors.append(f"{where}: results need ids, disjoint from allowed")
        for code in sorted(ids | extra):
            listing = demo["properties"].get(code)
            if listing is None or listing["status"] != "active":
                errors.append(f"{where}: {code} is not an active demo listing")
            elif not matches(demo, listing, search, relaxed=True):
                errors.append(f"{where}: {code} does not fit the request, even relaxed")
        missing = strict - ids - extra
        if missing:
            errors.append(f"{where}: clear matches {sorted(missing)} are not expected")
    elif kind == "clarify":
        if not outcome.get("slots") or not set(outcome["slots"]) <= SLOTS:
            errors.append(f"{where}: clarify needs requirement slots")
        for key in outcome.get("candidates") or []:
            if key not in demo["locations"]:
                errors.append(f"{where}: unknown candidate {key}")
    elif kind == "photos":
        shown = (item.get("context") or {}).get("shown") or []
        position = item["expected_tools"][0]["args"]["ref"]["position"]
        if not 1 <= position <= len(shown) or shown[position - 1] != outcome.get("listing"):
            errors.append(f"{where}: photos listing is not shown position {position}")
    context = item.get("context")
    if context:
        if context.get("after") not in by_id:
            errors.append(f"{where}: context.after {context.get('after')!r} is unknown")
        for code in context.get("shown") or []:
            if not _active_code(demo, code):
                errors.append(f"{where}: shown {code} is not an active listing")


def check_staff_outcome(demo, item, created, errors):
    where, outcome = item["id"], item["expected_outcome"]
    kind = outcome.get("type")
    if kind not in STAFF_OUTCOMES:
        errors.append(f"{where}: outcome {kind!r}")
        return
    if kind == "draft":
        fields = outcome.get("fields") or {}
        unknown = set(fields) - DRAFT_FIELDS
        if unknown:
            errors.append(f"{where}: unknown draft fields {sorted(unknown)}")
        enums = {"purpose": demo_dataset.PURPOSES, "property_type": demo_dataset.PROPERTY_TYPES,
                 "area_unit": demo_dataset.AREA_UNITS,
                 "construction_status": demo_dataset.CONSTRUCTION,
                 "source_type": demo_dataset.SOURCE_TYPES}
        for name, values in enums.items():
            if name in fields and fields[name] not in values:
                errors.append(f"{where}: {name} {fields[name]!r}")
        if "location_key" in fields and fields["location_key"] not in demo["locations"]:
            errors.append(f"{where}: unknown location {fields['location_key']}")
        required = set(REQUIRED_FIELDS)
        if fields.get("property_type") in RESIDENTIAL:
            required.add("bedrooms")
        missing = sorted(required - set(fields))
        if missing != sorted(outcome.get("missing") or []):
            errors.append(f"{where}: missing should be {missing}")
        if outcome.get("save_allowed") != (not missing):
            errors.append(f"{where}: Save only when nothing required is missing")
        unit, value = fields.get("area_unit"), fields.get("area_value")
        nominal = fields.get("size_nominal_sqyd")
        if nominal is not None and unit in SQFT_PER_UNIT and value is not None:
            sqyd = Decimal(value) * SQFT_PER_UNIT[unit] / 9
            if unit != "sq_yd" and sqyd != nominal:
                errors.append(f"{where}: nominal {nominal} sq yd != {value} {unit}")
        for code in outcome.get("duplicate_of") or []:
            if code not in demo["properties"]:
                errors.append(f"{where}: unknown duplicate {code}")
        code = outcome.get("expected_code")
        if code:
            prefix = area_code(demo["locations"], fields.get("location_key"))
            if not code.startswith(f"{prefix}-") or code in demo["properties"]:
                errors.append(f"{where}: expected code {code} must be a new {prefix}-n code")
            created.add(code)
    elif kind == "update":
        listing = demo["properties"].get(outcome.get("listing"))
        if listing is None:
            errors.append(f"{where}: unknown listing {outcome.get('listing')}")
        changes = outcome.get("changes") or {}
        if not changes or not set(changes) <= UPDATE_FIELDS:
            errors.append(f"{where}: changes must be among {sorted(UPDATE_FIELDS)}")
        if "status" in changes and changes["status"] not in demo_dataset.STATUSES:
            errors.append(f"{where}: status {changes['status']!r}")
    elif kind == "disambiguate":
        candidates = outcome.get("candidates") or []
        if len(candidates) < 2:
            errors.append(f"{where}: disambiguation needs >= 2 candidates")
        for code in candidates + (outcome.get("allowed") or []):
            if code not in demo["properties"] and code not in created:
                errors.append(f"{where}: unknown candidate {code}")
    if kind in ("update", "disambiguate") and outcome.get("confirm_required") is not True:
        errors.append(f"{where}: every staff write needs a confirmation tap")


def check_hidden_and_private(demo, data, errors):
    hidden = {c for c, p in demo["properties"].items() if p["status"] != "active"}
    for items in data.values():
        for item in items:
            outcome = item.get("expected_outcome") or {}
            if item["set"] != "staff":
                shown = set(outcome.get("ids") or []) | set(outcome.get("allowed") or [])
                if shown & hidden:
                    errors.append(f"{item['id']}: hidden listings {sorted(shown & hidden)} expected")
            if PHONE_LIKE.search(json.dumps(item)):
                errors.append(f"{item['id']}: contains a phone-number-like digit run")


def validate(data, demo):
    errors = []
    check_counts(data, errors)
    check_text_coverage(data, errors)
    check_voice_matrix(data, errors)
    by_id = {i["id"]: i for items in data.values() for i in items}
    created = set()
    for set_name in ("customer_text", "customer_voice", "control", "staff"):
        for item in data[set_name]:
            check_slots(demo, item, errors)
            check_tools(item, errors)
            if set_name == "staff":
                check_staff_outcome(demo, item, created, errors)
            else:
                check_customer_outcome(demo, item, by_id, errors)
    check_hidden_and_private(demo, data, errors)
    return errors


def summary(data):
    return {name: len(items) for name, items in data.items()}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        data = load(argv[0] if argv else BENCH_DIR)
        demo = load_demo()
    except (OSError, ValueError, KeyError, BenchmarkError, demo_dataset.DatasetError) as exc:
        print(f"Benchmark set could not be read: {exc}")
        return 1
    errors = validate(data, demo)
    print(json.dumps(summary(data), sort_keys=True))
    for error in errors:
        print(f"ERROR {error}")
    print("Benchmark set valid." if not errors else f"{len(errors)} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
