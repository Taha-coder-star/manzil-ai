"""Task 5.3 operator export: the benchmark voice notes as pulled through the real path.

    python -m app.bench_manifest --agency-id <uuid> --since <ISO time> [--until <ISO time>]

Prints CSV, one row per inbound audio message received in the window, in arrival
order: seq, conversation (first 8 characters of its internal UUID, so notes from
one phone group together), received_at (WhatsApp time, UTC), status, is_voice,
mime_type, duration_ms, byte_size, sha256 and storage_key. The operator adds
the benchmark item id and speaker / phone ids from the offline recording sheet
and commits the result as bench/voice_manifest.csv (docs/BENCHMARK.md). The audio
stays in the private bucket; no phone number, media ID or message text is printed.

Environment: DATABASE_URL (the manzil_app runtime login). Read-only.
"""

import argparse
import csv
import os
import sys
import uuid
from datetime import UTC, datetime

import psycopg

from app.db import tenant_transaction

COLUMNS = ("seq", "conversation", "received_at", "status", "is_voice", "mime_type",
           "duration_ms", "byte_size", "sha256", "storage_key")


def _when(value):
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError("include a UTC offset, e.g. 2026-10-12T09:00+05:00")
    return when


def rows(conn, agency_id, since, until=None):
    with tenant_transaction(conn, agency_id):
        found = conn.execute(
            """
            SELECT a.conversation_id, COALESCE(m.provider_timestamp, a.created_at), a.status,
                   a.is_voice, a.mime_type, a.duration_ms, a.byte_size, a.content_sha256,
                   a.storage_key
            FROM inbound_audio a JOIN message m ON m.id = a.message_id
            WHERE COALESCE(m.provider_timestamp, a.created_at) >= %s
              AND (%s::timestamptz IS NULL OR COALESCE(m.provider_timestamp, a.created_at) < %s)
            ORDER BY 2, a.created_at
            """,
            (since, until, until),
        ).fetchall()
    return [
        {"seq": n, "conversation": str(conversation)[:8],
         "received_at": f"{received.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}",
         "status": status, "is_voice": "" if voice is None else str(voice).lower(),
         "mime_type": mime or "", "duration_ms": "" if duration is None else duration,
         "byte_size": "" if size is None else size, "sha256": sha or "", "storage_key": key or ""}
        for n, (conversation, received, status, voice, mime, duration, size, sha, key)
        in enumerate(found, 1)
    ]


def main(argv=None, environ=None, out=None):
    environ = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agency-id", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    args = parser.parse_args(argv)
    try:
        agency_id = uuid.UUID(args.agency_id)
        since = _when(args.since)
        until = _when(args.until) if args.until else None
    except ValueError as exc:
        print(f"Bad argument: {exc}", file=sys.stderr)
        return 1
    url = (environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
            found = rows(conn, agency_id, since, until)
    except psycopg.Error as exc:
        print(f"Database error ({type(exc).__name__})", file=sys.stderr)
        return 1
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(found)
    print(f"{len(found)} audio message(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
