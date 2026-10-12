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
STT belong to C4 (app.speech, Task 6.3). The process_message job enqueues
fetch_audio while it records the message, so the turn usually runs first: speech
downloads in the turn through ensure_stored (Red-Team R-02), and the job then
finds the row stored and does nothing. Both paths write the same object and only
a 'pending' row is updated, so either order is safe.

Task 6.3 also adds pure helpers for C4: Opus packet sizes (activity_ratio, the
silence pre-check that needs no decoder) and truncate_ogg (the first 120 s).

Retention is short (docs/DECISIONS.md). Never log audio bytes, phone numbers or
URLs.
"""

import hashlib
import logging
import struct
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

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
OGG_EOS = 0x04
OGG_CRC_OFFSET = 22
# Silence pre-check (Task 6.3). An Opus packet of at most SILENT_PACKET_BYTES carries
# no speech (silence / comfort-noise frames are a few bytes; speech frames at
# WhatsApp's bitrates are tens of bytes). PROVISIONAL: calibrate on the silent and
# near_silent benchmark controls (docs/SPEECH.md); until then the value only catches
# clearly empty notes, and Scribe's "no words" result is the second guard.
SILENT_PACKET_BYTES = 10


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
        with closing(client_factory(phone_number_id)) as client:
            _download_and_store(conn, job.agency_id, audio_id, wa_media_id, client,
                                storage)
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


def _download_and_store(conn, agency_id, audio_id, wa_media_id, client, storage):
    """Download, check and store one audio; returns (bytes, mime, duration_ms)."""
    data, _ = client.fetch_media(wa_media_id, MAX_DOWNLOAD_BYTES)
    mime = sniff_mime(data)
    if mime is None:
        raise AudioRejected("unsupported_type")
    duration = ogg_duration_ms(data) if mime == "audio/ogg" else None
    key = object_key(agency_id, audio_id, mime)
    storage.put(key, data, mime)

    now = datetime.now(UTC)
    with tenant_transaction(conn, agency_id):
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
    return data, mime, duration


@dataclass(frozen=True)
class StoredAudio:
    id: UUID
    data: bytes
    mime_type: str
    duration_ms: int | None


def ensure_stored(conn, agency_id, message_id, client, storage):
    """Task 6.3, inside a turn: the message's audio, downloaded now if the fetch_audio
    job has not run yet. Returns a StoredAudio, or None when the audio is unusable
    (rejected, or failed for good). A transient download or storage error propagates,
    so the turn's job retries."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        row = conn.execute(
            """
            SELECT id, status, wa_media_id, mime_type, duration_ms, storage_key
            FROM inbound_audio WHERE message_id = %s
            """,
            (message_id,),
        ).fetchone()
    if row is None:
        return None
    audio_id, status, wa_media_id, mime, duration, key = row
    if status == "stored":
        return StoredAudio(audio_id, storage.get(key), mime, duration)
    if status != "pending":
        return None
    try:
        data, mime, duration = _download_and_store(conn, agency_id, audio_id, wa_media_id,
                                                   client, storage)
    except (whatsapp.PermanentMediaError, AudioRejected) as exc:
        _close(conn, agency_id, audio_id, "rejected", exc.code)
        return None
    return StoredAudio(audio_id, data, mime, duration)


# --- Ogg / Opus helpers for the speech pre-checks (Task 6.3) --------------------------

def _ogg_pages(data):
    """(offset, header fields, segment table, body offset, end) of each page of the
    first logical stream, or None when the container cannot be read."""
    pages, offset, serial = [], 0, None
    while offset + OGG_HEADER.size <= len(data):
        fields = OGG_HEADER.unpack_from(data, offset)
        capture, page_serial, segments = fields[0], fields[4], fields[7]
        if capture != b"OggS":
            return None
        table = offset + OGG_HEADER.size
        body = table + segments
        if body > len(data):
            return None
        end = body + sum(data[table:body])
        if end > len(data):
            return None
        if serial is None:
            serial = page_serial
        if page_serial == serial:
            pages.append((offset, fields, data[table:body], body, end))
        offset = end
    if offset != len(data) or not pages:
        return None
    return pages


def _is_opus(data, pages):
    first = pages[0]
    return data[first[3]:first[4]].startswith(b"OpusHead") and first[4] - first[3] >= 12


def opus_packet_sizes(data):
    """Sizes of the audio packets of an Ogg Opus stream (the OpusHead and OpusTags
    packets skipped), or None when it is not readable Ogg Opus."""
    pages = _ogg_pages(data)
    if pages is None or not _is_opus(data, pages):
        return None
    sizes, current = [], 0
    for _, _, table, _, _ in pages:
        for lacing in table:
            current += lacing
            if lacing < 255:
                sizes.append(current)
                current = 0
    return sizes[2:] if len(sizes) >= 2 else None


def activity_ratio(data):
    """Share of Opus packets larger than SILENT_PACKET_BYTES (a speech-energy proxy
    that needs no decoder), or None when it cannot be measured."""
    sizes = opus_packet_sizes(data)
    if not sizes:
        return None
    return sum(1 for size in sizes if size > SILENT_PACKET_BYTES) / len(sizes)


def _crc_table():
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
        table.append(crc & 0xFFFFFFFF)
    return table


_CRC_TABLE = _crc_table()


def ogg_crc(page):
    """The Ogg page checksum (CRC-32, polynomial 0x04C11DB7, no reflection), computed
    over the page with its checksum field zeroed."""
    crc = 0
    for byte in page:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def truncate_ogg(data, max_ms):
    """The first max_ms of an Ogg Opus stream, cut at a page boundary (never longer
    than max_ms), with the end-of-stream flag and checksum set on the new last page.
    None when the stream cannot be read or has no audio page within max_ms."""
    pages = _ogg_pages(data)
    if pages is None or not _is_opus(data, pages):
        return None
    pre_skip = struct.unpack_from("<H", data, pages[0][3] + 10)[0]
    limit = pre_skip + max_ms * OPUS_RATE // 1000
    kept = []
    for page in pages:
        if page[1][3] > limit:
            break
        kept.append(page)
    # Trailing pages on which no packet ends (granule -1) would leave a packet cut.
    while kept and kept[-1][1][3] == -1:
        kept.pop()
    if not kept or kept[-1][1][3] <= 0:  # header pages only: no audio to keep
        return None
    out = bytearray(b"".join(data[offset:end] for offset, _, _, _, end in kept))
    last = len(out) - (kept[-1][4] - kept[-1][0])
    out[last + 5] |= OGG_EOS
    struct.pack_into("<I", out, last + OGG_CRC_OFFSET, 0)
    struct.pack_into("<I", out, last + OGG_CRC_OFFSET, ogg_crc(out[last:]))
    return bytes(out)
