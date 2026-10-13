"""Requirement-state manager (Task 7.3, component C8; R17 sections 2.1, 3, 5, Red-Team
R-22 / R-23, docs/LOCATION_RESOLVER.md "Boundary with 7.3").

The LLM passes RequirementPatch items {slot, op, raw_span} through the update_requirements
tool. Everything else is deterministic code over the stored requirement_state:

1. Every raw span must be a (whitespace- and case-insensitive) substring of ONE message of
   the current turn: the typed text or the accepted voice transcript (Red-Team R-22). A
   span that is not is dropped. Only budget, size and purpose are then re-extracted by
   running the normalisers over the message (explicit amount / unit / purpose wording only);
   a place is never guessed. Checking message by message means a span cannot straddle two
   messages, and a rejected transcript or an earlier message never counts.
2. A location span is resolved by app.location against the read-only gazetteer snapshot.
   The active area comes from the STORED location slot (the area above its nodes, only when
   exactly one area is involved), never from an area_hint. Only RESOLVED results change the
   slot. UNKNOWN (and its area_hint) and OUT_OF_COVERAGE change nothing; AMBIGUOUS opens
   (or replaces) the one pending_clarification and blocks nothing else.
3. A clarification answer is matched against the candidates stored with the question, never
   resolved on its own: an answer names exactly one candidate (the candidate itself, or an
   area / node above it). Two unanswered customer turns, or 24 hours, expire the question;
   the location then falls back to the one area the candidates share (SUBTREE), else it is
   left as it was.
4. set / add / remove / clear / relative semantics, relative money from the last search,
   the buy <-> rent budget move and amount -> purpose inference follow R17 2.1 / 4.4 / 4.5
   with app.normalise doing every number. Nothing is invented: a value the normaliser cannot
   read is not stored.

Every update_requirements call is one patch: one requirement_state version and one
requirement_patch_log row holding every op with its outcome (also rejections, for the
alias review of R-23 c and the span audit of R-22). Replays are safe without a migration:
the row carries the turn and call ids and is found again by source message + call id, so a
retried turn (provider execution is at-least-once, docs/AI_TURN_ENGINE.md) never applies a
patch twice; the unanswered-turn count is derived from completed turns, not incremented.

Never log message text, spans, keys, transcripts or ids of places: only counts and codes.
"""

import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

from app import ai_tools, location, normalise
from app.config import ConfigurationError
from app.db import tenant_transaction
from app.gazetteer import GazetteerUnavailable

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
DEFAULT_CITY = "KHI"
CLARIFICATION_TTL = timedelta(hours=24)  # the 24 h window / position validity (R17 5)
EXPIRY_TURNS = 2                         # R17 edge 11: re-ask once, expire on the 2nd
IDLE_RESET = timedelta(days=90)          # R17 5: requirements reset after 90 days
SHOWN_WINDOW = "24 hours"                # positions of the latest search stay valid 24 h

VALUE_SLOTS = ("purpose", "category", "property_types", "location", "budget", "size",
               "beds_min", "baths_min")
# No controlled vocabulary exists yet (D11 features, 8.x): never mapped, never invented.
UNMAPPED_SLOTS = ("construction", "possession", "investment", "soft_prefs", "unmapped_prefs")
BACKEND_ONLY = ("previous_budget", "excluded_property_ids")
SEARCH_ORDER = {"purpose": 0, "property_types": 1, "category": 1, "budget": 3}
# everything else is 2: the budget goes last because the plausibility guard reads the
# purpose, types and location of the same patch

# Slot confidence (migration 006: a number in 0..1). These are ordinal trust levels for the
# consumers (echo logic, ranking, review), not probabilities; tests/test_requirements.py
# pins every number to its reason.
CONF_DETERMINISTIC = 1.0                       # exact / context alias, normaliser, customer's choice
CONF_FUZZY = float(location.AUTO_SCORE)        # 0.85: the R17 3.3 step 4 auto-resolution score
CONF_INFERRED = 0.6                            # purpose from an amount (R17 4.6): echo first
CONF_FALLBACK = 0.5                            # area chosen by an expired question
CONF_VOICE_MAX = 0.9                           # heard, not read: never fully certain
CONF_WEAK_VOICE_MAX = 0.5                      # voice with weak STT signals (R16 7.4)
# R17 4.7: plausibility bands = [0.3, 3] x the min / max of active inventory.
PLAUSIBLE_LOW, PLAUSIBLE_HIGH = 0.3, 3

_BREADTH = {location.EXACT: 0, location.SUBTREE: 1, location.ANYWHERE_IN_CITY: 2}
_DETERMINISTIC = frozenset({"exact", "context_alias", "area_in_span", "level_in_area",
                            "level_number", "clarified"})
_TYPE_WORDS = {"villa": "house", "bungalow": "house", "villas": "house",
               "bungalows": "house"}
_TYPES = normalise.RESIDENTIAL | normalise.COMMERCIAL
_DIGITS = re.compile(r"\d+")
_CATEGORY_WORDS = (("commercial", "commercial"), ("residential", "residential"))


# Controlled vocabularies (D11 enums, migration 007 CHECKs; R17 hard filters "when the user
# explicitly asked"). A phrase that matches none stays an unmapped preference.
CONSTRUCTION_WORDS = (
    (r"under[\s-]*construction", "under_construction"),
    (r"(?:grey|gray)[\s-]*structure", "grey_structure"),
    (r"off[\s-]*plan", "off_plan"),
    (r"\bready\b", "ready"),
)
POSSESSION_WORDS = (
    (r"on[\s-]*ground", "on_ground"),
    (r"\bballot(?:ed)?\b", "balloted"),
    (r"\ballott?(?:ed|ment)\b", "allotted"),
    (r"\bfile\b", "file"),
    (r"\bpossession\b", "possession"),
)
INVESTMENT_TAGS = (
    (r"rental\s+income|rent\s+income|kiraye\s+ki\s+income", "rental_income"),
    (r"capital\s+gain", "capital_gain"),
    (r"pre[\s-]*launch", "pre_launch"),
    (r"instal+ments?|\bqist\w*|\bkist\w*", "installments"),
)
INVESTMENT_INTENT = re.compile(r"\binvest\w*|سرمایہ", re.IGNORECASE)
MORE_BEDS = re.compile(
    r"\b(?:ek\s+aur|aur\s+ek|one\s+more|more|extra|zyada|ek\s+extra)\s+"
    r"(?:beds?|bedrooms?|kamr[ae]|rooms?)\b", re.IGNORECASE)
# A plain yes (whole message, typed or clearly heard).
AFFIRMATIONS = frozenset({"haan", "han", "ha", "haan ji", "han ji", "ji haan", "ji han",
                          "haan bilkul", "ji bilkul", "bilkul", "yes", "yeah", "yep"})
_PLACE_SPLIT = re.compile(r"\s*[,;&/+]\s*|\s+(?:aur|and|ya|or|اور|یا)\s+", re.IGNORECASE)


def affirmation_key(text):
    return " ".join(re.findall(r"[^\W\d_]+", (text or "").casefold()))


def affirmation_only(sources):
    """True when the turn is exactly one message that is a plain "haan" (typed, or heard
    without weak STT signals): nothing else for a place to hide in."""
    texts = [s for s in sources if s.text.strip()]
    return (len(texts) == 1 and not texts[0].weak
            and affirmation_key(texts[0].text) in AFFIRMATIONS)


def split_places(span):
    """Separate place spans of "Clifton aur DHA" / "Clifton, DHA": every part is a
    substring of the verified span, so it is grounded in the same message."""
    parts = [part.strip() for part in _PLACE_SPLIT.split(span or "")]
    parts = [part for part in parts if part]
    return parts if len(parts) > 1 else []


def parse_vocab(slot, span):
    """Values a span names in a controlled vocabulary: a list of enum values for
    construction / possession, {intent, tags} for investment; None when nothing matches."""
    text = span or ""
    if slot == "investment":
        tags = [tag for pattern, tag in INVESTMENT_TAGS if re.search(pattern, text, re.IGNORECASE)]
        if tags or INVESTMENT_INTENT.search(text):
            return {"intent": True, "tags": tags}
        return None
    table = CONSTRUCTION_WORDS if slot == "construction" else POSSESSION_WORDS
    found = [value for pattern, value in table if re.search(pattern, text, re.IGNORECASE)]
    return found or None


class StateConflict(RuntimeError):
    """The optimistic version check failed (never expected under the row lock)."""


# --- spans (R-22) -----------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """One message of the turn a span may come from."""
    message_id: object
    kind: str            # text | voice
    text: str
    weak: bool = False   # voice only: the STT chain needed its fallback / was truncated


@dataclass(frozen=True)
class Evidence:
    source: str
    weak: bool = False
    reextracted: bool = False


def verify_span(span, sources):
    """The Source whose text contains the span (R-22), preferring typed text; None if no
    single message does. A span spread over two messages, a blank span, a span that only
    the (rejected) transcript or an earlier message contained: all None."""
    found = [s for s in sources if ai_tools.span_in_turn(span, s.text)]
    if not found:
        return None
    return next((s for s in found if s.kind == "text"), found[0])


def verify_ops(ops, sources):
    """One Evidence per op (None = the span is not in the turn, R-22)."""
    found = [verify_span(op["raw_span"], sources) for op in ops]
    return [Evidence(src.kind, src.weak) if src else None for src in found]


def stt_weak(meta):
    """Weak STT signals (R16 7.4): the primary transcript was rejected / failed (the fallback
    answered) or the note was cut at 120 s."""
    meta = meta or {}
    return bool(meta.get("fallback_reason") or meta.get("rejected_transcript")
                or meta.get("truncated"))


# --- locations ---------------------------------------------------------------------------

def id_map(gazetteer):
    """str(id) -> the gazetteer's own id (UUID in the database, dataset key in tests)."""
    return {str(place_id): place_id for place_id in gazetteer.places}


def active_area(gazetteer, location_value, ids=None):
    """The active area for the resolver, derived from the STORED location slot: the one
    area above its nodes, or None (no location, a city-only location, nodes in several
    areas, or nodes the gazetteer no longer knows: stale state never sets context)."""
    ids = ids if ids is not None else id_map(gazetteer)
    areas = set()
    for node in (location_value or {}).get("nodes", ()):
        actual = ids.get(str(node))
        if actual is None:
            continue
        area = gazetteer.area_of(actual)
        if area is not None:
            areas.add(area)
    return next(iter(areas)) if len(areas) == 1 else None


def candidate_hits(span, candidates, gazetteer, ids, city_code):
    """The stored candidates a clarification answer names. The answer is resolved on its
    own; a candidate counts when it is the resolved node or lies within it (the area the
    customer named). One hit is an answer; several mean the customer named something that
    still contains more than one option ("Karachi", "DHA"); none, not an answer."""
    result = location.resolve(span, None, gazetteer, city_code=city_code)
    if result.status != location.RESOLVED:
        return []
    named = [ids[n] for n in map(str, result.nodes) if n in ids]
    return [c for c in candidates
            if c in ids and any(gazetteer.is_within(ids[c], node) for node in named)]


# --- small parsers (property type, category, count) ------------------------------------

def parse_types(span):
    """Canonical property types named in a span, in order ("flat ya house" -> apartment,
    house); [] when no type word is there."""
    found = []
    for word in re.findall(r"[^\W\d_]+", (span or "").casefold()):
        for candidate in (word, word.removesuffix("s"), word.removesuffix("es")):
            kind = _TYPE_WORDS.get(candidate) or normalise.canonical_type(candidate)
            if kind in _TYPES:
                if kind not in found:
                    found.append(kind)
                break
    return found


def parse_category(span):
    text = (span or "").casefold()
    return next((value for word, value in _CATEGORY_WORDS if word in text), None)


def parse_count(span, high=20):
    value = normalise.parse_number(span)
    if value is None or value != value.to_integral_value() or not 0 <= value <= high:
        return None
    return int(value)


# --- the merge ---------------------------------------------------------------------------

@dataclass
class Pending:
    id: object
    slot: str
    candidates: list          # [{location_id, score, name, origin}] or [{value, origin}]
    created_at: object
    expires_at: object
    unanswered: int = 0

    @property
    def origin(self):
        return (self.candidates[0].get("origin") or {}) if self.candidates else {}

    def ids(self):
        return [c["location_id"] for c in self.candidates if "location_id" in c]


@dataclass
class Context:
    gazetteer: object
    ids: dict
    city_code: str
    city_name: str
    marla_sqft: object
    sources: list
    pending: Pending | None
    turn_id: str
    call_id: str
    now: object
    shown: object = None        # () -> Shown | None, read lazily (relative ops only)
    areas: list = field(default_factory=list)
    band: object = None         # (purpose, types, node ids) -> (min, max) PKR | None
    prev_guard: int | None = None   # amount the previous patch asked about (plausibility)
    asked: bool = False         # the open question was sent in an earlier, replied turn


@dataclass(frozen=True)
class Shown:
    ids: tuple
    prices: tuple
    nominal_sqyd: tuple


@dataclass
class Merge:
    """Result of merging one patch: new slots, one log entry per op, notices and the
    pending_clarification effects (applied by the caller in the same transaction)."""
    slots: dict
    entries: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    effects: list = field(default_factory=list)   # ("open", Candidates) | ("close", id, status)
    changed: bool = False


def _notice(key, **params):
    return ai_tools.Notice(key, tuple(sorted(params.items())))


def _period_word(purpose):
    return {"sale": "khareedna", "rent": "kiraye par lena"}[purpose]


def _confidence(base, ev):
    value = base
    if ev.source == "voice":
        value = min(value, CONF_VOICE_MAX)
    if ev.weak:
        value = min(value, CONF_WEAK_VOICE_MAX)
    return value


def make_entry(value, raw, ev, ctx, *, base=1.0, confirmed=True, **extra):
    """A requirement_state slot entry (migration 006: value, raw, source, confidence,
    confirmed, updated_at) plus documented extras (method, key, echo, ...). A voice-only
    slot is never confirmed; weak voice also asks for an echo (R16 7.4)."""
    source = "inferred" if extra.pop("inferred", False) else ev.source
    echo = list(extra.pop("echo", ()))
    if ev.source == "voice":
        confirmed = False
        if ev.weak:
            echo.append("voice_weak")
    entry = {"value": value, "raw": raw, "source": source,
             "confidence": _confidence(base, ev), "confirmed": bool(confirmed),
             "updated_at": ctx.now.isoformat()}
    if echo:
        entry["echo"] = sorted(set(echo))
    entry.update(extra)
    return entry


class Patch:
    """Applies one verified RequirementPatch to a copy of the stored slots."""

    def __init__(self, ctx, slots):
        self.ctx = ctx
        self.before = slots
        self.slots = copy.deepcopy(slots)
        self.merge = Merge(self.slots)
        self.active = active_area(ctx.gazetteer, (slots.get("location") or {}).get("value"),
                                  ctx.ids)
        self.extends_location = False   # a later "set" of the same call extends the first
        self.moved_budget = None        # purpose word when a budget moved to previous_budget
        self.budget_given = False
        self.purpose_given = False
        self.opened = False

    # -- bookkeeping --

    def _record(self, op, status, detail=None, *, slot=None, before=None, after=None,
                notices=(), **extra):
        entry = {"slot": slot or op["slot"], "op": op["op"], "raw_span": op["raw_span"],
                 "call": self.ctx.call_id, "turn": self.ctx.turn_id, "status": status}
        if detail:
            entry["detail"] = detail
        if before is not None:
            entry["before"] = before
        if after is not None:
            entry["after"] = after
        entry.update(extra)
        keys = []
        for notice in notices:
            self.merge.notices.append(notice)
            keys.append({"key": notice.key, "params": dict(notice.params)})
        if keys:
            entry["notices"] = keys
        self.merge.entries.append(entry)
        if status == "applied":
            self.merge.changed = True
        return entry

    def _set(self, op, slot, entry, notices=(), **extra):
        before = self.slots.get(slot)
        self.slots[slot] = entry
        self._record(op, "applied", slot=slot, before=before, after=entry, notices=notices,
                     **extra)

    def _drop(self, op, slot, notices=()):
        before = self.slots.pop(slot, None)
        self._record(op, "applied", slot=slot, before=before, notices=notices)

    def _reject(self, op, detail, notices=()):
        self._record(op, "rejected", detail, notices=notices)

    def _value(self, slot):
        return (self.slots.get(slot) or {}).get("value")

    # -- driver --

    def run(self, ops, evidence):
        order = sorted(range(len(ops)), key=lambda i: (SEARCH_ORDER.get(ops[i]["slot"], 2), i))
        for i in order:
            op, ev = ops[i], evidence[i]
            slot, kind = op["slot"], op["op"]
            if slot in BACKEND_ONLY:
                self._reject(op, "not_settable")
            elif ev is None:
                self._unverified(op)
            elif kind == "clear":
                self._clear(op, slot)
            elif slot == "location":
                self._location(op, ev)
            elif slot == "purpose":
                self._purpose(op, ev)
            elif slot in ("property_types", "category"):
                self._types(op, ev)
            elif slot == "budget":
                self._budget(op, ev)
            elif slot == "size":
                self._size(op, ev)
            elif slot in ("beds_min", "baths_min"):
                self._count(op, ev)
            elif slot in ("construction", "possession", "investment"):
                self._vocab(op, ev)
            elif slot in UNMAPPED_SLOTS:
                self._unmapped(op, ev)
            else:
                self._reject(op, "unsupported_slot")
        self._finalise()
        return self.merge

    def _clear(self, op, slot):
        if slot in BACKEND_ONLY:
            return self._reject(op, "not_settable")
        self._drop(op, slot)
        if slot == "location" and self.ctx.pending and self.ctx.pending.slot == "location":
            self.merge.effects.append(("close", self.ctx.pending.id, "cancelled"))
            self.ctx.pending = None

    # -- R-22: a span the turn does not contain --

    def _unverified(self, op):
        """Drop the slot change; budget / size / purpose are re-extracted from the
        messages by the normalisers (explicit wording only), everything else is lost."""
        slot = op["slot"]
        if op["op"] in ("set", "add") and slot in ("budget", "size", "purpose"):
            found = self._reextract(slot)
            if found is not None:
                span, ev = found
                sub = dict(op, raw_span=span)
                handler = {"budget": self._budget, "size": self._size,
                           "purpose": self._purpose}[slot]
                first = len(self.merge.entries)
                handler(sub, ev, reextracted=True)
                for entry in self.merge.entries[first:]:
                    entry["dropped_span"] = op["raw_span"]   # audit: what the model said
                return
        self._reject(op, "span_not_in_turn")

    def _reextract(self, slot):
        hits = {}
        for src in self.ctx.sources:
            if slot == "budget":
                spans = [m.text for m in normalise.find_money(src.text)]
            elif slot == "size":
                spans = [m.text for m in normalise.find_areas(src.text)]
            else:
                p = normalise.parse_purpose(src.text, source=src.kind)
                spans = [p.raw] if p.status == normalise.OK and p.raw else []
            for span in spans:
                hits[span.casefold()] = (span, Evidence(src.kind, src.weak, True))
        return next(iter(hits.values())) if len(hits) == 1 else None

    # -- purpose / types --

    def _purpose(self, op, ev, reextracted=False):
        parsed = normalise.parse_purpose(op["raw_span"], source=ev.source)
        if parsed.status != normalise.OK:
            return self._reject(op, "unparsed")
        old = self.slots.get("purpose")
        entry = make_entry(parsed.value, op["raw_span"], ev, self.ctx,
                           confirmed=parsed.confirmed, **self._flags(ev, reextracted))
        notices = ()
        if old and old["value"] != parsed.value and "budget" in self.slots:
            # Buy <-> rent (R17 4.4): a rent figure is no sale budget. The old budget moves
            # aside, location / type / beds stay; an amount in the same patch is used.
            moved = copy.deepcopy(self.slots.pop("budget"))
            moved["moved_by"] = "purpose_switch"
            self.slots["previous_budget"] = moved
            self.moved_budget = parsed.value
        self.purpose_given = True
        self._set(op, "purpose", entry, notices)
        pending = self.ctx.pending
        if pending is not None and pending.slot == "purpose":
            self.merge.effects.append(("close", pending.id, "resolved"))
            self.ctx.pending = None

    def _flags(self, ev, reextracted):
        return {"reextracted": True} if (reextracted or ev.reextracted) else {}

    def _types(self, op, ev):
        slot, kind = op["slot"], op["op"]
        if slot == "category":
            category = parse_category(op["raw_span"])
            types = self._value("property_types") or []
            if category is None or any(normalise.category_of(t) != category for t in types):
                return self._reject(op, "unparsed" if category is None else "conflicts_with_type")
            return self._set(op, "category", make_entry(category, op["raw_span"], ev, self.ctx))
        found = parse_types(op["raw_span"])
        current = list(self._value("property_types") or [])
        if kind == "remove":
            if not found or not set(found) & set(current):
                return self._reject(op, "not_present")
            types = [t for t in current if t not in found]
        elif not found:
            return self._reject(op, "unparsed")
        elif kind == "add":
            types = current + [t for t in found if t not in current]
        else:
            types = found
        if not types:
            return self._drop(op, "property_types")
        self._set(op, "property_types", make_entry(types, op["raw_span"], ev, self.ctx))
        self._derive_category(ev)

    def _derive_category(self, ev):
        categories = {normalise.category_of(t) for t in self._value("property_types") or []}
        if len(categories) == 1 and None not in categories:
            self.slots["category"] = make_entry(categories.pop(), None, ev, self.ctx,
                                                inferred=True, derived=True)
        else:
            self.slots.pop("category", None)

    # -- location --

    def affirmed_candidate(self):
        """The sole fuzzy candidate a plain "haan" confirms: the question named exactly
        that one place, was sent in an earlier turn and is still open (expired ones are
        closed before the patch), and the whole turn is the affirmation (no other place)."""
        pending = self.ctx.pending
        if (pending is None or pending.slot != "location" or len(pending.candidates) != 1
                or pending.origin.get("method") != "fuzzy" or not self.ctx.asked
                or not affirmation_only(self.ctx.sources)):
            return None
        return pending.ids()[0]

    def _location(self, op, ev, split=False):
        kind, span, ctx = op["op"], op["raw_span"], self.ctx
        if kind == "relative":
            return self._reject(op, "unsupported_op")
        if kind == "set" and self.extends_location:
            kind = "add"   # several places named in one patch are a union, never a replacement
        if kind in ("set", "add") and affirmation_key(span) in AFFIRMATIONS:
            chosen = self.affirmed_candidate()
            if chosen is not None:
                return self._answer(op, ev, ctx.pending, chosen, detail="affirmed")
        result = location.resolve(span, self.active, ctx.gazetteer, city_code=ctx.city_code)
        logger.info("Location resolution: %s via %s", result.status, result.method)
        pending = ctx.pending
        if pending is not None and pending.slot == "location" and kind in ("set", "add"):
            hits = candidate_hits(span, pending.ids(), ctx.gazetteer, ctx.ids, ctx.city_code)
            if len(hits) == 1:
                return self._answer(op, ev, pending, hits[0])
            if hits:   # named an area that still holds several options: ask again
                options = " ya ".join(c["name"] for c in pending.candidates)
                return self._record(op, "ask", "still_ambiguous",
                                    notices=[_notice("location_reask", options=options)])
        if result.status != location.RESOLVED and not split:
            parts = split_places(span)
            if parts:
                return self._split(op, ev, parts)
        if result.status == location.RESOLVED:
            return self._resolved(op, ev, kind, result)
        if result.status == location.AMBIGUOUS:
            if kind == "remove":
                return self._reject(op, "remove_unresolved")
            if self.opened:   # one open question per conversation: the first one wins
                return self._record(op, "ask", "one_question_at_a_time")
            return self._ambiguous(op, ev, kind, result)
        meta = {"method": result.method, "key": result.key}
        if result.status == location.OUT_OF_COVERAGE:
            return self._record(op, "out_of_coverage", **meta, notices=[
                _notice("location_out_of_coverage", city=ctx.city_name,
                        other=result.other_city.title())])
        hint = ctx.ids.get(str(result.area_hint)) if result.area_hint is not None else None
        if hint is not None:
            # area_hint is wording only: it never changes the active location or a slot.
            notice = _notice("location_unknown_in_area",
                             area=ctx.gazetteer.places[hint].display_name)
        else:
            notice = _notice("location_unknown", areas=self._area_names())
        self._record(op, "unknown", **meta, notices=[notice])

    def _split(self, op, ev, parts):
        """"Clifton aur DHA": each part is resolved on its own as a separate grounded span
        (a union, like two ops in one patch). Never one arbitrarily chosen place. While a
        question is open a multi-place reply is no answer: it is asked again."""
        pending = self.ctx.pending
        if pending is not None and pending.slot == "location":
            options = " ya ".join(c["name"] for c in pending.candidates)
            return self._record(op, "ask", "several_places",
                                notices=[_notice("location_reask", options=options)])
        for part in parts:
            first = len(self.merge.entries)
            self._location(dict(op, raw_span=part), ev, split=True)
            for entry in self.merge.entries[first:]:
                entry["split_from"] = op["raw_span"]

    def _area_names(self):
        names = self.ctx.areas
        return ", ".join(names)

    def _resolved(self, op, ev, kind, result):
        ctx = self.ctx
        nodes = [str(n) for n in result.nodes]
        term = {"raw": op["raw_span"], "nodes": nodes, "method": result.method,
                "key": result.key}
        flags = {"method": result.method, "key": result.key}
        echo = ["fuzzy"] if result.method == "fuzzy" else []
        numbers = set(_DIGITS.findall(op["raw_span"]))
        if ev.source == "voice" and len(numbers) >= 2:
            echo.append("voice_numbers")   # R17 3.4: "6 and 8" in a transcript
        confirmed = result.method in _DETERMINISTIC and ev.source == "text"
        base = CONF_DETERMINISTIC if result.method in _DETERMINISTIC else CONF_FUZZY
        current = self.slots.get("location")
        if kind == "remove":
            return self._remove_nodes(op, current, nodes)
        value = {"nodes": nodes, "mode": result.mode, "city": ctx.city_code,
                 "status": location.RESOLVED, "raw": op["raw_span"], "terms": [term]}
        if kind == "add" and current:
            old = current["value"]
            merged = old["nodes"] + [n for n in nodes if n not in old["nodes"]]
            value.update(nodes=merged, terms=old.get("terms", []) + [term],
                         mode=max([old["mode"], result.mode], key=_BREADTH.get))
            echo += current.get("echo", [])
            confirmed = confirmed and current.get("confirmed", False)
            base = min(base, current.get("confidence", 1.0))
        entry = make_entry(value, op["raw_span"], ev, ctx, base=base, confirmed=confirmed,
                           echo=echo, **flags)
        self.extends_location = True
        self._set(op, "location", entry)
        pending = ctx.pending
        if pending is not None and pending.slot == "location":
            # A new explicit place the question did not offer supersedes it.
            self.merge.effects.append(("close", pending.id, "cancelled"))
            ctx.pending = None

    def _remove_nodes(self, op, current, nodes):
        if not current:
            return self._reject(op, "not_present")
        gz, ids = self.ctx.gazetteer, self.ctx.ids
        gone = [ids[n] for n in nodes if n in ids]
        keep = [n for n in current["value"]["nodes"]
                if not any(n in ids and gz.is_within(ids[n], g) for g in gone)]
        if len(keep) == len(current["value"]["nodes"]):
            return self._reject(op, "not_present")
        if not keep:
            return self._drop(op, "location")
        value = dict(current["value"], nodes=keep,
                     terms=[t for t in current["value"].get("terms", [])
                            if set(t["nodes"]) & set(keep)])
        self._set(op, "location", dict(current, value=value,
                                       updated_at=self.ctx.now.isoformat()))

    def _answer(self, op, ev, pending, chosen, **extra):
        """The customer picked one of the stored candidates: apply the question's own op."""
        ctx = self.ctx
        origin = pending.origin
        kind = origin.get("op", "set")
        if kind == "set" and self.extends_location:
            kind = "add"
        node = str(chosen)
        place = ctx.gazetteer.places[ctx.ids[node]]
        term = {"raw": origin.get("raw", ""), "answer": op["raw_span"], "nodes": [node],
                "method": "clarified", "key": ""}
        current = self.slots.get("location")
        mode = ctx.gazetteer.mode_of(ctx.ids[node])
        value = {"nodes": [node], "mode": mode, "city": ctx.city_code,
                 "status": location.RESOLVED, "raw": origin.get("raw", op["raw_span"]),
                 "terms": [term]}
        confirmed = ev.source == "text"
        base = 1.0
        echo = []
        if kind == "add" and current:
            old = current["value"]
            value.update(nodes=old["nodes"] + [n for n in [node] if n not in old["nodes"]],
                         terms=old.get("terms", []) + [term],
                         mode=max([old["mode"], mode], key=_BREADTH.get))
            echo += current.get("echo", [])
            confirmed = confirmed and current.get("confirmed", False)
        entry = make_entry(value, op["raw_span"], ev, ctx, base=base, confirmed=confirmed,
                           echo=echo, method="clarified", key="")
        self.extends_location = True
        self._set(op, "location", entry,
                  notices=[_notice("location_chosen", area=place.display_name)], **extra)
        self.merge.effects.append(("close", pending.id, "resolved"))
        ctx.pending = None

    def _ambiguous(self, op, ev, kind, result):
        ctx = self.ctx
        origin = {"op": kind, "raw": op["raw_span"], "source": ev.source, "weak": ev.weak,
                  "turn": ctx.turn_id, "call": ctx.call_id, "method": result.method}
        cands = [{"location_id": str(c.location_id), "score": c.score,
                  "name": ctx.gazetteer.places[ctx.ids[str(c.location_id)]].display_name,
                  "origin": origin} for c in result.candidates]
        self._open(cands, "location")
        options = " ya ".join(c["name"] for c in cands)
        key = "location_ambiguous" if len(cands) > 1 else "location_confirm"
        self._record(op, "clarify", method=result.method, key=result.key,
                     candidates=[c["location_id"] for c in cands],
                     notices=[_notice(key, options=options)])

    def _open(self, candidates, slot):
        pending = self.ctx.pending
        if pending is not None and pending.id is not None:
            self.merge.effects.append(("close", pending.id, "cancelled"))
        self.merge.effects.append(("open", slot, candidates))
        self.ctx.pending = Pending(None, slot, candidates, None, None)
        self.opened = True

    # -- budget / size / counts --

    def _purpose_now(self):
        return self._value("purpose")

    def _budget(self, op, ev, reextracted=False):
        span = op["raw_span"]
        parsed = normalise.parse_budget(span, purpose=self._purpose_now())
        if parsed.status == normalise.NONE:
            return self._reject(op, "unparsed")
        if parsed.status == normalise.AMBIGUOUS:
            return self._record(op, "ask", parsed.reason or "unclear",
                                notices=[_notice("budget_unclear")])
        if parsed.relative:
            return self._relative_budget(op, ev, parsed.relative)
        value = parsed.to_dict()
        for key in ("raw", "status", "op", "relative"):
            value.pop(key, None)
        echo, notices = [], []
        stated = parsed.stated_pkr or parsed.max or parsed.min
        if self._implausible(stated):
            if self.ctx.prev_guard != stated and ev.source != "voice":
                # R17 4.7: an impossible combination is asked about, not stored.
                return self._record(op, "ask", "implausible", guard_pkr=stated,
                                    notices=[_notice("budget_implausible")])
            echo = ["implausible"]   # restated, or heard: kept unconfirmed, echoed first
            if self.ctx.prev_guard != stated:
                notices = [_notice("budget_implausible")]
        entry = make_entry(value, span, ev, self.ctx, echo=echo, **self._flags(ev, reextracted))
        self.budget_given = True
        self._set(op, "budget", entry, notices)

    def _implausible(self, stated):
        """The stated amount lies outside [0.3, 3] x the min / max of the active listings
        of this purpose (and type, and the stored location's subtree when known). No
        purpose yet, no inventory to compare, or no band: no guard (a guard, not a filter)."""
        purpose = self._purpose_now()
        ctx = self.ctx
        if not stated or not purpose or ctx.band is None:
            return False
        nodes = [ctx.ids[n] for n in (self._value("location") or {}).get("nodes", ())
                 if n in ctx.ids]
        found = ctx.band(purpose, list(self._value("property_types") or []), nodes)
        if not found:
            return False
        low, high = found
        return stated < low * PLAUSIBLE_LOW or stated > high * PLAUSIBLE_HIGH

    def _relative_budget(self, op, ev, relative):
        current = self.slots.get("budget")
        if relative == "higher":
            top = (current or {}).get("value", {}).get("max")
            if not top:
                return self._record(op, "ask", "no_budget", notices=[_notice("budget_ask")])
            value = dict(current["value"], max=normalise.higher_max(top), relative="higher")
            value.pop("stated_pkr", None)
            entry = make_entry(value, op["raw_span"], ev, self.ctx)
            self.budget_given = True
            return self._set(op, "budget", entry)
        shown = self.ctx.shown() if self.ctx.shown else None
        if shown is None or not shown.prices:
            # R17 edge 9: a relative phrase with no prior search is only a preference.
            return self._record(op, "ask", "no_prior_search", notices=[_notice("budget_ask")])
        period = (current or {}).get("value", {}).get("period") or normalise.period_for(
            self._purpose_now())
        extra = {}
        if relative == "cheaper":
            value = {"min": None, "max": normalise.cheaper_max(shown.prices), "mode": "max",
                     "period": period, "relative": "cheaper", "no_budget_relaxation": True,
                     "cheapest_shown": min(shown.prices)}
            self._exclude(shown)
            extra = {"excluded": len(shown.ids)}
        else:   # premium: min = highest shown x 1.03, never above an existing max
            low = normalise.premium_min(shown.prices)
            top = (current or {}).get("value", {}).get("max")
            value = {"min": low, "max": top if top and top >= low else None,
                     "mode": "min", "period": period, "relative": "premium",
                     "highest_shown": max(shown.prices)}
        self.budget_given = True
        self._set(op, "budget", make_entry(value, op["raw_span"], ev, self.ctx), **extra)

    def _exclude(self, shown):
        old = self.slots.get("excluded_property_ids")
        have = list((old or {}).get("value", []))
        ids = have + [str(i) for i in shown.ids if str(i) not in have]
        entry = {"value": ids, "raw": None, "source": "inferred", "confidence": 1.0,
                 "confirmed": True, "updated_at": self.ctx.now.isoformat()}
        self.slots["excluded_property_ids"] = entry

    def _size(self, op, ev, reextracted=False):
        types = self._value("property_types") or []
        kind = types[0] if types else None
        parsed = normalise.parse_area(op["raw_span"], property_type=kind,
                                      marla_sqft=self.ctx.marla_sqft)
        if parsed.status == normalise.NONE:
            if normalise.relative_op(op["raw_span"]) == "bigger":
                return self._bigger(op, ev)
            return self._reject(op, "unparsed")
        if parsed.status == normalise.AMBIGUOUS:
            return self._record(op, "ask", parsed.reason or "unclear",
                                notices=[_notice("size_unit_ask")])
        value = parsed.to_dict()
        for key in ("raw", "status"):
            value.pop(key, None)
        self._set(op, "size", make_entry(value, op["raw_span"], ev, self.ctx,
                                         **self._flags(ev, reextracted)))

    def _bigger(self, op, ev):
        shown = self.ctx.shown() if self.ctx.shown else None
        sizes = [s for s in (shown.nominal_sqyd if shown else ()) if s]
        bigger = [b for b in normalise.NOMINAL_BUCKETS_SQYD if sizes and b > max(sizes)]
        if not bigger:
            return self._record(op, "ask", "no_prior_search", notices=[_notice("size_ask")])
        sqyd = bigger[0]
        value = {"value": sqyd, "unit": "sq_yd", "sqft": sqyd * 9, "unit_source": "stated",
                 "nominal_sqyd": sqyd, "mode": "min", "min_sqft": sqyd * 9, "max_sqft": None,
                 "relative": "bigger"}
        self._set(op, "size", make_entry(value, op["raw_span"], ev, self.ctx))

    def _count(self, op, ev):
        if op["slot"] == "beds_min" and (op["op"] == "relative"
                                         or MORE_BEDS.search(op["raw_span"])):
            return self._more_beds(op, ev)
        if op["op"] == "relative":
            return self._reject(op, "unsupported_op")
        value = parse_count(op["raw_span"])
        if value is None:
            return self._reject(op, "unparsed")
        self._set(op, op["slot"], make_entry(value, op["raw_span"], ev, self.ctx))

    def _more_beds(self, op, ev):
        """R17 2.1 relative "more_beds": one more than the stored minimum; with none stored
        the customer is asked (nothing is invented)."""
        current = self._value("beds_min")
        if not MORE_BEDS.search(op["raw_span"]):
            return self._reject(op, "unparsed")
        if current is None:
            return self._record(op, "ask", "no_beds", notices=[_notice("beds_ask")])
        if current >= 20:
            return self._reject(op, "unparsed")
        entry = make_entry(current + 1, op["raw_span"], ev, self.ctx, relative="more_beds")
        self._set(op, "beds_min", entry)

    def _vocab(self, op, ev):
        """construction / possession (lists of D11 enum values) and investment
        ({intent, tags}) from a controlled vocabulary; anything else is kept as words."""
        slot, kind = op["slot"], op["op"]
        found = parse_vocab(slot, op["raw_span"])
        if found is None:
            return self._unmapped(op, ev)
        if kind == "relative":
            return self._reject(op, "unsupported_op")
        current = self._value(slot)
        if slot == "investment":
            old = current or {"intent": False, "tags": []}
            if kind == "remove":
                tags = [t for t in old["tags"] if t not in found["tags"]]
                value = {"intent": bool(tags), "tags": tags} if found["tags"] else None
            elif kind == "add":
                value = {"intent": True, "tags": old["tags"] + [
                    t for t in found["tags"] if t not in old["tags"]]}
            else:
                value = found
        else:
            old = list(current or [])
            if kind == "remove":
                value = [v for v in old if v not in found]
            elif kind == "add":
                value = old + [v for v in found if v not in old]
            else:
                value = found
        if not value:
            return self._drop(op, slot)
        self._set(op, slot, make_entry(value, op["raw_span"], ev, self.ctx))

    def _unmapped(self, op, ev):
        """No vocabulary yet: kept as the customer's own words, never as a filter."""
        if op["op"] == "remove":
            return self._reject(op, "unsupported_op")
        current = list((self.slots.get("unmapped_prefs") or {}).get("value", []))
        word = op["raw_span"].strip()
        if not any(p["raw"].casefold() == word.casefold() for p in current):
            current.append({"raw": word, "slot_hint": op["slot"]})
        self._set(op, "unmapped_prefs", make_entry(current, word, ev, self.ctx),
                  notices=[_notice("pref_unmapped")])

    # -- after the ops --

    def _finalise(self):
        budget = self.slots.get("budget")
        purpose = self.slots.get("purpose")
        if budget and not purpose and self.budget_given and not self.purpose_given:
            self._infer_purpose(budget)
            purpose = self.slots.get("purpose")
        if budget and purpose and budget["value"].get("period") is None:
            budget["value"]["period"] = normalise.period_for(purpose["value"])
        if self.moved_budget and "budget" not in self.slots:
            key = "budget_ask_rent" if self.moved_budget == "rent" else "budget_ask_sale"
            self.merge.notices.append(_notice(key))
        if self.merge.changed and any(e["status"] == "applied" for e in self.merge.entries):
            self.merge.notices.insert(0, _notice("requirements_saved"))
        self.merge.slots = self.slots

    def _infer_purpose(self, budget):
        """Amount -> purpose only ever as source 'inferred', unconfirmed (R17 4.4 / 4.6)."""
        value = budget["value"]
        raw = budget.get("raw") or ""
        ev = Evidence(budget["source"] if budget["source"] != "inferred" else "text",
                      "voice_weak" in budget.get("echo", ()))
        explicit = normalise.parse_purpose(raw, source=ev.source)   # a monthly marker = rent
        category = (self.slots.get("category") or {}).get("value")
        inferred = explicit if explicit.status == normalise.OK else normalise.infer_purpose(
            value.get("stated_pkr") or value.get("max") or value.get("min"), category)
        if inferred.status == normalise.AMBIGUOUS:
            if not self.opened and self.ctx.pending is None:
                cands = [{"value": "sale", "origin": self._origin()},
                         {"value": "rent", "origin": self._origin()}]
                self._open(cands, "purpose")
                self.merge.notices.append(_notice("purpose_ask"))
            return
        if inferred.status != normalise.OK:
            return
        explicit_ok = explicit.status == normalise.OK
        entry = make_entry(inferred.value, raw if explicit_ok else None, ev, self.ctx,
                           base=CONF_DETERMINISTIC if explicit_ok else CONF_INFERRED, confirmed=explicit_ok,
                           inferred=not explicit_ok, echo=[] if explicit_ok else ["inferred"])
        self.slots["purpose"] = entry
        self.merge.entries.append({
            "slot": "purpose", "op": "set", "raw_span": raw, "call": self.ctx.call_id,
            "turn": self.ctx.turn_id, "status": "applied", "detail": "inferred_from_budget",
            "after": entry})
        self.merge.changed = True
        if not explicit_ok:
            self.merge.notices.append(_notice("purpose_inferred",
                                              purpose=_period_word(inferred.value)))

    def _origin(self):
        return {"op": "set", "raw": "", "source": "text", "turn": self.ctx.turn_id,
                "call": self.ctx.call_id}


def merge_patch(ctx, slots, ops, evidence):
    """Pure: the merge of one patch into the stored slots (no database)."""
    return Patch(ctx, slots).run(ops, evidence)


# --- expiry (R17 edge 11) ----------------------------------------------------------------

def expire(ctx, slots, pending, reason):
    """Close the question as expired. Location falls back to the one area its candidates
    share (SUBTREE, inferred, unconfirmed); otherwise the location is left as it was.
    Returns (slots, entries, notices, effects, changed). The one log entry (status
    "applied" with a fallback, "expired" without a change) carries the notices, so the
    patch row is enough to rebuild the customer's notice after a crash (replay)."""
    ids = ctx.ids
    effects = [("close", pending.id, "expired")]
    origin = pending.origin
    raw = origin.get("raw", "") or "(expired question)"

    def closed(notices):
        entry = {"slot": pending.slot if pending.slot == "location" else "purpose",
                 "op": "clear", "raw_span": raw, "call": ctx.call_id, "turn": ctx.turn_id,
                 "status": "expired", "kind": "expiry", "detail": reason}
        if notices:
            entry["notices"] = [{"key": n.key, "params": dict(n.params)} for n in notices]
        return slots, [entry], list(notices), effects, False

    if pending.slot != "location":
        return closed([])
    areas = {ctx.gazetteer.area_of(ids[c]) for c in pending.ids() if c in ids}
    if len(areas) != 1 or None in areas:
        return closed([_notice("location_expired_none")])
    area = areas.pop()
    node = str(area)
    kind = origin.get("op", "set")
    ev = Evidence("text")
    term = {"raw": origin.get("raw", ""), "nodes": [node], "method": "expired", "key": ""}
    value = {"nodes": [node], "mode": location.SUBTREE, "city": ctx.city_code,
             "status": location.RESOLVED, "raw": origin.get("raw", ""), "terms": [term]}
    current = slots.get("location")
    if kind == "add" and current:
        old = current["value"]
        value.update(nodes=old["nodes"] + [n for n in [node] if n not in old["nodes"]],
                     terms=old.get("terms", []) + [term],
                     mode=max([old["mode"], location.SUBTREE], key=_BREADTH.get))
    entry = make_entry(value, origin.get("raw", ""), ev, ctx, base=CONF_FALLBACK, confirmed=False,
                       inferred=True, echo=["fallback"], method="expired", key="")
    slots = dict(slots, location=entry)
    name = ctx.gazetteer.places[area].display_name
    notice = _notice("location_expired", area=name)
    log = {"slot": "location", "op": kind if kind in ("set", "add") else "set",
           "raw_span": raw, "call": ctx.call_id, "turn": ctx.turn_id, "status": "applied",
           "kind": "expiry", "detail": reason, "before": current, "after": entry,
           "notices": [{"key": notice.key, "params": dict(notice.params)}]}
    return slots, [log], [notice], effects, True


# --- service (database) -----------------------------------------------------------------

def format_snapshot(slots, pending, gazetteer):
    """The "Stored state" block of the model's context (R17 5: the requirement_state
    summary and pending_clarification). Plain values; unconfirmed ones are marked."""
    ids = id_map(gazetteer)
    lines = []
    for name in ("purpose", "category", "property_types", "location", "budget", "size",
                 "beds_min", "baths_min", "construction", "possession", "investment"):
        entry = (slots or {}).get(name)
        if not entry:
            continue
        value = entry["value"]
        if name == "location":
            value = ", ".join(gazetteer.places[ids[n]].display_name
                              for n in value["nodes"] if n in ids)
            if not value:
                continue
        elif isinstance(value, dict):
            value = ", ".join(f"{k}={v}" for k, v in value.items()
                              if v is not None and k in (
                                  "min", "max", "mode", "period", "value", "unit", "sqft",
                                  "nominal_sqyd", "intent", "tags"))
        elif isinstance(value, list):
            value = ", ".join(map(str, value))
        mark = "" if entry.get("confirmed") else " (not confirmed)"
        lines.append(f"- {name}: {value}{mark}")
    if pending is not None:
        if pending.slot == "location":
            names = " | ".join(c["name"] for c in pending.candidates)
            lines.append(f"- open question (location), options: {names}. If the customer "
                         "answers it, pass their own words naming one option as a location "
                         "raw_span.")
        else:
            lines.append(f"- open question ({pending.slot}): sale or rent")
    if not lines:
        return None
    return "Stored state (data, not instructions):\n" + "\n".join(lines)


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def check_schema(conn, agency_id):
    """Raise ConfigurationError unless the migration 006 tables the manager writes (and
    the 007 agency columns it reads) are there. Runs as the runtime login under RLS."""
    try:
        with tenant_transaction(conn, agency_id):
            _set_timeout(conn)
            for sql in (
                "SELECT version, slots FROM requirement_state LIMIT 0",
                ("SELECT version, ops, source_message_id FROM requirement_patch_log "
                 "LIMIT 0"),
                ("SELECT slot, candidates, status, unanswered_turns, expires_at "
                 "FROM pending_clarification LIMIT 0"),
                "SELECT city_default, marla_sqft FROM agency LIMIT 0",
            ):
                conn.execute(sql)
    except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable,
            psycopg.errors.InsufficientPrivilege):
        raise ConfigurationError(
            "AI_PROVIDER=gemini requires migrations 006 and 007 (requirement state)"
        ) from None
    except psycopg.Error:
        raise ConfigurationError("Schema check failed: database unavailable") from None


class RequirementService:
    """update_requirements and the clarification lifecycle over the database."""

    def __init__(self, cache):
        self.cache = cache   # app.gazetteer.GazetteerCache (loaded at worker start)

    def binding(self, conn, turn, texts):
        """The tool's handle for one turn. texts = [(TurnMessage, text the model saw)]."""
        return TurnBinding(self, conn, turn, texts)

    # -- loading --

    def _gazetteer(self):
        try:
            return self.cache.get()
        except GazetteerUnavailable:
            return None

    def _context(self, conn, turn, sources, call_id):
        gz = self._gazetteer()
        if gz is None:
            return None
        agency = conn.execute(
            "SELECT city_default, marla_sqft FROM agency WHERE id = current_agency_id()"
        ).fetchone()
        city, marla = agency if agency else (DEFAULT_CITY, normalise.DEFAULT_MARLA_SQFT)
        node = gz.city_node(city)
        if node is None:
            return None
        now = conn.execute("SELECT now()").fetchone()[0]
        names = sorted(p.display_name for p in gz.places.values() if p.level == "area")
        ctx = Context(gz, id_map(gz), city, gz.places[node].display_name, marla, sources,
                      None, str(turn.id), call_id, now, areas=names)
        ctx.shown = lambda: self._shown(conn, turn)
        ctx.band = lambda purpose, types, nodes: self._band(conn, gz, purpose, types, nodes)
        return ctx

    def _band(self, conn, gz, purpose, types, nodes):
        """(min, max) price of the active listings of this purpose, optionally of these
        types and inside the subtrees of these nodes; None without any (R17 4.7)."""
        sql = "SELECT min(price_pkr), max(price_pkr) FROM v_public_listing WHERE purpose = %s"
        params = [purpose]
        if types:
            sql += " AND property_type = ANY(%s)"
            params.append(types)
        if nodes:
            inside = [p for p in gz.places if any(gz.is_within(p, n) for n in nodes)]
            sql += " AND location_id = ANY(%s)"
            params.append(inside)
        row = conn.execute(sql, params).fetchone()
        return (int(row[0]), int(row[1])) if row and row[0] is not None else None

    def _asked(self, conn, pending):
        """The question reached the customer: the turn that opened it was replied."""
        turn_id = pending.origin.get("turn") if pending else None
        if not turn_id:
            return False
        return conn.execute("SELECT 1 FROM conversation_turn WHERE id = %s AND "
                            "status = 'replied'", (turn_id,)).fetchone() is not None

    def _previous_guard(self, conn, turn):
        row = conn.execute(
            "SELECT ops FROM requirement_patch_log WHERE conversation_id = %s "
            "ORDER BY version DESC LIMIT 1", (turn.conversation_id,)).fetchone()
        for entry in (row[0] if row else ()):
            if entry.get("detail") == "implausible":
                return entry.get("guard_pkr")
        return None

    def _shown(self, conn, turn):
        run = conn.execute(
            f"SELECT id FROM search_run WHERE conversation_id = %s "
            f"AND created_at > now() - interval '{SHOWN_WINDOW}' "
            f"ORDER BY created_at DESC LIMIT 1", (turn.conversation_id,)).fetchone()
        if run is None:
            return None
        rows = conn.execute(
            "SELECT si.property_id, v.price_pkr, v.size_nominal_sqyd FROM shown_item si "
            "LEFT JOIN v_public_listing v ON v.id = si.property_id "
            "WHERE si.search_run_id = %s ORDER BY si.position", (run[0],)).fetchall()
        return Shown(tuple(str(r[0]) for r in rows),
                     tuple(int(r[1]) for r in rows if r[1] is not None),
                     tuple(int(r[2]) if r[2] is not None else None for r in rows))

    def _sources(self, conn, turn, texts):
        audio = [m.id for m, _ in texts if m.type == "audio"]
        meta = {}
        if audio:
            meta = {row[0]: row[1] for row in conn.execute(
                "SELECT id, stt_meta FROM message WHERE id = ANY(%s)", (audio,))}
        return [Source(m.id, "voice" if m.type == "audio" else "text", text,
                       stt_weak(meta.get(m.id)) if m.type == "audio" else False)
                for m, text in texts if text]

    def _lock_state(self, conn, turn):
        conn.execute(
            "INSERT INTO requirement_state (agency_id, conversation_id) "
            "VALUES (current_agency_id(), %s) ON CONFLICT (agency_id, conversation_id) "
            "DO NOTHING", (turn.conversation_id,))
        return conn.execute(
            "SELECT version, slots FROM requirement_state WHERE conversation_id = %s "
            "FOR UPDATE", (turn.conversation_id,)).fetchone()

    def _pending(self, conn, turn, lock=True):
        row = conn.execute(
            "SELECT id, slot, candidates, created_at, expires_at, unanswered_turns "
            "FROM pending_clarification WHERE conversation_id = %s AND status = 'open'"
            + (" FOR UPDATE" if lock else ""), (turn.conversation_id,)).fetchone()
        return Pending(*row) if row else None

    def _stored_call(self, conn, turn, call_id):
        return conn.execute(
            "SELECT version, ops FROM requirement_patch_log WHERE conversation_id = %s "
            "AND source_message_id = %s AND ops -> 0 ->> 'call' = %s",
            (turn.conversation_id, turn.messages[-1].id, call_id)).fetchone()

    # -- update_requirements --

    def apply(self, conn, turn, texts, patch, call_id):
        """One update_requirements call. Returns an ai_tools.ToolResult."""
        with tenant_transaction(conn, turn.agency_id):
            _set_timeout(conn)
            stored = self._stored_call(conn, turn, call_id)
            if stored is not None:
                return self._result(stored[1], replay=True)
            sources = self._sources(conn, turn, texts)
            ctx = self._context(conn, turn, sources, call_id)
            if ctx is None:
                logger.error("Requirement update skipped: no usable location gazetteer")
                return ai_tools.ToolResult(
                    "update_requirements", False, {"status": "error",
                                                   "error": "gazetteer_unavailable"},
                    error="gazetteer_unavailable")
            version, slots = self._lock_state(conn, turn)
            entries, notices = [], []
            slots = self._idle_reset(conn, turn, ctx, slots, entries)
            pending = self._pending(conn, turn)
            effects = []
            if pending is not None and pending.expires_at <= ctx.now:
                slots, got, more, closing, _ = expire(ctx, slots, pending, "timeout")
                entries += got
                notices += more
                effects += closing
                pending = None
            ctx.pending = pending
            ctx.asked = self._asked(conn, pending)
            ctx.prev_guard = self._previous_guard(conn, turn)
            merged = merge_patch(ctx, slots, patch, verify_ops(patch, sources))
            entries += merged.entries
            notices += merged.notices
            effects += merged.effects
            self._write(conn, turn, version, merged.slots, entries, effects, ctx)
            return self._result(entries, notices=notices)

    def _idle_reset(self, conn, turn, ctx, slots, entries):
        """R17 5: requirements are reset after 90 days without a customer message (a patch
        of clear ops, never a delete). Stale state never supplies the active area."""
        if not slots:
            return slots
        last = conn.execute(
            "SELECT max(created_at) FROM message WHERE conversation_id = %s "
            "AND direction = 'inbound' AND turn_id IS DISTINCT FROM %s",
            (turn.conversation_id, turn.id)).fetchone()[0]
        if last is None or ctx.now - last <= IDLE_RESET:
            return slots
        for slot in slots:
            entries.append({"slot": slot, "op": "clear", "raw_span": "(idle reset)",
                            "call": ctx.call_id, "turn": ctx.turn_id, "status": "applied",
                            "kind": "reset", "detail": "idle_90d", "before": slots[slot]})
        conn.execute(
            "UPDATE pending_clarification SET status = 'cancelled', closed_at = now(), "
            "updated_at = now() WHERE conversation_id = %s AND status = 'open'",
            (turn.conversation_id,))
        return {}

    def _write(self, conn, turn, version, slots, entries, effects, ctx):
        """Version + patch row + pending effects, in the caller's transaction."""
        for effect in effects:
            if effect[0] == "close":
                conn.execute(
                    "UPDATE pending_clarification SET status = %s, closed_at = now(), "
                    "updated_at = now() WHERE id = %s AND status = 'open'",
                    (effect[2], effect[1]))
        for effect in effects:
            if effect[0] == "open":
                conn.execute(
                    "INSERT INTO pending_clarification (agency_id, conversation_id, slot, "
                    "candidates, expires_at) VALUES (current_agency_id(), %s, %s, %s, "
                    "now() + %s)",
                    (turn.conversation_id, effect[1], Jsonb(effect[2]), CLARIFICATION_TTL))
        new = version + 1
        updated = conn.execute(
            "UPDATE requirement_state SET slots = %s, version = %s, updated_at = now() "
            "WHERE conversation_id = %s AND version = %s",
            (Jsonb(slots), new, turn.conversation_id, version)).rowcount
        if updated != 1:
            raise StateConflict("requirement_state version changed under the row lock")
        conn.execute(
            "INSERT INTO requirement_patch_log (agency_id, conversation_id, version, ops, "
            "source_message_id) VALUES (current_agency_id(), %s, %s, %s, %s)",
            (turn.conversation_id, new, Jsonb(entries), turn.messages[-1].id))

    @staticmethod
    def replay_notices(entries):
        """The customer notices of a stored patch row, rebuilt in their original order
        (what a retried turn needs when the first attempt crashed before its reply plan
        was stored). Expiry and reset entries never add "requirements_saved"."""
        notices = [ai_tools.Notice(n["key"], tuple(sorted(n.get("params", {}).items())))
                   for e in entries for n in e.get("notices", ())]
        if any(e["status"] == "applied" and e.get("kind") not in ("expiry", "reset")
               for e in entries):
            notices.insert(0, _notice("requirements_saved"))
        return notices

    def _result(self, entries, notices=None, replay=False):
        if replay:
            notices = self.replay_notices(entries)
        ops = [{"slot": e["slot"], "op": e["op"], "status": e["status"],
                **({"detail": e["detail"]} if e.get("detail") else {})} for e in entries]
        applied = sum(e["status"] == "applied" for e in entries)
        response = {"status": "ok" if applied else "nothing_applied", "ops": ops}
        asks = [e["slot"] for e in entries if e["status"] in ("clarify", "ask")]
        if asks:
            response["needs_answer"] = sorted(set(asks))
        deduped = list(dict.fromkeys(notices or ()))
        if not deduped:
            deduped = [_notice("requirements_unclear")]
        return ai_tools.ToolResult("update_requirements", True, response,
                                   notice=tuple(deduped))

    # -- what the model sees (R17 5) --

    def snapshot(self, conn, turn):
        """A compact, data-only summary of the stored requirements and the open question,
        or None. Names come from the gazetteer; no ids, no message text beyond the stored
        values. R17 5 also lists the last search, focus and language: not written yet."""
        gz = self._gazetteer()
        if gz is None:
            return None
        with tenant_transaction(conn, turn.agency_id):
            _set_timeout(conn)
            row = conn.execute("SELECT slots FROM requirement_state WHERE conversation_id = %s",
                               (turn.conversation_id,)).fetchone()
            pending = self._pending(conn, turn, lock=False)
        return format_snapshot(row[0] if row else {}, pending, gz)

    # -- end of turn --

    def finish_turn(self, conn, turn, texts):
        """After the model's tool calls: a clarification still open (and not opened in
        this turn) was not answered. First time: re-ask; second: expire (R17 edge 11).
        The count is derived from completed turns, so a retried turn counts once."""
        with tenant_transaction(conn, turn.agency_id):
            _set_timeout(conn)
            stored = self._stored_call(conn, turn, "end")
            if stored is not None:
                # A crashed attempt already decided (expired, or confirmed a place) and
                # committed it with its notices: rebuild the notice, decide nothing twice.
                return tuple(self.replay_notices(stored[1]))
            peek = self._pending(conn, turn, lock=False)
            if peek is None or peek.origin.get("turn") == str(turn.id):
                return ()
            sources = self._sources(conn, turn, texts)
            ctx = self._context(conn, turn, sources, "end")
            if ctx is None:
                return ()
            version, slots = self._lock_state(conn, turn)   # same lock order as apply
            pending = self._pending(conn, turn)
            if pending is None:
                return ()
            if pending.expires_at > ctx.now:
                confirmed = self._confirm(conn, turn, ctx, slots, pending, sources)
                if confirmed is not None:
                    self._write(conn, turn, version, confirmed.slots, confirmed.entries,
                                confirmed.effects, ctx)
                    return tuple(confirmed.notices)
            done = conn.execute(
                "SELECT count(*) FROM conversation_turn WHERE conversation_id = %s "
                "AND route = 'customer' AND status = 'replied' AND created_at > %s "
                "AND id <> %s", (turn.conversation_id, pending.created_at, turn.id)
            ).fetchone()[0]
            unanswered = done + 1
            if unanswered < EXPIRY_TURNS and pending.expires_at > ctx.now:
                conn.execute(
                    "UPDATE pending_clarification SET unanswered_turns = %s, "
                    "updated_at = now() WHERE id = %s", (unanswered, pending.id))
                return (self._reask(pending),)
            slots, entries, notices, effects, _ = expire(
                ctx, slots, pending, "unanswered" if unanswered >= EXPIRY_TURNS else "timeout")
            # Always a patch row (also when the location is left as it was): it is the
            # durable record the retry above finds, in the same transaction as the close.
            self._write(conn, turn, version, slots, entries, effects, ctx)
            return tuple(notices)

    def _confirm(self, conn, turn, ctx, slots, pending, sources):
        """A plain "haan" with no tool call confirms the sole fuzzy candidate (same rules
        as Patch.affirmed_candidate); None when it does not apply."""
        ctx.pending = pending
        ctx.asked = self._asked(conn, pending)
        patch = Patch(ctx, slots)
        chosen = patch.affirmed_candidate()
        if chosen is None:
            return None
        text = next(s.text for s in sources if s.text.strip())
        op = {"slot": "location", "op": "set", "raw_span": text.strip()[:200]}
        ev = next(Evidence(s.kind, s.weak) for s in sources if s.text.strip())
        patch._answer(op, ev, pending, chosen, detail="affirmed")
        patch._finalise()
        return patch.merge

    def _reask(self, pending):
        if pending.slot == "purpose":
            return _notice("purpose_ask")
        options = " ya ".join(c["name"] for c in pending.candidates)
        return _notice("location_reask", options=options)


class TurnBinding:
    """The update_requirements tool's view of one customer turn: the DB connection, the
    turn and the message texts the model saw. Each call gets its own call id."""

    def __init__(self, service, conn, turn, texts):
        self.service, self.conn, self.turn, self.texts = service, conn, turn, texts
        self.calls = 0

    def update(self, patch):
        call_id = f"c{self.calls}"
        self.calls += 1
        return self.service.apply(self.conn, self.turn, self.texts, patch, call_id)

    def snapshot(self):
        return self.service.snapshot(self.conn, self.turn)

    def finish(self):
        return self.service.finish_turn(self.conn, self.turn, self.texts)
