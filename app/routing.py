"""Trusted sender-to-agency routing (Task 2.4).

The tenant comes only from the receiving business phone_number_id of an
authenticated webhook, never from message content. Audit WP5 (TD-11): the
sender_route table (migration 021) is the source, read through
DatabaseSenderResolver with a short cache; while it is empty,
WEBHOOK_SENDER_AGENCIES is used as before (EnvSenderResolver).
"""

import enum
import threading
import time
from typing import Protocol
from uuid import UUID

import psycopg

ROUTE_CACHE_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 5


class RoutingConfigError(Exception):
    """Routing is misconfigured; the event must not be acknowledged."""


class SenderResolver(Protocol):
    def resolve(self, phone_number_id: str) -> UUID | None:
        """Return the agency for a connected sender, or None if it is unknown."""


def parse_sender_agencies(raw):
    """Parse 'phone_number_id=agency_uuid[,phone_number_id=agency_uuid]'."""
    if not raw:
        raise RoutingConfigError("WEBHOOK_SENDER_AGENCIES is not set")
    mapping = {}
    for item in raw.split(","):
        phone_number_id, sep, agency = (part.strip() for part in item.partition("="))
        if not sep or not phone_number_id or not agency:
            raise RoutingConfigError("WEBHOOK_SENDER_AGENCIES has a malformed entry")
        if phone_number_id in mapping:
            raise RoutingConfigError("WEBHOOK_SENDER_AGENCIES repeats a phone_number_id")
        try:
            mapping[phone_number_id] = UUID(agency)
        except ValueError:
            raise RoutingConfigError(
                "WEBHOOK_SENDER_AGENCIES has an invalid agency id"
            ) from None
    return mapping


class EnvSenderResolver:
    def __init__(self, mapping):
        self._mapping = dict(mapping)

    @classmethod
    def from_config(cls, raw):
        return cls(parse_sender_agencies(raw))

    def resolve(self, phone_number_id):
        return self._mapping.get(phone_number_id)


class DatabaseSenderResolver:
    """Routes from sender_route, refreshed at most every ROUTE_CACHE_SECONDS, so a new
    agency is reachable without a restart. An empty table falls back to the env map
    (WEBHOOK_SENDER_AGENCIES); empty table and no env map raise RoutingConfigError.
    A database error raises psycopg.Error (the webhook answers 503; Meta retries)."""

    def __init__(self, database_url, fallback_raw, *, ttl=ROUTE_CACHE_SECONDS,
                 clock=time.monotonic):
        self._url, self._fallback_raw = database_url, fallback_raw
        self._ttl, self._clock = ttl, clock
        self._lock = threading.Lock()
        self._mapping, self._expires = None, 0.0

    def routes(self):
        with self._lock:
            if self._mapping is None or self._clock() >= self._expires:
                with psycopg.connect(self._url, autocommit=True,
                                     connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
                    rows = conn.execute(
                        "SELECT meta_phone_number_id, agency_id FROM sender_route").fetchall()
                self._mapping = dict(rows) or parse_sender_agencies(self._fallback_raw)
                self._expires = self._clock() + self._ttl
            return dict(self._mapping)

    def resolve(self, phone_number_id):
        return self.routes().get(phone_number_id)

    def agency_ids(self):
        return sorted(set(self.routes().values()), key=str)


class AgencyCheck(enum.Enum):
    OK = "ok"
    INACTIVE = "inactive"
    MISMATCH = "mismatch"


def verify_agency(conn, phone_number_id):
    """Inside tenant_transaction: confirm the resolved agency owns this sender.

    RLS limits the query to the agency in context, so a missing row or a
    different meta_phone_number_id means the routing configuration is wrong.
    """
    row = conn.execute(
        "SELECT is_active, meta_phone_number_id FROM agency WHERE id = current_agency_id()"
    ).fetchone()
    if row is None or row[1] != phone_number_id:
        return AgencyCheck.MISMATCH
    return AgencyCheck.OK if row[0] else AgencyCheck.INACTIVE
