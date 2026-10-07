"""Task 3.2 acceptance tests: idempotent sends, the send-time gate, the echo bot and
status reconciliation, all through the restricted manzil_app login (RLS on).

The WhatsApp client is a fake; all data is fictional.
"""

import threading
import time
from types import SimpleNamespace

import psycopg
import pytest

from app import outbound, webhook, whatsapp
from app.db import tenant_transaction
from app.handlers import make_handlers
from app.ingest import store_event
from app.provision_agency import create_agency
from app.worker import Worker

PNID = "100000000000001"
CUSTOMER = "923000000001"


class FakeClient:
    """Records calls; send() pops scripted outcomes (an exception or None = success)."""

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.sent = []
        self.read = []
        self.closed = 0

    def send(self, to, payload, *, reply_to=None):
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        self.sent.append((to, payload))
        return f"wamid.OUT{len(self.sent)}"

    def mark_read(self, wamid, *, typing=False):
        self.read.append(wamid)

    def close(self):
        self.closed += 1


@pytest.fixture
def gw(app_role_db):
    _, app_url, _ = app_role_db
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, "Demo Realty (DEMO)", "demo", PNID)
    client = FakeClient()
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0",
        s3_bucket=None, site_address="demo.example",
    )
    handlers = make_handlers(settings, client_factory=lambda pnid: client)
    worker = Worker(app_url, [agency], handlers, threading.Event(), backoff=lambda n: 0)

    def deliver(*, messages=(), statuses=()):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": PNID},
            "messages": list(messages), "statuses": list(statuses),
        }}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(conn, agency):
            for event in events:
                store_event(conn, agency, event)

    def drain():
        while worker.run_once():
            pass

    def query(sql, params=()):
        with tenant_transaction(conn, agency):
            return conn.execute(sql, params).fetchall()

    yield SimpleNamespace(
        conn=conn, agency=agency, client=client, worker=worker,
        deliver=deliver, drain=drain, query=query,
    )
    worker.close()
    conn.close()


def text_message(wamid, body="hi", ts=None):
    return {
        "from": CUSTOMER, "id": wamid, "timestamp": str(ts or int(time.time())),
        "type": "text", "text": {"body": body},
    }


def status(wamid, state, **extra):
    return {"id": wamid, "status": state, "timestamp": str(int(time.time())),
            "recipient_id": CUSTOMER, **extra}


def outbound_rows(gw):
    return gw.query(
        "SELECT status, provider_message_id, delivery_status, delivered_at IS NOT NULL, "
        "read_at IS NOT NULL, delivery_error_code, last_error_code FROM outbound_message"
    )


def job_states(gw, job_type):
    return gw.query(
        "SELECT status, last_error_code FROM job WHERE job_type = %s ORDER BY created_at",
        (job_type,),
    )


def conversation_id(gw):
    return gw.query("SELECT id FROM conversation")[0][0]


def test_echo_replies_and_logs_messages(gw):
    gw.deliver(messages=[text_message("wamid.IN1", "Salaam")])
    gw.drain()

    assert gw.client.read == ["wamid.IN1"]
    assert gw.client.sent == [(CUSTOMER, whatsapp.text("Echo: Salaam"))]
    assert gw.client.closed == 1
    assert job_states(gw, "process_message") == [("succeeded", None)]
    assert gw.query(
        "SELECT direction, message_type, text_body, provider_message_id FROM message "
        "ORDER BY direction"
    ) == [
        ("inbound", "text", "Salaam", "wamid.IN1"),
        ("outbound", "text", "Echo: Salaam", "wamid.OUT1"),
    ]
    assert outbound_rows(gw) == [("sent", "wamid.OUT1", None, False, False, None, None)]
    assert gw.query(
        "SELECT customer_wa_id, last_customer_message_at IS NOT NULL FROM conversation"
    ) == [(CUSTOMER, True)]


def test_replayed_job_never_sends_twice(gw):
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()
    gw.query("UPDATE job SET status = 'pending', completed_at = NULL RETURNING id")
    gw.drain()

    assert len(gw.client.sent) == 1
    assert len(gw.query("SELECT id FROM message")) == 2


def test_interrupted_send_becomes_unknown_and_is_not_resent(gw):
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()
    conv = conversation_id(gw)
    # A worker died after marking the row 'sending' (before Meta answered).
    _, state = outbound._prepare(
        gw.conn, gw.agency, conv, CUSTOMER, whatsapp.text("x"), "k1", True
    )
    assert state == "sending"

    result = outbound.send_message(
        gw.conn, gw.client, gw.agency, conv, CUSTOMER, whatsapp.text("x"), "k1"
    )
    assert result.status == "unknown"
    assert len(gw.client.sent) == 1  # only the echo


def test_control_gate_cancels_ai_messages(gw):
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()
    conv = conversation_id(gw)
    gw.query("UPDATE conversation SET mode = 'human' RETURNING id")

    result = outbound.send_message(
        gw.conn, gw.client, gw.agency, conv, CUSTOMER, whatsapp.text("AI reply"), "k1"
    )
    assert result.status == "cancelled"
    exempt = outbound.send_message(
        gw.conn, gw.client, gw.agency, conv, CUSTOMER, whatsapp.text("Holding reply"), "k2",
        ai_generated=False,
    )
    assert exempt.status == "sent"
    assert [p["text"]["body"] for _, p in gw.client.sent[1:]] == ["Holding reply"]


def test_closed_window_blocks_session_messages(gw):
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()
    gw.query(
        "UPDATE conversation SET last_customer_message_at = now() - interval '25 hours' "
        "RETURNING id"
    )
    result = outbound.send_message(
        gw.conn, gw.client, gw.agency, conversation_id(gw), CUSTOMER, whatsapp.text("x"), "k1"
    )
    assert result.status == "failed"
    assert gw.query("SELECT last_error_code FROM outbound_message WHERE dedupe_key = 'k1'") == [
        ("outside_window",)
    ]


def test_retryable_error_requeues_then_sends_once(gw):
    gw.client.outcomes = [whatsapp.RetryableSendError("http_500")]
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()

    assert job_states(gw, "process_message") == [("succeeded", None)]
    assert len(gw.client.sent) == 1
    assert gw.query("SELECT status, attempts FROM outbound_message") == [("sent", 2)]


def test_permanent_error_dead_letters(gw):
    gw.client.outcomes = [whatsapp.PermanentSendError("http_400_meta_131047")]
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()

    assert job_states(gw, "process_message") == [("dead", "http_400_meta_131047")]
    assert outbound_rows(gw)[0][0] == "failed"


def test_ambiguous_error_is_never_retried(gw):
    gw.client.outcomes = [whatsapp.AmbiguousSendError("ReadTimeout")]
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()

    assert job_states(gw, "process_message") == [("succeeded", None)]
    assert gw.client.sent == []
    assert outbound_rows(gw)[0][0] == "unknown"


def test_statuses_only_move_forward(gw):
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()
    gw.deliver(statuses=[status("wamid.OUT1", "sent"), status("wamid.OUT1", "delivered")])
    gw.drain()
    assert outbound_rows(gw)[0][2:5] == ("delivered", True, False)

    gw.deliver(statuses=[status("wamid.OUT1", "read")])
    gw.drain()
    # Late or out-of-order statuses never move it back; failed after read is ignored.
    gw.deliver(statuses=[status("wamid.OUT1", "delivered"), status("wamid.OUT1", "failed")])
    gw.drain()

    assert outbound_rows(gw)[0][2:6] == ("read", True, True, None)
    assert {state for state, _ in job_states(gw, "reconcile_status")} == {"succeeded"}


def test_failed_status_is_terminal_and_keeps_error_code(gw):
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()
    gw.deliver(statuses=[status("wamid.OUT1", "failed", errors=[{"code": 131047}])])
    gw.drain()
    gw.deliver(statuses=[status("wamid.OUT1", "read")])
    gw.drain()

    assert outbound_rows(gw)[0][2:6] == ("failed", False, False, "meta_131047")


def test_unmatched_status_retries_then_succeeds(gw):
    gw.deliver(statuses=[status("wamid.NOT-OURS", "delivered")])
    gw.worker.run_once()
    assert job_states(gw, "reconcile_status") == [("pending", "StatusNotMatchedYet")]

    gw.drain()  # backoff is 0 in tests: attempts 2 and 3
    assert job_states(gw, "reconcile_status") == [("succeeded", None)]


def test_status_before_send_is_recorded_once_the_send_lands(gw):
    gw.deliver(statuses=[status("wamid.OUT1", "sent")])
    gw.worker.run_once()  # no outbound row yet: retried
    gw.deliver(messages=[text_message("wamid.IN1")])
    gw.drain()

    assert outbound_rows(gw)[0][:3] == ("sent", "wamid.OUT1", "sent")


def test_without_token_only_status_handler_is_registered():
    settings = SimpleNamespace(
        meta_access_token=None, meta_api_version="v25.0", s3_bucket=None, site_address=None,
    )
    assert set(make_handlers(settings)) == {"reconcile_status"}
