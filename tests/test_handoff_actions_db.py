"""Task 11.1 step 4: the lead card's buttons (Accept / Pass / Called), Full details and the
card resend, through the real worker, orchestrator and outbound path (disposable
PostgreSQL, restricted manzil_app login, RLS on). Gemini is a fake transport; WhatsApp is
the FakeClient. All data and numbers are fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import uuid

import psycopg

from app import handoff
from tests.test_customer_flow_db import tap, turn
from tests.test_customer_search_db import SCENARIO_2
from tests.test_handoff_alert_db import (
    ADMIN_NUMBER,
    cards_to,
    handoff_id,
    run_due,
    visit_with_summary,
)
from tests.test_handoff_db import BROKER_NUMBER, control
from tests.test_handoff_gate_db import bodies, broker_says_hi, to
from tests.test_requirements_db import st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_B


def open_admin_window(st):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute(
            "INSERT INTO conversation (agency_id, customer_wa_id, last_customer_message_at) "
            "VALUES (%s, %s, now()) ON CONFLICT (agency_id, customer_wa_id) DO UPDATE SET "
            "last_customer_message_at = now()", (st.agency, ADMIN_NUMBER))


def press(st, action, sender, hid=None):
    """A staff member taps a lead-card button; the texts they got back."""
    before = len(st.client.sent)
    tap(st, f"ho:{action}:{hid or handoff_id(st)}", sender=sender)
    return bodies(to(st, sender, before))


def lead(st):
    return st.query(
        "SELECT h.status, a.role, b.role, h.escalated_at IS NOT NULL, h.called_at IS NOT NULL "
        "FROM handoff h JOIN staff a ON a.id = h.assigned_staff_id "
        "LEFT JOIN staff b ON b.id = h.accepted_by_staff_id")[0]


def ready(st):
    broker_says_hi(st)
    visit_with_summary(st)


# --- Accept -------------------------------------------------------------------------------

def test_accept_hands_the_conversation_to_the_broker_with_full_details(st):
    ready(st)
    replies = press(st, "accept", BROKER_NUMBER)
    assert replies[0] == f"You accepted L-01. Chat with the customer: https://wa.me/{CUSTOMER_B}"
    details = replies[1].split("\n")
    assert details[0] == "L-01 full details"
    assert any(line.startswith("- #") and "DH6-101" in line for line in details)
    assert details[-1].endswith(f"] {SCENARIO_2}")
    assert lead(st)[:3] == ("accepted", "broker", "broker")
    assert control(st) == ("human_owned", "human")
    [(due_ok,)] = st.query("SELECT release_due_at BETWEEN now() + interval '47 hours' "
                           "AND now() + interval '49 hours' FROM handoff")
    assert due_ok
    assert st.query("SELECT count(*) FROM job WHERE job_type = %s",
                    (handoff.RELEASE_JOB,)) == [(1,)]


def test_after_accept_the_customer_only_gets_the_holding_reply(st):
    """Scenario 5: "price kam?" after Accept reaches the broker; 0 AI messages (except the
    one holding reply), no model call."""
    ready(st)
    press(st, "accept", BROKER_NUMBER)
    calls = len(st.requests)
    before = len(st.client.sent)
    turn(st, "price kam ho sakti hai?")
    turn(st, "aur properties bhi dikhao")
    assert bodies(to(st, BROKER_NUMBER, before)) == [
        f"L-01 · +{CUSTOMER_B}: price kam ho sakti hai?",
        f"L-01 · +{CUSTOMER_B}: aur properties bhi dikhao"]
    assert bodies(to(st, CUSTOMER_B, before)) == [
        "Ahmed ko aapka message bhej diya hai; woh jald jawab denge."]
    assert len(st.requests) == calls


def test_the_first_tap_wins_and_a_later_one_changes_nothing(st):
    ready(st)
    open_admin_window(st)
    press(st, "accept", BROKER_NUMBER)
    assert press(st, "accept", ADMIN_NUMBER) == ["L-01 was already accepted by Ahmed."]
    assert press(st, "accept", BROKER_NUMBER) == ["You already accepted L-01."]
    assert lead(st)[:3] == ("accepted", "broker", "broker")


def test_after_escalation_the_admin_may_accept_and_the_broker_is_told(st):
    ready(st)
    open_admin_window(st)
    run_due(st, handoff.TIMEOUT_JOB)        # 15 min: the admin gets the card too
    assert len(cards_to(st, ADMIN_NUMBER)) == 1
    press(st, "accept", ADMIN_NUMBER)
    assert lead(st)[:3] == ("accepted", "broker", "admin")
    assert press(st, "accept", BROKER_NUMBER) == ["L-01 was already accepted by Admin."]


def test_the_broker_can_still_accept_after_the_timeout(st):
    ready(st)
    open_admin_window(st)
    run_due(st, handoff.TIMEOUT_JOB)
    assert press(st, "accept", BROKER_NUMBER)[0].startswith("You accepted L-01.")
    assert lead(st)[:3] == ("accepted", "broker", "broker")


# --- Pass and Called ----------------------------------------------------------------------

def test_pass_hands_the_lead_to_the_admin(st):
    ready(st)
    open_admin_window(st)
    before = len(st.client.sent)
    assert press(st, "pass", BROKER_NUMBER) == ["L-01 passed to Admin."]
    assert len(cards_to(st, ADMIN_NUMBER, before)) == 1
    assert lead(st)[:2] == ("pending", "admin") and lead(st)[3]
    # The broker who passed can no longer take it; the admin can.
    assert press(st, "accept", BROKER_NUMBER) == ["L-01 is no longer assigned to you."]
    press(st, "accept", ADMIN_NUMBER)
    assert lead(st)[:3] == ("accepted", "admin", "admin")


def test_called_is_recorded_once(st):
    ready(st)
    assert press(st, "called", BROKER_NUMBER) == ["Noted: you called the customer of L-01."]
    first = st.query("SELECT called_at FROM handoff")
    press(st, "called", BROKER_NUMBER)
    assert st.query("SELECT called_at FROM handoff") == first and lead(st)[4]


# --- forged, stale and misplaced taps -----------------------------------------------------

def test_an_unknown_lead_id_changes_nothing(st):
    ready(st)
    assert press(st, "accept", BROKER_NUMBER, hid=str(uuid.uuid4())) == [
        "That lead was not found."]
    assert lead(st)[0] == "pending"


def test_a_customer_cannot_accept_a_lead(st):
    ready(st)
    before = len(st.client.sent)
    tap(st, f"ho:accept:{handoff_id(st)}", sender=CUSTOMER_B)   # gated: forwarded only
    assert lead(st)[0] == "pending" and control(st) == ("handoff_pending", "human")
    assert to(st, BROKER_NUMBER, before)  # the tap's title was forwarded, nothing else


# --- the card resend ------------------------------------------------------------------------

def test_a_broker_who_writes_later_gets_the_card_once(st):
    visit_with_summary(st)                  # the broker's window was closed: template only
    assert cards_to(st, BROKER_NUMBER) == []
    before = len(st.client.sent)
    broker_says_hi(st)
    assert len(cards_to(st, BROKER_NUMBER, before)) == 1
    before = len(st.client.sent)
    broker_says_hi(st)
    assert cards_to(st, BROKER_NUMBER, before) == []
