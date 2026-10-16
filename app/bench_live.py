"""Task 10.3: benchmark runners through the real application path (R16 section 4 / 5).

    python -m app.bench_live plan     {text,staff,controls}
    python -m app.bench_live dry-run  {text,staff,controls} --database-url URL --out DIR
    python -m app.bench_live live     {text,staff,controls} --database-url URL --out DIR
                                      --confirm-live --max-calls N --spend-cap-usd X
                                      --prices-file FILE [--repeats 3]

Every case runs through the production worker, orchestrator, turn engine, 7.3 requirements,
8.1 search, 9.1 renderer, 8.2 staff tools and (controls) the app.speech chain, on a fresh
disposable PostgreSQL schema per case group (restricted manzil_app login, RLS on). WhatsApp is
a recording fake: nothing is ever sent to a phone. Only the model differs between modes:

- plan: no database, no key, no request; case groups and the request bounds.
- dry-run: a scripted model replays the gold tool calls (text), the hand-written spans of
  bench/staff_spans.json (staff), or answers with no tool (controls). This is a ceiling of
  the backend, NOT a model score; results carry "model": null.
- live: the production GeminiClient behind a spend guard. It refuses to start without an
  explicit request cap, a currency cap and a dated prices file, checks both caps before every
  provider request (primary, failover and continuation attempts each count), and stops the
  run when a cap would be crossed; what is left is reported NOT_RUN, never skipped.

Measurement boundaries:
- latency_ms: from the inbound message being stored (webhook already done) to the first
  payload handed to the fake WhatsApp client. It excludes Meta's webhook delivery, the queue's
  poll interval (jobs are drained at once) and the send to Meta. Staff segment turns are timed
  from the moment the 7.4 quiet window (75 s, skipped here by advancing job.available_at) ends.
- provider attempts: one record per request (case, turn, model, outcome, latency, tokens),
  cross-checked against the usage_event rows the engine itself wrote.
- extraction: the requirement_state slots after the turn (text) or the stored listing_draft
  (staff) against the gold values. end_to_end: what the customer / staff member got (shown
  listings, the question asked, the card, the candidate list). Reported separately.

Never written: transcripts, phone numbers, keys, prompts or model prose. Item text is public
benchmark text.
"""

import argparse
import contextlib
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

from app import benchmark, llm, seed, staff_bench, stt_bench, webhook
from app.llm import ProviderError

SCHEMA = 1
TASK = "10.3"
SETS = ("text", "scope", "staff", "controls")
# Scope set (Demo Scope section 6 photos): fictional photos per listing in the bench schema
# (production holds media for DH6-101 and PRJ-103 only); recorded in the run identity.
MEDIA_FIXTURE_PHOTOS = 8
BLOCKED = {"handoff": "Task 11.x (human handoff; Developer 2): request_handoff is a stub"}
PNID = "100000000000310"
# Fictional seed phones (the digits the webhook would deliver); never real numbers.
STAFF_ENV = {"DEMO_ADMIN_PHONE": "15550310101", "DEMO_STAFF_PHONE": "15550310102",
             "DEMO_BROKER_PHONE": "15550310103"}
STAFF_SENDER = STAFF_ENV["DEMO_STAFF_PHONE"]  # the agent role
CUSTOMER_PREFIX = "1555031"
# Every customer-visible question the customer tools can ask (text or buttons).
QUESTION_KEYS = ("location_ambiguous", "location_confirm", "location_reask", "purpose_ask",
                 "type_unclear", "size_unit_ask", "size_ask", "budget_operator_ask",
                 "budget_unclear", "budget_implausible", "budget_ask", "budget_ask_rent",
                 "budget_ask_sale", "nearby_ask", "listing_unknown")
# The 8.1 search gate's questions (app.render.TEXT); purpose_ask is shared with the notices.
RENDER_QUESTION_KEYS = ("location_ask", "type_ask", "generic_ask", "which_place",
                        "confirm_place")
HIDDEN = frozenset({"sold", "reserved", "rented"})
# Replies that never answer a no-match or a clarification: the failure / refusal templates,
# the generic default prompt and the "not ready" stubs (app.turn_engine.TEMPLATES[_EN]).
INAPPROPRIATE_REPLIES = ("failure", "refusal", "customer_default", "search_unavailable",
                         "requirements_unavailable", "requirements_unclear")
NO_MATCH_WORDING = ("no_match", "no_match_cheaper")  # app.render.TEXT, both languages
CRITICAL = ("purpose", "budget", "size", "location")  # R16 5 A / B critical fields
# Turn engine bounds (app.turn_engine): MAX_ITERATIONS rounds; before a model has answered an
# iteration may try primary + failover; one continuation repeats an iteration. Worst case per
# turn: 2 (first iteration) + 2 (its continuation) or 2 + 1 + 1 + 1 = 5.
CALLS_PER_TURN = {"min": 1, "typical": 2, "max": 5}
SEGMENT_FLUSH_ROUNDS = 5


class BenchError(Exception):
    pass


# --- spend guard ------------------------------------------------------------------------------

def load_prices(path):
    """{"source", "verified_on", "currency": "USD", "reserve_usd_per_call",
    "models": {model: {"input_per_mtok", "cached_input_per_mtok", "output_per_mtok"}}}."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchError(f"prices file unreadable ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not data.get("source") or not data.get("verified_on"):
        raise BenchError("prices file needs 'source' and 'verified_on'")
    if data.get("currency") != "USD":
        raise BenchError("prices file currency must be USD")
    models = data.get("models")
    keys = ("input_per_mtok", "cached_input_per_mtok", "output_per_mtok")
    if not isinstance(models, dict) or not models or not all(
            isinstance(m, dict) and all(_price(m.get(k)) is not None for k in keys)
            for m in models.values()):
        raise BenchError("prices file needs models with " + ", ".join(keys))
    if _price(data.get("reserve_usd_per_call")) in (None, Decimal(0)):
        raise BenchError("prices file needs a positive reserve_usd_per_call")
    return data


def _price(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number >= 0 and not isinstance(value, bool) else None


def call_cost(prices, model, usage):
    """Estimated USD of one attempt from its reported tokens, or None when it cannot be
    priced (unknown model, a token count not reported). Thinking tokens are billed as output
    (assumption, recorded in the report)."""
    rates = (prices or {}).get("models", {}).get(model)
    if rates is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    cached = usage.cached_input_tokens or 0
    output = usage.output_tokens + (usage.thinking_tokens or 0)
    million = Decimal(1_000_000)
    return ((usage.input_tokens - cached) * _price(rates["input_per_mtok"])
            + cached * _price(rates["cached_input_per_mtok"])
            + output * _price(rates["output_per_mtok"])) / million


class SpendCapReached(ProviderError):
    def __init__(self, reason):
        super().__init__("spend_cap", failover=False)
        self.reason = reason


class SpendGuard:
    """Checks the caps before every provider request. Thread-safe; in-flight reservations
    count against the cap, so concurrent requests cannot together cross it.

    The request cap is hard. The currency cap is an estimate: Gemini is called without an
    output-token limit, so one request can cost more than its reservation, and an attempt
    whose tokens are not reported is charged its reservation. Never call it guaranteed."""

    def __init__(self, *, max_calls, cap_usd=None, prices=None):
        if max_calls is None or max_calls < 1:
            raise BenchError("a positive request cap is required")
        self.max_calls, self.cap_usd, self.prices = max_calls, cap_usd, prices
        self._lock = threading.Lock()
        self.started = 0
        self.in_flight = Decimal(0)
        self.spent = Decimal(0)
        self.unpriced = 0
        self.largest = Decimal(0)
        self.stopped = None

    def _reserve_size(self):
        if self.cap_usd is None:
            return Decimal(0)
        return max(_price(self.prices["reserve_usd_per_call"]), self.largest)

    def reserve(self):
        with self._lock:
            if self.stopped:
                raise SpendCapReached(self.stopped)
            if self.started >= self.max_calls:
                self.stopped = "max_calls"
                raise SpendCapReached(self.stopped)
            size = self._reserve_size()
            if (self.cap_usd is not None
                    and self.spent + self.in_flight + size > self.cap_usd):
                self.stopped = "spend_cap_usd"
                raise SpendCapReached(self.stopped)
            self.started += 1
            self.in_flight += size
            return size

    def settle(self, size, model, usage):
        with self._lock:
            self.in_flight -= size
            if self.cap_usd is None:
                return None
            cost = call_cost(self.prices, model, usage)
            if cost is None:
                self.unpriced += 1
                charged = size
            else:
                charged = cost
                self.largest = max(self.largest, cost)
            self.spent += charged
            return cost

    def summary(self):
        return {"requests_started": self.started, "max_calls": self.max_calls,
                "cap_usd": str(self.cap_usd) if self.cap_usd is not None else None,
                "estimated_spent_usd": str(self.spent) if self.cap_usd is not None else None,
                "unpriced_requests": self.unpriced, "stopped": self.stopped,
                "currency_cap_guaranteed": False,
                "note": ("request cap is hard; the currency figure is an estimate from "
                         "reported tokens and the prices file (no output-token limit is "
                         "sent; unreported tokens are charged the reservation)")}


# --- providers --------------------------------------------------------------------------------

class MeteredProvider:
    """The GeminiClient interface (generate) with a record per request and the spend guard in
    front. `where` names the case and turn the next requests belong to."""

    def __init__(self, client, guard=None, clock=time.monotonic):
        self.client, self.guard, self._clock = client, guard, clock
        self.attempts = []
        self.where = {}

    def generate(self, model, *, system, tools, contents, timeout_s, thinking_level=None):
        record = {**self.where, "n": len(self.attempts) + 1, "model": model,
                  "tool_results_seen": _tool_results(contents)}
        self.attempts.append(record)
        size = Decimal(0)
        if self.guard is not None:
            try:
                size = self.guard.reserve()
            except SpendCapReached as exc:
                record.update(outcome="not_sent", detail=f"cap:{exc.reason}")
                raise
        began = self._clock()
        try:
            response = self.client.generate(model, system=system, tools=tools,
                                            contents=contents, timeout_s=timeout_s,
                                            thinking_level=thinking_level)
        except BaseException as exc:
            # a failed request may still be billed: it is charged its reservation
            record.update(outcome="failed",
                          detail=exc.code if isinstance(exc, ProviderError) else "exception")
            self._settle(record, began, size, model, llm.Usage())
            raise
        record.update(outcome="succeeded", detail=response.detail,
                      model_version=response.model_version,
                      tool_calls=[c.name if isinstance(c.name, str) else "invalid"
                                  for c in response.tool_calls],
                      usage=_usage(response.usage))
        self._settle(record, began, size, model, response.usage)
        return response

    def _settle(self, record, began, size, model, usage):
        record["latency_ms"] = int((self._clock() - began) * 1000)
        if self.guard is not None:
            cost = self.guard.settle(size, model, usage)
            record["estimated_cost_usd"] = str(cost) if cost is not None else None


def _usage(usage):
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "thinking_tokens": usage.thinking_tokens}


def _tool_results(contents):
    """Name and status / error of each tool result the request carries back to the model
    (no payload values)."""
    seen = []
    last = contents[-1] if contents else {}
    for part in last.get("parts", []) if isinstance(last, dict) else []:
        response = part.get("functionResponse") if isinstance(part, dict) else None
        if isinstance(response, dict):
            body = response.get("response") or {}
            seen.append({"name": response.get("name"),
                         "status": body.get("status"), "error": body.get("error")})
    return seen


class ScriptedClient:
    """Dry-run model: per turn, a queue of tool-call rounds, then a plain answer. It goes
    through app.llm.parse_response like a real body; no request leaves the process."""

    def __init__(self):
        self.rounds = []

    def script(self, rounds):
        self.rounds = list(rounds)

    def generate(self, model, *, system, tools, contents, timeout_s, thinking_level=None):
        calls = self.rounds.pop(0) if self.rounds else []
        parts = ([{"functionCall": {"name": name, "args": args, "id": f"call-{n}"},
                   "thoughtSignature": "dry-run"} for n, (name, args) in enumerate(calls)]
                 or [{"text": "dry-run"}])
        return llm.parse_response({
            "candidates": [{"content": {"role": "model", "parts": parts},
                            "finishReason": "STOP"}],
            "modelVersion": "dry-run"})


def gold_rounds(item):
    """The text item's gold tool calls as one round. Gold args state intent; the one shape
    that differs from the declared schema (photos {"ref": {"position": n}}) is adapted."""
    calls = []
    for call in item["expected_tools"]:
        args = dict(call.get("args") or {})
        if call["name"] == "send_property_photos" and isinstance(args.get("ref"), dict):
            args = dict(args.pop("ref"), **args)
        calls.append((call["name"], args))
    return [calls] if calls else []


def span_rounds(spec):
    """A staff item's hand-written span calls, one per round (the model sees find_listings'
    result before it proposes, as in a live turn)."""
    return [[(call["tool"], call["args"])] for call in spec["calls"]]


# --- the fake WhatsApp channel and storage ----------------------------------------------------

class FakeWhatsApp:
    """The WhatsAppClient interface; records payloads with their time. No network."""

    def __init__(self, clock=time.monotonic):
        self.sent, self.media, self._clock = [], {}, clock

    def send(self, to, payload, *, reply_to=None):
        self.sent.append((self._clock(), payload))
        return f"wamid.BENCH{len(self.sent)}"

    def mark_read(self, wamid, *, typing=False):
        pass

    def fetch_media(self, media_id, max_bytes):
        return self.media[media_id], "audio/ogg"

    def close(self):
        pass


class MemoryStorage:
    def __init__(self):
        self.objects = {}

    def put(self, key, data, content_type, cache_control=None):
        self.objects[key] = data

    def get(self, key):
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)

    def presigned_url(self, key, seconds):
        return f"https://bench.invalid/{key}"


# --- disposable environment -------------------------------------------------------------------

def check_disposable(url):
    """Same rule as tests/conftest.py: only the dedicated loopback manzil_schema_test DB."""
    from psycopg.conninfo import conninfo_to_dict
    try:
        options = conninfo_to_dict(url or "")
    except Exception:  # noqa: BLE001 - never echo the URL
        raise BenchError("database URL unreadable") from None
    if options.get("host") not in {"localhost", "127.0.0.1", "::1"} or (
            options.get("dbname") != "manzil_schema_test"):
        raise BenchError("benchmarks run only on the dedicated loopback manzil_schema_test DB")


@contextlib.contextmanager
def environment(database_url, provider, *, primary, failover, thinking_level, speech=None,
                media=0):
    """A fresh migrated schema, the restricted login, the demo agency seeded, and the real
    worker with the engine on `provider`. Dropped afterwards, whatever happens. media: that
    many fictional photos per listing, with a cached WhatsApp id (sent by id, never uploaded),
    inserted by the schema owner like the seed's own rows (tests/test_customer_flow_db.py)."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from app import gazetteer, migrate, requirements
    from app.handlers import make_handlers
    from app.provision_agency import create_agency
    from app.turn_engine import TurnEngine
    from app.worker import Worker

    check_disposable(database_url)
    # migration_test_ prefix: the documented orphan clean-up (drop migration_test_%) covers it
    schema = "migration_test_bench_" + uuid.uuid4().hex[:12]
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin_url = make_conninfo(database_url, options=f"-c search_path={schema}")
    conn = worker = None
    try:
        migrate.apply_migrations(admin_url)
        password = secrets.token_urlsafe(32)
        with psycopg.connect(admin_url, autocommit=True) as admin:
            verifier = admin.pgconn.encrypt_password(password.encode(), b"manzil_app",
                                                     b"scram-sha-256")
            admin.execute(sql.SQL("ALTER ROLE manzil_app PASSWORD {}").format(
                sql.Literal(verifier.decode("ascii"))))
        app_url = make_conninfo(admin_url, user="manzil_app", password=password)
        conn = psycopg.connect(app_url, autocommit=True)
        agency = create_agency(conn, seed.DEMO_AGENCY_NAME, "demo", PNID)
        result = seed.run(agency, admin_url, app_url, STAFF_ENV, apply=True)
        if result.conflicts:
            raise BenchError("seed conflicts in a fresh schema")
        with psycopg.connect(admin_url, autocommit=True) as admin:
            ids, _ = seed.resolve_locations(admin, seed.load_dataset())
            if media:
                admin.execute(
                    "INSERT INTO property_media (agency_id, property_id, original_key, "
                    "wa_image_key, sort_order, wa_media_id, wa_media_expires_at) "
                    "SELECT p.agency_id, p.id, 'bench/' || p.listing_code || '-' || n || '.jpg', "
                    "'bench/' || p.listing_code || '-' || n || '-wa.jpg', n, "
                    "'bench-' || p.listing_code || '-' || n, now() + interval '25 days' "
                    "FROM property p, generate_series(0, %s - 1) n WHERE p.agency_id = %s",
                    (media, agency))
        cache = gazetteer.GazetteerCache.from_database_url(app_url)
        cache.load()
        service = requirements.RequirementService(cache)
        engine = TurnEngine(provider, primary, failover, requirements=service,
                            thinking_level=thinking_level)
        client, storage = FakeWhatsApp(), MemoryStorage()
        settings = SimpleNamespace(meta_access_token="unused", meta_api_version="v25.0",
                                   s3_bucket=None, site_address="bench.invalid")
        handlers = make_handlers(settings, client_factory=lambda pnid: client, engine=engine,
                                 speech_service=speech, storage=storage)
        worker = Worker(app_url, [agency], handlers, threading.Event(), backoff=lambda n: 0)
        yield Env(conn, agency, worker, client, {str(v): k for k, v in ids.items()}, media)
    finally:
        if worker is not None:
            worker.close()
        if conn is not None:
            conn.close()
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)))
            admin.execute("ALTER ROLE manzil_app PASSWORD NULL")


@dataclass
class Env:
    conn: object
    agency: object
    worker: object
    client: FakeWhatsApp
    location_keys: dict  # location uuid (str) -> demo location key
    media: int = 0
    _n: list = field(default_factory=lambda: [0])

    def query(self, sql_text, params=()):
        from app.db import tenant_transaction
        with tenant_transaction(self.conn, self.agency):
            return self.conn.execute(sql_text, params).fetchall()

    def message_id(self):
        self._n[0] += 1
        return f"wamid.BENCHIN{self._n[0]}"

    def deliver(self, message):
        from app.db import tenant_transaction
        from app.ingest import store_event
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": PNID}, "messages": [message]}}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(self.conn, self.agency):
            for event in events:
                store_event(self.conn, self.agency, event)

    def drain(self):
        while self.worker.run_once():
            pass

    def flush_segments(self):
        """The 7.4 quiet window and media waits, skipped (as tests/test_turn_engine.py does)."""
        for _ in range(SEGMENT_FLUSH_ROUNDS):
            from app.db import tenant_transaction
            with tenant_transaction(self.conn, self.agency):
                self.conn.execute("UPDATE job SET available_at = now() WHERE status = 'pending' "
                                  "AND job_type LIKE 'staff_segment_%'")
            self.drain()

    def text(self, sender, body, quoted=None):
        message = {"from": sender, "id": self.message_id(), "timestamp": str(int(time.time())),
                   "type": "text", "text": {"body": body}}
        if quoted:
            message["context"] = {"id": quoted}  # a swipe-reply to that card
        return message

    def card_wamid(self, code):
        """The WhatsApp id of the latest card shown for `code` (what a quote carries)."""
        rows = self.query(
            "SELECT o.provider_message_id FROM shown_item s "
            "JOIN outbound_message o ON o.message_id = s.card_message_id "
            "JOIN property p ON p.id = s.property_id WHERE p.listing_code = %s "
            "ORDER BY s.shown_at DESC LIMIT 1", (code,))
        return rows[0][0] if rows else None

    def image_listings(self, payloads):
        """[(media id, listing code or None)] for every image payload."""
        ids = [p["image"].get("id") for p in payloads if p.get("type") == "image"]
        found = dict(self.query(
            "SELECT pm.wa_media_id, p.listing_code FROM property_media pm "
            "JOIN property p ON p.id = pm.property_id WHERE pm.wa_media_id = ANY(%s)",
            (ids,))) if ids else {}
        return [(i, found.get(i)) for i in ids]

    def voice(self, sender, data):
        media_id = f"bench-audio-{self._n[0] + 1}"
        self.client.media[media_id] = data
        return {"from": sender, "id": self.message_id(), "timestamp": str(int(time.time())),
                "type": "audio", "audio": {"id": media_id, "mime_type": "audio/ogg; codecs=opus",
                                           "voice": True}}

    def counts(self):
        tables = ("search_run", "pending_clarification", "requirement_patch_log",
                  "listing_draft", "usage_event")
        return {t: self.query(f"SELECT count(*) FROM {t}")[0][0] for t in tables}

    def property_fingerprint(self):
        return self.query("SELECT count(*), COALESCE(sum(version), 0), "
                          "md5(COALESCE(string_agg(listing_code || status || price_pkr::text, "
                          "',' ORDER BY listing_code), '')) FROM property")[0]

    def run_turn(self, message, *, staff=False):
        """Deliver one message and process it; (payloads, latency_ms or None)."""
        before = len(self.client.sent)
        start = time.monotonic()
        self.deliver(message)
        self.drain()
        if staff and len(self.client.sent) == before:
            # a segment turn: timed from the end of the (skipped) quiet window; an immediate
            # staff turn was already answered by the drain above and keeps its own start
            start = time.monotonic()
            self.flush_segments()
        sent = self.client.sent[before:]
        latency = int((sent[0][0] - start) * 1000) if sent else None
        return [payload for _, payload in sent], latency


# --- scoring helpers --------------------------------------------------------------------------

def texts_of(payloads):
    from app import grounding
    return [t for payload in payloads for t in grounding.payload_texts(payload)]


def row_ids(payload):
    action = payload.get("interactive", {}).get("action", {}) if isinstance(payload, dict) else {}
    return ([b["reply"]["id"] for b in action.get("buttons", [])]
            + [r["id"] for s in action.get("sections", []) for r in s.get("rows", [])])


def asked_question(payloads):
    """The question keys whose approved wording appears in the reply (the turn engine's notices
    and the 8.1 search gate's questions in app.render), plus button questions."""
    from app import render
    from app.turn_engine import TEMPLATES, TEMPLATES_EN
    said = "\n".join(texts_of(payloads))
    found = [key for key in QUESTION_KEYS
             for template in (TEMPLATES.get(key), TEMPLATES_EN.get(key))
             if template and _fragments_in(template, said)]
    found += [f"search_gate:{key}" for key in RENDER_QUESTION_KEYS
              for words in render.TEXT.values()
              if words.get(key) and _fragments_in(words[key], said)]
    buttons = [i for p in payloads for i in row_ids(p)
               if i.startswith(("clarify:", "echo:"))]
    return sorted(set(found)), buttons


def _fragments_in(template, said):
    """Every literal part (>= 4 characters) of an approved template is in `said` (a bare
    prefix such as "Budget" would also match the button title "Budget barhayein")."""
    parts = [part.strip() for part in re.split(r"\{[^}]*\}", template)]
    parts = [part for part in parts if len(part) >= 4]
    return bool(parts) and all(part in said for part in parts)


def _plain(value):
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _same(gold, got):
    if isinstance(gold, list) and isinstance(got, list):
        return sorted(map(str, gold)) == sorted(map(str, got))
    return _plain(gold) == _plain(got)


def score_slot(name, gold, slots, location_keys):
    """{gold, got, ok} for one gold_extraction slot against requirement_state.slots."""
    entry = slots.get(name) or {}
    value = entry.get("value")
    if name == "location":
        nodes = (value or {}).get("nodes") if isinstance(value, dict) else None
        got = sorted(location_keys.get(str(n), str(n)) for n in nodes or [])
        if gold.get("status", "RESOLVED") != "RESOLVED":
            return {"gold": {"status": gold["status"]}, "got": got, "ok": not got}
        return {"gold": sorted(gold["value"]), "got": got, "ok": got == sorted(gold["value"])}
    if name == "purpose":
        return {"gold": gold["value"], "got": value, "ok": value == gold["value"]}
    if name == "property_types":
        got = value if isinstance(value, list) else None
        return {"gold": gold["value"], "got": got, "ok": _same(gold["value"], got or [])}
    if name in ("budget", "size"):
        keys = (("min", "max", "mode", "period") if name == "budget" else ("value", "unit"))
        want = {k: gold.get(k) for k in keys if k in gold}
        got = {k: (value or {}).get(k) for k in want} if isinstance(value, dict) else None
        ok = got is not None and all(_same(want[k], got[k]) for k in want)
        return {"gold": want, "got": got, "ok": ok}
    if name == "investment" and isinstance(value, dict):
        value = value.get("intent")  # the state keeps {intent, tags}
    want = gold.get("value")
    return {"gold": want, "got": value, "ok": _same(want, value)}


# --- text runner ------------------------------------------------------------------------------

def text_groups(items):
    """Case groups in run order: a follow-up joins the group of the item it runs after, so the
    whole chain shares one conversation; every other item is its own group (fresh schema)."""
    by_id = {i["id"]: i for i in items}
    groups, owner = [], {}
    for item in items:
        after = (item.get("context") or {}).get("after")
        if after:
            if after not in by_id:
                raise BenchError(f"{item['id']}: context.after {after} is not in the set")
            owner[item["id"]] = owner.get(after, after)
    heads = [i["id"] for i in items if i["id"] not in owner]
    for head in heads:
        chain = [head]
        while True:
            nxt = next((i["id"] for i in items
                        if (i.get("context") or {}).get("after") == chain[-1]), None)
            if nxt is None:
                break
            chain.append(nxt)
        groups.append(chain)
    return groups


def reply_flags(payloads):
    """What the customer actually got: visible payloads, the honest no-match wording, and
    which inappropriate templates (failure, refusal, default prompt, stubs) it contains."""
    from app import customer_reply, render
    from app.turn_engine import TEMPLATES, TEMPLATES_EN
    visible = customer_reply.visible(payloads)
    said = "\n".join(texts_of(visible))
    bad = sorted({key for key in INAPPROPRIATE_REPLIES
                  for template in (TEMPLATES.get(key), TEMPLATES_EN.get(key))
                  if template and _fragments_in(template, said)})
    no_match = any(_fragments_in(words[key], said) for words in render.TEXT.values()
                   for key in NO_MATCH_WORDING if words.get(key))
    return {"visible": len(visible), "no_match_wording": no_match, "inappropriate": bad}


def score_photos(gold, images, earlier):
    """Demo Scope p7: images sent belong to the referenced property / repeats. A repeat is an
    image already sent in this conversation or sent twice in this reply."""
    listings = [code for _, code in images]
    seen, repeats = set(earlier), []
    for media_id, _ in images:
        if media_id in seen:
            repeats.append(media_id)
        seen.add(media_id)
    return {"images": len(images), "image_listings": sorted(set(map(str, listings))),
            "repeats": repeats,
            "ok": bool(images) and all(c == gold["listing"] for c in listings) and not repeats}


def score_text_turn(item, payloads, slots, before, after, shown, location_keys, statuses,
                    photos=None):
    gold = item["expected_outcome"]
    golds = dict(item.get("gold_extraction") or {})
    merged = gold.get("search") or {}
    if item.get("context") and "location" in golds and merged.get("locations"):
        # a follow-up's state is the merged requirement: its location gold is the merged search
        golds["location"] = dict(golds["location"], value=merged["locations"])
    extraction = {name: score_slot(name, g, slots, location_keys) for name, g in golds.items()}
    critical = [n for n in CRITICAL if n in extraction]
    searched = after["search_run"] > before["search_run"]
    hidden = sorted(c for c in shown if statuses.get(c) in HIDDEN)
    asked, buttons = asked_question(payloads)
    said = "\n".join(texts_of(payloads))
    kind = gold["type"]
    flags = reply_flags(payloads)
    e2e = {"type": kind, "searched": searched, "shown": shown, "hidden_shown": hidden,
           "asked": asked, "question_buttons": buttons, "reply_flags": flags,
           "new_clarifications": after["pending_clarification"] - before["pending_clarification"]}
    answered = flags["visible"] > 0 and not flags["inappropriate"]
    if kind == "results":
        want, allowed = set(gold["ids"]), set(gold.get("allowed", []))
        off = sorted(set(shown) - want - allowed)
        e2e.update(missing=sorted(want - set(shown)), off_criteria=off)
        ok = searched and want <= set(shown) and not off
    elif kind == "no_match":  # the customer must be told, honestly, that nothing fits
        ok = searched and not shown and answered and flags["no_match_wording"]
    elif kind == "clarify":  # a question the customer can see; a pending row alone is not one
        ok = not searched and answered and bool(asked or buttons)
    elif kind == "photos" and photos is not None:  # media fixture: the images themselves
        e2e["photos"] = photos
        ok = photos["ok"]
    elif kind == "photos":  # no photos in the schema: the reply must name the listing
        ok = gold["listing"] in said
    elif kind == "handoff":
        from app.turn_engine import TEMPLATES
        e2e["product_stub"] = TEMPLATES["handoff_unavailable"] in said
        if e2e["product_stub"]:
            e2e["blocked_by"] = BLOCKED["handoff"]
        ok = not e2e["product_stub"] and bool(asked or buttons)
    else:
        ok = False
    return {"extraction": extraction,
            "critical_exact": (all(extraction[n]["ok"] for n in critical) if critical else None),
            "budget_exact": extraction["budget"]["ok"] if "budget" in extraction else None,
            "end_to_end": {**e2e, "ok": bool(ok and not hidden)}}


def run_text(ctx, items):
    out, ordinal = [], 0
    by_id = {i["id"]: i for i in items}
    for group in text_groups(items):
        if ctx.stopped():
            out += [ctx.not_run(by_id[i], "stopped_by_cap") for i in group]
            continue
        ordinal += 1
        sender = f"{CUSTOMER_PREFIX}{ordinal:04d}"
        sent_media = set()  # every image id sent in this conversation (repeat check)
        with ctx.env(media=ctx.media) as env:
            statuses = {r[0]: r[1] for r in env.query("SELECT listing_code, status FROM property")}
            for turn, item_id in enumerate(group, 1):
                item = by_id[item_id]
                if ctx.stopped():
                    out.append(ctx.not_run(item, "stopped_by_cap"))
                    continue
                context_shown = (item.get("context") or {}).get("shown")
                prior = _latest_shown(env, sender) if context_shown else None
                quoted = (item.get("quote") or {}).get("listing")
                wamid = env.card_wamid(quoted) if quoted else None
                ctx.script(gold_rounds(item))
                record = ctx.begin(item, group=group, turn=turn)
                if quoted:
                    record["quote"] = {"listing": quoted, "card_found": wamid is not None}
                before, runs_before = env.counts(), set(_search_runs(env))
                payloads, latency = env.run_turn(env.text(sender, item["input"], wamid))
                after = env.counts()
                slots = _slots(env, sender)
                new_runs = [r for r in _search_runs(env) if r not in runs_before]
                shown = _shown(env, new_runs[-1]) if new_runs else []  # this turn's search only
                photos = None
                if env.media and item["expected_outcome"]["type"] == "photos":
                    images = env.image_listings(payloads)
                    photos = score_photos(item["expected_outcome"], images, sent_media)
                    sent_media |= {media_id for media_id, _ in images}
                    position = _position(item)
                    if position and prior:  # the target as the customer saw the list
                        photos["displayed_target"] = (prior[position - 1]
                                                      if position <= len(prior) else None)
                record.update(score_text_turn(item, payloads, slots, before, after, shown,
                                              env.location_keys, statuses, photos))
                record.update(ctx.finish(env, latency, payloads, before, after))
                if context_shown is not None:
                    # the order matters only to an item that names a position
                    same = (prior == context_shown if _position(item)
                            else sorted(prior) == sorted(context_shown))
                    record["context"] = {"expected_shown": context_shown, "had_shown": prior,
                                         "order_checked": bool(_position(item)),
                                         "as_designed": same}
                out.append(record)
    return out


def _position(item):
    for call in item.get("expected_tools", []):
        args = call.get("args") or {}
        position = args.get("position") or (args.get("ref") or {}).get("position")
        if call["name"] == "send_property_photos" and position:
            return position
    return None


def _conversation(env, sender):
    rows = env.query("SELECT id FROM conversation WHERE customer_wa_id = %s", (sender,))
    return rows[0][0] if rows else None


def _slots(env, sender):
    conv = _conversation(env, sender)
    rows = env.query("SELECT slots FROM requirement_state WHERE conversation_id = %s",
                     (conv,)) if conv else []
    return rows[0][0] if rows else {}


def _search_runs(env):
    return [r[0] for r in env.query("SELECT id FROM search_run ORDER BY created_at, id")]


def _shown(env, search_run_id):
    return [r[0] for r in env.query(
        "SELECT p.listing_code FROM shown_item s JOIN property p ON p.id = s.property_id "
        "WHERE s.search_run_id = %s ORDER BY s.position", (search_run_id,))]


def _latest_shown(env, sender):
    conv = _conversation(env, sender)
    if conv is None:
        return []
    return [r[0] for r in env.query(
        "SELECT p.listing_code FROM shown_item s JOIN property p ON p.id = s.property_id "
        "WHERE s.search_run_id = (SELECT search_run_id FROM shown_item WHERE conversation_id "
        "= %s ORDER BY shown_at DESC LIMIT 1) ORDER BY s.position", (conv,))]


def status_lists(records):
    """Items never started, and items whose turn a cap cut short (both count as incorrect)."""
    return {"not_run": [r["id"] for r in records if r["status"] == "not_run"],
            "stopped_by_cap": [r["id"] for r in records if r["status"] == "stopped_by_cap"]}


def _scope_fields(item):
    keys = ("scope_role", "follow_up_kind", "metrics", "gold_status")
    return {k: item[k] for k in keys if k in item}


def gold_flags(item):
    golds = item.get("gold_extraction") or {}
    return {"has_critical": any(n in golds for n in CRITICAL), "has_budget": "budget" in golds}


def text_summary(records):
    """Every rate keeps NOT_RUN items in its denominator as incorrect."""
    scored = [r for r in records if r["status"] == "ran"]

    def ran(r, key):
        return r.get(key) if r["status"] == "ran" else False

    def rate(values):
        values = list(values)
        return {"correct": sum(bool(v) for v in values), "of": len(values)}

    def e2e_ok(r):
        return r["status"] == "ran" and r["end_to_end"]["ok"]

    def asked(r):
        return r["status"] == "ran" and bool(r["end_to_end"]["asked"]
                                             or r["end_to_end"]["question_buttons"])

    clarify = [r for r in records if r["gold_type"] == "clarify"]
    clear = [r for r in records if r["gold_type"] in ("results", "no_match")]
    handoff = [r for r in records if r["gold_type"] == "handoff"]
    return {
        "denominator": len(records),
        **status_lists(records),
        "extraction_critical_exact": rate(ran(r, "critical_exact") for r in records
                                          if r["has_critical"]),
        "price_normalisation_exact": rate(ran(r, "budget_exact") for r in records
                                          if r["has_budget"]),
        "tool_selection_exact": rate(ran(r, "tools_ok") for r in records),
        "end_to_end_exact": rate(e2e_ok(r) for r in records),
        "blocked": [{"id": r["id"], "by": r["end_to_end"]["blocked_by"],
                     "counted_as": "incorrect in end_to_end_exact (denominator kept)"}
                    for r in scored if r["end_to_end"].get("blocked_by")],
        "end_to_end_exact_excluding_blocked": rate(
            e2e_ok(r) for r in records
            if not (r["status"] == "ran" and r["end_to_end"].get("blocked_by"))),
        "clarify_on_designed_ambiguous": rate(e2e_ok(r) for r in clarify),
        "unnecessary_questions_on_clear_items": {"asked": sum(map(asked, clear)),
                                                 "of": len(clear)},
        "handoff_recall": rate(r["status"] == "ran" and "request_handoff" in r["tools"]
                               for r in handoff),
        "false_handoffs": sum(r["status"] == "ran" and r["gold_type"] != "handoff"
                              and "request_handoff" in r["tools"] for r in records),
        "hidden_listings_shown": sum(len(r["end_to_end"]["hidden_shown"]) for r in scored),
        "context_deviations": [r["id"] for r in scored
                               if r.get("context") and not r["context"]["as_designed"]],
        "latency_ms": _latency(scored),
    }


def _latency(records):
    values = sorted(r["latency_ms"] for r in records if r.get("latency_ms") is not None)
    return {"n": len(values), "p50": stt_bench.percentile(values, 50),
            "p90": stt_bench.percentile(values, 90),
            "boundary": "inbound stored -> first payload to the fake WhatsApp client"}


# --- staff runner -----------------------------------------------------------------------------

def run_staff(ctx, items, spans):
    out = []
    for item in items:
        if ctx.stopped():
            out.append(ctx.not_run(item, "stopped_by_cap"))
            continue
        with ctx.env() as env:
            ctx.script(span_rounds(spans[item["id"]]))
            record = ctx.begin(item, group=[item["id"]], turn=1)
            fingerprint = env.property_fingerprint()
            before = env.counts()
            payloads, latency = env.run_turn(env.text(STAFF_SENDER, item["input"]), staff=True)
            after = env.counts()
            record.update(score_staff_item(item, env, payloads))
            record["listing_writes"] = int(env.property_fingerprint() != fingerprint)
            record.update(ctx.finish(env, latency, payloads, before, after))
            out.append(record)
    return out


def score_staff_item(item, env, payloads):
    """The staff_bench critical fields from what the pipeline stored and sent."""
    kind = item["expected_outcome"]["type"]
    rows = env.query("SELECT kind, status, payload FROM listing_draft ORDER BY ordinal")
    creates = [r[2] for r in rows if r[0] == "create"]
    updates = [r[2] for r in rows if r[0] == "update"]
    if kind == "draft":
        if not creates:
            return staff_bench.failed_item(item, "no_draft")
        return {"kind": kind, **staff_bench.score_draft(
            item["expected_outcome"], creates[0],
            location_key=lambda lid: env.location_keys.get(str(lid), lid))}
    proposal = ({"code": updates[0]["target_code"],
                 "changes": {f: int(_plain(c["to"])) if f == "price_pkr" else c["to"]
                             for f, c in updates[0]["changes"].items()}} if updates else None)
    picks = [(code, enc) for p in payloads for i in row_ids(p)
             if i.startswith("pick:") for code, enc in [i.split(":")[1:3]]]
    from app import staff_actions
    pending = staff_actions.decode_changes(picks[0][1]) if picks else None
    return {"kind": kind, **staff_bench.score_update(
        item["expected_outcome"], proposal, [c for c, _ in picks], pending,
        failure=None if (proposal or picks) else "no_proposal_or_list")}


# --- controls runner --------------------------------------------------------------------------

def run_controls(ctx, items, transcripts, audio_dir):
    """Each control as a customer voice note through app.speech (the recorded STT results
    replayed behind the provider interface: no STT request) and the turn engine. Zero
    hallucinated requirements = no requirement patch, no requirement slot, no search."""
    from app import speech as speech_module
    out = []
    for n, item in enumerate(items, 1):
        pair = transcripts.get(item["id"])
        path = Path(audio_dir) / f"{item['id']}.ogg" if audio_dir else None
        if pair is None or path is None or not path.is_file():
            out.append(ctx.not_run(item, "no_recording_or_stt_result"))
            continue
        if ctx.stopped():
            out.append(ctx.not_run(item, "stopped_by_cap"))
            continue
        service = speech_module.SpeechService(
            stt_bench._Replay("elevenlabs", stt_bench.SCRIBE_MODEL, *pair["scribe"]),
            MemoryStorage(), fallback=stt_bench._Replay("gemini", "recorded", *pair["gemini"]),
            fallback_model="recorded")
        with ctx.env(speech=service) as env:
            ctx.script([])
            record = ctx.begin(item, group=[item["id"]], turn=1)
            sender = f"{CUSTOMER_PREFIX}9{n:03d}"
            before = env.counts()
            payloads, latency = env.run_turn(env.voice(sender, path.read_bytes()))
            after = env.counts()
            slots = _slots(env, sender)
            stt = env.query("SELECT stt_meta->>'status' FROM message WHERE message_type = 'audio'")
            created = {
                "patches": after["requirement_patch_log"] - before["requirement_patch_log"],
                "slots": sorted(slots), "searches": after["search_run"] - before["search_run"]}
            llm_reached = any(a.get("item") == item["id"] for a in ctx.provider.attempts)
            zero = not created["patches"] and not created["slots"] and not created["searches"]
            verdict = ("PASS" if zero else "FAIL") if ctx.live or not llm_reached else (
                "UNDECIDED" if zero else "FAIL")
            record.update(stt_status=stt[0][0] if stt else None, created=created,
                          llm_reached=llm_reached, zero_requirements=zero, verdict=verdict)
            record.update(ctx.finish(env, latency, payloads, before, after))
            if record["status"] == "stopped_by_cap":
                record["verdict"] = "NOT_SCORED"  # the model step was cut short
            out.append(record)
    return out


def load_transcripts(stt_run):
    """{item_id: {"scribe": (raw, code), "gemini": (raw, code)}} from a stt_bench run dir."""
    run_dir = Path(stt_run)
    try:
        outcomes = json.loads((run_dir / "outcomes.json").read_text("utf-8"))
    except (OSError, ValueError):
        raise BenchError("stt run directory has no readable outcomes.json") from None
    cache = stt_bench.Cache(run_dir / "cache")
    pairs = {}
    for item_id, by_provider in outcomes.items():
        pairs[item_id] = {}
        for name in ("scribe", "gemini"):
            raw, code, _ = stt_bench.load_raw(cache, by_provider.get(name))
            pairs[item_id][name] = (raw, code)
    return pairs


# --- run context ------------------------------------------------------------------------------

class RunContext:
    def __init__(self, args, provider, scripted, *, live):
        self.args, self.provider, self.scripted, self.live = args, provider, scripted, live

    media = 0

    def env(self, speech=None, media=0):
        return environment(self.args.database_url, self.provider, primary=self.args.primary,
                           failover=self.args.failover, thinking_level=self.args.thinking_level,
                           speech=speech, media=media)

    def stopped(self):
        guard = self.provider.guard
        return guard is not None and guard.stopped is not None

    def script(self, rounds):
        if self.scripted is not None:
            self.scripted.script(rounds)

    def begin(self, item, *, group, turn):
        self.provider.where = {"item": item["id"], "turn": turn}
        self._first = len(self.provider.attempts)
        return {"id": item["id"], "status": "ran", "gold_type": item["expected_outcome"]["type"],
                "category": item.get("category"), "language": item.get("language"),
                "group": group, "turn_in_group": turn, **gold_flags(item), **_scope_fields(item),
                "expected_tools": sorted({c["name"] for c in item.get("expected_tools", [])})}

    def finish(self, env, latency, payloads, before, after):
        attempts = self.provider.attempts[self._first:]
        tools = sorted({t for a in attempts for t in a.get("tool_calls", [])})
        metered = after["usage_event"] - before["usage_event"]
        sent = sum(a["outcome"] != "not_sent" for a in attempts)
        result = {"latency_ms": latency, "attempts": attempts, "tools": tools,
                  "provider_requests": sent, "usage_rows": metered,
                  # the engine also meters a refused (cap) attempt as a failed call
                  "metering_consistent": metered == len(attempts),
                  "reply": texts_of(payloads), "payload_types": [p.get("type") for p in payloads]}
        if self.stopped() and any(a["outcome"] == "not_sent" for a in attempts):
            result["status"] = "stopped_by_cap"
        return result

    def not_run(self, item, reason):
        return {"id": item["id"], "status": "not_run", "reason": reason,
                "gold_type": item["expected_outcome"]["type"], "category": item.get("category"),
                "tools": [], **gold_flags(item), **_scope_fields(item)}


def score_tools(records):
    for r in records:
        if r["status"] == "ran":
            r["tools_ok"] = set(r["tools"]) == set(r["expected_tools"])


# --- identity, plan, CLI ----------------------------------------------------------------------

def text_sha256(path):
    """sha256 of a text file with CRLF read as LF: the repository bytes, the same on a Windows
    checkout (core.autocrlf) and on Linux (as tests/test_staff_bench.py pins the gold)."""
    return stt_bench.sha256_bytes(Path(path).read_bytes().replace(b"\r\n", b"\n"))


def identity(args, mode, provider_name, guard):
    files = {name: text_sha256(benchmark.BENCH_DIR / name)
             for name in ("customer_text.json", "scope_conversations.json", "staff.json",
                          "staff_spans.json", "voice_controls.json")}
    sources = {name: text_sha256(Path(__file__).resolve().parent / name)
        for name in ("bench_live.py", "turn_engine.py", "ai_tools.py", "customer_tools.py",
                     "requirements.py", "search.py", "staff_tools.py", "staff_normalise.py",
                     "speech.py", "llm.py")}
    from app import turn_engine
    return {"schema": SCHEMA, "task": TASK, "run_id": uuid.uuid4().hex, "mode": mode,
            "set": args.set, "commit": args.commit, "commit_source": args.commit_source,
            "bench_sha256": files,
            "source_sha256": sources, "demo_sha256": stt_bench.bench_hash(),
            "provider": provider_name,
            "model": None if mode == "dry-run" else {
                "primary": args.primary, "failover": args.failover,
                "thinking_level": args.thinking_level},
            "engine": {"call_timeout_s": turn_engine.CALL_TIMEOUT_SECONDS,
                       "turn_budget_s": turn_engine.TURN_BUDGET_SECONDS,
                       "continuation_s": turn_engine.CONTINUATION_BUDGET_SECONDS,
                       "max_iterations": turn_engine.MAX_ITERATIONS},
            "repeats": args.repeats, "concurrency": 1,
            "media_fixture_photos_per_listing": (MEDIA_FIXTURE_PHOTOS if args.set == "scope"
                                                 else 0),
            "caps": guard.summary() if guard else None,
            "started_at": stt_bench.now_iso()}


def load_set(name):
    if name == "text":
        return json.loads((benchmark.BENCH_DIR / "customer_text.json").read_text("utf-8"))
    if name == "scope":
        text = json.loads((benchmark.BENCH_DIR / "customer_text.json").read_text("utf-8"))
        return benchmark.scope_items(benchmark.load_scope(), text)
    if name == "staff":
        return json.loads((benchmark.BENCH_DIR / "staff.json").read_text("utf-8"))
    return json.loads((benchmark.BENCH_DIR / "voice_controls.json").read_text("utf-8"))


def plan(name):
    items = load_set(name)
    turns = len(items)
    groups = (text_groups(items) if name in ("text", "scope")
              else [[i["id"]] for i in items])
    calls = {k: v * turns for k, v in CALLS_PER_TURN.items()}
    if name == "controls":
        calls["min"] = 0  # a note the speech checks reject never reaches the model
    return {"set": name, "items": turns, "case_groups": len(groups),
            "groups": [g for g in groups if len(g) > 1], "turns": turns,
            "requests_per_repeat": calls,
            "note": "requests per repeat: min / typical / max from the turn engine bounds; "
                    "R16 4.4 asks for 3 repeats of the LLM steps"}


def scope_summary(records):
    """Demo Scope p7 conversation rows. Setup turns are reported, never counted."""
    def ok(r):
        return r["status"] == "ran" and r["end_to_end"]["ok"]

    def rate(rows):
        return {"correct": sum(map(ok, rows)), "of": len(rows)}

    turns = [r for r in records if r.get("scope_role") == "turn"]
    follow = [r for r in turns if "follow_up" in r.get("metrics", [])]
    photos = [r for r in turns if "photos" in r.get("metrics", [])]
    unresolved = [r["id"] for r in turns if r.get("gold_status") == "unresolved"]
    kinds = sorted({r.get("follow_up_kind") for r in follow if r.get("follow_up_kind")})
    return {
        "follow_up_correct_target": rate(follow),
        "follow_up_by_kind": {k: rate([r for r in follow if r.get("follow_up_kind") == k])
                              for k in kinds},
        "follow_up_excluding_unresolved": rate([r for r in follow if r["id"] not in unresolved]),
        "photos_images_belong": rate(photos),
        "photo_repeats": sum(len((r.get("end_to_end", {}).get("photos") or {}).get("repeats", []))
                             for r in photos),
        "unresolved_gold": unresolved,
        "setup": {r["id"]: ok(r) for r in records if r.get("scope_role") == "setup"},
        **status_lists(records),
        "context_deviations": [r["id"] for r in records if r["status"] == "ran"
                               and r.get("context") and not r["context"]["as_designed"]],
        "latency_ms": _latency([r for r in records if r["status"] == "ran"]),
        "media_fixture": f"{MEDIA_FIXTURE_PHOTOS} fictional photos per listing",
    }


def summarise(name, records):
    if name == "text":
        return text_summary(records)
    if name == "scope":
        return scope_summary(records)
    if name == "staff":
        ran = [r for r in records if r["status"] == "ran"]
        # NOT_RUN and cap-stopped items keep every expected check, failed
        by_id = {i["id"]: i for i in load_set("staff")}
        summary = staff_bench.summary([r if r["status"] == "ran" else staff_bench.failed_item(
            by_id[r["id"]], r.get("reason", r["status"])) for r in records])
        summary["denominator"] = len(records)
        summary.update(status_lists(records))
        summary["listing_writes_without_confirmation"] = sum(r.get("listing_writes", 0)
                                                             for r in records)
        summary["tool_selection_exact"] = {"correct": sum(r.get("tools_ok", False) for r in ran),
                                           "of": len(records)}
        summary["latency_ms"] = _latency(ran)
        return summary
    return {"denominator": len(records),
            "verdicts": {v: [r["id"] for r in records if r.get("verdict") == v]
                         for v in ("PASS", "FAIL", "UNDECIDED")},
            **status_lists(records),
            "hallucinated_requirements": sum(not r.get("zero_requirements", True)
                                             for r in records),
            "gate": "R16 5 B: hallucinated content on silence / music controls = 0"}


def select_items(items, only):
    """(items to run, requested ids, filtered-out ids). Unknown or empty --only is refused."""
    every = [i["id"] for i in items]
    if only is None:
        return items, every, []
    wanted = [part.strip() for part in only.split(",") if part.strip()]
    if not wanted:
        raise BenchError("--only names no item")
    unknown = sorted(set(wanted) - set(every))
    if unknown:
        raise BenchError("--only names unknown item(s): " + ", ".join(unknown))
    chosen = [i for i in items if i["id"] in set(wanted)]
    return chosen, [i["id"] for i in chosen], [i for i in every if i not in set(wanted)]


def coverage(set_ids, requested, filtered, records):
    """Which cases were asked for, which completed and which did not. A full-set completion
    needs every case of the set requested and completed (a missing recording or STT result,
    a cap stop or a filtered case all prevent it)."""
    by_id = {r["id"]: r for r in records}
    executed = [i for i in requested if by_id.get(i, {}).get("status") == "ran"]
    missing = [{"id": i, "reason": by_id[i].get("reason", by_id[i]["status"])
                if i in by_id else "no_record"} for i in requested if i not in executed]
    subset_complete = bool(requested) and not missing
    return {"set_size": len(set_ids), "requested": requested, "filtered_out": filtered,
            "executed": executed, "missing": missing, "partial": bool(filtered),
            "subset_complete": subset_complete,
            "full_set_complete": subset_complete and not filtered
            and len(requested) == len(set_ids)}


def execute(args, environ, transport=None):
    """transport: an httpx transport for the Gemini client (tests only)."""
    everything = load_set(args.set)
    items, requested, filtered = select_items(everything, args.only)
    set_ids = [i["id"] for i in everything]
    guard = scripted = None
    if args.mode == "live":
        guard = live_guard(args, environ)
        client = llm.GeminiClient(environ["GEMINI_API_KEY"], transport=transport)
        provider_name = "gemini"
    else:
        scripted = client = ScriptedClient()
        provider_name = "scripted-dry-run"
    provider = MeteredProvider(client, guard)
    ctx = RunContext(args, provider, scripted, live=args.mode == "live")
    ctx.media = MEDIA_FIXTURE_PHOTOS if args.set == "scope" else 0
    run_identity = identity(args, args.mode, provider_name, guard)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    repeats = []
    for repeat in range(1, args.repeats + 1):
        if args.set in ("text", "scope"):
            records = run_text(ctx, items)
        elif args.set == "staff":
            spans = json.loads((benchmark.BENCH_DIR / staff_bench.SPANS_FILE).read_text("utf-8"))
            records = run_staff(ctx, items, spans)
        else:
            records = run_controls(ctx, items, load_transcripts(args.stt_run)
                                   if args.stt_run else {}, args.audio_dir)
        score_tools(records)
        repeats.append({"repeat": repeat, "summary": summarise(args.set, records),
                        "coverage": coverage(set_ids, requested, filtered, records),
                        "items": records})
        stt_bench.write_json_atomic(out_dir / "result.json", {
            "identity": run_identity, "banner": banner(args.mode), "status": "in_progress",
            "repeats_planned": args.repeats, "repeats": repeats,
            "caps": guard.summary() if guard else None})
    covered = [r["coverage"] for r in repeats]
    result = {"identity": run_identity, "banner": banner(args.mode), "status": "finished",
              "repeats_planned": args.repeats, "repeats": repeats,
              "caps": guard.summary() if guard else None,
              "coverage": {
                  "set_size": len(set_ids), "requested": requested, "filtered_out": filtered,
                  "partial": bool(filtered),
                  "subset_complete": all(c["subset_complete"] for c in covered),
                  "full_set_complete": all(c["full_set_complete"] for c in covered),
                  "executed_in_every_repeat": [i for i in requested
                                               if all(i in c["executed"] for c in covered)]}}
    stt_bench.write_json_atomic(out_dir / "result.json", result)
    return result


def resolve_commit(stated):
    """(commit, source). The source file hashes in the identity pin the exact code either way;
    the commit names the base it came from."""
    found = stt_bench.git_commit()
    if stated and found != "unknown" and stated != found:
        raise BenchError("--commit does not match the checkout's HEAD")
    if stated:
        return stated, "stated" if found == "unknown" else "git"
    return found, "git" if found != "unknown" else "unknown"


def banner(mode):
    if mode == "dry-run":
        return ("DRY RUN - a scripted model replayed gold tool calls / hand-written spans "
                "through the real pipeline. Backend ceiling only; NOT a model benchmark.")
    return "LIVE RUN - provider calls were made; see identity.model and caps."


def live_guard(args, environ):
    missing = [name for name, value in (
        ("--confirm-live", args.confirm_live), ("--max-calls", args.max_calls),
        ("--spend-cap-usd", args.spend_cap_usd), ("--prices-file", args.prices_file),
        ("GEMINI_API_KEY", environ.get("GEMINI_API_KEY")),
        ("--primary", args.primary), ("--failover", args.failover),
        ("--commit (git cannot tell)", args.commit != "unknown")) if not value]
    if missing:
        raise BenchError("live mode refused; missing: " + ", ".join(missing))
    prices = load_prices(args.prices_file)
    unpriced = [m for m in (args.primary, args.failover) if m not in prices["models"]]
    if unpriced:
        raise BenchError("live mode refused; no price for model(s): " + ", ".join(unpriced))
    cap = _price(args.spend_cap_usd)
    if cap is None or cap <= 0:
        raise BenchError("--spend-cap-usd must be a positive amount")
    return SpendGuard(max_calls=args.max_calls, cap_usd=cap, prices=prices)


def main(argv=None, environ=None, out=None, transport=None):
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m app.bench_live",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=("plan", "dry-run", "live"))
    parser.add_argument("set", choices=SETS)
    parser.add_argument("--database-url", default=environ.get("TEST_DATABASE_URL"))
    parser.add_argument("--out")
    parser.add_argument("--only", help="comma-separated item ids (a partial run is labelled so)")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--primary", default=environ.get("LLM_PRIMARY_MODEL"))
    parser.add_argument("--failover", default=environ.get("LLM_FAILOVER_MODEL"))
    parser.add_argument("--thinking-level", default=llm.DEFAULT_THINKING_LEVEL,
                        choices=llm.THINKING_LEVELS)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--spend-cap-usd")
    parser.add_argument("--prices-file")
    parser.add_argument("--stt-run", help="controls: a stt_bench run directory")
    parser.add_argument("--audio-dir", help="controls: private <item_id>.ogg recordings")
    parser.add_argument("--commit", default=environ.get("BENCH_GIT_COMMIT"),
                        help="the commit the run is made from, when git cannot tell (a worktree "
                             "mounted into a container); checked against git when both exist")
    args = parser.parse_args(argv)
    try:
        if args.mode == "plan":
            out.write(json.dumps(plan(args.set), indent=2) + "\n")
            return 0
        if not args.out:
            raise BenchError("--out is required")
        if args.repeats < 1:
            raise BenchError("--repeats must be at least 1")
        if args.mode == "dry-run":
            args.primary = args.primary or "dry-run-primary"
            args.failover = args.failover or "dry-run-failover"
        check_disposable(args.database_url)
        args.commit, args.commit_source = resolve_commit(args.commit)
        result = execute(args, environ, transport)
    except BenchError as exc:
        out.write(f"bench_live: {exc}\n")
        return 2
    cover = result["coverage"]
    out.write(result["banner"] + "\n")
    out.write(json.dumps({"coverage": {k: cover[k] for k in (
                              "set_size", "partial", "subset_complete", "full_set_complete")},
                          "missing": [r["coverage"]["missing"] for r in result["repeats"]],
                          "caps": result["caps"],
                          "summaries": [r["summary"] for r in result["repeats"]]},
                         indent=2, default=str) + "\n")
    if not cover["executed_in_every_repeat"]:
        out.write("bench_live: no case was executed (see coverage.missing)\n")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
