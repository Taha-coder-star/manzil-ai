"""Task 8.2 slice 4b: candidate matching for find_listings and the update rules of
propose_update (pure, over the 30 demo listings; docs/STAFF_EXTRACTION.md section 7)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app import benchmark, demo_dataset, location
from app import staff_normalise as sn

GAZETTEER = location.Gazetteer.from_dataset(demo_dataset.load())
GOLD = {i["id"]: i["expected_outcome"]
        for i in json.loads((Path(benchmark.BENCH_DIR) / "staff.json").read_text("utf-8"))}


def rows():
    out = []
    for code, p in benchmark.load_demo()["properties"].items():
        nominal = p["size_nominal_sqyd"]
        sqft = (Decimal(nominal) * 9 if nominal is not None
                else Decimal(p["area_value"]) * benchmark.SQFT_PER_UNIT[p["area_unit"]])
        out.append({"id": code, "version": 1, "code": code, "status": p["status"],
                    "purpose": p["purpose"], "property_type": p["property_type"],
                    "location_id": p["location_key"], "sqft": sqft,
                    "price_pkr": p["price_pkr"], "bedrooms": p["bedrooms"]})
    return out


ROWS = rows()


def find(**criteria):
    return sn.find_candidates(criteria, ROWS, GAZETTEER)


def codes(result):
    return [r["code"] for r in result["candidates"]]


# --- the benchmark's update-by-description items -------------------------------------------

def test_st14_a_single_portion_in_north_nazimabad():
    result = find(location="North Nazimabad", property_type="portion")
    assert result["status"] == "single" and codes(result) == ["NNZ-101"]
    assert GOLD["ST-14"]["listing"] == "NNZ-101"


def test_st15_a_similar_place_is_asked_about_not_assumed():
    result = find(location="Sports City", property_type="plot")
    assert result == {"error": "location_unclear", "candidates": ["btk-sports-city"]}
    assert GOLD["ST-15"]["listing"] == "BTK-106"  # the gold wants it; the resolver does not
    sure = find(location="Bahria Sports City", property_type="plot")  # a resolved place
    assert sure["status"] == "single" and codes(sure) == ["BTK-106"]


def test_st16_dha_6_houses_are_several_and_never_chosen_for_the_staff():
    result = find(location="DHA 6", property_type="ghar")
    gold = GOLD["ST-16"]
    assert result["status"] == "several"
    assert set(gold["candidates"]) <= set(codes(result)) <= set(
        gold["candidates"] + gold["allowed"])
    assert codes(result) == ["DH6-101", "DH6-102", "DH6-103", "DH6-104", "DH6-106"]  # active first


def test_st17_bahria_250_gaz_houses_match_the_nominal_size():
    result = find(location="Bahria", property_type="ghar", size="250 gaz")
    assert result["status"] == "several" and codes(result) == GOLD["ST-17"]["candidates"]


def test_st18_phase_8_flats():
    result = find(location="Phase 8", property_type="flat")
    assert codes(result) == GOLD["ST-18"]["candidates"]


# --- the rest of the matching ------------------------------------------------------------

def test_a_listing_code_is_a_direct_target_in_any_status():
    assert (find(code="#dh6-105")["status"], codes(find(code="#dh6-105"))) == (
        "direct", ["DH6-105"])  # sold: history can still be named
    assert find(code="DH6-999") == {"status": "none", "candidates": []}


def test_sold_and_rented_listings_are_not_candidates_by_description():
    assert "DH6-105" not in codes(find(location="DHA 6", property_type="ghar"))
    assert "CLF-104" not in codes(find(location="Clifton", property_type="shop"))


def test_every_criterion_narrows_the_candidates():
    assert codes(find(location="DHA 6", property_type="ghar", purpose="sale",
                      price="7.8 crore", bedrooms="4 bed")) == ["DH6-101", "DH6-106"]
    assert find(location="DHA 6", property_type="ghar", price="1 crore")["status"] == "none"
    assert codes(find(location="DHA 6", purpose="rent")) == ["DH6-107", "DH6-108"]


def test_more_than_ten_candidates_ask_for_a_narrower_description():
    many = [dict(ROWS[0], code=f"DH6-{n}") for n in range(300, 312)]
    result = sn.find_candidates({"location": "DHA 6"}, many, GAZETTEER)
    assert result["status"] == "too_many" and len(result["candidates"]) == 10


@pytest.mark.parametrize(("criteria", "error"), [
    ({"location": "Moon Colony"}, "location_unclear"),
    ({"location": "Block 2"}, "location_unclear"),   # two places
    ({"property_type": "ghar ya flat"}, "type_unclear"),
    ({"property_type": "mehal wala"}, "type_unclear"),
    ({"size": "kafi bara"}, "size_unclear"),
    ({"price": "8 crore ya 9 crore"}, "price_unclear"),
    ({"bedrooms": "bohat"}, "bedrooms_unclear"),
    ({"purpose": "kuch"}, "purpose_unclear"),
])
def test_an_unusable_criterion_is_an_error_not_a_guess(criteria, error):
    assert find(**criteria)["error"] == error


# --- parse_status and check_update --------------------------------------------------------

@pytest.mark.parametrize(("span", "status"), [
    ("sold ho gaya", "sold"), ("bik gaya", "sold"), ("rent ho gaya", "rented"),
    ("rented", "rented"), ("#CLF-103 reserved", "reserved"), ("withdrawn", "withdrawn"),
    ("on hold", "on_hold"), ("available", "active"), ("sold or rented", None), ("xyz", None)])
def test_status_words(span, status):
    assert sn.parse_status(span) == status


SALE = {"id": "p", "version": 3, "code": "DH6-102", "status": "active", "purpose": "sale",
        "price_pkr": 80_000_000}
RENT = dict(SALE, code="NNZ-101", purpose="rent", price_pkr=85_000)


def update(prop, **changes):
    return sn.check_update(prop, changes)


def test_a_price_change_is_stored_with_its_old_and_new_value():
    ok = update(SALE, price_pkr=78_000_000)
    assert ok == {"changes": {"price_pkr": {"from": 80_000_000, "to": 78_000_000}},
                  "warnings": []}


@pytest.mark.parametrize(("prop", "change", "error"), [
    (SALE, {"price_pkr": 80_000_000}, "no_change"),
    (SALE, {"status": "active"}, "no_change"),
    (SALE, {"price_pkr": 0}, "bad_amount"),
    (SALE, {"status": "rented"}, "rented_needs_rent"),
    (RENT, {"status": "sold"}, "sold_needs_sale"),
    (dict(SALE, status="sold"), {"price_pkr": 70_000_000}, "not_live"),
    (dict(SALE, status="sold"), {"status": "reserved"}, "not_live"),
    (dict(SALE, status="archived"), {"status": "sold"}, "not_live"),
])
def test_a_change_must_be_a_real_permitted_change(prop, change, error):
    assert update(prop, **change)["error"] == error


@pytest.mark.parametrize("status", ["sold", "reserved", "withdrawn", "on_hold"])
def test_a_live_listing_may_move_to_any_other_status(status):
    assert update(SALE, status=status)["changes"]["status"] == {"from": "active", "to": status}


def test_a_closed_listing_can_only_come_back_to_active():
    sold = dict(SALE, status="sold")
    assert update(sold, status="active")["changes"]["status"] == {"from": "sold", "to": "active"}
    assert update(dict(RENT, status="rented"), status="active")["changes"]


def test_a_price_ten_times_off_is_a_warning_not_a_refusal():
    assert update(SALE, price_pkr=780_000_000)["warnings"] == ["price_unusual"]
    assert update(SALE, price_pkr=7_800_000)["warnings"] == ["price_unusual"]
    assert update(SALE, price_pkr=78_000_000)["warnings"] == []


def test_two_changes_are_checked_together():
    both = update(SALE, price_pkr=78_000_000, status="reserved")
    assert set(both["changes"]) == {"price_pkr", "status"}
    assert update(SALE, price_pkr=78_000_000, status="rented")["error"] == "rented_needs_rent"
