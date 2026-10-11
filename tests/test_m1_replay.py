"""Task 5.4 gate M1: (d) a replayed, correctly signed webhook creates no duplicate
event, job, message or outbound message (app.m1_replay), and the operator view of
(a)-(c) (app.m1_status); end to end through the real web app and worker, WhatsApp
is the fake of tests/test_media.py."""

import io
import threading
import time
import uuid
from contextlib import redirect_stdout
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import m1_replay, m1_status, webhook
from app.db import tenant_transaction
from app.handlers import make_handlers
from app.provision_agency import create_agency
from app.web import app
from app.worker import Worker
from tests.test_ingest import CUSTOMER, PNID_A, SECRET, change, envelope, post
from tests.test_media import STAFF, FakeChannel, FakeStorage, photo

client = TestClient(app)
TEXT = "Fictional replay check DHA Phase 6"
BROKER = "923000000008"


def message(wamid, text, sender=CUSTOMER):
    # Current time: an old timestamp is outside the 24 h window and gets no reply.
    return {"from": sender, "id": wamid, "timestamp": str(int(time.time())),
            "type": "text", "text": {"body": text}}


def via_test_client(url, body, headers):
    assert url == "http://127.0.0.1:8000/webhook"
    return client.post("/webhook", content=body, headers=headers).status_code


@pytest.fixture
def m1(app_role_db, monkeypatch):
    _, app_url, _ = app_role_db
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, "Demo Realty (DEMO)", "demo", PNID_A)
    with tenant_transaction(conn, agency):
        conn.execute(
            "INSERT INTO staff (agency_id, name, whatsapp_number, role) VALUES "
            "(current_agency_id(), 'Demo agent', %s, 'agent'), "
            "(current_agency_id(), 'Demo broker', %s, 'broker')", (STAFF, BROKER))
    environ = {"META_APP_SECRET": SECRET, "DATABASE_URL": app_url, "PORT": "8000"}
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("WEBHOOK_SENDER_AGENCIES", f"{PNID_A}={agency}")
    channel = FakeChannel()
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket=None, s3_region=None,
        s3_endpoint_url=None, media_public_base_url="https://demo-bucket.example",
        site_address="demo.example",
    )
    handlers = make_handlers(settings, client_factory=lambda pnid: channel,
                             storage=FakeStorage(), image_link=lambda: "https://x.example/a.jpg")
    worker = Worker(app_url, [agency], handlers, threading.Event(), backoff=lambda n: 0)

    def drain(*_):
        while worker.run_once():
            pass

    def query(sql):
        with tenant_transaction(conn, agency):
            return conn.execute(sql).fetchall()

    yield SimpleNamespace(conn=conn, agency=agency, environ=environ, channel=channel,
                          drain=drain, query=query)
    worker.close()
    conn.close()


def deliver_and_answer(m1, wamid="wamid.M1REPLAY01"):
    assert post(envelope(change(PNID_A, messages=[message(wamid, TEXT)]))).status_code == 200
    m1.drain()
    assert len(m1.channel.sent) == 1  # the stub reply


def replay(m1, *args, post_fn=via_test_client):
    out = io.StringIO()
    with redirect_stdout(out):
        code = m1_replay.main(["--agency-id", str(m1.agency), *args], m1.environ,
                              post=post_fn, sleep=m1.drain)
    return code, out.getvalue()


def test_replayed_webhook_creates_nothing_new(m1):
    deliver_and_answer(m1)
    code, out = replay(m1, "--times", "3")

    assert code == 0, out
    assert "HTTP [200, 200, 200]" in out
    assert "M1(d) PASS" in out
    for name, count in [("inbound_event", 1), ("job", 1), ("message", 1),
                        ("outbound_message", 1), ("conversation_turn", 1)]:
        assert f"{name}: before {count}, after {count}" in out
    assert len(m1.channel.sent) == 1  # no second reply reached WhatsApp
    assert m1.query("SELECT count(*) FROM inbound_event") == [(1,)]
    for private in (CUSTOMER, TEXT, SECRET, "sha256=", "wamid.M1REPLAY01"):
        assert private not in out
    assert "WAMID ...PLAY01" in out


def test_the_rebuilt_body_is_a_real_duplicate():
    """Same event key and job dedupe key as the original delivery."""
    original = envelope(change(PNID_A, messages=[message("wamid.SAME", TEXT)]))
    [event], _ = webhook.extract_events(original)
    [again], _ = webhook.extract_events(m1_replay.envelope(event.payload))
    assert (again.event_key, again.dedupe_key, again.phone_number_id, again.payload) == (
        event.event_key, event.dedupe_key, event.phone_number_id, event.payload)


def test_a_new_message_would_be_counted(m1):
    """Control: the counters do see a genuinely new delivery."""
    deliver_and_answer(m1, "wamid.M1FIRST")
    deliver_and_answer_new = envelope(change(PNID_A, messages=[message("wamid.M1SECOND", TEXT)]))

    def post_new(url, body, headers):
        return post(deliver_and_answer_new).status_code

    code, out = replay(m1, "--times", "1", post_fn=post_new)
    # The picked event is M1FIRST; the new message adds a turn and a reply to its conversation.
    assert code == 2 and "M1(d) FAIL" in out
    assert "outbound_message: before 1, after 2" in out


def test_wrong_signature_fails(m1):
    deliver_and_answer(m1)
    m1.environ["META_APP_SECRET"] = "not-the-app-secret"
    code, out = replay(m1)
    assert code == 2 and "HTTP [401, 401]" in out and "M1(d) FAIL" in out


def test_unprocessed_or_unknown_events_are_not_replayed(m1):
    code, out = replay(m1)
    assert code == 1 and "no processed inbound message event" in out
    deliver_and_answer(m1)
    code, out = replay(m1, "--event-id", str(uuid.uuid4()))
    assert code == 1 and "with that id" in out


@pytest.mark.parametrize(("args", "environ", "text"), [
    (["--agency-id", "x"], {"META_APP_SECRET": "s", "DATABASE_URL": "d"}, "must be UUIDs"),
    (["--agency-id", str(uuid.uuid4())], {"DATABASE_URL": "d"}, "must be set"),
    (["--agency-id", str(uuid.uuid4()), "--times", "9"],
     {"META_APP_SECRET": "s", "DATABASE_URL": "d"}, "--times must be 1-5"),
])
def test_bad_arguments(args, environ, text):
    out = io.StringIO()
    with redirect_stdout(out):
        assert m1_replay.main(args, environ) == 1
    assert text in out.getvalue()


def test_status_view_shows_coalescing_routes_and_photos(m1):
    since = "2000-01-01T00:00:00+00:00"
    # (a) three customer texts before the worker runs: one turn, one reply; a broker turn.
    post(envelope(change(PNID_A, messages=[message(f"wamid.M1C{n}", TEXT) for n in range(3)])))
    m1.drain()
    post(envelope(change(PNID_A, messages=[message("wamid.M1B", TEXT, BROKER)])))
    m1.drain()
    # (b) a staff photo.
    m1.channel.files["777"] = photo()
    post(envelope(change(PNID_A, messages=[{
        "from": STAFF, "id": "wamid.M1P", "timestamp": str(int(time.time())),
        "type": "image", "image": {"id": "777", "mime_type": "image/jpeg"}}])))
    m1.drain()

    out = io.StringIO()
    with redirect_stdout(out):
        assert m1_status.main(["--agency-id", str(m1.agency), "--since", since], m1.environ) == 0
    text = out.getvalue()
    assert "customer 3 message(s) replied replies sent 1" in text
    assert "broker   1 message(s) replied replies sent 1" in text
    assert "staff    1 message(s) replied" in text
    assert "Staff photos (M1(b)): 1" in text and "stored" in text and "exif_stripped=true" in text
    assert f"original (private): tenant/{m1.agency}/inbound/" in text
    assert "card (public): https://demo-bucket.example/card/" in text
    assert "Inbound audio (M1(c)): table not present" in text or "Inbound audio (M1(c)): 0" in text
    for private in (CUSTOMER, BROKER, STAFF, TEXT, "777", "wamid"):
        assert private not in text


@pytest.mark.parametrize(("args", "text"), [
    (["--agency-id", "x", "--since", "2026-10-11T00:00+00:00"], "must be a UUID"),
    (["--agency-id", str(uuid.uuid4()), "--since", "2026-10-11T00:00"], "needs a UTC offset"),
])
def test_status_bad_arguments(args, text):
    out = io.StringIO()
    with redirect_stdout(out):
        assert m1_status.main(args, {"DATABASE_URL": "d"}) == 1
    assert text in out.getvalue()
