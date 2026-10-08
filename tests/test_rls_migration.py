import uuid

import psycopg
import pytest
from psycopg import sql

from app import migrate


@pytest.fixture
def pending_rls(migration_database):
    database_url, schema = migration_database
    with psycopg.connect(database_url) as conn:
        try:
            conn.execute((migrate.MIGRATIONS_DIR / "001_schema_v1.sql").read_text())
            conn.execute("CREATE TABLE schema_migration (name text)")
            if not conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'manzil_app'"
            ).fetchone():
                conn.execute("CREATE ROLE manzil_app LOGIN NOINHERIT")
            yield conn, schema
        finally:
            # Role mutations are cluster-wide: every adversarial test rolls them back.
            conn.rollback()


@pytest.mark.parametrize("attribute", [
    "SUPERUSER", "BYPASSRLS", "CREATEDB", "CREATEROLE", "REPLICATION", "INHERIT", "NOLOGIN",
])
def test_unsafe_existing_role_rejected(pending_rls, attribute):
    conn, _ = pending_rls
    conn.execute(sql.SQL("ALTER ROLE manzil_app {}").format(sql.SQL(attribute)))
    with pytest.raises(psycopg.errors.RaiseException, match="unsafe attributes"):
        conn.execute((migrate.MIGRATIONS_DIR / "002_tenant_rls.sql").read_text())


def test_role_membership_rejected(pending_rls):
    conn, _ = pending_rls
    group = sql.Identifier("test_group_" + uuid.uuid4().hex)
    conn.execute(sql.SQL("CREATE ROLE {}").format(group))
    conn.execute(sql.SQL("GRANT {} TO manzil_app").format(group))
    with pytest.raises(psycopg.errors.RaiseException, match="role memberships"):
        conn.execute((migrate.MIGRATIONS_DIR / "002_tenant_rls.sql").read_text())


@pytest.mark.parametrize("object_kind", ["schema", "table"])
def test_runtime_ownership_rejected(pending_rls, object_kind):
    conn, schema = pending_rls
    if object_kind == "schema":
        conn.execute(sql.SQL("ALTER SCHEMA {} OWNER TO manzil_app").format(sql.Identifier(schema)))
    else:
        conn.execute("ALTER TABLE agency OWNER TO manzil_app")
    with pytest.raises(psycopg.errors.RaiseException, match="must not own"):
        conn.execute((migrate.MIGRATIONS_DIR / "002_tenant_rls.sql").read_text())


def test_existing_permissive_policy_rejected(pending_rls):
    conn, _ = pending_rls
    conn.execute("CREATE POLICY unexpected ON agency USING (true)")
    with pytest.raises(psycopg.errors.RaiseException, match="Unexpected existing"):
        conn.execute((migrate.MIGRATIONS_DIR / "002_tenant_rls.sql").read_text())


def become_migrator(conn, schema):
    """Switch to a fresh schema owner with CREATEROLE but no superuser or database CREATE."""
    owner = sql.Identifier("test_migrator_" + uuid.uuid4().hex)
    conn.execute(sql.SQL("CREATE ROLE {} CREATEROLE NOSUPERUSER NOBYPASSRLS").format(owner))
    # Exercise the CREATE ROLE path even if prior tests provisioned manzil_app.
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'manzil_app'").fetchone():
        conn.execute(sql.SQL("ALTER ROLE manzil_app RENAME TO {}").format(
            sql.Identifier("saved_app_" + uuid.uuid4().hex)
        ))
    conn.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
        sql.Identifier(schema), owner
    ))
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(owner))
    assert conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).fetchone() == (False, False)
    assert conn.execute(
        "SELECT has_database_privilege(current_database(), 'CREATE')"
    ).fetchone() == (False,)
    conn.execute("CREATE TABLE schema_migration (name text)")


def test_schema_owner_with_createrole_can_apply_without_superuser(migration_database):
    database_url, schema = migration_database
    with psycopg.connect(database_url) as conn:
        try:
            # pg_trgm pre-provisioned by a DBA; the migration reuses it without extra privileges.
            conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA {}").format(
                sql.Identifier(schema)
            ))
            become_migrator(conn, schema)
            for _, _, statement in migrate.load_migrations():
                conn.execute(statement, prepare=False)
            # FORCE also applies to an ordinary table owner with no tenant policy.
            assert conn.execute("SELECT count(*) FROM agency").fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM property").fetchone() == (0,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("INSERT INTO agency (name, slug) VALUES ('demo', 'demo')")
        finally:
            conn.rollback()


def test_control_state_backfill_works_for_owner_under_force_rls(migration_database):
    """Upgrade with live conversations as a non-superuser owner, as on RDS.

    A superuser bypasses RLS, so only this path proves the backfill is not a
    silent no-op on the forced conversation table.
    """
    database_url, schema = migration_database
    with psycopg.connect(database_url) as conn:
        try:
            conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA {}").format(
                sql.Identifier(schema)
            ))
            become_migrator(conn, schema)
            owner = sql.Identifier(conn.execute("SELECT current_user").fetchone()[0])
            for name, _, statement in migrate.load_migrations():
                if name == "006_conversation_state.sql":
                    break
                conn.execute(statement, prepare=False)

            # The test migrator does not own the schema, so 002's schema grant was a no-op.
            conn.execute("RESET ROLE")
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO manzil_app").format(
                sql.Identifier(schema)
            ))
            # Existing tenant data, written the only way FORCE RLS allows.
            agency = uuid.uuid4()
            conn.execute("SET LOCAL ROLE manzil_app")
            conn.execute("SELECT set_config('app.agency_id', %s, true)", (str(agency),))
            conn.execute(
                "INSERT INTO agency (id, name, slug) VALUES (%s, 'Fictional (DEMO)', 'demo')",
                (agency,),
            )
            for n, mode in enumerate(("ai", "human", "paused")):
                conversation = conn.execute(
                    "INSERT INTO conversation (agency_id, customer_wa_id, mode) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (agency, f"92300000000{n}", mode),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO message (agency_id, conversation_id, direction, message_type) "
                    "VALUES (%s, %s, 'inbound', 'audio')",
                    (agency, conversation),
                )

            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(owner))
            # The owner has no policy: FORCE hides every row from it.
            assert conn.execute("SELECT count(*) FROM conversation").fetchone() == (0,)
            conn.execute(statement, prepare=False)
            assert conn.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'conversation'::regclass"
            ).fetchone() == (True, True)

            conn.execute("SET LOCAL ROLE manzil_app")
            assert dict(conn.execute("SELECT mode, control_state FROM conversation").fetchall()) == {
                "ai": "ai_active", "human": "human_owned", "paused": "human_owned",
            }
            assert conn.execute("SELECT DISTINCT modality FROM message").fetchall() == [("voice",)]
        finally:
            conn.rollback()


def test_missing_pg_trgm_without_create_privilege_fails_clearly(migration_database):
    database_url, schema = migration_database
    with psycopg.connect(database_url) as conn:
        try:
            assert conn.execute(
                "SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'"
            ).fetchone() == (0,)
            become_migrator(conn, schema)
            for name, _, statement in migrate.load_migrations():
                if name == "005_inventory.sql":
                    break
                conn.execute(statement, prepare=False)
            with pytest.raises(psycopg.errors.RaiseException, match="pg_trgm is not installed"):
                conn.execute(statement, prepare=False)
        finally:
            conn.rollback()
