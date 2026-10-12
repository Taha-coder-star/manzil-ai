"""Location resolver (Task 7.1, component C7): R17 3.3 steps 1-7.

Sources: R17 section 3 (3.1 output shape, 3.2 context aliases and level labels,
3.3 resolution steps, 3.4 auto vs must-ask table), Data Model (R11-R13) sections
2-3 (hierarchy, alias strategy, normalisation before lookup), Red-Team R-22 /
R-23, the Task 7.1 discovery report and the owner decisions of 2026-10-12
(docs/DECISIONS.md). Design: docs/LOCATION_RESOLVER.md.

The LLM passes a raw span (R-22); this module turns it into canonical gazetteer
IDs or says why it cannot. It never invents an ID: every node and candidate
comes from the Gazetteer snapshot it is given. Nothing reads or writes the
database and nothing is logged here, so the resolver is table-testable. The
trigram score is pg_trgm's similarity(), computed in Python over the snapshot.

Not here: the database loader / cache; the check that the raw span is a
substring of the customer's text and the merge into requirement_state (7.3);
the neighbour ladder and search (8.1); replies (9.1).
"""

import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from types import MappingProxyType

from app import normalise

# --- result contract (R17 3.1) ---

RESOLVED, AMBIGUOUS, UNKNOWN, OUT_OF_COVERAGE = (
    "RESOLVED", "AMBIGUOUS", "UNKNOWN", "OUT_OF_COVERAGE")
STATUSES = frozenset({RESOLVED, AMBIGUOUS, UNKNOWN, OUT_OF_COVERAGE})
EXACT, SUBTREE, ANYWHERE_IN_CITY = "EXACT", "SUBTREE", "ANYWHERE_IN_CITY"
MODES = frozenset({EXACT, SUBTREE, ANYWHERE_IN_CITY})

# How a result was reached (for the Red-Team R-23 echo and alias-review logs).
METHODS = frozenset({
    "exact",               # context-free alias
    "context_alias",       # alias valid only inside the active area
    "context_required",    # context-only alias without its context -> ask
    "area_in_span",        # "DHA phase 8": the span names its own area
    "two_areas",           # "Clifton DHA": two areas in one span -> ask
    "level_in_area",       # bare level + identifier inside the active area
    "not_in_active_area",  # bare level + identifier the active area does not have
    "level_number",        # bare level + identifier, no active area
    "fuzzy",               # trigram match on aliases (R17 3.3 step 4)
    "other_city",          # another city named
    "unsupported_script",  # text left in a script with no gazetteer key
    "empty",               # nothing usable in the span
    "none",                # nothing matched
})

# Score for candidates found by exact / structural rules (they are not ranked);
# fuzzy candidates will carry their trigram score.
STRUCTURAL_SCORE = 1.0

_CITY_CODE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class Candidate:
    location_id: object
    score: float = STRUCTURAL_SCORE

    def __post_init__(self):
        if self.location_id is None:
            raise ValueError("candidate without a location_id")
        if not 0 < self.score <= 1:
            raise ValueError(f"candidate score out of range: {self.score!r}")


@dataclass(frozen=True)
class LocationResult:
    """R17 3.1 location slot value. `nodes` only when RESOLVED, `candidates` only
    when AMBIGUOUS. `raw` is the span exactly as given; `key` is the cleaned form."""
    status: str
    raw: str
    city: str
    nodes: tuple = ()
    mode: str | None = None
    candidates: tuple = ()
    method: str = "none"
    key: str = ""
    area_hint: object = None   # UNKNOWN: the area that was recognised
    other_city: str | None = None  # OUT_OF_COVERAGE: the city named

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        if not isinstance(self.raw, str):
            raise TypeError("raw must be the original span (str)")
        if not isinstance(self.city, str) or not _CITY_CODE.match(self.city):
            raise ValueError(f"city must be a 3-letter code, got {self.city!r}")
        if self.method not in METHODS:
            raise ValueError(f"unknown method {self.method!r}")
        if not isinstance(self.nodes, tuple) or not isinstance(self.candidates, tuple):
            raise TypeError("nodes and candidates must be tuples")
        if any(not isinstance(c, Candidate) for c in self.candidates):
            raise TypeError("candidates must be Candidate objects")
        if len({c.location_id for c in self.candidates}) != len(self.candidates):
            raise ValueError("duplicate candidate")
        if self.status == RESOLVED:
            if not self.nodes or None in self.nodes or self.candidates:
                raise ValueError("RESOLVED needs nodes and no candidates")
            if self.mode not in MODES:
                raise ValueError("RESOLVED needs a mode")
        else:
            if self.nodes or self.mode is not None:
                raise ValueError(f"{self.status} carries no nodes or mode")
            if (self.status == AMBIGUOUS) != bool(self.candidates):
                raise ValueError("candidates exist only (and always) when AMBIGUOUS")
        if (self.status == OUT_OF_COVERAGE) != bool(self.other_city):
            raise ValueError("other_city is set only (and always) for OUT_OF_COVERAGE")
        if self.area_hint is not None and self.status != UNKNOWN:
            raise ValueError("area_hint is only for UNKNOWN")

    def to_dict(self):
        """R17 3.1 keys, plus method / key and the optional hints."""
        out = {"nodes": list(self.nodes), "mode": self.mode, "city": self.city,
               "status": self.status, "raw": self.raw, "method": self.method,
               "key": self.key}
        if self.status == AMBIGUOUS:
            out["candidates"] = [{"location_id": c.location_id, "score": c.score}
                                 for c in self.candidates]
        if self.area_hint is not None:
            out["area_hint"] = self.area_hint
        if self.other_city:
            out["other_city"] = self.other_city
        return out


# ---------------------------------------------------------------------------
# Text cleaning (Data Model 3.2; R17 3.3 step 1). The same function builds the
# alias keys, so both sides always meet in one form.
# ---------------------------------------------------------------------------

# Arabic code points some keyboards type for the Urdu letters.
_LETTER_FOLD = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک"})

# Urdu-script gazetteer terms -> Roman key (Data Model 3.2 step 2), in logical
# Unicode order. Limited coverage (owner decision 2026-10-12): the Data Model's
# four examples, verified by decoding the PDF's visual-order presentation forms
# (reversed + NFKC); dha / phase / clifton also equal the owner-authored bench
# gold (CT-20, CT-21). Anything else in Urdu script stays UNKNOWN
# (unsupported_script). This is not general Urdu or voice-script support.
URDU_TERMS = (
    (("ڈی", "ایچ", "اے"), "dha"),   # U+0688 U+06CC / U+0627 U+06CC U+0686 / U+0627 U+06D2
    (("فیز",), "phase"),             # U+0641 U+06CC U+0632
    (("کلفٹن",), "clifton"),         # U+06A9 U+0644 U+0641 U+0679 U+0646
    (("بحریہ",), "bahria"),          # U+0628 U+062D U+0631 U+06CC U+06C1
)

# Data Model 3.2 step 3, single tokens. "p" is deliberately NOT mapped to
# precinct: R17 3.2 uses "p6" for DHA Phase 6 too, so "p" stays its own token
# and means phase or precinct depending on the area.
TOKEN_CANON = {
    "ph": "phase", "fase": "phase", "phaze": "phase",
    "extension": "ext", "extn": "ext",
    "blk": "block",
    "prec": "precinct",
    "defence": "dha", "defense": "dha", "dfa": "dha",
}
# "Defence City" is not made "dha city" by the generic rule (owner decision
# 2026-10-12): no source says it names DHA City Karachi, so it stays unresolved
# until a verified alias is seeded.
_DEFENCE = frozenset({"defence", "defense", "dfa"})

LEVEL_WORDS = frozenset({"phase", "block", "precinct", "sector"})
P_LABELS = frozenset({"phase", "precinct"})  # what "p" can mean
# Words after which a number word or Roman numeral is a location number.
NUMBER_LEADS = LEVEL_WORDS | {"p", "dha", "clifton", "iqbal", "bahria", "town", "city",
                              "pechs", "nazimabad"}
# Roman numerals only after these (not after "block": North Nazimabad has
# letter blocks A-T, so "block l" / "block i" are letters, not 50 / 1).
ROMAN_LEADS = frozenset({"phase", "precinct", "sector", "dha"})
_ROMAN = re.compile(r"^x{0,3}(?:ix|iv|v?i{0,3})$")
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10}
_ORDINAL = frozenset({"st", "nd", "rd", "th"})
_TOKEN = re.compile(r"[^\W\d_]+|\d+")
_NUMBER_WINDOW = 3  # longest run of words read as one number ("twenty seven")


def _roman(token):
    if not token or not _ROMAN.match(token):
        return None
    total = 0
    for a, b in zip(token, token[1:] + " "):
        value = _ROMAN_VALUES[a]
        total += -value if b != " " and _ROMAN_VALUES[b] > value else value
    return total


def _whole_number(text):
    """An integer location number when `text` is exactly one number expression
    (app.normalise: digits in any script, Roman Urdu / English / Urdu words)."""
    found = normalise.find_numbers(text)
    if len(found) != 1 or found[0].start != 0 or found[0].end != len(text):
        return None
    value = found[0].value
    if value != value.to_integral_value() or not 0 < value < 1000:
        return None
    return int(value)


def _urdu_terms(tokens):
    out, i = [], 0
    while i < len(tokens):
        for words, roman in URDU_TERMS:
            if tuple(tokens[i:i + len(words)]) == words:
                out.append(roman)
                i += len(words)
                break
        else:
            out.append(tokens[i])
            i += 1
    return out


def _phrases(tokens):
    """Multi-token canonicalisation (Data Model 3.2 step 3)."""
    out, i = [], 0
    while i < len(tokens):
        t, nxt = tokens[i], tokens[i + 1] if i + 1 < len(tokens) else None
        if t == "gulshan":
            if nxt == "e":  # "gulshan e <name>": keep, so an unlisted Gulshan-e-X
                out.append(t)  # is never turned into Gulshan-e-Iqbal (R-23)
                i += 1
            else:
                out += ["gulshan", "e", "iqbal"]
                i += 2 if nxt == "iqbal" else 1
        elif (t, nxt) in (("nazimabad", "north"), ("n", "nazimabad"), ("n", "n")):
            out += ["north", "nazimabad"]
            i += 2
        else:
            out.append(t)
            i += 1
    return out


def _numbers(tokens):
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in _ORDINAL and out and out[-1].isdigit():
            i += 1  # "6th" -> 6
            continue
        if t.isdigit():
            out.append(str(_whole_number(t) or t))
            i += 1
            continue
        lead = out[-1] if out else None
        before_ext = i + 1 < len(tokens) and tokens[i + 1] == "ext"
        if lead in ROMAN_LEADS and _roman(t):
            out.append(str(_roman(t)))
            i += 1
            continue
        if lead in NUMBER_LEADS or before_ext:
            for width in range(min(_NUMBER_WINDOW, len(tokens) - i), 0, -1):
                value = _whole_number(" ".join(tokens[i:i + width]))
                if value is not None:
                    out.append(str(value))
                    i += width
                    break
            else:
                out.append(t)
                i += 1
            continue
        out.append(t)
        i += 1
    return out


def clean_tokens(text):
    """Span -> canonical tokens. Deterministic; the raw span is never changed."""
    text = unicodedata.normalize("NFKC", text or "").translate(_LETTER_FOLD).casefold()
    tokens = _urdu_terms(_TOKEN.findall(text))
    tokens = [t if t in _DEFENCE and tokens[i + 1:i + 2] == ["city"]
              else TOKEN_CANON.get(t, t) for i, t in enumerate(tokens)]
    tokens = _numbers(_phrases(tokens))
    # Collapse repeats: "DHA Defence" -> "dha dha" -> "dha".
    return [t for i, t in enumerate(tokens) if i == 0 or t != tokens[i - 1]]


def clean(text):
    return " ".join(clean_tokens(text))


# ---------------------------------------------------------------------------
# Trigram score (R17 3.3 step 4; "trigram fuzzy on aliases", D7)
# ---------------------------------------------------------------------------

AUTO_SCORE = Fraction(85, 100)    # R17 3.3 step 4: score >= 0.85 ...
AUTO_MARGIN = Fraction(15, 100)   # ... and margin >= 0.15 over the next
LENGTH_LIMIT = Fraction(30, 100)  # Red-Team R-23 (b): length difference <= 30 %
# Owner decisions 2026-10-12 (project choices, not source requirements):
CANDIDATE_FLOOR = Fraction(30, 100)  # provisional clarification-candidate floor
MIN_NAME_LETTERS = 4                 # fuzzy needs >= 4 letters of place name
MAX_CANDIDATES = 3                   # R17 3.3 step 4: "AMBIGUOUS (top 3)"


def trigrams(key):
    """pg_trgm's trigram set: lower-cased alphanumeric words, each padded with
    two spaces before and one after, every 3-character window, no repeats."""
    out = set()
    for word in re.findall(r"[^\W_]+", key.lower()):
        padded = f"  {word} "
        out.update(padded[i:i + 3] for i in range(len(padded) - 2))
    return frozenset(out)


def similarity(a, b):
    """pg_trgm similarity(a, b) as an exact fraction: shared / all distinct
    trigrams (PostgreSQL returns the same value as a float4)."""
    ta, tb = trigrams(a), trigrams(b)
    union = ta | tb
    return Fraction(len(ta & tb), len(union)) if union else Fraction(0)


def _is_identifier(tokens, i):
    token = tokens[i]
    after_level = i > 0 and (tokens[i - 1] in LEVEL_WORDS or tokens[i - 1] == "p")
    return token.isdigit() or token == "ext" or (
        after_level and len(token) == 1 and token.isalpha())


def identifiers(tokens):
    """Sub-area identifiers in a token list, compared exactly by the fuzzy step:
    numbers, a single letter after a level word ("block h"), and "ext"."""
    return tuple(sorted(t for i, t in enumerate(tokens) if _is_identifier(tokens, i)))


def structure(tokens):
    """What fuzzy matching never changes: identifiers and level words ("p" too)."""
    levels = tuple(sorted(t for t in tokens if t in LEVEL_WORDS or t == "p"))
    return identifiers(tokens), levels


def name_part(tokens):
    """The place-name tokens: everything but level words, "p" and identifiers.
    Fuzzy scores compare only this text, so shared "block 1" never makes two
    different places look alike."""
    return [t for i, t in enumerate(tokens)
            if not (t in LEVEL_WORDS or t == "p" or _is_identifier(tokens, i))]


def name_letters(tokens):
    """Letters of place name, not counting level words, "p" or identifiers."""
    return sum(len(t) for t in name_part(tokens) if t.isalpha())


@dataclass(frozen=True)
class _FuzzyRow:
    location_id: object
    context_id: object
    shape: tuple       # structure(): identifiers + level words, matched exactly
    name: str          # name_part() text, the only text that is scored
    name_trigrams: frozenset


def _fuzzy_row(tokens, location_id, context_id):
    if name_letters(tokens) < MIN_NAME_LETTERS:
        return None
    text = " ".join(name_part(tokens))
    return _FuzzyRow(location_id, context_id, structure(tokens), text, trigrams(text))


def length_difference(a, b):
    """Red-Team R-23 (b), owner decision: characters without spaces, divided by
    the longer of the two."""
    la, lb = len(a.replace(" ", "")), len(b.replace(" ", ""))
    return Fraction(abs(la - lb), max(la, lb)) if max(la, lb) else Fraction(0)


# ---------------------------------------------------------------------------
# Gazetteer snapshot
# ---------------------------------------------------------------------------

LEVELS = ("city", "area", "sub_area", "zone")


class GazetteerError(ValueError):
    pass


@dataclass(frozen=True)
class Place:
    id: object
    parent_id: object
    level: str
    name: str
    display_name: str
    level_label: str | None = None
    city_code: str | None = None


@dataclass(frozen=True)
class Alias:
    location_id: object
    alias: str
    context_id: object = None
    kind: str = "common"


class Gazetteer:
    """Immutable in-process snapshot of location + location_alias (shared, not
    tenant data). IDs are opaque: UUIDs from the database, dataset keys in tests."""

    def __init__(self, places, aliases):
        self.places = MappingProxyType(self._check_places(places))
        self.aliases = tuple(aliases)
        index, areas, cities = {}, {}, {}
        for alias in self.aliases:
            self._check_alias(alias)
            key = clean(alias.alias)
            if not key:
                raise GazetteerError(f"alias {alias.alias!r} has no usable key")
            index.setdefault(key, set()).add((alias.location_id, alias.context_id))
            level = self.places[alias.location_id].level
            if alias.context_id is None and level == "area":
                areas.setdefault(key, set()).add(alias.location_id)
            if alias.context_id is None and level == "city":
                cities.setdefault(alias.location_id, set()).add(tuple(key.split()))
        self._index = MappingProxyType({k: frozenset(v) for k, v in index.items()})
        self._area_keys = MappingProxyType({k: frozenset(v) for k, v in areas.items()})
        self._city_keys = MappingProxyType({k: frozenset(v) for k, v in cities.items()})
        self._designators = tuple(
            (place.id, place.level_label, self._designator(place))
            for place in self.places.values()
            if place.level_label in LEVEL_WORDS and self._designator(place))
        # Fuzzy keys (internal, never used for exact matching): every alias key plus
        # each location's canonical display name ("North Nazimabad Block H"), which
        # joins the parent and child names without adding aliases to the data.
        keys = {(key, loc, ctx) for key, entries in index.items() for loc, ctx in entries}
        keys |= {(clean(place.display_name), place.id, None)
                 for place in self.places.values() if clean(place.display_name)}
        self._fuzzy_keys = tuple(sorted(keys, key=lambda r: (r[0], str(r[1]), str(r[2]))))
        # Global rows; aliases with no place name ("block 1", "phase 8", "p 27")
        # never take part: the 4-letter name guard applies to both sides.
        self._fuzzy = tuple(row for row in (
            _fuzzy_row(key.split(), loc, ctx) for key, loc, ctx in self._fuzzy_keys) if row)

    @classmethod
    def from_dataset(cls, data):
        """From app.demo_dataset.load(): dataset keys become the IDs."""
        places = [Place(id=row["key"], parent_id=row["parent_key"] or None,
                        level=row["level"], name=row["name"],
                        display_name=row["display_name"] or row["name"],
                        level_label=row["level_label"] or None,
                        city_code=row["city_code"] or None)
                  for row in data["locations.csv"]]
        aliases = [Alias(location_id=row["location_key"], alias=row["alias"],
                         context_id=row["context_key"] or None, kind=row["kind"])
                   for row in data["location_aliases.csv"]]
        return cls(places, aliases)

    @staticmethod
    def _check_places(places):
        out = {}
        for place in places:
            if place.id is None or place.id in out:
                raise GazetteerError(f"duplicate or missing location id {place.id!r}")
            if place.level not in LEVELS:
                raise GazetteerError(f"location {place.id!r}: level {place.level!r}")
            out[place.id] = place
        for place in out.values():
            if (place.parent_id is None) != (place.level == "city"):
                raise GazetteerError(f"location {place.id!r}: parent does not fit its level")
            if place.parent_id is not None and place.parent_id not in out:
                raise GazetteerError(f"location {place.id!r}: unknown parent")
            seen, node = set(), place
            while node.parent_id is not None:
                if node.id in seen:
                    raise GazetteerError(f"location {place.id!r}: cycle")
                seen.add(node.id)
                node = out[node.parent_id]
        return out

    def _check_alias(self, alias):
        if alias.location_id not in self.places:
            raise GazetteerError(f"alias {alias.alias!r}: unknown location")
        if alias.context_id is not None:
            context = self.places.get(alias.context_id)
            if context is None or context.level != "area":
                raise GazetteerError(f"alias {alias.alias!r}: context must be an area")
            if alias.context_id == alias.location_id:
                raise GazetteerError(f"alias {alias.alias!r}: context is the location")

    @staticmethod
    def _designator(place):
        tokens = clean_tokens(place.name)
        if place.level_label in LEVEL_WORDS and tokens[:1] == [place.level_label]:
            return " ".join(tokens[1:])
        return None

    # --- tree helpers ---

    def ancestors(self, location_id):
        """IDs from the node itself up to the city."""
        chain, node = [], self.places.get(location_id)
        while node is not None:
            chain.append(node.id)
            node = self.places.get(node.parent_id)
        return chain

    def is_within(self, location_id, ancestor_id):
        return ancestor_id in self.ancestors(location_id)

    def area_of(self, location_id):
        """The AREA above (or at) a node; None for the city node."""
        if location_id not in self.places:
            raise ValueError("unknown location id")
        for node in self.ancestors(location_id):
            if self.places[node].level == "area":
                return node
        return None

    def mode_of(self, location_id):
        level = self.places[location_id].level
        return {"city": ANYWHERE_IN_CITY, "area": SUBTREE}.get(level, EXACT)

    # --- lookups ---

    def exact(self, key):
        """(location_id, context_id) pairs for a cleaned key."""
        return self._index.get(key, frozenset())

    def area_named(self, key):
        """The single area a context-free alias key names, else None."""
        areas = self._area_keys.get(key, ())
        return next(iter(areas)) if len(areas) == 1 else None

    def city_keys(self, city_code):
        """Token tuples naming the agency's city ("karachi"), for stripping."""
        return frozenset(words for city, keys in self._city_keys.items()
                         if self.places[city].city_code == city_code for words in keys)

    def city_node(self, city_code):
        for place in self.places.values():
            if place.level == "city" and place.city_code == city_code:
                return place.id
        return None

    def fuzzy_entries(self):
        """Global fuzzy rows (internal)."""
        return self._fuzzy

    def fuzzy_keys(self):
        """(key, location_id, context_id): alias keys plus display-name keys."""
        return self._fuzzy_keys

    def by_level(self, label, designator, area_id=None):
        """Numbered / lettered sub-areas: label None = any level, "p" = phase or precinct."""
        labels = P_LABELS if label == "p" else LEVEL_WORDS if label is None else {label}
        return {place_id for place_id, place_label, place_designator in self._designators
                if place_label in labels and place_designator == designator
                and (area_id is None or self.area_of(place_id) == area_id)}


# ---------------------------------------------------------------------------
# Resolution (R17 3.3 steps 1-7; fuzzy, step 4, runs last)
# ---------------------------------------------------------------------------

# Cities outside coverage (R17 3.3 step 6, 3.4; Data Model 3.4, 7): only the
# cities the sources name, matched as whole tokens (owner decision 2026-10-12;
# no expansion without verified fixtures). Others give UNKNOWN, never a guess.
OTHER_CITIES = frozenset({"lahore", "islamabad"})


def _identifier(tokens):
    """A sub-area identifier: a number or a single letter, optionally + "ext"
    ("6", "h", "2 ext"). Identifiers are compared exactly, never approximately."""
    if not tokens or len(tokens) > 2 or tokens[1:] not in ([], ["ext"]):
        return False
    head = tokens[0]
    return head.isdigit() or (len(head) == 1 and head.isascii() and head.isalpha())


def _level_number(tokens, *, bare_number):
    """("block", "6") for "block 6"; ("p", "6") for "p6"; ("block", "h") for
    "block h"; (None, "2 ext") for a bare identifier when `bare_number`."""
    if tokens and tokens[0] in LEVEL_WORDS and _identifier(tokens[1:]):
        return tokens[0], " ".join(tokens[1:])
    if tokens[:1] == ["p"] and tokens[1:2] and tokens[1].isdigit() and _identifier(tokens[1:]):
        return "p", " ".join(tokens[1:])
    if bare_number and tokens and tokens[0].isdigit() and _identifier(tokens):
        return None, " ".join(tokens)
    return None


def _strip_city(tokens, city_keys):
    out, i = [], 0
    while i < len(tokens):
        for words in city_keys:
            if tuple(tokens[i:i + len(words)]) == words:
                i += len(words)
                break
        else:
            out.append(tokens[i])
            i += 1
    return out


def _ordered(gazetteer, ids):
    return sorted(ids, key=lambda i: (gazetteer.places[i].display_name.lower(), str(i)))


class _Outcome:
    """Internal: status + IDs (+ fuzzy scores) before the public result is built."""

    def __init__(self, status, ids=(), method="none", area_hint=None, scores=None):
        self.status, self.ids, self.method, self.area_hint = status, ids, method, area_hint
        self.scores = scores  # location_id -> Fraction, fuzzy only


def _found(ids, resolved_method, ambiguous_method):
    if len(ids) == 1:
        return _Outcome(RESOLVED, ids, resolved_method)
    if ids:
        return _Outcome(AMBIGUOUS, ids, ambiguous_method)
    return None


def _in_active_area(tokens, area, gazetteer):
    """R17 3.3 step 3, active-area scope (owner decision 2026-10-12): a span that
    is only a level + identifier, "p" + number or a bare identifier is read inside
    the active area and nowhere else. This deliberately runs before R17 step 2's
    exact aliases, so a mechanically derived global alias ("block 5" -> Clifton)
    cannot move the customer to another area. Absent there -> UNKNOWN with the
    area kept as area_hint, whether or not the area has that level at all."""
    level = _level_number(tokens, bare_number=True)
    if area is None or level is None:
        return None
    inside = gazetteer.by_level(*level, area)
    inside |= {loc for loc, ctx in gazetteer.exact(" ".join(tokens))
               if (ctx is None or ctx == area) and gazetteer.is_within(loc, area)}
    found = _found(inside, "level_in_area", "level_in_area")
    if found:
        return found
    return _Outcome(UNKNOWN, method="not_in_active_area", area_hint=area)


def _exact(tokens, area, gazetteer):
    """R17 3.3 step 2: context-free aliases, and context aliases of the active area."""
    entries = gazetteer.exact(" ".join(tokens))
    if not entries:
        return None
    usable = {loc for loc, ctx in entries if ctx is None or ctx == area}
    by_context = all(ctx is not None for loc, ctx in entries if loc in usable)
    found = _found(usable, "context_alias" if by_context else "exact", "exact")
    if found:
        return found
    # Only context aliases, none for this area: valid only in context -> ask.
    ids = {loc for loc, _ in entries}
    level = _level_number(tokens, bare_number=False)
    if level:
        ids |= gazetteer.by_level(*level)
    return _Outcome(AMBIGUOUS, ids, "context_required")


def _within_area(rest, area, gazetteer):
    """The rest of a span that named its own area ("DHA" + "phase 8")."""
    other_area = gazetteer.area_named(" ".join(rest))
    if other_area is not None and other_area != area:
        return _Outcome(AMBIGUOUS, {area, other_area}, "two_areas")
    hits = {loc for loc, ctx in gazetteer.exact(" ".join(rest))
            if (ctx is None or ctx == area) and gazetteer.is_within(loc, area)}
    found = _found(hits, "area_in_span", "area_in_span")
    if found:
        return found
    level = _level_number(rest, bare_number=True)
    if level:
        found = _found(gazetteer.by_level(level[0], level[1], area),
                       "area_in_span", "area_in_span")
        if found:
            return found
    # Scoped fuzzy (owner approval 2026-10-12): only this area's own children,
    # with exact identifiers; never the area itself and never another area.
    return _fuzzy_within(rest, area, gazetteer) or _Outcome(
        UNKNOWN, method="none", area_hint=area)


def _area_in_span(tokens, gazetteer):
    """Longest area name at the start, then at the end, of the span."""
    for n in range(len(tokens) - 1, 0, -1):
        for head, rest in ((tokens[:n], tokens[n:]), (tokens[-n:], tokens[:-n])):
            area = gazetteer.area_named(" ".join(head))
            if area is not None:
                return _within_area(rest, area, gazetteer)
    return None


def _bare_level(tokens, gazetteer):
    """R17 3.3 step 3 without an active area: one area -> resolved, more -> ask."""
    level = _level_number(tokens, bare_number=False)
    if level is None:
        return None
    return _found(gazetteer.by_level(*level), "level_number", "level_number")


def _fuzzy_query(tokens):
    """(name text, shape) of a span, or None when fuzzy must not run: non-Latin
    text, fewer than MIN_NAME_LETTERS letters of name, or the unresolved
    "Defence City" (owner decision: never offered as DHA City)."""
    if not " ".join(tokens).isascii() or name_letters(tokens) < MIN_NAME_LETTERS:
        return None
    if any(a in _DEFENCE and b == "city" for a, b in pairwise(tokens)):
        return None
    return " ".join(name_part(tokens)), structure(tokens)


def _rank(query, rows, gazetteer):
    """R17 3.3 step 4 over eligible rows (owner decisions 2026-10-12):

    - only rows with exactly the span's shape (identifiers, level words): an
      absent child never becomes another child, an explicit one never its parent;
    - score = pg_trgm similarity of the place-name text only;
    - each location keeps its best row; margin = best - best of the next
      different location (0 if none); ties by display name, then ID;
    - RESOLVED with score >= 0.85, margin >= 0.15 and the winning row within the
      character length rule; else AMBIGUOUS, top 3 at or above the 0.30 floor;
      else None (UNKNOWN)."""
    name, shape = query
    name_trigrams = trigrams(name)
    best = {}  # location -> (score, length_ok)
    for row in rows:
        if row.shape != shape:
            continue
        union = name_trigrams | row.name_trigrams
        score = Fraction(len(name_trigrams & row.name_trigrams), len(union))
        if score == 0:
            continue
        candidate = (score, length_difference(name, row.name) <= LENGTH_LIMIT)
        best[row.location_id] = max(best.get(row.location_id, candidate), candidate)
    ranked = sorted(best.items(), key=lambda item: (
        -item[1][0], gazetteer.places[item[0]].display_name.lower(), str(item[0])))
    if not ranked:
        return None
    (top, (score, length_ok)) = ranked[0]
    margin = score - (ranked[1][1][0] if len(ranked) > 1 else 0)
    if score >= AUTO_SCORE and margin >= AUTO_MARGIN and length_ok:
        return _Outcome(RESOLVED, (top,), "fuzzy", scores={top: score})
    shortlist = [(loc, s) for loc, (s, _) in ranked[:MAX_CANDIDATES] if s >= CANDIDATE_FLOOR]
    if shortlist:
        return _Outcome(AMBIGUOUS, tuple(loc for loc, _ in shortlist), "fuzzy",
                        scores=dict(shortlist))
    return None


def _fuzzy(tokens, area, gazetteer):
    """Global fuzzy: runs only when no deterministic step matched anything. Rows
    are alias keys (context-free, or of the active area) and display names."""
    query = _fuzzy_query(tokens)
    if query is None:
        return None
    rows = [row for row in gazetteer.fuzzy_entries()
            if row.context_id is None or row.context_id == area]
    return _rank(query, rows, gazetteer)


def _without_area(tokens, area, gazetteer):
    """Drop the longest leading or trailing name of `area` ("bahria sports city"
    -> "sports city" under Bahria Town Karachi)."""
    for n in range(len(tokens), 0, -1):
        if gazetteer.area_named(" ".join(tokens[:n])) == area:
            return tokens[n:]
        if gazetteer.area_named(" ".join(tokens[-n:])) == area:
            return tokens[:-n]
    return tokens


def _fuzzy_within(rest, area, gazetteer):
    """Scoped fuzzy below a recognised parent area: the span's remaining text is
    compared with the area's own descendants' keys, the area's name removed from
    them too. Never the area itself, never another area."""
    query = _fuzzy_query(rest)
    if query is None:
        return None
    rows = []
    for key, location, context in gazetteer.fuzzy_keys():
        if location == area or not gazetteer.is_within(location, area) \
                or context not in (None, area):
            continue
        row = _fuzzy_row(_without_area(key.split(), area, gazetteer), location, context)
        if row:
            rows.append(row)
    return _rank(query, rows, gazetteer)


def _resolve_tokens(tokens, area, gazetteer):
    for step in (lambda: _in_active_area(tokens, area, gazetteer),
                 lambda: _exact(tokens, area, gazetteer),
                 lambda: _area_in_span(tokens, gazetteer),
                 lambda: _bare_level(tokens, gazetteer)):
        outcome = step()
        if outcome is not None:
            return outcome
    return None


def resolve(raw_span, active_area_id, gazetteer, *, city_code="KHI"):
    """Resolve one raw location span against a gazetteer snapshot.

    raw_span: the customer's words exactly as passed by the LLM. The caller
        (7.3) has already checked it is a substring of the message (R-22).
    active_area_id: any node of the current requirement location, or None; the
        area above it is the context for context aliases and bare numbers.
    city_code: the agency's city_default (agency, migration 007).
    """
    if not isinstance(raw_span, str):
        raise TypeError("raw_span must be a str")
    area = gazetteer.area_of(active_area_id) if active_area_id is not None else None
    tokens = clean_tokens(raw_span)
    key = " ".join(tokens)

    def result(status, method, ids=(), area_hint=None, other_city=None, scores=None):
        # Fuzzy candidates keep their ranked order and scores; others sort by name.
        ids = tuple(ids) if scores else tuple(_ordered(gazetteer, ids))
        if status == RESOLVED:
            return LocationResult(status, raw_span, city_code, nodes=ids,
                                  mode=gazetteer.mode_of(ids[0]), method=method, key=key)
        candidates = tuple(
            Candidate(i, float(scores[i]) if scores else STRUCTURAL_SCORE)
            for i in ids) if status == AMBIGUOUS else ()
        return LocationResult(status, raw_span, city_code, candidates=candidates,
                              method=method, key=key, area_hint=area_hint,
                              other_city=other_city)

    if not tokens:
        return result(UNKNOWN, "empty")
    # 1. Active-area scope (names no area or city, so it can hide no other city).
    # 2. A full-span local alias is authoritative (R17 step 2 precedes step 6),
    #    e.g. "DHA City Karachi"; only a whole-span alias can contain a city word.
    outcome = _in_active_area(tokens, area, gazetteer) or _exact(tokens, area, gazetteer)
    if outcome is None:
        # 3. Another city named as a whole token: before any partial local match,
        #    so "Bahria Lahore" is never Bahria Town Karachi.
        named = [t for t in tokens if t in OTHER_CITIES]
        if named:
            return result(OUT_OF_COVERAGE, "other_city", other_city=named[0])
        # 4. The agency city is dropped ("Clifton Karachi" -> clifton) unless it is
        #    all there is ("Karachi" matched above as the city node).
        tokens = _strip_city(tokens, gazetteer.city_keys(city_code)) or tokens
        # 5. Deterministic steps on the rest, then (only if nothing matched) fuzzy.
        outcome = _resolve_tokens(tokens, area, gazetteer) or _fuzzy(tokens, area, gazetteer)
    if outcome is not None:
        return result(outcome.status, outcome.method, outcome.ids, outcome.area_hint,
                      scores=outcome.scores)

    if not key.isascii():
        return result(UNKNOWN, "unsupported_script")
    return result(UNKNOWN, "none")
