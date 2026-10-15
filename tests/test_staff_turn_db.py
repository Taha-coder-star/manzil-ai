"""Task 8.2 slice 4c: the staff turn end to end (fake Gemini, disposable PostgreSQL): drafts
are stored once and answered with cards (Task 9.2: with Save / Edit / Cancel buttons), the
7.4 media warning comes after the cards, an overlong message is refused instead of clipped.
No live model, no WhatsApp send."""

# ruff: noqa: F811  (tests take the imported ai fixture as a parameter)

import pytest

from app import ai_tools, demo_dataset, location, staff_drafts, staff_tools, whatsapp
from app.db import tenant_transaction
from app.llm import ToolCall
from app.turn_engine import TEMPLATES, TurnEngine
from tests.test_inventory import active_property, insert, inv  # noqa: F401
from tests.test_staff_update_db import staff_turn
from tests.test_turn_engine import (
    AGENT,
    ai,  # noqa: F401
    answer,
    flush_staff_segments,
    media,
    text,
    tool,
    tools_answer,
)

GAZETTEER = location.Gazetteer.from_dataset(demo_dataset.load())
PASTE = ("Phase 6 ghar 500 gaz 4 bed 8 crore sale\n"
         "Clifton block 5 shop 350 sq ft 2.5 lakh rent")
LINES = PASTE.split("\n")
TWO = {"drafts": [
    {"text": LINES[0], "purpose": "sale", "property_type": "ghar", "location": "Phase 6",
     "size": "500 gaz", "price": "8 crore", "bedrooms": "4 bed"},
    {"text": LINES[1], "purpose": "rent", "property_type": "shop",
     "location": "Clifton block 5", "size": "350 sq ft", "price": "2.5 lakh"}]}


@pytest.fixture
def staffed(ai, monkeypatch):
    """The engine with the real staff tool bodies over the demo gazetteer (no worker)."""
    monkeypatch.setattr(
        TurnEngine, "_staff_binding",
        lambda self, conn, turn: staff_tools.StaffBinding(conn, turn, GAZETTEER))
    return ai


def segment_replies(ai, *messages):
    """The bodies of every message sent for the segment, cards included, in order."""
    before = len(ai.client.sent)
    ai.deliver(*messages)
    ai.drain()
    flush_staff_segments(ai)
    return [whatsapp.summary(payload) for _, payload in ai.client.sent[before:]]


def draft_rows(ai):
    return ai.query("SELECT ordinal, kind, status, payload FROM listing_draft "
                    "WHERE source_turn_id IS NOT NULL ORDER BY ordinal")


def test_a_pasted_segment_gets_one_text_card_per_draft_then_the_media_warning(staffed):
    ai = staffed
    ai.script += [tools_answer(tool("draft_listing", TWO, "d1")), answer()]
    sent = segment_replies(ai, text(PASTE, sender=AGENT), media("image", sender=AGENT))

    assert len(sent) == 3
    assert sent[0].startswith("Draft 1 of 2 · Proposed ID DH6-101")
    assert sent[1].startswith("Draft 2 of 2 · Proposed ID CLF-101")
    assert all("Photos ke liye is card ko reply karke photos bhejein." in s for s in sent[:2])
    assert sent[2] == TEMPLATES["staff_media_missing"]  # 7.4's warning stays after the cards
    rows = draft_rows(ai)
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, "create", "open"), (2, "create", "open")]
    assert all("buttons" not in r[3] and "actions" not in r[3] for r in rows)
    assert ai.query("SELECT payload->>'type' FROM outbound_message ORDER BY created_at") == [
        ("interactive",), ("interactive",), ("text",)]
    assert ai.query("SELECT count(*) FROM property") == [(0,)]  # nothing is saved
    # each card is recorded for reply-to-card (migration 017)
    assert ai.query("SELECT count(*) FROM staff_card c JOIN listing_draft d "
                    "ON d.id = c.listing_draft_id") == [(2,)]


def test_a_complete_draft_is_ready_and_an_incomplete_one_says_what_is_missing(staffed):
    ai = staffed
    full = {"drafts": [{"purpose": "sale", "property_type": "ghar", "location": "Phase 6",
                        "size": "500 gaz", "price": "8 crore", "bedrooms": "4 bed"}]}
    ai.script += [tools_answer(tool("draft_listing", full, "d1")), answer()]
    [done] = segment_replies(ai, text(LINES[0], sender=AGENT)) or [None]
    assert done.splitlines()[-1] == "Sab zaroori maloomat mil gayi. Save dabayein."

    ai.script += [tools_answer(tool("draft_listing", {"drafts": [
        {"purpose": "sale", "property_type": "ghar", "location": "Phase 7",
         "size": "500 gaz"}]}, "d2")), answer()]
    sent = segment_replies(ai, text("Phase 7 ghar 500 gaz sale", sender=AGENT))
    assert "⚠ Missing: Price, Bedrooms." in sent[-1]
    assert "Save ke liye maloomat poori karein (Edit)." in sent[-1]


def test_an_overlong_segment_message_is_refused_never_clipped(staffed):
    ai = staffed
    long = "Phase 6 ghar 500 gaz 8 crore " + "x" * 3600
    sent = segment_replies(ai, text(long, sender=AGENT))
    assert sent == [TEMPLATES["staff_draft_too_long"]]
    assert ai.requests == [] and draft_rows(ai) == []  # no model call, no draft


def test_without_the_gazetteer_the_staff_tools_stay_stubs(ai):
    ai.script += [tools_answer(tool("draft_listing", TWO, "d1")), answer()]
    sent = segment_replies(ai, text(PASTE, sender=AGENT))
    assert sent == [TEMPLATES["draft_unavailable"]] and draft_rows(ai) == []


# --- the draft_listing body itself: retries and refusals ----------------------------------

def context(conn, turn, message):
    return ai_tools.ToolContext(
        "staff", message, staff=staff_drafts.context([message], {1}, True),
        binding=staff_tools.StaffBinding(conn, turn, GAZETTEER))


def run(ctx, args):
    return ai_tools.execute(ToolCall("draft_listing", args, None), ctx)


def test_a_retried_draft_turn_returns_the_same_cards_whatever_the_spans(inv):
    turn = staff_turn(inv.conn, inv.a)
    first = run(context(inv.conn, turn, PASTE), TWO)
    changed = {"drafts": [dict(d, price="9 crore" if "8 crore" in d["text"] else "3 lakh")
                          for d in TWO["drafts"]]}
    retry = run(context(inv.conn, turn, PASTE), changed)
    assert (first.response["status"], retry.response["status"]) == ("drafted", "existing")
    assert retry.payloads == first.payloads
    assert first.trusted_codes == retry.trusted_codes and "CLF-101" in first.trusted_codes
    with tenant_transaction(inv.conn, inv.a.agency):
        assert inv.conn.execute("SELECT count(*) FROM listing_draft WHERE source_turn_id = %s",
                                (turn.id,)).fetchone() == (2,)


@pytest.mark.parametrize(("args", "error", "notice"), [
    ({"drafts": [{"text": f"Phase {n} ghar 500 gaz"} for n in range(1, 7)]},
     "too_many_drafts", "staff_draft_too_many"),
    ({"drafts": [{"description": "nice"}]}, "no_listing", "staff_draft_none"),
])
def test_unusable_drafts_are_refused_with_a_reply_and_store_nothing(inv, args, error, notice):
    turn = staff_turn(inv.conn, inv.a)
    text_ = "\n".join(d.get("text", "nice garden") for d in args["drafts"])
    result = run(context(inv.conn, turn, text_), args)
    assert (result.ok, result.error, result.notice) == (False, error, notice)
    with tenant_transaction(inv.conn, inv.a.agency):
        assert inv.conn.execute("SELECT count(*) FROM listing_draft WHERE source_turn_id = %s",
                                (turn.id,)).fetchone() == (0,)


def test_another_agencys_listings_never_make_a_duplicate_or_a_code(inv):
    # agency B owns CLF-777; agency A's draft in Clifton is proposed CLF-101, not CLF-778
    with tenant_transaction(inv.conn, inv.b.agency):
        insert(inv.conn, "property", **active_property(
            inv.b.agency, inv.geo.ph6, listing_code="CLF-777", price_pkr=250_000,
            purpose="rent", property_type="shop", area_unit="sq_ft", area_value=350,
            bedrooms=None))
    turn = staff_turn(inv.conn, inv.a)
    spans = dict(TWO["drafts"][1])
    spans.pop("text")
    result = run(context(inv.conn, turn, LINES[1]), {"drafts": [spans]})
    card = whatsapp.summary(result.payloads[0])
    assert result.ok and "Proposed ID CLF-101" in card
    assert "CLF-777" not in card and "Milti-julti" not in card
