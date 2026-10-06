"""WhatsApp Cloud API webhook authentication (Task 2.3).

Both checks run before anything is parsed or stored. Never log tokens,
signatures, or payload bodies.
"""

import hashlib
import hmac
import logging
import re

SIGNATURE_HEADER = "X-Hub-Signature-256"
MAX_BODY_BYTES = 1024 * 1024

_SIGNATURE_RE = re.compile(r"sha256=([0-9a-fA-F]{64})")
_VERIFY_TOKEN_RE = re.compile(r"(hub\.verify_token=)[^&\s\"]*")


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
