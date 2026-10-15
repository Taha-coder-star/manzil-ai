"""Task 10.2: an unanswered 7.3 question about the type, size or budget keeps the search
waiting (requirements.open_asks, read from the patch log), in fresh and existing
conversations, through corrections, expiry and a retried turn. Real worker, orchestrator,
turn engine and outbound path (disposable PostgreSQL, manzil_app login, RLS on); Gemini is
a fake transport, WhatsApp the FakeClient. All data is fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import psycopg

from app import turn_engine
from app.turn_engine import TEMPLATES
from tests.test_customer_flow_db import scenario_2, turn
from tests.test_customer_search_db import sent_text
from tests.test_requirements_db import p, st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_B, conversation
from tests.test_turn_engine import answer, text, tool, tools_answer

TWO_AMOUNTS = "budget 5 crore ya 6 crore"
SEARCH = ("search_properties", {})


def update(*ops):
    return ("update_requirements", {"patch": list(ops)})


def runs(st):
    return st.query("SELECT count(*) FROM search_run WHERE conversation_id = %s",
                    (conversation(st, CUSTOMER_B),))[0][0]


def cards_in(payloads):
    return [pl for pl in payloads if pl["type"] in ("interactive", "image")]


def test_a_fresh_conversation_waits_for_the_budget_answer(st):
    first = turn(st, f"DHA Phase 6 mein ghar khareedna hai, {TWO_AMOUNTS}",
                 update(p("purpose", "set", "khareedna"), p("location", "set", "DHA Phase 6"),
                        p("property_types", "set", "ghar"), p("budget", "set", "5 crore ya 6 crore")),
                 SEARCH)
    assert TEMPLATES["budget_unclear"] in sent_text(first)
    assert runs(st) == 0 and not cards_in(first)
    # Nothing answered: the next search asks the same question again.
    again = turn(st, "dikhao", SEARCH)
    assert sent_text(again) == TEMPLATES["budget_unclear"] and runs(st) == 0
    done = turn(st, "6 crore tak", update(p("budget", "set", "6 crore tak")), SEARCH)
    assert runs(st) == 1 and cards_in(done)


def test_an_existing_budget_is_not_searched_while_its_replacement_is_asked(st):
    scenario_2(st)
    assert runs(st) == 1
    asked = turn(st, TWO_AMOUNTS, update(p("budget", "set", "5 crore ya 6 crore")), SEARCH)
    assert TEMPLATES["budget_unclear"] in sent_text(asked)
    assert runs(st) == 1 and not cards_in(asked)                # the 8 crore was not used
    assert st.state()[1]["budget"]["value"]["max"] == 80_000_000  # ... though still stored
    # A correction of another slot does not answer the budget question.
    other = turn(st, "Phase 7 bhi", update(p("location", "add", "Phase 7")), SEARCH)
    assert TEMPLATES["budget_unclear"] in sent_text(other) and runs(st) == 1
    answered = turn(st, "6 crore tak", update(p("budget", "set", "6 crore tak")), SEARCH)
    assert runs(st) == 2 and st.state()[1]["budget"]["value"]["max"] == 60_000_000
    assert cards_in(answered) or "abhi available nahi" in sent_text(answered)


def test_a_size_question_holds_the_search_too(st):
    # No type is stored, so "500" has no unit: asked, and the budget alone is not searched.
    asked = turn(st, "DHA Phase 6 mein khareedna hai, 8 crore tak, size 500",
                 update(p("purpose", "set", "khareedna"), p("location", "set", "DHA Phase 6"),
                        p("budget", "set", "8 crore tak"), p("size", "set", "500")), SEARCH)
    assert TEMPLATES["size_unit_ask"] in sent_text(asked) and runs(st) == 0
    turn(st, "500 gaz", update(p("size", "set", "500 gaz")), SEARCH)
    assert runs(st) == 1


def age_log(st):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE requirement_patch_log SET created_at = now() - interval '3 days'")


def test_an_unanswered_question_is_asked_again_after_24_hours(st):
    """Review fix 2: no expiry silently lifts the hold (a fresh conversation: there is no
    budget at all, and the type + place alone are not searched)."""
    turn(st, f"DHA Phase 6 mein ghar khareedna hai, {TWO_AMOUNTS}",
         update(p("purpose", "set", "khareedna"), p("location", "set", "DHA Phase 6"),
                p("property_types", "set", "ghar"), p("budget", "set", "5 crore ya 6 crore")),
         SEARCH)
    age_log(st)
    later = turn(st, "dikhao", SEARCH)
    assert sent_text(later) == TEMPLATES["budget_unclear"] and runs(st) == 0


def test_an_earlier_budget_is_not_used_after_its_question_is_old(st):
    """Review fix 2: the earlier 8 crore stays unused however old the question is."""
    scenario_2(st)
    turn(st, TWO_AMOUNTS, update(p("budget", "set", "5 crore ya 6 crore")), SEARCH)
    age_log(st)
    later = turn(st, "dikhao", SEARCH)
    assert TEMPLATES["budget_unclear"] in sent_text(later) and runs(st) == 1
    turn(st, "6 crore tak", update(p("budget", "set", "6 crore tak")), SEARCH)
    assert runs(st) == 2


def test_a_retried_held_turn_asks_once_and_searches_nothing(st, monkeypatch):
    failures = [RuntimeError("crash before the reply plan is stored")]
    original = turn_engine.render_customer

    def crash_once(*args, **kwargs):
        if failures:
            raise failures.pop()
        return original(*args, **kwargs)

    monkeypatch.setattr(turn_engine, "render_customer", crash_once)
    patch = [p("purpose", "set", "khareedna"), p("location", "set", "DHA Phase 6"),
             p("property_types", "set", "ghar"), p("budget", "set", "5 crore ya 6 crore")]
    for _ in range(2):   # the retry asks the model again
        st.script.append(tools_answer(tool("update_requirements", {"patch": patch})))
        st.script.append(tools_answer(tool("search_properties", call_id="call-1")))
        st.script.append(answer())
    st.deliver(text(f"DHA Phase 6 mein ghar khareedna hai, {TWO_AMOUNTS}", sender=CUSTOMER_B))
    st.drain()
    assert runs(st) == 0
    asks = st.query("SELECT count(*) FROM requirement_patch_log, jsonb_array_elements(ops) e "
                    "WHERE e ->> 'slot' = 'budget' AND e ->> 'status' = 'ask'")[0][0]
    assert asks == 1
    assert st.sent_texts().count(
        TEMPLATES["requirements_saved"] + "\n\n" + TEMPLATES["budget_unclear"]) == 1
    keys = [r[0] for r in st.query("SELECT dedupe_key FROM outbound_message")]
    assert len(keys) == len(set(keys))
