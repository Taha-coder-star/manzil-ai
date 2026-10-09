"""Task 4.3 demo dataset (demo/): offline loading and validation, no database.

The files transcribe D11 section 6 (R11-R13) with the Demo Scope section 4
overrides (DH8-101 changed, D2X-101 replaced by DH8-102). Decisions and open
gaps: docs/DEMO_DATASET.md. Vocabularies mirror migrations 005 and 007; the
database constraints stay the backstop when the loader runs.

The scenario checks apply explicit, documented data filters to confirm that
the dataset can produce the Demo Scope result sets. They are not the search
engine and must not be used as one.

    python -m app.demo_dataset [demo_dir]
"""

import csv
import datetime
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"

COLUMNS = {
    "locations.csv": [
        "key", "parent_key", "level", "name", "display_name", "code", "level_label",
        "city_code", "is_commercial_zone", "source", "derived",
    ],
    "location_aliases.csv": [
        "location_key", "alias", "context_key", "kind", "weight", "source", "derived",
    ],
    "location_neighbors.csv": ["location_a_key", "location_b_key", "weight", "source"],
    "projects.csv": [
        "key", "name", "location_key", "developer_name", "project_type",
        "construction_status", "expected_completion", "payment_plan_summary", "amenities",
        "is_demo", "source", "derived",
    ],
    "properties.csv": [
        "listing_code", "status", "purpose", "property_type", "category", "location_key",
        "project_key", "price_pkr", "area_value", "area_unit", "size_nominal_sqyd",
        "bedrooms", "bathrooms", "construction_status", "portion_level", "furnishing",
        "facing", "is_corner", "features", "possession_status", "expected_possession",
        "installment_available", "is_tenanted", "investment_tags", "pf_parking_spaces",
        "pf_floor_no", "pf_rent_advance_months", "pf_current_rent_pkr",
        "pf_installment_months", "source_type", "internal_notes", "is_demo", "source",
        "override", "derived",
    ],
    "staff.csv": ["key", "name", "role", "phone_ref", "if_exists", "source", "derived"],
}
FRESHNESS_FILE = "freshness_policy.json"

# Migrations 005 / 007.
LEVEL_PARENTS = {"city": (None,), "area": ("city",), "sub_area": ("area",),
                 "zone": ("area", "sub_area")}
LEVEL_LABELS = {"phase", "precinct", "block", "sector", "zone", "corridor"}
ALIAS_KINDS = {"official", "common", "misspelling", "urdu_script", "voice_stt"}
PROJECT_TYPES = {"apartment_tower", "mixed_use", "commercial_mall", "housing_scheme"}
CONSTRUCTION = {"ready", "under_construction", "grey_structure", "off_plan"}
STATUSES = {"active", "reserved", "on_hold", "sold", "rented", "withdrawn", "archived"}
PURPOSES = {"sale", "rent"}
PROPERTY_TYPES = {"house", "portion", "apartment", "penthouse", "plot", "shop", "office",
                  "showroom", "warehouse", "building", "floor"}
CATEGORIES = {"residential", "commercial"}
AREA_UNITS = {"sq_ft", "sq_yd", "marla", "kanal"}
PORTION_LEVELS = {"ground", "first", "second", "upper", "lower"}
FURNISHING = {"unfurnished", "semi", "furnished"}
FACING = {"west_open", "east_open", "north_open", "south_open", "park_facing",
          "road_facing", "sea_facing"}
FEATURES = {"basement", "annex", "servant_quarter", "lift", "generator_backup", "solar",
            "gas_connection", "water_kwsb", "water_boring", "security", "lawn",
            "roof_access", "sea_view", "main_boulevard", "drawing_dining"}
POSSESSION = {"possession", "on_ground", "file", "balloted", "allotted"}
INVESTMENT_TAGS = {"rental_income", "capital_gain", "pre_launch", "installments"}
SOURCE_TYPES = {"owner_direct", "agent_listing", "sub_agent", "developer"}
PUBLIC_FEATURE_KEYS = {
    "title", "description", "floor_no", "total_floors", "parking_spaces",
    "price_negotiable", "rent_security_deposit_pkr", "rent_advance_months",
    "maintenance_pkr_month", "min_lease_months", "available_from", "down_payment_pkr",
    "installment_months", "installment_amount_pkr", "current_rent_pkr",
}
STAFF_ROLES = {"admin", "agent", "broker"}
BEDROOMS_REQUIRED = {"house", "apartment", "penthouse", "portion"}
LOCATION_CODE = re.compile(r"^[A-Z0-9]{2,4}$")
LISTING_CODE = re.compile(r"^([A-Z0-9]+)-([0-9]+)$")
PHONE_REF = re.compile(r"^env:[A-Z][A-Z0-9_]*$")

# D11 4.1 nominal sq-yd buckets for plots and houses; Bahria "250" is 272 actual.
NOMINAL_BUCKETS = {120, 125, 160, 200, 235, 240, 250, 300, 350, 400, 500, 600, 1000, 2000}
NOMINAL_ACTUAL_EXCEPTIONS = {(250, Decimal(272))}
NOMINAL_TYPES = {"house", "plot"}

# D11 1.1 categories by type; a plot's category is not stated in the sources.
TYPE_CATEGORY = {"house": "residential", "apartment": "residential",
                 "portion": "residential", "penthouse": "residential",
                 "office": "commercial", "shop": "commercial", "showroom": "commercial",
                 "warehouse": "commercial"}

# Source facts (D11 p6, p11-14; Demo Scope p5).
EXPECTED_LISTINGS = 30
EXPECTED_ACTIVE = 27
EXPECTED_HIDDEN = {"DH6-105": "sold", "DH6-106": "reserved", "CLF-104": "rented"}
EXPECTED_PROJECTS = 2
DHA_PHASE_CODES = {"Phase 1": "DH1", "Phase 2": "DH2", "Phase 2 Ext": "D2X",
                   "Phase 4": "DH4", "Phase 5": "DH5", "Phase 6": "DH6", "Phase 7": "DH7",
                   "Phase 7 Ext": "D7X", "Phase 8": "DH8"}
AREA_CODES = {"DHA City Karachi": "DHC", "Clifton": "CLF", "Bahria Town Karachi": "BTK",
              "Gulshan-e-Iqbal": "GLI", "PECHS": "PEC", "North Nazimabad": "NNZ",
              "Shahrah-e-Faisal": "SHF"}
CLIFTON_BLOCKS = {f"Block {n}" for n in range(1, 10)}
DH8_OVERRIDES = {
    "DH8-101": {"purpose": "sale", "property_type": "house", "location_key": "dha-ph8",
                "area_value": Decimal(500), "area_unit": "sq_yd", "bedrooms": 4,
                "bathrooms": 5, "price_pkr": 74_000_000, "status": "active"},
    "DH8-102": {"purpose": "sale", "property_type": "house", "location_key": "dha-ph8",
                "area_value": Decimal(500), "area_unit": "sq_yd", "bedrooms": 3,
                "bathrooms": 4, "price_pkr": 69_000_000, "status": "active"},
}
REMOVED_LISTINGS = {"D2X-101"}
RESERVED_LISTINGS = {"DH6-109"}  # Created by staff in Demo Scope scenario 4a.

# Real-looking phone numbers must never be committed. Text in any script
# (Urdu included) is allowed; unverified Urdu aliases are left out of the data
# and listed in docs/DEMO_DATASET_GAPS.md instead of being filtered here.
PHONE_PATTERNS = (
    re.compile(r"\+\s*92"),
    re.compile(r"(?<!\d)0?3\d{2}[\s-]?\d{7}(?!\d)"),
    re.compile(r"\d{10,}"),
)
OFFICIAL_DERIVED = "alias (canonical name)"
STAFF_IF_EXISTS = "reuse_unchanged"


class DatasetError(Exception):
    pass


def _read_csv(path, columns):
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != columns:
            raise DatasetError(f"{path.name}: columns {reader.fieldnames} != {columns}")
        rows = []
        for line, row in enumerate(reader, start=2):
            if None in row or None in row.values():
                raise DatasetError(f"{path.name} line {line}: wrong number of fields")
            rows.append({key: value.strip() for key, value in row.items()})
        return rows


def load(directory=DEMO_DIR):
    directory = Path(directory)
    data = {name: _read_csv(directory / name, columns) for name, columns in COLUMNS.items()}
    data[FRESHNESS_FILE] = json.loads((directory / FRESHNESS_FILE).read_text("utf-8"))
    data["_raw"] = {
        path.name: path.read_text("utf-8")
        for path in sorted(directory.iterdir()) if path.is_file()
    }
    return data


# Typed cell parsing; blank = unknown (NULL). Errors go to the `errors` list.
def _int(value, where, errors):
    if value == "":
        return None
    if not re.fullmatch(r"-?[0-9]+", value):
        errors.append(f"{where}: {value!r} is not a whole number")
        return None
    return int(value)


def _decimal(value, where, errors):
    if value == "":
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        errors.append(f"{where}: {value!r} is not a number")
        return None
    if not number.is_finite():
        errors.append(f"{where}: {value!r} is not a number")
        return None
    return number


def _bool(value, where, errors):
    if value == "":
        return None
    if value not in ("true", "false"):
        errors.append(f"{where}: {value!r} is not true/false")
        return None
    return value == "true"


def _month(value, where, errors):
    if value == "":
        return None
    try:
        date = datetime.date.fromisoformat(value)
    except ValueError:
        errors.append(f"{where}: {value!r} is not a date")
        return None
    if date.day != 1:
        errors.append(f"{where}: {value} must be the first day of a month")
    return date


def _enum(value, allowed, where, errors):
    if value != "" and value not in allowed:
        errors.append(f"{where}: {value!r} not in {sorted(allowed)}")
    return value or None


def _set(value, allowed, where, errors):
    if value == "":
        return None
    items = value.split(";")
    if len(set(items)) != len(items):
        errors.append(f"{where}: repeated item in {value!r}")
    for item in items:
        if item not in allowed:
            errors.append(f"{where}: {item!r} not in {sorted(allowed)}")
    return frozenset(items)


def _duplicates(values):
    seen, repeated = set(), set()
    for value in values:
        (repeated if value in seen else seen).add(value)
    return repeated


def alias_norm(alias):
    # Same expression as location_alias.alias_norm (005).
    return re.sub(r"\s+", " ", alias.strip()).lower()


def ancestors(locations, key):
    """Keys from the node itself up to the city."""
    chain = []
    while key and key in locations and key not in chain:
        chain.append(key)
        key = locations[key]["parent_key"]
    return chain


def subtree(locations, key):
    return {k for k in locations if key in ancestors(locations, k)}


def check_locations(rows, errors):
    for key in _duplicates(row["key"] for row in rows):
        errors.append(f"locations: duplicate key {key}")
    by_key = {row["key"]: row for row in rows}
    for name in _duplicates((row["parent_key"], row["name"].lower()) for row in rows):
        errors.append(f"locations: duplicate name under parent {name}")
    for code in _duplicates(row["code"] for row in rows if row["code"]):
        errors.append(f"locations: duplicate code {code}")

    for row in rows:
        where = f"location {row['key']}"
        for column in ("key", "level", "name", "source"):
            if not row[column]:
                errors.append(f"{where}: {column} is blank")
        parent = by_key.get(row["parent_key"]) if row["parent_key"] else None
        if row["parent_key"] and parent is None:
            errors.append(f"{where}: unknown parent {row['parent_key']}")
        allowed = LEVEL_PARENTS.get(row["level"])
        if allowed is None:
            errors.append(f"{where}: unknown level {row['level']!r}")
        elif (parent["level"] if parent else None) not in allowed:
            errors.append(f"{where}: level {row['level']} cannot sit under "
                          f"{parent['level'] if parent else 'nothing'}")
        if row["code"] and not LOCATION_CODE.match(row["code"]):
            errors.append(f"{where}: bad code {row['code']!r}")
        _enum(row["level_label"], LEVEL_LABELS, where, errors)
        if row["city_code"] and (row["level"] != "city"
                                 or not re.fullmatch(r"[A-Z]{3}", row["city_code"])):
            errors.append(f"{where}: city_code only on a city, three capitals")
        commercial = _bool(row["is_commercial_zone"], where, errors)
        if commercial is None:
            errors.append(f"{where}: is_commercial_zone must be true or false")
        elif commercial and row["level"] != "zone":
            errors.append(f"{where}: only a zone can be a commercial zone")
        if row["display_name"] == "":
            errors.append(f"{where}: display_name is blank")

    if len(by_key) != len(rows):
        return by_key
    for row in rows:
        if len(ancestors(by_key, row["key"])) > 4:
            errors.append(f"location {row['key']}: hierarchy cycle or too deep")

    cities = [row for row in rows if row["level"] == "city"]
    if [(c["name"], c["city_code"]) for c in cities] != [("Karachi", "KHI")]:
        errors.append("locations: exactly one city, Karachi (KHI), is expected")
    areas = {row["name"]: row for row in rows if row["level"] == "area"}
    expected_areas = {"DHA Defence"} | set(AREA_CODES)
    if set(areas) != expected_areas:
        errors.append(f"locations: areas {sorted(areas)} != {sorted(expected_areas)}")
    for name, code in AREA_CODES.items():
        if name in areas and areas[name]["code"] != code:
            errors.append(f"locations: area {name} code {areas[name]['code']!r} != {code}")
    if "DHA Defence" in areas:
        dha = areas["DHA Defence"]
        if dha["code"]:
            errors.append("locations: DHA Defence has no area code in D11 (phases do)")
        phases = {row["name"]: row["code"] for row in rows
                  if row["parent_key"] == dha["key"] and row["level"] == "sub_area"}
        if phases != DHA_PHASE_CODES:
            errors.append(f"locations: DHA phases/codes {phases} != {DHA_PHASE_CODES}")
    if "Clifton" in areas:
        blocks = {row["name"] for row in rows if row["parent_key"] == areas["Clifton"]["key"]}
        if blocks != CLIFTON_BLOCKS:
            errors.append(f"locations: Clifton blocks {sorted(blocks)} != Block 1..9")
    # DHA City Karachi is its own area, never under DHA Defence (D11 p6): areas
    # sit directly under the city, which the level rule above enforces.
    return by_key


def check_aliases(rows, locations, errors):
    seen = set()
    for row in rows:
        where = f"alias {row['alias']!r} -> {row['location_key']}"
        if row["location_key"] not in locations:
            errors.append(f"{where}: unknown location")
        if not row["alias"] or not row["source"]:
            errors.append(f"{where}: alias and source are required")
        if row["alias"] != alias_norm(row["alias"]):
            errors.append(f"{where}: store the normalised form")
        context = row["context_key"] or None
        if context is not None:
            if context == row["location_key"]:
                errors.append(f"{where}: context equals the location")
            elif context not in ancestors(locations, row["location_key"]):
                errors.append(f"{where}: context {context} is not an ancestor")
        _enum(row["kind"], ALIAS_KINDS, where, errors)
        if row["kind"] == "":
            errors.append(f"{where}: kind is blank")
        weight = _decimal(row["weight"], where, errors)
        if weight is None or not 0 <= weight <= 1:
            errors.append(f"{where}: weight must be in [0, 1]")
        identity = (row["location_key"], alias_norm(row["alias"]), context)
        if identity in seen:
            errors.append(f"{where}: duplicate alias")
        seen.add(identity)
        if (row["kind"] == "official") != (row["derived"] == OFFICIAL_DERIVED):
            errors.append(f"{where}: official aliases are exactly the derived name aliases")
        if row["derived"] == OFFICIAL_DERIVED and row["location_key"] in locations:
            expected = official_alias_identity(locations, row["location_key"])
            if identity != expected:
                errors.append(f"{where}: official alias must be {expected}")

    # Every canonical name resolves: by its official alias, or by a sourced alias
    # with the same identity (which is kept instead of a duplicate).
    for key in locations:
        if official_alias_identity(locations, key) not in seen:
            errors.append(f"location {key}: no alias for its canonical name")


def official_alias_identity(locations, key):
    """(location, alias_norm, context) of a location's canonical-name alias.

    A name used by more than one location (Block 2, Block 6) only applies inside
    its parent; a unique name applies everywhere.
    """
    name = alias_norm(locations[key]["name"])
    repeated = sum(1 for row in locations.values() if alias_norm(row["name"]) == name) > 1
    return key, name, locations[key]["parent_key"] if repeated else None


def check_neighbors(rows, locations, errors):
    pairs = set()
    for row in rows:
        a, b = row["location_a_key"], row["location_b_key"]
        where = f"neighbour {a} / {b}"
        if a not in locations or b not in locations:
            errors.append(f"{where}: unknown location")
        if a == b:
            errors.append(f"{where}: a location cannot neighbour itself")
        if frozenset((a, b)) in pairs:
            errors.append(f"{where}: duplicate pair")
        pairs.add(frozenset((a, b)))
        weight = _decimal(row["weight"], where, errors)
        if weight is None or not 0 < weight <= 1:
            errors.append(f"{where}: weight must be in (0, 1]")
        if not row["source"]:
            errors.append(f"{where}: source is required")
    return pairs


def check_projects(rows, locations, errors):
    if len(rows) != EXPECTED_PROJECTS:
        errors.append(f"projects: {len(rows)} rows, expected {EXPECTED_PROJECTS}")
    for key in _duplicates(row["key"] for row in rows):
        errors.append(f"projects: duplicate key {key}")
    for name in _duplicates(row["name"].lower() for row in rows):
        errors.append(f"projects: duplicate name {name}")
    for row in rows:
        where = f"project {row['key']}"
        if not row["name"].endswith("(DEMO)"):
            errors.append(f"{where}: fictional names end with '(DEMO)'")
        if row["location_key"] not in locations:
            errors.append(f"{where}: unknown location")
        if row["developer_name"] and not row["developer_name"].endswith("(DEMO)"):
            errors.append(f"{where}: fictional developer names end with '(DEMO)'")
        _enum(row["project_type"], PROJECT_TYPES, where, errors)
        _enum(row["construction_status"], CONSTRUCTION, where, errors)
        _month(row["expected_completion"], where, errors)
        if _bool(row["is_demo"], where, errors) is not True:
            errors.append(f"{where}: is_demo must be true")
        if not row["source"]:
            errors.append(f"{where}: source is required")
    return {row["key"]: row for row in rows}


def parse_property(row, errors):
    where = f"listing {row['listing_code']}"
    item = {
        "listing_code": row["listing_code"],
        "status": _enum(row["status"], STATUSES, where, errors),
        "purpose": _enum(row["purpose"], PURPOSES, where, errors),
        "property_type": _enum(row["property_type"], PROPERTY_TYPES, where, errors),
        "category": _enum(row["category"], CATEGORIES, where, errors),
        "location_key": row["location_key"] or None,
        "project_key": row["project_key"] or None,
        "price_pkr": _int(row["price_pkr"], where, errors),
        "area_value": _decimal(row["area_value"], where, errors),
        "area_unit": _enum(row["area_unit"], AREA_UNITS, where, errors),
        "size_nominal_sqyd": _int(row["size_nominal_sqyd"], where, errors),
        "bedrooms": _int(row["bedrooms"], where, errors),
        "bathrooms": _int(row["bathrooms"], where, errors),
        "construction_status": _enum(row["construction_status"], CONSTRUCTION, where, errors),
        "portion_level": _enum(row["portion_level"], PORTION_LEVELS, where, errors),
        "furnishing": _enum(row["furnishing"], FURNISHING, where, errors),
        "facing": _set(row["facing"], FACING, where, errors),
        "is_corner": _bool(row["is_corner"], where, errors),
        "features": _set(row["features"], FEATURES, where, errors),
        "possession_status": _enum(row["possession_status"], POSSESSION, where, errors),
        "expected_possession": _month(row["expected_possession"], where, errors),
        "installment_available": _bool(row["installment_available"], where, errors),
        "is_tenanted": _bool(row["is_tenanted"], where, errors),
        "investment_tags": _set(row["investment_tags"], INVESTMENT_TAGS, where, errors),
        "source_type": _enum(row["source_type"], SOURCE_TYPES, where, errors),
        "internal_notes": row["internal_notes"] or None,
        "is_demo": _bool(row["is_demo"], where, errors),
        "public_features": {},
    }
    for column, value in row.items():
        if column.startswith("pf_") and value != "":
            item["public_features"][column[3:]] = _int(value, f"{where} {column}", errors)
    return item


def check_properties(rows, locations, projects, errors):
    items = [parse_property(row, errors) for row in rows]
    by_code = {item["listing_code"]: item for item in items}
    if len(rows) != EXPECTED_LISTINGS:
        errors.append(f"properties: {len(rows)} rows, expected {EXPECTED_LISTINGS}")
    for code in _duplicates(row["listing_code"] for row in rows):
        errors.append(f"properties: duplicate listing_code {code}")
    hidden = {i["listing_code"]: i["status"] for i in items if i["status"] != "active"}
    if hidden != EXPECTED_HIDDEN:
        errors.append(f"properties: hidden listings {hidden} != {EXPECTED_HIDDEN}")
    active = sum(1 for i in items if i["status"] == "active")
    if active != EXPECTED_ACTIVE:
        errors.append(f"properties: {active} active, expected {EXPECTED_ACTIVE}")
    for code in sorted(REMOVED_LISTINGS & set(by_code)):
        errors.append(f"properties: {code} was replaced (Demo Scope p5) and must be absent")
    for code in sorted(RESERVED_LISTINGS & set(by_code)):
        errors.append(f"properties: {code} is reserved for scenario 4a and must be absent")
    for code, expected in DH8_OVERRIDES.items():
        item = by_code.get(code)
        if item is None:
            errors.append(f"properties: override row {code} is missing")
            continue
        actual = {field: item[field] for field in expected}
        if actual != expected:
            errors.append(f"properties: {code} {actual} != Demo Scope override {expected}")
        if not rows[items.index(item)]["override"]:
            errors.append(f"properties: {code} must record its override provenance")

    for row, item in zip(rows, items):
        where = f"listing {item['listing_code']}"
        match = LISTING_CODE.match(item["listing_code"])
        if not match:
            errors.append(f"{where}: bad listing code")
        if not row["source"]:
            errors.append(f"{where}: source is required")
        if item["is_demo"] is not True:
            errors.append(f"{where}: is_demo must be true")
        if item["status"] is None:
            errors.append(f"{where}: status is required")
        location = item["location_key"]
        if location not in locations:
            errors.append(f"{where}: unknown location {location}")
        project = projects.get(item["project_key"]) if item["project_key"] else None
        if item["project_key"] and project is None:
            errors.append(f"{where}: unknown project {item['project_key']}")

        # Listing-code prefix: PRJ for project units, else the nearest coded ancestor.
        if match and location in locations:
            if project is not None:
                if match.group(1) != "PRJ":
                    errors.append(f"{where}: project units use the PRJ prefix")
                if project["location_key"] != location:
                    errors.append(f"{where}: location differs from its project's")
            else:
                coded = [locations[k]["code"] for k in ancestors(locations, location)
                         if locations[k]["code"]]
                if not coded or coded[0] != match.group(1):
                    errors.append(f"{where}: prefix does not match the location code "
                                  f"{coded[0] if coded else None}")

        price = item["price_pkr"]
        if price is not None and price <= 0:
            errors.append(f"{where}: price must be positive whole rupees")
        if item["area_value"] is not None and item["area_value"] <= 0:
            errors.append(f"{where}: area must be positive")
        if (item["area_value"] is None) != (item["area_unit"] is None):
            errors.append(f"{where}: area value and unit go together")
        if item["area_unit"] in ("marla", "kanal"):
            errors.append(f"{where}: marla / kanal sizes need the agency basis (loader)")
        for column in ("bedrooms", "bathrooms"):
            if item[column] is not None and item[column] < 0:
                errors.append(f"{where}: {column} must be >= 0")
        if item["status"] == "active":
            required = ("purpose", "property_type", "location_key", "price_pkr", "area_value")
            missing = [c for c in required if item[c] is None]
            if item["property_type"] in BEDROOMS_REQUIRED and item["bedrooms"] is None:
                missing.append("bedrooms")
            if missing:
                errors.append(f"{where}: an active listing needs {missing}")
        if item["portion_level"] and item["property_type"] != "portion":
            errors.append(f"{where}: portion_level only on a portion")
        expected_category = TYPE_CATEGORY.get(item["property_type"])
        if item["category"] and expected_category and item["category"] != expected_category:
            errors.append(f"{where}: category {item['category']} != {expected_category}")

        nominal = item["size_nominal_sqyd"]
        if nominal is not None:
            if item["property_type"] not in NOMINAL_TYPES or item["area_unit"] != "sq_yd":
                errors.append(f"{where}: nominal sq yd is for sq-yd houses and plots")
            elif nominal not in NOMINAL_BUCKETS:
                errors.append(f"{where}: nominal {nominal} is not a D11 bucket")
            elif (Decimal(nominal) != item["area_value"]
                  and (nominal, item["area_value"]) not in NOMINAL_ACTUAL_EXCEPTIONS):
                errors.append(f"{where}: nominal {nominal} vs actual {item['area_value']}")
        unknown = set(item["public_features"]) - PUBLIC_FEATURE_KEYS
        if unknown:
            errors.append(f"{where}: public_features keys {sorted(unknown)} not allowed")
        for key, value in item["public_features"].items():
            if value is not None and value < 0:
                errors.append(f"{where}: public feature {key} must be >= 0")
        if item["expected_possession"] and item["construction_status"] == "ready":
            errors.append(f"{where}: a ready listing has no expected possession")

    # Bahria nominal 250 / actual 272 (D11 p1, p13).
    for code in ("BTK-103", "BTK-104"):
        item = by_code.get(code)
        if item and (item["area_value"], item["area_unit"], item["size_nominal_sqyd"]) != (
                Decimal(272), "sq_yd", 250):
            errors.append(f"properties: {code} must be 272 sq yd actual, 250 nominal")
    # Phase 6 sequence ends at 108, so staff's next listing is DH6-109.
    dh6 = [int(LISTING_CODE.match(c).group(2)) for c in by_code
           if c.startswith("DH6-") and LISTING_CODE.match(c)]
    if dh6 and max(dh6) != 108:
        errors.append(f"properties: DH6 sequence ends at {max(dh6)}, expected 108")
    return by_code


def check_staff(rows, errors):
    roles = sorted(row["role"] for row in rows)
    if roles != sorted(STAFF_ROLES):
        errors.append(f"staff: roles {roles} != one each of {sorted(STAFF_ROLES)}")
    for ref in _duplicates(row["phone_ref"] for row in rows):
        errors.append(f"staff: duplicate phone_ref {ref}")
    for key in _duplicates(row["key"] for row in rows):
        errors.append(f"staff: duplicate key {key}")
    for row in rows:
        where = f"staff {row['key']}"
        if not row["name"]:
            errors.append(f"{where}: name is required")
        if not PHONE_REF.match(row["phone_ref"]):
            errors.append(f"{where}: phone_ref must be a symbolic env:NAME reference")
        if not row["source"]:
            errors.append(f"{where}: source is required")
        # An existing staff row (matched by phone) is reused as it is, never renamed.
        if row["if_exists"] != STAFF_IF_EXISTS:
            errors.append(f"{where}: if_exists must be {STAFF_IF_EXISTS}")


def check_freshness(policy, properties, errors):
    if policy.get("status") != "adopted" or policy.get("applies") != "insertion":
        errors.append("freshness: must be the adopted, insertion-time policy")
    if policy.get("default_confirmed_days_ago") != 0:
        errors.append("freshness: default must be confirmed at insertion (0 days)")
    listings = policy.get("listings") or {}
    if len(listings) != 1:
        errors.append("freshness: D11 shows the staleness check on exactly one listing")
    for code, entry in listings.items():
        item = properties.get(code)
        if item is None or item["status"] != "active":
            errors.append(f"freshness: {code} must be an active listing")
        days = entry.get("confirmed_days_ago")
        if not isinstance(days, int) or not 30 < days < 45:
            errors.append(f"freshness: {code} must be over 30 and under 45 days old")
        if not entry.get("reason"):
            errors.append(f"freshness: {code} needs a reason")
    if "never changes last_confirmed_at" not in policy.get("rerun", ""):
        errors.append("freshness: reruns must preserve last_confirmed_at")


def check_raw_text(raw, errors):
    for name, text in raw.items():
        for pattern in PHONE_PATTERNS:
            found = pattern.search(text)
            if found:
                errors.append(f"{name}: phone-like text {found.group()!r}")


# Scenario filters. Each states its rule; budgets follow D11 4.3 / R17 2.1
# (max X searches up to X * 1.05; "around X" is X +/- 10%; size +/- 10%).
def _active(properties):
    return [i for i in properties.values() if i["status"] == "active"]


def _in_locations(locations, item, nodes):
    return any(item["location_key"] in subtree(locations, node) for node in nodes)


def _houses_500(locations, properties, nodes, max_price, exclude=()):
    return {
        i["listing_code"] for i in _active(properties)
        if i["purpose"] == "sale" and i["property_type"] == "house"
        and _in_locations(locations, i, nodes)
        and i["size_nominal_sqyd"] is not None and 450 <= i["size_nominal_sqyd"] <= 550
        and i["price_pkr"] <= max_price * Decimal("1.05")
        and i["listing_code"] not in exclude
    }


def neighbours_of(pairs, key):
    return {other for pair in pairs if key in pair for other in pair if other != key}


def scenario_results(locations, pairs, properties):
    """Actual and expected sets for the Demo Scope / D11 scripted phrases."""
    results = {}
    active = _active(properties)
    # Scenario 1 (Demo Scope p6): rent office anywhere in Karachi, 5 lakh +/- 10%.
    results["s1_office_rent_5_lakh"] = (
        {i["listing_code"] for i in active if i["purpose"] == "rent"
         and i["property_type"] == "office" and 450_000 <= i["price_pkr"] <= 550_000},
        {"DH6-108", "SHF-101", "PEC-101"})
    # Scenario 2 (Demo Scope p6): sale house, Phase 6, 500 sq yd, max 8 crore.
    exact = _houses_500(locations, properties, ["dha-ph6"], 80_000_000)
    results["s2_exact"] = (exact, {"DH6-101", "DH6-102"})
    results["s2_budget_plus_10pct"] = (
        _houses_500(locations, properties, ["dha-ph6"], 88_000_000), {"DH6-101", "DH6-102"})
    near = neighbours_of(pairs, "dha-ph6")
    results["s2_neighbour_step"] = (
        _houses_500(locations, properties, sorted(near), 80_000_000), {"DH7-101"})
    shown = {"DH6-101", "DH6-102", "DH7-101"}
    # Scenario 3b: cheaper than the cheapest shown (7.5 crore * 0.97), shown excluded.
    cheaper = 75_000_000 * Decimal("0.97")
    results["s3b_cheaper"] = (
        _houses_500(locations, properties, ["dha-ph6", *sorted(near)], cheaper, shown),
        set())
    # Scenario 3c: Phase 6 + Phase 8 at the reduced budget; 5% tolerance labelled.
    found = _houses_500(locations, properties, ["dha-ph6", "dha-ph8"], cheaper, shown)
    results["s3c_phase_8"] = (found, {"DH8-101", "DH8-102"})
    results["s3c_in_budget"] = (
        {c for c in found if properties[c]["price_pkr"] <= cheaper}, {"DH8-102"})
    # Scenario 4c: active DHA Phase 6 houses before DH6-109 exists.
    results["s4c_dha6_houses"] = (
        {i["listing_code"] for i in active if i["property_type"] == "house"
         and _in_locations(locations, i, ["dha-ph6"])},
        {"DH6-101", "DH6-102", "DH6-103", "DH6-104"})
    # D11 p14 coverage: Clifton rent shops.
    results["d11_clifton_shop_rent"] = (
        {i["listing_code"] for i in active if i["purpose"] == "rent"
         and i["property_type"] == "shop" and _in_locations(locations, i, ["clf"])},
        {"CLF-101"})
    # D11 p14: Bahria 10 marla = 2,250 sq ft = 250 sq yd; exact nominal bucket.
    # (R17's +/- 10% would also admit BTK-102 at 235: see docs/DEMO_DATASET.md.)
    results["d11_bahria_10_marla_bucket"] = (
        {i["listing_code"] for i in active if i["purpose"] == "sale"
         and i["property_type"] == "house" and _in_locations(locations, i, ["btk"])
         and i["size_nominal_sqyd"] == 250},
        {"BTK-103", "BTK-104"})
    # D11 p14: under-construction / off-plan investment.
    results["d11_under_construction"] = (
        {i["listing_code"] for i in active
         if i["construction_status"] in ("under_construction", "off_plan")},
        {"PRJ-101", "PRJ-102", "PRJ-103"})
    # Demo Scope p5 coverage: rent listings (D2X-101 gone).
    results["coverage_rent"] = (
        {c for c, i in properties.items() if i["purpose"] == "rent"},
        {"DH6-107", "DH6-108", "CLF-101", "CLF-104", "SHF-101", "PEC-101", "BTK-105",
         "NNZ-101"})
    return results


def validate(data):
    errors = []
    locations = check_locations(data["locations.csv"], errors)
    check_aliases(data["location_aliases.csv"], locations, errors)
    pairs = check_neighbors(data["location_neighbors.csv"], locations, errors)
    projects = check_projects(data["projects.csv"], locations, errors)
    properties = check_properties(data["properties.csv"], locations, projects, errors)
    check_staff(data["staff.csv"], errors)
    check_freshness(data[FRESHNESS_FILE], properties, errors)
    check_raw_text(data.get("_raw", {}), errors)
    if not errors:
        for name, (actual, expected) in scenario_results(
                locations, pairs, properties).items():
            if actual != expected:
                errors.append(f"scenario {name}: {sorted(actual)} != {sorted(expected)}")
    return errors


def summary(data):
    rows = data["properties.csv"]
    levels = {}
    for row in data["locations.csv"]:
        levels[row["level"]] = levels.get(row["level"], 0) + 1
    return {
        "listings": len(rows),
        "active": sum(1 for row in rows if row["status"] == "active"),
        "projects": len(data["projects.csv"]),
        "locations": len(data["locations.csv"]),
        "location_levels": levels,
        "aliases": len(data["location_aliases.csv"]),
        "aliases_official": sum(1 for row in data["location_aliases.csv"]
                                if row["kind"] == "official"),
        "aliases_with_context": sum(1 for row in data["location_aliases.csv"]
                                    if row["context_key"]),
        "neighbour_pairs": len(data["location_neighbors.csv"]),
        "staff": len(data["staff.csv"]),
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        data = load(argv[0] if argv else DEMO_DIR)
    except (OSError, ValueError, DatasetError) as exc:
        print(f"Demo dataset could not be read: {exc}")
        return 1
    errors = validate(data)
    print(json.dumps(summary(data), sort_keys=True))
    for error in errors:
        print(f"ERROR {error}")
    print("Demo dataset valid." if not errors else f"{len(errors)} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
