"""Task 6.2: table-driven tests for the money / area / budget / purpose normalisers
(Data Model 4.1-4.4, R17 4, CLAUDE.md 6, owner decisions 2026-10-12). Offline."""

from decimal import Decimal

import pytest

from app import benchmark
from app import normalise as n

LAKH, CRORE = 100_000, 10_000_000

# --- money: (span, PKR) ---
MONEY = [
    ("5 lakh", 5 * LAKH),
    ("80 lac", 80 * LAKH),
    ("80 lacs", 80 * LAKH),
    ("5L", 5 * LAKH),
    ("7.8cr", 78_000_000),
    ("7,8 cr", 78_000_000),
    ("7.8 crore", 78_000_000),
    ("3 karor", 3 * CRORE),
    ("2 karod", 2 * CRORE),
    ("1 arab", 1_000_000_000),
    ("2 million", 2_000_000),
    ("2m", 2_000_000),
    ("50k", 50_000),
    ("48k", 48_000),
    ("90 hazar", 90_000),
    ("nabbe hazaar", 90_000),
    ("fifty thousand", 50_000),
    ("Rs 45,000", 45_000),
    ("45,000 rupees", 45_000),
    ("1 crore 5 lakh", 10_500_000),
    ("1 crore 30 lakh", 13_000_000),
    ("ek crore saath lakh", 16_000_000),
    ("teen lakh pachaas hazaar", 350_000),
    ("sawa crore", 12_500_000),
    ("sawa 2 crore", 22_500_000),
    ("saade 7 crore", 75_000_000),
    ("saadhe paanch lakh", 550_000),
    ("sarhe teen crore", 35_000_000),
    ("paune do crore", 17_500_000),
    ("dedh crore", 15_000_000),
    ("dhai crore", 25_000_000),
    ("dhaai lakh", 250_000),
    ("aadha crore", 5_000_000),
    ("aath crore", 80_000_000),
    ("pachees lakh", 25 * LAKH),
    ("assi lakh", 80 * LAKH),
    ("saat point aath crore", 78_000_000),
    ("three and a half lakh", 350_000),
    ("one and a half crore", 15_000_000),
    ("twenty five lakh", 25 * LAKH),
    ("eighteen crore", 18 * CRORE),
    ("one hundred and fifty thousand", 150_000),
    ("a crore", CRORE),
    ("8 کروڑ", 8 * CRORE),
    ("۸ کروڑ", 8 * CRORE),
    ("5 لاکھ", 5 * LAKH),
]


@pytest.mark.parametrize(("span", "pkr"), MONEY)
def test_money(span, pkr):
    result = n.parse_money(span)
    assert (result.status, result.pkr) == ("ok", pkr)


@pytest.mark.parametrize("span", [
    "do you have 2 beds",  # 'do' and '2' are not amounts without a multiplier
    "500 gaz",  # a size, not money
    "5 m",  # a lone 'm' counts only glued to digits ("2m")
    "sawa",  # a fraction word with no amount
    "ghar saath chahiye",  # 'saath' = with
    "",
])
def test_not_money(span):
    assert n.parse_money(span).status == "none"


def test_two_amounts_are_ambiguous():
    assert n.parse_money("5 lakh ya 7 lakh").status == "ambiguous"


def test_find_money_spans_for_the_validator():
    text = "DHA 6, 500 gaz, demand 8 crore, rent 2.5 lakh"
    found = n.find_money(text)
    assert [(text[m.start:m.end], m.value) for m in found] == [
        ("8 crore", Decimal(80_000_000)), ("2.5 lakh", Decimal(250_000))]


def test_trailing_number_after_crore_is_not_part_of_the_amount():
    kinds = [(m.kind, m.value) for m in n.find_numbers("8 crore 500 gaz")]
    assert kinds == [("money", Decimal(80_000_000)), ("area", Decimal(500))]


# --- area: (span, property_type, value, unit, sqft, unit_source) ---
AREA = [
    ("500 yards", "plot", 500, "sq_yd", 4500, "stated"),
    ("500 yard", "house", 500, "sq_yd", 4500, "stated"),
    ("500 gaz", "house", 500, "sq_yd", 4500, "stated"),
    ("500 guz", "house", 500, "sq_yd", 4500, "stated"),
    ("240 sq yd", "house", 240, "sq_yd", 2160, "stated"),
    ("500 sqyd", "plot", 500, "sq_yd", 4500, "stated"),
    ("500 گز", "house", 500, "sq_yd", 4500, "stated"),
    ("2000 sq ft", "office", 2000, "sq_ft", 2000, "stated"),
    ("1,800 sqft", "apartment", 1800, "sq_ft", 1800, "stated"),
    ("950 sft", "apartment", 950, "sq_ft", 950, "stated"),
    ("pandrah sau square feet", "office", 1500, "sq_ft", 1500, "stated"),
    ("do hazaar square feet", "office", 2000, "sq_ft", 2000, "stated"),
    ("10 marla", "house", 10, "marla", 2250, "stated"),
    ("das marla", "house", 10, "marla", 2250, "stated"),
    ("2 kanal", "house", 2, "kanal", 9000, "stated"),
    ("two kanal", "house", 2, "kanal", 9000, "stated"),
    ("1 knl", "plot", 1, "kanal", 4500, "stated"),
    ("chaar sau gaz", "portion", 400, "sq_yd", 3600, "stated"),
    ("teen sau pachaas gaz", "house", 350, "sq_yd", 3150, "stated"),
    ("dedh sau gaz", "house", 150, "sq_yd", 1350, "stated"),
    ("500 gaz", "apartment", 500, "sq_yd", 4500, "stated"),  # stated unit wins
    # Missing unit (Data Model 4.1): house / plot / portion <= 2,000 -> sq yd.
    ("400 ka", "portion", 400, "sq_yd", 3600, "default"),
    ("500 ka", "house", 500, "sq_yd", 4500, "default"),
    ("2000", "plot", 2000, "sq_yd", 18000, "default"),
    ("900", "apartment", 900, "sq_ft", 900, "default"),
    ("350", "shop", 350, "sq_ft", 350, "default"),
    ("1200", "flat", 1200, "sq_ft", 1200, "default"),
]


@pytest.mark.parametrize(("span", "kind", "value", "unit", "sqft", "source"), AREA)
def test_area(span, kind, value, unit, sqft, source):
    result = n.parse_area(span, property_type=kind)
    assert (result.status, result.value, result.unit, result.sqft, result.unit_source) == (
        "ok", value, unit, sqft, source)


@pytest.mark.parametrize(("span", "kind", "reason"), [
    ("2500 ka", "house", "unit_unclear"),  # > 2,000 without a unit: ask
    ("500", None, "unit_missing"),  # no unit, no type: ask
    ("500", "unknown", "unit_missing"),
])
def test_area_ambiguous(span, kind, reason):
    result = n.parse_area(span, property_type=kind)
    assert (result.status, result.reason) == ("ambiguous", reason)


def test_marla_follows_the_agency_basis():
    assert n.parse_area("10 marla", property_type="house").sqft == 2250
    assert n.parse_area("10 marla", property_type="house", marla_sqft=272.25).sqft == 2723


@pytest.mark.parametrize(("sqyd", "bucket"), [
    (250, 250), (272, 250), (240, 240), (500, 500), (520, 500), (2250 / 9, 250),
    (1000, 1000), (700, None), (0, None),
])
def test_nominal_bucket(sqyd, bucket):
    assert n.nominal_bucket(sqyd) == bucket


@pytest.mark.parametrize(("span", "mode", "min_sqft", "max_sqft"), [
    ("10 marla se bara", "min", 2250, None),
    ("at least 500 gaz", "min", 4500, None),
    ("300-500 gaz", "range", 2700, 4500),
    ("300 se 500 gaz", "range", 2700, 4500),
])
def test_area_modes(span, mode, min_sqft, max_sqft):
    result = n.parse_area(span, property_type="plot")
    assert (result.mode, result.min_sqft, result.max_sqft) == (mode, min_sqft, max_sqft)


def test_unit_table_matches_the_benchmark():
    for unit, per in benchmark.SQFT_PER_UNIT.items():
        assert n.sqft_per_unit(unit) == per
    assert n.SQYD_TYPES == benchmark.SQYD_TYPES


# --- budget: (span, purpose, mode, min, max, period) ---
BUDGET = [
    ("under 7 crore", None, "max", None, 70_000_000, None),
    ("dedh crore tak", "sale", "max", None, 15_000_000, "one_time"),
    ("Max 3.5 lakh a month", "rent", "max", None, 350_000, "monthly"),
    ("3 crore max", "sale", "max", None, 30_000_000, "one_time"),
    ("up to 50k monthly", None, "max", None, 50_000, "monthly"),
    ("zyada se zyada 2 crore", None, "max", None, 20_000_000, None),
    ("2 crore se kam", None, "max", None, 20_000_000, None),
    ("8 کروڑ تک", "sale", "max", None, 80_000_000, "one_time"),
    ("budget aath crore tak", "sale", "max", None, 80_000_000, "one_time"),  # tak wins
    ("around 5 lakh per month", "rent", "around", 450_000, 550_000, "monthly"),
    ("lagbhag 2 crore", None, "around", 18_000_000, 22_000_000, None),
    ("saadhe teen crore ke aas paas", "sale", "around", 31_500_000, 38_500_000, "one_time"),
    # A bare amount and "budget X" = around +/-10 % (owner decision 2026-10-12).
    ("budget 18 crore", "sale", "around", 162_000_000, 198_000_000, "one_time"),
    ("1.6 crore budget", "sale", "around", 14_400_000, 17_600_000, "one_time"),
    ("5 lakh", "rent", "around", 450_000, 550_000, "monthly"),
    ("kam se kam 2 crore", None, "min", 20_000_000, None, None),
    ("at least 50 lakh", None, "min", 5_000_000, None, None),
    ("2 se 3 crore", None, "range", 20_000_000, 30_000_000, None),
    ("50 lakh se 1 crore", None, "range", 5_000_000, 10_000_000, None),
    ("between 2 and 3 crore", None, "range", 20_000_000, 30_000_000, None),
    ("2-3 crore", None, "range", 20_000_000, 30_000_000, None),
]


@pytest.mark.parametrize(("span", "purpose", "mode", "low", "high", "period"), BUDGET)
def test_budget(span, purpose, mode, low, high, period):
    result = n.parse_budget(span, purpose=purpose)
    assert (result.status, result.mode, result.min, result.max, result.period) == (
        "ok", mode, low, high, period)


def test_budget_keeps_the_stated_amount_apart_from_the_range():
    result = n.parse_budget("budget 18 crore")
    assert (result.stated_pkr, result.min, result.max) == (
        180_000_000, 162_000_000, 198_000_000)


@pytest.mark.parametrize(("span", "status"), [
    ("5 lakh aur 7 lakh", "ambiguous"),
    ("budget kya hoga", "none"),
])
def test_budget_unresolved(span, status):
    assert n.parse_budget(span).status == status


# --- relative ops (R17 4.5; the state rules are Task 7.3) ---
@pytest.mark.parametrize(("span", "op"), [
    ("Kuch sasta dikhao", "cheaper"),
    ("cheaper options", "cheaper"),
    ("thoda upar dekh lo", "higher"),
    ("budget barha do", "higher"),
    ("aur mehnga dikhao", "premium"),
    ("thoda bara ghar", "bigger"),
    ("Phase 8 mein bhi dekho", None),
])
def test_relative_op(span, op):
    assert n.relative_op(span) == op


def test_relative_budget_span():
    assert n.parse_budget("Kuch sasta").to_dict() == {
        "raw": "Kuch sasta", "op": "relative", "relative": "cheaper", "status": "ok"}


def test_relative_formulas():
    assert n.cheaper_max([75_000_000, 78_000_000]) == 72_750_000  # cheapest x 0.97
    assert n.higher_max(80_000_000) == 88_000_000
    assert n.premium_min([69_000_000, 74_000_000]) == 76_220_000


# --- purpose ---
@pytest.mark.parametrize(("text", "value", "raw"), [
    ("office on rent in Karachi", "rent", "on rent"),
    ("shops for rent in Clifton", "rent", "for rent"),
    ("office rent pe chahiye", "rent", "rent pe"),
    ("portion kiraye pe chahiye", "rent", "kiraye pe"),
    ("kiraye pe lena hai", "rent", "kiraye pe"),  # rent beats sale
    ("کلفٹن میں دکان کرائے پر چاہیے", "rent", "کرائے پر"),
    ("4.5 lakh monthly", "rent", "monthly"),  # a monthly amount means rent
    ("Looking to buy a plot", "sale", "buy"),
    ("apartment for sale", "sale", "for sale"),
    ("dukaan chahiye khareedne ke liye", "sale", "khareedne"),
    ("dukaan khareedni hai", "sale", "khareedni hai"),
    ("10 marla ka ghar lena hai", "sale", "lena hai"),
])
def test_explicit_purpose(text, value, raw):
    result = n.parse_purpose(text)
    assert (result.status, result.value, result.raw, result.source, result.confirmed) == (
        "ok", value, raw, "text", True)


@pytest.mark.parametrize("text", [
    "ghar chahiye", "current flat dikhao", "shop chahiye investment ke liye", ""])
def test_no_explicit_purpose(text):
    assert n.parse_purpose(text).status == "none"


@pytest.mark.parametrize(("pkr", "category", "value"), [
    (15 * LAKH, "residential", "rent"),
    (15 * LAKH + 1, "residential", None),
    (49 * LAKH, "residential", None),
    (50 * LAKH, "residential", "sale"),
    (25 * LAKH, "commercial", "rent"),
    (30 * LAKH, "commercial", None),
    (CRORE, "commercial", "sale"),  # owner decision 2026-10-12
    (5 * LAKH, "commercial", "rent"),
    (20 * LAKH, None, None),
    (2 * CRORE, None, "sale"),
])
def test_inferred_purpose(pkr, category, value):
    result = n.infer_purpose(pkr, category)
    assert (result.value, result.source, result.confirmed) == (value, "inferred", False)
    assert result.status == ("ok" if value else "ambiguous")


@pytest.mark.parametrize(("kind", "category"), [
    ("house", "residential"), ("plot", "residential"), ("flat", "residential"),
    ("office", "commercial"), ("shop", "commercial"), ("villa", None), (None, None),
])
def test_category(kind, category):
    assert n.category_of(kind) == category


# Devanagari (live 2026-10-17: Scribe writes Roman Urdu speech partly in Devanagari)
@pytest.mark.parametrize(("text", "roman"), [
    ("8 crore तक", "8 crore tak"),
    ("घर चाहिए", "ghar चाहिए"),                      # unknown words stay as they are
    ("बजट आठ करोड़ तक।", "बजट aath crore tak।"),     # nukta, danda
    ("५०० गज़", "500 gaz"),                          # Devanagari digits
    ("पाँच सौ गज", "paanch sau gaz"),                 # chandrabindu
    ("DHA phase 6", "DHA phase 6"),
])
def test_romanise(text, roman):
    assert n.romanise(text) == roman


def test_devanagari_amounts_sizes_and_purpose_are_read():
    assert n.parse_area("पाँच सौ गज़", property_type="house").sqft == 4500
    assert n.parse_budget("आठ करोड़ तक", purpose="sale").max == 80_000_000
    assert n.parse_purpose("किराए पर चाहिए").value == "rent"
    assert n.parse_purpose("घर खरीदना है").value == "sale"
    assert n.relative_op("कुछ सस्ता दिखाओ") == "cheaper"
