"""Task 4.4 demo listing images, offline part: plan, intake, watermark, review, bundle.

    python -m app.demo_media validate [--plan-only] [--listings A,B] [--assets-dir DIR]
    python -m app.demo_media pack [--check]
    python -m app.demo_media prepare --sources-dir DIR --out-dir DIR [--listings A,B] [--record]
    python -m app.demo_media bundle --assets-dir DIR --out FILE.zip [--listings A,B]

Files (docs/DEMO_MEDIA.md):
- demo/media/scenes.csv: the generation plan, four scenes per listing (asset ID,
  order, cover, label, scene class, prompt, fact basis, what must not be shown);
- demo/media/intake.csv: one row per generated image (source file and checksum,
  generator / model / date / prompt, licence reference, prepared checksum,
  review status). Empty until images are generated; never filled with fixtures.

prepare turns a generator file into the "prepared" image the loader stores: it is
validated by app.media.process_image, oriented, scaled to <= 2,560 px, marked
DEMO (or DEMO / CONCEPT) inside the region the centred 1.91:1 card crop keeps,
re-encoded without metadata, and run through process_image again; the mark is
then checked in the original, WhatsApp and card derivatives, and labelled
review sheets are written. Image folders live outside the repository or in the
excluded .local/demo-media workspace; ZIP bundles stay outside the repository.

Nothing here touches a database, S3 or Meta.
"""

import argparse
import csv
import datetime
import hashlib
import io
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import PIL
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app import demo_dataset as dd
from app import media

REPO_ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = dd.DEMO_DIR / "media"
SCENES_FILE = "scenes.csv"
INTAKE_FILE = "intake.csv"
PACK_FILE = REPO_ROOT / "docs" / "DEMO_MEDIA_PROMPTS.md"

SCENE_COLUMNS = (
    "asset_id", "listing_code", "sort_order", "cover", "label", "scene_class",
    "mark_corner", "pilot", "title", "prompt", "negative_prompt", "fact_basis",
    "must_not_show",
)
INTAKE_COLUMNS = (
    "asset_id", "source_file", "source_sha256", "generator", "generator_model",
    "generated_on", "generation_prompt", "licence_reference", "prepared_sha256",
    "previous_prepared_sha256", "review_status", "reviewed_by", "reviewed_on",
    "review_notes",
)

ASSET_ID = re.compile(r"^(?P<code>[A-Z0-9]{3}-[0-9]{3})/m(?P<n>[0-9]{2})$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# A relative POSIX path of plain names: no drive, no "..", no backslash.
SOURCE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")

# property_media.label (008) minus the two this task must not invent.
LABELS = {"exterior", "lounge", "kitchen", "bedroom", "bath", "view"}
FORBIDDEN_LABELS = {"map", "floor_plan"}
ROOM_LABELS = {"lounge", "kitchen", "bedroom", "bath"}
ROOM_TYPES = {"house", "apartment", "penthouse", "portion"}
SCENE_CLASSES = {"photo", "concept", "vacant_land"}
MARKS = {"photo": "DEMO", "vacant_land": "DEMO", "concept": "DEMO / CONCEPT"}
CORNERS = {"br", "bl", "tr", "tl"}
REVIEW_STATUSES = {"pending", "approved", "rejected"}
# Provenance not yet supplied by the owner is written with this marker; it allows
# prepare / review but never approval.
UNCONFIRMED = "UNCONFIRMED"
PROVENANCE_COLUMNS = ("generator", "generator_model", "generation_prompt", "licence_reference")
PILOT_LISTINGS = ("DH6-101", "PRJ-103", "BTK-106")
MIN_PER_LISTING, MAX_PER_LISTING = 4, 6
SPECULATIVE_CONSTRUCTION = {"under_construction", "off_plan", "grey_structure"}

# Source images: landscape, close to the card ratio, wide enough for a 1,200 px card.
MIN_ASPECT, MAX_ASPECT = 1.5, 1.95
MIN_WIDTH = 1280
MAX_SOURCE_BYTES = 40 * 1024 * 1024
PREPARED_QUALITY = 95

# The visible mark (watermark v1). Reproducible: Pillow's bundled scalable font,
# fixed geometry relative to the crop-safe region, fixed colours.
FONT_NAME = ("Aileron", "Regular")
MARK_HEIGHT = 0.09        # pill height / crop-safe height
MARK_INSET = 0.035        # gap to the crop-safe edges / crop-safe height
MARK_MIN_HEIGHT = 24
ROUNDING_PX = 2           # slack for the card resampling
PILL_RGBA = (0, 0, 0, 190)
TEXT_RGBA = (255, 255, 255, 255)
WATERMARK_VERSION = "v1"


class MediaPlanError(Exception):
    """An error whose message is safe to print."""


@dataclass(frozen=True)
class Asset:
    asset_id: str
    listing_code: str
    sort_order: int
    label: str | None
    scene_class: str
    corner: str
    prepared_sha256: str
    previous: tuple

    @property
    def mark(self):
        return MARKS[self.scene_class]

    @property
    def file_name(self):
        return prepared_name(self.asset_id)


def prepared_name(asset_id):
    """The prepared file name; derived from the validated ID, never from input paths."""
    if not ASSET_ID.match(asset_id):
        raise MediaPlanError(f"invalid asset ID {asset_id!r}")
    return asset_id.replace("/", "_") + ".jpg"


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def watermark_config():
    return (f"watermark {WATERMARK_VERSION}: Pillow {PIL.__version__}, bundled font "
            f"{' '.join(FONT_NAME)}, pill {MARK_HEIGHT:.1%} of the crop-safe height, inset "
            f"{MARK_INSET:.1%}, JPEG q{PREPARED_QUALITY}, no metadata")


# --- manifest files --------------------------------------------------------------

def read_csv(path, columns):
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != columns:
                raise MediaPlanError(f"{Path(path).name}: unexpected columns")
            return list(reader)
    except OSError as exc:
        raise MediaPlanError(f"{Path(path).name} could not be read ({type(exc).__name__})") \
            from None


def write_intake(rows, media_dir=MEDIA_DIR):
    path = Path(media_dir) / INTAKE_FILE
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INTAKE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_plan(media_dir=MEDIA_DIR):
    media_dir = Path(media_dir)
    return (read_csv(media_dir / SCENES_FILE, SCENE_COLUMNS),
            read_csv(media_dir / INTAKE_FILE, INTAKE_COLUMNS))


def load_properties(demo_dir=dd.DEMO_DIR):
    try:
        rows = dd.load(demo_dir)["properties.csv"]
    except (OSError, ValueError, dd.DatasetError) as exc:
        raise MediaPlanError(f"demo dataset could not be read ({type(exc).__name__})") from None
    return {row["listing_code"]: row for row in rows}


def parse_listings(value, scenes):
    """None (all listings) or the set of listing codes named in a comma list."""
    if not value:
        return None
    codes = {code.strip() for code in value.split(",") if code.strip()}
    known = {row["listing_code"] for row in scenes}
    unknown = sorted(codes - known)
    if unknown:
        raise MediaPlanError(f"unknown listing codes: {', '.join(unknown)}")
    return codes


def needs_concept(prop):
    """True when only an explicitly illustrative concept may be shown."""
    if prop["possession_status"] == "file":
        return True
    if prop["construction_status"] in SPECULATIVE_CONSTRUCTION:
        return True
    # A building type with no recorded build status stays unknown (DH8-102).
    return prop["property_type"] != "plot" and not prop["construction_status"]


def validate_scenes(scenes, properties):
    """The generation plan: 30 listings, 4-6 scenes each, labels fit the facts."""
    errors = []
    by_listing = {}
    seen = set()
    for number, row in enumerate(scenes, start=2):
        where = f"scenes.csv line {number}"
        match = ASSET_ID.match(row["asset_id"])
        if not match:
            errors.append(f"{where}: invalid asset_id")
            continue
        if row["asset_id"] in seen:
            errors.append(f"{where}: duplicate asset_id {row['asset_id']}")
        seen.add(row["asset_id"])
        code = row["listing_code"]
        if match["code"] != code:
            errors.append(f"{where}: asset_id does not start with listing_code")
        prop = properties.get(code)
        if prop is None:
            errors.append(f"{where}: listing {code} is not in demo/properties.csv")
            continue
        by_listing.setdefault(code, []).append(row)
        if row["label"] in FORBIDDEN_LABELS:
            errors.append(f"{where}: label {row['label']} is not allowed (no factual "
                          "maps or floor plans)")
        elif row["label"] and row["label"] not in LABELS:
            errors.append(f"{where}: unknown label {row['label']!r}")
        if row["label"] in ROOM_LABELS and prop["property_type"] not in ROOM_TYPES:
            errors.append(f"{where}: room label {row['label']} on a "
                          f"{prop['property_type']}")
        klass = row["scene_class"]
        if klass not in SCENE_CLASSES:
            errors.append(f"{where}: unknown scene_class {klass!r}")
        elif needs_concept(prop) and klass != "concept":
            errors.append(f"{where}: {code} needs concept scenes (status "
                          f"{prop['construction_status'] or prop['possession_status'] or 'unknown'})")
        elif klass == "vacant_land" and prop["property_type"] != "plot":
            errors.append(f"{where}: vacant_land scene for a {prop['property_type']}")
        elif klass == "photo" and prop["property_type"] == "plot":
            errors.append(f"{where}: photo scene for a plot (use vacant_land or concept)")
        if row["mark_corner"] not in CORNERS:
            errors.append(f"{where}: mark_corner must be one of {sorted(CORNERS)}")
        for column in ("title", "prompt", "negative_prompt", "fact_basis"):
            if not row[column].strip():
                errors.append(f"{where}: {column} is empty")
        if row["pilot"] not in ("", "true"):
            errors.append(f"{where}: pilot must be 'true' or blank")
        if row["cover"] not in ("", "true"):
            errors.append(f"{where}: cover must be 'true' or blank")
    missing = sorted(set(properties) - set(by_listing))
    if missing:
        errors.append(f"listings without scenes: {', '.join(missing)}")
    for code, rows in by_listing.items():
        if not MIN_PER_LISTING <= len(rows) <= MAX_PER_LISTING:
            errors.append(f"{code}: {len(rows)} scenes (need {MIN_PER_LISTING}-{MAX_PER_LISTING})")
        orders = sorted(int(r["sort_order"]) for r in rows if r["sort_order"].isdigit())
        if orders != list(range(len(rows))):
            errors.append(f"{code}: sort_order must be 0..{len(rows) - 1} without gaps")
        covers = [r for r in rows if r["cover"] == "true"]
        if len(covers) != 1 or covers[0]["sort_order"] != "0":
            errors.append(f"{code}: exactly one cover, at sort_order 0")
        if len({r["prompt"] for r in rows}) != len(rows):
            errors.append(f"{code}: scenes must be distinct (duplicate prompts)")
    pilots = sorted({r["listing_code"] for r in scenes if r["pilot"] == "true"})
    if pilots != sorted(PILOT_LISTINGS):
        errors.append(f"pilot listings must be {', '.join(PILOT_LISTINGS)}")
    return errors


def _iso_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def validate_intake(scenes, intake, listings=None, *, require_approved=True, today=None):
    """Intake rows for the listings in scope (all 30 when listings is None).

    require_approved: every scene in scope needs an approved, prepared asset (load
    and bundle); False only checks the rows present (prepare, review)."""
    errors = []
    # Dates are written in the operator's time zone (PKT is UTC+5) and checked on hosts in
    # any zone (EC2 and CI run in UTC): allow one day of slack.
    today = (today or datetime.datetime.now(datetime.UTC).date()) + datetime.timedelta(days=1)
    scene_ids = {r["asset_id"]: r for r in scenes}
    in_scope = {a for a, r in scene_ids.items()
                if listings is None or r["listing_code"] in listings}
    seen, prepared, sources = set(), {}, {}
    for number, row in enumerate(intake, start=2):
        where = f"intake.csv line {number}"
        asset_id = row["asset_id"]
        if asset_id not in scene_ids:
            errors.append(f"{where}: asset_id {asset_id!r} is not in scenes.csv")
            continue
        if asset_id in seen:
            errors.append(f"{where}: duplicate asset_id {asset_id}")
        seen.add(asset_id)
        if not SOURCE_PATH.match(row["source_file"]) or len(row["source_file"]) > 200:
            errors.append(f"{where}: source_file must be a relative path of plain names")
        if not SHA256.match(row["source_sha256"]):
            errors.append(f"{where}: source_sha256 must be 64 lowercase hex")
        elif row["source_sha256"] in sources:
            errors.append(f"{where}: same source file as {sources[row['source_sha256']]}")
        else:
            sources[row["source_sha256"]] = asset_id
        for column in ("generator", "generator_model", "generation_prompt",
                       "licence_reference"):
            if not row[column].strip():
                errors.append(f"{where}: {column} is required (record the actual value; "
                              "never invent it)")
        generated = _iso_date(row["generated_on"])
        if generated is None or generated > today:
            errors.append(f"{where}: generated_on must be an ISO date, not in the future")
        status = row["review_status"]
        if status not in REVIEW_STATUSES:
            errors.append(f"{where}: review_status must be one of {sorted(REVIEW_STATUSES)}")
        sha = row["prepared_sha256"]
        if sha:
            if not SHA256.match(sha):
                errors.append(f"{where}: prepared_sha256 must be 64 lowercase hex")
            elif sha in prepared:
                errors.append(f"{where}: same prepared image as {prepared[sha]} "
                              "(duplicates do not count as separate images)")
            else:
                prepared[sha] = asset_id
        previous = [p for p in row["previous_prepared_sha256"].split(";") if p]
        if any(not SHA256.match(p) for p in previous) or (sha and sha in previous):
            errors.append(f"{where}: previous_prepared_sha256 must be ';'-separated hashes "
                          "other than the current one")
        if status == "approved":
            if not sha:
                errors.append(f"{where}: an approved asset needs prepared_sha256")
            unconfirmed = [c for c in PROVENANCE_COLUMNS
                           if row[c].strip().upper().startswith(UNCONFIRMED)]
            if unconfirmed:
                errors.append(f"{where}: an approved asset needs confirmed provenance "
                              f"({', '.join(unconfirmed)} still UNCONFIRMED)")
            if not row["reviewed_by"].strip() or _iso_date(row["reviewed_on"]) is None:
                errors.append(f"{where}: an approved asset needs reviewed_by and reviewed_on")
    if require_approved:
        approved = {r["asset_id"] for r in intake if r["review_status"] == "approved"}
        missing = sorted(in_scope - approved)
        if missing:
            errors.append(f"{len(missing)} scene(s) in scope have no approved asset "
                          f"(first: {missing[0]}); asset intake / review is pending")
    return errors


def approved_assets(scenes, intake, listings=None):
    """Asset records for the approved intake rows in scope, in listing/order sequence."""
    scene_ids = {r["asset_id"]: r for r in scenes}
    assets = []
    for row in intake:
        scene = scene_ids.get(row["asset_id"])
        if scene is None or row["review_status"] != "approved":
            continue
        if listings is not None and scene["listing_code"] not in listings:
            continue
        assets.append(Asset(
            asset_id=row["asset_id"], listing_code=scene["listing_code"],
            sort_order=int(scene["sort_order"]), label=scene["label"] or None,
            scene_class=scene["scene_class"], corner=scene["mark_corner"],
            prepared_sha256=row["prepared_sha256"],
            previous=tuple(p for p in row["previous_prepared_sha256"].split(";") if p),
        ))
    return sorted(assets, key=lambda a: (a.listing_code, a.sort_order))


def read_prepared(assets_dir, asset):
    """The prepared bytes of an asset, checked against the manifest hash."""
    path = Path(assets_dir) / "prepared" / asset.file_name
    if path.is_symlink() or not path.is_file():
        raise MediaPlanError(f"{asset.asset_id}: prepared file missing ({asset.file_name})")
    if path.stat().st_size > media.MAX_DOWNLOAD_BYTES:
        raise MediaPlanError(f"{asset.asset_id}: prepared file too large")
    data = path.read_bytes()
    if sha256_hex(data) != asset.prepared_sha256:
        raise MediaPlanError(f"{asset.asset_id}: prepared file does not match "
                             "prepared_sha256 (changed or wrong file)")
    return data


def check_files(assets, assets_dir):
    errors = []
    for asset in assets:
        try:
            read_prepared(assets_dir, asset)
        except MediaPlanError as exc:
            errors.append(str(exc))
    return errors


def inside_repo(path):
    resolved = Path(path).resolve()
    return resolved == REPO_ROOT or REPO_ROOT in resolved.parents


def allowed_image_folder(path):
    """Keep the in-repository exception confined to the excluded media workspace."""
    resolved = Path(path).resolve()
    local_media = REPO_ROOT / ".local" / "demo-media"
    return not inside_repo(resolved) or resolved.is_relative_to(local_media)


def safe_source(sources_dir, relative):
    """A source file path inside sources_dir (no traversal, no links)."""
    if not SOURCE_PATH.match(relative):
        raise MediaPlanError("source_file must be a relative path of plain names")
    base = Path(sources_dir).resolve()
    path = base / relative
    if path.is_symlink() or not path.is_file() or base not in path.resolve().parents:
        raise MediaPlanError(f"source file not found inside the sources directory: {relative}")
    return path


# --- crop-safe region and mark -----------------------------------------------------

def crop_box(width, height, size=media.CARD_SIZE):
    """The source rectangle ImageOps.fit keeps for the card (centring 0.5, no bleed)."""
    out_ratio = size[0] / size[1]
    ratio = width / height
    if ratio == out_ratio:
        crop_w, crop_h = width, height
    elif ratio >= out_ratio:
        crop_w, crop_h = out_ratio * height, height
    else:
        crop_w, crop_h = width, width / out_ratio
    left = (width - crop_w) * 0.5
    top = (height - crop_h) * 0.5
    return left, top, left + crop_w, top + crop_h


def font(size):
    face = ImageFont.load_default(size=size)
    if not hasattr(face, "getname") or face.getname() != FONT_NAME:
        raise MediaPlanError("the bundled scalable font is not available (Pillow with "
                             "FreeType is required for a reproducible mark)")
    return face


def mark_layout(width, height, text, corner):
    """(pill box, text origin, font size) in source pixels, inside the crop-safe region."""
    left, top, right, bottom = crop_box(width, height)
    safe_h = bottom - top
    pill_h = max(MARK_MIN_HEIGHT, round(safe_h * MARK_HEIGHT))
    size = max(10, round(pill_h * 0.58))
    face = font(size)
    x0, y0, x1, y1 = face.getbbox(text)
    pad = round(pill_h * 0.45)
    pill_w = (x1 - x0) + 2 * pad
    inset = round(safe_h * MARK_INSET) + ROUNDING_PX
    if corner in ("br", "tr"):
        px1 = int(right) - inset
        px0 = px1 - pill_w
    else:
        px0 = int(left + 0.999) + inset
        px1 = px0 + pill_w
    if corner in ("br", "bl"):
        py1 = int(bottom) - inset
        py0 = py1 - pill_h
    else:
        py0 = int(top + 0.999) + inset
        py1 = py0 + pill_h
    box = (px0, py0, px1, py1)
    origin = (px0 + pad - x0, py0 + (pill_h - (y1 - y0)) / 2 - y0)
    return box, origin, size


def apply_mark(image, text, corner):
    """A copy of an RGB image with the pill mark composited."""
    box, origin, size = mark_layout(image.width, image.height, text, corner)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 4, fill=PILL_RGBA)
    draw.text(origin, text, font=font(size), fill=TEXT_RGBA)
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def _region_stats(image, box):
    """(dark share, light share) of the luminance inside the inner 80% of box."""
    x0, y0, x1, y1 = box
    dx, dy = (x1 - x0) * 0.1, (y1 - y0) * 0.1
    inner = tuple(round(v) for v in (x0 + dx, y0 + dy, x1 - dx, y1 - dy))
    if inner[0] < 0 or inner[1] < 0 or inner[2] > image.width or inner[3] > image.height \
            or inner[2] <= inner[0] or inner[3] <= inner[1]:
        return 0.0, 0.0
    histogram = image.convert("L").crop(inner).histogram()
    total = sum(histogram)
    return sum(histogram[:90]) / total, sum(histogram[200:]) / total


def mark_visible(image, box):
    """True if a dark pill with light text is where the mark should be."""
    dark, light = _region_stats(image, box)
    return dark >= 0.5 and light >= 0.03


def derivative_boxes(width, height, box, processed):
    """The mark box mapped into the original, WhatsApp and card derivatives."""
    boxes = {}
    for name in ("original", "wa"):
        with Image.open(io.BytesIO(getattr(processed, name))) as image:
            scale = image.width / width
        boxes[name] = tuple(v * scale for v in box)
    # The card holds the whole photo, fitted and centred (media.card_placement).
    left, top, fitted_w, _ = media.card_placement(width, height)
    scale = fitted_w / width
    boxes["card"] = (left + box[0] * scale, top + box[1] * scale,
                     left + box[2] * scale, top + box[3] * scale)
    return boxes


def check_marks(width, height, box, processed):
    """Names of the derivatives in which the mark is not detected (empty = all good)."""
    missing = []
    for name, mapped in derivative_boxes(width, height, box, processed).items():
        with Image.open(io.BytesIO(getattr(processed, name))) as image:
            image.load()
            if not mark_visible(image, mapped):
                missing.append(name)
    return missing


@dataclass(frozen=True)
class Prepared:
    data: bytes
    sha256: str
    width: int
    height: int
    box: tuple
    processed: media.ProcessedImage


def prepare_image(source, mark, corner):
    """Validate, orient, scale, mark and re-encode a generator file; check derivatives.

    Raises media.MediaRejected (invalid image) or MediaPlanError (unsuitable shape,
    mark not detected)."""
    media.process_image(source)  # existing MIME / pixel-limit / decoding checks
    with Image.open(io.BytesIO(source)) as image:
        image.seek(0)
        upright = ImageOps.exif_transpose(image)
        upright.load()
    width, height = upright.size
    aspect = width / height
    if not MIN_ASPECT <= aspect <= MAX_ASPECT or width < MIN_WIDTH:
        raise MediaPlanError(f"source must be landscape, aspect {MIN_ASPECT}-{MAX_ASPECT} and "
                             f">= {MIN_WIDTH} px wide (got {width}x{height})")
    rgb = media._fit(media._flatten(upright), media.ORIGINAL_MAX_EDGE)
    marked = apply_mark(rgb, mark, corner)
    data = media._encode(marked, "JPEG", quality=PREPARED_QUALITY, subsampling=0)
    processed = media.process_image(data, "image/jpeg")
    box, _, _ = mark_layout(marked.width, marked.height, mark, corner)
    missing = check_marks(marked.width, marked.height, box, processed)
    if missing:
        raise MediaPlanError(f"mark not detected in: {', '.join(missing)}")
    return Prepared(data, sha256_hex(data), marked.width, marked.height, box, processed)


# --- review sheets -------------------------------------------------------------------

SHEET_COLUMNS = (("card", (600, 314)), ("card@320", (320, 168)), ("wa", (480, 314)),
                 ("original", (480, 314)))


def contact_sheet(listing_code, entries):
    """A labelled JPEG sheet: per asset the card (full and at phone-carousel width),
    the WhatsApp image and the original. entries: (caption, ProcessedImage)."""
    gap, row_h, head = 20, 314 + 44, 64
    width = sum(size[0] for _, size in SHEET_COLUMNS) + gap * (len(SHEET_COLUMNS) + 1)
    sheet = Image.new("RGB", (width, head + row_h * len(entries) + gap), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)
    draw.text((gap, 18), f"{listing_code} - REVIEW SHEET (not a deliverable) - "
              f"{watermark_config()}", font=font(20), fill=(0, 0, 0))
    for index, (caption, processed) in enumerate(entries):
        y = head + index * row_h
        x = gap
        for name, size in SHEET_COLUMNS:
            data = processed.card if name.startswith("card") else getattr(processed, name)
            with Image.open(io.BytesIO(data)) as image:
                tile = image.convert("RGB")
                tile.thumbnail(size, Image.Resampling.LANCZOS)
            sheet.paste(tile, (x, y))
            draw.text((x, y + size[1] + 4), name, font=font(16), fill=(60, 60, 60))
            x += size[0] + gap
        draw.text((gap, y + 314 + 22), caption, font=font(16), fill=(0, 0, 0))
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=85)
    return out.getvalue()


# --- generation pack -----------------------------------------------------------------

def render_pack(scenes, properties):
    """docs/DEMO_MEDIA_PROMPTS.md, generated from scenes.csv (pack --check keeps it in sync)."""
    lines = [
        "# Demo media generation pack (Task 4.4)",
        "",
        ("Generated by `python -m app.demo_media pack` from `demo/media/scenes.csv`; edit the CSV, "
         "not this file. Status: **no image has been generated yet.**"),
        "",
        "How to use (docs/DEMO_MEDIA.md, sections 3 and 4):",
        "",
        ("- Generate each scene as a **16:9 landscape image, at least 1,280 px wide** (1,920 x 1,080 "
         "or larger preferred). Do not add any text or watermark: `prepare` adds the DEMO / CONCEPT "
         "mark."),
        ("- Keep every generator file unmodified in a sources folder outside the repository or in "
         "the excluded `.local/demo-media/sources` folder and "
         "record it in `demo/media/intake.csv`: `source_file`, `source_sha256`, the **actual** "
         "generator, model, date and the prompt as submitted, and the licence / terms reference "
         "you checked. Never invent these values."),
        ("- Prompts describe fictional scenes. An image never adds a listing fact; the "
         "\"must not show\" line lists unrecorded attributes to keep out of the image."),
        ("- Reject and regenerate images with people, legible text, logos, number plates, "
         "recognisable real buildings, warped geometry, or anything in \"must not show\"."),
        "",
    ]
    by_listing = {}
    for row in scenes:
        by_listing.setdefault(row["listing_code"], []).append(row)
    order = list(PILOT_LISTINGS) + [c for c in by_listing if c not in PILOT_LISTINGS]
    for index, code in enumerate(order):
        if index == 0:
            lines += ["## Pilot (3 listings x 4 images)", ""]
        elif index == len(PILOT_LISTINGS):
            lines += ["## Remaining listings (27 x 4 images)", ""]
        rows = sorted(by_listing[code], key=lambda r: int(r["sort_order"]))
        prop = properties[code]
        lines += [f"### {code}", "",
                  f"Facts: {rows[0]['fact_basis']}. Status `{prop['status']}`.", "",
                  f"Must not show: {rows[0]['must_not_show'] or '-'}.", ""]
        for row in rows:
            cover = " (cover)" if row["cover"] == "true" else ""
            lines += [
                f"#### {row['asset_id']} - {row['title']}{cover}",
                "",
                (f"- Order {row['sort_order']}; label `{row['label'] or 'NULL'}`; class "
                 f"`{row['scene_class']}`; mark `{MARKS.get(row['scene_class'], '?')}` "
                 f"({row['mark_corner']} corner of the crop-safe region)"),
                f"- Negative prompt: {row['negative_prompt']}",
                "",
                "```text",
                row["prompt"],
                "```",
                "",
            ]
    return "\n".join(lines).rstrip("\n") + "\n"


# --- bundle --------------------------------------------------------------------------

ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def build_bundle(assets, assets_dir, media_dir=MEDIA_DIR):
    """A ZIP of the prepared files, SHA256SUMS and copies of the two manifests."""
    out = io.BytesIO()
    sums = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for asset in assets:
            data = read_prepared(assets_dir, asset)
            name = f"prepared/{asset.file_name}"
            archive.writestr(zipfile.ZipInfo(name, ZIP_TIME), data)
            sums.append(f"{asset.prepared_sha256}  {name}")
        for manifest in (SCENES_FILE, INTAKE_FILE):
            archive.writestr(zipfile.ZipInfo(f"manifest/{manifest}", ZIP_TIME),
                             (Path(media_dir) / manifest).read_bytes())
        archive.writestr(zipfile.ZipInfo("SHA256SUMS", ZIP_TIME), "\n".join(sums) + "\n")
    return out.getvalue()


# --- CLI -----------------------------------------------------------------------------

def _print_errors(errors):
    for error in errors[:200]:
        print(f"  ERROR {error}")
    if len(errors) > 200:
        print(f"  ... {len(errors) - 200} more")


def cmd_validate(args):
    scenes, intake = load_plan(args.media_dir)
    properties = load_properties()
    errors = validate_scenes(scenes, properties)
    listings = parse_listings(args.listings, scenes)
    if not args.plan_only:
        errors += validate_intake(scenes, intake, listings)
        if args.assets_dir and not errors:
            errors += check_files(approved_assets(scenes, intake, listings), args.assets_dir)
    scope = "all 30 listings" if listings is None else ", ".join(sorted(listings))
    if errors:
        print(f"Demo media manifest INVALID ({scope}): {len(errors)} error(s)")
        _print_errors(errors)
        return 1
    print(f"Demo media manifest valid ({'plan only' if args.plan_only else scope}): "
          f"{len(scenes)} scenes, {len(intake)} intake rows")
    return 0


def cmd_pack(args):
    scenes, _ = load_plan(args.media_dir)
    properties = load_properties()
    errors = validate_scenes(scenes, properties)
    if errors:
        print("scenes.csv is invalid; fix it first")
        _print_errors(errors)
        return 1
    text = render_pack(scenes, properties)
    current = PACK_FILE.read_text(encoding="utf-8") if PACK_FILE.exists() else None
    if args.check:
        if current is not None and current.replace("\r\n", "\n") == text:
            print("Generation pack is up to date")
            return 0
        print(f"{PACK_FILE.name} is out of date; run python -m app.demo_media pack")
        return 1
    PACK_FILE.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {PACK_FILE.relative_to(REPO_ROOT)} ({len(scenes)} scenes)")
    return 0


def cmd_prepare(args):
    if not allowed_image_folder(args.out_dir) or not allowed_image_folder(args.sources_dir):
        raise MediaPlanError("keep image folders outside the repository or in .local/demo-media")
    scenes, intake = load_plan(args.media_dir)
    errors = validate_scenes(scenes, load_properties())
    listings = parse_listings(args.listings, scenes)
    errors += validate_intake(scenes, intake, listings, require_approved=False)
    if errors:
        print("Manifest invalid; nothing prepared")
        _print_errors(errors)
        return 1
    scene_ids = {r["asset_id"]: r for r in scenes}
    out = Path(args.out_dir)
    sheets, failures, changed = {}, [], False
    for row in intake:
        scene = scene_ids[row["asset_id"]]
        code = scene["listing_code"]
        if listings is not None and code not in listings:
            continue
        name = prepared_name(row["asset_id"])
        try:
            source = safe_source(args.sources_dir, row["source_file"])
            if source.stat().st_size > MAX_SOURCE_BYTES:
                raise MediaPlanError("source file too large")
            data = source.read_bytes()
            if sha256_hex(data) != row["source_sha256"]:
                raise MediaPlanError("source file does not match source_sha256")
            result = prepare_image(data, MARKS[scene["scene_class"]], scene["mark_corner"])
        except (MediaPlanError, media.MediaRejected) as exc:
            failures.append(f"{row['asset_id']}: {getattr(exc, 'code', None) or exc}")
            continue
        if row["prepared_sha256"] and row["prepared_sha256"] != result.sha256:
            failures.append(f"{row['asset_id']}: prepared image differs from the recorded "
                            "prepared_sha256 (move the old hash to previous_prepared_sha256 "
                            "and reset the review first)")
            continue
        if args.record and not row["prepared_sha256"]:
            row["prepared_sha256"] = result.sha256
            changed = True
        (out / "prepared").mkdir(parents=True, exist_ok=True)
        (out / "prepared" / name).write_bytes(result.data)
        review = out / "review" / code
        review.mkdir(parents=True, exist_ok=True)
        stem = name[:-4]
        for kind in ("original", "wa", "card"):
            data_out = getattr(result.processed, kind)
            ext = result.processed.original_ext if kind == "original" else "jpg"
            (review / f"{stem}_{kind}.{ext}").write_bytes(data_out)
        caption = (f"{row['asset_id']} | order {scene['sort_order']} | label "
                   f"{scene['label'] or 'NULL'} | {scene['scene_class']} | review "
                   f"{row['review_status']} | prepared {result.sha256[:12]} | "
                   f"{result.width}x{result.height}")
        sheets.setdefault(code, []).append((int(scene["sort_order"]), caption,
                                            result.processed))
        print(f"  {row['asset_id']}: prepared {result.sha256} ({result.width}x{result.height}, "
              "mark found in original / wa / card)")
    for code, entries in sheets.items():
        entries.sort(key=lambda e: e[0])
        sheet = contact_sheet(code, [(c, p) for _, c, p in entries])
        (out / "review" / f"{code}_sheet.jpg").write_bytes(sheet)
    if changed:
        write_intake(intake, args.media_dir)
        print("Recorded prepared_sha256 in intake.csv")
    for failure in failures:
        print(f"  FAILED {failure}")
    print(f"Prepared {sum(len(e) for e in sheets.values())} image(s); "
          f"{len(failures)} failure(s); review sheets in {out / 'review'}")
    print("Visual review is still required: inspect every sheet and derivative, then set "
          "review_status, reviewed_by and reviewed_on in intake.csv.")
    return 1 if failures else 0


def cmd_bundle(args):
    if inside_repo(args.out):
        raise MediaPlanError("write the bundle outside the repository")
    scenes, intake = load_plan(args.media_dir)
    listings = parse_listings(args.listings, scenes)
    errors = validate_scenes(scenes, load_properties()) + validate_intake(
        scenes, intake, listings)
    assets = approved_assets(scenes, intake, listings)
    if not errors:
        errors = check_files(assets, args.assets_dir)
    if errors:
        print("Bundle not written")
        _print_errors(errors)
        return 1
    data = build_bundle(assets, args.assets_dir, args.media_dir)
    Path(args.out).write_bytes(data)
    print(f"Wrote {Path(args.out).name}: {len(assets)} prepared image(s), "
          f"{len(data)} bytes, sha256 {sha256_hex(data)}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--media-dir", default=str(MEDIA_DIR), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="check the plan (and intake / prepared files)")
    validate.add_argument("--plan-only", action="store_true")
    validate.add_argument("--listings")
    validate.add_argument("--assets-dir")
    pack = sub.add_parser("pack", help="write docs/DEMO_MEDIA_PROMPTS.md from scenes.csv")
    pack.add_argument("--check", action="store_true")
    prepare = sub.add_parser("prepare", help="mark and check generator files; review sheets")
    prepare.add_argument("--sources-dir", required=True)
    prepare.add_argument("--out-dir", required=True)
    prepare.add_argument("--listings")
    prepare.add_argument("--record", action="store_true",
                         help="write new prepared_sha256 values into intake.csv")
    bundle = sub.add_parser("bundle", help="ZIP the approved prepared files for transfer")
    bundle.add_argument("--assets-dir", required=True)
    bundle.add_argument("--out", required=True)
    bundle.add_argument("--listings")
    args = parser.parse_args(argv)
    commands = {"validate": cmd_validate, "pack": cmd_pack, "prepare": cmd_prepare,
                "bundle": cmd_bundle}
    try:
        return commands[args.command](args)
    except MediaPlanError as exc:
        print(f"Demo media: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
