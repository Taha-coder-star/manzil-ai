"""Task 4.3 checkpoint 2: the demo seed loader against a migrated, disposable schema.

All data is fictional. Tenant writes and checks run as the restricted manzil_app
login; the gazetteer is written on the schema-owner connection.
"""

import datetime
import threading
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app import seed
from app.db import tenant_transaction
from app.provision_agency import create_agency

# Synthetic numbers (555 range); never real phones.
ENV = {
    "DEMO_ADMIN_PHONE": "15550000101",
    "DEMO_STAFF_PHONE": "+1 555 000 0102",
    "DEMO_BROKER_PHONE": "1-555-000-0103",
}
SEED_TABLES = ("location", "location_alias", "location_neighbor", "staff", "project",
               "property", "property_event")


@pytest.fixture
def db(app_role_db):
    admin_url, app_url, _ = app_role_db
    with psycopg.connect(app_url, autocommit=True) as conn:
        agency = create_agency(conn, seed.DEMO_AGENCY_NAME, "demo-" + uuid.uuid4().hex,
                               uuid.uuid4().hex)
    return admin_url, app_url, agency


def run(db, apply=True, env=None, agency=None):
    admin_url, app_url, demo_agency = db
    return seed.run(agency or demo_agency, admin_url, app_url, dict(ENV, **(env or {})),
                    apply=apply)


def tenant_rows(app_url, agency, query, params=()):
    with psycopg.connect(app_url, autocommit=True) as conn, tenant_transaction(conn, agency):
        return conn.execute(query, params).fetchall()


def tenant_counts(app_url, agency):
    return {
        table: tenant_rows(app_url, agency, f"SELECT count(*) FROM {table}")[0][0]
        for table in ("staff", "project", "property", "v_public_listing")
    }


def owner_counts(admin_url):
    with psycopg.connect(admin_url, autocommit=True) as conn:
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in SEED_TABLES}


def snapshot(app_url, agency):
    return {
        "property": tenant_rows(app_url, agency, "SELECT listing_code, id, created_at, "
                                "updated_at, last_confirmed_at, version FROM property "
                                "ORDER BY listing_code"),
        "staff": tenant_rows(app_url, agency, "SELECT id, name, whatsapp_number, role, "
                             "updated_at FROM staff ORDER BY id"),
        "project": tenant_rows(app_url, agency, "SELECT id, name, updated_at FROM project "
                               "ORDER BY id"),
    }


def add_staff(app_url, agency, name, number, role):
    with psycopg.connect(app_url, autocommit=True) as conn, tenant_transaction(conn, agency):
        return conn.execute(
            "INSERT INTO staff (agency_id, name, whatsapp_number, role) "
            "VALUES (%s, %s, %s, %s) RETURNING id", (agency, name, number, role)
        ).fetchone()[0]


def inserts(result, phase):
    return next(p for p in result.phases if p.name == phase).inserts


# --- no database ---------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    ("15550000101", "15550000101"),
    ("+1 555 000 0102", "15550000102"),
    ("1-555-000-0103", "15550000103"),
    ("(1) 555.000.0104", "15550000104"),
    ("05550000101", None),   # national form, no country code
    ("12345", None),
    ("1555000010x", None),
    ("", None),
])
def test_normalize_phone(raw, expected):
    assert seed.normalize_phone(raw) == expected


def test_phone_inputs_are_checked_before_any_connection():
    data = seed.load_dataset()
    with pytest.raises(seed.SeedError) as missing:
        seed.resolve_phones(data, {k: v for k, v in ENV.items() if k != "DEMO_ADMIN_PHONE"})
    assert "DEMO_ADMIN_PHONE is not set" in str(missing.value)
    secret = "92-300-SECRET-1"
    with pytest.raises(seed.SeedError) as bad:
        seed.resolve_phones(data, dict(ENV, DEMO_BROKER_PHONE=secret))
    assert "DEMO_BROKER_PHONE" in str(bad.value) and secret not in str(bad.value)
    with pytest.raises(seed.SeedError, match="different numbers"):
        seed.resolve_phones(data, dict(ENV, DEMO_BROKER_PHONE=ENV["DEMO_ADMIN_PHONE"]))


def test_bad_agency_id_and_missing_urls_fail_before_connecting():
    with pytest.raises(seed.SeedError, match="UUID"):
        seed.run("not-a-uuid", "x", "y", ENV)
    with pytest.raises(seed.SeedError, match="SEED_OWNER_DATABASE_URL is not set"):
        seed.run(str(uuid.uuid4()), "", "y", ENV)


def test_connection_failure_does_not_leak_the_url():
    secret = "s3cret-" + uuid.uuid4().hex
    url = f"postgresql://manzil:{secret}@127.0.0.1:1/nowhere"
    with pytest.raises(seed.SeedError) as failure:
        seed.run(str(uuid.uuid4()), url, url, ENV)
    message = str(failure.value)
    assert secret not in message and "127.0.0.1" not in message
    assert failure.value.__cause__ is None and failure.value.__suppress_context__


# --- database ------------------------------------------------------------------

def test_fresh_agency_is_fully_seeded(db):
    admin_url, app_url, agency = db
    result = run(db)
    assert result.conflicts == []
    assert [p.outcome for p in result.phases] == ["committed", "committed"]
    assert inserts(result, "gazetteer") == {
        "location": 53, "location_alias": 87, "location_neighbor": 3}
    assert inserts(result, "tenant") == {"staff": 3, "project": 2, "property": 30}
    assert tenant_counts(app_url, agency) == {
        "staff": 3, "project": 2, "property": 30, "v_public_listing": 27}
    assert owner_counts(admin_url)["property_event"] == 0

    staff = dict(tenant_rows(app_url, agency, "SELECT role, whatsapp_number FROM staff"))
    assert staff == {"admin": "15550000101", "agent": "15550000102",
                     "broker": "15550000103"}
    hidden = dict(tenant_rows(app_url, agency,
                              "SELECT listing_code, status FROM property "
                              "WHERE status <> 'active'"))
    assert hidden == {"DH6-105": "sold", "DH6-106": "reserved", "CLF-104": "rented"}
    btk, = tenant_rows(app_url, agency, "SELECT area_value, area_unit, size_nominal_sqyd, "
                       "area_sqft FROM property WHERE listing_code = 'BTK-103'")
    assert (btk[0], btk[1], btk[2], btk[3]) == (272, "sq_yd", 250, 2448)
    dh8, = tenant_rows(app_url, agency, "SELECT price_pkr, bedrooms, area_value "
                       "FROM property WHERE listing_code = 'DH8-101'")
    assert dh8 == (74_000_000, 4, 500)
    features, = tenant_rows(app_url, agency, "SELECT public_features, facing, features "
                            "FROM property WHERE listing_code = 'CLF-102'")
    assert features == ({"current_rent_pkr": 120000}, None, None)
    projects = dict(tenant_rows(app_url, agency, "SELECT p.listing_code, j.name FROM "
                                "property p JOIN project j ON j.id = p.project_id"))
    assert projects == {"PRJ-101": "Saahil View Residency (DEMO)",
                        "PRJ-102": "Saahil View Residency (DEMO)",
                        "PRJ-103": "Gulshan Trade Centre (DEMO)"}

    # Freshness: DH6-104 is 35 days older than the rest, which share the insert time.
    confirmed = dict(tenant_rows(app_url, agency,
                                 "SELECT listing_code, last_confirmed_at FROM property"))
    others = {v for k, v in confirmed.items() if k != "DH6-104"}
    assert len(others) == 1
    assert others.pop() - confirmed["DH6-104"] == datetime.timedelta(days=35)

    with psycopg.connect(admin_url, autocommit=True) as conn:
        contexts = conn.execute(
            "SELECT l.name, c.name FROM location_alias a JOIN location l ON l.id = "
            "a.location_id JOIN location c ON c.id = a.context_parent_id "
            "WHERE a.alias_norm = 'block 6' ORDER BY c.name").fetchall()
    assert contexts == [("Block 6", "Clifton"), ("Block 6", "Gulshan-e-Iqbal"),
                        ("Block 6", "PECHS")]


def test_existing_agent_is_reused_unchanged(db):
    _, app_url, agency = db
    agent = add_staff(app_url, agency, "Owner", "+1 (555) 000-0102", "agent")
    before = tenant_rows(app_url, agency, "SELECT * FROM staff WHERE id = %s", (agent,))
    result = run(db)
    assert result.conflicts == []
    assert inserts(result, "tenant")["staff"] == 2
    assert next(p for p in result.phases if p.name == "tenant").existing["staff"] == 1
    assert tenant_counts(app_url, agency)["staff"] == 3
    assert tenant_rows(app_url, agency, "SELECT * FROM staff WHERE id = %s",
                       (agent,)) == before


def test_second_run_inserts_nothing_and_preserves_rows(db):
    admin_url, app_url, agency = db
    run(db)
    before, gazetteer = snapshot(app_url, agency), owner_counts(admin_url)
    result = run(db)
    assert result.conflicts == []
    assert all(not phase.inserts for phase in result.phases)
    assert snapshot(app_url, agency) == before
    assert owner_counts(admin_url) == gazetteer


def test_dry_run_on_an_empty_database_writes_nothing(db, monkeypatch):
    admin_url, app_url, agency = db
    before = owner_counts(admin_url)
    real, writes = seed._insert_row, []
    monkeypatch.setattr(seed, "_insert_row",
                        lambda conn, table, values: writes.append(table) or real(
                            conn, table, values))
    result = run(db, apply=False)
    assert writes == []  # no INSERT is even issued, not just rolled back
    assert result.conflicts == []
    assert [p.outcome for p in result.phases] == ["planned (read-only, rolled back)"] * 2
    # The full plan, with tenant rows pointing at not-yet-inserted locations.
    assert inserts(result, "gazetteer") == {
        "location": 53, "location_alias": 87, "location_neighbor": 3}
    assert inserts(result, "tenant") == {"staff": 3, "project": 2, "property": 30}
    assert owner_counts(admin_url) == before == dict.fromkeys(SEED_TABLES, 0)
    assert tenant_counts(app_url, agency) == dict.fromkeys(
        ("staff", "project", "property", "v_public_listing"), 0)


def gazetteer_rows(admin_url):
    """location and location_alias rows by id (UUIDs), as the owner sees them."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        locations = {r[0]: r[1:] for r in conn.execute(
            "SELECT id, parent_id, name, level, code FROM location")}
        aliases = {r[0]: r[1:] for r in conn.execute(
            "SELECT id, alias, location_id, context_parent_id FROM location_alias")}
    return locations, aliases


def test_precinct_6_upgrade_inserts_only_the_new_gazetteer_rows(db, tmp_path):
    """Task 7.1 seed change: a database seeded before Bahria Precinct 6 existed gets
    exactly the new location and its two aliases on a rerun (the production path),
    tenant rows untouched; the next rerun plans nothing."""
    admin_url, app_url, agency = db
    old = tmp_path / "demo"
    old.mkdir()
    for path in seed.dd.DEMO_DIR.iterdir():
        if path.is_file():
            lines = path.read_text("utf-8").splitlines(keepends=True)
            (old / path.name).write_text(
                "".join(line for line in lines if not line.startswith("btk-p6,")), "utf-8")
    seed.run(agency, admin_url, app_url, ENV, demo_dir=old, apply=True)
    assert owner_counts(admin_url)["location"] == 52
    assert owner_counts(admin_url)["location_alias"] == 85
    before = snapshot(app_url, agency)
    old_locations, old_aliases = gazetteer_rows(admin_url)

    result = run(db)
    assert result.conflicts == []
    assert inserts(result, "gazetteer") == {"location": 1, "location_alias": 2}
    assert inserts(result, "tenant") == {}
    assert snapshot(app_url, agency) == before
    counts = owner_counts(admin_url)
    assert (counts["location"], counts["location_alias"], counts["location_neighbor"]) == (
        53, 87, 3)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        contexts = conn.execute(
            "SELECT l.name, c.name FROM location_alias a JOIN location l ON l.id = "
            "a.location_id JOIN location c ON c.id = a.context_parent_id "
            "WHERE a.alias_norm = 'p6' ORDER BY c.name").fetchall()
        precinct = conn.execute(
            "SELECT l.level, l.level_label, l.code, p.name FROM location l JOIN location p "
            "ON p.id = l.parent_id WHERE l.name = 'Precinct 6'").fetchall()
    assert contexts == [("Precinct 6", "Bahria Town Karachi"), ("Phase 6", "DHA Defence")]
    assert precinct == [("sub_area", "precinct", None, "Bahria Town Karachi")]
    # Existing gazetteer rows keep their UUIDs and content; only the new ones appear.
    new_locations, new_aliases = gazetteer_rows(admin_url)
    assert old_locations.items() <= new_locations.items()
    assert old_aliases.items() <= new_aliases.items()
    assert [row[1] for key, row in new_locations.items() if key not in old_locations] == [
        "Precinct 6"]
    assert sorted(row[0] for key, row in new_aliases.items() if key not in old_aliases) == [
        "p6", "precinct 6"]

    again = run(db)
    assert again.conflicts == [] and all(not phase.inserts for phase in again.phases)


def test_dry_run_after_seeding_plans_nothing(db):
    run(db)
    result = run(db, apply=False)
    assert result.conflicts == [] and all(not p.inserts for p in result.phases)


def edit_listing_and_remove_another(db):
    admin_url, app_url, agency = db
    with psycopg.connect(app_url, autocommit=True) as conn, tenant_transaction(conn, agency):
        conn.execute("UPDATE property SET price_pkr = 78000000, version = version + 1 "
                     "WHERE listing_code = 'DH6-102'")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("DELETE FROM property WHERE listing_code = 'GLI-101'")


def test_edited_listing_is_preserved_and_reported(db, capsys):
    _, app_url, agency = db
    run(db)
    edit_listing_and_remove_another(db)
    before = snapshot(app_url, agency)
    result = run(db)
    assert len(result.conflicts) == 1
    assert "property DH6-102: existing price_pkr 78000000" in result.conflicts[0]
    tenant = next(p for p in result.phases if p.name == "tenant")
    assert tenant.inserts == {"property": 1} and tenant.outcome == "rolled back: conflicts"
    # Nothing written: the missing listing is not inserted while a conflict stands.
    assert snapshot(app_url, agency) == before
    assert tenant_rows(app_url, agency, "SELECT price_pkr FROM property "
                       "WHERE listing_code = 'DH6-102'") == [(78_000_000,)]
    exit_code = seed.main(["--agency-id", str(agency), "--apply"], environ=dict(
        ENV, SEED_OWNER_DATABASE_URL=db[0], DATABASE_URL=app_url))
    assert exit_code == 2
    assert "CONFLICT property DH6-102" in capsys.readouterr().out


def test_conflict_found_on_recheck_rolls_back_the_tenant_phase(db, monkeypatch):
    # Skip the preflight so the conflict is only seen by the locked apply-time recheck.
    _, app_url, agency = db
    run(db)
    edit_listing_and_remove_another(db)
    before = snapshot(app_url, agency)
    monkeypatch.setattr(seed, "preflight", lambda *args: [])
    result = run(db)
    tenant = next(p for p in result.phases if p.name == "tenant")
    assert tenant.outcome == "rolled back: conflicts" and tenant.mode == "apply"
    assert snapshot(app_url, agency) == before


def failing_insert(monkeypatch, table, after, message="forced failure"):
    real, calls = seed._insert_row, {"n": 0}

    def insert(conn, target, values):
        if target == table:
            calls["n"] += 1
            if calls["n"] > after:
                raise psycopg.errors.UniqueViolation(message)
        return real(conn, target, values)

    monkeypatch.setattr(seed, "_insert_row", insert)


def test_tenant_insert_failure_rolls_back_and_is_recoverable(db, monkeypatch):
    admin_url, app_url, agency = db
    # The raw error text names a phone; the reported error must not.
    failing_insert(monkeypatch, "property", after=10,
                   message="duplicate key (whatsapp_number)=(15550000101)")
    with pytest.raises(seed.SeedError) as failure:
        run(db)
    message = str(failure.value)
    assert "tenant phase rolled back" in message and "UniqueViolation" in message
    assert "gazetteer phase had already committed" in message
    assert "15550000101" not in message
    # Gazetteer committed; every tenant row (staff, projects, 10 listings) rolled back.
    assert owner_counts(admin_url)["location"] == 53
    assert tenant_counts(app_url, agency) == dict.fromkeys(
        ("staff", "project", "property", "v_public_listing"), 0)

    monkeypatch.undo()
    result = run(db)
    assert inserts(result, "gazetteer") == {}
    assert inserts(result, "tenant") == {"staff": 3, "project": 2, "property": 30}
    assert tenant_counts(app_url, agency)["property"] == 30


def test_gazetteer_insert_failure_rolls_back_the_gazetteer_phase(db, monkeypatch):
    admin_url = db[0]
    failing_insert(monkeypatch, "location_alias", after=40)
    with pytest.raises(seed.SeedError, match="gazetteer phase rolled back"):
        run(db)
    assert owner_counts(admin_url) == dict.fromkeys(SEED_TABLES, 0)


def test_wrong_or_unknown_agency_writes_nothing(db):
    admin_url, app_url, _ = db
    with psycopg.connect(app_url, autocommit=True) as conn:
        other = create_agency(conn, "Other Realty (TEST)", "other-" + uuid.uuid4().hex,
                              uuid.uuid4().hex)
    with pytest.raises(seed.SeedError, match="not the demo agency"):
        run(db, agency=other)
    with pytest.raises(seed.SeedError, match="agency not found"):
        run(db, agency=uuid.uuid4())
    assert owner_counts(admin_url) == dict.fromkeys(SEED_TABLES, 0)
    assert tenant_counts(app_url, other)["staff"] == 0


@pytest.mark.parametrize(("existing", "fragment"), [
    ([("Someone", "15550000101", "broker")],
     "staff admin: the number in DEMO_ADMIN_PHONE belongs to an existing broker row"),
    ([("A", "15550000102", "agent"), ("B", "+1 555 000 0102", "agent")],
     "staff agent: 2 existing staff rows match DEMO_STAFF_PHONE; ambiguous"),
    ([("Other admin", "15550000199", "admin")],
     "staff admin: an active admin already exists with a different number"),
])
def test_staff_role_and_phone_conflicts_write_nothing(db, existing, fragment):
    admin_url, app_url, agency = db
    for name, number, role in existing:
        add_staff(app_url, agency, name, number, role)
    before = snapshot(app_url, agency)
    result = run(db)
    assert any(fragment in c for c in result.conflicts), result.conflicts
    assert all("1555" not in c for c in result.conflicts)
    assert snapshot(app_url, agency) == before
    assert owner_counts(admin_url)["location"] == 0  # preflight stopped everything


def test_cross_tenant_rows_stay_isolated(db):
    _, app_url, agency = db
    with psycopg.connect(app_url, autocommit=True) as conn:
        other = create_agency(conn, seed.DEMO_AGENCY_NAME, "b-" + uuid.uuid4().hex,
                              uuid.uuid4().hex)
    other_staff = add_staff(app_url, other, "B agent", "15550000102", "agent")
    run(db)
    # The other agency's identical phone is a separate tenant's row: not matched,
    # not changed, and none of agency A's rows are visible from agency B.
    assert tenant_counts(app_url, other) == {
        "staff": 1, "project": 0, "property": 0, "v_public_listing": 0}
    assert tenant_rows(app_url, other, "SELECT id FROM staff") == [(other_staff,)]
    assert tenant_counts(app_url, agency)["staff"] == 3
    with psycopg.connect(app_url, autocommit=True) as conn, tenant_transaction(conn, other):
        assert conn.execute("SELECT count(*) FROM property WHERE agency_id = %s",
                            (agency,)).fetchone()[0] == 0
    # Seeding agency B (also a demo-named agency here) does not touch agency A.
    before = snapshot(app_url, agency)
    result = run(db, agency=other)
    assert inserts(result, "gazetteer") == {}
    assert inserts(result, "tenant") == {"staff": 2, "project": 2, "property": 30}
    assert snapshot(app_url, agency) == before


def test_concurrent_runs_do_not_duplicate(db):
    admin_url, app_url, agency = db
    barrier, results, errors = threading.Barrier(3), [], []

    def worker():
        try:
            barrier.wait()
            results.append(run(db))
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert errors == [] and len(results) == 3
    assert all(r.conflicts == [] for r in results)
    assert sum(inserts(r, "tenant").get("property", 0) for r in results) == 30
    assert sum(inserts(r, "gazetteer").get("location", 0) for r in results) == 53
    assert owner_counts(admin_url)["location"] == 53
    assert tenant_counts(app_url, agency) == {
        "staff": 3, "project": 2, "property": 30, "v_public_listing": 27}


def test_runtime_login_must_be_restricted(db):
    admin_url, _, agency = db
    with pytest.raises(seed.SeedError, match="restricted runtime login"):
        seed.run(agency, admin_url, admin_url, ENV, apply=True)
    assert owner_counts(admin_url) == dict.fromkeys(SEED_TABLES, 0)


def test_owner_login_must_write_the_gazetteer(db):
    _, app_url, agency = db
    with pytest.raises(seed.SeedError, match="cannot write the gazetteer"):
        seed.run(agency, app_url, app_url, ENV, apply=True)


def test_urls_must_reach_the_same_schema(db, test_database_url):
    admin_url, app_url, agency = db
    schema = "seed_other_" + uuid.uuid4().hex  # same server, different (empty) schema
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        options = f"-c search_path={schema}"
        with pytest.raises(seed.SeedError, match="seed tables not found"):
            seed.run(agency, make_conninfo(admin_url, options=options), app_url, ENV)
        with pytest.raises(seed.SeedError, match="same database"):
            seed.run(agency, admin_url, make_conninfo(app_url, options=options), ENV)
    finally:
        with psycopg.connect(test_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_cli_output_has_no_secrets(db, capsys):
    admin_url, app_url, agency = db
    add_staff(app_url, agency, "Someone", "15550000101", "broker")
    environ = dict(ENV, SEED_OWNER_DATABASE_URL=admin_url, DATABASE_URL=app_url)
    assert seed.main(["--agency-id", str(agency)], environ=environ) == 2
    out = capsys.readouterr().out
    assert "Mode: dry run" in out and "not globally atomic" in out
    for value in ("15550000101", "555 000 0102", "1-555-000-0103", admin_url, app_url):
        assert value not in out
