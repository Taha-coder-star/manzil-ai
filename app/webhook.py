"""WhatsApp Cloud API webhook authentication (Task 2.3) and event extraction (Task 2.4).

Authentication runs before anything is parsed or stored. Never log tokens,
signatures, or payload bodies.
"""

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass

SIGNATURE_HEADER = "X-Hub-Signature-256"
MAX_BODY_BYTES = 1024 * 1024

_SIGNATURE_RE = re.compile(r"sha256=([0-9a-fA-F]{64})")
# Meta sends the parameters twice: hub.verify_token and hub_verify_token.
_VERIFY_TOKEN_RE = re.compile(r"(hub(?:\.|_|%2e)verify_token=)[^&\s\"]*", re.IGNORECASE)


def verify_challenge(mode, token, challenge, expected_token):
    """Return the challenge to echo for a valid subscribe request, else None."""
    if not expected_token or mode != "subscribe" or not token or challenge is None:
        return None
    if not hmac.compare_digest(token.encode("utf-8"), expected_token.encode("utf-8")):
        return None
    return challenge


def verify_signature(app_secret, raw_body, header):
    """Check X-Hub-Signature-256 against HMAC-SHA256 of the exact request bytes."""
    if not app_secret or not header:
        return False
    match = _SIGNATURE_RE.fullmatch(header.strip())
    if not match:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1).lower())


@dataclass(frozen=True)
class InboundEvent:
    phone_number_id: str
    event_type: str
    event_key: str
    provider_message_id: str
    payload: dict
    job_type: str
    dedupe_key: str
    job_payload: dict


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _dicts(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def extract_events(envelope):
    """Return every supported event in an authenticated envelope, plus a skip count.

    Walks all entries, changes, messages and statuses. Only the "messages"
    field is supported. Items without the identifiers needed for a stable key
    are skipped rather than stored under an unstable one.
    """
    events, skipped = [], 0
    for entry in _dicts(envelope.get("entry")):
        for change in _dicts(entry.get("changes")):
            value = change.get("value")
            if change.get("field") != "messages" or not isinstance(value, dict):
                skipped += 1
                continue
            metadata = value.get("metadata")
            phone_number_id = _text(metadata.get("phone_number_id")) if isinstance(
                metadata, dict
            ) else None
            if phone_number_id is None:
                skipped += 1
                continue
            contacts = {c.get("wa_id"): c for c in _dicts(value.get("contacts"))}

            for message in _dicts(value.get("messages")):
                wamid = _text(message.get("id"))
                if wamid is None:
                    skipped += 1
                    continue
                events.append(InboundEvent(
                    phone_number_id=phone_number_id,
                    event_type="message",
                    event_key=f"wa:msg:{wamid}",
                    provider_message_id=wamid,
                    payload={
                        "metadata": metadata,
                        "contact": contacts.get(message.get("from")),
                        "message": message,
                    },
                    job_type="process_message",
                    dedupe_key=f"process_message:{wamid}",
                    job_payload={"provider_message_id": wamid},
                ))

            for status in _dicts(value.get("statuses")):
                wamid, state = _text(status.get("id")), _text(status.get("status"))
                if wamid is None or state is None:
                    skipped += 1
                    continue
                # sent/delivered/read share the WAMID; the status keeps them distinct.
                events.append(InboundEvent(
                    phone_number_id=phone_number_id,
                    event_type="status",
                    event_key=f"wa:status:{wamid}:{state}",
                    provider_message_id=wamid,
                    payload={"metadata": metadata, "status": status},
                    job_type="reconcile_status",
                    dedupe_key=f"reconcile_status:{wamid}:{state}",
                    job_payload={"provider_message_id": wamid, "status": state},
                ))
    return events, skipped


def redact_verify_token(text):
    return _VERIFY_TOKEN_RE.sub(r"\1[REDACTED]", text)


class RedactVerifyTokenFilter(logging.Filter):
    """Keep hub.verify_token out of access logs, which record the query string."""

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_verify_token(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = redact_verify_token(record.msg)
        return True
