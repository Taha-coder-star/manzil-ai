"""Strict tool registry for the AI turn engine (Task 6.1, component C6; RFC 3 and 5).

The role comes from the turn's allowlist routing (app.orchestrator), never from
message content or from the model. Each route sees only its own declarations, and
execute() re-checks the route and validates the arguments before any tool body
runs, so a tool the model was not offered (or invented) never executes:

- customer: update_requirements, search_properties, send_property_photos,
  get_property (Task 10.1), request_handoff (a stub until 11.1: it says no agent was
  told)
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

from app import staff_drafts

# R17 requirement slots and patch ops (migration 006 requirement_patch_log CHECK).
SLOTS = (
    "purpose", "category", "property_types", "location", "budget", "previous_budget",
    "size", "beds_min", "baths_min", "construction", "possession", "investment",
    "soft_prefs", "unmapped_prefs", "excluded_property_ids",
)
OPS = ("set", "add", "remove", "clear", "relative")

_SPAN = {"type": "string", "minLength": 1, "maxLength": 300}

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
    "get_property": {
        "description": (
            "Answer the customer's question about one listing they were shown (a feature, "
            "parking, floor, possession, documents, ...), referred to by its position in "
            "the last results (1-10) or by the customer's own description."
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
        "description": (
            "Start 1-5 listing drafts from the staff member's buffered description. One "
            "draft per listing; for several listings in one message give each draft's own "
            "text, the exact part of the message about that listing. Every other field is "
            "raw_span style: the exact words copied from the staff messages. Never "
            "convert, count, compute or invent a value, ID or photo; leave out whatever "
            "was not stated."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "drafts": {
                    "type": "array", "minItems": 1, "maxItems": 10,  # > 5: too_many_drafts
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                            **{name: _SPAN for name in staff_drafts.DRAFT_FIELDS},
                        },
                    },
                },
            },
            "required": ["drafts"],
        },
    },
    "find_listings": {
        "description": (
            "Find agency listings a staff message refers to by description. Give the "
            "exact words of each criterion the staff member stated (location, type, "
            "size, price, ...); at least one. A listing code goes in code."
        ),
        "parameters": {
            "type": "object", "minProperties": 1,
            "properties": {name: _SPAN for name in staff_drafts.FIND_FIELDS},
        },
    },
    "propose_update": {
        "description": (
            "Propose a change to ONE listing: target.code is its listing code (as the "
            "staff member wrote it, or the only candidate find_listings returned); each "
            "change names the field (price or status) and span, the exact words stating "
            "it. Nothing is saved until the staff member confirms with a button."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "object",
                    "properties": {"code": {"type": "string", "minLength": 1,
                                            "maxLength": 12}},
                    "required": ["code"],
                },
                "changes": {
                    "type": "array", "minItems": 1, "maxItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "enum": ["price", "status"]},
                            "span": {"type": "string", "minLength": 1, "maxLength": 200},
                        },
                        "required": ["field", "span"],
                    },
                },
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
                 "get_property", "request_handoff"),
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
    # Task 8.2: app.staff_drafts.StaffContext of a staff turn (the messages spans are
    # verified against); None on other routes.
    staff: object = None
    # Task 8.2: app.staff_tools.StaffBinding (database, turn, gazetteer) or None (the stub).
    binding: object = None


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
    # Task 8.2: backend-built WhatsApp payloads (staff cards) sent before the notices, and
    # the listing codes they (or the notices) may show.
    payloads: tuple = ()
    trusted_codes: frozenset = frozenset()


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
        if len(value) < schema.get("minProperties", 0):
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


def _staff_stub(name, notice, verify):
    """A staff tool body of slice 1: spans are verified, nothing is stored or sent."""
    def run(args, context):
        body = getattr(context.binding, name, None)
        if body is not None:
            return body(args, context)
        result = verify(args, context.staff)
        if "error" in result:
            return _error(name, result["error"])
        result = {k: v for k, v in result.items() if k not in ("spans", "verified")}
        return ToolResult(name, True, dict(STUB_RESPONSE, **result), notice=notice)
    return run


def _make_stub(name, notice):
    def run(args, context):
        return ToolResult(name, True, dict(STUB_RESPONSE), notice=notice)
    return run


def _customer_tool(name, notice):
    """A customer tool body of app.customer_tools.CustomerBinding (Task 10.1), else the stub."""
    stub = _make_stub(name, notice)

    def run(args, context):
        body = getattr(context.binding, name, None) if context.route == "customer" else None
        return body(args, context) if body is not None else stub(args, context)
    return run


_STUBS = {
    "update_requirements": _update_requirements,
    "search_properties": _customer_tool("search_properties", "search_unavailable"),
    "send_property_photos": _customer_tool("send_property_photos", "photos_unavailable"),
    "get_property": _customer_tool("get_property", "details_unavailable"),
    "request_handoff": _make_stub("request_handoff", "handoff_unavailable"),
    "draft_listing": _staff_stub("draft_listing", "draft_unavailable",
                                 staff_drafts.verify_draft_listing),
    "find_listings": _staff_stub("find_listings", "find_unavailable",
                                 staff_drafts.verify_find_listings),
    "propose_update": _staff_stub("propose_update", "update_unavailable",
                                  staff_drafts.verify_propose_update),
    "list_my_listings": _make_stub("list_my_listings", "my_listings_unavailable"),
}
