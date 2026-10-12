"""Task 7.1: the resolver's pure-Python trigram score equals PostgreSQL pg_trgm
similarity() (the semantics R17 / D7 name). Needs the disposable test database:
migrations install pg_trgm (005) and the comparison runs in one query."""

import psycopg
import pytest
from psycopg import sql

from app import demo_dataset, migrate
from app import location as loc

PROBES = [
    "cliftn", "klifton", "bukhari comercial", "zamzama comercial", "khadda markit",
    "north nazimabaad", "northnazimabad", "bahriya town", "tariq rd", "sports city",
    "pecs", "gulshn block 13", "gulshan e maymar", "north", "malir cantt", "hyderabad",
    "commercial bukhari commercial", "dha phase 6", "dha 6", "p 6", "block h", "2 ext",
    "a", "",
]


@pytest.fixture
def trgm(migration_database):
    admin_url, _ = migration_database
    migrate.apply_migrations(admin_url)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        schema = conn.execute(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n "
            "ON n.oid = e.extnamespace WHERE e.extname = 'pg_trgm'").fetchone()[0]
        yield conn, schema


def test_python_similarity_equals_pg_trgm(trgm):
    conn, schema = trgm
    gazetteer = loc.Gazetteer.from_dataset(demo_dataset.load())
    keys = sorted({key for key, _, _ in gazetteer.fuzzy_keys()} | {
        row.name for row in gazetteer.fuzzy_entries()})
    pairs = [(probe, key) for probe in PROBES for key in keys]
    rows = conn.execute(
        sql.SQL("SELECT a, b, {}.similarity(a, b) FROM unnest(%s::text[], %s::text[]) "
                "AS t(a, b)").format(sql.Identifier(schema)),
        ([a for a, _ in pairs], [b for _, b in pairs])).fetchall()
    assert len(rows) == len(pairs) >= 1800
    mismatches = [(a, b, float(loc.similarity(a, b)), pg) for a, b, pg in rows
                  if abs(float(loc.similarity(a, b)) - pg) > 1e-6]  # pg returns float4
    assert mismatches == []
