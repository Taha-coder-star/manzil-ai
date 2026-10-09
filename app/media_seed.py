"""Task 4.4 demo media loader: approved prepared images -> S3 + property_media.

    python -m app.media_seed load --agency-id <uuid> --assets-dir DIR --journal FILE
                                  [--listings A,B] [--apply]
    python -m app.media_seed cleanup --agency-id <uuid> --journal FILE [--apply]
    python -m app.media_seed verify --agency-id <uuid> [--listings A,B] [--objects]
                                    [--urls-out FILE]
    python -m app.media_seed fingerprint --agency-id <uuid>

load and cleanup are dry runs unless --apply: a dry run makes no S3 write, no
database write and no journal write. Nothing here sends WhatsApp messages.

Environment: DATABASE_URL (the restricted manzil_app login; tenant rows are only
written inside app.db.tenant_transaction), and for --apply / --objects S3_BUCKET,
S3_REGION, optional S3_ENDPOINT_URL and MEDIA_PUBLIC_BASE_URL (app.media). AWS
credentials come from the instance role. docs/DEMO_MEDIA.md has the runbook.

Identity. An asset (demo/media/scenes.csv, e.g. DH6-101/m01) is the logical
scene; its prepared_sha256 is the content and becomes property_media.content_sha256.
The row ID and the public card token are random UUIDs chosen once per upload
attempt and kept in the journal (a local JSON file, written durably before any
object is uploaded), so a retry reuses the same keys and the card URL cannot be
derived from the manifest. On a rerun an existing row is matched by content
hash (UNIQUE (agency_id, property_id, content_sha256)) and must have the planned
order and label; anything else is a conflict and nothing is written.

Load (--apply):
1. Validate the manifests and every prepared file (sha256, app.media.process_image).
2. Reconcile the journal with the database under the loader advisory lock (the
   lock also waits out a transaction of an interrupted run).
3. Plan read-only: unchanged / insert / conflict per asset; cover per listing.
   Any conflict: report, exit 2, no writes.
4. Journal new attempts (fsync), then upload original, WhatsApp image and card
   (card last) per asset; a session advisory lock coordinates load/cleanup across
   journal paths throughout the operation, without listing row locks during I/O.
5. One tenant transaction: advisory lock, the listings locked FOR UPDATE, plan
   rechecked (any difference: rollback, exit 2), rows inserted, cover set only
   where NULL, version + 1 and updated_at for each listing whose media changed,
   one property_event 'media_added' per such listing; journal marked
   'committing' before COMMIT, 'committed' after it.

Cleanup deletes only the exact keys of journal attempts that, after
reconciliation under the lock, have no row and are referenced by no row.
"""

import argparse
import datetime
import hashlib
import io
import json
import os
import socket
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import psycopg
from PIL import Image
from psycopg.types.json import Jsonb

from app import demo_media as dm
from app import media
from app.db import tenant_transaction
from app.seed import DEMO_AGENCY_NAME, LOGIN

APP_URL_ENV = "DATABASE_URL"
# Two-key advisory lock namespace (seed 4301, queue 3101).
LOCK_NAMESPACE = 4404
LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "30s"
JOURNAL_FORMAT = 1
# What a journal is bound to besides the agency (Journal.bind).
TARGETS = ("database", "storage")
STATES = ("planned", "uploaded", "committing", "committed", "cleaned")
EVENT_SOURCE = "app.media_seed (Task 4.4 demo media)"


class MediaSeedError(Exception):
    """A failure whose message is safe to print (no URLs, credentials or raw errors)."""


class StateChanged(Exception):
    """The database changed between the plan and the locked recheck."""


def parse_agency_id(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise MediaSeedError("--agency-id must be a UUID") from None


def object_keys(agency_id, property_id, media_id, card_token):
    base = f"tenant/{agency_id}/property/{property_id}/{media_id}"
    return {"original": f"{base}/original.jpg", "wa": f"{base}/wa.jpg",
            "card": f"card/{agency_id}/{card_token}.jpg"}


def _db_error(stage, exc):
    # Class, SQLSTATE and constraint only: messages can quote row values.
    try:
        constraint = exc.diag.constraint_name
    except Exception:  # noqa: BLE001 - diagnostics are optional
        constraint = None
    detail = f"{type(exc).__name__}, SQLSTATE {getattr(exc, 'sqlstate', None) or 'n/a'}"
    if constraint:
        detail += f", constraint {constraint}"
    if getattr(exc, "sqlstate", None) == "55P03":
        detail += "; a lock is held (another media load, or an interrupted run whose "\
                  "database session is still open): retry later"
    return MediaSeedError(f"{stage}: database error ({detail})")


# --- journal ------------------------------------------------------------------------

class Journal:
    """Upload attempts of this loader for one agency, in a local JSON file.

    Writes are durable (temporary file, fsync, atomic replace). A lock file keeps
    two applying runs off the same journal; a dry run only reads it."""

    def __init__(self, path, agency_id, entries, locked, target=None):
        self.path = Path(path)
        self.agency_id = str(agency_id)
        self.entries = entries
        self.target = dict(target or {})
        self._locked = locked

    @classmethod
    def open(cls, path, agency_id, *, lock):
        path = Path(path)
        locked = False
        if lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path.with_name(path.name + ".lock"),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise MediaSeedError(
                    f"journal {path.name} is locked by another run; if no run is active, "
                    f"remove {path.name}.lock") from None
            with os.fdopen(fd, "w") as handle:
                handle.write(f"{socket.gethostname()} {os.getpid()} "
                             f"{datetime.datetime.now(datetime.UTC).isoformat()}\n")
            locked = True
        try:
            entries, target = cls._read(path, agency_id)
        except MediaSeedError:
            if locked:
                path.with_name(path.name + ".lock").unlink(missing_ok=True)
            raise
        return cls(path, agency_id, entries, locked, target)

    @staticmethod
    def _read(path, agency_id):
        if not path.exists():
            return {}, {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise MediaSeedError(f"journal {path.name} is unreadable; keep it for "
                                 "inspection and do not delete objects by hand") from None
        if not isinstance(data, dict) or data.get("format") != JOURNAL_FORMAT \
                or not isinstance(data.get("entries"), dict):
            raise MediaSeedError(f"journal {path.name} has an unknown format")
        if data.get("agency_id") != str(agency_id):
            raise MediaSeedError(f"journal {path.name} belongs to another agency")
        target = data.get("target")
        if not isinstance(target, dict) or any(
                not isinstance(target.get(name), str) or not target[name]
                for name in TARGETS):
            raise MediaSeedError(f"journal {path.name} does not record its database and "
                                 "storage target; nothing was changed")
        for media_id, entry in data["entries"].items():
            if not _entry_valid(media_id, entry, agency_id):
                raise MediaSeedError(f"journal {path.name} has an inconsistent entry; "
                                     "nothing was changed")
        return data["entries"], target

    def bind(self, **target):
        """Refuse a journal written for another database or bucket; record new targets.

        Uploaded attempts are only reusable, and their keys only deletable, in the
        storage and database they were made for. None = not known in this run."""
        for name, value in target.items():
            recorded = self.target.get(name)
            if value is None:
                continue
            if recorded is not None and recorded != value:
                raise MediaSeedError(f"journal {self.path.name} was written for another "
                                     f"{name} target; nothing was changed (use the journal "
                                     "of this target, or a new journal file)")
        self.target.update({name: value for name, value in target.items() if value is not None})

    def save(self):
        if any(not self.target.get(name) for name in TARGETS):
            raise MediaSeedError("internal error: the journal target is not bound")
        payload = json.dumps({"format": JOURNAL_FORMAT, "agency_id": self.agency_id,
                              "target": self.target, "entries": self.entries},
                             indent=1, sort_keys=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        if hasattr(os, "O_DIRECTORY"):  # POSIX: make the rename durable too
            fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def close(self):
        if self._locked:
            self.path.with_name(self.path.name + ".lock").unlink(missing_ok=True)
            self._locked = False

    def attempts(self, asset_id, states):
        return [(m, e) for m, e in self.entries.items()
                if e["asset_id"] == asset_id and e["state"] in states]

    def new_attempt(self, asset, property_id):
        media_id, card_token = str(uuid.uuid4()), str(uuid.uuid4())
        self.entries[media_id] = {
            "asset_id": asset.asset_id, "listing_code": asset.listing_code,
            "property_id": str(property_id), "content_sha256": asset.prepared_sha256,
            "card_token": card_token, "state": "planned",
            "keys": object_keys(self.agency_id, property_id, media_id, card_token),
            "updated_at": _now(),
        }
        return media_id

    def set_state(self, media_id, state):
        self.entries[media_id]["state"] = state
        self.entries[media_id]["updated_at"] = _now()


def _now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _entry_valid(media_id, entry, agency_id):
    try:
        uuid.UUID(media_id)
        uuid.UUID(entry["property_id"])
        uuid.UUID(entry["card_token"])
    except (KeyError, TypeError, ValueError):
        return False
    hashes = entry.get("derivative_sha256")
    if hashes is not None and (not isinstance(hashes, dict)
                              or set(hashes) != {"original", "wa", "card"}
                              or any(not isinstance(v, str) or not dm.SHA256.fullmatch(v)
                                     for v in hashes.values())):
        return False
    return (
        entry.get("state") in STATES
        and isinstance(entry.get("asset_id"), str) and dm.ASSET_ID.match(entry["asset_id"])
        and isinstance(entry.get("content_sha256"), str)
        and dm.SHA256.match(entry["content_sha256"])
        # Keys must match the journaled IDs. This is consistency checking, not
        # authentication: the operator must preserve a trusted journal.
        and entry.get("keys") == object_keys(agency_id, entry["property_id"], media_id,
                                             entry["card_token"])
    )


# --- database state and plan ------------------------------------------------------

@dataclass
class ListingState:
    code: str
    id: object
    cover_media_id: object
    version: int
    is_demo: bool
    media: list = field(default_factory=list)


@dataclass
class Plan:
    unchanged: dict = field(default_factory=dict)    # asset_id -> row id
    inserts: list = field(default_factory=list)      # assets
    covers: dict = field(default_factory=dict)       # listing code -> asset_id (cover NULL)
    conflicts: list = field(default_factory=list)
    preserved: int = 0

    def signature(self):
        return (sorted(self.unchanged.items()), sorted(a.asset_id for a in self.inserts),
                sorted(self.covers.items()), self.conflicts)


MEDIA_COLUMNS = ("id", "property_id", "sort_order", "label", "kind", "content_sha256",
                 "original_key", "wa_image_key", "card_image_key", "public_url_card",
                 "exif_stripped", "wa_media_id", "wa_media_expires_at")


def _timeouts(conn, lock_timeout):
    conn.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def _lock(conn, agency_id):
    conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s, pg_catalog.hashtext(%s))",
                 (LOCK_NAMESPACE, str(agency_id)))


def _operation_lock(conn, agency_id, lock_timeout):
    """Serialize load/cleanup, including uploads and copied journals.

    Session lock survives transaction boundaries; the dedicated connection is
    closed in the caller's finally block (also on failure/uncertain commit).
    Acquire BEFORE reading the journal. No transaction or row lock spans uploads.
    """
    try:
        with tenant_transaction(conn, agency_id):
            _timeouts(conn, lock_timeout)
            conn.execute("SELECT pg_catalog.pg_advisory_lock(%s, pg_catalog.hashtext(%s))",
                         (LOCK_NAMESPACE, str(agency_id)))
    except psycopg.Error as exc:
        raise _db_error("operation lock", exc) from None


def _check_agency(conn, agency_id):
    row = conn.execute("SELECT name FROM agency WHERE id = %s", (agency_id,)).fetchone()
    if row is None:
        raise MediaSeedError("agency not found for the runtime login (check --agency-id)")
    if row[0] != DEMO_AGENCY_NAME:
        raise MediaSeedError(f"agency is not the demo agency {DEMO_AGENCY_NAME!r}; "
                             "nothing written")


def read_state(conn, codes, *, for_update=False):
    rows = conn.execute(
        "SELECT listing_code, id, cover_media_id, version, is_demo FROM property "
        "WHERE listing_code = ANY(%s) ORDER BY id" + (" FOR UPDATE" if for_update else ""),
        (sorted(codes),),
    ).fetchall()
    listings = {r[0]: ListingState(*r) for r in rows}
    missing = sorted(set(codes) - set(listings))
    if missing:
        raise MediaSeedError(f"listings not found in this agency: {', '.join(missing)} "
                             "(Task 4.3 seed must be complete)")
    not_demo = sorted(c for c, s in listings.items() if not s.is_demo)
    if not_demo:
        raise MediaSeedError(f"not demo listings (is_demo false): {', '.join(not_demo)}")
    by_id = {s.id: s for s in listings.values()}
    for row in conn.execute(
        "SELECT " + ", ".join(MEDIA_COLUMNS) + " FROM property_media "
        "WHERE property_id = ANY(%s) ORDER BY property_id, sort_order",
        ([s.id for s in listings.values()],),
    ).fetchall():
        by_id[row[1]].media.append(dict(zip(MEDIA_COLUMNS, row)))
    return listings


def _incompatible(asset, row):
    problems = []
    if row["sort_order"] != asset.sort_order:
        problems.append(f"order {row['sort_order']} (planned {asset.sort_order})")
    if row["label"] != asset.label:
        problems.append(f"label {row['label'] or 'NULL'} (planned {asset.label or 'NULL'})")
    if row["kind"] != "image":
        problems.append(f"kind {row['kind']}")
    if not (row["original_key"] and row["wa_image_key"] and row["card_image_key"]
            and row["exif_stripped"] and (row["public_url_card"] or "").startswith("https://")):
        problems.append("missing derivative keys, card URL or exif_stripped")
    return problems


def classify(assets, listings, journal_entries):
    """The plan: per asset unchanged / insert / conflict, and covers to set."""
    plan = Plan()
    committed = {}
    for media_id, entry in journal_entries.items():
        if entry["state"] == "committed":
            committed.setdefault(entry["asset_id"], []).append((media_id, entry))
    by_listing = {}
    for asset in assets:
        by_listing.setdefault(asset.listing_code, []).append(asset)
    for code, group in sorted(by_listing.items()):
        state = listings[code]
        rows = state.media
        row_ids = {r["id"] for r in rows}
        row_id_text = {str(i) for i in row_ids}
        by_sha = {r["content_sha256"]: r for r in rows if r["content_sha256"]}
        by_order = {r["sort_order"]: r for r in rows}
        matched, targets = set(), {}
        for asset in group:
            where = f"{asset.asset_id}"
            row = by_sha.get(asset.prepared_sha256)
            if row is not None:
                matched.add(row["id"])
                problems = _incompatible(asset, row)
                if problems:
                    plan.conflicts.append(f"{where}: incompatible_metadata - the stored image "
                                          f"has {'; '.join(problems)}; left unchanged")
                else:
                    plan.unchanged[asset.asset_id] = row["id"]
                    targets[asset.asset_id] = row["id"]
                continue
            old = [r for r in rows if r["content_sha256"] in asset.previous]
            loaded = committed.get(asset.asset_id, [])
            if old or any(m in row_id_text for m, _ in loaded):
                matched.update(r["id"] for r in old)
                plan.conflicts.append(f"{where}: changed_asset - an earlier image of this "
                                      "asset is stored; replacing it needs a reviewed policy")
                continue
            if loaded:
                plan.conflicts.append(f"{where}: removed_after_load - this asset was loaded "
                                      "and its row has since been removed; not re-added")
                continue
            holder = by_order.get(asset.sort_order)
            if holder is not None:
                plan.conflicts.append(f"{where}: slot_occupied - order {asset.sort_order} is "
                                      "held by other media; left unchanged")
                continue
            plan.inserts.append(asset)
        plan.preserved += len(row_ids - matched)
        cover_asset = next((a for a in group if a.sort_order == 0), None)
        if cover_asset is None:
            continue
        if state.cover_media_id is None and cover_asset.asset_id in targets:
            # The planned cover is already stored but the listing has no cover: staff
            # cleared it after loading. Not reset without a reviewed policy.
            plan.conflicts.append(f"{code}: cover_cleared - {cover_asset.asset_id} is stored "
                                  "but the listing has no cover; left unchanged")
        elif state.cover_media_id is None:
            plan.covers[code] = cover_asset.asset_id
        elif state.cover_media_id != targets.get(cover_asset.asset_id):
            plan.conflicts.append(f"{code}: cover_taken - the listing already has another "
                                  "cover; it is preserved")
    return plan


# --- run ----------------------------------------------------------------------------

@dataclass
class LoadResult:
    apply: bool
    plan: Plan | None = None
    outcome: str = ""
    uploaded: int = 0
    inserted: int = 0
    covers_set: int = 0
    notes: list = field(default_factory=list)

    @property
    def conflicts(self):
        return bool(self.plan and self.plan.conflicts) or self.outcome.startswith("rolled back")


@dataclass
class Hooks:
    """Test seams: called at the named points of an apply."""
    after_put: object = None       # (asset, kind)
    before_db: object = None       # ()
    before_commit: object = None   # (conn)


def _connect(url):
    if not url:
        raise MediaSeedError(f"{APP_URL_ENV} is not set")
    try:
        conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
    except psycopg.Error as exc:
        raise MediaSeedError(f"could not connect with {APP_URL_ENV} "
                             f"({type(exc).__name__})") from None
    try:
        superuser, bypass, owns = conn.execute(LOGIN).fetchone()
        current, session = conn.execute("SELECT current_user, session_user").fetchone()
    except (psycopg.Error, TypeError) as exc:
        conn.close()
        raise MediaSeedError(f"connection check failed ({type(exc).__name__}); apply "
                             "migrations 001-009 first") from None
    if superuser or bypass or owns or current != "manzil_app" or session != "manzil_app":
        conn.close()
        raise MediaSeedError(f"{APP_URL_ENV} must be the restricted runtime login "
                             "manzil_app (no superuser, no BYPASSRLS, not the table owner)")
    return conn


def storage_from_env(environ):
    bucket = (environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        raise MediaSeedError("S3_BUCKET is not set")
    return media.S3Storage(bucket, (environ.get("S3_REGION") or "").strip() or None,
                           (environ.get("S3_ENDPOINT_URL") or "").strip() or None)


def public_base_from_env(environ):
    settings = SimpleNamespace(
        s3_bucket=(environ.get("S3_BUCKET") or "").strip() or None,
        s3_region=(environ.get("S3_REGION") or "").strip() or None,
        s3_endpoint_url=(environ.get("S3_ENDPOINT_URL") or "").strip() or None,
        media_public_base_url=(environ.get("MEDIA_PUBLIC_BASE_URL") or "").strip() or None,
    )
    base = media.public_base_url(settings)
    if base is None or not base.startswith("https://"):
        raise MediaSeedError("the public card base URL is unknown or not https "
                             "(set MEDIA_PUBLIC_BASE_URL, or S3_BUCKET and S3_REGION)")
    return base


def storage_target(environ):
    """The bucket (and non-AWS endpoint) objects go to, or None if S3_BUCKET is unset.
    A custom endpoint is recorded only as a hash."""
    bucket = (environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        return None
    endpoint = (environ.get("S3_ENDPOINT_URL") or "").strip().rstrip("/")
    where = ("endpoint " + hashlib.sha256(endpoint.encode()).hexdigest()[:16]
             if endpoint else "aws")
    return f"s3://{bucket} ({where})"


def database_target(conn, agency_id):
    """Bind to the resolved connection target, schema and agency creation time.

    Only an allowlist of non-secret libpq connection fields is hashed. Passwords,
    SSL key paths, options and other credentials are never journaled. This is a
    connection fingerprint, NOT physical identity: endpoint reuse or a restore
    at the same endpoint/schema can still match.
    """
    params = conn.info.get_parameters()
    endpoint = {k: params.get(k, "")
                for k in ("host", "hostaddr", "port", "dbname", "service")}
    digest = hashlib.sha256(json.dumps(endpoint, sort_keys=True).encode()).hexdigest()
    with tenant_transaction(conn, agency_id):
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute("SELECT pg_catalog.current_database(), created_at, "
                           "pg_catalog.current_schema() FROM agency "
                           "WHERE id = %s", (agency_id,)).fetchone()
    if row is None:
        raise MediaSeedError("agency not found for the runtime login (check --agency-id)")
    identity = [digest, row[0], row[1].isoformat(), row[2]]
    return "connection-v2 " + hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def load_assets(media_dir, listings_arg, assets_dir):
    try:
        scenes, intake = dm.load_plan(media_dir)
        listings = dm.parse_listings(listings_arg, scenes)
        errors = dm.validate_scenes(scenes, dm.load_properties()) + dm.validate_intake(
            scenes, intake, listings)
    except dm.MediaPlanError as exc:
        raise MediaSeedError(str(exc)) from None
    if errors:
        raise MediaSeedError(f"manifest invalid ({len(errors)} error(s); first: {errors[0]}); "
                             "run python -m app.demo_media validate")
    assets = dm.approved_assets(scenes, intake, listings)
    data = {}
    if assets_dir is not None:
        for asset in assets:
            try:
                data[asset.asset_id] = dm.read_prepared(assets_dir, asset)
            except dm.MediaPlanError as exc:
                raise MediaSeedError(str(exc)) from None
    return assets, data


def reconcile(conn, agency_id, journal, *, write, lock_timeout=LOCK_TIMEOUT):
    """Bring open journal attempts in line with the database.

    Under the advisory lock, so any transaction of an earlier or concurrent run
    has ended: an attempt with a row is committed; 'committing' without a row did
    not commit and goes back to 'uploaded'. Only with write is the journal saved
    (a dry run reconciles in memory). Returns the IDs of committed attempts whose
    rows are gone."""
    ids = list(journal.entries)
    if not ids:
        return []
    with tenant_transaction(conn, agency_id):
        _timeouts(conn, lock_timeout)
        _lock(conn, agency_id)
        present = {str(r[0]) for r in conn.execute(
            "SELECT id FROM property_media WHERE id = ANY(%s::uuid[])", (ids,)).fetchall()}
    gone = []
    for media_id, entry in journal.entries.items():
        if entry["state"] in ("planned", "uploaded", "committing") and media_id in present:
            journal.set_state(media_id, "committed")
        elif entry["state"] == "committing":
            journal.set_state(media_id, "uploaded")
        elif entry["state"] == "committed" and media_id not in present:
            gone.append(media_id)
    if write:
        journal.save()
    return gone


def _process(asset, data):
    try:
        processed = media.process_image(data, "image/jpeg")
    except media.MediaRejected as exc:
        raise MediaSeedError(f"{asset.asset_id}: prepared file rejected ({exc.code})") from None
    if processed.original_ext != "jpg":
        raise MediaSeedError(f"{asset.asset_id}: prepared file must be JPEG")
    return processed


def run_load(agency_id, app_url, environ, assets_dir, journal_path, *, listings=None,
             apply=False, storage=None, media_dir=dm.MEDIA_DIR, hooks=None,
             lock_timeout=LOCK_TIMEOUT):
    agency_id = parse_agency_id(agency_id)
    hooks = hooks or Hooks()
    assets, data = load_assets(media_dir, listings, assets_dir)
    if not assets:
        raise MediaSeedError("no approved assets in scope")
    public_base = None
    if apply:
        public_base = public_base_from_env(environ)
        storage = storage or storage_from_env(environ)
    result = LoadResult(apply=apply)
    conn = _connect(app_url)
    journal = None
    try:
        _operation_lock(conn, agency_id, lock_timeout)
        journal = Journal.open(journal_path, agency_id, lock=apply)
        try:
            journal.bind(database=database_target(conn, agency_id),
                         storage=storage_target(environ))
            gone = reconcile(conn, agency_id, journal, write=apply, lock_timeout=lock_timeout)
            with tenant_transaction(conn, agency_id):
                _timeouts(conn, lock_timeout)
                conn.execute("SET TRANSACTION READ ONLY")
                _check_agency(conn, agency_id)
                listings_state = read_state(conn, {a.listing_code for a in assets})
        except psycopg.Error as exc:
            raise _db_error("plan", exc) from None
        if gone:
            result.notes.append(f"{len(gone)} loaded row(s) were removed after loading")
        plan = classify(assets, listings_state, journal.entries)
        result.plan = plan
        processed = {a.asset_id: _process(a, data[a.asset_id]) for a in plan.inserts}
        if plan.conflicts or not apply:
            result.outcome = ("conflicts: nothing written" if plan.conflicts
                              else "dry run: nothing written")
            return result
        if not plan.inserts and not plan.covers:
            result.outcome = "unchanged: nothing to do"
            return result

        # Journal every attempt before the first upload.
        attempt = {}
        for asset in plan.inserts:
            open_attempts = [m for m, e in journal.attempts(asset.asset_id,
                                                           ("planned", "uploaded"))
                             if e["content_sha256"] == asset.prepared_sha256
                             and e["property_id"] == str(listings_state[asset.listing_code].id)]
            attempt[asset.asset_id] = (open_attempts[0] if open_attempts else journal.new_attempt(
                asset, listings_state[asset.listing_code].id))
            entry = journal.entries[attempt[asset.asset_id]]
            hashes = {k: dm.sha256_hex(getattr(processed[asset.asset_id], k))
                      for k in ("original", "wa", "card")}
            if entry.get("derivative_sha256", hashes) != hashes:
                raise MediaSeedError(f"{asset.asset_id}: processing output changed; review "
                                     "the processing environment before retrying")
            entry["derivative_sha256"] = hashes
        journal.save()

        for asset in plan.inserts:
            media_id = attempt[asset.asset_id]
            entry = journal.entries[media_id]
            was_uploaded = entry["state"] == "uploaded"
            image = processed[asset.asset_id]
            for kind, body, content_type, cache in (
                ("original", image.original, image.original_type, None),
                ("wa", image.wa, "image/jpeg", None),
                ("card", image.card, "image/jpeg", media.CARD_CACHE_CONTROL),
            ):
                try:
                    existing = _get_if_present(storage, entry["keys"][kind])
                    if existing is not None:
                        if dm.sha256_hex(existing) != dm.sha256_hex(body):
                            raise MediaSeedError(f"{asset.asset_id}: {kind} checksum mismatch; "
                                                 "existing object left unchanged")
                    elif was_uploaded:
                        raise MediaSeedError(f"{asset.asset_id}: uploaded {kind} object missing; "
                                             "nothing committed; review recovery")
                    else:
                        storage.put(entry["keys"][kind], body, content_type, cache)
                except MediaSeedError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reported without its message
                    raise MediaSeedError(
                        f"upload failed for {asset.asset_id} ({kind}, {type(exc).__name__}); "
                        "nothing was written to the database: rerun load --apply to retry, "
                        "or cleanup --apply to remove this run's objects") from None
                if hooks.after_put:
                    hooks.after_put(asset, kind)
            journal.set_state(media_id, "uploaded")
            journal.save()
            result.uploaded += not was_uploaded

        if hooks.before_db:
            hooks.before_db()
        # Authenticated readback immediately before committing. Prepared JPEGs
        # are re-encoded by process_image, so compare with its actual derivatives.
        for asset in plan.inserts:
            entry = journal.entries[attempt[asset.asset_id]]
            row = dict(zip(("original_key", "wa_image_key", "card_image_key"),
                           (entry["keys"][k] for k in ("original", "wa", "card"))))
            problems = _check_objects(storage, asset, row, processed[asset.asset_id])
            if problems:
                raise MediaSeedError("; ".join(problems))
        try:
            _db_phase(conn, agency_id, assets, plan, journal, attempt, public_base, result,
                      hooks, lock_timeout)
        except StateChanged:
            result.outcome = ("rolled back: the database changed since the plan; the "
                              "uploaded objects are journaled (rerun load to retry, or "
                              "cleanup --apply)")
            result.inserted = result.covers_set = 0
            return result
        for media_id in attempt.values():
            journal.set_state(media_id, "committed")
        journal.save()
        result.outcome = "committed"
        return result
    finally:
        if journal is not None:
            journal.close()
        conn.close()


def _db_phase(conn, agency_id, assets, plan, journal, attempt, public_base, result, hooks,
              lock_timeout):
    reached_commit = False
    try:
        with tenant_transaction(conn, agency_id):
            _timeouts(conn, lock_timeout)
            _lock(conn, agency_id)
            _check_agency(conn, agency_id)
            state = read_state(conn, {a.listing_code for a in assets}, for_update=True)
            recheck = classify(assets, state, journal.entries)
            if recheck.signature() != plan.signature():
                result.plan = recheck
                raise StateChanged()
            new_ids = {}
            for asset in plan.inserts:
                media_id = attempt[asset.asset_id]
                entry = journal.entries[media_id]
                keys = entry["keys"]
                conn.execute(
                    """
                    INSERT INTO property_media (
                        id, agency_id, property_id, original_key, wa_image_key, card_image_key,
                        content_sha256, mime_type, sort_order, kind, label, public_url_card,
                        exif_stripped
                    ) VALUES (%s, current_agency_id(), %s, %s, %s, %s, %s, 'image/jpeg', %s,
                              'image', %s, %s, TRUE)
                    """,
                    (media_id, state[asset.listing_code].id, keys["original"], keys["wa"],
                     keys["card"], asset.prepared_sha256, asset.sort_order, asset.label,
                     f"{public_base}/{keys['card']}"),
                )
                new_ids[asset.asset_id] = media_id
                result.inserted += 1
            changed = {a.listing_code for a in plan.inserts} | set(plan.covers)
            for code in sorted(changed):
                listing = state[code]
                cover = None
                if code in plan.covers:
                    asset_id = plan.covers[code]
                    cover = new_ids.get(asset_id) or plan.unchanged[asset_id]
                # Follows the property.version convention (INVENTORY_SCHEMA.md): every
                # committed listing change bumps it. last_confirmed_at is never touched.
                row = conn.execute(
                    """
                    UPDATE property SET cover_media_id = COALESCE(cover_media_id, %s),
                        version = version + 1, updated_at = now()
                    WHERE id = %s AND version = %s
                      AND cover_media_id IS NOT DISTINCT FROM %s
                    RETURNING version
                    """,
                    (cover, listing.id, listing.version, listing.cover_media_id),
                ).fetchone()
                if row is None:
                    raise StateChanged()
                if cover is not None:
                    result.covers_set += 1
                added = [new_ids[a.asset_id] for a in plan.inserts if a.listing_code == code]
                conn.execute(
                    """
                    INSERT INTO property_event (agency_id, property_id, event_type,
                                                from_version, to_version, changes)
                    VALUES (current_agency_id(), %s, 'media_added', %s, %s, %s)
                    """,
                    (listing.id, listing.version, row[0], Jsonb({
                        "source": EVENT_SOURCE, "media_ids": [str(m) for m in added],
                        "asset_ids": [a.asset_id for a in plan.inserts if a.listing_code == code],
                        "cover_media_id": str(cover) if cover else None,
                    })),
                )
            for media_id in attempt.values():
                journal.set_state(media_id, "committing")
            journal.save()
            reached_commit = True
            if hooks.before_commit:
                hooks.before_commit(conn)
    except StateChanged:
        for media_id in attempt.values():
            if journal.entries[media_id]["state"] == "committing":
                journal.set_state(media_id, "uploaded")
        journal.save()
        raise
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        if reached_commit:
            raise MediaSeedError(
                f"commit outcome unknown ({type(exc).__name__}): run load again (dry run "
                "first) to reconcile the journal with the database; do not run cleanup "
                "before that") from None
        raise _db_error("database phase rolled back", exc) from None
    except psycopg.Error as exc:
        for media_id in attempt.values():
            if journal.entries[media_id]["state"] == "committing":
                journal.set_state(media_id, "uploaded")
        journal.save()
        raise _db_error("database phase rolled back", exc) from None


# --- cleanup ------------------------------------------------------------------------

@dataclass
class CleanupResult:
    apply: bool
    candidates: list = field(default_factory=list)
    kept: list = field(default_factory=list)
    deleted: int = 0
    failures: list = field(default_factory=list)


def run_cleanup(agency_id, app_url, environ, journal_path, *, apply=False, storage=None,
                lock_timeout=LOCK_TIMEOUT):
    """Delete the exact objects of journal attempts proven unreferenced."""
    agency_id = parse_agency_id(agency_id)
    if apply:
        storage = storage or storage_from_env(environ)
    if not Path(journal_path).exists():
        raise MediaSeedError("journal not found: without it no object can be proven to "
                             "belong to this loader, so nothing is deleted")
    result = CleanupResult(apply=apply)
    conn = _connect(app_url)
    journal = None
    try:
        _operation_lock(conn, agency_id, lock_timeout)
        journal = Journal.open(journal_path, agency_id, lock=apply)
        try:
            journal.bind(database=database_target(conn, agency_id),
                         storage=storage_target(environ))
            reconcile(conn, agency_id, journal, write=apply, lock_timeout=lock_timeout)
            open_attempts = {m: e for m, e in journal.entries.items()
                             if e["state"] in ("planned", "uploaded")}
            keys = [k for e in open_attempts.values() for k in e["keys"].values()]
            with tenant_transaction(conn, agency_id):
                _timeouts(conn, lock_timeout)
                if apply:
                    _lock(conn, agency_id)
                referenced = set()
                for table in ("property_media", "inbound_media"):
                    for row in conn.execute(
                        f"SELECT original_key, wa_image_key, card_image_key FROM {table} "
                        "WHERE original_key = ANY(%s) OR wa_image_key = ANY(%s) "
                        "OR card_image_key = ANY(%s)", (keys, keys, keys)).fetchall():
                        referenced.update(k for k in row if k)
                rows = {str(r[0]) for r in conn.execute(
                    "SELECT id FROM property_media WHERE id = ANY(%s::uuid[])",
                    (list(open_attempts),)).fetchall()}
        except psycopg.Error as exc:
            raise _db_error("cleanup", exc) from None
        for media_id, entry in sorted(open_attempts.items()):
            if media_id in rows or referenced & set(entry["keys"].values()):
                result.kept.append(f"{entry['asset_id']}: referenced by a row; kept")
            else:
                result.candidates.append((media_id, entry))
        if not apply:
            return result
        for media_id, entry in result.candidates:
            try:
                # Do not delete unexpected pre-existing/replaced content, even if
                # its key happens to match a journaled random attempt key.
                hashes = entry.get("derivative_sha256")
                if hashes is None:
                    result.kept.append(f"{entry['asset_id']}: no derivative hashes; kept")
                    continue
                blobs = {kind: _get_if_present(storage, key)
                         for kind, key in entry["keys"].items()}
                if any(body is not None and dm.sha256_hex(body) != hashes[kind]
                       for kind, body in blobs.items()):
                    result.kept.append(f"{entry['asset_id']}: unexpected object bytes; kept")
                    continue
                for kind in ("card", "wa", "original"):  # the public object first
                    storage.delete(entry["keys"][kind])
            except Exception as exc:  # noqa: BLE001 - reported without its message
                result.failures.append(f"{entry['asset_id']}: delete failed "
                                       f"({type(exc).__name__}); rerun cleanup")
                continue
            journal.set_state(media_id, "cleaned")
            journal.save()
            result.deleted += 1
        return result
    finally:
        if journal is not None:
            journal.close()
        conn.close()


# --- verify and fingerprint -----------------------------------------------------------

FINGERPRINT_EXCLUDED = ("cover_media_id", "version", "updated_at")


def fingerprint(conn, agency_id):
    """Counts and a hash of every listing column except cover / version / updated_at."""
    with tenant_transaction(conn, agency_id):
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            "SELECT listing_code, (to_jsonb(p) - %s - %s - %s)::text FROM property p "
            "ORDER BY listing_code", FINGERPRINT_EXCLUDED).fetchall()
        counts = dict(conn.execute(
            "SELECT status, count(*) FROM property GROUP BY status").fetchall())
        public = conn.execute("SELECT count(*) FROM v_public_listing").fetchone()[0]
        freshness = conn.execute(
            "SELECT listing_code, last_confirmed_at FROM property ORDER BY listing_code"
        ).fetchall()
    digest = hashlib.sha256("\n".join(f"{c}\t{j}" for c, j in rows).encode()).hexdigest()
    fresh = hashlib.sha256("\n".join(f"{c}\t{t.isoformat() if t else ''}"
                                     for c, t in freshness).encode()).hexdigest()
    return {"listings": len(rows), "statuses": dict(sorted(counts.items())),
            "public_listings": public, "business_sha256": digest, "freshness_sha256": fresh}


@dataclass
class VerifyResult:
    problems: list = field(default_factory=list)
    checked_rows: int = 0
    checked_objects: int = 0
    listings: int = 0
    fingerprint: dict = field(default_factory=dict)


def run_verify(agency_id, app_url, environ, *, listings=None, objects=False, storage=None,
               urls_out=None, media_dir=dm.MEDIA_DIR, assets_dir=None):
    agency_id = parse_agency_id(agency_id)
    if objects and assets_dir is None:
        raise MediaSeedError("--objects requires --assets-dir with approved prepared files")
    assets, data = load_assets(media_dir, listings, assets_dir if objects else None)
    expected = {a.asset_id: _process(a, data[a.asset_id]) for a in assets} if objects else {}
    public_base = public_base_from_env(environ) if (objects or urls_out) else None
    if objects:
        storage = storage or storage_from_env(environ)
    result = VerifyResult()
    conn = _connect(app_url)
    try:
        try:
            with tenant_transaction(conn, agency_id):
                conn.execute("SET TRANSACTION READ ONLY")
                _check_agency(conn, agency_id)
                state = read_state(conn, {a.listing_code for a in assets})
            result.fingerprint = fingerprint(conn, agency_id)
        except psycopg.Error as exc:
            raise _db_error("verify", exc) from None
    finally:
        conn.close()
    plan = classify(assets, state, {})
    result.listings = len(state)
    result.problems += plan.conflicts
    missing = [a.asset_id for a in plan.inserts]
    if missing:
        result.problems.append(f"{len(missing)} asset(s) not loaded (first: {missing[0]})")
    by_asset = {a.asset_id: a for a in assets}
    rows = {r["id"]: (code, r) for code, s in state.items() for r in s.media}
    lines = []
    for asset_id, row_id in sorted(plan.unchanged.items()):
        asset = by_asset[asset_id]
        code, row = rows[row_id]
        result.checked_rows += 1
        if asset.sort_order == 0 and state[code].cover_media_id != row_id:
            result.problems.append(f"{code}: cover is not {asset_id}")
        if (row["wa_media_id"] is None) != (row["wa_media_expires_at"] is None):
            result.problems.append(f"{asset_id}: inconsistent WhatsApp cache fields")
        if public_base and not row["public_url_card"].startswith(f"{public_base}/card/"):
            result.problems.append(f"{asset_id}: card URL is not under the public base")
        if not row["card_image_key"].startswith(f"card/{agency_id}/") or not \
                row["original_key"].startswith(f"tenant/{agency_id}/"):
            result.problems.append(f"{asset_id}: unexpected object keys")
        if urls_out:
            lines.append("\t".join((asset_id, row["public_url_card"],
                                    f"{public_base}/{row['original_key']}",
                                    f"{public_base}/{row['wa_image_key']}")))
        if objects:
            result.problems += _check_objects(storage, asset, row, expected[asset_id])
            result.checked_objects += 3
    for code, listing in state.items():
        count = len(listing.media)
        if count < dm.MIN_PER_LISTING:
            result.problems.append(f"{code}: {count} image(s), fewer than {dm.MIN_PER_LISTING}")
    if urls_out:
        Path(urls_out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def _get_if_present(storage, key):
    """Only a definite missing-key response permits a put; denial/outage must stop."""
    try:
        return storage.get(key)
    except KeyError:  # in-memory fake storage
        return None
    except Exception as exc:
        response = getattr(exc, "response", {})
        if response.get("Error", {}).get("Code") == "NoSuchKey":
            return None
        raise


def _check_objects(storage, asset, row, expected):
    try:
        blobs = {k: storage.get(row[c]) for k, c in (("original", "original_key"),
                                                    ("wa", "wa_image_key"),
                                                    ("card", "card_image_key"))}
    except Exception as exc:  # noqa: BLE001 - reported without its message
        return [f"{asset.asset_id}: object missing or unreadable ({type(exc).__name__})"]
    try:
        return _inspect_objects(asset, blobs, expected)
    except (OSError, ValueError, SyntaxError) as exc:
        return [f"{asset.asset_id}: object unreadable ({type(exc).__name__})"]


def _inspect_objects(asset, blobs, expected):
    problems = []
    for kind, blob in blobs.items():
        if dm.sha256_hex(blob) != dm.sha256_hex(getattr(expected, kind)):
            problems.append(f"{asset.asset_id}: {kind} checksum mismatch")
        if media.has_metadata(blob):
            problems.append(f"{asset.asset_id}: {kind} carries metadata")
    with Image.open(io.BytesIO(blobs["card"])) as card:
        if card.size != media.CARD_SIZE:
            problems.append(f"{asset.asset_id}: card is {card.size}, not {media.CARD_SIZE}")
    with Image.open(io.BytesIO(blobs["wa"])) as wa:
        if max(wa.size) > media.WA_MAX_EDGE or len(blobs["wa"]) > media.WA_MAX_BYTES:
            problems.append(f"{asset.asset_id}: WhatsApp image too large")
    with Image.open(io.BytesIO(blobs["original"])) as original:
        width, height = original.size
    box, _, _ = dm.mark_layout(width, height, asset.mark, asset.corner)
    missing = dm.check_marks(width, height, box, SimpleNamespace(**blobs))
    if missing:
        problems.append(f"{asset.asset_id}: mark not detected in {', '.join(missing)}")
    return problems


# --- CLI ----------------------------------------------------------------------------

def _print_plan(result):
    plan = result.plan
    print("Mode: apply" if result.apply else
          "Mode: dry run (no S3, database or journal writes; use --apply)")
    if plan is not None:
        print(f"Assets: {len(plan.unchanged)} unchanged, {len(plan.inserts)} to insert; "
              f"covers to set: {len(plan.covers)}; other media preserved: {plan.preserved}")
        for conflict in plan.conflicts:
            print(f"  CONFLICT {conflict}")
    for note in result.notes:
        print(f"  NOTE {note}")
    if result.apply:
        print(f"Uploaded {result.uploaded} asset(s) (3 objects each); inserted "
              f"{result.inserted} row(s); covers set {result.covers_set}")
    print(f"Outcome: {result.outcome}")


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--media-dir", default=str(dm.MEDIA_DIR), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="plan (default) or --apply the media load")
    load.add_argument("--agency-id", required=True)
    load.add_argument("--assets-dir", required=True)
    load.add_argument("--journal", required=True)
    load.add_argument("--listings")
    load.add_argument("--apply", action="store_true")
    cleanup = sub.add_parser("cleanup", help="remove objects of unfinished attempts")
    cleanup.add_argument("--agency-id", required=True)
    cleanup.add_argument("--journal", required=True)
    cleanup.add_argument("--apply", action="store_true")
    verify = sub.add_parser("verify", help="check loaded media (read-only)")
    verify.add_argument("--agency-id", required=True)
    verify.add_argument("--listings")
    verify.add_argument("--objects", action="store_true", help="also read the S3 objects")
    verify.add_argument("--assets-dir", help="approved prepared files (required with --objects)")
    verify.add_argument("--urls-out")
    finger = sub.add_parser("fingerprint", help="listing counts and business-column hash")
    finger.add_argument("--agency-id", required=True)
    args = parser.parse_args(argv)
    app_url = (environ.get(APP_URL_ENV) or "").strip()
    try:
        if args.command == "load":
            result = run_load(args.agency_id, app_url, environ, args.assets_dir,
                              args.journal, listings=args.listings, apply=args.apply,
                              media_dir=args.media_dir)
            _print_plan(result)
            return 2 if result.conflicts else 0
        if args.command == "cleanup":
            result = run_cleanup(args.agency_id, app_url, environ, args.journal,
                                 apply=args.apply)
            print("Mode: apply" if args.apply else "Mode: dry run (nothing deleted)")
            print(f"Unfinished attempts to remove: {len(result.candidates)} "
                  f"({3 * len(result.candidates)} exact object keys); kept: {len(result.kept)}")
            for kept in result.kept:
                print(f"  KEPT {kept}")
            for failure in result.failures:
                print(f"  FAILED {failure}")
            if args.apply:
                print(f"Deleted the objects of {result.deleted} attempt(s)")
            return 1 if result.failures else 0
        if args.command == "verify":
            result = run_verify(args.agency_id, app_url, environ, listings=args.listings,
                                objects=args.objects, urls_out=args.urls_out,
                                media_dir=args.media_dir, assets_dir=args.assets_dir)
            print(f"Listings checked: {result.listings}; rows: {result.checked_rows}; "
                  f"objects: {result.checked_objects}")
            print(f"Fingerprint: {json.dumps(result.fingerprint, sort_keys=True)}")
            for problem in result.problems:
                print(f"  PROBLEM {problem}")
            print("Verify: OK" if not result.problems else
                  f"Verify: {len(result.problems)} problem(s)")
            return 0 if not result.problems else 2
        conn = _connect(app_url)
        try:
            with conn:
                print(json.dumps(fingerprint(conn, parse_agency_id(args.agency_id)),
                                 sort_keys=True))
        except psycopg.Error as exc:
            raise _db_error("fingerprint", exc) from None
        return 0
    except MediaSeedError as exc:
        print(f"Media seed failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
