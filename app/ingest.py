"""Durable, idempotent webhook ingestion (Task 2.4).

Each event becomes one inbound_event plus its linked pending job, written in a
single statement inside a tenant transaction. The unique keys on
(agency_id, event_key) and (agency_id, dedupe_key) make retries and concurrent
duplicate deliveries no-ops without a check-then-insert race.
"""

import logging
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from app.db import tenant_transaction
from app.routing import AgencyCheck, RoutingConfigError, verify_agency

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT = "5s"

_STORE_EVENT_SQL = """
WITH new_event AS (
    INSERT INTO inbound_event (agency_id, event_key, event_type, provider_message_id, payload)
    VALUES (%(agency_id)s, %(event_key)s, %(event_type)s, %(provider_message_id)s, %(payload)s)
    ON CONFLICT (agency_id, event_key) DO NOTHING
    RETURNING id
), new_job AS (
    INSERT INTO job (
        agency_id, inbound_event_id, job_type, dedupe_key, payload, conversation_key
    )
    SELECT %(agency_id)s, id, %(job_type)s, %(dedupe_key)s, %(job_payload)s,
           %(conversation_key)s
    FROM new_event
    ON CONFLICT (agency_id, dedupe_key) DO NOTHING
    RETURNING id
)
SELECT (SELECT count(*) FROM new_event), (SELECT count(*) FROM new_job)
"""


class IngestError(Exception):
    """Persistence did not complete; the delivery must not be acknowledged."""


@dataclass
class IngestResult:
    stored: int = 0
    duplicate: int = 0
    unknown_sender: int = 0
    inactive_agency: int = 0


def store_event(conn, agency_id, event):
    """Insert one event and its job; return True if new, False if a duplicate."""
    events, jobs = conn.execute(_STORE_EVENT_SQL, {
        "agency_id": agency_id,
        "event_key": event.event_key,
        "event_type": event.event_type,
        "provider_message_id": event.provider_message_id,
        "payload": Jsonb(event.payload),
        "job_type": event.job_type,
        "dedupe_key": event.dedupe_key,
        "job_payload": Jsonb(event.job_payload),
        "conversation_key": event.conversation_key,
    }).fetchone()
    if events != jobs:
        # A job already holds this dedupe key without its event: never leave
        # an event without its job. Raising rolls back the tenant transaction.
        raise IngestError("event inserted without its job")
    return events == 1


def _ingest_group(database_url, agency_id, phone_number_id, events, result):
    with psycopg.connect(
        database_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
    ) as conn, tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        check = verify_agency(conn, phone_number_id)
        if check is AgencyCheck.MISMATCH:
            raise RoutingConfigError("Configured agency does not own this sender")
        if check is AgencyCheck.INACTIVE:
            result.inactive_agency += len(events)
            return
        for event in events:
            if store_event(conn, agency_id, event):
                result.stored += 1
            else:
                result.duplicate += 1


def ingest_events(database_url, resolver, events):
    """Persist events per agency; each agency's events commit in one transaction.

    Raises on any routing or persistence failure so the caller answers non-2xx
    and Meta retries the whole envelope; groups already committed then dedupe.
    """
    groups = {}
    result = IngestResult()
    for event in events:
        agency_id = resolver.resolve(event.phone_number_id)
        if agency_id is None:
            # Never fall back to a default agency.
            result.unknown_sender += 1
            logger.warning(
                "Webhook event dropped: unknown sender ...%s", event.phone_number_id[-4:]
            )
            continue
        groups.setdefault((agency_id, event.phone_number_id), []).append(event)

    for (agency_id, phone_number_id), group in groups.items():
        _ingest_group(database_url, agency_id, phone_number_id, group, result)
    if result.inactive_agency:
        logger.warning("Webhook events dropped for inactive agency: %d", result.inactive_agency)
    return result
