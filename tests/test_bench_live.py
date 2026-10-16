"""Task 10.3 benchmark runner, offline parts: the spend guard, the metered provider, the
refusals before live mode, case grouping and the scorers. No database, no network."""

import io
import json
from decimal import Decimal

import pytest

from app import bench_live, benchmark, staff_bench, whatsapp
from app.bench_live import BenchError, MeteredProvider, SpendCapReached, SpendGuard
from app.llm import ProviderError, Usage
from app.turn_engine import TEMPLATES

PRICES = {"source": "fictional test prices", "verified_on": "2026-10-16", "currency": "USD",
          "reserve_usd_per_call": "0.01",
          "models": {"p": {"input_per_mtok": "1", "cached_input_per_mtok": "0.25",
                           "output_per_mtok": "4"},
                     "f": {"input_per_mtok": "2", "cached_input_per_mtok": "0.5",
                           "output_per_mtok": "8"}}}
DISPOSABLE = "postgresql://bench@127.0.0.1:5432/manzil_schema_test"


def items(name):
    return bench_live.load_set(name)


# --- spend guard ----------------------------------------------------------------------------

def test_the_request_cap_is_hard_and_stops_the_run():
    guard = SpendGuard(max_calls=2)
    guard.settle(guard.reserve(), "p", Usage())
    guard.settle(guard.reserve(), "p", Usage())
    with pytest.raises(SpendCapReached) as caught:
        guard.reserve()
    assert caught.value.code == "spend_cap" and caught.value.failover is False
    assert guard.stopped == "max_calls" and guard.started == 2
    with pytest.raises(SpendCapReached):  # stays stopped
        guard.reserve()


def test_the_currency_cap_is_checked_before_dispatch_with_in_flight_reservations():
    guard = SpendGuard(max_calls=100, cap_usd=Decimal("0.025"), prices=PRICES)
    first = guard.reserve()
    second = guard.reserve()  # concurrent: both reservations count
    assert first == second == Decimal("0.01")
    with pytest.raises(SpendCapReached):
        guard.reserve()  # 0.01 + 0.01 + 0.01 > 0.025
    assert guard.stopped == "spend_cap_usd" and guard.started == 2


def test_a_priced_call_is_charged_its_tokens_and_raises_the_next_reservation():
    guard = SpendGuard(max_calls=100, cap_usd=Decimal(1), prices=PRICES)
    size = guard.reserve()
    cost = guard.settle(size, "p", Usage(input_tokens=10_000, output_tokens=1_000,
                                         cached_input_tokens=4_000, thinking_tokens=1_000))
    # (6000 x 1 + 4000 x 0.25 + 2000 x 4) / 1e6
    assert cost == Decimal("0.015")
    assert guard.spent == Decimal("0.015") and guard._reserve_size() == Decimal("0.015")


def test_an_unpriced_call_is_charged_its_reservation_and_never_called_guaranteed():
    guard = SpendGuard(max_calls=5, cap_usd=Decimal(1), prices=PRICES)
    assert guard.settle(guard.reserve(), "p", Usage()) is None  # tokens not reported
    assert guard.settle(guard.reserve(), "unknown-model", Usage(1, 1)) is None
    summary = guard.summary()
    assert summary["unpriced_requests"] == 2 and summary["estimated_spent_usd"] == "0.02"
    assert summary["currency_cap_guaranteed"] is False and "estimate" in summary["note"]


@pytest.mark.parametrize("change", [
    {"source": ""}, {"verified_on": None}, {"currency": "PKR"}, {"reserve_usd_per_call": "0"},
    {"models": {"p": {"input_per_mtok": "-1", "cached_input_per_mtok": "0",
                      "output_per_mtok": "1"}}},
    {"models": {}},
])
def test_an_incomplete_prices_file_is_refused(tmp_path, change):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({**PRICES, **change}), "utf-8")
    with pytest.raises(BenchError):
        bench_live.load_prices(path)


# --- metered provider -----------------------------------------------------------------------

class Script:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def generate(self, model, **kwargs):
        self.calls.append(model)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def response(tools=()):
    return bench_live.llm.parse_response({
        "candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": t, "args": {}}} for t in tools] or [{"text": "x"}]},
            "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10}})


def call(provider, model="p"):
    return provider.generate(model, system="s", tools=[], contents=[], timeout_s=1)


def test_every_attempt_is_recorded_including_the_failed_one_before_a_failover():
    client = Script(ProviderError("http_503", failover=True), response(["search_properties"]))
    guard = SpendGuard(max_calls=10, cap_usd=Decimal(1), prices=PRICES)
    provider = MeteredProvider(client, guard)
    provider.where = {"item": "CT-01", "turn": 1}
    with pytest.raises(ProviderError):
        call(provider, "p")
    call(provider, "f")
    first, second = provider.attempts
    assert (first["outcome"], first["detail"], first["model"]) == ("failed", "http_503", "p")
    assert first["estimated_cost_usd"] is None  # a failed request is charged its reservation
    assert second["outcome"] == "succeeded" and second["tool_calls"] == ["search_properties"]
    assert second["usage"]["input_tokens"] == 100 and second["item"] == "CT-01"
    assert guard.started == 2 and guard.unpriced == 1


def test_a_request_over_the_cap_is_never_sent():
    client = Script(response())
    provider = MeteredProvider(client, SpendGuard(max_calls=1))
    call(provider)
    with pytest.raises(SpendCapReached):
        call(provider)
    assert len(client.calls) == 1
    assert provider.attempts[-1] == {"n": 2, "model": "p", "tool_results_seen": [],
                                     "outcome": "not_sent", "detail": "cap:max_calls"}


def test_the_tool_results_carried_back_are_recorded_without_values():
    contents = [{"role": "user", "parts": [{"functionResponse": {
        "name": "search_properties",
        "response": {"status": "error", "error": "render_failed", "rows": ["secret"]}}}]}]
    assert bench_live._tool_results(contents) == [
        {"name": "search_properties", "status": "error", "error": "render_failed"}]


# --- live mode refusals (nothing is constructed, nothing is sent) -----------------------------

def run_cli(argv, environ=None):
    out = io.StringIO()
    code = bench_live.main(argv, environ=environ or {}, out=out)
    return code, out.getvalue()


def test_live_mode_is_refused_without_every_cap_and_confirmation(tmp_path):
    code, said = run_cli(["live", "text", "--database-url", DISPOSABLE,
                          "--out", str(tmp_path / "o")])
    assert code == 2
    for name in ("--confirm-live", "--max-calls", "--spend-cap-usd", "--prices-file",
                 "GEMINI_API_KEY", "--primary", "--failover"):
        assert name in said
    assert not (tmp_path / "o").exists()


def test_live_mode_is_refused_for_a_model_without_a_price(tmp_path, monkeypatch):
    monkeypatch.setattr(bench_live.stt_bench, "git_commit", lambda: "unknown")
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps(PRICES), "utf-8")
    code, said = run_cli(
        ["live", "staff", "--database-url", DISPOSABLE, "--out", str(tmp_path / "o"),
         "--confirm-live", "--max-calls", "10", "--spend-cap-usd", "1",
         "--prices-file", str(prices), "--primary", "p", "--failover", "unpriced",
         "--commit", "fictional-commit"],
        environ={"GEMINI_API_KEY": "fictional"})
    assert code == 2 and "no price for model(s): unpriced" in said


@pytest.mark.parametrize("url", [
    "postgresql://bench@db.example.com:5432/manzil_schema_test",
    "postgresql://bench@127.0.0.1:5432/manzil",
    None,
])
def test_only_the_dedicated_disposable_database_is_accepted(tmp_path, url):
    argv = ["dry-run", "text", "--out", str(tmp_path / "o")]
    code, said = run_cli(argv + (["--database-url", url] if url else []))
    assert code == 2 and "manzil_schema_test" in said


def test_plan_needs_no_database_and_keeps_every_item():
    code, said = run_cli(["plan", "text"])
    plan = json.loads(said)
    assert code == 0 and plan["items"] == 30 and plan["case_groups"] == 26
    assert plan["groups"] == [["CT-13", "CT-27", "CT-28", "CT-29", "CT-30"]]
    assert plan["requests_per_repeat"] == {"min": 30, "typical": 60, "max": 150}
    assert json.loads(run_cli(["plan", "staff"])[1])["items"] == 20
    assert json.loads(run_cli(["plan", "controls"])[1])["requests_per_repeat"]["min"] == 0


# --- grouping and scripted calls ------------------------------------------------------------

def test_follow_ups_share_one_conversation_and_every_other_item_is_alone():
    groups = bench_live.text_groups(items("text"))
    assert sum(len(g) for g in groups) == 30
    assert [g for g in groups if len(g) > 1] == [["CT-13", "CT-27", "CT-28", "CT-29", "CT-30"]]


def test_the_dry_run_model_replays_gold_calls_with_the_declared_photo_argument():
    by_id = {i["id"]: i for i in items("text")}
    assert bench_live.gold_rounds(by_id["CT-27"]) == [[("send_property_photos",
                                                        {"position": 2})]]
    [round_] = bench_live.gold_rounds(by_id["CT-13"])
    assert [name for name, _ in round_] == ["update_requirements", "search_properties"]
    spans = json.loads((benchmark.BENCH_DIR / "staff_spans.json").read_text("utf-8"))
    assert [r[0][0] for r in bench_live.span_rounds(spans["ST-15"])] == [
        "find_listings", "propose_update"]


# --- scorers --------------------------------------------------------------------------------

def test_a_button_title_is_not_mistaken_for_a_question():
    no_match = [whatsapp.text("Is area mein abhi match nahi."),
                whatsapp.reply_buttons("Options", [("nomatch:budget", "Budget barhayein")])]
    assert bench_live.asked_question(no_match) == ([], [])
    asked = [whatsapp.text(TEMPLATES["budget_operator_ask"].format(amount="8 crore"))]
    assert bench_live.asked_question(asked) == (["budget_operator_ask"], [])
    assert bench_live.asked_question([whatsapp.text(TEMPLATES["purpose_ask"])])[0] == [
        "purpose_ask"]


def test_slot_scoring_against_the_requirement_state():
    keys = {"u6": "dha-ph6", "u8": "dha-ph8"}
    slots = {"location": {"value": {"nodes": ["u8", "u6"]}},
             "budget": {"value": {"min": None, "max": 80000000, "mode": "max",
                                  "period": "one_time"}},
             "investment": {"value": {"intent": True, "tags": []}}}
    ok = bench_live.score_slot
    assert ok("location", {"value": ["dha-ph6", "dha-ph8"]}, slots, keys)["ok"]
    assert not ok("location", {"value": ["dha-ph6"]}, slots, keys)["ok"]
    assert ok("location", {"status": "AMBIGUOUS", "value": []}, {}, keys)["ok"]
    assert ok("budget", {"min": None, "max": 80000000, "mode": "max", "period": "one_time",
                         "raw": "x"}, slots, keys)["ok"]
    assert not ok("budget", {"max": 72750000}, slots, keys)["ok"]
    assert ok("investment", {"value": True}, slots, keys)["ok"]


def text_record(item_id, status="ran", **fields):
    base = {"id": item_id, "status": status, "gold_type": "results", "has_critical": True,
            "has_budget": True, "tools": [], "end_to_end": {
                "ok": True, "asked": [], "question_buttons": [], "hidden_shown": []},
            "critical_exact": True, "budget_exact": True, "tools_ok": True, "latency_ms": 10}
    return {**base, **fields}


def test_not_run_items_stay_in_every_denominator_as_incorrect():
    records = [text_record("CT-01"), {"id": "CT-02", "status": "not_run", "reason": "cap",
                                      "gold_type": "results", "has_critical": True,
                                      "has_budget": True, "tools": []}]
    summary = bench_live.text_summary(records)
    for key in ("extraction_critical_exact", "price_normalisation_exact",
                "tool_selection_exact", "end_to_end_exact"):
        assert summary[key] == {"correct": 1, "of": 2}, key
    assert summary["not_run"] == ["CT-02"] and summary["latency_ms"]["n"] == 1


def test_a_stored_draft_payload_scores_like_the_fixture():
    """Live drafts come back from listing_draft.payload as JSON (numbers as strings)."""
    gold = next(i for i in items("staff") if i["id"] == "ST-02")["expected_outcome"]
    payload = {"fields": {"purpose": {"value": "sale"}, "property_type": {"value": "house"},
                          "location": {"location_id": "uuid-btk-p10"},
                          "price_pkr": {"value": "10500000"},
                          "size": {"value": "125", "unit": "sq_yd", "nominal_sqyd": "125"},
                          "bedrooms": {"value": 3}},
               "missing": [], "duplicates": [{"code": "BTK-101"}], "proposed_code": "BTK-107",
               "blockers": [], "warnings": []}
    result = staff_bench.score_draft(gold, payload,
                                     location_key={"uuid-btk-p10": "btk-p10"}.get)
    assert result["exact"] and all(c["ok"] for c in result["workflow"].values())


def test_st01_stays_visible_and_out_of_the_workflow_rate():
    record = {"id": "ST-01", "kind": "draft", "exact": True, "failure": None, "fields": {},
              "workflow": {"duplicate_of": {"gold": [], "got": ["DH6-101", "DH6-106"],
                                            "ok": False},
                           "save_allowed": {"gold": True, "got": True, "ok": True}}}
    summary = staff_bench.summary([record])
    assert summary["workflow"] == {"passed": 1, "of": 1}
    [known] = summary["known_gold_disagreements"]
    assert (known["item"], known["gold"], known["got"]) == ("ST-01", [], ["DH6-101", "DH6-106"])


# --- review round: scope set, photos, questions, blocked items, ST-01 ---------------------------

def test_the_search_gate_questions_are_recognised():
    from app import render
    for language in render.TEXT:
        asked = bench_live.asked_question([whatsapp.text(render.TEXT[language]["location_ask"])])
        assert asked == (["search_gate:location_ask"], [])


def test_photos_must_all_belong_to_the_target_and_never_repeat():
    gold = {"listing": "DH8-102"}
    good = bench_live.score_photos(gold, [("m1", "DH8-102"), ("m2", "DH8-102")], set())
    assert good["ok"] and good["images"] == 2 and good["repeats"] == []
    assert not bench_live.score_photos(gold, [("m1", "DH8-102"), ("m9", "DH8-101")], set())["ok"]
    repeated = bench_live.score_photos(gold, [("m1", "DH8-102")], {"m1"})
    assert not repeated["ok"] and repeated["repeats"] == ["m1"]
    assert not bench_live.score_photos(gold, [], set())["ok"]  # a text-only reply sends nothing


def test_st01_duplicate_check_stays_unscored_even_when_it_agrees_with_the_gold():
    record = {"id": "ST-01", "kind": "draft", "exact": True, "failure": None,
              "fields": {"price_pkr": {"gold": 80000000, "got": 80000000, "ok": True}},
              "workflow": {"duplicate_of": {"gold": [], "got": [], "ok": True},
                           "save_allowed": {"gold": True, "got": True, "ok": True}}}
    summary = staff_bench.summary([record])
    assert summary["workflow"] == {"passed": 1, "of": 1}
    assert summary["known_gold_disagreements"][0]["agrees_with_gold"] is True
    assert summary["scored_denominators"] == {
        "items": 1, "fields": 1, "workflow_scored": 1, "workflow_unscored": 1,
        "unscored_checks": ["ST-01 duplicate_of"]}
    assert summary["items_exact"] == {"exact": 1, "of": 1}  # its fields still count


def test_a_blocked_handoff_stays_in_the_denominator_and_is_listed():
    blocked = text_record("CT-30", gold_type="handoff", end_to_end={
        "ok": False, "asked": [], "question_buttons": [], "hidden_shown": [],
        "blocked_by": bench_live.BLOCKED["handoff"]}, tools=["request_handoff"])
    summary = bench_live.text_summary([text_record("CT-01"), blocked])
    assert summary["end_to_end_exact"] == {"correct": 1, "of": 2}
    assert summary["end_to_end_exact_excluding_blocked"] == {"correct": 1, "of": 1}
    assert [b["id"] for b in summary["blocked"]] == ["CT-30"]
    assert "11.x" in summary["blocked"][0]["by"]
    assert summary["handoff_recall"] == {"correct": 1, "of": 1}  # tool selection only


def test_the_scope_plan_keeps_every_turn_and_its_chains():
    code, said = run_cli(["plan", "scope"])
    plan = json.loads(said)
    assert code == 0 and plan["items"] == 29 and plan["case_groups"] == 5
    assert plan["groups"][0][:5] == ["CT-13", "CT-27", "CT-28", "CT-29", "SC-01"]
    assert "CT-30" not in {i for g in plan["groups"] for i in g}  # blocked; not a Scope follow-up


def test_the_run_commit_is_pinned_by_git_or_stated_and_never_contradicted(monkeypatch):
    monkeypatch.setattr(bench_live.stt_bench, "git_commit", lambda: "unknown")
    assert bench_live.resolve_commit(None) == ("unknown", "unknown")
    assert bench_live.resolve_commit("09a9ec7") == ("09a9ec7", "stated")
    monkeypatch.setattr(bench_live.stt_bench, "git_commit", lambda: "abc123")
    assert bench_live.resolve_commit(None) == ("abc123", "git")
    with pytest.raises(BenchError, match="does not match"):
        bench_live.resolve_commit("09a9ec7")


# --- round 3: reply checks, staff denominators, coverage, photo repeats --------------------------

def turn_result(kind, payloads, *, searched, new_rows=0, shown=()):
    from app.bench_live import score_text_turn
    before = {"search_run": 0, "pending_clarification": 0}
    after = {"search_run": int(searched), "pending_clarification": new_rows}
    return score_text_turn({"expected_outcome": {"type": kind}}, payloads, {}, before, after,
                           list(shown), {}, {})["end_to_end"]


def no_match_text():
    from app import render
    return whatsapp.text(render.TEXT["roman_urdu"]["no_match"].format(
        place="DHA Phase 6 mein ", what=" ghar"))


@pytest.mark.parametrize(("payloads", "ok"), [
    ([], False),                                                    # nothing sent
    ([whatsapp.text(TEMPLATES["failure"])], False),                 # failure template
    ([whatsapp.text(TEMPLATES["customer_default"])], False),        # generic default prompt
    ([whatsapp.text(TEMPLATES["search_unavailable"])], False),      # a "not ready" stub
    ([no_match_text(), whatsapp.text(TEMPLATES["failure"])], False),
    ([whatsapp.text("Ji, kuch options hain")], False),              # visible, but no no-match
    ([no_match_text()], True),
])
def test_a_no_match_needs_a_visible_honest_no_match_reply(payloads, ok):
    assert turn_result("no_match", payloads, searched=True)["ok"] is ok


def test_a_no_match_without_a_search_or_with_results_fails():
    assert not turn_result("no_match", [no_match_text()], searched=False)["ok"]
    assert not turn_result("no_match", [no_match_text()], searched=True, shown=["DH6-101"])["ok"]


@pytest.mark.parametrize(("payloads", "new_rows", "ok"), [
    ([], 1, False),                                                  # a DB row alone
    ([whatsapp.text(TEMPLATES["requirements_saved"])], 1, False),    # visible, no question
    ([whatsapp.text(TEMPLATES["customer_default"])], 0, False),
    ([whatsapp.text(TEMPLATES["purpose_ask"]), whatsapp.text(TEMPLATES["failure"])], 0, False),
    ([whatsapp.text(TEMPLATES["purpose_ask"])], 0, True),
    ([whatsapp.reply_buttons("Kaunsi jagah?", [("clarify:location:1", "Clifton Block 6")])],
     1, True),
])
def test_a_clarification_needs_a_visible_question(payloads, new_rows, ok):
    result = turn_result("clarify", payloads, searched=False, new_rows=new_rows)
    assert result["ok"] is ok


def test_photo_repeats_count_within_one_reply_and_across_turns():
    gold = {"listing": "DH8-102"}
    within = bench_live.score_photos(gold, [("m1", "DH8-102"), ("m1", "DH8-102")], set())
    assert within["repeats"] == ["m1"] and not within["ok"]
    across = bench_live.score_photos(gold, [("m2", "DH8-102"), ("m1", "DH8-102")], {"m1"})
    assert across["repeats"] == ["m1"] and not across["ok"]
    both = bench_live.score_photos(gold, [("m1", "DH8-102"), ("m3", "DH8-102"),
                                          ("m3", "DH8-102")], {"m1"})
    assert both["repeats"] == ["m1", "m3"]
    records = [{"id": "SC-02", "status": "ran", "scope_role": "turn", "metrics": ["photos"],
                "end_to_end": {"ok": False, "photos": both}, "latency_ms": 1}]
    assert bench_live.scope_summary(records)["photo_repeats"] == 2


def staff_not_run(item_id, kind, reason="stopped_by_cap"):
    status = "stopped_by_cap" if reason == "stopped_by_cap" else "not_run"
    return {"id": item_id, "status": status, "reason": reason, "gold_type": kind}


def test_expected_staff_checks_match_what_a_run_scores():
    fixture = staff_bench.run()["summary"]["scored_denominators"]
    fields = workflow = 0
    for item in items("staff"):
        f, w = staff_bench.expected_checks(item["expected_outcome"])
        fields, workflow = fields + len(f), workflow + len(w)
    assert (fields, workflow) == (fixture["fields"], fixture["workflow_scored"] + 1)


def test_an_all_unrun_staff_set_keeps_every_check_as_failed():
    records = [staff_not_run(i["id"], i["expected_outcome"]["type"],
                             "stopped_by_cap" if n % 2 else "not_run")
               for n, i in enumerate(items("staff"))]
    summary = bench_live.summarise("staff", records)
    assert summary["items_exact"] == {"exact": 0, "of": 20}
    assert summary["fields"] == {"correct": 0, "of": 94}
    assert summary["workflow"] == {"passed": 0, "of": 33}
    assert summary["scored_denominators"]["unscored_checks"] == ["ST-01 duplicate_of"]


def test_a_mixed_staff_run_keeps_the_full_denominators():
    ran = {i["id"]: dict(i, status="ran", tools_ok=True, listing_writes=0, latency_ms=5)
           for i in staff_bench.run()["items"]}
    gold = {i["id"]: i for i in items("staff")}
    records = [ran[k] if n < 10 else staff_not_run(k, gold[k]["expected_outcome"]["type"])
               for n, k in enumerate(sorted(gold))]
    summary = bench_live.summarise("staff", records)
    assert summary["fields"]["of"] == 94 and summary["workflow"]["of"] == 33
    assert summary["items_exact"]["of"] == 20 and summary["stopped_by_cap"] == sorted(gold)[10:]
    assert summary["scored_denominators"]["workflow_unscored"] == 1  # ST-01 ran, still unscored


def test_a_draft_that_was_never_stored_keeps_its_checks():
    item = next(i for i in items("staff") if i["id"] == "ST-02")
    failed = staff_bench.failed_item(item, "no_draft")
    assert not failed["exact"] and failed["failure"] == "no_draft"
    assert len(failed["fields"]) == 8 and set(failed["workflow"]) == {
        "missing", "save_allowed", "duplicate_of"}
    assert not any(c["ok"] for c in [*failed["fields"].values(), *failed["workflow"].values()])


def test_unknown_or_empty_only_ids_are_refused(tmp_path):
    for only in ("CT-01,CT-99", " , "):
        code, said = run_cli(["dry-run", "text", "--database-url", DISPOSABLE,
                              "--out", str(tmp_path / "o"), "--only", only])
        assert code == 2 and ("unknown item(s): CT-99" in said or "names no item" in said)
    assert not (tmp_path / "o").exists()


def test_coverage_separates_subset_and_full_set_completion():
    ids = ["A", "B", "C"]
    done = [{"id": i, "status": "ran"} for i in ids]
    full = bench_live.coverage(ids, ids, [], done)
    assert full["subset_complete"] and full["full_set_complete"] and not full["partial"]
    subset = bench_live.coverage(ids, ["A"], ["B", "C"], done[:1])
    assert subset["subset_complete"] and not subset["full_set_complete"] and subset["partial"]
    missing = bench_live.coverage(ids, ids, [], done[:2] + [
        {"id": "C", "status": "not_run", "reason": "no_recording_or_stt_result"}])
    assert not missing["subset_complete"] and not missing["full_set_complete"]
    assert missing["missing"] == [{"id": "C", "reason": "no_recording_or_stt_result"}]


def test_controls_without_recordings_never_complete_and_never_succeed(tmp_path):
    code, said = run_cli(["dry-run", "controls", "--database-url", DISPOSABLE,
                          "--out", str(tmp_path)])
    data = json.loads((tmp_path / "result.json").read_text("utf-8"))
    assert code == 3 and "no case was executed" in said
    assert data["status"] == "finished" and not data["coverage"]["full_set_complete"]
    assert not data["coverage"]["subset_complete"]
    assert [m["reason"] for m in data["repeats"][0]["coverage"]["missing"]] == [
        "no_recording_or_stt_result"] * 5


def test_run_hashes_are_the_repository_bytes_on_any_checkout(tmp_path):
    (tmp_path / "lf.json").write_bytes(b'{"a": 1}\n')
    (tmp_path / "crlf.json").write_bytes(b'{"a": 1}\r\n')
    assert (bench_live.text_sha256(tmp_path / "lf.json")
            == bench_live.text_sha256(tmp_path / "crlf.json"))
