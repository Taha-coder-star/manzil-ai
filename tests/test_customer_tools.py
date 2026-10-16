"""Task 10.1 pure tests: reference parsing, tap ids, customer notes (order, language, no
repetition) and the grounding of customer payloads. No database, no model."""

import string
from types import SimpleNamespace

import pytest

from app import (
    ai_tools,
    customer_reply,
    customer_tools,
    grounding,
    references,
    whatsapp,
)
from app.turn_engine import (
    TEMPLATES,
    TEMPLATES_EN,
    Outcome,
    ground_customer,
    render_customer,
)


@pytest.mark.parametrize(("text", "position"), [
    ("Property 2 ki aur pictures", 2), ("#3 ki photos", 3), ("2nd wala ghar", 2),
    ("doosri property", 2), ("teesri wali", 3), ("option 1", 1), ("2 number wali", 2),
    ("aur pictures bhejo", None), ("500 gaz ka ghar 5 lakh", None), ("Phase 8 mein bhi", None),
    ("doosra ghar dikhao", None), ("pehle se sasta", None), ("#1 aur #2", None),
])
def test_text_position(text, position):
    assert references.text_position(text) == position


@pytest.mark.parametrize("reply_id", [
    "photos:DH6-101", "visit:DH6-101", "similar:DH6-101", "listing:DH6-101",
    "clarify:location:0", "clarify:location:none", "clarify:purpose:sale",
    "clarify:purpose:rent", "nomatch:nearby", "nomatch:budget", "handoff:request",
    "handoff:visit:DH6-101", "handoff:agent:all",
])
def test_known_customer_taps(reply_id):
    assert customer_tools.TAP.fullmatch(reply_id)


@pytest.mark.parametrize("reply_id", [
    "photos:dh6-101", "clarify:location:x", "nomatch:cheaper", "handoff:now", "demo_btn_1", "",
    "handoff:visit:dh6-101", "handoff:price:all", "handoff:visit:",
])
def test_other_ids_are_no_customer_taps(reply_id):
    assert not customer_tools.TAP.fullmatch(reply_id)


def test_every_english_template_has_a_roman_urdu_one_with_the_same_fields():
    fields = string.Formatter()
    for key, english in TEMPLATES_EN.items():
        assert key in TEMPLATES
        assert ({f for _, f, _, _ in fields.parse(english) if f}
                == {f for _, f, _, _ in fields.parse(TEMPLATES[key]) if f}), key


def customer(language="roman_urdu", **flags):
    return SimpleNamespace(language=language, gazetteer=None, facts=grounding.Facts(),
                           asked=flags.get("asked", False), results=flags.get("results", False),
                           echoed=flags.get("echoed", False))


CARD = whatsapp.reply_buttons("Ji, ye options hain:", [("photos:DH6-101", "Aur photos")])


def test_the_voice_echo_question_is_not_preceded_by_notes_it_already_says():
    # Task 10.2: the echo lists the purpose, budget and the saved values.
    echo = whatsapp.reply_buttons("Aap ne kaha: (no facts in this fake) -- sahi hai?",
                                  [("echo:yes:3", "Haan"), ("echo:edit", "Badlein")])
    notices = ("requirements_saved", "budget_implausible",
               ai_tools.Notice("purpose_inferred", (("purpose", "khareedna"),)))
    out = render_customer(Outcome("done", "stop", notices, payloads=(echo,)),
                          customer(asked=True, echoed=True))
    assert out == [echo]


def test_of_several_questions_only_the_first_in_priority_is_sent():
    # Task 10.2: one question per turn; the others stay open in the patch log.
    operator = ai_tools.Notice("budget_operator_ask", (("amount", "8 crore"),))
    place = ai_tools.Notice("location_ambiguous", (("options", "A ya B"),))
    out = render_customer(Outcome("done", "stop", ("requirements_saved", operator,
                                                   "type_unclear")), customer())
    assert out == [whatsapp.text(TEMPLATES["requirements_saved"] + "\n\n"
                                 + TEMPLATES["type_unclear"])]
    out = render_customer(Outcome("done", "stop", ("budget_unclear", place)), customer())
    assert out == [whatsapp.text(TEMPLATES["location_ambiguous"].format(options="A ya B"))]
    one = render_customer(Outcome("done", "stop", ("budget_unclear",)), customer())
    assert one == [whatsapp.text(TEMPLATES["budget_unclear"])]


def test_a_question_with_buttons_replaces_every_text_question():
    question = whatsapp.reply_buttons("Aap ka matlab kaunsi jagah hai?",
                                      [("clarify:location:0", "DHA Phase 6")])
    out = render_customer(Outcome("done", "stop", ("budget_unclear", "type_unclear"),
                                  payloads=(question,)), customer(asked=True))
    assert out == [question]


def test_echo_tap_ids_are_customer_taps():
    for reply_id in ("echo:yes:12", "echo:edit"):
        assert customer_tools.TAP.fullmatch(reply_id)
    assert customer_tools.ECHO_TAP.fullmatch("echo:yes:12").group(1) == "12"
    assert not customer_tools.TAP.fullmatch("echo:yes:")
    assert not customer_tools.TAP.fullmatch("echo:no")


def test_notes_come_before_the_listings():
    notice = ai_tools.Notice("purpose_inferred", (("purpose", "khareedna"),))
    out = render_customer(Outcome("done", "stop", (notice,), payloads=(CARD,)),
                          SimpleNamespace(**{**vars(customer(results=True)),
                                             "facts": grounding.Facts(codes=frozenset(
                                                 {"DH6-101"}))}))
    assert out[0] == whatsapp.text(TEMPLATES["purpose_inferred"].format(purpose="khareedna"))
    assert out[1] == CARD


def test_notes_follow_the_customers_language():
    notice = ai_tools.Notice("purpose_inferred", (("purpose", "kiraye par lena"),))
    [note] = render_customer(Outcome("done", "stop", (notice,)), customer("english"))
    assert note == whatsapp.text(TEMPLATES_EN["purpose_inferred"].format(purpose="to rent"))


def test_a_question_asked_with_buttons_is_not_repeated_as_text():
    question = whatsapp.reply_buttons("Aap ka matlab kaunsi jagah hai?",
                                      [("clarify:location:0", "DHA Phase 6")])
    notices = ("requirements_saved", ai_tools.Notice("location_ambiguous",
                                                     (("options", "A ya B"),)))
    out = render_customer(Outcome("done", "stop", notices, payloads=(question,)),
                          customer(asked=True))
    assert out == [whatsapp.text(TEMPLATES["requirements_saved"]), question]


def test_what_the_results_line_says_is_not_acknowledged_again():
    out = render_customer(Outcome("done", "stop", ("requirements_saved",), payloads=(CARD,)),
                          SimpleNamespace(**{**vars(customer(results=True)),
                                             "facts": grounding.Facts(codes=frozenset(
                                                 {"DH6-101"}))}))
    assert out == [CARD]


def test_an_unsupported_fact_is_replaced_by_agent_confirm_and_nothing_counts_as_shown():
    card = whatsapp.reply_buttons("#1 DH6-101 · PKR 3 crore", [("photos:DH6-101", "Photos")])
    shown = customer_reply.shown_entry("run", [(1, "pid", "DH6-101")])
    out = ground_customer([card, shown], grounding.Facts(codes=frozenset({"DH6-101"})), None,
                          "english")
    assert out == [whatsapp.reply_buttons(
        "I don't have that information -- the agent will confirm it.",
        [("handoff:request", "Talk to agent")])]


def test_a_photo_caption_is_checked_too():
    entry = customer_reply.image_entry("pid", "mid", "#2 · DH6-102")
    facts = grounding.Facts(codes=frozenset({"DH6-102"}), positions=frozenset({2}),
                            position_codes={2: "DH6-102"})
    assert ground_customer([entry], facts, None, "english") == [entry]
    assert ground_customer([entry], grounding.Facts(), None, "english") != [entry]
