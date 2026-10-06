import hashlib
import logging
import os
import re
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_NAME = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")
MIGRATION_LOCK_ID = 60172001


class MigrationError(Exception):
    pass


def load_migrations():
    if not MIGRATIONS_DIR.is_dir():
        raise MigrationError("Migration directory is missing.")

    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))

    if not paths:
        raise MigrationError("No SQL migrations found.")

    migrations = []
    versions = set()

    for path in paths:
        if not MIGRATION_NAME.fullmatch(path.name):
            raise MigrationError(
                f"Invalid migration filename: {path.name}"
            )

        version = path.name.split("_", 1)[0]

        if version in versions:
            raise MigrationError(
                f"Duplicate migration version: {version}"
            )

        versions.add(version)
        content = path.read_bytes()
        sql = content.decode("utf-8-sig")

        if not sql.strip():
            raise MigrationError(
                f"Empty migration: {path.name}"
            )

        checksum = hashlib.sha256(content).hexdigest()
        migrations.append((path.name, checksum, sql))

    return migrations


def apply_migrations(database_url):
    migrations = load_migrations()
    available = {
        name: checksum
        for name, checksum, _ in migrations
    }
    applied_count = 0
    skipped_count = 0

    with psycopg.connect(
        database_url,
        connect_timeout=10,
        autocommit=True,
    ) as conn, conn.transaction():
        # Serialize migration runs against this database.
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (MIGRATION_LOCK_ID,),
        )

        conn.execute(
            """
                CREATE TABLE IF NOT EXISTS schema_migration (
                    name TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
        )

        applied = dict(
            conn.execute(
                "SELECT name, checksum FROM schema_migration"
            ).fetchall()
        )

        for name, checksum in applied.items():
            if name not in available:
                raise MigrationError(
                    f"Applied migration file is missing: {name}"
                )

            if available[name] != checksum:
                raise MigrationError(
                    f"Applied migration was modified: {name}"
                )

        for name, checksum, sql in migrations:
            if name in applied:
                skipped_count += 1
                continue

            if applied and name < max(applied):
                raise MigrationError(
                    f"Cannot insert an older migration: {name}"
                )

            conn.execute(sql, prepare=False)

            conn.execute(
                """
                    INSERT INTO schema_migration (name, checksum)
                    VALUES (%s, %s)
                    """,
                (name, checksum),
            )

            applied[name] = checksum
            applied_count += 1

    # Report success only after the transaction has committed.
    return applied_count, skipped_count


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )

    database_url = os.getenv("DATABASE_URL", "").strip()

    if not database_url:
        logger.error("DATABASE_URL is not configured.")
        return 1

    try:
        applied, skipped = apply_migrations(database_url)
    except MigrationError as exc:
        logger.error("%s", exc)
        return 1
    except psycopg.errors.InsufficientPrivilege:
        logger.error(
            "Migration permission denied; use the schema/table owner with "
            "CREATEROLE and required role-administration privileges, "
            "not the runtime login. Changes were rolled back."
        )
        return 1
    except (psycopg.Error, OSError, UnicodeError) as exc:
        # Do not print database errors or connection credentials.
        logger.error(
            "Migration failed (%s); changes were rolled back "
            "if a transaction had started.",
            type(exc).__name__,
        )
        return 1

    logger.info(
        "Migrations complete: %s applied, %s skipped.",
        applied,
        skipped,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
