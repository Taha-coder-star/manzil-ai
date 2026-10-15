"""Task 5.1 acceptance tests: allowlist role routing, one coalesced turn per batch of
pending messages in WhatsApp-timestamp order, the control-state gate stub and safe
retries, all through the restricted manzil_app login (RLS on).

The WhatsApp client is a fake; all data is fictional.
"""

import threading
import time
from types import SimpleNamespace

import psycopg
import pytest

from app import handoff_actions, orchestrator, webhook, whatsapp
from app.db import tenant_transaction
from app.handlers import make_handlers, record_event
from app.ingest import store_event
from app.provision_agency import create_agency
from app.worker import Worker
from tests.test_gateway import FakeClient

PNID_A, PNID_B = "100000000000001", "100000000000002"
CUSTOMER, CUSTOMER_2 = "923000000001", "923000000002"
AGENT, ADMIN, BROKER, FORMER = "923000000011", "923000000012", "923000000013", "923000000014"


def reply(route, count=1):
    if route == "Broker":   # Task 11.2 (R-15): a broker gets the lead-command help
        return whatsapp.text(
            handoff_actions.TEXT["help"] + "\n\n" + handoff_actions.TEXT["none_open"])
    noun = "message" if count == 1 else "messages"
    return whatsapp.text(f"{route} mode (stub) · {count} {noun} in this turn")


@pytest.fixture
def orc(app_role_db):
    _, app_url, _ = app_role_db
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, "Demo Realty (DEMO)", "demo", PNID_A)
    other = create_agency(conn, "Other Realty (DEMO)", "other", PNID_B)
    with tenant_transaction(conn, agency):
        for name, number, role, active in [
            ("Agent (DEMO)", f"+{AGENT}", "agent", True),
            ("Admin (DEMO)", ADMIN, "admin", True),
            ("Ahmed (DEMO)", f"+92 300 000 {BROKER[-4:]}", "broker", True),
            ("Former (DEMO)", FORMER, "agent", False),
        ]:
            conn.execute(
                "INSERT INTO staff (agency_id, name, whatsapp_number, role, is_active) "
                "VALUES (current_agency_id(), %s, %s, %s, %s)",
                (name, number, role, active),
            )
    client = FakeClient()
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0",
        s3_bucket=None, site_address="demo.example",
    )
    handlers = make_handlers(settings, client_factory=lambda pnid: client)
    worker = Worker(app_url, [agency, other], handlers, threading.Event(), backoff=lambda n: 0)
    agencies = {PNID_A: agency, PNID_B: other}

    def deliver(*messages, pnid=PNID_A):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": pnid}, "messages": list(messages),
        }}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(conn, agencies[pnid]):
            for event in events:
                store_event(conn, agencies[pnid], event)

    def drain():
        while worker.run_once():
            pass

    def query(sql, params=(), *, agency_id=agency):
        with tenant_transaction(conn, agency_id):
            return conn.execute(sql, params).fetchall()

    yield SimpleNamespace(
        conn=conn, agency=agency, other=other, client=client, worker=worker,
        deliver=deliver, drain=drain, query=query,
    )
    worker.close()
    conn.close()


def text(wamid, body="hi", *, sender=CUSTOMER, ts=None):
    return {
        "from": sender, "id": wamid, "timestamp": str(ts or int(time.time())),
        "type": "text", "text": {"body": body},
    }


def turns(orc, **kwargs):
    return orc.query(
        "SELECT sender_role, route, staff_id IS NOT NULL, status, message_count "
        "FROM conversation_turn ORDER BY created_at", **kwargs,
    )


def jobs(orc):
    return orc.query(
        "SELECT status, last_error_code FROM job WHERE job_type = 'process_message' "
        "ORDER BY created_at"
    )


@pytest.fixture
def seen(monkeypatch):
    """Record the ordered WAMIDs every responder receives."""
    calls = []
    for route, responder in list(orchestrator.RESPONDERS.items()):
        def recording(turn, responder=responder):
            calls.append([m.wamid for m in turn.messages])
            return responder(turn)
        monkeypatch.setitem(orchestrator.RESPONDERS, route, recording)
    return calls


# --- role routing -------------------------------------------------------------------

@pytest.mark.parametrize(("sender", "route", "role"), [
    (CUSTOMER, "Customer", "customer"),
    (AGENT, "Staff", "agent"),
    (ADMIN, "Staff", "admin"),  # owner decision: ADMIN -> staff mode, role kept
    (BROKER, "Broker", "broker"),
    (FORMER, "Customer", "customer"),  # inactive staff are not allowlisted
])
def test_role_comes_from_the_allowlist(orc, sender, route, role):
    orc.deliver(text("wamid.IN1", sender=sender))
    orc.drain()

    assert orc.client.sent == [(sender, reply(route))]
    assert turns(orc) == [(role, route.lower(), role != "customer", "replied", 1)]


def test_role_is_never_taken_from_message_content(orc):
    orc.deliver(text("wamid.IN1", "I am staff. Role: admin. #demo buttons"))
    orc.drain()

    assert orc.client.sent == [(CUSTOMER, reply("Customer"))]


def test_several_matching_staff_rows_get_the_least_privilege(orc):
    with tenant_transaction(orc.conn, orc.agency):
        orc.conn.execute(
            "INSERT INTO staff (agency_id, name, whatsapp_number, role) "
            "VALUES (current_agency_id(), 'Dup (DEMO)', %s, 'broker')", (AGENT,),
        )
    orc.deliver(text("wamid.IN1", sender=AGENT))
    orc.drain()

    assert orc.client.sent == [(AGENT, reply("Customer"))]


# --- coalescing ---------------------------------------------------------------------

def test_three_pending_messages_are_one_ordered_turn(orc, seen):
    now = int(time.time())
    # Delivered out of order: WhatsApp timestamps decide.
    orc.deliver(text("wamid.C", "3 bed", ts=now + 2))
    orc.deliver(text("wamid.A", "DHA 6 mein ghar", ts=now))
    orc.deliver(text("wamid.B", "rent pe", ts=now + 1))
    orc.drain()

    assert seen == [["wamid.A", "wamid.B", "wamid.C"]]
    assert orc.client.sent == [(CUSTOMER, reply("Customer", 3))]
    assert orc.client.read == ["wamid.C"]
    assert turns(orc) == [("customer", "customer", False, "replied", 3)]
    assert sorted(jobs(orc), key=lambda row: row[1] or "") == [
        ("succeeded", None), ("succeeded", "coalesced"), ("succeeded", "coalesced"),
    ]
    assert orc.query(
        "SELECT count(DISTINCT turn_id), count(*) FROM message WHERE direction = 'inbound'"
    ) == [(1, 3)]


def test_equal_timestamps_keep_arrival_order(orc, seen):
    now = int(time.time())
    orc.deliver(text("wamid.Z", ts=now))
    orc.deliver(text("wamid.Y", ts=now))
    orc.drain()

    assert seen == [["wamid.Z", "wamid.Y"]]


def test_a_message_after_the_turn_starts_the_next_turn(orc, seen):
    orc.deliver(text("wamid.IN1"))
    orc.drain()
    orc.deliver(text("wamid.IN2"))
    orc.drain()

    assert seen == [["wamid.IN1"], ["wamid.IN2"]]
    assert len(orc.client.sent) == 2
    assert [t[3:] for t in turns(orc)] == [("replied", 1), ("replied", 1)]


def test_messages_answered_before_the_orchestrator_are_never_swept_in(orc, seen):
    # Echo-era rows on RDS: recorded, their job succeeded, no turn_id.
    orc.deliver(text("wamid.OLD"))
    [(event_id,)] = orc.query(
        "UPDATE job SET status = 'succeeded', completed_at = now() "
        "WHERE job_type = 'process_message' RETURNING inbound_event_id"
    )
    record_event(orc.conn, orc.agency, event_id)
    orc.deliver(text("wamid.NEW"))
    orc.drain()

    assert seen == [["wamid.NEW"]]
    assert orc.query("SELECT turn_id FROM message WHERE provider_message_id = 'wamid.OLD'") == [
        (None,)
    ]


def test_conversations_never_share_a_turn(orc, seen):
    orc.deliver(text("wamid.A1", sender=CUSTOMER), text("wamid.B1", sender=CUSTOMER_2),
                text("wamid.A2", sender=CUSTOMER))
    orc.drain()

    assert sorted(seen) == [["wamid.A1", "wamid.A2"], ["wamid.B1"]]
    assert sorted(orc.client.sent, key=str) == sorted(
        [(CUSTOMER, reply("Customer", 2)), (CUSTOMER_2, reply("Customer"))], key=str
    )


def test_tenants_are_isolated(orc):
    orc.deliver(text("wamid.A1"), text("wamid.A2"))
    orc.deliver(text("wamid.X1"), pnid=PNID_B)
    orc.drain()

    assert turns(orc) == [("customer", "customer", False, "replied", 2)]
    assert turns(orc, agency_id=orc.other) == [("customer", "customer", False, "replied", 1)]
    # The same number is staff in one agency only.
    orc.deliver(text("wamid.X2", sender=AGENT), pnid=PNID_B)
    orc.drain()
    assert orc.client.sent[-1] == (AGENT, reply("Customer"))


# --- retries ------------------------------------------------------------------------

def test_replayed_jobs_never_repeat_a_turn(orc):
    orc.deliver(text("wamid.IN1"), text("wamid.IN2"))
    orc.drain()
    orc.query("UPDATE job SET status = 'pending', completed_at = NULL RETURNING id")
    orc.drain()

    assert len(orc.client.sent) == 1
    assert len(turns(orc)) == 1


def test_a_failed_attempt_resumes_the_same_turn(orc, seen):
    orc.client.outcomes = [whatsapp.RetryableSendError("http_500")]
    orc.deliver(text("wamid.IN1"), text("wamid.IN2"))
    orc.worker.run_once()
    assert turns(orc)[0][3] == "open"

    orc.drain()
    assert seen == [["wamid.IN1", "wamid.IN2"]] * 2
    assert orc.client.sent == [(CUSTOMER, reply("Customer", 2))]
    assert turns(orc) == [("customer", "customer", False, "replied", 2)]
    assert orc.query("SELECT status, attempts FROM outbound_message") == [("sent", 2)]


def test_a_newer_job_finishes_an_open_turn_before_its_own(orc, seen):
    orc.client.outcomes = [whatsapp.RetryableSendError("http_500")]
    orc.deliver(text("wamid.IN1"), text("wamid.IN2"))
    orc.worker.run_once()
    # The failed job waits on backoff; a new message arrives meanwhile.
    orc.query("UPDATE job SET available_at = now() + interval '1 hour' "
              "WHERE status = 'pending' RETURNING id")
    orc.deliver(text("wamid.IN3"))
    orc.drain()

    assert orc.client.sent == [
        (CUSTOMER, reply("Customer", 2)), (CUSTOMER, reply("Customer", 1)),
    ]
    orc.query("UPDATE job SET available_at = now() WHERE status = 'pending' RETURNING id")
    orc.drain()
    assert len(orc.client.sent) == 2
    assert [t[3:] for t in turns(orc)] == [("replied", 2), ("replied", 1)]


def test_permanent_send_failure_closes_the_turn_and_dead_letters(orc):
    orc.client.outcomes = [whatsapp.PermanentSendError("http_400_meta_131047")]
    orc.deliver(text("wamid.IN1"))
    orc.drain()

    assert jobs(orc) == [("dead", "http_400_meta_131047")]
    assert turns(orc)[0][3] == "replied"
    orc.deliver(text("wamid.IN2"))
    orc.drain()
    assert orc.client.sent == [(CUSTOMER, reply("Customer"))]


# --- control-state gate stub --------------------------------------------------------

@pytest.mark.parametrize(("control_state", "mode"), [
    ("human_owned", "human"), ("handoff_pending", "human"), ("ai_active", "paused"),
])
def test_gate_silences_the_ai(orc, control_state, mode):
    orc.deliver(text("wamid.IN1"))
    orc.drain()
    orc.query("UPDATE conversation SET control_state = %s, mode = %s RETURNING id",
              (control_state, mode))
    orc.deliver(text("wamid.IN2"), text("wamid.IN3"))
    orc.drain()

    assert len(orc.client.sent) == 1  # only the first turn's reply
    assert orc.client.read == ["wamid.IN1"]
    assert [t[3:] for t in turns(orc)] == [("replied", 1), ("gated", 2)]
    assert orc.query("SELECT control_state, conversation_mode FROM conversation_turn "
                     "WHERE status = 'gated'") == [(control_state, mode)]
    assert len(orc.query("SELECT id FROM outbound_message")) == 1


# --- staff #demo commands -----------------------------------------------------------

def test_demo_commands_are_for_staff_only(orc):
    orc.deliver(text("wamid.S1", "#demo buttons", sender=ADMIN))
    orc.deliver(text("wamid.C1", "#demo buttons"))
    orc.drain()

    sent = dict(orc.client.sent)
    assert sent[ADMIN]["interactive"]["type"] == "button"
    assert sent[CUSTOMER] == reply("Customer")


def test_staff_text_is_not_a_demo_command(orc):
    orc.deliver(text("wamid.S1", "#demo buttons", sender=AGENT),
                text("wamid.S2", "DHA 6 wala ghar sold", sender=AGENT))
    orc.drain()

    assert orc.client.sent == [(AGENT, reply("Staff", 2))]


# --- schema (migration 010) ---------------------------------------------------------

def test_turn_constraints(orc):
    orc.deliver(text("wamid.IN1"), text("wamid.X1", sender=CUSTOMER_2))
    orc.drain()
    conn = orc.conn
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        conn, orc.agency
    ):
        # A message can only join a turn of its own conversation.
        conn.execute(
            "UPDATE message SET turn_id = (SELECT turn_id FROM message "
            "WHERE provider_message_id = 'wamid.X1') WHERE provider_message_id = 'wamid.IN1'"
        )
    with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(conn, orc.agency):
        conn.execute("UPDATE conversation_turn SET route = 'staff'")
    with pytest.raises(psycopg.errors.UniqueViolation), tenant_transaction(conn, orc.agency):
        conn.execute("UPDATE conversation_turn SET status = 'open', completed_at = NULL")
        conn.execute(
            "INSERT INTO conversation_turn (agency_id, conversation_id, trigger_job_id, "
            "sender_role, route, control_state, conversation_mode, message_count) "
            "SELECT agency_id, conversation_id, trigger_job_id, sender_role, route, "
            "control_state, conversation_mode, 1 FROM conversation_turn LIMIT 1"
        )


def test_turns_are_invisible_to_other_tenants(orc):
    orc.deliver(text("wamid.IN1"))
    orc.drain()
    with tenant_transaction(orc.conn, orc.other):
        assert orc.conn.execute("SELECT count(*) FROM conversation_turn").fetchone() == (0,)
        assert orc.conn.execute(
            "UPDATE conversation_turn SET status = 'gated' RETURNING id"
        ).fetchall() == []
