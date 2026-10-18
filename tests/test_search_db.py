"""Task 8.1 acceptance tests: the search service on the seeded demo dataset (disposable
PostgreSQL, restricted manzil_app login, RLS on).

Done condition (Execution Plan 8.1): Scenario 1 = {DH6-108, SHF-101, PEC-101};
Scenario 2 = {DH6-101, DH6-102, DH7-101}; 3b = no match; 3c = {DH8-102, DH8-101 (above
budget)}; 0 hidden-status results. The scenarios run through the 7.3 requirement manager
(fake Gemini, fixed patches) exactly as a customer turn stores them; the bench gold sweep
checks every gold search spec (ids must be shown, allowed may be shown, nothing else).
All data is fictional.
"""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import json
import logging
from pathlib import Path

import pytest

from app import search, seed
from app.db import tenant_transaction
from app.provision_agency import create_agency
from tests.test_requirements_db import p, st  # noqa: F401  (st: the 7.3 fixture)
from tests.test_seed import ENV

CUSTOMER_A, CUSTOMER_B = "923000000101", "923000000102"
HIDDEN = {"DH6-105", "DH6-106", "CLF-104"}
BENCH = Path(__file__).resolve().parents[1] / "bench"


def conversation(st, sender):
    return st.query("SELECT id FROM conversation WHERE customer_wa_id = %s", (sender,))[0][0]


def find(st, sender):
    return search.search_conversation(st.conn, st.agency, conversation(st, sender),
                                      st.cache.get())


def shown(st, sender, result):
    search.record_shown(st.conn, st.agency, conversation(st, sender), result.search_run_id,
                        [h.row.id for h in result.hits[:search.TOP]])


def labelled(result):
    return [(h.row.listing_code, h.above_budget, h.needed) for h in result.hits]


def scenario_2(st):
    st.say("Assalam o alaikum, mujhe DHA Phase 6 mein 500 gaz ka ghar chahiye, budget aath "
           "crore tak hai.",
           [p("location", "set", "DHA Phase 6"), p("size", "set", "500 gaz"),
            p("property_types", "set", "ghar"), p("budget", "set", "budget aath crore tak")],
           sender=CUSTOMER_B)
    return find(st, CUSTOMER_B)


# --- the plan's golden scenarios ------------------------------------------------------

def test_scenario_1_office_on_rent_anywhere_in_karachi(st):
    st.say("Hi, I'm looking for an office on rent in Karachi, around 5 lakh per month.",
           [p("purpose", "set", "on rent"), p("property_types", "set", "office"),
            p("location", "set", "Karachi"), p("budget", "set", "around 5 lakh per month")],
           sender=CUSTOMER_A)
    result = find(st, CUSTOMER_A)

    assert result.status == search.OK
    assert set(result.codes) == {"DH6-108", "SHF-101", "PEC-101"}
    assert result.codes[0] == "DH6-108"  # exactly the stated 5 lakh
    assert all(not h.needed and not h.above_budget for h in result.hits)
    assert result.relaxations == []
    run = st.query("SELECT requirement_version, total_matches, relaxations, filters "
                   "FROM search_run WHERE id = %s", (result.search_run_id,))[0]
    assert run[:3] == (1, 3, [])
    assert run[3]["purpose"] == "rent" and run[3]["types"] == ["office"]


def test_scenario_2_two_exact_matches_and_the_labelled_neighbour(st):
    result = scenario_2(st)

    assert result.status == search.OK
    # Set membership is the success criterion (Demo Scope 5); order: exact before the
    # relaxed neighbour, and closer to the max first (8.0 cr before 7.8 cr, R17 step 8).
    assert labelled(result) == [
        ("DH6-102", False, []), ("DH6-101", False, []), ("DH7-101", False, ["neighbour"])]
    assert result.hits[2].place == "neighbour"
    assert result.relaxations == ["neighbour"]
    assert ("purpose", ("inferred",)) in result.echo  # crore magnitude: echo the purpose
    assert result.excluded_reasons.get("over_budget") == 2  # DH6-103 (9.5 cr), DH6-104 (18 cr)


def test_scenario_3b_cheaper_is_an_honest_no_match(st):
    first = scenario_2(st)
    shown(st, CUSTOMER_B, first)
    st.say("Kuch sasta dikhao", [p("budget", "relative", "Kuch sasta")], sender=CUSTOMER_B)
    budget = st.state()[1]["budget"]["value"]
    assert budget["max"] == 72_750_000 and budget["no_budget_relaxation"]

    result = find(st, CUSTOMER_B)
    assert result.status == search.NO_MATCH and result.hits == []
    assert result.relaxations == []
    assert result.excluded_reasons["already_shown"] == 3  # DH6-101 / 102 + neighbour DH7-101
    # R17 edge 8: the lowest ACTIVE price in the requested scope, the shown ones too
    assert result.lowest_price_pkr == 78_000_000          # DH6-101 (live 2026-10-18: not 9.5 cr)


def test_scenario_3c_phase_8_added_with_the_cheaper_budget_kept(st):
    first = scenario_2(st)
    shown(st, CUSTOMER_B, first)
    st.say("Kuch sasta dikhao", [p("budget", "relative", "Kuch sasta")], sender=CUSTOMER_B)
    no_match = find(st, CUSTOMER_B)
    assert no_match.status == search.NO_MATCH
    st.say("Phase 8 mein bhi dekho", [p("location", "add", "Phase 8")], sender=CUSTOMER_B)
    assert st.state()[1]["budget"]["value"]["max"] == 72_750_000  # unchanged

    result = find(st, CUSTOMER_B)
    assert labelled(result) == [("DH8-102", False, []), ("DH8-101", True, [])]
    assert "DH7-101" not in result.codes  # shown, excluded by the cheaper rule


# --- R-24 assertion, gate, isolation ----------------------------------------------------

def test_injected_invalid_rows_are_dropped_and_logged(st, monkeypatch, caplog):
    real = search.fetch_rows

    def poisoned(conn, request, scope):
        rows = real(conn, request, scope)
        good = rows[0]
        bad = [good.__class__(**{**good.__dict__, "id": "bad-status", "status": "sold"}),
               good.__class__(**{**good.__dict__, "id": "bad-purpose", "purpose": "rent"}),
               good.__class__(**{**good.__dict__, "id": "bad-type",
                                 "property_type": "apartment"})]
        return rows + bad

    monkeypatch.setattr(search, "fetch_rows", poisoned)
    with caplog.at_level(logging.ERROR, logger="app.search"):
        result = scenario_2(st)
    # The injected rows counted as exact matches, so the ladder stopped; the assertion
    # (the last line of defence) then removed all three: nothing invalid is shown.
    assert result.codes == ["DH6-102", "DH6-101"]
    assert result.dropped == 3
    assert {"status", "purpose", "type"} == {
        r.getMessage().rsplit("(", 1)[1].rstrip(")") for r in caplog.records}


def test_an_ambiguous_location_blocks_the_search(st):
    st.say("Block 6 mein ghar, 3 crore tak, khareedna hai",
           [p("purpose", "set", "khareedna"), p("property_types", "set", "ghar"),
            p("location", "set", "Block 6"), p("budget", "set", "3 crore tak")],
           sender=CUSTOMER_A)
    assert st.pending()[0][1] == "open"
    result = find(st, CUSTOMER_A)
    assert result.status == search.CLARIFY
    assert "pending_clarification" in result.missing
    assert st.query("SELECT count(*) FROM search_run")[0][0] == 0


def test_a_conversation_without_requirements_is_not_searched(st):
    st.say("salaam", sender=CUSTOMER_A)
    result = find(st, CUSTOMER_A)
    assert result.status == search.CLARIFY
    assert result.missing == ["purpose", "location", "type_budget_or_size"]


def test_another_agencys_listings_never_appear(st):
    # The seed only writes to an agency with the demo name: a second one, other slug.
    other = create_agency(st.conn, seed.DEMO_AGENCY_NAME, "other-demo", "100000000000009")
    assert seed.run(other, st.admin_url, st.app_url, ENV, apply=True).conflicts == []
    result = scenario_2(st)
    ids = [h.row.id for h in result.hits]
    assert len(ids) == 3
    assert st.query("SELECT count(*) FROM property WHERE id::text = ANY(%s)", (ids,))[0][0] == 3
    assert st.query("SELECT count(*) FROM property WHERE id::text = ANY(%s)", (ids,),
                    agency_id=other)[0][0] == 0


def test_only_shown_searches_feed_the_cheaper_rule(st):
    first = scenario_2(st)
    find(st, CUSTOMER_B)  # a second, unsent search of the same state
    shown(st, CUSTOMER_B, first)
    find(st, CUSTOMER_B)  # newest run has no shown items: 7.3 still sees the shown one
    st.say("Kuch sasta dikhao", [p("budget", "relative", "Kuch sasta")], sender=CUSTOMER_B)
    assert st.state()[1]["budget"]["value"]["cheapest_shown"] == 75_000_000


def test_shown_items_demote_on_refinement(st):
    first = scenario_2(st)
    # The turn's own reply (a full request) already showed the three listings (customer
    # test 2026-10-18), so nothing is recorded again here.
    rows = st.query("SELECT position, property_id::text FROM shown_item ORDER BY position")
    assert sorted(r[1] for r in rows) == sorted(h.row.id for h in first.hits)
    again = find(st, CUSTOMER_B)
    assert all(h.shown for h in again.hits)  # all three still rank top 3: not demoted
    assert again.codes == first.codes


# --- bench gold sweep -------------------------------------------------------------------

def gold_items():
    for name in ("customer_text.json", "customer_voice.json"):
        for item in json.loads((BENCH / name).read_text(encoding="utf-8")):
            if item["expected_outcome"].get("type") in ("results", "no_match"):
                yield item


def slots_for(st, spec):
    def e(value):
        return {"value": value, "source": "text", "confidence": 1.0, "confirmed": True}
    gz = st.cache.get()
    nodes = [st.ids[k] for k in spec["locations"]]
    ids = {str(k): k for k in gz.places}
    modes = {gz.mode_of(ids[n]) for n in nodes}
    slots = {
        "purpose": e(spec["purpose"]),
        "property_types": e(spec["property_types"]),
        "location": e({"nodes": nodes, "status": "RESOLVED", "city": "KHI",
                       "mode": "ANYWHERE_IN_CITY" if "ANYWHERE_IN_CITY" in modes
                       else ("SUBTREE" if "SUBTREE" in modes else "EXACT")}),
    }
    if spec.get("budget_min") is not None or spec.get("budget_max") is not None:
        lo, hi = spec.get("budget_min"), spec.get("budget_max")
        slots["budget"] = e({"min": lo, "max": hi,
                             "stated_pkr": (lo + hi) // 2 if lo and hi else hi or lo,
                             # the gold's "exclude" is scenario 3b/3c: the cheaper rule
                             "no_budget_relaxation": bool(spec.get("exclude"))})
    if spec.get("size_sqyd"):
        slots["size"] = e({"nominal_sqyd": spec["size_sqyd"], "sqft": spec["size_sqyd"] * 9,
                           "mode": "target"})
    if spec.get("size_sqft"):
        slots["size"] = e({"sqft": spec["size_sqft"], "mode": "target"})
    if spec.get("beds_min"):
        slots["beds_min"] = e(spec["beds_min"])
    if spec.get("exclude"):
        codes = st.query("SELECT id::text FROM property WHERE listing_code = ANY(%s)",
                         (spec["exclude"],))
        slots["excluded_property_ids"] = e([c[0] for c in codes])
    return slots


@pytest.fixture
def conv(st):
    st.say("buy karna hai", [p("purpose", "set", "buy")], sender=CUSTOMER_A)  # version 1
    st.conv = conversation(st, CUSTOMER_A)
    return st


def test_every_bench_gold_search_matches(conv):
    st, failures = conv, []
    for item in gold_items():
        outcome = item["expected_outcome"]
        slots = slots_for(st, outcome["search"])
        with tenant_transaction(st.conn, st.agency):
            result = search.run(st.conn, st.cache.get(), conversation_id=st.conv,
                                requirement_version=1, slots=slots)
        got = set(result.codes)
        if outcome["type"] == "no_match":
            ok = result.status == search.NO_MATCH
        else:
            ok = set(outcome["ids"]) <= got <= set(outcome["ids"]) | set(outcome["allowed"])
        if not ok:
            failures.append((item["id"], sorted(got)))
    assert failures == []


def test_no_hidden_status_listing_in_any_search(conv):
    st = conv
    hidden = dict(st.query("SELECT listing_code, status FROM property "
                           "WHERE listing_code = ANY(%s)", (sorted(HIDDEN),)))
    assert hidden == {"DH6-105": "sold", "DH6-106": "reserved", "CLF-104": "rented"}
    for item in gold_items():
        slots = slots_for(st, item["expected_outcome"]["search"])
        with tenant_transaction(st.conn, st.agency):
            result = search.run(st.conn, st.cache.get(), conversation_id=st.conv,
                                requirement_version=1, slots=slots)
        assert not HIDDEN & set(result.codes), item["id"]
