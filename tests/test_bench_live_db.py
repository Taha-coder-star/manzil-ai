"""Task 10.3 benchmark runner on the disposable PostgreSQL: dry-runs through the real worker,
the spend cap stopping a live-mode run (Gemini is a fake transport), and the control scorer on
fictional audio. No request leaves the process; WhatsApp is the recording fake."""

import io
import json
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from app import bench_live
from tests.test_speech import opus_note
from tests.test_turn_engine import answer, tool, tools_answer

PRICES = {"source": "fictional test prices", "verified_on": "2026-10-16", "currency": "USD",
          "reserve_usd_per_call": "0.001",
          "models": {m: {"input_per_mtok": "0.1", "cached_input_per_mtok": "0.01",
                         "output_per_mtok": "0.4"} for m in ("fake-primary", "fake-failover")}}


def leftover_schemas(url):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT count(*) FROM pg_namespace "
                            "WHERE nspname LIKE 'migration_test_bench_%'").fetchone()[0]


def run(argv, url, environ=None, transport=None):
    out = io.StringIO()
    code = bench_live.main([*argv, "--database-url", url], environ=environ or {}, out=out,
                           transport=transport)
    return code, out.getvalue()


def result(out_dir):
    return json.loads((out_dir / "result.json").read_text("utf-8"))


def test_text_dry_run_scores_a_result_and_a_clarification_end_to_end(test_database_url, tmp_path):
    code, said = run(["dry-run", "text", "--only", "CT-13,CT-25", "--out", str(tmp_path)],
                     test_database_url)
    assert code == 0 and "NOT a model benchmark" in said
    data = result(tmp_path)
    assert data["identity"]["model"] is None
    cover = data["coverage"]
    assert cover["requested"] == ["CT-13", "CT-25"] and cover["partial"]
    assert len(cover["filtered_out"]) == 28 and "CT-01" in cover["filtered_out"]
    assert cover["subset_complete"] and not cover["full_set_complete"]
    assert data["repeats"][0]["coverage"]["executed"] == ["CT-13", "CT-25"]
    records = {r["id"]: r for r in data["repeats"][0]["items"]}
    scenario = records["CT-13"]
    assert scenario["end_to_end"]["ok"] and scenario["critical_exact"]
    assert scenario["end_to_end"]["shown"] and not scenario["end_to_end"]["hidden_shown"]
    assert scenario["tools"] == ["search_properties", "update_requirements"]
    assert scenario["metering_consistent"] and scenario["provider_requests"] == 2
    assert scenario["latency_ms"] is not None
    block6 = records["CT-25"]
    assert block6["end_to_end"]["ok"] and not block6["end_to_end"]["searched"]
    assert block6["end_to_end"]["question_buttons"] or block6["end_to_end"]["asked"]
    assert leftover_schemas(test_database_url) == 0


def test_the_request_cap_stops_a_live_run_and_reports_the_rest_not_run(test_database_url,
                                                                      tmp_path, monkeypatch):
    monkeypatch.setattr(bench_live.stt_bench, "git_commit", lambda: "unknown")
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps(PRICES), "utf-8")
    patch = [{"slot": "location", "op": "set", "raw_span": "DHA Phase 6"}]
    sent = []

    def handler(request):
        sent.append(request.url.path)
        body = tools_answer(tool("update_requirements", {"patch": patch}),
                            promptTokenCount=500, candidatesTokenCount=20)
        return httpx.Response(200, json=body if len(sent) == 1 else answer())

    out = tmp_path / "out"
    code, _ = run(["live", "text", "--only", "CT-13,CT-25", "--out", str(out), "--confirm-live",
                   "--max-calls", "1", "--spend-cap-usd", "0.50", "--prices-file", str(prices),
                   "--primary", "fake-primary", "--failover", "fake-failover",
                   "--commit", "fictional-commit"],
                  test_database_url, environ={"GEMINI_API_KEY": "fictional"},
                  transport=httpx.MockTransport(handler))
    assert code == 3 and len(sent) == 1  # nothing completed; the 2nd request was never sent
    data = result(out)
    assert data["caps"]["stopped"] == "max_calls"
    assert not data["coverage"]["subset_complete"] and not data["coverage"]["full_set_complete"]
    assert data["repeats"][0]["coverage"]["missing"] == [
        {"id": "CT-13", "reason": "stopped_by_cap"}, {"id": "CT-25", "reason": "stopped_by_cap"}]
    assert data["caps"]["currency_cap_guaranteed"] is False
    assert (data["identity"]["commit"], data["identity"]["commit_source"]) == (
        "fictional-commit", "stated")
    records = {r["id"]: r for r in data["repeats"][0]["items"]}
    assert records["CT-13"]["status"] == "stopped_by_cap"
    assert [a["outcome"] for a in records["CT-13"]["attempts"]] == ["succeeded", "not_sent"]
    assert records["CT-13"]["usage_rows"] == 2 and records["CT-13"]["metering_consistent"]
    assert records["CT-25"] == {"id": "CT-25", "status": "not_run", "reason": "stopped_by_cap",
                                "gold_type": "clarify", "category": "ambiguous", "tools": [],
                                "has_critical": True, "has_budget": False}
    summary = data["repeats"][0]["summary"]
    assert summary["end_to_end_exact"] == {"correct": 0, "of": 2}
    assert summary["not_run"] == ["CT-25"] and summary["stopped_by_cap"] == ["CT-13"]
    assert leftover_schemas(test_database_url) == 0


def test_staff_dry_run_keeps_st01_visible_and_st15_failing(test_database_url, tmp_path):
    code, _ = run(["dry-run", "staff", "--only", "ST-01,ST-15", "--out", str(tmp_path)],
                  test_database_url)
    assert code == 0
    summary = result(tmp_path)["repeats"][0]["summary"]
    assert [k["item"] for k in summary["known_gold_disagreements"]] == ["ST-01"]
    assert summary["failed_items"] == [{"id": "ST-15", "reason": "no_proposal_or_list"}]
    assert summary["listing_writes_without_confirmation"] == 0
    records = {r["id"]: r for r in result(tmp_path)["repeats"][0]["items"]}
    assert "Bahria Sports City" in records["ST-15"]["reply"][0]
    assert records["ST-01"]["exact"] and records["ST-01"]["metering_consistent"]


# --- controls -------------------------------------------------------------------------------

SILENT = {"scribe": ({"text": "", "language": None, "language_probability": 0.0,
                      "word_count": 0, "event_count": 0}, None),
          "gemini": ({"speech_present": False, "text": "", "language": None}, None)}
HEARD = "mujhe DHA Phase 6 mein ghar chahiye budget aath crore"
SPEECH = {"scribe": ({"text": HEARD, "language": "urd", "language_probability": 0.95,
                      "word_count": len(HEARD.split()), "event_count": 0}, None),
          "gemini": ({"speech_present": True, "text": HEARD, "language": "ur"}, None)}


class Hallucinating(bench_live.ScriptedClient):
    """A model that turns whatever it hears into a requirement (and ignores resets)."""

    def script(self, rounds):
        self.rounds = [[("update_requirements", {"patch": [
            {"slot": "location", "op": "set", "raw_span": "DHA Phase 6"}]})]]


def controls(url, tmp_path, transcripts, client):
    audio = tmp_path / "audio"
    audio.mkdir()
    for item_id in transcripts:
        (audio / f"{item_id}.ogg").write_bytes(opus_note(8))
    args = SimpleNamespace(database_url=url, primary="dry-run-primary",
                           failover="dry-run-failover", thinking_level="low")
    provider = bench_live.MeteredProvider(client)
    ctx = bench_live.RunContext(args, provider, client, live=False)
    items = [i for i in bench_live.load_set("controls") if i["id"] in ("VC-01", "VC-03", "VC-04")]
    return {r["id"]: r for r in bench_live.run_controls(ctx, items, transcripts, audio)}


def test_controls_silence_passes_and_a_reached_model_is_undecided_in_a_dry_run(
        test_database_url, tmp_path):
    records = controls(test_database_url, tmp_path, {"VC-01": SILENT, "VC-04": SPEECH},
                       bench_live.ScriptedClient())
    silent, spoken, music = records["VC-01"], records["VC-04"], records["VC-03"]
    assert silent["verdict"] == "PASS" and not silent["llm_reached"]
    assert silent["zero_requirements"] and silent["provider_requests"] == 0
    assert spoken["llm_reached"] and spoken["verdict"] == "UNDECIDED"
    assert music == {"id": "VC-03", "status": "not_run", "reason": "no_recording_or_stt_result",
                     "gold_type": "fallback", "category": "music", "tools": [],
                     "has_critical": False, "has_budget": False}
    assert leftover_schemas(test_database_url) == 0


def test_a_hallucinated_requirement_on_a_control_fails_it(test_database_url, tmp_path):
    records = controls(test_database_url, tmp_path, {"VC-04": SPEECH}, Hallucinating())
    spoken = records["VC-04"]
    assert spoken["verdict"] == "FAIL" and not spoken["zero_requirements"]
    assert spoken["created"]["patches"] == 1 and spoken["created"]["slots"] == ["location"]


@pytest.fixture(autouse=True)
def _no_orphans(test_database_url):
    yield
    assert leftover_schemas(test_database_url) == 0


def test_scope_dry_run_sends_the_quoted_and_positioned_listing_photos(test_database_url,
                                                                      tmp_path):
    code, _ = run(["dry-run", "scope", "--only", "CT-01,SC-07,SC-08", "--out", str(tmp_path)],
                  test_database_url)
    assert code == 0
    data = result(tmp_path)
    assert data["identity"]["media_fixture_photos_per_listing"] == 8
    records = {r["id"]: r for r in data["repeats"][0]["items"]}
    assert records["CT-01"]["scope_role"] == "setup" and records["CT-01"]["end_to_end"]["ok"]
    first = records["SC-07"]["end_to_end"]["photos"]
    assert first["ok"] and first["image_listings"] == ["DH6-108"] and first["images"] == 5
    assert first["displayed_target"] == "DH6-108"
    quoted = records["SC-08"]
    assert quoted["quote"] == {"listing": "SHF-101", "card_found": True}
    assert quoted["end_to_end"]["photos"]["image_listings"] == ["SHF-101"]
    summary = data["repeats"][0]["summary"]
    assert summary["photos_images_belong"] == {"correct": 2, "of": 2}
    assert summary["photo_repeats"] == 0 and summary["setup"] == {"CT-01": True}


def test_an_immediate_staff_turn_keeps_its_own_latency(test_database_url, tmp_path):
    code, _ = run(["dry-run", "staff", "--only", "ST-10", "--out", str(tmp_path)],
                  test_database_url)
    assert code == 0
    [record] = result(tmp_path)["repeats"][0]["items"]
    assert record["exact"] and record["latency_ms"] is not None and record["latency_ms"] > 0
