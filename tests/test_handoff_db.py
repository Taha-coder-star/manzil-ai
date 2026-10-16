"""Task 11.1 step 2: the customer side of the handoff through the real worker, orchestrator,
turn engine and outbound path (disposable PostgreSQL, restricted manzil_app login, RLS on):
property confirmation (R-31), the lead / handoff write in one transaction, routing (broker,
then ADMIN) and the honest customer message with its wa.me CTA. Gemini is a fake transport;
WhatsApp is the FakeClient. All data and numbers are fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import psycopg

from app import handoff, references
from app.db import tenant_transaction
from app.turn_engine import TEMPLATES
from tests.test_customer_flow_db import button_ids, position_of, scenario_2, tap, turn
from tests.test_customer_search_db import (
    SCENARIO_1,
    SCENARIO_1_PATCH,
    search_turn,
    sent_text,
)
from tests.test_requirements_db import st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_A, CUSTOMER_B, conversation

BROKER_NUMBER = "15550000103"   # tests.test_seed.ENV DEMO_BROKER_PHONE, digits
VISIT = "Mujhe visit karni hai, kal shaam"


def lead_rows(st, sender=CUSTOMER_B):
    return st.query(
        "SELECT l.code, l.status, l.reason, s.role FROM lead l "
        "LEFT JOIN staff s ON s.id = l.assigned_staff_id WHERE l.conversation_id = %s",
        (conversation(st, sender),))


def lead_codes(st, sender=CUSTOMER_B):
    return sorted(r[0] for r in st.query(
        "SELECT p.listing_code FROM lead_property lp JOIN lead l ON l.id = lp.lead_id "
        "JOIN property p ON p.id = lp.property_id WHERE l.conversation_id = %s",
        (conversation(st, sender),)))


def control(st, sender=CUSTOMER_B):
    return st.query("SELECT control_state, mode FROM conversation WHERE id = %s",
                    (conversation(st, sender),))[0]


def handoff_jobs(st):
    return sorted(st.query(
        "SELECT job_type, status, available_at > now() + interval '14 minutes' FROM job "
        "WHERE job_type IN (%s, %s)", (handoff.ALERT_JOB, handoff.TIMEOUT_JOB)))


def staff_active(st, role, active):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE staff SET is_active = %s WHERE agency_id = %s AND role = %s",
                      (active, st.agency, role))


def ctas(payloads):
    """The customer's handoff message(s) among the sends (the broker alert goes too)."""
    return [p for p in payloads if p.get("interactive", {}).get("type") == "cta_url"]


def cta(payload):
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "cta_url"
    params = payload["interactive"]["action"]["parameters"]
    return payload["interactive"]["body"]["text"], params["display_text"], params["url"]


# --- property confirmation (R-31) ----------------------------------------------------------

def test_visit_after_several_cards_asks_which_property_with_sab(st):
    scenario_2(st)
    shown = [c for _, c, _ in st.query(
        "SELECT s.position, p.listing_code, 0 FROM shown_item s JOIN property p "
        "ON p.id = s.property_id ORDER BY s.position")]
    [question] = turn(st, VISIT, ("request_handoff", {"reason": "visit"}))
    assert question["interactive"]["body"]["text"] == "Kaunsi property ke liye visit?"
    assert button_ids(question) == [f"handoff:visit:{c}" for c in shown] + ["handoff:visit:all"]
    assert lead_rows(st) == [] and control(st) == ("ai_active", "ai")


def test_stale_focus_does_not_pick_the_property(st):
    # Scenario 5 after 3a: focus is on card #2, but "visit karni hai" names nothing.
    scenario_2(st)
    tap(st, "listing:DH6-102")
    assert st.query("SELECT count(*) FROM focus WHERE property_id IS NOT NULL") == [(1,)]
    [question] = turn(st, VISIT, ("request_handoff", {"reason": "visit"}))
    assert button_ids(question)[-1] == "handoff:visit:all"
    assert lead_rows(st) == []


def test_picking_a_row_opens_the_lead_and_sends_the_honest_cta(st):
    scenario_2(st)
    turn(st, VISIT, ("request_handoff", {"reason": "visit"}))
    n = position_of(st, "DH6-102")
    [reply] = ctas(tap(st, "handoff:visit:DH6-102"))
    body, button, url = cta(reply)
    assert body == (
        "Main Manzil ka AI assistant hoon — aapko hamare consultant *Ahmed* se connect kar "
        f"raha hoon. Woh jald rabta karenge.\n\n🏠 Property: *#{n} · DH6-102*\n"
        "🔖 Aapka reference: *L-01*")
    assert button == "Chat with Ahmed"
    assert url == f"https://wa.me/{BROKER_NUMBER}?text=Ref%20L-01"
    assert lead_rows(st) == [("L-01", "open", "visit", "broker")]
    assert lead_codes(st) == ["DH6-102"]
    assert control(st) == ("handoff_pending", "human")
    [(status, due_ok, holding)] = st.query(
        "SELECT status, accept_due_at BETWEEN now() + interval '14 minutes' "
        "AND now() + interval '16 minutes', holding_reply_sent_at FROM handoff")
    assert (status, due_ok, holding) == ("pending", True, None)
    # The broker alert and the 15 min accept timer are queued with the write (outbox).
    assert handoff_jobs(st) == [(handoff.TIMEOUT_JOB, "pending", True),
                                (handoff.ALERT_JOB, "succeeded", False)]
    # Sent past the gate as a system message, and logged.
    assert st.query("SELECT o.status FROM outbound_message o JOIN message m ON "
                    "m.id = o.message_id WHERE m.message_type = 'interactive' "
                    "AND m.text_body LIKE 'Main Manzil%%'") == [("sent",)]


def test_sab_links_every_shown_listing(st):
    scenario_2(st)
    shown = sorted(c for _, c, _ in st.query(
        "SELECT 0, p.listing_code, 0 FROM shown_item s JOIN property p ON p.id = s.property_id"))
    [reply] = ctas(tap(st, "handoff:visit:all"))
    assert lead_codes(st) == shown
    assert all(c in cta(reply)[0] for c in shown)


def test_a_named_position_needs_no_question(st):
    scenario_2(st)
    n = position_of(st, "DH6-101")
    [reply] = ctas(turn(st, f"Property {n} ki visit karni hai", ("request_handoff",
                                                           {"reason": "visit"})))
    assert f"#{n} · DH6-101" in cta(reply)[0]
    assert lead_codes(st) == ["DH6-101"]


def test_the_cards_visit_button_is_the_confirmation(st):
    scenario_2(st)
    [reply] = ctas(tap(st, "visit:DH6-101"))
    assert "DH6-101" in cta(reply)[0]
    assert lead_codes(st) == ["DH6-101"] and lead_rows(st)[0][2] == "visit"


def test_one_shown_listing_is_echoed_without_a_question(st):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:   # leave one office
        admin.execute("UPDATE property SET status = 'withdrawn' WHERE agency_id = %s AND "
                      "listing_code IN ('SHF-101', 'PEC-101')", (st.agency,))
    search_turn(st, SCENARIO_1, SCENARIO_1_PATCH, CUSTOMER_A)
    [reply] = ctas(turn(st, "I want to visit it", ("request_handoff", {"reason": "visit"}),
                   sender=CUSTOMER_A))
    body, button, _ = cta(reply)
    assert body.startswith("I'm Manzil's AI assistant") and "#1 · DH6-108" in body
    assert button == "Chat with Ahmed"


def test_agent_request_with_nothing_shown_opens_a_lead_without_listings(st):
    [reply] = ctas(turn(st, "agent se baat karao", ("request_handoff", {"reason": "agent"})))
    body = cta(reply)[0]
    assert "Property:" not in body and "L-01" in body
    assert lead_rows(st) == [("L-01", "open", "agent", "broker")] and lead_codes(st) == []


def test_the_agent_button_resolves_like_the_model(st):
    scenario_2(st)
    [question] = tap(st, "handoff:request")
    assert button_ids(question)[-1] == "handoff:agent:all"
    assert lead_rows(st) == []


# --- routing ------------------------------------------------------------------------------

def test_no_active_broker_escalates_to_admin(st):
    staff_active(st, "broker", False)
    turn(st, "agent se baat karao", ("request_handoff", {"reason": "agent"}))
    assert lead_rows(st)[0][3] == "admin"


def test_the_listings_assigned_broker_comes_first(st):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute(
            "UPDATE property SET assigned_broker_id = (SELECT id FROM staff WHERE "
            "agency_id = %(a)s AND role = 'admin') WHERE agency_id = %(a)s AND "
            "listing_code = 'DH6-101'", {"a": st.agency})
    scenario_2(st)
    tap(st, "visit:DH6-101")
    assert lead_rows(st)[0][3] == "admin"


def test_nobody_to_take_it_is_said_honestly_and_nothing_is_written(st):
    staff_active(st, "broker", False)
    staff_active(st, "admin", False)
    payloads = turn(st, "agent se baat karao", ("request_handoff", {"reason": "agent"}))
    assert sent_text(payloads) == TEMPLATES["handoff_no_agent"]
    assert lead_rows(st) == [] and control(st) == ("ai_active", "ai")
    assert handoff_jobs(st) == []


# --- idempotency and the gate ---------------------------------------------------------------

def test_a_repeated_request_reuses_the_open_handoff(st):
    scenario_2(st)
    tap(st, "visit:DH6-101")
    conv = conversation(st, CUSTOMER_B)
    with tenant_transaction(st.conn, st.agency):
        items = references.shown_items(st.conn, conv)
        again = handoff.open_handoff(st.conn, conv, items, "visit")
    assert again.status == handoff.ALREADY_OPEN and again.lead_code == "L-01"
    assert st.query("SELECT count(*) FROM handoff") == [(1,)]
    assert len(handoff_jobs(st)) == 2


def test_after_the_handoff_the_ai_stays_silent(st):
    scenario_2(st)
    tap(st, "visit:DH6-101")
    payloads = turn(st, "price kam ho sakti hai?")   # the turn is gated: no model call
    # Only the one holding reply (Task 11.2), sent as a system message.
    assert [p["text"]["body"] for p in payloads] == [
        "✅ *Ahmed* ko aapka message bhej diya hai; woh jald jawab denge."]
    assert st.script  # the scripted model answer was never used


def test_lead_numbers_follow_each_other_across_customers(st):
    turn(st, "agent se baat karao", ("request_handoff", {"reason": "agent"}))
    turn(st, "I want to talk to an agent", ("request_handoff", {"reason": "agent"}),
         sender=CUSTOMER_A)
    assert lead_rows(st, CUSTOMER_A)[0][0] == "L-02"
