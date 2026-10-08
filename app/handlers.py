"""Worker job handlers (Task 3.2): process_message (echo bot) and reconcile_status;
Task 4.1 adds fetch_media (app.media), enqueued here for staff photos.

Both are idempotent: the inbound message insert dedupes on its WAMID, replies
dedupe on "echo:{wamid}:{seq}" (app.outbound), and status updates only move
forward (sent < delivered < failed-unless-read < read; failed is terminal).
Never log payloads, message text or phone numbers.
"""

import logging
from contextlib import closing
from datetime import UTC, datetime

from app import echo, media, outbound, whatsapp
from app.db import tenant_transaction
from app.queue import PermanentJobError

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
DEMO_IMAGE_KEY = "demo/echo-test.jpg"
DEMO_IMAGE_LINK_SECONDS = 600
MEDIA_TYPES = ("image", "audio", "video", "document", "sticker")
DELIVERY_STATUSES = ("sent", "delivered", "read", "failed")


class StatusNotMatchedYet(Exception):
    """The status arrived before its send was recorded; retry shortly."""


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def _inbound_fields(message):
    """(message_type, text_body, media_id, mime_type) for the message log."""
    kind = message.get("type") or "unknown"
    content = message.get(kind) if isinstance(message.get(kind), dict) else {}
    text_body = None
    if kind == "text":
        text_body = content.get("body")
    elif kind == "interactive":
        reply = content.get(content.get("type")) or {}
        text_body = reply.get("title")
    elif kind == "button":
        text_body = content.get("text")
    elif kind in MEDIA_TYPES:
        text_body = content.get("caption")
    media_id = content.get("id") if kind in MEDIA_TYPES else None
    mime_type = content.get("mime_type") if kind in MEDIA_TYPES else None
    return kind, text_body, media_id, mime_type


def _timestamp(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_inbound(conn, job):
    """Store the conversation and inbound message; return (conversation_id, message, sender id)."""
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        row = conn.execute(
            """
            SELECT e.payload, a.meta_phone_number_id
            FROM inbound_event e JOIN agency a ON a.id = e.agency_id
            WHERE e.id = %s
            """,
            (job.inbound_event_id,),
        ).fetchone()
        if row is None:
            raise PermanentJobError("inbound_event_missing")
        payload, phone_number_id = row
        message = payload.get("message") or {}
        sender, wamid = message.get("from"), message.get("id")
        if not sender or not wamid or not phone_number_id:
            raise PermanentJobError("inbound_message_incomplete")
        sent_at = _timestamp(message.get("timestamp"))

        conversation_id = conn.execute(
            """
            INSERT INTO conversation (agency_id, customer_wa_id, last_customer_message_at)
            VALUES (current_agency_id(), %(sender)s,
                    COALESCE(to_timestamp(%(ts)s::bigint), now()))
            ON CONFLICT (agency_id, customer_wa_id) DO UPDATE SET
                last_customer_message_at = GREATEST(
                    conversation.last_customer_message_at,
                    EXCLUDED.last_customer_message_at
                ),
                updated_at = now()
            RETURNING id
            """,
            {"sender": sender, "ts": sent_at},
        ).fetchone()[0]

        message_type, text_body, media_id, mime_type = _inbound_fields(message)
        row = conn.execute(
            """
            INSERT INTO message (
                agency_id, conversation_id, inbound_event_id, provider_message_id,
                direction, message_type, text_body, media_id, provider_timestamp
            )
            VALUES (
                current_agency_id(), %s, %s, %s, 'inbound', %s, %s, %s,
                to_timestamp(%s::bigint)
            )
            ON CONFLICT (agency_id, provider_message_id) DO NOTHING
            RETURNING id
            """,
            (conversation_id, job.inbound_event_id, wamid, message_type, text_body,
             media_id, sent_at),
        ).fetchone()
        if row is None:  # a replayed job: the message is already logged
            row = conn.execute(
                "SELECT id FROM message WHERE provider_message_id = %s", (wamid,)
            ).fetchone()
        if media_id and media.is_stageable(message_type, mime_type):
            media.stage_inbound_media(
                conn, conversation_id, row[0], sender, media_id, job.inbound_event_id
            )
    return conversation_id, message, phone_number_id


def make_handlers(settings, *, client_factory=None, image_link=None, storage=None):
    """Build the job handlers. process_message is only registered when sending is
    configured, so its jobs wait (pending) instead of failing without a token;
    fetch_media also needs the bucket and its public URL."""

    handlers = {"reconcile_status": reconcile_status}
    if client_factory is None:
        if not settings.meta_access_token:
            logger.error("META_ACCESS_TOKEN is not set; process_message jobs stay pending")
            return handlers

        def client_factory(phone_number_id):
            return whatsapp.WhatsAppClient(
                phone_number_id, settings.meta_access_token, settings.meta_api_version
            )

    if image_link is None and settings.s3_bucket:
        def image_link():
            return presigned_demo_image(settings)

    privacy_url = f"https://{settings.site_address}/privacy" if settings.site_address else None

    def process(conn, job):
        process_message(
            conn, job, client_factory, privacy_url=privacy_url, image_link=image_link
        )

    handlers["process_message"] = process

    public_base = media.public_base_url(settings)
    if storage is None and settings.s3_bucket:
        storage = media.S3Storage(
            settings.s3_bucket, settings.s3_region, settings.s3_endpoint_url
        )
    if storage is None or public_base is None:
        logger.error("Media bucket or public URL not configured; fetch_media jobs stay pending")
        return handlers

    def fetch(conn, job):
        media.fetch_media(conn, job, client_factory, storage, public_base)

    handlers[media.JOB_TYPE] = fetch
    return handlers


def process_message(conn, job, client_factory, *, privacy_url=None, image_link=None):
    conversation_id, message, phone_number_id = record_inbound(conn, job)
    wamid, sender = message["id"], message["from"]
    with closing(client_factory(phone_number_id)) as client:
        try:
            client.mark_read(wamid)
        except whatsapp.SendError as exc:
            # Blue ticks are cosmetic; never block the reply on them.
            logger.warning("Mark-read failed: %s", exc.code)

        for seq, payload in enumerate(
            echo.reply(message, privacy_url=privacy_url, image_link=image_link)
        ):
            try:
                outbound.send_message(
                    conn, client, job.agency_id, conversation_id, sender, payload,
                    f"echo:{wamid}:{seq}",
                )
            except whatsapp.PermanentSendError as exc:
                raise PermanentJobError(exc.code) from None


def reconcile_status(conn, job):
    """Apply one statuses webhook to its outbound_message, forward-only."""
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        payload = conn.execute(
            "SELECT payload FROM inbound_event WHERE id = %s", (job.inbound_event_id,)
        ).fetchone()
        status = (payload[0].get("status") if payload else None) or {}
        state, wamid = status.get("status"), status.get("id")
        if state not in DELIVERY_STATUSES or not wamid:
            logger.info("Status webhook ignored: %s", state if isinstance(state, str) else "?")
            return
        errors = status.get("errors")
        error_code = None
        if state == "failed" and isinstance(errors, list) and errors:
            code = errors[0].get("code") if isinstance(errors[0], dict) else None
            error_code = f"meta_{code}" if code is not None else None

        updated = conn.execute(
            """
            UPDATE outbound_message SET
                delivery_status = %(state)s,
                delivered_at = CASE WHEN %(state)s IN ('delivered', 'read')
                    THEN COALESCE(delivered_at, %(at)s) ELSE delivered_at END,
                read_at = CASE WHEN %(state)s = 'read'
                    THEN COALESCE(read_at, %(at)s) ELSE read_at END,
                failed_at = CASE WHEN %(state)s = 'failed' THEN %(at)s ELSE failed_at END,
                delivery_error_code = CASE WHEN %(state)s = 'failed'
                    THEN %(error_code)s ELSE delivery_error_code END
            WHERE provider_message_id = %(wamid)s
              AND (
                  delivery_status IS NULL
                  OR (
                      delivery_status <> 'failed'
                      AND array_position(%(order)s, delivery_status)
                          < array_position(%(order)s, %(state)s)
                  )
              )
            RETURNING id
            """,
            {
                "state": state, "wamid": wamid, "error_code": error_code,
                "at": _status_time(status.get("timestamp")),
                # failed ranks above delivered but below read: a read message stays read.
                "order": ["sent", "delivered", "failed", "read"],
            },
        ).fetchone()
        if updated is not None:
            return
        known = conn.execute(
            "SELECT 1 FROM outbound_message WHERE provider_message_id = %s", (wamid,)
        ).fetchone()
    if known is not None:
        return  # an older status arriving late: nothing to change
    if job.attempts < job.max_attempts:
        raise StatusNotMatchedYet()
    # Not one of ours (e.g. a template sent from the Meta dashboard).
    logger.info("Status webhook for an unknown message; ignored")


def _status_time(value):
    ts = _timestamp(value)
    return datetime.fromtimestamp(ts, UTC) if ts is not None else datetime.now(UTC)


def presigned_demo_image(settings):
    """A 10-minute public link to the fictional demo image in the private bucket."""
    import boto3

    client = boto3.client(
        "s3", region_name=settings.s3_region, endpoint_url=settings.s3_endpoint_url
    )
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": DEMO_IMAGE_KEY},
        ExpiresIn=DEMO_IMAGE_LINK_SECONDS,
    )
