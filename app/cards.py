"""Property result cards (Task 5.2; R4/R7/R9 D9 and D14): public listings -> WhatsApp.

Formats (docs/DECISIONS.md, Task 5.2):

- 1 result: one reply-buttons message, the cover as image header and the full
  details in the body; three buttons: More photos, Visit / Details, Similar.
- 2+ results: the numbered summary is the carousel's main body, so a reply such
  as "2" works without a tap; the carousel holds the top 5 that have a cover, in
  rank order. A top-5 listing without a cover keeps its number in the summary
  ("photos coming soon") and follows as a text-header card: never dropped.
  Fewer than 2 covers means no carousel, only individual cards.
- Individual cards (the per-contact fallback of migration 011, a carousel Meta
  refuses, or too few covers): the summary as text, then one reply-buttons card
  per listing (image header where a cover exists) with the same two buttons as
  a carousel card.
- More than 5: the rest (up to 10 more) in a list message.

Sending (docs/DECISIONS.md, Task 5.2):

- A turn's payloads are built once and stored on the turn (conversation_turn.
  reply_plan, migration 011) before anything is sent, so a retried or resumed
  turn sends the same messages under the same "turn:{id}:{seq}" keys whatever
  changed in between (the fallback flag, a cover).
- A failed carousel is replaced by individual cards built only from the stored
  carousel payload (same summary, card texts, images and buttons), so retries
  rebuild identical messages: in the same turn when Meta refuses it at once
  ("turn:{id}:{seq}:fallback:{n}"), or by one carousel_fallback job when a
  status webhook reports it failed after Meta accepted it and before any
  delivery ("carousel-fallback:{outbound id}:{n}"). Cards are never re-fallen
  back, and an ambiguous ('unknown') carousel is never resent.

Listings come from v_public_listing only (active, customer-safe columns); the
cover is the card derivative's public URL (Task 4.1 / 4.4). Never log message
text or phone numbers.
"""

import logging
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal

from psycopg.types.json import Jsonb

from app import outbound, whatsapp
from app.db import tenant_transaction
from app.queue import PermanentJobError, enqueue_job

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
FALLBACK_JOB_TYPE = "carousel_fallback"
SHOWN_MAX = 5
RESULTS_MAX = SHOWN_MAX + whatsapp.LIST_ROWS_MAX
NO_COVER = "photos coming soon"
UNITS = {"sq_yd": "sq yd", "sq_ft": "sq ft", "marla": "marla", "kanal": "kanal"}
STATUSES = {"ready": "Ready", "under_construction": "Under construction", "off_plan": "Off-plan"}


@dataclass(frozen=True)
class Listing:
    code: str
    purpose: str | None
    property_type: str | None
    price_pkr: int | None
    area_value: Decimal | None
    area_unit: str | None
    size_nominal_sqyd: Decimal | None
    bedrooms: int | None
    bathrooms: int | None
    construction_status: str | None
    location: str | None
    cover_url: str | None


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def load_listings(conn, agency_id, codes):
    """The public listings with these codes, in the order given; a code that is not a
    public (active) listing of this agency is left out, for the caller to report."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        rows = conn.execute(
            """
            SELECT l.listing_code, l.purpose, l.property_type, l.price_pkr, l.area_value,
                   l.area_unit, l.size_nominal_sqyd, l.bedrooms, l.bathrooms,
                   l.construction_status, COALESCE(loc.display_name, loc.name),
                   CASE WHEN m.card_image_key IS NOT NULL THEN m.public_url_card END
            FROM v_public_listing l
            LEFT JOIN location loc ON loc.id = l.location_id
            LEFT JOIN property_media m
                ON m.id = l.cover_media_id AND m.property_id = l.id
            WHERE l.listing_code = ANY(%s)
            """,
            (list(codes),),
        ).fetchall()
    by_code = {row[0]: Listing(*row) for row in rows}
    return [by_code[code] for code in codes if code in by_code]


# --- per-contact fallback (migration 011) -------------------------------------------

def fallback_status(conn, agency_id, conversation_id):
    """(enabled, set_at) of the conversation's carousel fallback flag."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        row = conn.execute(
            "SELECT carousel_fallback, carousel_fallback_set_at FROM conversation "
            "WHERE id = %s",
            (conversation_id,),
        ).fetchone()
    if row is None:
        raise LookupError("conversation not found")
    return row[0], row[1]


def set_fallback(conn, agency_id, conversation_id, enabled, staff_id):
    """Set or clear the flag; staff_id records who changed it."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        updated = conn.execute(
            """
            UPDATE conversation SET carousel_fallback = %s, carousel_fallback_set_at = now(),
                carousel_fallback_set_by = %s, updated_at = now()
            WHERE id = %s
            """,
            (enabled, staff_id, conversation_id),
        ).rowcount
    if updated != 1:
        raise LookupError("conversation not found")


# --- text -----------------------------------------------------------------------------

def _number(value):
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def price_text(price_pkr, purpose):
    if price_pkr is None:
        return None
    for divisor, unit in ((10**7, "crore"), (10**5, "lakh")):
        if price_pkr >= divisor:
            text = f"PKR {_number(round(Decimal(price_pkr) / divisor, 2))} {unit}"
            break
    else:
        text = f"PKR {int(price_pkr):,}"
    return text + (" / month" if purpose == "rent" else "")


def area_text(listing):
    if listing.area_value is None:
        return None
    unit = UNITS.get(listing.area_unit, listing.area_unit)
    text = f"{_number(listing.area_value)} {unit}"
    nominal = listing.size_nominal_sqyd
    if listing.area_unit == "sq_yd" and nominal is not None and nominal != listing.area_value:
        # Bahria nominal sizes (Task 4.3): the actual size, with the size as marketed.
        text += f" ({_number(nominal)} nominal)"
    return text


def rooms_text(listing):
    parts = []
    if listing.bedrooms is not None:
        parts.append(f"{listing.bedrooms} bed")
    if listing.bathrooms is not None:
        parts.append(f"{listing.bathrooms} bath")
    return " / ".join(parts) or None


def kind_text(listing):
    kind = (listing.property_type or "property").replace("_", " ").capitalize()
    if listing.purpose in ("sale", "rent"):
        kind += f" for {listing.purpose}"
    return kind


def status_text(listing):
    status = listing.construction_status
    return STATUSES.get(status, status.replace("_", " ").capitalize()) if status else None


def _join(parts):
    return " · ".join(part for part in parts if part)


def _fit(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def card_body(rank, listing):
    """Key facts in at most 160 characters and 2 line breaks (a carousel card body)."""
    lines = [
        f"#{rank} {listing.code} · {kind_text(listing)}",
        _join([listing.location, area_text(listing), rooms_text(listing)]),
        _join([price_text(listing.price_pkr, listing.purpose), status_text(listing)]),
    ]
    lines = [line for line in lines if line]
    spare = whatsapp.CARD_BODY_MAX - sum(len(line) for line in lines) - (len(lines) - 1)
    if spare < 0 and len(lines) > 1:
        lines[1] = _fit(lines[1], max(len(lines[1]) + spare, 1))
    return _fit("\n".join(lines), whatsapp.CARD_BODY_MAX)


def details(listing):
    """Full details for the single-result card."""
    lines = [
        f"*{listing.code}* · {kind_text(listing)}",
        listing.location,
        _join([area_text(listing), rooms_text(listing)]),
        _join([price_text(listing.price_pkr, listing.purpose), status_text(listing)]),
    ]
    if listing.cover_url is None:
        lines.append(f"({NO_COVER})")
    return "\n".join(line for line in lines if line)


def index_line(rank, listing):
    line = _join([f"#{rank} {listing.code}", kind_text(listing), listing.location,
                  price_text(listing.price_pkr, listing.purpose)])
    return line + (f" ({NO_COVER})" if listing.cover_url is None else "")


def summary(listings, shown, *, tap_hint):
    lines = [f"{len(listings)} matching listings:"]
    lines += [index_line(rank, listing) for rank, listing in enumerate(shown, 1)]
    if len(listings) > len(shown):
        lines.append(f"#{len(shown) + 1}-#{len(listings)} are in the list below.")
    lines.append(f"Reply with a number, or {tap_hint}.")
    return _fit("\n".join(lines), whatsapp.INTERACTIVE_BODY_MAX)


# --- payloads -------------------------------------------------------------------------

def card_buttons(code):
    return [(f"photos:{code}", "More photos"), (f"visit:{code}", "Visit / Details")]


def single_card(listing):
    """Exactly one result: cover image header (or a text header), full details, 3 buttons."""
    buttons = card_buttons(listing.code) + [(f"similar:{listing.code}", "Similar")]
    return _card(details(listing), buttons, listing)


def listing_card(rank, listing):
    """One card of the individual-cards format, or a coverless listing's text card."""
    return _card(card_body(rank, listing), card_buttons(listing.code), listing)


def _card(body, buttons, listing):
    if listing.cover_url is not None:
        return whatsapp.reply_buttons(body, buttons, image_header={"link": listing.cover_url})
    return whatsapp.reply_buttons(body, buttons, header=f"{listing.code} · {NO_COVER}")


def overflow_list(listings, start):
    rows = [
        (f"listing:{listing.code}", f"#{rank} {listing.code}",
         _fit(_join([kind_text(listing), listing.location,
                     price_text(listing.price_pkr, listing.purpose)]),
              whatsapp.ROW_DESCRIPTION_MAX))
        for rank, listing in enumerate(listings, start)
    ]
    return whatsapp.list_message(
        f"{len(rows)} more matching listings. Pick one for details.", "More results",
        [(None, rows)],
    )


def render(listings, *, fallback=False):
    """The payloads presenting ranked public listings, in send order."""
    listings = list(listings)
    if not listings:
        return [whatsapp.text("No matching listings right now.")]
    if len(listings) > RESULTS_MAX:
        raise ValueError(f"at most {RESULTS_MAX} results can be presented")
    if len(listings) == 1:
        return [single_card(listings[0])]
    shown = listings[:SHOWN_MAX]
    ranked = list(enumerate(shown, 1))
    covered = [(rank, listing) for rank, listing in ranked if listing.cover_url is not None]
    if not fallback and len(covered) >= whatsapp.CAROUSEL_CARDS_MIN:
        payloads = [whatsapp.carousel(
            summary(listings, shown, tap_hint="tap a card"),
            [(listing.cover_url, card_body(rank, listing), card_buttons(listing.code))
             for rank, listing in covered],
        )]
        payloads += [listing_card(rank, listing) for rank, listing in ranked
                     if listing.cover_url is None]
    else:
        payloads = [whatsapp.text(summary(listings, shown, tap_hint="tap a card's button"))]
        payloads += [listing_card(rank, listing) for rank, listing in ranked]
    if len(listings) > SHOWN_MAX:
        payloads.append(overflow_list(listings[SHOWN_MAX:], SHOWN_MAX + 1))
    return payloads


def is_carousel(payload):
    return payload["type"] == "interactive" and payload["interactive"]["type"] == "carousel"


# --- sending --------------------------------------------------------------------------

def plan_results(conn, agency_id, conversation_id, listings, *, note=None, force_fallback=False):
    """The payloads presenting these listings to this conversation (its fallback flag, or
    force_fallback, selects individual cards), preceded by an optional note."""
    fallback = force_fallback or fallback_status(conn, agency_id, conversation_id)[0]
    return ([whatsapp.text(note)] if note else []) + render(listings, fallback=fallback)


def fixed_plan(conn, agency_id, turn_id, build):
    """The turn's payloads: build() runs only while the turn has no stored plan; the
    first stored list wins and every later attempt gets it back unchanged."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        row = conn.execute(
            "SELECT reply_plan FROM conversation_turn WHERE id = %s", (turn_id,)
        ).fetchone()
    if row is None:
        raise LookupError("turn not found")
    if row[0] is not None:
        return row[0]
    payloads = build()
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        return conn.execute(
            """
            UPDATE conversation_turn SET reply_plan = COALESCE(reply_plan, %s)
            WHERE id = %s RETURNING reply_plan
            """,
            (Jsonb(payloads), turn_id),
        ).fetchone()[0]


def carousel_cards(payload):
    """Individual cards showing exactly what a carousel showed: its summary as text, then
    one image card per carousel card with the same body and buttons. A pure function
    of the stored payload, so every retry rebuilds the same messages."""
    interactive = payload["interactive"]
    cards = [whatsapp.text(interactive["body"]["text"])]
    for card in interactive["action"]["cards"]:
        buttons = [(b["quick_reply"]["id"], b["quick_reply"]["title"])
                   for b in card["action"]["buttons"]]
        body = (card.get("body") or {}).get("text") or f"#{card['card_index'] + 1}"
        cards.append(whatsapp.reply_buttons(
            body, buttons, image_header={"link": card["header"]["image"]["link"]},
        ))
    return cards


def send_plan(conn, client, agency_id, conversation_id, recipient, payloads, dedupe_prefix):
    """Send a turn's stored payloads one at a time, each confirmed before the next (D14),
    with the keys "{dedupe_prefix}:{seq}". A carousel Meta refuses at once is replaced
    by its cards (carousel_cards) and the plan goes on; a retried job finds the refusal
    recorded and sends the same cards under the same keys. Anything else that is not
    'sent' (gate, window, ambiguous) stops the turn. Returns the last status."""
    for seq, payload in enumerate(payloads):
        key = f"{dedupe_prefix}:{seq}"
        try:
            status = outbound.send_message(
                conn, client, agency_id, conversation_id, recipient, payload, key,
            ).status
        except whatsapp.PermanentSendError as exc:
            if not is_carousel(payload):
                raise
            logger.warning("Carousel refused (%s); sending its cards", exc.code)
            status = "refused"
        if is_carousel(payload) and (
            status == "refused" or (status == "failed" and _refused(conn, agency_id, key))
        ):
            status = send_all(conn, client, agency_id, conversation_id, recipient,
                              carousel_cards(payload), f"{key}:fallback")
        if status != "sent":
            return status
    return "sent"


def send_all(conn, client, agency_id, conversation_id, recipient, payloads, dedupe_prefix):
    """Send payloads in order under "{dedupe_prefix}:{n}"; stop at the first that is not
    'sent'. Never falls back again: a permanent refusal propagates."""
    for n, payload in enumerate(payloads):
        result = outbound.send_message(
            conn, client, agency_id, conversation_id, recipient, payload,
            f"{dedupe_prefix}:{n}",
        )
        if result.status != "sent":
            return result.status
    return "sent"


def _refused(conn, agency_id, key):
    """True when the send recorded under key was refused by Meta (not the window)."""
    attempt = outbound.prior_attempt(conn, agency_id, key)
    return attempt is not None and (attempt[1] or "").startswith("http_")


# --- a carousel that fails after Meta accepted it -------------------------------------

def on_failed_status(conn, outbound_id, recipient, payload, *, delivered):
    """Inside reconcile_status's tenant transaction, for a 'failed' status: queue one
    carousel_fallback job for a carousel that was never delivered. reconcile_status
    applies 'failed' only once (terminal) and the job dedupe key is per outbound
    row, so a replayed webhook queues nothing. Cards are not carousels: no recursion."""
    if not is_carousel(payload) or delivered:
        return
    enqueue_job(conn, FALLBACK_JOB_TYPE, f"carousel-fallback:{outbound_id}",
                {"outbound_message_id": str(outbound_id)}, conversation_key=recipient)


def carousel_fallback(conn, job, client_factory):
    """The carousel_fallback job: send the failed carousel's cards through the normal
    send path (send-time gate, 24 h window, keys "carousel-fallback:{id}:{n}")."""
    with tenant_transaction(conn, job.agency_id):
        _set_timeout(conn)
        row = conn.execute(
            """
            SELECT o.id, o.conversation_id, o.recipient_wa_id, o.payload, o.status,
                   a.meta_phone_number_id
            FROM outbound_message o JOIN agency a ON a.id = o.agency_id
            WHERE o.id = %s
            """,
            (job.payload.get("outbound_message_id"),),
        ).fetchone()
    if row is None or row[4] != "sent" or not is_carousel(row[3]):
        raise PermanentJobError("carousel_fallback_source_unusable")
    outbound_id, conversation_id, recipient, payload, _, phone_number_id = row
    with closing(client_factory(phone_number_id)) as client:
        try:
            status = send_all(conn, client, job.agency_id, conversation_id, recipient,
                              carousel_cards(payload), f"carousel-fallback:{outbound_id}")
        except whatsapp.PermanentSendError as exc:
            raise PermanentJobError(exc.code) from None
    logger.info("Carousel fallback for %s: %s", outbound_id, status)
