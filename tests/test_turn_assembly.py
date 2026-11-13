"""Day 12 timing and supersede checks; all messages are synthetic."""
# ruff: noqa: F811
import time
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import customer_reply, orchestrator, turn_assembly, turn_engine, whatsapp
from app.queue import DeferJob
from tests.test_orchestrator import orc, text  # noqa: F401
from tests.test_requirements_db import st  # noqa: F401


@pytest.fixture
def customer_debounce_window():
    return 1.5, 4.0


@pytest.mark.parametrize("first,last,voice,expected", [
    (0, 0, False, 1.5), (3.9, 0, False, 0.1),
    (4, 0, False, None), (1.6, 1.6, False, None), (0, 0, True, None),
])
def test_quiet_window_and_hard_cap(monkeypatch, first, last, voice, expected):
    monkeypatch.setattr(turn_assembly, "tenant_transaction", lambda *a: nullcontext())
    monkeypatch.setattr(orchestrator, "resolve_role", lambda *a: ("customer", "customer", None))
    rows = iter([("ai_active", "ai"), None, (first, last, not voice)])
    conn = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: next(rows)))
    job = SimpleNamespace(agency_id=uuid4(), conversation_key="synthetic", id=uuid4())
    if expected is None:
        turn_assembly.debounce(conn, job, "synthetic")
    else:
        with pytest.raises(DeferJob) as got:
            turn_assembly.debounce(conn, job, "synthetic")
        assert got.value.seconds == pytest.approx(expected)


def test_replan_uses_old_input_as_context_only():
    old, new = uuid4(), uuid4()
    turn = SimpleNamespace(route="customer", staff_segment_id=None,
                           original_message_ids=(old,), messages=(
        SimpleNamespace(id=old, text="kuch sasta", type="text"),
        SimpleNamespace(id=new, text="DHA 8 mein", type="text")))
    assert [m.id for m, _ in turn_engine.message_texts(turn)] == [new]
    block, active = turn_engine.current_messages(turn)
    assert "context only" in block and "kuch sasta" in block
    assert active == "DHA 8 mein"


def test_replan_provider_calls_have_distinct_metering_identity(monkeypatch):
    from app.llm import Usage

    monkeypatch.setattr(turn_engine, "tenant_transaction", lambda *a: nullcontext())
    inserts = []

    def execute(sql, params=None):
        if "INSERT INTO usage_event" in sql:
            inserts.append(params)

    conn = SimpleNamespace(execute=execute)
    job = SimpleNamespace(id=uuid4(), attempts=1)
    turn = SimpleNamespace(id=uuid4(), agency_id=uuid4(), route="customer",
                           original_message_ids=())
    engine = SimpleNamespace(_clock=lambda: 0, provider_name="gemini")
    for original in ((), (uuid4(),)):
        turn.original_message_ids = original
        turn_engine.TurnEngine._meter(engine, conn, job, turn, 0, "primary", "fake",
                                     "succeeded", "stop", Usage(), 0)
    assert len(inserts) == 2 and inserts[0][4] != inserts[1][4]
    assert inserts[0][:4] == inserts[1][:4]


def age(orc):
    orc.query("UPDATE inbound_event SET received_at = received_at - interval '5 seconds' "
              "RETURNING id")
    orc.query("UPDATE job SET available_at = now() RETURNING id")


def test_burst_defers_without_spending_attempt_then_coalesces(orc):
    orc.deliver(text("wamid.b1", "DHA 6 mein ghar"))
    assert orc.worker.run_once()
    assert orc.query("SELECT status, attempts FROM job") == [("pending", 0)]
    assert not orc.client.sent
    orc.deliver(text("wamid.b2", "500 gaz"), text("wamid.b3", "budget 8 crore"))
    age(orc)
    orc.drain()
    assert orc.query("SELECT message_count, status FROM conversation_turn") == [(3, "replied")]
    assert len(orc.client.sent) == 1


def test_input_during_planning_replans_once_and_leaves_later_input_pending(orc, monkeypatch):
    calls = []

    def respond(turn):
        calls.append([m.wamid for m in turn.messages])
        orc.deliver(text(f"wamid.late{len(calls)}", "correction"))
        return [whatsapp.text(f"plan {len(calls)}")]

    monkeypatch.setitem(orchestrator.RESPONDERS, "customer", respond)
    orc.deliver(text("wamid.first"))
    age(orc)
    orc.worker.run_once()
    assert calls == [["wamid.first"], ["wamid.first", "wamid.late1"]]
    assert [p["text"]["body"] for _, p in orc.client.sent] == ["plan 2"]
    assert orc.query("SELECT replan_count, dispatch_started FROM conversation_turn") == [(1, True)]
    assert orc.query("SELECT count(*) FROM job WHERE status = 'pending'") == [(1,)]


def test_system_plan_is_never_superseded(orc, monkeypatch):
    def respond(turn):
        orc.deliver(text("wamid.after-handoff", "more"))
        return [customer_reply.system_entry(whatsapp.text("handoff"))]

    monkeypatch.setitem(orchestrator.RESPONDERS, "customer", respond)
    orc.deliver(text("wamid.handoff"))
    age(orc)
    orc.worker.run_once()
    assert orc.query("SELECT replan_count FROM conversation_turn") == [(0,)]
    assert [p["text"]["body"] for _, p in orc.client.sent] == ["handoff"]


def test_crash_after_dispatch_boundary_cannot_replace_plan(orc, monkeypatch):
    original_send = orchestrator._send
    failures = []

    def crash(*args, **kwargs):
        failures.append(True)
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(orchestrator, "_send", crash)
    orc.deliver(text("wamid.before-crash"))
    age(orc)
    orc.worker.run_once()
    assert failures and not orc.client.sent
    assert orc.query("SELECT dispatch_started FROM conversation_turn") == [(True,)]
    orc.deliver(text("wamid.after-crash"))
    monkeypatch.setattr(orchestrator, "_send", original_send)
    age(orc)
    orc.worker.run_once()
    assert orc.query("SELECT replan_count FROM conversation_turn ORDER BY created_at")[0] == (0,)


def test_fresh_shuffled_burst_matches_canonical_membership(orc):
    from tests.test_orchestrator import CUSTOMER, CUSTOMER_2

    for sender, indices in [(CUSTOMER, [0, 1, 2]), (CUSTOMER_2, [2, 0, 1])]:
        start = int(time.time()) - 10
        messages = [text(f"wamid.{sender}.{n}", body, sender=sender, ts=start + n)
                    for n, body in enumerate(["DHA 6 mein ghar", "500 gaz", "8 crore"])]
        orc.deliver(*(messages[n] for n in indices))
        age(orc)
        orc.drain()
    rows = orc.query(
        "SELECT c.customer_wa_id, t.route, t.status, t.message_count, "
        "array_agg(m.text_body ORDER BY m.provider_timestamp) "
        "FROM conversation_turn t JOIN conversation c ON c.id = t.conversation_id "
        "JOIN message m ON m.turn_id = t.id GROUP BY c.customer_wa_id, t.id "
        "ORDER BY c.customer_wa_id")
    assert len(rows) == 2 and rows[0][1:] == rows[1][1:]
    assert len(orc.client.sent) == 2


def test_burst_runs_one_real_requirement_and_search_chain(st):
    from tests.test_customer_search_db import SCENARIO_2_PATCH
    from tests.test_search_db import CUSTOMER_B
    from tests.test_turn_engine import answer, tool, tools_answer

    st.script.extend([
        tools_answer(tool("update_requirements", {"patch": SCENARIO_2_PATCH})),
        tools_answer(tool("search_properties", call_id="search")), answer(),
    ])
    start = int(time.time()) - 10
    st.deliver(*(text(f"wamid.burst{n}", body, sender=CUSTOMER_B, ts=start + n)
                 for n, body in enumerate([
                     "DHA Phase 6 mein ghar", "500 gaz", "budget aath crore tak"])))
    age(st)
    st.drain()
    slots = st.state()[1]
    assert slots["budget"]["value"]["max"] == 80_000_000
    assert slots["size"]["value"]["nominal_sqyd"] == 500
    assert slots["location"]["value"]["nodes"] == [st.ids["dha-ph6"]]
    assert st.query("SELECT count(*) FROM search_run") == [(1,)]
    assert st.query("SELECT count(*) FROM shown_item")[0][0] > 0
    assert st.query("SELECT message_count, status FROM conversation_turn") == [(3, "replied")]
