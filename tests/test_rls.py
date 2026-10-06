import secrets
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from app import migrate
from app.db import tenant_transaction

TABLES = (
    "agency", "staff", "tenant_secret", "inbound_event", "job",
    "conversation", "message", "outbound_message", "usage_event",
)


def tenant_rows():
    ids = {table: uuid.uuid4() for table in TABLES}
    agency = ids["agency"]
    rows = {
        "agency": {"name": "Fictional agency", "slug": uuid.uuid4().hex},
        "staff": {"name": "Demo", "whatsapp_number": "demo", "role": "agent"},
        "tenant_secret": {
            "secret_name": "demo", "ciphertext": b"fictional", "encryption_key_id": "demo",
        },
        "inbound_event": {
            "event_key": "demo", "event_type": "demo", "payload": Jsonb({}),
        },
        "job": {
            "inbound_event_id": ids["inbound_event"], "job_type": "demo", "dedupe_key": "demo",
        },
        "conversation": {"customer_wa_id": "demo", "assigned_staff_id": ids["staff"]},
        "message": {
            "conversation_id": ids["conversation"], "inbound_event_id": ids["inbound_event"],
            "direction": "outbound", "message_type": "text",
        },
        "outbound_message": {
            "conversation_id": ids["conversation"], "message_id": ids["message"],
            "dedupe_key": "demo", "recipient_wa_id": "demo", "payload": Jsonb({}),
        },
        "usage_event": {
            "job_id": ids["job"], "provider": "demo", "operation": "demo",
            "request_key": "demo", "outcome": "succeeded",
        },
    }
    for table, row in rows.items():
        row["id"] = ids[table]
        if table != "agency":
            row["agency_id"] = agency
    return rows


def insert_row(conn, table, row):
    conn.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, row)),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
        ),
        tuple(row.values()),
    )


@pytest.fixture
def rls_db(migration_database):
    admin_url, schema = migration_database
    migrate.apply_migrations(admin_url)
    # Random and confined to the dedicated test cluster. Never print the password.
    password = secrets.token_urlsafe(32)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE manzil_app PASSWORD {}").format(sql.Literal(password)))
    try:
        with psycopg.connect(
            make_conninfo(admin_url, user="manzil_app", password=password), autocommit=True
        ) as conn:
            tenants = [tenant_rows(), tenant_rows()]
            for rows in tenants:
                with tenant_transaction(conn, rows["agency"]["id"]):
                    for table, row in rows.items():
                        insert_row(conn, table, row)
            yield conn, tenants, schema, admin_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute("ALTER ROLE manzil_app PASSWORD NULL")


@pytest.mark.parametrize("table", TABLES)
def test_two_tenants_and_missing_context(rls_db, table):
    conn, tenants, _, _ = rls_db
    query = sql.SQL("SELECT id FROM {}").format(sql.Identifier(table))
    assert conn.execute(query).fetchall() == []
    for rows in tenants:
        with tenant_transaction(conn, rows["agency"]["id"]):
            assert conn.execute(query).fetchall() == [(rows[table]["id"],)]
        assert conn.execute(query).fetchall() == []


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("context", ["other", "missing"])
def test_insert_requires_matching_tenant(rls_db, table, context):
    conn, (a, b), _, _ = rls_db
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        if context == "other":
            with tenant_transaction(conn, a["agency"]["id"]):
                insert_row(conn, table, b[table])
        else:
            insert_row(conn, table, b[table])


@pytest.mark.parametrize("table", TABLES)
def test_cannot_move_row_to_other_tenant(rls_db, table):
    conn, (a, b), _, _ = rls_db
    column = "id" if table == "agency" else "agency_id"
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_transaction(
        conn, a["agency"]["id"]
    ):
        conn.execute(
            sql.SQL("UPDATE {} SET {} = %s WHERE id = %s").format(
                sql.Identifier(table), sql.Identifier(column)
            ),
            (b["agency"]["id"], a[table]["id"]),
        )


@pytest.mark.parametrize("table", TABLES)
def test_invisible_rows_cannot_be_updated_or_deleted(rls_db, table):
    conn, (a, b), _, _ = rls_db
    with tenant_transaction(conn, a["agency"]["id"]):
        for statement in ["UPDATE {} SET id = id WHERE id = %s", "DELETE FROM {} WHERE id = %s"]:
            assert conn.execute(
                sql.SQL(statement).format(sql.Identifier(table)), (b[table]["id"],)
            ).rowcount == 0
        assert conn.execute(
            sql.SQL("UPDATE {} SET id = id WHERE id = %s").format(sql.Identifier(table)),
            (a[table]["id"],),
        ).rowcount == 1


def test_matching_tenant_can_delete(rls_db):
    conn, tenants, _, _ = rls_db
    for rows in tenants:
        with tenant_transaction(conn, rows["agency"]["id"]):
            for table in reversed(TABLES):
                assert conn.execute(
                    sql.SQL("DELETE FROM {}").format(sql.Identifier(table))
                ).rowcount == 1


@pytest.mark.parametrize("table,column,parent", [
    ("job", "inbound_event_id", "inbound_event"),
    ("conversation", "assigned_staff_id", "staff"),
    ("message", "conversation_id", "conversation"),
    ("message", "inbound_event_id", "inbound_event"),
    ("outbound_message", "conversation_id", "conversation"),
    ("outbound_message", "message_id", "message"),
    ("usage_event", "job_id", "job"),
])
def test_cross_tenant_foreign_keys(rls_db, table, column, parent):
    conn, (a, b), _, _ = rls_db
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        conn, a["agency"]["id"]
    ):
        conn.execute(
            sql.SQL("UPDATE {} SET {} = %s WHERE id = %s").format(
                sql.Identifier(table), sql.Identifier(column)
            ), (b[parent]["id"], a[table]["id"]),
        )


def test_outbound_message_must_match_conversation(rls_db):
    conn, (a, _), _, _ = rls_db
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        conn, a["agency"]["id"]
    ):
        other = uuid.uuid4()
        insert_row(conn, "conversation", {
            "id": other, "agency_id": a["agency"]["id"], "customer_wa_id": "other",
        })
        conn.execute("UPDATE outbound_message SET conversation_id = %s", (other,))


@pytest.mark.parametrize("autocommit", [True, False])
@pytest.mark.parametrize("rollback", [True, False])
def test_connection_reuse_clears_context(rls_db, autocommit, rollback):
    conn, (a, b), _, _ = rls_db
    conn.autocommit = autocommit
    try:
        with tenant_transaction(conn, a["agency"]["id"]):
            conn.execute("UPDATE agency SET name = 'changed'")
            if rollback:
                raise RuntimeError("deliberate rollback")
    except RuntimeError:
        assert rollback
    assert conn.info.transaction_status == TransactionStatus.IDLE
    with conn.transaction():
        assert conn.execute("SELECT current_agency_id()").fetchone() == (None,)
        assert conn.execute("SELECT id FROM agency").fetchall() == []
    with tenant_transaction(conn, a["agency"]["id"]):
        expected = "Fictional agency" if rollback else "changed"
        assert conn.execute("SELECT name FROM agency").fetchone() == (expected,)
    with tenant_transaction(conn, b["agency"]["id"]):
        assert conn.execute("SELECT id FROM agency").fetchall() == [(b["agency"]["id"],)]


@pytest.mark.parametrize("invalid", [None, 12, "", "not-a-uuid", "'; RESET ROLE; --"])
def test_invalid_tenant_is_rejected_before_transaction(rls_db, invalid):
    conn, _, _, _ = rls_db
    with pytest.raises((TypeError, ValueError), match="must be a UUID"), tenant_transaction(conn, invalid):
        pytest.fail("Invalid context was accepted")
    assert conn.info.transaction_status == TransactionStatus.IDLE


def test_nested_and_implicit_transactions_rejected(rls_db):
    conn, (a, b), _, _ = rls_db
    with tenant_transaction(conn, str(a["agency"]["id"])):
        with pytest.raises(ValueError, match="idle"), tenant_transaction(conn, b["agency"]["id"]):
            pytest.fail("Nested transaction was accepted")
        assert conn.execute("SELECT id FROM agency").fetchall() == [(a["agency"]["id"],)]
    conn.autocommit = False
    conn.execute("SELECT 1")
    with pytest.raises(ValueError, match="idle"), tenant_transaction(conn, a["agency"]["id"]):
        pytest.fail("Open transaction was accepted")
    conn.rollback()


def test_session_level_context_rejected(rls_db):
    conn, (a, b), _, _ = rls_db
    conn.execute("SELECT set_config('app.agency_id', %s, false)", (str(a["agency"]["id"]),))
    with pytest.raises(ValueError, match="session-level"), tenant_transaction(conn, b["agency"]["id"]):
        pytest.fail("Contaminated connection was accepted")
    conn.execute("RESET app.agency_id")


def test_role_and_table_hardening(rls_db):
    conn, _, schema, _ = rls_db
    assert conn.execute("SELECT session_user, current_user").fetchone() == (
        "manzil_app", "manzil_app",
    )
    assert conn.execute(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication, rolinherit "
        "FROM pg_roles WHERE rolname = current_user"
    ).fetchone() == (False,) * 6
    assert conn.execute(
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
        "WHERE r.rolname = current_user"
    ).fetchone() == (0,)
    rows = conn.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, r.rolname "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_roles r ON r.oid = c.relowner WHERE n.nspname = %s AND c.relkind = 'r'",
        (schema,),
    ).fetchall()
    assert {r[0] for r in rows} == set(TABLES) | {"schema_migration"}
    for name, enabled, forced, owner in rows:
        assert owner != "manzil_app"
        if name != "schema_migration":
            assert enabled and forced


@pytest.mark.parametrize("statement", [
    "CREATE TABLE forbidden (id int)",
    "CREATE SCHEMA forbidden_schema",
    "ALTER TABLE agency ADD COLUMN forbidden int",
    "ALTER TABLE agency DISABLE ROW LEVEL SECURITY",
    "TRUNCATE agency CASCADE",
    "SELECT * FROM schema_migration",
    "DELETE FROM schema_migration",
    "SET ROLE manzil_test",
])
def test_runtime_privileges_are_limited(rls_db, statement):
    conn, (a, _), _, _ = rls_db
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_transaction(
        conn, a["agency"]["id"]
    ):
        conn.execute(statement)
