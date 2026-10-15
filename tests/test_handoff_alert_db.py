"""Task 11.1 step 3: the broker's lead card, the L3 summary, the template fallback and the
accept-timeout escalation, through the real worker, job handlers and outbound path
(disposable PostgreSQL, restricted manzil_app login, RLS on). Gemini is a fake transport;
WhatsApp is the FakeClient. All data and numbers are fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import json

import psycopg

from app import handoff, handoff_alert, whatsapp
from tests.test_customer_flow_db import scenario_2, tap
from tests.test_customer_search_db import SCENARIO_2
from tests.test_handoff_db import BROKER_NUMBER
from tests.test_handoff_gate_db import bodies, broker_says_hi, to
from tests.test_requirements_db import st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_B
from tests.test_turn_engine import answer, candidate, text, tool, tools_answer

ADMIN_NUMBER = "15550000101"    # tests.test_seed.ENV DEMO_ADMIN_PHONE


def summary(text):
    return candidate([{"text": json.dumps({"summary": text})}],
                     promptTokenCount=40, candidatesTokenCount=12)


def cards_to(st, number, before=0):
    return [p for p in to(st, number, before) if p["type"] == "interactive"]


def templates_to(st, number, before=0):
    return [p for p in to(st, number, before) if p["type"] == "template"]


def handoff_id(st):
    return st.query("SELECT id::text FROM handoff")[0][0]


def run_due(st, job_type):
    """Make a delayed job due now and run it."""
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE job SET available_at = now() WHERE job_type = %s AND "
                      "status = 'pending'", (job_type,))
    st.drain()


def visit_with_summary(st, text="Customer wants to visit DH6-101 tomorrow evening."):
    scenario_2(st)
    st.script.append(summary(text))
    return tap(st, "visit:DH6-101")


# --- the card -----------------------------------------------------------------------------

def test_the_broker_gets_a_complete_card_with_three_buttons(st):
    broker_says_hi(st)
    before = len(st.client.sent)
    visit_with_summary(st)
    [card] = cards_to(st, BROKER_NUMBER, before)
    body = card["interactive"]["body"]["text"]
    lines = body.split("\n")
    assert lines[0] == "New lead L-01 (visit)"
    assert lines[1] == f"Customer: +{CUSTOMER_B}"
    assert lines[2].startswith("Wants: ") and "DHA Phase 6" in lines[2]
    assert lines[3].startswith("Property: #") and "DH6-101" in lines[3]
    assert lines[4] == f'Last message: "{SCENARIO_2}"'   # the words, not the tap title
    assert "Summary: Customer wants to visit DH6-101 tomorrow evening." in lines
    assert lines[-1] == "Tap Accept within 15 min."
    hid = handoff_id(st)
    assert [b["reply"]["id"] for b in card["interactive"]["action"]["buttons"]] == [
        f"ho:accept:{hid}", f"ho:pass:{hid}", f"ho:called:{hid}"]
    assert handoff_alert.missing_fields(handoff_alert.load_packet(
        st.conn, st.agency, hid)) == []
    assert st.query("SELECT summary FROM lead") == [
        ("Customer wants to visit DH6-101 tomorrow evening.",)]
    assert st.query("SELECT operation, outcome FROM usage_event WHERE operation = 'L3'") == [
        ("L3", "succeeded")]
    # The summary prompt never carries the customer's number.
    assert CUSTOMER_B not in st.requests[-1].raw.decode()


def test_a_summary_with_an_unsupported_fact_is_left_out(st):
    broker_says_hi(st)
    before = len(st.client.sent)
    visit_with_summary(st, "Customer will pay 5 crore for DH6-101.")
    [card] = cards_to(st, BROKER_NUMBER, before)
    assert "Summary:" not in card["interactive"]["body"]["text"]
    assert st.query("SELECT summary FROM lead") == [(None,)]


def test_a_failed_summary_never_blocks_the_card(st):
    broker_says_hi(st)
    before = len(st.client.sent)
    scenario_2(st)
    tap(st, "visit:DH6-101")          # no scripted summary: the provider call fails
    [card] = cards_to(st, BROKER_NUMBER, before)
    assert "Summary:" not in card["interactive"]["body"]["text"]


def test_a_lead_without_listings_or_requirements_says_not_stated(st):
    broker_says_hi(st)
    before = len(st.client.sent)
    # The turn's two model steps, then the alert job's summary call.
    st.script.extend([tools_answer(tool("request_handoff", {"reason": "agent"})), answer(),
                      summary("Customer asked to talk to an agent.")])
    st.deliver(text("agent se baat karao", sender=CUSTOMER_B))
    st.drain()
    [card] = cards_to(st, BROKER_NUMBER, before)
    body = card["interactive"]["body"]["text"]
    assert "Wants: not stated" in body and "Property: none chosen" in body
    assert handoff_alert.missing_fields(handoff_alert.load_packet(
        st.conn, st.agency, handoff_id(st))) == ["requirements"]


# --- the closed window and failed delivery --------------------------------------------------

def test_a_closed_window_sends_the_template_instead(st, monkeypatch):
    monkeypatch.setenv("LEAD_ALERT_PARAMS", "lead,reason")
    before = len(st.client.sent)
    visit_with_summary(st)            # the broker never wrote: window closed
    assert cards_to(st, BROKER_NUMBER, before) == []
    [alert] = templates_to(st, BROKER_NUMBER, before)
    assert alert == whatsapp.template("new_lead_alert", "en", ["L-01", "visit"])
    assert st.query("SELECT escalated_at FROM handoff") == [(None,)]


def test_nothing_delivered_escalates_to_the_admin_at_once(st):
    broker_says_hi(st)
    scenario_2(st)
    st.script.append(summary("Customer wants a visit."))
    # The customer's CTA goes out; then the broker's card and template are refused.
    refused = whatsapp.PermanentSendError("131026")
    st.client.outcomes = [None, refused, refused]
    before = len(st.client.sent)
    tap(st, "visit:DH6-101")
    assert to(st, BROKER_NUMBER, before) == []
    assert len(templates_to(st, ADMIN_NUMBER, before)) == 1   # the admin never wrote
    assert st.query("SELECT escalated_at IS NOT NULL, a.role FROM handoff h JOIN staff a "
                    "ON a.id = h.assigned_staff_id") == [(True, "broker")]


# --- the accept timer ------------------------------------------------------------------------

def test_the_timeout_adds_the_admin_and_keeps_the_broker(st):
    broker_says_hi(st)
    visit_with_summary(st)
    with psycopg.connect(st.admin_url, autocommit=True) as admin:   # the admin wrote too
        admin.execute(
            "INSERT INTO conversation (agency_id, customer_wa_id, last_customer_message_at) "
            "VALUES (%s, %s, now()) ON CONFLICT (agency_id, customer_wa_id) DO UPDATE SET "
            "last_customer_message_at = now()", (st.agency, ADMIN_NUMBER))
    before = len(st.client.sent)
    run_due(st, handoff.TIMEOUT_JOB)
    [card] = cards_to(st, ADMIN_NUMBER, before)
    assert card["interactive"]["body"]["text"].endswith(
        "Not accepted within 15 min: please take it.")
    assert to(st, BROKER_NUMBER, before) == []
    assert st.query("SELECT h.status, h.escalated_at IS NOT NULL, s.role FROM handoff h "
                    "JOIN staff s ON s.id = h.assigned_staff_id") == [("pending", True, "broker")]
    # Round 2 is queued; when it runs it only logs.
    before = len(st.client.sent)
    run_due(st, handoff.TIMEOUT_JOB)
    assert st.client.sent[before:] == []
    assert st.query("SELECT count(*) FROM job WHERE job_type = %s AND status = 'succeeded'",
                    (handoff.TIMEOUT_JOB,)) == [(2,)]


def test_a_timeout_after_accept_does_nothing(st):
    broker_says_hi(st)
    visit_with_summary(st)
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE handoff SET status = 'accepted', accepted_at = now(), "
                      "accepted_by_staff_id = assigned_staff_id WHERE agency_id = %s",
                      (st.agency,))
    before = len(st.client.sent)
    run_due(st, handoff.TIMEOUT_JOB)
    assert st.client.sent[before:] == []
    assert st.query("SELECT escalated_at FROM handoff") == [(None,)]
    assert st.query("SELECT count(*) FROM job WHERE job_type = %s",
                    (handoff.TIMEOUT_JOB,)) == [(1,)]


def test_template_parameters_are_checked():
    import os
    os.environ["LEAD_ALERT_PARAMS"] = "lead,phone"
    try:
        try:
            handoff_alert.template_settings()
        except ValueError as exc:
            assert "phone" in str(exc)
        else:
            raise AssertionError("an unknown field must be refused")
    finally:
        del os.environ["LEAD_ALERT_PARAMS"]


def test_holding_reply_wording_stays_out_of_the_card(st):
    broker_says_hi(st)
    before = len(st.client.sent)
    visit_with_summary(st)
    assert not any("bhej diya" in b for b in bodies(to(st, BROKER_NUMBER, before)))
