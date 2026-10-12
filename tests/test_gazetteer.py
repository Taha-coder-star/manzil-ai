"""Task 7.1: read-only gazetteer loader and process cache (app/gazetteer.py).

Offline: snapshot validation and the cache lifecycle with a fake loader.
Database: the runtime role loads the seeded gazetteer read-only, results carry the
database UUIDs, and the DB snapshot resolves exactly like the seeded-data (CSV)
snapshot for every alias, gold span and resolver table case."""

import threading
import uuid

import psycopg
import pytest

from app import benchmark, demo_dataset, seed
from app import gazetteer as gz
from app import location as loc
from app.provision_agency import create_agency
from tests import test_location as cases
from tests.test_seed import ENV

DATA = demo_dataset.load()
CSV = loc.Gazetteer.from_dataset(DATA)


def rows(data=DATA):
    """The demo dataset as database-shaped rows (dataset keys as IDs)."""
    locations = [(r["key"], r["parent_key"] or None, r["level"], r["name"],
                  r["display_name"] or None, r["level_label"] or None, r["city_code"] or None)
                 for r in data["locations.csv"]]
    aliases = [(r["location_key"], r["alias"], r["context_key"] or None, r["kind"])
               for r in data["location_aliases.csv"]]
    return locations, aliases


# --- snapshot validation (offline) ----------------------------------------------

def test_snapshot_from_rows_matches_the_dataset():
    snapshot = gz.build_snapshot(*rows())
    assert (snapshot.locations, snapshot.aliases) == (53, 87)
    assert len(snapshot.version) == 64
    assert loc.resolve("DHA chay", None, snapshot.gazetteer).nodes == ("dha-ph6",)


def test_snapshot_version_ignores_row_order_and_tracks_content():
    locations, aliases = rows()
    first = gz.build_snapshot(locations, aliases)
    assert gz.build_snapshot(locations[::-1], aliases[::-1]).version == first.version
    changed = gz.build_snapshot(locations, [*aliases, ("clf", "klifton town", None, "common")])
    assert changed.version != first.version


@pytest.mark.parametrize("mutate", [
    lambda l, a: ([], a),                                            # empty
    lambda l, a: (l, []),
    lambda l, a: ([r for r in l if r[2] != "city"] , a),             # parentless areas
    lambda l, a: ([(*r[:6], "LHE") if r[2] == "city" else r for r in l], a),  # no KHI node
    lambda l, a: (l, [*a, ("no-such-place", "ghost", None, "common")]),
    lambda l, a: (l, [*a, ("clf", "!!!", None, "common")]),          # no usable key
    lambda l, a: ([*l, ("x", "x", "area", "Loop", None, None, None)], a),  # cycle
])
def test_invalid_snapshots_are_rejected(mutate):
    with pytest.raises(loc.GazetteerError):
        gz.build_snapshot(*mutate(*rows()))


# --- cache lifecycle (offline, fake loader) --------------------------------------

class FakeLoader:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def __call__(self):
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def snapshot(extra_alias=None):
    locations, aliases = rows()
    if extra_alias:
        aliases = [*aliases, ("clf", extra_alias, None, "common")]
    return gz.build_snapshot(locations, aliases)


def test_get_before_load_never_loads():
    loader = FakeLoader(snapshot())
    cache = gz.GazetteerCache(loader)
    with pytest.raises(gz.GazetteerUnavailable):
        cache.get()
    assert loader.calls == 0


def test_load_once_then_reuse():
    loader = FakeLoader(snapshot())
    cache = gz.GazetteerCache(loader)
    first = cache.load()
    assert cache.load() is first and cache.get() is first.gazetteer
    assert loader.calls == 1


def test_concurrent_loads_are_single_flight():
    loader = FakeLoader(snapshot())
    cache = gz.GazetteerCache(loader)
    barrier = threading.Barrier(6)
    results = []

    def worker():
        barrier.wait()
        results.append(cache.load())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert loader.calls == 1 and len({id(r) for r in results}) == 1


def test_failed_first_load_publishes_nothing():
    cache = gz.GazetteerCache(FakeLoader(psycopg.OperationalError("down")))
    with pytest.raises(psycopg.OperationalError):
        cache.load()
    with pytest.raises(gz.GazetteerUnavailable):
        cache.get()
    assert cache.snapshot is None


def test_refresh_publishes_only_a_changed_valid_snapshot():
    old, new = snapshot(), snapshot("klifton town")
    loader = FakeLoader(old, old, new)
    cache = gz.GazetteerCache(loader)
    cache.load()
    assert cache.refresh() is False and cache.snapshot is old    # unchanged: kept
    assert cache.refresh() is True and cache.snapshot is new
    assert loc.resolve("klifton town", None, cache.get()).nodes == ("clf",)


@pytest.mark.parametrize("error", [psycopg.OperationalError("down"),
                                   loc.GazetteerError("gazetteer is empty")])
def test_failed_refresh_keeps_the_previous_snapshot(error):
    old = snapshot()
    cache = gz.GazetteerCache(FakeLoader(old, error))
    cache.load()
    with pytest.raises(type(error)):
        cache.refresh()
    assert cache.snapshot is old and cache.get() is old.gazetteer


def test_loader_must_return_a_snapshot():
    cache = gz.GazetteerCache(FakeLoader(None))
    with pytest.raises(loc.GazetteerError):
        cache.load()
    assert cache.snapshot is None


# --- database --------------------------------------------------------------------

@pytest.fixture
def seeded(app_role_db):
    """Disposable schema, migrated, gazetteer + demo agency seeded (app.seed)."""
    admin_url, app_url, _ = app_role_db
    with psycopg.connect(app_url, autocommit=True) as conn:
        agency = create_agency(conn, seed.DEMO_AGENCY_NAME, "demo-" + uuid.uuid4().hex,
                               uuid.uuid4().hex)
    result = seed.run(agency, admin_url, app_url, ENV, apply=True)
    assert result.conflicts == []
    with psycopg.connect(admin_url, autocommit=True) as conn:
        ids, _ = seed.resolve_locations(conn, seed.load_dataset())
    return admin_url, app_url, {key: uid for key, uid in ids.items()}


def test_runtime_role_loads_read_only_with_database_ids(seeded):
    _, app_url, ids = seeded
    with psycopg.connect(app_url, autocommit=True) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == "manzil_app"
        privileges = conn.execute(
            "SELECT bool_or(has_table_privilege(t, p)) FROM unnest(%s::text[]) t, "
            "unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p",
            (["location", "location_alias", "location_neighbor"],)).fetchone()[0]
        assert privileges is False
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO location_alias (location_id, alias) "
                         "SELECT id, 'x' FROM location LIMIT 1")
        snapshot = gz.load_snapshot(conn)
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert (snapshot.locations, snapshot.aliases) == (53, 87)
    assert all(isinstance(i, uuid.UUID) for i in snapshot.gazetteer.places)
    result = loc.resolve("DHA chay", None, snapshot.gazetteer)
    assert result.nodes == (ids["dha-ph6"],) and isinstance(result.nodes[0], uuid.UUID)


def test_load_snapshot_needs_an_idle_connection(seeded):
    _, app_url, _ = seeded
    with psycopg.connect(app_url) as conn:  # not autocommit: a transaction is open
        conn.execute("SELECT 1")
        with pytest.raises(ValueError):
            gz.load_snapshot(conn)


def _cases():
    """(span, active key) for every alias, gold slot and resolver table case."""
    out = [(r["alias"], r["context_key"] or None) for r in DATA["location_aliases.csv"]]
    out += [(r["alias"], None) for r in DATA["location_aliases.csv"]]
    for items in benchmark.load().values():
        for item in items:
            gold = (item.get("gold_extraction") or {}).get("location")
            if gold:
                out.append((gold["raw"], "dha-ph6" if item["id"] == "CT-29" else None))
    out += [(span, active) for span, active, _, _ in cases.CONTEXT]
    out += [(span, active) for span, active in cases.SCOPE_UNKNOWN + cases.OTHER]
    out += [(span, None) for span, _ in cases.NUMBERS + cases.AREAS + cases.UNKNOWNS]
    out += [(span, None) for span, _, _ in cases.FUZZY + cases.SCOPED]
    out += [("P6", a) for a in (None, "dha", "btk", "btk-p10", "clf")]
    out += [("Clifton DHA", None), ("DHA City sector 13", "dha"), ("Defence City", None),
            ("Bahria Lahore", "btk"), ("Gulshan block 1", None), ("Bahria precinct 12", None),
            ("ڈی ایچ اے فیز 6", None), ("بحریہ", None), ("डीएचए फेज 6", None)]
    return list(dict.fromkeys(out))


def _as_keys(result, keys_by_id):
    d = result.to_dict()
    d["nodes"] = [keys_by_id.get(i, i) for i in d["nodes"]]
    d["candidates"] = [(keys_by_id.get(c["location_id"], c["location_id"]), c["score"])
                       for c in d.get("candidates", [])]
    d["area_hint"] = keys_by_id.get(d.get("area_hint"), d.get("area_hint"))
    return d


def test_database_snapshot_resolves_like_the_seeded_dataset(seeded):
    _, app_url, ids = seeded
    with psycopg.connect(app_url, autocommit=True) as conn:
        db = gz.load_snapshot(conn).gazetteer
    keys_by_id = {uid: key for key, uid in ids.items()}
    checked = 0
    for span, active in _cases():
        expected = _as_keys(loc.resolve(span, active, CSV), {})
        actual = _as_keys(loc.resolve(span, ids[active] if active else None, db), keys_by_id)
        assert actual == expected, (span, active)
        checked += 1
    assert checked >= 250  # distinct (span, active) pairs


def test_cache_from_database_url_reuses_and_refreshes(seeded):
    admin_url, app_url, ids = seeded
    cache = gz.GazetteerCache.from_database_url(app_url)
    first = cache.load()
    assert cache.load() is first and cache.fetches == 1
    assert cache.refresh() is False and cache.snapshot is first
    with psycopg.connect(admin_url, autocommit=True) as conn:  # an owner change
        conn.execute("INSERT INTO location_alias (location_id, alias, kind) "
                     "VALUES (%s, 'klifton town', 'common')", (ids["clf"],))
    assert loc.resolve("klifton town", None, cache.get()).status == loc.UNKNOWN  # until refresh
    assert cache.refresh() is True
    assert loc.resolve("klifton town", None, cache.get()).nodes == (ids["clf"],)
    assert cache.snapshot.aliases == 88


def test_empty_or_unreachable_database_publishes_nothing(app_role_db):
    _, app_url, _ = app_role_db  # migrated, not seeded
    cache = gz.GazetteerCache.from_database_url(app_url)
    with pytest.raises(loc.GazetteerError, match="empty"):
        cache.load()
    with pytest.raises(gz.GazetteerUnavailable):
        cache.get()
    unreachable = gz.GazetteerCache.from_database_url(
        "postgresql://manzil_app@127.0.0.1:1/manzil_schema_test")
    with pytest.raises(psycopg.OperationalError):
        unreachable.load()
    assert unreachable.snapshot is None
