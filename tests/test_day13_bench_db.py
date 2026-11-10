"""Fresh speech + model through the real worker/RLS on a disposable DB; HTTP mocked."""

import json
from types import SimpleNamespace

import httpx

from app import bench_live, day13_trace, speech, stt, stt_bench
from app.bench_budget import BoundedGemini, BoundedScribe, Ledger
from tests.test_day13_bench import candidate as candidate_identity
from tests.test_day13_bench import policy
from tests.test_speech import opus_note
from tests.test_turn_engine import answer, tool, tools_answer


def context(url, tmp_path, *, control=False, exhausted=False, item_id=None):
    data = policy()
    data["models"]["m"]["max_request_bytes"] = 200000
    data["models"]["m"]["max_input_tokens"] = 60000
    data["models"]["m"]["model_input_token_limit"] = 60000
    data["scribe"] = {"per_hour": "0.5", "keyterms_per_hour": "0.1",
                      "minimum_seconds": 20, "round_to_seconds": 1}
    ledger = Ledger(tmp_path / "budget.sqlite", data)
    if exhausted:
        ledger.reserve("tuning", "gemini", "m", 2_000_000)
    seen = {"scribe": 0, "model": 0}
    items = bench_live.load_set("controls" if control else "voice")
    item = next(i for i in items if i["id"] == item_id) if item_id else items[0]
    rounds = list(bench_live.gold_rounds(item))
    def scribe_reply(request):
        seen["scribe"] += 1
        text = "" if control else item["script"]
        return httpx.Response(200, json={"text": text, "language_code": "eng",
            "language_probability": .99,
            "words": ([{"type": "audio_event", "text": "silence"}] if control
                      else [{"type": "word", "text": word} for word in text.split()])})
    def model_reply(request):
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 100})
        seen["model"] += 1
        if "responseSchema" in json.loads(request.content).get("generationConfig", {}):
            raw = tools_answer({"text": '{"summary":"Agent requested"}'})
            raw["usageMetadata"] = {"promptTokenCount": 100, "candidatesTokenCount": 10,
                                    "thoughtsTokenCount": 0, "cachedContentTokenCount": 0}
            return httpx.Response(200, json=raw)
        calls = rounds.pop(0) if rounds else []
        raw = tools_answer(*(tool(name, args) for name, args in calls)) if calls else answer()
        raw["usageMetadata"] = {"promptTokenCount": 100, "candidatesTokenCount": 10,
                                "thoughtsTokenCount": 0, "cachedContentTokenCount": 0}
        return httpx.Response(200, json=raw)
    model = BoundedGemini("test", ledger=ledger, phase="tuning",
                          transport=httpx.MockTransport(model_reply))
    primary = BoundedScribe(stt.ScribeClient("test", transport=httpx.MockTransport(scribe_reply)),
                            ledger=ledger, phase="tuning")
    args = SimpleNamespace(database_url=url, primary="m", failover="m", thinking_level="low",
                           set="controls" if control else "voice", audio_dir=str(tmp_path))
    ctx = bench_live.RunContext(args, bench_live.MeteredProvider(
        model, bench_live.LedgerGuard(ledger, 50)), None, live=True)
    ctx.clients = ctx.budget_clients = [model, primary]
    ctx.speech_factory = lambda storage: speech.SpeechService(
        primary, storage, fallback=model, fallback_model="m", thinking_level="low")
    ctx.voice_gold = {item["id"]: stt_bench.Gold(item["id"], "verified",
        item["gold_transcript"]["roman"], item["gold_extraction"], False)}
    audio = opus_note(8)
    (tmp_path / f"{item['id']}.ogg").write_bytes(audio)
    ctx.recording_check = SimpleNamespace(rows={item["id"]: SimpleNamespace(
        sha256=stt_bench.sha256_bytes(audio), noise="quiet", duration_ms=8000)})
    return ctx, item, seen


def test_fresh_voice_is_scored_with_stt_and_all_model_attempts_metered(test_database_url, tmp_path):
    ctx, item, seen = context(test_database_url, tmp_path)
    try:
        first = bench_live.run_text(ctx, [item])[0]
        second = bench_live.run_text(ctx, [item])[0]  # no STT cache reuse between repeats
        assert seen["scribe"] == 2
        assert first["status"] == "ran" and first["fresh_stt"]
        assert first["stt"]["usable"] and first["stt"]["exact"]
        assert first["critical_exact"] and first["end_to_end"]["ok"]
        assert first["metering_consistent"]
        assert first["usage_rows"] == first["provider_requests"] + 1
        assert len(first["metering"]) == first["usage_rows"]
        assert len(first["budget_request_ids"]) == first["usage_rows"]
        assert set(first["budget_request_ids"]).isdisjoint(second["budget_request_ids"])
    finally:
        for client in ctx.clients:
            client.close()


def test_fresh_silence_control_never_calls_model_or_creates_requirements(test_database_url, tmp_path):
    ctx, item, seen = context(test_database_url, tmp_path, control=True)
    try:
        record = bench_live.run_fresh_controls(ctx, [item])[0]
        assert seen == {"scribe": 1, "model": 0}
        assert record["verdict"] == "PASS" and record["zero_requirements"]
        assert record["fresh_stt"] and record["metering_consistent"]
        assert record["usage_rows"] == 1
    finally:
        for client in ctx.clients:
            client.close()


def test_stt_budget_stop_is_never_a_successful_control(test_database_url, tmp_path):
    ctx, item, seen = context(test_database_url, tmp_path, control=True, exhausted=True)
    try:
        record = bench_live.run_fresh_controls(ctx, [item])[0]
        assert record["status"] == "stopped_by_cap" and record["verdict"] == "FAIL"
        assert seen == {"scribe": 0, "model": 0}
    finally:
        for client in ctx.clients:
            client.close()


def test_explicit_voice_handoff_is_scored_and_its_summary_is_not_free(test_database_url, tmp_path):
    ctx, item, seen = context(test_database_url, tmp_path, item_id="CV-30")
    try:
        record = bench_live.run_text(ctx, [item])[0]
        assert record["end_to_end"]["handoff_opened"] and record["end_to_end"]["ok"]
        assert record["metering_consistent"]
        assert any(a.get("operation") == "lead_summary" for a in record["attempts"])
        assert len(record["budget_request_ids"]) == seen["model"] + seen["scribe"]
        assert record["usage_rows"] == seen["model"] + seen["scribe"]
    finally:
        for client in ctx.clients:
            client.close()


def test_production_trace_export_is_tenant_scoped_read_only_and_sanitized(test_database_url):
    import json
    import uuid

    import pytest

    from app.db import tenant_transaction

    scripted = bench_live.ScriptedClient()
    item = next(i for i in bench_live.load_set("text") if i["id"] == "CT-13")
    scripted.script(bench_live.gold_rounds(item))
    with bench_live.environment(test_database_url, scripted, primary="p", failover="f",
                                thinking_level="low") as env:
        sender = "15550319999"  # fictional test number
        env.run_turn(env.text(sender, item["input"]))
        turn_id = env.query("SELECT id FROM conversation_turn")[0][0]
        count = env.query("SELECT count(*) FROM outbound_message")[0][0]
        mapping = [{"turn_id": str(turn_id), "item_id": "CT-13", "repeat": "1", "modality": "text",
                    "kinds": ";".join(["substantive"] * count), "inquiry_id": "I1"}]
        original = env.conn.read_only
        exported = day13_trace.export(env.conn, env.agency, mapping, candidate_identity())
        assert env.conn.read_only == original
        row = exported["turns"][0]
        assert row["received_ms"] and row["sends"] and row["metering"]
        dumped = json.dumps(exported)
        assert str(turn_id) not in dumped and sender not in dumped and item["input"] not in dumped
        with pytest.raises(ValueError, match="missing"):
            day13_trace.export(env.conn, uuid.uuid4(), mapping, candidate_identity())
        assert env.conn.read_only == original
        with tenant_transaction(env.conn, env.agency):
            assert env.conn.execute("SELECT current_setting('transaction_read_only')").fetchone()[0] == "off"
