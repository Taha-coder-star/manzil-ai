"""Idempotent outbound sends with an outbound_message log (Task 3.2, component C12).

Each send has a stable dedupe key (the job's dedupe key plus a sequence number),
so a retried job never sends the same reply twice:

1. One tenant transaction inserts (or finds) the message + outbound_message rows,
   applies the send-time control gate (R-28) and the 24 h window check, then
   marks the row 'sending'.
2. The HTTP call runs outside any transaction.
3. A second transaction records 'sent' / 'failed' / 'unknown', or puts the row
   back to 'pending' for a retry.

A row still 'sending' when its job is retried means the worker died mid-call.
Meta may already have delivered it and the Cloud API has no idempotency key, so
it becomes 'unknown' and is never resent (at most once). The same applies to a
read timeout.

Residual gate window: a control-state change that commits after step 1 but
before Meta accepts the call is not seen; it is bounded by one HTTP call.

Templates (Task 11.1: new_lead_alert) are the only messages Meta delivers outside the
24 h window, so a template payload skips the window check. It must be sent with
ai_generated=False: templates carry handoff notices, never model output.
"""

import logging
import os
import socket
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from app import whatsapp
from app.db import tenant_transaction

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
SESSION_WINDOW_HOURS = 24
# Longer than the HTTP timeouts; a row past its lease belongs to a dead worker.
SEND_LEASE_SECONDS = 60
LOCKED_BY = f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class SendResult:
    status: str
    provider_message_id: str | None = None


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def send_message(
    conn, client, agency_id, conversation_id, recipient, payload, dedupe_key,
    *, ai_generated=True, reply_to=None, media_source=None,
):
    """Send one built payload at most once per dedupe_key.

    Returns a SendResult whose status is 'sent', 'cancelled' (control gate),
    'failed' (outside the 24 h window, or an earlier permanent failure) or
    'unknown' (ambiguous). Raises RetryableSendError after putting the row back
    to 'pending', and PermanentSendError after recording 'failed'.
    media_source: (table, id) of the stored image an image payload shows (app.media).
    A template payload needs ai_generated=False (ValueError otherwise).
    """
    if payload["type"] == "template" and ai_generated:
        raise ValueError("a template is a system message: pass ai_generated=False")
    outbound_id, status = _prepare(
        conn, agency_id, conversation_id, recipient, payload, dedupe_key, ai_generated,
        media_source,
    )
    if status != "sending":
        return SendResult(status)

    try:
        wamid = client.send(recipient, payload, reply_to=reply_to)
    except whatsapp.RetryableSendError:
        _record(conn, agency_id, outbound_id, "pending")
        raise
    except whatsapp.PermanentSendError as exc:
        _record(conn, agency_id, outbound_id, "failed", error_code=exc.code)
        raise
    except whatsapp.AmbiguousSendError as exc:
        _record(conn, agency_id, outbound_id, "unknown", error_code=exc.code)
        logger.warning("Outbound %s outcome unknown (%s); not resending", outbound_id, exc.code)
        return SendResult("unknown")
    except BaseException:
        # Anything else (a bug, an interrupt) may have reached Meta: never resend.
        _record(conn, agency_id, outbound_id, "unknown", error_code="unexpected_error")
        raise
    _record(conn, agency_id, outbound_id, "sent", wamid=wamid)
    return SendResult("sent", wamid)


def prior_attempt(conn, agency_id, dedupe_key):
    """(status, last_error_code) of an earlier send with this key, or None."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        return conn.execute(
            "SELECT status, last_error_code FROM outbound_message WHERE dedupe_key = %s",
            (dedupe_key,),
        ).fetchone()


def _prepare(conn, agency_id, conversation_id, recipient, payload, dedupe_key, ai_generated,
             media_source=None):
    """Return (outbound_id, status); status is 'sending' only when this call may send."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        conversation = conn.execute(
            """
            SELECT mode,
                   last_customer_message_at > now() - make_interval(hours => %s)
            FROM conversation WHERE id = %s
            FOR SHARE
            """,
            (SESSION_WINDOW_HOURS, conversation_id),
        ).fetchone()
        if conversation is None:
            raise LookupError("conversation not found")
        mode, in_window = conversation

        row = conn.execute(
            "SELECT id, status FROM outbound_message WHERE dedupe_key = %s FOR UPDATE",
            (dedupe_key,),
        ).fetchone()
        if row is not None:
            outbound_id, status = row
            if status == "sending":
                _set_status(conn, outbound_id, "unknown", error_code="interrupted_send")
                logger.warning("Outbound %s was interrupted mid-send; not resending", outbound_id)
                return outbound_id, "unknown"
            if status != "pending":
                return outbound_id, status
        else:
            message_id = conn.execute(
                """
                INSERT INTO message (
                    agency_id, conversation_id, direction, message_type, text_body
                )
                VALUES (current_agency_id(), %s, 'outbound', %s, %s)
                RETURNING id
                """,
                (conversation_id, payload["type"], whatsapp.summary(payload)),
            ).fetchone()[0]
            outbound_id = conn.execute(
                """
                INSERT INTO outbound_message (
                    agency_id, conversation_id, message_id, dedupe_key,
                    recipient_wa_id, payload, media_source, media_source_id
                )
                VALUES (current_agency_id(), %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (conversation_id, message_id, dedupe_key, recipient, Jsonb(payload),
                 *(media_source or (None, None))),
            ).fetchone()[0]

        # Send-time gate (R-28): after a handoff the AI never speaks.
        if ai_generated and mode != "ai":
            _set_status(conn, outbound_id, "cancelled", error_code="control_gate")
            return outbound_id, "cancelled"
        if not in_window and payload["type"] != "template":
            # Session messages need an open 24 h window; a template does not.
            _set_status(conn, outbound_id, "failed", error_code="outside_window")
            return outbound_id, "failed"

        conn.execute(
            """
            UPDATE outbound_message SET
                status = 'sending', attempts = attempts + 1, locked_by = %s,
                lease_expires_at = now() + make_interval(secs => %s)
            WHERE id = %s
            """,
            (LOCKED_BY, SEND_LEASE_SECONDS, outbound_id),
        )
        return outbound_id, "sending"


def _set_status(conn, outbound_id, status, *, error_code=None):
    conn.execute(
        """
        UPDATE outbound_message SET
            status = %s, last_error_code = %s, locked_by = NULL, lease_expires_at = NULL
        WHERE id = %s
        """,
        (status, error_code, outbound_id),
    )


def _record(conn, agency_id, outbound_id, status, *, wamid=None, error_code=None):
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        if status == "sent":
            message_id = conn.execute(
                """
                UPDATE outbound_message SET
                    status = 'sent', provider_message_id = %s, sent_at = now(),
                    last_error_code = NULL, locked_by = NULL, lease_expires_at = NULL
                WHERE id = %s
                RETURNING message_id
                """,
                (wamid, outbound_id),
            ).fetchone()[0]
            conn.execute(
                "UPDATE message SET provider_message_id = %s WHERE id = %s",
                (wamid, message_id),
            )
        else:
            _set_status(conn, outbound_id, status, error_code=error_code)
