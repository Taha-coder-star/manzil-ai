"""Transactions scoped to an agency already authorized by trusted application code."""

from contextlib import contextmanager
from uuid import UUID

from psycopg.pq import TransactionStatus


@contextmanager
def tenant_transaction(conn, agency_id):
    """Yield an exclusive, idle connection inside a tenant-scoped transaction.

    UUID validation is not authorization: never pass arbitrary customer input.
    Do not share the connection concurrently, set session-level tenant context,
    or commit/rollback manually inside this block. Nested transactions are rejected.
    """
    if not isinstance(agency_id, (str, UUID)):
        raise TypeError("agency_id must be a UUID")
    try:
        tenant_id = UUID(str(agency_id))
    except ValueError:
        raise ValueError("agency_id must be a UUID") from None

    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("tenant_transaction requires an idle connection")

    with conn.transaction():
        previous = conn.execute(
            "SELECT pg_catalog.current_setting('app.agency_id', true)"
        ).fetchone()[0]
        if previous:
            raise ValueError("Connection has session-level tenant context; discard it")
        conn.execute(
            "SELECT pg_catalog.set_config('app.agency_id', %s, true)",
            (str(tenant_id),),
        )
        yield conn
