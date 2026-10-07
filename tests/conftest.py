import os
import secrets
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app import migrate


@pytest.fixture(scope="session")
def test_database_url():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        if os.getenv("CI") or os.getenv("REQUIRE_DATABASE_TESTS") == "1":
            pytest.fail("TEST_DATABASE_URL is required for database checks")
        pytest.skip("TEST_DATABASE_URL is not configured")
    options = conninfo_to_dict(database_url)
    # These tests change a cluster role's password; only a dedicated local DB is safe.
    if options.get("host") not in {"localhost", "127.0.0.1", "::1"} or (
        options.get("dbname") != "manzil_schema_test"
    ):
        pytest.fail("Database tests require the dedicated loopback manzil_schema_test DB")
    return database_url


@pytest.fixture
def migration_database(test_database_url):
    schema = "migration_test_" + uuid.uuid4().hex
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema))
        )
    try:
        yield make_conninfo(test_database_url, options=f"-c search_path={schema}"), schema
    finally:
        with psycopg.connect(test_database_url, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                    psycopg.sql.Identifier(schema)
                )
            )


@pytest.fixture
def app_role_db(migration_database):
    """Migrated schema plus a conninfo for the restricted manzil_app login."""
    admin_url, schema = migration_database
    migrate.apply_migrations(admin_url)
    # Random and confined to the dedicated test cluster. Never print the password.
    password = secrets.token_urlsafe(32)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        verifier = admin.pgconn.encrypt_password(
            password.encode("utf-8"), b"manzil_app", b"scram-sha-256"
        )
        admin.execute(sql.SQL("ALTER ROLE manzil_app PASSWORD {}").format(
            sql.Literal(verifier.decode("ascii"))
        ))
    try:
        yield admin_url, make_conninfo(admin_url, user="manzil_app", password=password), schema
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute("ALTER ROLE manzil_app PASSWORD NULL")
