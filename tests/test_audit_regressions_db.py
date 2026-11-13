"""Regression tests for the 2026-11-12 system audit, work package 1 (MA-001, MA-002,
MA-003, MA-005, MA-010; MA-004 is in tests/test_queue.py). Each audit proof is here with
its final assertions inverted, plus the cases the remediation plan lists. Real worker,
orchestrator, turn engine and outbound path (disposable PostgreSQL, restricted manzil_app
login, RLS on). Gemini is a fake transport; WhatsApp is the FakeClient. All data and
numbers are fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import time
from types import SimpleNamespace

import httpx
import psycopg

from app import handoff, handoff_actions, turn_engine
from tests.test_customer_flow_db import SCENARIO_2, SCENARIO_2_PATCH, turn
from tests.test_handoff_actions_db import press
from tests.test_handoff_alert_db import ADMIN_NUMBER, handoff_id, run_due
from tests.test_handoff_db import BROKER_NUMBER, control
from tests.test_handoff_gate_db import bodies, broker_says_hi, to
from tests.test_requirements_db import st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_B
from tests.test_turn_engine import text, tool, tools_answer

FAILURE = {turn_engine.TEMPLATES["failure"], turn_engine.TEMPLATES_EN["failure"]}
UNSENT = {handoff.TEXT[lang]["holding_unsent"].format(name="Ahmed") for lang in handoff.TEXT}
EXPIRED = handoff.TEXT["roman_urdu"]["expired"].format(code="L-01", name="Ahmed")
STAFF_EXPIRED = (f"⏰ *L-01* was not accepted within 30 min. The AI assistant is answering "
                 f"the customer again.\n\n💬 Chat with the customer:\nhttps://wa.me/{CUSTOMER_B}")


def _http_503(request):
    return httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}})


def _last_turn(st):
    return st.query("SELECT status FROM conversation_turn ORDER BY created_at DESC LIMIT 1")


def _reaction(sender=CUSTOMER_B):
    return {"from": sender, "id": f"wamid.REACT{time.time_ns()}",
            "timestamp": str(int(time.time())), "type": "reaction",
            "reaction": {"message_id": "wamid.SOMETHING", "emoji": "\U0001F44D"}}


def _ask_for_agent(st):
    turn(st, "Mujhe agent se baat karni hai", ("request_handoff", {"reason": "agent"}))
    assert control(st) == ("handoff_pending", "human")


def _both_timers(st):
    run_due(st, handoff.TIMEOUT_JOB)     # round 1: the admins get the card
    run_due(st, handoff.TIMEOUT_JOB)     # round 2: the lead is released


def _expiry_rows(st):
    return st.query("SELECT recipient_wa_id, status, last_error_code FROM outbound_message "
                    "WHERE dedupe_key LIKE 'handoff:%%:expired%%' ORDER BY recipient_wa_id")


# --- MA-002 -----------------------------------------------------------------------------

def test_ma002_a_failed_round_after_the_handoff_still_sends_the_handoff_message(st):
    before = len(st.client.sent)
    st.script.append(tools_answer(tool("request_handoff", {"reason": "agent"})))
    st.script.append(_http_503)
    st.deliver(text("Mujhe agent se baat karni hai", sender=CUSTOMER_B))
    st.drain()

    assert st.query("SELECT status FROM handoff") == [("pending",)]
    assert st.query("SELECT status FROM lead") == [("open",)]
    assert control(st) == ("handoff_pending", "human")
    [message] = to(st, CUSTOMER_B, before)
    assert message["type"] == "interactive" and message["interactive"]["type"] == "cta_url"
    assert st.query("SELECT status, last_error_code FROM outbound_message "
                    "WHERE recipient_wa_id = %s", (CUSTOMER_B,)) == [("sent", None)]
    assert _last_turn(st) == [("replied",)]


# --- MA-003 -----------------------------------------------------------------------------

def test_ma003_a_finished_search_is_sent_when_the_last_round_fails(st):
    before = len(st.client.sent)
    st.script.append(tools_answer(tool("update_requirements", {"patch": SCENARIO_2_PATCH})))
    st.script.append(tools_answer(tool("search_properties", {}, call_id="call-2")))
    st.script.append(_http_503)
    st.deliver(text(SCENARIO_2, sender=CUSTOMER_B))
    st.drain()

    sent = to(st, CUSTOMER_B, before)
    assert sent and not FAILURE & set(bodies(sent))
    assert st.query("SELECT count(*) FROM shown_item")[0][0] >= 1
    assert _last_turn(st) == [("replied",)]


def test_ma003_saved_requirements_get_the_code_search_when_the_next_round_fails(st):
    before = len(st.client.sent)
    st.script.append(tools_answer(tool("update_requirements", {"patch": SCENARIO_2_PATCH})))
    st.script.append(_http_503)
    st.deliver(text(SCENARIO_2, sender=CUSTOMER_B))
    st.drain()

    sent = to(st, CUSTOMER_B, before)
    assert sent and not FAILURE & set(bodies(sent))
    assert st.query("SELECT count(*) FROM search_run")[0][0] >= 1
    assert st.script == []        # no provider call beyond the failed one


# --- MA-010 -----------------------------------------------------------------------------

def test_ma010_a_reaction_alone_gets_no_reply_and_no_model_call(st):
    before, calls = len(st.client.sent), len(st.requests)
    st.deliver(_reaction())
    st.drain()
    assert to(st, CUSTOMER_B, before) == []
    assert st.query("SELECT count(*) FROM outbound_message WHERE recipient_wa_id = %s",
                    (CUSTOMER_B,)) == [(0,)]
    assert _last_turn(st) == [("replied",)]
    assert len(st.requests) == calls


def test_ma010_a_reaction_with_a_text_still_answers_the_text(st):
    before = len(st.client.sent)
    st.deliver(_reaction(), text("salam", sender=CUSTOMER_B))
    st.drain()
    assert len(to(st, CUSTOMER_B, before)) == 1
    assert _last_turn(st) == [("replied",)]


def test_ma010_a_reaction_during_a_handoff_is_not_forwarded_and_keeps_the_holding_reply(st):
    _ask_for_agent(st)
    before = len(st.client.sent)
    st.deliver(_reaction())
    st.drain()
    assert st.client.sent[before:] == []
    assert st.query("SELECT count(*) FROM outbound_message WHERE dedupe_key LIKE %s",
                    ("turn:%:fwd:%",)) == [(0,)]
    assert st.query("SELECT holding_reply_sent_at FROM handoff") == [(None,)]


# --- MA-005 -----------------------------------------------------------------------------

def test_ma005_a_refused_forward_gets_the_truthful_holding_reply(st):
    _ask_for_agent(st)            # the broker never wrote: their window is closed
    before = len(st.client.sent)
    st.deliver(text("Kal 5 baje call kar dein please", sender=CUSTOMER_B))
    st.drain()

    assert st.query("SELECT status, last_error_code FROM outbound_message "
                    "WHERE dedupe_key LIKE %s", ("turn:%:fwd:%",)) == [("failed", "outside_window")]
    [holding] = to(st, CUSTOMER_B, before)
    assert holding["text"]["body"] in UNSENT
    assert "bhej diya hai" not in holding["text"]["body"]
    assert "passed your message" not in holding["text"]["body"]


# --- MA-001 -----------------------------------------------------------------------------

def test_ma001_an_unaccepted_lead_returns_the_customer_to_the_ai(st):
    broker_says_hi(st)
    _ask_for_agent(st)
    before = len(st.client.sent)
    _both_timers(st)

    assert st.query("SELECT h.status, h.accepted_at IS NULL, h.closed_at IS NOT NULL, l.status "
                    "FROM handoff h JOIN lead l ON l.id = h.lead_id") == [
        ("released", True, True, "closed")]
    assert control(st) == ("ai_active", "ai")
    [message] = to(st, CUSTOMER_B, before)
    assert message["interactive"]["type"] == "cta_url"
    assert message["interactive"]["body"]["text"] == EXPIRED
    assert message["interactive"]["action"]["parameters"]["url"].startswith(
        f"https://wa.me/{BROKER_NUMBER}")
    assert bodies(to(st, BROKER_NUMBER, before)) == [STAFF_EXPIRED]
    # The admin never wrote: their notice is recorded, not sent; nothing is retried.
    assert ("failed", "outside_window") in [r[1:] for r in _expiry_rows(st)
                                            if r[0] == ADMIN_NUMBER]
    assert st.query("SELECT count(*) FROM job WHERE status IN ('pending', 'running')") \
        == [(0,)]

    # The customer's next message is answered by the engine, not gated.
    calls, before = len(st.requests), len(st.client.sent)
    turn(st, "DHA Phase 6 mein 500 gaz ka ghar dikhao")
    assert to(st, CUSTOMER_B, before) != []
    assert len(st.requests) > calls
    assert _last_turn(st) == [("replied",)]


def test_ma001_running_the_expiry_again_sends_nothing_more(st):
    broker_says_hi(st)
    _ask_for_agent(st)
    _both_timers(st)
    rows = _expiry_rows(st)
    before = len(st.client.sent)
    job = SimpleNamespace(agency_id=st.agency, payload={"handoff_id": handoff_id(st),
                                                        "round": 2})
    handoff_actions.expire(st.conn, job, lambda pnid: st.client)   # a retried job
    assert st.client.sent[before:] == []
    assert _expiry_rows(st) == rows
    assert st.query("SELECT status FROM handoff") == [("released",)]


def test_ma001_an_accept_before_round_two_wins(st):
    broker_says_hi(st)
    _ask_for_agent(st)
    run_due(st, handoff.TIMEOUT_JOB)                 # round 1
    press(st, "accept", BROKER_NUMBER)
    run_due(st, handoff.TIMEOUT_JOB)                 # round 2 finds it accepted
    assert st.query("SELECT status FROM handoff") == [("accepted",)]
    assert control(st) == ("human_owned", "human")
    assert _expiry_rows(st) == []


def test_ma001_an_accept_after_expiry_is_told_the_lead_is_closed(st):
    broker_says_hi(st)
    _ask_for_agent(st)
    _both_timers(st)
    assert press(st, "accept", BROKER_NUMBER) == ["L-01 is closed."]
    assert st.query("SELECT status, accepted_at FROM handoff") == [("released", None)]
    assert control(st) == ("ai_active", "ai")


def test_ma001_a_new_request_after_expiry_opens_a_new_lead_with_new_timers(st):
    broker_says_hi(st)
    _ask_for_agent(st)
    _both_timers(st)
    _ask_for_agent(st)
    assert st.query("SELECT l.code, l.status, h.status FROM handoff h JOIN lead l "
                    "ON l.id = h.lead_id ORDER BY h.requested_at") == [
        ("L-01", "closed", "released"), ("L-02", "open", "pending")]
    new = st.query("SELECT id::text FROM handoff WHERE status = 'pending'")[0][0]
    assert st.query("SELECT job_type, status, payload->>'round' FROM job "
                    "WHERE payload->>'handoff_id' = %s ORDER BY job_type", (new,)) == [
        (handoff.TIMEOUT_JOB, "pending", "1"), (handoff.ALERT_JOB, "succeeded", None)]


def test_ma001_a_closed_customer_window_still_releases_the_lead(st):
    broker_says_hi(st)
    _ask_for_agent(st)
    run_due(st, handoff.TIMEOUT_JOB)                 # round 1
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE conversation SET last_customer_message_at = now() - "
                      "interval '2 days' WHERE customer_wa_id = %s", (CUSTOMER_B,))
    run_due(st, handoff.TIMEOUT_JOB)                 # round 2
    assert st.query("SELECT status FROM handoff") == [("released",)]
    assert control(st) == ("ai_active", "ai")
    assert (CUSTOMER_B, "failed", "outside_window") in _expiry_rows(st)
    assert st.query("SELECT count(*) FROM job WHERE job_type = %s AND status = 'succeeded'",
                    (handoff.TIMEOUT_JOB,)) == [(2,)]
