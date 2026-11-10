"""Customer listing delivery guards. No model output participates in these checks."""

from app import cards, details, outbound, whatsapp
from app.db import tenant_transaction

GUARD = "manzil_listing"


def unavailable(code, language):
    english = language == cards.ENGLISH
    return whatsapp.reply_buttons(
        f"{code} is not available right now." if english else f"{code} abhi available nahi.",
        [(f"similar:{code}", "Similar listings" if english else "Milti-julti listings")])


def guard(payload, item, language, *, kind="payload", fields=()):
    return {"type": GUARD, "payload": payload, "property_id": str(item.property_id),
            "code": item.code, "position": item.position, "language": language,
            "kind": kind, "fields": list(fields)}


def send(conn, client, turn, entry, key, image_sender=None):
    """Rebuild factual answers from current public rows; retain stable outbound identities."""
    prior = outbound.prior_attempt(conn, turn.agency_id, key)
    if prior and prior[0] == "sending":
        with tenant_transaction(conn, turn.agency_id):
            stored = conn.execute("SELECT payload FROM outbound_message WHERE dedupe_key=%s",
                                  (key,)).fetchone()[0]
        return outbound.send_message(conn, client, turn.agency_id, turn.conversation_id,
                                     turn.sender_wa_id, stored, key).status
    if prior and prior[0] != "pending":
        return prior[0]
    code, language = entry["code"], entry["language"]
    prefix = getattr(turn, "outbound_prefix", None) or f"turn:{turn.id}"
    listing = cards.load_listings(conn, turn.agency_id, [code])
    if not listing:
        return outbound.send_message(
            conn, client, turn.agency_id, turn.conversation_id, turn.sender_wa_id,
            unavailable(code, language), f"{prefix}:unavailable:{entry['property_id']}").status
    current = listing[0]
    with tenant_transaction(conn, turn.agency_id):
        conn.execute("SET LOCAL statement_timeout = '5s'")
        previous = conn.execute(
            "SELECT rendered_price_pkr FROM shown_item WHERE conversation_id = %s "
            "AND property_id = %s ORDER BY shown_at DESC, id DESC LIMIT 1",
            (turn.conversation_id, entry["property_id"])).fetchone()
    if previous and previous[0] is not None and previous[0] != current.price_pkr:
        amount = cards.price_text(current.price_pkr, current.purpose)
        line = (f"Update: price is now {amount}." if language == cards.ENGLISH
                else f"Update: price ab {amount} hai.")
        status = outbound.send_message(
            conn, client, turn.agency_id, turn.conversation_id, turn.sender_wa_id,
            whatsapp.text(line),
            f"{prefix}:price:{entry['property_id']}").status
        if status != "sent":
            return status
    payload = entry["payload"]
    if entry["kind"] == "card":
        payload = cards.single_card(current, language)
    elif entry["kind"] == "detail":
        known = details.load_known(conn, turn.agency_id, code)
        payload = (details.answer_detail(known, entry["fields"], language,
                                        entry["position"])[0] if known else unavailable(code, language))
    if payload["type"] == "manzil_property_image":
        return image_sender(payload)
    return cards.send_one(conn, client, turn.agency_id, turn.conversation_id,
                          turn.sender_wa_id, payload, key)
