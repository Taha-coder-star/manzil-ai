"""Task 4.3 demo seed loader: the validated demo/ dataset -> the database.

    python -m app.seed --agency-id <uuid>            # dry run (default): plan only
    python -m app.seed --agency-id <uuid> --apply    # write missing rows

Environment: SEED_OWNER_DATABASE_URL (schema owner, the migration login),
DATABASE_URL (runtime manzil_app login), and the staff phones named in
demo/staff.csv (DEMO_ADMIN_PHONE, DEMO_STAFF_PHONE, DEMO_BROKER_PHONE).
docs/DEMO_SEED.md has the runbook.

Rules:
- Insert missing rows only. An existing row is never updated; if it differs from
  the dataset (for example a listing staff have edited) that is a conflict, it is
  reported and left as it is, and the phase writes nothing.
- Two phases, two transactions, NOT globally atomic:
  1. gazetteer (location, location_alias, location_neighbor): shared tables the
     runtime login may only read, so they are written on the schema-owner
     connection in one transaction;
  2. tenant (staff, project, property): written on the runtime login through
     app.db.tenant_transaction (FORCE RLS) in one transaction.
  A committed gazetteer phase stays if the tenant phase fails; rerunning is safe.
- Every run first builds the full plan read-only (planned gazetteer rows are
  resolved in memory). With --apply each phase rebuilds its plan under an
  advisory lock in its own transaction, and rolls the phase back on any conflict
  or insertion error.
- The agency must already exist and be the demo agency; it is never created here.
- Phone numbers, connection strings and raw database errors are never printed.
"""

import argparse
import datetime
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app import demo_dataset as dd
from app.db import tenant_transaction

DEMO_AGENCY_NAME = "Manzil Demo Realty (DEMO)"
OWNER_URL_ENV = "SEED_OWNER_DATABASE_URL"
APP_URL_ENV = "DATABASE_URL"
# Transaction-scoped advisory locks. Single-bigint keys share a keyspace with the
# migration lock (60172001); two-key locks are separate (the queue uses 3101).
GAZETTEER_LOCK_ID = 60172043
TENANT_LOCK_NAMESPACE = 4301
# WhatsApp wa_id form: country code first, digits only (what webhooks deliver and
# what app.media.staff_id_for compares against).
PHONE = re.compile(r"^[1-9][0-9]{9,14}$")
TABLES = ("location", "location_alias", "location_neighbor", "staff", "project", "property")


class SeedError(Exception):
    """A failure whose message is safe to print (no phones, URLs or raw DB errors)."""


class _Conflicts(Exception):
    """Raised inside a phase transaction to roll it back."""


class _Planned:
    """A row that does not exist yet; stands in for its ID while planning."""

    def __init__(self, key):
        self.key = key

    def __repr__(self):
        return f"<not yet inserted: {self.key}>"


# A dataset fact "no value" (as opposed to a blank cell = unknown, never compared).
NONE = object()


@dataclass
class PhaseReport:
    name: str
    mode: str
    existing: dict = field(default_factory=dict)
    inserts: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    outcome: str = "not run"

    def count(self, bucket, table):
        target = self.existing if bucket == "existing" else self.inserts
        target[table] = target.get(table, 0) + 1


@dataclass
class SeedResult:
    apply: bool
    phases: list = field(default_factory=list)

    @property
    def conflicts(self):
        return [c for phase in self.phases for c in phase.conflicts]


# --- inputs --------------------------------------------------------------------

def normalize_phone(value):
    """Digits-only international form, or None when the value is not a phone."""
    if value is None:
        return None
    text = re.sub(r"[\s().-]", "", value.strip())
    text = text.removeprefix("+")
    return text if PHONE.match(text) else None


def load_dataset(demo_dir=dd.DEMO_DIR):
    try:
        data = dd.load(demo_dir)
    except (OSError, ValueError, dd.DatasetError) as exc:
        raise SeedError(f"demo dataset could not be read ({type(exc).__name__})") from None
    errors = dd.validate(data)
    if errors:
        raise SeedError(f"demo dataset is invalid ({len(errors)} errors); "
                        "run python -m app.demo_dataset")
    parse_errors = []
    data["_properties"] = [dd.parse_property(row, parse_errors)
                           for row in data["properties.csv"]]
    if parse_errors:
        raise SeedError("demo dataset is invalid; run python -m app.demo_dataset")
    return data


def resolve_phones(data, environ):
    """Staff key -> normalised phone from the variables named in staff.csv."""
    phones, problems = {}, []
    for row in data["staff.csv"]:
        name = row["phone_ref"].removeprefix("env:")
        raw = environ.get(name, "")
        if not raw.strip():
            problems.append(f"{name} is not set")
            continue
        phone = normalize_phone(raw)
        if phone is None:
            problems.append(f"{name} is not an international phone number "
                            "(country code first, digits only)")
            continue
        phones[row["key"]] = phone
    if len(set(phones.values())) != len(phones):
        problems.append("the staff phone variables must hold different numbers")
    if problems:
        raise SeedError("; ".join(problems))
    return phones


def parse_agency_id(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise SeedError("--agency-id must be a UUID") from None


# --- connections ---------------------------------------------------------------

def _connect(url, label):
    if not url:
        raise SeedError(f"{label} is not set")
    try:
        return psycopg.connect(url, autocommit=True, connect_timeout=10)
    except psycopg.Error as exc:
        raise SeedError(f"could not connect with {label} ({type(exc).__name__})") from None


FINGERPRINT = """
SELECT pg_catalog.current_database(),
       (SELECT oid FROM pg_catalog.pg_database
        WHERE datname = pg_catalog.current_database()),
       pg_catalog.pg_postmaster_start_time(),
       pg_catalog.to_regclass('location')::oid,
       pg_catalog.to_regclass('property')::oid
"""
LOGIN = """
SELECT r.rolsuper, r.rolbypassrls,
       pg_catalog.pg_has_role(c.relowner, 'USAGE')
FROM pg_catalog.pg_roles r, pg_catalog.pg_class c
WHERE r.rolname = current_user AND c.oid = pg_catalog.to_regclass('property')
"""
GAZETTEER_WRITE = """
SELECT pg_catalog.has_table_privilege('location', 'INSERT')
   AND pg_catalog.has_table_privilege('location_alias', 'INSERT')
   AND pg_catalog.has_table_privilege('location_neighbor', 'INSERT')
"""


def check_connections(owner, app):
    """Same database and schema; owner can write the gazetteer; app is restricted."""
    owner_print = owner.execute(FINGERPRINT).fetchone()
    app_print = app.execute(FINGERPRINT).fetchone()
    if owner_print[3] is None or owner_print[4] is None:
        raise SeedError("seed tables not found; apply migrations 001-009 first")
    if owner_print != app_print:
        raise SeedError("the two database URLs do not reach the same database and schema")
    if not owner.execute(GAZETTEER_WRITE).fetchone()[0]:
        raise SeedError(f"{OWNER_URL_ENV} cannot write the gazetteer (use the schema owner)")
    superuser, bypass, owns = app.execute(LOGIN).fetchone()
    if superuser or bypass or owns:
        raise SeedError(f"{APP_URL_ENV} must be the restricted runtime login "
                        "(no superuser, no BYPASSRLS, not the table owner)")


# --- planning helpers ----------------------------------------------------------

def _db_values(conn, expression, values):
    """Apply a SQL expression to each value in the database (exact DB semantics)."""
    if not values:
        return []
    rows = conn.execute(
        sql.SQL("SELECT {} FROM unnest(%s::text[]) WITH ORDINALITY AS t(v, n) ORDER BY n")
        .format(sql.SQL(expression)),
        (list(values),),
    ).fetchall()
    return [row[0] for row in rows]


def _ordered_locations(rows):
    by_key = {row["key"]: row for row in rows}
    ordered = sorted(rows, key=lambda row: len(dd.ancestors(by_key, row["key"])))
    return ordered, by_key


def _differs(existing, expected):
    if expected is NONE:
        return existing is not None
    if isinstance(expected, frozenset):
        return set(existing or []) != expected
    return existing != expected


def _compare(report, label, existing, expected):
    """Record a conflict for each dataset-specified value an existing row lacks."""
    for column, value in expected.items():
        if value is None:
            continue
        if _differs(existing[column], value):
            shown = None if value is NONE else value
            report.conflicts.append(
                f"{label}: existing {column} {existing[column]!r} differs from the "
                f"dataset {shown!r}; left unchanged")


def _insert_row(conn, table, values):
    """INSERT one row and return its id (None for tables without an id column)."""
    columns = list(values)
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() * len(columns)),
    )
    if table == "location_neighbor":
        conn.execute(query, [values[c] for c in columns])
        return None
    row = conn.execute(query + sql.SQL(" RETURNING id"), [values[c] for c in columns])
    return row.fetchone()[0]


def resolve_locations(conn, data, report=None):
    """Dataset location key -> existing id or _Planned, plus the insert plan."""
    ordered, by_key = _ordered_locations(data["locations.csv"])
    lowered = dict(zip([r["key"] for r in ordered],
                       _db_values(conn, "lower(v)", [r["name"] for r in ordered])))
    existing = {
        (row[1], row[9]): row for row in conn.execute(
            "SELECT id, parent_id, level, name, display_name, code, level_label, "
            "city_code, is_commercial_zone, lower(name) FROM location").fetchall()
    }
    codes = {row[5]: row[0] for row in existing.values() if row[5]}
    ids, plan = {}, []
    for row in ordered:
        key = row["key"]
        parent = ids.get(row["parent_key"]) if row["parent_key"] else None
        current = None if isinstance(parent, _Planned) else existing.get((parent, lowered[key]))
        expected = {
            "level": row["level"], "name": row["name"],
            "display_name": row["display_name"] or None, "code": row["code"] or None,
            "level_label": row["level_label"] or None, "city_code": row["city_code"] or None,
            "is_commercial_zone": row["is_commercial_zone"] == "true",
        }
        if current is not None:
            ids[key] = current[0]
            if report is not None:
                report.count("existing", "location")
                _compare(report, f"location {key}", dict(zip(
                    ("id", "parent_id", "level", "name", "display_name", "code",
                     "level_label", "city_code", "is_commercial_zone"), current)), expected)
            continue
        if report is not None and expected["code"] in codes:
            report.conflicts.append(
                f"location {key}: code {expected['code']} is used by another location")
        ids[key] = _Planned(key)
        parent_level = by_key[row["parent_key"]]["level"] if row["parent_key"] else None
        plan.append((key, dict(expected, parent_level=parent_level), row["parent_key"]))
        if report is not None:
            report.count("inserts", "location")
    return ids, plan


# --- gazetteer phase -----------------------------------------------------------

def _gazetteer(conn, data, report, write):
    conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (GAZETTEER_LOCK_ID,))
    ids, location_plan = resolve_locations(conn, data, report)

    aliases = data["location_aliases.csv"]
    norms = _db_values(conn, r"lower(regexp_replace(btrim(v), '\s+', ' ', 'g'))",
                       [row["alias"] for row in aliases])
    existing_aliases = {
        (row[0], row[1], row[2]): row for row in conn.execute(
            "SELECT location_id, alias_norm, context_parent_id, kind, weight "
            "FROM location_alias").fetchall()
    }
    alias_plan, planned_identities = [], set()
    for row, norm in zip(aliases, norms):
        location = ids[row["location_key"]]
        context = ids[row["context_key"]] if row["context_key"] else None
        label = f"alias {row['alias']!r} -> {row['location_key']}"
        identity = (row["location_key"], norm, row["context_key"] or None)
        if identity in planned_identities:
            report.conflicts.append(f"{label}: duplicate identity in the dataset")
            continue
        planned_identities.add(identity)
        expected = {"kind": row["kind"], "weight": Decimal(row["weight"])}
        current = None
        if not isinstance(location, _Planned) and not isinstance(context, _Planned):
            current = existing_aliases.get((location, norm, context))
        if current is not None:
            report.count("existing", "location_alias")
            _compare(report, label, {"kind": current[3], "weight": current[4]}, expected)
        else:
            report.count("inserts", "location_alias")
            alias_plan.append((row, expected))

    existing_pairs = {
        (row[0], row[1]): row[2] for row in conn.execute(
            "SELECT location_a_id, location_b_id, weight FROM location_neighbor").fetchall()
    }
    neighbor_plan = []
    for row in data["location_neighbors.csv"]:
        a, b = ids[row["location_a_key"]], ids[row["location_b_key"]]
        weight = Decimal(row["weight"])
        label = f"neighbour {row['location_a_key']} / {row['location_b_key']}"
        if not isinstance(a, _Planned) and not isinstance(b, _Planned):
            pair = tuple(sorted((a, b)))
            if pair in existing_pairs:
                report.count("existing", "location_neighbor")
                _compare(report, label, {"weight": existing_pairs[pair]}, {"weight": weight})
                continue
        report.count("inserts", "location_neighbor")
        neighbor_plan.append((row, weight))

    if report.conflicts:
        raise _Conflicts()
    if not write:
        return

    for key, values, parent_key in location_plan:
        values = dict(values, parent_id=ids[parent_key] if parent_key else None)
        ids[key] = _insert_row(conn, "location", values)
    for row, expected in alias_plan:
        _insert_row(conn, "location_alias", {
            "location_id": ids[row["location_key"]], "alias": row["alias"],
            "context_parent_id": ids[row["context_key"]] if row["context_key"] else None,
            "kind": expected["kind"], "weight": expected["weight"],
        })
    for row, weight in neighbor_plan:
        a, b = sorted((ids[row["location_a_key"]], ids[row["location_b_key"]]))
        _insert_row(conn, "location_neighbor",
                    {"location_a_id": a, "location_b_id": b, "weight": weight})


# --- tenant phase --------------------------------------------------------------

PROPERTY_COLUMNS = (
    "status", "purpose", "property_type", "category", "price_pkr", "area_value",
    "area_unit", "size_nominal_sqyd", "bedrooms", "bathrooms", "construction_status",
    "portion_level", "furnishing", "facing", "is_corner", "features",
    "possession_status", "expected_possession", "installment_available", "is_tenanted",
    "investment_tags", "source_type", "internal_notes", "is_demo",
)
PROJECT_COLUMNS = (
    "name", "location_id", "developer_name", "project_type", "construction_status",
    "expected_completion", "payment_plan_summary", "is_demo",
)


def _staff_plan(conn, data, phones, report):
    rows = conn.execute(
        "SELECT id, role, is_active, regexp_replace(whatsapp_number, '[^0-9]', '', 'g') "
        "FROM staff").fetchall()
    plan = []
    for staff in data["staff.csv"]:
        key, role = staff["key"], staff["role"]
        variable = staff["phone_ref"].removeprefix("env:")
        matches = [row for row in rows if row[3] == phones[key]]
        label = f"staff {key}"
        if len(matches) > 1:
            report.conflicts.append(
                f"{label}: {len(matches)} existing staff rows match {variable}; ambiguous")
        elif matches:
            _, existing_role, active, _ = matches[0]
            if existing_role != role:
                report.conflicts.append(
                    f"{label}: the number in {variable} belongs to an existing "
                    f"{existing_role} row, not {role}")
            elif not active:
                report.conflicts.append(
                    f"{label}: the number in {variable} belongs to an inactive {role} row")
            else:
                report.count("existing", "staff")  # reused unchanged
        elif any(row[1] == role and row[2] for row in rows):
            report.conflicts.append(
                f"{label}: an active {role} already exists with a different number than "
                f"{variable}; check the variable")
        else:
            report.count("inserts", "staff")
            plan.append({"name": staff["name"], "whatsapp_number": phones[key], "role": role})
    return plan


def _project_values(row, ids):
    return {
        "name": row["name"], "location_id": ids[row["location_key"]],
        "developer_name": row["developer_name"] or None,
        "project_type": row["project_type"] or None,
        "construction_status": row["construction_status"] or None,
        "expected_completion": (datetime.date.fromisoformat(row["expected_completion"])
                                if row["expected_completion"] else None),
        "payment_plan_summary": row["payment_plan_summary"] or None,
        "is_demo": row["is_demo"] == "true",
    }


def _tenant(conn, agency_id, data, phones, report, write):
    conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s, pg_catalog.hashtext(%s))",
                 (TENANT_LOCK_NAMESPACE, str(agency_id)))
    agency = conn.execute("SELECT name FROM agency WHERE id = %s", (agency_id,)).fetchone()
    if agency is None:
        raise SeedError("agency not found for the runtime login (check --agency-id)")
    if agency[0] != DEMO_AGENCY_NAME:
        raise SeedError(f"agency is not the demo agency {DEMO_AGENCY_NAME!r}; nothing written")

    ids, _ = resolve_locations(conn, data)
    if write and any(isinstance(v, _Planned) for v in ids.values()):
        raise SeedError("gazetteer is incomplete; the gazetteer phase must run first")

    staff_plan = _staff_plan(conn, data, phones, report)

    projects = data["projects.csv"]
    lowered = _db_values(conn, "lower(v)", [row["name"] for row in projects])
    existing_projects = {
        row[0]: row for row in conn.execute(
            "SELECT lower(name), id, " + ", ".join(PROJECT_COLUMNS) + " FROM project"
        ).fetchall()
    }
    project_ids, project_plan = {}, []
    for row, name_key in zip(projects, lowered):
        values = _project_values(row, ids)
        current = existing_projects.get(name_key)
        if current is not None:
            project_ids[row["key"]] = current[1]
            report.count("existing", "project")
            _compare(report, f"project {row['key']}",
                     dict(zip(PROJECT_COLUMNS, current[2:])), values)
        else:
            project_ids[row["key"]] = _Planned(row["key"])
            report.count("inserts", "project")
            project_plan.append((row["key"], values))

    existing_properties = {
        row[0]: row for row in conn.execute(
            "SELECT listing_code, location_id, project_id, public_features, "
            + ", ".join(PROPERTY_COLUMNS) + " FROM property").fetchall()
    }
    days = {code: entry["confirmed_days_ago"]
            for code, entry in data[dd.FRESHNESS_FILE]["listings"].items()}
    default_days = data[dd.FRESHNESS_FILE]["default_confirmed_days_ago"]
    property_plan = []
    for item in data["_properties"]:
        code = item["listing_code"]
        location = ids[item["location_key"]]
        project = project_ids[item["project_key"]] if item["project_key"] else None
        current = existing_properties.get(code)
        if current is not None:
            report.count("existing", "property")
            existing = dict(zip(("listing_code", "location_id", "project_id",
                                 "public_features") + PROPERTY_COLUMNS, current))
            expected = {column: item[column] for column in PROPERTY_COLUMNS}
            expected.update(location_id=location,
                            project_id=NONE if project is None else project,
                            public_features=item["public_features"])
            _compare(report, f"property {code}", existing, expected)
        else:
            report.count("inserts", "property")
            property_plan.append((item, days.get(code, default_days)))

    if report.conflicts:
        raise _Conflicts()
    if not write:
        return

    for values in staff_plan:
        _insert_row(conn, "staff", dict(values, agency_id=agency_id))
    for key, values in project_plan:
        project_ids[key] = _insert_row(conn, "project", dict(values, agency_id=agency_id))
    now = conn.execute("SELECT now()").fetchone()[0]
    for item, age_days in property_plan:
        values = {column: item[column] for column in PROPERTY_COLUMNS}
        for column in ("facing", "features", "investment_tags"):
            values[column] = sorted(values[column]) if values[column] is not None else None
        values.update(
            agency_id=agency_id, listing_code=item["listing_code"],
            location_id=ids[item["location_key"]],
            project_id=project_ids[item["project_key"]] if item["project_key"] else None,
            public_features=Jsonb(item["public_features"]),
            # Freshness is set on first insertion only (freshness_policy.json).
            last_confirmed_at=now - datetime.timedelta(days=age_days),
        )
        _insert_row(conn, "property", values)


# --- phases and run ------------------------------------------------------------

def _db_failure(phase, exc):
    # Only the error class, SQLSTATE and constraint name: the message and detail
    # can quote row values such as phone numbers.
    try:
        constraint = exc.diag.constraint_name
    except Exception:  # noqa: BLE001 - diagnostics are optional
        constraint = None
    detail = f"{type(exc).__name__}, SQLSTATE {getattr(exc, 'sqlstate', None) or 'n/a'}"
    if constraint:
        detail += f", constraint {constraint}"
    return SeedError(f"{phase} phase rolled back after a database error ({detail})")


def gazetteer_phase(conn, data, write):
    report = PhaseReport("gazetteer", "apply" if write else "plan")
    try:
        with conn.transaction():
            if not write:
                conn.execute("SET TRANSACTION READ ONLY")
            _gazetteer(conn, data, report, write)
            if not write:
                raise psycopg.Rollback()
        report.outcome = "committed" if write else "planned (read-only, rolled back)"
    except _Conflicts:
        report.outcome = "rolled back: conflicts"
    except psycopg.Error as exc:
        raise _db_failure("gazetteer", exc) from None
    return report


def tenant_phase(conn, agency_id, data, phones, write):
    report = PhaseReport("tenant", "apply" if write else "plan")
    try:
        with tenant_transaction(conn, agency_id):
            if not write:
                conn.execute("SET TRANSACTION READ ONLY")
            _tenant(conn, agency_id, data, phones, report, write)
            if not write:
                raise psycopg.Rollback()
        report.outcome = "committed" if write else "planned (read-only, rolled back)"
    except _Conflicts:
        report.outcome = "rolled back: conflicts"
    except psycopg.Error as exc:
        raise _db_failure("tenant", exc) from None
    return report


def preflight(owner, app, agency_id, data, phones):
    """The full read-only plan for both phases."""
    return [gazetteer_phase(owner, data, write=False),
            tenant_phase(app, agency_id, data, phones, write=False)]


def run(agency_id, owner_url, app_url, environ, demo_dir=dd.DEMO_DIR, apply=False):
    data = load_dataset(demo_dir)
    phones = resolve_phones(data, environ)
    agency_id = parse_agency_id(agency_id)
    result = SeedResult(apply=apply)
    owner = _connect(owner_url, OWNER_URL_ENV)
    try:
        app = _connect(app_url, APP_URL_ENV)
    except SeedError:
        owner.close()
        raise
    with owner, app:
        try:
            check_connections(owner, app)
        except psycopg.Error as exc:
            raise SeedError(f"connection check failed ({type(exc).__name__})") from None
        result.phases = preflight(owner, app, agency_id, data, phones)
        if not apply or result.conflicts:
            return result
        # Each phase rechecks its plan under its lock, in its own transaction.
        gazetteer = gazetteer_phase(owner, data, write=True)
        result.phases = [gazetteer]
        if gazetteer.conflicts:
            return result
        try:
            result.phases.append(tenant_phase(app, agency_id, data, phones, write=True))
        except SeedError as exc:
            raise SeedError(f"{exc}. The gazetteer phase had already committed; "
                            "rerunning is safe.") from None
    return result


# --- CLI -----------------------------------------------------------------------

def _print_result(result):
    print("Mode: apply" if result.apply else "Mode: dry run (no writes; use --apply to write)")
    print("Phases are separate transactions and not globally atomic: the gazetteer phase "
          "commits before the tenant phase; after a tenant failure, rerun.")
    for phase in result.phases:
        parts = [f"{t} +{phase.inserts.get(t, 0)} ={phase.existing.get(t, 0)}"
                 for t in TABLES if t in phase.inserts or t in phase.existing]
        print(f"{phase.name} [{phase.mode}] {phase.outcome}: " + ", ".join(parts))
        for conflict in phase.conflicts:
            print(f"  CONFLICT {conflict}")
    print("(+N = to insert / inserted, =N = already present and left unchanged)")


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agency-id", required=True)
    parser.add_argument("--apply", action="store_true", help="write missing rows")
    parser.add_argument("--demo-dir", default=str(dd.DEMO_DIR))
    args = parser.parse_args(argv)
    try:
        result = run(args.agency_id, environ.get(OWNER_URL_ENV, "").strip(),
                     environ.get(APP_URL_ENV, "").strip(), environ,
                     demo_dir=args.demo_dir, apply=args.apply)
    except SeedError as exc:
        print(f"Seed failed: {exc}")
        return 1
    _print_result(result)
    return 2 if result.conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
