"""Synthetic full-session replay through signed HTTP, real ingestion and workers."""
import hashlib
import hmac
import json
import random
import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import web
from tests.conftest import app_role_db, migration_database
from tests.test_customer_flow_db import tap, turn
from tests.test_customer_search_db import SCENARIO_2_PATCH, search_turn
from tests.test_gateway import status
from tests.test_requirements_db import PNID, p, st
from tests.test_search_db import CUSTOMER_B
from tests.test_speech import scribe_body
from tests.test_turn_engine import text
from tests.test_voice_flow_db import vf


def snapshot(st):
    """Stable identities retain relationships without generated UUIDs or wall clocks."""
    queries = {
        "turns": "SELECT route, status, message_count, replan_count FROM conversation_turn "
                 "ORDER BY created_at, id",
        "messages": "SELECT m.provider_message_id, m.message_type, m.transcript, "
                    "(SELECT array_agg(s.provider_message_id ORDER BY s.provider_message_id) "
                    "FROM message s WHERE s.turn_id = m.turn_id) FROM message m "
                    "WHERE m.direction = 'inbound' ORDER BY m.provider_message_id",
        "outbound": "SELECT provider_message_id, payload, status, delivery_status "
                    "FROM outbound_message ORDER BY provider_message_id",
        "jobs": "SELECT dedupe_key, job_type, status FROM job ORDER BY dedupe_key",
        "leads": "SELECT code, status, reason FROM lead ORDER BY code",
        "handoffs": "SELECT status FROM handoff ORDER BY created_at",
        "shown": "SELECT p.listing_code, s.position FROM shown_item s JOIN property p "
                 "ON p.id = s.property_id ORDER BY p.listing_code, s.position",
        "media": "SELECT count(*) FROM media_sent",
        "clarifications": "SELECT slot, status, candidates FROM pending_clarification "
                          "ORDER BY created_at",
        "control": "SELECT control_state, mode FROM conversation ORDER BY customer_wa_id",
        "requirements": "SELECT version, slots FROM requirement_state ORDER BY conversation_id",
    }
    # Exact same-DB replay must also preserve timestamps/UUIDs embedded in JSON.
    return {name: st.query(sql) for name, sql in queries.items()}


def run_session(vf, monkeypatch, seed):
    st = vf.st
    monkeypatch.setattr(web, "load_settings", lambda: SimpleNamespace(
        meta_app_secret="synthetic-replay-secret", database_url=st.app_url,
        webhook_sender_agencies=f"{PNID}={st.agency}"))
    http = TestClient(web.app)
    captured = []
    rng = random.Random(seed)
    inbound_count = 0

    def post(envelope):
        body = json.dumps(envelope).encode()
        signature = hmac.new(b"synthetic-replay-secret", body, hashlib.sha256).hexdigest()
        response = http.post("/webhook", content=body, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + signature})
        assert response.status_code == 200
        assert response.json() == {"status": "received"}

    def deliver(*messages, statuses=()):
        nonlocal inbound_count
        for message in messages:
            inbound_count += 1
            message["id"] = f"wamid.IN{inbound_count}"
        items = [("messages", m) for m in messages] + [("statuses", s) for s in statuses]
        if seed:
            rng.shuffle(items)
        for kind, item in items:
            envelope = {
                "object": "whatsapp_business_account",
                "entry": [{"changes": [{"field": "messages", "value": {
                    "metadata": {"phone_number_id": PNID}, kind: [item]}}]}],
            }
            captured.append(envelope)
            post(envelope)

    st.deliver = deliver
    # Search, ordered voice + correction, property tap, then handoff.
    search_turn(st, "DHA Phase 6 mein 500 gaz ghar, budget aath crore tak",
                SCENARIO_2_PATCH, CUSTOMER_B)
    vf.scribe.script.append(scribe_body("budget 8 crore tak"))
    voice = vf.voice()
    correction = text("nahi budget 7 crore tak", sender=CUSTOMER_B)
    correction["timestamp"] = str(int(voice["timestamp"]) + 1)
    vf.say(voice, correction, calls=[
        ("update_requirements", {"patch": [p("budget", "set", "7 crore tak")]}),
        ("search_properties", {}),
    ])
    assert st.state()[1]["budget"]["value"]["max"] == 70_000_000
    tap(st, "listing:DH6-102")
    turn(st, "visit karni hai", ("request_handoff", {"reason": "visit"}))
    tap(st, "handoff:visit:all")
    # Status arrival order deliberately reverses the normal transport progression.
    for wamid, in st.query("SELECT provider_message_id FROM outbound_message "
                           "WHERE provider_message_id IS NOT NULL"):
        deliver(statuses=[status(wamid, s) for s in ("read", "delivered", "sent")])
    st.drain()
    before, sends, requests = snapshot(st), len(st.client.sent), len(st.requests)
    assert all(row[0] == "read" for row in st.query(
        "SELECT delivery_status FROM outbound_message WHERE provider_message_id IS NOT NULL"))
    for _ in range(2):
        replay = list(captured)
        rng.shuffle(replay)
        for envelope in replay:
            post(envelope)
        st.drain()
        assert snapshot(st) == before
        assert len(st.client.sent) == sends
        assert len(st.requests) == requests
    http.close()
    return normalized_snapshot(st, before)


def normalized_snapshot(st, raw):
    aliases = {str(st.agency): "agency"}
    aliases.update({str(value): key for key, value in st.ids.items()})
    for uid, wamid in st.query("SELECT id, provider_message_id FROM message "
                               "WHERE provider_message_id IS NOT NULL"):
        aliases[str(uid)] = wamid
    for uid, members in st.query(
        "SELECT t.id, array_agg(m.provider_message_id ORDER BY m.provider_message_id) "
        "FROM conversation_turn t JOIN message m ON m.turn_id = t.id GROUP BY t.id"):
        aliases[str(uid)] = "turn:" + ",".join(members)
    pattern = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")

    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in sorted(value.items())
                    if not key.endswith("_at")}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            def replace(match):
                key = match.group()
                if key not in aliases:
                    aliases[key] = f"ref:{len(aliases)}"
                return aliases[key]
            return pattern.sub(replace, value)
        return value

    normalized = {key: normalize(value) for key, value in sorted(raw.items())}
    normalized["jobs"].sort(key=lambda row: row[0])
    return normalized


@pytest.mark.parametrize("seed", [0, 19, 73])
def test_signed_session_replayed_twice_and_statuses_shuffled(vf, monkeypatch, seed):
    run_session(vf, monkeypatch, seed)


def test_fresh_full_session_shuffle_matches_canonical(test_database_url, monkeypatch):
    # Sequential isolated schemas: role-password fixtures must never run concurrently.
    results = []
    for seed in (0, 19):
        with (
            contextmanager(migration_database.__wrapped__)(test_database_url) as db,
            contextmanager(app_role_db.__wrapped__)(db) as role_db,
            contextmanager(st.__wrapped__)(role_db) as state,
            contextmanager(vf.__wrapped__)(state) as flow,
        ):
            results.append(run_session(flow, monkeypatch, seed))
    assert results[0] == results[1]
