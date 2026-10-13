"""Staff buffer segmentation (Task 7.4; Red-Team R-08, R-09, R-10; R3 staff step 3).

A staff member (admin / agent) sends listings as bursts of text and photos. Each
message of a staff turn is classified (classify_text, pure):

- description: property-like text (a property type word and an amount or a size);
- done:        the whole message is "done" / "bas" / "بس";
- command:     "#..." commands, a listing code (DH6-102), or an update phrase
               ("DHA 6 wala ghar sold", "price 3 crore kar do");
- note:        any other short text ("photos neeche hain");
- photo:       an image staged by app.media (its caption is classified as text).

absorb() (inside the staff turn, app.orchestrator) groups them into segments:

- a description opens a new segment; when the open segment already has one, that
  segment is closed first ('next_description'), so two listings sent back to back
  never share photos (R-09);
- photos join the open segment, or open one when there is none: photos before any
  description wait for the next description (R-09);
- a note joins the open segment; without one it is answered as a normal staff turn;
- "done" / "bas" closes the open segment ('done');
- commands, taps and other types are answered as a normal staff turn and leave the
  open segment (and its timer) alone.

Every item bumps the segment version and pushes the quiet-window flush to
QUIET_SECONDS after it; the staff_segment_flush job carries the version it was
scheduled for, so an older timer does nothing ('timer' close).

A closed segment is answered once by staff_segment_answer: it waits a little for
photos still downloading (app.media), flags missing ones, then runs one staff turn
whose messages are the segment's items (conversation_turn.staff_segment_id; the
reply goes through app.orchestrator like any turn, under turn:{id}:{seq} keys). A
segment without a description is flagged photo_only and asked about (R-10); the
attach flow itself (reply-to-card, recent drafts list) is Task 9.2, draft extraction
Task 8.2. Jobs use the staff member's WhatsApp ID as conversation_key, so they are
serialised with that staff member's process_message jobs (app.queue).

Never log message text or phone numbers.
"""

import logging
import re
from contextlib import closing

from psycopg.types.json import Jsonb

from app import normalise, requirements
from app.db import tenant_transaction
from app.queue import PermanentJobError, enqueue_job

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
FLUSH_JOB = "staff_segment_flush"
ANSWER_JOB = "staff_segment_answer"
# R-09 / R3: "60-90 s of quiet". 75 s: the middle of the range (owner decision).
QUIET_SECONDS = 75
# Photos still downloading when a segment closes: wait this long, at most MEDIA_WAITS
# times, then answer and flag them (media_pending).
MEDIA_WAIT_SECONDS = 15
MEDIA_WAITS = 2

DESCRIPTION, NOTE, DONE, COMMAND = "description", "note", "done", "command"
FLAG_PHOTO_ONLY, FLAG_MEDIA_PENDING, FLAG_MEDIA_FAILED = (
    "photo_only", "media_pending", "media_failed")

DONE_WORDS = frozenset({"done", "bas", "بس"})
LISTING_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{2,4}-[0-9]{3}(?![0-9])")
# An update of an existing listing (bench ST-10..ST-18; R3 step 5 UPDATE).
UPDATE_WORDS = re.compile(
    r"\b(?:wala|wale|wali|walay|waley|kar\s*do|ho\s*(?:gaya|gayi|gya|gyi|chuka|chuki)|"
    r"sold|rented|reserved|withdrawn|withdraw|on\s+hold|undo)\b",
    re.IGNORECASE,
)
# "New house ... wala" is still a new listing.
NEW_WORDS = re.compile(r"\b(?:new|naya|nayi|nai)\b", re.IGNORECASE)
_PUNCTUATION = re.compile(r"[^\w\s]+")


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def _done_key(text):
    return " ".join(_PUNCTUATION.sub(" ", text.casefold()).split())


def is_description(text):
    """Property-like text: a property type word and an amount or a size."""
    if not requirements.parse_types(text):
        return False
    return bool(normalise.find_money(text) or normalise.find_areas(text))


def classify_text(text):
    """description / done / command / note for a staff message text; None when empty."""
    text = (text or "").strip()
    if not text:
        return None
    if _done_key(text) in DONE_WORDS:
        return DONE
    if text.startswith("#") or LISTING_CODE.search(text):
        return COMMAND
    description = is_description(text)
    if UPDATE_WORDS.search(text) and not (description and NEW_WORDS.search(text)):
        return COMMAND
    return DESCRIPTION if description else NOTE


def classify(message, is_photo):
    """(action, text_role, is_photo) for one turn message: action is description,
    photo, note, done or command (answered as a normal staff turn)."""
    role = classify_text(message.text) if message.type in ("text", "image", "document") \
        else COMMAND
    if is_photo:
        if role == DESCRIPTION:
            return DESCRIPTION, DESCRIPTION, True
        return "photo", (NOTE if role else None), True
    if message.type != "text":
        return COMMAND, None, False
    return role, role if role in (DESCRIPTION, NOTE) else None, False


# --- segment storage ----------------------------------------------------------------

class _Segment:
    def __init__(self, row):
        (self.id, self.version, self.has_description, self.items,
         self.has_photo) = row


def _open_segment(conn, staff_id):
    row = conn.execute(
        """
        SELECT s.id, s.version,
               COALESCE(bool_or(i.text_role = 'description'), FALSE),
               count(i.id), COALESCE(bool_or(i.is_photo), FALSE)
        FROM staff_segment s LEFT JOIN staff_segment_item i ON i.segment_id = s.id
        WHERE s.staff_id = %s AND s.status = 'open'
        GROUP BY s.id
        """,
        (staff_id,),
    ).fetchone()
    if row is None:
        return None
    # Lock the row (aggregates cannot be locked directly).
    conn.execute("SELECT 1 FROM staff_segment WHERE id = %s FOR UPDATE", (row[0],))
    return _Segment(row)


def _create_segment(conn, turn):
    row = conn.execute(
        """
        INSERT INTO staff_segment (agency_id, staff_id, conversation_id, flush_due_at)
        VALUES (current_agency_id(), %s, %s, now() + make_interval(secs => %s))
        RETURNING id, version
        """,
        (turn.staff_id, turn.conversation_id, QUIET_SECONDS),
    ).fetchone()
    return _Segment((row[0], row[1], False, 0, False))


def _add_item(conn, turn, segment, message_id, text_role, is_photo):
    segment.items += 1
    conn.execute(
        """
        INSERT INTO staff_segment_item (
            agency_id, conversation_id, segment_id, message_id, position, text_role,
            is_photo
        )
        VALUES (current_agency_id(), %s, %s, %s, %s, %s, %s)
        """,
        (turn.conversation_id, segment.id, message_id, segment.items, text_role, is_photo),
    )
    segment.version += 1
    segment.has_description = segment.has_description or text_role == DESCRIPTION
    segment.has_photo = segment.has_photo or is_photo


def _close(conn, segment_id, reason, conversation_key):
    """Freeze the segment's items (status 'flushed') and queue its answer. Returns
    False when it was no longer open."""
    row = conn.execute(
        """
        UPDATE staff_segment s SET status = 'flushed', flushed_at = now(),
            flush_reason = %s, updated_at = now(),
            kind = CASE WHEN EXISTS (
                SELECT 1 FROM staff_segment_item i
                WHERE i.segment_id = s.id AND i.text_role = 'description'
            ) THEN 'description' ELSE 'photo_only' END
        WHERE id = %s AND status = 'open'
        RETURNING kind
        """,
        (reason, segment_id),
    ).fetchone()
    if row is None:
        return False
    if row[0] == "photo_only":
        _add_flags(conn, segment_id, [FLAG_PHOTO_ONLY])
    enqueue_job(conn, ANSWER_JOB, f"staff-segment-answer:{segment_id}",
                {"segment_id": str(segment_id), "waits": 0},
                conversation_key=conversation_key)
    logger.info("Staff segment %s closed (%s, %s)", segment_id, reason, row[0])
    return True


def _add_flags(conn, segment_id, flags):
    conn.execute(
        """
        UPDATE staff_segment SET updated_at = now(), flags = (
            SELECT COALESCE(jsonb_agg(DISTINCT f ORDER BY f), '[]'::jsonb)
            FROM jsonb_array_elements_text(flags || %s::jsonb) AS f
        )
        WHERE id = %s
        """,
        (Jsonb(sorted(flags)), segment_id),
    )


def _persist(conn, segment):
    """Store the version and quiet-window deadline after new items."""
    conn.execute(
        """
        UPDATE staff_segment SET version = %s, last_item_at = now(), updated_at = now(),
            flush_due_at = now() + make_interval(secs => %s)
        WHERE id = %s
        """,
        (segment.version, QUIET_SECONDS, segment.id),
    )


def _schedule(conn, segment, conversation_key):
    """_persist, then the flush job for this version (older ones become stale)."""
    _persist(conn, segment)
    enqueue_job(conn, FLUSH_JOB, f"staff-flush:{segment.id}:{segment.version}",
                {"segment_id": str(segment.id), "version": segment.version},
                conversation_key=conversation_key, delay_seconds=QUIET_SECONDS)


# --- absorb (called from the staff turn) ---------------------------------------------

def absorb(conn, turn):
    """Buffer this staff turn's segment messages; return the messages still to be
    answered as a normal staff turn (commands, taps, notes without a segment, other
    types), in order. Idempotent: a message already in a segment is skipped, so a
    resumed turn changes nothing twice."""
    with tenant_transaction(conn, turn.agency_id):
        _set_timeout(conn)
        ids = [message.id for message in turn.messages]
        photos = {row[0] for row in conn.execute(
            "SELECT message_id FROM inbound_media WHERE message_id = ANY(%s)", (ids,))}
        done = {row[0] for row in conn.execute(
            "SELECT message_id FROM staff_segment_item WHERE message_id = ANY(%s)", (ids,))}
        segment = _open_segment(conn, turn.staff_id)
        touched, rest = False, []
        for message in turn.messages:
            action, role, is_photo = classify(message, message.id in photos)
            if message.id in done:
                continue
            if action == COMMAND:
                rest.append(message)
            elif action == DONE:
                if segment is not None:
                    if touched:
                        _persist(conn, segment)
                    _close(conn, segment.id, "done", turn.sender_wa_id)
                    segment, touched = None, False
            elif action == NOTE and segment is None:
                rest.append(message)
            else:
                if action == DESCRIPTION and segment is not None and segment.has_description:
                    if touched:
                        _persist(conn, segment)
                    _close(conn, segment.id, "next_description", turn.sender_wa_id)
                    segment, touched = None, False
                if segment is None:
                    segment = _create_segment(conn, turn)
                _add_item(conn, turn, segment, message.id, role, is_photo)
                touched = True
        if segment is not None and touched:
            _schedule(conn, segment, turn.sender_wa_id)
    return rest


# --- jobs ----------------------------------------------------------------------------

def flush(conn, job):
    """staff_segment_flush: close the segment after its quiet window, unless a newer
    item (version) or another close got there first."""
    segment_id, version = job.payload.get("segment_id"), job.payload.get("version")
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        row = conn.execute(
            "SELECT status, version FROM staff_segment WHERE id = %s FOR UPDATE",
            (segment_id,),
        ).fetchone()
        if row is None:
            raise PermanentJobError("segment_missing")
        if row[0] != "open" or row[1] != version:
            logger.info("Staff segment %s flush %s is stale", segment_id, version)
            return
        _close(conn, segment_id, "timer", job.conversation_key)


def answer(conn, job, client_factory, *, engine=None):
    """staff_segment_answer: one staff turn for a closed segment (app.orchestrator)."""
    segment_id = job.payload.get("segment_id")
    waits = int(job.payload.get("waits") or 0)
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        row = conn.execute(
            """
            SELECT s.status, s.conversation_id, c.customer_wa_id, a.meta_phone_number_id
            FROM staff_segment s
            JOIN conversation c ON c.id = s.conversation_id
            JOIN agency a ON a.id = s.agency_id
            WHERE s.id = %s
            """,
            (segment_id,),
        ).fetchone()
        if row is None or row[0] != "flushed":
            raise PermanentJobError("segment_not_flushed")
        _, conversation_id, sender, phone_number_id = row
        media = conn.execute(
            """
            SELECT m.status FROM staff_segment_item i
            JOIN inbound_media m ON m.message_id = i.message_id
            WHERE i.segment_id = %s AND i.is_photo
            """,
            (segment_id,),
        ).fetchall()
        statuses = [r[0] for r in media]
        if "pending" in statuses and waits < MEDIA_WAITS:
            enqueue_job(conn, ANSWER_JOB, f"staff-segment-answer:{segment_id}:w{waits + 1}",
                        {"segment_id": segment_id, "waits": waits + 1},
                        conversation_key=job.conversation_key,
                        delay_seconds=MEDIA_WAIT_SECONDS)
            logger.info("Staff segment %s waits for photos (%d)", segment_id, waits + 1)
            return
        flags = []
        if "pending" in statuses:
            flags.append(FLAG_MEDIA_PENDING)
        if any(s in ("failed", "rejected") for s in statuses):
            flags.append(FLAG_MEDIA_FAILED)
        if flags:
            _add_flags(conn, segment_id, flags)

    from app import orchestrator  # orchestrator imports this module

    turn = orchestrator.segment_turn(conn, job, sender, conversation_id, segment_id)
    if turn is None:
        return  # already answered
    with closing(client_factory(phone_number_id)) as client:
        try:
            orchestrator.answer_turn(conn, client, job, turn, engine=engine)
        except Exception as exc:
            if not (isinstance(exc, PermanentJobError) or job.attempts >= job.max_attempts):
                raise  # retried: the open segment turn is resumed
            if orchestrator.fail_turn(conn, turn, exc):
                orchestrator.send_failure_notice(
                    conn, client, job.agency_id, conversation_id, sender, turn.id)
            raise
