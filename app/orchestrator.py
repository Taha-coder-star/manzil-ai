"""Conversation orchestrator skeleton (Task 5.1, component C3; RFC flow steps 2-3).

One process_message job runs one turn for its conversation:

1. The handler records the trigger message and every pending sibling message of
   the same conversation (app.handlers.record_event). The worker holds the
   conversation lock (app.queue), so no sibling job can run meanwhile.
2. assemble_turn, in one tenant transaction: resume an open turn if a crashed
   attempt left one; otherwise take every recorded inbound message whose
   process_message job is this job or still pending, in WhatsApp timestamp
   order, insert one conversation_turn, point the messages at it, and mark the
   absorbed sibling jobs succeeded ('coalesced'). A message recorded after this
   transaction (the cutoff) waits for the next turn; debounce and
   supersede-before-send are Task 12.1.
3. Role routing from the sender allowlist only (never from message content):
   active admin / agent -> staff, active broker -> broker, anyone else -> customer.
4. Control-state gate stub: the AI acts only when control_state = 'ai_active'
   and mode = 'ai'; otherwise the turn is 'gated' and nothing is sent
   (forwarding to the broker is Task 11.x). The send-time gate in app.outbound
   still re-checks before every message (R-28).
5. The reply: Task 6.1's AI turn engine (app.turn_engine) when AI_PROVIDER=gemini,
   otherwise the route's stub responder. Sends use the dedupe key
   "turn:{turn_id}:{seq}", so a resumed turn never sends twice.
6. The turn is closed ('replied') only after its sends are recorded. A turn whose
   job will not retry (a permanent error, or the failure of its last attempt) is
   closed as 'failed' (migration 014), so it cannot block the conversation's later
   messages. A crashed attempt runs no cleanup and leaves the turn open; every
   attempt is counted in conversation_turn.attempts before it starts, and an open
   turn found with MAX_TURN_ATTEMPTS attempts is closed as 'failed' instead of
   resumed, so crashes cannot block the conversation either.

Task 5.2: a turn ending in a tap on a listing card (More photos, Visit / Details,
Similar, an overflow-list row) gets a stub reply naming the action and listing;
the real behaviour is Tasks 10.x / 11.x.

Never log message text or phone numbers.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

from app import echo, outbound, whatsapp
from app.config import ConfigurationError
from app.db import tenant_transaction
from app.queue import PermanentJobError, error_code

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
JOB_TYPE = "process_message"
COALESCED = "coalesced"
ROUTES = {"admin": "staff", "agent": "staff", "broker": "broker"}
# A resumed open turn plus the new turn of the job that found it.
MAX_TURNS_PER_JOB = 2
# Attempts (by any job, crashed or not) after which an open turn is abandoned as
# 'failed' instead of resumed again; the same as the job default max_attempts.
MAX_TURN_ATTEMPTS = 3
TURN_FAILED = "turn_failed"
# Reply ids of the listing cards (app.cards). The id is client-supplied: only this exact
# shape is ever echoed back.
TAP_ID = re.compile(r"(photos|visit|similar|listing):([A-Z0-9]{2,4}-[0-9]{3})")
TAP_ACTIONS = {
    "photos": "More photos", "visit": "Visit / Details", "similar": "Similar listings",
    "listing": "Details",
}


@dataclass(frozen=True)
class TurnMessage:
    id: UUID
    wamid: str
    type: str
    text: str | None
    media_id: str | None
    provider_timestamp: datetime | None
    reply_id: str | None = None  # id of a tapped button, list row or carousel quick reply


@dataclass(frozen=True)
class Turn:
    """What a responder sees: one coalesced, ordered turn and its snapshots."""

    id: UUID
    agency_id: UUID
    conversation_id: UUID
    sender_wa_id: str
    sender_role: str  # customer | admin | agent | broker
    route: str  # customer | staff | broker
    staff_id: UUID | None
    control_state: str
    conversation_mode: str
    messages: tuple[TurnMessage, ...]

    @property
    def ai_allowed(self):
        return self.control_state == "ai_active" and self.conversation_mode == "ai"


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def resolve_role(conn, sender):
    """Inside a tenant transaction: (sender_role, route, staff_id) from the allowlist.

    Phones match on their digits, as app.media.staff_id_for does. More than one
    active match is ambiguous and gets the least privilege (customer).
    """
    rows = conn.execute(
        """
        SELECT id, role FROM staff
        WHERE is_active AND regexp_replace(whatsapp_number, '[^0-9]', '', 'g') = %s
        """,
        (sender,),
    ).fetchall()
    if len(rows) == 1:
        staff_id, role = rows[0]
        return role, ROUTES[role], staff_id
    if rows:
        logger.warning("Sender matches %d active staff rows; routed as customer", len(rows))
    return "customer", "customer", None


def pending_sibling_events(conn, job):
    """inbound_event ids of the other pending process_message jobs of this conversation."""
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        return [row[0] for row in conn.execute(
            """
            SELECT inbound_event_id FROM job
            WHERE job_type = %s AND status = 'pending' AND conversation_key = %s
              AND id <> %s AND inbound_event_id IS NOT NULL
            ORDER BY created_at
            """,
            (JOB_TYPE, job.conversation_key, job.id),
        )]


# The message log keeps a tap's title only; its id comes from the stored webhook event:
# message.interactive.<interactive type>.id for reply buttons and list rows
# (button_reply, list_reply), and message.button.payload for a carousel quick reply
# (type "button"; seen on a device 2026-10-10, not in Meta's carousel docs).
_MESSAGE_COLUMNS = (
    "m.id, m.provider_message_id, m.message_type, m.text_body, m.media_id, "
    "m.provider_timestamp, "
    "CASE m.message_type "
    "WHEN 'interactive' THEN e.payload #>> ARRAY["
    "'message', 'interactive', e.payload #>> '{message,interactive,type}', 'id'] "
    "WHEN 'button' THEN e.payload #>> '{message,button,payload}' END"
)
_MESSAGE_COLUMN_COUNT = 7
# WhatsApp timestamps have one-second resolution: receive time, then WAMID, break ties.
_ORDER = "m.provider_timestamp NULLS LAST, e.received_at, m.provider_message_id"


def assemble_turn(conn, job, sender):
    """Return (turn, resumed): the open turn left by a crashed attempt (resumed=True),
    or a newly assembled one; (None, False) when nothing is pending."""
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        conversation = conn.execute(
            "SELECT id, control_state, mode FROM conversation WHERE customer_wa_id = %s "
            "FOR UPDATE",
            (sender,),
        ).fetchone()
        if conversation is None:
            raise PermanentJobError("conversation_missing")
        conversation_id, control_state, mode = conversation

        turn = conn.execute(
            """
            SELECT id, sender_role, route, staff_id, control_state, conversation_mode,
                   attempts
            FROM conversation_turn WHERE conversation_id = %s AND status = 'open'
            """,
            (conversation_id,),
        ).fetchone()
        if turn is not None and turn[6] >= MAX_TURN_ATTEMPTS:
            # Every attempt so far ended without closing the turn (a crash runs no
            # cleanup; this count is all it leaves): give the turn up, then answer
            # the messages waiting behind it.
            _close_failed(conn, turn[0])
            logger.warning("Turn %s abandoned after %d attempts", turn[0], turn[6])
            turn = None
        if turn is not None:
            # Counted before any work, in this committed transaction: a crash cannot
            # undercount it.
            conn.execute(
                "UPDATE conversation_turn SET attempts = attempts + 1 WHERE id = %s",
                (turn[0],),
            )
            turn = turn[:6]
            rows = conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS} FROM message m
                LEFT JOIN inbound_event e ON e.id = m.inbound_event_id
                WHERE m.turn_id = %s ORDER BY {_ORDER}
                """,
                (turn[0],),
            ).fetchall()
            logger.info("Resuming open turn %s (%d messages)", turn[0], len(rows))
            return _turn(job.agency_id, conversation_id, sender, turn, rows), True

        rows = conn.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS}, j.id FROM message m
            JOIN inbound_event e ON e.id = m.inbound_event_id
            JOIN job j ON j.inbound_event_id = m.inbound_event_id AND j.job_type = %(type)s
            WHERE m.conversation_id = %(conversation)s AND m.direction = 'inbound'
              AND m.turn_id IS NULL
              AND (j.id = %(job)s OR j.status = 'pending')
            ORDER BY {_ORDER}
            FOR UPDATE OF m
            """,
            {"type": JOB_TYPE, "conversation": conversation_id, "job": job.id},
        ).fetchall()
        if not rows:
            return None, False  # a replay: every message is already in a turn

        sender_role, route, staff_id = resolve_role(conn, sender)
        turn_id = conn.execute(
            """
            INSERT INTO conversation_turn (
                agency_id, conversation_id, trigger_job_id, sender_role, route, staff_id,
                control_state, conversation_mode, message_count
            )
            VALUES (current_agency_id(), %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (conversation_id, job.id, sender_role, route, staff_id, control_state, mode,
             len(rows)),
        ).fetchone()[0]
        conn.execute(
            "UPDATE message SET turn_id = %s WHERE id = ANY(%s)",
            (turn_id, [row[0] for row in rows]),
        )
        siblings = [row[_MESSAGE_COLUMN_COUNT] for row in rows
                    if row[_MESSAGE_COLUMN_COUNT] != job.id]
        if siblings:
            conn.execute(
                """
                UPDATE job SET status = 'succeeded', completed_at = now(),
                    last_error_code = %s
                WHERE id = ANY(%s) AND status = 'pending'
                """,
                (COALESCED, siblings),
            )
        logger.info("Turn %s assembled: %s route, %d messages", turn_id, route, len(rows))
        snapshot = (turn_id, sender_role, route, staff_id, control_state, mode)
        return _turn(job.agency_id, conversation_id, sender, snapshot,
                     [row[:_MESSAGE_COLUMN_COUNT] for row in rows]), False


def _turn(agency_id, conversation_id, sender, snapshot, rows):
    turn_id, sender_role, route, staff_id, control_state, mode = snapshot
    return Turn(
        id=turn_id, agency_id=agency_id, conversation_id=conversation_id,
        sender_wa_id=sender, sender_role=sender_role, route=route, staff_id=staff_id,
        control_state=control_state, conversation_mode=mode,
        messages=tuple(TurnMessage(*row) for row in rows),
    )


def check_schema(conn, agency_id):
    """Raise ConfigurationError unless migration 014 (conversation_turn.attempts and
    the 'failed' status) is applied; the turn lifecycle needs it with any AI_PROVIDER.
    Runs as the runtime login inside a tenant transaction (RLS on)."""
    try:
        with tenant_transaction(conn, agency_id):
            _set_timeout(conn)
            conn.execute("SELECT attempts FROM conversation_turn LIMIT 0")
            row = conn.execute(
                """
                SELECT pg_catalog.pg_get_constraintdef(oid) FROM pg_catalog.pg_constraint
                WHERE conrelid = 'conversation_turn'::regclass
                  AND conname = 'conversation_turn_status_check'
                """
            ).fetchone()
    except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable,
            psycopg.errors.InsufficientPrivilege):
        row = None
    except psycopg.Error:
        raise ConfigurationError("Schema check failed: database unavailable") from None
    if row is None or "'failed'" not in row[0]:
        raise ConfigurationError("The worker requires migration 014 (turn attempts and "
                                 "failed turns)")


def close_turn(conn, turn, status):
    with tenant_transaction(conn, turn.agency_id):
        _set_timeout(conn)
        conn.execute(
            """
            UPDATE conversation_turn SET status = %s, completed_at = now()
            WHERE id = %s AND status = 'open'
            """,
            (status, turn.id),
        )


# --- responders (stubs; used when no AI turn engine is configured) -------------------

def mode_reply(turn):
    count = len(turn.messages)
    noun = "message" if count == 1 else "messages"
    return [whatsapp.text(
        f"{turn.route.capitalize()} mode (stub) · {count} {noun} in this turn"
    )]


RESPONDERS = {"customer": mode_reply, "staff": mode_reply, "broker": mode_reply}


def tap_reply(turn):
    """A stub for a turn ending in a tap on a listing card, else None (any route)."""
    match = TAP_ID.fullmatch(turn.messages[-1].reply_id or "")
    if match is None:
        return None
    action, code = match.groups()
    return [whatsapp.text(f"{TAP_ACTIONS[action]} for {code} (stub) · coming in a later task")]


def demo_command(turn):
    """The raw message of a staff turn ending in '#demo …', else None (customers never)."""
    if turn.route != "staff":
        return None
    last = turn.messages[-1]
    if last.type != "text" or not echo.is_demo(last.text or ""):
        return None
    return {"type": "text", "text": {"body": last.text}}


def run_turns(conn, job, client, sender, *, demo=None, engine=None):
    """Answer the conversation's pending turn for this job; an open turn a crashed
    attempt left behind is finished first, then the new messages form the next turn.

    demo(turn, message, dedupe_prefix) handles a staff '#demo …' turn (app.handlers).
    engine: the AI turn engine (app.turn_engine.TurnEngine), or None for the stubs.

    When this job will not retry (a permanent error or its last attempt), a failing
    turn is closed as 'failed'. If it was a resumed (older) turn, this job's own
    messages still get their turn before the failure is raised, so its dead job
    does not strand them; an error marked blocks_later_turns (a usage row that
    cannot be written) records them as a failed turn instead, with no provider call.
    """
    failure = None
    for _ in range(MAX_TURNS_PER_JOB):
        turn, resumed = assemble_turn(conn, job, sender)
        if turn is None:
            break
        if failure is not None and getattr(failure, "blocks_later_turns", False):
            fail_turn(conn, turn, failure)
            break
        try:
            _answer(conn, client, job, turn, demo, engine)
        except Exception as exc:
            if not (isinstance(exc, PermanentJobError) or job.attempts >= job.max_attempts):
                raise  # retried; the turn stays open and is resumed
            if not fail_turn(conn, turn, exc) or not resumed or failure is not None:
                raise
            failure = exc
            continue
        if not resumed:
            break
    if failure is not None:
        raise failure


def _close_failed(conn, turn_id):
    """Inside a tenant transaction holding the conversation lock: close an open turn
    as 'failed' and dead-letter its trigger job if it is still waiting to retry (its
    messages stay in the failed turn, so a retry would only report success). A turn
    that is no longer open (already replied or gated) is left as it is."""
    closed = conn.execute(
        "UPDATE conversation_turn SET status = 'failed', completed_at = now() "
        "WHERE id = %s AND status = 'open' RETURNING trigger_job_id",
        (turn_id,),
    ).fetchone()
    if closed is not None:
        conn.execute(
            "UPDATE job SET status = 'dead', completed_at = now(), last_error_code = %s "
            "WHERE id = %s AND status = 'pending'",
            (TURN_FAILED, closed[0]),
        )


def fail_turn(conn, turn, error):
    """The job will not retry: close the turn as 'failed' (_close_failed) so the
    conversation's next message starts a new turn instead of resuming this one. Never
    'replied'; the job keeps its error code. Returns False if the close itself failed
    (the database still down): the turn then stays open, and the next job resumes it
    within MAX_TURN_ATTEMPTS."""
    if conn.closed:
        return False
    try:
        if conn.info.transaction_status != TransactionStatus.IDLE:
            conn.rollback()
        with tenant_transaction(conn, turn.agency_id):
            _set_timeout(conn)
            _close_failed(conn, turn.id)
    except psycopg.Error as exc:
        logger.error("Turn %s left open: closing it as failed raised %s", turn.id,
                     type(exc).__name__)
        return False
    logger.warning("Turn %s failed (%s); the next message starts a new turn", turn.id,
                   error_code(error))
    return True


def _answer(conn, client, job, turn, demo, engine=None):
    if not turn.ai_allowed:
        # Control-state gate stub: no AI output; forwarding is Task 11.x.
        close_turn(conn, turn, "gated")
        logger.info("Turn %s gated (%s / %s)", turn.id, turn.control_state,
                    turn.conversation_mode)
        return
    try:
        client.mark_read(turn.messages[-1].wamid)
    except whatsapp.SendError as exc:
        # Blue ticks are cosmetic; never block the reply on them.
        logger.warning("Mark-read failed: %s", exc.code)

    try:
        command = demo_command(turn) if demo is not None else None
        if command is not None:
            demo(turn, command, f"turn:{turn.id}")
        else:
            payloads = tap_reply(turn)
            if payloads is None:
                payloads = (engine.respond(conn, job, turn) if engine is not None
                            else RESPONDERS[turn.route](turn))
            # Outbound transport time is outside the AI turn budget; logged separately.
            began = time.monotonic()
            for seq, payload in enumerate(payloads):
                outbound.send_message(
                    conn, client, turn.agency_id, turn.conversation_id, turn.sender_wa_id,
                    payload, f"turn:{turn.id}:{seq}",
                )
            logger.info("Turn %s: %d message(s) through outbound in %d ms", turn.id,
                        len(payloads), int((time.monotonic() - began) * 1000))
    except whatsapp.PermanentSendError as exc:
        # The failure is on its outbound_message row; the turn is finished.
        close_turn(conn, turn, "replied")
        raise PermanentJobError(exc.code) from None
    close_turn(conn, turn, "replied")
