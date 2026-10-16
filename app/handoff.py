"""Customer -> human handoff (Task 11.1, component C13; R6, RFC 7, R-28 - R-31;
docs/HANDOFF.md).

open() is the one write of a handoff request, in one tenant transaction (the outbox rule,
RFC 8): the open lead (upserted), the confirmed listing(s) in lead_property, the pending
handoff with its accept deadline, control_state handoff_pending (mode 'human': PENDING
behaves as OWNED, R-28), and the jobs that alert the broker and run the accept timer.
A repeated request while a handoff is open changes nothing and returns it again.

route() picks who gets the lead: the listing's assigned broker, then a broker whose
areas_covered contains the listing's place, then the active broker with the fewest open
handoffs, then an active admin (R-29: the demo has one broker; ADMIN covers the rest).

The customer message (customer_message) is backend wording around backend values only:
an honest AI disclosure, never the broker's voice (R6, FR-C10), and a wa.me CTA to the
broker with the lead code.

forward_turn (Task 11.2, R-28 / RFC 9b): while a handoff is open (pending or accepted)
the AI is silent; each customer message (a voice note as its transcript) goes one way
to the assigned staff member's own conversation, and the customer gets one holding reply
per handoff (its dedupe key is the handoff's, so it is sent at most once ever). An
accepted handoff's 48 h release moves with every forwarded message. Never log names,
numbers or message text.
"""

import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import quote

from psycopg.types.json import Jsonb

from app import outbound, queue, render, whatsapp
from app.cards import ENGLISH, ROMAN_URDU
from app.db import tenant_transaction

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
ACCEPT_MINUTES_DEFAULT = 15   # RFC 7.6 "X min (config, e.g. 15)"; owner 2026-10-15
ALERT_JOB = "handoff_alert"
TIMEOUT_JOB = "handoff_accept_timeout"
RELEASE_JOB = "handoff_release"
RELEASE_HOURS = 48            # R6 / RFC 7: release after 48 h without activity
FORWARD_CHARS = 3500          # a forward stays one WhatsApp text (4096)
REASONS = ("visit", "negotiation", "agent", "legal", "frustration")
OPENED, ALREADY_OPEN, NO_AGENT = "opened", "already_open", "no_agent"

# Customer wording (R6 / Demo Scope scenario 5; awaits owner approval).
TEXT = {
    ROMAN_URDU: {
        "connect": ("Main Manzil ka AI assistant hoon — aapko hamare consultant *{name}* se "
                    "connect kar raha hoon. Woh jald rabta karenge."),
        "property": "🏠 Property: *{refs}*",
        "lead": "🔖 Aapka reference: *{code}*",
        "chat": "Chat with {name}",
        "chat_short": "Agent se chat",
        # Task 11.2 holding reply (R6 "Ahmed ko aapka message bhej diya hai")
        "holding": "✅ *{name}* ko aapka message bhej diya hai; woh jald jawab denge.",
    },
    ENGLISH: {
        "connect": ("I'm Manzil's AI assistant — I'm connecting you with our consultant "
                    "*{name}*. They will contact you soon."),
        "property": "🏠 Property: *{refs}*",
        "lead": "🔖 Your reference: *{code}*",
        "chat": "Chat with {name}",
        "chat_short": "Chat with agent",
        "holding": "✅ I've passed your message to *{name}*; they will reply soon.",
    },
}


@dataclass(frozen=True)
class Agent:
    staff_id: str
    name: str
    number: str     # digits only


@dataclass(frozen=True)
class Opened:
    status: str                 # opened | already_open | no_agent
    handoff_id: str | None = None
    lead_code: str | None = None
    agent: Agent | None = None
    refs: tuple = ()            # "#2 · DH6-102" of the confirmed listings


def accept_minutes():
    """HANDOFF_ACCEPT_MINUTES (1-1440), default 15."""
    raw = os.environ.get("HANDOFF_ACCEPT_MINUTES", "").strip()
    if not raw:
        return ACCEPT_MINUTES_DEFAULT
    value = int(raw)
    if not 1 <= value <= 1440:
        raise ValueError("HANDOFF_ACCEPT_MINUTES must be 1-1440")
    return value


def digits(number):
    return re.sub(r"\D", "", number or "")


def _agent(row):
    return Agent(str(row[0]), row[1], digits(row[2])) if row else None


_LOAD = ("(SELECT count(*) FROM handoff h WHERE h.assigned_staff_id = s.id "
         "AND h.status IN ('pending', 'accepted'))")


def route(conn, property_ids):
    """Inside a tenant transaction: the Agent for a lead on these listings, or None."""
    if property_ids:
        row = conn.execute(
            """
            SELECT s.id, s.name, s.whatsapp_number FROM property p
            JOIN staff s ON s.id = p.assigned_broker_id
            WHERE p.id = %s AND s.is_active AND s.role IN ('broker', 'admin')
            """, (property_ids[0],)).fetchone()
        if row:
            return _agent(row)
        row = conn.execute(
            f"""
            SELECT s.id, s.name, s.whatsapp_number FROM property p
            JOIN location l ON l.id = p.location_id
            LEFT JOIN location lp ON lp.id = l.parent_id
            JOIN staff s ON s.is_active AND s.role = 'broker'
             AND s.areas_covered && ARRAY[l.id, l.parent_id, lp.parent_id]
            WHERE p.id = %s
            ORDER BY {_LOAD}, s.created_at, s.id LIMIT 1
            """, (property_ids[0],)).fetchone()
        if row:
            return _agent(row)
    for role in ("broker", "admin"):
        row = conn.execute(
            f"""
            SELECT s.id, s.name, s.whatsapp_number FROM staff s
            WHERE s.is_active AND s.role = %s
            ORDER BY {_LOAD}, s.created_at, s.id LIMIT 1
            """, (role,)).fetchone()
        if row:
            return _agent(row)
    return None


def _open_handoff(conn, conversation_id):
    return conn.execute(
        """
        SELECT h.id::text, l.code, s.id, s.name, s.whatsapp_number FROM handoff h
        JOIN lead l ON l.id = h.lead_id
        LEFT JOIN staff s ON s.id = h.assigned_staff_id
        WHERE h.conversation_id = %s AND h.status IN ('pending', 'accepted')
        """, (conversation_id,)).fetchone()


def _refs(conn, lead_id, positions):
    rows = conn.execute(
        "SELECT p.listing_code FROM lead_property lp JOIN property p ON p.id = lp.property_id "
        "WHERE lp.lead_id = %s ORDER BY lp.created_at, p.listing_code", (lead_id,)).fetchall()
    return tuple(f"#{positions[c]} · {c}" if positions.get(c) else c for (c,) in rows)


def open_handoff(conn, conversation_id, items, reason):
    """Inside the caller's tenant transaction. items: the confirmed references.Item(s),
    possibly none (a request with no listing shown). Returns an Opened."""
    if reason not in REASONS:
        raise ValueError("unknown handoff reason")
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
    conn.execute("SELECT id FROM conversation WHERE id = %s FOR UPDATE", (conversation_id,))
    positions = {i.code: i.position for i in items}
    existing = _open_handoff(conn, conversation_id)
    if existing is not None:
        handoff_id, code, *staff = existing
        lead_id = conn.execute("SELECT lead_id FROM handoff WHERE id = %s",
                               (handoff_id,)).fetchone()[0]
        return Opened(ALREADY_OPEN, handoff_id, code, _agent(staff if staff[0] else None),
                      _refs(conn, lead_id, positions))
    property_ids = [i.property_id for i in items]
    agent = route(conn, property_ids)
    if agent is None:
        return Opened(NO_AGENT)
    state = conn.execute(
        """
        SELECT rs.slots,
               (SELECT max(version) FROM requirement_patch_log pl
                WHERE pl.conversation_id = rs.conversation_id AND pl.version = rs.version)
        FROM requirement_state rs WHERE rs.conversation_id = %s
        """, (conversation_id,)).fetchone()
    slots, version = state if state else ({}, None)
    # The customer's own latest words (text or voice), not a tapped button's title.
    last = conn.execute(
        "SELECT id FROM message WHERE conversation_id = %s AND direction = 'inbound' "
        "ORDER BY message_type IN ('text', 'audio') DESC, created_at DESC LIMIT 1",
        (conversation_id,)).fetchone()
    params = {"slots": Jsonb(slots), "version": version, "last": last[0] if last else None,
              "reason": reason, "staff": agent.staff_id, "c": conversation_id}
    lead = conn.execute(
        "SELECT id, code FROM lead WHERE conversation_id = %s AND status = 'open' FOR UPDATE",
        (conversation_id,)).fetchone()
    if lead is None:
        lead = conn.execute(
            """
            INSERT INTO lead (agency_id, conversation_id, lead_number, requirement_version,
                              requirement_snapshot, last_customer_message_id, reason,
                              assigned_staff_id)
            VALUES (current_agency_id(), %(c)s, next_lead_number(), %(version)s, %(slots)s,
                    %(last)s, %(reason)s, %(staff)s)
            RETURNING id, code
            """, params).fetchone()
    else:
        conn.execute(
            """
            UPDATE lead SET requirement_version = %(version)s,
                requirement_snapshot = %(slots)s, last_customer_message_id = %(last)s,
                reason = %(reason)s, assigned_staff_id = %(staff)s, updated_at = now()
            WHERE id = %(lead)s
            """, {**params, "lead": lead[0]})
    lead_id, code = lead
    for property_id in property_ids:
        conn.execute(
            "INSERT INTO lead_property (agency_id, lead_id, property_id) "
            "VALUES (current_agency_id(), %s, %s) ON CONFLICT DO NOTHING",
            (lead_id, property_id))
    minutes = accept_minutes()
    handoff_id = conn.execute(
        """
        INSERT INTO handoff (agency_id, conversation_id, lead_id, assigned_staff_id,
                             accept_due_at)
        VALUES (current_agency_id(), %s, %s, %s, now() + make_interval(mins => %s))
        RETURNING id::text
        """, (conversation_id, lead_id, agent.staff_id, minutes)).fetchone()[0]
    # R-28: PENDING behaves as OWNED; mode follows (conversation_control_state_gate_check).
    conn.execute(
        "UPDATE conversation SET control_state = 'handoff_pending', mode = 'human', "
        "updated_at = now() WHERE id = %s", (conversation_id,))
    payload = {"handoff_id": handoff_id}
    queue.enqueue_job(conn, ALERT_JOB, f"handoff-alert:{handoff_id}", payload)
    queue.enqueue_job(conn, TIMEOUT_JOB, f"handoff-timeout:{handoff_id}:1",
                      {**payload, "round": 1}, delay_seconds=minutes * 60)
    return Opened(OPENED, handoff_id, code, agent, _refs(conn, lead_id, positions))



def first_name(name):
    """The broker's name as the customer sees it: the staff name without "(DEMO)"-style
    notes in brackets."""
    return re.sub(r"\s*\([^)]*\)", "", name or "").strip() or None


def customer_message(opened, language):
    """The customer's handoff reply: disclosure + listing echo + lead reference, with a
    wa.me CTA to the assigned staff member ("Ref L-07" pre-filled)."""
    words = TEXT.get(language, TEXT[ROMAN_URDU])
    name = first_name(opened.agent.name) or words["chat_short"]
    lines = [words["connect"].format(name=name), ""]
    if opened.refs:
        lines.append(words["property"].format(refs=", ".join(opened.refs)))
    lines.append(words["lead"].format(code=opened.lead_code))
    button = words["chat"].format(name=name)
    if len(button) > whatsapp.CTA_DISPLAY_TEXT_MAX:
        button = words["chat_short"]
    url = f"https://wa.me/{opened.agent.number}?text={quote('Ref ' + opened.lead_code)}"
    return whatsapp.cta_url("\n".join(lines), button, url)


# -- Task 11.2: forwarding and the holding reply ---------------------------------------------

@dataclass(frozen=True)
class Holder:
    handoff_id: str
    status: str                 # pending | accepted
    lead_code: str
    agent: Agent | None


def holder(conn, conversation_id):
    """Inside a tenant transaction: the conversation's open handoff, or None."""
    row = conn.execute(
        """
        SELECT h.id::text, h.status, l.code, s.id, s.name, s.whatsapp_number FROM handoff h
        JOIN lead l ON l.id = h.lead_id
        LEFT JOIN staff s ON s.id = h.assigned_staff_id
        WHERE h.conversation_id = %s AND h.status IN ('pending', 'accepted')
        """, (conversation_id,)).fetchone()
    if row is None:
        return None
    return Holder(row[0], row[1], row[2], _agent(row[3:]) if row[3] else None)


def staff_conversation(conn, number):
    """Inside a tenant transaction: the id of the staff member's own conversation with the
    business number (created when they never wrote), so their 24 h window and gate apply
    to what they are sent, never the customer's."""
    return conn.execute(
        """
        INSERT INTO conversation (agency_id, customer_wa_id) VALUES (current_agency_id(), %s)
        ON CONFLICT (agency_id, customer_wa_id) DO UPDATE SET updated_at = conversation.updated_at
        RETURNING id
        """, (number,)).fetchone()[0]


def forward_text(code, customer, message):
    """Broker-facing (English, D15): "💬 *L-07* · +923000000102" and the message below it."""
    label = {"text": "", "audio": " (voice)"}.get(message.type, f" ({message.type})")
    body = (message.text or "").strip() or f"[{message.type}]"
    if len(body) > FORWARD_CHARS:
        body = body[:FORWARD_CHARS - 1] + "…"
    return f"💬 *{code}* · +{customer}{label}\n{body}"


def forward_turn(conn, client, turn, transcribe=None):
    """Forward a gated customer turn to whoever holds its handoff and send the one holding
    reply. Returns False (nothing done) when the conversation has no open handoff.
    transcribe(turn) -> turn with voice notes as text; called only when there is one."""
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        held = holder(conn, turn.conversation_id)
        if held is None or held.agent is None:
            return False
        staff_conv = staff_conversation(conn, held.agent.number)
        if held.status == "accepted":
            touch_release(conn, held.handoff_id)
    if transcribe is not None:
        turn = transcribe(turn)
    for seq, message in enumerate(turn.messages):
        try:
            result = outbound.send_message(
                conn, client, turn.agency_id, staff_conv, held.agent.number,
                whatsapp.text(forward_text(held.lead_code, turn.sender_wa_id, message)),
                f"turn:{turn.id}:fwd:{seq}", ai_generated=False)
        except whatsapp.PermanentSendError as exc:
            result = outbound.SendResult(f"failed:{exc.code}")
        if result.status != "sent":
            # Recorded on its outbound_message row; the holding reply still goes out.
            logger.warning("Handoff %s: forward not sent (%s)", held.handoff_id,
                           result.status)
    language = render.detect_language(" ".join(m.text or "" for m in turn.messages))
    words = TEXT.get(language, TEXT[ROMAN_URDU])
    name = first_name(held.agent.name) or words["chat_short"]
    sent = outbound.send_message(
        conn, client, turn.agency_id, turn.conversation_id, turn.sender_wa_id,
        whatsapp.text(words["holding"].format(name=name)),
        f"handoff:{held.handoff_id}:holding", ai_generated=False)
    if sent.status == "sent":
        with tenant_transaction(conn, turn.agency_id):
            conn.execute(
                "UPDATE handoff SET holding_reply_sent_at = now(), updated_at = now() "
                "WHERE id = %s AND holding_reply_sent_at IS NULL", (held.handoff_id,))
    return True


def touch_release(conn, handoff_id):
    """Inside a tenant transaction: an accepted handoff is released 48 h after its latest
    activity; each move queues a release job keyed by the new deadline (older jobs find a
    later deadline and do nothing)."""
    due = conn.execute(
        """
        UPDATE handoff SET release_due_at = now() + make_interval(hours => %s),
            updated_at = now()
        WHERE id = %s AND status = 'accepted'
        RETURNING extract(epoch FROM release_due_at)::bigint
        """, (RELEASE_HOURS, handoff_id)).fetchone()
    if due is not None:
        queue.enqueue_job(conn, RELEASE_JOB, f"handoff-release:{handoff_id}:{due[0]}",
                          {"handoff_id": handoff_id}, delay_seconds=RELEASE_HOURS * 3600)
