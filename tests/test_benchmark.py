"""Task 5.3: the committed benchmark set (bench/) is valid against R16 section 4 and
the demo dataset, and the validator catches broken gold data. Offline; no database."""

import copy

import pytest

from app import benchmark


@pytest.fixture(scope="module")
def committed():
    return benchmark.load(), benchmark.load_demo()


@pytest.fixture
def data(committed):
    return copy.deepcopy(committed[0])


@pytest.fixture
def demo(committed):
    return committed[1]


def find(data, item_id):
    return next(i for items in data.values() for i in items if i["id"] == item_id)


def errors_for(data, demo):
    return "\n".join(benchmark.validate(data, demo))


def test_committed_set_is_valid(data, demo):
    assert benchmark.validate(data, demo) == []
    assert benchmark.summary(data) == {
        "customer_text": 30, "customer_voice": 30, "control": 5, "staff": 20}


def test_cli_reports_valid(capsys):
    assert benchmark.main([]) == 0
    assert "Benchmark set valid." in capsys.readouterr().out


def test_demo_scenarios_are_in_the_set(data):
    """Demo Scope scenarios 1-5 (with the CLAUDE.md DH8 overrides)."""
    def ids(item_id):
        return set(find(data, item_id)["expected_outcome"]["ids"])

    assert ids("CT-01") == {"DH6-108", "SHF-101", "PEC-101"}
    assert ids("CT-13") == ids("CV-06") == {"DH6-101", "DH6-102", "DH7-101"}
    assert find(data, "CT-27")["expected_outcome"]["listing"] == "DH6-102"
    assert find(data, "CT-28")["expected_outcome"]["type"] == "no_match"
    assert ids("CT-29") == {"DH8-101", "DH8-102"}
    assert find(data, "CV-29")["expected_outcome"]["type"] == "handoff"
    assert find(data, "ST-01")["expected_outcome"]["expected_code"] == "DH6-109"
    assert find(data, "ST-10")["expected_outcome"]["changes"] == {"price_pkr": 78_000_000}
    assert "DH6-109" in find(data, "ST-16")["expected_outcome"]["allowed"]


def test_spans_come_from_the_input(data):
    for items in data.values():
        for item in items:
            text = benchmark._spoken(item) if item["set"] != "staff" else item["input"]
            for value in (item.get("gold_extraction") or {}).values():
                if value.get("raw"):
                    assert value["raw"].lower() in text.lower(), item["id"]


@pytest.mark.parametrize(("item_id", "mutate", "message"), [
    ("CT-13", lambda i: i["expected_outcome"]["ids"].append("DH6-105"), "hidden listings"),
    ("CT-13", lambda i: i["expected_outcome"]["ids"].remove("DH6-102"), "clear matches"),
    ("CT-01", lambda i: i["expected_outcome"]["ids"].append("CLF-101"), "does not fit"),
    ("CT-18", lambda i: i["expected_outcome"]["search"].update(budget_max=400_000_000),
     "no_match but"),
    ("CT-02", lambda i: i["gold_extraction"]["location"].update(raw="Karsaz"), "not in the input"),
    ("CT-02", lambda i: i["gold_extraction"]["location"].update(value=["nowhere"]),
     "unknown location"),
    ("CT-25", lambda i: i["gold_extraction"]["location"].update(candidates=["clf-b6"]),
     "AMBIGUOUS"),
    ("CT-03", lambda i: i["gold_extraction"]["size"].update(sqft=500), "size sqft"),
    ("CT-04", lambda i: i["gold_extraction"]["budget"].update(period="one_time"), "period"),
    ("CT-04", lambda i: i["gold_extraction"].update(colour={"raw": "shops"}), "unknown slot"),
    ("CT-04", lambda i: i["expected_tools"].append({"name": "commit_change", "args": {}}),
     "not a customer_text tool"),
    ("CT-04", lambda i: i["expected_outcome"]["search"].update(budget_max=1), "does not follow"),
    ("CT-27", lambda i: i["expected_outcome"].update(listing="DH6-101"), "not shown position"),
    ("CT-30", lambda i: i.update(input="call me on 03001234567"), "phone-number-like"),
    ("CV-06", lambda i: i.update(paired_with=None), "paired items"),
    ("CV-06", lambda i: i["recording"].update(noise="street"), "noise"),
    ("CV-06", lambda i: i["recording"].update(target_seconds=90), "target_seconds"),
    ("CV-06", lambda i: i["gold_transcript"].update(native=""), "native script"),
    ("VC-01", lambda i: i["gold_extraction"].update(budget={"raw": None}), "a control expects"),
    ("ST-07", lambda i: i["expected_outcome"].update(missing=["price_pkr"]), "missing should be"),
    ("ST-07", lambda i: i["expected_outcome"].update(save_allowed=True), "Save only"),
    ("ST-01", lambda i: i["expected_outcome"].update(expected_code="DH6-101"), "new DH6-n"),
    ("ST-20", lambda i: i["expected_outcome"]["fields"].update(size_nominal_sqyd=300),
     "nominal 300"),
    ("ST-10", lambda i: i["expected_outcome"].update(confirm_required=False),
     "confirmation tap"),
    ("ST-10", lambda i: i["expected_tools"].append({"name": "search_properties", "args": {}}),
     "not a staff tool"),
    ("ST-16", lambda i: i["expected_outcome"].update(candidates=["DH6-101"]), ">= 2 candidates"),
    ("ST-11", lambda i: i.update(category="new_full"), "staff counts"),
])
def test_validator_catches_broken_gold(data, demo, item_id, mutate, message):
    mutate(find(data, item_id))
    assert message in errors_for(data, demo)


def test_text_coverage_targets_are_enforced(data, demo):
    for item in data["customer_text"]:
        item["gold_extraction"].pop("size", None)
    assert "units" in errors_for(data, demo)


def test_matches_uses_r17_tolerances(demo):
    house = demo["properties"]["DH6-103"]  # 9.5 crore, 500 sq yd, DHA Phase 6
    search = {"purpose": "sale", "property_types": ["house"], "locations": ["dha-ph6"],
              "budget_max": 92_000_000, "size_sqyd": 500}
    assert benchmark.matches(demo, house, search, relaxed=False)  # 9.5 <= 9.2 x 1.05
    search["budget_max"] = 85_000_000
    assert not benchmark.matches(demo, house, search, relaxed=False)
    assert benchmark.matches(demo, house, search, relaxed=True)  # budget +10 %
    near = demo["properties"]["DH7-101"]
    assert not benchmark.matches(demo, near, search, relaxed=False)
    assert benchmark.matches(demo, near, search, relaxed=True)  # neighbour step
    sold = demo["properties"]["DH6-105"]
    assert not benchmark.matches(demo, sold, search, relaxed=True)


def test_area_code_of_a_location(demo):
    assert benchmark.area_code(demo["locations"], "dha-ph6-bukhari") == "DH6"
    assert benchmark.area_code(demo["locations"], "btk-p10") == "BTK"
    assert benchmark.area_code(demo["locations"], "khi") is None
