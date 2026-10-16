"""The lead card's buttons (Task 11.1 step 4, component C13; R6, RFC 7.5-7.6, R-29, R-30;
docs/HANDOFF.md). Handled in code before the staff buffer and the engine, never by a model.

- [Accept] ho:accept:{handoff_id}: while pending, the assigned agent or any active admin
  (owner decision 2026-10-16: escalation adds ADMIN, it does not remove the broker); the
  first tap wins under the handoff's row lock. The handoff becomes accepted, the customer
  conversation human_owned (mode 'human'), the 48 h release starts, and the accepter gets
  the Full details text (R-30: messages and listings). A later tap changes nothing and
  says who has it.
- [Pass] ho:pass:{id}: the assigned broker hands a pending lead to an admin (they can no
  longer accept it); the admin gets the card.
- [Called] ho:called:{id}: records the first call (called_at).

The handoff id in a tap is not trusted: it is looked up under the sender's tenant (RLS),
and the sender's role and assignment are re-read in the same transaction. A forged,
stale or other-tenant id gets "not found" and changes nothing.

resend_cards: a staff member who writes opens their 24 h window, so a card that could not
reach them (closed window: only the template went) is sent now, once.

Task 11.2 commands (R-15): "#done L-07" ends an accepted handoff (completed, lead closed,
the conversation back to ai_active: the AI answers the customer's next message); "#pass
L-07" is the Pass button as text. Without a code they apply to the sender's only open
lead, else they ask which. Any other broker message gets the help reply with the
sender's open leads; it is never read as a listing or sent to a model.

release (handoff_release job): an accepted handoff with no customer message for 48 h is
released to the AI; a job whose deadline has moved on finds a later release_due_at and
does nothing. Never log names, numbers or message text.
"""

import dataclasses
import logging
import re

from app import handoff, handoff_alert, outbound, whatsapp
from app.db import tenant_transaction

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
TAP = re.compile(r"ho:(accept|pass|called):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                 r"[0-9a-f]{4}-[0-9a-f]{12})")
COMMAND = re.compile(r"\s*#(done|pass)(?:\s+L-?0*(\d{1,6}))?[\s.!]*", re.IGNORECASE)
DETAIL_MESSAGES = 10
DETAIL_MESSAGE_CHARS = 300

# Staff-facing wording (English, D15; awaits owner approval).
TEXT = {
    "accepted": ("✅ You accepted *{code}*.\n\n💬 Chat with the customer:\n"
                 "https://wa.me/{number}"),
    "already_mine": "You already accepted *{code}*.",
    "already": "*{code}* was already accepted by {name}.",
    "closed": "{code} is closed.",
    "not_yours": "{code} is no longer assigned to you.",
    "unknown": "That lead was not found.",
    "passed": "↪ *{code}* passed to {name}.",
    "no_admin": "There is no admin to pass {code} to; it stays with you.",
    "pass_accepted": "{code} is already accepted; send #done {code} when you have finished.",
    "called": "📞 Noted: you called the customer of *{code}*.",
    # Task 11.2
    "done": "✅ *{code}* is closed. The AI assistant will answer this customer again.",
    "done_pending": "{code} has not been accepted yet: tap Accept first, or Pass.",
    "which": "Which lead? Send {command} with its code, e.g. {command} {example}.",
    "none_open": "You have no open leads.",
    "help": ("*Manzil lead commands*\n\n- *#done L-07*: finish a lead; the AI answers the "
             "customer again\n- *#pass L-07*: pass a pending lead to the admin\n\nThe "
             "*Accept* / *Pass* / *Called* buttons are on each lead card."),
    "open": "Your open leads: {leads}.",
    "released": ("{code} was handed back to the AI assistant after {hours} h without a "
                 "customer message."),
}


def split(turn):
    """(lead-card taps and #done / #pass commands, other messages) of a staff or broker
    turn."""
    mine, others = [], []
    for message in turn.messages:
        ours = (TAP.fullmatch(message.reply_id or "")
                or (message.type == "text" and COMMAND.fullmatch(message.text or "")))
        (mine if ours else others).append(message)
    return mine, others


@dataclasses.dataclass(frozen=True)
class _Row:
    status: str
    assigned: str | None
    accepted_by: str | None
    accepted_name: str | None
    conversation_id: str
    code: str
    customer: str


def _lock(conn, handoff_id):
    row = conn.execute(
        """
        SELECT h.status, h.assigned_staff_id::text, h.accepted_by_staff_id::text, s.name,
               h.conversation_id::text, l.code, c.customer_wa_id
        FROM handoff h JOIN lead l ON l.id = h.lead_id
        JOIN conversation c ON c.id = h.conversation_id
        LEFT JOIN staff s ON s.id = h.accepted_by_staff_id
        WHERE h.id = %s FOR UPDATE OF h
        """, (handoff_id,)).fetchone()
    return _Row(*row) if row else None


def _actor(conn, staff_id):
    """(role, name) of an active staff member, or None."""
    return conn.execute("SELECT role, name FROM staff WHERE id = %s AND is_active",
                        (staff_id,)).fetchone()


def _text(key, **params):
    return whatsapp.text(TEXT[key].format(**params))


def respond(conn, client, turn, messages, gazetteer=None):
    """The replies to this turn's card taps, in order (Full details after an Accept)."""
    out = []
    for message in messages:
        tapped = TAP.fullmatch(message.reply_id or "")
        if tapped:
            action, handoff_id = tapped.groups()
        else:
            action, number = COMMAND.fullmatch(message.text).groups()
            action = action.lower()
            handoff_id, reply = _command_target(conn, turn, action, number)
            if handoff_id is None:
                out.append(reply)
                continue
        out += {"accept": accept, "pass": pass_lead, "called": called, "done": done}[action](
            conn, client, turn, handoff_id, gazetteer)
    return out


def _open_leads(conn, staff_id, admin):
    """[(handoff id, lead code, status)] the staff member holds: assigned or accepted by
    them (an admin also sees escalated pending ones)."""
    return conn.execute(
        """
        SELECT h.id::text, l.code, h.status FROM handoff h JOIN lead l ON l.id = h.lead_id
        WHERE h.status IN ('pending', 'accepted')
          AND (h.accepted_by_staff_id = %(s)s
               OR (h.status = 'pending' AND (h.assigned_staff_id = %(s)s
                                              OR (%(admin)s AND h.escalated_at IS NOT NULL))))
        ORDER BY h.requested_at
        """, {"s": staff_id, "admin": admin}).fetchall()


def _command_target(conn, turn, action, number):
    """(handoff id, None), or (None, the reply) when the command names no open lead."""
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        if number is not None:
            row = conn.execute(
                "SELECT h.id::text FROM handoff h JOIN lead l ON l.id = h.lead_id "
                "WHERE l.lead_number = %s ORDER BY h.requested_at DESC LIMIT 1",
                (int(number),)).fetchone()
            return (row[0], None) if row else (None, _text("unknown"))
        who = _actor(conn, str(turn.staff_id))
        leads = _open_leads(conn, str(turn.staff_id), bool(who) and who[0] == "admin")
    wanted = [lead for lead in leads if action != "done" or lead[2] == "accepted"] or leads
    if len(wanted) == 1:
        return wanted[0][0], None
    if not wanted:
        return None, _text("none_open")
    return None, _text("which", command=f"#{action}", example=wanted[0][1])


def help_reply(conn, turn):
    """R-15: what a broker can send, and their open leads."""
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        who = _actor(conn, str(turn.staff_id))
        leads = _open_leads(conn, str(turn.staff_id), bool(who) and who[0] == "admin")
    tail = (TEXT["open"].format(leads=", ".join(f"{code} ({status})"
                                                 for _, code, status in leads))
            if leads else TEXT["none_open"])
    return [whatsapp.text(f"{TEXT['help']}\n\n{tail}")]


def done(conn, client, turn, handoff_id, gazetteer=None):
    """#done: the handoff completed, the lead closed and the conversation back to the AI,
    in one transaction (STATE_SCHEMA: control_state and the handoff stay in step)."""
    actor = str(turn.staff_id)
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = _lock(conn, handoff_id)
        who = _actor(conn, actor)
        if row is None or who is None:
            return [_text("unknown")]
        if row.status == "pending":
            return [_text("done_pending", code=row.code)]
        if row.status != "accepted":
            return [_text("closed", code=row.code)]
        if actor != row.accepted_by and who[0] != "admin":
            return [_text("not_yours", code=row.code)]
        _close(conn, handoff_id, row.conversation_id, "completed")
    logger.info("Handoff %s completed", handoff_id)
    return [_text("done", code=row.code)]


def _close(conn, handoff_id, conversation_id, status):
    conn.execute("UPDATE handoff SET status = %s, closed_at = now(), updated_at = now() "
                 "WHERE id = %s", (status, handoff_id))
    conn.execute("UPDATE lead SET status = 'closed', closed_at = now(), updated_at = now() "
                 "WHERE id = (SELECT lead_id FROM handoff WHERE id = %s) AND status = 'open'",
                 (handoff_id,))
    conn.execute("UPDATE conversation SET control_state = 'ai_active', mode = 'ai', "
                 "updated_at = now() WHERE id = %s", (conversation_id,))


def release(conn, job, client_factory):
    """The handoff_release job: 48 h after the latest customer message of an accepted
    handoff, the conversation goes back to the AI and the agent is told."""
    handoff_id = job.payload["handoff_id"]
    with tenant_transaction(conn, job.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = _lock(conn, handoff_id)
        if row is None or row.status != "accepted":
            return
        due = conn.execute("SELECT release_due_at <= now() FROM handoff WHERE id = %s",
                           (handoff_id,)).fetchone()[0]
        if not due:
            return   # a later message moved the deadline; its own job releases it
        _close(conn, handoff_id, row.conversation_id, "released")
        holder = conn.execute("SELECT whatsapp_number FROM staff WHERE id = %s",
                              (row.accepted_by,)).fetchone()
        staff_conv = handoff.staff_conversation(conn, handoff.digits(holder[0])) \
            if holder else None
    logger.info("Handoff %s released after %d h", handoff_id, handoff.RELEASE_HOURS)
    if staff_conv is None:
        return
    with handoff_alert._client(conn, job.agency_id, client_factory) as client:
        try:
            outbound.send_message(
                conn, client, job.agency_id, staff_conv, handoff.digits(holder[0]),
                _text("released", code=row.code, hours=handoff.RELEASE_HOURS),
                f"handoff:{handoff_id}:released", ai_generated=False)
        except whatsapp.PermanentSendError as exc:
            logger.warning("Release notice not sent (%s)", exc.code)


def accept(conn, client, turn, handoff_id, gazetteer=None):
    actor = str(turn.staff_id)
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = _lock(conn, handoff_id)
        who = _actor(conn, actor)
        if row is None or who is None:
            return [_text("unknown")]
        if row.status == "accepted":
            if row.accepted_by == actor:
                return [_text("already_mine", code=row.code)]
            name = handoff.first_name(row.accepted_name) or "someone else"
            return [_text("already", code=row.code, name=name)]
        if row.status != "pending":
            return [_text("closed", code=row.code)]
        if actor != row.assigned and who[0] != "admin":
            return [_text("not_yours", code=row.code)]
        conn.execute(
            "UPDATE handoff SET status = 'accepted', accepted_at = now(), "
            "accepted_by_staff_id = %s, updated_at = now() WHERE id = %s",
            (actor, handoff_id))
        handoff.touch_release(conn, handoff_id)
        # R-28: from now on the AI never speaks in this conversation (send-time gate).
        conn.execute(
            "UPDATE conversation SET control_state = 'human_owned', mode = 'human', "
            "updated_at = now() WHERE id = %s", (row.conversation_id,))
    logger.info("Handoff %s accepted", handoff_id)
    return [_text("accepted", code=row.code, number=row.customer),
            whatsapp.text(full_details(conn, turn.agency_id, handoff_id, gazetteer))]


def pass_lead(conn, client, turn, handoff_id, gazetteer=None):
    actor = str(turn.staff_id)
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = _lock(conn, handoff_id)
        if row is None or _actor(conn, actor) is None:
            return [_text("unknown")]
        if row.status == "accepted":
            return [_text("pass_accepted", code=row.code)]
        if row.status != "pending":
            return [_text("closed", code=row.code)]
        if actor != row.assigned:
            return [_text("not_yours", code=row.code)]
        admin = conn.execute(
            "SELECT id::text, name FROM staff WHERE is_active AND role = 'admin' AND id <> %s "
            "ORDER BY created_at, id LIMIT 1", (actor,)).fetchone()
        if admin is None:
            return [_text("no_admin", code=row.code)]
        conn.execute(
            "UPDATE handoff SET assigned_staff_id = %s, escalated_at = COALESCE(escalated_at, "
            "now()), updated_at = now() WHERE id = %s", (admin[0], handoff_id))
        conn.execute("UPDATE lead SET assigned_staff_id = %s, updated_at = now() WHERE id = "
                     "(SELECT lead_id FROM handoff WHERE id = %s)", (admin[0], handoff_id))
    packet = handoff_alert.load_packet(conn, turn.agency_id, handoff_id, gazetteer)
    handoff_alert.send_card(conn, client, turn.agency_id, packet, packet.agent,
                            handoff.accept_minutes(), escalated=True)
    logger.info("Handoff %s passed to an admin", handoff_id)
    return [_text("passed", code=row.code, name=handoff.first_name(admin[1]) or "the admin")]


def called(conn, client, turn, handoff_id, gazetteer=None):
    actor = str(turn.staff_id)
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = _lock(conn, handoff_id)
        who = _actor(conn, actor)
        if row is None or who is None:
            return [_text("unknown")]
        if row.status not in ("pending", "accepted"):
            return [_text("closed", code=row.code)]
        if actor not in (row.assigned, row.accepted_by) and who[0] != "admin":
            return [_text("not_yours", code=row.code)]
        conn.execute("UPDATE handoff SET called_at = COALESCE(called_at, now()), "
                     "updated_at = now() WHERE id = %s", (handoff_id,))
    return [_text("called", code=row.code)]


def full_details(conn, agency_id, handoff_id, gazetteer=None):
    """R-30 Full details: the card's facts plus the customer's latest messages (voice as
    transcript), one WhatsApp text."""
    packet = handoff_alert.load_packet(conn, agency_id, handoff_id, gazetteer)
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        rows = conn.execute(
            """
            SELECT to_char(created_at AT TIME ZONE 'Asia/Karachi', 'DD Mon HH24:MI'),
                   message_type, COALESCE(transcript, text_body)
            FROM message WHERE conversation_id = %s AND direction = 'inbound'
              AND message_type IN ('text', 'audio')
            ORDER BY created_at DESC LIMIT %s
            """, (packet.conversation_id, DETAIL_MESSAGES)).fetchall()
    customer = f"+{packet.customer_number}"
    if packet.customer_name:
        customer = f"{packet.customer_name} · {customer}"
    lines = [f"📋 *{packet.lead_code} full details*", "", f"👤 *Customer:* {customer}",
             f"🏠 *Wants:* {packet.wants or handoff_alert.NOT_STATED}", "", "📍 *Properties:*"]
    lines += [f"- {handoff_alert.listing_line(li, packet.positions.get(li.code))}"
              for li in packet.listings] or ["- none chosen"]
    lines += ["", "💬 *Messages* (latest last):"]
    for when, kind, body in reversed(rows):
        label = " · voice" if kind == "audio" else ""
        lines += ["", f"_{when}{label}_",
                  handoff_alert._clip(body, DETAIL_MESSAGE_CHARS)]
    return "\n".join(lines)[:whatsapp.TEXT_MAX]


def resend_cards(conn, client, turn, gazetteer=None):
    """Cards of pending leads this staff member should have but never received (their
    window was closed): sent now that they wrote. Returns how many were sent."""
    actor = str(turn.staff_id)
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        who = _actor(conn, actor)
        if who is None:
            return 0
        ids = [r[0] for r in conn.execute(
            """
            SELECT h.id::text FROM handoff h
            WHERE h.status = 'pending'
              AND (h.assigned_staff_id = %(a)s
                   OR (%(admin)s AND h.escalated_at IS NOT NULL))
              AND NOT EXISTS (
                  SELECT 1 FROM outbound_message o
                  WHERE o.dedupe_key LIKE 'handoff:' || h.id || ':card:' || %(a)s || '%%'
                    AND o.status = 'sent')
            ORDER BY h.requested_at
            """, {"a": actor, "admin": who[0] == "admin"}).fetchall()]
    sent = 0
    minutes = handoff.accept_minutes()
    for handoff_id in ids:
        packet = handoff_alert.load_packet(conn, turn.agency_id, handoff_id, gazetteer)
        payload = handoff_alert.card(packet, minutes, escalated=packet.escalated)
        try:
            status = outbound.send_message(
                conn, client, turn.agency_id, turn.conversation_id, turn.sender_wa_id, payload,
                f"handoff:{handoff_id}:card:{actor}:{turn.id}", ai_generated=False).status
        except whatsapp.PermanentSendError as exc:
            logger.warning("Lead card resend refused (%s)", exc.code)
            continue
        sent += status == "sent"
    return sent
