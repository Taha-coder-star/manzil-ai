"""Task 5.3 acceptance tests: inbound audio (C5, RFC voice flow step 2, gate M1(c)).

Audio files are generated fictional Ogg containers (no speech); the WhatsApp
client and the bucket are the fakes of tests/test_media.py. Database tests use
the restricted manzil_app login (RLS on).
"""

import csv
import hashlib
import io
import struct
import threading
import time
from types import SimpleNamespace

import psycopg
import pytest

from app import audio, bench_manifest, webhook, whatsapp
from app.db import tenant_transaction
from app.handlers import make_handlers
from app.ingest import store_event
from app.provision_agency import create_agency
from app.worker import Worker
from tests.test_media import CUSTOMER, PNID, STAFF, FakeChannel, FakeStorage

BROKER = "923000000008"


def ogg_page(payload, granule, serial=1, seq=0):
    """One Ogg page (CRC not filled in: the reader does not check it)."""
    segments, rest = [], len(payload)
    while rest >= 255:
        segments.append(255)
        rest -= 255
    segments.append(rest)
    header = struct.pack("<4sBBqIIIB", b"OggS", 0, 0, granule, serial, seq, 0, len(segments))
    return header + bytes(segments) + payload


def ogg_opus(seconds, pre_skip=312):
    """A fictional Ogg Opus voice note of the given length (silent, no real frames)."""
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, pre_skip, 16_000, 0, 0)
    return (
        ogg_page(head, 0)
        + ogg_page(b"OpusTags" + b"\0" * 8, 0, seq=1)
        + ogg_page(b"\0" * 300, pre_skip + int(seconds * audio.OPUS_RATE) // 2, seq=2)
        + ogg_page(b"\0" * 300, pre_skip + int(seconds * audio.OPUS_RATE), seq=3)
    )


# --- format checks ----------------------------------------------------------------


@pytest.mark.parametrize(("data", "mime"), [
    (ogg_opus(1), "audio/ogg"),
    (b"#!AMR\n" + b"\0" * 16, "audio/amr"),
    (b"\0\0\0\x20ftypM4A " + b"\0" * 16, "audio/mp4"),
    (b"ID3\x04" + b"\0" * 16, "audio/mpeg"),
    (b"\xff\xfb\x90" + b"\0" * 16, "audio/mpeg"),
    (b"\xff\xf1\x50" + b"\0" * 16, "audio/aac"),
    (b"\xff\xd8\xff" + b"\0" * 16, None),  # a JPEG
    (b"%PDF-1.7", None),
    (b"", None),
])
def test_sniff_mime(data, mime):
    assert audio.sniff_mime(data) == mime


def test_ogg_duration_subtracts_pre_skip():
    assert audio.ogg_duration_ms(ogg_opus(12.5)) == 12_500
    assert audio.ogg_duration_ms(ogg_opus(0.4, pre_skip=0)) == 400


def test_ogg_duration_reads_vorbis_rate():
    head = b"\x01vorbis" + struct.pack("<IBI", 0, 1, 16_000) + b"\0" * 12
    data = ogg_page(head, 0) + ogg_page(b"\0" * 10, 16_000 * 3, seq=1)
    assert audio.ogg_duration_ms(data) == 3_000


@pytest.mark.parametrize("data", [
    ogg_opus(5)[:40],  # cut inside the first page
    ogg_opus(5)[:-10],  # cut inside the last page
    ogg_page(b"Unknown codec", 0) + ogg_page(b"\0", 100, seq=1),
    ogg_page(b"OpusHead" + b"\0" * 11, 0),  # header only: no audio page
    b"OggS" + b"\0" * 10,
])
def test_unreadable_ogg_has_no_duration(data):
    assert audio.ogg_duration_ms(data) is None


def test_object_key_is_private():
    assert audio.object_key("a1", "b2", "audio/ogg") == "tenant/a1/audio/b2.ogg"
    assert audio.object_key("a1", "b2", "audio/mp4") == "tenant/a1/audio/b2.m4a"


def test_only_audio_messages_are_staged():
    assert audio.is_stageable("audio")
    for kind in ("image", "document", "video", "sticker", "text"):
        assert not audio.is_stageable(kind)


# --- end to end through the worker -----------------------------------------------


@pytest.fixture
def ad(app_role_db):
    _, app_url, _ = app_role_db
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, "Demo Realty (DEMO)", "demo", PNID)
    with tenant_transaction(conn, agency):
        conn.execute(
            "INSERT INTO staff (agency_id, name, whatsapp_number, role) VALUES "
            "(current_agency_id(), 'Demo agent', %s, 'agent'), "
            "(current_agency_id(), 'Demo broker', %s, 'broker')",
            ("+" + STAFF, BROKER),
        )
    channel, storage = FakeChannel(), FakeStorage()
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket="demo-bucket",
        s3_region="us-east-1", s3_endpoint_url=None, media_public_base_url=None,
        site_address="demo.example",
    )
    handlers = make_handlers(
        settings, client_factory=lambda pnid: channel, storage=storage,
        image_link=lambda: "https://demo.example/x.jpg",
    )
    worker = Worker(app_url, [agency], handlers, threading.Event(), backoff=lambda n: 0)

    def deliver(*messages):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": PNID}, "messages": list(messages),
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

    yield SimpleNamespace(conn=conn, agency=agency, channel=channel, storage=storage,
                          deliver=deliver, drain=drain, query=query, worker=worker)
    worker.close()
    conn.close()


def voice_message(wamid, media_id, sender=CUSTOMER, voice=True):
    content = {"id": media_id, "mime_type": "audio/ogg; codecs=opus", "sha256": "ignored"}
    if voice is not None:
        content["voice"] = voice
    return {
        "from": sender, "id": wamid, "timestamp": str(int(time.time())), "type": "audio",
        "audio": content,
    }


def audio_rows(ad):
    return ad.query(
        "SELECT status, error_code, mime_type, is_voice, duration_ms, byte_size, "
        "storage_key, content_sha256, id::text FROM inbound_audio ORDER BY created_at"
    )


def audio_jobs(ad):
    return ad.query(
        "SELECT status, last_error_code FROM job WHERE job_type = %s ORDER BY created_at",
        (audio.JOB_TYPE,),
    )


@pytest.mark.parametrize("sender", [CUSTOMER, STAFF, BROKER])
def test_voice_note_from_any_sender_is_stored_privately(ad, sender):
    data = ogg_opus(7.25)
    ad.channel.files["555"] = data
    ad.deliver(voice_message("wamid.V1", "555", sender=sender))
    ad.drain()

    [row] = audio_rows(ad)
    status, error, mime, voice, duration, size, key, sha, item = row
    assert (status, error, mime, voice, duration, size) == (
        "stored", None, "audio/ogg", True, 7_250, len(data))
    assert key == f"tenant/{ad.agency}/audio/{item}.ogg"
    assert sha == hashlib.sha256(data).hexdigest()
    # Stored byte for byte, private (no public cache header), nothing else uploaded.
    assert ad.storage.objects == {key: (data, "audio/ogg", None)}
    assert audio_jobs(ad) == [("succeeded", None)]
    # The message is logged as a voice message and the turn still runs.
    assert ad.query(
        "SELECT message_type, modality, media_id FROM message WHERE direction = 'inbound'"
    ) == [("audio", "voice", "555")]
    assert ad.query("SELECT count(*) FROM conversation_turn") == [(1,)]


def test_audio_file_without_voice_flag(ad):
    ad.channel.files["556"] = b"ID3\x04" + b"\0" * 64
    ad.deliver(voice_message("wamid.V1", "556", voice=None))
    ad.drain()
    assert [r[:5] for r in audio_rows(ad)] == [("stored", None, "audio/mpeg", None, None)]
    assert audio_rows(ad)[0][6].endswith(".mp3")


def test_photos_and_texts_are_not_staged_as_audio(ad):
    ad.channel.files["111"] = b"\xff\xd8\xff" + b"\0" * 64
    ad.deliver(
        {"from": CUSTOMER, "id": "wamid.T1", "timestamp": str(int(time.time())),
         "type": "text", "text": {"body": "fictional"}},
        {"from": CUSTOMER, "id": "wamid.I1", "timestamp": str(int(time.time())),
         "type": "image", "image": {"id": "111", "mime_type": "image/jpeg"}},
    )
    ad.drain()
    assert audio_rows(ad) == [] and audio_jobs(ad) == []


def test_replayed_events_and_jobs_store_once(ad):
    ad.channel.files["555"] = ogg_opus(3)
    ad.deliver(voice_message("wamid.V1", "555"))
    ad.drain()
    ad.deliver(voice_message("wamid.V1", "555"))  # duplicate webhook
    with tenant_transaction(ad.conn, ad.agency):  # and a forced replay of every job
        ad.conn.execute("UPDATE job SET status = 'pending', attempts = 0")
    ad.drain()
    assert len(audio_rows(ad)) == 1 and len(audio_jobs(ad)) == 1
    assert len(ad.storage.puts) == 1


def test_non_audio_is_rejected_without_retry(ad):
    ad.channel.files["555"] = b"%PDF-1.7 fictional"
    ad.deliver(voice_message("wamid.V1", "555"))
    ad.drain()
    assert [r[:2] for r in audio_rows(ad)] == [("rejected", "unsupported_type")]
    assert audio_jobs(ad) == [("succeeded", None)] and ad.storage.puts == []


def test_gone_media_is_rejected(ad):
    ad.channel.media_errors = [whatsapp.PermanentMediaError("too_large")]
    ad.deliver(voice_message("wamid.V1", "555"))
    ad.drain()
    assert [r[:2] for r in audio_rows(ad)] == [("rejected", "too_large")]


def test_transient_errors_retry_then_store(ad):
    ad.channel.files["555"] = ogg_opus(2)
    ad.channel.media_errors = [whatsapp.RetryableMediaError("url_expired")]
    ad.storage.fail = 1
    ad.deliver(voice_message("wamid.V1", "555"))
    ad.drain()
    assert [r[0] for r in audio_rows(ad)] == ["stored"]
    assert audio_jobs(ad) == [("succeeded", None)]


def test_exhausted_retries_mark_the_audio_failed(ad):
    ad.channel.media_errors = [whatsapp.RetryableMediaError("download_http_503")] * 3
    ad.deliver(voice_message("wamid.V1", "555"))
    ad.drain()
    assert [r[:2] for r in audio_rows(ad)] == [("failed", "download_http_503")]
    assert audio_jobs(ad) == [("dead", "RetryableMediaError")]


def test_without_a_bucket_fetch_audio_is_not_registered():
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket=None,
        s3_region=None, s3_endpoint_url=None, media_public_base_url=None, site_address=None,
    )
    assert audio.JOB_TYPE not in make_handlers(settings, client_factory=lambda p: None)


def test_bucket_without_public_url_still_stores_audio():
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket=None,
        s3_region=None, s3_endpoint_url=None, media_public_base_url=None, site_address=None,
    )
    handlers = make_handlers(settings, client_factory=lambda p: None, storage=FakeStorage())
    assert audio.JOB_TYPE in handlers


# --- schema: tenancy, privileges, constraints -------------------------------------


def test_inbound_audio_is_tenant_isolated_and_append_by_status(ad, app_role_db):
    admin_url, _, schema = app_role_db
    ad.channel.files["555"] = ogg_opus(2)
    ad.deliver(voice_message("wamid.V1", "555"))
    ad.drain()

    other = create_agency(ad.conn, "Other Realty (DEMO)", "other", "100000000000002")
    with tenant_transaction(ad.conn, other):
        assert ad.conn.execute("SELECT count(*) FROM inbound_audio").fetchone()[0] == 0
    assert ad.conn.execute("SELECT count(*) FROM inbound_audio").fetchone()[0] == 0
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        tenant_transaction(ad.conn, ad.agency),
    ):
        ad.conn.execute("DELETE FROM inbound_audio")

    with psycopg.connect(admin_url) as admin:
        assert admin.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = 'inbound_audio'", (schema,)
        ).fetchone() == (True, True)
        privileges = {r[0] for r in admin.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'manzil_app' AND table_schema = %s "
            "AND table_name = 'inbound_audio'", (schema,)
        )}
        assert privileges == {"SELECT", "INSERT", "UPDATE"}


def test_cross_tenant_references_are_rejected(ad):
    ad.channel.files["555"] = ogg_opus(2)
    ad.deliver(voice_message("wamid.V1", "555"))
    ad.drain()
    [(message_id, conversation_id)] = ad.query(
        "SELECT message_id, conversation_id FROM inbound_audio"
    )
    other = create_agency(ad.conn, "Other Realty (DEMO)", "other", "100000000000002")
    with (
        pytest.raises(psycopg.errors.ForeignKeyViolation),
        tenant_transaction(ad.conn, other),
    ):
        ad.conn.execute(
            "INSERT INTO inbound_audio (agency_id, conversation_id, message_id, wa_media_id) "
            "VALUES (current_agency_id(), %s, %s, '1')",
            (conversation_id, message_id),
        )


@pytest.mark.parametrize(("update", "constraint"), [
    ("status = 'stored'", "inbound_audio_usable_check"),
    ("status = 'rejected'", "inbound_audio_error_check"),
    ("error_code = 'x'", "inbound_audio_error_check"),
    ("mime_type = 'audio/wav'", "inbound_audio_mime_type_check"),
    ("storage_key = 'card/x.ogg'", "inbound_audio_storage_key_check"),
    ("duration_ms = -1", "inbound_audio_duration_ms_check"),
    ("status = 'duplicate'", "inbound_audio_status_check"),
])
def test_inbound_audio_constraints(ad, update, constraint):
    ad.worker.handlers.pop(audio.JOB_TYPE)  # stage only; never fetch
    ad.deliver(voice_message("wamid.V1", "555"))
    ad.drain()
    assert [r[0] for r in audio_rows(ad)] == ["pending"]
    with (
        pytest.raises(psycopg.errors.CheckViolation) as caught,
        tenant_transaction(ad.conn, ad.agency),
    ):
        ad.conn.execute(f"UPDATE inbound_audio SET {update}")
    assert caught.value.diag.constraint_name == constraint


# --- benchmark manifest export (app.bench_manifest) ---------------------------------


def test_manifest_lists_stored_notes_without_phone_numbers(ad, app_role_db):
    _, app_url, _ = app_role_db
    ad.channel.files["555"] = ogg_opus(4)
    ad.channel.files["556"] = b"%PDF-1.7 fictional"
    ad.deliver(voice_message("wamid.V1", "555"), voice_message("wamid.V2", "556", sender=STAFF))
    ad.drain()

    out = io.StringIO()
    since = "2000-01-01T00:00:00+00:00"
    assert bench_manifest.main(["--agency-id", str(ad.agency), "--since", since],
                               {"DATABASE_URL": app_url}, out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0] == ",".join(bench_manifest.COLUMNS)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert sorted(r["status"] for r in rows) == ["rejected", "stored"]
    stored = next(r for r in rows if r["status"] == "stored")
    assert (stored["mime_type"], stored["duration_ms"], stored["is_voice"]) == (
        "audio/ogg", "4000", "true")
    assert stored["sha256"] == hashlib.sha256(ad.channel.files["555"]).hexdigest()
    assert stored["storage_key"].startswith(f"tenant/{ad.agency}/audio/")
    for secret in (CUSTOMER, STAFF, "wamid"):
        assert secret not in out.getvalue()

    out = io.StringIO()
    assert bench_manifest.main(["--agency-id", str(ad.agency), "--since", "2999-01-01T00:00+00:00"],
                               {"DATABASE_URL": app_url}, out) == 0
    assert out.getvalue().splitlines() == [",".join(bench_manifest.COLUMNS)]


@pytest.mark.parametrize("args", [
    ["--agency-id", "not-a-uuid", "--since", "2026-10-12T09:00+05:00"],
    ["--agency-id", "00000000-0000-0000-0000-000000000001", "--since", "2026-10-12T09:00"],
])
def test_manifest_rejects_bad_arguments(args):
    assert bench_manifest.main(args, {"DATABASE_URL": "postgresql://unused"}, io.StringIO()) == 1


def test_manifest_needs_a_database_url():
    args = ["--agency-id", "00000000-0000-0000-0000-000000000001", "--since",
            "2026-10-12T09:00+05:00"]
    assert bench_manifest.main(args, {}, io.StringIO()) == 1
