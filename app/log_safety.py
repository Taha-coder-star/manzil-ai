"""Redact sensitive values before records reach runtime logging handlers."""

import logging
import re
import traceback

_PHONE = re.compile(r"(?<![\w])\+?\d(?:[ ()-]?\d){9,14}(?![\w])")
_TOKEN = re.compile(
    r"(?i)((?:hub(?:\.|%2e|_)verify_token|access_token|api_key|password|"
    r"authorization|token|secret)[\"']?\s*(?:=|:)\s*[\"']?)(?:Bearer\s+)?[^\s&,\"'}]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,\"']+")


def redact(value):
    return _PHONE.sub("[REDACTED]", _BEARER.sub("Bearer [REDACTED]",
                                             _TOKEN.sub(r"\1[REDACTED]", value)))


def install():
    previous = logging.getLogRecordFactory()
    if getattr(previous, "manzil_redaction", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        # Uvicorn's access formatter reads the original five argument slots.
        # Preserve their structure rather than replacing them with formatted text.
        def clean(value):
            if isinstance(value, str):
                return redact(value)
            if isinstance(value, tuple):
                return tuple(clean(v) for v in value)
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            return value

        if record.name == "uvicorn.access":
            record.args = clean(record.args)
        else:
            record.msg, record.args = redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return record

    factory.manzil_redaction = True
    logging.setLogRecordFactory(factory)
