"""Inbound media (Task 4.1, component C5, D14).

A staff photo is staged in inbound_media by record_inbound (app.handlers), then
the fetch_media job runs at once, because the WhatsApp download URL lives for
5 minutes (the media ID for days, so retries query a fresh URL):

1. Look up and download the media (app.whatsapp), within MAX_DOWNLOAD_BYTES.
2. Check the MIME type against the file's magic bytes; hash the bytes.
3. Same hash already stored for the agency: reuse its objects (no upload).
4. Otherwise build three EXIF-free images with Pillow and upload them:
   - original: orientation applied, long edge <= 2,560 px (Red-Team R-36);
   - wa: JPEG, 8-bit RGB, long edge <= 1,600 px, < 5 MB (image messages);
   - card: JPEG cropped and centred to one ratio (carousel headers, public).
5. Record the keys and the public card URL.

Keys: tenant/<agency_id>/inbound/<inbound_media_id>/{original.jpg|png, wa.jpg}
and card/<agency_id>/<card_token>.jpg. Only card/ is publicly readable; the
token is random, so card URLs are unguessable. Objects are written once under
stable keys, so a retried job overwrites the same objects.

Never log image bytes, phone numbers or URLs.
"""

import hashlib
import io
import logging
import warnings
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from PIL import Image, ImageOps
from psycopg.pq import TransactionStatus

from app import whatsapp
from app.db import tenant_transaction
from app.queue import PermanentJobError, enqueue_job

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
JOB_TYPE = "fetch_media"

# WhatsApp images are <= 5 MB; photos sent as documents can be larger.
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
# Checked from the header before decoding: rejects decompression bombs.
MAX_PIXELS = 50_000_000

ORIGINAL_MAX_EDGE = 2560
ORIGINAL_JPEG_QUALITY = 92
WA_MAX_EDGE = 1600
WA_MAX_BYTES = 5 * 1024 * 1024
WA_JPEG_QUALITIES = (82, 72, 62, 50)
# One ratio for every carousel header (1.91:1). Rendering is checked on phones
# with the carousel (CLAUDE.md section 14, Day 5).
CARD_SIZE = (1200, 628)
CARD_JPEG_QUALITY = 80
CARD_CACHE_CONTROL = "public, max-age=31536000, immutable"

ACCEPTED_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
# Message types that can carry a listing photo (a document only with an image MIME).
STAGED_MESSAGE_TYPES = ("image", "document")


class MediaRejected(Exception):
    """The file is not an acceptable image; code is log-safe."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProcessedImage:
    mime_type: str
    width: int
    height: int
    original: bytes
    original_ext: str
    original_type: str
    wa: bytes
    card: bytes


def sniff_mime(data):
    """The image type from the magic bytes, or None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_stageable(message_type, mime_type):
    """True for a message that may carry a listing photo."""
    if message_type == "image":
        return True
    return message_type == "document" and mime_type in ACCEPTED_TYPES


def _encode(image, fmt, **options):
    out = io.BytesIO()
    # No exif/icc/xmp argument: Pillow writes no metadata it is not given.
    image.save(out, fmt, **options)
    return out.getvalue()


def _flatten(image):
    """8-bit RGB; transparency is composited on white."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _fit(image, max_edge):
    if max(image.size) <= max_edge:
        return image
    copy = image.copy()
    copy.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    return copy


def _wa_jpeg(image):
    """The WhatsApp derivative: under WA_MAX_BYTES, lowering quality, then size."""
    candidate = _fit(image, WA_MAX_EDGE)
    while True:
        for quality in WA_JPEG_QUALITIES:
            data = _encode(candidate, "JPEG", quality=quality, optimize=True)
            if len(data) <= WA_MAX_BYTES:
                return data
        candidate = _fit(candidate, int(max(candidate.size) * 0.75))


def has_metadata(data):
    """True if encoded image bytes still carry EXIF or XMP."""
    with Image.open(io.BytesIO(data)) as image:
        if image.getexif() or "exif" in image.info or "xmp" in image.info:
            return True
    return b"Exif\x00\x00" in data or b"http://ns.adobe.com/xap/" in data


def process_image(data, declared_mime=None):
    """Validate and transform downloaded bytes. Raises MediaRejected."""
    mime = sniff_mime(data)
    if mime is None:
        raise MediaRejected("unsupported_type")
    if declared_mime is not None and declared_mime.split(";")[0].strip().lower() != mime:
        raise MediaRejected("mime_mismatch")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            return _transform(data, mime)
    except MediaRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MediaRejected("too_many_pixels") from None
    except (OSError, ValueError, SyntaxError):
        raise MediaRejected("corrupt_image") from None


def _transform(data, mime):
    with Image.open(io.BytesIO(data)) as image:
        if image.format != ACCEPTED_TYPES[mime]:
            raise MediaRejected("mime_mismatch")
        if image.size[0] * image.size[1] > MAX_PIXELS:
            raise MediaRejected("too_many_pixels")
        image.seek(0)  # first frame of an animated file
        upright = ImageOps.exif_transpose(image)
        upright.load()

    width, height = upright.size
    if mime == "image/png":
        source = upright if upright.mode in ("RGB", "RGBA", "L", "LA") else upright.convert("RGBA")
        original = _encode(_fit(source, ORIGINAL_MAX_EDGE), "PNG", optimize=True)
        original_ext, original_type = "png", "image/png"
    else:
        original = _encode(
            _fit(_flatten(upright), ORIGINAL_MAX_EDGE), "JPEG",
            quality=ORIGINAL_JPEG_QUALITY, optimize=True,
        )
        original_ext, original_type = "jpg", "image/jpeg"

    rgb = _flatten(upright)
    wa = _wa_jpeg(rgb)
    card = _encode(
        ImageOps.fit(rgb, CARD_SIZE, Image.Resampling.LANCZOS, centering=(0.5, 0.5)),
        "JPEG", quality=CARD_JPEG_QUALITY, optimize=True, progressive=True,
    )
    for encoded in (original, wa, card):
        if has_metadata(encoded):
            raise MediaRejected("metadata_not_stripped")
    return ProcessedImage(
        mime_type=mime, width=width, height=height,
        original=original, original_ext=original_ext, original_type=original_type,
        wa=wa, card=card,
    )


def object_keys(agency_id, inbound_media_id, card_token, original_ext):
    base = f"tenant/{agency_id}/inbound/{inbound_media_id}"
    return {
        "original": f"{base}/original.{original_ext}",
        "wa": f"{base}/wa.jpg",
        "card": f"card/{agency_id}/{card_token}.jpg",
    }


class S3Storage:
    """The media bucket. On EC2 credentials come from the instance role."""

    def __init__(self, bucket, region=None, endpoint_url=None):
        import boto3

        self.bucket = bucket
        self._client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

    def put(self, key, data, content_type, cache_control=None):
        extra = {"CacheControl": cache_control} if cache_control else {}
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, **extra
        )

    def delete(self, key):
        self._client.delete_object(Bucket=self.bucket, Key=key)


def public_base_url(settings):
    """Base URL of the public card/ objects, or None if it cannot be known."""
    configured = getattr(settings, "media_public_base_url", None)
    if configured:
        return configured.rstrip("/")
    if settings.s3_bucket and settings.s3_region and not settings.s3_endpoint_url:
        return f"https://{settings.s3_bucket}.s3.{settings.s3_region}.amazonaws.com"
    return None


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def stage_inbound_media(conn, conversation_id, message_id, sender, wa_media_id,
                        inbound_event_id):
    """Inside the record_inbound tenant transaction: stage a photo from active
    inventory staff and enqueue its fetch. Returns the inbound_media id, or None
    when the sender is not admin / agent staff (role comes from the allowlist)."""
    staff = conn.execute(
        """
        SELECT id FROM staff
        WHERE is_active AND role IN ('admin', 'agent')
          AND regexp_replace(whatsapp_number, '[^0-9]', '', 'g') = %s
        """,
        (sender,),
    ).fetchone()
    if staff is None:
        return None
    row = conn.execute(
        """
        INSERT INTO inbound_media (agency_id, conversation_id, message_id, staff_id, wa_media_id)
        VALUES (current_agency_id(), %s, %s, %s, %s)
        ON CONFLICT (agency_id, message_id) DO NOTHING
        RETURNING id
        """,
        (conversation_id, message_id, staff[0], wa_media_id),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM inbound_media WHERE message_id = %s", (message_id,)
        ).fetchone()
    enqueue_job(
        conn, JOB_TYPE, f"media:{row[0]}", {"inbound_media_id": str(row[0])},
        inbound_event_id=inbound_event_id,
    )
    return row[0]


def _close(conn, agency_id, media_id, status, error_code):
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        conn.execute(
            """
            UPDATE inbound_media SET status = %s, error_code = %s, updated_at = now()
            WHERE id = %s AND status = 'pending'
            """,
            (status, error_code, media_id),
        )
    logger.warning("Inbound media %s %s: %s", media_id, status, error_code)


def _mark_duplicate(conn, agency_id, media_id, sha, size):
    """Point a pending row at the stored copy of the same bytes; False if none."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        row = conn.execute(
            """
            UPDATE inbound_media AS m SET
                status = 'duplicate', duplicate_of_id = s.id, content_sha256 = s.content_sha256,
                mime_type = s.mime_type, byte_size = %(size)s, width = s.width,
                height = s.height, original_key = s.original_key,
                wa_image_key = s.wa_image_key, card_image_key = s.card_image_key,
                public_url_card = s.public_url_card, exif_stripped = s.exif_stripped,
                stored_at = now(), updated_at = now()
            FROM inbound_media AS s
            WHERE m.id = %(id)s AND m.status = 'pending'
              AND s.status = 'stored' AND s.content_sha256 = %(sha)s AND s.id <> m.id
            RETURNING m.id
            """,
            {"id": media_id, "sha": sha, "size": size},
        ).fetchone()
    return row is not None


def fetch_media(conn, job, client_factory, storage, public_base):
    """The fetch_media job: idempotent; a finished row is never processed again."""
    media_id = job.payload.get("inbound_media_id")
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        row = conn.execute(
            """
            SELECT m.status, m.wa_media_id, m.card_token, a.meta_phone_number_id
            FROM inbound_media m JOIN agency a ON a.id = m.agency_id
            WHERE m.id = %s
            """,
            (media_id,),
        ).fetchone()
    if row is None:
        raise PermanentJobError("inbound_media_missing")
    status, wa_media_id, card_token, phone_number_id = row
    if status != "pending":
        return

    try:
        _fetch_and_store(conn, job, media_id, wa_media_id, card_token, phone_number_id,
                         client_factory, storage, public_base)
    except (whatsapp.PermanentMediaError, MediaRejected) as exc:
        _close(conn, job.agency_id, media_id, "rejected", exc.code)
    except Exception as exc:
        if job.attempts >= job.max_attempts and not conn.closed:
            # Lost for good; the staff flow asks for a resend (D14).
            if conn.info.transaction_status != TransactionStatus.IDLE:
                conn.rollback()
            code = exc.code if isinstance(exc, whatsapp.MediaError) else type(exc).__name__
            _close(conn, job.agency_id, media_id, "failed", code)
        raise


def _fetch_and_store(conn, job, media_id, wa_media_id, card_token, phone_number_id,
                     client_factory, storage, public_base):
    with closing(client_factory(phone_number_id)) as client:
        data, declared_mime = client.fetch_media(wa_media_id, MAX_DOWNLOAD_BYTES)
    sha = hashlib.sha256(data).hexdigest()
    if _mark_duplicate(conn, job.agency_id, media_id, sha, len(data)):
        return
    image = process_image(data, declared_mime)

    keys = object_keys(job.agency_id, media_id, card_token, image.original_ext)
    storage.put(keys["original"], image.original, image.original_type)
    storage.put(keys["wa"], image.wa, "image/jpeg")
    storage.put(keys["card"], image.card, "image/jpeg", CARD_CACHE_CONTROL)
    card_url = f"{public_base}/{keys['card']}"

    try:
        with tenant_transaction(conn, job.agency_id):
            _set_timeout(conn)
            conn.execute(
                """
                UPDATE inbound_media SET
                    status = 'stored', content_sha256 = %(sha)s, mime_type = %(mime)s,
                    byte_size = %(size)s, width = %(width)s, height = %(height)s,
                    original_key = %(original)s, wa_image_key = %(wa)s,
                    card_image_key = %(card)s, public_url_card = %(url)s,
                    exif_stripped = TRUE, stored_at = %(now)s, updated_at = %(now)s
                WHERE id = %(id)s AND status = 'pending'
                """,
                {
                    "sha": sha, "mime": image.mime_type, "size": len(data),
                    "width": image.width, "height": image.height,
                    "original": keys["original"], "wa": keys["wa"], "card": keys["card"],
                    "url": card_url, "now": datetime.now(UTC), "id": media_id,
                },
            )
    except psycopg.errors.UniqueViolation:
        # The same bytes were stored concurrently: keep that copy, drop ours.
        if not _mark_duplicate(conn, job.agency_id, media_id, sha, len(data)):
            raise
        for key in keys.values():
            try:
                storage.delete(key)
            except Exception:  # noqa: BLE001 - an orphan object is harmless
                logger.warning("Could not delete a superseded media object")
