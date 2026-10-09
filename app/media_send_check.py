"""Task 4.4 operator check: send one loaded demo listing image through Task 4.2.

    python -m app.media_send_check --agency-id <uuid> --listing DH6-101 [--order 0]
    python -m app.media_send_check --agency-id <uuid> --listing DH6-101 --send --check-id pilot-1

The default is a preview: it reads the property_media row and reports what a send
would do; it never calls Meta. --send uploads / reuses the WhatsApp media ID and
sends through app.media.send_image (outbound dedupe, control gate, 24 h window,
link fallback). The dedupe key is media-check:<check-id>:<media id>, so rerunning
the same check never sends twice.

Environment: DATABASE_URL (manzil_app). For --send also META_ACCESS_TOKEN,
optional META_API_VERSION, S3_BUCKET, S3_REGION, optional S3_ENDPOINT_URL, and
MEDIA_CHECK_RECIPIENT: the recipient's WhatsApp number, supplied privately and
never printed. The recipient must already have a conversation with the agency
(message the business number first, inside 24 hours). Not a chat command.
"""

import argparse
import os
import re
import sys
import uuid
from contextlib import closing
from dataclasses import dataclass

import psycopg

from app import media, whatsapp
from app.db import tenant_transaction
from app.media_seed import MediaSeedError, _connect, _db_error, parse_agency_id
from app.seed import normalize_phone

RECIPIENT_ENV = "MEDIA_CHECK_RECIPIENT"
CHECK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@dataclass
class CheckResult:
    listing: str
    order: int
    label: str | None
    media_id: object
    cached_id: bool
    card_url: bool
    recipient_set: bool = False
    conversation: bool | None = None
    window_open: bool | None = None
    sent_status: str | None = None
    sent_via: str | None = None
    cached_after: bool | None = None


def _media_row(conn, agency_id, listing, order):
    with tenant_transaction(conn, agency_id):
        conn.execute("SET LOCAL statement_timeout = '5s'")
        row = conn.execute(
            """
            SELECT m.id, m.label, m.wa_media_id IS NOT NULL
                   AND m.wa_media_expires_at > now(), m.public_url_card IS NOT NULL
            FROM property p JOIN property_media m ON m.property_id = p.id
            WHERE p.listing_code = %s AND m.sort_order = %s
            """,
            (listing, order),
        ).fetchone()
    if row is None:
        raise MediaSeedError(f"no media at order {order} for listing {listing}")
    return row


def _conversation(conn, agency_id, recipient):
    with tenant_transaction(conn, agency_id):
        conn.execute("SET LOCAL statement_timeout = '5s'")
        return conn.execute(
            """
            SELECT id, last_customer_message_at > now() - make_interval(hours => 24)
            FROM conversation WHERE customer_wa_id = %s
            """,
            (recipient,),
        ).fetchone()


def run(agency_id, app_url, environ, listing, order=0, *, send=False, check_id=None,
        client_factory=None, storage=None):
    agency_id = parse_agency_id(agency_id)
    if send and (check_id is None or not CHECK_ID.match(check_id)):
        raise MediaSeedError("--send needs --check-id (lowercase letters, digits, '-')")
    raw = (environ.get(RECIPIENT_ENV) or "").strip()
    recipient = normalize_phone(raw) if raw else None
    if raw and recipient is None:
        raise MediaSeedError(f"{RECIPIENT_ENV} is not a WhatsApp number with country code")
    if send and recipient is None:
        raise MediaSeedError(f"{RECIPIENT_ENV} is not set")
    conn = _connect(app_url)
    try:
        try:
            media_id, label, cached, card = _media_row(conn, agency_id, listing, order)
            result = CheckResult(listing, order, label, media_id, cached, card,
                                 recipient_set=recipient is not None)
            conversation = _conversation(conn, agency_id, recipient) if recipient else None
        except psycopg.Error as exc:
            raise _db_error("send check", exc) from None
        if recipient:
            result.conversation = conversation is not None
            result.window_open = bool(conversation and conversation[1])
        if not send:
            return result
        if conversation is None:
            raise MediaSeedError("the recipient has no conversation with this agency; message "
                                 "the business number from it first")
        if client_factory is None:
            token = (environ.get("META_ACCESS_TOKEN") or "").strip()
            if not token:
                raise MediaSeedError("META_ACCESS_TOKEN is not set")
            version = (environ.get("META_API_VERSION") or "").strip() or "v25.0"

            def client_factory(phone_number_id):
                return whatsapp.WhatsAppClient(phone_number_id, token, version)
        if storage is None:
            from app.media_seed import storage_from_env
            storage = storage_from_env(environ)
        try:
            with tenant_transaction(conn, agency_id):
                phone_number_id = conn.execute(
                    "SELECT meta_phone_number_id FROM agency WHERE id = %s", (agency_id,)
                ).fetchone()[0]
            if not phone_number_id:
                raise MediaSeedError("the agency has no WhatsApp phone number ID")
            with closing(client_factory(phone_number_id)) as client:
                sent = media.send_image(
                    conn, client, storage, agency_id, conversation[0], recipient,
                    ("property_media", media_id), f"media-check:{check_id}:{media_id}",
                    # ai_generated stays True: the control gate (R-28) still applies.
                    caption=f"Demo listing image {listing} (DEMO)",
                )
            result.sent_status, result.sent_via = sent.status, sent.via
            result.cached_after = _media_row(conn, agency_id, listing, order)[2]
        except MediaSeedError:
            raise
        except whatsapp.SendError as exc:
            raise MediaSeedError(f"send failed ({getattr(exc, 'code', type(exc).__name__)})"
                                 ) from None
        except psycopg.Error as exc:
            raise _db_error("send check", exc) from None
        except Exception as exc:  # noqa: BLE001 - S3 / HTTP errors can quote keys or URLs
            raise MediaSeedError(f"send failed ({type(exc).__name__}); check the outbound "
                                 "message status before retrying the same --check-id"
                                 ) from None
        return result
    finally:
        conn.close()


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agency-id", required=True)
    parser.add_argument("--listing", required=True)
    parser.add_argument("--order", type=int, default=0)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--check-id")
    args = parser.parse_args(argv)
    try:
        result = run(args.agency_id, (environ.get("DATABASE_URL") or "").strip(), environ,
                     args.listing, args.order, send=args.send, check_id=args.check_id)
    except MediaSeedError as exc:
        print(f"Send check failed: {exc}")
        return 1
    print("Mode: send" if args.send else "Mode: preview (nothing sent; use --send)")
    print(f"Listing {result.listing} order {result.order} label {result.label or 'NULL'} "
          f"media {uuid.UUID(str(result.media_id))}")
    print(f"WhatsApp ID cached: {'yes' if result.cached_id else 'no (first send uploads)'}; "
          f"card URL: {'yes' if result.card_url else 'no'}")
    if result.recipient_set:
        print(f"Recipient: set (not shown); conversation: "
              f"{'yes' if result.conversation else 'no'}; 24 h window open: "
              f"{'yes' if result.window_open else 'no'}")
    else:
        print(f"Recipient: {RECIPIENT_ENV} not set")
    if result.sent_status:
        print(f"Sent: {result.sent_status} via {result.sent_via}; ID cached now: "
              f"{'yes' if result.cached_after else 'no'}")
        return 0 if result.sent_status == "sent" else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
