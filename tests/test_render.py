"""Task 9.1 renderer tests (no database): the plan's scenarios 1, 2, 3b and 3c rendered
from trusted fixtures, labels, echo, positions, dropped listings and the clarification
gate. Every plan is checked by the grounding validator inside render_search. Listing
facts are the demo dataset's (demo/properties.csv); all data is fictional."""

from decimal import Decimal

import pytest

from app import cards, grounding, location, render, search, whatsapp
from app import demo_dataset as dd
from app.render import ENGLISH, ROMAN_URDU

G = location.Gazetteer.from_dataset(dd.load())
CDN = "https://demo-bucket.s3.us-east-1.amazonaws.com/card"


def entry(value, **extra):
    return {"value": value, "source": "text", "confidence": 1.0, "confirmed": True, **extra}


def listing(code, price, place, *, purpose="sale", kind="house", area=500, unit="sq_yd",
            beds=4, baths=5, cover=True, status="ready", **extra):
    nominal = Decimal(area) if unit == "sq_yd" else None
    return cards.Listing(
        code=code, purpose=purpose, property_type=kind, price_pkr=price,
        area_value=Decimal(area), area_unit=unit, size_nominal_sqyd=nominal,
        bedrooms=beds, bathrooms=baths, construction_status=status, location=place,
        cover_url=f"{CDN}/{code}.jpg" if cover else None, **extra)


def result(n, item, *, match="exact", above=False, relaxations=()):
    return {"position": n, "id": f"id-{item.code}", "listing_code": item.code,
            "purpose": item.purpose, "property_type": item.property_type,
            "location": item.location, "price_pkr": item.price_pkr,
            "area_value": str(item.area_value), "area_unit": item.area_unit,
            "size_nominal_sqyd": item.size_nominal_sqyd, "bedrooms": item.bedrooms,
            "bathrooms": item.bathrooms, "above_budget": above,
            "relaxations": list(relaxations), "location_match": match}


def payload(status, results=(), *, echo=(), lowest=None, missing=(), excluded=None):
    return {"status": status, "missing": list(missing), "results": list(results),
            "total_matches": len(results), "relaxations_applied": [],
            "excluded_reasons": excluded or {}, "unmapped_prefs": [],
            "echo": [{"slot": s, "reasons": list(r)} for s, r in echo],
            "lowest_price_pkr": lowest}


def texts(plan):
    return [t for p in plan.payloads for t in grounding.payload_texts(p)]


def joined(plan):
    return "\n".join(texts(plan))


# --- the demo rows ------------------------------------------------------------------------

DH6_101 = listing("DH6-101", 78_000_000, "DHA Phase 6", facing=("west_open",))
DH6_102 = listing("DH6-102", 80_000_000, "DHA Phase 6", beds=5, baths=6, is_corner=True,
                  features=("basement",))
DH7_101 = listing("DH7-101", 75_000_000, "DHA Phase 7", facing=("west_open",))
DH8_101 = listing("DH8-101", 74_000_000, "DHA Phase 8")
DH8_102 = listing("DH8-102", 69_000_000, "DHA Phase 8", beds=3, baths=4, status=None)
OFFICES = [
    listing("DH6-108", 500_000, "Bukhari Commercial", purpose="rent", kind="office",
            area=1200, unit="sq_ft", beds=None, baths=None, features=("lift",)),
    listing("SHF-101", 450_000, "Shahrah-e-Faisal", purpose="rent", kind="office",
            area=1500, unit="sq_ft", beds=None, baths=None, furnishing="furnished"),
    listing("PEC-101", 550_000, "PECHS Block 6", purpose="rent", kind="office", area=2000,
            unit="sq_ft", beds=None, baths=None),
]

S1 = search.Request.from_slots({
    "purpose": entry("rent"), "property_types": entry(["office"]),
    "location": entry({"nodes": ["khi"], "mode": "ANYWHERE_IN_CITY", "status": "RESOLVED"}),
    "budget": entry({"min": 450_000, "max": 550_000, "mode": "around", "period": "monthly",
                     "stated_pkr": 500_000}),
})
S2_SLOTS = {
    "purpose": entry("sale", source="inferred", confirmed=False, echo=["inferred"]),
    "property_types": entry(["house"]),
    "location": entry({"nodes": ["dha-ph6"], "mode": "EXACT", "status": "RESOLVED"}),
    "size": entry({"value": 500, "unit": "sq_yd", "sqft": 4500, "nominal_sqyd": 500,
                   "mode": "target"}),
    "budget": entry({"min": None, "max": 80_000_000, "mode": "max", "stated_pkr": 80_000_000}),
}
S2 = search.Request.from_slots(S2_SLOTS)
CHEAPER = entry({"min": None, "max": 72_750_000, "mode": "max", "relative": "cheaper",
                 "no_budget_relaxation": True})  # 7.5 cr x 0.97 after "kuch sasta"
S3B = search.Request.from_slots({**S2_SLOTS, "budget": CHEAPER})
S3C = search.Request.from_slots({**S2_SLOTS, "budget": CHEAPER, "location": entry({
    "nodes": ["dha-ph6", "dha-ph8"], "mode": "EXACT", "status": "RESOLVED"})})


# --- scenario 1 (English) ----------------------------------------------------------------

def test_scenario_1_is_an_english_carousel_of_exactly_the_three_offices():
    plan = render.render_search(
        payload(search.OK, [result(n, item) for n, item in enumerate(OFFICES, 1)]),
        OFFICES, ENGLISH, request=S1, gazetteer=G)

    assert plan.kind == render.RESULTS
    assert [p["interactive"]["type"] for p in plan.payloads] == ["carousel"]
    body = plan.payloads[0]["interactive"]["body"]["text"]
    assert body.splitlines()[0] == (
        "Here are offices for rent in Karachi, around 5 lakh per month:")
    cards_ = plan.payloads[0]["interactive"]["action"]["cards"]
    assert [c["body"]["text"] for c in cards_] == [
        ("#1 DH6-108 · Office for rent\nBukhari Commercial · 1200 sq ft\n"
         "PKR 5 lakh / month · Ready"),
        ("#2 SHF-101 · Office for rent\nShahrah-e-Faisal · 1500 sq ft\n"
         "PKR 4.5 lakh / month · Ready"),
        ("#3 PEC-101 · Office for rent\nPECHS Block 6 · 2000 sq ft\n"
         "PKR 5.5 lakh / month · Ready"),
    ]
    assert plan.shown == [(1, "id-DH6-108", "DH6-108"), (2, "id-SHF-101", "SHF-101"),
                          (3, "id-PEC-101", "PEC-101")]
    assert plan.trusted_codes == {"DH6-108", "SHF-101", "PEC-101"}
    assert "sale" not in joined(plan)
    assert [b["quick_reply"]["title"] for b in cards_[0]["action"]["buttons"]] == [
        "More photos", "Visit / Details"]


# --- scenario 2 (Roman Urdu) -------------------------------------------------------------

def scenario_2():
    results = [result(1, DH6_102), result(2, DH6_101),
               result(3, DH7_101, match="neighbour", relaxations=["neighbour"])]
    return render.render_search(
        payload(search.OK, results, echo=[("purpose", ("inferred",))]),
        [DH7_101, DH6_101, DH6_102], ROMAN_URDU, request=S2, gazetteer=G)


def test_scenario_2_wrapper_neighbour_label_and_positions():
    plan = scenario_2()
    body = plan.payloads[0]["interactive"]["body"]["text"].splitlines()
    assert body[0] == "Ji, DHA Phase 6 mein 500 gaz, 8 crore tak -- ye options hain:"
    assert body[1] == ("Ye khareedne ke options hain -- agar kiraye par chahiye to "
                       "bata dein.")  # inferred purpose is echoed, never blocking
    assert body[2] == "3 options:"
    assert body[3].startswith("#1 DH6-102 · Ghar · sale · DHA Phase 6 · PKR 8 crore")
    assert body[5] == ("#3 DH7-101 · Ghar · sale · DHA Phase 7 · PKR 7.5 crore · "
                       "Phase 7 -- qareeb")
    cards_ = plan.payloads[0]["interactive"]["action"]["cards"]
    assert cards_[2]["body"]["text"] == (
        "#3 DH7-101 · Ghar · sale\nDHA Phase 7 · 500 sq yd · 4 bed / 5 bath\n"
        "PKR 7.5 crore · Tayyar · Phase 7 -- qareeb")
    assert [code for _, _, code in plan.shown] == ["DH6-102", "DH6-101", "DH7-101"]
    assert [b["quick_reply"]["title"] for b in cards_[0]["action"]["buttons"]] == [
        "Aur photos", "Visit / Detail"]
    # Exact rows only: the Phase 6 cards carry no neighbour label.
    assert "qareeb" not in cards_[0]["body"]["text"] + cards_[1]["body"]["text"]


def test_the_echo_can_be_left_to_the_caller():
    results = [result(1, DH6_102), result(2, DH6_101)]
    plan = render.render_search(payload(search.OK, results, echo=[("purpose", ("inferred",))]),
                                [DH6_102, DH6_101], ROMAN_URDU, request=S2, gazetteer=G,
                                echo=False)
    assert "khareedne" not in joined(plan)


def test_the_voice_echo_states_the_heard_values_with_yes_and_change():
    # Task 10.2, R16 7.4: "Aapne kaha: DHA Phase 6, 500 gaz, 8 crore tak -- sahi?"
    plan = render.voice_echo(S2, ROMAN_URDU, G, 7)
    [payload] = plan.payloads
    assert payload["interactive"]["body"]["text"] == (
        "Aap ne kaha: DHA Phase 6, 500 gaz, 8 crore tak, khareedne ke liye -- sahi hai?")
    assert [(b["reply"]["id"], b["reply"]["title"])
            for b in payload["interactive"]["action"]["buttons"]] == [
        ("echo:yes:7", "Haan"), ("echo:edit", "Badlein")]
    assert plan.kind == render.CONFIRMATION != render.CLARIFICATION and not plan.shown
    english = render.voice_echo(S2, ENGLISH, G, 7).payloads[0]["interactive"]["body"]["text"]
    assert english == "You said: DHA Phase 6, 500 sq yd, up to 8 crore, to buy -- is that right?"


# --- scenario 3b / 3c ----------------------------------------------------------------------

def test_scenario_3b_is_an_honest_no_match_with_three_options():
    plan = render.render_search(
        payload(search.NO_MATCH, lowest=95_000_000, excluded={"already_shown": 3}),
        [], ROMAN_URDU, request=S3B, gazetteer=G)

    assert plan.kind == render.NO_MATCH_KIND and plan.shown == []
    assert plan.trusted_codes == frozenset()
    [message] = plan.payloads
    assert message["interactive"]["body"]["text"] == (
        "DHA Phase 6 mein is se kam price ka 500 gaz ghar abhi available nahi. "
        "Yahan price 9.5 crore se shuru hoti hai.")
    assert [(b["reply"]["id"], b["reply"]["title"])
            for b in message["interactive"]["action"]["buttons"]] == [
        ("nomatch:nearby", "Nearby area"), ("nomatch:budget", "Budget barhayein"),
        ("handoff:request", "Agent se baat")]


def test_no_match_without_the_cheaper_rule_names_the_budget():
    plan = render.render_search(payload(search.NO_MATCH), [], ENGLISH, request=S2,
                                gazetteer=G)
    assert plan.payloads[0]["interactive"]["body"]["text"] == (
        "No 500 sq yd houses for sale in DHA Phase 6 match this budget right now.")


def test_scenario_3c_labels_the_above_budget_card():
    results = [result(1, DH8_102), result(2, DH8_101, above=True)]
    plan = render.render_search(payload(search.OK, results), [DH8_101, DH8_102],
                                ROMAN_URDU, request=S3C, gazetteer=G)
    body = plan.payloads[0]["interactive"]["body"]["text"].splitlines()
    assert body[0] == ("Ji, DHA Phase 6 / DHA Phase 8 mein 500 gaz, pehle se kam price par "
                       "-- ye options hain:")
    cards_ = plan.payloads[0]["interactive"]["action"]["cards"]
    assert cards_[0]["body"]["text"].endswith("PKR 6.9 crore")  # DH8-102 has no status
    assert cards_[1]["body"]["text"].endswith("PKR 7.4 crore · Tayyar · budget se thoda upar")
    assert [code for _, _, code in plan.shown] == ["DH8-102", "DH8-101"]


# --- the re-fetch boundary, formats -------------------------------------------------------

def test_a_listing_gone_since_the_search_is_dropped_and_positions_renumbered():
    results = [result(1, DH6_102), result(2, DH6_101), result(3, DH7_101, match="neighbour")]
    plan = render.render_search(payload(search.OK, results), [DH6_101, DH7_101],
                                ROMAN_URDU, request=S2, gazetteer=G)
    assert plan.dropped == ["DH6-102"]
    assert plan.shown == [(1, "id-DH6-101", "DH6-101"), (2, "id-DH7-101", "DH7-101")]
    assert "DH6-102" not in joined(plan)
    assert plan.payloads[0]["interactive"]["action"]["cards"][0]["body"]["text"].startswith(
        "#1 DH6-101")


def test_card_facts_come_from_the_refetched_row_not_the_payload():
    stale = {**result(1, DH6_101), "price_pkr": 70_000_000}  # the price changed since
    plan = render.render_search(payload(search.OK, [stale]), [DH6_101], ENGLISH, request=S2,
                                gazetteer=G)
    assert "PKR 7.8 crore" in joined(plan) and "7 crore" not in joined(plan)


def test_when_every_listing_is_gone_the_reply_is_a_no_match():
    plan = render.render_search(payload(search.OK, [result(1, DH6_101)]), [], ROMAN_URDU,
                                request=S2, gazetteer=G)
    assert plan.kind == render.NO_MATCH_KIND and plan.dropped == ["DH6-101"]
    assert plan.shown == []


def test_one_result_is_a_single_card_with_the_wrapper_in_its_body():
    plan = render.render_search(payload(search.OK, [result(1, DH6_101)]), [DH6_101],
                                ROMAN_URDU, request=S2, gazetteer=G)
    [card] = plan.payloads
    assert card["interactive"]["type"] == "button"
    text = card["interactive"]["body"]["text"]
    assert text.startswith("Ji, DHA Phase 6 mein 500 gaz, 8 crore tak -- ye options hain:\n\n"
                           "*DH6-101* · Ghar · sale")
    assert [b["reply"]["title"] for b in card["interactive"]["action"]["buttons"]] == [
        "Aur photos", "Visit / Detail", "Milti-julti"]


def test_the_contact_fallback_sends_the_summary_then_individual_cards():
    results = [result(1, DH6_102), result(2, DH6_101)]
    plan = render.render_search(payload(search.OK, results), [DH6_102, DH6_101], ENGLISH,
                                request=S2, gazetteer=G, fallback=True)
    assert [p["type"] for p in plan.payloads] == ["text", "interactive", "interactive"]
    assert plan.payloads[0]["text"]["body"].startswith(
        "Here are 500 sq yd houses for sale in DHA Phase 6, up to 8 crore:")


def test_coverless_listings_keep_their_number_and_get_a_text_card():
    no_cover = listing("DH6-103", 79_000_000, "DHA Phase 6", cover=False)
    results = [result(1, DH6_102), result(2, DH6_101), result(3, no_cover)]
    plan = render.render_search(payload(search.OK, results), [DH6_102, DH6_101, no_cover],
                                ROMAN_URDU, request=S2, gazetteer=G)
    assert plan.payloads[1]["interactive"]["header"]["text"] == "DH6-103 · photos jald"
    assert "#3 DH6-103" in plan.payloads[0]["interactive"]["body"]["text"]


def test_relaxed_size_and_beds_are_labelled():
    results = [result(1, DH6_101, relaxations=["size"]),
               result(2, DH6_102, relaxations=["beds"])]
    plan = render.render_search(payload(search.OK, results), [DH6_101, DH6_102], ENGLISH,
                                request=S2, gazetteer=G)
    cards_ = plan.payloads[0]["interactive"]["action"]["cards"]
    assert cards_[0]["body"]["text"].endswith("size differs a little")
    assert cards_[1]["body"]["text"].endswith("fewer bedrooms")


def test_the_payload_of_a_real_search_result_renders():
    """The contract with app.search: Result.payload(gazetteer) is the input."""
    rows = {code: search.Row(
        id=f"id-{code}", listing_code=code, status="active", purpose="sale",
        property_type="house", category="residential", location_id=place, price_pkr=price,
        area_value=Decimal(500), area_unit="sq_yd", area_sqft=4500, size_nominal_sqyd=500,
        bedrooms=4, bathrooms=5, construction_status="ready", possession_status=None,
        investment_tags=(), last_confirmed_at=None, photos=3)
        for code, place, price in (("DH6-101", "dha-ph6", 78_000_000),
                                   ("DH7-101", "dha-ph7", 75_000_000))}
    found = search.Result(search.OK, hits=[
        search.Hit(rows["DH6-101"], "exact", [], False, 90.0),
        search.Hit(rows["DH7-101"], "neighbour", ["neighbour"], False, 70.0)],
        total_matches=2, echo=[("purpose", ("inferred",))])
    plan = render.render_search(found.payload(G), [DH6_101, DH7_101], ROMAN_URDU, request=S2,
                                gazetteer=G)
    assert [code for _, _, code in plan.shown] == ["DH6-101", "DH7-101"]
    assert "Phase 7 -- qareeb" in joined(plan)


# --- clarification gate ------------------------------------------------------------------

def test_clarification_never_shows_listings_and_asks_purpose_with_buttons():
    plan = render.render_search(payload(search.CLARIFY, missing=["purpose"]), [DH6_101],
                                ROMAN_URDU, gazetteer=G)
    assert plan.kind == render.CLARIFICATION and plan.shown == []
    [message] = plan.payloads
    assert message["interactive"]["body"]["text"] == (
        "Aap property khareedna chahte hain ya kiraye par lena chahte hain?")
    assert [(b["reply"]["id"], b["reply"]["title"])
            for b in message["interactive"]["action"]["buttons"]] == [
        ("clarify:purpose:sale", "Khareedna"), ("clarify:purpose:rent", "Kiraye par")]


def candidates(*names):
    ids = {p.display_name: p.id for p in G.places.values()}
    return {"slot": "location", "candidates": [
        {"location_id": ids[n], "name": n, "score": 0.9} for n in names]}


def test_an_ambiguous_location_offers_its_candidates_as_buttons():
    plan = render.render_search(payload(search.CLARIFY, missing=["pending_clarification"]),
                                [], ENGLISH, gazetteer=G,
                                pending=candidates("DHA Phase 6", "DHA Phase 7"))
    [message] = plan.payloads
    assert message["interactive"]["body"]["text"] == "Which place do you mean?"
    assert [(b["reply"]["id"], b["reply"]["title"])
            for b in message["interactive"]["action"]["buttons"]] == [
        ("clarify:location:0", "DHA Phase 6"), ("clarify:location:1", "DHA Phase 7")]


def test_one_candidate_is_a_yes_no_question_and_many_are_a_list():
    one = render.render_search(payload(search.CLARIFY, missing=["pending_clarification"]), [],
                               ROMAN_URDU, gazetteer=G, pending=candidates("DHA Phase 8"))
    assert one.payloads[0]["interactive"]["body"]["text"] == (
        "Kya aap DHA Phase 8 keh rahe hain?")
    many = render.render_search(
        payload(search.CLARIFY, missing=["pending_clarification"]), [], ROMAN_URDU,
        gazetteer=G, pending=candidates("DHA Phase 5", "DHA Phase 6", "DHA Phase 7",
                                        "DHA Phase 8"))
    assert many.payloads[0]["interactive"]["type"] == "list"
    rows = many.payloads[0]["interactive"]["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == [f"clarify:location:{i}" for i in range(4)]


@pytest.mark.parametrize(("missing", "expected"), [
    (["location"], "Aap kis area mein property dekh rahe hain?"),
    (["type_budget_or_size"], ("Kis qisam ki property chahiye (ghar, flat, plot, office) "
                               "aur budget kitna hai?")),
])
def test_other_gaps_get_one_text_question(missing, expected):
    plan = render.render_search(payload(search.CLARIFY, missing=missing), [], ROMAN_URDU,
                                gazetteer=G)
    assert plan.payloads == [whatsapp.text(expected)]


# --- language ------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "preferred", "expected"), [
    ("Hi, I'm looking for an office on rent in Karachi, around 5 lakh per month.", None,
     ENGLISH),
    (("Assalam o alaikum, mujhe DHA Phase 6 mein 500 gaz ka ghar chahiye, budget aath "
      "crore tak hai."), None, ROMAN_URDU),
    ("Kuch sasta dikhao", None, ROMAN_URDU),
    ("Phase 8 mein bhi dekho", None, ROMAN_URDU),
    ("show me flats in clifton", None, ENGLISH),
    ("DHA 6 house 8 crore", None, ROMAN_URDU),          # unclear: the default
    ("مجھے ڈی ایچ اے میں گھر چاہیے", None, ROMAN_URDU),  # Urdu script: cut list
    ("Kuch sasta dikhao", ENGLISH, ENGLISH),            # an explicit preference wins
])
def test_language_detection(text, preferred, expected):
    assert render.detect_language(text, preferred) == expected


def test_an_unknown_language_or_status_is_refused():
    with pytest.raises(ValueError):
        render.render_search(payload(search.OK), [], "urdu")
    with pytest.raises(ValueError):
        render.render_search({"status": "weird"}, [], ENGLISH)


def test_a_template_that_states_an_unsupported_fact_raises(monkeypatch):
    monkeypatch.setitem(render.TEXT[ENGLISH], "wrapper", "Guaranteed {things}{place}{budget}:")
    with pytest.raises(render.GroundingError):
        render.render_search(payload(search.OK, [result(1, DH6_101)]), [DH6_101], ENGLISH,
                             request=S2, gazetteer=G)
