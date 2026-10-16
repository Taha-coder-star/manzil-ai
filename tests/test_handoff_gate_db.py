"""Task 11.2 (first part): the send-time gate on control_state (R-28), forwarding to the
agent holding the handoff and the single holding reply, through the real worker,
orchestrator, turn engine and outbound path (disposable PostgreSQL, restricted manzil_app
login, RLS on). Gemini is a fake transport; WhatsApp is the FakeClient. All data and
numbers are fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import psycopg

from app import handoff, orchestrator, references
from app.db import tenant_transaction
from tests.test_customer_flow_db import scenario_2, tap, turn
from tests.test_customer_search_db import search_turn
from tests.test_handoff_db import BROKER_NUMBER, control
from tests.test_requirements_db import p, st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_B, conversation
from tests.test_turn_engine import media, text


def broker_says_hi(st):
    """Demo-day checklist (R-29): the broker writes first, so their 24 h window is open."""
    st.script.clear()
    st.deliver(text("hi", sender=BROKER_NUMBER))
    st.drain()


def to(st, number, before=0):
    return [payload for recipient, payload in st.client.sent[before:] if recipient == number]


def bodies(payloads):
    return [payload["text"]["body"] for payload in payloads if payload["type"] == "text"]


def open_visit(st):
    scenario_2(st)
    tap(st, "visit:DH6-101")
    assert control(st) == ("handoff_pending", "human")


def accept(st):
    """Stands in for the broker's Accept (11.1 step 4): accepted + human_owned."""
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute(
            "UPDATE handoff SET status = 'accepted', accepted_at = now(), "
            "accepted_by_staff_id = assigned_staff_id WHERE agency_id = %s", (st.agency,))
        admin.execute("UPDATE conversation SET control_state = 'human_owned' "
                      "WHERE agency_id = %s AND control_state = 'handoff_pending'",
                      (st.agency,))


# --- forwarding and the holding reply ----------------------------------------------------

def test_customer_messages_go_to_the_broker_with_one_holding_reply(st):
    open_visit(st)
    broker_says_hi(st)
    before = len(st.client.sent)
    assert turn(st, "price kam ho sakti hai?") != []
    assert bodies(to(st, BROKER_NUMBER, before)) == [
        f"💬 *L-01* · +{CUSTOMER_B}\nprice kam ho sakti hai?"]
    assert bodies(to(st, CUSTOMER_B, before)) == [
        "✅ *Ahmed* ko aapka message bhej diya hai; woh jald jawab denge."]
    # A second message is forwarded too, but there is no second holding reply.
    before = len(st.client.sent)
    turn(st, "kal 6 baje theek hai")
    assert bodies(to(st, BROKER_NUMBER, before)) == [
        f"💬 *L-01* · +{CUSTOMER_B}\nkal 6 baje theek hai"]
    assert to(st, CUSTOMER_B, before) == []
    assert st.query("SELECT holding_reply_sent_at IS NOT NULL FROM handoff") == [(True,)]
    assert st.query("SELECT status FROM conversation_turn WHERE conversation_id = %s "
                    "ORDER BY created_at DESC LIMIT 2",
                    (conversation(st, CUSTOMER_B),)) == [("gated",), ("gated",)]


def test_more_listings_during_a_handoff_start_no_search_and_no_model_call(st):
    open_visit(st)
    runs = st.query("SELECT count(*) FROM search_run")
    calls = len(st.requests)
    search_turn(st, "aur properties dikhao", [p("location", "set", "DHA Phase 6")], CUSTOMER_B)
    assert st.query("SELECT count(*) FROM search_run") == runs
    assert len(st.requests) == calls


def test_a_voice_note_is_forwarded_as_its_transcript(st):
    open_visit(st)
    broker_says_hi(st)
    st.speech.transcripts.append("visit kal shaam ho sakti hai?")
    before = len(st.client.sent)
    st.deliver(media("audio", sender=CUSTOMER_B))
    st.drain()
    assert bodies(to(st, BROKER_NUMBER, before)) == [
        f"💬 *L-01* · +{CUSTOMER_B} (voice)\nvisit kal shaam ho sakti hai?"]


def test_a_broker_whose_window_is_closed_misses_the_forward_but_the_customer_is_told(st):
    open_visit(st)
    before = len(st.client.sent)
    turn(st, "price kam ho sakti hai?")
    assert to(st, BROKER_NUMBER, before) == []
    assert len(to(st, CUSTOMER_B, before)) == 1
    assert st.query("SELECT o.status, o.last_error_code FROM outbound_message o "
                    "WHERE o.dedupe_key LIKE 'turn:%%:fwd:0'") == [("failed", "outside_window")]


def test_an_accepted_handoff_moves_its_48_hour_release(st):
    open_visit(st)
    broker_says_hi(st)
    accept(st)
    turn(st, "price kam ho sakti hai?")
    [(due_ok,)] = st.query("SELECT release_due_at BETWEEN now() + interval '47 hours' AND "
                           "now() + interval '49 hours' FROM handoff")
    assert due_ok
    assert st.query("SELECT count(*) FROM job WHERE job_type = %s",
                    (handoff.RELEASE_JOB,)) == [(1,)]


def test_a_gated_conversation_without_a_handoff_stays_silent(st):
    scenario_2(st)
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE conversation SET control_state = 'human_owned', mode = 'human' "
                      "WHERE agency_id = %s AND customer_wa_id = %s", (st.agency, CUSTOMER_B))
    before = len(st.client.sent)
    turn(st, "hello?")
    assert st.client.sent[before:] == []


# --- the send-time gate (R-28) ------------------------------------------------------------

def test_a_reply_drafted_before_the_handoff_landed_is_dropped(st, monkeypatch):
    """Deterministic race: the AI reply is planned, then the handoff commits, then the
    plan is sent. Every AI payload is cancelled by the gate and nothing counts as shown."""
    scenario_2(st)
    shown = st.query("SELECT count(*) FROM shown_item")
    respond = orchestrator._respond

    def drafted_then_handoff(conn, client, job, turn_, engine):
        payloads = respond(conn, client, job, turn_, engine)   # the drafted AI reply
        with tenant_transaction(conn, turn_.agency_id):
            items = references.shown_items(conn, turn_.conversation_id)
            handoff.open_handoff(conn, turn_.conversation_id, items[:1], "visit")
        return payloads

    monkeypatch.setattr(orchestrator, "_respond", drafted_then_handoff)
    before = len(st.client.sent)
    search_turn(st, "Phase 8 mein bhi dekho", [p("location", "add", "Phase 8")], CUSTOMER_B)
    assert to(st, CUSTOMER_B, before) == []   # (the broker's alert is not an AI message)
    assert st.query("SELECT DISTINCT o.status, o.last_error_code FROM outbound_message o "
                    "JOIN conversation_turn t ON o.dedupe_key LIKE 'turn:' || t.id || ':%%' "
                    "WHERE t.id = (SELECT id FROM conversation_turn ORDER BY created_at DESC "
                    "LIMIT 1)") == [("cancelled", "control_gate")]
    assert st.query("SELECT count(*) FROM shown_item") == shown


def test_the_gate_reads_control_state_and_the_mute(st):
    from app import outbound, whatsapp
    scenario_2(st)
    conv = conversation(st, CUSTOMER_B)

    def send(key, **kw):
        return outbound.send_message(st.conn, st.client, st.agency, conv, CUSTOMER_B,
                                     whatsapp.text("x"), key, **kw).status

    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE conversation SET ai_muted_until = now() + interval '1 hour' "
                      "WHERE id = %s", (conv,))
        assert send("g1") == "cancelled"
        admin.execute("UPDATE conversation SET ai_muted_until = NULL, mode = 'human', "
                      "control_state = 'handoff_pending' WHERE id = %s", (conv,))
        assert send("g2") == "cancelled"
        assert send("g3", ai_generated=False) == "sent"
        admin.execute("UPDATE conversation SET mode = 'ai', control_state = 'ai_active' "
                      "WHERE id = %s", (conv,))
        assert send("g4") == "sent"
