"""Task 8.1 offline tests: gate, tolerances, ladder labels, scoring and the R-25 ranking
rules of app.search (no database). All data is fictional."""

from datetime import UTC, datetime, timedelta

import pytest

from app import search
from app.search import Hit, Request, Row

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def entry(value, **extra):
    return {"value": value, "source": "text", "confidence": 1.0, "confirmed": True, **extra}


def row(code="DH6-101", *, price=78_000_000, kind="house", sqyd=500, sqft=4500, beds=4,
        baths=5, location="ph6", status="active", purpose="sale", photos=0, age_days=0):
    return Row(id=f"id-{code}", listing_code=code, status=status, purpose=purpose,
               property_type=kind, category="residential", location_id=location,
               price_pkr=price, area_value=sqyd or sqft, area_unit="sq_yd" if sqyd else "sq_ft",
               area_sqft=sqft, size_nominal_sqyd=sqyd, bedrooms=beds, bathrooms=baths,
               construction_status="ready", possession_status=None, investment_tags=(),
               last_confirmed_at=NOW - timedelta(days=age_days), photos=photos)


S2 = {
    "purpose": entry("sale", source="inferred", confirmed=False, echo=["inferred"]),
    "property_types": entry(["house"]),
    "location": entry({"nodes": ["ph6"], "mode": "EXACT", "city": "KHI",
                       "status": "RESOLVED"}),
    "size": entry({"value": 500, "unit": "sq_yd", "sqft": 4500, "nominal_sqyd": 500,
                   "mode": "target", "min_sqft": None, "max_sqft": None}),
    "budget": entry({"min": None, "max": 80_000_000, "mode": "max", "period": "one_time",
                     "stated_pkr": 80_000_000}),
}


# --- gate ----------------------------------------------------------------------------

def test_the_gate_passes_a_complete_request_with_an_inferred_purpose():
    assert search.gate(S2) == []


@pytest.mark.parametrize(("drop", "missing"), [
    ("purpose", ["purpose"]),
    ("location", ["location"]),
])
def test_the_gate_names_what_is_missing(drop, missing):
    slots = {k: v for k, v in S2.items() if k != drop}
    assert search.gate(slots) == missing


def test_the_gate_needs_a_type_budget_or_size():
    slots = {k: S2[k] for k in ("purpose", "location")}
    assert search.gate(slots) == ["type_budget_or_size"]
    assert search.gate({}) == ["purpose", "location", "type_budget_or_size"]


def test_an_open_clarification_blocks_the_search():
    assert search.gate(S2, pending_open=True) == ["pending_clarification"]


# --- request -------------------------------------------------------------------------

def test_request_from_slots():
    request = Request.from_slots({
        **S2,
        "property_types": entry(["apartment"]),
        "budget": entry({"min": None, "max": 72_750_000, "mode": "max",
                         "relative": "cheaper", "no_budget_relaxation": True}),
        "excluded_property_ids": entry(["a", "b"], source="inferred"),
        "construction": entry(["ready", "not-an-enum"]),
        "unmapped_prefs": entry([{"raw": "school ke qareeb", "slot_hint": "soft_prefs"}]),
    })
    assert request.types == ("apartment", "penthouse")  # R17: apartment includes penthouse
    assert request.no_budget_relaxation and request.excluded == {"a", "b"}
    assert request.construction == ("ready",)
    assert request.unmapped == ("school ke qareeb",)
    assert request.echo == (("purpose", ("inferred",)),)


def test_a_category_without_types_allows_its_types():
    request = Request.from_slots({"category": entry("commercial")})
    assert set(request.types) == {"shop", "office", "showroom", "warehouse", "building",
                                  "floor"}


# --- tolerances ----------------------------------------------------------------------

REQ = Request.from_slots(S2)


@pytest.mark.parametrize(("price", "fits", "relaxed_fits", "above"), [
    (78_000_000, True, True, False),
    (84_000_000, True, True, True),      # <= 8 cr x 1.05: tolerance, above budget
    (84_000_001, False, True, True),     # needs the budget step (8.8 cr x 1.05)
    (92_400_000, False, True, True),
    (92_400_001, False, False, True),
])
def test_price_tolerance_and_budget_step(price, fits, relaxed_fits, above):
    r = row(price=price)
    assert search.price_fits(r, REQ) is fits
    assert search.price_fits(r, REQ, relaxed=True) is relaxed_fits
    assert search.above_budget(r, REQ) is above


def test_min_budget_floor_is_ninety_percent():
    request = Request.from_slots({"budget": entry({"min": 450_000, "max": 550_000,
                                                   "stated_pkr": 500_000})})
    assert search.price_fits(row(price=405_000), request)
    assert not search.price_fits(row(price=404_999), request)
    assert search.price_fits(row(price=577_500), request)
    assert not search.price_fits(row(price=577_501), request)


@pytest.mark.parametrize(("sqyd", "fits", "relaxed_fits"), [
    (500, True, True), (450, True, True), (550, True, True), (449, False, True),
    (400, False, True), (399, False, False), (1000, False, False),
])
def test_size_tolerance_sq_yd_types(sqyd, fits, relaxed_fits):
    r = row(sqyd=sqyd, sqft=sqyd * 9)
    assert search.size_fits(r, REQ) is fits
    assert search.size_fits(r, REQ, relaxed=True) is relaxed_fits


def test_size_tolerance_sq_ft_types_is_fifteen_percent():
    request = Request.from_slots({"size": entry({"sqft": 2000, "mode": "target"})})
    assert search.size_fits(row(kind="office", sqyd=None, sqft=2300), request)
    assert not search.size_fits(row(kind="office", sqyd=None, sqft=2301), request)
    assert search.size_fits(row(kind="office", sqyd=None, sqft=1700), request)


def test_bahria_nominal_bucket_admits_235_for_250():
    request = Request.from_slots({"size": entry({"nominal_sqyd": 250, "sqft": 2250,
                                                 "mode": "target"})})
    assert search.size_fits(row(sqyd=235, sqft=2115), request)   # BENCHMARK.md CT-07
    assert search.size_fits(row(sqyd=250, sqft=2448), request)   # 272 actual, 250 nominal


def test_size_min_and_range_modes():
    minimum = Request.from_slots({"size": entry({"nominal_sqyd": 500, "sqft": 4500,
                                                 "mode": "min", "min_sqft": 4500})})
    assert search.size_fits(row(sqyd=1000, sqft=9000), minimum)
    assert not search.size_fits(row(sqyd=400, sqft=3600), minimum)
    between = Request.from_slots({"size": entry({"sqft": 2700, "mode": "range",
                                                 "min_sqft": 2700, "max_sqft": 4500})})
    assert search.size_fits(row(sqyd=500, sqft=4500), between)
    assert not search.size_fits(row(sqyd=600, sqft=5400), between)


def test_beds_minimum_and_beds_step():
    request = Request.from_slots({"beds_min": entry(4)})
    assert search.beds_fit(row(beds=4), request)
    assert not search.beds_fit(row(beds=3), request)
    assert search.beds_fit(row(beds=3), request, relaxed=True)
    assert not search.beds_fit(row(beds=None), request)


def test_labels_name_the_steps_a_row_needs():
    assert search.labels(row(), REQ, "exact") == []
    assert search.labels(row(price=90_000_000), REQ, "exact") == ["budget"]
    assert search.labels(row(sqyd=420, sqft=3780), REQ, "exact") == ["size"]
    assert search.labels(row(), REQ, "neighbour") == ["neighbour"]
    assert search.labels(row(price=95_000_000), REQ, "exact") is None


def test_the_cheaper_rule_never_relaxes_the_budget():
    cheaper = Request.from_slots({**S2, "budget": entry({
        "min": None, "max": 72_750_000, "mode": "max", "relative": "cheaper",
        "no_budget_relaxation": True})})
    assert search.labels(row(price=75_000_000), cheaper, "exact") == []   # 5 % tolerance
    assert search.labels(row(price=76_500_000), cheaper, "exact") is None


# --- scoring and ranking (R-25) ------------------------------------------------------

def hit(code, *, place="exact", needed=(), above=False, price=78_000_000, age=0, shown=False):
    r = row(code, price=price, age_days=age)
    return Hit(r, place, list(needed), above, search.score(r, REQ, place, NOW), shown)


def codes(hits):
    return [h.row.listing_code for h in hits]


def test_in_budget_always_ranks_above_above_budget():
    ordered = search.rank([hit("A", above=True, price=84_000_000),
                           hit("B", place="neighbour", needed=["neighbour"])])
    assert codes(ordered) == ["B", "A"]  # owner decision: in-budget wins over exact


def test_exact_ranks_above_relaxed_within_the_same_budget_class():
    ordered = search.rank([hit("A", place="neighbour", needed=["neighbour"]), hit("B")])
    assert codes(ordered) == ["B", "A"]


def test_location_weights_and_freshness():
    assert search.LOCATION_SCORE == {"exact": 1.0, "descendant": 0.9, "neighbour": 0.6}
    assert sum(search.WEIGHTS.values()) == 100
    fresh, stale = hit("A"), hit("B", age=45)
    assert fresh.score > stale.score
    assert codes(search.rank([stale, fresh])) == ["A", "B"]


def test_closer_to_the_max_ranks_higher_under_budget():
    assert codes(search.rank([hit("A", price=60_000_000), hit("B", price=79_000_000)])) == [
        "B", "A"]


def test_ties_break_on_freshness_then_code():
    a, b = hit("B"), hit("A")
    assert codes(search.rank([a, b])) == ["A", "B"]


def test_shown_listings_outside_the_top_three_are_demoted():
    hits = [hit(f"X{i}", price=79_000_000 - i * 1_000_000, shown=i in (1, 3)) for i in range(5)]
    assert codes(search.rank(hits)) == ["X0", "X1", "X2", "X4", "X3"]
