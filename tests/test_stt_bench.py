"""Task 7.2: the STT benchmark harness (app/stt_bench.py). Offline.

Audio is fictional Ogg Opus (tests/test_speech.opus_note), the providers are the real
app.stt / app.llm clients on httpx.MockTransport, and every "recording" is synthetic:
nothing here is live acceptance evidence, and the harness says so in its reports.
"""

import base64
import copy
import csv
import hashlib
import io
import json
import re

import httpx
import pytest

from app import benchmark, demo_dataset, speech, stt_bench
from app import location as loc
from app.stt_bench import BenchError
from tests.test_speech import gemini_reply, opus_note, scribe_body

KEY_SCRIBE = "scribe-key-not-a-secret"
KEY_GEMINI = "gemini-key-not-a-secret"
MODEL = "bench-model"
KEYTERMS = ["DHA", "Clifton", "marla"]

GAZETTEER = loc.Gazetteer.from_dataset(demo_dataset.load())
BENCH = stt_bench.bench_items()
ITEMS = {i["id"]: i for i in (*BENCH[0], *BENCH[1])}
DEMO = benchmark.load_demo()


def sha(data):
    return hashlib.sha256(data).hexdigest()


# --- a synthetic world: audio files, manifest, scripted providers -----------------------

class World:
    """35 fictional notes (30 voice + 5 controls) in tmp_path, a manifest for them, and
    two MockTransport providers that answer per recording from `says`."""

    def __init__(self, tmp_path, *, origin="synthetic"):
        self.dir = tmp_path / "audio"
        self.dir.mkdir()
        self.audio, self.by_bytes, self.calls = {}, {}, []
        self.says = {}  # (provider, item) -> response override
        self.fail = {}  # (provider, item) -> list of status codes to return first
        rows = []
        for n, item_id in enumerate(ITEMS):
            seconds = ITEMS[item_id]["recording"]["target_seconds"]  # VC-05: 130 s
            data = opus_note(seconds, pattern=[60 + n])
            (self.dir / f"{item_id}.ogg").write_bytes(data)
            self.audio[item_id], self.by_bytes[data] = data, item_id
            rec = ITEMS[item_id]["recording"]
            from app import audio
            rows.append({"item_id": item_id, "speaker": rec["speaker"],
                         "phone": rec["phone"], "noise": rec["noise"],
                         "status": "stored", "is_voice": "true", "mime_type": "audio/ogg",
                         "duration_ms": audio.ogg_duration_ms(data), "byte_size": len(data),
                         "sha256": sha(data), "origin": origin})
        self.rows = rows

    def manifest(self, rows=None):
        out = io.StringIO()
        columns = ["item_id", "speaker", "phone", "noise", "status", "is_voice", "mime_type",
                   "duration_ms", "byte_size", "sha256", "origin"]
        writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows if rows is not None else self.rows)
        return out.getvalue()

    def check(self, rows=None):
        return stt_bench.validate_manifest(self.manifest(rows), bench=BENCH,
                                           audio_dir=self.dir)

    def transport(self):
        def handler(request):
            if "elevenlabs" in str(request.url):
                provider, body = "scribe", request.read()
                item = self.identify(body, raw=True)
            else:
                provider = "gemini"
                payload = json.loads(request.read())
                part = payload["contents"][0]["parts"][0]["inlineData"]["data"]
                item = self.identify(base64.b64decode(part))
            self.calls.append((provider, item))
            queue = self.fail.get((provider, item))
            if queue:
                return httpx.Response(queue.pop(0), json={})
            say = self.says.get((provider, item))
            if say is None:
                say = self.spoken(item)
            if provider == "scribe":
                return httpx.Response(200, json=say if isinstance(say, dict)
                                      else scribe_body(say, "urd"))
            if isinstance(say, dict):
                return httpx.Response(200, json=say)
            return httpx.Response(200, json=gemini_reply(say, bool(say), "ur"))
        return httpx.MockTransport(handler)

    def identify(self, blob, raw=False):
        """The item whose audio this request carries (the 130 s control is sent cut, so
        match on the first bytes, which differ per item)."""
        for data, item in self.by_bytes.items():
            if (data[:400] in blob) if raw else blob.startswith(data[:400]):
                return item
        raise AssertionError("request carries unknown audio")

    @staticmethod
    def spoken(item):
        return ITEMS[item]["gold_transcript"]["roman"] if ITEMS[item]["set"] == "customer_voice" \
            else ""

    def environ(self):
        return {"ELEVENLABS_API_KEY": KEY_SCRIBE, "GEMINI_API_KEY": KEY_GEMINI,
                "LLM_PRIMARY_MODEL": MODEL, "LLM_THINKING_LEVEL": "low"}

    def adapters(self, environ=None):
        return stt_bench.providers_from_env(environ or self.environ(), KEYTERMS,
                                            transport=self.transport())

    def run(self, out_dir, *, adapters=None, max_attempts=2, max_requests=None, check=None,
            gold_sha="g" * 64):
        check = check or self.check()
        adapters = adapters or self.adapters()
        samples = stt_bench.prepare_samples(check, self.dir, BENCH)
        identity = stt_bench.run_identity(check, gold_sha, adapters,
                                          max_attempts=max_attempts, commit="test")
        outcomes = stt_bench.execute_run(
            samples, adapters, out_dir, identity, max_attempts=max_attempts, timeout_s=5,
            max_requests=max_requests, sleep=lambda s: None)
        return samples, outcomes

    def report(self, out_dir, samples, outcomes, check=None, synthetic=True):
        check = check or self.check()
        gold, problems = stt_bench.build_gold(list(ITEMS.values()), {}, check.rows, DEMO)
        cache = stt_bench.Cache(out_dir / "cache")
        evaluation = stt_bench.evaluate(check, gold, samples, outcomes, cache, GAZETTEER,
                                        bench=BENCH)
        usage = stt_bench.usage_summary(outcomes, cache, samples)
        return stt_bench.build_report({"commit": "test"}, check, problems,
                                      {k: g.status for k, g in gold.items()}, evaluation,
                                      usage, synthetic=synthetic)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# --- scoring ---------------------------------------------------------------------------

def test_every_planned_script_scores_correct_against_its_own_gold():
    """The scorer and the committed gold agree: a disagreement would be a finding for
    review (docs/BENCHMARK.md), never an automatic gold edit."""
    rows = stt_bench.selftest(GAZETTEER)
    assert len(rows) == 30
    assert [r for r in rows if not r["all_correct"]] == []


def gold(item_id):
    return ITEMS[item_id]["gold_extraction"]


def test_a_misheard_price_size_location_or_purpose_fails_its_slot():
    cv06 = gold("CV-06")  # DHA Phase 6, 500 gaz, aath crore tak (sale, inferred)
    ok = stt_bench.score_slots("DHA Phase 6 mein 500 gaz ka ghar, budget aath crore tak",
                               cv06, GAZETTEER)
    assert all(s["correct"] for s in ok.values())
    wrong = stt_bench.score_slots("DHA Phase 6 mein 300 gaz ka ghar, budget aath crore",
                                  cv06, GAZETTEER)
    assert wrong["size"] == {"correct": False, "reason": "different_value"}
    # "tak" dropped: a bare amount is "around", not the gold's max
    assert wrong["budget"] == {"correct": False, "reason": "different_value"}
    assert wrong["location"]["correct"] and wrong["purpose"]["correct"]
    other = stt_bench.score_slots("Clifton mein 500 gaz ka ghar, budget aath crore tak",
                                  cv06, GAZETTEER)
    assert other["location"] == {"correct": False, "reason": "different_location"}
    nothing = stt_bench.score_slots("mujhe ghar chahiye", cv06, GAZETTEER)
    assert {k: v["reason"] for k, v in nothing.items() if not v["correct"]} == {
        "budget": "none", "size": "missing", "location": "missing", "purpose": "none"}


def test_rent_for_sale_is_a_purpose_error():
    cv01 = gold("CV-01")
    text = "Hi, I am looking for an office for sale in Karachi, around five lakh per month."
    assert stt_bench.score_slots(text, cv01, GAZETTEER)["purpose"]["reason"] == "different_value"


def test_designed_ambiguity_must_stay_ambiguous():
    cv26 = gold("CV-26")
    assert cv26["location"]["status"] == "AMBIGUOUS"
    ok = stt_bench.score_slots("Block six mein office chahiye rent pe", cv26, GAZETTEER)
    assert ok["location"]["correct"]
    resolved = stt_bench.score_slots("Clifton block six mein office chahiye rent pe", cv26,
                                     GAZETTEER)
    assert resolved["location"]["reason"] == "resolved_instead_of_ambiguous"


def test_devanagari_output_is_reported_and_its_known_words_are_read():
    """The live 2026-10-12 finding: Scribe wrote Devanagari for Roman Urdu speech."""
    text = "DHA phase 6 में 500 gaz का घर, budget 8 crore तक का।"
    profile = stt_bench.script_profile(text)
    assert profile["devanagari"] > 0 and profile["arabic"] == 0
    slots = stt_bench.score_slots(text, gold("CV-06"), GAZETTEER)
    assert slots["budget"]["correct"]  # "तक" is read as "tak" (max) since 2026-10-17


def test_error_rates_are_reported_with_punctuation_and_case_ignored():
    assert stt_bench.error_rates("DHA phase 6, ghar", "dha phase 6 ghar") == (0.0, 0.0)
    wer, cer = stt_bench.error_rates("do teen", "do chaar")
    assert wer == 0.5 and 0 < cer < 1
    assert stt_bench.error_rates("", "x") == (None, None)


def test_event_tags_are_not_speech():
    assert stt_bench.scorer_text("(music) DHA  phase 6 (laughter)") == "DHA phase 6"


# --- manifest ----------------------------------------------------------------------------

def test_a_valid_synthetic_manifest_has_no_errors_but_is_never_eligible(world):
    check = world.check()
    assert check.errors == []
    assert not check.eligible
    assert any("real webhook path" in b for b in check.blockers)
    assert check.coverage["voice_recordings"] == 30 and check.coverage["control_recordings"] == 5
    assert check.coverage["speakers"] == 6 and check.coverage["phones"] == sorted(benchmark.PHONES)
    assert check.coverage["noise"] == benchmark.NOISE_COUNTS


def test_a_real_looking_manifest_can_be_eligible(world):
    key = ("tenant/11111111-1111-1111-1111-111111111111/audio/"
           "22222222-2222-2222-2222-222222222222.ogg")
    out = io.StringIO()
    columns = [*world.rows[0], "storage_key"]
    writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows({**r, "origin": "", "storage_key": key} for r in world.rows)
    check = stt_bench.validate_manifest(out.getvalue(), bench=BENCH, audio_dir=world.dir)
    assert check.errors == []
    assert check.blockers == []  # 6 speakers, 3 phones, 15/10/5 noise, 30 + 5, all webhook
    assert check.eligible


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda r: r.update(speaker="Ayesha"), "speaker must be a pseudonym"),
    (lambda r: r.update(phone="0300-1234567"), "phone must be one of"),
    (lambda r: r.update(noise="loud"), "noise must be one of"),
    (lambda r: r.update(status="failed"), "status must be stored"),
    (lambda r: r.update(is_voice="false"), "is_voice must be true"),
    (lambda r: r.update(mime_type="audio/mpeg"), "mime_type must be audio/ogg"),
    (lambda r: r.update(sha256="abc"), "sha256 must be 64"),
    (lambda r: r.update(duration_ms="0"), "positive integers"),
    (lambda r: r.update(item_id="CV-99"), "unknown item_id"),
    (lambda r: r.update(speaker="923000000000"), "looks like a phone number"),
])
def test_manifest_rows_are_validated(world, mutate, message):
    rows = copy.deepcopy(world.rows)
    mutate(rows[0])
    assert any(message in e for e in world.check(rows).errors)


def test_a_url_is_rejected_anywhere_in_the_manifest(world):
    rows = copy.deepcopy(world.rows)
    rows[0]["origin"] = "https://example.invalid/private.ogg"
    assert any("phone number or URL" in e for e in world.check(rows).errors)


def test_a_duplicate_item_needs_one_counted_retake(world):
    rows = world.rows + [copy.deepcopy(world.rows[0])]
    assert any("appears twice" in e for e in world.check(rows).errors)


def test_files_must_match_the_manifest(world):
    (world.dir / "CV-01.ogg").write_bytes(world.audio["CV-02"])
    errors = world.check().errors
    assert any(e.startswith("CV-01") and "sha256" in e for e in errors)
    (world.dir / "CV-02.ogg").unlink()
    assert any("CV-02" in e and "not found" in e for e in world.check().errors)


def test_the_ogg_duration_must_match(world):
    rows = copy.deepcopy(world.rows)
    rows[0]["duration_ms"] = int(rows[0]["duration_ms"]) + 5000
    assert any("Ogg duration" in e for e in world.check(rows).errors)


def test_coverage_is_computed_from_the_recordings_not_the_plan(world):
    rows = copy.deepcopy(world.rows)
    for row in rows:
        row["speaker"] = "S1"
        row["phone"] = "iphone"
        row["noise"] = "quiet"
    blockers = " ".join(world.check(rows).blockers)
    assert "1 speakers < 6" in blockers
    assert "phone classes" in blockers and "noise mix" in blockers
    partial = world.check([r for r in world.rows if r["item_id"] != "CV-30"])
    assert partial.coverage["missing"] == ["CV-30"]
    assert any("missing: CV-30" in b for b in partial.blockers)


def test_durations_outside_r16_are_a_blocker_not_an_error(world):
    data = opus_note(2, pattern=[99])
    (world.dir / "CV-01.ogg").write_bytes(data)
    rows = copy.deepcopy(world.rows)
    rows[0].update(sha256=sha(data), byte_size=len(data), duration_ms=2000)
    check = world.check(rows)
    assert check.errors == []
    assert any("outside 3-45 s" in b for b in check.blockers)


def test_a_missing_manifest_is_a_clear_error(tmp_path, capsys):
    code = stt_bench.main(["validate", "--manifest", str(tmp_path / "none.csv")], environ={})
    assert code == 1
    assert "have not been captured" in capsys.readouterr().err


# --- gold: planned vs verified --------------------------------------------------------------

def test_planned_scripts_are_never_labelled_verified(world):
    check = world.check()
    gold_map, problems = stt_bench.build_gold(list(ITEMS.values()), {}, check.rows, DEMO)
    assert problems == []
    assert {g.status for g in gold_map.values()} == {"planned"}


def test_verified_gold_is_bound_to_the_recording(world):
    check = world.check()
    entry = {"audio_sha256": world.rows[0]["sha256"], "verified_by": "R1",
             "verified_on": "2026-10-20", "as_scripted": True}
    good, problems = stt_bench.build_gold(list(ITEMS.values()), {"CV-01": entry},
                                          check.rows, DEMO)
    assert problems == [] and good["CV-01"].status == "verified"
    stale = {**entry, "audio_sha256": "0" * 64}
    bad, problems = stt_bench.build_gold(list(ITEMS.values()), {"CV-01": stale}, check.rows, DEMO)
    assert bad["CV-01"].status == "planned"
    assert "different recording" in problems[0]
    anon = {k: v for k, v in entry.items() if k != "verified_by"}
    assert stt_bench.build_gold(list(ITEMS.values()), {"CV-01": anon}, check.rows, DEMO)[1]


def test_a_deviating_transcript_replaces_the_planned_gold_and_is_validated(world):
    check = world.check()
    spoken = "Do you have any shops for rent in Clifton Block 5? Maximum four lakh a month."
    extraction = copy.deepcopy(gold("CV-02"))
    extraction["budget"].update(raw="Maximum four lakh a month", max=400000)
    entry = {"audio_sha256": world.rows[1]["sha256"], "verified_by": "R1",
             "verified_on": "2026-10-20", "as_scripted": False,
             "gold_transcript": {"roman": spoken, "native": spoken},
             "gold_extraction": extraction}
    result, problems = stt_bench.build_gold(list(ITEMS.values()), {"CV-02": entry},
                                            check.rows, DEMO)
    assert problems == []
    assert result["CV-02"].deviates and result["CV-02"].transcript == spoken
    assert result["CV-02"].extraction["budget"]["max"] == 400000
    broken = {**entry, "gold_extraction": {**extraction, "budget": {**extraction["budget"],
                                                                    "raw": "not in the text"}}}
    assert stt_bench.build_gold(list(ITEMS.values()), {"CV-02": broken}, check.rows, DEMO)[1]
    scripted_with_override = {**entry, "as_scripted": True}
    assert stt_bench.build_gold(list(ITEMS.values()), {"CV-02": scripted_with_override},
                                check.rows, DEMO)[1]


# --- run, cache, resume ---------------------------------------------------------------------

def test_a_full_synthetic_run_scores_and_is_marked_synthetic(world, tmp_path):
    out = tmp_path / "run"
    samples, outcomes = world.run(out)
    report = world.report(out, samples, outcomes)
    # 35 samples x 2 providers, VC-05 sent cut to 120 s
    assert len(world.calls) == 70
    cut = next(s for s in samples if s.item_id == "VC-05")
    assert cut.truncated and cut.duration_ms <= 120_000
    assert report["synthetic"] and not report["acceptance_eligible"]
    assert report["task_7_2_complete"] is False
    assert any("synthetic" in r for r in report["ineligible_reasons"])
    for name in ("scribe", "gemini"):
        a = report["providers"][name]["criterion_a"]
        assert a["overall"] == {"correct": 30, "total": 30, "rate": 1.0}
        assert a["quiet"]["total"] == 15
        assert report["providers"][name]["criterion_a_verdict"] == "INDICATIVE"
    markdown = stt_bench.render_markdown(report)
    assert "SYNTHETIC FIXTURE RESULTS - NOT ACCEPTANCE EVIDENCE" in markdown


def test_resume_makes_no_second_request_for_cached_results(world, tmp_path):
    out = tmp_path / "run"
    world.run(out)
    world.calls.clear()
    _, outcomes = world.run(out)
    assert world.calls == []
    assert all(o["cached"] for per in outcomes.values() for o in per.values()
               if o["status"] == "ok")


def test_a_changed_config_is_refused_in_a_used_directory(world, tmp_path):
    out = tmp_path / "run"
    world.run(out)
    world.calls.clear()
    env = {**world.environ(), "LLM_THINKING_LEVEL": "high"}
    with pytest.raises(BenchError, match="providers"):
        world.run(out, adapters=world.adapters(env))
    assert world.calls == []


def test_cache_keys_change_with_model_thinking_level_and_keyterms(world, tmp_path):
    first = world.adapters()
    env = {**world.environ(), "LLM_THINKING_LEVEL": "high"}
    second = world.adapters(env)
    assert first[0].config.key == second[0].config.key  # Scribe unaffected
    assert first[1].config.key != second[1].config.key  # Gemini thinking level
    third = world.adapters({**world.environ(), "LLM_PRIMARY_MODEL": "other-model"})
    assert third[1].config.key != first[1].config.key
    moved = stt_bench.providers_from_env(world.environ(), KEYTERMS + ["precinct"],
                                         transport=world.transport())
    assert moved[0].config.key != first[0].config.key  # keyterms version


def test_the_cache_rejects_a_tampered_or_mismatched_entry(tmp_path):
    cache = stt_bench.Cache(tmp_path)
    provider = {"config_key": "c" * 64}
    cache.store("a" * 64, provider, {"text": "x"}, [], 10, "2026-10-13T00:00:00Z")
    assert cache.load("a" * 64, "c" * 64)["result"] == {"text": "x"}
    assert cache.load("a" * 64, "d" * 64) is None
    assert cache.load("b" * 64, "c" * 64) is None
    path = next(tmp_path.glob("*.json"))
    entry = json.loads(path.read_text())
    entry["config_key"] = "e" * 64
    path.write_text(json.dumps(entry))
    assert cache.load("a" * 64, "c" * 64) is None
    path.write_text("not json")
    assert cache.load("a" * 64, "c" * 64) is None


def test_a_run_directory_is_tied_to_its_manifest_gold_and_providers(world, tmp_path):
    out = tmp_path / "run"
    world.run(out)
    with pytest.raises(BenchError, match="gold_sha256"):
        world.run(out, gold_sha="h" * 64)
    with pytest.raises(BenchError, match="max_attempts"):
        world.run(out, max_attempts=3)
    other = world.adapters({**world.environ(), "LLM_PRIMARY_MODEL": "other-model"})
    with pytest.raises(BenchError, match="providers"):
        world.run(out, adapters=other)


def test_a_failed_sample_is_retried_recorded_and_stays_in_the_denominator(world, tmp_path):
    out = tmp_path / "run"
    world.fail[("scribe", "CV-05")] = [503, 503]
    samples, outcomes = world.run(out, max_attempts=2)
    bad = outcomes["CV-05"]["scribe"]
    assert bad["status"] == "error" and bad["code"] == "http_503" and bad["attempts"] == 2
    journal = [e for e in stt_bench.Journal(out / "journal.jsonl").events()
               if e["item_id"] == "CV-05" and e["provider"] == "scribe"]
    assert [e["outcome"] for e in journal] == ["failed", "failed"]
    report = world.report(out, samples, outcomes)
    scribe = report["providers"]["scribe"]
    assert scribe["criterion_a"]["overall"] == {"correct": 29, "total": 30,
                                                "rate": 29 / 30}
    assert scribe["errors"]["statuses"]["error"] == 1
    assert scribe["criterion_a"]["failures"][0]["id"] == "CV-05"
    assert report["providers"]["gemini"]["criterion_a"]["overall"]["correct"] == 30
    # failures are not cached: the next run tries that sample again, nothing else
    world.calls.clear()
    world.run(out, max_attempts=2)
    assert world.calls == [("scribe", "CV-05")]


def test_a_transient_failure_that_recovers_counts_as_a_retry(world, tmp_path):
    world.fail[("gemini", "CV-07")] = [429]
    samples, outcomes = world.run(tmp_path / "run", max_attempts=2)
    ok = outcomes["CV-07"]["gemini"]
    assert ok["status"] == "ok" and ok["attempts"] == 2
    report = world.report(tmp_path / "run", samples, outcomes)
    assert report["providers"]["gemini"]["errors"]["retries"] == 1
    assert report["providers"]["gemini"]["usage"]["failed_requests"] == 1


def test_a_permanent_error_is_not_retried(world, tmp_path):
    world.fail[("scribe", "CV-08")] = [401, 401]
    _, outcomes = world.run(tmp_path / "run", max_attempts=3)
    assert outcomes["CV-08"]["scribe"]["attempts"] == 1
    assert outcomes["CV-08"]["scribe"]["code"] == "http_401"


def test_the_request_budget_stops_the_run_and_the_rest_count_as_failures(world, tmp_path):
    out = tmp_path / "run"
    samples, outcomes = world.run(out, max_requests=4)
    assert len(world.calls) == 4
    skipped = [o for per in outcomes.values() for o in per.values()
               if o["status"] == "not_attempted"]
    assert skipped
    report = world.report(out, samples, outcomes)
    for provider in report["providers"].values():
        assert provider["criterion_a"]["overall"]["total"] == 30
        assert provider["criterion_a"]["overall"]["correct"] < 30
        assert provider["errors"]["statuses"].get("not_attempted", 0) > 0


def test_attempts_must_be_bounded(world, tmp_path):
    with pytest.raises(BenchError, match="at least 1"):
        world.run(tmp_path / "run", max_attempts=0)


def test_a_missing_recording_is_a_failed_item_not_a_smaller_denominator(world, tmp_path):
    check = world.check([r for r in world.rows if r["item_id"] != "CV-30"])
    out = tmp_path / "run"
    samples, outcomes = world.run(out, check=check)
    report = world.report(out, samples, outcomes, check=check)
    a = report["providers"]["scribe"]["criterion_a"]["overall"]
    assert a["total"] == 30
    assert not report["acceptance_eligible"]
    assert any("missing: CV-30" in r for r in report["ineligible_reasons"])


# --- vendor scores vs application view vs fallback --------------------------------------------

def test_a_fallback_never_contaminates_a_vendor_score(world, tmp_path):
    """Scribe loops on CV-06: app.speech would use Gemini's transcript, but Scribe's own
    score stays a failure and Gemini's stays its own."""
    world.says[("scribe", "CV-06")] = "ghar ghar ghar ghar ghar ghar ghar ghar"
    out = tmp_path / "run"
    samples, outcomes = world.run(out)
    report = world.report(out, samples, outcomes)
    assert "CV-06" in [f["id"] for f in report["providers"]["scribe"]["criterion_a"]["failures"]]
    assert report["providers"]["gemini"]["criterion_a"]["overall"]["correct"] == 30
    assert report["providers"]["scribe"]["criterion_a"]["overall"]["correct"] == 29
    chain = report["pipeline_replay"]
    assert chain["voice_correct"] == 30  # the chain recovered it via Gemini
    assert chain["by_provider"] == {"elevenlabs": 29, "gemini": 1}
    private = stt_bench.evaluate(world.check(), stt_bench.build_gold(
        list(ITEMS.values()), {}, world.check().rows, DEMO)[0], samples, outcomes,
        stt_bench.Cache(out / "cache"), GAZETTEER, bench=BENCH)["records"]
    cv06 = next(r for r in private["scribe"]["voice"] if r["id"] == "CV-06")
    assert cv06["app_view"] == "repetition"  # the application would have rejected it


def test_wrong_but_plausible_scribe_output_is_not_rescued_by_the_chain(world, tmp_path):
    world.says[("scribe", "CV-06")] = "DHA Phase 6 mein 300 gaz ka ghar, budget aath crore tak"
    out = tmp_path / "run"
    samples, outcomes = world.run(out)
    report = world.report(out, samples, outcomes)
    assert report["pipeline_replay"]["voice_correct"] == 29  # accepted, wrong, no fallback
    assert report["providers"]["gemini"]["criterion_a"]["overall"]["correct"] == 30


def test_both_providers_failing_replays_as_failed(world, tmp_path):
    world.fail[("scribe", "CV-09")] = [500, 500]
    world.fail[("gemini", "CV-09")] = [500, 500]
    out = tmp_path / "run"
    samples, outcomes = world.run(out)
    report = world.report(out, samples, outcomes)
    assert report["pipeline_replay"]["voice_correct"] == 29


# --- controls --------------------------------------------------------------------------------

def test_controls_never_pass_the_gate_without_the_requirement_path(world, tmp_path):
    out = tmp_path / "run"
    samples, outcomes = world.run(out)
    report = world.report(out, samples, outcomes)
    assert report["control_gate"]["status"] == "unverified"
    assert "not substituted" in report["control_gate"]["reason"] or \
        "not run" in report["control_gate"]["reason"]
    for provider in report["providers"].values():
        controls = {c["id"]: c for c in provider["controls"]}
        assert set(controls) == {"VC-01", "VC-02", "VC-03", "VC-04", "VC-05"}
        assert all(c["signals"] == [] for c in controls.values())
    gemini = {c["id"]: c for c in report["providers"]["gemini"]["controls"]}
    assert gemini["VC-01"]["app_view"] == "no_speech"


def test_a_hallucinated_requirement_on_silence_is_surfaced_and_the_gate_stays_open(
        world, tmp_path):
    world.says[("gemini", "VC-01")] = "DHA phase 6 mein ghar chahiye budget aath crore tak"
    out = tmp_path / "run"
    samples, outcomes = world.run(out)
    report = world.report(out, samples, outcomes)
    signals = {c["id"]: c["signals"] for c in report["providers"]["gemini"]["controls"]}
    assert set(signals["VC-01"]) >= {"location", "budget"}
    assert report["providers"]["scribe"]["controls"][0]["signals"] == []
    assert report["control_gate"]["status"] == "unverified"
    # the application replay still stops Scribe's silence at no_speech: Gemini never runs
    chain = {c["id"]: c for c in report["pipeline_replay"]["controls"]}
    assert chain["VC-01"]["status"] == "no_speech" and chain["VC-01"]["signals"] == []


def test_music_only_scribe_output_is_no_speech(world, tmp_path):
    body = {"language_code": "eng", "language_probability": 0.5, "text": "(music)",
            "words": [{"text": "(music)", "type": "audio_event"}]}
    world.says[("scribe", "VC-03")] = body
    out = tmp_path / "run"
    samples, outcomes = world.run(out)
    report = world.report(out, samples, outcomes)
    music = next(c for c in report["providers"]["scribe"]["controls"] if c["id"] == "VC-03")
    assert music["app_view"] == "no_speech" and music["signals"] == []


# --- plan, config, privacy ---------------------------------------------------------------------

def test_the_dry_run_plan_counts_requests_seconds_and_retries(world):
    adapters = world.adapters()
    planned = stt_bench.plan(BENCH, adapters, max_attempts=2)
    assert planned["basis"].startswith("planned")
    assert planned["samples"] == 35 and planned["voice"] == 30 and planned["controls"] == 5
    for provider in planned["providers"].values():
        assert provider["requests_min"] == 35 and provider["requests_max"] == 70
    # 30 voice target seconds + controls (VC-05 cut to 120 s)
    expected = sum(min(i["recording"]["target_seconds"], 120) for i in ITEMS.values())
    assert planned["audio_seconds_per_provider"] == expected
    with_manifest = stt_bench.plan(
        BENCH, adapters, samples=stt_bench.prepare_samples(world.check(), world.dir, BENCH),
        max_attempts=2)
    assert with_manifest["basis"] == "manifest"
    assert with_manifest["providers"]["scribe"]["estimated_cost_usd"] is None


def test_cost_is_only_estimated_from_a_priced_file_with_a_source():
    slot = {"audio_seconds_sent": 3600.0, "input_tokens": 1_000_000, "output_tokens": 0,
            "thinking_tokens": 0}
    assert stt_bench._cost("scribe", slot, None) is None
    unsourced = {"scribe_usd_per_audio_hour": 0.22}
    assert stt_bench._cost("scribe", slot, unsourced) is None
    priced = {**unsourced, "source": "invoice", "verified_on": "2026-10-20"}
    assert stt_bench._cost("scribe", slot, priced) == 0.22


def test_settings_come_from_the_environment_and_never_leak(world, tmp_path):
    with pytest.raises(BenchError, match="ELEVENLABS_API_KEY"):
        stt_bench.providers_from_env({"GEMINI_API_KEY": "x", "LLM_PRIMARY_MODEL": MODEL},
                                     KEYTERMS)
    with pytest.raises(BenchError, match="valid model ID"):
        stt_bench.providers_from_env({**world.environ(), "LLM_PRIMARY_MODEL": "a b"}, KEYTERMS)
    with pytest.raises(BenchError, match="LLM_THINKING_LEVEL"):
        stt_bench.providers_from_env({**world.environ(), "LLM_THINKING_LEVEL": "max"}, KEYTERMS)
    off = stt_bench.providers_from_env({**world.environ(), "LLM_THINKING_LEVEL": "off"},
                                       KEYTERMS)
    assert off[1].config.settings["thinking_level"] is None
    out = tmp_path / "run"
    samples, outcomes = world.run(out)
    report = world.report(out, samples, outcomes)
    public = json.dumps(report) + stt_bench.render_markdown(report)
    for secret in (KEY_SCRIBE, KEY_GEMINI):
        assert secret not in public
        assert secret not in (out / "run.json").read_text()
        assert secret not in (out / "journal.jsonl").read_text()
    for item in BENCH[0]:  # no transcript text in the public report
        assert item["gold_transcript"]["roman"] not in public
    assert not re.search(r"\d{10,}|://", public)


def test_the_cli_run_requires_confirmation_and_writes_the_reports(world, tmp_path, capsys):
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(world.manifest())
    out = tmp_path / "cli"
    base = ["--manifest", str(manifest), "--audio-dir", str(world.dir), "--out", str(out),
            "--gold", str(tmp_path / "none.json")]
    assert stt_bench.main(["run", *base], environ=world.environ(),
                          transport=world.transport()) == 1
    assert "--confirm-live" in capsys.readouterr().err
    assert world.calls == []
    assert stt_bench.main(["run", *base, "--confirm-live", "--synthetic"],
                          environ=world.environ(), transport=world.transport()) == 0
    report = json.loads((out / "report.json").read_text())
    assert report["synthetic"] and not report["acceptance_eligible"]
    assert (out / "report.md").is_file() and (out / "private_detail.json").is_file()
    assert stt_bench.main(["report", *base, "--synthetic"], environ=world.environ(),
                          transport=world.transport()) == 0
    assert len(world.calls) == 70  # `report` made no request


def test_keyterms_follow_the_production_rules():
    terms = stt_bench.build_keyterms()
    assert len(terms) <= 100 and terms[:3] == ["DHA", "Bahria Town", "Clifton"]
    assert all(speech._keyterm_ok(t) for t in terms)
    assert len({t.lower() for t in terms}) == len(terms)


def test_percentiles_are_nearest_rank():
    assert stt_bench.percentile([], 90) is None
    assert stt_bench.percentile([5, 1, 3, 2, 4], 50) == 3
    assert stt_bench.percentile(list(range(1, 11)), 90) == 9


def test_the_voice_gold_verified_file_is_not_committed_before_recordings_exist():
    """Until a human verifies a real recording there is nothing to commit: an empty
    or absent file means every run is INDICATIVE."""
    entries = stt_bench.load_verified()
    assert entries == {} or all(e.get("audio_sha256") for e in entries.values())


# --- scoring review: items without critical slots, gold-free location, corruption --------------

NO_SLOT = ("CV-27", "CV-29", "CV-30")


def test_exactly_three_items_have_no_critical_gold_slot():
    have = [i["id"] for i in BENCH[0] if any(k in stt_bench.CRITICAL_SLOTS for k in gold(i["id"]))]
    assert [i["id"] for i in BENCH[0] if i["id"] not in have] == list(NO_SLOT)
    assert len(have) == 27


def test_r16_thresholds_are_unchanged():
    assert (stt_bench.THRESHOLD_OVERALL, stt_bench.THRESHOLD_QUIET) == (0.85, 0.90)
    assert stt_bench.CRITICAL_SLOTS == ("purpose", "budget", "size", "location")


@pytest.mark.parametrize("item_id", NO_SLOT)
def test_a_clean_accepted_transcript_passes_an_item_without_critical_slots(item_id):
    text = ITEMS[item_id]["gold_transcript"]["roman"]
    slots, ok, vacuous, why = stt_bench.judge_item(text, gold(item_id), GAZETTEER)
    assert (slots, ok, vacuous, why) == ({}, True, True, "clean")


@pytest.mark.parametrize(("item_id", "text", "accepted", "reason"), [
    ("CV-27", "", True, "no_transcript"),
    ("CV-29", "", True, "no_transcript"),
    ("CV-30", "Agent se baat karao please", False, "rejected"),
    ("CV-27", "ghar chahiye DHA phase 6 mein", True, "invented:location"),
    ("CV-27", "ghar chahiye budget aath crore tak", True, "invented:budget"),
    ("CV-29", "mujhe visit karni hai, kal shaam, rent pe", True, "invented:purpose"),
    ("CV-30", "agent se baat karao, 500 gaz ka ghar", True, "invented:size"),
    ("CV-30", "agent se baat karao Clifton rent pe do crore", True,
     "invented:purpose,budget,location"),
])
def test_an_item_without_critical_slots_fails_without_clean_accepted_speech(
        item_id, text, accepted, reason):
    _, ok, vacuous, why = stt_bench.judge_item(text, gold(item_id), GAZETTEER,
                                               accepted=accepted)
    assert (ok, vacuous, why) == (False, True, reason)


def test_a_word_that_merely_looks_like_a_place_is_not_a_location():
    """CV-30 "karao": the resolver offers Karachi at 0.4 (fuzzy); that is not a stated place."""
    assert stt_bench.find_locations("Agent se baat karao please", GAZETTEER) == []


def run_items(world, tmp_path, says=None, fail=None, **kwargs):
    world.says.update(says or {})
    world.fail.update(fail or {})
    out = tmp_path / "run"
    samples, outcomes = world.run(out, **kwargs)
    return world.report(out, samples, outcomes)


def no_slot_status(report, provider):
    return {i["id"]: (i["correct"], i["reason"])
            for i in report["providers"][provider]["criterion_a"]["no_critical_slot_items"]}


def test_no_slot_items_pass_end_to_end_only_through_successful_processing(world, tmp_path):
    report = run_items(world, tmp_path)
    for provider in ("scribe", "gemini"):
        assert no_slot_status(report, provider) == {i: (True, "clean") for i in NO_SLOT}
        a = report["providers"][provider]["criterion_a"]
        assert a["overall"]["total"] == 30 and a["overall"]["correct"] == 30
        assert a["critical_slot_diagnostic"]["total"] == 27


@pytest.mark.parametrize(("kind", "why"), [
    ("http_500", "no_transcript"), ("http_401", "no_transcript"),
    ("no_speech", "no_transcript"), ("malformed", "no_transcript"),
    ("loop", "rejected"), ("invent", "invented:purpose,budget,location"),
])
def test_no_slot_items_do_not_pass_on_failure_silence_or_inventions(world, tmp_path, kind, why):
    fail, says = {}, {}
    for item in NO_SLOT:
        if kind.startswith("http_"):
            fail[("scribe", item)] = [int(kind[5:])] * 2
        elif kind == "no_speech":
            says[("scribe", item)] = {"language_code": "eng", "language_probability": 0.9,
                                      "text": "", "words": []}
        elif kind == "malformed":
            says[("scribe", item)] = {"unexpected": True}
        elif kind == "loop":
            says[("scribe", item)] = "ghar ghar ghar ghar ghar ghar ghar ghar"
        else:
            says[("scribe", item)] = "DHA phase 6 mein ghar kiraye pe, budget aath crore tak"
    report = run_items(world, tmp_path, says=says, fail=fail)
    scribe = no_slot_status(report, "scribe")
    assert all(v == (False, why) for v in scribe.values()), scribe
    a = report["providers"]["scribe"]["criterion_a"]
    assert a["overall"] == {"correct": 27, "total": 30, "rate": 0.9}
    assert a["critical_slot_diagnostic"]["correct"] == 27
    assert a["critical_slot_diagnostic"]["total"] == 27
    assert report["providers"]["gemini"]["criterion_a"]["overall"]["correct"] == 30


def test_a_missing_recording_never_passes_an_item_without_slots(world, tmp_path):
    check = world.check([r for r in world.rows if r["item_id"] not in NO_SLOT])
    out = tmp_path / "run"
    samples, outcomes = world.run(out, check=check)
    report = world.report(out, samples, outcomes, check=check)
    for provider in ("scribe", "gemini"):
        assert no_slot_status(report, provider) == {i: (False, "no_transcript") for i in NO_SLOT}
        assert report["providers"][provider]["criterion_a"]["overall"]["total"] == 30


def test_no_request_made_never_passes_anything(world, tmp_path):
    samples, outcomes = world.run(tmp_path / "run", max_requests=0)
    report = world.report(tmp_path / "run", samples, outcomes)
    assert world.calls == []
    for provider in ("scribe", "gemini"):
        a = report["providers"][provider]["criterion_a"]
        assert a["overall"] == {"correct": 0, "total": 30, "rate": 0.0}
        assert a["critical_slot_diagnostic"]["correct"] == 0
        assert not any(ok for ok, _ in no_slot_status(report, provider).values())


def test_the_official_result_and_the_27_item_diagnostic_are_separate(world, tmp_path):
    world.fail[("gemini", "CV-06")] = [500, 500]
    report = run_items(world, tmp_path)
    a = report["providers"]["gemini"]["criterion_a"]
    assert a["overall"]["total"] == 30 and a["overall"]["correct"] == 29
    assert a["critical_slot_diagnostic"]["total"] == 27
    assert a["critical_slot_diagnostic"]["correct"] == 26
    assert "not R16" in a["critical_slot_diagnostic"]["note"]
    markdown = stt_bench.render_markdown(report)
    assert "Diagnostic (not R16's criterion)" in markdown
    assert "items without critical gold slots" in markdown


# location scoring never uses gold to pick the span

def test_the_span_finder_has_no_gold_input():
    import inspect
    assert list(inspect.signature(stt_bench.find_locations).parameters) == ["text", "gazetteer"]
    assert list(inspect.signature(stt_bench.find_location).parameters) == ["text", "gazetteer"]


def test_the_resolver_is_asked_the_same_questions_whatever_the_gold(monkeypatch):
    text = "DHA Phase 6 mein 500 gaz ka ghar, budget aath crore tak"
    calls = []
    real = loc.resolve

    def spy(span, area, gazetteer, **kw):
        calls.append((span, area))
        return real(span, area, gazetteer, **kw)

    monkeypatch.setattr(stt_bench.loc, "resolve", spy)
    runs = []
    for item_id in ("CV-06", "CV-26", "CV-01", "CV-16"):
        calls.clear()
        stt_bench.score_location(text, gold(item_id), GAZETTEER)
        runs.append(list(calls))
    assert runs[0] and all(r == runs[0] for r in runs)
    words = text.split()
    windows = {" ".join(words[i:j]) for i in range(len(words))
               for j in range(i + 1, len(words) + 1)}
    assert all(span in windows and area is None for span, area in runs[0])


def test_the_selected_place_does_not_change_with_the_gold():
    text = "Clifton mein 500 gaz ka ghar, budget aath crore tak"
    found = stt_bench.find_locations(text, GAZETTEER)
    assert [list(p.nodes) for p in found] == [["clf"]]
    for item_id in ("CV-06", "CV-26", "CV-01"):  # golds that would "want" other places
        assert stt_bench.score_location(text, gold(item_id), GAZETTEER)["correct"] is False
    wants_clifton = {"location": {"status": "RESOLVED", "value": ["clf"]}}
    assert stt_bench.score_location(text, wants_clifton, GAZETTEER)["correct"]


@pytest.mark.parametrize(("text", "reason"), [
    ("Clifton mein 500 gaz ka ghar", "different_location"),  # wrong place
    ("500 gaz ka ghar chahiye budget aath crore tak", "missing"),  # omitted
    ("DHA Phase 6 aur Clifton mein 500 gaz ka ghar", "several_locations"),  # multiple
    ("DHA Phase 6 ya DHA Phase 7 mein ghar", "several_locations"),
    ("DHA Phase 6 mein ghar, Bahria Town bhi", "several_locations"),  # an invented second
    ("Karachi mein ghar chahiye", "different_location"),  # broader than gold
])
def test_wrong_omitted_multiple_and_invented_locations_fail(text, reason):
    result = stt_bench.score_location(text, gold("CV-06"), GAZETTEER)
    assert result == {"correct": False, "reason": reason}


def test_one_correct_place_said_twice_is_one_place():
    text = "DHA Phase 6 mein ghar chahiye, haan DHA Phase 6 mein"
    assert stt_bench.score_location(text, gold("CV-06"), GAZETTEER)["correct"]


def test_an_invented_location_on_an_item_without_one_is_an_invented_requirement():
    _, ok, _, why = stt_bench.judge_item("ghar chahiye Clifton mein", gold("CV-27"), GAZETTEER)
    assert (ok, why) == (False, "invented:location")


@pytest.mark.parametrize(("text", "ok", "reason"), [
    ("Block six mein office chahiye rent pe", True, "match"),
    ("Clifton block six mein office chahiye rent pe", False, "resolved_instead_of_ambiguous"),
    ("Block six aur Clifton mein office rent pe", False, "several_locations"),
    ("office chahiye rent pe", False, "missing"),
])
def test_cv26_must_stay_ambiguous(text, ok, reason):
    result = stt_bench.score_location(text, gold("CV-26"), GAZETTEER)
    assert result == {"correct": ok, "reason": reason}


# corrupted transcripts, one slot at a time

CORRUPTIONS = [
    # (item, find, replace, the slot that must fail)
    ("CV-06", "aath crore", "saat crore", "budget"),
    ("CV-06", "aath crore", "aath lakh", "budget"),
    ("CV-06", "crore tak", "crore", "budget"),  # mode: max -> around
    ("CV-06", "budget aath crore tak", "budget", "budget"),  # price dropped
    ("CV-06", "500 gaz", "300 gaz", "size"),
    ("CV-06", "500 gaz", "500 marla", "size"),  # unit
    ("CV-06", "500 gaz", "500", "size"),  # unit lost
    ("CV-06", "500 gaz ka ", "", "size"),  # omitted
    ("CV-06", "DHA Phase 6", "DHA Phase 7", "location"),
    ("CV-06", "DHA Phase 6 mein ", "", "location"),
    ("CV-01", "on rent", "for sale", "purpose"),
    ("CV-13", "kiraye pe ", "", "purpose"),  # said nowhere, though inferable
    ("CV-08", "kiraye pe", "khareedne ke liye", "purpose"),
    ("CV-28", "paanch lakh", "paanch crore", "purpose"),  # inferred rent -> sale
]


@pytest.mark.parametrize(("item_id", "find", "replace", "slot"), CORRUPTIONS)
def test_a_corrupted_critical_slot_fails_exactly_that_item(item_id, find, replace, slot):
    spoken = ITEMS[item_id]["gold_transcript"]["roman"]
    ok = stt_bench.judge_item(spoken, gold(item_id), GAZETTEER)
    assert ok[1], ok[0]
    assert find in spoken, (item_id, find)
    slots, correct, _, _ = stt_bench.judge_item(spoken.replace(find, replace), gold(item_id),
                                                GAZETTEER)
    assert not correct
    assert slots[slot]["correct"] is False, slots


def test_the_price_period_comes_from_the_transcript_not_the_gold():
    """CV-09: rent, monthly. Say sale and drop "mahana": purpose and price follow the words."""
    spoken = ITEMS["CV-09"]["gold_transcript"]["roman"]
    assert stt_bench.judge_item(spoken, gold("CV-09"), GAZETTEER)[1]
    sale = spoken.replace("rent pe", "sale pe").replace(" mahana", "")
    slots = stt_bench.judge_item(sale, gold("CV-09"), GAZETTEER)[0]
    assert not slots["purpose"]["correct"] and not slots["budget"]["correct"]


@pytest.mark.parametrize(("item_id", "find", "replace", "slot"), CORRUPTIONS[::3])
def test_provider_corruption_reaches_the_report_end_to_end(world, tmp_path, item_id, find,
                                                           replace, slot):
    spoken = ITEMS[item_id]["gold_transcript"]["roman"]
    report = run_items(world, tmp_path, says={("gemini", item_id): spoken.replace(find, replace)})
    a = report["providers"]["gemini"]["criterion_a"]
    assert a["overall"]["correct"] == 29
    failure = next(f for f in a["failures"] if f["id"] == item_id)
    assert slot in failure["slots"]
    assert a["by_slot"][slot]["correct"] == a["by_slot"][slot]["total"] - 1
    assert report["providers"]["scribe"]["criterion_a"]["overall"]["correct"] == 30


@pytest.mark.parametrize("failure", ["http_500", "http_401", "malformed", "refused"])
def test_provider_errors_count_every_slot_as_incorrect(world, tmp_path, failure):
    item = "CV-06"
    if failure.startswith("http_"):
        world.fail[("gemini", item)] = [int(failure[5:])] * 2
    elif failure == "malformed":
        world.says[("gemini", item)] = {"candidates": []}
    else:
        world.says[("gemini", item)] = gemini_reply("x", True, "ur", reason="SAFETY")
    out = tmp_path / "run"
    samples, outcomes = world.run(out)
    report = world.report(out, samples, outcomes)
    a = report["providers"]["gemini"]["criterion_a"]
    assert a["overall"] == {"correct": 29, "total": 30, "rate": 29 / 30}
    failed = next(f for f in a["failures"] if f["id"] == item)
    assert failed["status"] == "error"
    assert set(failed["slots"].values()) == {"no_transcript"}
    assert set(failed["slots"]) == {"purpose", "budget", "size", "location"}
    assert report["providers"]["gemini"]["errors"]["statuses"]["error"] == 1


# --- recording checklist ------------------------------------------------------------------------

def test_the_checklist_lists_all_35_recordings_exactly_as_planned():
    rows = stt_bench.checklist_rows()
    assert [r["id"] for r in rows] == list(ITEMS)
    assert len(rows) == 35 and sum(r["kind"] == "control" for r in rows) == 5
    text = stt_bench.render_checklist()
    for item_id, item in ITEMS.items():
        planned = item["recording"]
        row = next(line for line in text.splitlines() if line.startswith(f"| {item_id} |"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[2:5] == [planned["speaker"], planned["phone"], planned["noise"]]
        assert cells[5].startswith(f"{planned['target_seconds']} s")
        if item["set"] == "customer_voice":
            assert item["script"].replace("|", "/") in cells[1]
        elif item.get("script"):
            assert item["script"] in cells[1]
        assert item["note"].split(".")[0] in text if item["set"] == "control" else True


def test_the_checklist_never_claims_verification_or_carries_private_data():
    text = stt_bench.render_checklist()
    assert "Nothing here is verified gold" in text
    assert "planned **scripts**" in text or "planned scripts" in text
    assert not re.search(r"\d{10,}|://|@", text)
    for step in ("bench_manifest", "voice_manifest.csv", "voice_gold_verified.json",
                 "as_scripted", "audio_sha256", "<item_id>.ogg", "validate"):
        assert step in text


def test_the_committed_checklist_is_current():
    committed = (stt_bench.REPO / "docs" / "STT_RECORDING_CHECKLIST.md").read_text("utf-8")
    assert committed.replace("\r\n", "\n") == stt_bench.render_checklist().replace("\r\n", "\n")
