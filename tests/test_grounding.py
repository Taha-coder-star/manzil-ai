"""Task 9.1 grounding validator tests (no database, no model): the Execution Plan's
injections (wrong price in digits, wrong number word, unsupported feature claim,
prohibited promise) are rejected; true statements pass; regenerate once, then the
template. Candidate texts are injected by hand: no model is called."""

import pytest

from app import grounding, search, whatsapp
from app.grounding import ground_wrapper, validate
from tests.test_render import (
    DH6_101,
    DH6_102,
    DH7_101,
    OFFICES,
    S1,
    S2,
    G,
    listing,
)

FACTS = grounding.facts_for([DH6_102, DH6_101, DH7_101], positions=(1, 2, 3), request=S2,
                            gazetteer=G)


def kinds(text, facts=FACTS):
    return [v.kind for v in validate(text, facts, G)]


# --- the four required injections (Execution Plan 9.1) ------------------------------------

@pytest.mark.parametrize("text", [
    "DH6-101 sirf 7.5 crore ka hai",       # 7.5 cr is DH7-101's price, not DH6-101's
    "#2 DH6-101 ki price 7.6 crore hai",
    "DH6-101: PKR 76,000,000",
    "DH6-101 ab 75 lakh mein",
])
def test_a_wrong_price_in_digits_is_rejected(text):
    assert "money" in kinds(text)


@pytest.mark.parametrize("text", [
    "DH6-101 saade saat crore ka hai",     # 7.5 cr in words; the row says 7.8 cr
    "DH6-102 paune aath crore",             # 7.75 cr; the row says 8 cr
    "DH6-101 ساڑھے سات کروڑ",              # Urdu script, 7.5 cr
    "Ye ghar nau crore ka hai",             # 9 cr: no rendered listing costs that
])
def test_a_wrong_price_in_number_words_is_rejected(text):
    assert "money" in kinds(text)


@pytest.mark.parametrize("text", [
    "DH6-101 corner plot hai",             # only DH6-102 is a corner
    "DH6-102 sea view wala ghar",          # no rendered row has a sea view
    "Sab gharon mein lift hai",
    "DH6-101 mein basement bhi hai",        # DH6-102's feature, not DH6-101's
    "Gas connection nahi hai",              # absence is unknown, not false (R-05)
    "Fully furnished",
])
def test_an_unsupported_feature_claim_is_rejected(text):
    assert "feature" in kinds(text)


@pytest.mark.parametrize("text", [
    "Price guarantee ke saath",
    "Ye ghar aap ko zaroor mil jayega",
    "Final price yehi hai",
    "Pakka deal ho jayegi",
    "Owner se discount dilwa denge",
    "Ye ghar 100% available hai",
    "قیمت کی گارنٹی",
])
def test_a_prohibited_promise_is_rejected(text):
    assert "promise" in kinds(text)


# --- what passes --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Ji, DHA Phase 6 mein 500 gaz, 8 crore tak -- ye options hain:",
    "#1 DH6-102 · Ghar · bikau · DHA Phase 6 · PKR 8 crore",
    "#2 DH6-101 PKR 7.8 crore, 4 bed / 5 bath",
    "DH7-101 saade saat crore ka hai, Phase 7 -- qareeb",   # true for DH7-101
    "DH6-102 corner hai aur basement bhi hai",
    "DH6-101 west open hai",
    "Budget aath crore tak, DHA mein, Karachi",              # ancestors of rendered places
    "3 options:",                                            # a bare small number
])
def test_true_statements_pass(text):
    assert kinds(text) == []


# --- the other checks ---------------------------------------------------------------------

def test_units_counts_positions_codes_and_places():
    assert kinds("DH6-101 500 sq ft ka hai") == ["unit"]       # it is 500 sq yd
    assert kinds("DH6-101 1000 gaz") == ["area"]
    assert kinds("DH6-101 5 bed") == ["count"]                  # DH6-102 has 5, not DH6-101
    assert kinds("DH6-101 4 bed, 6 bath") == ["count"]
    assert kinds("#4 dekhein") == ["position"]
    assert kinds("#1 DH6-101") == ["position"]                   # #1 is DH6-102
    assert kinds("DH9-999 bhi hai") == ["code"]
    assert kinds("Clifton mein bhi milega") == ["place"]
    assert kinds("Bahria Town mein") == ["place"]


def test_the_bold_layout_is_checked_like_plain_text():
    """WhatsApp marks (*bold*, 📍) never hide a fact: a wrong pair or price is still found."""
    assert kinds("*#1 DH6-102* · Ghar · bikau\n📍 DHA Phase 6 · *PKR 8 crore*") == []
    assert kinds("*#1 DH6-101* · Ghar · bikau") == ["position"]     # #1 is DH6-102
    assert kinds("*#2 DH6-101* · Ghar\n📍 DHA Phase 6 · *PKR 7.6 crore*") == ["money"]
    assert kinds("💰 *PKR 9 crore*") == ["money"]


def test_rent_amounts_and_large_bare_numbers():
    facts = grounding.facts_for(OFFICES, positions=(1, 2, 3), request=S1, gazetteer=G)
    assert kinds("DH6-108: PKR 5 lakh / month, around 5 lakh", facts) == []
    assert kinds("SHF-101 sirf 4 lakh", facts) == ["money"]
    assert kinds("DH6-108 ka kiraya 480000", facts) == ["money"]
    assert kinds("DH6-108 1200 sq ft, 1,200 sq ft", facts) == []


def test_an_explicitly_false_fact_may_be_stated():
    not_corner = listing("DH6-103", 95_000_000, "DHA Phase 6", is_corner=False)
    facts = grounding.facts_for([not_corner], positions=(1,), gazetteer=G)
    assert kinds("DH6-103 corner nahi hai", facts) == []
    assert kinds("DH6-103 corner hai", facts) == ["feature"]


def test_every_text_of_a_payload_is_checked():
    """whatsapp.summary reads only the body; check_payloads also reads card bodies, headers
    and button titles (the gap in turn_engine.ground_reply)."""
    carousel = whatsapp.carousel("2 options:", [
        ("https://x.example/a.jpg", "#1 DH6-102 · PKR 8 crore", [("p:1", "More photos")]),
        ("https://x.example/b.jpg", "#2 DH6-101 · PKR 7 crore", [("p:2", "Guaranteed")]),
    ])
    found = grounding.check_payloads([carousel], FACTS, G)
    assert sorted(v.kind for v in found) == ["money", "promise"]


# --- regenerate once, then the template (R-21) --------------------------------------------

def generator(*candidates):
    calls = []

    def generate(violations):
        calls.append(violations)
        return candidates[len(calls) - 1]
    return generate, calls


def test_a_clean_candidate_is_used_at_once():
    generate, calls = generator("Ji, ye options hain:")
    grounded = ground_wrapper(generate, FACTS, "TEMPLATE", G)
    assert (grounded.text, grounded.outcome, grounded.attempts) == (
        "Ji, ye options hain:", "ok", 1)
    assert calls == [None]


def test_a_rejected_candidate_is_regenerated_once_with_the_violations():
    generate, calls = generator("DH6-101 saade saat crore", "Ji, ye options hain:")
    grounded = ground_wrapper(generate, FACTS, "TEMPLATE", G)
    assert (grounded.text, grounded.outcome, grounded.attempts) == (
        "Ji, ye options hain:", "regenerated", 2)
    assert [v.kind for v in calls[1]] == ["money"]


def test_two_rejections_fall_back_to_the_template():
    generate, calls = generator("Price guarantee", "Sea view bhi hai", "never asked")
    grounded = ground_wrapper(generate, FACTS, "TEMPLATE", G)
    assert (grounded.text, grounded.outcome, grounded.attempts) == (
        "TEMPLATE", "template_only", 2)
    assert len(calls) == 2  # exactly one regeneration
    assert [v.kind for v in grounded.violations] == ["feature"]


def test_a_failing_generator_falls_back_to_the_template():
    def broken(_):
        raise TimeoutError("provider timeout")
    grounded = ground_wrapper(broken, FACTS, "TEMPLATE", G)
    assert (grounded.text, grounded.outcome) == ("TEMPLATE", "template_only")


def test_facts_never_come_from_the_payload_or_model_text():
    """A Request from slots gives the stated budget; nothing else is trusted."""
    facts = grounding.facts_for([], request=search.Request.from_slots({}), gazetteer=G)
    assert facts.money == frozenset() and facts.codes == frozenset()
    assert kinds("8 crore", facts) == ["money"]
