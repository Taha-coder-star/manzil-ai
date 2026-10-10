"""'#demo …' samples for staff device checks (Task 3.2; kept by Task 5.1).

The plain echo replies are gone: the orchestrator (app.orchestrator) answers every
turn. A staff / admin turn whose last message is "#demo <type>" still gets a
fictional sample of each outbound message type, so rendering can be checked on
real phones; customers never reach these. "#demo photo [badid]" (app.handlers)
sends the sender's own latest staff photo back through the Task 4.2 media cache.
All sample content is fictional.
"""

from app import whatsapp

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


def is_demo(body):
    """True for a "#demo …" command."""
    return body.strip().lower().startswith("#demo")


def reply(message, *, privacy_url=None, image_link=None):
    """The payloads answering one "#demo …" text message (none for anything else).

    privacy_url: public https URL used by the CTA sample.
    image_link: callable returning a public https image URL, or None.
    """
    if message.get("type") != "text":
        return []
    body = (message.get("text") or {}).get("body") or ""
    if not is_demo(body):
        return []
    return [_demo(body.strip().lower().removeprefix("#demo").strip(), privacy_url, image_link)]


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
