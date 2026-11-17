"""Audit MA-006 (WP4): usage limits. Real worker, orchestrator, turn engine and outbound
path (disposable PostgreSQL, restricted manzil_app login, RLS on); Gemini is a fake
transport, WhatsApp the FakeClient. Limits are lowered through the environment so a few
messages reach them. All data and numbers are fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import logging
import time

import pytest

from app import limits, speech, turn_engine
from app.config import ConfigurationError
from tests.test_audio import voice_message
from tests.test_handoff_db import BROKER_NUMBER
from tests.test_handoff_gate_db import bodies, to
from tests.test_requirements_db import st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_A, CUSTOMER_B
from tests.test_turn_engine import answer, text

LIMIT = turn_engine.TEMPLATES["limit"]


def _send(st, body, sender=CUSTOMER_B):
    before = len(st.client.sent)
    st.deliver(text(body, sender=sender))
    st.drain()
    return bodies(to(st, sender, before))


def _turns(st, sender=CUSTOMER_B):
    return [r[0] for r in st.query(
        "SELECT t.status FROM conversation_turn t JOIN conversation c ON c.id = t.conversation_id "
        "WHERE c.customer_wa_id = %s ORDER BY t.created_at", (sender,))]


def test_over_the_hourly_limit_no_model_call_and_one_notice(st, monkeypatch):
    monkeypatch.setenv("LIMIT_CUSTOMER_TURNS_PER_HOUR", "2")
    assert _send(st, "salam") and _send(st, "salam")          # greetings, answered in code
    calls = len(st.requests)

    # The third turn would need the model; the script is empty, so a call would fail.
    assert _send(st, "DHA mein 2 bed flat chahiye") == [LIMIT]
    assert _send(st, "hello?") == []                           # same hour: silent
    assert len(st.requests) == calls
    assert _turns(st) == ["replied", "replied", "gated", "gated"]
    assert st.query("SELECT count(*) FROM outbound_message WHERE dedupe_key LIKE 'limit:%%'"
                    ) == [(1,)]


def test_the_notice_follows_the_customer_language(st, monkeypatch):
    monkeypatch.setenv("LIMIT_CUSTOMER_TURNS_PER_HOUR", "1")
    _send(st, "salam")
    assert _send(st, "please show me some houses for sale") == [
        turn_engine.TEMPLATES_EN["limit"]]


def test_the_daily_limit_counts_24_hours(st, monkeypatch):
    monkeypatch.setenv("LIMIT_CUSTOMER_TURNS_PER_DAY", "1")
    _send(st, "salam")
    assert _send(st, "salam") == [LIMIT]


def test_another_customer_is_not_affected(st, monkeypatch):
    monkeypatch.setenv("LIMIT_CUSTOMER_TURNS_PER_HOUR", "1")
    _send(st, "salam")
    assert _send(st, "salam") == [LIMIT]
    assert _send(st, "salam", sender=CUSTOMER_A) not in ([], [LIMIT])


def test_staff_are_exempt_from_the_per_number_limit(st, monkeypatch):
    monkeypatch.setenv("LIMIT_CUSTOMER_TURNS_PER_HOUR", "1")
    first, second = _send(st, "hi", BROKER_NUMBER), _send(st, "hi", BROKER_NUMBER)
    assert first and second and LIMIT not in first + second


def test_the_agency_model_call_cap_blocks_customers_and_alerts(st, monkeypatch, caplog):
    monkeypatch.setenv("LIMIT_AGENCY_MODEL_CALLS_PER_DAY", "1")
    st.script.append(answer())
    assert _send(st, "DHA mein 2 bed flat chahiye") != [LIMIT]   # one model call, metered
    assert st.query("SELECT count(*) FROM usage_event")[0][0] >= 1

    with caplog.at_level(logging.ERROR, logger="app.limits"):
        assert _send(st, "salam", sender=CUSTOMER_A) == [LIMIT]
    assert "ALERT agency_limit" in caplog.text


def test_a_voice_note_over_the_limit_is_never_staged_or_transcribed(st, monkeypatch):
    monkeypatch.setenv("LIMIT_CUSTOMER_VOICE_PER_HOUR", "1")
    st.speech.transcripts.append("salam")
    st.deliver(voice_message(f"wamid.V{time.time_ns()}", "media-v1", sender=CUSTOMER_B))
    st.drain()
    assert st.query("SELECT count(*) FROM inbound_audio") == [(1,)]

    before = len(st.client.sent)
    st.deliver(voice_message(f"wamid.V{time.time_ns()}", "media-v2", sender=CUSTOMER_B))
    st.drain()
    assert st.query("SELECT count(*) FROM inbound_audio") == [(1,)]       # not staged
    assert st.query("SELECT stt_meta FROM message WHERE media_id = 'media-v2'") == [
        ({"status": speech.LIMITED, "limit": "voice_per_hour"},)]
    assert bodies(to(st, CUSTOMER_B, before)) == [speech.TEMPLATES["voice_limit"]]
    assert st.speech.transcripts == []


def test_an_unusable_limit_stops_the_worker_at_start(monkeypatch):
    monkeypatch.setenv("LIMIT_CUSTOMER_TURNS_PER_HOUR", "0")
    with pytest.raises(ConfigurationError):
        limits.check_settings()
    monkeypatch.setenv("LIMIT_CUSTOMER_TURNS_PER_HOUR", "many")
    with pytest.raises(ConfigurationError):
        limits.check_settings()
