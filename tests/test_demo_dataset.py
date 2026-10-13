"""Task 4.3 checkpoint 1: the demo dataset files and the offline validator.

No database. Each negative case breaks one fact in a copy of the real dataset and
expects the validator to name it.
"""

import copy
import json
import shutil
import unicodedata

import pytest

from app import demo_dataset as dd


@pytest.fixture(scope="module")
def dataset():
    return dd.load()


@pytest.fixture
def data(dataset):
    return copy.deepcopy(dataset)


def row(data, file, column, value):
    return next(r for r in data[file] if r[column] == value)


def listing(data, code):
    return row(data, "properties.csv", "listing_code", code)


def errors_for(data, fragment):
    errors = dd.validate(data)
    assert any(fragment in error for error in errors), errors
    return errors


def test_real_dataset_is_valid(dataset):
    assert dd.validate(dataset) == []


def test_counts_and_identities(dataset):
    summary = dd.summary(dataset)
    assert summary["listings"] == 30 and summary["active"] == 27
    assert summary["projects"] == 2 and summary["staff"] == 3
    assert summary["location_levels"] == {"city": 1, "area": 8, "sub_area": 33, "zone": 11}
    codes = [r["listing_code"] for r in dataset["properties.csv"]]
    assert len(set(codes)) == 30
    assert "D2X-101" not in codes and "DH6-109" not in codes
    assert {"DH8-101", "DH8-102"} <= set(codes)
    assert row(dataset, "locations.csv", "code", "D2X")["name"] == "Phase 2 Ext"
    assert not any(r["code"] == "DH3" for r in dataset["locations.csv"])
    assert summary["aliases"] == 87
    assert summary["aliases_official"] == 49
    assert summary["aliases_with_context"] == 8  # 3 sourced (R17 p6 x2, phase 6) + 5 repeated block names


def test_scenarios_match_their_expected_sets(dataset):
    errors = []
    locations = dd.check_locations(dataset["locations.csv"], errors)
    pairs = dd.check_neighbors(dataset["location_neighbors.csv"], locations, errors)
    projects = dd.check_projects(dataset["projects.csv"], locations, errors)
    properties = dd.check_properties(dataset["properties.csv"], locations, projects, errors)
    assert errors == []
    results = dd.scenario_results(locations, pairs, properties)
    assert all(actual == expected for actual, expected in results.values()), results
    assert dd.neighbours_of(pairs, "dha-ph6") == {"dha-ph5", "dha-ph7"}


def test_every_row_has_provenance(dataset):
    for file in ("locations.csv", "location_aliases.csv", "location_neighbors.csv",
                 "projects.csv", "properties.csv", "staff.csv"):
        assert all(r["source"] for r in dataset[file]), file


@pytest.mark.parametrize(("field", "value", "fragment"), [
    ("price_pkr", "73000000", "Demo Scope override"),
    ("area_value", "300", "Demo Scope override"),
    ("bedrooms", "5", "Demo Scope override"),
    ("override", "", "override provenance"),
])
def test_dh8_override_is_exact(data, field, value, fragment):
    listing(data, "DH8-101")[field] = value
    errors_for(data, fragment)


def test_replaced_and_reserved_codes_are_absent(data):
    listing(data, "DH8-102")["listing_code"] = "D2X-101"
    errors_for(data, "D2X-101 was replaced")
    fresh = copy.deepcopy(data)
    listing(fresh, "D2X-101")["listing_code"] = "DH6-109"
    errors_for(fresh, "DH6-109 is reserved")


def test_hidden_set_is_exact(data):
    listing(data, "DH6-103")["status"] = "on_hold"
    errors_for(data, "hidden listings")


@pytest.mark.parametrize(("code", "field", "value", "fragment"), [
    ("DH6-101", "price_pkr", "78000000.5", "not a whole number"),
    ("DH6-101", "price_pkr", "7.8 crore", "not a whole number"),
    ("DH6-101", "status", "PENDING-APPROVAL", "not in"),
    ("DH6-101", "facing", "west_open;west_open", "repeated item"),
    ("DH6-101", "features", "pool", "not in"),
    ("DH6-101", "is_demo", "false", "is_demo must be true"),
    ("DH6-101", "source", "", "source is required"),
    ("DH6-101", "location_key", "dha-ph7", "prefix does not match"),
    ("DH6-101", "bedrooms", "", "needs ['bedrooms']"),
    ("DH6-101", "portion_level", "upper", "portion_level only"),
    ("DH6-101", "category", "commercial", "category"),
    ("DH6-101", "area_unit", "marla", "agency basis"),
    ("BTK-103", "size_nominal_sqyd", "272", "not a D11 bucket"),
    ("BTK-103", "area_value", "250", "272 sq yd actual, 250 nominal"),
    ("GLI-101", "size_nominal_sqyd", "250", "nominal 250 vs actual 240"),
    ("DH5-101", "size_nominal_sqyd", "250", "sq-yd houses and plots"),
    ("PRJ-101", "project_key", "", "prefix does not match"),
    ("PRJ-101", "location_key", "gli-b13", "differs from its project"),
    ("PRJ-101", "expected_possession", "2028-12-15", "first day of a month"),
])
def test_listing_field_rules(data, code, field, value, fragment):
    listing(data, code)[field] = value
    errors_for(data, fragment)


def test_public_features_keys_are_allowlisted(dataset, tmp_path):
    for path in dd.DEMO_DIR.iterdir():
        if path.is_file():  # demo/media/ (Task 4.4) is not part of the dataset
            shutil.copy(path, tmp_path / path.name)
    properties = tmp_path / "properties.csv"
    properties.write_text(
        properties.read_text("utf-8").replace("pf_floor_no", "pf_owner_phone", 1), "utf-8")
    with pytest.raises(dd.DatasetError, match="columns"):
        dd.load(tmp_path)
    data = copy.deepcopy(dataset)
    data["properties.csv"][0]["pf_owner_phone"] = "1"
    errors_for(data, "public_features keys ['owner_phone']")


def test_wrong_field_count_is_rejected(tmp_path):
    for path in dd.DEMO_DIR.iterdir():
        if path.is_file():  # demo/media/ (Task 4.4) is not part of the dataset
            shutil.copy(path, tmp_path / path.name)
    staff = tmp_path / "staff.csv"
    staff.write_text(staff.read_text("utf-8") + "extra,x,admin,env:X,reuse_unchanged,src,,surplus\n", "utf-8")
    with pytest.raises(dd.DatasetError, match="wrong number of fields"):
        dd.load(tmp_path)


@pytest.mark.parametrize(("key", "field", "value", "fragment"), [
    ("dhc", "parent_key", "dha", "level area cannot sit under area"),
    ("dha-ph6-bukhari", "parent_key", "khi", "level zone cannot sit under city"),
    ("dha-ph6", "parent_key", "nowhere", "unknown parent"),
    ("dha-ph6", "code", "DH66X", "bad code"),
    ("dha-ph2x", "code", "", "DHA phases/codes"),
    ("clf-b9", "name", "Block 10", "Clifton blocks"),
    ("dha-ph6", "level_label", "PENDING-APPROVAL", "not in"),
    ("dha-ph6", "is_commercial_zone", "true", "only a zone"),
    ("dha-ph7", "name", "Phase 6", "duplicate name under parent"),
    ("dha-ph6", "city_code", "KHI", "city_code only on a city"),
])
def test_location_rules(data, key, field, value, fragment):
    row(data, "locations.csv", "key", key)[field] = value
    errors_for(data, fragment)


@pytest.mark.parametrize(("index", "field", "value", "fragment"), [
    (0, "location_key", "nowhere", "unknown location"),
    (0, "alias", "DHA  6", "normalised form"),
    (0, "kind", "PENDING-APPROVAL", "not in"),
    (0, "weight", "1.5", "weight must be in [0, 1]"),
    (8, "context_key", "btk", "is not an ancestor"),
    (8, "context_key", "dha-ph6", "context equals the location"),
    (1, "alias", "dha 6", "duplicate alias"),
])
def test_alias_rules(data, index, field, value, fragment):
    data["location_aliases.csv"][index][field] = value
    errors_for(data, fragment)


def test_neighbour_rules(data):
    first = data["location_neighbors.csv"][0]
    data["location_neighbors.csv"].append(
        dict(first, location_a_key=first["location_b_key"],
             location_b_key=first["location_a_key"]))
    errors_for(data, "duplicate pair")
    fresh = copy.deepcopy(data)
    fresh["location_neighbors.csv"][0]["location_b_key"] = "dha-ph5"
    errors_for(fresh, "cannot neighbour itself")
    fresh = copy.deepcopy(data)
    fresh["location_neighbors.csv"][0]["weight"] = "0"
    errors_for(fresh, "weight must be in (0, 1]")


def test_neighbour_scenario_detects_a_six_eight_pair(data):
    # Scenario 2 must stay exactly {DH6-101, DH6-102, DH7-101} after relaxation.
    data["location_neighbors.csv"].append(
        {"location_a_key": "dha-ph6", "location_b_key": "dha-ph8", "weight": "1",
         "source": "test"})
    errors_for(data, "scenario s2_neighbour_step")


@pytest.mark.parametrize(("code", "field", "value", "scenario"), [
    ("DH7-101", "price_pkr", "90000000", "s2_neighbour_step"),
    ("DH6-103", "price_pkr", "84000000", "s2_exact"),
    ("SHF-101", "purpose", "sale", "s1_office_rent_5_lakh"),
    ("DH6-103", "price_pkr", "72000000", "s3c_phase_8"),
    ("DH6-103", "price_pkr", "72000000", "s3b_cheaper"),
    ("BTK-102", "size_nominal_sqyd", "250", "d11_bahria_10_marla_bucket"),
])
def test_scenario_filters_detect_data_changes(data, code, field, value, scenario):
    item = listing(data, code)
    item[field] = value
    if field == "size_nominal_sqyd":  # keep the nominal / actual rule satisfied
        item["area_value"] = "272"
    errors_for(data, f"scenario {scenario}")


def test_projects(data):
    row(data, "projects.csv", "key", "PRJ-P1")["name"] = "Saahil View Residency"
    errors_for(data, "end with '(DEMO)'")
    fresh = copy.deepcopy(data)
    row(fresh, "projects.csv", "key", "PRJ-P2")["construction_status"] = "PENDING-APPROVAL"
    errors_for(fresh, "not in")
    fresh = copy.deepcopy(data)
    fresh["projects.csv"].pop()
    errors_for(fresh, "projects: 1 rows")


@pytest.mark.parametrize(("key", "field", "value", "fragment"), [
    ("admin", "phone_ref", "+923001234567", "symbolic env:NAME"),
    ("admin", "role", "broker", "roles"),
    ("agent", "phone_ref", "env:DEMO_ADMIN_PHONE", "duplicate phone_ref"),
    ("broker", "name", "", "name is required"),
    ("agent", "if_exists", "rename", "if_exists must be reuse_unchanged"),
])
def test_staff_rules(data, key, field, value, fragment):
    row(data, "staff.csv", "key", key)[field] = value
    errors_for(data, fragment)


@pytest.mark.parametrize("text", [
    "+92 300 1234567", "03001234567", "0300-1234567", "923001234567",
])
def test_phone_numbers_are_rejected(data, text):
    data["_raw"]["properties.csv"] += f"\n{text}\n"
    errors_for(data, "phone-like text")


# Urdu "Clifton" in logical order, built from code points to keep this file ASCII.
# A test fixture only: not a verified PDF alias (those stay omitted).
URDU_CLIFTON = "".join(map(chr, (0x06A9, 0x0644, 0x0641, 0x0679, 0x0646)))


def test_urdu_fixture_code_points():
    assert [f"U+{ord(c):04X}" for c in URDU_CLIFTON] == [
        "U+06A9", "U+0644", "U+0641", "U+0679", "U+0646"]
    assert [unicodedata.name(c) for c in URDU_CLIFTON] == [
        "ARABIC LETTER KEHEH", "ARABIC LETTER LAM", "ARABIC LETTER FEH",
        "ARABIC LETTER TTEH", "ARABIC LETTER NOON"]
    # Base letters, not presentation forms, and stable under NFC and UTF-8.
    assert unicodedata.normalize("NFC", URDU_CLIFTON) == URDU_CLIFTON
    assert URDU_CLIFTON.encode("utf-8").decode("utf-8") == URDU_CLIFTON


def urdu_alias():
    return {"location_key": "clf", "alias": URDU_CLIFTON, "context_key": "",
            "kind": "urdu_script", "weight": "1", "source": "test", "derived": ""}


def test_valid_urdu_alias_is_accepted(data):
    data["location_aliases.csv"].append(urdu_alias())
    assert dd.validate(data) == []


def test_valid_urdu_alias_survives_the_csv_file(tmp_path):
    for path in dd.DEMO_DIR.iterdir():
        if path.is_file():  # demo/media/ (Task 4.4) is not part of the dataset
            shutil.copy(path, tmp_path / path.name)
    aliases = tmp_path / "location_aliases.csv"
    line = ",".join(urdu_alias().values())
    aliases.write_text(aliases.read_text("utf-8") + line + "\n", "utf-8")
    data = dd.load(tmp_path)
    assert dd.validate(data) == []
    assert data["location_aliases.csv"][-1]["alias"] == URDU_CLIFTON


def test_every_location_has_a_canonical_name_alias(dataset):
    errors = []
    locations = dd.check_locations(dataset["locations.csv"], errors)
    identities = {(r["location_key"], dd.alias_norm(r["alias"]), r["context_key"] or None)
                  for r in dataset["location_aliases.csv"]}
    for key in locations:
        assert dd.official_alias_identity(locations, key) in identities, key
    # Repeated names keep their parent as context; unique names have none.
    assert dd.official_alias_identity(locations, "gli-b6") == ("gli-b6", "block 6", "gli")
    assert dd.official_alias_identity(locations, "gli-b13") == ("gli-b13", "block 13", None)
    # A sourced alias with the same identity is kept instead of a duplicate.
    covered = row(dataset, "location_aliases.csv", "alias", "bahria town karachi")
    assert (covered["kind"], covered["derived"]) == ("common", "")


def official(data, key):
    return next(r for r in data["location_aliases.csv"]
                if r["location_key"] == key and r["kind"] == "official")


def test_official_alias_rules(data):
    data["location_aliases.csv"].remove(official(data, "clf-b7"))
    errors_for(data, "location clf-b7: no alias for its canonical name")
    fresh = copy.deepcopy(data)
    official(fresh, "pec-b6")["context_key"] = ""
    errors_for(fresh, "official alias must be ('pec-b6', 'block 6', 'pec')")
    fresh = copy.deepcopy(data)
    row(fresh, "location_aliases.csv", "alias", "dha 6")["kind"] = "official"
    errors_for(fresh, "exactly the derived name aliases")
    fresh = copy.deepcopy(data)
    fresh["location_aliases.csv"].remove(
        row(fresh, "location_aliases.csv", "alias", "bahria town karachi"))
    errors_for(fresh, "location btk: no alias for its canonical name")


@pytest.mark.parametrize(("change", "fragment"), [
    (lambda p: p["listings"]["DH6-104"].update(confirmed_days_ago=50), "under 45"),
    (lambda p: p["listings"]["DH6-104"].update(confirmed_days_ago=30), "over 30"),
    (lambda p: p.update(listings={"DH6-105": {"confirmed_days_ago": 35, "reason": "x"}}),
     "must be an active listing"),
    (lambda p: p.update(rerun="Reset on every run."), "preserve last_confirmed_at"),
    (lambda p: p.update(applies="every_run"), "insertion-time"),
    (lambda p: p.update(default_confirmed_days_ago=None), "default"),
])
def test_freshness_policy(data, change, fragment):
    change(data[dd.FRESHNESS_FILE])
    errors_for(data, fragment)


def test_freshness_policy_is_adopted_and_not_source_backed():
    policy = json.loads((dd.DEMO_DIR / dd.FRESHNESS_FILE).read_text("utf-8"))
    assert policy["status"] == "adopted"
    assert list(policy["listings"]) == ["DH6-104"]
    assert policy["listings"]["DH6-104"]["confirmed_days_ago"] == 35
    assert "not name it" in policy["authority"]


def test_cli_reports_success(capsys):
    assert dd.main([str(dd.DEMO_DIR)]) == 0
    assert "Demo dataset valid." in capsys.readouterr().out
