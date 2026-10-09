"""Task 4.4 offline checks: generation plan, intake rules, watermark, prepare, bundle,
journal and the rerun classification. No database, S3 or Meta. Images here are
synthetic TEST FIXTURES (tests/demo_media_fixtures.py), never demo images."""

import copy
import csv
import hashlib
import io
import json
import uuid
import zipfile

import pytest
from PIL import Image, ImageFont, ImageOps

from app import demo_media as dm
from app import media
from app import media_seed as ms
from tests.demo_media_fixtures import (
    FIXTURE_GENERATOR,
    build_media,
    fixture_source,
    intake_row,
    prepared_fixture,
)

SCENES = dm.read_csv(dm.MEDIA_DIR / dm.SCENES_FILE, dm.SCENE_COLUMNS)
PROPERTIES = dm.load_properties()


def scene(asset_id):
    return next(s for s in SCENES if s["asset_id"] == asset_id)


# --- generation plan ------------------------------------------------------------------

def test_committed_plan_covers_all_30_listings_with_four_distinct_scenes():
    assert dm.validate_scenes(SCENES, PROPERTIES) == []
    by_listing = {}
    for row in SCENES:
        by_listing.setdefault(row["listing_code"], []).append(row)
    assert set(by_listing) == set(PROPERTIES) and len(by_listing) == 30
    assert {len(rows) for rows in by_listing.values()} == {4} and len(SCENES) == 120
    assert "DH6-109" not in by_listing
    for rows in by_listing.values():
        assert [r["sort_order"] for r in rows] == ["0", "1", "2", "3"]
        assert [r["cover"] for r in rows] == ["true", "", "", ""]
    # Hidden listings are included.
    assert {"CLF-104", "DH6-105", "DH6-106"} <= set(by_listing)
    assert sorted({r["listing_code"] for r in SCENES if r["pilot"]}) == sorted(dm.PILOT_LISTINGS)


def test_plan_classes_follow_known_construction_state():
    classes = {r["listing_code"]: r["scene_class"] for r in SCENES}
    for code in ("PRJ-101", "PRJ-102", "PRJ-103", "BTK-106", "DH8-102"):
        assert classes[code] == "concept"
    assert classes["DHC-101"] == "vacant_land"
    assert classes["DH6-101"] == "photo"
    assert not {r["label"] for r in SCENES} & dm.FORBIDDEN_LABELS
    btk106 = [r for r in SCENES if r["listing_code"] == "BTK-106"]
    assert all("allocated" in r["must_not_show"] for r in btk106)
    assert [r["title"].split(": ")[1] for r in btk106] == [
        "elevated open plain", "ground-level rocky basin", "oblique erosion channels",
        "side-on rolling hills"]
    for row in btk106:
        assert "undeveloped" in row["prompt"]
        assert "not an actual location" in row["prompt"]
        assert "DEMO / CONCEPT" in row["prompt"]
        for restriction in ("allocated parcel", "plot boundaries", "approved layout",
                            "identifiable landmark", "infrastructure", "amenities",
                            "lighting-only variation"):
            assert restriction in row["negative_prompt"]
    for row in SCENES:
        if row["scene_class"] == "concept":
            assert "construction progress" in row["must_not_show"]
            assert "construction site" in row["negative_prompt"]
        assert "No people, no text" in row["prompt"]
    # Unknown furnishing on a sale listing: empty interiors (G-7).
    assert "unfurnished" in scene("DH6-101/m02")["prompt"]
    assert "Furnished" in scene("SHF-101/m02")["prompt"]


def test_corrected_scenes_do_not_assert_unsupported_features():
    assert "two-storey" not in scene("DH6-101/m01")["prompt"]
    assert "two-storey" not in scene("DH8-102/m01")["prompt"]
    for code in ("PRJ-101", "PRJ-102"):
        for row in (r for r in SCENES if r["listing_code"] == code):
            assert "balcony doors" not in row["prompt"]
            assert "walk-in-wardrobe doorway" not in row["prompt"]
            assert row["scene_class"] == "concept"
    for row in (r for r in SCENES if r["listing_code"] == "PRJ-103"):
        assert "not a site photograph" in row["prompt"]
        assert "shared corridor" in row["must_not_show"]
        assert "building height" in row["must_not_show"]
    reverse, entrance = scene("PRJ-103/m03"), scene("PRJ-103/m04")
    assert "Reverse interior perspective" in reverse["prompt"] and reverse["label"] == ""
    assert "Oblique architectural view" in entrance["prompt"]
    assert entrance["label"] == "exterior"
    for row in (reverse, entrance):
        assert "empty and unfurnished" in row["prompt"]
        assert "DEMO / CONCEPT" in row["prompt"]
        for restriction in ("material still-life", "abstract lighting artwork", "dimensions",
                            "shared corridors", "plazas", "surrounding development"):
            assert restriction in row["negative_prompt"]


def test_object_lookup_denial_is_not_treated_as_missing():
    from botocore.exceptions import ClientError

    class Store:
        code = "NoSuchKey"

        def get(self, key):
            raise ClientError({"Error": {"Code": self.code}}, "GetObject")

    store = Store()
    assert ms._get_if_present(store, "key") is None
    for code in ("AccessDenied", "NoSuchBucket", "InternalError"):
        store.code = code
        with pytest.raises(ClientError):
            ms._get_if_present(store, "key")


def _mutated(change):
    rows = copy.deepcopy(SCENES)
    change(rows)
    return dm.validate_scenes(rows, PROPERTIES)


def _set(asset_id, **values):
    def change(rows):
        next(r for r in rows if r["asset_id"] == asset_id).update(values)
    return change


@pytest.mark.parametrize(("change", "message"), [
    (lambda rows: rows.__delitem__(slice(0, 4)), "listings without scenes: DH6-101"),
    (_set("DH6-101/m04", label="floor_plan"), "no factual maps or floor plans"),
    (_set("DH6-101/m04", label="map"), "no factual maps or floor plans"),
    (_set("CLF-101/m02", label="bedroom"), "room label bedroom on a shop"),
    (_set("PRJ-103/m01", scene_class="photo"), "PRJ-103 needs concept scenes"),
    (_set("DH8-102/m01", scene_class="photo"), "DH8-102 needs concept scenes (status unknown)"),
    (_set("BTK-106/m01", scene_class="vacant_land"), "BTK-106 needs concept scenes"),
    (_set("DHC-101/m01", scene_class="photo"), "photo scene for a plot"),
    (_set("DH6-101/m01", scene_class="vacant_land"), "vacant_land scene for a house"),
    (_set("DH6-101/m03", cover="true"), "exactly one cover"),
    (_set("DH6-101/m04", sort_order="5"), "sort_order must be 0..3"),
    (_set("DH6-101/m04", prompt=scene("DH6-101/m03")["prompt"]), "duplicate prompts"),
    (_set("DH6-101/m04", listing_code="DH6-109"), "DH6-109 is not in demo/properties.csv"),
    (_set("GLI-101/m01", pilot="true"), "pilot listings must be"),
    (_set("DH6-101/m01", mark_corner="middle"), "mark_corner"),
    (lambda rows: rows.__delitem__(3), "DH6-101: 3 scenes"),
])
def test_plan_rules(change, message):
    errors = _mutated(change)
    assert any(message in e for e in errors), errors


def test_generation_pack_is_in_sync_and_pilot_first():
    text = dm.render_pack(SCENES, PROPERTIES)
    assert dm.PACK_FILE.read_text(encoding="utf-8").replace("\r\n", "\n") == text
    assert text.count("```text") == 120
    assert text.index("### DH6-101") < text.index("### PRJ-103") < text.index("### BTK-106") \
        < text.index("## Remaining listings") < text.index("### DH6-102")


# --- intake ---------------------------------------------------------------------------

def test_committed_intake_is_pilot_only_valid_and_full_load_is_pending():
    intake = dm.read_csv(dm.MEDIA_DIR / dm.INTAKE_FILE, dm.INTAKE_COLUMNS)
    # Only real pilot images (no fixtures); every row, approved or not, is valid, so
    # approved rows carry review fields and no UNCONFIRMED provenance.
    assert all(r["generator"] != FIXTURE_GENERATOR for r in intake)
    assert {r["asset_id"].split("/")[0] for r in intake} <= set(dm.PILOT_LISTINGS)
    assert dm.validate_intake(SCENES, intake, require_approved=False) == []
    approved = {r["asset_id"] for r in intake if r["review_status"] == "approved"}
    errors = dm.validate_intake(SCENES, intake)
    assert len(errors) == 1 and "have no approved asset" in errors[0]
    # A listing is loadable only when all of its scenes are approved.
    for code in dm.PILOT_LISTINGS:
        scope = {s["asset_id"] for s in SCENES if s["listing_code"] == code}
        complete = dm.validate_intake(SCENES, intake, {code}) == []
        assert complete == (scope <= approved), code


def test_unconfirmed_provenance_can_be_prepared_but_never_approved():
    row = _row(generator="UNCONFIRMED - owner to supply", review_status="pending")
    assert dm.validate_intake(SCENES, [row], require_approved=False) == []
    approved = dict(row, review_status="approved", reviewed_by="owner",
                    reviewed_on="2026-10-09")
    errors = dm.validate_intake(SCENES, [approved], require_approved=False)
    assert any("generator still UNCONFIRMED" in e for e in errors)


def _row(base="DH6-101/m01", **values):
    row = intake_row(scene(base), "DH6-101_m01.png", b"source " + base.encode(),
                     hashlib.sha256(base.encode()).hexdigest())
    row.update(values)
    return row


@pytest.mark.parametrize(("values", "message"), [
    ({"asset_id": "XYZ-999/m01"}, "is not in scenes.csv"),
    ({"source_file": "../outside.png"}, "relative path of plain names"),
    ({"source_file": "/etc/passwd"}, "relative path of plain names"),
    ({"source_file": "C:\\x.png"}, "relative path of plain names"),
    ({"source_sha256": "abc"}, "source_sha256 must be 64"),
    ({"generator": ""}, "generator is required"),
    ({"generator_model": " "}, "generator_model is required"),
    ({"generation_prompt": ""}, "generation_prompt is required"),
    ({"licence_reference": ""}, "licence_reference is required"),
    ({"generated_on": "2099-01-01"}, "not in the future"),
    ({"generated_on": "9 Oct"}, "ISO date"),
    ({"review_status": "ok"}, "review_status must be one of"),
    ({"prepared_sha256": ""}, "approved asset needs prepared_sha256"),
    ({"reviewed_by": ""}, "needs reviewed_by and reviewed_on"),
    ({"previous_prepared_sha256": hashlib.sha256(b"DH6-101/m01").hexdigest()},
     "other than the current one"),
])
def test_intake_rules(values, message):
    errors = dm.validate_intake(SCENES, [_row(**values)], {"DH6-101"},
                                require_approved=False)
    assert any(message in e for e in errors), errors


def test_duplicate_content_is_rejected_within_a_listing_and_across_listings():
    first, second = _row("DH6-101/m01"), _row("DH6-101/m02")
    second["prepared_sha256"] = first["prepared_sha256"]
    errors = dm.validate_intake(SCENES, [first, second], {"DH6-101"}, require_approved=False)
    assert any("same prepared image as DH6-101/m01" in e for e in errors)
    other = _row("PRJ-103/m01")
    other["prepared_sha256"] = first["prepared_sha256"]
    errors = dm.validate_intake(SCENES, [first, other], None, require_approved=False)
    assert any("duplicates do not count" in e for e in errors)
    clash = _row("DH6-101/m02", source_sha256=first["source_sha256"])
    errors = dm.validate_intake(SCENES, [first, clash], None, require_approved=False)
    assert any("same source file as DH6-101/m01" in e for e in errors)


def test_pilot_scope_still_needs_full_set_for_final_validation(tmp_path):
    ns = build_media(tmp_path, set(dm.PILOT_LISTINGS))
    scenes, intake = dm.load_plan(ns.media_dir)
    assert dm.validate_intake(scenes, intake, set(dm.PILOT_LISTINGS)) == []
    full = dm.validate_intake(scenes, intake)
    assert full and "108 scene(s) in scope have no approved asset" in full[0]
    assert dm.main(["--media-dir", str(ns.media_dir), "validate"]) == 1
    assert dm.main(["--media-dir", str(ns.media_dir), "validate", "--listings",
                    "DH6-101,PRJ-103,BTK-106", "--assets-dir", str(ns.assets_dir)]) == 0


def test_prepared_files_are_checked_by_hash_and_never_by_input_path(tmp_path):
    ns = build_media(tmp_path, {"DH6-101"})
    scenes, intake = dm.load_plan(ns.media_dir)
    assets = dm.approved_assets(scenes, intake, {"DH6-101"})
    assert dm.check_files(assets, ns.assets_dir) == []
    target = ns.assets_dir / "prepared" / assets[1].file_name
    target.write_bytes(target.read_bytes() + b"x")
    assert dm.check_files(assets, ns.assets_dir) == [
        "DH6-101/m02: prepared file does not match prepared_sha256 (changed or wrong file)"]
    target.unlink()
    assert "prepared file missing" in dm.check_files(assets, ns.assets_dir)[0]
    with pytest.raises(dm.MediaPlanError):
        dm.prepared_name("DH6-101/../../etc")
    with pytest.raises(dm.MediaPlanError):
        dm.safe_source(ns.sources_dir, "../media/intake.csv")


# --- crop-safe region and mark ----------------------------------------------------------

@pytest.mark.parametrize("size", [(1920, 1080), (2560, 1700), (1950, 1000), (1600, 1067),
                                  (2400, 1257)])
def test_crop_box_matches_imageops_fit(size):
    # Same pixels as the card process_image makes: the geometry is exactly fit's.
    image = Image.open(io.BytesIO(fixture_source(f"fit{size}", size))).convert("RGB")
    card = ImageOps.fit(image, media.CARD_SIZE, Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    manual = image.resize(media.CARD_SIZE, Image.Resampling.LANCZOS, box=dm.crop_box(*size))
    assert card.tobytes() == manual.tobytes()
    left, top, right, bottom = dm.crop_box(*size)
    assert (right - left) / (bottom - top) == pytest.approx(1200 / 628, rel=1e-6)
    assert (left == 0 and right == size[0]) or (top == 0 and bottom == size[1])


@pytest.mark.parametrize("corner", sorted(dm.CORNERS))
@pytest.mark.parametrize("size", [(1920, 1080), (2560, 1700), (1950, 1000)])
def test_mark_sits_inside_the_crop_safe_region_not_the_source_corner(size, corner):
    box, _, _ = dm.mark_layout(*size, "DEMO / CONCEPT", corner)
    left, top, right, bottom = dm.crop_box(*size)
    inset = (bottom - top) * dm.MARK_INSET
    assert box[0] >= left + inset and box[2] <= right - inset
    assert box[1] >= top + inset and box[3] <= bottom - inset
    assert box[3] - box[1] == round((bottom - top) * dm.MARK_HEIGHT)


@pytest.mark.parametrize(("size", "corner", "mark"), [
    ((1920, 1080), "br", "DEMO"), ((2560, 1700), "tl", "DEMO / CONCEPT"),
    ((1950, 1000), "tr", "DEMO / CONCEPT"), ((3000, 1688), "bl", "DEMO"),
])
def test_prepare_marks_every_derivative(size, corner, mark):
    result = dm.prepare_image(fixture_source(f"mark{size}{corner}", size), mark, corner)
    assert max(result.width, result.height) <= media.ORIGINAL_MAX_EDGE
    assert dm.check_marks(result.width, result.height, result.box, result.processed) == []
    with Image.open(io.BytesIO(result.processed.card)) as card:
        assert card.size == media.CARD_SIZE
    with Image.open(io.BytesIO(result.processed.wa)) as wa:
        assert max(wa.size) <= media.WA_MAX_EDGE
    # The unmarked image fails the same check: the detector is not trivially true.
    plain = media.process_image(fixture_source(f"mark{size}{corner}", size))
    with Image.open(io.BytesIO(plain.original)) as original:
        width, height = original.size
    box, _, _ = dm.mark_layout(width, height, mark, corner)
    assert dm.check_marks(width, height, box, plain) == ["original", "wa", "card"]


def test_source_corner_mark_would_be_lost_in_the_card():
    # Proof of the requirement: a mark in the 3:2 source corner leaves the card.
    width, height = 2400, 1600
    _, top, _, bottom = dm.crop_box(width, height)
    corner_box = (width - 300, height - 120, width - 20, height - 20)
    assert corner_box[1] > bottom  # entirely below the kept region
    box, _, _ = dm.mark_layout(width, height, "DEMO", "br")
    assert box[3] < bottom and box[1] > top


def test_prepare_strips_metadata_and_is_deterministic():
    image = Image.open(io.BytesIO(fixture_source("exif", (1920, 1080), "JPEG")))
    exif = Image.Exif()
    exif[0x010F] = "Fixture camera"
    exif[0x8825] = {1: "N", 2: (24.0, 51.0, 0.0)}  # GPS
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif.tobytes(), xmp=b"<x:xmpmeta>fixture</x:xmpmeta>")
    source = out.getvalue()
    assert media.has_metadata(source)
    first = dm.prepare_image(source, "DEMO", "br")
    second = dm.prepare_image(source, "DEMO", "br")
    assert first.sha256 == second.sha256 and first.data == second.data
    for blob in (first.data, first.processed.original, first.processed.wa,
                 first.processed.card):
        assert not media.has_metadata(blob)


@pytest.mark.parametrize(("size", "error"), [
    ((1080, 1920), "landscape"), ((1600, 1600), "landscape"),
    ((1200, 700), ">= 1280 px wide"), ((2200, 1000), "aspect"),
])
def test_prepare_rejects_unsuitable_shapes(size, error):
    with pytest.raises(dm.MediaPlanError, match=error):
        dm.prepare_image(fixture_source(f"shape{size}", size), "DEMO", "br")


def test_prepare_keeps_the_existing_validation():
    with pytest.raises(media.MediaRejected) as rejected:
        dm.prepare_image(b"%PDF-1.4 not an image", "DEMO", "br")
    assert rejected.value.code == "unsupported_type"
    with pytest.raises(media.MediaRejected):
        dm.prepare_image(b"\xff\xd8\xff" + b"\x00" * 64, "DEMO", "br")


def test_mark_needs_the_reproducible_bundled_font(monkeypatch):
    monkeypatch.setattr(ImageFont, "load_default", lambda size=None: ImageFont.load_default_imagefont())
    with pytest.raises(dm.MediaPlanError, match="bundled scalable font"):
        dm.mark_layout(1920, 1080, "DEMO", "br")


# --- prepare and bundle CLIs --------------------------------------------------------------

@pytest.mark.parametrize("local_workspace", [False, True])
def test_prepare_cli_records_hashes_and_writes_review_sheets(tmp_path, monkeypatch, local_workspace):
    workspace = tmp_path
    if local_workspace:
        monkeypatch.setattr(dm, "REPO_ROOT", tmp_path)
        workspace = tmp_path / ".local" / "demo-media"
    ns = build_media(workspace / "w", {"DH6-101"}, status="pending")
    originals = {p.name: p.read_bytes() for p in ns.sources_dir.iterdir()}
    for row in ns.intake:
        row["prepared_sha256"] = ""
    from tests.demo_media_fixtures import write_intake
    write_intake(ns)
    out = workspace / "out"
    assert dm.main(["--media-dir", str(ns.media_dir), "prepare", "--sources-dir",
                    str(ns.sources_dir), "--out-dir", str(out), "--record"]) == 0
    recorded = dm.read_csv(ns.media_dir / dm.INTAKE_FILE, dm.INTAKE_COLUMNS)
    assert {r["review_status"] for r in recorded} == {"pending"}
    assert {p.name: p.read_bytes() for p in ns.sources_dir.iterdir()} == originals
    expected = {s["asset_id"]: prepared_fixture(s["asset_id"], s)[1].sha256
                for s in SCENES if s["listing_code"] == "DH6-101"}
    assert {r["asset_id"]: r["prepared_sha256"] for r in recorded} == expected
    assert sorted(p.name for p in (out / "prepared").iterdir()) == [
        "DH6-101_m01.jpg", "DH6-101_m02.jpg", "DH6-101_m03.jpg", "DH6-101_m04.jpg"]
    assert len(list((out / "review" / "DH6-101").iterdir())) == 12
    with Image.open(out / "review" / "DH6-101_sheet.jpg") as sheet:
        assert sheet.width > 1900 and sheet.height > 4 * 300


@pytest.mark.parametrize("relative", [".", "app", "demo/media", ".local/other",
                                      ".local/demo-media-other", ".local/demo-media/../other"])
def test_local_media_exception_does_not_allow_other_repo_folders(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(dm, "REPO_ROOT", tmp_path)
    assert not dm.allowed_image_folder(tmp_path / relative)
    assert dm.allowed_image_folder(tmp_path / ".local/demo-media/sources")
    assert dm.allowed_image_folder(tmp_path / ".local/demo-media/work")


def test_prepare_cli_refuses_repo_folders_changed_sources_and_traversal(tmp_path, capsys):
    ns = build_media(tmp_path / "w", {"DH6-101"})
    args = ["--media-dir", str(ns.media_dir), "prepare", "--sources-dir", str(ns.sources_dir)]
    assert dm.main(args + ["--out-dir", str(dm.REPO_ROOT / "build-out")]) == 1
    assert "outside the repository" in capsys.readouterr().out
    (ns.sources_dir / "DH6-101_m02.png").write_bytes(b"tampered")
    ns.intake[2]["source_file"] = "../media/intake.csv"
    from tests.demo_media_fixtures import write_intake
    write_intake(ns)
    assert dm.main(args + ["--out-dir", str(tmp_path / "out")]) == 1
    assert "relative path of plain names" in capsys.readouterr().out
    ns.intake[2]["source_file"] = "DH6-101_m03.png"
    write_intake(ns)
    assert dm.main(args + ["--out-dir", str(tmp_path / "out")]) == 1
    output = capsys.readouterr().out
    assert "DH6-101/m02: source file does not match source_sha256" in output


def test_bundle_contains_checksums_and_only_approved_files(tmp_path, capsys):
    ns = build_media(tmp_path / "w", set(dm.PILOT_LISTINGS))
    zip_path = tmp_path / "pilot.zip"
    assert dm.main(["--media-dir", str(ns.media_dir), "bundle", "--assets-dir",
                    str(ns.assets_dir), "--out", str(zip_path), "--listings",
                    "DH6-101,PRJ-103,BTK-106"]) == 0
    data = zip_path.read_bytes()
    assert hashlib.sha256(data).hexdigest() in capsys.readouterr().out
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        sums = dict(line.split("  ")[::-1] for line in
                    archive.read("SHA256SUMS").decode().splitlines())
        for name, digest in sums.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    assert len([n for n in names if n.startswith("prepared/")]) == 12
    assert "manifest/intake.csv" in names and "manifest/scenes.csv" in names
    # The full set is required without --listings.
    assert dm.main(["--media-dir", str(ns.media_dir), "bundle", "--assets-dir",
                    str(ns.assets_dir), "--out", str(tmp_path / "all.zip")]) == 1


# --- journal and classification (no database) ------------------------------------------

AGENCY = uuid.UUID("11111111-2222-4333-8444-555555555555")


def test_journal_is_durable_validated_and_locked(tmp_path):
    path = tmp_path / "journal.json"
    journal = ms.Journal.open(path, AGENCY, lock=True)
    asset = dm.Asset("DH6-101/m01", "DH6-101", 0, "exterior", "photo", "br", "a" * 64, ())
    media_id = journal.new_attempt(asset, uuid.uuid4())
    with pytest.raises(ms.MediaSeedError, match="target is not bound"):
        journal.save()
    journal.bind(database="db-a", storage="s3://bucket-a (aws)")
    journal.save()
    with pytest.raises(ms.MediaSeedError, match="locked by another run"):
        ms.Journal.open(path, AGENCY, lock=True)
    journal.close()
    entry = ms.Journal.open(path, AGENCY, lock=False).entries[media_id]
    assert entry["state"] == "planned" and entry["content_sha256"] == "a" * 64
    # The card token is random: not derived from the asset, listing or content.
    assert entry["card_token"] not in json.dumps(asset.__dict__)
    assert entry["keys"]["card"] == f"card/{AGENCY}/{entry['card_token']}.jpg"
    with pytest.raises(ms.MediaSeedError, match="belongs to another agency"):
        ms.Journal.open(path, uuid.uuid4(), lock=False)
    # Bound to its database and bucket: another target is refused, an unknown one
    # (None, e.g. a dry run without S3 settings) is not checked.
    reopened = ms.Journal.open(path, AGENCY, lock=False)
    reopened.bind(database="db-a", storage=None)
    for other in ({"database": "db-b"}, {"storage": "s3://bucket-b (aws)"}):
        with pytest.raises(ms.MediaSeedError, match="another .* target"):
            ms.Journal.open(path, AGENCY, lock=False).bind(**other)
    data = json.loads(path.read_text())
    del data["target"]
    path.write_text(json.dumps(data))
    with pytest.raises(ms.MediaSeedError, match="does not record its database"):
        ms.Journal.open(path, AGENCY, lock=False)
    data["target"] = {"database": "db-a", "storage": "s3://bucket-a (aws)"}
    data["entries"][media_id]["keys"]["original"] = "tenant/other/secret.jpg"
    path.write_text(json.dumps(data))
    with pytest.raises(ms.MediaSeedError, match="inconsistent entry"):
        ms.Journal.open(path, AGENCY, lock=False)
    path.write_text("{not json")
    with pytest.raises(ms.MediaSeedError, match="unreadable"):
        ms.Journal.open(path, AGENCY, lock=False)
    assert not (tmp_path / "journal.json.lock").exists()


def _asset(n, sha, label="exterior", previous=()):
    return dm.Asset(f"DH6-101/m{n + 1:02d}", "DH6-101", n, label, "photo", "br", sha, previous)


def _row_for(asset, row_id=None, **values):
    row = {"id": row_id or uuid.uuid4(), "property_id": None, "sort_order": asset.sort_order,
           "label": asset.label, "kind": "image", "content_sha256": asset.prepared_sha256,
           "original_key": "k1", "wa_image_key": "k2", "card_image_key": "k3",
           "public_url_card": "https://x/card/k.jpg", "exif_stripped": True,
           "wa_media_id": None, "wa_media_expires_at": None}
    row.update(values)
    return row


def _state(rows, cover=None):
    return {"DH6-101": ms.ListingState("DH6-101", uuid.uuid4(), cover, 1, True, rows)}


def test_classification_cases():
    a0, a1 = _asset(0, "0" * 64), _asset(1, "1" * 64, "lounge")
    fresh = ms.classify([a0, a1], _state([]), {})
    assert [a.asset_id for a in fresh.inserts] == ["DH6-101/m01", "DH6-101/m02"]
    assert fresh.covers == {"DH6-101": "DH6-101/m01"} and not fresh.conflicts

    r0, r1 = _row_for(a0), _row_for(a1)
    exact = ms.classify([a0, a1], _state([r0, r1], cover=r0["id"]), {})
    assert exact.unchanged == {"DH6-101/m01": r0["id"], "DH6-101/m02": r1["id"]}
    assert not exact.inserts and not exact.covers and not exact.conflicts

    staff = _row_for(_asset(7, "7" * 64), sort_order=7)
    preserved = ms.classify([a0, a1], _state([r0, r1, staff], cover=r0["id"]), {})
    assert preserved.preserved == 1 and not preserved.conflicts

    cases = {
        "incompatible_metadata": _state([_row_for(a0, sort_order=5), r1]),
        "slot_occupied": _state([_row_for(_asset(0, "9" * 64)), r1]),
        "cover_taken": _state([r0, r1], cover=uuid.uuid4()),
    }
    for kind, state in cases.items():
        plan = ms.classify([a0, a1], state, {})
        assert any(kind in c for c in plan.conflicts), (kind, plan.conflicts)
    # A loaded cover that staff cleared is not reset (and no cover-only change).
    cleared = ms.classify([a0, a1], _state([r0, r1], cover=None), {})
    assert any("cover_cleared" in c for c in cleared.conflicts) and not cleared.covers
    relabelled = ms.classify([a0, a1], _state([r0, _row_for(a1, label="kitchen")], r0["id"]), {})
    assert "label kitchen (planned lounge)" in relabelled.conflicts[0]

    changed = _asset(1, "2" * 64, "lounge", previous=("1" * 64,))
    plan = ms.classify([a0, changed], _state([r0, r1], cover=r0["id"]), {})
    assert any("changed_asset" in c for c in plan.conflicts) and not plan.inserts

    journal = {str(r1["id"]): {"asset_id": "DH6-101/m02", "state": "committed",
                               "content_sha256": "1" * 64}}
    plan = ms.classify([a0, _asset(1, "2" * 64, "lounge")], _state([r0, r1], r0["id"]), journal)
    assert any("changed_asset" in c for c in plan.conflicts)
    plan = ms.classify([a0, a1], _state([r0], r0["id"]), journal)
    assert any("removed_after_load" in c for c in plan.conflicts)


def test_object_keys_keep_originals_private_and_cards_public():
    keys = ms.object_keys(AGENCY, "p", "m", "t")
    assert keys == {"original": f"tenant/{AGENCY}/property/p/m/original.jpg",
                    "wa": f"tenant/{AGENCY}/property/p/m/wa.jpg",
                    "card": f"card/{AGENCY}/t.jpg"}


def test_fixture_intake_is_labelled_as_fixture(tmp_path):
    ns = build_media(tmp_path, {"PRJ-103"})
    with open(ns.media_dir / dm.INTAKE_FILE, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {r["generator"] for r in rows} == {FIXTURE_GENERATOR}
