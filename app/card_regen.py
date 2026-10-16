"""Rebuild the stored card images with the uncropped layout (media.card_image).

    python -m app.card_regen --agency-id <uuid>            # dry run: counts only
    python -m app.card_regen --agency-id <uuid> --apply

Card images made before the contain layout were centre-cropped to 1.91:1. For each
distinct card object of the agency (property_media and stored inbound_media share
objects), this reads the private original, builds the new card and writes it under a
NEW key, card/<agency_id>/v2/<random uuid>.jpg: card objects are cached as immutable,
so they are never overwritten. Then, in one tenant transaction per object, every row
that pointed at the old key gets the new key and public URL. A crash leaves at most an
unused object. Rows already under card/<agency>/v2/ are skipped, so a rerun only
rebuilds what is left. The old objects stay (rollback: point the rows back).

Environment: the app's (DATABASE_URL as manzil_app, S3_BUCKET, S3_REGION, ...). Prints
counts only: no URL, key or image bytes.
"""

import argparse
import io
import sys
import uuid

import psycopg
from PIL import Image

from app import media
from app.config import load_settings
from app.db import tenant_transaction

STATEMENT_TIMEOUT = "5s"
V2 = "/v2/"


def pending(conn, agency_id):
    """[(old card key, original key)] of the agency's cards not yet rebuilt."""
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        rows = conn.execute(
            """
            SELECT card_image_key, min(original_key) FROM (
                SELECT card_image_key, original_key FROM property_media
                WHERE card_image_key IS NOT NULL AND original_key IS NOT NULL
                UNION ALL
                SELECT card_image_key, original_key FROM inbound_media
                WHERE status = 'stored' AND card_image_key IS NOT NULL
                  AND original_key IS NOT NULL
            ) cards
            WHERE position(%s IN card_image_key) = 0
            GROUP BY card_image_key ORDER BY card_image_key
            """, (V2,)).fetchall()
    return [(row[0], row[1]) for row in rows]


def rebuild(original):
    """The new card JPEG from the stored (upright, metadata-free) original."""
    with Image.open(io.BytesIO(original)) as image:
        image.load()
        card = media._encode(media.card_image(media._flatten(image)), "JPEG",
                             quality=media.CARD_JPEG_QUALITY, optimize=True, progressive=True)
    if media.has_metadata(card):
        raise ValueError("metadata_not_stripped")
    return card


def repoint(conn, agency_id, old_key, new_key, public_base):
    """Point every row of old_key at new_key; returns the number of rows changed."""
    url = f"{public_base}/{new_key}"
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        changed = conn.execute(
            "UPDATE property_media SET card_image_key = %s, public_url_card = %s "
            "WHERE card_image_key = %s", (new_key, url, old_key)).rowcount
        changed += conn.execute(
            "UPDATE inbound_media SET card_image_key = %s, public_url_card = %s, "
            "updated_at = now() WHERE card_image_key = %s", (new_key, url, old_key)).rowcount
    return changed


def run(conn, storage, agency_id, public_base, apply=False):
    """(cards found, cards rebuilt, rows repointed, failures)."""
    found = pending(conn, agency_id)
    if not apply:
        return len(found), 0, 0, 0
    rebuilt = rows = failed = 0
    for old_key, original_key in found:
        try:
            card = rebuild(storage.get(original_key))
        except Exception as exc:  # noqa: BLE001 - one bad image must not stop the rest
            print(f"skipped one card: {type(exc).__name__}")
            failed += 1
            continue
        new_key = f"card/{agency_id}{V2}{uuid.uuid4()}.jpg"
        storage.put(new_key, card, "image/jpeg", media.CARD_CACHE_CONTROL)
        rows += repoint(conn, agency_id, old_key, new_key, public_base)
        rebuilt += 1
    return len(found), rebuilt, rows, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agency-id", required=True, type=uuid.UUID)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args(argv)
    settings = load_settings()
    public_base = media.public_base_url(settings)
    if not settings.s3_bucket or not public_base:
        print("S3_BUCKET / the public card base URL is not configured")
        return 2
    storage = media.S3Storage(settings.s3_bucket, settings.s3_region, settings.s3_endpoint_url)
    with psycopg.connect(settings.database_url, autocommit=True, connect_timeout=10) as conn:
        found, rebuilt, rows, failed = run(conn, storage, str(args.agency_id), public_base,
                                           apply=args.apply)
    mode = "applied" if args.apply else "dry run"
    print(f"{mode}: {found} card(s) to rebuild, {rebuilt} rebuilt, {rows} row(s) repointed, "
          f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
