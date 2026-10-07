"""Trusted sender-to-agency routing (Task 2.4).

The tenant comes only from the receiving business phone_number_id of an
authenticated webhook, never from message content. The demo resolves it from
private configuration; a database or platform resolver can replace
EnvSenderResolver later without changing the ingest pipeline.
"""

import enum
from typing import Protocol
from uuid import UUID


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
