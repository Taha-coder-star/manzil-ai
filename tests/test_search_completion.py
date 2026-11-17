"""Bounded completion: save a redundant model round, never lose a second action."""

from types import SimpleNamespace

import pytest

from app import ai_tools, customer_intent, customer_tools, llm, turn_engine


@pytest.mark.parametrize("text", [
    "office on rent in Karachi, around 5 lakh per month",
    "DHA Phase 6 mein 500 gaz ka ghar, budget aath crore tak",
    "Please show me a flat for sale in Clifton under 2.5 crore",
    "warehouse for rent in north nazimabad budget 3 lakh",
])
def test_one_plain_search_can_finish_after_its_rendered_result(text):
    assert customer_intent.single_search(text)


@pytest.mark.parametrize("suffix", [
    " and send photos", " aur agent se baat karao", " visit karni hai",
    " final price batao", "?", "; call me", "\nshow me photos", " not sold",
    " if available", " without commission", " and a flat", " then cheaper",
    " documents clear hain", " park facing", " rent sale", " ignoresomething",
    "\u061f", " + agent", " / photos", "\n",
])
def test_unknown_or_compound_words_keep_model_continuation(suffix):
    assert not customer_intent.single_search("house for rent in Karachi" + suffix)


def run_search(monkeypatch, text, *, ok=True, rendered=True, real_binding=True,
               route="customer", handoff=None, results_ready=True):
    binding = object.__new__(customer_tools.CustomerBinding) if real_binding else SimpleNamespace()
    binding.results, binding.handoff = results_ready, handoff
    context = ai_tools.ToolContext(route, text, binding=binding)
    results = [ai_tools.ToolResult("search_properties", ok, {},
                                  error=None if ok else "render_failed",
                                  payloads=({"type": "text"},) if rendered else ())]
    rounds = iter([
        (llm.LLMResponse("tool_calls", "tool_calls", tool_calls=(
            llm.ToolCall("search_properties", {}, None),)), None, None, 1),
        (llm.LLMResponse("text", "stop"), None, None, 1),
    ])
    engine = turn_engine.TurnEngine(object(), "p", "f")
    monkeypatch.setattr(engine, "_call", lambda *a: next(rounds))
    monkeypatch.setattr(ai_tools, "execute", lambda *a: results[0])
    # All _run work remains real; only provider/DB boundaries are substituted.
    return engine._run(None, None, SimpleNamespace(), [], context,
                       turn_engine.Budget(100), None)


def test_completed_search_needs_no_third_stop_or_provider_success(monkeypatch):
    outcome = run_search(monkeypatch, "house for rent in Karachi")
    assert outcome.kind == "done" and outcome.detail == "search_complete"
    assert outcome.calls == 1 and outcome.payloads == ({"type": "text"},)


@pytest.mark.parametrize("kwargs", [
    {"text": "house for rent in Karachi and send photos"},
    {"ok": False}, {"rendered": False}, {"real_binding": False},
    {"route": "staff"}, {"handoff": object()}, {"results_ready": False},
])
def test_nonterminal_or_untrusted_search_keeps_original_loop(monkeypatch, kwargs):
    outcome = run_search(monkeypatch, **{"text": "house for rent in Karachi", **kwargs})
    assert outcome.detail == "stop" and outcome.calls == 2
