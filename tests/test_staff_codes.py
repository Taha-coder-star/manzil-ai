"""Task 8.2 slice 3: duplicate warnings and advisory listing codes (pure;
docs/STAFF_EXTRACTION.md sections 4.4 and 4.5).

Rows are the agency's own listings: the caller reads them inside its tenant transaction
(app.staff_tools.load_listings, tested against two tenants in test_staff_codes_db.py).
Fixture spans are hand-written; nothing here claims model accuracy.
"""

import dataclasses
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app import benchmark, demo_dataset, location, staff_drafts
from app import staff_normalise as sn
from tests.test_staff_drafts import ST, build

DATA = demo_dataset.load()
GAZETTEER = location.Gazetteer.from_dataset(DATA)
GOLD = {i["id"]: i["expected_outcome"]
        for i in json.loads((Path(benchmark.BENCH_DIR) / "staff.json").read_text("utf-8"))}
SQFT = benchmark.SQFT_PER_UNIT


def rows():
    """The 30 demo listings in the shape staff_normalise compares (all statuses)."""
    out = []
    for code, p in benchmark.load_demo()["properties"].items():
        nominal = p["size_nominal_sqyd"]
        sqft = (Decimal(nominal) * 9 if nominal is not None
                else Decimal(p["area_value"]) * SQFT[p["area_unit"]])
        out.append({"code": code, "status": p["status"], "purpose": p["purpose"],
                    "property_type": p["property_type"], "location_id": p["location_key"],
                    "sqft": sqft, "price_pkr": p["price_pkr"], "bedrooms": p["bedrooms"]})
    return out


ROWS = rows()


def dups(draft, listings=ROWS):
    return [d["code"] for d in sn.find_duplicates(draft, listings, GAZETTEER)]


# --- the benchmark's duplicate items ------------------------------------------------------

def test_st02_is_a_possible_duplicate_of_bahria_101_and_still_saveable():
    draft = build(*ST["ST-02"])
    found = sn.find_duplicates(draft, ROWS, GAZETTEER)
    assert found == [{"code": "BTK-101", "status": "active",
                      "reasons": ["location", "size", "price", "beds"]}]
    assert GOLD["ST-02"]["duplicate_of"] == ["BTK-101"]
    annotated = sn.annotate([draft], ROWS, GAZETTEER)[0]
    assert annotated.duplicates == tuple(found)
    assert "duplicate" in annotated.warnings and annotated.blockers() == ()  # non-blocking


def test_st01_known_disagreement_with_the_gold_is_reported_not_tuned_away():
    """The gold says ST-01 duplicates nothing, yet DH6-101 (78 lakh, 4 bed, 500 yd, same
    phase) and the reserved DH6-106 are closer than BTK-101 is to ST-02. Q1 in
    docs/STAFF_EXTRACTION.md: the gold, not the rule, is to be corrected."""
    assert GOLD["ST-01"]["duplicate_of"] == []  # bench/staff.json is unchanged
    found = sn.find_duplicates(build(*ST["ST-01"]), ROWS, GAZETTEER)
    assert [(d["code"], d["status"]) for d in found] == [
        ("DH6-101", "active"), ("DH6-106", "reserved")]
    assert sn.annotate([build(*ST["ST-01"])], ROWS, GAZETTEER)[0].blockers() == ()


@pytest.mark.parametrize("item", [i for i in ST if i != "ST-01"])
def test_every_other_draft_item_matches_the_gold(item):
    assert dups(build(*ST[item])) == GOLD[item]["duplicate_of"]


# --- the rule ---------------------------------------------------------------------------

def draft_like(**overrides):
    spans = {"purpose": "sale", "property_type": "ghar", "location": "Phase 7",
             "size": "500 gaz", "price": "7.5 crore", "bedrooms": "4 bed"}
    spans.update(overrides)
    spans = {k: v for k, v in spans.items() if v is not None}
    return build(" ".join(spans.values()), spans)


def test_a_draft_matching_on_every_criterion_finds_the_listing():
    found = sn.find_duplicates(draft_like(), ROWS, GAZETTEER)
    assert [(d["code"], d["reasons"]) for d in found] == [
        ("DH7-101", ["location", "size", "price", "beds"])]


@pytest.mark.parametrize(("span", "field"), [
    ("location", "location"), ("size", "area_value"), ("price", "price_pkr"),
    ("property_type", "property_type")])
def test_a_draft_missing_a_compared_field_skips_the_check(span, field):
    assert dups(draft_like()) == ["DH7-101"]  # the control: it would match
    draft = draft_like(**{span: None})
    assert field in draft.missing
    assert sn.find_duplicates(draft, ROWS, GAZETTEER) == []


def test_a_draft_with_a_conflict_skips_the_check():
    conflicted = build("ghar Phase 7 500 gaz 7.5 crore final 7 crore 4 bed sale",
                       {"purpose": "sale", "property_type": "ghar", "location": "Phase 7",
                        "size": "500 gaz", "price": "7.5 crore", "bedrooms": "4 bed"})
    assert conflicted.conflicts and sn.find_duplicates(conflicted, ROWS, GAZETTEER) == []


@pytest.mark.parametrize(("price", "expected"), [
    ("7.8 crore", ["DH7-101"]),   # +4 %
    ("7.87 crore", ["DH7-101"]),  # +4.9 %
    ("7.9 crore", []),            # +5.3 %
    ("7.2 crore", ["DH7-101"]),   # -4 %
    ("7.1 crore", []),            # -5.3 %
])
def test_price_tolerance_is_five_percent_of_the_drafts_price(price, expected):
    # relative to the draft: |75 - p| / p <= 5 %
    assert dups(draft_like(price=price)) == expected


@pytest.mark.parametrize(("size", "expected"), [
    ("480 gaz", ["DH7-101"]),   # the 500 gaz bucket
    ("520 gaz", ["DH7-101"]),
    ("560 gaz", []),            # the 600 gaz bucket: 17 % larger
    ("400 gaz", []),
])
def test_house_sizes_compare_through_their_nominal_bucket(size, expected):
    assert dups(draft_like(size=size)) == expected


@pytest.mark.parametrize(("size", "expected"), [
    ("1300 sq ft", ["X"]),  # 7.7 % of the draft
    ("1320 sq ft", ["X"]),  # 9.1 %
    ("1340 sq ft", []),     # 10.4 %
    ("1000 sq ft", []),
])
def test_other_sizes_are_within_ten_percent_of_the_drafts(size, expected):
    office = {"code": "X", "status": "active", "purpose": "rent", "property_type": "office",
              "location_id": "pec-b2", "sqft": Decimal(1200), "price_pkr": 500000,
              "bedrooms": None}
    draft = build(f"office PECHS block 2 {size} 5 lakh rent",
                  {"purpose": "rent", "property_type": "office", "location": "PECHS block 2",
                   "size": size, "price": "5 lakh"})
    assert dups(draft, [office]) == expected


def test_different_bedrooms_are_not_duplicates_but_an_unknown_count_is_ignored():
    assert dups(draft_like(bedrooms="3 bed")) == []
    unknown = [dict(r, bedrooms=None) if r["code"] == "DH7-101" else r for r in ROWS]
    assert dups(draft_like(bedrooms="3 bed"), unknown) == ["DH7-101"]


def test_purpose_type_and_place_must_match_but_a_sub_place_counts():
    assert dups(draft_like(purpose="rent")) == []
    assert dups(draft_like(property_type="flat")) == []
    assert dups(draft_like(location="Phase 4")) == []
    # a draft for the phase meets a listing in a zone of that phase, and the reverse
    zone = {"code": "DH6-900", "status": "active", "purpose": "rent",
            "property_type": "office", "location_id": "dha-ph6-bukhari",
            "sqft": Decimal(1200), "price_pkr": 500000, "bedrooms": None}
    office = build("office dha phase 6 1200 sq ft 5 lakh rent",
                   {"purpose": "rent", "property_type": "office", "location": "DHA Phase 6",
                    "size": "1200 sq ft", "price": "5 lakh"})
    assert dups(office, [zone]) == ["DH6-900"]
    inside = build("office Bukhari Commercial 1200 sq ft 5 lakh rent",
                   {"purpose": "rent", "property_type": "office",
                    "location": "Bukhari Commercial", "size": "1200 sq ft",
                    "price": "5 lakh"})
    assert dups(inside, [dict(zone, location_id="dha-ph6")]) == ["DH6-900"]


def test_only_live_listings_count_as_duplicates():
    base = {"code": "DH7-900", "purpose": "sale", "property_type": "house",
            "location_id": "dha-ph7", "sqft": Decimal(4500), "price_pkr": 75_000_000,
            "bedrooms": 4}
    for status, hit in (("active", True), ("reserved", True), ("on_hold", True),
                        ("sold", False), ("rented", False), ("withdrawn", False),
                        ("archived", False)):
        assert bool(dups(draft_like(), [dict(base, status=status)])) is hit, status


def test_the_nominal_bucket_of_a_listing_is_what_is_compared():
    # a Bahria "250 yd" plot is 2,250 sq ft nominal even though its actual size is larger
    listing = {"code": "BTK-900", "status": "active", "purpose": "sale",
               "property_type": "house", "location_id": "btk-p10",
               "sqft": Decimal(250) * 9, "price_pkr": 31_000_000, "bedrooms": 4}
    assert dups(build(*ST["ST-19"]), [listing]) == ["BTK-900"]


def test_two_drafts_of_one_message_are_checked_against_each_other():
    text = "Phase 7 ghar 500 gaz 4 bed 7.5 crore sale\nPhase 7 ghar 500 gaz 4 bed 7.6 crore sale"
    first, second = text.split("\n")
    staff = staff_drafts.context([text], {1}, True)
    spans = {"purpose": "sale", "property_type": "ghar", "location": "Phase 7",
             "size": "500 gaz", "bedrooms": "4 bed"}
    drafts = sn.normalise_drafts({"drafts": [
        {"text": first, **spans, "price": "7.5 crore"},
        {"text": second, **spans, "price": "7.6 crore"}]},
        staff, GAZETTEER, marla_sqft=225, listings=[])["drafts"]
    assert [d.duplicates for d in drafts] == [
        ({"draft": 2, "reasons": ["location", "size", "price", "beds"]},),
        ({"draft": 1, "reasons": ["location", "size", "price", "beds"]},)]
    assert all("duplicate" in d.warnings and d.blockers() == () for d in drafts)


# --- listing codes ----------------------------------------------------------------------

CODES = [r["code"] for r in ROWS]


def code_of(draft, codes=CODES):
    return sn.proposed_codes([draft], codes, GAZETTEER).get(draft.ordinal)


def test_the_gazetteer_exposes_the_nearest_coded_ancestor():
    assert GAZETTEER.code_of("dha-ph6") == "DH6"
    assert GAZETTEER.code_of("dha-ph6-bukhari") == "DH6"  # a zone: its phase
    assert GAZETTEER.code_of("pec-b6") == "PEC"  # a block: its area
    assert GAZETTEER.code_of("khi") is None


@pytest.mark.parametrize(("item", "code"), [
    ("ST-01", "DH6-109"),   # DH6-101..108 exist (a sold and a reserved one included)
    ("ST-02", "BTK-107"),
    ("ST-03", "PEC-102"),
    ("ST-04", "CLF-105"),
    ("ST-05", "GLI-102"),
    ("ST-06", "DHC-102"),
    ("ST-07", "DH7-102"),
    ("ST-08", "SHF-102"),
    ("ST-19", "BTK-107"),
    ("ST-20", "GLI-102"),
])
def test_the_prefix_is_the_coded_ancestor_and_the_number_follows_every_listing(item, code):
    assert code_of(build(*ST[item])) == code
    expected = GOLD[item].get("expected_code")
    assert expected is None or expected == code  # the gold's own ST-01 code


def test_a_draft_without_a_resolved_location_gets_no_code():
    assert code_of(build(*ST["ST-09"])) is None


def test_all_statuses_count_and_a_new_prefix_starts_at_101():
    assert code_of(build(*ST["ST-01"]), CODES + ["DH6-150"]) == "DH6-151"
    assert code_of(build(*ST["ST-01"]), ["DH6-105", "DH7-900"]) == "DH6-106"
    first = build("ghar DHA Phase 1 500 gaz", {"property_type": "ghar", "location": "DHA Phase 1"})
    assert code_of(first) == "DH1-101"
    # a prefix below the demo convention still continues from 101
    assert code_of(first, ["DH1-007"]) == "DH1-101"


def test_proposals_in_one_segment_are_sequential_per_prefix():
    text = ("Phase 6 ghar 500 gaz 4 bed 8 crore\nPhase 6 ghar 500 gaz 4 bed 9 crore\n"
            "Clifton block 5 shop 350 sq ft 2.5 lakh rent\nPhase 6 ghar 1000 gaz 6 bed 18 crore")
    parts = text.split("\n")
    staff = staff_drafts.context([text], {1}, True)
    spans = [
        {"property_type": "ghar", "location": "Phase 6"},
        {"property_type": "ghar", "location": "Phase 6"},
        {"property_type": "shop", "location": "Clifton block 5"},
        {"property_type": "ghar", "location": "Phase 6"},
    ]
    result = sn.normalise_drafts(
        {"drafts": [{"text": t, **s} for t, s in zip(parts, spans, strict=True)]},
        staff, GAZETTEER, marla_sqft=225, listings=ROWS)
    assert [d.proposed_code for d in result["drafts"]] == [
        "DH6-109", "DH6-110", "CLF-105", "DH6-111"]


def test_a_proposed_code_is_advisory_and_reserves_nothing():
    codes = list(CODES)
    draft = build(*ST["ST-01"])
    assert code_of(draft, codes) == code_of(draft, codes) == "DH6-109"
    assert codes == CODES  # the caller's list is not extended
    # a second staff member drafting the same area is proposed the same code
    assert code_of(build(*ST["ST-01"]), codes) == "DH6-109"


def test_codes_follow_only_the_rows_the_caller_passes():
    """Tenant isolation: the pure functions see exactly the agency's rows they are
    given (app.staff_tools.load_listings reads them under RLS)."""
    other_agency = ["DH6-300"]
    assert code_of(build(*ST["ST-01"]), CODES) == "DH6-109"
    assert code_of(build(*ST["ST-01"]), CODES + other_agency) == "DH6-301"  # it is a row


def test_annotate_adds_codes_and_duplicates_without_changing_blockers():
    second = dataclasses.replace(build(*ST["ST-07"]), ordinal=2)
    out = sn.annotate([build(*ST["ST-01"]), second], ROWS, GAZETTEER)
    assert [d.proposed_code for d in out] == ["DH6-109", "DH7-102"]
    assert [bool(d.duplicates) for d in out] == [True, False]
    assert out[0].blockers() == () and out[1].blockers() == (
        "missing:price_pkr", "missing:bedrooms")
