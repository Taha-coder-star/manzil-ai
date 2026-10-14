"""Read-only gazetteer loader and process cache for the location resolver (Task 7.1, C7).

The gazetteer (location, location_alias) is shared, not tenant data: no agency_id,
no RLS, SELECT only for manzil_app (migration 005). The runtime loads it once into
an immutable app.location.Gazetteer whose IDs are the database UUIDs, and resolves
against that snapshot in memory (RFC: "in-process gazetteer / alias cache, small,
versioned, reloaded on change"; handoff 6.2/6.3 section 5).

Lifecycle (docs/LOCATION_RESOLVER.md, "Database snapshot and cache"):

- load(): at process start. Loads, validates and publishes one snapshot; later calls
  return it without touching the database. A failure raises and publishes nothing:
  there is never an empty or partial gazetteer.
- get(): the published Gazetteer, or GazetteerUnavailable. It never loads, so a
  missing snapshot cannot turn every resolution into a database query.
- refresh(): explicit reload after the gazetteer changes (an owner seed run). The
  new snapshot is published only after it loaded and validated in full; on any
  failure the previous one stays published and the error is raised. Restarting the
  process is the other way to pick up changes.

Wiring into the worker start (7.3) follows app.worker.check_schema: a load failure
there is a configuration error (exit 78), not a silent fallback.
"""

import hashlib
import threading
from dataclasses import dataclass

import psycopg
from psycopg.pq import TransactionStatus

from app import location

STATEMENT_TIMEOUT = "5s"
CONNECT_TIMEOUT_SECONDS = 5

# Ordered so the version hash does not depend on the scan order.
LOCATION_SQL = """
    SELECT id, parent_id, level, name, display_name, level_label, city_code, code
    FROM location ORDER BY id
"""
ALIAS_SQL = """
    SELECT location_id, alias, context_parent_id, kind
    FROM location_alias ORDER BY id
"""


class GazetteerUnavailable(RuntimeError):
    """No validated snapshot has been published (not loaded, or the load failed)."""


@dataclass(frozen=True)
class Snapshot:
    gazetteer: location.Gazetteer
    version: str  # sha256 of the loaded rows: equal rows, equal version
    locations: int
    aliases: int


def build_snapshot(location_rows, alias_rows, *, city_code="KHI"):
    """Validate rows and build a Snapshot; raises location.GazetteerError.

    Beyond the Gazetteer's own checks (levels, parents, no cycles, alias targets and
    contexts, usable keys): not empty, the agency's city node exists, at least one
    area."""
    location_rows, alias_rows = list(location_rows), list(alias_rows)
    if not location_rows or not alias_rows:
        raise location.GazetteerError("gazetteer is empty")
    places = [location.Place(id=r[0], parent_id=r[1], level=r[2], name=r[3],
                             display_name=r[4] or r[3], level_label=r[5], city_code=r[6],
                             code=r[7] if len(r) > 7 else None)
              for r in location_rows]
    aliases = [location.Alias(location_id=r[0], alias=r[1], context_id=r[2], kind=r[3])
               for r in alias_rows]
    gazetteer = location.Gazetteer(places, aliases)
    if gazetteer.city_node(city_code) is None:
        raise location.GazetteerError(f"no city node for {city_code}")
    if not any(place.level == "area" for place in gazetteer.places.values()):
        raise location.GazetteerError("gazetteer has no areas")
    digest = hashlib.sha256()
    for row in sorted(map(repr, location_rows)) + ["--"] + sorted(map(repr, alias_rows)):
        digest.update(row.encode("utf-8") + b"\n")
    return Snapshot(gazetteer, digest.hexdigest(), len(location_rows), len(alias_rows))


def load_snapshot(conn, *, city_code="KHI"):
    """One consistent read (REPEATABLE READ, READ ONLY) of location + location_alias
    on an idle runtime connection, validated before it is returned."""
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("load_snapshot requires an idle connection")
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        location_rows = conn.execute(LOCATION_SQL).fetchall()
        alias_rows = conn.execute(ALIAS_SQL).fetchall()
    return build_snapshot(location_rows, alias_rows, city_code=city_code)


class GazetteerCache:
    """One published, validated snapshot per process (see the module docstring)."""

    def __init__(self, fetch):
        self._fetch = fetch  # () -> Snapshot; raises on any failure
        self._lock = threading.Lock()
        self._snapshot = None
        self.fetches = 0

    @classmethod
    def from_database_url(cls, database_url, *, city_code="KHI"):
        def fetch():
            with psycopg.connect(database_url, autocommit=True,
                                 connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
                return load_snapshot(conn, city_code=city_code)
        return cls(fetch)

    def _load_new(self):
        self.fetches += 1
        snapshot = self._fetch()
        if not isinstance(snapshot, Snapshot):
            raise location.GazetteerError("loader returned no snapshot")
        return snapshot

    def load(self):
        """Load and publish once (single-flight); later calls reuse the snapshot."""
        with self._lock:
            if self._snapshot is None:
                self._snapshot = self._load_new()
            return self._snapshot

    def refresh(self):
        """Reload and publish a changed snapshot. Returns True if it changed. On
        failure the previous snapshot stays published and the error propagates."""
        with self._lock:
            snapshot = self._load_new()
            if self._snapshot is not None and snapshot.version == self._snapshot.version:
                return False
            self._snapshot = snapshot
            return True

    @property
    def snapshot(self):
        return self._snapshot

    def get(self):
        snapshot = self._snapshot
        if snapshot is None:
            raise GazetteerUnavailable("location gazetteer is not loaded")
        return snapshot.gazetteer
