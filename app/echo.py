"""Temporary echo bot (Task 3.2): proves the send / status path end to end.

Removed when the conversation orchestrator (C3) replaces process_message.
"#demo <type>" sends a fictional sample of each outbound message type so the
rendering can be checked on real phones. All sample content is fictional.
"#demo photo [badid]" (staff only, app.handlers) sends the sender's own latest
staff photo back through the Task 4.2 media cache.
"""

from app import whatsapp

ECHO_PREFIX = "Echo: "
DEMO_TYPES = ("buttons", "list", "location", "cta", "image", "photo")
PHOTO_VARIANTS = ("", "badid")
DEMO_IMAGE_CAPTION = "Sample image (DEMO)"


def photo_command(message):
    """'' or 'badid' for "#demo photo [badid]", else None."""
    if message.get("type") != "text":
        return None
    words = ((message.get("text") or {}).get("body") or "").strip().lower().split()
    if words[:2] != ["#demo", "photo"] or len(words) > 3:
        return None
    variant = words[2] if len(words) == 3 else ""
    return variant if variant in PHOTO_VARIANTS else None


def reply(message, *, privacy_url=None, image_link=None):
    """Return the payloads answering one inbound message (possibly none).

    privacy_url: public https URL used by the CTA sample.
    image_link: callable returning a public https image URL, or None.
    """
    kind = message.get("type")
    if kind == "reaction":
        return []
    if kind == "text":
        body = (message.get("text") or {}).get("body") or ""
        command = body.strip().lower()
        if command.startswith("#demo"):
            return [_demo(command.removeprefix("#demo").strip(), privacy_url, image_link)]
        return [whatsapp.text(_truncate(ECHO_PREFIX + body, whatsapp.TEXT_MAX))]
    if kind == "interactive":
        interactive = message.get("interactive") or {}
        picked = interactive.get(interactive.get("type")) or {}
        return [whatsapp.text(
            _truncate(f"You picked {picked.get('id')} · {picked.get('title')}", whatsapp.TEXT_MAX)
        )]
    return [whatsapp.text(f"Received a message of type {kind or 'unknown'}")]


def _demo(kind, privacy_url, image_link):
    if kind == "buttons":
        return whatsapp.reply_buttons(
            "Sample reply buttons (DEMO). Tap one.",
            [("demo_btn_1", "More photos"), ("demo_btn_2", "Visit"), ("demo_btn_3", "Agent")],
            footer="Manzil Demo Realty (DEMO)",
        )
    if kind == "list":
        return whatsapp.list_message(
            "Sample list (DEMO). Pick a row.",
            "View options",
            [
                ("Phase 6 (DEMO)", [
                    ("demo_row_1", "#1 · DEMO-101", "Fictional 500 sq yd house"),
                    ("demo_row_2", "#2 · DEMO-102", "Fictional 500 sq yd house"),
                ]),
                ("Phase 8 (DEMO)", [
                    ("demo_row_3", "#3 · DEMO-103", "Fictional 500 sq yd house"),
                ]),
            ],
            header="Sample results",
        )
    if kind == "location":
        return whatsapp.location(
            24.8138, 67.0300,
            name="Manzil Demo Office (DEMO)", address="Fictional address, Karachi (DEMO)",
        )
    if kind == "cta":
        if not privacy_url:
            return whatsapp.text("CTA demo is not configured (no site address).")
        return whatsapp.cta_url(
            "Sample link button (DEMO).", "Open page", privacy_url,
        )
    if kind == "image":
        link = image_link() if image_link else None
        if not link:
            return whatsapp.text("Image demo is not configured (no bucket).")
        return whatsapp.image(link=link, caption=DEMO_IMAGE_CAPTION)
    return whatsapp.text("Try: #demo " + " | ".join(DEMO_TYPES))


def _truncate(value, limit):
    return value if len(value) <= limit else value[: limit - 1] + "…"
