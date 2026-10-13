"""Task 7.2: STT benchmark harness (R16 section 4.2 / 4.4 step 1 / 5 criterion A).

    python -m app.stt_bench validate --manifest bench/voice_manifest.csv --audio-dir <dir>
    python -m app.stt_bench selftest
    python -m app.stt_bench plan   [--manifest ... --audio-dir ...]
    python -m app.stt_bench run    --manifest ... --audio-dir ... --out <dir> --confirm-live
    python -m app.stt_bench report --manifest ... --audio-dir ... --out <dir>

Runs ElevenLabs Scribe v2 and Gemini audio independently on the same recorded notes,
through the production adapters (app.stt.ScribeClient, app.llm.GeminiClient) with the
production request settings, and scores R16 criterion A from the *raw* provider
output. Nothing here changes a production setting or touches the database.

What is kept apart, on purpose:

* planned scripts (bench/customer_voice.json "script") vs what was actually said
  (bench/voice_gold_verified.json, one human-verified entry per recording, bound to
  the recording's sha256). Only verified gold can make a run acceptance-eligible;
  with planned scripts the report is labelled INDICATIVE.
* raw vendor output (scored for criterion A, WER / CER) vs the application's view of
  it (app.speech.check_transcript: rejected / no_speech / script) vs a replay of the
  app.speech fallback chain on the same raw results (no extra calls). The chain
  never feeds the vendor scores, so one vendor's output cannot rescue the other's.
* a transcript screen on the controls (does the text carry any price / size /
  location / purpose?) vs the real gate (zero *requirements* from the requirement
  extraction path), which this harness cannot run: reported "unverified".

Caching: one JSON file per (sent audio sha256, provider config), where the config
includes model, request settings and a prompt / keyterms / adapter fingerprint; any
change makes a different key, so a stale result is never reused. Only successful
results are cached; failed samples are retried on the next run (bounded attempts, each
recorded in journal.jsonl) and always stay in the denominators.

Privacy: transcripts and audio stay in the (git-ignored) output directory. report.json
and report.md hold ids, codes and counts only: no transcripts, phone numbers, storage
keys or URLs. Keys come from the environment and are never printed or stored.
"""

import argparse
import csv
import dataclasses
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from app import audio, benchmark, demo_dataset, speech
from app import location as loc
from app import normalise as n
from app import stt as stt_module
from app.llm import (
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVELS,
    TRANSCRIBE_INSTRUCTION,
    TRANSCRIBE_SCHEMA,
    GeminiClient,
    ProviderError,
    Usage,
    build_transcribe_body,
)
from app.stt import MAX_KEYTERMS, SCRIBE_MODEL, ScribeClient, ScribeResult

SCHEMA = 1
REPO = Path(__file__).resolve().parent.parent
VERIFIED_GOLD = benchmark.BENCH_DIR / "voice_gold_verified.json"
DEFAULT_MANIFEST = benchmark.BENCH_DIR / "voice_manifest.csv"

# R16 section 5 criterion A: voice items with all critical slots (price, size + unit,
# location, purpose) correctly transcribed. "budget" is the price slot's gold key.
CRITICAL_SLOTS = ("purpose", "budget", "size", "location")
THRESHOLD_OVERALL = 0.85
THRESHOLD_QUIET = 0.90
VOICE_SECONDS = (3, 45)  # R16 4.2 durations
DURATION_TOLERANCE_MS = 500  # manifest duration vs the Ogg container
LOCATION_WINDOW = 7  # words
TRANSIENT = re.compile(r"timeout|transport:.*|http_429|http_5\d\d")
SPEAKER = re.compile(r"S\d{1,2}")
WEBHOOK_KEY = re.compile(r"tenant/[0-9a-f-]{36}/audio/[0-9a-f-]{36}\.ogg")
SHA256 = re.compile(r"[0-9a-f]{64}")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PHONE_LIKE = re.compile(r"\d{10,}")
REQUIRED_COLUMNS = ("item_id", "speaker", "phone", "noise", "status", "is_voice",
                    "mime_type", "duration_ms", "byte_size", "sha256")
OPTIONAL_COLUMNS = ("seq", "conversation", "received_at", "storage_key", "origin")
ENV_KEYS = ("ELEVENLABS_API_KEY", "GEMINI_API_KEY", "LLM_PRIMARY_MODEL", "LLM_THINKING_LEVEL")


class BenchError(Exception):
    """A usage or data problem that stops a command (never carries a secret)."""


# --- helpers ------------------------------------------------------------------------

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value):
    return sha256_bytes(canonical(value).encode("utf-8"))


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def percentile(values, q):
    """Nearest-rank percentile (q in 0..100); None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def bench_items(data=None):
    """(customer voice items, control items) of the committed benchmark set."""
    data = data or benchmark.load()
    return list(data["customer_voice"]), list(data["control"])


# --- manifest -----------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ManifestRow:
    item_id: str
    speaker: str  # pseudonymous S1..; the number -> person map stays in the private sheet
    phone: str  # device class, never a number
    noise: str
    mime_type: str
    duration_ms: int
    byte_size: int
    sha256: str
    provenance: str  # webhook | synthetic | unknown


@dataclasses.dataclass
class ManifestCheck:
    rows: dict
    errors: list  # malformed data: nothing may run
    blockers: list  # the run may happen but is not acceptance-eligible
    coverage: dict
    sha256: str

    @property
    def eligible(self):
        return not self.errors and not self.blockers


def provenance(row):
    if (row.get("origin") or "").strip().lower() == "synthetic":
        return "synthetic"
    return "webhook" if WEBHOOK_KEY.fullmatch((row.get("storage_key") or "").strip()) \
        else "unknown"


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def audio_path(audio_dir, item_id):
    return Path(audio_dir) / f"{item_id}.ogg"


def validate_manifest(text, *, bench=None, audio_dir=None):
    """Check bench/voice_manifest.csv (docs/BENCHMARK.md) against the benchmark set and,
    with audio_dir, against the files (<item_id>.ogg: size, sha256, Ogg duration)."""
    bench = bench or bench_items()
    voice, controls = bench
    expected = {item["id"] for item in (*voice, *controls)}
    errors, blockers = [], []
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise BenchError("manifest lacks columns: " + ", ".join(missing))
    rows, seen = {}, Counter()
    for number, raw in enumerate(reader, 2):
        where = f"manifest line {number}"
        raw = {k: (v or "").strip() for k, v in raw.items() if k}
        for field, value in raw.items():
            # a sha256 or a storage key (UUIDs) may hold a long digit run by chance; the
            # first is checked by SHA256 below, the second by WEBHOOK_KEY
            if field not in ("sha256", "storage_key") and (PHONE_LIKE.search(value) or "://" in value):
                errors.append(f"{where}: {field} looks like a phone number or URL")
        item_id = raw["item_id"]
        seen[item_id] += 1
        if item_id not in expected:
            errors.append(f"{where}: unknown item_id {item_id!r}")
            continue
        if seen[item_id] > 1:
            errors.append(f"{where}: {item_id} appears twice (keep the counted retake only)")
            continue
        problems = []
        if not SPEAKER.fullmatch(raw["speaker"]):
            problems.append("speaker must be a pseudonym like S1")
        if raw["phone"] not in benchmark.PHONES:
            problems.append(f"phone must be one of {sorted(benchmark.PHONES)}")
        if raw["noise"] not in benchmark.NOISE_COUNTS:
            problems.append(f"noise must be one of {sorted(benchmark.NOISE_COUNTS)}")
        if raw["status"] != "stored":
            problems.append("status must be stored (re-record rejected / failed notes)")
        if raw["is_voice"] != "true":
            problems.append("is_voice must be true")
        if raw["mime_type"] != "audio/ogg":
            problems.append("mime_type must be audio/ogg (the WhatsApp path)")
        if not SHA256.fullmatch(raw["sha256"]):
            problems.append("sha256 must be 64 lowercase hex characters")
        duration, size = _int(raw["duration_ms"]), _int(raw["byte_size"])
        if not duration or duration <= 0 or not size or size <= 0:
            problems.append("duration_ms and byte_size must be positive integers")
        if problems:
            errors.extend(f"{where} ({item_id}): {p}" for p in problems)
            continue
        rows[item_id] = ManifestRow(item_id, raw["speaker"], raw["phone"], raw["noise"],
                                    raw["mime_type"], duration, size, raw["sha256"],
                                    provenance(raw))
    if audio_dir is None:
        blockers.append("audio files not checked (no --audio-dir)")
    else:
        for item_id, row in rows.items():
            _check_file(audio_path(audio_dir, item_id), row, errors)
    coverage = _coverage(rows, voice, controls, blockers)
    return ManifestCheck(rows, errors, blockers, coverage, sha256_bytes(text.encode("utf-8")))


def _check_file(path, row, errors):
    if not path.is_file():
        errors.append(f"{row.item_id}: audio file {path.name} not found")
        return
    data = path.read_bytes()
    if len(data) != row.byte_size:
        errors.append(f"{row.item_id}: file size differs from byte_size")
    if sha256_bytes(data) != row.sha256:
        errors.append(f"{row.item_id}: file sha256 differs from the manifest")
    if audio.sniff_mime(data) != "audio/ogg":
        errors.append(f"{row.item_id}: file is not Ogg audio")
        return
    real = audio.ogg_duration_ms(data)
    if real is None or abs(real - row.duration_ms) > DURATION_TOLERANCE_MS:
        errors.append(f"{row.item_id}: Ogg duration {real} ms differs from the manifest")


def _coverage(rows, voice, controls, blockers):
    """ACTUAL coverage (R16 4.2 / Task 5.3), computed from the manifest, not the plan."""
    voice_ids = [item["id"] for item in voice]
    control_ids = [item["id"] for item in controls]
    have = [rows[i] for i in voice_ids if i in rows]
    absent = [i for i in (*voice_ids, *control_ids) if i not in rows]
    speakers = {r.speaker for r in have}
    phones = {r.phone for r in have}
    noise = Counter(r.noise for r in have)
    short = [r.item_id for r in have
             if not VOICE_SECONDS[0] * 1000 <= r.duration_ms <= VOICE_SECONDS[1] * 1000]
    not_real = [r.item_id for r in rows.values() if r.provenance != "webhook"]
    if absent:
        blockers.append(f"{len(absent)} recording(s) missing: {', '.join(absent)}")
    if len(speakers) < benchmark.MIN_SPEAKERS:
        blockers.append(f"{len(speakers)} speakers < {benchmark.MIN_SPEAKERS}")
    if phones != benchmark.PHONES:
        blockers.append(f"phone classes {sorted(phones)} != {sorted(benchmark.PHONES)}")
    if len(have) == len(voice_ids) and noise != benchmark.NOISE_COUNTS:
        blockers.append(f"noise mix {dict(noise)} != {benchmark.NOISE_COUNTS}")
    if short:
        blockers.append(f"{len(short)} note(s) outside {VOICE_SECONDS[0]}-{VOICE_SECONDS[1]} s: "
                        + ", ".join(short))
    if not_real:
        blockers.append(f"{len(not_real)} recording(s) not from the real webhook path "
                        "(synthetic or unknown storage_key)")
    return {
        "voice_recordings": len(have), "voice_expected": len(voice_ids),
        "control_recordings": len([i for i in control_ids if i in rows]),
        "control_expected": len(control_ids),
        "speakers": len(speakers), "phones": sorted(phones), "noise": dict(noise),
        "missing": absent, "not_webhook": sorted(not_real),
    }


# --- gold: planned scripts vs verified transcripts ------------------------------------

@dataclasses.dataclass(frozen=True)
class Gold:
    item_id: str
    status: str  # verified | planned
    transcript: str
    extraction: dict
    deviates: bool


def load_verified(path=VERIFIED_GOLD):
    if not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text("utf-8"))
    if data.get("schema") != SCHEMA or not isinstance(data.get("items"), dict):
        raise BenchError(f"{Path(path).name}: unsupported format (schema {SCHEMA}, items)")
    return data["items"]


def build_gold(items, verified, manifest_rows, demo=None):
    """Gold per item id and a list of problems. An entry is 'verified' only when it
    names the sha256 of the recording it was checked against; the planned script is
    never relabelled as verified. A deviating entry replaces the transcript and the
    extraction wholesale (docs/BENCHMARK.md) and is validated like committed gold."""
    gold, problems = {}, []
    for item in items:
        item_id = item["id"]
        entry = verified.get(item_id)
        planned = Gold(item_id, "planned", item["gold_transcript"]["roman"],
                       item["gold_extraction"], False)
        if entry is None:
            gold[item_id] = planned
            continue
        row = manifest_rows.get(item_id)
        if row is None or entry.get("audio_sha256") != row.sha256:
            problems.append(f"{item_id}: verified gold is for a different recording")
            gold[item_id] = planned
            continue
        if not entry.get("verified_by") or not entry.get("verified_on"):
            problems.append(f"{item_id}: verified gold needs verified_by and verified_on")
            gold[item_id] = planned
            continue
        if entry.get("as_scripted") is True:
            if "gold_transcript" in entry or "gold_extraction" in entry:
                problems.append(f"{item_id}: as_scripted entries carry no overrides")
            gold[item_id] = Gold(item_id, "verified", planned.transcript,
                                 planned.extraction, False)
            continue
        text = (entry.get("gold_transcript") or {}).get("roman")
        if entry.get("as_scripted") is not False or not isinstance(text, str):
            problems.append(f"{item_id}: say as_scripted true, or give gold_transcript.roman "
                            "and gold_extraction (as_scripted false)")
            gold[item_id] = planned
            continue
        extraction = entry.get("gold_extraction")
        if not isinstance(extraction, dict):
            problems.append(f"{item_id}: a deviating entry needs gold_extraction")
            gold[item_id] = planned
            continue
        if demo is not None and item.get("set") == "customer_voice":
            merged = {**item, "gold_transcript": {"roman": text, "native": text},
                      "script": text, "gold_extraction": extraction}
            found = []
            benchmark.check_slots(demo, merged, found)
            problems.extend(found)
        gold[item_id] = Gold(item_id, "verified", text, extraction, True)
    return gold, problems


# --- scoring (criterion A) ------------------------------------------------------------

def scorer_text(text):
    """Provider text as the application sees speech: audio-event tags removed."""
    return " ".join(speech._EVENT_TAG.sub(" ", text or "").split())


def _fail(reason):
    return {"correct": False, "reason": reason}


def _ok():
    return {"correct": True, "reason": "match"}


def transcript_purpose(text, gold):
    """The purpose a reader gets from the transcript alone: spoken words, else inferred
    from the spoken amount. The gold supplies only the property type (a context input
    for the residential / commercial thresholds), never the answer."""
    found = n.parse_purpose(text, source="voice")
    if found.status == n.OK:
        return found
    types = (gold.get("property_types") or {}).get("value") or [None]
    budget = n.parse_budget(text)
    amount = budget.stated_pkr if budget.status == n.OK else None
    return n.infer_purpose(amount, n.category_of(types[0]))


def score_price(text, gold):
    expected = gold["budget"]
    purpose = transcript_purpose(text, gold)
    got = n.parse_budget(text, purpose=purpose.value if purpose.status == n.OK else None)
    if got.status != n.OK:
        return _fail(got.reason or got.status)
    shown = got.to_dict()
    same = all(shown.get(k) == v for k, v in expected.items() if k != "raw")
    return _ok() if same else _fail("different_value")


def score_size(text, gold):
    expected = gold["size"]
    found = n.find_areas(text)
    if not found:
        return _fail("missing")
    types = (gold.get("property_types") or {}).get("value") or [None]
    parsed = [n.parse_area(m.text, property_type=types[0]).to_dict() for m in found]
    keys = [k for k in expected if k != "raw"]
    shown = {canonical({k: p.get(k) for k in keys}) for p in parsed if p["status"] == "ok"}
    if not shown:
        return _fail("unparsed")
    if len(shown) > 1:
        return _fail("several_sizes")
    return _ok() if json.loads(shown.pop()) == {k: expected[k] for k in keys} \
        else _fail("different_value")


def find_locations(text, gazetteer):
    """The distinct locations a reader would take from free text, chosen from the text
    alone (no gold argument exists): every word window of up to LOCATION_WINDOW words
    goes through the 7.1 resolver; RESOLVED / AMBIGUOUS windows are taken longest
    first without overlapping, leftmost first among equals. Deterministic (non-fuzzy)
    matches beat fuzzy ones, because a fuzzy match on extra words ("Bahria Heights,
    up to") only adds noise. A fuzzy AMBIGUOUS result is the resolver offering "did you
    mean ...?" for a word that merely looks like a place ("karao" -> Karachi, 0.4): it
    is not a location the speaker stated and is ignored. A fuzzy RESOLVED window counts
    only when nothing deterministic resolves, and then only the shortest one. R16 does not say how to find the span; this is the harness's rule
    (docs/STT_BENCHMARK.md)."""
    words = text.split()
    found = []
    for i in range(len(words)):
        for j in range(min(len(words), i + LOCATION_WINDOW), i, -1):
            result = loc.resolve(" ".join(words[i:j]), None, gazetteer)
            if result.method == "fuzzy" and result.status != loc.RESOLVED:
                continue
            if result.status in (loc.RESOLVED, loc.AMBIGUOUS):
                found.append((i, j, result))
    exact = [w for w in found if w[2].method != "fuzzy"]
    if not exact:
        fuzzy = sorted(found, key=lambda w: (w[1] - w[0], w[0]))
        return [fuzzy[0][2]] if fuzzy else []
    taken, chosen = set(), []
    for i, j, result in sorted(exact, key=lambda w: (-(w[1] - w[0]),
                                                      w[2].status != loc.RESOLVED, w[0])):
        if taken.isdisjoint(range(i, j)):
            taken.update(range(i, j))
            chosen.append((i, result))
    chosen.sort(key=lambda c: c[0])
    distinct = {}
    for _, result in chosen:
        key = (result.status, tuple(result.nodes),
               tuple(sorted(str(c.location_id) for c in result.candidates)))
        distinct.setdefault(key, result)
    return list(distinct.values())


def find_location(text, gazetteer):
    """The one location in a text, or None when there is none (or several: see
    find_locations)."""
    found = find_locations(text, gazetteer)
    return found[0] if len(found) == 1 else None


def score_location(text, gold, gazetteer):
    expected = gold["location"]
    places = find_locations(text, gazetteer)
    if not places:
        return _fail("missing")
    if len(places) > 1:
        return _fail("several_locations")
    found = places[0]
    if expected["status"] == loc.RESOLVED:
        if found.status != loc.RESOLVED:
            return _fail("ambiguous_instead_of_resolved")
        return _ok() if list(found.nodes) == expected["value"] else _fail("different_location")
    if expected["status"] == loc.AMBIGUOUS:
        if found.status != loc.AMBIGUOUS:
            return _fail("resolved_instead_of_ambiguous")
        same = sorted(c.location_id for c in found.candidates) == sorted(expected["candidates"])
        return _ok() if same else _fail("different_candidates")
    return _fail("unsupported_gold_status")


def score_purpose(text, gold):
    expected = gold["purpose"]
    found = n.parse_purpose(text, source="voice")
    if found.status != n.OK and expected.get("source") == "inferred":
        budget = n.parse_budget(text)
        types = (gold.get("property_types") or {}).get("value") or [None]
        amount = budget.stated_pkr if budget.status == n.OK else None
        found = n.infer_purpose(amount, n.category_of(types[0]))
    if found.status != n.OK:
        return _fail(found.status)
    return _ok() if found.value == expected["value"] else _fail("different_value")


def score_slots(text, extraction, gazetteer):
    """{slot: {correct, reason}} for the critical slots present in the gold."""
    out = {}
    if "purpose" in extraction:
        out["purpose"] = score_purpose(text, extraction)
    if "budget" in extraction:
        out["budget"] = score_price(text, extraction)
    if "size" in extraction:
        out["size"] = score_size(text, extraction)
    if "location" in extraction:
        out["location"] = score_location(text, extraction, gazetteer)
    return out


def judge_item(text, extraction, gazetteer, *, accepted=True):
    """(slots, all_correct, vacuous, reason) for one voice item's transcript.

    With critical gold slots: correct only if every slot is. With none (CV-27, CV-29,
    CV-30) there is nothing to match, so the item passes only on *successful
    processing with no invented requirement*: a non-empty transcript that the
    application would accept (`accepted`) and that carries no price, size, location
    or purpose. No transcript (error, not run, missing audio), an empty or rejected
    one, or an invented critical slot fails."""
    gold_slots = {k: v for k, v in extraction.items() if k in CRITICAL_SLOTS}
    if gold_slots:
        slots = score_slots(text, extraction, gazetteer)
        return slots, all(s["correct"] for s in slots.values()), False, None
    if not text:
        return {}, False, True, "no_transcript"
    if not accepted:
        return {}, False, True, "rejected"
    signals = requirement_signals(text, gazetteer)
    if signals:
        return {}, False, True, "invented:" + ",".join(signals)
    return {}, True, True, "clean"


def requirement_signals(text, gazetteer):
    """Controls: which slot kinds a transcript still carries. A transcript screen, not
    the requirement-extraction gate."""
    signals = []
    if n.parse_purpose(text).status == n.OK:
        signals.append("purpose")
    if n.find_money(text):
        signals.append("budget")
    if n.find_areas(text):
        signals.append("size")
    if text and find_locations(text, gazetteer):
        signals.append("location")
    return signals


def _words(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.findall(r"\w+", text)


def _distance(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def error_rates(reference, hypothesis):
    """(WER, CER) after lowercasing and dropping punctuation; reported, never gating."""
    ref, hyp = _words(reference), _words(hypothesis)
    if not ref:
        return None, None
    ref_chars, hyp_chars = list(" ".join(ref)), list(" ".join(hyp))
    return _distance(ref, hyp) / len(ref), _distance(ref_chars, hyp_chars) / len(ref_chars)


def script_profile(text):
    """Share of letters per script. Devanagari / Arabic-script words are read by neither
    the 6.2 normalisers nor (beyond four terms) the 7.1 resolver."""
    counts = Counter()
    for char in text or "":
        if not char.isalpha():
            continue
        point = ord(char)
        if 0x0900 <= point <= 0x097F:
            counts["devanagari"] += 1
        elif 0x0600 <= point <= 0x06FF or 0x0750 <= point <= 0x077F or 0xFB50 <= point <= 0xFEFF:
            counts["arabic"] += 1
        else:
            counts["latin_other"] += 1
    total = sum(counts.values())
    return {k: round(counts[k] / total, 3) if total else 0.0
            for k in ("latin_other", "arabic", "devanagari")}


# --- providers ------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ProviderConfig:
    name: str  # scribe | gemini
    model: str
    settings: dict
    fingerprint: str  # prompt / keyterms / adapter-source version

    @property
    def key(self):
        return sha256_json({"name": self.name, "model": self.model,
                            "settings": self.settings, "fingerprint": self.fingerprint})

    def describe(self):
        return {"name": self.name, "model": self.model, "settings": self.settings,
                "fingerprint": self.fingerprint, "config_key": self.key}


def usage_dict(usage):
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "thinking_tokens": usage.thinking_tokens}


class ScribeAdapter:
    """app.stt.ScribeClient with the production request: auto language, the gazetteer
    keyterms (capped at MAX_KEYTERMS), tag_audio_events, word timestamps."""

    def __init__(self, client, keyterms):
        self.client = client
        self.keyterms = list(keyterms)[:MAX_KEYTERMS]
        source = Path(stt_module.__file__).read_bytes()
        self.config = ProviderConfig(
            "scribe", SCRIBE_MODEL,
            {"language_code": None, "tag_audio_events": True,
             "timestamps_granularity": "word", "keyterm_count": len(self.keyterms)},
            sha256_json({"keyterms": self.keyterms, "adapter": sha256_bytes(source)}),
        )

    def call(self, data, mime_type, timeout_s):
        got = self.client.transcribe(data, mime_type, self.keyterms, timeout_s=timeout_s)
        return {"text": got.text, "speech_present": None, "language": got.language_code,
                "language_probability": got.language_probability,
                "word_count": got.word_count, "event_count": got.event_count,
                "model_version": None, "usage": usage_dict(Usage())}


class GeminiAdapter:
    """app.llm.GeminiClient.transcribe with the production prompt and schema."""

    def __init__(self, client, model, thinking_level):
        self.client, self.model, self.thinking_level = client, model, thinking_level
        body = build_transcribe_body(b"", "audio/ogg", thinking_level)
        body["contents"][0]["parts"][0]["inlineData"]["data"] = ""
        self.config = ProviderConfig(
            "gemini", model,
            {"thinking_level": thinking_level, "temperature": 0,
             "response_schema": "TRANSCRIBE_SCHEMA", "mime": "inline audio part"},
            sha256_json({"request": body, "instruction": TRANSCRIBE_INSTRUCTION,
                         "schema": TRANSCRIBE_SCHEMA}),
        )

    def call(self, data, mime_type, timeout_s):
        got = self.client.transcribe(self.model, data, mime_type, timeout_s=timeout_s,
                                     thinking_level=self.thinking_level)
        return {"text": got.transcript, "speech_present": got.speech_present,
                "language": got.language, "language_probability": None,
                "word_count": None, "event_count": None,
                "model_version": got.model_version, "usage": usage_dict(got.usage)}


def build_keyterms(dataset=None):
    """The keyterm list app.speech.keyterms would build from the shared gazetteer: the
    SQL's ordering (rank, term) over the committed demo dataset. A production database
    that collates differently may order ties differently: pass the real list with
    --keyterms-file when it matters."""
    data = dataset or demo_dataset.load()
    ranked = []
    for row in data["locations.csv"]:
        if row["level"] != "city":
            ranked.append((1 if row["level"] == "area" else 4,
                           row["display_name"] or row["name"]))
    place = {row["key"]: row for row in data["locations.csv"]}
    for row in data["location_aliases.csv"]:
        if row["kind"] in ("voice_stt", "common", "official") and \
                place[row["location_key"]]["level"] != "city":
            ranked.append(({"voice_stt": 2, "common": 3}.get(row["kind"], 5), row["alias"]))
    return speech.build_keyterms([term for _, term in sorted(ranked)], MAX_KEYTERMS)


def load_keyterms(path):
    terms = [line.strip() for line in Path(path).read_text("utf-8").splitlines() if line.strip()]
    if len(terms) > MAX_KEYTERMS or any(not speech._keyterm_ok(t) for t in terms):
        raise BenchError(f"{Path(path).name}: more than {MAX_KEYTERMS} terms or an invalid term")
    return terms


def providers_from_env(environ, keyterms, *, transport=None):
    """Both production adapters, configured from the worker's variable names."""
    missing = [k for k in ("ELEVENLABS_API_KEY", "GEMINI_API_KEY", "LLM_PRIMARY_MODEL")
               if not (environ.get(k) or "").strip()]
    if missing:
        raise BenchError("set " + ", ".join(missing) + " in the environment")
    model = environ["LLM_PRIMARY_MODEL"].strip()
    if not MODEL_ID.fullmatch(model):
        raise BenchError("LLM_PRIMARY_MODEL is not a valid model ID")
    level = (environ.get("LLM_THINKING_LEVEL") or "").strip().lower()
    level = DEFAULT_THINKING_LEVEL if not level else (None if level == "off" else level)
    if level is not None and level not in THINKING_LEVELS:
        raise BenchError("LLM_THINKING_LEVEL must be one of: off, " + ", ".join(THINKING_LEVELS))
    return [
        ScribeAdapter(ScribeClient(environ["ELEVENLABS_API_KEY"].strip(), transport=transport),
                      keyterms),
        GeminiAdapter(GeminiClient(environ["GEMINI_API_KEY"].strip(), transport=transport),
                      model, level),
    ]


# --- cache, journal, runner -----------------------------------------------------------

class Cache:
    """One file per (sent audio sha256, provider config). Entries repeat their inputs
    and are rejected when anything differs, so a stale or edited file is never used."""

    def __init__(self, directory):
        self.directory = Path(directory)

    @staticmethod
    def key(audio_sha, config_key):
        return sha256_json({"audio": audio_sha, "config": config_key})

    def load(self, audio_sha, config_key):
        path = self.directory / f"{self.key(audio_sha, config_key)}.json"
        try:
            entry = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        valid = (isinstance(entry, dict) and entry.get("schema") == SCHEMA
                 and entry.get("audio_sha256") == audio_sha
                 and entry.get("config_key") == config_key
                 and isinstance(entry.get("result"), dict))
        return entry if valid else None

    def store(self, audio_sha, provider, result, attempts, latency_ms, recorded_at):
        entry = {"schema": SCHEMA, "audio_sha256": audio_sha, "config_key": provider["config_key"],
                 "provider": provider, "result": result, "attempts": attempts,
                 "latency_ms": latency_ms, "recorded_at": recorded_at}
        write_json_atomic(self.directory / f"{self.key(audio_sha, provider['config_key'])}.json",
                          entry)
        return entry


class Journal:
    """Append-only record of every request attempt: ids, codes and timings only."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, **event):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical(event) + "\n")

    def events(self):
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text("utf-8").splitlines() if line]


@dataclasses.dataclass
class Sample:
    item_id: str
    kind: str  # voice | control
    data: bytes  # the bytes actually sent (cut to 120 s like app.speech)
    mime_type: str
    duration_ms: int
    audio_sha256: str  # of the bytes sent
    truncated: bool
    precheck: str | None  # too_short: nothing is sent


def prepare_samples(check, audio_dir, bench=None):
    """The audio as app.speech would send it: unchanged, or cut at an Ogg page boundary
    to 120 s; under 1 s is a pre-check outcome (no request)."""
    voice, controls = bench or bench_items()
    samples = []
    for kind, items in (("voice", voice), ("control", controls)):
        for item in items:
            row = check.rows.get(item["id"])
            if row is None:
                continue
            data = audio_path(audio_dir, item["id"]).read_bytes()
            duration, cut = row.duration_ms, False
            if duration < speech.MIN_DURATION_MS:
                samples.append(Sample(item["id"], kind, data, row.mime_type, duration,
                                      sha256_bytes(data), False, "too_short"))
                continue
            if duration > speech.MAX_DURATION_MS:
                trimmed = audio.truncate_ogg(data, speech.MAX_DURATION_MS)
                if trimmed is None:
                    raise BenchError(f"{item['id']}: cannot cut to 120 s")
                data, duration, cut = trimmed, audio.ogg_duration_ms(trimmed) or 120_000, True
            samples.append(Sample(item["id"], kind, data, row.mime_type, duration,
                                  sha256_bytes(data), cut, None))
    return samples


class RequestBudget:
    def __init__(self, limit):
        self.limit, self.used = limit, 0

    def take(self):
        if self.limit is not None and self.used >= self.limit:
            return False
        self.used += 1
        return True


def now_iso(clock=None):
    return (clock() if clock else datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_sample(sample, adapter, cache, journal, *, max_attempts, timeout_s, budget,
               sleep=time.sleep, monotonic=time.monotonic, clock=None):
    """One (sample, provider): a cache hit, or up to max_attempts live requests.
    Returns the outcome record stored in outcomes.json (no transcript)."""
    config = adapter.config
    base = {"item_id": sample.item_id, "provider": config.name,
            "config_key": config.key, "audio_sha256": sample.audio_sha256}
    if sample.precheck:
        return {**base, "status": "precheck", "code": sample.precheck, "attempts": 0,
                "cached": False}
    hit = cache.load(sample.audio_sha256, config.key)
    if hit:
        journal.add(**base, event="cache_hit")
        return {**base, "status": "ok", "attempts": len(hit.get("attempts") or []),
                "cached": True}
    attempts = []
    for number in range(1, max_attempts + 1):
        if not budget.take():
            journal.add(**base, event="not_attempted", reason="request budget exhausted")
            return {**base, "status": "not_attempted", "code": "request_budget",
                    "attempts": len(attempts), "cached": False}
        began = monotonic()
        try:
            result = adapter.call(sample.data, sample.mime_type, timeout_s)
        except ProviderError as exc:
            ms = int((monotonic() - began) * 1000)
            attempts.append({"n": number, "outcome": "failed", "code": exc.code, "ms": ms})
            journal.add(**base, event="attempt", n=number, outcome="failed", code=exc.code, ms=ms)
            if number < max_attempts and TRANSIENT.fullmatch(exc.code):
                sleep(min(2 ** (number - 1), 8))
                continue
            return {**base, "status": "error", "code": exc.code, "attempts": len(attempts),
                    "cached": False, "attempt_log": attempts}
        ms = int((monotonic() - began) * 1000)
        attempts.append({"n": number, "outcome": "succeeded", "code": "ok", "ms": ms})
        journal.add(**base, event="attempt", n=number, outcome="succeeded", code="ok", ms=ms)
        cache.store(sample.audio_sha256, config.describe(), result, attempts, ms, now_iso(clock))
        return {**base, "status": "ok", "attempts": len(attempts), "cached": False}
    raise AssertionError("unreachable")


def run_identity(check, gold_sha, adapters, *, max_attempts, commit):
    return {"schema": SCHEMA, "manifest_sha256": check.sha256, "gold_sha256": gold_sha,
            "bench_sha256": bench_hash(), "commit": commit, "max_attempts": max_attempts,
            "providers": [a.config.describe() for a in adapters]}


def bench_hash():
    parts = {name: sha256_bytes((benchmark.BENCH_DIR / name).read_bytes())
             for name in ("customer_voice.json", "voice_controls.json")}
    return sha256_json(parts)


def git_commit():
    """HEAD of the checkout the run was made from ('unknown' without git)."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"


def execute_run(samples, adapters, out_dir, identity, *, max_attempts, timeout_s,
                max_requests, sleep=time.sleep, monotonic=time.monotonic, clock=None):
    """Run every sample through every provider, resumably. Returns outcomes."""
    if max_attempts < 1:
        raise BenchError("--max-attempts must be at least 1")
    out_dir = Path(out_dir)
    run_file = out_dir / "run.json"
    if run_file.is_file():
        previous = json.loads(run_file.read_text("utf-8"))
        changed = [k for k in ("manifest_sha256", "gold_sha256", "bench_sha256", "providers",
                               "max_attempts") if previous.get(k) != identity[k]]
        if changed:
            raise BenchError("this output directory belongs to a run with a different "
                             + ", ".join(changed) + "; use a new --out directory")
    else:
        write_json_atomic(run_file, {**identity, "started_at": now_iso(clock)})
    cache, journal = Cache(out_dir / "cache"), Journal(out_dir / "journal.jsonl")
    budget = RequestBudget(max_requests)
    outcomes = {}
    for sample in samples:
        for adapter in adapters:
            outcomes.setdefault(sample.item_id, {})[adapter.config.name] = run_sample(
                sample, adapter, cache, journal, max_attempts=max_attempts,
                timeout_s=timeout_s, budget=budget, sleep=sleep, monotonic=monotonic,
                clock=clock)
        write_json_atomic(out_dir / "outcomes.json", outcomes)
    write_json_atomic(out_dir / "outcomes.json", outcomes)
    return outcomes


# --- evaluation -----------------------------------------------------------------------

class _Replay:
    """A recorded result (or failure) behind the adapter interface, so the real
    app.speech chain logic can be replayed without a request."""

    def __init__(self, provider, model, raw, code):
        self.provider, self.model, self._raw, self._code = provider, model, raw, code

    def _get(self):
        if self._raw is None:
            raise ProviderError(self._code or "no_result", failover=True)
        return self._raw

    def transcribe(self, *args, timeout_s=None, thinking_level=None):
        raw = self._get()
        if self.provider == "elevenlabs":
            return ScribeResult(raw["text"], raw["language"], raw["language_probability"],
                                raw["word_count"] or 0, raw["event_count"] or 0)
        from app.llm import Transcription
        return Transcription(bool(raw["speech_present"]), raw["text"], raw["language"],
                             Usage(), raw.get("model_version"))


def replay_chain(scribe, gemini, duration_ms):
    """The app.speech fallback chain applied to the recorded raw results: status and
    the transcript the application would have used. scribe / gemini are (raw, code)."""
    service = speech.SpeechService(
        _Replay("elevenlabs", SCRIBE_MODEL, *scribe), None,
        fallback=_Replay("gemini", "recorded", *gemini), fallback_model="recorded")
    start = service._clock()
    text, reason, _ = service._primary(b"", "audio/ogg", [], duration_ms, start)
    if reason is None:
        return {"status": speech.TRANSCRIBED, "provider": "elevenlabs", "text": text}
    if reason in speech.NO_FALLBACK_REASONS:
        return {"status": reason, "provider": "elevenlabs", "text": None}
    text, reason, attempt = service._fallback(b"", "audio/ogg", duration_ms, start)
    if attempt is None or (reason is not None and reason not in speech.NO_FALLBACK_REASONS):
        return {"status": "failed", "provider": None, "text": None}
    if reason in speech.NO_FALLBACK_REASONS:
        return {"status": reason, "provider": "gemini", "text": None}
    return {"status": speech.TRANSCRIBED, "provider": "gemini", "text": text}


def app_view(config_name, raw, duration_ms):
    """How app.speech's sanity checks judge one provider's raw output (None = accepted)."""
    text = scorer_text(raw["text"])
    if config_name == "gemini":
        if not raw["speech_present"]:
            return "no_speech"
        return speech.check_transcript(text, duration_ms=duration_ms, language=raw["language"])
    reason = speech.check_transcript(
        text, word_count=raw["word_count"] or None, duration_ms=duration_ms,
        language=raw["language"], language_probability=raw["language_probability"])
    if raw["word_count"] == 0 and raw["event_count"]:
        return "no_speech"
    return reason


def load_raw(cache, outcome):
    """(raw result or None, error code or None, cache entry or None) for an outcome."""
    if outcome is None:
        return None, "not_run", None
    if outcome["status"] == "ok":
        entry = cache.load(outcome["audio_sha256"], outcome["config_key"])
        if entry is None:
            return None, "cache_missing", None
        return entry["result"], None, entry
    return None, outcome.get("code") or outcome["status"], None


def evaluate(check, gold, samples, outcomes, cache, gazetteer, *, bench=None):
    """Score everything from the cached raw results. Pure given its inputs."""
    voice, controls = bench or bench_items()
    by_id = {s.item_id: s for s in samples}
    providers = sorted({p for per in outcomes.values() for p in per})
    result = {name: {"voice": [], "controls": []} for name in providers}
    for kind, items in (("voice", voice), ("controls", controls)):
        for item in items:
            row = check.rows.get(item["id"])
            sample = by_id.get(item["id"])
            for name in providers:
                outcome = (outcomes.get(item["id"]) or {}).get(name)
                raw, code, entry = load_raw(cache, outcome)
                if row is None:
                    code = "missing_recording"
                record = {
                    "id": item["id"], "language": item.get("language"),
                    "category": item["category"],
                    "noise": row.noise if row else item["recording"]["noise"],
                    "speaker": row.speaker if row else None,
                    "phone": row.phone if row else None,
                    "duration_ms": sample.duration_ms if sample else None,
                    "gold_status": gold[item["id"]].status,
                    "status": "ok" if raw is not None else (
                        outcome["status"] if outcome else "missing"),
                    "code": code, "attempts": (outcome or {}).get("attempts", 0),
                    "latency_ms": (entry or {}).get("latency_ms"),
                }
                text = scorer_text(raw["text"]) if raw is not None else None
                if kind == "voice":
                    record.update(_score_voice(item, gold[item["id"]], text, raw, name,
                                               sample, gazetteer))
                else:
                    record.update(_score_control(text, raw, name, sample, gazetteer))
                result[name][kind].append(record)
    chain = _chain_results(voice, controls, check, gold, by_id, outcomes, cache, gazetteer)
    return {"providers": {name: _aggregate(result[name]) for name in providers},
            "pipeline_replay": chain, "records": result}


def _score_voice(item, gold, text, raw, name, sample, gazetteer):
    if text is None:
        slots, ok, vacuous, why = judge_item("", gold.extraction, gazetteer)
        if not vacuous:
            slots = {slot: _fail("no_transcript") for slot in gold.extraction
                     if slot in CRITICAL_SLOTS}
        extra = {"script": None, "wer": None, "cer": None, "app_view": None}
    else:
        view = app_view(name, raw, sample.duration_ms if sample else None)
        slots, ok, vacuous, why = judge_item(text, gold.extraction, gazetteer,
                                             accepted=view is None)
        wer, cer = error_rates(gold.transcript, text)
        extra = {"script": script_profile(text), "wer": wer, "cer": cer, "app_view": view}
    return {"slots": slots, "vacuous": vacuous, "vacuous_reason": why,
            "all_correct": ok, **extra}


def _score_control(text, raw, name, sample, gazetteer):
    if text is None:
        return {"signals": None, "app_view": None, "word_count": None,
                "speech_present": None}
    return {"signals": requirement_signals(text, gazetteer),
            "app_view": app_view(name, raw, sample.duration_ms if sample else None),
            "word_count": len(_words(text)), "speech_present": raw["speech_present"]}


def _chain_results(voice, controls, check, gold, by_id, outcomes, cache, gazetteer):
    """Criterion A on the transcript the app.speech chain would have used (replay of
    the same raw results). Kept out of the vendor scores."""
    rows, controls_out = [], []
    for kind, items in (("voice", voice), ("controls", controls)):
        for item in items:
            sample = by_id.get(item["id"])
            if sample is None or sample.precheck:
                chain = {"status": "no_recording" if sample is None else sample.precheck,
                         "provider": None, "text": None}
            else:
                pair = []
                for name in ("scribe", "gemini"):
                    raw, code, _ = load_raw(cache, (outcomes.get(item["id"]) or {}).get(name))
                    pair.append((raw, code))
                chain = replay_chain(pair[0], pair[1], sample.duration_ms)
            record = {"id": item["id"], "status": chain["status"], "provider": chain["provider"]}
            text = scorer_text(chain["text"]) if chain["text"] else None
            if kind == "voice":
                g = gold[item["id"]]
                record["all_correct"] = judge_item(text or "", g.extraction, gazetteer)[1]
                rows.append(record)
            else:
                record["signals"] = requirement_signals(text, gazetteer) if text else []
                controls_out.append(record)
    correct = sum(r["all_correct"] for r in rows)
    return {"voice_correct": correct, "voice_total": len(rows),
            "by_provider": dict(Counter(r["provider"] or "none" for r in rows)),
            "controls": controls_out,
            "note": "replay of app.speech on the same raw results; not a vendor score"}


def _aggregate(records):
    voice, controls = records["voice"], records["controls"]
    total = len(voice)
    correct = [r for r in voice if r["all_correct"]]
    quiet = [r for r in voice if r["noise"] == "quiet"]
    quiet_ok = [r for r in quiet if r["all_correct"]]
    scored = [r for r in voice if not r["vacuous"]]
    scored_quiet = [r for r in scored if r["noise"] == "quiet"]
    slot_stats = {}
    for slot in CRITICAL_SLOTS:
        have = [r for r in voice if slot in r["slots"]]
        slot_stats[slot] = {"correct": sum(r["slots"][slot]["correct"] for r in have),
                            "total": len(have),
                            "reasons": dict(Counter(r["slots"][slot]["reason"] for r in have
                                                    if not r["slots"][slot]["correct"]))}
    attempts = sum(r["attempts"] for r in voice + controls)
    ok_latency = [r["latency_ms"] for r in voice + controls if r["latency_ms"] is not None]
    long_latency = [r["latency_ms"] for r in voice if r["latency_ms"] is not None
                    and r["duration_ms"] and r["duration_ms"] >= 25_000]
    statuses = Counter(r["status"] for r in voice + controls)
    scripts = [r["script"] for r in voice if r["script"]]
    return {
        "criterion_a": {
            "overall": {"correct": len(correct), "total": total,
                        "rate": len(correct) / total if total else None},
            "quiet": {"correct": len(quiet_ok), "total": len(quiet),
                      "rate": len(quiet_ok) / len(quiet) if quiet else None},
            "critical_slot_diagnostic": {
                "note": "diagnostic only, not R16's criterion: the items that have critical "
                        "gold slots (27 in the committed set)",
                "correct": sum(r["all_correct"] for r in scored), "total": len(scored),
                "rate": sum(r["all_correct"] for r in scored) / len(scored) if scored else None,
                "quiet_correct": sum(r["all_correct"] for r in scored_quiet),
                "quiet_total": len(scored_quiet)},
            "no_critical_slot_items": [
                {"id": r["id"], "correct": r["all_correct"], "reason": r["vacuous_reason"],
                 "status": r["status"]} for r in voice if r["vacuous"]],
            "by_slot": slot_stats,
            "by_noise": _group(voice, "noise"), "by_language": _group(voice, "language"),
            "by_category": _group(voice, "category"),
            "failures": [{"id": r["id"], "status": r["status"], "code": r["code"],
                          "slots": {s: v["reason"] for s, v in r["slots"].items()
                                    if not v["correct"]}} for r in voice
                         if not r["all_correct"]],
        },
        "errors": {"statuses": dict(statuses), "attempts": attempts,
                   "retries": sum(max(0, r["attempts"] - 1) for r in voice + controls)},
        "script": {"devanagari": sum(1 for s in scripts if s["devanagari"] > 0),
                   "arabic": sum(1 for s in scripts if s["arabic"] > 0),
                   "transcripts": len(scripts)},
        "app_view": dict(Counter(r["app_view"] or "accepted" for r in voice if r["status"] == "ok")),
        "error_rates": {
            "wer_mean": _mean([r["wer"] for r in voice]),
            "cer_mean": _mean([r["cer"] for r in voice]), "gating": False},
        "latency_ms": {"p50": percentile(ok_latency, 50), "p90": percentile(ok_latency, 90),
                       "n": len(ok_latency), "p90_notes_25s_plus": percentile(long_latency, 90),
                       "n_25s_plus": len(long_latency), "target_p90_30s_note_ms": 3000},
        "controls": [{k: r[k] for k in ("id", "category", "status", "code", "signals",
                                        "app_view", "speech_present", "word_count")}
                     for r in controls],
    }


def _group(records, key):
    groups = {}
    for r in records:
        slot = groups.setdefault(str(r[key]), {"correct": 0, "total": 0})
        slot["total"] += 1
        slot["correct"] += bool(r["all_correct"])
    return groups


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


# --- usage and cost -------------------------------------------------------------------

def usage_summary(outcomes, cache, samples, prices=None):
    """Metered usage per provider from the recorded attempts: requests, audio seconds
    sent, and Gemini tokens. Cost only with a priced file that names its source."""
    by_id = {s.item_id: s for s in samples}
    out = {}
    for item_id, per in outcomes.items():
        sample = by_id.get(item_id)
        for name, outcome in per.items():
            slot = out.setdefault(name, {"requests": 0, "failed_requests": 0,
                                         "audio_seconds_sent": 0.0, "input_tokens": 0,
                                         "output_tokens": 0, "thinking_tokens": 0})
            slot["requests"] += outcome.get("attempts", 0)
            if outcome["status"] == "ok":
                slot["failed_requests"] += max(0, outcome.get("attempts", 0) - 1)
                entry = cache.load(outcome["audio_sha256"], outcome["config_key"])
                for key in ("input_tokens", "output_tokens", "thinking_tokens"):
                    slot[key] += ((entry or {}).get("result", {}).get("usage") or {}).get(key) or 0
            else:
                slot["failed_requests"] += outcome.get("attempts", 0)
            if sample:
                slot["audio_seconds_sent"] += (sample.duration_ms / 1000) * outcome.get(
                    "attempts", 0)
    for name, slot in out.items():
        slot["audio_seconds_sent"] = round(slot["audio_seconds_sent"], 1)
        slot["estimated_cost_usd"] = _cost(name, slot, prices)
    return out


def _cost(name, slot, prices):
    if not prices or not prices.get("source") or not prices.get("verified_on"):
        return None
    if name == "scribe" and "scribe_usd_per_audio_hour" in prices:
        return round(slot["audio_seconds_sent"] / 3600 * prices["scribe_usd_per_audio_hour"], 4)
    if name == "gemini" and {"gemini_usd_per_mtok_in", "gemini_usd_per_mtok_out"} <= set(prices):
        return round((slot["input_tokens"] * prices["gemini_usd_per_mtok_in"]
                      + (slot["output_tokens"] + slot["thinking_tokens"])
                      * prices["gemini_usd_per_mtok_out"]) / 1_000_000, 4)
    return None


# --- report ---------------------------------------------------------------------------

CONTROL_GATE_REASON = (
    "R16 4.2 / Demo Scope section 6: silence and music controls must produce zero "
    "requirements. That is a property of the requirement-extraction path (turn engine + "
    "requirement state, Tasks 6.1 / 7.3 / 8.1), which this harness does not run; "
    "a blank or rejected transcript is not substituted for it. Reported: the raw "
    "transcripts' slot signals and the app.speech verdict, as evidence only.")


def build_report(identity, check, gold_problems, gold_status, evaluation, usage, *,
                 synthetic):
    reasons = []
    if synthetic:
        reasons.append("synthetic fixture recordings: results cannot satisfy live acceptance")
    reasons += check.errors + check.blockers + gold_problems
    unverified = sorted(i for i, s in gold_status.items() if s != "verified")
    if unverified:
        reasons.append(f"{len(unverified)} item(s) without human-verified gold: planned "
                       "scripts are not what was said")
    eligible = not reasons
    providers = {}
    for name, summary in evaluation["providers"].items():
        a = summary["criterion_a"]
        verdict = "INDICATIVE"
        if eligible:
            verdict = "PASS" if (a["overall"]["rate"] is not None
                                 and a["overall"]["rate"] >= THRESHOLD_OVERALL
                                 and a["quiet"]["rate"] is not None
                                 and a["quiet"]["rate"] >= THRESHOLD_QUIET) else "FAIL"
        providers[name] = {**summary, "criterion_a_verdict": verdict,
                           "usage": usage.get(name)}
    return {
        "schema": SCHEMA, "task": "7.2", "generated_at": now_iso(),
        "synthetic": synthetic, "acceptance_eligible": eligible,
        "ineligible_reasons": reasons,
        "criterion_a": {"source": "R16 section 5 A (also Demo Scope section 6)",
                        "definition": "voice items with ALL gold critical slots "
                                      "(price, size + unit, location, purpose) correct after "
                                      "normalisation; denominator = all 30 voice items",
                        "threshold_overall": THRESHOLD_OVERALL,
                        "threshold_quiet": THRESHOLD_QUIET},
        "control_gate": {"status": "unverified", "reason": CONTROL_GATE_REASON},
        "task_7_2_complete": False,
        "identity": identity, "manifest_coverage": check.coverage,
        "providers": providers, "pipeline_replay": evaluation["pipeline_replay"],
    }


def render_markdown(report):
    lines = ["# STT benchmark report (Task 7.2)", ""]
    banner = ("SYNTHETIC FIXTURE RESULTS - NOT ACCEPTANCE EVIDENCE" if report["synthetic"]
              else ("ACCEPTANCE-ELIGIBLE RUN" if report["acceptance_eligible"]
                    else "INDICATIVE - not acceptance-eligible"))
    lines += [f"**{banner}**", ""]
    for reason in report["ineligible_reasons"]:
        lines.append(f"- {reason}")
    gate = report["control_gate"]
    lines += ["", f"Control gate: **{gate['status']}** - {gate['reason']}", "",
              "## Criterion A (voice items with all critical slots correct)", "",
              "| Provider | Overall | Quiet | Verdict |", "|---|---|---|---|"]
    for name, p in report["providers"].items():
        a = p["criterion_a"]
        lines.append(f"| {name} | {a['overall']['correct']}/{a['overall']['total']} "
                     f"({_pct(a['overall']['rate'])}) | {a['quiet']['correct']}/"
                     f"{a['quiet']['total']} ({_pct(a['quiet']['rate'])}) | "
                     f"{p['criterion_a_verdict']} |")
    lines += ["", "Diagnostic (not R16's criterion): items that have critical gold slots only", ""]
    for name, p in report["providers"].items():
        d = p["criterion_a"]["critical_slot_diagnostic"]
        lines.append(f"- {name}: {d['correct']}/{d['total']} ({_pct(d['rate'])}), quiet "
                     f"{d['quiet_correct']}/{d['quiet_total']}")
    for name, p in report["providers"].items():
        verdicts = ", ".join(
            f"{i['id']}={'pass' if i['correct'] else 'FAIL (' + str(i['reason']) + ')'}"
            for i in p["criterion_a"]["no_critical_slot_items"])
        lines.append(f"- {name} items without critical gold slots: {verdicts}")
    lines += ["", "## Per critical slot", "", "| Provider | purpose | price | size | location |",
              "|---|---|---|---|---|"]
    for name, p in report["providers"].items():
        cells = [f"{s['correct']}/{s['total']}" for s in
                 (p["criterion_a"]["by_slot"][k] for k in CRITICAL_SLOTS)]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## Errors, scripts, latency, usage", ""]
    for name, p in report["providers"].items():
        lat, script = p["latency_ms"], p["script"]
        lines.append(f"- **{name}**: statuses {p['errors']['statuses']}; retries "
                     f"{p['errors']['retries']}; Devanagari in {script['devanagari']}/"
                     f"{script['transcripts']} transcripts, Arabic script in "
                     f"{script['arabic']}; latency p50/p90 {lat['p50']}/{lat['p90']} ms "
                     f"(n={lat['n']}); usage {p['usage']}")
    lines += ["", "## Controls (transcript screen only)", ""]
    for name, p in report["providers"].items():
        for c in p["controls"]:
            lines.append(f"- {name} {c['id']} ({c['category']}): status {c['status']}, "
                         f"app view {c['app_view']}, slot signals {c['signals']}")
    chain = report["pipeline_replay"]
    ident = report["identity"]
    identity = (f"Run identity: commit {ident.get('commit')}, manifest "
                f"{str(ident.get('manifest_sha256'))[:12]}, gold "
                f"{str(ident.get('gold_sha256'))[:12]}")
    replayed = (f"{chain['voice_correct']}/{chain['voice_total']} items correct via "
                f"{chain['by_provider']}")
    lines += ["", "## app.speech chain replay (not a vendor score)", "", replayed, "",
              identity, ""]
    return "\n".join(lines)


def _pct(rate):
    return "n/a" if rate is None else f"{rate:.1%}"


# --- plan (dry run) -------------------------------------------------------------------

def plan(bench, adapters, *, check=None, samples=None, max_attempts, prices=None):
    """Requests, audio duration and retry bounds for a live run; no request is made.
    With a manifest the numbers are actual; without one they come from the script
    targets (target_seconds) and are marked planned."""
    voice, controls = bench
    if samples is not None:
        rows = [(s.item_id, s.duration_ms / 1000, s.precheck) for s in samples]
        basis = "manifest"
    else:
        rows = [(i["id"], min(i["recording"]["target_seconds"], 120), None)
                for i in (*voice, *controls)]
        basis = "planned target_seconds (no manifest yet)"
    sendable = [(i, s) for i, s, pre in rows if not pre]
    seconds = round(sum(s for _, s in sendable), 1)
    out = {"basis": basis, "samples": len(rows), "voice": sum(i.startswith("CV-") for i, _, _ in rows),
           "controls": sum(i.startswith("VC-") for i, _, _ in rows),
           "sendable_samples": len(sendable), "audio_seconds_per_provider": seconds,
           "max_attempts": max_attempts, "providers": {}}
    for adapter in adapters:
        out["providers"][adapter.config.name] = {
            "model": adapter.config.model, "requests_min": len(sendable),
            "requests_max": len(sendable) * max_attempts,
            "audio_seconds_min": seconds, "audio_seconds_max": round(seconds * max_attempts, 1),
            "estimated_cost_usd": _cost(adapter.config.name, {
                "audio_seconds_sent": seconds, "input_tokens": 0, "output_tokens": 0,
                "thinking_tokens": 0}, prices) if adapter.config.name == "scribe" else None,
        }
    return out


# --- selftest -------------------------------------------------------------------------

def selftest(gazetteer, bench=None):
    """Score each planned script as if it were the transcript: every item must come out
    correct, or the scorer and the gold disagree (a finding, never a gold edit)."""
    voice, _ = bench or bench_items()
    rows = []
    for item in voice:
        slots, ok, _, why = judge_item(item["gold_transcript"]["roman"],
                                       item["gold_extraction"], gazetteer)
        rows.append({"id": item["id"], "all_correct": ok, "reason": why,
                     "failed": {k: v["reason"] for k, v in slots.items() if not v["correct"]}})
    return rows


# --- recording checklist ----------------------------------------------------------------

NOISE_HELP = {
    "quiet": "quiet room, door closed, no TV / fan",
    "moderate": "moderate noise: traffic through a window, a TV or a fan running",
    "street": "car or street: inside a moving or idling car, or on a street",
}
PHONE_HELP = {"android_low": "Android low-end", "android_mid": "Android mid-range",
              "iphone": "iPhone"}


def _cell(text):
    return str(text).replace("|", "/").replace("\n", " ")


def checklist_rows(bench=None):
    """One row per benchmark recording (30 voice + 5 controls), from the committed
    bench files only, in item order."""
    voice, controls = bench or bench_items()
    rows = []
    for item in (*voice, *controls):
        rec = item["recording"]
        control = item["set"] == "control"
        rows.append({
            "id": item["id"], "kind": "control" if control else "voice",
            "category": item["category"], "language": item.get("language") or "-",
            "say": item.get("script") or item.get("note"),
            "note": item.get("note") if control else None,
            "speaker": rec["speaker"], "phone": rec["phone"], "noise": rec["noise"],
            "seconds": rec["target_seconds"], "paired_with": item.get("paired_with"),
        })
    return rows


def render_checklist(bench=None):
    """The owner's recording checklist (docs/STT_RECORDING_CHECKLIST.md): every item, who
    says what on which phone under which noise, and how each recording becomes a
    private audio file, a manifest row and a human-verified transcript."""
    rows = checklist_rows(bench)
    lines = [
        "# STT benchmark recording checklist (Task 7.2)",
        "",
        ("Generated by `python -m app.stt_bench checklist` from `bench/customer_voice.json` and "
        "`bench/voice_controls.json`; do not edit by hand. The words below are the **planned "
        "scripts**: they are what each volunteer is asked to say, not what was said. Nothing "
        "here is verified gold."),
        "",
        "## Before recording (owner)",
        "",
        ("1. Volunteers: six people (S1..S6; mixed gender / age, Karachi accents) who agreed in "
        "writing (a message is enough) to a short fictional recording, private storage until "
        "the benchmark closes, use only for this evaluation, and deletion afterwards. Nobody "
        "says a name, number or address of their own. Free Gemini tiers may train on data: "
        "team / volunteer phones only."),
        ("2. Phones: three devices, one per class (`android_low`, `android_mid`, `iphone`). "
        "Speakers share the devices as in the session plan. Add each device's number to the "
        "Meta test-number recipient list (owner only)."),
        ("3. Keep the private sheet (never in Git or chat): speaker S-number -> person, device "
        "class -> phone number, and for every send: item id, send time with UTC offset, "
        "device, retake yes / no (which send counts)."),
        ("4. Check that `STT_PROVIDER` is `off` in the runtime while recording (owner; this "
        "task changes no setting), otherwise each note spends Scribe minutes before the "
        "benchmark. Audio is stored by Task 5.3 either way (`S3_BUCKET` set, migration 012). "
        "With STT off each note gets the usual \"please type\" reply; ignore it."),
        "",
        "## How to record each note",
        "",
        ("- Inside WhatsApp, to the test number: press and hold the microphone (hold-to-record "
        "voice note, **not** an audio file, not a forward), speak, release to send. No editing, "
        "no trimming, no speaker-phone playback of a recording (controls excepted: the music "
        "clip)."),
        ("- Leave about one second of natural pause before speaking and after finishing: R16 "
        "wants 3-45 s notes and the validator flags anything under 3.0 s (several scripts are "
        "only a few words long)."),
        ("- Say the words naturally and in your own pace. If you stumble or change words, "
        "resend; note on the sheet which send counts. Only the counted send goes in the "
        "manifest (a duplicate id is rejected). If you still deviate from the script, that is "
        "fine: it is corrected at verification, not here."),
        ("- Send in item order within a session. One device session = one phone, one noise "
        "setting at a time (see the session plan)."),
        "",
        "## Master list (35 items)",
        "",
        "| ID | Words to say / control instruction | Speaker | Phone | Noise | Length | Paired text |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        say = row["say"]
        if row["kind"] == "control" and row["note"]:
            say = f"CONTROL ({row['category']}): {row['note']}" + (
                f" Then say exactly: \"{row['say']}\"" if row["say"] != row["note"] else "")
        else:
            say = f"\"{say}\""
        seconds = f"{row['seconds']} s" + (" (more than 120 s)" if row["seconds"] > 120 else "")
        lines.append(f"| {row['id']} | {_cell(say)} | {row['speaker']} | {row['phone']} | "
                     f"{row['noise']} | {seconds} | {row['paired_with'] or '-'} |")
    lines += ["", "Noise conditions: " + "; ".join(f"**{k}** = {v}" for k, v in NOISE_HELP.items())
              + ".", "",
              "## Session plan (one block = one phone, one speaker, one noise)", ""]
    blocks = {}
    for row in rows:
        blocks.setdefault((row["phone"], row["speaker"], row["noise"]), []).append(row["id"])
    lines += ["| Phone | Speaker | Noise | Items, in order |", "|---|---|---|---|"]
    for (phone, speaker, noise), ids in sorted(blocks.items()):
        lines.append(f"| {phone} ({PHONE_HELP[phone]}) | {speaker} | {noise} | {', '.join(sorted(ids))} |")
    voice = [r for r in rows if r["kind"] == "voice"]
    lines += [
        "",
        "Planned coverage (the manifest, not this plan, decides): "
        f"{len({r['speaker'] for r in voice})} speakers, "
        f"{len({r['phone'] for r in voice})} phone classes, noise "
        + ", ".join(f"{k} {sum(1 for r in voice if r['noise'] == k)}" for k in NOISE_HELP)
        + f", {len(voice)} voice + {len(rows) - len(voice)} controls.",
        "",
        "## From a sent note to scored evidence",
        "",
        "| Step | Who | What | Result |",
        "|---|---|---|---|",
        ("| 1 Webhook capture | automatic | the note reaches the webhook, `inbound_audio` row, "
        "`fetch_audio` stores the bytes unchanged in the private bucket (`tenant/<agency>/audio/"
        "<id>.ogg`) | a stored object per note |"),
        ("| 2 Manifest export | owner, in the worker container (read-only, `manzil_app` "
        "`DATABASE_URL`) | `python -m app.bench_manifest --agency-id <uuid> --since <start with "
        "offset> --until <end>` prints seq, conversation, received_at, status, is_voice, "
        "mime_type, duration_ms, byte_size, sha256, storage_key | CSV in arrival order |"),
        ("| 3 Label | owner | match each row to the sheet by conversation (one phone) and "
        "received_at; add `item_id`, `speaker`, `phone`, `noise`; drop non-benchmark rows and "
        "uncounted retakes; every kept row must be `stored` | `bench/voice_manifest.csv` "
        "(committable: no phone, URL or text) |"),
        ("| 4 Private audio | owner | download each kept `storage_key` object from the private "
        "bucket to a private local folder as `<item_id>.ogg`; never into Git | audio folder |"),
        ("| 5 Validate | anyone | `python -m app.stt_bench validate --manifest "
        "bench/voice_manifest.csv --audio-dir <folder>`: ids, hashes, sizes, Ogg durations, "
        "pseudonymous labels, actual coverage (6 speakers, 3 phones, 15 / 10 / 5 noise, 30 + 5, "
        "3-45 s) | errors / blockers list |"),
        ("| 6 Human verification | a person who **listens** (preferably not the speaker), one "
        "item at a time | compare the audio with the planned script; if identical write "
        "`as_scripted: true`, otherwise `as_scripted: false` with the words actually said "
        "(`gold_transcript`) and the matching `gold_extraction`; controls: confirm what is "
        "audible (silence, music, Punjabi, long) | an entry in `bench/voice_gold_verified.json` "
        "bound to that recording's sha256 |"),
        ("| 7 Review | second person | spot-check entries marked `as_scripted` and every "
        "deviation | `validate` shows `gold_verified` = 35 |"),
        ("| 8 Score | owner approves provider spend first | `plan`, then `run --confirm-live` "
        "(docs/STT_BENCHMARK.md) | report: official 30-item criterion A, 27-item diagnostic, "
        "controls screen, control gate unverified |"),
        "",
        ("Verified entry shape (one per item; do not create it from the script without "
        "listening):"),
        "",
        "```json",
        ('{"schema": 1, "items": {"CV-06": {"audio_sha256": "<sha256 of CV-06.ogg>", '
        '"verified_by": "<initials>", "verified_on": "<date>", "as_scripted": true}}}'),
        "```",
        "",
        "## Retention",
        "",
        ("Audio stays in the private bucket until benchmark run #2 (Task 10.3 / 14.x), then "
        "the owner deletes the benchmark objects named in the manifest. No audio, phone "
        "number or private URL goes to Git, the report or chat."),
        "",
    ]
    return "\n".join(lines)


# --- CLI ------------------------------------------------------------------------------

def _load_context(args, *, need_audio):
    bench = bench_items()
    manifest = Path(args.manifest)
    if not manifest.is_file():
        raise BenchError(f"{manifest.name} not found: the recordings have not been "
                         "captured / exported yet (docs/BENCHMARK.md recording protocol)")
    audio_dir = Path(args.audio_dir) if args.audio_dir else None
    if need_audio and audio_dir is None:
        raise BenchError("--audio-dir is required")
    check = validate_manifest(manifest.read_text("utf-8"), bench=bench, audio_dir=audio_dir)
    demo = benchmark.load_demo()
    verified = load_verified(args.gold)
    gold, problems = build_gold([*bench[0], *bench[1]], verified, check.rows, demo)
    gold_sha = sha256_bytes(Path(args.gold).read_bytes()) if Path(args.gold).is_file() \
        else sha256_json({})
    return bench, check, gold, problems, gold_sha


def _adapters(args, environ, transport=None):
    keyterms = load_keyterms(args.keyterms_file) if args.keyterms_file else build_keyterms()
    return providers_from_env(environ, keyterms, transport=transport)


def _print(out, text):
    out.write(text + "\n")


def main(argv=None, environ=None, out=None, transport=None):
    environ = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(prog="python -m app.stt_bench",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan", "run", "report"):
        p = sub.add_parser(name)
        p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
        p.add_argument("--audio-dir")
        p.add_argument("--gold", default=str(VERIFIED_GOLD))
        if name in ("plan", "run", "report"):
            p.add_argument("--keyterms-file")
            p.add_argument("--max-attempts", type=int, default=2)
            p.add_argument("--prices-file")
        if name in ("run", "report"):
            p.add_argument("--out", required=True)
        if name == "run":
            p.add_argument("--confirm-live", action="store_true")
            p.add_argument("--max-requests", type=int)
            p.add_argument("--timeout", type=float, default=30.0)
            p.add_argument("--synthetic", action="store_true",
                           help="the audio is fixture audio: the report says so")
        if name == "report":
            p.add_argument("--synthetic", action="store_true")
    sub.add_parser("selftest")
    check_cmd = sub.add_parser("checklist")
    check_cmd.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        return _dispatch(args, environ, out, transport)
    except BenchError as exc:
        print(f"stt_bench: {exc}", file=sys.stderr)
        return 1


def _dispatch(args, environ, out, transport):
    if args.command == "checklist":
        text = render_checklist()
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        else:
            _print(out, text)
        return 0
    if args.command == "selftest":
        gazetteer = loc.Gazetteer.from_dataset(demo_dataset.load())
        rows = selftest(gazetteer)
        bad = [r for r in rows if not r["all_correct"]]
        _print(out, f"selftest: {len(rows) - len(bad)}/{len(rows)} planned scripts score "
                    "correct against their own gold")
        for r in bad:
            _print(out, f"  {r['id']}: {r['failed']}")
        return 0 if not bad else 1
    prices = json.loads(Path(args.prices_file).read_text("utf-8")) \
        if getattr(args, "prices_file", None) else None
    if args.command == "plan" and not Path(args.manifest).is_file():
        bench = bench_items()
        adapters = _plan_adapters(args, environ)
        _print(out, json.dumps(plan(bench, adapters, max_attempts=args.max_attempts,
                                    prices=prices), indent=2))
        return 0
    bench, check, gold, problems, gold_sha = _load_context(
        args, need_audio=args.command != "validate")
    if args.command == "validate":
        _print(out, json.dumps({"errors": check.errors, "blockers": check.blockers,
                                "gold_problems": problems, "coverage": check.coverage,
                                "gold_verified": sum(g.status == "verified"
                                                     for g in gold.values()),
                                "acceptance_eligible": check.eligible and not problems},
                               indent=2))
        return 0 if not check.errors and not problems else 1
    if check.errors or problems:
        raise BenchError("manifest or gold has errors; run `validate`")
    samples = prepare_samples(check, args.audio_dir, bench)
    adapters = _adapters(args, environ, transport)
    out_dir = Path(args.out)
    if args.command == "plan":
        _print(out, json.dumps(plan(bench, adapters, samples=samples,
                                    max_attempts=args.max_attempts, prices=prices), indent=2))
        return 0
    identity = run_identity(check, gold_sha, adapters, max_attempts=args.max_attempts,
                            commit=git_commit())
    if args.command == "run":
        if not args.confirm_live:
            raise BenchError("a run makes paid provider requests: pass --confirm-live")
        outcomes = execute_run(samples, adapters, out_dir, identity,
                               max_attempts=args.max_attempts, timeout_s=args.timeout,
                               max_requests=args.max_requests)
    else:
        outcomes = json.loads((out_dir / "outcomes.json").read_text("utf-8"))
    cache = Cache(out_dir / "cache")
    gazetteer = loc.Gazetteer.from_dataset(demo_dataset.load())
    evaluation = evaluate(check, gold, samples, outcomes, cache, gazetteer, bench=bench)
    usage = usage_summary(outcomes, cache, samples, prices)
    report = build_report(identity, check, problems, {k: g.status for k, g in gold.items()},
                          evaluation, usage, synthetic=args.synthetic)
    write_json_atomic(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    write_json_atomic(out_dir / "private_detail.json", evaluation["records"])
    _print(out, render_markdown(report))
    return 0


def _plan_adapters(args, environ):
    """Adapters for a plan without a manifest: configuration only, no key needed."""
    keyterms = load_keyterms(args.keyterms_file) if args.keyterms_file else build_keyterms()
    model = (environ.get("LLM_PRIMARY_MODEL") or "<LLM_PRIMARY_MODEL>").strip()
    level = (environ.get("LLM_THINKING_LEVEL") or "").strip().lower()
    level = DEFAULT_THINKING_LEVEL if not level else (None if level == "off" else level)
    return [ScribeAdapter(ScribeClient("plan-only"), keyterms),
            GeminiAdapter(GeminiClient("plan-only"), model, level)]


if __name__ == "__main__":
    sys.exit(main())
