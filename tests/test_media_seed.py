"""Task 4.4 loader against a migrated, disposable schema seeded with the Task 4.3 dataset.

Tenant reads and writes use the restricted manzil_app login. S3 is an in-memory
fake and WhatsApp a fake channel; images are synthetic TEST FIXTURES
(tests/demo_media_fixtures.py). Nothing reaches AWS or Meta.
"""

import json
import secrets
import threading
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app import demo_media as dm
from app import media_seed as ms
from app import media_send_check as sc
from app import seed
from app.db import tenant_transaction
from app.provision_agency import create_agency
from tests.demo_media_fixtures import build_media, replace_asset, write_intake
from tests.test_media import FakeChannel, FakeStorage
from tests.test_seed import ENV as SEED_ENV

S3_ENV = {"S3_BUCKET": "demo-bucket", "S3_REGION": "us-east-1"}
PUBLIC = "https://demo-bucket.s3.us-east-1.amazonaws.com"
PILOT = set(dm.PILOT_LISTINGS)
PILOT_ARG = ",".join(dm.PILOT_LISTINGS)


class Bucket(FakeStorage):
    def __init__(self):
        super().__init__()
        self.deletes = []

    def delete(self, key):
        self.deletes.append(key)
        super().delete(key)


def make_demo_agency(admin_url, app_url, name=seed.DEMO_AGENCY_NAME):
    with psycopg.connect(app_url, autocommit=True) as conn:
        agency = create_agency(conn, name, "demo-" + uuid.uuid4().hex, uuid.uuid4().hex)
    if name == seed.DEMO_AGENCY_NAME:
        result = seed.run(agency, admin_url, app_url, SEED_ENV, apply=True)
        assert not result.conflicts
    return agency


@pytest.fixture
def demo(app_role_db, tmp_path):
    admin_url, app_url, _ = app_role_db
    agency = make_demo_agency(admin_url, app_url)
    return SimpleNamespace(admin_url=admin_url, app_url=app_url, agency=agency,
                           tmp=tmp_path, bucket=Bucket(), journal=tmp_path / "j" / "media.json")


@pytest.fixture
def pilot(demo):
    demo.media = build_media(demo.tmp / "pilot", PILOT)
    return demo


def load(d, *, apply=True, listings=PILOT_ARG, journal=None, agency=None, **kwargs):
    return ms.run_load(agency or d.agency, d.app_url, S3_ENV, d.media.assets_dir,
                       journal or d.journal, listings=listings, apply=apply,
                       storage=d.bucket, media_dir=d.media.media_dir, **kwargs)


def q(d, sql, params=(), agency=None):
    with psycopg.connect(d.app_url, autocommit=True) as conn, \
            tenant_transaction(conn, agency or d.agency):
        return conn.execute(sql, params).fetchall()


def listing_rows(d, agency=None):
    return {code: (version, cover, updated, confirmed) for code, version, cover, updated,
            confirmed in q(d, "SELECT listing_code, version, cover_media_id, updated_at, "
                              "last_confirmed_at FROM property", agency=agency)}


def media_rows(d, agency=None):
    return q(d, "SELECT p.listing_code, m.sort_order, m.label, m.id, m.content_sha256, "
                "m.original_key, m.wa_image_key, m.card_image_key, m.public_url_card, "
                "m.kind, m.mime_type, m.exif_stripped, m.wa_media_id, m.wa_media_expires_at, "
                "m.source_inbound_media_id FROM property_media m JOIN property p "
                "ON p.id = m.property_id ORDER BY 1, 2", agency=agency)


def fingerprint(d, agency=None):
    with psycopg.connect(d.app_url, autocommit=True) as conn:
        return ms.fingerprint(conn, agency or d.agency)


def journal_states(d):
    return sorted(e["state"] for e in json.loads(d.journal.read_text())["entries"].values())


def add_staff_media(d, code, order, sha="e" * 64, cover=False):
    with psycopg.connect(d.app_url, autocommit=True) as conn, tenant_transaction(conn, d.agency):
        prop = conn.execute("SELECT id FROM property WHERE listing_code = %s", (code,)).fetchone()[0]
        media_id = conn.execute(
            "INSERT INTO property_media (agency_id, property_id, original_key, sort_order, "
            "content_sha256, label) VALUES (current_agency_id(), %s, %s, %s, %s, 'view') "
            "RETURNING id", (prop, f"tenant/{d.agency}/inbound/{uuid.uuid4()}/original.jpg",
                             order, sha)).fetchone()[0]
        if cover:
            conn.execute("UPDATE property SET cover_media_id = %s WHERE id = %s", (media_id, prop))
    return media_id


# --- dry run and first load -------------------------------------------------------------

def test_dry_run_makes_no_s3_db_or_journal_change(pilot):
    before = (listing_rows(pilot), fingerprint(pilot))
    result = load(pilot, apply=False)
    assert result.outcome == "dry run: nothing written" and not result.conflicts
    assert len(result.plan.inserts) == 12 and len(result.plan.covers) == 3
    assert pilot.bucket.puts == [] and not pilot.journal.exists()
    assert media_rows(pilot) == [] and (listing_rows(pilot), fingerprint(pilot)) == before
    assert q(pilot, "SELECT count(*) FROM property_event")[0][0] == 0


def test_pilot_load_writes_rows_objects_covers_versions_and_events(pilot):
    before, finger = listing_rows(pilot), fingerprint(pilot)
    result = load(pilot)
    assert result.outcome == "committed"
    assert (result.uploaded, result.inserted, result.covers_set) == (12, 12, 3)
    rows = media_rows(pilot)
    assert len(rows) == 12
    scenes = {(s["listing_code"], int(s["sort_order"])): s for s in pilot.media.scenes}
    intake = {r["asset_id"]: r for r in pilot.media.intake}
    after = listing_rows(pilot)
    tokens = set()
    for (code, order, label, media_id, sha, original, wa, card, url, kind, mime, exif,
         wa_id, wa_exp, source) in rows:
        scene = scenes[(code, order)]
        assert label == (scene["label"] or None)
        assert sha == intake[scene["asset_id"]]["prepared_sha256"]
        prefix = f"tenant/{pilot.agency}/property/"
        assert original.startswith(prefix) and original.endswith(f"/{media_id}/original.jpg")
        assert wa.startswith(prefix) and wa.endswith(f"/{media_id}/wa.jpg")
        assert card.startswith(f"card/{pilot.agency}/") and url == f"{PUBLIC}/{card}"
        token = card.rsplit("/", 1)[1][:-4]
        uuid.UUID(token)
        # Random, not derived from the asset, listing or content; distinct per image.
        assert code not in card and sha not in card and token not in tokens
        tokens.add(token)
        assert (kind, mime, exif, wa_id, wa_exp, source) == (
            "image", "image/jpeg", True, None, None, None)
        assert pilot.bucket.objects[card][1:] == ("image/jpeg", ms.media.CARD_CACHE_CONTROL)
        assert pilot.bucket.objects[original][2] is None and pilot.bucket.objects[wa][2] is None
        if order == 0:
            assert after[code][1] == media_id
    assert len(pilot.bucket.puts) == 36
    for code, (version, cover, updated, confirmed) in after.items():
        if code in PILOT:
            assert version == before[code][0] + 1 and updated > before[code][2]
        else:
            assert (version, cover, updated) == before[code][:3]
        assert confirmed == before[code][3]  # freshness never changes
    # Statuses, prices, freshness and every other listing column are untouched.
    assert fingerprint(pilot) == finger
    events = q(pilot, "SELECT p.listing_code, e.event_type, e.from_version, e.to_version, "
                      "e.actor_staff_id, e.changes FROM property_event e JOIN property p "
                      "ON p.id = e.property_id ORDER BY 1")
    assert [e[:5] for e in events] == [(c, "media_added", 1, 2, None) for c in sorted(PILOT)]
    assert all(len(e[5]["media_ids"]) == 4 and e[5]["cover_media_id"] for e in events)
    assert journal_states(pilot) == ["committed"] * 12


def test_exact_rerun_uploads_and_writes_nothing_even_without_the_journal(pilot):
    load(pilot)
    state = (media_rows(pilot), listing_rows(pilot))
    puts = len(pilot.bucket.puts)
    again = load(pilot)
    assert again.outcome == "unchanged: nothing to do" and not again.conflicts
    assert len(again.plan.unchanged) == 12 and again.uploaded == again.inserted == 0
    fresh = load(pilot, journal=pilot.tmp / "other" / "journal.json")
    assert fresh.outcome == "unchanged: nothing to do"
    assert len(pilot.bucket.puts) == puts and (media_rows(pilot), listing_rows(pilot)) == state
    assert q(pilot, "SELECT count(*) FROM property_event")[0][0] == 3


def test_full_load_covers_all_30_listings_including_hidden_ones(demo):
    demo.media = build_media(demo.tmp / "full")
    result = load(demo, listings=None)
    assert result.outcome == "committed" and result.inserted == 120
    counts = dict(q(demo, "SELECT p.listing_code, count(m.id) FROM property p "
                          "LEFT JOIN property_media m ON m.property_id = p.id GROUP BY 1"))
    assert len(counts) == 30 and set(counts.values()) == {4}
    hidden = q(demo, "SELECT listing_code, status, cover_media_id IS NOT NULL FROM property "
                     "WHERE status <> 'active' ORDER BY 1")
    assert hidden == [("CLF-104", "rented", True), ("DH6-105", "sold", True),
                      ("DH6-106", "reserved", True)]
    finger = fingerprint(demo)
    assert finger["listings"] == 30 and finger["public_listings"] == 27
    urls = demo.tmp / "urls.tsv"
    check = ms.run_verify(demo.agency, demo.app_url, S3_ENV, objects=True, storage=demo.bucket,
                          urls_out=urls, media_dir=demo.media.media_dir,
                          assets_dir=demo.media.assets_dir)
    assert check.problems == [] and check.checked_rows == 120 and check.checked_objects == 360
    assert len(urls.read_text().splitlines()) == 120


def test_final_load_requires_all_30_listings(pilot):
    with pytest.raises(ms.MediaSeedError, match="108 scene"):
        load(pilot, listings=None)
    assert pilot.bucket.puts == []


# --- reruns with changes ----------------------------------------------------------------

def test_changed_image_under_an_existing_asset_is_a_conflict(pilot):
    load(pilot)
    state = media_rows(pilot)
    replace_asset(pilot.media, "DH6-101/m02", "changed-v2")
    result = load(pilot)
    assert any("DH6-101/m02: changed_asset" in c for c in result.plan.conflicts)
    assert result.outcome == "conflicts: nothing written"
    # Without the previous-hash record the journal still identifies it.
    row = next(r for r in pilot.media.intake if r["asset_id"] == "DH6-101/m02")
    row["previous_prepared_sha256"] = ""
    write_intake(pilot.media)
    again = load(pilot)
    assert any("DH6-101/m02: changed_asset" in c for c in again.plan.conflicts)
    assert media_rows(pilot) == state and len(pilot.bucket.puts) == 36


def test_staff_reorder_or_relabel_is_an_incompatible_metadata_conflict(pilot):
    load(pilot)
    with psycopg.connect(pilot.app_url, autocommit=True) as conn, \
            tenant_transaction(conn, pilot.agency):
        conn.execute("SET CONSTRAINTS property_media_order_key DEFERRED")
        conn.execute("UPDATE property_media m SET sort_order = 3 - sort_order FROM property p "
                     "WHERE p.id = m.property_id AND p.listing_code = 'PRJ-103' "
                     "AND m.sort_order IN (2, 1)")
        conn.execute("UPDATE property_media m SET label = 'view' FROM property p "
                     "WHERE p.id = m.property_id AND p.listing_code = 'DH6-101' "
                     "AND m.sort_order = 3")
    state = media_rows(pilot)
    result = load(pilot)
    conflicts = result.plan.conflicts
    assert any("PRJ-103/m02: incompatible_metadata" in c and "order 2 (planned 1)" in c
               for c in conflicts)
    assert any("DH6-101/m04: incompatible_metadata" in c and "label view (planned bedroom)" in c
               for c in conflicts)
    assert media_rows(pilot) == state


def test_unrelated_staff_media_is_preserved_or_reported_never_overwritten(pilot):
    kept = add_staff_media(pilot, "DH6-101", 10)
    result = load(pilot)
    assert result.outcome == "committed" and result.plan.preserved == 1
    assert kept in {r[3] for r in media_rows(pilot)}

    other = build_media(pilot.tmp / "second", {"GLI-101"})
    pilot.media = other
    staff_cover = add_staff_media(pilot, "GLI-101", 0, sha="f" * 64, cover=True)
    before = (media_rows(pilot), listing_rows(pilot))
    blocked = load(pilot, listings="GLI-101")
    assert any("GLI-101/m01: slot_occupied" in c for c in blocked.plan.conflicts)
    assert any("GLI-101: cover_taken" in c for c in blocked.plan.conflicts)
    assert (media_rows(pilot), listing_rows(pilot)) == before
    assert listing_rows(pilot)["GLI-101"][1] == staff_cover


def test_same_content_in_another_slot_is_not_adopted(pilot):
    sha = next(r["prepared_sha256"] for r in pilot.media.intake if r["asset_id"] == "DH6-101/m03")
    add_staff_media(pilot, "DH6-101", 9, sha=sha)
    result = load(pilot)
    assert any("DH6-101/m03: incompatible_metadata" in c and "order 9 (planned 2)" in c
               for c in result.plan.conflicts)
    assert pilot.bucket.puts == []


def test_invalid_prepared_file_is_rejected_before_any_upload(pilot):
    row = next(r for r in pilot.media.intake if r["asset_id"] == "BTK-106/m02")
    path = pilot.media.assets_dir / "prepared" / "BTK-106_m02.jpg"
    path.write_bytes(b"\xff\xd8\xff not really a jpeg")
    row["prepared_sha256"] = dm.sha256_hex(path.read_bytes())
    write_intake(pilot.media)
    with pytest.raises(ms.MediaSeedError, match="BTK-106/m02: prepared file rejected"):
        load(pilot)
    assert pilot.bucket.puts == [] and media_rows(pilot) == []


# --- agency, login and tenant isolation ---------------------------------------------------

def test_wrong_agency_owner_login_and_cross_tenant_isolation(pilot):
    with pytest.raises(ms.MediaSeedError, match="agency not found"):
        load(pilot, agency=str(uuid.uuid4()))
    other_name = make_demo_agency(pilot.admin_url, pilot.app_url, name="Another Realty")
    with pytest.raises(ms.MediaSeedError, match="not the demo agency"):
        load(pilot, agency=other_name)
    with pytest.raises(ms.MediaSeedError, match="restricted runtime login"):
        ms.run_load(pilot.agency, pilot.admin_url, S3_ENV, pilot.media.assets_dir,
                    pilot.journal, listings=PILOT_ARG, storage=pilot.bucket,
                    media_dir=pilot.media.media_dir)
    assert pilot.bucket.puts == []

    second = make_demo_agency(pilot.admin_url, pilot.app_url)
    load(pilot)
    assert media_rows(pilot, agency=second) == []
    with pytest.raises(ms.MediaSeedError, match="belongs to another agency"):
        load(pilot, agency=second)
    result = load(pilot, agency=second, journal=pilot.tmp / "second.json")
    assert result.inserted == 12
    first_keys = {r[5] for r in media_rows(pilot)}
    second_keys = {r[5] for r in media_rows(pilot, agency=second)}
    assert all(f"/{second}/" in k for k in second_keys) and not first_keys & second_keys


# --- failures, recovery and concurrency ---------------------------------------------------

def test_upload_failure_then_retry_reuses_journaled_keys(pilot):
    pilot.bucket.fail = 0
    calls = {"n": 0}
    real_put = pilot.bucket.put

    def flaky(key, data, content_type, cache_control=None):
        calls["n"] += 1
        if calls["n"] == 5:
            raise ConnectionError("SECRET-endpoint-detail")
        real_put(key, data, content_type, cache_control)

    pilot.bucket.put = flaky
    with pytest.raises(ms.MediaSeedError) as failed:
        load(pilot)
    assert "SECRET" not in str(failed.value) and "ConnectionError" in str(failed.value)
    assert media_rows(pilot) == [] and journal_states(pilot).count("uploaded") == 1
    planned = {m: e["keys"] for m, e in json.loads(pilot.journal.read_text())["entries"].items()}
    result = load(pilot)
    assert result.outcome == "committed" and result.uploaded == 11
    rows = media_rows(pilot)
    assert {r[3] for r in rows} == {uuid.UUID(m) for m in planned}
    assert set(pilot.bucket.objects) == {k for r in rows for k in r[5:8]}


def test_crash_after_a_partial_derivative_upload_is_cleaned_exactly(pilot):
    stranger = f"tenant/{pilot.agency}/inbound/{uuid.uuid4()}/original.jpg"
    pilot.bucket.put(stranger, b"pre-existing", "image/jpeg")

    def crash(asset, kind):
        if asset.asset_id == "BTK-106/m02" and kind == "wa":
            raise KeyboardInterrupt  # the process dies between two puts

    with pytest.raises(KeyboardInterrupt):
        load(pilot, hooks=ms.Hooks(after_put=crash))
    assert not (pilot.journal.parent / "media.json.lock").exists()  # released on exit
    preview = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, pilot.journal,
                             storage=pilot.bucket)
    assert len(preview.candidates) == 12 and pilot.bucket.deletes == []
    done = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, pilot.journal, apply=True,
                          storage=pilot.bucket)
    assert done.deleted == 12 and len(pilot.bucket.deletes) == 36
    entries = json.loads(pilot.journal.read_text())["entries"].values()
    assert set(pilot.bucket.deletes) == {k for e in entries for k in e["keys"].values()}
    assert set(pilot.bucket.objects) == {stranger}
    assert journal_states(pilot) == ["cleaned"] * 12
    assert load(pilot).outcome == "committed" and len(media_rows(pilot)) == 12
    # A new attempt gets new random IDs and card tokens: deleted public keys are never reused.
    old_cards = {e["keys"]["card"] for e in entries}
    assert not old_cards & {r[7] for r in media_rows(pilot)}


def test_crash_during_the_very_first_upload_leaves_a_journal_for_cleanup(pilot):
    def crash(asset, kind):
        raise KeyboardInterrupt  # dies right after the first object is written

    with pytest.raises(KeyboardInterrupt):
        load(pilot, hooks=ms.Hooks(after_put=crash))
    assert len(pilot.bucket.objects) == 1
    # Every attempt was journaled (durably) before the first put.
    assert journal_states(pilot) == ["planned"] * 12
    done = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, pilot.journal, apply=True,
                          storage=pilot.bucket)
    assert done.deleted == 12 and pilot.bucket.objects == {}


def test_database_change_between_plan_and_locked_recheck_rolls_back(pilot):
    staff = {}

    def staff_upload():
        staff["id"] = add_staff_media(pilot, "DH6-101", 2)

    result = load(pilot, hooks=ms.Hooks(before_db=staff_upload))
    assert result.outcome.startswith("rolled back") and result.conflicts
    assert any("DH6-101/m03: slot_occupied" in c for c in result.plan.conflicts)
    assert [r[3] for r in media_rows(pilot)] == [staff["id"]]
    assert listing_rows(pilot)["DH6-101"][1] is None
    assert journal_states(pilot) == ["uploaded"] * 12
    cleaned = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, pilot.journal, apply=True,
                             storage=pilot.bucket)
    assert cleaned.deleted == 12 and pilot.bucket.objects == {}


@pytest.mark.parametrize("holder", ["advisory", "row", "staff_upload"])
def test_held_locks_give_a_bounded_actionable_error_then_retry_succeeds(pilot, holder):
    blocker = psycopg.connect(pilot.app_url, autocommit=True)
    try:
        if holder == "advisory":
            blocker.execute("SELECT pg_advisory_lock(%s, hashtext(%s))",
                            (ms.LOCK_NAMESPACE, str(pilot.agency)))
        else:
            blocker.execute("BEGIN")
            blocker.execute("SELECT set_config('app.agency_id', %s, true)", (str(pilot.agency),))
            if holder == "row":
                blocker.execute("SELECT id FROM property WHERE listing_code = 'PRJ-103' "
                                "FOR UPDATE")
            else:
                # A staff photo being attached to PRJ-103 in another, unplanned slot,
                # not yet committed: the loader waits for it rather than planning past it.
                blocker.execute(
                    "INSERT INTO property_media (agency_id, property_id, original_key, "
                    "sort_order, content_sha256) SELECT agency_id, id, %s, 9, %s FROM property "
                    "WHERE listing_code = 'PRJ-103'",
                    (f"tenant/{pilot.agency}/inbound/{uuid.uuid4()}/original.jpg", "d" * 64))
        with pytest.raises(ms.MediaSeedError) as held:
            load(pilot, lock_timeout="300ms")
        assert "55P03" in str(held.value) and "retry later" in str(held.value)
        assert media_rows(pilot) == []
        if holder == "advisory":
            assert not pilot.journal.exists() and pilot.bucket.puts == []
        else:
            assert journal_states(pilot) == ["uploaded"] * 12
        if holder == "staff_upload":
            blocker.execute("COMMIT")
    finally:
        blocker.close()
    puts = len(pilot.bucket.puts)
    result = load(pilot)
    assert result.outcome == "committed"
    assert len(pilot.bucket.puts) == (36 if holder == "advisory" else puts)
    assert result.plan.preserved == (1 if holder == "staff_upload" else 0)


def test_concurrent_loaders_never_duplicate_rows(pilot):
    results, errors = [], []

    def run(path):
        try:
            results.append(load(pilot, journal=path))
        except ms.MediaSeedError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(pilot.tmp / f"j{i}.json",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(media_rows(pilot)) == 12 and not errors
    outcomes = sorted(r.outcome.split(":")[0] for r in results)
    assert outcomes in (["committed", "rolled back"], ["committed", "unchanged"])
    # The loser's objects are journaled and removable; the winner's rows stay.
    loser = next((r for r in results if r.outcome.startswith("rolled back")), None)
    if loser is not None:
        path = next(p for p in (pilot.tmp / "j0.json", pilot.tmp / "j1.json")
                    if "uploaded" in p.read_text())
        cleaned = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, path, apply=True,
                                 storage=pilot.bucket)
        assert cleaned.deleted == 12
    assert set(pilot.bucket.objects) == {k for r in media_rows(pilot) for k in r[5:8]}


def test_crash_after_db_statements_before_commit_is_reconciled(pilot):
    def die(conn):
        raise RuntimeError("process killed")

    with pytest.raises(RuntimeError):
        load(pilot, hooks=ms.Hooks(before_commit=die))
    assert journal_states(pilot) == ["committing"] * 12 and media_rows(pilot) == []
    puts = len(pilot.bucket.puts)
    result = load(pilot)
    assert result.outcome == "committed" and len(pilot.bucket.puts) == puts
    assert journal_states(pilot) == ["committed"] * 12


def test_lost_connection_at_commit_is_reported_uncertain_then_reconciled(pilot):
    def kill_own_session(conn):
        with psycopg.connect(pilot.admin_url, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))

    with pytest.raises(ms.MediaSeedError, match="commit outcome unknown"):
        load(pilot, hooks=ms.Hooks(before_commit=kill_own_session))
    assert journal_states(pilot) == ["committing"] * 12
    # Cleanup reconciles first: nothing was committed, so the objects are removable,
    # but rerunning the load is the normal path and reuses them.
    preview = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, pilot.journal,
                             storage=pilot.bucket)
    assert len(preview.candidates) == 12 and pilot.bucket.deletes == []
    puts = len(pilot.bucket.puts)
    assert load(pilot).outcome == "committed" and len(pilot.bucket.puts) == puts


def test_crash_after_commit_before_journal_update_is_reconciled_without_deleting(pilot):
    load(pilot)
    data = json.loads(pilot.journal.read_text())
    for entry in data["entries"].values():
        entry["state"] = "committing"
    pilot.journal.write_text(json.dumps(data))
    preview = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, pilot.journal, apply=True,
                             storage=pilot.bucket)
    assert preview.candidates == [] and pilot.bucket.deletes == []
    assert journal_states(pilot) == ["committed"] * 12
    assert load(pilot).outcome == "unchanged: nothing to do"


def test_missing_or_inconsistent_journal_never_deletes(pilot):
    load(pilot)
    with pytest.raises(ms.MediaSeedError, match="journal not found"):
        ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, pilot.tmp / "none.json",
                       apply=True, storage=pilot.bucket)
    # A row removed by staff after loading is not silently re-added.
    q(pilot, "DELETE FROM property_media m USING property p WHERE p.id = m.property_id "
             "AND p.listing_code = 'PRJ-103' AND m.sort_order = 3 RETURNING m.id")
    result = load(pilot)
    assert any("PRJ-103/m04: removed_after_load" in c for c in result.plan.conflicts)
    # Its objects stay: the attempt is committed, so cleanup never treats it as orphaned.
    cleaned = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, pilot.journal, apply=True,
                             storage=pilot.bucket)
    assert cleaned.candidates == [] and pilot.bucket.deletes == []
    # A journal lock from a crashed run blocks applying runs until removed.
    lock = pilot.journal.parent / "media.json.lock"
    lock.write_text("crashed run")
    with pytest.raises(ms.MediaSeedError, match="locked by another run"):
        load(pilot)


def test_journal_is_bound_to_its_database_and_bucket(pilot):
    pilot.bucket.fail = 1
    with pytest.raises(ms.MediaSeedError, match="upload failed"):
        load(pilot)
    target = json.loads(pilot.journal.read_text())["target"]
    assert target["storage"] == "s3://demo-bucket (aws)" and "SECRET" not in json.dumps(target)
    puts = len(pilot.bucket.puts)
    other_bucket = dict(S3_ENV, S3_BUCKET="other-bucket")
    other_endpoint = dict(S3_ENV, S3_ENDPOINT_URL="http://user:SECRET@minio.local:9000",
                          MEDIA_PUBLIC_BASE_URL=PUBLIC)
    for env in (other_bucket, other_endpoint):
        # Uploaded attempts are never reused, nor their keys deleted, in another bucket.
        with pytest.raises(ms.MediaSeedError, match="another storage target"):
            ms.run_load(pilot.agency, pilot.app_url, env, pilot.media.assets_dir,
                        pilot.journal, listings=PILOT_ARG, apply=True, storage=pilot.bucket,
                        media_dir=pilot.media.media_dir)
        with pytest.raises(ms.MediaSeedError, match="another storage target"):
            ms.run_cleanup(pilot.agency, pilot.app_url, env, pilot.journal, apply=True,
                           storage=pilot.bucket)
    # Another database with the same agency ID (simulated: the agency recreated).
    with psycopg.connect(pilot.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE agency SET created_at = created_at - interval '1 day' "
                      "WHERE id = %s", (pilot.agency,))
    with pytest.raises(ms.MediaSeedError, match="another database target"):
        load(pilot)
    with pytest.raises(ms.MediaSeedError, match="another database target"):
        load(pilot, apply=False)
    assert len(pilot.bucket.puts) == puts and pilot.bucket.deletes == []
    assert media_rows(pilot) == [] and not (pilot.journal.parent / "media.json.lock").exists()


def test_missing_asset_is_added_without_touching_an_existing_cover(pilot):
    load(pilot)
    q(pilot, "DELETE FROM property_media m USING property p WHERE p.id = m.property_id "
             "AND p.listing_code = 'DH6-101' AND m.sort_order = 3 RETURNING m.id")
    before = listing_rows(pilot)["DH6-101"]
    # Without the journal nothing records that the row was loaded and then removed
    # (documented limitation), so the gap is filled; the cover stays as it is.
    result = load(pilot, journal=pilot.tmp / "fresh.json")
    assert result.outcome == "committed" and result.inserted == 1 and result.covers_set == 0
    after = listing_rows(pilot)["DH6-101"]
    assert after[1] == before[1] and after[0] == before[0] + 1 and after[3] == before[3]


def test_cli_output_is_secret_safe(pilot, capsys):
    secret_url = "postgresql://manzil_app:SECRETPW@127.0.0.1:1/manzil_schema_test"
    code = ms.main(["--media-dir", str(pilot.media.media_dir), "load", "--agency-id",
                    str(pilot.agency), "--assets-dir", str(pilot.media.assets_dir),
                    "--journal", str(pilot.journal), "--listings", PILOT_ARG],
                   environ={"DATABASE_URL": secret_url})
    out = capsys.readouterr().out
    assert code == 1 and "SECRETPW" not in out and "could not connect" in out
    code = ms.main(["--media-dir", str(pilot.media.media_dir), "load", "--agency-id",
                    str(pilot.agency), "--assets-dir", str(pilot.media.assets_dir),
                    "--journal", str(pilot.journal), "--listings", PILOT_ARG],
                   environ={"DATABASE_URL": pilot.app_url})
    out = capsys.readouterr().out
    assert code == 0 and "Mode: dry run" in out and "12 to insert" in out
    assert "manzil_app:" not in out and "password" not in out.lower()


def test_verify_reports_missing_media_and_bad_objects(pilot):
    early = ms.run_verify(pilot.agency, pilot.app_url, S3_ENV, listings=PILOT_ARG,
                          media_dir=pilot.media.media_dir)
    assert "12 asset(s) not loaded" in early.problems[0]
    load(pilot)
    card = next(k for k in pilot.bucket.objects if k.startswith("card/"))
    pilot.bucket.objects[card] = (pilot.bucket.objects[card][0][:2000], "image/jpeg", None)
    check = ms.run_verify(pilot.agency, pilot.app_url, S3_ENV, listings=PILOT_ARG,
                          objects=True, storage=pilot.bucket, media_dir=pilot.media.media_dir,
                          assets_dir=pilot.media.assets_dir)
    assert len(check.problems) == 1 and "object unreadable" in check.problems[0]


def _interrupt_before_commit(pilot):
    def stop():
        raise RuntimeError("test interruption")

    with pytest.raises(RuntimeError, match="test interruption"):
        load(pilot, hooks=ms.Hooks(before_db=stop))
    return json.loads(pilot.journal.read_text())["entries"]


@pytest.mark.parametrize("kind", ["original", "wa", "card"])
@pytest.mark.parametrize("damage", ["missing", "substituted"])
def test_uploaded_retry_checks_each_object_before_commit(pilot, kind, damage):
    entries = _interrupt_before_commit(pilot)
    first, second = list(entries.values())[:2]
    key = first["keys"][kind]
    if damage == "missing":
        del pilot.bucket.objects[key]
    else:
        # A valid marked image of the same size/type, but the wrong scene.
        pilot.bucket.objects[key] = pilot.bucket.objects[second["keys"][kind]]
    before = dict(pilot.bucket.objects)
    puts = list(pilot.bucket.puts)
    with pytest.raises(ms.MediaSeedError, match="missing|checksum mismatch"):
        load(pilot)
    assert media_rows(pilot) == [] and pilot.bucket.objects == before
    assert pilot.bucket.puts == puts


@pytest.mark.parametrize("kind", ["original", "wa", "card"])
@pytest.mark.parametrize("damage", ["missing", "substituted"])
def test_verify_requires_exact_stored_derivatives(pilot, kind, damage):
    load(pilot)
    entries = json.loads(pilot.journal.read_text())["entries"]
    first, second = list(entries.values())[:2]
    key = first["keys"][kind]
    if damage == "missing":
        del pilot.bucket.objects[key]
    else:
        pilot.bucket.objects[key] = pilot.bucket.objects[second["keys"][kind]]
    result = ms.run_verify(pilot.agency, pilot.app_url, S3_ENV, listings=PILOT_ARG,
                           objects=True, storage=pilot.bucket, media_dir=pilot.media.media_dir,
                           assets_dir=pilot.media.assets_dir)
    assert any(("missing or unreadable" if damage == "missing" else f"{kind} checksum mismatch")
               in p for p in result.problems)


def test_partial_retry_never_overwrites_incompatible_object(pilot):
    def stop(asset, kind):
        raise RuntimeError("partial upload")

    with pytest.raises(RuntimeError, match="partial upload"):
        load(pilot, hooks=ms.Hooks(after_put=stop))
    key = next(iter(pilot.bucket.objects))
    pilot.bucket.objects[key] = (b"pre-existing incompatible bytes", "image/jpeg", None)
    before = dict(pilot.bucket.objects)
    puts = list(pilot.bucket.puts)
    with pytest.raises(ms.MediaSeedError, match="checksum mismatch"):
        load(pilot)
    assert pilot.bucket.objects == before and pilot.bucket.puts == puts
    assert media_rows(pilot) == []
    result = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, pilot.journal, apply=True,
                            storage=pilot.bucket)
    assert any("unexpected object bytes" in kept for kept in result.kept)
    assert pilot.bucket.objects == before and key not in pilot.bucket.deletes


def test_readback_catches_corruption_after_put(pilot):
    def corrupt():
        key = next(iter(pilot.bucket.objects))
        body, mime, cache = pilot.bucket.objects[key]
        pilot.bucket.objects[key] = (body + b"changed", mime, cache)

    with pytest.raises(ms.MediaSeedError, match="checksum mismatch"):
        load(pilot, hooks=ms.Hooks(before_db=corrupt))
    assert media_rows(pilot) == []


def test_target_change_fails_before_journal_or_storage_mutation(pilot):
    _interrupt_before_commit(pilot)
    before = pilot.journal.read_bytes()
    puts = list(pilot.bucket.puts)
    # Same actual DB/schema/agency timestamp through another connection target:
    # stricter than relying on those restored values to distinguish endpoints.
    other_url = make_conninfo(pilot.app_url, host="localhost")
    for apply in (False, True):
        with pytest.raises(ms.MediaSeedError, match="another database target"):
            ms.run_load(pilot.agency, other_url, S3_ENV, pilot.media.assets_dir,
                        pilot.journal, listings=PILOT_ARG, apply=apply, storage=pilot.bucket,
                        media_dir=pilot.media.media_dir)
        with pytest.raises(ms.MediaSeedError, match="another database target"):
            ms.run_cleanup(pilot.agency, other_url, S3_ENV, pilot.journal,
                           apply=apply, storage=pilot.bucket)
        assert pilot.journal.read_bytes() == before
    assert pilot.bucket.puts == puts and pilot.bucket.deletes == []
    assert media_rows(pilot) == []


@pytest.mark.parametrize("active", ["upload", "delete"])
def test_copied_journal_load_cleanup_race_is_serialized(pilot, active):
    _interrupt_before_commit(pilot)
    copy = pilot.tmp / "copied.json"
    copy.write_bytes(pilot.journal.read_bytes())
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []

    def pause():
        entered.set()
        assert release.wait(10), "test did not release active operation"

    def run():
        try:
            if active == "upload":
                # Pause in the storage phase, before any listing row lock exists.
                original_get = pilot.bucket.get

                def blocking_get(key):
                    pilot.bucket.get = original_get
                    pause()
                    return original_get(key)

                pilot.bucket.get = blocking_get
                results.append(load(pilot))
            else:
                original_delete = pilot.bucket.delete

                def blocking_delete(key):
                    pilot.bucket.delete = original_delete
                    pause()
                    original_delete(key)

                pilot.bucket.delete = blocking_delete
                results.append(ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV,
                                               pilot.journal, apply=True, storage=pilot.bucket))
        except Exception as exc:  # noqa: BLE001 - capture failures in the test thread
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(ms.MediaSeedError, match="55P03"):
            if active == "upload":
                ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, copy, apply=True,
                               storage=pilot.bucket, lock_timeout="300ms")
            else:
                load(pilot, journal=copy, lock_timeout="300ms")
        # Upload coordination does not block ordinary listing edits.
        q(pilot, "SELECT id FROM property WHERE listing_code = 'DH6-101' FOR UPDATE")
    finally:
        release.set()
        thread.join(45)
    assert not thread.is_alive() and not errors and len(results) == 1
    if active == "upload":
        cleaned = ms.run_cleanup(pilot.agency, pilot.app_url, S3_ENV, copy, apply=True,
                                 storage=pilot.bucket)
        assert cleaned.deleted == 0 and pilot.bucket.deletes == []
        assert len(media_rows(pilot)) == 12 and len(pilot.bucket.objects) == 36
    else:
        # Stale uploaded state in the copy cannot commit references to deleted objects.
        with pytest.raises(ms.MediaSeedError, match="object missing"):
            load(pilot, journal=copy)
        assert media_rows(pilot) == [] and pilot.bucket.objects == {}


def test_another_restricted_login_is_not_the_runtime_role(pilot):
    role = "media_test_" + uuid.uuid4().hex
    password = secrets.token_urlsafe(32)
    with psycopg.connect(pilot.admin_url, autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL("CREATE ROLE {} LOGIN INHERIT").format(
            psycopg.sql.Identifier(role)))
        admin.execute(psycopg.sql.SQL("GRANT manzil_app TO {}").format(
            psycopg.sql.Identifier(role)))
        try:
            verifier = admin.pgconn.encrypt_password(password.encode(), role.encode(),
                                                     b"scram-sha-256")
            admin.execute(psycopg.sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                psycopg.sql.Identifier(role), psycopg.sql.Literal(verifier.decode())))
            url = make_conninfo(pilot.app_url, user=role, password=password)
            with psycopg.connect(url, autocommit=True) as restricted:
                assert restricted.execute(seed.LOGIN).fetchone() == (False, False, False)
            with pytest.raises(ms.MediaSeedError, match="runtime login manzil_app"):
                ms._connect(url)
        finally:
            admin.execute(psycopg.sql.SQL("DROP ROLE {}").format(psycopg.sql.Identifier(role)))


# --- operator send check (G-10, fake Meta) -------------------------------------------------

RECIPIENT = "15550000199"  # synthetic 555 number


def test_send_check_previews_by_default_and_sends_once_per_check_id(pilot, capsys):
    load(pilot)
    channel = FakeChannel()
    env = {"MEDIA_CHECK_RECIPIENT": "+1 555 000 0199"}
    with pytest.raises(ms.MediaSeedError, match="no conversation"):
        sc.run(pilot.agency, pilot.app_url, env, "DH6-101", send=True, check_id="pilot-1",
               client_factory=lambda pnid: channel, storage=pilot.bucket)
    q(pilot, "INSERT INTO conversation (agency_id, customer_wa_id, last_customer_message_at) "
             "VALUES (current_agency_id(), %s, now()) RETURNING id", (RECIPIENT,))
    preview = sc.run(pilot.agency, pilot.app_url, env, "DH6-101")
    assert (preview.conversation, preview.window_open, preview.cached_id) == (True, True, False)
    assert channel.sent == [] and channel.uploads == []
    with pytest.raises(ms.MediaSeedError, match="--check-id"):
        sc.run(pilot.agency, pilot.app_url, env, "DH6-101", send=True)

    sent = sc.run(pilot.agency, pilot.app_url, env, "DH6-101", send=True, check_id="pilot-1",
                  client_factory=lambda pnid: channel, storage=pilot.bucket)
    assert (sent.sent_status, sent.sent_via, sent.cached_after) == ("sent", "id", True)
    assert len(channel.uploads) == 1 and channel.sent[0]["image"]["caption"].endswith("(DEMO)")
    again = sc.run(pilot.agency, pilot.app_url, env, "DH6-101", send=True, check_id="pilot-1",
                   client_factory=lambda pnid: channel, storage=pilot.bucket)
    assert again.sent_status == "sent" and len(channel.sent) == 1  # deduplicated
    assert q(pilot, "SELECT media_source, status FROM outbound_message") == [
        ("property_media", "sent")]
    # The CLI never prints the recipient.
    code = sc.main(["--agency-id", str(pilot.agency), "--listing", "DH6-101"],
                   environ=dict(env, DATABASE_URL=pilot.app_url))
    out = capsys.readouterr().out
    assert code == 0 and "Mode: preview" in out and "0199" not in out and RECIPIENT not in out

    # Storage / HTTP failures are reported by class only (their text can quote keys or URLs).
    def broken_get(key):
        raise ConnectionError(f"SECRET detail for {key}")

    pilot.bucket.get = broken_get
    with pytest.raises(ms.MediaSeedError) as failed:
        sc.run(pilot.agency, pilot.app_url, env, "DH6-101", order=1, send=True,
               check_id="pilot-2", client_factory=lambda pnid: channel, storage=pilot.bucket)
    assert "ConnectionError" in str(failed.value) and "SECRET" not in str(failed.value)
    assert "tenant/" not in str(failed.value) and len(channel.sent) == 1
