"""Sending a customer turn's stored plan with its side effects (Task 10.1; docs/SEARCH.md,
docs/RENDERER.md section 1; R17 references; D14).

The engine plans and this module executes. A customer plan (conversation_turn.reply_plan)
holds WhatsApp payloads plus two internal entries that are never sent to Meta:

- SHOWN: the listings a search reply showed. Recorded (search.record_shown, with the
  internal message id of the card each listing was on) only when every payload before it
  was sent: a failed, gated or refused send leaves no history (docs/SEARCH.md).
- IMAGE: one listing photo (property_media), sent through media.send_image under the
  entry's own key and recorded in media_sent, so "aur pictures" never repeats one.

Every entry keeps its plan index as its seq, so a retried turn uses the same
"turn:{id}:{seq}" keys and record_shown / media_sent ignore what is already there.
Never log message text, codes of the customer's choice or phone numbers.
"""

import logging
import re

from app import cards, media, search
from app.db import tenant_transaction

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
SHOWN = "manzil_shown"
IMAGE = "manzil_property_image"
INTERNAL = frozenset({SHOWN, IMAGE})
_CARD_ID = re.compile(r"(?:photos|visit|similar|listing):([A-Z0-9]{2,4}-[0-9]{3})")


def shown_entry(search_run_id, shown):
    """shown: render.RenderPlan.shown, [(position, property_id, code)] in position order."""
    return {"type": SHOWN, "search_run_id": str(search_run_id),
            "items": [[position, str(property_id), code] for position, property_id, code
                      in sorted(shown)]}


def image_entry(property_id, property_media_id, caption):
    return {"type": IMAGE, "property_id": str(property_id),
            "property_media_id": str(property_media_id), "caption": caption}


def is_internal(payload):
    return payload.get("type") in INTERNAL


def visible(payloads):
    """The payloads a customer sees (internal entries left out)."""
    return [payload for payload in payloads if not is_internal(payload)]


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def send(conn, client, storage, turn, payloads):
    """Send a customer plan in order under turn:{id}:{seq}; stop at the first payload that
    is not 'sent' (gate, window, ambiguous), as cards.send_plan does. Returns that status,
    or 'sent'."""
    prefix = f"turn:{turn.id}"
    for seq, payload in enumerate(payloads):
        key = f"{prefix}:{seq}"
        if payload["type"] == SHOWN:
            _record_shown(conn, turn, payloads[:seq], prefix, payload)
            continue
        if payload["type"] == IMAGE:
            status = _send_image(conn, client, storage, turn, payload, key)
        else:
            status = cards.send_one(conn, client, turn.agency_id, turn.conversation_id,
                                    turn.sender_wa_id, payload, key)
        if status != "sent":
            logger.info("Turn %s stopped at payload %d (%s)", turn.id, seq, status)
            return status
    return "sent"


def card_codes(payload):
    """[(card index, listing code)] of the listing buttons and rows in a payload: a carousel
    card by its index, anything else (a card, an overflow list) as index 0."""
    interactive = payload.get("interactive") or {}
    if interactive.get("type") == "carousel":
        return [(index, code) for index, card in enumerate(interactive["action"]["cards"])
                for code in dict.fromkeys(_CARD_ID.findall(repr(card)))]
    return [(0, code) for code in dict.fromkeys(_CARD_ID.findall(repr(interactive)))]


def _message_ids(conn, agency_id, keys):
    """{dedupe key: internal message id} of the sent outbound rows among keys."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        rows = conn.execute(
            "SELECT dedupe_key, message_id FROM outbound_message "
            "WHERE dedupe_key = ANY(%s) AND status = 'sent'", (list(keys),)).fetchall()
    return dict(rows)


def _record_shown(conn, turn, before, prefix, entry):
    """The shown_item rows of a search reply, each with the message its card was on: the
    carousel, or its fallback card when Meta refused the carousel."""
    where = {}
    for seq, payload in enumerate(before):
        for index, code in card_codes(payload):
            where.setdefault(code, (seq, index, cards.is_carousel(payload)))
    keys = {}
    for _, _, code in entry["items"]:
        if code in where:
            seq, index, carousel = where[code]
            key = f"{prefix}:{seq}"
            keys[code] = ([f"{key}:fallback:{index + 1}", key] if carousel else [key])
    sent = _message_ids(conn, turn.agency_id, [k for ks in keys.values() for k in ks])
    message_ids = [next((sent[k] for k in keys.get(code, ()) if k in sent), None)
                   for _, _, code in entry["items"]]
    search.record_shown(conn, turn.agency_id, turn.conversation_id, entry["search_run_id"],
                        [property_id for _, property_id, _ in entry["items"]], message_ids)


def _send_image(conn, client, storage, turn, entry, key):
    """One listing photo; a photo that cannot be sent (no storage, gone) is skipped."""
    if storage is None:
        logger.warning("Turn %s: no media storage configured; photo skipped", turn.id)
        return "sent"
    try:
        result = media.send_image(
            conn, client, storage, turn.agency_id, turn.conversation_id, turn.sender_wa_id,
            ("property_media", entry["property_media_id"]), key, caption=entry["caption"])
    except LookupError:
        logger.warning("Turn %s: a listing photo is no longer usable; skipped", turn.id)
        return "sent"
    if result.status == "sent":
        message_id = next(iter(
            _message_ids(conn, turn.agency_id, [key, f"{key}:link"]).values()), None)
        with tenant_transaction(conn, turn.agency_id):
            _set_timeout(conn)
            conn.execute(
                """
                INSERT INTO media_sent (agency_id, conversation_id, property_id,
                                        property_media_id, message_id)
                VALUES (current_agency_id(), %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (turn.conversation_id, entry["property_id"], entry["property_media_id"],
                 message_id),
            )
    return result.status
