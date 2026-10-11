"""Task 5.4 operator check for gate M1(d): replaying a webhook creates no duplicate
inbound event, job, message or outbound message.

    python -m app.m1_replay --agency-id <uuid> [--event-id <uuid>] [--times 2] [--wait 15]

Run inside the web container (it has META_APP_SECRET, DATABASE_URL and PORT).
It takes an inbound message event that was already processed (the newest one
whose process_message job succeeded, or --event-id), rebuilds the webhook body
Meta delivered for it from the stored payload (same metadata, contact and
message, so the same WAMID and phone_number_id), signs it with META_APP_SECRET
exactly as Meta does (X-Hub-Signature-256) and POSTs it to this server's own
/webhook --times times. After --wait seconds (time for the worker to pick up
anything new) it compares the counts with those taken before:

- inbound_event rows with the event's key, and jobs with its dedupe key;
- message rows with its WAMID;
- outbound messages and turns of its conversation.

PASS = every POST answered 200 and nothing changed. Read-only apart from the
POSTs. Prints no phone number, message text, secret or signature; the WAMID
only as its last 6 characters.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import httpx
import psycopg

from app import webhook
from app.db import tenant_transaction

COUNTS = ("inbound_event", "job", "message", "outbound_message", "conversation_turn")


class ReplayError(Exception):
    pass


def pick_event(conn, agency_id, event_id=None):
    """(event id, event_key, payload, conversation_id) of a processed message event."""
    with tenant_transaction(conn, agency_id):
        row = conn.execute(
            """
            SELECT e.id, e.event_key, e.payload, m.conversation_id
            FROM inbound_event e
            JOIN job j ON j.inbound_event_id = e.id AND j.job_type = 'process_message'
            JOIN message m ON m.provider_message_id = e.provider_message_id
                          AND m.direction = 'inbound'
            WHERE e.event_type = 'message' AND j.status = 'succeeded'
              AND (%(id)s::uuid IS NULL OR e.id = %(id)s::uuid)
            ORDER BY e.received_at DESC
            LIMIT 1
            """,
            {"id": event_id},
        ).fetchone()
    if row is None:
        raise ReplayError("no processed inbound message event found"
                          + (" with that id" if event_id else ""))
    return row


def counts(conn, agency_id, event_key, wamid, conversation_id):
    with tenant_transaction(conn, agency_id):
        return dict(zip(COUNTS, conn.execute(
            """
            SELECT
                (SELECT count(*) FROM inbound_event WHERE event_key = %(key)s),
                (SELECT count(*) FROM job WHERE dedupe_key = %(dedupe)s),
                (SELECT count(*) FROM message WHERE provider_message_id = %(wamid)s),
                (SELECT count(*) FROM outbound_message WHERE conversation_id = %(conv)s),
                (SELECT count(*) FROM conversation_turn WHERE conversation_id = %(conv)s)
            """,
            {"key": event_key, "dedupe": f"process_message:{wamid}", "wamid": wamid,
             "conv": conversation_id},
        ).fetchone(), strict=True))


def envelope(payload):
    """The webhook body Meta sends for one message, rebuilt from the stored event."""
    value = {"messaging_product": "whatsapp", "metadata": payload["metadata"],
             "messages": [payload["message"]]}
    if payload.get("contact"):
        value["contacts"] = [payload["contact"]]
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "m1-replay", "changes": [{"field": "messages", "value": value}]}]}


def signed(body, app_secret):
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", webhook.SIGNATURE_HEADER: f"sha256={digest}"}


def http_post(url, body, headers):
    return httpx.post(url, content=body, headers=headers, timeout=10).status_code


def run(conn, agency_id, app_secret, url, *, event_id=None, times=2, wait=15,
        post=http_post, sleep=time.sleep):
    event_id_, event_key, payload, conversation_id = pick_event(conn, agency_id, event_id)
    wamid = payload["message"]["id"]
    before = counts(conn, agency_id, event_key, wamid, conversation_id)
    body = json.dumps(envelope(payload), separators=(",", ":")).encode("utf-8")
    statuses = [post(url, body, signed(body, app_secret)) for _ in range(times)]
    sleep(wait)
    after = counts(conn, agency_id, event_key, wamid, conversation_id)
    passed = all(s == 200 for s in statuses) and after == before and before["inbound_event"] == 1
    return {"event_id": str(event_id_), "message_type": payload["message"].get("type"),
            "wamid_tail": wamid[-6:], "statuses": statuses, "before": before, "after": after,
            "passed": passed}


def main(argv=None, environ=None, post=http_post, sleep=time.sleep):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agency-id", required=True)
    parser.add_argument("--event-id")
    parser.add_argument("--times", type=int, default=2)
    parser.add_argument("--wait", type=float, default=15)
    args = parser.parse_args(argv)
    try:
        agency_id = uuid.UUID(args.agency_id)
        event_id = str(uuid.UUID(args.event_id)) if args.event_id else None
    except ValueError:
        print("--agency-id and --event-id must be UUIDs")
        return 1
    secret = (environ.get("META_APP_SECRET") or "").strip()
    database_url = (environ.get("DATABASE_URL") or "").strip()
    if not secret or not database_url:
        print("META_APP_SECRET and DATABASE_URL must be set (run inside the web container)")
        return 1
    if not 1 <= args.times <= 5:
        print("--times must be 1-5")
        return 1
    url = f"http://127.0.0.1:{(environ.get('PORT') or '8000').strip()}/webhook"
    try:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as conn:
            result = run(conn, agency_id, secret, url, event_id=event_id, times=args.times,
                         wait=args.wait, post=post, sleep=sleep)
    except ReplayError as exc:
        print(f"Replay not run: {exc}")
        return 1
    except (psycopg.Error, httpx.HTTPError) as exc:
        print(f"Replay failed ({type(exc).__name__})")
        return 1
    print(f"Event {result['event_id']} ({result['message_type']}, WAMID ...{result['wamid_tail']})")
    print(f"Replayed {len(result['statuses'])} time(s): HTTP {result['statuses']}")
    for name in COUNTS:
        print(f"  {name}: before {result['before'][name]}, after {result['after'][name]}")
    print("M1(d) PASS: no duplicate event, job, message or outbound message"
          if result["passed"] else "M1(d) FAIL")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
