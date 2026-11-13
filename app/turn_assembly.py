"""Customer scheduling and the durable pre-dispatch supersede boundary (R-01)."""

import dataclasses

from app import customer_reply
from app.db import tenant_transaction
from app.queue import DeferJob, PermanentJobError

QUIET_SECONDS = 1.5
CAP_SECONDS = 4.0


def debounce(conn, job, sender):
    from app.orchestrator import resolve_role

    with tenant_transaction(conn, job.agency_id):
        if resolve_role(conn, sender)[1] != "customer":
            return
        control = conn.execute(
            "SELECT control_state, mode FROM conversation WHERE customer_wa_id = %s",
            (sender,),
        ).fetchone()
        if control and control != ("ai_active", "ai"):
            return
        if conn.execute(
            "SELECT 1 FROM conversation_turn t JOIN conversation c "
            "ON c.id = t.conversation_id WHERE c.customer_wa_id = %s "
            "AND t.status = 'open'", (sender,),
        ).fetchone():
            return
        # Include unrecorded events too: ingestion continues while the worker runs.
        row = conn.execute(
            """
            SELECT extract(epoch FROM now() - min(e.received_at)),
                   extract(epoch FROM now() - max(e.received_at)),
                   bool_and(e.payload #>> '{message,type}' = 'text')
            FROM job j JOIN inbound_event e ON e.id = j.inbound_event_id
            WHERE j.conversation_key = %s AND j.job_type = 'process_message'
              AND (j.status = 'pending' OR j.id = %s)
            """, (job.conversation_key, job.id),
        ).fetchone()
        if row[0] is None or not row[2]:
            return
        delay = min(CAP_SECONDS - float(row[0]), QUIET_SECONDS - float(row[1]))
    if delay > 0:
        raise DeferJob(delay)


def before_dispatch(conn, job, turn, payloads):
    """Return an expanded turn to replan, or durably commit to this plan's dispatch."""
    from app import orchestrator as o
    from app.handlers import record_event

    if turn.route != "customer":
        return None
    # Record newly persisted events before inspecting message membership.
    for event_id in o.pending_sibling_events(conn, job):
        try:
            record_event(conn, job.agency_id, event_id)
        except PermanentJobError:
            pass
    with tenant_transaction(conn, job.agency_id):
        count, started = conn.execute(
            "SELECT replan_count, dispatch_started FROM conversation_turn "
            "WHERE id = %s FOR UPDATE", (turn.id,),
        ).fetchone()
        protected = any(p.get("type") == customer_reply.SYSTEM for p in payloads)
        # Interim notifications also make dispatch irrevocable.
        attempted = conn.execute(
            "SELECT 1 FROM outbound_message WHERE dedupe_key LIKE %s LIMIT 1",
            (f"turn:{turn.id}:%",),
        ).fetchone()
        rows = []
        if not count and not started and not protected and not attempted:
            rows = conn.execute(
                f"""
                SELECT {o._MESSAGE_COLUMNS}, j.id FROM message m
                JOIN inbound_event e ON e.id = m.inbound_event_id
                JOIN job j ON j.inbound_event_id = e.id
                WHERE m.conversation_id = %s AND m.direction = 'inbound'
                  AND m.turn_id IS NULL AND j.status = 'pending'
                  AND j.job_type = 'process_message'
                ORDER BY {o._ORDER} FOR UPDATE OF m
                """, (turn.conversation_id,),
            ).fetchall()
        if not rows:
            conn.execute("UPDATE conversation_turn SET dispatch_started = TRUE WHERE id = %s",
                         (turn.id,))
            return None
        original = [m.id for m in turn.messages]
        conn.execute(
            "UPDATE conversation_turn SET replan_count = 1, original_message_ids = %s, "
            "reply_plan = NULL, message_count = message_count + %s WHERE id = %s",
            (original, len(rows), turn.id),
        )
        conn.execute("UPDATE message SET turn_id = %s WHERE id = ANY(%s)",
                     (turn.id, [r[0] for r in rows]))
        conn.execute(
            "UPDATE job SET status = 'succeeded', completed_at = now(), "
            "last_error_code = 'coalesced' WHERE id = ANY(%s) AND status = 'pending'",
            ([r[o._MESSAGE_COLUMN_COUNT] for r in rows],),
        )
        messages = conn.execute(
            f"SELECT {o._MESSAGE_COLUMNS} FROM message m "
            "LEFT JOIN inbound_event e ON e.id = m.inbound_event_id "
            f"WHERE m.turn_id = %s ORDER BY {o._ORDER}", (turn.id,),
        ).fetchall()
    return dataclasses.replace(turn, messages=tuple(o.TurnMessage(*r) for r in messages),
                               original_message_ids=tuple(original))
