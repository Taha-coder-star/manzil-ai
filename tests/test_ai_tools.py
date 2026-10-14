"""Task 6.1: the strict tool registry (app.ai_tools). Pure functions, no database."""

import pytest

from app import ai_tools, staff_drafts
from app.ai_tools import ToolContext, execute
from app.llm import ToolCall

CUSTOMER_TOOLS = ["update_requirements", "search_properties", "send_property_photos",
                  "request_handoff"]
STAFF_TOOLS = ["draft_listing", "find_listings", "propose_update", "list_my_listings"]
CUSTOMER = ToolContext("customer", "DHA phase 6 mein 500 gaz ka ghar, budget  8 crore tak")
STAFF = ToolContext("staff", "DH6-101 sold ho gaya",
                    staff=staff_drafts.context(["DH6-101 sold ho gaya"], (), False))
SEGMENT = ToolContext("staff", "500 gaz ghar", staff=staff_drafts.context(["500 gaz ghar"], {1}, True))


def call(name, args=None):
    return ToolCall(name, {} if args is None else args, "id-1")


def test_each_route_is_offered_only_its_own_tools():
    assert [d["name"] for d in ai_tools.declarations("customer")] == CUSTOMER_TOOLS
    assert [d["name"] for d in ai_tools.declarations("staff")] == STAFF_TOOLS
    assert ai_tools.declarations("broker") == []


def test_commit_change_is_never_offered():
    for route in ai_tools.REGISTRY:
        assert "commit_change" not in {d["name"] for d in ai_tools.declarations(route)}


@pytest.mark.parametrize(("context", "name"), [
    *[(CUSTOMER, name) for name in STAFF_TOOLS],
    *[(STAFF, name) for name in CUSTOMER_TOOLS],
    (CUSTOMER, "commit_change"), (STAFF, "commit_change"),
    (ToolContext("broker", "x"), "search_properties"),
    (ToolContext("broker", "x"), "find_listings"),
])
def test_tools_outside_the_route_are_refused_at_execution(context, name):
    result = execute(call(name), context)
    assert (result.ok, result.error, result.notice) == (False, "tool_not_allowed", None)
    assert result.response == {"status": "error", "error": "tool_not_allowed"}


@pytest.mark.parametrize("name", ["run_sql", "", "Search_Properties", None, 7, {"x": 1},
                                  ["search_properties"]])
def test_unknown_tool_names_are_refused_and_never_echoed_to_logs(name):
    result = execute(call(name), CUSTOMER)
    assert (result.ok, result.error, result.name) == (False, "unknown_tool", "unknown")


@pytest.mark.parametrize(("context", "name", "args"), [
    (CUSTOMER, "search_properties", {"location": "DHA"}),  # no-argument tool
    (CUSTOMER, "search_properties", []),
    (CUSTOMER, "update_requirements", {}),  # missing required
    (CUSTOMER, "update_requirements", {"patch": []}),  # minItems
    (CUSTOMER, "update_requirements", {"patch": "budget 8 crore"}),
    (CUSTOMER, "update_requirements", {"patch": [{"slot": "price", "op": "set",
                                                  "raw_span": "8 crore"}]}),  # enum
    (CUSTOMER, "update_requirements", {"patch": [{"slot": "budget", "op": "set"}]}),
    (CUSTOMER, "update_requirements", {"patch": [{"slot": "budget", "op": "set",
                                                  "raw_span": "8 crore", "value": 8}]}),
    (CUSTOMER, "update_requirements", {"patch": [{"slot": "budget", "op": "set",
                                                  "raw_span": "x" * 201}]}),
    (CUSTOMER, "send_property_photos", {"position": 0}),
    (CUSTOMER, "send_property_photos", {"position": True}),
    (CUSTOMER, "send_property_photos", {"position": "2"}),
    (CUSTOMER, "request_handoff", {"reason": "x", "phone": "x"}),
    (STAFF, "propose_update", {"target": "DH6-101"}),
    (STAFF, "draft_listing", {"raw_text": "x", "photo_refs": ["p"] * 16}),
    (STAFF, "draft_listing", {"drafts": [{"price_pkr": 80000000}]}),
    (STAFF, "find_listings", None),
    (STAFF, "find_listings", {"description": "phase 6 ghar"}),
])
def test_bad_arguments_are_refused(context, name, args):
    result = execute(ToolCall(name, args, None), context)
    assert (result.ok, result.error) == (False, "invalid_arguments")


@pytest.mark.parametrize(("context", "name", "args", "notice"), [
    (CUSTOMER, "search_properties", None, "search_unavailable"),
    (CUSTOMER, "send_property_photos", {"position": 2}, "photos_unavailable"),
    (CUSTOMER, "request_handoff", {}, "handoff_unavailable"),
    (SEGMENT, "draft_listing", {"drafts": [{"size": "500 gaz"}]}, "draft_unavailable"),
    (STAFF, "find_listings", {"location": "DH6"}, "find_unavailable"),
    (STAFF, "propose_update", {"target": {"code": "DH6-101"},
                               "changes": [{"field": "status", "span": "sold"}]},
     "update_unavailable"),
    (STAFF, "list_my_listings", {}, "my_listings_unavailable"),
])
def test_stubs_admit_that_nothing_happened(context, name, args, notice):
    result = execute(ToolCall(name, args, None), context)
    assert (result.ok, result.name, result.notice) == (True, name, notice)
    assert result.response["status"] == "not_available"
    assert "nothing was searched, saved, sent or forwarded" in result.response["detail"]


def test_requirement_spans_must_come_from_the_turn():
    result = execute(call("update_requirements", {"patch": [
        {"slot": "budget", "op": "set", "raw_span": "8  CRORE tak"},  # spacing / case
        {"slot": "location", "op": "set", "raw_span": "phase 6"},
        {"slot": "budget", "op": "set", "raw_span": "80 lakh"},  # not in the turn (R-22)
    ]}), CUSTOMER)
    assert result.ok and result.notice == "requirements_unavailable"
    assert (result.response["spans_verified"], result.response["spans_rejected"]) == (2, 1)


def test_span_check():
    assert ai_tools.span_in_turn("Phase 6", "dha phase  6 mein")
    assert not ai_tools.span_in_turn("   ", "anything")
    assert not ai_tools.span_in_turn("phase 7", "dha phase 6")
