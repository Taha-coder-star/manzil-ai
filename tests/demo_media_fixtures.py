"""Synthetic TEST FIXTURE images and manifests for the Task 4.4 tests.

These are generated shapes labelled "TEST FIXTURE", never demo images: they live
only in pytest temporary folders and their intake rows say so.
"""

import csv
import datetime
import hashlib
import io
import random
import shutil
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from app import demo_media as dm

FIXTURE_GENERATOR = "TEST FIXTURE (synthetic shapes; not a demo image)"
PILOT = set(dm.PILOT_LISTINGS)
_prepared = {}


def fixture_source(key, size=(1280, 720), fmt="PNG", **save):
    """Busy synthetic shapes, distinct per key, with a TEST FIXTURE caption."""
    rnd = random.Random(hashlib.sha256(key.encode()).digest())
    image = Image.new("RGB", size, tuple(rnd.randrange(80, 220) for _ in range(3)))
    draw = ImageDraw.Draw(image)
    for _ in range(120):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        draw.rectangle((x, y, x + rnd.randrange(10, 260), y + rnd.randrange(10, 160)),
                       fill=tuple(rnd.randrange(256) for _ in range(3)))
    draw.text((24, 24), f"TEST FIXTURE {key}", fill=(0, 0, 0))
    out = io.BytesIO()
    image.save(out, fmt, **save)
    return out.getvalue()


def prepared_fixture(key, scene):
    """dm.prepare_image of the fixture for key (cached across tests)."""
    cache_key = (key, scene["scene_class"], scene["mark_corner"])
    if cache_key not in _prepared:
        source = fixture_source(key)
        _prepared[cache_key] = (source, dm.prepare_image(
            source, dm.MARKS[scene["scene_class"]], scene["mark_corner"]))
    return _prepared[cache_key]


def intake_row(scene, source_name, source, prepared_sha, status="approved"):
    today = datetime.date(2026, 10, 9).isoformat()
    return {
        "asset_id": scene["asset_id"], "source_file": source_name,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "generator": FIXTURE_GENERATOR, "generator_model": "pytest fixture",
        "generated_on": today, "generation_prompt": "synthetic test fixture",
        "licence_reference": "test fixture; not for delivery", "prepared_sha256": prepared_sha,
        "previous_prepared_sha256": "", "review_status": status,
        "reviewed_by": "pytest" if status == "approved" else "",
        "reviewed_on": today if status == "approved" else "", "review_notes": "fixture",
    }


def build_media(root, listings=None, status="approved"):
    """A media dir (real scenes.csv + fixture intake), prepared files and sources."""
    root = Path(root)
    media_dir, assets_dir, sources_dir = root / "media", root / "assets", root / "sources"
    for folder in (media_dir, assets_dir / "prepared", sources_dir):
        folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(dm.MEDIA_DIR / dm.SCENES_FILE, media_dir / dm.SCENES_FILE)
    scenes = dm.read_csv(media_dir / dm.SCENES_FILE, dm.SCENE_COLUMNS)
    rows = []
    for scene in scenes:
        if listings is not None and scene["listing_code"] not in listings:
            continue
        source, prepared = prepared_fixture(scene["asset_id"], scene)
        name = dm.prepared_name(scene["asset_id"])
        source_name = name.replace(".jpg", ".png")
        (sources_dir / source_name).write_bytes(source)
        (assets_dir / "prepared" / name).write_bytes(prepared.data)
        rows.append(intake_row(scene, source_name, source, prepared.sha256, status))
    ns = SimpleNamespace(root=root, media_dir=media_dir, assets_dir=assets_dir,
                         sources_dir=sources_dir, scenes=scenes, intake=rows)
    write_intake(ns)
    return ns


def write_intake(ns):
    with open(ns.media_dir / dm.INTAKE_FILE, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=dm.INTAKE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(ns.intake)


def replace_asset(ns, asset_id, key, *, keep_previous=True):
    """Give an asset new prepared bytes (a changed image) and record it in the intake."""
    scene = next(s for s in ns.scenes if s["asset_id"] == asset_id)
    row = next(r for r in ns.intake if r["asset_id"] == asset_id)
    source, prepared = prepared_fixture(key, scene)
    (ns.assets_dir / "prepared" / dm.prepared_name(asset_id)).write_bytes(prepared.data)
    if keep_previous:
        row["previous_prepared_sha256"] = row["prepared_sha256"]
    row["prepared_sha256"] = prepared.sha256
    row["source_sha256"] = hashlib.sha256(source).hexdigest()
    write_intake(ns)
    return prepared
