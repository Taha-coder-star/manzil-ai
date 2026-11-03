"""Demo-data reset: put the demo agency back to the seeded dataset between test runs.

    python -m app.demo_reset --agency-id <uuid>            # dry run (default): plan only
    python -m app.demo_reset --agency-id <uuid> --apply    # write

Environment: DATABASE_URL (runtime manzil_app login). docs/DEMO_SEED.md has the runbook.

manzil_app cannot delete listings, events or conversation history, and a reset must not get
broader rights, so history is detached instead of removed:
- A listing outside the dataset gets a TEST-<n> code and goes WITHDRAWN. Codes are allocated
  from the highest of a prefix over every status, so this is what frees DH6-109 again.
- A seeded listing that changed goes back to the dataset, with version + 1 so an old Undo or
  draft of it stops with "later change". No property_event: "updated" is undoable, so
  "#undo CODE" would put the test value back. Older events stay as they are.
- Photos staff attached to seeded listings are deleted (their S3 objects stay); loaded demo
  photos have no source_inbound_media_id and stay.
- Customer conversations are renamed to retired-<id> and closed, so the next message from that
  phone starts a new one. shown_item and media_sent cannot be cleared, and left in place they
  would change search order and hold back "more photos". Their open leads and handoffs close.
  Staff and broker conversations stay: a new one would have a closed WhatsApp window.
- Open listing drafts are cancelled and staff sessions deleted.

One transaction, rolled back on any conflict or failed check. Lead numbers keep rising.
A pending job is a conflict (wait, then rerun); handoff timers are not, they do nothing once
their handoff is closed. Phone numbers, connection strings and raw errors are never printed.
"""

import argparse
import datetime
import os
import sys
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app import demo_dataset as dd
from app import seed
from app.db import tenant_transaction
from app.handoff import RELEASE_JOB, TIMEOUT_JOB
from app.staff_normalise import next_code

RETIRED_PREFIX = "TEST"
NO_WAIT_JOBS = [TIMEOUT_JOB, RELEASE_JOB]
ARRAY_COLUMNS = ("facing", "features", "investment_tags")


@dataclass
class ResetResult:
    apply: bool
    outcome: str = "not run"
    conflicts: list = field(default_factory=list)
    retired: list = field(default_factory=list)  # (old code, new code)
    restored: dict = field(default_factory=dict)  # code -> changed columns
    photos: int = 0
    drafts: int = 0
    sessions: int = 0
    conversations: int = 0
    leads: int = 0
    handoffs: int = 0


def _ids(conn, query, *params):
    return [row[0] for row in conn.execute(query, params).fetchall()]


def _expected(item, location_id, project_id):
    """The columns a seeded listing has when inserted, including the None of unknown facts."""
    values = {column: item[column] for column in seed.PROPERTY_COLUMNS}
    values.update(location_id=location_id, project_id=project_id,
                  public_features=item["public_features"])
    return values


def _differs(existing, expected):
    if isinstance(expected, frozenset):
        return set(existing or []) != expected
    return existing != expected


def _listings(conn, data, result, write):
    locations, _ = seed.resolve_locations(conn, data)
    projects = dict(conn.execute("SELECT lower(name), id FROM project").fetchall())
    keys = dict(zip([row["key"] for row in data["projects.csv"]],
                    seed._db_values(conn, "lower(v)",
                                    [row["name"] for row in data["projects.csv"]])))
    columns = ("location_id", "project_id", "public_features") + seed.PROPERTY_COLUMNS
    rows = {
        row[0]: row for row in conn.execute(
            "SELECT listing_code, id, created_at, last_confirmed_at, "
            + ", ".join(columns) + " FROM property").fetchall()
    }
    days = {code: entry["confirmed_days_ago"]
            for code, entry in data[dd.FRESHNESS_FILE]["listings"].items()}
    default_days = data[dd.FRESHNESS_FILE]["default_confirmed_days_ago"]

    plan, seeded_ids = [], []
    for item in data["_properties"]:
        code = item["listing_code"]
        row = rows.get(code)
        location = locations[item["location_key"]]
        project = projects.get(keys[item["project_key"]]) if item["project_key"] else None
        if row is None or isinstance(location, seed._Planned) or (
                item["project_key"] and project is None):
            result.conflicts.append(f"listing {code}: not fully seeded; run app.seed first")
            continue
        seeded_ids.append(row[1])
        existing = dict(zip(columns, row[4:]))
        expected = _expected(item, location, project)
        changes = {c: v for c, v in expected.items() if _differs(existing[c], v)}
        # Set at insertion only: now() there is also the row's created_at.
        age = days.get(code, default_days)
        confirmed = row[2] - datetime.timedelta(days=age)
        if row[3] != confirmed:
            changes["last_confirmed_at"] = confirmed
        if changes:
            result.restored[code] = sorted(changes)
            plan.append((row[1], changes))

    dataset_codes = {item["listing_code"] for item in data["_properties"]}
    used = [code for code in rows if code.startswith(f"{RETIRED_PREFIX}-")]
    retire = []
    for code in sorted(set(rows) - dataset_codes - set(used)):
        new_code = next_code(RETIRED_PREFIX, used)
        used.append(new_code)
        result.retired.append((code, new_code))
        retire.append((rows[code][1], code, new_code))

    photos = _ids(conn, "SELECT id FROM property_media WHERE property_id = ANY(%s) "
                        "AND source_inbound_media_id IS NOT NULL", seeded_ids)
    result.photos = len(photos)
    if result.conflicts or not write:
        return

    for property_id, old_code, new_code in retire:
        conn.execute(
            "UPDATE property SET listing_code = %s, status = 'withdrawn', "
            "internal_notes = concat_ws(' ', internal_notes, %s::text), "
            "version = version + 1, updated_at = now() WHERE id = %s",
            (new_code, f"Retired by the demo reset; was {old_code}.", property_id))
    conn.execute("DELETE FROM property_media WHERE id = ANY(%s)", (photos,))
    for property_id, changes in plan:
        values = {c: sorted(v) if c in ARRAY_COLUMNS and v is not None
                  else Jsonb(v) if c == "public_features" else v
                  for c, v in changes.items()}
        conn.execute(
            sql.SQL("UPDATE property SET {}, version = version + 1, updated_at = now() "
                    "WHERE id = {}").format(
                sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder())
                                   for c in values),
                sql.Placeholder()),
            [*values.values(), property_id])


def _staff_state(conn, result, write):
    drafts = _ids(conn, "SELECT id FROM listing_draft WHERE status = 'open'")
    sessions = _ids(conn, "SELECT id FROM staff_session")
    result.drafts, result.sessions = len(drafts), len(sessions)
    if write:
        conn.execute("UPDATE listing_draft SET status = 'cancelled', closed_at = now(), "
                     "updated_at = now() WHERE id = ANY(%s)", (drafts,))
        conn.execute("DELETE FROM staff_session WHERE id = ANY(%s)", (sessions,))


def _conversations(conn, result, write):
    customers = _ids(conn, """
        SELECT c.id FROM conversation c
        WHERE c.customer_wa_id NOT LIKE 'retired-%%' AND NOT EXISTS (
            SELECT 1 FROM staff s
            WHERE regexp_replace(s.whatsapp_number, '[^0-9]', '', 'g')
                = regexp_replace(c.customer_wa_id, '[^0-9]', '', 'g'))
        """)
    leads = _ids(conn, "SELECT id FROM lead WHERE status = 'open' AND conversation_id = ANY(%s)",
                 customers)
    handoffs = _ids(conn, "SELECT id FROM handoff WHERE status IN ('pending', 'accepted') "
                          "AND conversation_id = ANY(%s)", customers)
    result.conversations, result.leads, result.handoffs = (
        len(customers), len(leads), len(handoffs))
    if write:
        conn.execute("UPDATE lead SET status = 'closed', closed_at = now(), updated_at = now() "
                     "WHERE id = ANY(%s)", (leads,))
        conn.execute("UPDATE handoff SET status = 'cancelled', closed_at = now(), "
                     "updated_at = now() WHERE id = ANY(%s)", (handoffs,))
        conn.execute("UPDATE conversation SET customer_wa_id = 'retired-' || id::text, "
                     "control_state = 'closed', mode = 'paused', updated_at = now() "
                     "WHERE id = ANY(%s)", (customers,))


def _reset(conn, agency_id, data, result, write):
    # Same lock as the seed loader: the two never run at once.
    conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s, pg_catalog.hashtext(%s))",
                 (seed.TENANT_LOCK_NAMESPACE, str(agency_id)))
    agency = conn.execute("SELECT name FROM agency WHERE id = %s", (agency_id,)).fetchone()
    if agency is None:
        raise seed.SeedError("agency not found for the runtime login (check --agency-id)")
    if agency[0] != seed.DEMO_AGENCY_NAME:
        raise seed.SeedError(
            f"agency is not the demo agency {seed.DEMO_AGENCY_NAME!r}; nothing written")

    for job_type, count in conn.execute(
            "SELECT job_type, count(*) FROM job WHERE status IN ('pending', 'running') "
            "AND job_type <> ALL(%s) GROUP BY job_type ORDER BY job_type",
            (NO_WAIT_JOBS,)).fetchall():
        result.conflicts.append(f"{count} {job_type} job(s) still to run; wait, then rerun")

    _listings(conn, data, result, write and not result.conflicts)
    if result.conflicts:
        raise seed._Conflicts()
    _staff_state(conn, result, write)
    _conversations(conn, result, write)

    if write:
        public = conn.execute("SELECT count(*) FROM v_public_listing").fetchone()[0]
        expected = sum(item["status"] == "active" for item in data["_properties"])
        if public != expected:
            raise seed.SeedError(f"reset rolled back: {public} public listings, "
                                 f"expected {expected}")


def run(agency_id, app_url, demo_dir=dd.DEMO_DIR, apply=False):
    data = seed.load_dataset(demo_dir)
    agency_id = seed.parse_agency_id(agency_id)
    result = ResetResult(apply=apply)
    with seed._connect(app_url, seed.APP_URL_ENV) as conn:
        try:
            superuser, bypass, owns = conn.execute(seed.LOGIN).fetchone()
            if superuser or bypass or owns:
                raise seed.SeedError(f"{seed.APP_URL_ENV} must be the restricted runtime login "
                                     "(no superuser, no BYPASSRLS, not the table owner)")
            with tenant_transaction(conn, agency_id):
                if not apply:
                    conn.execute("SET TRANSACTION READ ONLY")
                _reset(conn, agency_id, data, result, apply)
                if not apply:
                    raise psycopg.Rollback()
            result.outcome = "committed" if apply else "planned (read-only, rolled back)"
        except seed._Conflicts:
            result.outcome = "rolled back: conflicts"
        except psycopg.Error as exc:
            raise seed._db_failure("reset", exc) from None
    return result


def _print_result(result):
    print("Mode: apply" if result.apply else "Mode: dry run (no writes; use --apply to write)")
    print(f"Reset {result.outcome}")
    for old, new in result.retired:
        print(f"  retire {old} -> {new} (withdrawn)")
    for code, columns in sorted(result.restored.items()):
        print(f"  restore {code}: {', '.join(columns)}")
    print(f"Attached photos {result.photos}, open drafts {result.drafts}, "
          f"staff sessions {result.sessions}, customer conversations {result.conversations}, "
          f"open leads {result.leads}, open handoffs {result.handoffs}")
    for conflict in result.conflicts:
        print(f"  CONFLICT {conflict}")


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agency-id", required=True)
    parser.add_argument("--apply", action="store_true", help="write the reset")
    parser.add_argument("--demo-dir", default=str(dd.DEMO_DIR))
    args = parser.parse_args(argv)
    try:
        result = run(args.agency_id, environ.get(seed.APP_URL_ENV, "").strip(),
                     demo_dir=args.demo_dir, apply=args.apply)
    except seed.SeedError as exc:
        print(f"Reset failed: {exc}")
        return 1
    _print_result(result)
    return 2 if result.conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
