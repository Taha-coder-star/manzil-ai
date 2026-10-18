"""Filters from an earlier session, a greeting alone, typed answers and fresh-search commands
through the real worker, turn engine, taps and outbound path (disposable PostgreSQL,
restricted manzil_app login, RLS on). Gemini is a fake transport; WhatsApp is the FakeClient.
Slots are aged by rewriting their stored times, the one way to leave a session behind
without waiting. All data is fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg

from app import webhook
from app.db import tenant_transaction
from app.ingest import store_event
from app.provision_agency import create_agency
from app.turn_engine import TEMPLATES
from app.worker import Worker
from tests.test_customer_flow_db import button_ids, tap, turn
from tests.test_customer_search_db import sent_text
from tests.test_requirements_db import p, st  # noqa: F401  (st: the 7.3 worker fixture)
from tests.test_search_db import CUSTOMER_A, CUSTOMER_B, conversation
from tests.test_turn_engine import text

SESSION_ONE = "mjhy DHA Phase 6 mein 500 gaz ka ghar kiraye par chahiye"
SESSION_ONE_PATCH = [p("location", "set", "DHA Phase 6"), p("property_types", "set", "ghar"),
                     p("size", "set", "500 gaz"), p("purpose", "set", "kiraye par")]


def update(patch):
    return ("update_requirements", {"patch": patch})


SEARCH = ("search_properties", {})


def slots_of(st, sender=CUSTOMER_B):
    return st.query("SELECT slots FROM requirement_state WHERE conversation_id = %s",
                    (conversation(st, sender),))[0][0]


def age_slots(st, sender=CUSTOMER_B, days=2):
    """Make every stored slot look like it was said `days` ago: a session ago."""
    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conv = conversation(st, sender)
    with tenant_transaction(st.conn, st.agency):
        st.conn.execute(
            "UPDATE requirement_state SET slots = (SELECT jsonb_object_agg(k, "
            "jsonb_set(v, '{updated_at}', to_jsonb(%s::text))) FROM jsonb_each(slots) "
            "AS e(k, v)) WHERE conversation_id = %s", (stamp, conv))


def searches(st):
    return st.query("SELECT count(*) FROM search_run")[0][0]


def keep_id(payloads, answer):
    return next(i for pl in payloads for i in button_ids(pl) if i.startswith(f"keep:{answer}:"))


def earlier_session(st, sender=CUSTOMER_B):
    """A rent request for a 500 gaz house in DHA Phase 6, a session ago."""
    turn(st, SESSION_ONE, update(SESSION_ONE_PATCH), sender=sender)
    age_slots(st, sender)


def asked_with_new_area(st, sender=CUSTOMER_B):
    earlier_session(st, sender)
    return turn(st, "mera area hy dha phase 6",
                update([p("location", "set", "dha phase 6")]), SEARCH, sender=sender)


def typed(st, body, sender=CUSTOMER_B):
    """A customer text that must not reach the model (no script entry is added)."""
    before = len(st.client.sent)
    st.deliver(text(body, sender=sender))
    st.drain()
    return [payload for _, payload in st.client.sent[before:]]


# --- the question ------------------------------------------------------------------------

def test_older_filters_are_asked_about_and_nothing_is_searched(st):
    payloads = asked_with_new_area(st)
    said = sent_text(payloads)
    assert "Theek hai, note kar liya: DHA Phase 6." in said     # the filters in use
    assert "Pichli search mein 500 gaz ghar, kiraye par tha" in said
    ids = {i for pl in payloads for i in button_ids(pl)}
    assert len(ids) == 2 and all(i.split(":")[0] == "keep" for i in ids)
    assert searches(st) == 0
    assert {"purpose", "property_types", "size"} <= set(slots_of(st))   # asked, not dropped


def test_a_new_budget_is_not_read_as_monthly_rent_while_the_question_is_open(st):
    asked_with_new_area(st)
    payloads = turn(st, "aur budget hy 2 crore", update([p("budget", "set", "2 crore")]), SEARCH)
    slots = slots_of(st)
    assert slots["budget"]["value"]["period"] is None
    assert slots["budget"]["value"]["max"] == 22_000_000        # +/-10 % around, owner 2026-10-12
    assert slots["location"]["raw"] == "dha phase 6"            # the new area survives
    said = sent_text(payloads)
    assert "mahana" not in said and "per month" not in said
    assert "Theek hai, note kar liya: DHA Phase 6, 2 crore ke aas paas." in said
    assert searches(st) == 0


def test_the_next_message_gets_a_short_reminder_not_the_same_question(st):
    first = asked_with_new_area(st)
    second = turn(st, "aur budget hy 2 crore", update([p("budget", "set", "2 crore")]), SEARCH)
    assert "Pichli search mein" in sent_text(first)
    assert "Pichli search mein" not in sent_text(second)
    assert TEMPLATES_REMINDER in sent_text(second)
    assert keep_id(first, "yes") == keep_id(second, "yes")      # the same question, same token


TEMPLATES_REMINDER = "Pichli search abhi baqi hai. Wohi rakhein ya nayi search?"


# --- the two answers, tapped -------------------------------------------------------------

def test_nayi_search_keeps_this_sessions_area_and_budget_and_drops_the_rest(st):
    asked_with_new_area(st)
    payloads = turn(st, "aur budget hy 2 crore", update([p("budget", "set", "2 crore")]), SEARCH)
    before = slots_of(st)
    replies = tap(st, keep_id(payloads, "no"), sender=CUSTOMER_B)
    slots = slots_of(st)
    assert {"property_types", "size", "category"}.isdisjoint(slots)
    assert slots["location"]["value"] == before["location"]["value"]
    assert slots["budget"]["value"]["max"] == 22_000_000
    assert "500 gaz" not in sent_text(replies)
    assert slots.get("purpose", {}).get("value") != "rent"
    assert slots.get("purpose", {}).get("source", "inferred") == "inferred"


def test_haan_wohi_keeps_the_older_filters_and_is_not_asked_again(st):
    question = asked_with_new_area(st)
    replies = tap(st, keep_id(question, "yes"), sender=CUSTOMER_B)
    slots = slots_of(st)
    assert slots["purpose"]["value"] == "rent" and "kept_at" in slots["purpose"]
    assert slots["size"]["value"]["value"] == 500
    assert searches(st) == 1                                    # now it searches
    assert not any(i.startswith("keep:") for pl in replies for i in button_ids(pl))
    again = turn(st, "aur budget hy 2 crore", update([p("budget", "set", "2 crore")]), SEARCH)
    assert not any(i.startswith("keep:") for pl in again for i in button_ids(pl))
    assert slots_of(st)["budget"]["value"]["period"] == "monthly"          # the kept purpose
    assert "mahana" in sent_text(again)                         # and the reply says so


def test_a_tap_on_an_answered_question_changes_nothing(st):
    question = asked_with_new_area(st)
    yes, no = keep_id(question, "yes"), keep_id(question, "no")
    tap(st, yes, sender=CUSTOMER_B)                             # answered
    before = slots_of(st)
    assert sent_text(tap(st, no, sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert sent_text(tap(st, yes, sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert slots_of(st) == before


def test_a_button_of_an_older_question_cannot_answer_a_newer_one(st):
    first = asked_with_new_area(st)
    old_yes = keep_id(first, "yes")
    # the customer says the size again: the set of inherited filters (and so the question) changes
    second = turn(st, "250 gaz", update([p("size", "set", "250 gaz")]), SEARCH)
    assert keep_id(second, "yes") != old_yes
    before = slots_of(st)
    assert sent_text(tap(st, old_yes, sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert slots_of(st) == before and searches(st) == 0
    tap(st, keep_id(second, "yes"), sender=CUSTOMER_B)          # the current one works
    assert searches(st) == 1


def test_a_tap_with_no_question_open_changes_nothing(st):
    turn(st, SESSION_ONE, update(SESSION_ONE_PATCH))             # nothing is inherited
    before = slots_of(st)
    assert sent_text(tap(st, "keep:yes:deadbeef", sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert sent_text(tap(st, "keep:no:deadbeef", sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert slots_of(st) == before and searches(st) == 0


# --- the two answers, typed --------------------------------------------------------------

def test_a_typed_haan_wohi_answers_the_open_question_without_the_model(st):
    asked_with_new_area(st)
    before = len(st.requests)
    typed(st, "haan wohi chahiye")
    assert slots_of(st)["purpose"]["value"] == "rent" and "kept_at" in slots_of(st)["purpose"]
    assert searches(st) == 1 and len(st.requests) == before


def test_a_typed_nahi_starts_a_fresh_search_without_the_model(st):
    asked_with_new_area(st)
    before = len(st.requests)
    typed(st, "nahi")
    assert {"property_types", "size", "purpose"}.isdisjoint(slots_of(st))
    assert slots_of(st)["location"]["raw"] == "dha phase 6" and len(st.requests) == before


def test_haan_with_no_question_open_is_an_ordinary_message(st):
    turn(st, SESSION_ONE, update(SESSION_ONE_PATCH))
    before = slots_of(st)
    turn(st, "haan")                                            # the model answers (no tool)
    assert slots_of(st) == before and len(st.requests) >= 2


def test_nahi_kharidna_hai_resolves_the_purpose_and_asks_about_the_rest(st):
    asked_with_new_area(st)
    turn(st, "aur budget hy 2 crore", update([p("budget", "set", "2 crore")]), SEARCH)
    payloads = turn(st, "nahi, kharidna hai", update([p("purpose", "set", "kharidna")]), SEARCH)
    slots = slots_of(st)
    assert slots["purpose"]["value"] == "sale" and "kept_at" not in slots["purpose"]
    assert slots["budget"]["value"]["period"] == "one_time"     # not moved aside, not monthly
    assert "previous_budget" not in slots
    said = sent_text(payloads)
    assert "Pichli search mein 500 gaz ghar tha" in said and "kiraye" not in said
    assert searches(st) == 0                                    # size and type still unresolved


# --- fresh-search commands ---------------------------------------------------------------

def test_a_standalone_purani_requirements_hata_do_clears_everything(st):
    turn(st, SESSION_ONE, update(SESSION_ONE_PATCH))
    before = len(st.requests)
    payloads = turn(st, "purani requirements hata do")
    assert sent_text(payloads) == TEMPLATES["fresh_all"]
    assert slots_of(st) == {} and len(st.requests) == before     # no model call


def test_purani_requirements_hata_do_with_a_request_keeps_this_sessions_values(st):
    earlier_session(st)
    turn(st, "mera area hy dha phase 6", update([p("location", "set", "dha phase 6")]))
    turn(st, "purani requirements hata do, flat chahiye",
         update([p("property_types", "set", "flat")]))
    slots = slots_of(st)
    assert slots["property_types"]["value"] == ["apartment"]
    assert slots["location"]["raw"] == "dha phase 6"            # said this session: kept
    assert {"size", "purpose"}.isdisjoint(slots)


def test_nayi_search_with_a_request_drops_older_filters_and_keeps_the_new_ones(st):
    earlier_session(st)
    payloads = turn(st, "nayi search: dha phase 6 mein flat",
                    update([p("location", "set", "dha phase 6"),
                            p("property_types", "set", "flat")]))
    slots = slots_of(st)
    assert slots["property_types"]["value"] == ["apartment"]
    assert {"size", "purpose"}.isdisjoint(slots)
    assert "500 gaz" not in sent_text(payloads)


def test_a_standalone_nayi_search_lists_the_filters_that_stay(st):
    earlier_session(st)
    turn(st, "mera area hy dha phase 6", update([p("location", "set", "dha phase 6")]))
    payloads = turn(st, "nayi search")
    assert sent_text(payloads) == TEMPLATES["fresh_kept"].format(summary="DHA Phase 6")
    assert set(slots_of(st)) == {"location"}


def test_typed_nayi_search_and_the_button_do_the_same(st):
    def values(sender):
        return {k: v["value"] for k, v in slots_of(st, sender).items()}

    question = asked_with_new_area(st, CUSTOMER_B)
    asked_with_new_area(st, CUSTOMER_A)
    tap(st, keep_id(question, "no"), sender=CUSTOMER_B)          # the button
    turn(st, "nayi search", sender=CUSTOMER_A)                    # the typed command
    assert {"property_types", "size", "category"}.isdisjoint(values(CUSTOMER_A))
    assert values(CUSTOMER_A) == values(CUSTOMER_B)
    assert values(CUSTOMER_A)["location"]["raw"] == "dha phase 6"


def test_nayi_search_with_nothing_inherited_clears_nothing(st):
    turn(st, SESSION_ONE, update(SESSION_ONE_PATCH))             # all said this session
    before = slots_of(st)
    payloads = turn(st, "nayi search")
    assert slots_of(st) == before                                # not "older than this message"
    assert TEMPLATES["fresh_kept"].split("{")[0] in sent_text(payloads)
    assert sent_text(tap(st, "keep:no:deadbeef", sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert slots_of(st) == before


def test_purani_requirements_hata_do_with_nothing_inherited_still_clears_everything(st):
    turn(st, SESSION_ONE, update(SESSION_ONE_PATCH))
    turn(st, "purani requirements hata do")
    assert slots_of(st) == {}


# --- greetings ---------------------------------------------------------------------------

def test_salam_alone_gets_a_greeting_without_the_model(st):
    before = len(st.requests)
    st.deliver(text("salam", sender=CUSTOMER_B))
    st.drain()
    assert st.sent_texts()[-1] == TEMPLATES["greeting_salam"]
    assert not st.sent_texts()[-1].startswith("Shukriya")
    assert len(st.requests) == before and st.script == []


def test_a_greeting_with_a_request_is_still_a_request(st):
    payloads = turn(st, "salam, dha phase 6 mein flat chahiye",
                    update([p("location", "set", "dha phase 6"),
                            p("property_types", "set", "flat")]))
    assert "Wa alaikum" not in sent_text(payloads)
    assert slots_of(st)["property_types"]["value"] == ["apartment"]


# --- the session boundary ----------------------------------------------------------------

def test_the_session_ignores_delivery_statuses_and_our_own_messages(st):
    turn(st, SESSION_ONE, update(SESSION_ONE_PATCH))
    conv = conversation(st, CUSTOMER_B)
    with tenant_transaction(st.conn, st.agency):
        row = st.conn.execute("SELECT created_at FROM message WHERE conversation_id = %s "
                              "AND direction = 'inbound' ORDER BY created_at LIMIT 1",
                              (conv,)).fetchone()
        outbound = st.conn.execute("SELECT count(*) FROM message WHERE conversation_id = %s "
                                   "AND direction = 'outbound'", (conv,)).fetchone()[0]
    assert outbound >= 1                                         # our replies are not sessions
    # a status webhook never becomes a message: the session start is the first inbound text
    start = _session_start(st, CUSTOMER_B)
    assert start == row[0]


def _session_start(st, sender):
    conv = conversation(st, sender)
    with tenant_transaction(st.conn, st.agency):
        turn_row = st.conn.execute("SELECT id FROM conversation_turn WHERE conversation_id = %s "
                                   "ORDER BY created_at DESC LIMIT 1", (conv,)).fetchone()
        message = st.conn.execute(
            "SELECT created_at FROM message WHERE turn_id = %s", (turn_row[0],)).fetchone()
        return st.service._session_start(
            st.conn, type("T", (), {"conversation_id": conv})(), message[0])


def test_a_turn_processed_late_uses_its_own_sessions_start(st):
    """A message of an old session processed after a newer session began is still said in
    the old one: its slots stay inherited-by-the-new-session, not fresh."""
    turn(st, SESSION_ONE, update(SESSION_ONE_PATCH))
    conv = conversation(st, CUSTOMER_B)
    with tenant_transaction(st.conn, st.agency):
        first = st.conn.execute("SELECT created_at FROM message WHERE conversation_id = %s "
                                "AND direction = 'inbound' ORDER BY created_at LIMIT 1",
                                (conv,)).fetchone()[0]
        # a later message 7 h after the first: a new session for turns that see it
        st.conn.execute(
            "INSERT INTO message (agency_id, conversation_id, direction, message_type, "
            "text_body, created_at) VALUES (current_agency_id(), %s, 'inbound', 'text', 'x', "
            "%s)", (conv, first + timedelta(hours=7)))
    turn_ = type("T", (), {"conversation_id": conv})()
    with tenant_transaction(st.conn, st.agency):
        as_of_first = st.service._session_start(st.conn, turn_, first)
        as_of_later = st.service._session_start(st.conn, turn_, first + timedelta(hours=8))
    assert as_of_first == first and as_of_later == first + timedelta(hours=7)


def test_unresolved_purpose_never_gets_a_period_through_any_budget_op(st):
    asked_with_new_area(st)
    for span in ("2 crore", "2 crore tak", "kam se kam 2 crore", "20000000"):
        turn(st, f"budget {span}", update([p("budget", "set", span)]), SEARCH)
        budget = slots_of(st)["budget"]["value"]
        assert budget["period"] is None, span
    assert slots_of(st)["purpose"]["value"] == "rent"          # still the old one, still asked


# --- what a fresh search searches, and the lowest price it quotes -----------------------------

def make_flat(st, price):
    """DH6-103 (a 95 mn house) becomes an active flat in the same area at this price."""
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE property SET property_type = 'apartment', price_pkr = %s "
                      "WHERE agency_id = %s AND listing_code = 'DH6-103'", (price, st.agency))


def last_filters(st):
    return st.query("SELECT filters FROM search_run ORDER BY created_at DESC LIMIT 1")[0][0]


def fresh_with_budget(st, budget="aur budget hy 2 crore", span="2 crore"):
    """The recorded failure: a rent house search a session ago, now an area and a budget;
    the customer taps [Nayi search]."""
    asked_with_new_area(st)
    question = turn(st, budget, update([p("budget", "set", span)]), SEARCH)
    return tap(st, keep_id(question, "no"), sender=CUSTOMER_B)


def test_a_fresh_search_covers_every_type_and_a_cheaper_flat_is_found(st):
    make_flat(st, 21_000_000)                                  # 2.1 crore: inside 2 crore +/-10 %
    said = sent_text(fresh_with_budget(st))
    assert "DH6-103" in said                                    # a flat: no house is restored
    assert last_filters(st)["types"] == []                      # across types, not asked
    assert "property_types" not in slots_of(st) and "size" not in slots_of(st)
    assert "ghar, flat" not in said and "qisam" not in said     # and no type question


def test_the_lowest_price_comes_from_active_inventory_of_every_allowed_type(st):
    make_flat(st, 35_000_000)                                   # 3.5 crore flat, above the budget
    said = sent_text(fresh_with_budget(st))
    assert "abhi available nahi" in said
    assert "Yahan price 3.5 crore se shuru hoti hai." in said
    assert "7.8 crore" not in said                              # not the old house scope


def test_the_lowest_price_keeps_a_stated_type_and_hides_unavailable_listings(st):
    make_flat(st, 35_000_000)
    fresh_with_budget(st)
    said = sent_text(turn(st, "ghar chahiye", update([p("property_types", "set", "ghar")]),
                          SEARCH))
    assert last_filters(st)["types"] == ["house"]
    # active houses start at 7.8 crore: the sold 7.2 crore and reserved 7.9 crore ones and
    # the 3.5 crore flat are not in this scope
    assert "Yahan price 7.8 crore se shuru hoti hai." in said


def test_the_lowest_price_is_omitted_when_nothing_is_available_in_scope(st):
    with psycopg.connect(st.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE property SET status = 'withdrawn' WHERE agency_id = %s AND "
                      "listing_code LIKE 'DH6-10%%' AND purpose = 'sale'", (st.agency,))
    said = sent_text(fresh_with_budget(st))
    assert "abhi available nahi" in said and "Yahan price" not in said


# --- buttons cannot cross conversations --------------------------------------------------

def other_tenant(st, number="100000000000002"):
    """A second agency with its own worker, and an st-like view of it for the helpers."""
    with psycopg.connect(st.app_url, autocommit=True) as conn:
        agency2 = create_agency(conn, "Other Realty (DEMO)", "other", number)
    stop = threading.Event()
    worker2 = Worker(st.app_url, [agency2], st.handlers, stop, backoff=lambda n: 0)

    def deliver(*messages):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": number}, "messages": list(messages)}}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(st.conn, agency2):
            for event in events:
                store_event(st.conn, agency2, event)

    def query(sql, params=(), *, agency_id=agency2):
        with tenant_transaction(st.conn, agency_id):
            return st.conn.execute(sql, params).fetchall()

    view = SimpleNamespace(**{**vars(st), "agency": agency2, "deliver": deliver,
                              "drain": lambda: [None for _ in iter(worker2.run_once, False)],
                              "query": query})
    return view, worker2


def test_another_customers_button_in_the_same_agency_changes_nothing(st):
    mine = asked_with_new_area(st, CUSTOMER_A)
    theirs = asked_with_new_area(st, CUSTOMER_B)
    # make the two chats' inherited filters identical down to the stored times
    stamp = datetime.now(UTC) - timedelta(days=3)
    for sender in (CUSTOMER_A, CUSTOMER_B):
        conv = conversation(st, sender)
        with tenant_transaction(st.conn, st.agency):
            st.conn.execute(
                "UPDATE requirement_state SET slots = (SELECT jsonb_object_agg(k, "
                "jsonb_set(v, '{updated_at}', to_jsonb(%s::text))) FROM jsonb_each(slots) "
                "AS e(k, v)) WHERE conversation_id = %s", (stamp.isoformat(), conv))
    a_before, b_before = slots_of(st, CUSTOMER_A), slots_of(st, CUSTOMER_B)
    assert sent_text(tap(st, keep_id(theirs, "yes"), sender=CUSTOMER_A)) == TEMPLATES[
        "echo_stale"]
    assert sent_text(tap(st, keep_id(mine, "no"), sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert slots_of(st, CUSTOMER_A) == a_before and slots_of(st, CUSTOMER_B) == b_before
    assert searches(st) == 0


def test_another_tenants_button_cannot_confirm_or_alter_this_conversation(st):
    mine = asked_with_new_area(st)
    st2, worker2 = other_tenant(st)
    try:
        theirs = asked_with_new_area(st2)
        a_before, b_before = slots_of(st), slots_of(st2)
        assert sent_text(tap(st, keep_id(theirs, "yes"), sender=CUSTOMER_B)) == TEMPLATES[
            "echo_stale"]
        assert sent_text(tap(st2, keep_id(mine, "no"), sender=CUSTOMER_B)) == TEMPLATES[
            "echo_stale"]
        assert slots_of(st) == a_before and slots_of(st2) == b_before
        assert searches(st) == 0 and st2.query("SELECT count(*) FROM search_run")[0][0] == 0
        tap(st2, keep_id(theirs, "yes"), sender=CUSTOMER_B)     # its own button works
        assert "kept_at" in slots_of(st2)["purpose"] and "kept_at" not in slots_of(st)["purpose"]
    finally:
        worker2.close()


def test_an_old_button_cannot_answer_after_a_full_reset_and_similar_filters(st):
    first = asked_with_new_area(st)
    old_yes, old_no = keep_id(first, "yes"), keep_id(first, "no")
    turn(st, "purani requirements hata do")                      # the full reset
    st.script.clear()                                            # (no model step was used)
    assert slots_of(st) == {}
    second = asked_with_new_area(st)                             # the same words, a new session
    assert keep_id(second, "yes") != old_yes
    before = slots_of(st)
    assert sent_text(tap(st, old_yes, sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert sent_text(tap(st, old_no, sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert slots_of(st) == before and searches(st) == 0
    tap(st, keep_id(second, "yes"), sender=CUSTOMER_B)
    assert searches(st) == 1


# --- negated commands, and a question left unanswered across a session gap --------------------

def test_negated_commands_change_nothing_and_reach_the_model(st):
    turn(st, SESSION_ONE, update(SESSION_ONE_PATCH))
    before = slots_of(st)
    versions = st.query("SELECT count(*) FROM requirement_patch_log")[0][0]
    for body in ("purani requirements mat hata do", "do not clear old requirements",
                 "nayi search nahi chahiye"):
        requests = len(st.requests)
        turn(st, body)                                           # the model answers; no tool
        assert len(st.requests) == requests + 1, body            # it was the model's to read
        assert slots_of(st) == before, body
    assert st.query("SELECT count(*) FROM requirement_patch_log")[0][0] == versions


def shift_history(st, hours, sender=CUSTOMER_B):
    """Make every stored message look `hours` older (a quiet gap before the next message)."""
    conv = conversation(st, sender)
    with tenant_transaction(st.conn, st.agency):
        for table in ("message", "outbound_message"):
            st.conn.execute(f"UPDATE {table} SET created_at = created_at - %s * interval "
                            "'1 hour' WHERE conversation_id = %s", (hours, conv))


def test_a_question_ignored_across_a_session_gap_is_asked_again_in_full(st):
    first = asked_with_new_area(st)
    old_yes, old_no = keep_id(first, "yes"), keep_id(first, "no")
    shift_history(st, 8)                                         # > 6 h of silence
    # the same words again: the same filters are inherited, unchanged, in a NEW session
    second = turn(st, "mera area hy dha phase 6", update([p("location", "set", "dha phase 6")]),
                  SEARCH)
    said = sent_text(second)
    assert "Pichli search mein 500 gaz ghar, kiraye par tha" in said     # full question
    assert TEMPLATES_REMINDER not in said                                 # not yesterday's reminder
    assert keep_id(second, "yes") != old_yes
    before = slots_of(st)
    assert sent_text(tap(st, old_yes, sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert sent_text(tap(st, old_no, sender=CUSTOMER_B)) == TEMPLATES["echo_stale"]
    assert slots_of(st) == before and searches(st) == 0
    # and a typed answer counts only for the question of the current session
    tap(st, keep_id(second, "yes"), sender=CUSTOMER_B)
    assert searches(st) == 1
