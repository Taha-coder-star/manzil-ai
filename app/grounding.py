"""Grounding validator (Task 9.1, C11; Red-Team R-21, R-05, R-06; docs/RENDERER.md).

Customer-facing property facts are rendered by templates from DB rows (app.render). This
module checks any text against the facts that were actually rendered (Facts) and
rejects what they do not support:

- money in digits or number words ("7.5 crore", "saade saat crore", Urdu script), with
  the shared normalisers (app.normalise.find_numbers): every amount must be a rendered
  price, a stated budget or the no-match lowest price;
- sizes and their units ("500 gaz" is fine for a 500 sq yd house, "500 sq ft" is not);
- counts: "#n" positions and bed / bath numbers;
- listing codes no tool supplied;
- place names that are neither rendered nor an ancestor of a rendered place;
- the feature vocabulary of migration 007 (features, facing, corner, furnishing,
  possession, construction): a claim must be true in a rendered row; absence is unknown,
  never false, so a negative claim ("gas nahi") is unsupported too;
- prohibited promises ("guarantee", "final price", "mil jayega", ...; R-06).

Bare small numbers that are not money, sizes, positions or room counts ("Phase 7",
"2 options") are not checked: they cannot state a price, size or count by themselves.

ground_wrapper is the seam for an optional model-written wrapper line: validate,
regenerate once with the violations, then fall back to the template (R-21). The demo
keeps the wrapper templated (owner decision 2026-10-14), so nothing calls it live yet.
Never log the checked text: it is customer-facing message content.
"""

import functools
import re
from dataclasses import dataclass, field
from decimal import Decimal

from app import location, normalise
from app.cards import price_text

LISTING_CODE = re.compile(r"\b[A-Z0-9]{2,4}-[0-9]{3}\b")  # = turn_engine.LISTING_CODE
POSITION = re.compile(r"#\s*(\d+)")
PAIRED = re.compile(r"#\s*(\d+)\W{0,3}(" + LISTING_CODE.pattern + ")")
_WORD = r"(?![^\W\d_])"
BEDS_AFTER = re.compile(r"\s*(?:-\s*)?(?:bed(?:room)?s?|bd|kamr[ae]y?|kamra|rooms?)" + _WORD,
                        re.IGNORECASE)
BATHS_AFTER = re.compile(r"\s*(?:-\s*)?(?:bath(?:room)?s?|washrooms?|toilets?)" + _WORD,
                         re.IGNORECASE)
LARGE_NUMBER = 1000  # a bare number this big could be an amount or a size: checked

# Prohibited promises (R-06), English / Roman Urdu / Urdu script.
PROMISES = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bguarant(?:ee|eed|ees|y)\b",
    r"\bfinal\s+(?:price|rate|qeemat)\b",
    r"\bmil\s+ja(?:y|)e?(?:ga|gi|nge)\b",
    r"\bzaroor\s+mil",
    r"\bpakk[aie]\b",
    r"\barrange\s+ho\s+ja(?:y|)e?(?:ga|gi)\b",
    r"\bho\s+ja(?:y|)e?ga\s+deal\b",
    r"\b100\s*%|\bsau\s+fee?\s*sad\b",
    r"\bdiscount\b",
    r"\b(?:price|qeemat|rate)\s+kam\s+ho\s+ja(?:y|)e?(?:ga|gi)\b",
    r"\bdefinitely\s+(?:available|yours)\b",
    r"\bconfirm(?:ed)?\s+(?:deal|booking)\b",
    r"گارنٹی", r"ضرور\s*مل", r"مل\s*جائے\s*گ", r"پکا", r"پکی", r"فائنل",
))

# Feature vocabulary (migration 007 enums). Each phrase claims one token; a rendered
# listing supports the token when its row has it.
FEATURE_TERMS = (
    ("unfurnished", r"\bun-?furnished\b"),
    ("semi", r"\bsemi[- ]?furnished\b"),
    ("furnished", r"\bfurnished\b|\bfurniture\b"),
    ("basement", r"\bbasement\b|\btehkhana\b"),
    ("annex", r"\bannexe?\b"),
    ("servant_quarter", r"\bservant\s*(?:quarters?|rooms?)\b"),
    ("lift", r"\blifts?\b|\belevators?\b"),
    ("generator_backup", r"\bgenerators?\b|\b(?:power\s+)?backup\b"),
    ("solar", r"\bsolar\b"),
    ("gas_connection", r"\b(?:sui\s+)?gas\b"),
    ("water_kwsb", r"\bkwsb\b|\bwater\s+board\b"),
    ("water_boring", r"\bboring\b"),
    ("security", r"\bsecurity\b|\bguards?\b|\bgated\b"),
    ("lawn", r"\blawns?\b|\bgardens?\b"),
    ("roof_access", r"\broof\b|\bchhat\b"),
    ("sea", r"\bsea\b|\bsamandar\b|\bsamundar\b"),
    ("main_boulevard", r"\bboulevard\b"),
    ("drawing_dining", r"\bdrawing\b|\bdining\b"),
    ("west_open", r"\bwest\b|\bmaghrib\b"),
    ("east_open", r"\beast\b|\bmashriq\b"),
    ("north_open", r"\bnorth\b|\bshumal\b"),
    ("south_open", r"\bsouth\b|\bjunoob\b"),
    ("park_facing", r"\bpark\b"),
    ("road_facing", r"\broad\s+facing\b|\bfacing\s+(?:the\s+)?road\b"),
    ("corner", r"\bcorner\b"),
    ("possession", r"\bpossession\b|\bqabz[ae]\b"),
    ("ready", r"\bready\b|\btayyar\b"),
    ("under_construction", r"\bunder\s+construction\b|\bzer-?e-?tameer\b"),
    ("grey_structure", r"\bgr[ae]y\s+structure\b"),
    ("off_plan", r"\boff-?plan\b"),
)
_FEATURE_RE = tuple((token, re.compile(p, re.IGNORECASE)) for token, p in FEATURE_TERMS)
# A row value -> the vocabulary token it supports.
ROW_TOKENS = {"sea_view": "sea", "sea_facing": "sea", "on_ground": "possession"}
NEGATION = re.compile(r"^\W*(?:\w+\W+){0,2}?(?:nahi|nahin|nai|not|no)\b", re.IGNORECASE)
NEGATION_BEFORE = re.compile(r"\b(?:no|not|without|baghair|bina)\W+(?:\w+\W+)?$",
                             re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    kind: str    # money | area | unit | count | position | code | place | feature | promise
    text: str    # the offending span
    detail: str = ""


@dataclass(frozen=True)
class Facts:
    """What a customer text may state: only values that were rendered from DB rows (or
    stated by the customer), never anything taken from model output."""
    codes: frozenset = frozenset()
    money: frozenset = frozenset()       # int PKR
    areas: frozenset = frozenset()       # (Decimal value, unit)
    beds: frozenset = frozenset()
    baths: frozenset = frozenset()
    positions: frozenset = frozenset()
    places: frozenset = frozenset()      # gazetteer ids, with their ancestors
    features: frozenset = frozenset()    # vocabulary tokens true in a rendered row
    false_features: frozenset = frozenset()  # tokens explicitly false (is_corner = false)
    by_code: dict = field(default_factory=dict, compare=False)  # code -> that row's Facts
    position_codes: dict = field(default_factory=dict, compare=False)  # n -> code

    def merge(self, other):
        merged = {f: getattr(self, f) | getattr(other, f) for f in SET_FIELDS + ("positions",)}
        return Facts(**merged, by_code={**self.by_code, **other.by_code},
                     position_codes={**self.position_codes, **other.position_codes})


SET_FIELDS = ("codes", "money", "areas", "beds", "baths", "places", "features",
              "false_features")


def _dec(value):
    value = Decimal(value)
    return value.quantize(Decimal(1)) if value == value.to_integral() else value.normalize()


def _amounts(values):
    """Each amount, plus the value its rendered form reads as ("PKR 1.23 crore")."""
    out = set()
    for value in values:
        if value is None:
            continue
        out.add(int(value))
        out.update(int(m.value) for m in normalise.find_money(price_text(int(value), None)))
    return out


def place_ids(gazetteer, display_name):
    """Gazetteer ids whose display name (or name) is this rendered location text."""
    if gazetteer is None or not display_name:
        return set()
    return {p.id for p in gazetteer.places.values()
            if display_name in (p.display_name, p.name)}


def with_ancestors(gazetteer, ids):
    """The ids and their ancestors, as gazetteer keys (slots store ids as text)."""
    if gazetteer is None:
        return set(ids)
    keys, out = {str(k): k for k in gazetteer.places}, set()
    for place in ids:
        out.update(gazetteer.ancestors(keys.get(str(place), place)))
    return out


def facts_for(listings, *, positions=(), request=None, gazetteer=None, extra_money=(),
              extra_places=()):
    """Facts of rendered listings (app.cards.Listing), their positions (n of each listing,
    in order), the customer's request (app.search.Request: budget, size, places) and
    extra amounts (the no-match lowest price). Each listing's own facts are kept too: a
    value stated after its code or "#n" must be that listing's (by_code)."""
    listings, positions = list(listings), list(positions)
    whole = _facts(listings, request, gazetteer, extra_money, extra_places)
    return Facts(
        **{f: getattr(whole, f) for f in SET_FIELDS}, positions=frozenset(positions),
        by_code={item.code: _facts([item], None, gazetteer, (), ()) for item in listings},
        position_codes=dict(zip(positions, (item.code for item in listings), strict=False)),
    )


def _facts(listings, request, gazetteer, extra_money, extra_places):
    money, areas, places, tokens, false = set(), set(), set(extra_places), set(), set()
    for item in listings:
        if item.area_value is not None:
            areas.add((_dec(item.area_value), item.area_unit))
        if item.size_nominal_sqyd is not None:
            areas.add((_dec(item.size_nominal_sqyd), "sq_yd"))
        places |= place_ids(gazetteer, item.location)
        for value in (*item.features, *item.facing, item.furnishing, item.possession_status,
                      item.construction_status):
            if value:
                tokens.add(ROW_TOKENS.get(value, value))
        if item.furnishing == "semi":
            tokens.add("furnished")
        if item.is_corner:
            tokens.add("corner")
        elif item.is_corner is False:
            false.add("corner")
        if item.furnishing == "unfurnished":
            false.add("furnished")
    if request is not None:
        money.update(v for v in (request.budget_min, request.budget_max, request.stated) if v)
        size = request.size or {}
        if size.get("value") is not None and size.get("unit"):
            areas.add((_dec(size["value"]), size["unit"]))
        if size.get("nominal_sqyd") is not None:
            areas.add((_dec(size["nominal_sqyd"]), "sq_yd"))
        places.update(request.nodes)
    money = _amounts([*(item.price_pkr for item in listings), *money, *extra_money])
    return Facts(
        codes=frozenset(item.code for item in listings),
        money=frozenset(money), areas=frozenset(areas),
        beds=frozenset(item.bedrooms for item in listings if item.bedrooms is not None),
        baths=frozenset(item.bathrooms for item in listings if item.bathrooms is not None),
        places=frozenset(with_ancestors(gazetteer, places)),
        features=frozenset(tokens), false_features=frozenset(false),
    )


# --- checks ------------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _place_index(gazetteer):
    """Cleaned key -> gazetteer ids: aliases, display names and names."""
    index = {}
    for key, place, _ in gazetteer.fuzzy_keys():
        index.setdefault(key, set()).add(place)
    for place in gazetteer.places.values():
        for name in (place.name, place.display_name):
            index.setdefault(location.clean(name), set()).add(place.id)
    # A key that is only numbers or one letter names nothing on its own.
    return {k: frozenset(v) for k, v in index.items()
            if k and not all(t.isdigit() or len(t) < 2 for t in k.split())}


def place_mentions(text, gazetteer):
    """[(key, ids)] for the place names in text, longest match first."""
    index = _place_index(gazetteer)
    longest = max((len(k.split()) for k in index), default=0)
    tokens, out, i = location.clean_tokens(text), [], 0
    while i < len(tokens):
        for n in range(min(longest, len(tokens) - i), 0, -1):
            key = " ".join(tokens[i:i + n])
            if key in index:
                out.append((key, index[key]))
                i += n
                break
        else:
            i += 1
    return out


def _references(text, facts):
    """[(start, code)] of the listing references in text: codes, and "#n" of a rendered
    position. A value after a reference (until the next one) is about that listing."""
    refs = [(m.start(), m.group()) for m in LISTING_CODE.finditer(text)]
    refs += [(m.start(), facts.position_codes[int(m.group(1))])
             for m in POSITION.finditer(text) if int(m.group(1)) in facts.position_codes]
    return sorted(refs)


def _scope(refs, facts, at):
    """The facts a value at offset `at` is checked against: the referenced listing's,
    or all rendered facts before any reference."""
    code = None
    for start, ref in refs:
        if start >= at:
            break
        code = ref
    return facts.by_code.get(code, facts) if code is not None else facts


def _check_numbers(text, facts, out):
    refs = _references(text, facts)
    masked = LISTING_CODE.sub(lambda m: " " * len(m.group()), text)
    for match in normalise.find_numbers(masked):
        scope = _scope(refs, facts, match.start)
        if masked[:match.start].rstrip().endswith("#"):
            if int(match.value) not in facts.positions:
                out.append(Violation("position", f"#{match.text}", "no such position"))
        elif match.kind == "money":
            value = int(match.value)
            if value not in scope.money:
                out.append(Violation("money", match.text, f"{value} PKR is not a rendered "
                                     "price or the stated budget"))
        elif match.kind == "area":
            value = _dec(match.value)
            if (value, match.unit) in scope.areas:
                continue
            units = {unit for v, unit in scope.areas if v == value}
            out.append(Violation("unit" if units else "area", match.text,
                                 f"{value} {match.unit} is not a rendered size"))
        else:
            value = match.value
            if BEDS_AFTER.match(masked, match.end):
                if int(value) not in scope.beds:
                    out.append(Violation("count", match.text, "bedrooms not rendered"))
            elif BATHS_AFTER.match(masked, match.end):
                if int(value) not in scope.baths:
                    out.append(Violation("count", match.text, "bathrooms not rendered"))
            elif value >= LARGE_NUMBER and int(value) not in scope.money and not any(
                    value == v for v, _ in scope.areas):
                out.append(Violation("money", match.text, "an unsupported large number"))


def _check_features(text, facts, out):
    refs, taken = _references(text, facts), []
    for token, pattern in _FEATURE_RE:
        for match in pattern.finditer(text):
            if any(s <= match.start() < e for s, e in taken):
                continue  # "unfurnished" is not also "furnished"
            taken.append(match.span())
            scope = _scope(refs, facts, match.start())
            negated = bool(NEGATION.match(text[match.end():])
                           or NEGATION_BEFORE.search(text[:match.start()]))
            if negated:
                if token not in scope.false_features:
                    out.append(Violation("feature", match.group(), f"'not {token}' is not "
                                         "a known fact (absent means unknown)"))
            elif token not in scope.features:
                out.append(Violation("feature", match.group(), f"{token} is not true in a "
                                     "rendered listing"))


def validate(text, facts, gazetteer=None):
    """The violations of one customer-facing text against the rendered facts; [] = ok."""
    text = text or ""
    out = []
    for code in LISTING_CODE.findall(text):
        if code not in facts.codes:
            out.append(Violation("code", code, "listing code no tool supplied"))
    for match in PAIRED.finditer(text):  # "#2 DH6-101": the ID echo must match position 2
        n, code = int(match.group(1)), match.group(2)
        if n in facts.position_codes and facts.position_codes[n] != code:
            out.append(Violation("position", match.group(),
                                 f"#{n} is {facts.position_codes[n]}"))
    _check_numbers(text, facts, out)
    if gazetteer is not None:
        for key, ids in place_mentions(text, gazetteer):
            if not ids & facts.places:
                out.append(Violation("place", key, "place not rendered"))
    _check_features(text, facts, out)
    for pattern in PROMISES:
        for match in pattern.finditer(text):
            out.append(Violation("promise", match.group(), "prohibited promise (R-06)"))
    return out


# --- payloads ------------------------------------------------------------------------------

def payload_texts(payload):
    """Every customer-visible text of a built WhatsApp payload (bodies, headers, footers,
    button and row titles, card bodies, captions). whatsapp.summary reads only the body."""
    kind = payload.get("type")
    if kind == "text":
        return [payload["text"]["body"]]
    if kind == "image":
        return [payload["image"].get("caption") or ""]
    if kind != "interactive":
        return []
    inter = payload["interactive"]
    texts = [inter.get("body", {}).get("text", ""), inter.get("footer", {}).get("text", "")]
    header = inter.get("header") or {}
    if header.get("type") == "text":
        texts.append(header.get("text", ""))
    action = inter.get("action", {})
    texts += [b["reply"]["title"] for b in action.get("buttons", []) if "reply" in b]
    for section in action.get("sections", []):
        texts.append(section.get("title", ""))
        for row in section["rows"]:
            texts += [row["title"], row.get("description", "")]
    for card in action.get("cards", []):
        texts.append((card.get("body") or {}).get("text", ""))
        texts += [b["quick_reply"]["title"] for b in card["action"]["buttons"]]
    if action.get("button"):
        texts.append(action["button"])
    return [t for t in texts if t]


def check_payloads(payloads, facts, gazetteer=None):
    """Violations across all texts of the payloads (the R-21 last line of defence)."""
    return [v for payload in payloads for text in payload_texts(payload)
            for v in validate(text, facts, gazetteer)]


# --- optional wrapper: regenerate once, then the template --------------------------------

@dataclass(frozen=True)
class Grounded:
    text: str
    outcome: str            # ok | regenerated | template_only
    attempts: int
    violations: tuple = ()  # of the last rejected candidate


def ground_wrapper(generate, facts, fallback, gazetteer=None):
    """generate(violations_or_None) -> candidate text. Validate it; on a violation ask
    once more with the violations; if that fails too (or generate raises), the
    template fallback is used (R-21: strip and regenerate once -> template-only)."""
    rejected = ()
    for attempt in (1, 2):
        try:
            candidate = generate(rejected or None)
        except Exception:  # noqa: BLE001 - any generator failure means the template
            return Grounded(fallback, "template_only", attempt, rejected)
        found = tuple(validate(candidate, facts, gazetteer))
        if not found:
            return Grounded(candidate, "ok" if attempt == 1 else "regenerated", attempt,
                            rejected)
        rejected = found
    return Grounded(fallback, "template_only", 2, rejected)
