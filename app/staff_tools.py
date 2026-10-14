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

import psycopg
from psycopg.types.json import Jsonb

from app import ai_tools, staff_cards, staff_drafts, staff_normalise
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
                   COALESCE(size_nominal_sqyd * 9, area_sqft), price_pkr, bedrooms,
                   id, version
            FROM property
            WHERE agency_id = current_agency_id()
            ORDER BY listing_code
            """
        ).fetchall()
    return [{"code": r[0], "status": r[1], "purpose": r[2], "property_type": r[3],
             "location_id": r[4], "sqft": None if r[5] is None else Decimal(r[5]),
             "price_pkr": r[6], "bedrooms": r[7], "id": r[8], "version": r[9]}
            for r in rows]


def agency_settings(conn, agency_id):
    """(city_default, marla_sqft) of the agency."""
    with tenant_transaction(conn, agency_id):
        row = conn.execute(
            "SELECT city_default, marla_sqft FROM agency WHERE id = current_agency_id()"
        ).fetchone()
    return row if row else ("KHI", 225)


def photo_count(conn, turn):
    """Photos of the turn's segment (0 for a turn without one)."""
    if turn.staff_segment_id is None:
        return 0
    with tenant_transaction(conn, turn.agency_id):
        return conn.execute(
            "SELECT count(*) FROM staff_segment_item WHERE agency_id = current_agency_id() "
            "AND segment_id = %s AND is_photo", (turn.staff_segment_id,)).fetchone()[0]


@dataclass(frozen=True)
class StoredDraft:
    id: object
    ordinal: int
    version: int
    status: str
    payload: dict


def _stored(conn, turn_id, kind):
    rows = conn.execute(
        """
        SELECT id, ordinal, version, status, payload FROM listing_draft
        WHERE agency_id = current_agency_id() AND source_turn_id = %s AND kind = %s
        ORDER BY ordinal
        """,
        (turn_id, kind),
    ).fetchall()
    return [StoredDraft(*row) for row in rows]


def _lock_turn(conn, turn):
    """Lock the turn's row (its retries run one after the other) and check it is this
    agency's turn of this staff member. Inside a tenant transaction."""
    row = conn.execute(
        "SELECT staff_id FROM conversation_turn "
        "WHERE agency_id = current_agency_id() AND id = %s FOR UPDATE",
        (turn.id,),
    ).fetchone()
    if row is None:
        raise LookupError("turn not found")
    if row[0] != turn.staff_id:
        raise ValueError("the turn belongs to another staff member")


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
        _lock_turn(conn, turn)
        existing = _stored(conn, turn.id, "create")
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
        return _stored(conn, turn.id, "create"), True


# --- update proposals (slice 4b) -------------------------------------------------------------

def _summary(row, gazetteer):
    place = gazetteer.places.get(row["location_id"]) if row["location_id"] is not None else None
    return {"code": row["code"], "status": row["status"], "type": row["property_type"],
            "purpose": row["purpose"], "price_pkr": row["price_pkr"],
            "location": place.display_name if place else None}


def _proposal(conn, turn, code, changes, raw):
    """One transaction: this turn's draft if it has one (even superseded), else supersede the
    staff member's open proposal for the listing and store the new one. Returns
    {"draft": StoredDraft, "created": bool} or {"error": code}."""
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        _lock_turn(conn, turn)
        existing = _stored(conn, turn.id, "update")
        if existing:
            return {"draft": existing[0], "created": False}
        row = conn.execute(
            "SELECT id, version, status, purpose, price_pkr FROM property "
            "WHERE agency_id = current_agency_id() AND listing_code = %s", (code,)
        ).fetchone()
        if row is None:
            return {"error": "target_not_found"}
        prop = {"id": row[0], "version": row[1], "status": row[2], "purpose": row[3],
                "price_pkr": row[4]}
        checked = staff_normalise.check_update(prop, changes)
        if "error" in checked:
            return checked
        payload = {"schema": 1, "target_code": code, "warnings": checked["warnings"],
                   "changes": {f: dict(c, raw=raw[f]) for f, c in checked["changes"].items()}}
        # a newer proposal replaces the older open one (R-14); the version bump makes the
        # old buttons stale
        conn.execute(
            "UPDATE listing_draft SET status = 'superseded', closed_at = now(), "
            "updated_at = now(), version = version + 1 "
            "WHERE agency_id = current_agency_id() AND staff_id = %s "
            "AND target_property_id = %s AND kind = 'update' AND status = 'open'",
            (turn.staff_id, prop["id"]))
        conn.execute(
            "INSERT INTO listing_draft (agency_id, staff_id, kind, target_property_id, "
            "base_property_version, source_turn_id, ordinal, payload) "
            "VALUES (current_agency_id(), %s, 'update', %s, %s, %s, 1, %s)",
            (turn.staff_id, prop["id"], prop["version"], turn.id,
             Jsonb(payload, dumps=lambda value: json.dumps(value, default=str))))
        return {"draft": _stored(conn, turn.id, "update")[0], "created": True}


def propose_update_draft(conn, turn, code, changes, raw):
    """_proposal with one retry: a concurrent proposal for the same staff member and listing
    can win the unique index (listing_draft_one_open_update_idx); the retry then supersedes it."""
    for attempt in (1, 2):
        try:
            return _proposal(conn, turn, code, changes, raw)
        except psycopg.errors.UniqueViolation:
            if attempt == 2:
                raise


class StaffBinding:
    """The staff tools' view of one staff turn: the connection, the turn, the gazetteer and
    what find_listings found, so propose_update accepts a code the staff member did not write
    only when it was the single candidate of this turn. No tool here changes a property."""

    def __init__(self, conn, turn, gazetteer, *, city_code="KHI", marla_sqft=225):
        self.conn, self.turn, self.gazetteer = conn, turn, gazetteer
        self.city_code, self.marla_sqft = city_code, marla_sqft
        self.found = None      # the last find_listings result: {"status", "candidates"}
        self.proposed = None   # the turn's proposal result (StoredDraft) once made

    def _fail(self, name, error, notice=None, codes=()):
        """An error result; the staff member still gets the reply `notice` names."""
        result = ai_tools._error(name, error)
        return ai_tools.ToolResult(name, False, result.response, error, notice,
                                   trusted_codes=frozenset(codes))

    def draft_listing(self, args, context):
        existing = self._stored("create")
        if existing:  # a retried turn: its own drafts, whatever the model says now
            return self._drafted(existing, False)
        bound = staff_drafts.bind_draft_listing(args, context.staff)
        notices = {"too_many_drafts": "staff_draft_too_many", "no_segment": None}
        if "error" in bound:
            return self._fail("draft_listing", bound["error"], notices.get(bound["error"]))
        if not any({"property_type", "size", "price", "location"} & set(d.spans)
                   for d in bound["drafts"]):
            return self._fail("draft_listing", "no_listing", "staff_draft_none")
        listings = load_listings(self.conn, self.turn.agency_id)
        drafts = staff_normalise.normalise_drafts(
            args, context.staff, self.gazetteer, marla_sqft=self.marla_sqft,
            city_code=self.city_code, listings=listings)["drafts"]
        stored, created = save_drafts(self.conn, self.turn, drafts)
        return self._drafted(stored, created)

    def _drafted(self, stored, created):
        photos = photo_count(self.conn, self.turn)
        payloads = tuple(staff_cards.draft_card(d, len(stored), self.gazetteer, photos)
                         for d in stored)
        codes = frozenset().union(*(staff_cards.draft_codes(d) for d in stored))
        response = {"status": "drafted" if created else "existing", "drafts": [
            {"ordinal": d.ordinal, "complete": not d.payload["blockers"],
             "missing": d.payload["missing"], "conflicts": len(d.payload["conflicts"]),
             "duplicates": [x["code"] for x in d.payload["duplicates"] if "code" in x],
             "proposed_code": d.payload["proposed_code"]} for d in stored]}
        return ai_tools.ToolResult("draft_listing", True, response, payloads=payloads,
                                   trusted_codes=codes)

    def _stored(self, kind):
        with tenant_transaction(self.conn, self.turn.agency_id):
            self.conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            return _stored(self.conn, self.turn.id, kind)

    def find_listings(self, args, context):
        result = staff_drafts.verify_find_listings(args, context.staff)
        if "error" in result:
            return ai_tools._error("find_listings", result["error"])
        found = staff_normalise.find_candidates(
            result["spans"], load_listings(self.conn, self.turn.agency_id), self.gazetteer,
            marla_sqft=self.marla_sqft, city_code=self.city_code)
        if "error" in found:
            return self._fail("find_listings", found["error"], "staff_find_unclear")
        self.found = found
        summaries = [_summary(row, self.gazetteer) for row in found["candidates"]]
        response = {"status": found["status"], "candidates": summaries}
        codes = frozenset(s["code"] for s in summaries)
        if found["status"] in ("several", "too_many"):  # a choice for the staff member
            return ai_tools.ToolResult(
                "find_listings", True, response, trusted_codes=codes,
                payloads=(staff_cards.candidates_reply(summaries),))
        notice = "staff_update_none" if found["status"] == "none" else None
        return ai_tools.ToolResult("find_listings", True, response, notice=notice,
                                   trusted_codes=codes)

    def propose_update(self, args, context):
        if self.proposed is not None:
            return self._fail("propose_update", "one_per_turn", "staff_one_at_a_time")
        existing = self._existing()
        if existing is not None:  # a retried turn: its own draft, whatever the model says now
            return self._result(existing, False)
        result = staff_drafts.verify_propose_update(args, context.staff)
        if "error" in result:
            return ai_tools._error("propose_update", result["error"])
        code = result["target"]
        if not result["target_in_text"]:
            only = self.found and self.found["status"] in ("single", "direct")
            if not only or code not in {r["code"] for r in self.found["candidates"]}:
                several = self.found and self.found["status"] in ("several", "too_many")
                return self._fail(
                    "propose_update", "ambiguous_target" if several else "target_not_in_text",
                    None if several else "staff_update_pick")  # a list was already sent
        changes, raw = {}, {}
        for item in result["verified"]:
            field, span = item["field"], item["span"]
            amounts = {pkr for pkr, _ in staff_normalise._amounts(span)}
            value = (amounts.pop() if len(amounts) == 1 else None) if field == "price"                 else staff_normalise.parse_status(span)
            if value is None:
                return self._invalid(code, "invalid_change")
            key = "price_pkr" if field == "price" else "status"
            changes[key], raw[key] = value, span
        outcome = propose_update_draft(self.conn, self.turn, code, changes, raw)
        if outcome.get("error") == "target_not_found":
            return self._fail("propose_update", "target_not_found",
                              ai_tools.Notice("staff_update_not_found", (("code", code),)),
                              codes=(code,))
        if "error" in outcome:
            return self._invalid(code, outcome["error"])
        self.proposed = outcome["draft"]
        return self._result(outcome["draft"], outcome["created"])

    def _existing(self):
        with tenant_transaction(self.conn, self.turn.agency_id):
            conn = self.conn
            conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            rows = _stored(conn, self.turn.id, "update")
        return rows[0] if rows else None

    def _invalid(self, code, error):
        reason = staff_cards.REASONS.get(error, staff_cards.REASONS["invalid_change"])
        return self._fail("propose_update", error,
                          ai_tools.Notice("staff_update_invalid",
                                          (("code", code), ("reason", reason))), codes=(code,))

    @staticmethod
    def _result(draft, created):
        response = {"status": "proposed" if created else "existing", "draft_id": str(draft.id),
                    "draft_status": draft.status, "target": draft.payload["target_code"],
                    "changes": draft.payload["changes"]}
        return ai_tools.ToolResult(
            "propose_update", True, response, payloads=(staff_cards.update_card(draft),),
            trusted_codes=frozenset({draft.payload["target_code"]}))
