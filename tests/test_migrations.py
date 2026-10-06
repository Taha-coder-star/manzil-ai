import os
import shutil
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app import migrate


@pytest.fixture
def migration_database():
    database_url = os.getenv("TEST_DATABASE_URL")

    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured.")

    # Each test gets its own empty schema.
    schema = "migration_test_" + uuid.uuid4().hex

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(
                psycopg.sql.Identifier(schema)
            )
        )

    test_url = make_conninfo(
        database_url,
        options=f"-c search_path={schema}",
    )

    try:
        yield test_url, schema
    finally:
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                    psycopg.sql.Identifier(schema)
                )
            )


@pytest.fixture
def migration_files(tmp_path, monkeypatch):
    for source in migrate.MIGRATIONS_DIR.glob("*.sql"):
        shutil.copyfile(source, tmp_path / source.name)

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    return tmp_path


def test_apply_and_repeat(migration_database, migration_files):
    database_url, schema = migration_database

    assert migrate.apply_migrations(database_url) == (1, 0)

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
    assert migrate.apply_migrations(database_url) == (0, 1)

    with psycopg.connect(database_url) as conn:
        count = conn.execute(
            "SELECT count(*) FROM schema_migration"
        ).fetchone()[0]

    assert count == 1


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

    # Fail after the first migration has executed.
    (migration_files / "002_failure.sql").write_text(
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