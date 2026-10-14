"""Task 8.2 slice 1: the spans-only staff tool contract and span verification.

Pure functions, no database. The model names tool choice and raw spans; it never
supplies values, IDs, counts or photo references (docs/STAFF_EXTRACTION.md sections 2, 7).
"""

import uuid
from datetime import UTC, datetime

import pytest

from app import ai_tools, staff_drafts, turn_engine
from app.ai_tools import ToolContext, execute
from app.llm import ToolCall
from app.orchestrator import Turn, TurnMessage

HOUSE = "New house DHA Phase 6, 500 yard, 4 bed 5 bath, demand 8 crore, available, owner direct"
PASTE = ("1. Phase 7 ghar 500 gaz 4 bed 7.5 crore sale\n"
         "2. Clifton block 5 shop 350 sq ft rent 2.5 lakh\n"
         "3. Gulshan block 13 flat 950 sq ft 85 lakh")
L1, L2, L3 = PASTE.split("\n")


def staff(*texts, descriptions=(), segment=True):
    return ToolContext("staff", "\n".join(t for t in texts if t),
                       staff=staff_drafts.context(texts, descriptions, segment))


def run(name, args, context):
    return execute(ToolCall(name, args, None), context)


def draft(**fields):
    return {k: v for k, v in fields.items()}


def drafts_call(context, *drafts):
    return run("draft_listing", {"drafts": list(drafts)}, context)


# --- draft_listing: valid spans ---------------------------------------------------------

def test_a_single_draft_verifies_each_span_against_the_segment_text():
    context = staff(HOUSE, descriptions={1})
    result = drafts_call(context, draft(
        purpose="owner direct", property_type="house", location="DHA  Phase 6",
        size="500 YARD", price="8 crore", bedrooms="4 bed", bathrooms="5 bath"))
    assert (result.ok, result.notice) == (True, "draft_unavailable")  # still a stub body
    response = result.response
    assert (response["spans_verified"], response["spans_rejected"]) == (7, 0)
    assert response["drafts"] == [{"ordinal": 1, "source_message": 1, "verified": 7,
                                   "rejected": []}]


def test_a_missing_span_is_dropped_counted_and_named():
    context = staff(HOUSE, descriptions={1})
    result = drafts_call(context, draft(price="80 lakh", location="DHA Phase 6",
                                        size="1 kanal"))
    assert (result.response["spans_verified"], result.response["spans_rejected"]) == (1, 2)
    assert result.response["drafts"][0]["rejected"] == ["size", "price"]


def test_a_single_draft_may_use_a_note_of_the_segment():
    context = staff(HOUSE, "", "owner ka number baad mein", descriptions={1})
    result = drafts_call(context, draft(location="DHA Phase 6", description="owner ka number"))
    assert result.response["drafts"][0]["rejected"] == []


def test_a_span_cannot_straddle_two_messages():
    context = staff("500 yard ghar", "8 crore", descriptions={1})
    result = drafts_call(context, draft(size="500 yard ghar 8 crore"))
    assert result.response["drafts"][0]["rejected"] == ["size"]


def test_with_a_text_the_spans_must_lie_inside_it():
    context = staff(HOUSE, "owner direct hai", descriptions={1})
    result = drafts_call(context, draft(text="500 yard, 4 bed 5 bath", size="500 yard",
                                        price="8 crore", bedrooms="4 bed"))
    assert result.response["drafts"][0]["rejected"] == ["price"]


# --- draft_listing: 1-5 drafts -----------------------------------------------------------

def test_several_drafts_each_verify_only_against_their_own_text():
    context = staff(PASTE, descriptions={1})
    result = drafts_call(
        context,
        draft(text=L1, price="7.5 crore", size="500 gaz", location="Phase 7"),
        draft(text=L2, price="2.5 lakh", property_type="shop"),
        draft(text=L3, price="7.5 crore", bedrooms="2 bed"))  # price belongs to draft 1
    assert result.ok
    assert [d["ordinal"] for d in result.response["drafts"]] == [1, 2, 3]
    assert [d["rejected"] for d in result.response["drafts"]] == [[], [], ["price", "bedrooms"]]
    assert {d["source_message"] for d in result.response["drafts"]} == {1}


@pytest.mark.parametrize("count", [1, 5])
def test_one_to_five_drafts_are_accepted(count):
    lines = [f"Phase {n} ghar 500 gaz {n} crore" for n in range(1, count + 1)]
    context = staff("\n".join(lines), descriptions={1})
    result = drafts_call(context, *[draft(text=line) for line in lines])
    assert result.ok and len(result.response["drafts"]) == count


@pytest.mark.parametrize(("count", "error"), [
    (0, "invalid_arguments"), (6, "too_many_drafts"), (10, "too_many_drafts"),
    (11, "invalid_arguments")])
def test_zero_or_more_than_five_drafts_are_refused(count, error):
    context = staff("x", descriptions={1})
    result = drafts_call(context, *[draft(text="x") for _ in range(count)])
    assert (result.ok, result.error) == (False, error)


def test_several_drafts_must_each_name_their_text():
    context = staff(PASTE, descriptions={1})
    result = drafts_call(context, draft(text=L1), draft(price="2.5 lakh"))
    assert (result.ok, result.error) == (False, "draft_text_missing")


def test_several_drafts_must_come_from_a_description_message():
    context = staff(PASTE, "Phase 8 ghar 500 gaz 6 crore", descriptions={1})
    result = drafts_call(context, draft(text=L1), draft(text="Phase 8 ghar 500 gaz 6 crore"))
    assert (result.ok, result.error) == (False, "draft_text_not_found")


def test_a_text_that_is_not_in_the_segment_is_refused():
    context = staff(PASTE, descriptions={1})
    result = drafts_call(context, draft(text=L1), draft(text="Defence 1000 gaz 20 crore"))
    assert (result.ok, result.error) == (False, "draft_text_not_found")


def test_the_same_listing_cannot_be_drafted_twice():
    context = staff(PASTE, descriptions={1})
    result = drafts_call(context, draft(text=L1), draft(text=L1))
    assert (result.ok, result.error) == (False, "draft_text_overlap")


def test_overlapping_texts_are_refused_but_a_shared_word_is_not():
    context = staff(PASTE, descriptions={1})
    wide = "Phase 7 ghar 500 gaz 4 bed 7.5 crore sale\n2. Clifton block 5 shop"
    assert drafts_call(context, draft(text=wide), draft(text=L2)).error == "draft_text_overlap"
    shared = staff("DHA Phase 6 ghar 500 gaz 8 crore. DHA Phase 7 ghar 500 gaz 7 crore",
                   descriptions={1})
    ok = drafts_call(shared, draft(text="DHA Phase 6 ghar 500 gaz 8 crore. DHA"),
                     draft(text="DHA Phase 7 ghar 500 gaz 7 crore"))
    assert ok.ok  # "DHA" is the only overlap (< 20 % of the shorter text)


# --- draft_listing: what the model may not send -------------------------------------------

@pytest.mark.parametrize("args", [
    {"raw_text": HOUSE},  # the 6.1 parameters are gone
    {"drafts": [{"price": "8 crore"}], "photo_refs": ["p1"]},
    {"drafts": [{"price": "8 crore"}], "agency_id": "x"},
    {"drafts": [{"price_pkr": 80000000}]},  # a normalised value
    {"drafts": [{"price": 80000000}]},
    {"drafts": [{"bedrooms": 4}]},  # a count
    {"drafts": [{"photos": ["p1"]}]},
    {"drafts": [{"property_id": str(uuid.uuid4())}]},
    {"drafts": [{"ordinal": 1, "price": "8 crore"}]},
    {"drafts": [{"price": ""}]},
    {"drafts": [{"price": "x" * 301}]},
    {"drafts": "8 crore"},
    {},
])
def test_model_supplied_values_ids_counts_and_photos_are_refused(args):
    result = run("draft_listing", args, staff(HOUSE, descriptions={1}))
    assert (result.ok, result.error) == (False, "invalid_arguments")


def test_draft_listing_needs_a_segment_turn():
    args = {"drafts": [{"price": "8 crore"}]}
    assert run("draft_listing", args, staff(HOUSE, descriptions={1}, segment=False)).error \
        == "no_segment"
    assert run("draft_listing", args, ToolContext("staff", HOUSE)).error == "no_segment"


# --- find_listings -----------------------------------------------------------------------

IMMEDIATE = staff("North Nazimabad wala portion rent ho gaya", segment=False)


def test_find_listings_verifies_each_criterion_span():
    result = run("find_listings", {"location": "North Nazimabad", "property_type": "portion",
                                   "size": "500 gaz"}, IMMEDIATE)
    assert result.ok and result.notice == "find_unavailable"
    assert (result.response["spans_verified"], result.response["spans_rejected"]) == (2, 1)
    assert result.response["rejected"] == ["size"]


@pytest.mark.parametrize("args", [
    {}, None, {"description": "North Nazimabad wala portion"},  # the 6.1 parameter
    {"location": 5}, {"price_pkr": 100}, {"location": "x", "property_id": "y"},
    {"location": ""}, {"location": "x" * 301},
])
def test_find_listings_refuses_unknown_empty_or_valued_arguments(args):
    result = ToolCall("find_listings", args, None)
    assert execute(result, IMMEDIATE).error == "invalid_arguments"


def test_find_listings_without_a_verified_span_does_nothing():
    result = run("find_listings", {"location": "Clifton"}, IMMEDIATE)
    assert (result.ok, result.error) == (False, "no_verified_span")


def test_find_listings_is_not_a_segment_tool():
    context = staff("North Nazimabad wala portion", descriptions={1}, segment=True)
    assert run("find_listings", {"location": "North Nazimabad"}, context).error \
        == "segment_turn"
    assert run("find_listings", {"location": "x"}, ToolContext("staff", "x")).error \
        == "no_staff_context"


# --- propose_update ----------------------------------------------------------------------

UPDATE = staff("#dh6-102 price 7.8 crore", segment=False)


def update(code="dh6-102", changes=None):
    changes = [{"field": "price", "span": "7.8 crore"}] if changes is None else changes
    return {"target": {"code": code}, "changes": changes}


def test_propose_update_with_an_explicit_code_and_a_verified_change():
    result = run("propose_update", update("#DH6-102"), UPDATE)
    assert result.ok and result.notice == "update_unavailable"
    assert result.response["target_in_text"] is True
    assert (result.response["changes_verified"], result.response["changes_rejected"]) == (1, 0)


def test_a_code_the_staff_did_not_write_is_flagged_for_later_resolution():
    # Slice 4b accepts it only as the single find_listings candidate of the same turn.
    result = run("propose_update", update("NNZ-101"), UPDATE)
    assert result.ok and result.response["target_in_text"] is False


@pytest.mark.parametrize("args", [
    update("DH6"),  # not a listing code (reaches the body)
    {"target": {"code": "DH6-102"}},  # no changes
    {"target": "DH6-102", "changes": [{"field": "price", "span": "7.8 crore"}]},
    update(changes=[{"field": "price", "span": "7.8 crore", "value": 78000000}]),
    update(changes=[{"field": "location", "span": "7.8 crore"}]),
    update(changes=[{"field": "price", "span": "7.8 crore"}] * 3),
    {"target": {"code": "DH6-102", "property_id": "x"}, "changes": []},
    {**update(), "version": 3},
    {"target": "DH6-101", "changes": "sold"},  # the 6.1 shape
])
def test_propose_update_refuses_bad_shapes_codes_and_extra_fields(args):
    result = run("propose_update", args, UPDATE)
    assert result.ok is False and result.error in ("invalid_arguments", "invalid_code")


def test_propose_update_needs_a_verified_change_and_distinct_fields():
    context = staff("DH6-102 sold ho gaya", segment=False)
    assert run("propose_update", update(changes=[{"field": "status", "span": "bik gaya"}]),
               context).error == "no_verified_span"
    twice = update(changes=[{"field": "price", "span": "sold"},
                            {"field": "price", "span": "ho gaya"}])
    assert run("propose_update", twice, context).error == "duplicate_field"
    both = update("DH6-102", [{"field": "status", "span": "sold ho gaya"},
                              {"field": "price", "span": "7 crore"}])
    result = run("propose_update", both, context)
    assert (result.response["changes_verified"], result.response["changes_rejected"]) == (1, 1)


def test_propose_update_is_not_a_segment_tool():
    context = staff("DH6-102 price 7.8 crore", descriptions={1}, segment=True)
    assert run("propose_update", update(), context).error == "segment_turn"


# --- routes ------------------------------------------------------------------------------

VALID = {
    "draft_listing": {"drafts": [{"price": "8 crore"}]},
    "find_listings": {"location": "DHA"},
    "propose_update": update(),
}


@pytest.mark.parametrize("route", ["customer", "broker"])
@pytest.mark.parametrize("name", list(VALID))
def test_customer_and_broker_cannot_run_the_staff_tools(route, name):
    context = ToolContext(route, HOUSE,
                          staff=staff_drafts.context([HOUSE], {1}, segment=True))
    result = run(name, VALID[name], context)
    assert (result.ok, result.error, result.notice) == (False, "tool_not_allowed", None)


def test_the_staff_tools_are_still_offered_to_staff_only_and_without_commit_change():
    assert [d["name"] for d in ai_tools.declarations("staff")] == [
        "draft_listing", "find_listings", "propose_update", "list_my_listings"]
    assert "commit_change" not in ai_tools.REGISTRY["staff"]


# --- how the engine numbers a segment's messages -----------------------------------------

def message(kind, text, number):
    return TurnMessage(
        id=uuid.uuid4(), wamid=f"w{number}", type=kind, text=text, media_id=None,
        provider_timestamp=datetime(2026, 10, 14, 10, 0, number, tzinfo=UTC))


def segment_turn(*messages, segment=True):
    return Turn(
        id=uuid.uuid4(), agency_id=uuid.uuid4(), conversation_id=uuid.uuid4(),
        sender_wa_id="x", sender_role="agent", route="staff", staff_id=uuid.uuid4(),
        control_state="ai_active", conversation_mode="ai", messages=tuple(messages),
        staff_segment_id=uuid.uuid4() if segment else None)


def test_the_staff_context_is_aligned_with_the_current_messages_numbering():
    turn = segment_turn(message("image", None, 1), message("text", HOUSE, 2),
                        message("image", "photos neeche hain", 3))
    context = turn_engine.staff_context(turn)
    block, _ = turn_engine.current_messages(turn)
    assert block.splitlines()[1] == "1. [photo]"
    assert block.splitlines()[2] == f"2. {HOUSE}"
    assert context.texts == ("", HOUSE, "photos neeche hain")
    assert context.descriptions == frozenset({2}) and context.segment is True
    # A photo-first segment still finds a description message and its note.
    result = drafts_call(ToolContext("staff", HOUSE, staff=context),
                         draft(size="500 yard", description="photos neeche"))
    assert result.response["drafts"][0]["source_message"] == 2
    assert result.response["drafts"][0]["rejected"] == []


def test_an_immediate_staff_turn_is_not_a_segment():
    turn = segment_turn(message("text", "DH6-102 sold ho gaya", 1), segment=False)
    assert turn_engine.staff_context(turn).segment is False
