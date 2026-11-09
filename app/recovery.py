"""One delayed AI recovery, serialised by the source conversation's queue lock.

The original turn remains the tool identity. Its apology plan is never replaced.
No raw content is copied into jobs or logs. Planning is at most once: a crash
after entering planning fails closed; persisted transport plans can be resumed.
"""

import dataclasses
import logging
from contextlib import closing

from psycopg.types.json import Jsonb

from app.db import tenant_transaction
from app.queue import DeferJob, enqueue_job

logger = logging.getLogger(__name__)
JOB_TYPE = "ai_recovery"
DELAY_SECONDS = 30


def retryable(code):
    return code in {"timeout", "http_408", "http_429"} or code.startswith("transport:") or (
        code.startswith("http_") and code[5:].isdigit() and int(code[5:]) >= 500)


def schedule(conn, job, turn, outcomes):
    """Called in the same tenant transaction that fixes the original reply plan."""
    if not outcomes or not outcomes[-1].recoverable:
        return
    row = conn.execute(
        "INSERT INTO ai_recovery (agency_id, source_turn_id, generation, message_ids) "
        "SELECT current_agency_id(), id, replan_count, %s FROM conversation_turn WHERE id = %s "
        "ON CONFLICT DO NOTHING RETURNING source_turn_id", ([m.id for m in turn.messages], turn.id),
    ).fetchone()
    if row:
        enqueue_job(conn, JOB_TYPE, f"ai-recovery:{turn.id}",
                    {"source_turn_id": str(turn.id)}, conversation_key=job.conversation_key,
                    delay_seconds=DELAY_SECONDS, max_attempts=5)


def _finish(conn, job, turn_id, status, reason):
    with tenant_transaction(conn, job.agency_id):
        conn.execute("UPDATE ai_recovery SET status = %s, reason = %s, completed_at = now() "
                     "WHERE source_turn_id = %s", (status, reason, turn_id))
    if status == "failed":
        logger.error("ALERT ai_recovery_failed agency=%s job=%s turn=%s reason=%s",
                     job.agency_id, job.id, turn_id, reason)


def _load(conn, job, turn_id):
    from app import orchestrator as o

    with tenant_transaction(conn, job.agency_id):
        conn.execute("SET LOCAL statement_timeout = '5s'")
        row = conn.execute(
            "SELECT t.id, t.sender_role, t.route, t.staff_id, t.control_state, "
            "t.conversation_mode, t.conversation_id, c.customer_wa_id, "
            "a.meta_phone_number_id, t.staff_segment_id, t.original_message_ids, "
            "r.status, r.reply_plan, r.generation, t.replan_count, t.status, r.message_ids "
            "FROM ai_recovery r JOIN conversation_turn t ON t.id = r.source_turn_id "
            "JOIN conversation c ON c.id = t.conversation_id "
            "JOIN agency a ON a.id = t.agency_id WHERE r.source_turn_id = %s",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        if row[9]:
            turn = o._segment_turn(conn, job.agency_id, row[6], row[7], row[:6], row[9])
        else:
            messages = conn.execute(
                f"SELECT {o._MESSAGE_COLUMNS} FROM message m LEFT JOIN inbound_event e "
                f"ON e.id = m.inbound_event_id WHERE m.turn_id = %s ORDER BY {o._ORDER}",
                (turn_id,),
            ).fetchall()
            turn = o._turn(job.agency_id, row[6], row[7], row[:6], messages)
        turn = dataclasses.replace(turn, original_message_ids=tuple(row[10]),
                                   outbound_prefix=f"recovery:{turn_id}",
                                   messages=tuple(m for m in turn.messages if m.id in row[16]))
        transcripts = dict(conn.execute(
            "SELECT id, transcript FROM message WHERE turn_id = %s AND transcript IS NOT NULL",
            (turn_id,),
        ).fetchall())
        turn = dataclasses.replace(turn, messages=tuple(
            dataclasses.replace(m, text=transcripts[m.id]) if m.id in transcripts else m
            for m in turn.messages))
        # Check unrecorded inbound events too. Status events cannot stale a reply.
        fresh = conn.execute(
            "SELECT 1 FROM inbound_event e JOIN job j ON j.inbound_event_id = e.id "
            "WHERE j.conversation_key = %s AND j.job_type = 'process_message' "
            "AND e.received_at > (SELECT max(e2.received_at) FROM message m "
            "JOIN inbound_event e2 ON e2.id = m.inbound_event_id WHERE m.turn_id = %s) "
            "LIMIT 1", (job.conversation_key, turn_id),
        ).fetchone()
        gate = conn.execute(
            "SELECT control_state = %s AND mode = %s AND control_state = 'ai_active' "
            "AND mode = 'ai' AND (ai_muted_until IS NULL OR ai_muted_until <= now()) "
            "AND last_customer_message_at > now() - interval '24 hours' "
            "FROM conversation WHERE id = %s", (row[4], row[5], row[6]),
        ).fetchone()[0]
        role = o.resolve_role(conn, row[7])
        # Only the failure/apology and interim are permissible original output.
        from app import turn_engine
        allowed = [turn_engine.TEMPLATES[k] for k in ("failure", "interim")]
        allowed += [turn_engine.TEMPLATES_EN[k] for k in ("failure", "interim")
                    if k in turn_engine.TEMPLATES_EN]
        substantive = conn.execute(
            "SELECT 1 FROM outbound_message WHERE dedupe_key LIKE %s "
            "AND NOT (payload->>'type' = 'text' AND payload #>> '{text,body}' = ANY(%s)) "
            "LIMIT 1", (f"turn:{turn_id}:%", allowed),
        ).fetchone()
        stale = bool(fresh or not gate or role != (row[1], row[2], row[3])
                     or substantive or row[13] != row[14])
        return turn, row[8], row[11], row[12], stale, row[15]


def run(conn, job, client_factory, engine, storage=None):
    from app import customer_reply, outbound, staff_actions, whatsapp
    from app.queue import PermanentJobError

    turn_id = job.payload["source_turn_id"]
    loaded = _load(conn, job, turn_id)
    if loaded is None:
        return
    turn, phone_id, status, plan, stale, source_status = loaded
    if status in {"completed", "cancelled", "failed"}:
        return
    if stale or engine is None:
        return _finish(conn, job, turn_id, "cancelled", "stale_or_disabled")
    if source_status == "open":
        raise DeferJob(5)  # original apology transport still in progress
    if source_status != "replied":
        return _finish(conn, job, turn_id, "cancelled", "source_failed")
    if status == "planning":
        return _finish(conn, job, turn_id, "failed", "planning_interrupted")
    if plan is None:
        with tenant_transaction(conn, job.agency_id):
            conn.execute("UPDATE ai_recovery SET status = 'planning' WHERE source_turn_id = %s",
                         (turn_id,))
        outcomes = []
        try:
            plan = engine.plan(conn, job, turn, outcome_sink=outcomes.append)
        except Exception:
            _finish(conn, job, turn_id, "failed", "planning_error")
            raise
        if outcomes and outcomes[-1].kind != "done":
            return _finish(conn, job, turn_id, "failed", "provider_failed")
        with tenant_transaction(conn, job.agency_id):
            conn.execute("UPDATE ai_recovery SET status = 'ready', reply_plan = %s "
                         "WHERE source_turn_id = %s", (Jsonb(plan), turn_id))
    if _load(conn, job, turn_id)[4]:
        return _finish(conn, job, turn_id, "cancelled", "stale_before_dispatch")
    send_status = "sent"
    try:
        with closing(client_factory(phone_id)) as client:
            staff_actions.register_cards(conn, turn, plan)
            if turn.route == "customer":
                send_status = customer_reply.send(conn, client, storage, turn, plan)
            else:
                for seq, payload in enumerate(plan):
                    send_status = outbound.send_message(
                        conn, client, turn.agency_id, turn.conversation_id,
                        turn.sender_wa_id, payload, f"{turn.outbound_prefix}:{seq}").status
                    if send_status != "sent":
                        break
    except whatsapp.PermanentSendError as exc:
        _finish(conn, job, turn_id, "failed", "permanent_send")
        raise PermanentJobError(exc.code) from None
    if send_status != "sent":
        return _finish(conn, job, turn_id, "cancelled" if send_status == "cancelled" else "failed",
                       f"send_{send_status}")
    _finish(conn, job, turn_id, "completed", "dispatched")
