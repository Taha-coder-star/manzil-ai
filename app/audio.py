"""Inbound audio (Task 5.3; component C5, RFC voice flow step 2; gate M1(c)).

Every inbound audio message is staged in inbound_audio by record_inbound
(app.handlers), whoever sent it: customers' voice notes feed speech (C4, Task
6.3), and the R16 benchmark recordings come in through this same path. The
fetch_audio job runs at once, because the WhatsApp download URL lives for 5
minutes (the media ID for days, so retries query a fresh URL):

1. Look up and download the media (app.whatsapp), within MAX_DOWNLOAD_BYTES.
2. Find the type from the magic bytes (the declared MIME of audio varies by
   client, so it is not trusted); anything else is rejected.
3. Read the duration from an Ogg container (WhatsApp voice notes are Ogg Opus).
4. Store the bytes unchanged under tenant/<agency_id>/audio/<id>.<ext>, a
   private key: audio is never public and never under card/. A retried job
   overwrites the same object.

No transcoding and no pre-checks here: the < 1 s / > 120 s rules, silence and
STT belong to C4. Retention is short (docs/DECISIONS.md). Never log audio
bytes, phone numbers or URLs.
"""

import hashlib
import logging
import struct
from contextlib import closing
from datetime import UTC, datetime

from psycopg.pq import TransactionStatus

from app import whatsapp
from app.db import tenant_transaction
from app.queue import PermanentJobError, enqueue_job

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
JOB_TYPE = "fetch_audio"
# WhatsApp audio is <= 16 MB.
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
STAGED_MESSAGE_TYPES = ("audio",)
EXTENSIONS = {
    "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac",
    "audio/amr": "amr",
}
# Opus always counts granules at 48 kHz (RFC 7845).
OPUS_RATE = 48_000
OGG_HEADER = struct.Struct("<4sBBqIIIB")


class AudioRejected(Exception):
    """The file is not acceptable audio; code is log-safe."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def is_stageable(message_type):
    return message_type in STAGED_MESSAGE_TYPES


def sniff_mime(data):
    """The audio type from the magic bytes, or None."""
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"#!AMR"):
        return "audio/amr"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    if data.startswith(b"ID3"):
        return "audio/mpeg"
    if len(data) >= 2 and data[0] == 0xFF:
        if data[1] & 0xF6 == 0xF0:  # ADTS sync word, layer 0
            return "audio/aac"
        if data[1] & 0xE0 == 0xE0:  # MPEG audio frame sync
            return "audio/mpeg"
    return None


def ogg_duration_ms(data):
    """Duration of the first logical stream of an Ogg Opus / Vorbis file, or None
    when the container cannot be read."""
    offset, serial, rate, pre_skip, last = 0, None, None, 0, None
    while offset + OGG_HEADER.size <= len(data):
        capture, _, _, granule, page_serial, _, _, segments = OGG_HEADER.unpack_from(
            data, offset)
        if capture != b"OggS":
            return None
        table = offset + OGG_HEADER.size
        if table + segments > len(data):
            return None
        body = table + segments
        size = sum(data[table:body])
        if body + size > len(data):
            return None
        if serial is None:
            serial = page_serial
            payload = data[body:body + size]
            if payload.startswith(b"OpusHead") and len(payload) >= 12:
                rate, pre_skip = OPUS_RATE, struct.unpack_from("<H", payload, 10)[0]
            elif payload.startswith(b"\x01vorbis") and len(payload) >= 16:
                rate = struct.unpack_from("<I", payload, 12)[0]
            else:
                return None
        elif page_serial == serial and granule >= 0:
            last = granule
        offset = body + size
    if not rate or last is None:
        return None
    return max(last - pre_skip, 0) * 1000 // rate


def object_key(agency_id, inbound_audio_id, mime_type):
    return f"tenant/{agency_id}/audio/{inbound_audio_id}.{EXTENSIONS[mime_type]}"


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def stage_inbound_audio(conn, conversation_id, message_id, wa_media_id, is_voice,
                        inbound_event_id):
    """Inside the record_inbound tenant transaction: stage an audio message from any
    sender and enqueue its fetch. Returns the inbound_audio id."""
    row = conn.execute(
        """
        INSERT INTO inbound_audio (agency_id, conversation_id, message_id, wa_media_id,
                                   is_voice)
        VALUES (current_agency_id(), %s, %s, %s, %s)
        ON CONFLICT (agency_id, message_id) DO NOTHING
        RETURNING id
        """,
        (conversation_id, message_id, wa_media_id,
         is_voice if isinstance(is_voice, bool) else None),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM inbound_audio WHERE message_id = %s", (message_id,)
        ).fetchone()
    enqueue_job(
        conn, JOB_TYPE, f"audio:{row[0]}", {"inbound_audio_id": str(row[0])},
        inbound_event_id=inbound_event_id,
    )
    return row[0]


def _close(conn, agency_id, audio_id, status, error_code):
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        conn.execute(
            """
            UPDATE inbound_audio SET status = %s, error_code = %s, updated_at = now()
            WHERE id = %s AND status = 'pending'
            """,
            (status, error_code, audio_id),
        )
    logger.warning("Inbound audio %s %s: %s", audio_id, status, error_code)


def fetch_audio(conn, job, client_factory, storage):
    """The fetch_audio job: idempotent; a finished row is never processed again."""
    audio_id = job.payload.get("inbound_audio_id")
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        row = conn.execute(
            """
            SELECT m.status, m.wa_media_id, a.meta_phone_number_id
            FROM inbound_audio m JOIN agency a ON a.id = m.agency_id
            WHERE m.id = %s
            """,
            (audio_id,),
        ).fetchone()
    if row is None:
        raise PermanentJobError("inbound_audio_missing")
    status, wa_media_id, phone_number_id = row
    if status != "pending":
        return

    try:
        _fetch_and_store(conn, job, audio_id, wa_media_id, phone_number_id,
                         client_factory, storage)
    except (whatsapp.PermanentMediaError, AudioRejected) as exc:
        _close(conn, job.agency_id, audio_id, "rejected", exc.code)
    except Exception as exc:
        if job.attempts >= job.max_attempts and not conn.closed:
            # Lost for good; speech asks the sender to resend (C4).
            if conn.info.transaction_status != TransactionStatus.IDLE:
                conn.rollback()
            code = exc.code if isinstance(exc, whatsapp.MediaError) else type(exc).__name__
            _close(conn, job.agency_id, audio_id, "failed", code)
        raise


def _fetch_and_store(conn, job, audio_id, wa_media_id, phone_number_id, client_factory,
                     storage):
    with closing(client_factory(phone_number_id)) as client:
        data, _ = client.fetch_media(wa_media_id, MAX_DOWNLOAD_BYTES)
    mime = sniff_mime(data)
    if mime is None:
        raise AudioRejected("unsupported_type")
    duration = ogg_duration_ms(data) if mime == "audio/ogg" else None
    key = object_key(job.agency_id, audio_id, mime)
    storage.put(key, data, mime)

    now = datetime.now(UTC)
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        conn.execute(
            """
            UPDATE inbound_audio SET
                status = 'stored', mime_type = %(mime)s, content_sha256 = %(sha)s,
                byte_size = %(size)s, duration_ms = %(duration)s, storage_key = %(key)s,
                stored_at = %(now)s, updated_at = %(now)s
            WHERE id = %(id)s AND status = 'pending'
            """,
            {
                "mime": mime, "sha": hashlib.sha256(data).hexdigest(), "size": len(data),
                "duration": duration, "key": key, "now": now, "id": audio_id,
            },
        )
