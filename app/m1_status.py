"""Task 5.4 operator view for gate M1 (a)-(c): what the system recorded recently.

    python -m app.m1_status --agency-id <uuid> --since <ISO time with offset>

Read-only. Prints, newest last:

- turns (M1(a)): time, conversation prefix, route, messages coalesced, status and
  how many replies were sent for the turn with their delivery status;
- staff photos (M1(b)): status, MIME type, EXIF stripped, the private original /
  wa keys and the public card URL;
- inbound audio (M1(c), migration 012): status, voice flag, type, duration and key.

Times are UTC (Karachi is UTC+5: give --since with its offset). Prints no phone
number, message text or media ID. Environment: DATABASE_URL (manzil_app).
"""

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime

import psycopg

from app.db import tenant_transaction


def _utc(value):
    return f"{value.astimezone(UTC):%Y-%m-%d %H:%M:%S}Z" if value else "-"


def turns(conn, since):
    return conn.execute(
        """
        SELECT t.created_at, left(t.conversation_id::text, 8), t.route, t.message_count,
               t.status,
               count(o.id) FILTER (WHERE o.status = 'sent'),
               string_agg(DISTINCT coalesce(o.delivery_status, o.status), '/')
        FROM conversation_turn t
        LEFT JOIN outbound_message o
            ON o.conversation_id = t.conversation_id
           AND o.dedupe_key LIKE 'turn:' || t.id::text || ':%%'
        WHERE t.created_at >= %s
        GROUP BY t.id ORDER BY t.created_at
        """,
        (since,),
    ).fetchall()


def photos(conn, since):
    return conn.execute(
        """
        SELECT created_at, status, error_code, mime_type, exif_stripped, original_key,
               wa_image_key, public_url_card
        FROM inbound_media WHERE created_at >= %s ORDER BY created_at
        """,
        (since,),
    ).fetchall()


def audio(conn, since):
    if conn.execute("SELECT to_regclass('inbound_audio')").fetchone()[0] is None:
        return None
    return conn.execute(
        """
        SELECT created_at, status, error_code, is_voice, mime_type, duration_ms, storage_key
        FROM inbound_audio WHERE created_at >= %s ORDER BY created_at
        """,
        (since,),
    ).fetchall()


def report(conn, agency_id, since):
    with tenant_transaction(conn, agency_id):
        found = {"turns": turns(conn, since), "photos": photos(conn, since),
                 "audio": audio(conn, since)}
    lines = [f"Turns since {_utc(since)} (M1(a)):"]
    for at, conv, route, count, status, sent, delivery in found["turns"]:
        lines.append(f"  {_utc(at)} conv {conv} {route:<8} {count} message(s) {status:<7} "
                     f"replies sent {sent} ({delivery or 'none'})")
    lines.append(f"Staff photos (M1(b)): {len(found['photos'])}")
    for at, status, error, mime, stripped, original, wa, card in found["photos"]:
        lines.append(f"  {_utc(at)} {status} {error or ''} {mime or '-'} "
                     f"exif_stripped={str(stripped).lower()}")
        if status in ("stored", "duplicate"):
            lines += [f"    original (private): {original}", f"    wa (private): {wa}",
                      f"    card (public): {card}"]
    if found["audio"] is None:
        lines.append("Inbound audio (M1(c)): table not present (migration 012 not applied)")
    else:
        lines.append(f"Inbound audio (M1(c)): {len(found['audio'])}")
        for at, status, error, voice, mime, duration, key in found["audio"]:
            lines.append(f"  {_utc(at)} {status} {error or ''} voice={voice} {mime or '-'} "
                         f"{duration if duration is not None else '-'} ms {key or ''}")
    return found, lines


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agency-id", required=True)
    parser.add_argument("--since", required=True)
    args = parser.parse_args(argv)
    try:
        agency_id = uuid.UUID(args.agency_id)
        since = datetime.fromisoformat(args.since)
    except ValueError:
        print("--agency-id must be a UUID and --since an ISO time")
        return 1
    if since.tzinfo is None:
        print("--since needs a UTC offset, e.g. 2026-10-11T10:00+05:00")
        return 1
    database_url = (environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        print("DATABASE_URL is not set")
        return 1
    try:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as conn:
            _, lines = report(conn, agency_id, since)
    except psycopg.Error as exc:
        print(f"Database error ({type(exc).__name__})")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
