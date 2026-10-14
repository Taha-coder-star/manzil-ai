"""Staff tools, database layer (Task 8.2).

Everything runs in the caller's agency: tenant_transaction sets the RLS context and
every statement also names current_agency_id(), so another agency's listings can
neither be compared with a draft nor take part in a code sequence. A staff turn's
create drafts are stored once under (agency_id, source_turn_id, ordinal) (migration 016);
nothing here writes a property, reserves a listing code or exposes a Save action (9.2).
Never log rows, payloads or message text.
"""

import json
from dataclasses import dataclass
from decimal import Decimal

from psycopg.types.json import Jsonb

from app import staff_normalise
from app.db import tenant_transaction

STATEMENT_TIMEOUT = "5s"
MAX_DRAFTS = 5


def load_listings(conn, agency_id):
    """The agency's listings, every status, as the rows staff_normalise compares: code,
    status, purpose, property_type, location_id, sqft (the nominal sq-yd bucket when there
    is one, else the actual size), price_pkr, bedrooms."""
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        rows = conn.execute(
            """
            SELECT listing_code, status, purpose, property_type, location_id,
                   COALESCE(size_nominal_sqyd * 9, area_sqft), price_pkr, bedrooms
            FROM property
            WHERE agency_id = current_agency_id()
            ORDER BY listing_code
            """
        ).fetchall()
    return [{"code": r[0], "status": r[1], "purpose": r[2], "property_type": r[3],
             "location_id": r[4], "sqft": None if r[5] is None else Decimal(r[5]),
             "price_pkr": r[6], "bedrooms": r[7]} for r in rows]


@dataclass(frozen=True)
class StoredDraft:
    id: object
    ordinal: int
    version: int
    status: str
    payload: dict


def _stored(conn, turn_id):
    rows = conn.execute(
        """
        SELECT id, ordinal, version, status, payload FROM listing_draft
        WHERE agency_id = current_agency_id() AND source_turn_id = %s ORDER BY ordinal
        """,
        (turn_id,),
    ).fetchall()
    return [StoredDraft(*row) for row in rows]


def save_drafts(conn, turn, drafts):
    """Store 1-5 create drafts of a staff turn in one tenant transaction and return
    ([StoredDraft], created). A turn that already has drafts returns them unchanged, whatever
    `drafts` now says: the model's second answer to a retried turn changes nothing.

    The turn row is locked first, so two retries of one turn run one after the other (the
    second finds the first's rows) and the unique index is only the last line of defence.
    """
    if turn.route != "staff" or turn.staff_id is None:
        raise ValueError("only a staff turn can draft listings")
    if not 1 <= len(drafts) <= MAX_DRAFTS:
        raise ValueError(f"1-{MAX_DRAFTS} drafts per turn")
    if [d.ordinal for d in drafts] != list(range(1, len(drafts) + 1)):
        raise ValueError("draft ordinals must run 1..n")
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = conn.execute(
            "SELECT staff_id FROM conversation_turn "
            "WHERE agency_id = current_agency_id() AND id = %s FOR UPDATE",
            (turn.id,),
        ).fetchone()
        if row is None:
            raise LookupError("turn not found")
        if row[0] != turn.staff_id:
            raise ValueError("the turn belongs to another staff member")
        existing = _stored(conn, turn.id)
        if existing:
            return existing, False
        for draft in drafts:
            conn.execute(
                """
                INSERT INTO listing_draft (agency_id, staff_id, kind, source_turn_id,
                                           ordinal, payload)
                VALUES (current_agency_id(), %s, 'create', %s, %s, %s)
                """,
                (turn.staff_id, turn.id, draft.ordinal,
                 Jsonb(staff_normalise.to_payload(draft),
                       dumps=lambda value: json.dumps(value, default=str))),
            )
        return _stored(conn, turn.id), True
