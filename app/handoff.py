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
broker with the lead code. Never log names, numbers or message text.
"""

import os
import re
from dataclasses import dataclass
from urllib.parse import quote

from psycopg.types.json import Jsonb

from app import queue, whatsapp
from app.cards import ENGLISH, ROMAN_URDU

STATEMENT_TIMEOUT = "5s"
ACCEPT_MINUTES_DEFAULT = 15   # RFC 7.6 "X min (config, e.g. 15)"; owner 2026-10-15
ALERT_JOB = "handoff_alert"
TIMEOUT_JOB = "handoff_accept_timeout"
REASONS = ("visit", "negotiation", "agent", "legal", "frustration")
OPENED, ALREADY_OPEN, NO_AGENT = "opened", "already_open", "no_agent"

# Customer wording (R6 / Demo Scope scenario 5; awaits owner approval).
TEXT = {
    ROMAN_URDU: {
        "connect": ("Main Manzil ka AI assistant hoon -- aapko hamare consultant {name} se "
                    "connect kar raha hoon. Woh jald rabta karenge."),
        "property": "Property: {refs}",
        "lead": "Aapka reference: {code}",
        "chat": "Chat with {name}",
        "chat_short": "Agent se chat",
    },
    ENGLISH: {
        "connect": ("I'm Manzil's AI assistant -- I'm connecting you with our consultant "
                    "{name}. They will contact you soon."),
        "property": "Property: {refs}",
        "lead": "Your reference: {code}",
        "chat": "Chat with {name}",
        "chat_short": "Chat with agent",
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
    last = conn.execute(
        "SELECT id FROM message WHERE conversation_id = %s AND direction = 'inbound' "
        "ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
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
    lines = [words["connect"].format(name=name)]
    if opened.refs:
        lines.append(words["property"].format(refs=", ".join(opened.refs)))
    lines.append(words["lead"].format(code=opened.lead_code))
    button = words["chat"].format(name=name)
    if len(button) > whatsapp.CTA_DISPLAY_TEXT_MAX:
        button = words["chat_short"]
    url = f"https://wa.me/{opened.agent.number}?text={quote('Ref ' + opened.lead_code)}"
    return whatsapp.cta_url("\n".join(lines), button, url)
