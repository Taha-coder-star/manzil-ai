"""Task 6.3 acceptance tests: the speech service (C4; RFC 4, R16 section 7, R-02).

Audio is generated fictional Ogg Opus (packet sizes only, no speech). Scribe and
Gemini are scripted fakes or real clients on httpx.MockTransport: no key, no
network. The WhatsApp client and the bucket are the fakes of tests/test_media.py.
Database tests use the restricted manzil_app login (RLS on). All data is fictional.
"""

import base64
import json
import logging
import struct
import threading
import time
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from app import audio, config, outbound, speech, webhook, whatsapp
from app.config import ConfigurationError
from app.db import tenant_transaction
from app.handlers import make_handlers
from app.ingest import store_event
from app.llm import GeminiClient, ProviderError, Transcription
from app.provision_agency import create_agency
from app.speech import TEMPLATES, SpeechService
from app.stt import ScribeClient, ScribeResult
from app.worker import Worker
from tests.test_media import CUSTOMER, PNID, STAFF, FakeChannel, FakeStorage

KEY = "test-key-not-a-secret"
MODEL = "fallback-model"
SPOKEN = "DHA Phase 6 mein 500 gaz ka ghar, budget aath crore tak"


# --- fictional Ogg Opus -----------------------------------------------------------

def page(packets, granule, seq, *, continued=False, serial=1):
    """One Ogg page holding whole packets (CRC left 0: the readers do not check it)."""
    table = []
    for packet in packets:
        table += [255] * (len(packet) // 255) + [len(packet) % 255]
    header = struct.pack("<4sBBqIIIB", b"OggS", 0, 1 if continued else 0, granule, serial,
                         seq, 0, len(table))
    return header + bytes(table) + b"".join(packets)


def opus_note(seconds, packet_bytes=60, pre_skip=312, per_page=50, pattern=None):
    """A fictional Ogg Opus note of 20 ms packets of packet_bytes each (or cycling
    through the sizes in pattern)."""
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, pre_skip, 16_000, 0, 0)
    pages = [page([head], 0, 0), page([b"OpusTags" + b"\0" * 8], 0, 1)]
    count, seq = int(seconds * 50), 2
    for first in range(0, count, per_page):
        n = min(per_page, count - first)
        granule = pre_skip + (first + n) * 960
        sizes = [pattern[(first + i) % len(pattern)] if pattern else packet_bytes
                 for i in range(n)]
        pages.append(page([b"\x01" * size for size in sizes], granule, seq))
        seq += 1
    return b"".join(pages)


def test_truncate_keeps_at_most_the_limit_and_closes_the_stream():
    data = opus_note(150)
    assert audio.ogg_duration_ms(data) == 150_000
    cut = audio.truncate_ogg(data, 120_000)
    assert audio.ogg_duration_ms(cut) == 120_000
    # The new last page has the end-of-stream flag and a valid checksum; the pages
    # before it are unchanged.
    last = cut.rindex(b"OggS")
    assert cut[:last] == data[:last]
    assert cut[last + 5] & audio.OGG_EOS
    stored = struct.unpack_from("<I", cut, last + 22)[0]
    zeroed = bytearray(cut[last:])
    struct.pack_into("<I", zeroed, 22, 0)
    assert stored == audio.ogg_crc(bytes(zeroed))


def test_ogg_crc_matches_the_reference_vector():
    # CRC-32/POSIX (check 0x765E7680) without its final inversion.
    assert audio.ogg_crc(b"") == 0
    assert audio.ogg_crc(b"123456789") == 0x89A1897F


@pytest.mark.parametrize("data", [b"", b"ID3\x04", opus_note(1)[:30],
                                  page([b"OpusHead" + b"\0" * 11], 0, 0)])
def test_unreadable_audio_is_not_truncated(data):
    assert audio.truncate_ogg(data, 120_000) is None


# --- sanity checks, notices, keyterms -------------------------------------------------

@pytest.mark.parametrize(("text", "kwargs", "reason"), [
    (SPOKEN, {"duration_ms": 6_000, "language": "ur", "language_probability": 0.9}, None),
    ("DHA فیز 6 میں 500 گز کا گھر", {"duration_ms": 4_000}, None),
    ("डीएचए फेज 6 में घर", {"duration_ms": 3_000, "language": "hi"}, None),
    ("I need a flat in Clifton", {"language": "en"}, None),
    ("", {}, "no_speech"),
    ("  ...  ", {}, "no_speech"),
    ("Je cherche une maison", {"language": "fr", "language_probability": 0.95},
     "unsupported_language"),
    ("ghar chahiye", {"language": "pa", "language_probability": 0.4}, None),
    ("ДХА фаза шесть дом", {}, "bad_script"),
    (" ".join(["ghar"] * 30), {"duration_ms": 2_000}, "implausible_rate"),
    ("ghar ghar ghar ghar ghar", {"duration_ms": 10_000}, "repetition"),
    ("thank you for watching " * 5, {"duration_ms": 30_000}, "repetition"),
    ("haan", {"duration_ms": 30_000}, None),
])
def test_check_transcript(text, kwargs, reason):
    assert speech.check_transcript(text, **kwargs) == reason


def test_notices_are_approved_templates_once_each():
    assert speech.notice_texts(()) == []
    assert speech.notice_texts(("transcribed",)) == []
    assert speech.notice_texts(("no_speech", "too_short", "failed")) == [
        TEMPLATES["voice_retry"], TEMPLATES["voice_failed"]]
    assert speech.notice_texts(("truncated",)) == [TEMPLATES["voice_truncated"]]
    assert speech.notice_texts(("unsupported_language",)) == [TEMPLATES["voice_language"]]
    for text in TEMPLATES.values():
        assert len(text) < 200


def test_keyterms_follow_the_api_rules():
    names = ["DHA Defence", "dha defence", "x" * 60, "a b c d e f", "Precinct <6>",
             "فیز 6", "Bahria Town Karachi", None, ""]
    terms = speech.build_keyterms(names, 100)
    assert terms[:len(speech.DOMAIN_TERMS)] == list(speech.DOMAIN_TERMS)
    assert "DHA Defence" in terms and "dha defence" not in terms
    assert "Bahria Town Karachi" in terms
    for term in terms:
        assert len(term) < 50 and len(term.split()) <= 5
        assert not set(term) & set("<>{}[]\\")
    assert len(speech.build_keyterms([f"Area {n}" for n in range(500)], 100)) == 100


# --- provider clients -----------------------------------------------------------------

def scribe_body(text=SPOKEN, language="urd", probability=0.93, events=0):
    words = [{"text": word, "type": "word", "logprob": -0.1} for word in text.split()
             if not word.startswith("(")]
    words += [{"text": "(music)", "type": "audio_event", "logprob": 0}] * events
    return {"language_code": language, "language_probability": probability, "text": text,
            "words": words}


def test_scribe_request_and_response():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=scribe_body())

    client = ScribeClient(KEY, transport=httpx.MockTransport(handler))
    result = client.transcribe(b"OggS-audio", "audio/ogg", ["DHA", "marla"], timeout_s=5)
    assert result == ScribeResult(SPOKEN, "urd", 0.93, len(SPOKEN.split()), 0)
    [request] = seen
    assert str(request.url) == "https://api.elevenlabs.io/v1/speech-to-text"
    assert request.headers["xi-api-key"] == KEY
    body = request.read().decode("latin-1")
    for field, value in [("model_id", "scribe_v2"), ("tag_audio_events", "true")]:
        assert f'name="{field}"\r\n\r\n{value}\r\n' in body
    assert body.count('name="keyterms"') == 2
    assert 'name="language_code"' not in body
    assert 'filename="voice.ogg"' in body and "OggS-audio" in body
    assert KEY not in repr(client)


def test_scribe_caps_keyterms_at_100():
    seen = []
    client = ScribeClient(KEY, transport=httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200, json=scribe_body())))
    client.transcribe(b"x", "audio/ogg", [f"term {n}" for n in range(150)], timeout_s=5)
    assert seen[0].read().decode().count('name="keyterms"') == 100


@pytest.mark.parametrize(("response", "code"), [
    (httpx.Response(401, json={"detail": "fictional"}), "http_401"),
    (httpx.Response(500), "http_500"),
    (httpx.Response(200, content=b"not json"), "invalid_json"),
    (httpx.Response(200, json={"words": []}), "malformed:no_text"),
])
def test_scribe_errors_are_sanitised_codes(response, code):
    client = ScribeClient(KEY, transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(ProviderError) as caught:
        client.transcribe(b"x", "audio/ogg", [], timeout_s=5)
    assert caught.value.code == code


def gemini_reply(transcript=SPOKEN, speech_present=True, language="ur", reason="STOP"):
    text = json.dumps({"speech_present": speech_present, "transcript": transcript,
                       "language": language}, ensure_ascii=False)
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "thinking", "thought": True}, {"text": text}]}, "finishReason": reason}],
        "usageMetadata": {"promptTokenCount": 400, "candidatesTokenCount": 30},
        "modelVersion": "fallback-model-001"}


def test_gemini_transcription_request_and_response():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=gemini_reply())

    client = GeminiClient(KEY, transport=httpx.MockTransport(handler))
    result = client.transcribe(MODEL, b"OggS-audio", "audio/ogg", timeout_s=5)
    assert result.transcript == SPOKEN and result.speech_present and result.language == "ur"
    assert result.usage.input_tokens == 400 and result.model_version == "fallback-model-001"
    [request] = seen
    assert request.url.path.endswith(f"/models/{MODEL}:generateContent")
    assert request.headers["x-goog-api-key"] == KEY and KEY not in str(request.url)
    body = json.loads(request.content)
    inline = body["contents"][0]["parts"][0]["inlineData"]
    assert inline == {"mimeType": "audio/ogg",
                      "data": base64.b64encode(b"OggS-audio").decode()}
    config_ = body["generationConfig"]
    assert config_["responseMimeType"] == "application/json"
    assert config_["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "tools" not in body
    assert "Never translate" in body["systemInstruction"]["parts"][0]["text"]


@pytest.mark.parametrize(("reply", "code"), [
    (gemini_reply(reason="SAFETY"), "refused:safety"),
    (gemini_reply(reason="MAX_TOKENS"), "finish:max_tokens"),
    ({"candidates": [{"content": {"parts": [{"text": "plain words"}]},
                      "finishReason": "STOP"}]}, "malformed:not_json"),
    ({"candidates": [{"content": {"parts": [{"text": "{\"transcript\": 5}"}]},
                      "finishReason": "STOP"}]}, "malformed:transcription"),
])
def test_unusable_gemini_transcriptions_raise(reply, code):
    client = GeminiClient(KEY, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=reply)))
    with pytest.raises(ProviderError) as caught:
        client.transcribe(MODEL, b"x", "audio/ogg", timeout_s=5)
    assert caught.value.code == code


# --- settings -----------------------------------------------------------------------

def settings(**overrides):
    values = {"ai_provider": "stub", "stt_provider": "elevenlabs",
              "elevenlabs_api_key": KEY, "s3_bucket": "demo-bucket", "stt_fallback": None,
              "gemini_api_key": KEY, "llm_primary_model": "gemini-3.5-flash-lite"}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(("overrides", "message"), [
    ({"stt_provider": "whisper"}, "STT_PROVIDER must be one of"),
    ({"stt_fallback": "openai"}, "STT_FALLBACK must be one of"),
    ({"elevenlabs_api_key": None}, "requires ELEVENLABS_API_KEY"),
    ({"s3_bucket": None}, "requires S3_BUCKET"),
    ({"stt_fallback": "gemini", "gemini_api_key": None}, "requires GEMINI_API_KEY"),
    ({"llm_primary_model": "bad/model"}, "not a valid model ID"),
])
def test_unusable_speech_settings_fail_clearly(overrides, message):
    with pytest.raises(ConfigurationError, match=message) as caught:
        config.check_stt_settings(settings(**overrides))
    assert KEY not in str(caught.value)


def test_speech_settings_defaults():
    config.check_stt_settings(SimpleNamespace())  # off by default
    config.check_stt_settings(settings(stt_provider="off", elevenlabs_api_key=None))
    assert config.stt_fallback(settings()) == "gemini"
    assert config.stt_fallback(settings(gemini_api_key=None)) == "off"
    assert config.stt_fallback(settings(stt_fallback="off")) == "off"
    assert speech.build_speech(settings(stt_provider="off"), FakeStorage()) is None
    built = speech.build_speech(settings(), FakeStorage())
    assert built.fallback is not None and built.fallback_model == "gemini-3.5-flash-lite"
    assert speech.build_speech(settings(stt_fallback="off"), FakeStorage()).fallback is None


def test_the_fallback_follows_llm_thinking_level():
    # Task 6.1 follow-up: LLM_THINKING_LEVEL applies to every Gemini call, transcription
    # included; 'off' (None) is the kill switch that leaves the field out.
    assert speech.build_speech(settings(), FakeStorage()).thinking_level == "low"
    for level in ("minimal", None):
        built = speech.build_speech(settings(llm_thinking_level=level), FakeStorage())
        assert built.thinking_level == level
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=gemini_reply())

    client = GeminiClient(KEY, transport=httpx.MockTransport(handler))
    client.transcribe(MODEL, b"OggS-audio", "audio/ogg", timeout_s=5, thinking_level=None)
    assert "thinkingConfig" not in bodies[0]["generationConfig"]


def test_unusable_speech_settings_stop_make_handlers():
    with pytest.raises(ConfigurationError):
        make_handlers(settings(elevenlabs_api_key=None, meta_access_token="unused",
                               meta_api_version="v25.0"))


# --- through the worker -------------------------------------------------------------

class FakeScribe:
    provider, model = "elevenlabs", "scribe_v2"

    def __init__(self):
        self.script, self.calls = [], []

    def transcribe(self, data, mime_type, keyterms, *, timeout_s):
        self.calls.append(SimpleNamespace(data=data, mime=mime_type, keyterms=keyterms,
                                          timeout=timeout_s))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, dict):
            return ScribeResult(step["text"], step["language_code"],
                                step["language_probability"],
                                sum(w["type"] == "word" for w in step["words"]),
                                sum(w["type"] == "audio_event" for w in step["words"]))
        return step


class FakeGemini:
    def __init__(self):
        self.script, self.calls = [], []

    def transcribe(self, model, data, mime_type, *, timeout_s, thinking_level=None):
        self.calls.append(SimpleNamespace(model=model, data=data, timeout=timeout_s,
                                          thinking_level=thinking_level))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def sp(app_role_db):
    admin_url, app_url, _ = app_role_db
    with psycopg.connect(admin_url, autocommit=True) as admin:
        city = admin.execute(
            "INSERT INTO location (level, name) VALUES ('city', 'Karachi') RETURNING id"
        ).fetchone()[0]
        area = admin.execute(
            "INSERT INTO location (parent_id, parent_level, level, name) "
            "VALUES (%s, 'city', 'area', 'DHA Defence') RETURNING id", (city,)
        ).fetchone()[0]
        admin.execute("INSERT INTO location_alias (location_id, alias, kind) "
                      "VALUES (%s, 'Difence', 'voice_stt')", (area,))
    speech._keyterm_cache.update(expires=0.0, terms=None)
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, "Demo Realty (DEMO)", "demo", PNID)
    with tenant_transaction(conn, agency):
        conn.execute("INSERT INTO staff (agency_id, name, whatsapp_number, role) VALUES "
                     "(current_agency_id(), 'Demo agent', %s, 'agent')", ("+" + STAFF,))
    channel, storage = FakeChannel(), FakeStorage()
    scribe, gemini = FakeScribe(), FakeGemini()
    service = SpeechService(scribe, storage, fallback=gemini, fallback_model=MODEL)
    worker_settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket="demo-bucket",
        s3_region="us-east-1", s3_endpoint_url=None, media_public_base_url=None,
        site_address="demo.example",
    )

    def build(speech_service):
        handlers = make_handlers(
            worker_settings, client_factory=lambda pnid: channel, storage=storage,
            image_link=lambda: "https://demo.example/x.jpg", speech_service=speech_service,
        )
        return Worker(app_url, [agency], handlers, threading.Event(), backoff=lambda n: 0)

    state = SimpleNamespace(worker=build(service))
    ids = iter(range(1, 10_000))

    def deliver(*messages):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": PNID}, "messages": list(messages),
        }}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(conn, agency):
            for event in events:
                store_event(conn, agency, event)

    def voice(data, *, sender=CUSTOMER):
        media_id = f"{next(ids)}"
        channel.files[media_id] = data
        return {"from": sender, "id": f"wamid.V{next(ids)}", "timestamp": str(int(time.time())),
                "type": "audio", "audio": {"id": media_id, "mime_type": "audio/ogg; codecs=opus",
                                           "voice": True}}

    def text(body):
        return {"from": CUSTOMER, "id": f"wamid.T{next(ids)}",
                "timestamp": str(int(time.time())), "type": "text", "text": {"body": body}}

    def drain():
        while state.worker.run_once():
            pass

    def query(sql, params=()):
        with tenant_transaction(conn, agency):
            return conn.execute(sql, params).fetchall()

    def replies():
        return [payload["text"]["body"] for payload in channel.sent if payload["type"] == "text"]

    def stt():
        return query("SELECT transcript, stt_meta FROM message WHERE message_type = 'audio' "
                     "ORDER BY created_at")

    def usage():
        return query("SELECT provider, model, operation, outcome, outcome_detail, "
                     "audio_seconds::float, input_tokens, request_key FROM usage_event "
                     "ORDER BY created_at, request_key")

    def rebuild(speech_service):
        state.worker.close()
        state.worker = build(speech_service)

    yield SimpleNamespace(conn=conn, agency=agency, channel=channel, storage=storage,
                          scribe=scribe, gemini=gemini, deliver=deliver, voice=voice,
                          text=text, drain=drain, query=query, replies=replies, stt=stt,
                          usage=usage, rebuild=rebuild, app_url=app_url)
    state.worker.close()
    conn.close()


def stub_reply(count=1, voice="transcribed"):
    noun = "message" if count == 1 else "messages"
    return f"Customer mode (stub) · {count} {noun} in this turn · voice: {voice}"


def test_a_voice_note_from_a_customer_gets_a_stored_transcript(sp):
    """Done condition 1."""
    note = opus_note(6)
    sp.scribe.script.append(scribe_body())
    sp.deliver(sp.voice(note))
    sp.drain()

    [(transcript, meta)] = sp.stt()
    assert transcript == SPOKEN
    assert meta["status"] == "transcribed" and meta["provider"] == "elevenlabs"
    assert meta["model"] == "scribe_v2" and meta["duration_ms"] == 6_000
    assert meta["truncated"] is False and "activity_ratio" not in meta
    assert meta["primary"]["detail"] == "transcribed" and "fallback" not in meta
    assert sp.replies() == [stub_reply()]
    # The note was sent as stored, with the domain vocabulary and the gazetteer.
    [call] = sp.scribe.calls
    assert call.data == note and call.mime == "audio/ogg" and call.timeout == 6.0
    assert call.keyterms[0] == "DHA" and "DHA Defence" in call.keyterms
    assert "Difence" in call.keyterms and "Karachi" not in call.keyterms
    [row] = sp.usage()
    assert row[:7] == ("elevenlabs", "scribe_v2", "stt", "succeeded", "transcribed", 6.0,
                       None)
    assert row[7].endswith(":primary") and ":stt:" in row[7]
    assert sp.query("SELECT last_modality FROM conversation") == [("voice",)]
    # Downloaded in the turn; the fetch_audio job then found it stored and did nothing.
    assert sp.query("SELECT status FROM inbound_audio") == [("stored",)]
    assert len(sp.storage.puts) == 1
    assert sp.query("SELECT status FROM job WHERE job_type = %s",
                    (audio.JOB_TYPE,)) == [("succeeded",)]


def test_a_forced_primary_error_uses_the_fallback(sp):
    """Done condition 2, with the real clients on a fake transport (invalid key)."""
    seen = []

    def scribe_handler(request):
        seen.append("scribe")
        return httpx.Response(401, json={"detail": {"status": "invalid_api_key"}})

    def gemini_handler(request):
        seen.append("gemini")
        return httpx.Response(200, json=gemini_reply())

    service = SpeechService(
        ScribeClient("invalid", transport=httpx.MockTransport(scribe_handler)), sp.storage,
        fallback=GeminiClient(KEY, transport=httpx.MockTransport(gemini_handler)),
        fallback_model=MODEL)
    sp.rebuild(service)
    sp.deliver(sp.voice(opus_note(6)))
    sp.drain()

    assert seen == ["scribe", "gemini"]
    [(transcript, meta)] = sp.stt()
    assert transcript == SPOKEN
    assert meta["status"] == "transcribed" and meta["provider"] == "gemini"
    assert meta["model"] == "fallback-model-001"
    assert meta["primary"]["outcome"] == "failed" and meta["primary"]["detail"] == "http_401"
    assert meta["fallback_reason"] == "http_401"
    assert meta["fallback"]["detail"] == "transcribed"
    assert sp.replies() == [stub_reply()]
    assert [row[:7] for row in sp.usage()] == [
        ("elevenlabs", "scribe_v2", "stt", "failed", "http_401", 6.0, None),
        ("gemini", "fallback-model-001", "stt", "succeeded", "transcribed", 6.0, 400),
    ]


def test_a_silent_control_asks_to_resend_and_never_reaches_the_fallback(sp):
    """Done condition 3 (as seen live 2026-10-12: Scribe returns no words)."""
    sp.scribe.script.append({"text": "", "language_code": "eng",
                             "language_probability": 0.4, "words": []})
    sp.deliver(sp.voice(opus_note(5)))
    sp.drain()
    assert sp.replies() == [TEMPLATES["voice_retry"]]
    assert "dobara bhej" in TEMPLATES["voice_retry"].lower()
    [(transcript, meta)] = sp.stt()
    assert transcript is None and meta["status"] == "no_speech"
    assert meta["primary"]["detail"] == "no_speech" and "fallback" not in meta
    assert sp.gemini.calls == []
    assert [row[:5] for row in sp.usage()] == [
        ("elevenlabs", "scribe_v2", "stt", "succeeded", "no_speech")]


def test_event_tags_are_not_part_of_the_transcript(sp):
    sp.scribe.script.append(scribe_body(text=f"(background noise) {SPOKEN} (laughter)",
                                        events=2))
    sp.deliver(sp.voice(opus_note(8)))
    sp.drain()
    assert sp.stt()[0][0] == SPOKEN


def test_music_or_noise_is_no_speech_and_never_reaches_the_fallback(sp):
    sp.scribe.script.append(scribe_body(text="(music)", events=1))
    sp.deliver(sp.voice(opus_note(8)))
    sp.drain()
    assert sp.replies() == [TEMPLATES["voice_retry"]]
    assert sp.stt()[0][1]["status"] == "no_speech"
    assert sp.gemini.calls == []


def test_a_note_under_one_second_is_not_transcribed(sp):
    sp.deliver(sp.voice(opus_note(0.5)))
    sp.drain()
    assert sp.replies() == [TEMPLATES["voice_retry"]]
    assert sp.stt()[0][1]["status"] == "too_short" and sp.scribe.calls == []


def test_a_long_note_sends_only_the_first_two_minutes_and_says_so(sp):
    sp.scribe.script.append(scribe_body())
    sp.deliver(sp.voice(opus_note(150)))
    sp.drain()
    [call] = sp.scribe.calls
    assert audio.ogg_duration_ms(call.data) == 120_000
    [(transcript, meta)] = sp.stt()
    assert transcript == SPOKEN and meta["status"] == "truncated" and meta["truncated"]
    assert sp.replies() == [stub_reply(voice="truncated"), TEMPLATES["voice_truncated"]]
    assert sp.usage()[0][5] == 120.0


def test_garbage_goes_to_the_fallback_and_both_failing_asks_to_type(sp, caplog):
    sp.scribe.script.append(scribe_body(text="thank you " * 10))
    sp.gemini.script.append(ProviderError("http_503", failover=True))
    sp.deliver(sp.voice(opus_note(20)))
    with caplog.at_level(logging.INFO):
        sp.drain()
    assert sp.replies() == [TEMPLATES["voice_failed"]]
    [(transcript, meta)] = sp.stt()
    assert transcript is None and meta["status"] == "failed"
    assert meta["fallback_reason"] == "repetition"
    assert [call.thinking_level for call in sp.gemini.calls] == ["low"]
    assert meta["rejected_transcript"].startswith("thank you")
    assert [row[3:5] for row in sp.usage()] == [("succeeded", "repetition"),
                                                ("failed", "http_503")]
    assert any("ALERT speech failed" in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)
    assert "thank you" not in caplog.text


def test_fallback_no_speech_is_trusted(sp):
    sp.scribe.script.append(ProviderError("timeout", failover=True))
    sp.gemini.script.append(Transcription(False, "", None))
    sp.deliver(sp.voice(opus_note(4)))
    sp.drain()
    assert sp.stt()[0][1]["status"] == "no_speech"
    assert sp.replies() == [TEMPLATES["voice_retry"]]


def test_an_unsupported_language_asks_for_urdu_or_english(sp):
    sp.scribe.script.append(scribe_body(text="Je cherche une maison", language="fra",
                                        probability=0.97))
    sp.deliver(sp.voice(opus_note(4)))
    sp.drain()
    assert sp.replies() == [TEMPLATES["voice_language"]]
    assert sp.gemini.calls == []


def test_without_a_fallback_a_primary_error_asks_to_type(sp):
    sp.rebuild(SpeechService(sp.scribe, sp.storage))
    sp.scribe.script.append(ProviderError("http_429", failover=True))
    sp.deliver(sp.voice(opus_note(4)))
    sp.drain()
    assert sp.replies() == [TEMPLATES["voice_failed"]]
    assert sp.stt()[0][1]["primary"]["detail"] == "http_429"


def test_gone_audio_asks_to_resend(sp):
    sp.channel.media_errors.append(whatsapp.PermanentMediaError("http_404"))
    sp.deliver(sp.voice(opus_note(4)))
    sp.drain()
    assert sp.replies() == [TEMPLATES["voice_retry"]]
    assert sp.stt()[0][1]["status"] == "download_failed"
    assert sp.query("SELECT status, error_code FROM inbound_audio") == [
        ("rejected", "http_404")]


def test_text_and_voice_in_one_turn_share_the_reply(sp):
    sp.scribe.script.append(scribe_body())
    sp.deliver(sp.text("salaam"), sp.voice(opus_note(4)))
    sp.drain()
    assert sp.replies() == [stub_reply(count=2)]


def test_a_failed_note_next_to_text_still_answers_the_text(sp):
    sp.scribe.script.append(scribe_body(text="(static)", events=1))
    sp.deliver(sp.text("salaam"), sp.voice(opus_note(4)))
    sp.drain()
    assert sp.replies() == [stub_reply(count=2, voice="no_speech"),
                            TEMPLATES["voice_retry"]]


def test_a_resumed_turn_never_pays_for_a_note_twice(sp, monkeypatch):
    real = outbound.send_message
    failures = [ConnectionError("fictional outage")]

    def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)

    monkeypatch.setattr(outbound, "send_message", flaky)
    sp.scribe.script.append(scribe_body())
    sp.deliver(sp.voice(opus_note(4)))
    sp.drain()
    assert len(sp.scribe.calls) == 1
    assert sp.replies() == [stub_reply()]
    assert len(sp.usage()) == 1


def test_staff_and_gated_turns_are_not_transcribed(sp):
    sp.deliver(sp.voice(opus_note(4), sender=STAFF))
    sp.drain()
    assert sp.scribe.calls == [] and sp.stt() == [(None, None)]
    with tenant_transaction(sp.conn, sp.agency):
        sp.conn.execute("INSERT INTO conversation (agency_id, customer_wa_id, mode, "
                        "control_state) VALUES (current_agency_id(), %s, 'human', "
                        "'human_owned')", (CUSTOMER,))
    sp.deliver(sp.voice(opus_note(4)))
    sp.drain()
    assert sp.scribe.calls == []


def test_speech_off_keeps_the_6_1_behaviour(sp):
    sp.rebuild(None)
    sp.deliver(sp.voice(opus_note(4)))
    sp.drain()
    assert sp.replies() == ["Customer mode (stub) · 1 message in this turn"]
    assert sp.stt() == [(None, None)]


def test_the_turn_engine_reads_the_transcript_as_text(sp):
    from app.turn_engine import TurnEngine

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model",
                              "parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})

    engine = TurnEngine(GeminiClient(KEY, transport=httpx.MockTransport(handler)),
                        "primary-model", "failover-model")
    worker_settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket=None,
        site_address="demo.example")
    service = SpeechService(sp.scribe, sp.storage)
    handlers = make_handlers(worker_settings, client_factory=lambda pnid: sp.channel,
                             storage=sp.storage, engine=engine, speech_service=service)
    worker = Worker(sp.app_url, [sp.agency], handlers, threading.Event(),
                    backoff=lambda n: 0)
    try:
        sp.scribe.script.append(scribe_body())
        sp.deliver(sp.voice(opus_note(4)))
        while worker.run_once():
            pass
    finally:
        worker.close()
    [body] = requests
    current = body["contents"][0]["parts"][-1]["text"]
    assert f"1. {SPOKEN}" in current and "no transcript yet" not in current


def test_a_slow_engine_after_a_voice_note_sends_the_interim_once_and_continues(sp):
    # #29 + #30 combined: STT runs first, then the 6.1 engine; an exhausted 15 s budget
    # sends the interim (turn:{id}:interim) once and the one continuation answers.
    from app.turn_engine import TEMPLATES as ENGINE_TEMPLATES
    from app.turn_engine import TurnEngine

    clock = SimpleNamespace(now=1000.0)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            clock.now += 16
            raise httpx.ReadTimeout("deadline")
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model",
                              "parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})

    engine = TurnEngine(GeminiClient(KEY, transport=httpx.MockTransport(handler)),
                        "primary-model", "failover-model", clock=lambda: clock.now)
    worker_settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket=None,
        site_address="demo.example")
    handlers = make_handlers(worker_settings, client_factory=lambda pnid: sp.channel,
                             storage=sp.storage, engine=engine,
                             speech_service=SpeechService(sp.scribe, sp.storage))
    worker = Worker(sp.app_url, [sp.agency], handlers, threading.Event(),
                    backoff=lambda n: 0)
    try:
        sp.scribe.script.append(scribe_body())
        sp.deliver(sp.voice(opus_note(4)))
        while worker.run_once():
            pass
    finally:
        worker.close()
    assert len(calls) == 2
    assert sp.replies() == [ENGINE_TEMPLATES["interim"], ENGINE_TEMPLATES["customer_default"]]
    assert sp.query("SELECT count(*) FROM outbound_message "
                    "WHERE dedupe_key LIKE 'turn:%%:interim'") == [(1,)]
    [(transcript, meta)] = sp.stt()
    assert transcript == SPOKEN and meta["status"] == "transcribed"


def test_speech_logs_hold_no_transcript_or_phone(sp, caplog):
    sp.scribe.script.append(scribe_body())
    with caplog.at_level(logging.DEBUG):
        sp.deliver(sp.voice(opus_note(4)))
        sp.drain()
    assert SPOKEN not in caplog.text and "aath crore" not in caplog.text
    assert CUSTOMER not in caplog.text and KEY not in caplog.text
