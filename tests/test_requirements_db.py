"""Task 7.3 acceptance tests: the requirement-state manager through the real worker,
orchestrator, turn engine, outbound path and gazetteer cache, with the restricted
manzil_app login (RLS on).

Gemini is a fake transport (no key, no network); speech is a scripted fake; WhatsApp is
the FakeClient of tests/test_gateway.py. The gazetteer is the seeded demo dataset loaded
through app.gazetteer, so locations carry database UUIDs. All data is fictional.
"""

import dataclasses
import json
import logging
import threading
import time
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from app import (
    ai_tools,
    config,
    gazetteer,
    requirements,
    seed,
    turn_engine,
    webhook,
    whatsapp,
)
from app import worker as worker_main
from app.db import tenant_transaction
from app.handlers import make_handlers
from app.ingest import store_event
from app.llm import GeminiClient
from app.provision_agency import create_agency
from app.turn_engine import TEMPLATES, TurnEngine
from app.worker import Worker
from tests.test_gateway import FakeClient
from tests.test_seed import ENV
from tests.test_turn_engine import (
    CUSTOMER,
    FAILOVER,
    KEY,
    PRIMARY,
    FakeClock,
    answer,
    media,
    text,
    tool,
    tools_answer,
)

PNID = "100000000000001"


class FakeSpeech:
    """Stands in for app.speech.SpeechService: transcribes each voice note to a scripted
    transcript and stores stt_meta, as the real service does."""

    def __init__(self):
        self.transcripts = []
        self.meta = {"status": "transcribed", "provider": "elevenlabs"}

    def transcribe_turn(self, conn, job, turn, client):
        messages, statuses = [], []
        for message in turn.messages:
            if message.type == "audio":
                transcript = self.transcripts.pop(0)
                with tenant_transaction(conn, turn.agency_id):
                    conn.execute("UPDATE message SET transcript = %s, stt_meta = %s "
                                 "WHERE id = %s",
                                 (transcript, psycopg.types.json.Jsonb(self.meta), message.id))
                message = dataclasses.replace(message, text=transcript)
                statuses.append(self.meta["status"])
            messages.append(message)
        return dataclasses.replace(turn, messages=tuple(messages), voice=tuple(statuses))


@pytest.fixture
def st(app_role_db):
    admin_url, app_url, _ = app_role_db
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, seed.DEMO_AGENCY_NAME, "demo", PNID)
    result = seed.run(agency, admin_url, app_url, ENV, apply=True)
    assert result.conflicts == []
    with psycopg.connect(admin_url, autocommit=True) as admin:
        ids, _ = seed.resolve_locations(admin, seed.load_dataset())
    ids = {key: str(uid) for key, uid in ids.items()}
    cache = gazetteer.GazetteerCache.from_database_url(app_url)
    cache.load()
    service = requirements.RequirementService(cache)
    clock, script, requests = FakeClock(), [], []

    def handler(request):
        requests.append(SimpleNamespace(
            model=request.url.path.rsplit("/", 1)[1].split(":")[0],
            raw=request.content, body=json.loads(request.content)))
        step = script.pop(0)
        if callable(step):
            return step(request)
        return httpx.Response(200, json=step)

    engine = TurnEngine(GeminiClient(KEY, transport=httpx.MockTransport(handler)),
                        PRIMARY, FAILOVER, clock=clock, requirements=service)
    client, speech = FakeClient(), FakeSpeech()
    settings = SimpleNamespace(meta_access_token="unused", meta_api_version="v25.0",
                               s3_bucket=None, site_address="demo.example")
    handlers = make_handlers(settings, client_factory=lambda pnid: client, engine=engine,
                             speech_service=speech)
    worker = Worker(app_url, [agency], handlers, threading.Event(), backoff=lambda n: 0)

    def deliver(*messages):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": PNID}, "messages": list(messages)}}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(conn, agency):
            for event in events:
                store_event(conn, agency, event)

    def drain():
        while worker.run_once():
            pass

    def query(sql, params=(), *, agency_id=agency):
        with tenant_transaction(conn, agency_id):
            return conn.execute(sql, params).fetchall()

    def sent_texts():
        return [payload["text"]["body"] for _, payload in client.sent
                if payload["type"] == "text"]

    def say(body, *patches, sender=CUSTOMER):
        """One customer text turn whose model calls update_requirements once per patch."""
        before = len(sent_texts())
        for patch in patches:
            script.append(tools_answer(tool("update_requirements", {"patch": patch})))
        script.append(answer())
        deliver(text(body, sender=sender))
        drain()
        return sent_texts()[before:]

    def state():
        rows = query("SELECT version, slots FROM requirement_state")
        return rows[0] if rows else (0, {})

    def patches():
        return query("SELECT version, ops, source_message_id FROM requirement_patch_log "
                     "ORDER BY version")

    def pending():
        return query("SELECT slot, status, unanswered_turns, candidates "
                     "FROM pending_clarification ORDER BY created_at")

    def nodes():
        return state()[1].get("location", {}).get("value", {}).get("nodes")

    yield SimpleNamespace(
        conn=conn, app_url=app_url, admin_url=admin_url, agency=agency, client=client,
        engine=engine, service=service, cache=cache, ids=ids, script=script,
        requests=requests, speech=speech, deliver=deliver, drain=drain, query=query,
        sent_texts=sent_texts, say=say, state=state, patches=patches, pending=pending,
        nodes=nodes, worker=worker, handlers=handlers, clock=clock,
    )
    worker.close()
    conn.close()


def p(slot, kind, span):
    return {"slot": slot, "op": kind, "raw_span": span}


def fix(st, *keys):
    return [st.ids[k] for k in keys]


# --- the plan's done conditions ---------------------------------------------------------

def test_update_requirements_stores_a_versioned_patch_through_the_real_worker(st):
    reply = st.say(
        "buy ghar DHA Phase 6, 500 gaz, budget 8 crore tak",
        [p("purpose", "set", "buy"), p("property_types", "set", "ghar"),
         p("location", "set", "DHA Phase 6"), p("size", "set", "500 gaz"),
         p("budget", "set", "8 crore tak")])
    assert reply == [TEMPLATES["requirements_saved"]]
    version, slots = st.state()
    assert version == 1
    assert slots["location"]["value"]["nodes"] == fix(st, "dha-ph6")
    assert slots["location"]["value"]["city"] == "KHI"
    assert slots["location"]["method"] == "exact" and slots["location"]["key"] == "dha phase 6"
    assert slots["purpose"]["value"] == "sale" and slots["property_types"]["value"] == ["house"]
    assert slots["budget"]["value"]["max"] == 80_000_000
    assert slots["size"]["value"]["nominal_sqyd"] == 500

    ((v, ops, source),) = st.patches()
    turn_id, message_id = st.query("SELECT t.id, m.id FROM conversation_turn t "
                                   "JOIN message m ON m.turn_id = t.id")[0]
    assert v == 1 and source == message_id and len(ops) == 5
    assert {o["call"] for o in ops} == {"c0"} and {o["turn"] for o in ops} == {str(turn_id)}
    assert all(o["status"] == "applied" for o in ops)


def test_the_model_sees_outcomes_without_ids_or_customer_words(st):
    st.say("DHA Phase 6 mein ghar", [p("location", "set", "DHA Phase 6"),
                                      p("location", "add", "Atlantis")])
    response = st.requests[1].body["contents"][-1]["parts"][0]["functionResponse"]["response"]
    assert response["status"] == "ok"
    assert [(o["slot"], o["status"]) for o in response["ops"]] == [
        ("location", "applied"), ("location", "rejected")]
    blob = json.dumps(response)
    assert "dha" not in blob.lower() and not any(i in blob for i in st.ids.values())


def test_follow_up_resolves_a_bare_level_inside_the_stored_area_and_bhi_adds(st):
    st.say("DHA Phase 6 mein ghar", [p("location", "set", "DHA Phase 6")])
    reply = st.say("Phase 8 mein bhi dekho", [p("location", "add", "Phase 8")])
    assert reply == [TEMPLATES["requirements_saved"]]
    assert st.nodes() == fix(st, "dha-ph6", "dha-ph8")
    assert st.state()[1]["location"]["method"] == "level_in_area"
    # No "bhi": the same words replace.
    st.say("Phase 8 mein dikhao", [p("location", "set", "Phase 8")])
    assert st.nodes() == fix(st, "dha-ph8")
    assert st.state()[0] == 3


def test_a_stale_area_hint_never_becomes_the_active_area(st):
    st.say("Clifton", [p("location", "set", "Clifton")])
    reply = st.say("phase 8", [p("location", "set", "phase 8")])
    assert reply == [TEMPLATES["location_unknown_in_area"].format(area="Clifton")]
    assert st.nodes() == fix(st, "clf")                       # unchanged
    reply = st.say("block 5", [p("location", "set", "block 5")])   # still Clifton's context
    assert st.nodes() == fix(st, "clf-b5")
    assert reply == [TEMPLATES["requirements_saved"]]


def test_unknown_names_change_nothing_and_list_the_covered_areas(st):
    st.say("DHA Phase 6", [p("location", "set", "DHA Phase 6")])
    reply = st.say("Malir Cantt", [p("location", "set", "Malir Cantt")])
    assert st.nodes() == fix(st, "dha-ph6")
    assert reply[0].startswith("Ye jagah mujhe nahi mili.") and "Clifton" in reply[0]
    reply = st.say("Bahria precinct 12", [p("location", "set", "Bahria precinct 12")])
    assert "Bahria" in reply[0] and st.nodes() == fix(st, "dha-ph6")


def test_other_cities_are_declined_and_served_in_one_reply(st):
    reply = st.say("Clifton aur Lahore", [p("location", "set", "Clifton"),
                                            p("location", "add", "Lahore")])
    assert st.nodes() == fix(st, "clf")
    assert reply == [TEMPLATES["requirements_saved"] + "\n\n" +
                     TEMPLATES["location_out_of_coverage"].format(city="Karachi",
                                                                   other="Lahore")]


def test_dha_and_dha_city_are_distinct_in_the_database_gazetteer(st):
    st.say("DHA City mein plot", [p("location", "set", "DHA City")])
    assert st.nodes() == fix(st, "dhc")
    st.say("sector 13", [p("location", "set", "sector 13")])
    assert st.nodes() == fix(st, "dhc-s13")
    st.say("DHA phase 6", [p("location", "set", "DHA phase 6")])
    assert st.nodes() == fix(st, "dha-ph6")
    reply = st.say("DHA sector 13", [p("location", "set", "DHA sector 13")])
    assert st.nodes() == fix(st, "dha-ph6")               # never DHA City's sector
    assert "DHA" in reply[0]


# --- adversarial spans ----------------------------------------------------------------------

def test_a_fabricated_span_changes_nothing(st):
    reply = st.say("kuch dikhao", [p("location", "set", "Clifton"),
                                    p("budget", "set", "99 crore")])
    assert st.state()[1] == {}
    assert reply == [TEMPLATES["requirements_unclear"]]
    ((_, ops, _),) = st.patches()      # the attempt is still logged, for the audit
    assert [o["detail"] for o in ops] == ["span_not_in_turn", "span_not_in_turn"]


def test_history_does_not_make_a_span_valid(st):
    st.say("Clifton mein ghar", [p("location", "set", "Clifton")])
    st.say("kuch aur", [p("location", "set", "DHA")])        # not in this turn's text
    assert st.nodes() == fix(st, "clf")


def test_a_span_cannot_straddle_two_messages_of_a_turn(st):
    for body in ("DHA phase 6", "8 crore tak"):
        st.deliver(text(body))
    st.script.append(tools_answer(tool("update_requirements", {"patch": [
        p("location", "set", "6 8 crore")]})))
    st.script.append(answer())
    st.drain()
    assert st.state()[1] == {}


def test_a_misread_amount_is_replaced_by_the_normaliser_over_the_message(st):
    st.say("mera budget 80 lakh hai, kiraye pe", [p("budget", "set", "8 crore")])
    slots = st.state()[1]
    assert slots["budget"]["value"]["stated_pkr"] == 8_000_000
    ((_, ops, _),) = st.patches()
    assert ops[0]["dropped_span"] == "8 crore" and ops[0]["raw_span"] == "80 lakh"


# --- clarification ----------------------------------------------------------------------------

def test_an_ambiguous_place_asks_and_the_answer_is_checked_against_the_stored_candidates(st):
    reply = st.say("P6 mein ghar", [p("location", "set", "P6")])
    assert len(reply) == 1 and "DHA Phase 6" in reply[0] and "Bahria Precinct 6" in reply[0]
    assert st.nodes() is None
    ((slot, status, unanswered, candidates),) = st.pending()
    assert (slot, status, unanswered) == ("location", "open", 0)
    assert {c["location_id"] for c in candidates} == set(fix(st, "dha-ph6", "btk-p6"))
    assert candidates[0]["origin"]["raw"] == "P6"

    reply = st.say("Bahria", [p("location", "set", "Bahria")])
    assert st.nodes() == fix(st, "btk-p6")
    assert reply == [TEMPLATES["requirements_saved"] + "\n\n" +
                     TEMPLATES["location_chosen"].format(area="Bahria Precinct 6")]
    assert [row[1] for row in st.pending()] == ["resolved"]
    assert st.state()[1]["location"]["method"] == "clarified"


def test_an_unrelated_turn_keeps_the_question_and_re_asks_once(st):
    st.say("P6 mein ghar", [p("location", "set", "P6")])
    reply = st.say("3 bed chahiye", [p("beds_min", "set", "3 bed")])
    assert reply[0].startswith(TEMPLATES["requirements_saved"])
    assert TEMPLATES["location_reask"].split("{")[0] in reply[0] and "DHA Phase 6" in reply[0]
    ((_, status, unanswered, _),) = st.pending()
    assert (status, unanswered) == ("open", 1)
    assert st.state()[1]["beds_min"]["value"] == 3          # other slots keep accumulating


def test_the_second_unanswered_turn_expires_the_question(st):
    st.say("P6 mein ghar", [p("location", "set", "P6")])
    st.say("3 bed chahiye", [p("beds_min", "set", "3 bed")])
    reply = st.say("garage bhi", [])
    assert reply[-1] == TEMPLATES["location_expired_none"]   # DHA and Bahria share no area
    assert [row[1] for row in st.pending()] == ["expired"]
    assert st.nodes() is None


def test_expiry_falls_back_to_the_shared_area(st):
    st.say("Bahria heigts", [p("location", "set", "Bahria heigts")])
    (row,) = st.pending()
    assert len(row[3]) == 1 and row[3][0]["location_id"] == st.ids["btk-heights"]
    st.say("kuch aur", [])
    reply = st.say("kuch aur", [])
    assert reply[-1] == TEMPLATES["location_expired"].format(area="Bahria Town Karachi")
    assert st.nodes() == [st.ids["btk"]]
    slots = st.state()[1]["location"]
    assert slots["source"] == "inferred" and slots["confirmed"] is False
    assert slots["echo"] == ["fallback"]
    assert [r[1] for r in st.pending()] == ["expired"]


def test_an_answer_on_the_second_turn_wins_over_expiry(st):
    st.say("P6", [p("location", "set", "P6")])
    st.say("kuch aur", [])
    st.say("DHA", [p("location", "set", "DHA")])
    assert st.nodes() == fix(st, "dha-ph6")
    assert [r[1] for r in st.pending()] == ["resolved"]


def test_a_single_fuzzy_candidate_is_asked_and_answered_by_name(st):
    reply = st.say("zamzama comercial", [p("location", "set", "zamzama comercial")])
    assert reply == [TEMPLATES["location_confirm"].format(options="Zamzama Commercial")]
    st.say("Zamzama Commercial", [p("location", "set", "Zamzama Commercial")])
    assert st.nodes() == fix(st, "dha-ph5-zamzama")


def test_a_time_expired_question_is_closed_before_the_next_patch(st):
    st.say("Bahria heigts", [p("location", "set", "Bahria heigts")])
    st.query("UPDATE pending_clarification SET created_at = now() - interval '3 days', "
             "expires_at = now() - interval '2 days' RETURNING id")
    st.say("Clifton", [p("location", "set", "Clifton")])
    assert st.nodes() == fix(st, "clf")
    assert [r[1] for r in st.pending()] == ["expired"]


def test_only_one_question_is_open_per_conversation(st):
    st.say("P6", [p("location", "set", "P6")])
    st.say("block 6", [p("location", "set", "block 6")])
    assert [r[1] for r in st.pending()] == ["cancelled", "open"]
    assert st.query("SELECT count(*) FROM pending_clarification WHERE status = 'open'") == [(1,)]


# --- purpose, budget, relative ops ----------------------------------------------------------

def test_buy_to_rent_moves_the_budget_and_asks_for_the_monthly_one(st):
    st.say("buy ghar DHA Phase 6, 8 crore tak",
           [p("purpose", "set", "buy"), p("property_types", "set", "ghar"),
            p("location", "set", "DHA Phase 6"), p("budget", "set", "8 crore tak")])
    reply = st.say("rent pe bhi mil sakta hai?", [p("purpose", "set", "rent pe")])
    assert reply == [TEMPLATES["requirements_saved"] + "\n\n" + TEMPLATES["budget_ask_rent"]]
    slots = st.state()[1]
    assert slots["purpose"]["value"] == "rent" and "budget" not in slots
    assert slots["previous_budget"]["value"]["max"] == 80_000_000
    assert slots["location"]["value"]["nodes"] == fix(st, "dha-ph6")
    assert slots["property_types"]["value"] == ["house"]


def test_cheaper_uses_the_last_search_excludes_it_and_never_invents_an_amount(st):
    st.say("buy ghar DHA Phase 6, 8 crore tak",
           [p("purpose", "set", "buy"), p("property_types", "set", "ghar"),
            p("location", "set", "DHA Phase 6"), p("budget", "set", "8 crore tak")])
    reply = st.say("Kuch sasta dikhao", [p("budget", "relative", "Kuch sasta")])
    assert reply == [TEMPLATES["budget_ask"]]           # no search yet: nothing invented
    assert st.state()[1]["budget"]["value"]["max"] == 80_000_000

    codes = ["DH6-101", "DH6-102", "DH7-101"]
    props = st.query("SELECT id, listing_code, price_pkr FROM v_public_listing "
                     "WHERE listing_code = ANY(%s) ORDER BY listing_code", (codes,))
    assert len(props) == 3
    version = st.state()[0]
    run_id = st.query("INSERT INTO search_run (agency_id, conversation_id, "
                      "requirement_version, filters, relaxations, total_matches) "
                      "SELECT current_agency_id(), conversation_id, %s, '{}', '[]', 3 "
                      "FROM requirement_state RETURNING id", (version,))[0][0]
    for position, (prop_id, _, _) in enumerate(props, 1):
        st.query("INSERT INTO shown_item (agency_id, conversation_id, search_run_id, "
                 "position, property_id) SELECT current_agency_id(), conversation_id, %s, "
                 "%s, %s FROM search_run WHERE id = %s RETURNING id",
                 (run_id, position, prop_id, run_id))
    st.say("Kuch sasta dikhao", [p("budget", "relative", "Kuch sasta")])
    cheapest = min(int(row[2]) for row in props)
    slots = st.state()[1]
    assert slots["budget"]["value"]["max"] == round(cheapest * 0.97)
    assert slots["budget"]["value"]["no_budget_relaxation"] is True
    assert sorted(slots["excluded_property_ids"]["value"]) == sorted(str(r[0]) for r in props)


def test_an_inferred_purpose_is_labelled_and_echoed(st):
    reply = st.say("8 crore ka ghar", [p("budget", "set", "8 crore")])
    slots = st.state()[1]
    assert slots["purpose"]["source"] == "inferred" and slots["purpose"]["confirmed"] is False
    assert reply == [TEMPLATES["requirements_saved"] + "\n\n"
                     + TEMPLATES["purpose_inferred"].format(purpose="khareedna")]


def test_a_purpose_question_is_answered_by_an_explicit_purpose(st):
    reply = st.say("30 lakh ka ghar", [p("budget", "set", "30 lakh")])
    assert TEMPLATES["purpose_ask"] in reply[0]
    ((slot, status, _, _),) = st.pending()
    assert (slot, status) == ("purpose", "open")
    st.say("khareedna hai", [p("purpose", "set", "khareedna hai")])
    assert st.state()[1]["purpose"]["value"] == "sale"
    assert [r[1] for r in st.pending()] == ["resolved"]
    assert st.state()[1]["budget"]["value"]["period"] == "one_time"


# --- voice -----------------------------------------------------------------------------------

def test_a_voice_transcript_feeds_the_same_path_but_is_unconfirmed(st):
    st.speech.transcripts.append("Clifton block 5 mein ghar, budget aath crore tak")
    st.script.append(tools_answer(tool("update_requirements", {"patch": [
        p("location", "set", "Clifton block 5"), p("budget", "set", "aath crore tak")]})))
    st.script.append(answer())
    st.deliver(media("audio"))
    st.drain()
    slots = st.state()[1]
    assert slots["location"]["value"]["nodes"] == fix(st, "clf-b5")
    assert slots["location"]["source"] == "voice" and slots["location"]["confirmed"] is False
    assert "echo" not in slots["location"]
    assert slots["budget"]["value"]["max"] == 80_000_000
    assert st.sent_texts()[0].startswith(TEMPLATES["requirements_saved"])


def test_a_weak_transcript_is_marked_for_an_echo(st):
    st.speech.meta = {"status": "transcribed", "provider": "gemini",
                      "fallback_reason": "repetition", "rejected_transcript": "x"}
    st.speech.transcripts.append("Clifton block 5")
    st.script.append(tools_answer(tool("update_requirements", {"patch": [
        p("location", "set", "Clifton block 5")]})))
    st.script.append(answer())
    st.deliver(media("audio"))
    st.drain()
    slots = st.state()[1]["location"]
    assert slots["echo"] == ["voice_weak"] and slots["confidence"] == 0.5


def test_text_and_a_voice_note_in_one_turn_keep_their_own_source(st):
    st.speech.transcripts.append("Clifton block 5")
    st.deliver(media("audio"), text("3 bed"))
    st.script.append(tools_answer(tool("update_requirements", {"patch": [
        p("location", "set", "Clifton block 5"), p("beds_min", "set", "3 bed")]})))
    st.script.append(answer())
    st.drain()
    slots = st.state()[1]
    assert slots["location"]["source"] == "voice" and slots["beds_min"]["source"] == "text"


# --- replays, idempotency, concurrency -----------------------------------------------------------

def test_a_retried_turn_never_applies_a_patch_twice(st, monkeypatch):
    st.say("buy ghar DHA Phase 6, 8 crore tak",
           [p("purpose", "set", "buy"), p("property_types", "set", "ghar"),
            p("location", "set", "DHA Phase 6"), p("budget", "set", "8 crore tak")])
    failures = [RuntimeError("crash before the plan is stored")]
    original = turn_engine.ground_reply

    def crash_once(payloads, *args):
        if failures:
            raise failures.pop()
        return original(payloads, *args)

    monkeypatch.setattr(turn_engine, "ground_reply", crash_once)
    for _ in range(2):   # the first attempt crashes after the tool ran; the retry asks again
        st.script.append(tools_answer(tool("update_requirements", {"patch": [
            p("budget", "relative", "thoda upar")]})))
        st.script.append(answer())
    st.deliver(text("thoda upar"))
    st.drain()
    # "thoda upar" compounds (max x 1.10); a replay must not apply it a second time.
    assert st.state()[1]["budget"]["value"]["max"] == 88_000_000
    assert st.state()[0] == 2 and len(st.patches()) == 2
    assert st.sent_texts()[-1] == TEMPLATES["requirements_saved"]


def test_a_retried_turn_counts_an_unanswered_question_once(st, monkeypatch):
    st.say("P6", [p("location", "set", "P6")])
    failures = [RuntimeError("crash")]
    original = turn_engine.ground_reply

    def crash_once(payloads, *args):
        if failures:
            raise failures.pop()
        return original(payloads, *args)

    monkeypatch.setattr(turn_engine, "ground_reply", crash_once)
    st.say("kuch aur", [], [])
    ((_, status, unanswered, _),) = st.pending()
    assert (status, unanswered) == ("open", 1)           # one customer turn, one count


def test_concurrent_patches_serialise_on_the_state_row(st, monkeypatch):
    st.say("Clifton", [p("location", "set", "Clifton")])
    turn_row = st.query("SELECT t.id, t.conversation_id, m.id FROM conversation_turn t "
                        "JOIN message m ON m.turn_id = t.id")[0]
    message = SimpleNamespace(id=turn_row[2], type="text")
    turn = SimpleNamespace(id=turn_row[0], agency_id=st.agency, conversation_id=turn_row[1],
                           messages=(message,))
    inside, release = threading.Event(), threading.Event()
    original = requirements.merge_patch

    def held(ctx, slots, ops, evidence):
        if ctx.call_id == "slow":
            inside.set()
            assert release.wait(10)
        return original(ctx, slots, ops, evidence)

    monkeypatch.setattr(requirements, "merge_patch", held)
    second = psycopg.connect(st.app_url, autocommit=True)
    out = {}

    def apply(name, conn, patch, span):
        out[name] = st.service.apply(conn, turn, [(message, span)], patch, name)

    first_thread = threading.Thread(target=apply, args=(
        "slow", st.conn, [p("beds_min", "set", "3 bed")], "3 bed"))
    first_thread.start()
    assert inside.wait(10)
    other = threading.Thread(target=apply, args=(
        "fast", second, [p("baths_min", "set", "2 bath")], "2 bath"))
    other.start()
    time.sleep(0.5)
    assert "fast" not in out                     # waits for the row lock
    release.set()
    first_thread.join(10)
    other.join(10)
    second.close()
    version, slots = st.state()
    assert version == 3 and slots["beds_min"]["value"] == 3 and slots["baths_min"]["value"] == 2
    assert [row[0] for row in st.patches()] == [1, 2, 3]


def test_two_worker_threads_run_one_conversation_s_turns_one_at_a_time(st):
    blocked, release = threading.Event(), threading.Event()

    def first_call(request):
        blocked.set()
        assert release.wait(15)
        return httpx.Response(200, json=tools_answer(tool("update_requirements", {"patch": [
            p("location", "set", "DHA Phase 6")]})))

    st.script += [first_call, answer(),
                  tools_answer(tool("update_requirements", {"patch": [
                      p("location", "add", "Phase 8")]})), answer()]
    st.deliver(text("DHA Phase 6"))
    other = Worker(st.app_url, [st.agency], st.handlers, threading.Event(),
                   backoff=lambda n: 0, name="worker-2")
    first = threading.Thread(target=st.drain)
    first.start()
    assert blocked.wait(10)
    st.deliver(text("Phase 8 bhi"))
    done = threading.Event()

    def loop():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not done.is_set():
            if not other.run_once():
                time.sleep(0.05)

    second = threading.Thread(target=loop)
    second.start()
    time.sleep(0.6)
    assert len(st.requests) == 1                 # the second job waits for the conversation
    release.set()
    first.join(20)
    for _ in range(100):
        if st.query("SELECT count(*) FROM conversation_turn WHERE status = 'replied'") == [(2,)]:
            break
        time.sleep(0.1)
    done.set()
    second.join(10)
    other.close()
    assert st.nodes() == fix(st, "dha-ph6", "dha-ph8")     # the 2nd turn saw the 1st's state
    assert [row[0] for row in st.patches()] == [1, 2]


# --- stale state ------------------------------------------------------------------------------

def test_requirements_reset_after_90_idle_days_and_stale_context_is_not_used(st):
    st.say("Gulshan block 13", [p("location", "set", "Gulshan block 13")])
    assert st.nodes() == fix(st, "gli-b13")
    st.query("UPDATE message SET created_at = now() - interval '100 days' RETURNING id")
    st.say("block 6", [p("location", "set", "block 6")])
    # Without the stale Gulshan context "block 6" is ambiguous, not Gulshan's block 6.
    assert st.nodes() is None
    assert [r[1] for r in st.pending()] == ["open"]
    ops = st.patches()[-1][1]
    assert [o["kind"] for o in ops if o.get("kind")] == ["reset"]


# --- owner decisions after the first review ---------------------------------------------------------

def test_across_area_expiry_preserves_the_previous_location(st):
    st.say("Clifton aur DHA", [p("location", "set", "Clifton aur DHA")])
    st.say("P6 mein ghar", [p("location", "set", "P6")])   # two areas stored: no context
    st.say("3 bed chahiye", [p("beds_min", "set", "3 bed")])
    reply = st.say("garage bhi", [])
    assert reply[-1] == TEMPLATES["location_expired_none"]
    assert st.nodes() == fix(st, "clf", "dha")               # neither DHA 6 nor Bahria was guessed
    assert [r[1] for r in st.pending()] == ["expired"]


def ask_zamzama(st):
    reply = st.say("zamzama comercial", [p("location", "set", "zamzama comercial")])
    assert reply == [TEMPLATES["location_confirm"].format(options="Zamzama Commercial")]


def test_a_plain_haan_confirms_the_sole_fuzzy_candidate_without_a_tool_call(st):
    ask_zamzama(st)
    reply = st.say("haan")                                     # the model calls no tool
    assert st.nodes() == fix(st, "dha-ph5-zamzama")
    assert reply == [TEMPLATES["requirements_saved"] + "\n\n"
                     + TEMPLATES["location_chosen"].format(area="Zamzama Commercial")]
    assert [r[1] for r in st.pending()] == ["resolved"]
    ((_, ops, _), *_) = st.patches()[-1:]
    assert ops[0]["detail"] == "affirmed" and ops[0]["call"] == "end"


def test_a_plain_haan_through_the_tool_confirms_it_too(st):
    ask_zamzama(st)
    st.say("haan", [p("location", "set", "haan")])
    assert st.nodes() == fix(st, "dha-ph5-zamzama")
    assert st.state()[1]["location"]["method"] == "clarified"


def test_haan_does_not_pick_one_of_several_candidates(st):
    st.say("block 6", [p("location", "set", "block 6")])
    reply = st.say("haan", [p("location", "set", "haan")])
    assert st.nodes() is None
    assert [r[1] for r in st.pending()] == ["open"]
    assert TEMPLATES["location_reask"].split("{")[0] in reply[-1]


def test_haan_after_the_question_expired_confirms_nothing(st):
    ask_zamzama(st)
    st.query("UPDATE pending_clarification SET created_at = now() - interval '2 days', "
             "expires_at = now() - interval '1 day' RETURNING id")
    reply = st.say("haan", [p("location", "set", "haan")])
    assert [r[1] for r in st.pending()] == ["expired"]
    assert st.nodes() == [st.ids["dha"]]                     # the shared-area fallback, not Zamzama
    assert TEMPLATES["location_chosen"].format(area="Zamzama Commercial") not in reply[-1]


def test_haan_with_another_named_place_follows_the_place(st):
    ask_zamzama(st)
    st.say("haan Clifton", [p("location", "set", "Clifton")])
    assert st.nodes() == fix(st, "clf")
    assert [r[1] for r in st.pending()] == ["cancelled"]


def test_haan_in_a_turn_with_a_second_message_confirms_nothing(st):
    ask_zamzama(st)
    st.deliver(text("haan"), text("Lahore bhi"))
    st.script.append(answer())
    st.drain()
    assert st.nodes() is None
    ((_, status, unanswered, _),) = st.pending()
    assert (status, unanswered) == ("open", 1)


def test_a_question_that_was_never_sent_cannot_be_confirmed(st):
    ask_zamzama(st)
    st.query("UPDATE conversation_turn SET status = 'failed' RETURNING id")
    st.say("haan")
    assert st.nodes() is None


def test_clifton_aur_dha_is_two_grounded_spans_in_one_patch(st):
    reply = st.say("Clifton aur DHA mein dekhna hai", [p("location", "set", "Clifton aur DHA")])
    assert st.nodes() == fix(st, "clf", "dha")
    ((_, ops, _),) = st.patches()
    assert [(o["raw_span"], o["split_from"]) for o in ops] == [
        ("Clifton", "Clifton aur DHA"), ("DHA", "Clifton aur DHA")]
    assert reply == [TEMPLATES["requirements_saved"]]


def test_an_impossible_budget_is_asked_then_kept_when_restated(st):
    st.say("buy house DHA Phase 6", [p("purpose", "set", "buy"),
                                      p("property_types", "set", "house"),
                                      p("location", "set", "DHA Phase 6")])
    reply = st.say("budget 5 lakh", [p("budget", "set", "5 lakh")])
    assert reply == [TEMPLATES["budget_implausible"]]
    assert "budget" not in st.state()[1]
    ((_, ops, _),) = st.patches()[-1:]
    assert ops[0]["guard_pkr"] == 500_000 and ops[0]["detail"] == "implausible"
    reply = st.say("budget 5 lakh", [p("budget", "set", "5 lakh")])
    assert st.state()[1]["budget"]["echo"] == ["implausible"]
    assert st.state()[1]["budget"]["confirmed"] is False
    assert reply == [TEMPLATES["requirements_saved"]]
    st.say("budget 8 crore tak", [p("budget", "set", "8 crore tak")])
    assert st.state()[1]["budget"]["value"]["max"] == 80_000_000
    assert "echo" not in st.state()[1]["budget"]
    assert st.state()[1]["budget"]["confirmed"] is True


def test_a_budget_inside_the_inventory_band_is_stored_normally(st):
    st.say("buy house DHA Phase 6, 8 crore tak",
           [p("purpose", "set", "buy"), p("property_types", "set", "house"),
            p("location", "set", "DHA Phase 6"), p("budget", "set", "8 crore tak")])
    assert st.state()[1]["budget"]["value"]["max"] == 80_000_000


def test_construction_and_investment_are_stored_from_the_vocabulary(st):
    st.say("under construction, investment ke liye, rental income",
           [p("construction", "set", "under construction"),
            p("investment", "set", "investment ke liye"),
            p("investment", "add", "rental income")])
    slots = st.state()[1]
    assert slots["construction"]["value"] == ["under_construction"]
    assert slots["investment"]["value"] == {"intent": True, "tags": ["rental_income"]}


def test_the_model_sees_the_stored_state_and_the_open_question(st):
    st.say("Karachi, buy", [p("location", "set", "Karachi"), p("purpose", "set", "buy")])
    st.say("P6", [p("location", "set", "P6")])
    st.say("kuch aur", [])
    parts = st.requests[-1].body["contents"][0]["parts"]
    snapshot = next(part["text"] for part in parts if part["text"].startswith("Stored state"))
    assert "- location: Karachi" in snapshot and "- purpose: sale" in snapshot
    assert "options: Bahria Precinct 6 | DHA Phase 6" in snapshot
    assert not any(i in snapshot for i in st.ids.values())
    assert parts[-1]["text"].startswith("Current messages")


def test_the_first_turn_has_no_snapshot(st):
    st.say("hello", [])
    parts = st.requests[0].body["contents"][0]["parts"]
    assert not any(part["text"].startswith("Stored state") for part in parts)


def test_the_24_hour_cap_is_exact_enough(st):
    st.say("Bahria heigts", [p("location", "set", "Bahria heigts")])
    st.query("UPDATE conversation_turn SET created_at = now() - interval '30 hours' "
             "RETURNING id")
    st.query("UPDATE pending_clarification SET created_at = now() - interval '23 hours', "
             "expires_at = now() + interval '1 hour' RETURNING id")
    st.say("Clifton", [])
    assert [r[1] for r in st.pending()] == ["open"]
    st.query("UPDATE pending_clarification SET created_at = now() - interval '25 hours', "
             "expires_at = now() - interval '1 hour' RETURNING id")
    st.say("Clifton", [p("location", "set", "Clifton")])
    assert [r[1] for r in st.pending()] == ["expired"] and st.nodes() == fix(st, "clf")


# --- the expiry / confirmation decision survives a crash after its state write -------------------

def _crash_once(monkeypatch):
    failures = [RuntimeError("crash after the state write, before the plan is stored")]
    original = turn_engine.render_customer  # the step between the state write and the plan

    def crash(outcome, customer):
        if failures:
            raise failures.pop()
        return original(outcome, customer)

    monkeypatch.setattr(turn_engine, "render_customer", crash)


def _setup_cross_area(st):
    st.say("Clifton aur DHA", [p("location", "set", "Clifton aur DHA")])
    st.say("P6", [p("location", "set", "P6")])
    st.say("3 bed", [p("beds_min", "set", "3 bed")])
    return "garage bhi", TEMPLATES["location_expired_none"], fix(st, "clf", "dha")


def _setup_fallback(st):
    st.say("Bahria heigts", [p("location", "set", "Bahria heigts")])
    st.say("kuch aur", [])
    return "kuch aur", TEMPLATES["location_expired"].format(area="Bahria Town Karachi"), (
        [st.ids["btk"]])


def _setup_confirm(st):
    ask_zamzama(st)
    expected = (TEMPLATES["requirements_saved"] + "\n\n"
                + TEMPLATES["location_chosen"].format(area="Zamzama Commercial"))
    return "haan", expected, fix(st, "dha-ph5-zamzama")


@pytest.mark.parametrize("setup", [_setup_cross_area, _setup_fallback, _setup_confirm],
                         ids=["expiry-no-change", "expiry-fallback", "haan-confirm"])
def test_a_crash_after_the_decision_never_loses_the_notice(st, monkeypatch, setup):
    body, expected, nodes = setup(st)
    versions = len(st.patches())
    sent, calls = len(st.sent_texts()), len(st.requests)
    _crash_once(monkeypatch)
    st.script += [answer(), answer()]          # the first attempt, then the retry
    st.deliver(text(body))
    st.drain()
    assert len(st.requests) - calls == 2      # at-least-once: the retry asks the provider again
    texts = st.sent_texts()[sent:]
    assert texts == [expected]                  # exactly one notice, eventually
    assert st.nodes() == nodes
    assert st.pending()[-1][1] in ("expired", "resolved")
    assert len(st.patches()) == versions + 1    # decided once: one "end" row, not two
    assert st.query("SELECT count(*) FROM requirement_patch_log WHERE ops -> 0 ->> 'call' "
                    "= 'end'") == [(1,)]
    assert st.query("SELECT count(*) FROM outbound_message WHERE dedupe_key LIKE 'turn:%%'"
                    )[0][0] == len(st.sent_texts())


def test_a_send_failure_after_the_decision_resends_the_stored_notice_once(st, monkeypatch):
    body, expected, _ = _setup_cross_area(st)
    sent, calls = len(st.sent_texts()), len(st.requests)
    st.client.outcomes = [whatsapp.RetryableSendError("http_500")]
    st.script += [answer()]
    st.deliver(text(body))
    st.drain()
    assert st.sent_texts()[sent:] == [expected]
    assert len(st.requests) - calls == 1        # no second provider call: the plan was stored


def test_an_expiry_decided_in_the_patch_also_replays_its_notice(st, monkeypatch):
    st.say("Bahria heigts", [p("location", "set", "Bahria heigts")])
    st.query("UPDATE pending_clarification SET created_at = now() - interval '3 days', "
             "expires_at = now() - interval '2 days' RETURNING id")
    st.query("UPDATE conversation_turn SET created_at = now() - interval '3 days' "
             "RETURNING id")
    _crash_once(monkeypatch)
    sent = len(st.sent_texts())
    for _ in range(2):
        st.script.append(tools_answer(tool("update_requirements", {"patch": [
            p("beds_min", "set", "3 bed")]})))
        st.script.append(answer())
    st.deliver(text("3 bed"))
    st.drain()
    (reply,) = st.sent_texts()[sent:]
    assert TEMPLATES["location_expired"].format(area="Bahria Town Karachi") in reply
    assert reply.count(TEMPLATES["location_expired"].format(area="Bahria Town Karachi")) == 1
    assert st.nodes() == [st.ids["btk"]] and st.state()[1]["beds_min"]["value"] == 3


# --- the gazetteer cache and the schema --------------------------------------------------------

def test_a_gazetteer_change_is_picked_up_only_by_an_explicit_refresh(st):
    before = st.cache.snapshot.version
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("INSERT INTO location_alias (location_id, alias, kind) "
                      "VALUES (%s, 'jannat nagar', 'common')", (st.ids["clf"],))
    st.say("jannat nagar", [p("location", "set", "jannat nagar")])
    assert st.nodes() is None                      # the running snapshot does not know it
    assert st.cache.snapshot.version == before
    assert st.cache.refresh() is True
    st.say("jannat nagar", [p("location", "set", "jannat nagar")])
    assert st.nodes() == fix(st, "clf")
    assert st.state()[1]["location"]["method"] == "exact"


def test_a_bad_refresh_keeps_the_previous_snapshot_and_the_service_working(st):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("INSERT INTO location_alias (location_id, alias, kind) "
                      "VALUES (%s, '!!!', 'common')", (st.ids["clf"],))
    with pytest.raises(Exception, match="no usable key"):
        st.cache.refresh()
    st.say("Clifton", [p("location", "set", "Clifton")])
    assert st.nodes() == fix(st, "clf")


def test_the_schema_check_names_the_missing_migration(st):
    requirements.check_schema(st.conn, st.agency)
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("DROP TABLE pending_clarification")
    with pytest.raises(config.ConfigurationError, match="migrations 006 and 007") as caught:
        requirements.check_schema(st.conn, st.agency)
    assert "does not exist" not in str(caught.value)


def test_an_unloaded_gazetteer_is_a_tool_error_never_a_guess(st):
    empty = gazetteer.GazetteerCache(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    service = requirements.RequirementService(empty)
    st.engine.requirements = service
    reply = st.say("Clifton", [p("location", "set", "Clifton")])
    assert st.state()[1] == {}
    assert reply == [TEMPLATES["customer_default"]]


# --- worker start -------------------------------------------------------------------------------

def start(monkeypatch, value, agency):
    """worker_main.run() up to the point where it would start claiming jobs."""
    seen = {}
    monkeypatch.setattr(worker_main.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker_main, "load_settings", lambda: value)
    monkeypatch.setattr(worker_main, "parse_sender_agencies", lambda v: {PNID: agency})

    def handlers(settings, **kwargs):
        seen.update(kwargs)
        return {"process_message": None}

    class Started(Exception):
        pass

    def never(*args, **kwargs):
        raise Started

    monkeypatch.setattr(worker_main, "make_handlers", handlers)
    monkeypatch.setattr(worker_main, "Worker", never)
    try:
        worker_main.run()
    except SystemExit as exit_:
        return exit_.code, seen
    except Started:
        return "started", seen
    raise AssertionError("run() returned")


def settings_for(app_url, agency, provider="gemini"):
    return SimpleNamespace(webhook_sender_agencies="x", database_url=app_url,
                           ai_provider=provider, agency=agency, worker_concurrency=1)


def test_worker_start_loads_the_gazetteer_before_any_job(st, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    code, seen = start(monkeypatch, settings_for(st.app_url, st.agency), st.agency)
    assert code == "started"
    service = seen["requirements"]
    assert isinstance(service, requirements.RequirementService)
    assert service.cache.get().places and "Location gazetteer loaded: 53 locations" in caplog.text


def test_the_stub_does_not_need_the_gazetteer(st, monkeypatch):
    code, seen = start(monkeypatch, settings_for(st.app_url, st.agency, "stub"), st.agency)
    assert code == "started" and seen["requirements"] is None


def test_an_empty_gazetteer_stops_the_worker(app_role_db, monkeypatch, caplog):
    _, app_url, _ = app_role_db      # migrated, not seeded
    with psycopg.connect(app_url, autocommit=True) as conn:
        agency = create_agency(conn, "Demo (DEMO)", "demo", PNID)
    code, _ = start(monkeypatch, settings_for(app_url, agency), agency)
    assert code == worker_main.CONFIGURATION_EXIT_CODE == 78
    assert "Worker not started: AI_PROVIDER=gemini requires a valid location gazetteer" in (
        caplog.text)
    assert "manzil_app" not in caplog.text


def test_one_invalid_owner_row_stops_the_worker_fail_closed(st, monkeypatch, caplog):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("INSERT INTO location_alias (location_id, alias, kind) "
                      "VALUES (%s, '!!!', 'common')", (st.ids["clf"],))
    code, _ = start(monkeypatch, settings_for(st.app_url, st.agency), st.agency)
    assert code == 78
    assert "Gazetteer load failed (GazetteerError)" in caplog.text
    assert "!!!" not in caplog.text


def test_a_missing_requirements_table_stops_the_worker(st, monkeypatch, caplog):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("DROP TABLE pending_clarification")
    code, _ = start(monkeypatch, settings_for(st.app_url, st.agency), st.agency)
    assert code == 78 and "requires migrations 006 and 007" in caplog.text


# --- isolation and privacy ----------------------------------------------------------------------

def test_requirement_rows_are_tenant_isolated(st):
    st.say("P6 mein ghar", [p("location", "set", "P6")])
    with psycopg.connect(st.app_url, autocommit=True) as conn:
        other = create_agency(conn, "Other (DEMO)", "other", "100000000000009")
    for table in ("requirement_state", "requirement_patch_log", "pending_clarification"):
        assert st.query(f"SELECT count(*) FROM {table}", agency_id=other) == [(0,)]
        assert st.query(f"SELECT count(*) FROM {table}")[0][0] >= 1


def test_logs_hold_no_customer_words_keys_or_numbers(st, caplog):
    caplog.set_level(logging.DEBUG)
    st.say("Gulshan block 13 mein 8 crore ka ghar, Bahria Lahore bhi",
           [p("location", "set", "Gulshan block 13"), p("location", "add", "Bahria Lahore"),
            p("budget", "set", "8 crore")])
    st.say("P6", [p("location", "set", "P6")])
    log = caplog.text
    for secret in ("Gulshan", "gulshan", "block 13", "Lahore", "8 crore", "P6", CUSTOMER,
                   "bahria lahore", st.ids["gli-b13"], KEY):
        assert secret not in log, secret
    assert "Location resolution: RESOLVED via" in log


def test_every_reply_template_is_clean_for_the_grounding_seam(st):
    gz = st.cache.get()
    for place in gz.places.values():
        payload = [whatsapp.text(TEMPLATES["location_ambiguous"].format(
            options=place.display_name))]
        assert turn_engine.ground_reply(payload) == payload
    assert all(key in TEMPLATES for key in (
        "requirements_saved", "requirements_unclear", "location_ambiguous", "location_confirm",
        "location_reask", "location_chosen", "location_unknown", "location_unknown_in_area",
        "location_out_of_coverage", "location_expired", "location_expired_none", "purpose_ask",
        "purpose_inferred", "budget_ask", "budget_ask_rent", "budget_ask_sale",
        "budget_unclear", "size_unit_ask", "pref_unmapped"))


def test_the_stub_body_is_kept_without_a_service():
    context = ai_tools.ToolContext("customer", "Clifton")
    call = SimpleNamespace(name="update_requirements", call_id=None, args={"patch": [
        p("location", "set", "Clifton")]})
    result = ai_tools.execute(call, context)
    assert result.notice == "requirements_unavailable"
