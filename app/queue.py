"""Postgres job queue primitives (Task 3.1, component C2).

Claiming uses FOR UPDATE SKIP LOCKED inside a tenant transaction, so RLS still
applies. Jobs sharing a conversation_key are serialised by a session-level
advisory lock that the claiming connection holds from the claim until the
final status commits; if the worker dies, Postgres drops the lock with its
connection and the lease reaper requeues the job.

Ordering note: a job waiting on retry backoff does not block newer jobs of the
same conversation. The conversation orchestrator (C3) coalesces all pending
messages of a turn in WhatsApp timestamp order, so this is safe.

Never log job payloads or exception messages; they can contain customer data.
"""

import logging
import random
from dataclasses import dataclass
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import tenant_transaction

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
CLAIM_BATCH = 10
DEFAULT_LEASE_SECONDS = 300
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 300
# Two-key advisory locks live in a keyspace separate from single-bigint locks
# (such as the migration lock); the first key namespaces conversation locks.
CONVERSATION_LOCK_NAMESPACE = 3101
MAX_ERROR_CODE_LENGTH = 100
# A process_message job that dies of a crash on its last attempt ran no failure
# handling; the reaper queues this job so the customer still hears that the turn
# failed (app.orchestrator.notify_failed_job; RFC 13 poison jobs).
TURN_FAILED_NOTICE_JOB = "turn_failed_notice"
_NOTICE_FOR = "process_message"

_JOB_COLUMNS = (
    "id, agency_id, job_type, payload, conversation_key, attempts, max_attempts, "
    "inbound_event_id"
)


class PermanentJobError(Exception):
    """Retrying cannot help: the job is dead-lettered on this attempt."""

    def __init__(self, code="permanent_error"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Job:
    id: UUID
    agency_id: UUID
    job_type: str
    payload: dict
    conversation_key: str | None
    attempts: int
    max_attempts: int
    inbound_event_id: UUID | None


def backoff_seconds(attempts, rng=random.random):
    """Delay before the next attempt: exponential from 5 s, capped at 300 s, ±20% jitter."""
    delay = min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS)
    return delay * (0.8 + 0.4 * rng())


def error_code(exc):
    """A log-safe failure code: the exception class or explicit code, never its message."""
    code = exc.code if isinstance(exc, PermanentJobError) else type(exc).__name__
    return str(code)[:MAX_ERROR_CODE_LENGTH]


def _lock_args(agency_id, conversation_key):
    return CONVERSATION_LOCK_NAMESPACE, f"{agency_id}:{conversation_key}"


def try_lock_conversation(conn, agency_id, conversation_key):
    """Take the session-level conversation lock without waiting; True if acquired."""
    return conn.execute(
        "SELECT pg_catalog.pg_try_advisory_lock(%s, pg_catalog.hashtext(%s))",
        _lock_args(agency_id, conversation_key),
    ).fetchone()[0]


def unlock_conversation(conn, agency_id, conversation_key):
    conn.execute(
        "SELECT pg_catalog.pg_advisory_unlock(%s, pg_catalog.hashtext(%s))",
        _lock_args(agency_id, conversation_key),
    )


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def enqueue_job(
    conn, job_type, dedupe_key, payload=None, *, conversation_key=None,
    delay_seconds=0, inbound_event_id=None,
):
    """Inside tenant_transaction: add a job, optionally delayed. Returns its id, or
    None when a job with this dedupe_key already exists."""
    row = conn.execute(
        """
        INSERT INTO job (
            agency_id, inbound_event_id, job_type, dedupe_key, payload,
            conversation_key, available_at
        )
        VALUES (
            current_agency_id(), %s, %s, %s, %s, %s,
            now() + make_interval(secs => %s)
        )
        ON CONFLICT (agency_id, dedupe_key) DO NOTHING
        RETURNING id
        """,
        (inbound_event_id, job_type, dedupe_key, Jsonb(payload or {}),
         conversation_key, delay_seconds),
    ).fetchone()
    return row[0] if row else None


def claim_job(conn, agency_id, job_types, locked_by, lease_seconds=DEFAULT_LEASE_SECONDS):
    """Claim the oldest due job of the given types whose conversation is free.

    On success the caller holds the conversation lock (if the job has a key)
    and must release it through finish_job.
    """
    locked_key = None
    try:
        with tenant_transaction(conn, agency_id):
            _set_timeout(conn)
            candidates = conn.execute(
                """
                SELECT id, conversation_key FROM job
                WHERE status = 'pending' AND available_at <= now()
                  AND job_type = ANY(%s)
                ORDER BY available_at, created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (list(job_types), CLAIM_BATCH),
            ).fetchall()
            for job_id, key in candidates:
                if key is not None:
                    if not try_lock_conversation(conn, agency_id, key):
                        continue
                    locked_key = key
                row = conn.execute(
                    f"""
                    UPDATE job SET
                        status = 'running',
                        attempts = attempts + 1,
                        locked_at = now(),
                        locked_by = %s,
                        lease_expires_at = now() + make_interval(secs => %s)
                    WHERE id = %s
                    RETURNING {_JOB_COLUMNS}
                    """,
                    (locked_by, lease_seconds, job_id),
                ).fetchone()
                return Job(*row)
        return None
    except BaseException:
        # Session locks survive a rollback; release one taken for an unclaimed job.
        if locked_key is not None and not conn.closed:
            unlock_conversation(conn, agency_id, locked_key)
        raise


def finish_job(conn, job, locked_by, error=None, *, backoff=backoff_seconds):
    """Record the outcome, then release the conversation lock.

    Returns the new status: 'succeeded', 'pending' (retry scheduled) or 'dead',
    or None if this worker no longer owned the job (its lease was reaped).
    """
    try:
        with tenant_transaction(conn, job.agency_id):
            _set_timeout(conn)
            if error is None:
                row = conn.execute(
                    """
                    UPDATE job SET
                        status = 'succeeded', completed_at = now(), last_error_code = NULL,
                        locked_at = NULL, locked_by = NULL, lease_expires_at = NULL
                    WHERE id = %s AND status = 'running' AND locked_by = %s
                    RETURNING status
                    """,
                    (job.id, locked_by),
                ).fetchone()
            else:
                dead = isinstance(error, PermanentJobError) or job.attempts >= job.max_attempts
                row = conn.execute(
                    """
                    UPDATE job SET
                        status = CASE WHEN %(dead)s THEN 'dead' ELSE 'pending' END,
                        available_at = CASE WHEN %(dead)s THEN available_at
                            ELSE now() + make_interval(secs => %(delay)s) END,
                        completed_at = CASE WHEN %(dead)s THEN now() END,
                        last_error_code = %(code)s,
                        locked_at = NULL, locked_by = NULL, lease_expires_at = NULL
                    WHERE id = %(id)s AND status = 'running' AND locked_by = %(locked_by)s
                    RETURNING status
                    """,
                    {
                        "dead": dead, "delay": 0 if dead else backoff(job.attempts),
                        "code": error_code(error), "id": job.id, "locked_by": locked_by,
                    },
                ).fetchone()
    finally:
        if job.conversation_key is not None and not conn.closed:
            unlock_conversation(conn, job.agency_id, job.conversation_key)

    if row is None:
        logger.warning("Job %s finished after losing its lease", job.id)
        return None
    return row[0]


def reap_expired(conn, agency_id):
    """Requeue running jobs whose lease expired and whose owner is gone.

    A job with a conversation key is only requeued if its conversation lock is
    free, which proves the owning connection has ended. Exhausted jobs go dead.
    A process_message job that goes dead here also gets a turn_failed_notice job
    (same conversation key). Returns the number of jobs reaped.
    """
    reaped = []
    locked_keys = []
    try:
        with tenant_transaction(conn, agency_id):
            _set_timeout(conn)
            expired = conn.execute(
                """
                SELECT id, conversation_key, job_type FROM job
                WHERE status = 'running' AND lease_expires_at < now()
                FOR UPDATE SKIP LOCKED
                """
            ).fetchall()
            for job_id, key, job_type in expired:
                if key is not None:
                    if not try_lock_conversation(conn, agency_id, key):
                        continue
                    locked_keys.append(key)
                status = conn.execute(
                    """
                    UPDATE job SET
                        status = CASE WHEN attempts >= max_attempts THEN 'dead'
                            ELSE 'pending' END,
                        completed_at = CASE WHEN attempts >= max_attempts THEN now() END,
                        available_at = now(),
                        last_error_code = 'lease_expired',
                        locked_at = NULL, locked_by = NULL, lease_expires_at = NULL
                    WHERE id = %s
                    RETURNING status
                    """,
                    (job_id,),
                ).fetchone()[0]
                reaped.append((job_id, status))
                if status == "dead" and job_type == _NOTICE_FOR:
                    enqueue_job(conn, TURN_FAILED_NOTICE_JOB, f"turn-failed-notice:{job_id}",
                                {"job_id": str(job_id)}, conversation_key=key)
    finally:
        # Released after the transaction ends: the row locks kept other
        # workers off these jobs until commit, and an aborted transaction
        # cannot run the unlock.
        if not conn.closed:
            for key in locked_keys:
                unlock_conversation(conn, agency_id, key)
    for job_id, status in reaped:
        logger.warning("Job %s lease expired; now %s", job_id, status)
    return len(reaped)
