import shutil

import psycopg
import pytest

from app import migrate


@pytest.fixture
def migration_files(tmp_path, monkeypatch):
    for source in migrate.MIGRATIONS_DIR.glob("*.sql"):
        shutil.copyfile(source, tmp_path / source.name)

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    return tmp_path


def test_apply_and_repeat(migration_database, migration_files):
    database_url, schema = migration_database

    migration_count = len(migrate.load_migrations())
    assert migrate.apply_migrations(database_url) == (migration_count, 0)

    expected = {
        "agency",
        "staff",
        "tenant_secret",
        "inbound_event",
        "job",
        "outbound_message",
        "conversation",
        "message",
        "usage_event",
        "schema_migration",
    }

    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = %s
            """,
            (schema,),
        ).fetchall()

    assert {row[0] for row in rows} == expected
    assert migrate.apply_migrations(database_url) == (0, migration_count)

    with psycopg.connect(database_url) as conn:
        count = conn.execute(
            "SELECT count(*) FROM schema_migration"
        ).fetchone()[0]

    assert count == migration_count


def test_modified_migration_rejected(
    migration_database, migration_files
):
    database_url, _ = migration_database
    migrate.apply_migrations(database_url)

    path = migration_files / "001_schema_v1.sql"
    path.write_bytes(path.read_bytes() + b"\n-- Changed after apply\n")

    with pytest.raises(
        migrate.MigrationError,
        match="Applied migration was modified",
    ):
        migrate.apply_migrations(database_url)


def test_failed_migration_rolls_back(
    migration_database, migration_files
):
    database_url, schema = migration_database

    # Fail after all real migrations have executed.
    next_version = max(int(p.name[:3]) for p in migration_files.glob("*.sql")) + 1
    (migration_files / f"{next_version:03d}_failure.sql").write_text(
        "SELECT * FROM deliberately_missing_table;",
        encoding="utf-8",
    )

    with pytest.raises(psycopg.errors.UndefinedTable):
        migrate.apply_migrations(database_url)

    with psycopg.connect(database_url) as conn:
        count = conn.execute(
            """
            SELECT count(*)
            FROM pg_tables
            WHERE schemaname = %s
            """,
            (schema,),
        ).fetchone()[0]

    # Neither the application tables nor ledger should survive.
    assert count == 0


def test_upgrade_from_schema_v1_preserves_data(migration_database, migration_files):
    database_url, _ = migration_database
    rls_path = migration_files / "002_tenant_rls.sql"
    pending_path = rls_path.with_suffix(".pending")
    rls_path.rename(pending_path)
    assert migrate.apply_migrations(database_url) == (1, 0)
    with psycopg.connect(database_url) as conn:
        agency_id = conn.execute(
            "INSERT INTO agency (name, slug) VALUES ('Existing demo', 'existing') RETURNING id"
        ).fetchone()[0]
    pending_path.rename(rls_path)
    assert migrate.apply_migrations(database_url) == (1, 1)
    assert migrate.apply_migrations(database_url) == (0, 2)
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT id FROM agency").fetchone() == (agency_id,)
        assert conn.execute("SELECT count(*) FROM schema_migration").fetchone() == (2,)


def test_permission_failure_explains_admin_requirement_without_details(monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", "test-only-placeholder")

    def denied(_):
        raise psycopg.errors.InsufficientPrivilege("sensitive-error-detail")

    monkeypatch.setattr(migrate, "apply_migrations", denied)
    assert migrate.main() == 1
    assert "CREATEROLE" in caplog.text
    assert "sensitive-error-detail" not in caplog.text
    assert "test-only-placeholder" not in caplog.text
