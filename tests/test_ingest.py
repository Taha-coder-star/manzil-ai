"""Task 2.4 acceptance tests: durable, idempotent, tenant-routed ingestion.

All data is fictional; phone numbers use the reserved 555 / 0300-0000xxx style.
"""

import hashlib
import hmac
import json
import threading

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import webhook
from app.db import tenant_transaction
from app.ingest import ingest_events
from app.provision_agency import create_agency
from app.routing import EnvSenderResolver
from app.web import app

client = TestClient(app)

SECRET = "test-app-secret"
PNID_A, PNID_B = "100000000000001", "100000000000002"
CUSTOMER = "923000000001"


def message(wamid, text="DHA Phase 6 mein ghar chahiye"):
    return {
        "from": CUSTOMER, "id": wamid, "timestamp": "1791273600",
        "type": "text", "text": {"body": text},
    }


def status(wamid, state):
    return {"id": wamid, "status": state, "timestamp": "1791273601", "recipient_id": CUSTOMER}


def change(phone_number_id, messages=(), statuses=()):
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": phone_number_id},
    }
    if messages:
        value["contacts"] = [{"profile": {"name": "Demo Customer"}, "wa_id": CUSTOMER}]
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = list(statuses)
    return {"field": "messages", "value": value}


def envelope(*changes):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA-DEMO", "changes": [c]} for c in changes],
    }


def post(payload):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook", content=body,
        headers={"Content-Type": "application/json", webhook.SIGNATURE_HEADER: signature},
    )


@pytest.fixture
def ingest_db(app_role_db, monkeypatch):
    admin_url, app_url, _ = app_role_db
    with psycopg.connect(app_url, autocommit=True) as conn:
        agency_a = create_agency(conn, "Demo Realty A (DEMO)", "demo-a", PNID_A)
        agency_b = create_agency(conn, "Demo Realty B (DEMO)", "demo-b", PNID_B)
        monkeypatch.setenv("META_APP_SECRET", SECRET)
        monkeypatch.setenv("DATABASE_URL", app_url)
        monkeypatch.setenv(
            "WEBHOOK_SENDER_AGENCIES", f"{PNID_A}={agency_a},{PNID_B}={agency_b}"
        )

        def rows(agency_id):
            with tenant_transaction(conn, agency_id):
                events = conn.execute(
                    "SELECT id, event_key, event_type FROM inbound_event ORDER BY event_key"
                ).fetchall()
                jobs = conn.execute(
                    "SELECT inbound_event_id, job_type, dedupe_key, status FROM job"
                ).fetchall()
            return events, jobs

        yield {
            "conn": conn, "admin_url": admin_url, "app_url": app_url,
            "a": agency_a, "b": agency_b, "rows": rows,
        }


def test_sequential_duplicate_stores_one_linked_pair(ingest_db):
    payload = envelope(change(PNID_A, messages=[message("wamid.SEQ1")]))

    assert post(payload).status_code == 200
    assert post(payload).status_code == 200

    events, jobs = ingest_db["rows"](ingest_db["a"])
    assert [(key, kind) for _, key, kind in events] == [("wa:msg:wamid.SEQ1", "message")]
    assert jobs == [(events[0][0], "process_message", "process_message:wamid.SEQ1", "pending")]


def test_concurrent_duplicates_store_one_pair(ingest_db):
    events, _ = webhook.extract_events(envelope(change(PNID_A, messages=[message("wamid.RACE")])))
    resolver = EnvSenderResolver({PNID_A: ingest_db["a"]})
    barrier = threading.Barrier(8)
    results, errors = [], []

    def deliver():
        barrier.wait()
        try:
            results.append(ingest_events(ingest_db["app_url"], resolver, events))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sum(r.stored for r in results) == 1
    assert sum(r.duplicate for r in results) == 7
    stored_events, jobs = ingest_db["rows"](ingest_db["a"])
    assert len(stored_events) == 1 and len(jobs) == 1


def test_job_failure_rolls_back_event_and_retry_succeeds(ingest_db):
    payload = envelope(change(PNID_A, messages=[message("wamid.FAIL")]))
    with psycopg.connect(ingest_db["admin_url"], autocommit=True) as admin:
        admin.execute(
            "CREATE FUNCTION fail_job_insert() RETURNS trigger LANGUAGE plpgsql "
            "AS $$BEGIN RAISE EXCEPTION 'forced job failure'; END$$"
        )
        admin.execute(
            "CREATE TRIGGER fail_job_insert BEFORE INSERT ON job "
            "FOR EACH ROW EXECUTE FUNCTION fail_job_insert()"
        )

    assert post(payload).status_code == 503
    assert ingest_db["rows"](ingest_db["a"]) == ([], [])

    with psycopg.connect(ingest_db["admin_url"], autocommit=True) as admin:
        admin.execute("DROP TRIGGER fail_job_insert ON job")

    assert post(payload).status_code == 200
    events, jobs = ingest_db["rows"](ingest_db["a"])
    assert len(events) == 1 and len(jobs) == 1


def test_statuses_and_batch_envelope(ingest_db):
    payload = envelope(
        change(PNID_A, messages=[message("wamid.M1"), message("wamid.M2", "Phase 8 bhi")]),
        change(PNID_A, statuses=[status("wamid.OUT", "sent"), status("wamid.OUT", "delivered")]),
        change(PNID_A, statuses=[status("wamid.OUT", "read"), status("wamid.OUT", "delivered")]),
    )

    assert post(payload).status_code == 200
    assert post(payload).status_code == 200

    events, jobs = ingest_db["rows"](ingest_db["a"])
    assert [key for _, key, _ in events] == [
        "wa:msg:wamid.M1", "wa:msg:wamid.M2",
        "wa:status:wamid.OUT:delivered", "wa:status:wamid.OUT:read", "wa:status:wamid.OUT:sent",
    ]
    assert sorted(job_type for _, job_type, _, _ in jobs) == [
        "process_message", "process_message",
        "reconcile_status", "reconcile_status", "reconcile_status",
    ]
    event_ids = {event_id for event_id, _, _ in events}
    assert {event_id for event_id, _, _, _ in jobs} == event_ids


def test_payload_keeps_message_media_and_contact(ingest_db):
    image = {
        "from": CUSTOMER, "id": "wamid.IMG", "timestamp": "1791273600", "type": "image",
        "image": {"id": "MEDIA-1", "mime_type": "image/jpeg", "sha256": "abc"},
    }
    assert post(envelope(change(PNID_A, messages=[image]))).status_code == 200

    conn = ingest_db["conn"]
    with tenant_transaction(conn, ingest_db["a"]):
        payload, provider_id = conn.execute(
            "SELECT payload, provider_message_id FROM inbound_event"
        ).fetchone()
    assert provider_id == "wamid.IMG"
    assert payload["message"]["image"]["id"] == "MEDIA-1"
    assert payload["contact"]["wa_id"] == CUSTOMER
    assert payload["metadata"]["phone_number_id"] == PNID_A


def test_two_senders_route_to_their_own_agency(ingest_db):
    payload = envelope(
        change(PNID_A, messages=[message("wamid.FOR_A")]),
        change(PNID_B, messages=[message("wamid.FOR_B")]),
    )

    assert post(payload).status_code == 200

    events_a, _ = ingest_db["rows"](ingest_db["a"])
    events_b, _ = ingest_db["rows"](ingest_db["b"])
    assert [key for _, key, _ in events_a] == ["wa:msg:wamid.FOR_A"]
    assert [key for _, key, _ in events_b] == ["wa:msg:wamid.FOR_B"]
    conn = ingest_db["conn"]
    assert conn.execute("SELECT count(*) FROM inbound_event").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM job").fetchone() == (0,)


def test_unknown_sender_is_dropped_not_defaulted(ingest_db):
    payload = envelope(change("100000000000999", messages=[message("wamid.STRANGER")]))

    assert post(payload).status_code == 200

    assert ingest_db["rows"](ingest_db["a"]) == ([], [])
    assert ingest_db["rows"](ingest_db["b"]) == ([], [])


def test_inactive_agency_is_dropped(ingest_db):
    conn = ingest_db["conn"]
    with tenant_transaction(conn, ingest_db["b"]):
        conn.execute("UPDATE agency SET is_active = false")

    assert post(envelope(change(PNID_B, messages=[message("wamid.INACTIVE")]))).status_code == 200

    assert ingest_db["rows"](ingest_db["b"]) == ([], [])


def test_mapping_to_agency_that_does_not_own_sender_fails(ingest_db, monkeypatch):
    # PNID_B mapped to agency A: A's row has a different phone number id.
    monkeypatch.setenv(
        "WEBHOOK_SENDER_AGENCIES", f"{PNID_A}={ingest_db['a']},{PNID_B}={ingest_db['a']}"
    )

    assert post(envelope(change(PNID_B, messages=[message("wamid.MISROUTE")]))).status_code == 503

    assert ingest_db["rows"](ingest_db["a"]) == ([], [])
    assert ingest_db["rows"](ingest_db["b"]) == ([], [])


@pytest.mark.parametrize("variable", ["WEBHOOK_SENDER_AGENCIES", "DATABASE_URL"])
def test_missing_configuration_is_not_acknowledged(ingest_db, monkeypatch, variable):
    monkeypatch.delenv(variable)

    assert post(envelope(change(PNID_A, messages=[message("wamid.NOCONF")]))).status_code == 503

    assert ingest_db["rows"](ingest_db["a"]) == ([], [])


def test_provisioning_rejects_duplicate_sender(ingest_db):
    with pytest.raises(psycopg.errors.UniqueViolation):
        create_agency(ingest_db["conn"], "Copy (DEMO)", "demo-copy", PNID_A)
