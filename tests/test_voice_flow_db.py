"""Task 10.2: a customer voice note end to end through the real worker, orchestrator,
speech service (in-turn download + STT), turn engine, 7.3 requirements, 8.1 search, 9.1
renderer and outbound path (disposable PostgreSQL, restricted manzil_app login, RLS on).

Scribe and the Gemini audio fallback are fakes (app.speech's real chain runs on them);
the turn's model is the Gemini fake transport; WhatsApp is the FakeClient. All data and
speech are fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import logging
import threading
import time
from types import SimpleNamespace

import psycopg
import pytest

from app import requirements, turn_engine
from app.handlers import make_handlers
from app.llm import ProviderError, Transcription
from app.speech import TEMPLATES as SPEECH_TEMPLATES
from app.speech import SpeechService
from app.turn_engine import TEMPLATES
from app.worker import Worker
from tests.test_customer_flow_db import button_ids, latest_shown, tap
from tests.test_customer_search_db import sent_text
from tests.test_media import FakeStorage
from tests.test_requirements_db import p, st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_B, conversation
from tests.test_speech import MODEL, FakeGemini, FakeScribe, opus_note, scribe_body
from tests.test_turn_engine import answer, text, tool, tools_answer

# Demo Scope scenario 2, as spoken (Roman Urdu) and as Scribe wrote it live on 2026-10-12
# (Devanagari mixed with Latin; docs/SPEECH.md).
SPOKEN = ("Assalam o alaikum, mujhe DHA Phase 6 mein 500 gaz ka ghar chahiye, budget aath "
          "crore tak hai.")
SPOKEN_PATCH = [p("location", "set", "DHA Phase 6"), p("size", "set", "500 gaz"),
                p("property_types", "set", "ghar"), p("budget", "set", "budget aath crore tak")]
DEVANAGARI = "Assalam o alaikum, mujhe DHA phase 6 में 500 gaz का घर चाहिए, budget 8 crore तक है।"
DEVANAGARI_PATCH = [p("location", "set", "DHA phase 6"), p("size", "set", "500 gaz"),
                    p("property_types", "set", "घर"), p("budget", "set", "8 crore")]
URDU = "السلام علیکم، مجھے ڈی ایچ اے فیز 6 میں 500 گز کا گھر چاہیے، بجٹ آٹھ کروڑ تک ہے۔"
URDU_PATCH = [p("location", "set", "ڈی ایچ اے فیز 6"), p("size", "set", "500 گز"),
              p("property_types", "set", "گھر"), p("budget", "set", "آٹھ کروڑ تک")]
SCENARIO_2_IDS = {"DH6-101", "DH6-102", "DH7-101"}
_n = iter(range(1, 10_000))


@pytest.fixture
def vf(st):
    """st with the real SpeechService on fake providers, and voice notes the FakeClient can
    hand out (the in-turn download, Red-Team R-02)."""
    scribe, gemini, storage = FakeScribe(), FakeGemini(), FakeStorage()
    service = SpeechService(scribe, storage, fallback=gemini, fallback_model=MODEL)
    files = {}
    st.client.fetch_media = lambda media_id, max_bytes: (files[media_id], "audio/ogg")
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket="demo-bucket",
        s3_region="us-east-1", s3_endpoint_url=None, media_public_base_url=None,
        site_address="demo.example")
    handlers = make_handlers(settings, client_factory=lambda pnid: st.client, engine=st.engine,
                             speech_service=service, storage=storage)
    worker = Worker(st.app_url, [st.agency], handlers, threading.Event(), backoff=lambda n: 0)
    drain = st.drain
    st.drain = lambda: [None for _ in iter(worker.run_once, False)]

    def voice(seconds=8, sender=CUSTOMER_B):
        media_id = f"voice-{next(_n)}"
        files[media_id] = opus_note(seconds)
        return {"from": sender, "id": f"wamid.V{next(_n)}", "timestamp": str(int(time.time())),
                "type": "audio", "audio": {"id": media_id, "mime_type": "audio/ogg; codecs=opus",
                                           "voice": True}}

    def say(*messages, calls=()):
        """One customer turn of these messages; the model makes these tool calls."""
        before = len(st.client.sent)
        for n, (name, args) in enumerate(calls):
            st.script.append(tools_answer(tool(name, args, call_id=f"call-{n}")))
        if calls:
            st.script.append(answer())
        st.deliver(*messages)
        st.drain()
        return [payload for _, payload in st.client.sent[before:]]

    yield SimpleNamespace(st=st, scribe=scribe, gemini=gemini, voice=voice, say=say)
    st.drain = drain
    worker.close()


def search_calls(patch):
    return [("update_requirements", {"patch": patch}), ("search_properties", {})]


def slots(st):
    return st.state()[1]


def runs(st):
    return st.query("SELECT count(*) FROM search_run WHERE conversation_id = %s",
                    (conversation(st, CUSTOMER_B),))[0][0]


def echo_question(payloads):
    return next((pl for pl in payloads
                 if any(i.startswith("echo:yes:") for i in button_ids(pl))), None)


# --- scenario 2 -----------------------------------------------------------------------------

def test_scenario_2_by_voice_gives_the_three_listings_in_roman_urdu(vf):
    """M3 locally: transcript slots, the exact result set, a Roman Urdu reply, no echo."""
    st = vf.st
    vf.scribe.script.append(scribe_body(SPOKEN))
    payloads = vf.say(vf.voice(), calls=search_calls(SPOKEN_PATCH))

    [(transcript, meta)] = st.query("SELECT transcript, stt_meta FROM message "
                                    "WHERE message_type = 'audio'")
    assert transcript == SPOKEN and meta["status"] == "transcribed"
    assert len(vf.scribe.calls) == 1 and not vf.gemini.calls
    state = slots(st)
    assert state["location"]["value"]["nodes"] == [st.ids["dha-ph6"]]
    assert state["size"]["value"]["value"] == 500 and state["size"]["value"]["unit"] == "sq_yd"
    assert state["budget"]["value"]["max"] == 80_000_000
    assert state["budget"]["value"]["mode"] == "max"
    assert state["purpose"]["value"] == "sale" and state["purpose"]["source"] == "inferred"
    assert state["location"]["source"] == "voice" and not state["location"]["confirmed"]
    assert requirements.uncertain_slots(state) == []

    assert set(latest_shown(st)) == SCENARIO_2_IDS
    assert echo_question(payloads) is None
    said = sent_text(payloads)
    # The inferred purpose is said once, as the note before the cards (D5).
    assert said.count("Raqam se main ne samjha ke aap khareedna chahte hain") == 1
    assert "Ye khareedne ke options hain" not in said
    assert "Ji, DHA Phase 6 mein 500 gaz, 8 crore tak -- ye options hain:" in said
    assert "Phase 7 -- qareeb" in said
    assert runs(st) == 1


def test_scenario_2_in_urdu_script_gives_the_same_three_listings(vf):
    """Urdu script is read (R16: the normalisers cover both scripts): same slots, same ids."""
    st = vf.st
    vf.scribe.script.append(scribe_body(URDU, language="urd"))
    payloads = vf.say(vf.voice(), calls=search_calls(URDU_PATCH))
    state = slots(st)
    assert state["property_types"]["value"] == ["house"]
    assert state["location"]["value"]["nodes"] == [st.ids["dha-ph6"]]
    assert (state["budget"]["value"]["mode"], state["budget"]["value"]["max"]) == (
        "max", 80_000_000)
    assert state["purpose"]["value"] == "sale"
    assert set(latest_shown(st)) == SCENARIO_2_IDS and echo_question(payloads) is None
    assert "Ji, DHA Phase 6 mein 500 gaz ghar, 8 crore tak" not in sent_text(payloads)
    assert "Ji, DHA Phase 6 mein 500 gaz, 8 crore tak -- ye options hain:" in sent_text(payloads)


# --- budget operators in other scripts --------------------------------------------------------

def test_scribe_devanagari_asks_type_then_operator_and_searches_nothing(vf):
    """Scribe wrote "घर" and "8 crore तक": the type and the operator are unread. One
    question per turn (type first, 7.3 order); the operator question stays open and is asked
    next; nothing is searched until both are answered (never every type, never "around");
    script alone confirms nothing else."""
    st = vf.st
    vf.scribe.script.append(scribe_body(DEVANAGARI, language="hin"))
    payloads = vf.say(vf.voice(), calls=search_calls(DEVANAGARI_PATCH))
    said = sent_text(payloads)
    assert TEMPLATES["type_unclear"] in said
    assert TEMPLATES["budget_operator_ask"].format(amount="8 crore") not in said
    assert echo_question(payloads) is None and runs(st) == 0
    state = slots(st)
    assert not {"budget", "purpose", "property_types"} & set(state)
    assert state["location"]["source"] == "voice" and "echo" not in state["location"]
    [(ops,)] = st.query("SELECT ops FROM requirement_patch_log")
    assert {e["slot"]: e.get("detail") for e in ops if e["status"] == "ask"} == {
        "budget": "operator_unknown", "property_types": "unreadable"}

    # The customer answers the type in writing: the operator question comes next.
    typed = vf.say(text("ghar", sender=CUSTOMER_B),
                   calls=search_calls([p("property_types", "set", "ghar")]))
    assert slots(st)["property_types"]["value"] == ["house"]
    assert sent_text(typed).endswith(TEMPLATES["budget_operator_ask"].format(amount="8 crore"))
    assert runs(st) == 0
    vf.say(text("8 crore tak", sender=CUSTOMER_B),
           calls=search_calls([p("budget", "set", "8 crore tak")]))
    budget = slots(st)["budget"]
    assert budget["value"]["mode"] == "max" and budget["value"]["max"] == 80_000_000
    assert budget["source"] == "text" and runs(st) == 1


def test_an_amount_in_devanagari_is_asked_not_dropped(vf):
    st = vf.st
    spoken = "DHA Phase 6 mein 500 gaz ka ghar, budget 8 करोड़ तक"
    vf.scribe.script.append(scribe_body(spoken, language="hin"))
    payloads = vf.say(vf.voice(), calls=search_calls([
        p("location", "set", "DHA Phase 6"), p("budget", "set", "8 करोड़ तक")]))
    assert TEMPLATES["budget_unclear"] in sent_text(payloads)
    assert "budget" not in slots(st)
    [(ops,)] = st.query("SELECT ops FROM requirement_patch_log")
    assert [(e["status"], e.get("detail")) for e in ops if e["slot"] == "budget"] == [
        ("ask", "unreadable")]


# --- the echo before a search (R16 7.4) -----------------------------------------------------

def question_wamid(st):
    """The WAMID of the newest confirmation question sent (what a tap on it quotes)."""
    return st.query("SELECT provider_message_id FROM outbound_message WHERE payload::text "
                    "LIKE '%%echo:yes:%%' ORDER BY created_at DESC LIMIT 1")[0][0]


def haan(st, version, wamid=None):
    """[Haan] on a question message (by default the newest one), as WhatsApp sends it."""
    return tap(st, f"echo:yes:{version}", context=wamid or question_wamid(st))


def age(st, sql):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute(sql)


def weak_scenario_2(vf):
    """Scenario 2 heard through the fallback (Scribe failed): every heard slot is weak."""
    vf.scribe.script.append(ProviderError("http_401", failover=True))
    vf.gemini.script.append(Transcription(True, SPOKEN, "ur"))
    return vf.say(vf.voice(), calls=search_calls(SPOKEN_PATCH))


def test_a_weak_transcript_is_confirmed_before_any_search(vf):
    st = vf.st
    payloads = weak_scenario_2(vf)
    [meta] = [r[0] for r in st.query("SELECT stt_meta FROM message WHERE message_type = 'audio'")]
    assert meta["provider"] == "gemini"
    question = echo_question(payloads)
    assert question is not None and payloads == [question]   # one question, no other notes
    assert question["interactive"]["body"]["text"] == (
        "Aap ne kaha: DHA Phase 6, 500 gaz, 8 crore tak, khareedne ke liye -- sahi hai?")
    assert button_ids(question)[1] == "echo:edit"
    assert runs(st) == 0 and latest_shown(st) == []
    assert set(requirements.uncertain_slots(slots(st))) == {"location", "size", "budget"}

    version = st.state()[0]
    edit = tap(st, "echo:edit")
    assert sent_text(edit) == TEMPLATES["echo_edit"]
    assert st.state()[0] == version and runs(st) == 0       # [Badlein] changes nothing
    # ... and the search stays blocked: the next search asks the same confirmation again.
    asked = vf.say(text("dikhao", sender=CUSTOMER_B), calls=[("search_properties", {})])
    assert echo_question(asked) is not None and runs(st) == 0
    assert f"echo:yes:{version}" in button_ids(echo_question(asked))

    yes = haan(st, version)
    assert echo_question(yes) is None and runs(st) == 1
    assert set(latest_shown(st)) == SCENARIO_2_IDS
    state = slots(st)
    for slot in ("location", "size", "budget", "purpose"):
        assert state[slot]["confirmed"] is True and state[slot]["confirmed_by"] == "echo"
    [(ops,)] = st.query("SELECT ops FROM requirement_patch_log WHERE version = %s",
                        (version + 1,))
    assert {e["call"] for e in ops} == {"echo"} and {e["detail"] for e in ops} == {"echo"}

    # A second tap on the same Haan: stale, nothing confirmed or searched again.
    again = haan(st, version)
    assert sent_text(again) == TEMPLATES["echo_stale"] and runs(st) == 1
    assert st.state()[0] == version + 1


def test_a_haan_on_an_older_question_confirms_and_searches_nothing(vf):
    st = vf.st
    weak_scenario_2(vf)
    stale, first = st.state()[0], question_wamid(st)
    # The customer types a new budget instead of tapping; place and size stay weakly heard,
    # so that turn's search asks again with the current values.
    newer = vf.say(text("budget 7 crore tak", sender=CUSTOMER_B),
                   calls=search_calls([p("budget", "set", "budget 7 crore tak")]))
    assert slots(st)["budget"]["source"] == "text" and slots(st)["budget"]["confirmed"]
    current = st.state()[0]
    assert "7 crore tak" in echo_question(newer)["interactive"]["body"]["text"]
    assert f"echo:yes:{current}" in button_ids(echo_question(newer))

    old = haan(st, stale, first)
    assert sent_text(old) == TEMPLATES["echo_stale"]
    assert st.state()[0] == current and runs(st) == 0
    assert not slots(st)["location"]["confirmed"]


def test_a_haan_on_a_question_sent_over_24_hours_ago_confirms_nothing(vf):
    st = vf.st
    weak_scenario_2(vf)
    version = st.state()[0]
    age(st, "UPDATE outbound_message SET sent_at = sent_at - interval '25 hours'")
    late = haan(st, version)
    assert sent_text(late) == TEMPLATES["echo_stale"]
    assert st.state()[0] == version and runs(st) == 0


def test_a_new_question_on_day_old_uncertain_values_can_be_answered(vf):
    """Review fix 1: the 24 h run from when the question was sent, not from the state."""
    st = vf.st
    weak_scenario_2(vf)
    version = st.state()[0]
    age(st, "UPDATE requirement_state SET updated_at = updated_at - interval '2 days'")
    age(st, "UPDATE requirement_patch_log SET created_at = created_at - interval '2 days'")
    age(st, "UPDATE outbound_message SET sent_at = sent_at - interval '2 days', "
            "created_at = created_at - interval '2 days'")
    shown = vf.say(text("dikhao", sender=CUSTOMER_B), calls=[("search_properties", {})])
    assert f"echo:yes:{version}" in button_ids(echo_question(shown))
    yes = haan(st, version)                     # the new message, sent just now
    assert echo_question(yes) is None and runs(st) == 1
    assert slots(st)["budget"]["confirmed_by"] == "echo"


def test_an_old_button_stays_stale_after_a_new_question_or_a_refresh(vf):
    """Review fix 1: an expired question message is never revived, even when a new one with
    the same version is shown or the state's timestamp is refreshed."""
    st = vf.st
    weak_scenario_2(vf)
    version, old = st.state()[0], question_wamid(st)
    age(st, "UPDATE outbound_message SET sent_at = sent_at - interval '25 hours', "
            "created_at = created_at - interval '25 hours'")
    age(st, "UPDATE requirement_state SET updated_at = now()")
    assert sent_text(haan(st, version, old)) == TEMPLATES["echo_stale"]
    vf.say(text("dikhao", sender=CUSTOMER_B), calls=[("search_properties", {})])
    new = question_wamid(st)
    assert new != old
    assert sent_text(haan(st, version, old)) == TEMPLATES["echo_stale"]
    assert runs(st) == 0 and st.state()[0] == version
    assert echo_question(haan(st, version, new)) is None and runs(st) == 1


def test_a_haan_that_quotes_no_question_confirms_nothing(vf):
    st = vf.st
    weak_scenario_2(vf)
    version = st.state()[0]
    assert sent_text(tap(st, f"echo:yes:{version}")) == TEMPLATES["echo_stale"]
    assert sent_text(tap(st, f"echo:yes:{version}", context="wamid.NOT-OURS")) == \
        TEMPLATES["echo_stale"]
    assert runs(st) == 0 and st.state()[0] == version


def test_the_model_marking_a_heard_value_uncertain_asks_first(vf):
    st = vf.st
    vf.scribe.script.append(scribe_body(SPOKEN))
    patch = [*SPOKEN_PATCH[:3], dict(SPOKEN_PATCH[3], uncertain=True)]
    payloads = vf.say(vf.voice(), calls=search_calls(patch))
    assert echo_question(payloads) is not None and runs(st) == 0
    assert slots(st)["budget"]["echo"] == ["llm_uncertain"]


def test_uncertain_false_never_clears_a_weak_slot(vf):
    st = vf.st
    vf.scribe.script.append(ProviderError("http_401", failover=True))
    vf.gemini.script.append(Transcription(True, SPOKEN, "ur"))
    patch = [dict(op, uncertain=False) for op in SPOKEN_PATCH]
    payloads = vf.say(vf.voice(), calls=search_calls(patch))
    assert echo_question(payloads) is not None and runs(st) == 0
    assert slots(st)["budget"]["echo"] == ["voice_weak"]


def test_an_open_location_question_is_asked_before_the_echo(vf):
    """A clarification (a value missing or ambiguous) comes first; the confirmation of
    stored values follows once it is answered."""
    st = vf.st
    vf.scribe.script.append(ProviderError("http_401", failover=True))
    vf.gemini.script.append(Transcription(True, "Block 6 mein 500 gaz ka ghar, 8 crore tak", "ur"))
    payloads = vf.say(vf.voice(), calls=search_calls([
        p("location", "set", "Block 6"), p("size", "set", "500 gaz"),
        p("property_types", "set", "ghar"), p("budget", "set", "8 crore tak")]))
    assert echo_question(payloads) is None and runs(st) == 0
    assert any(i.startswith("clarify:location:") for pl in payloads for i in button_ids(pl))


def test_an_implausible_heard_budget_is_echoed_once(vf):
    # R17 edge 15: "80 crore" heard for a 500 gaz house: kept unconfirmed, echoed first;
    # the 7.3 implausible note is not sent beside the echo question. (The 7.3 guard needs
    # a purpose: an inferred one comes after the budget, so the purpose is stated here.)
    st = vf.st
    spoken = "DHA Phase 6 mein 500 gaz ka ghar khareedna hai, budget 80 crore"
    vf.scribe.script.append(scribe_body(spoken))
    payloads = vf.say(vf.voice(), calls=search_calls([
        p("purpose", "set", "khareedna"),
        p("location", "set", "DHA Phase 6"), p("size", "set", "500 gaz"),
        p("property_types", "set", "ghar"), p("budget", "set", "budget 80 crore")]))
    assert "implausible" in slots(st)["budget"]["echo"]
    question = echo_question(payloads)
    assert payloads == [question] and runs(st) == 0
    assert TEMPLATES["budget_implausible"] not in sent_text(payloads)


# --- ordering: voice before a later text ----------------------------------------------------

def test_a_typed_correction_after_a_voice_note_in_the_same_turn_wins(vf):
    """R-02: the earlier note is transcribed inside the turn before the model reads it, and
    the later typed amount is the stored budget (text, confirmed)."""
    st = vf.st
    vf.scribe.script.append(scribe_body(SPOKEN))
    vf.say(vf.voice(), text("nahi, budget 7 crore tak", sender=CUSTOMER_B),
           calls=search_calls([*SPOKEN_PATCH[:3], p("budget", "set", "budget 7 crore tak")]))
    budget = slots(st)["budget"]
    assert budget["value"]["max"] == 70_000_000
    assert budget["source"] == "text" and budget["confirmed"] is True
    assert slots(st)["location"]["source"] == "voice"
    assert len(vf.scribe.calls) == 1


def test_a_text_turn_after_a_voice_turn_changes_the_heard_budget(vf):
    st = vf.st
    vf.scribe.script.append(scribe_body(SPOKEN))
    vf.say(vf.voice(), calls=search_calls(SPOKEN_PATCH))
    vf.say(text("budget 7 crore tak", sender=CUSTOMER_B),
           calls=search_calls([p("budget", "set", "budget 7 crore tak")]))
    budget = slots(st)["budget"]
    assert budget["value"]["max"] == 70_000_000 and budget["source"] == "text"
    assert slots(st)["location"]["source"] == "voice"


# --- failed / silent STT ---------------------------------------------------------------------

def test_a_silent_note_gets_the_notice_and_no_model_search_or_patch(vf):
    st = vf.st
    vf.scribe.script.append(scribe_body(""))   # Scribe: no words -> no_speech
    payloads = vf.say(vf.voice())              # no model step is scripted: a call would fail
    assert sent_text(payloads) == SPEECH_TEMPLATES["voice_retry"]
    assert not vf.gemini.calls and runs(st) == 0
    assert st.query("SELECT count(*) FROM requirement_patch_log")[0][0] == 0


def test_both_providers_failing_asks_to_type_and_changes_nothing(vf):
    st = vf.st
    vf.scribe.script.append(ProviderError("timeout", failover=True))
    vf.gemini.script.append(ProviderError("timeout", failover=True))
    payloads = vf.say(vf.voice())
    assert sent_text(payloads) == SPEECH_TEMPLATES["voice_failed"]
    assert runs(st) == 0 and slots(st) == {}


# --- replay -------------------------------------------------------------------------------------

def test_a_retried_voice_turn_pays_once_and_sends_once(vf, monkeypatch):
    st = vf.st
    failures = [RuntimeError("crash before the reply plan is stored")]
    original = turn_engine.render_customer

    def crash_once(*args, **kwargs):
        if failures:
            raise failures.pop()
        return original(*args, **kwargs)

    monkeypatch.setattr(turn_engine, "render_customer", crash_once)
    vf.scribe.script.append(scribe_body(SPOKEN))
    for _ in range(2):   # the retry asks the model again; the tools must not act twice
        for n, (name, args) in enumerate(search_calls(SPOKEN_PATCH)):
            st.script.append(tools_answer(tool(name, args, call_id=f"call-{n}")))
        st.script.append(answer())
    st.deliver(vf.voice())
    st.drain()

    assert len(vf.scribe.calls) == 1                      # stt_meta reused by the retry
    assert st.query("SELECT count(*) FROM requirement_patch_log")[0][0] == 1
    assert set(latest_shown(st)) == SCENARIO_2_IDS
    keys = [r[0] for r in st.query("SELECT dedupe_key FROM outbound_message")]
    assert keys and len(keys) == len(set(keys))
    assert st.query("SELECT count(DISTINCT search_run_id) FROM shown_item")[0][0] == 1


def test_a_retried_haan_tap_confirms_once(vf):
    st = vf.st
    weak_scenario_2(vf)
    version, wamid = st.state()[0], question_wamid(st)
    haan(st, version, wamid)
    # The same tap processed again (provider execution is at least once): the patch row
    # of the tap message is found and nothing is written twice.
    [(turn_id,)] = st.query("SELECT id FROM conversation_turn ORDER BY created_at DESC LIMIT 1")
    [(message_id,)] = st.query("SELECT id FROM message WHERE direction = 'inbound' "
                               "AND message_type = 'interactive' ORDER BY created_at DESC LIMIT 1")
    again = SimpleNamespace(agency_id=st.agency, conversation_id=conversation(st, CUSTOMER_B),
                            id=turn_id, messages=(SimpleNamespace(id=message_id),))
    assert st.service.confirm_echo(st.conn, again, version, wamid) == "replayed"
    assert st.state()[0] == version + 1
    assert st.query("SELECT count(*) FROM requirement_patch_log WHERE ops -> 0 ->> 'call' = "
                    "'echo'")[0][0] == 1


# --- logs --------------------------------------------------------------------------------------

def test_voice_turn_logs_hold_no_transcript_or_phone(vf, caplog):
    caplog.set_level(logging.DEBUG)
    vf.scribe.script.append(scribe_body(DEVANAGARI, language="hin"))
    vf.say(vf.voice(), calls=search_calls(DEVANAGARI_PATCH))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("Assalam", "मुझे", "घर", "500 gaz", CUSTOMER_B):
        assert secret not in logged
