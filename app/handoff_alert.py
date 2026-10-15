"""The broker's lead card and the accept timer (Task 11.1 step 3, component C13; R6, RFC 7,
R-29, R-30; docs/HANDOFF.md).

handoff_alert job (queued by app.handoff.open_handoff in the handoff's own transaction):
the lead packet is read from the database, the L3 summary is written once (stored in
lead.summary), and the card goes to the assigned agent's own conversation:

- the card (R-30 order): customer name / number, requirements, the confirmed listings
  (re-fetched public rows), the last customer message, the reason, the summary; cover
  image header; [Accept] [Pass] [Called] (ho:accept|pass|called:{handoff_id}). Staff cards
  are English (D15). A deterministic completeness check logs any missing field; the card
  still goes out with "not stated".
- the agent's 24 h window closed -> the approved new_lead_alert template (configured name,
  language and body parameters); the card follows when they next write (step 4).
- nothing reaches them (template refused, Meta error) -> escalate to ADMIN at once.

handoff_accept_timeout job (R-29, owner decision 2026-10-16): still pending after
HANDOFF_ACCEPT_MINUTES -> the admins get the card too (escalated_at); the broker keeps it.
While pending, the assigned agent or any active admin may Accept (step 4). A second
timeout only logs: no callback time is promised to the customer.

L3 (RFC 7.3): one structured call through the engine's provider, primary then failover,
one usage_event per attempt (operation L3). The summary sees no phone number; it must pass
the grounding check against the lead's facts, else the card goes without it. Never log
names, numbers, message text or the summary.
"""

import dataclasses
import logging
import os
import time
from contextlib import closing
from dataclasses import dataclass

import psycopg

from app import (
    cards,
    grounding,
    handoff,
    outbound,
    queue,
    references,
    render,
    search,
    whatsapp,
)
from app.db import tenant_transaction
from app.llm import ProviderError, Usage

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
OPERATION = "L3"
SUMMARY_TIMEOUT_SECONDS = 8.0
SUMMARY_CHARS = 300
LAST_MESSAGE_CHARS = 300
LISTING_LINES = 5
PERSISTENT_SQLSTATE_CLASSES = ("22", "23", "42")
ACCEPT, PASS, CALLED = "accept", "pass", "called"
BUTTONS = ((ACCEPT, "Accept"), (PASS, "Pass"), (CALLED, "Called"))
NOT_STATED = "not stated"
SUMMARY_SYSTEM = (
    "You write a one-sentence note for a real-estate broker about a customer lead. Use only "
    "the facts in the message; never add a price, size, place, listing code, date or "
    "promise that is not there. No phone numbers. At most 200 characters, English. "
    "Return JSON {\"summary\": \"...\"}."
)


# The submitted new_lead_alert (Task 1.3, seen in WhatsApp Manager 2026-10-16): "A customer
# has requested agent assistance for property {{1}}. Inquiry reference: {{2}}. Please
# review this customer inquiry." -> {{1}} = property, {{2}} = lead.
DEFAULT_TEMPLATE_PARAMS = "property,lead"
NO_PROPERTY = "not yet chosen"


def template_settings():
    """(name, language, [parameter fields]) of the broker alert template: LEAD_ALERT_TEMPLATE
    (default new_lead_alert), LEAD_ALERT_LANGUAGE (default en) and LEAD_ALERT_PARAMS, a
    comma list of lead, reason, customer, wants, property in the template's {{n}} order
    (default property,lead: the submitted template)."""
    name = os.environ.get("LEAD_ALERT_TEMPLATE", "").strip() or "new_lead_alert"
    language = os.environ.get("LEAD_ALERT_LANGUAGE", "").strip() or "en"
    raw = os.environ.get("LEAD_ALERT_PARAMS", "").strip() or DEFAULT_TEMPLATE_PARAMS
    params = [p.strip() for p in raw.split(",") if p.strip()]
    unknown = set(params) - {"lead", "reason", "customer", "wants", "property"}
    if unknown:
        raise ValueError(f"LEAD_ALERT_PARAMS: unknown field(s) {sorted(unknown)}")
    return name, language, params


def tap_id(action, handoff_id):
    return f"ho:{action}:{handoff_id}"


@dataclass(frozen=True)
class Packet:
    handoff_id: str
    status: str
    conversation_id: str
    lead_id: str
    lead_code: str
    reason: str | None
    summary: str | None
    customer_number: str
    customer_name: str | None
    wants: str | None
    listings: tuple            # cards.Listing, re-fetched public rows
    positions: dict            # code -> position in the latest shown search
    last_message: str | None
    agent: handoff.Agent | None
    escalated: bool
    request: object            # search.Request of the snapshot


def load_packet(conn, agency_id, handoff_id, gazetteer=None):
    """The lead packet of a handoff, or None when it does not exist."""
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = conn.execute(
            """
            SELECT h.status, h.conversation_id::text, l.id::text, l.code, l.reason, l.summary,
                   c.customer_wa_id, l.requirement_snapshot,
                   COALESCE(m.transcript, m.text_body), s.id, s.name, s.whatsapp_number,
                   h.escalated_at IS NOT NULL,
                   (SELECT e.payload -> 'contact' -> 'profile' ->> 'name'
                    FROM message im JOIN inbound_event e ON e.id = im.inbound_event_id
                    WHERE im.conversation_id = h.conversation_id AND im.direction = 'inbound'
                    ORDER BY im.created_at DESC LIMIT 1)
            FROM handoff h
            JOIN lead l ON l.id = h.lead_id
            JOIN conversation c ON c.id = h.conversation_id
            LEFT JOIN message m ON m.id = l.last_customer_message_id
            LEFT JOIN staff s ON s.id = h.assigned_staff_id
            WHERE h.id = %s
            """, (handoff_id,)).fetchone()
        if row is None:
            return None
        codes = [c for (c,) in conn.execute(
            "SELECT p.listing_code FROM lead_property lp JOIN property p ON p.id = lp.property_id"
            " WHERE lp.lead_id = %s ORDER BY lp.created_at, p.listing_code", (row[2],))]
        positions = {i.code: i.position for i in references.shown_items(conn, row[1])
                     if i.position}
    (status, conversation_id, lead_id, code, reason, summary, number, snapshot, last,
     staff_id, staff_name, staff_number, escalated, name) = row
    request = search.Request.from_slots(snapshot or {})
    listings = tuple(cards.load_listings(conn, agency_id, codes)) if codes else ()
    agent = handoff.Agent(str(staff_id), staff_name, handoff.digits(staff_number)) \
        if staff_id else None
    return Packet(handoff_id, status, conversation_id, lead_id, code, reason, summary, number,
                  (name or "").strip() or None, render.requirements_line(request, gazetteer),
                  listings, {c: p for c, p in positions.items() if c in codes},
                  (last or "").strip() or None, agent, escalated, request)


def missing_fields(packet):
    """R-30 completeness: the lead fields a card needs; [] when complete."""
    missing = []
    if not packet.customer_number:
        missing.append("customer")
    if not packet.wants and not packet.listings:
        missing.append("requirements")
    if not packet.last_message:
        missing.append("last_message")
    if not packet.reason:
        missing.append("reason")
    return missing


def _clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def listing_line(listing, position):
    parts = [f"#{position} {listing.code}" if position else listing.code]
    if listing.price_pkr:
        parts.append(cards.price_text(listing.price_pkr, listing.purpose).removeprefix("PKR "))
    size = cards.area_text(listing)
    if size:
        parts.append(size)
    if listing.location:
        parts.append(listing.location)
    return " · ".join(parts)


def card_body(packet, minutes, *, escalated=False):
    """The card text (R-30 order), at most 1024 characters."""
    customer = f"+{packet.customer_number}"
    if packet.customer_name:
        customer = f"{_clip(packet.customer_name, 40)} · {customer}"
    lines = [f"New lead {packet.lead_code} ({packet.reason or NOT_STATED})",
             f"Customer: {customer}",
             f"Wants: {packet.wants or NOT_STATED}"]
    shown = [listing_line(li, packet.positions.get(li.code))
             for li in packet.listings[:LISTING_LINES]]
    if len(packet.listings) > LISTING_LINES:
        shown.append(f"+{len(packet.listings) - LISTING_LINES} more")
    lines.append("Property: " + ("; ".join(shown) if shown else "none chosen"))
    last = _clip(packet.last_message, LAST_MESSAGE_CHARS) if packet.last_message else None
    lines.append(f'Last message: "{last}"' if last else f"Last message: {NOT_STATED}")
    if packet.summary:
        lines.append(f"Summary: {packet.summary}")
    lines.append(f"Not accepted within {minutes} min: please take it." if escalated
                 else f"Tap Accept within {minutes} min.")
    body = "\n".join(lines)
    if len(body) > whatsapp.INTERACTIVE_BODY_MAX and packet.summary:
        return card_body(_without_summary(packet), minutes, escalated=escalated)
    return body[:whatsapp.INTERACTIVE_BODY_MAX]


def _without_summary(packet):
    return dataclasses.replace(packet, summary=None)


def card(packet, minutes, *, escalated=False):
    body = card_body(packet, minutes, escalated=escalated)
    buttons = [(tap_id(action, packet.handoff_id), title) for action, title in BUTTONS]
    cover = next((li.cover_url for li in packet.listings if li.cover_url), None)
    if cover:
        return whatsapp.reply_buttons(body, buttons, image_header={"link": cover})
    return whatsapp.reply_buttons(body, buttons, header=f"Lead {packet.lead_code}")


def template_alert(packet):
    name, language, fields = template_settings()
    values = {
        "lead": packet.lead_code,
        "reason": packet.reason or NOT_STATED,
        "customer": f"+{packet.customer_number}",
        "wants": _clip(packet.wants or NOT_STATED, 200),
        "property": ", ".join(li.code for li in packet.listings) or NO_PROPERTY,
    }
    return whatsapp.template(name, language, [values[f] for f in fields])


# -- L3 summary ------------------------------------------------------------------------------

def _summary_input(packet):
    lines = [f"Reason: {packet.reason or NOT_STATED}",
             f"Wants: {packet.wants or NOT_STATED}"]
    lines += [f"Listing: {listing_line(li, packet.positions.get(li.code))}"
              for li in packet.listings[:LISTING_LINES]]
    if packet.last_message:
        lines.append(f"Customer's last message: {_clip(packet.last_message, 500)}")
    return "\n".join(lines)


def summarize(conn, job, packet, engine, gazetteer, clock=time.monotonic):
    """The checked summary text, or None (no engine, every attempt failed, or it stated a
    fact the lead does not support)."""
    if engine is None:
        return None
    text = _summary_input(packet)
    attempts = [(engine.primary_model, "primary"), (engine.failover_model, "failover")]
    for model, label in attempts:
        began = clock()
        try:
            result = engine.provider.summarize(
                model, system=SUMMARY_SYSTEM, text=text, timeout_s=SUMMARY_TIMEOUT_SECONDS,
                thinking_level=engine.thinking_level)
        except ProviderError as exc:
            _meter(conn, job, packet, label, model, "failed", exc.code, Usage(), began, clock)
            if exc.failover and label == "primary":
                continue
            return None
        _meter(conn, job, packet, label, result.model_version or model, "succeeded", "text",
               result.usage, began, clock)
        return _checked(result.text, packet, gazetteer)
    return None


def _checked(summary, packet, gazetteer):
    summary = _clip(summary, SUMMARY_CHARS)
    if not summary:
        return None
    facts = grounding.facts_for(
        packet.listings, positions=[packet.positions.get(li.code) for li in packet.listings
                                    if packet.positions.get(li.code)],
        request=packet.request, gazetteer=gazetteer)
    found = grounding.check_payloads([whatsapp.text(summary)], facts, gazetteer)
    if found:
        logger.warning("Lead %s summary held back: unsupported %s", packet.lead_code,
                       ", ".join(sorted({v.kind for v in found})))
        return None
    return summary


def _meter(conn, job, packet, label, model, outcome, detail, usage, began, clock):
    key = f"handoff:{packet.handoff_id}:job:{job.id}:{job.attempts}:{label}"
    logger.info("LLM call L3 handoff %s %s %s: %s/%s, %d ms", packet.handoff_id, label, model,
                outcome, detail, int((clock() - began) * 1000))
    try:
        with tenant_transaction(conn, job.agency_id):
            conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            conn.execute(
                """
                INSERT INTO usage_event (
                    agency_id, job_id, provider, model, operation, request_key,
                    input_tokens, output_tokens, cached_input_tokens, thinking_tokens,
                    outcome, outcome_detail
                )
                VALUES (current_agency_id(), %s, 'gemini', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (job.id, model, OPERATION, key, usage.input_tokens, usage.output_tokens,
                 usage.cached_input_tokens, usage.thinking_tokens, outcome, detail[:64]),
            )
    except psycopg.Error as exc:
        if (exc.sqlstate or "")[:2] not in PERSISTENT_SQLSTATE_CLASSES:
            raise
        raise queue.PermanentJobError(f"usage_event_{exc.sqlstate.lower()}") from None


# -- sending ---------------------------------------------------------------------------------

def admins(conn, agency_id):
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        rows = conn.execute("SELECT id, name, whatsapp_number FROM staff WHERE is_active "
                            "AND role = 'admin' ORDER BY created_at, id").fetchall()
    return [handoff.Agent(str(r[0]), r[1], handoff.digits(r[2])) for r in rows]


def send_card(conn, client, agency_id, packet, agent, minutes, *, escalated=False):
    """The card to one agent's own conversation, else the template; True when either was
    sent."""
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        conversation_id = handoff.staff_conversation(conn, agent.number)
    key = f"handoff:{packet.handoff_id}:card:{agent.staff_id}"
    try:
        status = outbound.send_message(
            conn, client, agency_id, conversation_id, agent.number,
            card(packet, minutes, escalated=escalated), key, ai_generated=False).status
    except whatsapp.PermanentSendError as exc:
        logger.warning("Lead %s card refused (%s)", packet.lead_code, exc.code)
        status = "refused"
    if status == "sent":
        return True
    try:
        status = outbound.send_message(
            conn, client, agency_id, conversation_id, agent.number, template_alert(packet),
            f"handoff:{packet.handoff_id}:alert:{agent.staff_id}", ai_generated=False).status
    except whatsapp.PermanentSendError as exc:
        logger.warning("Lead %s alert template refused (%s)", packet.lead_code, exc.code)
        return False
    return status == "sent"


def _client(conn, agency_id, client_factory):
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        pnid = conn.execute("SELECT meta_phone_number_id FROM agency WHERE id = %s",
                            (agency_id,)).fetchone()[0]
    return closing(client_factory(pnid))


def alert(conn, job, client_factory, engine=None, gazetteer=None):
    """The handoff_alert job: the summary once, then the card to the assigned agent;
    nothing delivered -> escalate to the admins at once."""
    handoff_id = job.payload["handoff_id"]
    packet = load_packet(conn, job.agency_id, handoff_id, gazetteer)
    if packet is None or packet.status != "pending":
        return  # accepted / closed meanwhile: nothing to announce
    if packet.summary is None:
        try:
            summary = summarize(conn, job, packet, engine, gazetteer)
        except Exception as exc:  # noqa: BLE001 - the card never waits on the summary
            logger.warning("Lead %s summary failed (%s); card without it", packet.lead_code,
                           type(exc).__name__)
            summary = None
        if summary is not None:
            with tenant_transaction(conn, job.agency_id):
                conn.execute("UPDATE lead SET summary = %s, updated_at = now() WHERE id = %s "
                             "AND summary IS NULL", (summary, packet.lead_id))
            packet = load_packet(conn, job.agency_id, handoff_id, gazetteer)
    missing = missing_fields(packet)
    if missing:
        logger.warning("Lead %s card incomplete: %s", packet.lead_code, ", ".join(missing))
    minutes = handoff.accept_minutes()
    with _client(conn, job.agency_id, client_factory) as client:
        delivered = packet.agent is not None and send_card(
            conn, client, job.agency_id, packet, packet.agent, minutes)
        if not delivered:
            logger.warning("Lead %s not delivered to its agent; escalating", packet.lead_code)
            escalate(conn, client, job.agency_id, packet, minutes)


def escalate(conn, client, agency_id, packet, minutes):
    """R-29: the admins get the card too; the assigned agent keeps it. True when at least
    one admin was reached."""
    with tenant_transaction(conn, agency_id):
        conn.execute(
            "UPDATE handoff SET escalated_at = COALESCE(escalated_at, now()), "
            "updated_at = now() WHERE id = %s AND status = 'pending'", (packet.handoff_id,))
    reached = False
    for agent in admins(conn, agency_id):
        if packet.agent is not None and agent.staff_id == packet.agent.staff_id:
            continue    # the admin already holds it
        reached |= send_card(conn, client, agency_id, packet, agent, minutes, escalated=True)
    if not reached:
        logger.error("Lead %s reached no admin; it stays pending", packet.lead_code)
    return reached


def accept_timeout(conn, job, client_factory, gazetteer=None):
    """The handoff_accept_timeout job: round 1 escalates to the admins and starts round 2;
    round 2 only logs. A handoff no longer pending makes it a no-op."""
    handoff_id, round_ = job.payload["handoff_id"], int(job.payload.get("round", 1))
    with tenant_transaction(conn, job.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = conn.execute("SELECT status FROM handoff WHERE id = %s FOR UPDATE",
                           (handoff_id,)).fetchone()
        if row is None or row[0] != "pending":
            return
        minutes = handoff.accept_minutes()
        if round_ == 1:
            queue.enqueue_job(conn, handoff.TIMEOUT_JOB, f"handoff-timeout:{handoff_id}:2",
                              {"handoff_id": handoff_id, "round": 2},
                              delay_seconds=minutes * 60)
    if round_ != 1:
        logger.warning("Handoff %s still pending after the admin escalation", handoff_id)
        return
    packet = load_packet(conn, job.agency_id, handoff_id, gazetteer)
    with _client(conn, job.agency_id, client_factory) as client:
        escalate(conn, client, job.agency_id, packet, minutes)
