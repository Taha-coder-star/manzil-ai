"""Strict tool registry for the AI turn engine (Task 6.1, component C6; RFC 3 and 5).

The role comes from the turn's allowlist routing (app.orchestrator), never from
message content or from the model. Each route sees only its own declarations, and
execute() re-checks the route and validates the arguments before any tool body
runs, so a tool the model was not offered (or invented) never executes:

- customer: update_requirements, search_properties, send_property_photos,
  request_handoff
- staff (admin / agent): draft_listing, find_listings, propose_update, list_my_listings
- broker: none (the engine makes no provider call for brokers)

commit_change is never an LLM tool: a confirmation button triggers it (RFC 5,
Task 9.2). It is not declared for any route and is refused if a model names it.

Task 6.1 tool bodies are stubs: they search, save, send and forward nothing, and
say so both to the model and in the customer-visible notice. The real bodies are
Tasks 7.x (requirements / state), 8.x (search), 9.x (staff) and 10.x / 11.x.
Never log tool arguments.
"""

import re
from dataclasses import dataclass

# R17 requirement slots and patch ops (migration 006 requirement_patch_log CHECK).
SLOTS = (
    "purpose", "category", "property_types", "location", "budget", "previous_budget",
    "size", "beds_min", "baths_min", "construction", "possession", "investment",
    "soft_prefs", "unmapped_prefs", "excluded_property_ids",
)
OPS = ("set", "add", "remove", "clear", "relative")

_TOOLS = {
    # --- customer (L1) ---
    "update_requirements": {
        "description": (
            "Record changes to the customer's property requirements. Each change names "
            "an R17 slot, an operation and raw_span: the exact words copied from the "
            "customer's current messages that state it. Never paraphrase or convert "
            "values; never invent a span."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "array", "minItems": 1, "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "slot": {"type": "string", "enum": list(SLOTS)},
                            "op": {"type": "string", "enum": list(OPS)},
                            "raw_span": {"type": "string", "minLength": 1, "maxLength": 200},
                        },
                        "required": ["slot", "op", "raw_span"],
                    },
                },
            },
            "required": ["patch"],
        },
    },
    "search_properties": {
        "description": "Search listings for the customer's current requirements.",
        "parameters": None,
    },
    "send_property_photos": {
        "description": (
            "Send more photos of a listing the customer was shown, referred to by its "
            "position in the last results (1-10) or by the customer's own description."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "position": {"type": "integer", "minimum": 1, "maximum": 10},
                "description": {"type": "string", "minLength": 1, "maxLength": 200},
            },
        },
    },
    "request_handoff": {
        "description": "Ask for a human agent (visit, call, negotiation, or on request).",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": 300}},
        },
    },
    # --- staff (L2) ---
    "draft_listing": {
        "description": "Start a listing draft from a staff member's description.",
        "parameters": {
            "type": "object",
            "properties": {
                "raw_text": {"type": "string", "minLength": 1, "maxLength": 2000},
                "photo_refs": {
                    "type": "array", "maxItems": 15,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
            },
            "required": ["raw_text"],
        },
    },
    "find_listings": {
        "description": "Find agency listings matching a staff member's description.",
        "parameters": {
            "type": "object",
            "properties": {"description": {"type": "string", "minLength": 1, "maxLength": 500}},
            "required": ["description"],
        },
    },
    "propose_update": {
        "description": (
            "Propose a change to one listing (status, price, ...). Nothing is saved "
            "until the staff member confirms with a button."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "minLength": 1, "maxLength": 200},
                "changes": {"type": "string", "minLength": 1, "maxLength": 1000},
            },
            "required": ["target", "changes"],
        },
    },
    "list_my_listings": {
        "description": "List the staff member's own listings.",
        "parameters": None,
    },
}

REGISTRY = {
    "customer": ("update_requirements", "search_properties", "send_property_photos",
                 "request_handoff"),
    "staff": ("draft_listing", "find_listings", "propose_update", "list_my_listings"),
    "broker": (),
}
# Triggered by confirmation buttons only; never declared, never executed from a model.
NEVER_LLM_TOOLS = frozenset({"commit_change"})

STUB_RESPONSE = {
    "status": "not_available",
    "detail": "Stub in this build: nothing was searched, saved, sent or forwarded.",
}


@dataclass(frozen=True)
class ToolContext:
    route: str
    turn_text: str  # the current turn's text exactly as the model saw it
    # Task 7.3: the turn's app.requirements.TurnBinding (update(patch) -> ToolResult).
    # None keeps the 6.1 stub body (no state is read or written).
    requirements: object = None


@dataclass(frozen=True)
class Notice:
    """A customer-visible notice with parameters: TEMPLATES[key] formatted with values
    that come from the backend only (canonical place names), never from the model."""
    key: str
    params: tuple = ()  # ((name, value), ...), hashable


@dataclass(frozen=True)
class ToolResult:
    name: str  # the tool name when it is a registered tool, else "unknown"
    ok: bool
    response: dict  # sent back to the model as the functionResponse
    error: str | None = None
    # Template key (str), Notice, or a tuple of those for the customer-visible reply.
    notice: object = None


def declarations(route):
    """The route's tool declarations, in registry order (a stable cached prefix)."""
    return [
        {"name": name, "description": _TOOLS[name]["description"],
         "parameters": _TOOLS[name]["parameters"]}
        for name in REGISTRY[route]
    ]


def safe_name(name):
    return name if isinstance(name, str) and name in _TOOLS else "unknown"


def _error(name, error):
    return ToolResult(safe_name(name), False, {"status": "error", "error": error}, error=error)


def _valid(value, schema, depth=0):
    if depth > 4:
        return False
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if any(key not in properties for key in value):
            return False
        if any(key not in value for key in schema.get("required", ())):
            return False
        return all(_valid(value[key], properties[key], depth + 1) for key in value)
    if kind == "array":
        return (isinstance(value, list)
                and schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 50)
                and all(_valid(item, schema["items"], depth + 1) for item in value))
    if kind == "string":
        return (isinstance(value, str)
                and schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1000)
                and ("enum" not in schema or value in schema["enum"]))
    if kind == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)
                and schema.get("minimum", value) <= value <= schema.get("maximum", value))
    return False


def validate_args(name, args):
    parameters = _TOOLS[name]["parameters"]
    if parameters is None:
        return args in (None, {})
    return _valid({} if args is None else args, parameters)


def _squash(text):
    return re.sub(r"\s+", " ", text).strip().casefold()


def span_in_turn(span, turn_text):
    """R-22: a raw span must be a (whitespace- and case-insensitive) substring of the
    current turn's text."""
    span = _squash(span)
    return bool(span) and span in _squash(turn_text)


def execute(call, context):
    """Run one model tool call for this route, or refuse it. Never raises for model
    input: unknown tools, other routes' tools and bad arguments become error results."""
    name = call.name
    if not isinstance(name, str) or (name not in _TOOLS and name not in NEVER_LLM_TOOLS):
        return _error(name, "unknown_tool")
    if name in NEVER_LLM_TOOLS or name not in REGISTRY.get(context.route, ()):
        return _error(name, "tool_not_allowed")
    if not validate_args(name, call.args):
        return _error(name, "invalid_arguments")
    return _STUBS[name](call.args or {}, context)


def _update_requirements(args, context):
    if context.requirements is not None:
        return context.requirements.update(args["patch"])
    accepted = sum(span_in_turn(op["raw_span"], context.turn_text) for op in args["patch"])
    response = dict(STUB_RESPONSE, spans_verified=accepted,
                    spans_rejected=len(args["patch"]) - accepted)
    return ToolResult("update_requirements", True, response, notice="requirements_unavailable")


def _make_stub(name, notice):
    def run(args, context):
        return ToolResult(name, True, dict(STUB_RESPONSE), notice=notice)
    return run


_STUBS = {
    "update_requirements": _update_requirements,
    "search_properties": _make_stub("search_properties", "search_unavailable"),
    "send_property_photos": _make_stub("send_property_photos", "photos_unavailable"),
    "request_handoff": _make_stub("request_handoff", "handoff_unavailable"),
    "draft_listing": _make_stub("draft_listing", "draft_unavailable"),
    "find_listings": _make_stub("find_listings", "find_unavailable"),
    "propose_update": _make_stub("propose_update", "update_unavailable"),
    "list_my_listings": _make_stub("list_my_listings", "my_listings_unavailable"),
}
