"""WhatsApp Cloud API gateway (Task 3.2, component C12): payload builders and client.

Builders return a message body ({"type": ..., <type>: {...}}) and enforce the
WhatsApp limits (CLAUDE.md section 11) before anything is sent, so a bad payload
fails as ValueError instead of a Meta error. WhatsAppClient adds the envelope.

Never log the access token, request or response bodies, or recipient numbers.
"""

import hashlib
import logging
import re
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com"
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0

TEXT_MAX = 4096
INTERACTIVE_BODY_MAX = 1024
HEADER_TEXT_MAX = 60
FOOTER_MAX = 60
CAPTION_MAX = 1024
BUTTON_TITLE_MAX = 20
BUTTON_ID_MAX = 256
MAX_BUTTONS = 3
LIST_BUTTON_MAX = 20
LIST_ROWS_MAX = 10
ROW_ID_MAX = 200
ROW_TITLE_MAX = 24
ROW_DESCRIPTION_MAX = 72
SECTION_TITLE_MAX = 24
CTA_DISPLAY_TEXT_MAX = 20
TEMPLATE_NAME = re.compile(r"[a-z0-9_]{1,512}")
TEMPLATE_PARAM_REFUSED = re.compile(r"[\n\t]| {5,}")
# Interactive media carousel (Meta docs read 2026-10-10; docs/DECISIONS.md).
CAROUSEL_CARDS_MIN = 2
CAROUSEL_CARDS_MAX = 10
CARD_BODY_MAX = 160
CARD_BODY_LINE_BREAKS_MAX = 2
# Meta states no maximum for quick replies per card; its examples use two (D9 uses two).
CAROUSEL_BUTTONS_MAX = 2
# Meta's request syntax fixes every card's type as "cta_url", quick-reply cards included
# (and its quick-reply example uses it); the field is not described. Device test confirms.
CAROUSEL_CARD_TYPE = "cta_url"

# Meta error codes that are throttling or temporary: retrying can succeed.
RETRYABLE_META_CODES = frozenset({1, 2, 4, 80007, 130429, 131056})

# Media download URLs (5-minute life) are fetched with the access token, so the
# token is only ever sent to Meta's own hosts.
MEDIA_HOST_SUFFIXES = (".fbsbx.com", ".facebook.com", ".whatsapp.net", ".fbcdn.net")
# A download URL that has expired (or was never ours) answers with one of these.
EXPIRED_URL_STATUSES = frozenset({401, 403, 404, 410})


class SendError(Exception):
    """Base class; code is log-safe (never the message body or the token)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class RetryableSendError(SendError):
    """The request was not processed (connect failure, throttling, 5xx): safe to retry."""


class PermanentSendError(SendError):
    """Meta rejected the request; retrying the same payload cannot help."""


class AmbiguousSendError(SendError):
    """The request may have been processed (e.g. read timeout): never resend it."""


class MediaError(Exception):
    """Base class for media fetches; code is log-safe."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class RetryableMediaError(MediaError):
    """Transient (network, throttling, 5xx, corrupt transfer): a later attempt can succeed."""


class PermanentMediaError(MediaError):
    """The media is gone, too large or rejected by Meta; retrying cannot help."""


def _check_text(value, name, limit, *, required=True):
    if value is None and not required:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")


def _media(media_id, link):
    if (media_id is None) == (link is None):
        raise ValueError("exactly one of media_id or link is required")
    if link is not None:
        _check_https(link, "link")
        return {"link": link}
    _check_text(media_id, "media_id", 256)
    return {"id": media_id}


def _check_https(url, name):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(f"{name} must be an https URL")


def _decorate(interactive, header, footer):
    """Add an optional text header and footer to an interactive message."""
    if header is not None:
        _check_text(header, "header", HEADER_TEXT_MAX)
        interactive["header"] = {"type": "text", "text": header}
    if footer is not None:
        _check_text(footer, "footer", FOOTER_MAX)
        interactive["footer"] = {"text": footer}
    return interactive


def text(body, *, preview_url=False):
    _check_text(body, "body", TEXT_MAX)
    return {"type": "text", "text": {"body": body, "preview_url": bool(preview_url)}}


def reply_buttons(body, buttons, *, header=None, image_header=None, footer=None):
    """1-3 reply buttons as (id, title) pairs. image_header is {"id": ...} or {"link": ...}."""
    _check_text(body, "body", INTERACTIVE_BODY_MAX)
    buttons = list(buttons)
    if not 1 <= len(buttons) <= MAX_BUTTONS:
        raise ValueError(f"reply buttons need 1-{MAX_BUTTONS} buttons")
    ids = [button_id for button_id, _ in buttons]
    if len(set(ids)) != len(ids):
        raise ValueError("button ids must be unique")
    for button_id, title in buttons:
        _check_text(button_id, "button id", BUTTON_ID_MAX)
        _check_text(title, "button title", BUTTON_TITLE_MAX)
    if header is not None and image_header is not None:
        raise ValueError("use either a text header or an image header")
    interactive = {
        "type": "button",
        "body": {"text": body},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": button_id, "title": title}}
            for button_id, title in buttons
        ]},
    }
    if image_header is not None:
        interactive["header"] = {
            "type": "image", "image": _media(image_header.get("id"), image_header.get("link")),
        }
    return {"type": "interactive", "interactive": _decorate(interactive, header, footer)}


def list_message(body, button, sections, *, header=None, footer=None):
    """sections: [(section_title, [(row_id, title, description_or_None), ...]), ...]."""
    _check_text(body, "body", INTERACTIVE_BODY_MAX)
    _check_text(button, "list button", LIST_BUTTON_MAX)
    sections = [(title, list(rows)) for title, rows in sections]
    rows = [row for _, section_rows in sections for row in section_rows]
    if not sections or any(not section_rows for _, section_rows in sections):
        raise ValueError("a list needs sections with at least one row each")
    if len(rows) > LIST_ROWS_MAX:
        raise ValueError(f"a list allows at most {LIST_ROWS_MAX} rows in total")
    ids = [row_id for row_id, _, _ in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("row ids must be unique")
    built = []
    for title, section_rows in sections:
        # Meta requires section titles when there is more than one section.
        _check_text(title, "section title", SECTION_TITLE_MAX, required=len(sections) > 1)
        section = {"rows": []}
        if title is not None:
            section["title"] = title
        for row_id, row_title, description in section_rows:
            _check_text(row_id, "row id", ROW_ID_MAX)
            _check_text(row_title, "row title", ROW_TITLE_MAX)
            row = {"id": row_id, "title": row_title}
            if description is not None:
                _check_text(description, "row description", ROW_DESCRIPTION_MAX)
                row["description"] = description
            section["rows"].append(row)
        built.append(section)
    interactive = {
        "type": "list",
        "body": {"text": body},
        "action": {"button": button, "sections": built},
    }
    return {"type": "interactive", "interactive": _decorate(interactive, header, footer)}


def carousel(body, cards):
    """Interactive media carousel (D9). cards: [(image_link, card_body_or_None,
    [(id, title), ...]), ...] with quick-reply buttons.

    Card headers are public https image links only (Meta documents no media-ID
    form); every card has the same number of buttons; the message itself takes
    no header or footer.
    """
    _check_text(body, "body", INTERACTIVE_BODY_MAX)
    cards = [(link, card_body, list(buttons)) for link, card_body, buttons in cards]
    if not CAROUSEL_CARDS_MIN <= len(cards) <= CAROUSEL_CARDS_MAX:
        raise ValueError(f"a carousel needs {CAROUSEL_CARDS_MIN}-{CAROUSEL_CARDS_MAX} cards")
    counts = {len(buttons) for _, _, buttons in cards}
    if len(counts) != 1:
        raise ValueError("every carousel card needs the same number of buttons")
    if not 1 <= counts.pop() <= CAROUSEL_BUTTONS_MAX:
        raise ValueError(f"carousel cards need 1-{CAROUSEL_BUTTONS_MAX} buttons")
    ids = [button_id for _, _, buttons in cards for button_id, _ in buttons]
    if len(set(ids)) != len(ids):
        raise ValueError("button ids must be unique")
    built = []
    for index, (link, card_body, buttons) in enumerate(cards):
        _check_https(link, "card image link")
        card = {
            "card_index": index,
            "type": CAROUSEL_CARD_TYPE,
            "header": {"type": "image", "image": {"link": link}},
        }
        if card_body is not None:
            _check_text(card_body, "card body", CARD_BODY_MAX)
            if card_body.count("\n") > CARD_BODY_LINE_BREAKS_MAX:
                raise ValueError(
                    f"card body allows at most {CARD_BODY_LINE_BREAKS_MAX} line breaks"
                )
            card["body"] = {"text": card_body}
        for button_id, title in buttons:
            _check_text(button_id, "button id", BUTTON_ID_MAX)
            _check_text(title, "button title", BUTTON_TITLE_MAX)
        card["action"] = {"buttons": [
            {"type": "quick_reply", "quick_reply": {"id": button_id, "title": title}}
            for button_id, title in buttons
        ]}
        built.append(card)
    interactive = {"type": "carousel", "body": {"text": body}, "action": {"cards": built}}
    return {"type": "interactive", "interactive": interactive}


def image(*, media_id=None, link=None, caption=None):
    content = _media(media_id, link)
    if caption is not None:
        _check_text(caption, "caption", CAPTION_MAX)
        content["caption"] = caption
    return {"type": "image", "image": content}


def location(latitude, longitude, *, name=None, address=None):
    if not -90 <= float(latitude) <= 90 or not -180 <= float(longitude) <= 180:
        raise ValueError("latitude/longitude out of range")
    content = {"latitude": float(latitude), "longitude": float(longitude)}
    if name is not None:
        _check_text(name, "name", 1000)
        content["name"] = name
    if address is not None:
        _check_text(address, "address", 1000)
        content["address"] = address
    return {"type": "location", "location": content}


def cta_url(body, display_text, url, *, header=None, footer=None):
    _check_text(body, "body", INTERACTIVE_BODY_MAX)
    _check_text(display_text, "display text", CTA_DISPLAY_TEXT_MAX)
    _check_https(url, "url")
    interactive = {
        "type": "cta_url",
        "body": {"text": body},
        "action": {"name": "cta_url", "parameters": {"display_text": display_text, "url": url}},
    }
    return {"type": "interactive", "interactive": _decorate(interactive, header, footer)}


def template(name, language, body_params=()):
    """An approved message template (Task 11.1: new_lead_alert to a broker whose 24 h
    window is closed). body_params fill {{1}}, {{2}}, ... in order; Meta refuses a
    parameter with a line break, a tab or more than four spaces in a row (132018)."""
    if not TEMPLATE_NAME.fullmatch(name or ""):
        raise ValueError("template name must be lowercase letters, digits and underscores")
    _check_text(language, "template language", 15)
    content = {"name": name, "language": {"code": language}}
    params = []
    for value in body_params:
        _check_text(value, "template parameter", INTERACTIVE_BODY_MAX)
        if TEMPLATE_PARAM_REFUSED.search(value):
            raise ValueError("template parameter has a line break, tab or long space run")
        params.append({"type": "text", "text": value})
    if params:
        content["components"] = [{"type": "body", "parameters": params}]
    return {"type": "template", "template": content}


def summary(payload):
    """The human-readable text of a built payload, for the message log."""
    kind = payload["type"]
    if kind == "template":
        params = [p["text"] for c in payload["template"].get("components", ())
                  for p in c.get("parameters", ())]
        return " · ".join([f"[template {payload['template']['name']}]", *params])
    if kind == "text":
        return payload["text"]["body"]
    if kind == "image":
        return payload["image"].get("caption")
    if kind == "location":
        return payload["location"].get("name")
    if kind == "interactive":
        return payload["interactive"]["body"]["text"]
    return None


class WhatsAppClient:
    def __init__(self, phone_number_id, access_token, api_version, *, http=None):
        if not phone_number_id or not access_token or not api_version:
            raise ValueError("phone_number_id, access_token and api_version are required")
        self._graph = f"{GRAPH_URL}/{api_version}"
        self._phone_number_id = phone_number_id
        self._url = f"{self._graph}/{phone_number_id}/messages"
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        )
        self._headers = {"Authorization": f"Bearer {access_token}"}

    def close(self):
        self._http.close()

    def send(self, to, payload, *, reply_to=None):
        """Send a built payload to a wa_id; return the new message's wamid."""
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
        body.update(payload)
        if reply_to is not None:
            body["context"] = {"message_id": reply_to}
        data = self._post(body)
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError, TypeError):
            # Accepted but unidentifiable: it may still be delivered, so never resend.
            raise AmbiguousSendError("missing_message_id") from None

    def mark_read(self, wamid, *, typing=False):
        """Blue ticks for an inbound message; optionally show the typing indicator."""
        body = {"messaging_product": "whatsapp", "status": "read", "message_id": wamid}
        if typing:
            body["typing_indicator"] = {"type": "text"}
        self._post(body)

    def media_info(self, media_id):
        """Metadata of an inbound media ID: a fresh 5-minute URL, mime_type, sha256, size."""
        if not isinstance(media_id, str) or not media_id.isalnum():
            raise PermanentMediaError("invalid_media_id")
        try:
            response = self._http.get(
                f"{self._graph}/{media_id}",
                params={"phone_number_id": self._phone_number_id},
                headers=self._headers,
            )
        except httpx.TransportError as exc:
            raise RetryableMediaError(type(exc).__name__) from None
        if response.status_code != 200:
            raise _media_status_error(response, "info")
        try:
            data = response.json()
            url = data["url"]
        except (ValueError, KeyError, TypeError):
            raise RetryableMediaError("info_invalid_json") from None
        if not isinstance(url, str):
            raise RetryableMediaError("info_invalid_json")
        return data

    def download_media(self, url, max_bytes):
        """Bytes behind a media URL, at most max_bytes. Raises MediaExpired for a dead URL."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host.endswith(MEDIA_HOST_SUFFIXES):
            raise PermanentMediaError("untrusted_media_host")
        try:
            with self._http.stream("GET", url, headers=self._headers) as response:
                if response.status_code in EXPIRED_URL_STATUSES:
                    raise MediaUrlExpired(f"http_{response.status_code}")
                if response.status_code != 200:
                    raise _media_status_error(response, "download")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise PermanentMediaError("too_large")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise PermanentMediaError("too_large")
                    chunks.append(chunk)
        except httpx.TransportError as exc:
            raise RetryableMediaError(type(exc).__name__) from None
        return b"".join(chunks)

    def fetch_media(self, media_id, max_bytes):
        """Download an inbound media ID; returns (bytes, meta mime_type).

        The URL is re-queried once if it expired between lookup and download.
        """
        for attempt in (1, 2):
            info = self.media_info(media_id)
            declared = info.get("file_size")
            if isinstance(declared, int) and declared > max_bytes:
                raise PermanentMediaError("too_large")
            try:
                data = self.download_media(info["url"], max_bytes)
            except MediaUrlExpired:
                if attempt == 2:
                    raise RetryableMediaError("url_expired") from None
                continue
            expected = info.get("sha256")
            if isinstance(expected, str) and hashlib.sha256(data).hexdigest() != expected.lower():
                raise RetryableMediaError("sha256_mismatch")
            mime = info.get("mime_type")
            return data, mime if isinstance(mime, str) else None

    def upload_media(self, data, mime_type, filename="image.jpg"):
        """Upload bytes to the Media API; return the media ID (valid 30 days)."""
        try:
            response = self._http.post(
                f"{self._graph}/{self._phone_number_id}/media",
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (filename, data, mime_type)},
                headers=self._headers,
            )
        except httpx.TransportError as exc:
            # An upload that may have landed only leaves an unused ID at Meta.
            raise RetryableMediaError(type(exc).__name__) from None
        if response.status_code != 200:
            raise _media_status_error(response, "upload")
        try:
            media_id = response.json()["id"]
        except (ValueError, KeyError, TypeError):
            raise RetryableMediaError("upload_invalid_json") from None
        if not isinstance(media_id, str) or not media_id:
            raise RetryableMediaError("upload_invalid_json")
        return media_id

    def _post(self, body):
        try:
            response = self._http.post(self._url, json=body, headers=self._headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The request never reached Meta.
            raise RetryableSendError(type(exc).__name__) from None
        except httpx.TransportError as exc:
            # Read timeout, dropped connection, ...: Meta may have processed it.
            raise AmbiguousSendError(type(exc).__name__) from None

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                raise AmbiguousSendError("invalid_json") from None

        meta_code = _meta_error_code(response)
        code = f"http_{response.status_code}" + (f"_meta_{meta_code}" if meta_code else "")
        logger.warning("WhatsApp API rejected a request: %s", code)
        if (
            response.status_code == 429
            or response.status_code >= 500
            or meta_code in RETRYABLE_META_CODES
        ):
            raise RetryableSendError(code)
        raise PermanentSendError(code)


class MediaUrlExpired(RetryableMediaError):
    """The 5-minute download URL no longer works; query the media ID again."""


def _media_status_error(response, stage):
    meta_code = _meta_error_code(response)
    code = f"{stage}_http_{response.status_code}" + (f"_meta_{meta_code}" if meta_code else "")
    logger.warning("WhatsApp media request rejected: %s", code)
    if (
        response.status_code == 429
        or response.status_code >= 500
        or meta_code in RETRYABLE_META_CODES
    ):
        return RetryableMediaError(code)
    return PermanentMediaError(code)


def _meta_error_code(response):
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
    return code if isinstance(code, int) else None
