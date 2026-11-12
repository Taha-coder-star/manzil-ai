"""Read-only sanitized trace exporter for an explicitly mapped WhatsApp cohort.

DATABASE_URL stays private. Mapping CSV: turn_id,item_id,repeat,modality,kinds.
kinds is an ordered semicolon-separated echo/notice/substantive classification,
reviewed by the operator against the private conversation. It must cover every
outbound row. No inference from payload text, no phones/transcripts/UUIDs exported.
Run on the candidate image with the runtime restricted login; never as DB owner.
"""

import argparse
import csv
import json
import os
import re
import sys
import uuid
from pathlib import Path

import psycopg

from app.day13_bench import load, write
from app.day13_report import trace_rows
from app.db import tenant_transaction


def ms(value):
    return value.timestamp() * 1000 if value is not None else None


def export(conn, agency, mapping, candidate):
    previous = conn.read_only
    conn.read_only = True
    try:
        return _export(conn, agency, mapping, candidate)
    finally:
        conn.read_only = previous


def _export(conn, agency, mapping, candidate):
    turns = []
    with tenant_transaction(conn, agency):
        conn.execute("SET LOCAL statement_timeout='10s'")
        for entry in mapping:
            turn = uuid.UUID(entry["turn_id"])
            # Tenant transaction/RLS and every explicit agency predicate stay in place.
            row = conn.execute("SELECT status,created_at FROM conversation_turn "
                "WHERE agency_id=%s AND id=%s", (agency, turn)).fetchone()
            if not row:
                raise ValueError("mapped turn missing in the selected agency")
            receipts = conn.execute("SELECT COALESCE(e.received_at,m.created_at),m.stt_meta "
                "FROM message m LEFT JOIN inbound_event e ON e.agency_id=m.agency_id "
                "AND e.id=m.inbound_event_id WHERE m.agency_id=%s AND m.turn_id=%s "
                "ORDER BY m.created_at,m.id", (agency, turn)).fetchall()
            outbound = conn.execute("SELECT sent_at,delivered_at,status FROM outbound_message "
                "WHERE agency_id=%s AND dedupe_key LIKE %s ORDER BY created_at,id",
                (agency, f"turn:{turn}:%")).fetchall()
            kinds = entry["kinds"].split(";") if entry.get("kinds") else []
            if len(kinds) != len(outbound) or any(k not in ("echo", "notice", "substantive") for k in kinds):
                raise ValueError("classification must cover every outbound row in order")
            metas = [meta for _, meta in receipts if meta]
            speech = [meta.get("speech_ms") for meta in metas]
            durations = [meta.get("duration_ms") for meta in metas]
            columns = ("provider", "model", "operation", "input_tokens", "output_tokens",
                       "cached_input_tokens", "thinking_tokens", "audio_seconds", "outcome")
            usage = conn.execute("SELECT provider,model,operation,input_tokens,output_tokens,"
                "cached_input_tokens,thinking_tokens,audio_seconds,outcome FROM usage_event "
                "WHERE agency_id=%s AND (request_key LIKE %s OR EXISTS ("
                "SELECT 1 FROM handoff h JOIN lead l ON l.agency_id=h.agency_id AND l.id=h.lead_id "
                "JOIN message m ON m.agency_id=l.agency_id AND m.id=l.last_customer_message_id "
                "WHERE h.agency_id=usage_event.agency_id AND m.turn_id=%s "
                "AND usage_event.request_key LIKE 'handoff:' || h.id::text || ':%%')) "
                "ORDER BY created_at,id", (agency, f"turn:{turn}:%", turn)).fetchall()
            events = [dict(zip(columns, row, strict=True)) for row in usage]
            for event in events:
                if event["audio_seconds"] is not None:
                    event["audio_seconds"] = str(event["audio_seconds"])
            turns.append({"item_id": entry["item_id"], "repeat": int(entry["repeat"]),
                "inquiry_id": entry.get("inquiry_id"), "metering": events,
                "modality": entry["modality"], "status": "succeeded" if row[0] == "replied" else "failed",
                "received_ms": [ms(t) for t, _ in receipts],
                "duration_ms": sum(durations) if durations and all(d is not None for d in durations) else None,
                "sends": [{"kind": kind, "accepted_ms": ms(sent), "delivered_ms": ms(delivered)}
                          for kind, (sent, delivered, _) in zip(kinds, outbound, strict=True)],
                "breakdown_ms": {"assembly": ms(row[1]) - min(ms(t) for t, _ in receipts),
                                 "stt": sum(speech) if speech and all(s is not None for s in speech) else None}})
    result = {"schema": 1, "candidate": candidate, "turns": turns}
    trace_rows(result, candidate)  # validates timing, cohorts, pseudonyms and duplicates
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("agency-id", "mapping", "candidate", "out"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args(argv)
    try:
        agency = uuid.UUID(args.agency_id)
        mapping = list(csv.DictReader(Path(args.mapping).read_text("utf-8-sig").splitlines()))
        if not mapping or any(not re.fullmatch(r"(?:CT|CV|VC|SC|ST)-\d{2}", r.get("item_id", ""))
                              for r in mapping):
            raise ValueError("mapping requires benchmark item IDs")
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            result = export(conn, agency, mapping, load(args.candidate))
        write(args.out, result)
    except (KeyError, ValueError, OSError, psycopg.Error):
        # Driver exceptions can contain a connection string or auth details.
        print("Day13 trace export refused; check private mapping, classification and DB access", file=sys.stderr)
        return 2
    print(json.dumps({"exported_turns": len(result["turns"]), "read_only": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
