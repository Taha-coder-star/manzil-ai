import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo


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
