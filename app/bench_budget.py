"""Bench-only, durable provider budget. Never used by the deployed service.

Reservations survive crashes. A failed or unmetered request keeps its full bound.
All participants must use the SAME ledger, policy and phase allocation. A ledger
is not an account-level billing limit: unrelated clients are outside its control.
"""

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from app import audio, llm

PHASES = {"tuning": 2_000_000, "formal": 7_000_000, "retry": 1_000_000}


class BudgetError(llm.ProviderError):
    def __init__(self, code="budget_refused"):
        super().__init__(code, failover=False)


def amount(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid monetary amount") from None
    if isinstance(value, bool) or not number.is_finite() or number < 0:
        raise ValueError("invalid monetary amount")
    return number


def micros(usd):
    return int((amount(usd) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def load_policy(path):
    """No bundled rates: runtime model IDs and account pricing must be verified first.

    Rate tiers use the highest applicable rate, not the free tier or a hoped-for
    cache discount. Bounds include output AND thinking. Optional countTokens
    admission checks require verified free status; currency always reserves the
    full documented model context limit.
    """
    data = json.loads(Path(path).read_text("utf-8"))
    if data.get("schema") != 1 or data.get("currency") != "USD":
        raise ValueError("policy requires schema 1 and USD")
    verified = date.fromisoformat(data["verified_on"])
    if not 0 <= (datetime.now(UTC).date() - verified).days <= 14:
        raise ValueError("verify policy rates within the last 14 days")
    if not data.get("source", "").startswith("https://") or data.get("account_verified") is not True:
        raise ValueError("policy needs official source and account verification")
    if not data.get("models"):
        raise ValueError("policy has no verified models")
    for model, rates in data["models"].items():
        if not model or "/" in model or not rates.get("bounds_source", "").startswith("https://"):
            raise ValueError("invalid model or missing bounds source")
        for key in ("input_per_mtok", "cached_input_per_mtok", "output_per_mtok"):
            amount(rates[key])
        if amount(rates["cached_input_per_mtok"]) > amount(rates["input_per_mtok"]):
            raise ValueError("cached rate exceeds input bound")
        for key in ("max_input_tokens", "model_input_token_limit", "max_output_tokens", "max_request_bytes"):
            if type(rates[key]) is not int or rates[key] <= 0:
                raise ValueError("model request bounds must be positive integers")
        if rates["max_input_tokens"] > rates["model_input_token_limit"]:
            raise ValueError("request admission ceiling exceeds model context limit")
        if rates.get("output_includes_thinking") is not True:
            raise ValueError("verify the combined output/thinking bound")
        if type(rates.get("count_before_generate")) is not bool:
            raise ValueError("choose whether to run the optional token-count admission check")
        if rates["count_before_generate"] and rates.get("count_tokens_free") is not True:
            raise ValueError("verify optional token counting is free")
        if rates.get("single_candidate_default_verified") is not True:
            raise ValueError("verify model default is one candidate; candidateCount is not portable")
        if not isinstance(rates.get("thinking_levels"), list) or not rates["thinking_levels"]:
            raise ValueError("verify model-supported thinking levels")
        if type(rates.get("count_margin_tokens")) is not int or rates["count_margin_tokens"] < 0:
            raise ValueError("count_margin_tokens must be nonnegative")
    scribe = data.get("scribe")
    if scribe:
        for key in ("per_hour", "keyterms_per_hour"):
            amount(scribe[key])
        if not scribe.get("source", "").startswith("https://"):
            raise ValueError("Scribe needs an official pricing source")
        for key in ("minimum_seconds", "round_to_seconds"):
            if type(scribe[key]) is not int or scribe[key] <= 0:
                raise ValueError("verify Scribe billing minimum and rounding")
    return data


class Ledger:
    def __init__(self, path, policy):
        self.path, self.policy = str(path), policy
        self.stopped = None
        self.policy_hash = digest(policy)
        # Gold may be reviewed again between collection/tuning/formal cohorts.
        # Pricing/bounds/phase allocations cannot change or reset existing spend.
        pricing_hash = digest({"pricing": {k: v for k, v in policy.items() if k != "gold_sha256"},
                               "phase_caps_microusd": PHASES, "total_cap_microusd": 10_000_000})
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, hash TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, phase TEXT, "
                       "kind TEXT, model TEXT, bound INTEGER, charged INTEGER, actual INTEGER, "
                       "state TEXT, elapsed_ms INTEGER, usage TEXT, created REAL)")
            db.execute("INSERT OR IGNORE INTO config VALUES (1, ?)", (pricing_hash,))
            if db.execute("SELECT hash FROM config WHERE id=1").fetchone()[0] != pricing_hash:
                raise ValueError("ledger policy differs; do not reset the shared budget")

    @contextmanager
    def transaction(self):
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def reserve(self, phase, kind, model, bound):
        if phase not in PHASES or type(bound) is not int or bound <= 0:
            raise ValueError("invalid phase or reservation")
        request_id = uuid.uuid4().hex
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM requests WHERE state='bound_exceeded'").fetchone():
                self.stopped = "budget_bound_exceeded"
                raise BudgetError("budget_bound_exceeded")
            total, used = db.execute(
                "SELECT COALESCE(sum(charged),0), "
                "COALESCE(sum(CASE WHEN phase=? THEN charged ELSE 0 END),0) FROM requests",
                (phase,)).fetchone()
            if total + bound > sum(PHASES.values()) or used + bound > PHASES[phase]:
                self.stopped = "budget_cap_reached"
                raise BudgetError("budget_cap_reached")
            db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (request_id, phase, kind, model, bound, bound, None, "reserved",
                        None, None, time.time()))
        return request_id

    def settle(self, request_id, *, actual=None, usage=None, elapsed_ms=None, invalid_bound=False,
               conservative_upper=None):
        value = None if actual is None else micros(actual)
        with self.transaction() as db:
            row = db.execute("SELECT bound,state FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None or row[1] != "reserved":
                raise ValueError("unknown or already settled reservation")
            bound = row[0]
            charged = bound if value is None else value
            state = "unknown" if value is None else "settled"
            if value is None and conservative_upper is not None:
                charged = micros(conservative_upper)
                state = "bounded_estimate"
            if charged > bound or invalid_bound:
                state = "bound_exceeded"  # freeze ALL later requests; never hide the overrun
            db.execute("UPDATE requests SET charged=?,actual=?,state=?,elapsed_ms=?,usage=? "
                       "WHERE id=?", (charged, value, state, elapsed_ms,
                                      json.dumps(usage) if usage is not None else None, request_id))

    def summary(self):
        with self.transaction() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute("SELECT * FROM requests ORDER BY created,id")]
        return {"policy_sha256": self.policy_hash, "cap_usd": "10.00",
                "charged_usd": str(Decimal(sum(r["charged"] for r in rows)) / 1_000_000),
                "phases": {p: {"cap_microusd": cap, "charged_microusd": sum(
                    r["charged"] for r in rows if r["phase"] == p)} for p, cap in PHASES.items()},
                "unknown_or_inflight": sum(r["actual"] is None for r in rows),
                "inflight_or_unbounded": sum(r["state"] in ("reserved", "unknown") for r in rows),
                "bound_exceeded": any(r["state"] == "bound_exceeded" for r in rows),
                "requests": rows}


def token_cost(rates, usage):
    """Missing billing metadata stays unknown, including missing thinking/cache counts."""
    keys = ("input_tokens", "output_tokens", "thinking_tokens", "cached_input_tokens")
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in keys):
        return None
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        return None
    return ((usage["input_tokens"] - usage["cached_input_tokens"]) * amount(rates["input_per_mtok"])
            + usage["cached_input_tokens"] * amount(rates["cached_input_per_mtok"])
            + (usage["output_tokens"] + usage["thinking_tokens"]) * amount(rates["output_per_mtok"])
            ) / 1_000_000


def usage_from_raw(raw):
    try:
        data = json.loads(raw)
        usage = llm.parse_usage(data.get("usageMetadata"))
    except (ValueError, TypeError, AttributeError):
        return {}
    return {key: getattr(usage, key) for key in (
        "input_tokens", "output_tokens", "thinking_tokens", "cached_input_tokens")}


def token_upper_cost(rates, usage):
    """Missing cache is charged at FULL input price; missing thinking at the full
    combined output cap. These are explicit upper estimates, never reported zeroes.
    """
    tokens = usage.get("input_tokens")
    if type(tokens) is not int or tokens < 0:
        tokens = rates["model_input_token_limit"]
    output, thinking = usage.get("output_tokens"), usage.get("thinking_tokens")
    generated = output + thinking if all(type(v) is int and v >= 0 for v in (output, thinking)) \
        else rates["max_output_tokens"]
    return (tokens * amount(rates["input_per_mtok"])
            + generated * amount(rates["output_per_mtok"])) / 1_000_000


class BoundedGemini(llm.GeminiClient):
    """Includes every generate/transcribe/summary attempt, even failed parses/replans.

    Optional countTokens uses the identical model/full request including audio,
    tools and system. It is an admission check, not the currency bound.
    maxOutputTokens caps the combined generated/thinking token budget.
    """
    def __init__(self, api_key, *, ledger, phase, transport=None):
        super().__init__(api_key, transport=transport)
        self.ledger, self.phase = ledger, phase
        self.last_request_id = None
        self.request_ids = []

    def _post(self, url, body, timeout_s):
        started = time.monotonic()
        model = url.rsplit("/", 1)[-1].split(":")[0]
        rates = self.ledger.policy["models"].get(model)
        if not rates or len(body) > rates["max_request_bytes"]:
            raise BudgetError("budget_unbounded_request")
        request = json.loads(body)
        if "cachedContent" in request or any(set(tool) != {"functionDeclarations"}
                                               for tool in request.get("tools", [])):
            raise BudgetError("budget_unpriced_tool_or_cache")
        config = request.setdefault("generationConfig", {})
        effort = config.get("thinkingConfig", {}).get("thinkingLevel", "omitted")
        if effort not in rates["thinking_levels"]:
            raise BudgetError("budget_unsupported_effort")
        if config.get("candidateCount", 1) != 1:
            raise BudgetError("budget_multiple_candidates")
        config.pop("candidateCount", None)  # unsupported by Gemini 3.x; verified default is one
        config["maxOutputTokens"] = rates["max_output_tokens"]
        if rates["count_before_generate"]:
            counted = {"generateContentRequest": {"model": f"models/{model}", **request}}
            status, raw = super()._post(url.replace(":generateContent", ":countTokens"),
                                       json.dumps(counted).encode(), timeout_s)
            try:
                total = json.loads(raw)["totalTokens"] if status == 200 else None
            except (ValueError, KeyError, TypeError):
                total = None
            if type(total) is not int or total < 0 \
                    or total + rates["count_margin_tokens"] > rates["max_input_tokens"]:
                raise BudgetError("budget_input_bound")
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            raise BudgetError("budget_count_deadline")
        # countTokens is a useful admission check, not a guaranteed billing upper
        # bound. Reserve the ENTIRE verified model input limit. Never invent a
        # safety margin to turn an estimate into a currency guarantee.
        bound = micros((rates["model_input_token_limit"] * amount(rates["input_per_mtok"])
                        + rates["max_output_tokens"] * amount(rates["output_per_mtok"])) / 1_000_000)
        rid = self.ledger.reserve(self.phase, "gemini", model, max(1, bound))
        self.last_request_id = rid
        self.request_ids.append(rid)
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            self.ledger.settle(rid, actual=0, usage={"not_dispatched": True})
            raise BudgetError("budget_reservation_deadline")
        try:
            result = super()._post(url, json.dumps(request).encode(), remaining)
        except BaseException:
            self.ledger.settle(rid, elapsed_ms=int((time.monotonic() - started) * 1000))
            raise
        usage = usage_from_raw(result[1]) if result[0] == 200 else {}
        admission_limit = rates["max_input_tokens"] if rates["count_before_generate"] \
            else rates["model_input_token_limit"]
        invalid_bound = (type(usage.get("input_tokens")) is int
                         and usage["input_tokens"] > admission_limit)
        if all(type(usage.get(key)) is int for key in ("output_tokens", "thinking_tokens")):
            invalid_bound |= usage["output_tokens"] + usage["thinking_tokens"] > rates["max_output_tokens"]
        self.ledger.settle(rid, actual=token_cost(rates, usage), usage=usage,
                           elapsed_ms=int((time.monotonic() - started) * 1000),
                           invalid_bound=invalid_bound,
                           conservative_upper=token_upper_cost(rates, usage))
        return result


def runtime_budget(settings, environ=None):
    """Explicit opt-in for an owner-operated deployed timing/collection cohort.

    Mount the SAME host directory at /day13 in BOTH services. Never enable only
    one provider/process; unrelated account clients remain outside this guard.
    Missing/partial settings fail closed at startup. Default runtime is unchanged.
    """
    environ = os.environ if environ is None else environ
    keys = ("DAY13_BUDGET_POLICY", "DAY13_BUDGET_LEDGER", "DAY13_BUDGET_PHASE")
    if not any(environ.get(key) for key in keys):
        return None
    if not all(environ.get(key) for key in keys):
        raise ValueError("Day13 runtime budget requires policy, shared ledger and phase")
    if environ[keys[2]] not in PHASES:
        raise ValueError("invalid Day13 runtime budget phase")
    if environ[keys[1]] != "/day13/budget.sqlite":
        raise ValueError("runtime ledger must be the shared /day13/budget.sqlite bind mount")
    policy = load_policy(environ[keys[0]])
    for model in (settings.llm_primary_model, settings.llm_failover_model):
        if model not in policy["models"]:
            raise ValueError("runtime model has no verified request bound")
    collection = environ.get("DAY13_BUDGET_COLLECTION") == "true"
    if collection:
        if environ[keys[2]] != "tuning":
            raise ValueError("collection may only use the tuning allocation")
    else:
        from app.day13_bench import check_freeze, file_hash
        frozen = environ.get("DAY13_GOLD_FREEZE")
        if not frozen or file_hash(frozen) != policy.get("gold_sha256"):
            raise ValueError("runtime budget requires the reviewed gold freeze")
        check_freeze(frozen)
    return Ledger(environ[keys[1]], policy), environ[keys[2]]


class BoundedScribe:
    def __init__(self, client, *, ledger, phase):
        self.client, self.ledger, self.phase = client, ledger, phase
        self.provider, self.model = client.provider, client.model
        self.request_ids = []

    def close(self):
        self.client.close()

    def transcribe(self, data, mime_type, keyterms, *, timeout_s):
        started = time.monotonic()
        rates = self.ledger.policy.get("scribe")
        duration = audio.ogg_duration_ms(data) if mime_type == "audio/ogg" else None
        if not rates or duration is None or duration <= 0:
            raise BudgetError("budget_unbounded_audio")
        seconds = max(rates["minimum_seconds"], math.ceil(duration / 1000))
        seconds = math.ceil(seconds / rates["round_to_seconds"]) * rates["round_to_seconds"]
        rate = amount(rates["per_hour"]) + (amount(rates["keyterms_per_hour"]) if keyterms else 0)
        cost = seconds * rate / 3600
        rid = self.ledger.reserve(self.phase, "scribe", self.model, max(1, micros(cost)))
        self.request_ids.append(rid)
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            self.ledger.settle(rid, actual=0, usage={"not_dispatched": True})
            raise BudgetError("budget_reservation_deadline")
        try:
            result = self.client.transcribe(data, mime_type, keyterms, timeout_s=remaining)
        except BaseException:
            self.ledger.settle(rid, elapsed_ms=int((time.monotonic() - started) * 1000))
            raise
        # Duration pricing is estimated, not an invoice; charge the conservative bound.
        self.ledger.settle(rid, actual=cost, usage={"billed_seconds_bound": seconds},
                           elapsed_ms=int((time.monotonic() - started) * 1000))
        return result
