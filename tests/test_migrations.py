import shutil
import uuid

import psycopg
import pytest
from psycopg import sql

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
        "location",
        "location_alias",
        "location_neighbor",
        "project",
        "property",
        "property_media",
        "property_event",
        "listing_draft",
        "staff_session",
        "requirement_state",
        "requirement_patch_log",
        "search_run",
        "shown_item",
        "focus",
        "media_sent",
        "pending_clarification",
        "lead",
        "lead_property",
        "handoff",
        "inbound_media",
        "conversation_turn",
        "inbound_audio",
        "staff_segment",
        "staff_segment_item",
        "staff_card",
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
    # Everything after schema v1 (002 RLS and later) is pending at first.
    later = sorted(p for p in migration_files.glob("*.sql") if p.name[:3] != "001")
    for path in later:
        path.rename(path.with_suffix(".pending"))
    assert migrate.apply_migrations(database_url) == (1, 0)
    with psycopg.connect(database_url) as conn:
        agency_id = conn.execute(
            "INSERT INTO agency (name, slug) VALUES ('Existing demo', 'existing') RETURNING id"
        ).fetchone()[0]
        conversations = {}
        for n, mode in enumerate(("ai", "human", "paused")):
            conversations[mode] = conn.execute(
                "INSERT INTO conversation (agency_id, customer_wa_id, mode) "
                "VALUES (%s, %s, %s) RETURNING id",
                (agency_id, f"92300000000{n}", mode),
            ).fetchone()[0]
        for message_type in ("text", "audio", "image"):
            conn.execute(
                "INSERT INTO message (agency_id, conversation_id, direction, message_type) "
                "VALUES (%s, %s, 'inbound', %s)",
                (agency_id, conversations["ai"], message_type),
            )
    for path in later:
        path.with_suffix(".pending").rename(path)
    total = len(later) + 1
    assert migrate.apply_migrations(database_url) == (len(later), 1)
    assert migrate.apply_migrations(database_url) == (0, total)
    # The test login bypasses RLS; test_rls_migration covers a non-superuser owner.
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT id FROM agency").fetchone() == (agency_id,)
        assert conn.execute("SELECT count(*) FROM schema_migration").fetchone() == (total,)
        assert conn.execute("SELECT next_lead_number FROM agency").fetchone() == (1,)
        assert dict(conn.execute(
            "SELECT id, control_state FROM conversation"
        ).fetchall()) == {
            conversations["ai"]: "ai_active",
            conversations["human"]: "human_owned",
            conversations["paused"]: "human_owned",
        }
        assert dict(conn.execute("SELECT id, mode FROM conversation").fetchall()) == {
            conversation_id: mode for mode, conversation_id in conversations.items()
        }
        assert dict(conn.execute(
            "SELECT message_type, modality FROM message"
        ).fetchall()) == {"text": "text", "audio": "voice", "image": None}


def test_inventory_reuses_pg_trgm_installed_in_another_schema(
    migration_database, test_database_url
):
    database_url, schema = migration_database
    extension_name = "trgm_test_" + uuid.uuid4().hex
    extension_schema = sql.Identifier(extension_name)
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(extension_schema))
        conn.execute(sql.SQL("CREATE EXTENSION pg_trgm WITH SCHEMA {}").format(extension_schema))
    try:
        migrate.apply_migrations(database_url)
        with psycopg.connect(database_url) as conn:
            definition = conn.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = %s AND indexname = 'location_alias_trgm_idx'",
                (schema,),
            ).fetchone()[0]
            installed = conn.execute(
                "SELECT extnamespace::regnamespace::text FROM pg_extension "
                "WHERE extname = 'pg_trgm'"
            ).fetchall()
        assert f"{extension_name}.gin_trgm_ops" in definition
        assert installed == [(extension_name,)]
    finally:
        with psycopg.connect(test_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(extension_schema))


def test_permission_failure_explains_admin_requirement_without_details(monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", "test-only-placeholder")

    def denied(_):
        raise psycopg.errors.InsufficientPrivilege("sensitive-error-detail")

    monkeypatch.setattr(migrate, "apply_migrations", denied)
    assert migrate.main() == 1
    assert "CREATEROLE" in caplog.text
    assert "sensitive-error-detail" not in caplog.text
    assert "test-only-placeholder" not in caplog.text
