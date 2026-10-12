"""Worker job handlers (Task 3.2): process_message and reconcile_status; Task 4.1
adds fetch_media (app.media), enqueued here for staff photos; Task 4.2 adds
media_link_fallback, enqueued by reconcile_status for a failed image by ID;
Task 5.2 adds carousel_fallback (app.cards), enqueued by reconcile_status for an
accepted carousel that failed before delivery. Task 5.3 adds fetch_audio
(app.audio), enqueued here for every inbound audio message.
Task 5.1: process_message runs one coalesced turn (app.orchestrator) instead of
the echo bot. Task 5.2: staff "#demo carousel | cards | card | fallback …"
(app.cards), and inbound messages keep the WAMID they quote (context_wamid).

All are idempotent: the inbound message insert dedupes on its WAMID, replies
dedupe on "turn:{turn_id}:{seq}" (app.outbound), and status updates only move
forward (sent < delivered < failed-unless-read < read; failed is terminal).
Never log payloads, message text or phone numbers.
"""

import logging
from contextlib import closing
from datetime import UTC, datetime

from app import audio, cards, echo, media, orchestrator, outbound, turn_engine, whatsapp
from app.config import check_ai_settings
from app.db import tenant_transaction
from app.queue import TURN_FAILED_NOTICE_JOB, PermanentJobError

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


def _context_wamid(message):
    """The WAMID this message quotes or answers (a swipe-reply or a button tap)."""
    context = message.get("context")
    quoted = context.get("id") if isinstance(context, dict) else None
    return quoted if isinstance(quoted, str) and quoted.strip() else None


def _timestamp(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_inbound(conn, job):
    """Store the conversation and inbound message of a process_message job; return
    (conversation_id, message, business phone_number_id)."""
    return record_event(conn, job.agency_id, job.inbound_event_id)


def record_event(conn, agency_id, inbound_event_id):
    """record_inbound for any stored message event (coalesced sibling jobs too)."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        row = conn.execute(
            """
            SELECT e.payload, a.meta_phone_number_id
            FROM inbound_event e JOIN agency a ON a.id = e.agency_id
            WHERE e.id = %s
            """,
            (inbound_event_id,),
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
                direction, message_type, text_body, media_id, provider_timestamp,
                context_wamid
            )
            VALUES (
                current_agency_id(), %s, %s, %s, 'inbound', %s, %s, %s,
                to_timestamp(%s::bigint), %s
            )
            ON CONFLICT (agency_id, provider_message_id) DO NOTHING
            RETURNING id
            """,
            (conversation_id, inbound_event_id, wamid, message_type, text_body,
             media_id, sent_at, _context_wamid(message)),
        ).fetchone()
        if row is None:  # a replayed job: the message is already logged
            row = conn.execute(
                "SELECT id FROM message WHERE provider_message_id = %s", (wamid,)
            ).fetchone()
        if media_id and media.is_stageable(message_type, mime_type):
            media.stage_inbound_media(
                conn, conversation_id, row[0], sender, media_id, inbound_event_id
            )
        if media_id and audio.is_stageable(message_type):
            audio.stage_inbound_audio(
                conn, conversation_id, row[0], media_id, message["audio"].get("voice"),
                inbound_event_id,
            )
    return conversation_id, message, phone_number_id


def make_handlers(settings, *, client_factory=None, image_link=None, storage=None,
                  engine=None):
    """Build the job handlers. process_message is only registered when sending is
    configured, so its jobs wait (pending) instead of failing without a token;
    fetch_media also needs the bucket and its public URL; fetch_audio the bucket.
    Task 6.1: AI_PROVIDER=gemini replies through the AI turn engine (engine is for
    tests); unusable AI settings raise app.config.ConfigurationError, never a
    silent fallback to the stubs."""

    check_ai_settings(settings)
    if engine is None:
        engine = turn_engine.build_engine(settings)
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
    if storage is None and settings.s3_bucket:
        storage = media.S3Storage(
            settings.s3_bucket, settings.s3_region, settings.s3_endpoint_url
        )

    def process(conn, job):
        process_message(
            conn, job, client_factory, privacy_url=privacy_url, image_link=image_link,
            storage=storage, engine=engine,
        )

    handlers["process_message"] = process

    def failed_turn_notice(conn, job):
        orchestrator.notify_failed_job(conn, job, client_factory)

    handlers[TURN_FAILED_NOTICE_JOB] = failed_turn_notice

    def carousel_fallback(conn, job):
        cards.carousel_fallback(conn, job, client_factory)

    handlers[cards.FALLBACK_JOB_TYPE] = carousel_fallback

    if storage is not None:
        def fetch_voice(conn, job):
            audio.fetch_audio(conn, job, client_factory, storage)

        handlers[audio.JOB_TYPE] = fetch_voice

    public_base = media.public_base_url(settings)
    if storage is None or public_base is None:
        logger.error("Media bucket or public URL not configured; media jobs stay pending")
        return handlers

    def fetch(conn, job):
        media.fetch_media(conn, job, client_factory, storage, public_base)

    def fallback(conn, job):
        media.link_fallback(conn, job, client_factory, storage)

    handlers[media.JOB_TYPE] = fetch
    handlers[media.FALLBACK_JOB_TYPE] = fallback
    return handlers


def process_message(conn, job, client_factory, *, privacy_url=None, image_link=None,
                    storage=None, engine=None):
    """One coalesced turn (app.orchestrator): record this message and every pending
    sibling of the conversation, then answer them together."""
    _, message, phone_number_id = record_inbound(conn, job)
    for event_id in orchestrator.pending_sibling_events(conn, job):
        try:
            record_event(conn, job.agency_id, event_id)
        except PermanentJobError:
            pass  # left pending: that job dead-letters itself when it runs
    sender = message["from"]

    with closing(client_factory(phone_number_id)) as client:
        def demo(turn, command, dedupe_prefix):
            photo = echo.photo_command(command)
            if photo is not None:
                _demo_photo(conn, client, storage, turn, dedupe_prefix, photo)
                return
            listing_command = echo.cards_command(command)
            if listing_command is not None:
                _demo_cards(conn, client, turn, dedupe_prefix, *listing_command)
                return
            for seq, payload in enumerate(
                echo.reply(command, privacy_url=privacy_url, image_link=image_link)
            ):
                outbound.send_message(
                    conn, client, turn.agency_id, turn.conversation_id, turn.sender_wa_id,
                    payload, f"{dedupe_prefix}:{seq}",
                )

        orchestrator.run_turns(conn, job, client, sender, demo=demo, engine=engine)


def _demo_photo(conn, client, storage, turn, dedupe_prefix, variant):
    """Staff '#demo photo' check for Task 4.2: send the sender's newest stored photo
    back (cached ID on repeat; "badid" forces a refused ID, so the link fallback runs).
    Send errors propagate to app.orchestrator."""
    agency_id, sender, key = turn.agency_id, turn.sender_wa_id, f"{dedupe_prefix}:0"
    reply = None
    if storage is None:
        reply = "Photo demo is not configured (no bucket)."
    else:
        staff_id, photo_id = media.latest_staff_photo(conn, agency_id, sender)
        if staff_id is None:
            reply = "Photo demo is for staff numbers only."
        elif photo_id is None:
            reply = "No stored photo yet: send a photo first."
    if reply is not None:
        outbound.send_message(
            conn, client, agency_id, turn.conversation_id, sender, whatsapp.text(reply), key,
        )
        return
    if variant == "badid":
        media.force_invalid_cached_id(conn, agency_id, photo_id)
    result = media.send_image(
        conn, client, storage, agency_id, turn.conversation_id, sender,
        ("inbound_media", photo_id), key,
        caption=f"Your latest photo (DEMO) {variant or ''}".strip(),
    )
    logger.info("Demo photo sent via %s: %s", result.via, result.status)


def _demo_cards(conn, client, turn, dedupe_prefix, kind, option):
    """Staff Task 5.2 device checks. Recipient and conversation are always the sender's
    own (turn.sender_wa_id, turn.conversation_id); the listings are the fixed
    echo.DEMO_LISTINGS; the flag changed is the sender's own conversation's.
    "carousel" honours the flag, "cards" always sends individual cards and "card"
    sends the single-result card. Send errors propagate to app.orchestrator."""
    target = (conn, client, turn.agency_id, turn.conversation_id, turn.sender_wa_id)
    if kind == "fallback":
        if option != "status":
            cards.set_fallback(conn, turn.agency_id, turn.conversation_id, option == "on",
                               turn.staff_id)
        enabled, set_at = cards.fallback_status(conn, turn.agency_id, turn.conversation_id)
        reply = (
            "Carousel fallback is ON for this number: results come as individual cards."
            if enabled else
            "Carousel fallback is OFF for this number: results come as a carousel."
        )
        if set_at is not None:
            reply += f" (changed {set_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC)"
        outbound.send_message(*target, whatsapp.text(reply), f"{dedupe_prefix}:0")
        logger.info("Demo fallback %s: %s", option, "on" if enabled else "off")
        return

    def build():
        codes = echo.DEMO_LISTINGS[:1] if kind == "card" else echo.DEMO_LISTINGS
        listings = cards.load_listings(conn, turn.agency_id, codes)
        found = {listing.code for listing in listings}
        missing = [code for code in codes if code not in found]
        note = (
            f"DEMO: not a public listing of this agency: {', '.join(missing)}."
            if missing else None
        )
        if not listings:
            return [whatsapp.text(note)]
        return cards.plan_results(conn, turn.agency_id, turn.conversation_id, listings,
                                  note=note, force_fallback=kind == "cards")

    # Fixed on the turn before the first send, so a retry cannot change format or order.
    payloads = cards.fixed_plan(conn, turn.agency_id, turn.id, build)
    status = cards.send_plan(*target, payloads, dedupe_prefix)
    logger.info("Demo %s: %d messages planned, %s", kind, len(payloads), status)


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
            RETURNING id, media_source, media_source_id, payload, recipient_wa_id,
                delivered_at IS NOT NULL
            """,
            {
                "state": state, "wamid": wamid, "error_code": error_code,
                "at": _status_time(status.get("timestamp")),
                # failed ranks above delivered but below read: a read message stays read.
                "order": ["sent", "delivered", "failed", "read"],
            },
        ).fetchone()
        if updated is not None:
            if state == "failed":
                outbound_id, table, source_id, payload, recipient, delivered = updated
                media.on_failed_status(conn, outbound_id, table, source_id, payload)
                cards.on_failed_status(conn, outbound_id, recipient, payload,
                                       delivered=delivered)
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
