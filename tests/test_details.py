"""Task 9.1 unknown-field path (Red-Team R-05; CLAUDE.md rule 1): known fields are
answered from the row by template, absent fields go to the agent-confirm template with a
handoff button, legal / document and negotiation questions always go to the agent. No
database, no model; all data is fictional."""

from datetime import date

import pytest

from app import details, grounding
from app.cards import ENGLISH, ROMAN_URDU
from app.details import KnownListing, answer_detail, field_for, known_fields

DH6_101 = KnownListing("DH6-101", "sale", "house", 4, 5, facing=("west_open",),
                       construction_status="ready", public={"parking_spaces": 2})
DH6_102 = KnownListing("DH6-102", "sale", "house", 5, 6, features=("basement",),
                       is_corner=True, construction_status="ready")
DH6_108 = KnownListing("DH6-108", "rent", "office", features=("lift", "generator_backup"),
                       public={"maintenance_pkr_month": 15_000,
                               "rent_security_deposit_pkr": 1_000_000,
                               "rent_advance_months": 2})
AGENT_CONFIRM = "Ye maloomat mere paas nahi -- agent confirm kar denge."


def body(payloads):
    [payload] = payloads
    if payload["type"] == "text":
        return payload["text"]["body"]
    return payload["interactive"]["body"]["text"]


def buttons(payloads):
    action = payloads[0].get("interactive", {}).get("action", {})
    return [(b["reply"]["id"], b["reply"]["title"]) for b in action.get("buttons", [])]


# --- the Execution Plan case: an unknown field -> the agent-confirm template ---------------

def test_an_absent_gas_connection_takes_the_agent_confirm_path():
    question = "#1 wale ghar mein gas connection hai?"
    fields = field_for(question)
    assert fields == ["gas_connection"]
    reply = answer_detail(DH6_101, fields, ROMAN_URDU, position=1)
    assert body(reply) == AGENT_CONFIRM
    assert buttons(reply) == [("handoff:request", "Agent se baat")]


def test_absent_is_unknown_never_false():
    facts = known_fields(DH6_101)
    assert "gas_connection" not in facts and "corner" not in facts and "lift" not in facts
    # Not "corner nahi hai": an unknown corner flag is the agent-confirm template.
    assert body(answer_detail(DH6_101, ["corner"], ROMAN_URDU)) == AGENT_CONFIRM


def test_an_unrecognised_question_is_unknown_too():
    assert field_for("school qareeb kya hai?") == ["nearby_places"]
    assert body(answer_detail(DH6_101, ["nearby_places"], ROMAN_URDU)) == AGENT_CONFIRM
    assert field_for("kuch aur batao") == []
    assert body(answer_detail(DH6_101, [], ENGLISH)) == (
        "I don't have that information -- the agent will confirm it.")


def test_a_listing_no_longer_public_takes_the_agent_path():
    assert body(answer_detail(None, ["parking"], ROMAN_URDU)) == AGENT_CONFIRM


# --- known fields -> template facts --------------------------------------------------------

@pytest.mark.parametrize(("known", "question", "expected"), [
    (DH6_102, "corner hai?", "#2 · DH6-102: corner hai."),
    (DH6_102, "basement hai kya", "#2 · DH6-102: basement hai."),
    (DH6_101, "parking kitni hai?", "#2 · DH6-101: 2 parking hain."),
    (DH6_101, "kis taraf facing hai", "#2 · DH6-101: west open hai."),
    (DH6_101, "ghar tayyar hai?", "#2 · DH6-101: tayyar hai."),
    (DH6_108, "lift aur generator hai?",
     "#2 · DH6-108: lift hai.\n#2 · DH6-108: generator backup hai."),
    (DH6_108, "security deposit kitna hai", "#2 · DH6-108: security deposit PKR 10 lakh."),
    (DH6_108, "maintenance?", "#2 · DH6-108: maintenance PKR 15,000 mahana."),
])
def test_known_fields_are_answered_from_the_row(known, question, expected):
    reply = answer_detail(known, field_for(question), ROMAN_URDU, position=2)
    assert reply[0]["type"] == "text"
    assert body(reply) == expected


def test_english_answers_and_explicit_false_values():
    not_corner = KnownListing("DH6-103", is_corner=False, furnishing="unfurnished")
    assert body(answer_detail(not_corner, ["corner", "furnishing"], ENGLISH)) == (
        "DH6-103: is not a corner property.\nDH6-103: is not furnished.")


def test_a_mixed_question_says_what_is_known_then_the_agent_path():
    reply = answer_detail(DH6_101, field_for("parking aur gas hai?"), ROMAN_URDU)
    assert body(reply) == f"DH6-101: 2 parking hain.\n{AGENT_CONFIRM}"
    assert buttons(reply) == [("handoff:request", "Agent se baat")]


def test_possession_and_installments():
    project = KnownListing("PRJ-101", possession_status=None,
                           expected_possession=date(2027, 6, 1), installment_available=True,
                           public={"down_payment_pkr": 2_500_000})
    assert body(answer_detail(project, ["possession", "installments"], ENGLISH)) == (
        "PRJ-101: expected possession June 2027.\n"
        "PRJ-101: installments available, down payment PKR 25 lakh.")


# --- legal and negotiation: always the agent ----------------------------------------------

@pytest.mark.parametrize("question", [
    "documents clear hain?", "NOC mila hua hai?", "registry kis ke naam hai", "lease kitni hai",
    "kagzat theek hain?", "is the title legal?", "transfer mein masla to nahi",
])
def test_legal_and_document_questions_always_go_to_the_agent(question):
    known = KnownListing("DH6-101", public={"parking_spaces": 2, "min_lease_months": 11})
    fields = field_for(question)
    assert fields[0] == details.LEGAL
    reply = answer_detail(known, fields + ["parking"], ROMAN_URDU)
    assert body(reply).startswith("Documents aur legal baatein hamesha agent hi confirm")
    assert buttons(reply) == [("handoff:request", "Agent se baat")]


@pytest.mark.parametrize("question", [
    "price thori kam ho sakti hai?", "is it negotiable", "last price kya hai", "discount milega?",
])
def test_negotiation_always_goes_to_the_agent(question):
    known = KnownListing("DH6-101", public={"price_negotiable": True})
    assert field_for(question)[0] == details.NEGOTIATION
    reply = answer_detail(known, field_for(question), ROMAN_URDU)
    assert body(reply).startswith("Price ki baat agent hi karte hain")


def test_the_field_mapper_does_not_double_count_overlapping_words():
    assert field_for("security deposit kitna hai") == ["security_deposit"]
    assert field_for("gated community, security guard?") == ["security"]


def test_answers_state_no_promise_and_only_row_facts():
    """The detail templates pass the grounding promise check."""
    for language in (ENGLISH, ROMAN_URDU):
        for known in (DH6_101, DH6_102, DH6_108):
            reply = answer_detail(known, list(known_fields(known)), language, position=1)
            text = body(reply)
            assert not [v for v in grounding.validate(text, grounding.Facts())
                        if v.kind == "promise"]


def test_an_unknown_language_is_refused():
    with pytest.raises(ValueError):
        answer_detail(DH6_101, ["parking"], "urdu")
