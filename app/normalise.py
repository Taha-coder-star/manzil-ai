"""Deterministic normalisers for money, area, budget and purpose (Task 6.2, C7).

Sources: Data Model (R11-R13) section 4 (units, money, budget, purpose), R17
section 4 (state and search behaviour), CLAUDE.md section 6, docs/BENCHMARK.md
(the rules used for the gold values) and the owner decisions of 2026-10-12
(docs/DECISIONS.md). Design: docs/NORMALISATION.md.

The LLM passes raw spans (R-22); everything here turns those spans into canonical
values with no model involved. Nothing reads or writes the database. The same
scanners (find_money / find_areas) serve the Task 9.1 grounding validator, which
must recognise every way a number can be written (R-21).

Every parse result carries a status: 'ok', 'ambiguous' (a value cannot be
confidently converted; the caller asks) or 'none' (nothing found). Nothing is
guessed: a missing unit that the property type does not settle, two amounts in
one budget span, or an amount between the rent and sale thresholds stay open.

Out of scope here (later tasks): location resolution (7.1), merging into
requirement_state, the buy <-> rent previous_budget rule and the "cheaper"
ladder rule (7.3), and search tolerances (price x 1.05 / x 0.9, size +/-10 / 15 %;
8.1).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

# --- units (Data Model 4.1) ---
DEFAULT_MARLA_SQFT = Decimal(225)  # agency.marla_sqft (migration 007): 225 or 272.25
SQFT_PER_SQYD = Decimal(9)
SQFT_PER_ACRE = Decimal(43560)
MARLA_PER_KANAL = 20
SQYD_TYPES = frozenset({"house", "plot", "portion"})
SQFT_TYPES = frozenset({"apartment", "penthouse", "shop", "office", "showroom",
                        "warehouse", "building", "floor"})
MAX_DEFAULT_SQYD = 2000  # house / plot / portion without a unit, up to this -> sq yd
NOMINAL_BUCKETS_SQYD = (120, 125, 160, 200, 235, 240, 250, 300, 350, 400, 500, 600,
                        1000, 2000)
BUCKET_TOLERANCE = Decimal("0.10")

# --- property categories (migration 007 property_type values) ---
RESIDENTIAL = frozenset({"house", "portion", "apartment", "penthouse", "plot"})
COMMERCIAL = frozenset({"shop", "office", "showroom", "warehouse", "building", "floor"})
_TYPE_ALIASES = {"flat": "apartment", "ghar": "house", "makaan": "house",
                 "dukaan": "shop", "dukan": "shop"}

# --- rent / sale inference by amount (Data Model 4.4; commercial sale: owner 2026-10-12)
LAKH = 100_000
CRORE = 10_000_000
RENT_MAX = {"residential": 15 * LAKH, "commercial": 25 * LAKH}
SALE_MIN = {"residential": 50 * LAKH, "commercial": 1 * CRORE}

# --- budget (Data Model 4.3; bare amount / "budget X" = around: owner 2026-10-12) ---
AROUND = Decimal("0.10")
CHEAPER_FACTOR = Decimal("0.97")
HIGHER_FACTOR = Decimal("1.10")
PREMIUM_FACTOR = Decimal("1.03")

OK, AMBIGUOUS, NONE = "ok", "ambiguous", "none"


# ---------------------------------------------------------------------------
# Number vocabulary
# ---------------------------------------------------------------------------

def _words(table):
    out = {}
    for value, spellings in table.items():
        for spelling in spellings.split():
            out[spelling] = Decimal(value)
    return out


# Roman Urdu 1-99 (common spellings; voice transcripts need them, Data Model 4.2).
ROMAN_NUMBERS = _words({
    1: "ek aik", 2: "do dou", 3: "teen", 4: "chaar char chār", 5: "paanch panch",
    6: "chhe chhay che chay chhah chhey chhai", 7: "saat", 8: "aath aat", 9: "nau",
    10: "das dus", 11: "gyarah gyara giyarah", 12: "baarah barah", 13: "terah tera",
    14: "chaudah chauda chodah", 15: "pandrah pandra", 16: "solah sola",
    17: "satrah satra", 18: "atharah athara attharah", 19: "unees unnees", 20: "bees",
    21: "ikkees", 22: "baees bais", 23: "teis taees", 24: "chaubees chobees",
    25: "pachees pachchees pachis", 26: "chhabbees chabbees", 27: "sattaees sattais",
    28: "athaees atthaees", 29: "untees", 30: "tees", 31: "iktees", 32: "battees",
    33: "taintees", 34: "chauntees", 35: "paintees", 36: "chhattees", 37: "saintees",
    38: "artees", 39: "untaalees", 40: "chaalees chalees", 41: "iktaalees",
    42: "bayaalees", 43: "taintaalees", 44: "chawaalees", 45: "paintaalees",
    46: "chhiyaalees", 47: "saintaalees", 48: "artaalees", 49: "unchaas",
    50: "pachaas pachas", 51: "ikyaavan", 52: "baavan", 53: "tirpan", 54: "chauvan",
    55: "pachpan", 56: "chhappan", 57: "sattaavan", 58: "atthaavan", 59: "unsath",
    60: "saath sath", 61: "iksath", 62: "baasath", 63: "tirsath", 64: "chausath",
    65: "painsath", 66: "chhiyaasath", 67: "sarsath", 68: "arsath", 69: "unhattar",
    70: "sattar", 71: "ikhattar", 72: "bahattar", 73: "tihattar", 74: "chauhattar",
    75: "pachhattar", 76: "chhihattar", 77: "satattar", 78: "athattar", 79: "unaasi",
    80: "assi assee", 81: "ikyaasi", 82: "bayaasi", 83: "tiraasi", 84: "chauraasi",
    85: "pachaasi", 86: "chhiyaasi", 87: "sataasi", 88: "athaasi", 89: "navaasi",
    90: "nabbe nabbay navve", 91: "ikyaanve", 92: "baanve", 93: "tiraanve",
    94: "chauraanve", 95: "pachaanve", 96: "chhiyaanve", 97: "sattaanve",
    98: "atthaanve", 99: "ninyaanve",
})
ENGLISH_UNITS = _words({
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
    8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen",
    14: "fourteen", 15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
    19: "nineteen",
})
ENGLISH_TENS = _words({20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty",
                       70: "seventy", 80: "eighty", 90: "ninety"})
URDU_NUMBERS = _words({1: "ایک", 2: "دو", 3: "تین", 4: "چار", 5: "پانچ", 6: "چھ",
                       7: "سات", 8: "آٹھ", 9: "نو", 10: "دس", 20: "بیس", 25: "پچیس",
                       50: "پچاس", 80: "اسی"})
NUMBER_WORDS = {**ROMAN_NUMBERS, **ENGLISH_UNITS, **ENGLISH_TENS, **URDU_NUMBERS}

# Fractions (Data Model 4.2): standalone values and prefixes applied to the next number.
STANDALONE = _words({"0.5": "aadha adha", "1.5": "dedh derh deadh dairh ڈیڑھ",
                     "2.5": "dhai dhaai dhaee adhai arhai ڈھائی"})
PREFIXES = {
    **dict.fromkeys(("sawa", "sava", "swa", "سوا"), Decimal("0.25")),
    **dict.fromkeys(("saade", "saadhe", "sade", "sadhe", "sarhe", "saare", "saarhe",
                     "saadey", "ساڑھے"), Decimal("0.5")),
    **dict.fromkeys(("paune", "pone", "paunay", "پونے"), Decimal("-0.25")),
}

# Multipliers. Money: thousand and up. sau / hundred also build sizes ("chaar sau gaz").
HUNDRED = frozenset({"sau", "hundred", "hundreds", "سو"})
THOUSAND = frozenset({"hazar", "hazaar", "hzar", "thousand", "thousands", "k",
                      "ہزار"})
BIG = {
    **dict.fromkeys(("lakh", "lakhs", "lac", "lacs", "lak", "laakh", "l", "لاکھ"),
                    Decimal(LAKH)),
    **dict.fromkeys(("million", "millions", "mn", "mil", "m"), Decimal(1_000_000)),
    **dict.fromkeys(("crore", "crores", "cr", "crs", "karor", "karod", "karore", "caror",
                     "کروڑ"), Decimal(CRORE)),
    **dict.fromkeys(("arab", "ارب"), Decimal(1_000_000_000)),
}
# Single-letter multipliers count only when glued to digits ("50k", "5L", "2m").
GLUED_ONLY = frozenset({"k", "l", "m"})
_CURRENCY = r"(?:rs\.?|pkr|rupees?|rupay[ea]?|روپے)"
CURRENCY_AFTER = re.compile(r"\s*" + _CURRENCY + r"(?![^\W\d_])", re.IGNORECASE)
CURRENCY_BEFORE = re.compile(r"(?<![^\W\d_])" + _CURRENCY + r"\s*$", re.IGNORECASE)

# Area units (Data Model 4.1 input tokens), matched right after a number.
_UNIT_PATTERNS = (
    ("sq_yd", r"sq\.?\s*(?:yds?|yards?)|square\s+yards?|sqyds?|yards?|yds?|gaz|guz|gz|گز"),
    ("sq_ft", (r"sq\.?\s*(?:ft|feet|foot)|square\s+(?:feet|foot|ft)|sqft|sft|feet|foot|ft"
               r"|مربع\s*فٹ|فٹ")),
    ("marla", r"marlas?|mrla|مرلہ|مرلے"),
    ("kanal", r"kanals?|knl|کنال"),
    ("acre", r"acres?|ایکڑ"),
)
UNIT_RE = re.compile(
    r"\s*(?:" + "|".join(f"(?P<{name}>{pattern})" for name, pattern in _UNIT_PATTERNS)
    + r")(?![^\W\d_])", re.IGNORECASE,
)

TOKEN_RE = re.compile(r"\d+(?:,\d{3})+(?:\.\d+)?|\d+[.,]\d+|\d+|[^\W\d_]+|[-–/]")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def _plain(value):
    """Decimal -> int when whole, else float (JSON-friendly, gold-comparable)."""
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _round(value):
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Match:
    """A number expression found in text. kind: money | area | number."""
    start: int
    end: int
    value: Decimal
    kind: str
    unit: str | None = None  # area unit
    multiplier: Decimal | None = None  # largest money multiplier used
    text: str = ""


@dataclass(frozen=True)
class Money:
    raw: str
    pkr: int | None
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class Area:
    raw: str
    status: str
    value: Decimal | None = None
    unit: str | None = None
    sqft: int | None = None
    unit_source: str | None = None  # stated | default
    nominal_sqyd: int | None = None
    mode: str | None = None  # target | min | range
    min_sqft: int | None = None
    max_sqft: int | None = None
    reason: str | None = None

    def to_dict(self):
        return {"raw": self.raw, "value": _plain(self.value), "unit": self.unit,
                "sqft": self.sqft, "unit_source": self.unit_source,
                "nominal_sqyd": self.nominal_sqyd, "mode": self.mode,
                "min_sqft": self.min_sqft, "max_sqft": self.max_sqft,
                "status": self.status}


@dataclass(frozen=True)
class Budget:
    raw: str
    status: str
    mode: str | None = None  # max | around | range | min
    min: int | None = None
    max: int | None = None
    stated_pkr: int | None = None  # what the user said, before any widening
    period: str | None = None  # one_time | monthly
    relative: str | None = None  # cheaper | higher | premium (op 'relative')
    reason: str | None = None

    def to_dict(self):
        if self.relative is not None:
            return {"raw": self.raw, "op": "relative", "relative": self.relative,
                    "status": self.status}
        return {"raw": self.raw, "min": self.min, "max": self.max, "mode": self.mode,
                "period": self.period, "stated_pkr": self.stated_pkr,
                "status": self.status}


@dataclass(frozen=True)
class Purpose:
    status: str
    value: str | None = None  # sale | rent
    raw: str | None = None
    source: str | None = None  # text | voice | inferred
    confirmed: bool = False
    reasons: tuple = field(default=())

    def to_dict(self):
        return {"value": self.value, "raw": self.raw, "source": self.source,
                "confirmed": self.confirmed, "status": self.status}


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _digits(token):
    """'1,800' -> 1800, '7,8' / '7.8' -> 7.8; Urdu / Arabic-Indic digits too."""
    ascii_digits = "".join(str(unicodedata.digit(ch)) if ch.isdigit() else ch for ch in token)
    if re.fullmatch(r"\d+(?:,\d{3})+(?:\.\d+)?", ascii_digits):
        ascii_digits = ascii_digits.replace(",", "")
    return Decimal(ascii_digits.replace(",", "."))


def _tokens(text):
    return [(m.group(), m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def _gap(text, a, b):
    return text[a:b]


class _Run:
    """Accumulates one Indian-system number: total (lakh / crore / ...), section
    (hundreds / thousands), cur (the current small number), prefix (sawa / ...)."""

    def __init__(self):
        self.total = Decimal(0)
        self.section = Decimal(0)
        self.cur = None
        self.prefix = None
        self.english_tens = False
        self.money_mult = None
        self.small_mult = False
        self.seen = False

    def value(self):
        return self.total + self.section + (self.cur or 0)

    def take_prefix(self, n):
        if self.prefix is not None:
            n, self.prefix = n + self.prefix, None
        return n

    def base(self):
        """The number a multiplier applies to ('sawa crore' -> 1.25)."""
        if self.cur is None:
            return self.take_prefix(Decimal(1))
        return self.cur


def _scan(text):
    """Every number expression in text, left to right, as Match objects."""
    tokens = _tokens(text)
    lower = [t[0].lower() for t in tokens]
    out, i = [], 0
    while i < len(tokens):
        run, start, end, j = _Run(), None, None, i
        complete = None  # (j, end, value, money_mult) after the last multiplier
        while j < len(tokens):
            word, (_, t_start, t_end) = lower[j], tokens[j]
            if start is not None and not _gap(text, end, t_start).isspace() and \
                    _gap(text, end, t_start) != "":
                break
            glued = start is not None and _gap(text, end, t_start) == ""
            nxt = lower[j + 1] if j + 1 < len(tokens) else None
            if word[0].isdigit() or word in NUMBER_WORDS or word in STANDALONE:
                n = _digits(tokens[j][0]) if word[0].isdigit() else \
                    NUMBER_WORDS.get(word, STANDALONE.get(word))
                if run.cur is not None:
                    if run.english_tens and word in ENGLISH_UNITS and n < 10:
                        run.cur += n
                        run.english_tens = False
                        end = t_end
                        j += 1
                        continue
                    break
                run.cur = run.take_prefix(n)
                run.english_tens = word in ENGLISH_TENS
                run.seen = True
            elif word in PREFIXES and run.cur is None:
                run.prefix = PREFIXES[word]
            elif word == "point" and run.cur is not None and nxt is not None and \
                    (nxt[0].isdigit() or nxt in NUMBER_WORDS):
                frac = _digits(tokens[j + 1][0]) if nxt[0].isdigit() else NUMBER_WORDS[nxt]
                places = len(str(int(frac)))
                run.cur += frac / (Decimal(10) ** places)
                end = tokens[j + 1][2]
                j += 2
                continue
            elif word == "and" and run.cur is not None and lower[j + 1:j + 3] == ["a", "half"]:
                run.cur += Decimal("0.5")
                end = tokens[j + 2][2]
                j += 3
                continue
            elif word == "and" and run.cur is None and run.small_mult and nxt in NUMBER_WORDS:
                # "one hundred and fifty thousand": the 'and' joins two parts.
                end = t_end
                j += 1
                continue
            elif word == "half" and run.cur is None and run.seen is False:
                run.cur = Decimal("0.5")
                run.seen = True
            elif word == "a" and run.cur is None and nxt is not None and \
                    (nxt in BIG or nxt in THOUSAND or nxt in HUNDRED):
                run.cur = Decimal(1)
                run.seen = True
            elif word in HUNDRED and (run.cur is not None or run.prefix is not None):
                run.section += run.base() * 100
                run.cur, run.small_mult, run.seen = None, True, True
                if complete is not None:  # still part of a lakh / crore amount
                    complete = (j + 1, t_end, run.value(), run.money_mult)
            elif word in THOUSAND and (run.cur is not None or run.prefix is not None
                                       or run.section) and (word not in GLUED_ONLY or glued):
                run.section = (run.section + (run.cur or 0) or run.base()) * 1000
                run.cur, run.seen = None, True
                run.money_mult = max(run.money_mult or 0, Decimal(1000))
                if complete is not None:  # still part of a lakh / crore amount
                    complete = (j + 1, t_end, run.value(), run.money_mult)
            elif word in BIG and (run.cur is not None or run.prefix is not None
                                  or run.section) and (word not in GLUED_ONLY or glued):
                base = run.section + run.cur if run.cur is not None else (
                    run.section or run.base())
                run.total += base * BIG[word]
                run.section, run.cur, run.seen = Decimal(0), None, True
                run.money_mult = max(run.money_mult or 0, BIG[word])
                complete = (j + 1, t_end, run.value(), run.money_mult)
            else:
                break
            if start is None:
                start = t_start
            end = t_end
            j += 1
        if not run.seen:
            i = max(j, i + 1)
            continue
        # A trailing number after a lakh / crore / ... multiplier is not part of the
        # amount ("8 crore 500 gaz"): end the run there and rescan from that number.
        # After sau / hazaar it is ("teen sau pachaas" = 350).
        if complete is not None and run.cur is not None:
            j, end, value, money_mult = complete
        else:
            value, money_mult = run.value(), run.money_mult
        if run.prefix is not None and complete is None:
            i = max(j, i + 1)
            continue
        out.append(_classify(text, start, end, value, money_mult))
        i = j
    return out


def _classify(text, start, end, value, money_mult):
    unit = UNIT_RE.match(text, end)
    if unit is not None:
        name = next(k for k, v in unit.groupdict().items() if v)
        return Match(start, unit.end(), value, "area", unit=name, text=text[start:unit.end()])
    before = text[max(0, start - 8):start]
    after = CURRENCY_AFTER.match(text, end)
    if money_mult is not None or after or CURRENCY_BEFORE.search(before):
        return Match(start, end, value, "money", multiplier=money_mult, text=text[start:end])
    return Match(start, end, value, "number", text=text[start:end])


def find_numbers(text):
    """All number expressions (money, area and plain numbers), in order."""
    return _scan(text or "")


def find_money(text):
    """Money amounts in text (an amount with a multiplier or a currency word).
    For the 9.1 grounding validator and the R-22 re-extraction path."""
    return [m for m in _scan(text or "") if m.kind == "money"]


def find_areas(text):
    """Sizes with an explicit unit in text."""
    return [m for m in _scan(text or "") if m.kind == "area"]


def parse_number(text):
    """The first number expression in text (any kind), or None."""
    found = _scan(text or "")
    return found[0].value if found else None


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

def parse_money(span):
    """One amount in integer PKR. Two separate amounts -> ambiguous."""
    found = find_money(span)
    if not found:
        return Money(span, None, NONE)
    if len(found) > 1:
        return Money(span, _round(found[0].value), AMBIGUOUS, "several_amounts")
    return Money(span, _round(found[0].value), OK)


# ---------------------------------------------------------------------------
# Area
# ---------------------------------------------------------------------------

def sqft_per_unit(unit, marla_sqft=DEFAULT_MARLA_SQFT):
    marla_sqft = Decimal(str(marla_sqft))
    return {"sq_ft": Decimal(1), "sq_yd": SQFT_PER_SQYD, "marla": marla_sqft,
            "kanal": marla_sqft * MARLA_PER_KANAL, "acre": SQFT_PER_ACRE}[unit]


def nominal_bucket(sqyd):
    """The nearest market bucket (sq yd) within +/-10 %, else None (272 -> 250)."""
    if sqyd is None or sqyd <= 0:
        return None
    sqyd = Decimal(str(sqyd))
    best = min(NOMINAL_BUCKETS_SQYD, key=lambda b: abs(sqyd - b))
    return best if abs(sqyd - best) <= best * BUCKET_TOLERANCE else None


def canonical_type(property_type):
    if not property_type:
        return None
    key = property_type.strip().lower()
    return _TYPE_ALIASES.get(key, key)


def category_of(property_type):
    """residential | commercial | None (unknown type)."""
    kind = canonical_type(property_type)
    if kind in RESIDENTIAL:
        return "residential"
    if kind in COMMERCIAL:
        return "commercial"
    return None


_AREA_MIN = re.compile(r"(?:se\s+(?:bara|bada|bari|badi|zyada)|at\s*least|minimum|kam\s+se\s+kam"
                       r"|or\s+(?:more|bigger|larger)|\+|plus|کم از کم)", re.IGNORECASE)
_RANGE_GAP = re.compile(r"^\s*(?:-|–|to|se|sy|and)\s*$", re.IGNORECASE)


def parse_area(span, *, property_type=None, marla_sqft=DEFAULT_MARLA_SQFT):
    """A size span -> canonical sq ft, unit, nominal sq-yd bucket and mode.

    Missing unit (Data Model 4.1): house / plot / portion up to 2,000 -> sq yd;
    apartment / shop / office / ... -> sq ft; anything else is ambiguous.
    """
    kind = canonical_type(property_type)
    found = [m for m in _scan(span or "") if m.kind in ("area", "number")]
    if not found:
        return Area(span, NONE)
    first = found[0]
    # Range: "300-500 gaz", "300 se 500 gaz"; the unit may be given once, at the end.
    if len(found) >= 2 and _RANGE_GAP.match(span[first.end:found[1].start]):
        second = found[1]
        unit = second.unit or first.unit
        if unit is None:
            unit, source, reason = _default_unit(kind, max(first.value, second.value))
            if unit is None:
                return Area(span, AMBIGUOUS, value=first.value, mode="range", reason=reason)
        else:
            source = "stated"
        per = sqft_per_unit(unit, marla_sqft)
        lo, hi = sorted((first.value * per, second.value * per))
        return Area(span, OK, value=first.value, unit=unit, sqft=_round(lo), unit_source=source,
                    mode="range", min_sqft=_round(lo), max_sqft=_round(hi))
    areas = [m for m in found if m.kind == "area"]
    if len(areas) > 1 or (not areas and len(found) > 1):
        return Area(span, AMBIGUOUS, reason="several_sizes")
    match = areas[0] if areas else first
    if match.unit:
        unit, source, reason = match.unit, "stated", None
    else:
        unit, source, reason = _default_unit(kind, match.value)
        if unit is None:
            return Area(span, AMBIGUOUS, value=match.value, reason=reason)
    sqft = match.value * sqft_per_unit(unit, marla_sqft)
    mode = "min" if _AREA_MIN.search(span) else "target"
    bucket = nominal_bucket(sqft / SQFT_PER_SQYD) if kind in SQYD_TYPES or (
        kind is None and unit in ("sq_yd", "marla", "kanal")) else None
    return Area(span, OK, value=match.value, unit=unit, sqft=_round(sqft), unit_source=source,
                nominal_sqyd=bucket, mode=mode,
                min_sqft=_round(sqft) if mode == "min" else None)


def _default_unit(kind, value):
    if kind in SQYD_TYPES:
        if value <= MAX_DEFAULT_SQYD:
            return "sq_yd", "default", None
        return None, None, "unit_unclear"
    if kind in SQFT_TYPES:
        return "sq_ft", "default", None
    return None, None, "unit_missing"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

_MIN_WORDS = re.compile(r"kam\s+se\s+kam|at\s*least|minimum|\bmin\b|se\s+upar|se\s+oopar"
                        r"|above|more\s+than|کم از کم", re.IGNORECASE)
_MAX_WORDS = re.compile(r"zyada\s+se\s+zyada|ziada\s+se\s+ziada|\bunder\b|\bbelow\b"
                        r"|less\s+than|up\s*to|\bmax(?:imum)?\b|\btak\b|\btakk\b|se\s+kam"
                        r"|\bwithin\b|تک|سے کم", re.IGNORECASE)
_AROUND_WORDS = re.compile(r"\baround\b|\bapprox\w*|\babout\b|lag\s*bhag|\bkareeb\b|\bqareeb\b"
                           r"|taqreeban|aas\s*paas|لگ بھگ|تقریباً|قریب", re.IGNORECASE)
_MONTHLY = re.compile(r"per\s+month|a\s+month|/\s*month|\bmonthly\b|\bmonth\b|\bmahana\b"
                      r"|\bmaahana\b|\bmahina\b|ماہانہ|مہینہ", re.IGNORECASE)

_RELATIVE = (
    ("higher", re.compile(r"thoda\s+upar|thora\s+upar|budget\s+(?:barha|badha|bara|bada)"
                          r"|increase\s+(?:the\s+)?budget|zyada\s+budget|more\s+budget", re.IGNORECASE)),
    ("cheaper", re.compile(r"\bsast[aei]\b|\bcheaper\b|less\s+expensive|kam\s+(?:qeemat|price)"
                           r"|سستا|سستی", re.IGNORECASE)),
    ("premium", re.compile(r"mehng[aei]|mahang[aei]|\bpremium\b|more\s+expensive|\bluxury\b"
                           r"|مہنگا", re.IGNORECASE)),
    ("bigger", re.compile(r"\b(?:bara|bada|bari|badi|bigger|larger)\b", re.IGNORECASE)),
)


def relative_op(span):
    """cheaper | higher | premium | bigger | None. The backend computes the new
    constraint from the last search (R17 4.5; 7.3), never the LLM."""
    for name, pattern in _RELATIVE:
        if pattern.search(span or ""):
            return name
    return None


def cheaper_max(shown_prices):
    """'kuch sasta': max = cheapest shown x 0.97 (R17 4.5)."""
    return _round(min(Decimal(p) for p in shown_prices) * CHEAPER_FACTOR)


def higher_max(current_max):
    """'thoda upar' / 'budget barha do': max x 1.10."""
    return _round(Decimal(current_max) * HIGHER_FACTOR)


def premium_min(shown_prices):
    """'aur mehnga / premium': min = highest shown x 1.03."""
    return _round(max(Decimal(p) for p in shown_prices) * PREMIUM_FACTOR)


def period_for(purpose):
    return {"sale": "one_time", "rent": "monthly"}.get(purpose)


def parse_budget(span, *, purpose=None):
    """A budget span -> {mode, min, max, stated, period} (Data Model 4.3, R17 4.3).

    Modes: max (under / tak / max / up to ...), around (+/-10 %; also a bare amount
    and "budget X", owner decision 2026-10-12), range (X se Y, X-Y, between), min
    (kam se kam / at least / se upar). The stated amount is kept apart from the
    min / max; search tolerances are applied by search (8.1), not here.
    """
    span = span or ""
    monthly = bool(_MONTHLY.search(span))
    period = "monthly" if monthly else period_for(purpose)
    found = [m for m in _scan(span) if m.kind in ("money", "number")]
    money = [m for m in found if m.kind == "money"]
    if not money:
        relative = relative_op(span)
        if relative in ("cheaper", "higher", "premium"):
            return Budget(span, OK, relative=relative)
        return Budget(span, NONE)
    # Range: two amounts joined by se / to / - / and; a shared trailing multiplier
    # ("2 se 3 crore") applies to a first number that has none.
    for a, b in pairwise(found):
        if b.kind == "money" and _RANGE_GAP.match(span[a.end:b.start]):
            low = a.value if a.kind == "money" else a.value * b.multiplier
            lo, hi = sorted((_round(low), _round(b.value)))
            return Budget(span, OK, mode="range", min=lo, max=hi, stated_pkr=hi, period=period)
    if len(money) > 1:
        return Budget(span, AMBIGUOUS, period=period, reason="several_amounts")
    value = money[0].value
    stated = _round(value)
    if _MIN_WORDS.search(span) and not re.search(r"zyada\s+se\s+zyada", span, re.IGNORECASE):
        return Budget(span, OK, mode="min", min=stated, stated_pkr=stated, period=period)
    if _MAX_WORDS.search(span):
        return Budget(span, OK, mode="max", max=stated, stated_pkr=stated, period=period)
    return Budget(span, OK, mode="around", min=_round(value * (1 - AROUND)),
                  max=_round(value * (1 + AROUND)), stated_pkr=stated, period=period)


# ---------------------------------------------------------------------------
# Purpose
# ---------------------------------------------------------------------------

# Explicit words win (Data Model 4.4). Most specific first; rent beats sale.
_RENT = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:on|for)\s+rent\b", r"\brent\s+(?:pe|par)\b",
    r"\bkiray[ea]?\s+(?:pe|par|per)\b", r"\bkiraya\b", r"\bkiraye\b", r"\brental\b",
    r"\brent\b", r"\blease\b", r"کرائے\s*پر", r"کرایہ", r"کرائے",
)]
_SALE = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:for|on)\s+sale\b", r"\bsale\s+(?:pe|par|ke\s+liye|ka|ki)\b", r"\bbuy(?:ing)?\b",
    r"\bpurchase\b", r"\bkhareedn[aei](?:\s+hai)?\b", r"\bkhareed\w*", r"\blen[ai]\s+hai\b",
    r"\bsale\b", r"خرید\w*",
)]


def parse_purpose(text, *, source="text"):
    """Explicit sale / rent wording -> Purpose(source=text|voice), else status none.
    A monthly amount marker ("mahana", "per month") also means rent."""
    text = text or ""
    for value, patterns in (("rent", _RENT), ("sale", _SALE)):
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return Purpose(OK, value, match.group(), source, confirmed=True)
    monthly = _MONTHLY.search(text)
    if monthly:
        return Purpose(OK, "rent", monthly.group(), source, confirmed=True)
    return Purpose(NONE)


def infer_purpose(pkr, category=None):
    """Rent / sale from an amount alone (Data Model 4.4): residential <= 15 lakh
    rent, >= 50 lakh sale; commercial <= 25 lakh rent, >= 1 crore sale (owner
    2026-10-12); in between -> ambiguous (ask). Unknown category: rent only up to
    the residential limit, sale only from the commercial one. Always
    source='inferred', confirmed=False: the reply echoes it before the first search."""
    if pkr is None:
        return Purpose(NONE)
    if category in RENT_MAX:
        rent_max, sale_min = RENT_MAX[category], SALE_MIN[category]
    else:
        rent_max, sale_min = RENT_MAX["residential"], SALE_MIN["commercial"]
    if pkr <= rent_max:
        return Purpose(OK, "rent", None, "inferred")
    if pkr >= sale_min:
        return Purpose(OK, "sale", None, "inferred")
    return Purpose(AMBIGUOUS, None, None, "inferred", reasons=("amount_between_thresholds",))
