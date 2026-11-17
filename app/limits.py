"""Usage limits (audit MA-006; BD-8 default values; TD-10).

Counted in PostgreSQL from rows the system already writes, inside the caller's tenant
transaction, so limits survive restarts and need no new table. Units, not money:
usage_event.estimated_cost is NULL because provider prices were never verified (BD-9).

- Customer turns per number: conversation_turn rows of the conversation on the customer
  route in the last hour / 24 hours. Turns closed 'gated' (handoff, or over a limit) did
  no AI work and are not counted.
- Voice notes per number: inbound_audio rows of the conversation in the last hour.
- Per agency, since the start of the Karachi day: model calls (usage_event rows other
  than speech-to-text) and transcribed audio (succeeded speech-to-text seconds).

Staff are exempt from the per-number limits; their model calls count in the agency
total. Every value can be changed through the environment.
"""

import logging
import os

from app.config import ConfigurationError

logger = logging.getLogger(__name__)

DEFAULTS = {
    "LIMIT_CUSTOMER_TURNS_PER_HOUR": 20,
    "LIMIT_CUSTOMER_TURNS_PER_DAY": 60,
    "LIMIT_CUSTOMER_VOICE_PER_HOUR": 10,
    "LIMIT_AGENCY_MODEL_CALLS_PER_DAY": 1500,
    "LIMIT_AGENCY_AUDIO_SECONDS_PER_DAY": 3600,
}
STT_OPERATION = "stt"  # app.speech.OPERATION
_DAY_START = "date_trunc('day', now() AT TIME ZONE 'Asia/Karachi') AT TIME ZONE 'Asia/Karachi'"


def value(name):
    raw = os.environ.get(name, "").strip()
    try:
        number = int(raw) if raw else DEFAULTS[name]
    except ValueError:
        raise ConfigurationError(f"{name} must be a whole number") from None
    if number < 1:
        raise ConfigurationError(f"{name} must be at least 1")
    return number


def check_settings():
    """Raise ConfigurationError at worker start when a limit is unusable."""
    for name in DEFAULTS:
        value(name)


def customer_turn_limit(conn, turn):
    """Inside a tenant transaction: the name of the limit a customer turn is over, or
    None. The turn being answered is not counted."""
    hour, day = conn.execute(
        """
        SELECT count(*) FILTER (WHERE created_at > now() - interval '1 hour'), count(*)
        FROM conversation_turn
        WHERE conversation_id = %s AND route = 'customer' AND status <> 'gated'
          AND id <> %s AND created_at > now() - interval '24 hours'
        """,
        (turn.conversation_id, turn.id),
    ).fetchone()
    if hour >= value("LIMIT_CUSTOMER_TURNS_PER_HOUR"):
        return "turns_per_hour"
    if day >= value("LIMIT_CUSTOMER_TURNS_PER_DAY"):
        return "turns_per_day"
    calls = conn.execute(
        f"SELECT count(*) FROM usage_event WHERE operation <> %s AND created_at >= {_DAY_START}",
        (STT_OPERATION,),
    ).fetchone()[0]
    if calls >= value("LIMIT_AGENCY_MODEL_CALLS_PER_DAY"):
        logger.error("ALERT agency_limit agency=%s limit=model_calls_per_day used=%s",
                     turn.agency_id, calls)
        return "agency_model_calls_per_day"
    return None


def voice_limit(conn, conversation_id):
    """Inside a tenant transaction, before a customer's voice note is staged: the name
    of the limit it is over, or None."""
    notes = conn.execute(
        "SELECT count(*) FROM inbound_audio "
        "WHERE conversation_id = %s AND created_at > now() - interval '1 hour'",
        (conversation_id,),
    ).fetchone()[0]
    if notes >= value("LIMIT_CUSTOMER_VOICE_PER_HOUR"):
        return "voice_per_hour"
    seconds = conn.execute(
        f"SELECT COALESCE(sum(audio_seconds), 0) FROM usage_event "
        f"WHERE operation = %s AND outcome = 'succeeded' AND created_at >= {_DAY_START}",
        (STT_OPERATION,),
    ).fetchone()[0]
    if seconds >= value("LIMIT_AGENCY_AUDIO_SECONDS_PER_DAY"):
        logger.error("ALERT agency_limit limit=audio_seconds_per_day used=%s", int(seconds))
        return "agency_audio_seconds_per_day"
    return None
