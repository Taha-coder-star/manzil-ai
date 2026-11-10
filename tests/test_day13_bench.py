"""Money, evidence boundaries and formal coverage; all provider traffic is mocked."""

import copy
import csv
import io
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app import bench_live, day13_bench, day13_report
from app.bench_budget import (
    BoundedGemini,
    BudgetError,
    Ledger,
    amount,
    load_policy,
    runtime_budget,
    token_cost,
)


def policy():
    return {"schema": 1, "currency": "USD", "source": "https://example.test/official",
            "verified_on": datetime.now(UTC).date().isoformat(), "account_verified": True,
            "gold_sha256": "a" * 64,
            "models": {"m": {"input_per_mtok": "1", "cached_input_per_mtok": "0.25",
                "output_per_mtok": "4", "max_input_tokens": 1000, "max_output_tokens": 100,
                "model_input_token_limit": 1000,
                "max_request_bytes": 10000, "count_margin_tokens": 32,
                "cache_min_tokens": 100, "bounds_source": "https://example.test/bounds",
                "count_tokens_free": True, "output_includes_thinking": True,
                "single_candidate_default_verified": True,
                "count_before_generate": True,
                "thinking_levels": ["minimal", "low", "omitted"]}}}


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "-Infinity", "-1", None])
def test_invalid_money_never_reaches_a_reservation(value):
    with pytest.raises(ValueError):
        amount(value)


def test_crash_and_reopen_does_not_reset_budget_or_unknown_charges(tmp_path):
    path = tmp_path / "budget.sqlite"
    first = Ledger(path, policy())
    first.reserve("tuning", "gemini", "m", 1_500_000)  # simulate crash before settlement
    reopened = Ledger(path, policy())
    with pytest.raises(BudgetError):
        reopened.reserve("tuning", "scribe", "s", 500_001)
    assert reopened.summary()["unknown_or_inflight"] == 1
    assert reopened.summary()["charged_usd"] == "1.5"
    reopened.reserve("formal", "gemini", "m", 7_000_000)
    reopened.reserve("retry", "gemini", "m", 1_000_000)
    with pytest.raises(BudgetError):
        reopened.reserve("retry", "gemini", "m", 1)


def test_two_processes_cannot_double_spend_the_same_remaining_phase(tmp_path):
    path = tmp_path / "budget.sqlite"
    data = policy()
    Ledger(path, data)
    code = ("import json,sys; from app.bench_budget import Ledger,BudgetError; "
            "b=Ledger(sys.argv[1],json.loads(sys.argv[2]));\n"
            "try: b.reserve('tuning','gemini','m',1250000); print('reserved')\n"
            "except BudgetError: print('refused')\n")
    processes = [subprocess.Popen([sys.executable, "-c", code, str(path), json.dumps(data)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
    outputs = []
    for proc in processes:
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr
        outputs.append(stdout.strip())
    assert sorted(outputs) == ["refused", "reserved"]
    assert Ledger(path, data).summary()["charged_usd"] == "1.25"


def test_settlement_is_once_only_and_bound_violation_freezes_all_phases(tmp_path):
    ledger = Ledger(tmp_path / "b.sqlite", policy())
    rid = ledger.reserve("tuning", "gemini", "m", 100)
    ledger.settle(rid, actual="0.000101")
    with pytest.raises(ValueError):
        ledger.settle(rid, actual=0)
    with pytest.raises(BudgetError):
        ledger.reserve("formal", "gemini", "m", 1)
    assert ledger.summary()["bound_exceeded"]


def test_pricing_cannot_change_but_new_gold_cohort_keeps_all_prior_spend(tmp_path):
    path = tmp_path / "b.sqlite"
    Ledger(path, policy()).reserve("tuning", "gemini", "m", 123)
    changed = policy()
    changed["gold_sha256"] = "b" * 64
    assert Ledger(path, changed).summary()["charged_usd"] == "0.000123"
    changed["models"]["m"]["output_per_mtok"] = "5"
    with pytest.raises(ValueError):
        Ledger(path, changed)


def test_pricing_requires_recent_verified_account_and_combined_thinking_bound(tmp_path):
    path = tmp_path / "p.json"
    for key, value in (("account_verified", False), ("verified_on", "2000-01-01")):
        data = policy()
        data[key] = value
        day13_bench.write(path, data)
        with pytest.raises(ValueError):
            load_policy(path)
    data = policy()
    data["models"]["m"]["output_includes_thinking"] = False
    day13_bench.write(path, data)
    with pytest.raises(ValueError):
        load_policy(path)


def test_missing_cache_and_thinking_billing_are_unknown_not_free():
    rates = policy()["models"]["m"]
    usage = {"input_tokens": 1000, "cached_input_tokens": 500,
             "output_tokens": 100, "thinking_tokens": 10}
    assert token_cost(rates, usage) == Decimal("0.001065")
    for key in usage:
        assert token_cost(rates, {**usage, key: None}) is None


def test_identical_request_is_counted_and_output_is_bounded_before_paid_dispatch(tmp_path):
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 100})
        return httpx.Response(200, json={"candidates": [], "usageMetadata": {
            "promptTokenCount": 100, "candidatesTokenCount": 5,
            "thoughtsTokenCount": 2, "cachedContentTokenCount": 0}})
    ledger = Ledger(tmp_path / "b.sqlite", policy())
    with_client = BoundedGemini("test", ledger=ledger, phase="tuning",
                                transport=httpx.MockTransport(respond))
    try:
        with_client.generate("m", system="private prompt", tools=[], contents=[],
                             timeout_s=5, thinking_level="low")
    finally:
        with_client.close()
    assert len(requests) == 2
    assert requests[0][1]["generateContentRequest"]["systemInstruction"] == requests[1][1]["systemInstruction"]
    assert requests[1][1]["generationConfig"]["maxOutputTokens"] == 100
    assert "candidateCount" not in requests[1][1]["generationConfig"]
    summary = ledger.summary()
    assert summary["charged_usd"] == "0.000128"
    assert "private prompt" not in json.dumps(summary)


@pytest.mark.parametrize("count", [None, -1, 969, True])
def test_unusable_or_oversized_count_never_dispatches_paid_call(tmp_path, count):
    seen = []
    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"totalTokens": count})
    client = BoundedGemini("test", ledger=Ledger(tmp_path / "b.sqlite", policy()),
                           phase="tuning", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(BudgetError):
            client.generate("m", system="x", tools=[], contents=[], timeout_s=5)
        assert len(seen) == 1 and seen[0].endswith(":countTokens")
        assert not client.ledger.summary()["requests"]
    finally:
        client.close()


def test_provider_failure_keeps_full_reservation(tmp_path):
    def respond(request):
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 50})
        return httpx.Response(500)
    client = BoundedGemini("test", ledger=Ledger(tmp_path / "b.sqlite", policy()),
                           phase="formal", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(bench_live.ProviderError):
            client.generate("m", system="x", tools=[], contents=[], timeout_s=5)
        assert client.ledger.summary()["charged_usd"] == "0.0014"
        assert client.ledger.summary()["unknown_or_inflight"] == 1
    finally:
        client.close()


def test_full_context_reservation_does_not_require_an_extra_count_roundtrip(tmp_path):
    data = policy()
    data["models"]["m"].update(count_before_generate=False, model_input_token_limit=2000)
    seen = []
    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"candidates": [], "usageMetadata": {
            "promptTokenCount": 100, "candidatesTokenCount": 5}})
    client = BoundedGemini("test", ledger=Ledger(tmp_path / "b.sqlite", data), phase="tuning",
                           transport=httpx.MockTransport(respond))
    try:
        client.generate("m", system="x", tools=[], contents=[], timeout_s=5)
        assert len(seen) == 1 and seen[0].endswith(":generateContent")
        row = client.ledger.summary()["requests"][0]
        assert row["bound"] == 2400  # full model input context + combined output cap
        assert row["actual"] is None  # omitted metadata is still unknown
        assert row["state"] == "bounded_estimate" and row["charged"] == 500
    finally:
        client.close()


def test_paid_builtin_tools_are_refused_before_any_transport(tmp_path):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(500)
    client = BoundedGemini("test", ledger=Ledger(tmp_path / "b.sqlite", policy()), phase="tuning",
                           transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(BudgetError):
            client._post("https://test/models/m:generateContent", json.dumps({
                "tools": [{"googleSearch": {}}]}).encode(), 5)
        assert not seen
    finally:
        client.close()


def test_ledger_wait_cannot_extend_provider_deadline_or_charge_an_unsent_call(tmp_path):
    data = policy()
    data["models"]["m"]["count_before_generate"] = False
    class SlowLedger(Ledger):
        def reserve(self, *args):
            result = super().reserve(*args)
            time.sleep(.02)
            return result
    seen = []
    client = BoundedGemini("test", ledger=SlowLedger(tmp_path / "b.sqlite", data), phase="tuning",
                           transport=httpx.MockTransport(lambda request: seen.append(request)))
    try:
        with pytest.raises(BudgetError):
            client.generate("m", system="x", tools=[], contents=[], timeout_s=.01)
        assert not seen and client.ledger.summary()["charged_usd"] == "0"
    finally:
        client.close()


def test_mixed_text_voice_inquiry_adds_both_modalities_in_actual_cohort():
    event = {"provider": "gemini", "model": "m", "outcome": "succeeded",
             "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10, "thinking_tokens": 0}
    runs = {name: {"repeats": [{"items": [{"id": name, "group": ["I1"], "metering": [event]}]}
                               for _ in range(3)]} for name in ("text", "voice")}
    metrics = day13_report.metering_metrics(runs, policy(), shared_inquiries=True)
    assert metrics["text_cost_usd"][0]["value"] == .00014
    assert metrics["inquiry_cost_usd"][0]["value"] == .00028


def test_runtime_is_opt_in_and_partial_configuration_refuses_startup():
    settings = SimpleNamespace(llm_primary_model="m", llm_failover_model="m")
    assert runtime_budget(settings, {}) is None
    with pytest.raises(ValueError):
        runtime_budget(settings, {"DAY13_BUDGET_POLICY": "p"})


def candidate():
    return {"commit": "a" * 40, "image": "sha256:" + "b" * 64, "config_sha256": "c" * 64}


def trace():
    return {"schema": 1, "candidate": candidate(), "turns": [{
        "repeat": 1, "item_id": "CT-01", "modality": "text", "status": "succeeded",
        "received_ms": [1000, 4000], "sends": [
            {"kind": "echo", "accepted_ms": 4100},
            {"kind": "substantive", "accepted_ms": 8500, "delivered_ms": 9000}]}]}


def test_earliest_receipt_and_substantive_answer_define_sla_not_fast_echo():
    rows = day13_report.trace_rows(trace(), candidate())
    assert rows[0]["first_substantive_ms"] == 7500
    assert rows[0]["last_receipt_substantive_ms"] == 4500
    assert rows[0]["first_visible_ms"] is None  # acceptance is not client visibility
    assert rows[0]["first_delivered_ms"] == 8000
    assert rows[0]["queue_ms"] is None


def test_failures_stay_in_p90_and_long_voice_notes_are_separate():
    data = trace()
    data["turns"][0].update(modality="voice", item_id="CV-01", duration_ms=81000)
    rows = day13_report.trace_rows(data, candidate())
    assert not rows[0]["sla_eligible"]
    assert day13_report.latency_summary(rows)["voice"]["diagnostic_long_notes"] == 1
    stats = day13_report.quantiles([1, 2, 3, 4, 5, 6, 7, 8, math_inf(), math_inf()])
    assert stats == {"n": 10, "missing_or_failed": 2, "p50": 5, "p90": None}


def math_inf():
    return float("inf")


def test_duplicate_trace_and_cross_image_trace_are_rejected():
    data = trace()
    data["turns"] *= 2
    with pytest.raises(ValueError):
        day13_report.trace_rows(data, candidate())
    with pytest.raises(ValueError):
        day13_report.trace_rows(trace(), {**candidate(), "image": "sha256:" + "d" * 64})


def test_cache_unknown_zero_ineligible_and_hits_are_distinct():
    rates = policy()["models"]
    attempts = [{"model": "m", "usage": {"input_tokens": 200, "cached_input_tokens": n}}
                for n in (None, 0, 100)]
    attempts.append({"model": "m", "usage": {"input_tokens": 50}})
    report = day13_report.cache_summary(attempts, rates)
    assert (report["eligible"], report["unknown"], report["ineligible"], report["hit_calls"]) == (3, 1, 1, 1)
    assert report["hit_rate"] is None
    assert report["cached_input_ratio"] == 0.25


def test_worst_repeat_not_pooled_average_controls_acceptance():
    gate = {"min": 0.95}
    scored = day13_report.gate_result(gate, [
        {"numerator": 30, "denominator": 30}, {"numerator": 30, "denominator": 30},
        {"numerator": 27, "denominator": 30}])
    assert scored["status"] == "fail" and scored["worst_repeat"] == 0.9
    assert day13_report.gate_result(gate, [{"value": 1}] * 2)["status"] == "unmeasured"


def test_pack_contains_only_assignments_and_unverified_templates(tmp_path):
    summary = day13_bench.pack(tmp_path)
    assert summary["speech"] == 30 and summary["controls"] == 5 and summary["paired_text"] == 12
    assert summary["noise"] == {"quiet": 15, "moderate": 10, "street": 5}
    rows = list(csv.DictReader(io.StringIO((tmp_path / "assignments.csv").read_text("utf-8-sig"))))
    assert len({r["speaker"] for r in rows[:30]}) == 6
    assert len({r["android_device"] for r in rows[:30]}) == 3
    assert not list(tmp_path.glob("*.ogg"))
    gold = day13_bench.load(tmp_path / "verified-gold.template.json")
    assert all(v["as_scripted"] is None and not v["audio_sha256"] for v in gold["items"].values())


def test_empty_evidence_cannot_close_day13_and_reports_no_private_strings(tmp_path):
    bundle = tmp_path / "bundle.json"
    day13_bench.write(bundle, {"schema": 1, "private_notes": "sensitive prose not exported"})
    result = day13_report.report(bundle, tmp_path / "report")
    assert result["status"] == "incomplete" and len(result["remaining_gaps"]) > 30
    exported = (tmp_path / "report" / "report.json").read_text("utf-8")
    assert "sensitive prose" not in exported
    assert "iPhone" in exported


def test_scripted_and_partial_reports_never_count_as_formal(tmp_path):
    path = tmp_path / "fake.json"
    for mode in ("dry-run", "live"):
        day13_bench.write(path, {"identity": {"mode": mode}, "status": "finished"})
        blockers = []
        assert not day13_report.validated_runs({"text": path}, "a" * 64, blockers)
        assert any("text:" in b for b in blockers)


def formal_fixture(name):
    return {"status": "finished", "identity": {"mode": "live", "set": name,
        "commit": "a" * 40, "bench_sha256": {name: day13_bench.public_hash(
            day13_bench.benchmark.BENCH_DIR / name) for name in bench_live.IDENTITY_FILES},
        "source_sha256": {"test-fixture.py": "c" * 64}, "demo_sha256": "d" * 64,
        "model": {"primary": "m", "failover": "m", "thinking_level": "low"},
        "day13": {"phase": "formal", "freeze_sha256": "f" * 64,
                  "speech": "m", "policy_sha256": "e" * 64}},
        "repeats_planned": 3, "coverage": {"full_set_complete": True}, "caps": {},
        "repeats": [{"repeat": rep, "items": [{"id": i["id"], "status": "ran",
            "metering_consistent": True, "fresh_stt": True} for i in bench_live.load_set(name)]}
                    for rep in range(1, 4)]}


def test_formal_set_validator_requires_every_actual_case_and_same_model_cohort(tmp_path):
    paths = {}
    for name in bench_live.SETS:
        paths[name] = tmp_path / f"{name}.json"
        day13_bench.write(paths[name], formal_fixture(name))
    blockers = []
    assert len(day13_report.validated_runs(paths, "f" * 64, blockers)) == 5
    assert not blockers
    altered = formal_fixture("voice")
    altered["identity"]["model"]["thinking_level"] = "minimal"
    day13_bench.write(paths["voice"], altered)
    blockers = []
    assert "voice" not in day13_report.validated_runs(paths, "f" * 64, blockers)
    assert any("mixed" in b for b in blockers)
    altered = formal_fixture("voice")
    altered["repeats"][2]["items"].pop()
    day13_bench.write(paths["voice"], altered)
    assert "voice" not in day13_report.validated_runs(paths, "f" * 64, [])


def test_host_preflight_never_prints_unlisted_environment_values():
    from deploy.day13_preflight import inspect_runtime
    row = {"Name": "/deploy-web-1", "Image": "sha256:" + "a" * 64,
           "Config": {"Env": ["GEMINI_API_KEY=private-key", "DATABASE_URL=private-dsn",
                              "AI_PROVIDER=gemini", "LLM_PRIMARY_MODEL=m"]},
           "State": {"Running": True}, "Mounts": []}
    result = inspect_runtime([row, {**row, "Name": "/deploy-worker-1"}])
    assert result["same_image_and_config"] and not result["shared_writable_budget_mount"]
    text = json.dumps(result)
    assert "private-key" not in text and "private-dsn" not in text


def test_freeze_detects_changed_gold_before_any_provider_client(tmp_path):
    fake = {"schema": 1, "spec": day13_bench.load(day13_bench.SPEC),
            "bench_sha256": {p: day13_bench.file_hash(day13_bench.benchmark.BENCH_DIR / p)
                             for p in day13_bench.GOLD_FILES}}
    fake["bench_sha256"]["customer_text.json"] = "0" * 64
    path = tmp_path / "freeze.json"
    day13_bench.write(path, fake)
    with pytest.raises(ValueError, match="gold changed"):
        day13_bench.check_freeze(path)


def test_voice_plan_is_a_real_set_and_not_an_alias_for_controls():
    assert bench_live.plan("voice")["items"] == 30
    assert all(i["id"].startswith("CV-") for i in bench_live.load_set("voice"))


def test_missing_recording_evidence_refuses_fresh_voice_before_clients(tmp_path):
    frozen = {"manifest_sha256": "a" * 64, "verified_gold_sha256": "b" * 64}
    with pytest.raises(ValueError, match="recordings need"):
        day13_bench.recordings(None, None, None, frozen)


def test_legacy_handoff_reason_is_rejected_before_a_paid_benchmark():
    item = copy.deepcopy(next(i for i in bench_live.load_set("voice") if i["id"] == "CV-30"))
    item["expected_tools"][0]["args"]["reason"] = "agent_request"
    errors = []
    day13_bench.benchmark.check_tools(item, errors)
    assert errors and "approved tool schema" in errors[0]


def test_phase_and_policy_bounds_cannot_be_bypassed_with_negative_reservation(tmp_path):
    ledger = Ledger(tmp_path / "b.sqlite", policy())
    for value in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            ledger.reserve("tuning", "gemini", "m", value)
    with pytest.raises(ValueError):
        ledger.reserve("custom", "gemini", "m", 1)


def test_safety_flag_is_in_threshold_freeze_and_not_a_user_metric_override():
    spec = day13_bench.load(day13_bench.SPEC)
    gates = {g["id"]: g for g in spec["gates"]}
    assert gates["ai_after_takeover"]["safety"] is True
    assert gates["wrong_property"]["max"] == 0
    assert gates["text_p90_ms"]["max"] == 7000
    assert gates["voice_cost_usd"]["max"] == 0.07
    assert copy.deepcopy(spec) == spec
