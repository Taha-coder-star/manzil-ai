"""Task 11.2 (rest): #done / #pass, the broker help reply and the 48 h release, through
the real worker, orchestrator and outbound path (disposable PostgreSQL, restricted
manzil_app login, RLS on). Gemini is a fake transport; WhatsApp is the FakeClient. All
data and numbers are fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import psycopg

from app import handoff
from tests.test_customer_flow_db import turn
from tests.test_customer_search_db import search_turn
from tests.test_handoff_actions_db import lead, open_admin_window, press, ready
from tests.test_handoff_alert_db import ADMIN_NUMBER, run_due
from tests.test_handoff_db import BROKER_NUMBER, control
from tests.test_handoff_gate_db import bodies, to
from tests.test_requirements_db import p, st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_B
from tests.test_turn_engine import text


def say(st, body, sender=BROKER_NUMBER):
    """A staff member's text; the texts they got back (no model step is scripted)."""
    st.script.clear()
    before = len(st.client.sent)
    st.deliver(text(body, sender=sender))
    st.drain()
    return bodies(to(st, sender, before))


def closed(st):
    return st.query("SELECT h.status, l.status FROM handoff h JOIN lead l ON l.id = h.lead_id")


# --- #done: the AI resumes ------------------------------------------------------------------

def test_done_hands_the_customer_back_to_the_ai(st):
    """Scenario 5's end: after #done the customer's next message gets an AI answer."""
    ready(st)
    press(st, "accept", BROKER_NUMBER)
    assert say(st, "#done L-01") == [
        "✅ *L-01* is closed. The AI assistant will answer this customer again."]
    assert closed(st) == [("completed", "closed")]
    assert control(st) == ("ai_active", "ai")
    calls, before = len(st.requests), len(st.client.sent)
    search_turn(st, "Phase 8 mein bhi dekho", [p("location", "add", "Phase 8")], CUSTOMER_B)
    assert len(st.requests) > calls                       # the model runs again
    assert to(st, CUSTOMER_B, before)                     # and the customer is answered
    assert to(st, BROKER_NUMBER, before) == []            # nothing is forwarded any more


def test_done_without_a_code_closes_the_only_accepted_lead(st):
    ready(st)
    press(st, "accept", BROKER_NUMBER)
    assert say(st, "#DONE")[0].startswith("✅ *L-01* is closed.")
    assert closed(st) == [("completed", "closed")]


def test_done_accepts_a_short_code(st):
    ready(st)
    press(st, "accept", BROKER_NUMBER)
    assert say(st, "#done L-1")[0].startswith("✅ *L-01* is closed.")


def test_done_before_accept_changes_nothing(st):
    ready(st)
    assert say(st, "#done L-01") == [
        "L-01 has not been accepted yet: tap Accept first, or Pass."]
    assert closed(st) == [("pending", "open")]
    assert control(st) == ("handoff_pending", "human")


def test_done_twice_says_closed(st):
    ready(st)
    press(st, "accept", BROKER_NUMBER)
    say(st, "#done L-01")
    assert say(st, "#done L-01") == ["L-01 is closed."]


def test_an_admin_may_close_a_lead_the_broker_holds(st):
    ready(st)
    open_admin_window(st)
    press(st, "accept", BROKER_NUMBER)
    assert say(st, "#done L-01", sender=ADMIN_NUMBER)[0].startswith("✅ *L-01* is closed.")
    assert control(st) == ("ai_active", "ai")


def test_an_unknown_lead_code_is_not_found(st):
    ready(st)
    assert say(st, "#done L-99") == ["That lead was not found."]


# --- #pass and the help reply ---------------------------------------------------------------

def test_pass_as_text_hands_the_lead_to_the_admin(st):
    ready(st)
    open_admin_window(st)
    assert say(st, "#pass L-01") == ["↪ *L-01* passed to Admin."]
    assert lead(st)[:2] == ("pending", "admin")


def test_other_broker_text_gets_help_with_the_open_leads(st):
    ready(st)
    [reply] = say(st, "kal call karunga customer ko")
    assert reply.startswith("*Manzil lead commands*")
    assert reply.endswith("Your open leads: L-01 (pending).")
    assert st.query("SELECT count(*) FROM staff_segment") == [(0,)]   # never extraction


def test_a_bare_command_with_no_open_lead_says_so(st):
    assert say(st, "#done") == ["You have no open leads."]


# --- the 48 h release -----------------------------------------------------------------------

def make_release_due(st):
    """Age the handoff by 3 days (its timestamps keep their order, as the table requires)
    so its release deadline is a minute ago."""
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute(
            "UPDATE handoff SET requested_at = requested_at - interval '3 days', "
            "accept_due_at = accept_due_at - interval '3 days', "
            "accepted_at = accepted_at - interval '3 days', "
            "closed_at = closed_at - interval '3 days', "
            "release_due_at = now() - interval '1 minute' WHERE agency_id = %s", (st.agency,))


def test_48_hours_without_a_message_release_the_customer_to_the_ai(st):
    ready(st)
    press(st, "accept", BROKER_NUMBER)
    make_release_due(st)
    before = len(st.client.sent)
    run_due(st, handoff.RELEASE_JOB)
    assert closed(st) == [("released", "closed")]
    assert control(st) == ("ai_active", "ai")
    assert bodies(to(st, BROKER_NUMBER, before)) == [
        "L-01 was handed back to the AI assistant after 48 h without a customer message."]


def test_a_release_job_whose_deadline_moved_does_nothing(st):
    ready(st)
    press(st, "accept", BROKER_NUMBER)
    turn(st, "price kam ho sakti hai?")   # moves release_due_at and queues a newer job
    run_due(st, handoff.RELEASE_JOB)      # both jobs run now; the deadline is 48 h away
    assert closed(st) == [("accepted", "open")]
    assert control(st) == ("human_owned", "human")


def test_a_release_after_done_does_nothing(st):
    ready(st)
    press(st, "accept", BROKER_NUMBER)
    say(st, "#done L-01")
    make_release_due(st)
    before = len(st.client.sent)
    run_due(st, handoff.RELEASE_JOB)
    assert closed(st) == [("completed", "closed")] and st.client.sent[before:] == []
