"""Task 9.1 acceptance on the seeded demo dataset (disposable PostgreSQL, restricted
manzil_app login, RLS on): the 7.3 requirement manager stores the scenario, 8.1 searches
it, cards.load_listings re-fetches the public rows, render_search renders them. Every
price, size and location on a card equals the DB row; hidden and other-agency listings
never render. All data is fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import pytest

from app import cards, details, grounding, render, search
from app.provision_agency import create_agency
from tests.test_requirements_db import p, st  # noqa: F401  (st: the 7.3 fixture)
from tests.test_search_db import CUSTOMER_A, CUSTOMER_B, HIDDEN, conversation, shown

SCENARIO_1 = "Hi, I'm looking for an office on rent in Karachi, around 5 lakh per month."
SCENARIO_2 = ("Assalam o alaikum, mujhe DHA Phase 6 mein 500 gaz ka ghar chahiye, budget "
              "aath crore tak hai.")


def slots(st, sender):
    return st.query("SELECT slots FROM requirement_state WHERE conversation_id = %s",
                    (conversation(st, sender),))[0][0]


def answer(st, sender, text):
    """What 10.1 will do: search, re-fetch the public rows, render in the customer's
    language."""
    gz = st.cache.get()
    result = search.search_conversation(st.conn, st.agency, conversation(st, sender), gz)
    data = result.payload(gz)
    listings = cards.load_listings(st.conn, st.agency, [r["listing_code"]
                                                        for r in data["results"]])
    plan = render.render_search(data, listings, render.detect_language(text),
                                request=search.Request.from_slots(slots(st, sender)),
                                gazetteer=gz)
    return result, plan, listings


def db_rows(st, codes):
    return {r[0]: r[1:] for r in st.query(
        "SELECT v.listing_code, v.price_pkr, v.area_value, v.area_unit, "
        "COALESCE(l.display_name, l.name) FROM v_public_listing v "
        "JOIN location l ON l.id = v.location_id WHERE v.listing_code = ANY(%s)",
        (list(codes),))}


def texts(plan):
    return "\n".join(t for payload in plan.payloads for t in grounding.payload_texts(payload))


def assert_cards_match_db(st, plan):
    rows = db_rows(st, [code for _, _, code in plan.shown])
    assert set(rows) == {code for _, _, code in plan.shown}
    rendered = texts(plan)
    for code, (price, area, unit, place) in rows.items():
        stand_in = cards.Listing(code, None, None, price, area, unit, None, None, None, None,
                                 place, None)
        assert cards.price_text(price, None) in rendered, code
        assert cards.area_text(stand_in) in rendered, code
        assert place in rendered, code
    assert not HIDDEN & {code for _, _, code in plan.shown}


def scenario_2(st):
    st.say(SCENARIO_2,
           [p("location", "set", "DHA Phase 6"), p("size", "set", "500 gaz"),
            p("property_types", "set", "ghar"), p("budget", "set", "budget aath crore tak")],
           sender=CUSTOMER_B)
    return answer(st, CUSTOMER_B, SCENARIO_2)


def test_scenario_1_english_cards_match_the_db(st):
    st.say(SCENARIO_1,
           [p("purpose", "set", "on rent"), p("property_types", "set", "office"),
            p("location", "set", "Karachi"), p("budget", "set", "around 5 lakh per month")],
           sender=CUSTOMER_A)
    _, plan, _ = answer(st, CUSTOMER_A, SCENARIO_1)

    assert plan.language == render.ENGLISH and plan.kind == render.RESULTS
    assert {code for _, _, code in plan.shown} == {"DH6-108", "SHF-101", "PEC-101"}
    assert texts(plan).startswith(
        "Here are offices for rent in Karachi, around 5 lakh per month:")
    assert "for sale" not in texts(plan)
    assert_cards_match_db(st, plan)


def test_scenario_2_roman_urdu_with_the_labelled_neighbour(st):
    result, plan, _ = scenario_2(st)

    assert plan.language == render.ROMAN_URDU
    assert [code for _, _, code in plan.shown] == ["DH6-102", "DH6-101", "DH7-101"]
    assert [pid for _, pid, _ in plan.shown] == [h.row.id for h in result.hits]
    rendered = texts(plan)
    assert rendered.startswith(
        "Ji, DHA Phase 6 mein 500 gaz, 8 crore tak -- ye options hain:")
    assert "DH7-101 · Ghar · sale · DHA Phase 7 · PKR 7.5 crore · Phase 7 -- qareeb" in rendered
    assert rendered.count("qareeb") == 2  # the summary line and DH7-101's own card
    assert_cards_match_db(st, plan)


def test_scenario_3b_and_3c(st):
    first, _, _ = scenario_2(st)
    shown(st, CUSTOMER_B, first)
    st.say("Kuch sasta dikhao", [p("budget", "relative", "Kuch sasta")], sender=CUSTOMER_B)
    _, no_match, _ = answer(st, CUSTOMER_B, "Kuch sasta dikhao")
    assert no_match.kind == render.NO_MATCH_KIND
    assert texts(no_match).startswith(
        "DHA Phase 6 mein is se kam price ka 500 gaz ghar abhi available nahi. "
        "Yahan price 9.5 crore se shuru hoti hai.")

    st.say("Phase 8 mein bhi dekho", [p("location", "add", "Phase 8")], sender=CUSTOMER_B)
    _, phase_8, _ = answer(st, CUSTOMER_B, "Phase 8 mein bhi dekho")
    assert [code for _, _, code in phase_8.shown] == ["DH8-102", "DH8-101"]
    assert "DH8-101 · Ghar · sale · DHA Phase 8 · PKR 7.4 crore · budget se thoda upar" in \
        texts(phase_8)
    assert_cards_match_db(st, phase_8)


def test_a_listing_sold_after_the_search_is_not_rendered(st):
    """The answer-time re-fetch (R-26): the payload still names DH6-102; the public view no
    longer has it, so it is dropped and the positions close up."""
    st.say(SCENARIO_2,
           [p("location", "set", "DHA Phase 6"), p("size", "set", "500 gaz"),
            p("property_types", "set", "ghar"), p("budget", "set", "budget aath crore tak")],
           sender=CUSTOMER_B)
    gz = st.cache.get()
    data = search.search_conversation(st.conn, st.agency, conversation(st, CUSTOMER_B),
                                      gz).payload(gz)
    st.query("UPDATE property SET status = 'sold' WHERE listing_code = 'DH6-102' "
             "RETURNING id")
    listings = cards.load_listings(st.conn, st.agency, [r["listing_code"]
                                                        for r in data["results"]])
    plan = render.render_search(data, listings, render.ROMAN_URDU, gazetteer=gz,
                                request=search.Request.from_slots(slots(st, CUSTOMER_B)))
    assert plan.dropped == ["DH6-102"]
    assert [(n, code) for n, _, code in plan.shown] == [(1, "DH6-101"), (2, "DH7-101")]
    assert "DH6-102" not in texts(plan)


def test_an_ambiguous_location_renders_its_candidates_and_no_listing(st):
    st.say("Block 6 mein ghar, 3 crore tak, khareedna hai",
           [p("purpose", "set", "khareedna"), p("property_types", "set", "ghar"),
            p("location", "set", "Block 6"), p("budget", "set", "3 crore tak")],
           sender=CUSTOMER_A)
    [(slot, status, _, candidates)] = st.pending()
    assert (slot, status) == ("location", "open") and len(candidates) >= 2
    gz = st.cache.get()
    result = search.search_conversation(st.conn, st.agency, conversation(st, CUSTOMER_A), gz)
    plan = render.render_search(result.payload(gz), [], render.ROMAN_URDU, gazetteer=gz,
                                pending={"slot": slot, "candidates": candidates})
    assert plan.kind == render.CLARIFICATION and plan.shown == []
    rendered = texts(plan)
    assert rendered.startswith("Aap ka matlab kaunsi jagah hai?")
    assert all(c["name"] in rendered for c in candidates)


def test_load_listings_carries_the_public_feature_facts(st):
    by_code = {item.code: item for item in cards.load_listings(
        st.conn, st.agency, ["DH6-101", "DH6-102", "DH6-108"])}
    assert by_code["DH6-101"].facing == ("west_open",)
    assert by_code["DH6-102"].is_corner is True
    assert by_code["DH6-102"].features == ("basement",)
    assert set(by_code["DH6-108"].features) == {"lift", "generator_backup"}


# --- load_known (the unknown path's re-fetch) -------------------------------------------

def test_load_known_reads_public_facts_and_answers_from_them(st):
    known = details.load_known(st.conn, st.agency, "DH6-101")
    assert known.facing == ("west_open",) and known.public.get("parking_spaces") == 2
    reply = details.answer_detail(known, details.field_for("parking hai?"), render.ROMAN_URDU)
    assert reply[0]["text"]["body"] == "DH6-101: 2 parking hain."
    gas = details.answer_detail(known, details.field_for("gas connection hai?"),
                                render.ROMAN_URDU)
    assert gas[0]["interactive"]["body"]["text"] == (
        "Ye maloomat mere paas nahi -- agent confirm kar denge.")


@pytest.mark.parametrize("code", sorted(HIDDEN))
def test_load_known_never_returns_a_hidden_listing(st, code):
    assert st.query("SELECT status FROM property WHERE listing_code = %s", (code,))[0][0] \
        != "active"
    assert details.load_known(st.conn, st.agency, code) is None


def test_load_known_is_tenant_scoped(st):
    other = create_agency(st.conn, "Other Agency (DEMO)", "other", "100000000000009")
    assert details.load_known(st.conn, other, "DH6-101") is None
    assert cards.load_listings(st.conn, other, ["DH6-101"]) == []
