"""Day 12 fault injection through real worker/DB; providers are entirely fake."""
import logging

import psycopg
import pytest

from app import queue, recovery, turn_engine, whatsapp
from app.db import tenant_transaction
from tests.test_turn_engine import AGENT, answer, candidate, flush_staff_segments, text
from tests.test_turn_engine import ai as ai  # noqa: PLC0414


def outage(ai):
    ai.script += [503, 503]
    ai.deliver(text("flat"))
    ai.drain()


def due(ai):
    ai.query("UPDATE job SET available_at = now() WHERE job_type = 'ai_recovery' RETURNING id")


def test_outage_schedules_one_delayed_recovery_and_keeps_original_plan(ai):
    outage(ai)
    assert ai.sent_texts() == [turn_engine.TEMPLATES["failure"]]
    assert ai.query("SELECT status, max_attempts, available_at >= created_at + interval '29 seconds' "
                    "FROM job WHERE job_type = 'ai_recovery'") == [("pending", 5, True)]
    original = ai.query("SELECT reply_plan FROM conversation_turn")
    ai.script += [answer()]
    due(ai)
    ai.drain()
    assert len(ai.requests) == 3
    assert ai.query("SELECT status FROM ai_recovery") == [("completed",)]
    assert ai.query("SELECT reply_plan FROM conversation_turn") == original
    assert len(ai.sent_texts()) == 2
    assert ai.query("SELECT count(*) FROM outbound_message WHERE dedupe_key LIKE 'recovery:%%'") == [(1,)]
    assert ai.query("SELECT count(*) FROM ai_recovery", agency_id=ai.other) == [(0,)]
    ai.query("UPDATE job SET status = 'pending', completed_at = NULL "
             "WHERE job_type = 'ai_recovery' RETURNING id")
    ai.drain()
    assert len(ai.requests) == 3 and len(ai.sent_texts()) == 2


@pytest.mark.parametrize("change", ["new_input", "takeover", "window", "role", "planning"])
def test_recovery_guard_and_interrupted_planning(ai, change):
    outage(ai)
    if change == "new_input":
        ai.deliver(text("budget changed"))  # persisted but not yet recorded
        ai.query("UPDATE job SET available_at = now() + interval '1 hour' "
                 "WHERE job_type = 'process_message' AND status = 'pending' RETURNING id")
    elif change == "takeover":
        ai.query("UPDATE conversation SET mode = 'human' RETURNING id")
    elif change == "window":
        ai.query("UPDATE conversation SET last_customer_message_at = now() - interval '25 hours' RETURNING id")
    elif change == "role":
        ai.query("INSERT INTO staff (agency_id, name, whatsapp_number, role) "
                 "SELECT current_agency_id(), 'Test', customer_wa_id, 'agent' FROM conversation RETURNING id")
    else:
        ai.query("UPDATE ai_recovery SET status = 'planning' RETURNING source_turn_id")
    due(ai)
    ai.drain()
    assert len(ai.requests) == 2
    assert len(ai.sent_texts()) == 1
    assert ai.query("SELECT status FROM ai_recovery") == [(
        "failed" if change == "planning" else "cancelled",)]


def test_recovery_failure_stops_without_another_apology(ai, caplog):
    outage(ai)
    ai.script += [503, 503]
    due(ai)
    with caplog.at_level(logging.ERROR):
        ai.drain()
    assert len(ai.sent_texts()) == 1
    assert ai.query("SELECT status FROM ai_recovery") == [("failed",)]
    assert ai.query("SELECT count(*) FROM job WHERE job_type = 'ai_recovery'") == [(1,)]
    assert "ALERT ai_recovery_failed" in caplog.text


def test_recovery_transport_retry_uses_saved_plan(ai):
    outage(ai)
    ai.script += [answer()]
    ai.client.outcomes = [whatsapp.RetryableSendError("http_503")]
    due(ai)
    ai.drain()
    assert len(ai.requests) == 3
    assert ai.query("SELECT status FROM ai_recovery") == [("completed",)]


@pytest.mark.parametrize("error", [whatsapp.AmbiguousSendError("ReadTimeout"),
                                   whatsapp.PermanentSendError("http_400")])
def test_recovery_terminal_send_failure_never_regenerates_or_resends(ai, error):
    outage(ai)
    ai.script += [answer()]
    ai.client.outcomes = [error]
    due(ai)
    ai.drain()
    assert len(ai.requests) == 3 and len(ai.sent_texts()) == 1
    assert ai.query("SELECT status FROM ai_recovery") == [("failed",)]


def test_recovery_table_rejects_cross_tenant_source(ai):
    outage(ai)
    source = ai.query("SELECT id FROM conversation_turn")[0][0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(ai.conn, ai.other):
        ai.conn.execute("INSERT INTO ai_recovery (agency_id, source_turn_id, generation) "
                        "VALUES (current_agency_id(), %s, 0)", (source,))


def test_staff_outage_recovers_without_reopening_source_turn(ai):
    ai.script += [503, 503]
    ai.deliver(text("DHA house for sale", sender=AGENT))
    ai.drain()
    flush_staff_segments(ai)
    assert ai.query("SELECT count(*) FROM ai_recovery") == [(1,)]
    ai.script += [answer()]
    due(ai)
    ai.drain()
    assert ai.query("SELECT status FROM ai_recovery") == [("completed",)]
    assert ai.query("SELECT status FROM conversation_turn WHERE route = 'staff'") == [("replied",)]


@pytest.mark.parametrize("response", [401, candidate([], "SAFETY"),
                                      candidate([], "MALFORMED_FUNCTION_CALL")])
def test_non_retryable_failure_has_no_recovery(ai, response):
    ai.script += [response]
    ai.deliver(text("flat"))
    ai.drain()
    assert ai.query("SELECT count(*) FROM ai_recovery") == [(0,)]


def test_recovery_cancels_if_input_arrives_during_inference(ai, monkeypatch):
    outage(ai)
    original = ai.engine.plan

    def plan(*args, **kwargs):
        payloads = original(*args, **kwargs)
        ai.deliver(text("new budget"))
        ai.query("UPDATE job SET available_at = now() + interval '1 hour' "
                 "WHERE job_type = 'process_message' AND status = 'pending' RETURNING id")
        return payloads

    monkeypatch.setattr(ai.engine, "plan", plan)
    ai.script += [answer()]
    due(ai)
    ai.drain()
    assert len(ai.sent_texts()) == 1
    assert ai.query("SELECT status FROM ai_recovery") == [("cancelled",)]


@pytest.mark.parametrize("code,expected", [("timeout", True), ("http_503", True),
    ("http_429", True), ("transport:ConnectError", True), ("http_404", False),
    ("http_401", False), ("invalid_json", False), ("refused:safety", False)])
def test_retryable_failure_classes(code, expected):
    assert recovery.retryable(code) is expected


def test_dead_letter_alert_is_structured_and_contains_no_payload(caplog):
    with caplog.at_level(logging.ERROR):
        queue.alert_dead("tenant", "job", "fetch_audio", 5, 5, "lease_expired")
    record = caplog.records[-1]
    assert record.event == "dead_letter"
    assert record.attempts == record.max_attempts == 5
    assert record.reason == "lease_expired"
