"""Task 10.1 slices T3-T8 through the real worker, orchestrator, turn engine and outbound
path (disposable PostgreSQL, restricted manzil_app login, RLS on): references, photos,
details, taps in code, notes before cards, one question per turn and the grounding check.
Gemini is a fake transport; WhatsApp is the FakeClient. All data is fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import threading
import time
from types import SimpleNamespace

import psycopg
import pytest

from app import customer_tools, grounding, turn_engine, whatsapp
from app.handlers import make_handlers
from app.turn_engine import TEMPLATES, TEMPLATES_EN
from app.worker import Worker
from tests.test_customer_search_db import (
    SCENARIO_1,
    SCENARIO_1_PATCH,
    SCENARIO_2,
    SCENARIO_2_PATCH,
    search_turn,
    sent_text,
    shown_rows,
)
from tests.test_requirements_db import p, st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_A, CUSTOMER_B, conversation
from tests.test_turn_engine import answer, text, tool, tools_answer

_n = iter(range(1, 10_000))


def tap(st, reply_id, sender=CUSTOMER_B, context=None, title="Tap"):
    """A customer tap; no model step is scripted, so a model call would fail the turn."""
    before = len(st.client.sent)
    message = {"from": sender, "id": f"wamid.TAP{next(_n)}", "timestamp": str(int(time.time())),
               "type": "interactive",
               "interactive": {"type": "button_reply",
                               "button_reply": {"id": reply_id, "title": title}}}
    if context:
        message["context"] = {"id": context}
    st.deliver(message)
    st.drain()
    return [payload for _, payload in st.client.sent[before:]]


def turn(st, body, *calls, sender=CUSTOMER_B, context=None):
    """One customer text turn whose model makes these tool calls, one round each."""
    before = len(st.client.sent)
    for n, (name, args) in enumerate(calls):
        st.script.append(tools_answer(tool(name, args, call_id=f"call-{n}")))
    st.script.append(answer())
    message = text(body, sender=sender)
    if context:
        message["context"] = {"id": context}
    st.deliver(message)
    st.drain()
    return [payload for _, payload in st.client.sent[before:]]


def latest_shown(st, sender=CUSTOMER_B):
    """[code] of the latest search with shown items, in position order."""
    return [r[0] for r in st.query(
        "SELECT p.listing_code FROM shown_item s JOIN property p ON p.id = s.property_id "
        "WHERE s.search_run_id = (SELECT search_run_id FROM shown_item WHERE conversation_id "
        "= %s ORDER BY shown_at DESC LIMIT 1) ORDER BY s.position",
        (conversation(st, sender),))]


def position_of(st, code, sender=CUSTOMER_B):
    return next(n for n, c, _ in shown_rows(st, sender) if c == code)


def button_ids(payload):
    action = payload.get("interactive", {}).get("action", {})
    return ([b["reply"]["id"] for b in action.get("buttons", [])]
            + [r["id"] for s in action.get("sections", []) for r in s["rows"]])


def add_photos(st, code, count):
    """Stored listing photos with a valid cached WhatsApp id (no upload needed)."""
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        pid = admin.execute("SELECT id FROM property WHERE agency_id = %s AND listing_code = %s",
                            (st.agency, code)).fetchone()[0]
        for n in range(count):
            admin.execute(
                "INSERT INTO property_media (agency_id, property_id, original_key, "
                "wa_image_key, sort_order, wa_media_id, wa_media_expires_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, now() + interval '25 days')",
                (st.agency, pid, f"tenant/x/{code}-{n}.jpg", f"tenant/x/{code}-{n}-wa.jpg",
                 n, f"media-{code}-{n}"))


@pytest.fixture
def ps(st):
    """st whose worker has media storage configured (photos go out by their cached id;
    nothing is uploaded, so the storage object is never used)."""
    settings = SimpleNamespace(meta_access_token="unused", meta_api_version="v25.0",
                               s3_bucket=None, site_address="demo.example")
    handlers = make_handlers(settings, client_factory=lambda pnid: st.client,
                             engine=st.engine, speech_service=st.speech, storage=object())
    worker = Worker(st.app_url, [st.agency], handlers, threading.Event(), backoff=lambda n: 0)
    drain = st.drain
    st.drain = lambda: [None for _ in iter(worker.run_once, False)]
    yield st
    st.drain = drain
    worker.close()


def scenario_2(st):
    return search_turn(st, SCENARIO_2, SCENARIO_2_PATCH, CUSTOMER_B)


def images(payloads):
    return [(pl["image"]["id"], pl["image"]["caption"]) for pl in payloads
            if pl["type"] == "image"]


# --- notes before cards, language, no repetition ---------------------------------------

def test_scenario_1_sends_no_note_the_results_line_already_says(st):
    payloads = search_turn(st, SCENARIO_1, SCENARIO_1_PATCH, CUSTOMER_A)
    assert sent_text(payloads[:1]).startswith("Here are offices for rent in Karachi")
    assert TEMPLATES["requirements_saved"] not in sent_text(payloads)
    assert TEMPLATES_EN["requirements_saved"] not in sent_text(payloads)


def test_an_inferred_purpose_note_comes_before_the_cards_and_is_said_once(st):
    payloads = search_turn(st, "DHA Phase 6 mein ghar, 8 crore tak", [
        p("location", "set", "DHA Phase 6"), p("property_types", "set", "ghar"),
        p("budget", "set", "8 crore tak")], CUSTOMER_B)
    first = payloads[0]
    assert first["type"] == "text"
    assert first["text"]["body"] == TEMPLATES["purpose_inferred"].format(purpose="khareedna")
    assert "Ye khareedne ke options hain" not in sent_text(payloads[1:])  # D5: not twice
    assert shown_rows(st, CUSTOMER_B)


def test_an_english_customer_gets_english_notes(st):
    payloads = turn(st, "I want a house in Defence City please",
                    ("update_requirements", {"patch": [
                        p("location", "set", "Defence City")]}), sender=CUSTOMER_A)
    body = sent_text(payloads)
    assert body.startswith("I couldn't find that place")
    assert "mujhe nahi mili" not in body


# --- one question per turn (pending clarification) --------------------------------------

def test_an_ambiguous_place_is_asked_once_with_working_buttons(st):
    payloads = turn(st, "P6 mein ghar", ("update_requirements", {"patch": [
        p("location", "set", "P6"), p("property_types", "set", "ghar")]}),
        ("search_properties", {}))
    questions = [pl for pl in payloads if pl["type"] == "interactive"]
    assert len(questions) == 1 and not shown_rows(st, CUSTOMER_B)
    assert "Aap ka matlab kaunsa hai" not in sent_text(payloads)  # the 7.3 text question
    ids = button_ids(questions[0])
    assert ids[0] == "clarify:location:0"

    reply = tap(st, ids[0])
    assert st.pending()[0][1] == "resolved"
    assert len(st.nodes()) == 1  # the chosen candidate is the location now
    # The search ran on the answer and asks the next single question (purpose) with buttons.
    assert [pl["type"] for pl in reply] == ["text", "interactive"]
    assert button_ids(reply[1]) == ["clarify:purpose:sale", "clarify:purpose:rent"]


def test_another_place_closes_the_question_and_asks_for_the_area(st):
    turn(st, "P6 mein ghar", ("update_requirements", {"patch": [
        p("location", "set", "P6"), p("property_types", "set", "ghar")]}),
        ("search_properties", {}))
    reply = tap(st, "clarify:location:none")
    assert [r[1] for r in st.pending()] == ["cancelled"]
    assert len([pl for pl in reply if pl["type"] in ("text", "interactive")]) == 1


def test_a_purpose_button_answers_the_purpose_question(st):
    turn(st, "30 lakh ka ghar DHA Phase 6 mein", ("update_requirements", {"patch": [
        p("budget", "set", "30 lakh"), p("property_types", "set", "ghar"),
        p("location", "set", "DHA Phase 6")]}), ("search_properties", {}))
    assert [r[:2] for r in st.pending()] == [("purpose", "open")]
    tap(st, "clarify:purpose:rent")
    assert st.state()[1]["purpose"]["value"] == "rent"
    assert [r[1] for r in st.pending()] == ["resolved"]


def test_a_stale_clarify_tap_changes_nothing(st):
    scenario_2(st)
    version = st.state()[0]
    tap(st, "clarify:location:0")
    assert st.state()[0] == version


# --- references + photos (3a) ------------------------------------------------------------

def test_scenario_3a_sends_card_2s_unsent_photos_in_order_with_the_id(ps):
    st = ps
    add_photos(st, "DH6-102", 3)
    scenario_2(st)
    n = position_of(st, "DH6-102")
    first = turn(st, f"Property {n} ki aur pictures", ("send_property_photos", {}))
    assert images(first) == [(f"media-DH6-102-{i}", f"#{n} · DH6-102") for i in range(3)]
    assert len(st.query("SELECT 1 FROM media_sent")) == 3
    assert st.query("SELECT selected_via FROM focus") == [("position",)]

    again = turn(st, f"Property {n} ki aur pictures", ("send_property_photos", {}))
    assert images(again) == []
    assert sent_text(again) == TEMPLATES["no_more_photos"].format(ref=f"#{n} · DH6-102")


def test_photos_follow_the_photos_cap_and_the_next_ask_sends_the_rest(ps):
    st = ps
    add_photos(st, "DH6-101", customer_tools.PHOTOS_PER_ASK + 2)
    scenario_2(st)
    n = position_of(st, "DH6-101")
    first = turn(st, f"#{n} ki photos", ("send_property_photos", {"position": n}))
    second = turn(st, f"#{n} ki aur photos", ("send_property_photos", {"position": n}))
    assert len(images(first)) == customer_tools.PHOTOS_PER_ASK and len(images(second)) == 2


def test_an_unclear_reference_asks_with_a_list_of_the_shown_listings(st):
    scenario_2(st)
    payloads = turn(st, "aur pictures bhejo", ("send_property_photos", {}))
    [question] = payloads
    rows = button_ids(question)
    assert sorted(rows) == sorted(f"photos:{c}" for c in ("DH6-101", "DH6-102", "DH7-101"))


def test_a_quoted_card_resolves_to_its_listing(ps):
    st = ps
    add_photos(st, "DH7-101", 1)
    before = len(st.client.sent)
    scenario_2(st)
    sent = st.client.sent[before:]
    card = next(i for i, (_, pl) in enumerate(sent, before + 1)
                if "photos:DH7-101" in repr(pl))
    payloads = turn(st, "is ki aur pictures", ("send_property_photos", {}),
                    context=f"wamid.OUT{card}")
    assert [c for _, c in images(payloads)] == [f"#{position_of(st, 'DH7-101')} · DH7-101"]
    assert st.query("SELECT selected_via FROM focus") == [("quote",)]


def test_the_photos_card_button_sends_photos_in_code(ps):
    st = ps
    add_photos(st, "DH6-101", 1)
    scenario_2(st)
    payloads = tap(st, "photos:DH6-101")
    assert [i for i, _ in images(payloads)] == ["media-DH6-101-0"]
    assert st.query("SELECT selected_via FROM focus") == [("button",)]


# --- details (get_property) + other taps -------------------------------------------------

def test_a_question_about_a_listing_is_answered_from_its_row_or_the_agent(st):
    scenario_2(st)
    n = position_of(st, "DH6-101")
    payloads = turn(st, f"Property {n} ke documents clear hain?", ("get_property", {}))
    [reply] = payloads
    assert reply["interactive"]["body"]["text"].startswith("Documents aur legal")
    assert button_ids(reply) == ["handoff:request"]


def test_the_agent_button_says_truthfully_that_no_agent_was_told(st):
    scenario_2(st)
    assert sent_text(tap(st, "handoff:request")) == TEMPLATES["handoff_unavailable"]


def test_visit_and_listing_taps_are_answered_in_code(st):
    scenario_2(st)
    assert sent_text(tap(st, "visit:DH6-101")) == TEMPLATES["visit_unavailable"]
    assert "DH6-102" in sent_text(tap(st, "listing:DH6-102"))


# --- no-match taps (3b / 3c) -------------------------------------------------------------

def cheaper(st):
    scenario_2(st)
    return search_turn(st, "Kuch sasta dikhao", [p("budget", "relative", "Kuch sasta")],
                       CUSTOMER_B)


def test_scenario_3b_cheaper_is_an_honest_no_match_with_three_buttons(st):
    [reply] = cheaper(st)
    assert button_ids(reply) == ["nomatch:nearby", "nomatch:budget", "handoff:request"]
    assert st.state()[1]["budget"]["value"]["max"] <= 80_000_000  # never raised


def test_the_budget_button_asks_for_a_new_maximum(st):
    cheaper(st)
    budget = st.state()[1]["budget"]
    assert sent_text(tap(st, "nomatch:budget")) == TEMPLATES["budget_ask_sale"]
    assert st.state()[1]["budget"] == budget


def test_the_nearby_button_adds_only_configured_neighbours_with_labels(st):
    search_turn(st, "DHA Phase 6 mein 500 gaz ka ghar, budget 7 crore tak", [
        p("location", "set", "DHA Phase 6"), p("size", "set", "500 gaz"),
        p("property_types", "set", "ghar"), p("budget", "set", "budget 7 crore tak")],
        CUSTOMER_B)
    reply = tap(st, "nomatch:nearby")
    body = sent_text(reply)
    assert "qareeb" in body
    assert st.state()[1]["location"]["value"]["nodes"] == [st.ids["dha-ph6"]]  # unchanged


def test_the_nearby_button_without_configured_neighbours_asks_which_areas(st):
    search_turn(st, "Clifton mein flat 10 crore tak", [
        p("location", "set", "Clifton"), p("property_types", "set", "flat"),
        p("budget", "set", "10 crore tak")], CUSTOMER_B)
    assert sent_text(tap(st, "nomatch:nearby")) == TEMPLATES["nearby_ask"]


def test_scenario_3c_phase_8_added_after_cheaper(st):
    cheaper(st)
    payloads = search_turn(st, "Phase 8 mein bhi dekho", [p("location", "add", "Phase 8")],
                           CUSTOMER_B)
    assert latest_shown(st) == ["DH8-102", "DH8-101"]
    assert "budget se thoda upar" in sent_text(payloads)


def test_a_retried_tap_turn_sends_its_stored_plan_only(st):
    cheaper(st)
    tap(st, "nomatch:budget")
    turn_id = st.query("SELECT id FROM conversation_turn ORDER BY created_at DESC LIMIT 1")[0][0]
    plan = st.query("SELECT reply_plan FROM conversation_turn WHERE id = %s", (turn_id,))[0][0]
    assert plan == [whatsapp.text(TEMPLATES["budget_ask_sale"])]


# --- grounding (D4) ---------------------------------------------------------------------

def test_a_card_stating_an_unsupported_price_is_replaced_by_agent_confirm(st):
    facts = grounding.Facts(codes=frozenset({"DH6-101"}))
    card = whatsapp.reply_buttons("#1 DH6-101 · PKR 3 crore", [("photos:DH6-101", "Photos")])
    shown = {"type": "manzil_shown", "search_run_id": "x", "items": []}
    out = turn_engine.ground_customer([card, shown], facts, st.cache.get(), "roman_urdu")
    assert [pl["interactive"]["body"]["text"] for pl in out] == [
        "Ye maloomat mere paas nahi -- agent confirm kar denge."]  # and SHOWN dropped
