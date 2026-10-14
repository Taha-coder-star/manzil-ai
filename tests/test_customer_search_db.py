"""Task 10.1 slices T1 + T2 through the real worker, orchestrator, turn engine and outbound
path (disposable PostgreSQL, restricted manzil_app login, RLS on): the model calls
update_requirements then search_properties; 8.1 searches, 9.1 renders the re-fetched public
rows, the cards are sent, and shown_item is written only after they were. Gemini is a fake
transport; WhatsApp is the FakeClient. All data is fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

from types import SimpleNamespace

from app import customer_reply, grounding, whatsapp
from tests.test_render_db import SCENARIO_1, SCENARIO_2, db_rows
from tests.test_requirements_db import p, st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_A, CUSTOMER_B, HIDDEN, conversation
from tests.test_turn_engine import answer, text, tool, tools_answer

SCENARIO_1_PATCH = [p("purpose", "set", "on rent"), p("property_types", "set", "office"),
                    p("location", "set", "Karachi"),
                    p("budget", "set", "around 5 lakh per month")]
SCENARIO_2_PATCH = [p("location", "set", "DHA Phase 6"), p("size", "set", "500 gaz"),
                    p("property_types", "set", "ghar"),
                    p("budget", "set", "budget aath crore tak")]


def search_turn(st, body, patch, sender):
    """One customer turn: update_requirements, then search_properties, then a STOP."""
    before = len(st.client.sent)
    st.script.extend([tools_answer(tool("update_requirements", {"patch": patch})),
                      tools_answer(tool("search_properties", call_id="call-2")),
                      answer()])
    st.deliver(text(body, sender=sender))
    st.drain()
    return [payload for _, payload in st.client.sent[before:]]


def sent_text(payloads):
    return "\n".join(t for payload in payloads for t in grounding.payload_texts(payload))


def shown_rows(st, sender):
    return st.query(
        "SELECT s.position, p.listing_code, s.card_message_id FROM shown_item s "
        "JOIN property p ON p.id = s.property_id WHERE s.conversation_id = %s "
        "ORDER BY s.position", (conversation(st, sender),))


def test_scenario_1_sends_the_three_offices_and_records_them_after_the_send(st):
    payloads = search_turn(st, SCENARIO_1, SCENARIO_1_PATCH, CUSTOMER_A)

    rows = shown_rows(st, CUSTOMER_A)
    assert {code for _, code, _ in rows} == {"DH6-108", "SHF-101", "PEC-101"}
    assert [position for position, _, _ in rows] == [1, 2, 3]
    assert all(card is not None for _, _, card in rows)  # quoting a card finds its listing
    rendered = sent_text(payloads)
    assert rendered.startswith("Here are offices for rent in Karachi")
    for code, (price, _, _, place) in db_rows(st, [c for _, c, _ in rows]).items():
        assert code in rendered and place in rendered
    assert not any(code in rendered for code in HIDDEN)
    assert "model prose" not in rendered
    assert not any(customer_reply.is_internal(payload) for payload in payloads)


def test_scenario_2_labels_the_neighbouring_phase(st):
    payloads = search_turn(st, SCENARIO_2, SCENARIO_2_PATCH, CUSTOMER_B)

    assert {code for _, code, _ in shown_rows(st, CUSTOMER_B)} == {
        "DH6-101", "DH6-102", "DH7-101"}
    assert "Phase 7 -- qareeb" in sent_text(payloads)


def test_a_failed_send_records_nothing_as_shown(st):
    # The summary goes out, then Meta refuses the first card: the turn ends there.
    st.client.outcomes = [None, whatsapp.PermanentSendError("http_400_meta_131047")]
    search_turn(st, SCENARIO_1, SCENARIO_1_PATCH, CUSTOMER_A)

    assert shown_rows(st, CUSTOMER_A) == []


def test_a_retried_send_of_the_stored_plan_sends_and_records_nothing_twice(st):
    search_turn(st, SCENARIO_1, SCENARIO_1_PATCH, CUSTOMER_A)
    conv = conversation(st, CUSTOMER_A)
    turn_id, plan = st.query(
        "SELECT id, reply_plan FROM conversation_turn WHERE conversation_id = %s "
        "AND reply_plan IS NOT NULL", (conv,))[0]
    sent, rows = len(st.client.sent), shown_rows(st, CUSTOMER_A)

    turn = SimpleNamespace(id=turn_id, agency_id=st.agency, conversation_id=conv,
                           sender_wa_id=CUSTOMER_A)
    assert customer_reply.send(st.conn, st.client, None, turn, plan) == "sent"

    assert len(st.client.sent) == sent
    assert shown_rows(st, CUSTOMER_A) == rows


def test_card_codes_reads_carousel_cards_by_index_and_other_payloads_as_one():
    carousel = whatsapp.carousel("1. A\n2. B", [
        ("https://x.example/a.jpg", "#1", [("photos:DH6-101", "Photos"), ("visit:DH6-101", "V")]),
        ("https://x.example/b.jpg", "#2", [("photos:DH6-102", "Photos"), ("visit:DH6-102", "V")]),
    ])
    card = whatsapp.reply_buttons("#3", [("photos:DH7-101", "Photos")])

    assert customer_reply.card_codes(carousel) == [(0, "DH6-101"), (1, "DH6-102")]
    assert customer_reply.card_codes(card) == [(0, "DH7-101")]
    assert customer_reply.card_codes(whatsapp.text("DH6-101")) == []
