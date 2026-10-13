"""Task 7.1: table-driven tests for the deterministic location resolver (R17 3.1-3.4,
Data Model 2-3; provisional choices in docs/LOCATION_RESOLVER.md). Offline: the
gazetteer snapshot is built from demo/ with dataset keys as IDs."""

import copy
import dataclasses
from fractions import Fraction

import pytest

from app import demo_dataset
from app import location as loc

DATA = demo_dataset.load()
G = loc.Gazetteer.from_dataset(DATA)
R, A, U, O = loc.RESOLVED, loc.AMBIGUOUS, loc.UNKNOWN, loc.OUT_OF_COVERAGE


def run(span, active=None, gazetteer=G):
    return loc.resolve(span, active, gazetteer)


def check(result, status, expected):
    """expected: node IDs when RESOLVED, candidate IDs (any order) when AMBIGUOUS."""
    assert result.status == status, result.to_dict()
    if status == R:
        assert list(result.nodes) == expected
        assert result.candidates == ()
    elif status == A:
        assert sorted(c.location_id for c in result.candidates) == sorted(expected)
        assert result.nodes == ()
    else:
        assert result.nodes == () and result.candidates == ()


# --- the snapshot ---

def test_snapshot_matches_the_seeded_dataset():
    levels = {}
    for place in G.places.values():
        levels[place.level] = levels.get(place.level, 0) + 1
    assert len(G.places) == 53 and len(G.aliases) == 87  # + Bahria Precinct 6 (7.1)
    assert levels == {"city": 1, "area": 8, "sub_area": 33, "zone": 11}


def _alias_cases():
    by_key = {}
    for row in DATA["location_aliases.csv"]:
        by_key.setdefault(loc.clean(row["alias"]), set()).add(
            (row["location_key"], row["context_key"] or None))
    for row in DATA["location_aliases.csv"]:
        entries = by_key[loc.clean(row["alias"])]
        yield pytest.param(row["alias"], row["context_key"] or None, row["location_key"],
                           entries, id=f"{row['location_key']}:{row['alias']}")


@pytest.mark.parametrize(("alias", "context", "location", "entries"), list(_alias_cases()))
def test_every_seeded_alias_resolves_exactly(alias, context, location, entries):
    """Done condition: exact aliases 100 % correct. A context alias resolves inside
    its context; a key that is context-only everywhere asks without one."""
    check(run(alias, context), R, [location])
    if any(ctx is None for _, ctx in entries):
        check(run(alias), R, [location])
    else:
        result = run(alias)
        assert result.status == A and result.method == "context_required"
        assert location in {c.location_id for c in result.candidates}


def test_alias_lookup_is_case_space_and_punctuation_insensitive():
    for span in ("  dha   PHASE 6 ", "Dha-Phase-6", "dha phase 6!", "DHA, phase 6."):
        check(run(span), R, ["dha-ph6"])


# --- context aliases and bare level + identifier (R17 3.2, 3.3 step 3, 3.4) ---
# All on the seeded data (demo/), which includes Bahria Precinct 6 since 7.1.

CONTEXT = [
    # span, active node, status, expected
    ("Phase 8", "dha-ph6", R, ["dha-ph8"]),        # R17 3.4 row
    ("Block 6", None, A, ["clf-b6", "gli-b6", "pec-b6"]),  # R17 3.4 row
    ("block six", None, A, ["clf-b6", "gli-b6", "pec-b6"]),
    ("Block 2", None, A, ["clf-b2", "pec-b2"]),
    ("Block 6", "gli", R, ["gli-b6"]),
    ("block 6", "gli-b13", R, ["gli-b6"]),          # any node: its area is the context
    ("block 6", "pec", R, ["pec-b6"]),
    ("block 6", "clf-b5", R, ["clf-b6"]),
    ("block 2", "pec-tariq-road", R, ["pec-b2"]),
    ("block 5", None, R, ["clf-b5"]),               # no area: only one area has it
    ("block h", None, R, ["nnz-bh"]),
    ("block h", "nnz", R, ["nnz-bh"]),
    ("6", "dha", R, ["dha-ph6"]),                   # bare number read by the area's levels
    ("6", "clf", R, ["clf-b6"]),
    ("13", "gli-b6", R, ["gli-b13"]),
    ("6", None, U, []),                             # bare number, no area: never guessed
    ("2 ext", "dha", R, ["dha-ph2x"]),
    ("2 ext", None, R, ["dha-ph2x"]),
    ("sector 13", None, R, ["dhc-s13"]),
    ("sector 8", "dhc", R, ["dhc-s8"]),
    ("precinct 10", None, R, ["btk-p10"]),
    ("p 10", None, R, ["btk-p10"]),                 # "p" = phase or precinct
    ("p27", "btk", R, ["btk-p27"]),
    ("precinct 6", "btk", R, ["btk-p6"]),
    ("precinct 6", None, R, ["btk-p6"]),
]


@pytest.mark.parametrize(("span", "active", "status", "expected"), CONTEXT)
def test_context_and_level_rules(span, active, status, expected):
    check(run(span, active), status, expected)


# R17 3.4 P6 rows, now on the seeded data (owner decision: Bahria Precinct 6 added).
@pytest.mark.parametrize(("active", "status", "expected"), [
    ("dha", R, ["dha-ph6"]),
    ("dha-ph8", R, ["dha-ph6"]),
    ("dha-ph6-bukhari", R, ["dha-ph6"]),
    ("btk", R, ["btk-p6"]),
    ("btk-p10", R, ["btk-p6"]),
    (None, A, ["dha-ph6", "btk-p6"]),               # "Ask: [DHA Phase 6] [Bahria Precinct 6]"
])
def test_r17_p6_rows(active, status, expected):
    for span in ("P6", "p6", "p 6"):
        check(run(span, active), status, expected)


# Active-area scope (owner decision 2026-10-12): a bare level + identifier, "p" +
# number or bare identifier never leaves the active area, even when the area has
# no such level and a mechanically derived global alias exists elsewhere.
SCOPE_UNKNOWN = [
    ("block 5", "gli"),        # global "block 5" -> Clifton is not used
    ("block 9", "pec"),
    ("block 4", "nnz"),
    ("phase 8", "clf"),        # Clifton has no phases: still not DHA
    ("block 6", "dha"),        # DHA has no blocks: no three-area question
    ("2 ext", "clf"),          # DHA's 2 Ext is not Clifton's
    ("P6", "clf"),
    ("precinct 7", "btk"),     # not seeded
    ("sector 13", "dha"),      # DHA City's sector, not DHA's
    ("block h", "gli"),        # North Nazimabad's letter block
    ("13", "clf"),
    ("phase 8", "btk-p10"),
]


@pytest.mark.parametrize(("span", "active"), SCOPE_UNKNOWN)
def test_active_area_scope_returns_unknown_with_the_area(span, active):
    result = run(span, active)
    check(result, U, [])
    assert result.method == "not_in_active_area"
    assert result.area_hint == G.area_of(active)


@pytest.mark.parametrize(("span", "active", "expected"), [
    ("DHA phase 8", "clf", "dha-ph8"),      # an explicitly named area resolves normally
    ("Clifton block 5", "gli", "clf-b5"),
    ("Bahria precinct 10", "dha", "btk-p10"),
    ("DHA City sector 13", "dha", "dhc-s13"),
    ("Bukhari commercial", "btk", "dha-ph6-bukhari"),
    ("Karachi", "dha", "khi"),
    ("DCK", "dha", "dhc"),
    ("Clifton", "dha-ph6", "clf"),
])
def test_named_areas_are_not_limited_by_the_active_area(span, active, expected):
    check(run(span, active), R, [expected])


def test_level_in_active_area_reports_the_method():
    assert run("Phase 8", "dha-ph6").method == "level_in_area"
    assert run("P6", "dha").method == "level_in_area"
    assert run("P6", None).method == "context_required"


# --- number variants (Data Model 3.2 step 4; app.normalise) ---

NUMBERS = [
    ("DHA 6", "dha-ph6"),
    ("DHA chay", "dha-ph6"),
    ("DHA chhe", "dha-ph6"),
    ("dha che", "dha-ph6"),
    ("DHA six", "dha-ph6"),
    ("DHA Phase 6", "dha-ph6"),
    ("DHA phase chhe", "dha-ph6"),
    ("DHA Phase VI", "dha-ph6"),
    ("Phase VI", "dha-ph6"),
    ("phase vi", "dha-ph6"),
    ("defence phase vi", "dha-ph6"),
    ("Defence 6", "dha-ph6"),
    ("DHA Ph6", "dha-ph6"),
    ("DHA Ph-6", "dha-ph6"),
    ("DHA 6th phase", None),  # word order not supported: see below
    ("Phase 6th", "dha-ph6"),
    ("DHA phase ۶", "dha-ph6"),           # Urdu digit
    ("DHA Phase VIII", "dha-ph8"),
    ("DHA phase aath", "dha-ph8"),
    ("Defence phase aath", "dha-ph8"),
    ("DHA Phase Eight", "dha-ph8"),
    ("DHA 5", "dha-ph5"),
    ("DHA paanch", "dha-ph5"),
    ("Phase V", "dha-ph5"),
    ("DHA Phase 7 Ext", "dha-ph7x"),
    ("do extension", "dha-ph2x"),
    ("DHA 2 ext", "dha-ph2x"),
    ("phase 2 extension", "dha-ph2x"),
    ("Clifton block two", "clf-b2"),
    ("Clifton block 2", "clf-b2"),
    ("Clifton 5", "clf-b5"),
    ("kliftan 5", "clf-b5"),
    ("block 5 clifton", "clf-b5"),
    ("Gulshan block chhe", "gli-b6"),
    ("Gulshan thirteen", "gli-b13"),
    ("Gulshan 13", "gli-b13"),
    ("Gulshan-e-Iqbal block 13", "gli-b13"),
    ("gulshan iqbal 13", "gli-b13"),
    ("Bahria precinct sattaees", "btk-p27"),
    ("Bahria precinct 27", "btk-p27"),
    ("bahria 27", "btk-p27"),
    ("Bahria Town precinct 10", "btk-p10"),
    ("Bahria precinct 6", "btk-p6"),
    ("Bahria P6", "btk-p6"),
    ("DHA City sector 13", "dhc-s13"),
    ("DHA City sector thirteen", "dhc-s13"),
    ("PECHS block 2", "pec-b2"),
    ("North Nazimabad block H", "nnz-bh"),
    ("North Nazimabad block L", "nnz-bl"),  # a letter, not Roman 50
]


@pytest.mark.parametrize(("span", "expected"),
                         [c for c in NUMBERS if c[1]], ids=[c[0] for c in NUMBERS if c[1]])
def test_number_variants(span, expected):
    check(run(span), R, [expected])


def test_number_words_only_in_number_positions():
    assert loc.clean("DHA chay") == "dha 6"
    assert loc.clean("chay DHA") == "chay dha"         # not after a level / area word
    assert loc.clean("Bahria Sports City") == "bahria sports city"
    assert loc.clean("block l") == "block l" and loc.clean("block i") == "block i"
    assert loc.clean("phase i") == "phase 1"
    assert loc.clean("DHA 6th phase") == "dha 6 phase"  # unsupported order stays visible
    assert run("DHA 6th phase").status == U


def test_cleaning_keeps_the_raw_span_and_canonicalises():
    raw = "  Defence  PH-6 , Karachi "
    result = run(raw)
    assert result.raw == raw and result.key == "dha phase 6 karachi"
    check(result, R, ["dha-ph6"])
    assert loc.clean("DHA Defence") == "dha"
    assert loc.clean("Nazimabad North") == "north nazimabad"
    assert loc.clean("n.n") == "north nazimabad"
    assert loc.clean("blk 6") == "block 6"
    assert loc.clean("Ⅵ") == "vi"   # NFKC; a numeral only after a lead word
    assert loc.clean("فیز ٦") == "phase 6"   # Arabic-Indic digit


# --- area only -> SUBTREE (R17 3.3 step 5, 3.4) ---

AREAS = [
    ("DHA", "dha"), ("Defence", "dha"), ("DHA Defence", "dha"),
    ("Clifton", "clf"), ("Klifton", "clf"), ("Clifton Karachi", "clf"),
    ("Karachi Clifton", "clf"),
    ("Bahria", "btk"), ("Bahria Town", "btk"), ("Bahria Karachi", "btk"), ("BTK", "btk"),
    ("Bahria Town Karachi", "btk"),
    ("Gulshan", "gli"), ("Gulshan-e-Iqbal", "gli"), ("Gulshan Iqbal", "gli"),
    ("PECHS", "pec"), ("North Nazimabad", "nnz"), ("Nazimabad North", "nnz"),
    ("Shahrah-e-Faisal", "shf"), ("shahrah e faisal", "shf"),
    ("DHA City", "dhc"), ("DCK", "dhc"), ("DHA City Karachi", "dhc"),
    ("Superhighway DHA", "dhc"),
]


@pytest.mark.parametrize(("span", "area"), AREAS)
def test_area_only_is_a_subtree(span, area):
    result = run(span)
    check(result, R, [area])
    assert result.mode == loc.SUBTREE


def test_city_is_anywhere_and_lower_levels_are_exact():
    assert run("Karachi").mode == loc.ANYWHERE_IN_CITY
    assert run("Karachi").nodes == ("khi",)
    assert run("DHA 6").mode == loc.EXACT
    assert run("Bukhari commercial").mode == loc.EXACT


# --- DHA City is not DHA (Data Model 2; R17 3.4) ---

def test_dha_city_is_separate_from_dha():
    assert G.area_of("dhc-s13") == "dhc" and not G.is_within("dhc-s13", "dha")
    check(run("DHA City"), R, ["dhc"])
    check(run("DHA City sector 8"), R, ["dhc-s8"])
    result = run("DHA sector 13")            # sector 13 is in DHA City, not DHA
    check(result, U, [])
    assert result.area_hint == "dha"
    result = run("DHA City phase 6")
    check(result, U, [])
    assert result.area_hint == "dhc"


# --- other cities -> OUT_OF_COVERAGE before any local match (R17 3.3 step 6) ---

OTHER = [
    ("Bahria Lahore", None), ("DHA Lahore", None), ("DHA Islamabad", None),
    ("Bahria Town Lahore", None), ("lahore", None), ("Islamabad", None),
    ("DHA Lahore", "dha-ph6"), ("Bahria Lahore", "btk"), ("phase 6 lahore", "dha"),
    ("Clifton Lahore", None),
]


@pytest.mark.parametrize(("span", "active"), OTHER)
def test_other_city_is_out_of_coverage(span, active):
    result = run(span, active)
    check(result, O, [])
    assert result.method == "other_city" and result.other_city in loc.OTHER_CITIES


# --- UNKNOWN and AMBIGUOUS from the source contract ---

UNKNOWNS = [
    ("", "empty"), ("   ", "empty"), ("!!! ...", "empty"),
    ("Malir Cantt", "none"),                  # R17 3.4: not covered, never a guess
    ("Hyderabad", "none"),                    # not a listed other city (D8)
    ("डीएचए फेज 6", "unsupported_script"),     # Devanagari (D3)
    ("ملیر", "unsupported_script"),            # Urdu word without a gazetteer term
    ("بحریہ ٹاؤن 10", "none"),                  # Bahria recognised; the rest is not a term
    ("Defence City", "none"),                  # not DHA City by the generic rule
    ("Defense City", "none"),
    ("Lahori Gate", "none"),                   # whole tokens only: not Lahore
    ("Islamabadi", "none"),
]


@pytest.mark.parametrize(("span", "method"), UNKNOWNS)
def test_unknown_input(span, method):
    result = run(span)
    check(result, U, [])
    assert result.method == method


@pytest.mark.parametrize(("span", "hint"), [
    ("DHA phase 3", "dha"), ("Clifton phase 8", "clf"), ("Gulshan block 5", "gli"),
    ("Bahria precinct 7", "btk"), ("PECHS block 6 ext", "pec"),
    ("DHA Defence City", "dha"),             # DHA named; "defence city" is not its child
])
def test_area_recognised_but_part_unknown(span, hint):
    """Never widened to the whole area silently; the hint lets the reply offer it."""
    result = run(span)
    check(result, U, [])
    assert result.area_hint == hint


# Identifier protection (owner decision 2026-10-12), deterministic side: numbers,
# letters and "ext" are matched exactly. The fuzzy stage adds the same guard.

@pytest.mark.parametrize(("span", "hint"), [
    ("Bahria precinct 12", "btk"),    # not Precinct 1 / 10
    ("Bahria precinct 100", "btk"),
    ("Gulshan block 1", "gli"),       # not Block 13
    ("North Nazimabad block K", "nnz"),  # not Block H / L
    ("DHA phase 3", "dha"),           # not the DHA area itself
    ("DHA phase 4 ext", "dha"),       # no 4 Ext: not Phase 4
])
def test_absent_child_never_becomes_another_child_or_the_parent(span, hint):
    result = run(span)
    check(result, U, [])
    assert result.area_hint == hint


@pytest.mark.parametrize(("span", "expected"), [
    ("DHA phase 2", "dha-ph2"), ("DHA phase 2 ext", "dha-ph2x"),
    ("DHA Phase 7", "dha-ph7"), ("DHA Phase 7 Ext", "dha-ph7x"),
    ("Bahria precinct 1", "btk-p1"), ("Bahria precinct 10", "btk-p10"),
    ("North Nazimabad block H", "nnz-bh"), ("North Nazimabad block L", "nnz-bl"),
    ("Gulshan block 13", "gli-b13"), ("Gulshan block 6", "gli-b6"),
])
def test_explicit_child_keeps_its_identifier(span, expected):
    """Never a sibling, and never widened to the parent area."""
    result = run(span)
    check(result, R, [expected])
    assert result.mode == loc.EXACT


def test_two_areas_in_one_span_ask():
    result = run("Clifton DHA")
    check(result, A, ["clf", "dha"])
    assert result.method == "two_areas"


def test_urdu_script_terms():
    """The four Data Model terms only (limited coverage, not general Urdu support)."""
    check(run("ڈی ایچ اے فیز 6"), R, ["dha-ph6"])
    check(run("کلفٹن"), R, ["clf"])
    check(run("كلفٹن"), R, ["clf"])  # Arabic kaf folded to Urdu keheh
    check(run("بحریہ"), R, ["btk"])
    check(run("بحریہ precinct 27"), R, ["btk-p27"])
    assert loc.clean("بحریہ") == "bahria"
    assert [hex(ord(c)) for c in "بحریہ"] == ["0x628", "0x62d", "0x631", "0x6cc", "0x6c1"]


def test_defence_city_is_not_made_dha_city():
    """Owner decision 2026-10-12: the generic defence -> dha rule must not turn
    "Defence City" into DHA City; the valid DHA City aliases are unchanged."""
    assert loc.clean("Defence City") == "defence city"
    for span in ("DHA City", "DCK", "DHA City Karachi", "Superhighway DHA", "dha city"):
        check(run(span), R, ["dhc"])
    for span, expected in (("Defence", "dha"), ("DHA Defence", "dha"),
                           ("Defence phase 8", "dha-ph8"), ("defence 6", "dha-ph6")):
        check(run(span), R, [expected])


def _with_local_alias(alias, location):
    """Synthetic fixture (not demo data): one extra local alias."""
    data = copy.deepcopy(DATA)
    data["location_aliases.csv"].append({**data["location_aliases.csv"][0],
                                         "location_key": location, "alias": alias,
                                         "context_key": "", "kind": "common"})
    return loc.Gazetteer.from_dataset(data)


def test_full_span_local_alias_outranks_an_other_city_word():
    """A whole-span authoritative local alias wins (R17 step 2 precedes step 6);
    a partial local match never hides an explicitly named other city."""
    gazetteer = _with_local_alias("lahore test road", "clf")  # synthetic name
    check(run("Lahore Test Road", gazetteer=gazetteer), R, ["clf"])
    check(run("Lahore Test Road Clifton", gazetteer=gazetteer), O, [])
    check(run("Clifton Lahore", gazetteer=gazetteer), O, [])
    check(run("Lahore", gazetteer=gazetteer), O, [])


def test_invalid_arguments():
    with pytest.raises(TypeError):
        loc.resolve(None, None, G)
    with pytest.raises(TypeError):
        loc.resolve(6, None, G)
    with pytest.raises(ValueError):
        loc.resolve("DHA 6", "no-such-node", G)


def test_city_code_is_carried():
    assert run("DHA 6").city == "KHI"
    assert loc.resolve("Clifton", None, G, city_code="KHI").to_dict()["city"] == "KHI"


# --- result contract (R17 3.1) ---

def test_to_dict_has_the_r17_shape():
    resolved = run("DHA chay").to_dict()
    assert resolved == {"nodes": ["dha-ph6"], "mode": "EXACT", "city": "KHI",
                        "status": "RESOLVED", "raw": "DHA chay", "method": "exact",
                        "key": "dha 6"}
    ambiguous = run("Block 6").to_dict()
    assert ambiguous["nodes"] == [] and ambiguous["mode"] is None
    assert ambiguous["candidates"] == [
        {"location_id": "clf-b6", "score": 1.0},
        {"location_id": "gli-b6", "score": 1.0},
        {"location_id": "pec-b6", "score": 1.0}]
    assert "candidates" not in run("Malir Cantt").to_dict()
    assert run("DHA Lahore").to_dict()["other_city"] == "lahore"


INVALID_RESULTS = [
    {"status": "MAYBE"},
    {"status": R},  # no nodes
    {"status": R, "nodes": ("dha",)},  # no mode
    {"status": R, "nodes": ("dha",), "mode": "NEAR"},
    {"status": R, "nodes": ("dha",), "mode": loc.SUBTREE,
     "candidates": (loc.Candidate("clf"),)},
    {"status": A},  # no candidates
    {"status": A, "candidates": (loc.Candidate("a"), loc.Candidate("a"))},
    {"status": A, "candidates": (loc.Candidate("a"),), "nodes": ("a",)},
    {"status": U, "mode": loc.EXACT},
    {"status": U, "candidates": (loc.Candidate("a"),)},
    {"status": O},  # no other_city
    {"status": U, "other_city": "lahore"},
    {"status": R, "nodes": ("dha",), "mode": loc.SUBTREE, "area_hint": "dha"},
    {"status": U, "method": "guessed"},
    {"status": U, "city": "Karachi"},
]


@pytest.mark.parametrize("fields", INVALID_RESULTS)
def test_result_contract_rejects_invalid_combinations(fields):
    values = {"raw": "x", "city": "KHI", **fields}
    with pytest.raises(ValueError):
        loc.LocationResult(**values)


@pytest.mark.parametrize("fields", [
    {"status": U, "raw": None},
    {"status": A, "candidates": ("clf",)},
    {"status": R, "nodes": ["dha"], "mode": loc.SUBTREE},
])
def test_result_contract_rejects_wrong_types(fields):
    values = {"raw": "x", "city": "KHI", **fields}
    with pytest.raises(TypeError):
        loc.LocationResult(**values)


@pytest.mark.parametrize("score", [0, -0.1, 1.01])
def test_candidate_score_range(score):
    with pytest.raises(ValueError):
        loc.Candidate("clf", score)


def test_results_are_immutable_and_deterministic():
    first, second = run("Block 6"), run("Block 6")
    assert first == second
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.status = R


# --- gazetteer validation ---

def _places():
    return [loc.Place("khi", None, "city", "Karachi", "Karachi", city_code="KHI"),
            loc.Place("a1", "khi", "area", "Area", "Area")]


@pytest.mark.parametrize(("places", "aliases"), [
    (_places() + [loc.Place("a1", "khi", "area", "Dup", "Dup")], []),
    (_places() + [loc.Place("s1", "nope", "sub_area", "Block 1", "B1")], []),
    (_places() + [loc.Place("s1", "a1", "street", "X", "X")], []),
    ([loc.Place("x", "x", "city", "X", "X")], []),
    (_places(), [loc.Alias("nope", "area")]),
    (_places(), [loc.Alias("a1", "area", context_id="khi")]),   # context must be an area
    (_places(), [loc.Alias("a1", "area", context_id="a1")]),
    (_places(), [loc.Alias("a1", "!!!")]),                      # no usable key
])
def test_gazetteer_rejects_inconsistent_snapshots(places, aliases):
    with pytest.raises(loc.GazetteerError):
        loc.Gazetteer(places, aliases)


# --- fuzzy (R17 3.3 step 4; owner decisions 2026-10-12) -----------------------
# Auto: score >= 0.85, margin >= 0.15 over the next location, character length
# rule. Otherwise the top 3 at or above the 0.30 floor (a project choice), else
# UNKNOWN. Guards: >= 4 letters of place name (span and alias), identical
# identifiers, context. Scores are pg_trgm similarity().

FUZZY = [
    # span, status, expected (node, or candidates in ranked order)
    ("bukhari comercial", R, ["dha-ph6-bukhari"]),       # 0.85, margin 0.45
    ("ittehad comercial", R, ["dha-ph6-ittehad"]),       # 0.85
    ("zamzama comercial", A, ["dha-ph5-zamzama"]),        # 0.842; Badar / Sehar: name-word guard
    ("cliftn", A, ["clf"]),
    ("khadda markit", A, ["dha-ph5-khadda"]),
    ("north nazimabaad", A, ["nnz"]),                    # 0.82: below 0.85
    ("northnazimabad", A, ["nnz"]),
    ("bahriya town", A, ["btk"]),
    ("tariq rd", A, ["pec-tariq-road"]),
    ("sports city", A, ["btk-sports-city"]),              # parent "bahria" omitted; not DHA City
    ("zamzama", A, ["dha-ph5-zamzama"]),                 # "commercial" may be omitted
    ("zamzama comm", A, ["dha-ph5-zamzama"]),            # truncation of "commercial"
    ("tarik road", A, ["pec-tariq-road"]),
    ("cliftn block 5", A, ["clf-b5"]),
    ("gulshan e iqbaal", A, ["gli"]),                    # 0.83: asks, not auto
    ("pecs", A, ["pec"]),                                # exactly 4 letters
    ("nort nazimabad block h", A, ["nnz-bh"]),           # via the display name, H kept
    ("north nazimabd block l", A, ["nnz-bl"]),
    ("bahriya precinct 10", A, ["btk-p10"]),
]


@pytest.mark.parametrize(("span", "status", "expected"), FUZZY)
def test_fuzzy_matches(span, status, expected):
    result = run(span)
    assert result.method == "fuzzy"
    assert result.status == status, result.to_dict()
    if status == R:
        assert list(result.nodes) == expected
    else:
        assert [c.location_id for c in result.candidates] == expected  # ranked order
        scores = [c.score for c in result.candidates]
        assert scores == sorted(scores, reverse=True) and min(scores) >= 0.30


@pytest.mark.parametrize("span", [
    "malir cantt", "hyderabad", "korangi", "saddar", "bahadurabad", "gulistan e jauhar",
    "defence view",
    "btq", "dkc",                    # fewer than 4 letters of name
    "gulshn block 1",                # only Clifton has a "block 1": not offered
    "bahriya precinct 12",           # no precinct 12: never Precinct 1 / 10
    "nort nazimabad block k",
    "gulshn block 13",               # name-only score 0.28: below the floor
    "Defence City", "Defense City",  # unresolved alias: never offered as DHA City
])
def test_fuzzy_never_guesses(span):
    result = run(span)
    check(result, U, [])
    assert result.method == "none"


def test_fuzzy_scores_are_pg_trgm_similarity():
    result = run("zamzama comercial")
    assert result.candidates[0].score == float(
        loc.similarity("zamzama comercial", "zamzama commercial"))
    # Values checked against PostgreSQL 18.6 pg_trgm in the 7.1 discovery.
    for a, b, value in (("klifton", "clifton", (5, 11)), ("dha 6", "dha 8", (4, 8)),
                        ("nazimabad north", "north nazimabad", (1, 1)),
                        ("bukhari comercial", "bukhari commercial", (17, 20)),
                        ("d6", "p6", (0, 1))):
        assert loc.similarity(a, b) == Fraction(*value)


def test_fuzzy_guard_helpers():
    assert loc.identifiers(["dha", "phase", "2", "ext"]) == ("2", "ext")
    assert loc.identifiers(["north", "nazimabad", "block", "h"]) == ("h",)
    assert loc.identifiers(["gulshan", "e", "iqbal"]) == ()     # "e" is not after a level
    assert loc.name_letters(["block", "5"]) == 0
    assert loc.name_part(["north", "nazimabad", "block", "h"]) == ["north", "nazimabad"]
    assert loc.structure(["dha", "phase", "2", "ext"]) == (("2", "ext"), ("phase",))
    assert loc.name_letters(["btq"]) == 3 and loc.name_letters(["pecs"]) == 4
    assert loc.length_difference("north nazimabad", "northnazimabad") == 0
    assert loc.length_difference("abcdefghij", "abcdefg") == Fraction(3, 10)


# --- name-word compatibility guard (owner decision 2026-10-13, PR #31 review) ---
# Unknown Karachi places that share one word with a covered place must not be
# offered that place: not auto-resolved, not offered as a clarification candidate,
# and not given its area as area_hint.

UNRELATED = [
    "north karachi", "North Karachi", "north", "North Karachi block h",
    "Gulshan-e-Maymar", "Gulshan-e-Jauhar", "Gulshan-e-Hadeed", "Gulshan e Jamal",
    "gulshan-e-maymar block 13",
    "Gulshan Maymar", "Gulshan Hadeed", "Gulshan Jauhar", "Gulshan Jauhar block 13",
    "Gulshan Jamal", "gulshan maymar block 13",          # missing-"e" variants
    "Nazimabad", "Nazimabad block h", "nazimabad block l",
]


@pytest.mark.parametrize("active", [None, "gli", "nnz", "dha"])
@pytest.mark.parametrize("span", UNRELATED)
def test_unrelated_places_are_never_offered(span, active):
    result = run(span, active)
    check(result, U, [])
    assert result.area_hint is None and result.method == "none"


@pytest.mark.parametrize(("span", "expected"), [
    ("Gulshan", ["gli"]), ("gulshan iqbal", ["gli"]), ("Gulshan-e-Iqbal", ["gli"]),
    ("gulshan 13", ["gli-b13"]), ("gulshan block 13", ["gli-b13"]),
    ("Gulshan block chhe", ["gli-b6"]), ("gulshan chhe", ["gli-b6"]),
    ("Gulshan Karachi", ["gli"]),                        # the city word is stripped first
])
def test_bare_gulshan_shorthand_still_means_gulshan_e_iqbal(span, expected):
    check(run(span), R, expected)


def test_bare_gulshan_is_expanded_only_in_shorthand_contexts():
    assert loc.clean("Gulshan Maymar") == "gulshan maymar"
    assert loc.clean("Gulshan-e-Maymar") == "gulshan e maymar"
    assert loc.clean("gulshan") == "gulshan e iqbal"
    assert loc.clean("gulshan iqbal") == "gulshan e iqbal"
    assert loc.clean("gulshan blk 6") == "gulshan e iqbal block 6"
    assert loc.clean("gulshan chhe") == "gulshan e iqbal 6"


def test_name_word_guard_rules():
    ok = loc.names_compatible
    assert ok(["cliftn"], ["clifton"])                          # typo, word score 0.5
    assert ok(["zamzama"], ["zamzama", "commercial"])           # optional place-type word
    assert ok(["tariq", "rd"], ["tariq", "road"])               # "rd" is not distinctive
    assert ok(["northnazimabad"], ["north", "nazimabad"])       # fused form
    assert ok(["sports", "city"], ["bahria", "sports", "city"], {("bahria",)})  # parent
    assert not ok(["gulshan", "e", "maymar"], ["gulshan", "e", "iqbal"])   # span word lost
    assert not ok(["nazimabad"], ["north", "nazimabad"])        # candidate word unmatched
    assert not ok(["nazimabad"], ["north", "nazimabad"], {("north", "nazimabad")})
    assert not ok(["commercial"], ["zamzama", "commercial"])    # only the generic word
    assert not ok(["sports", "city"], ["dha", "city"])
    assert loc.OPTIONAL_CANDIDATE_WORDS == {"commercial", "road"}  # explicit and narrow


def test_name_word_guard_keeps_the_floor_and_auto_rules():
    assert loc.CANDIDATE_FLOOR == Fraction(30, 100) and loc.AUTO_SCORE == Fraction(85, 100)
    check(run("bukhari comercial"), R, ["dha-ph6-bukhari"])     # auto unchanged
    check(run("DHA bukhari comercial"), R, ["dha-ph6-bukhari"])
    assert run("gulshan e iqbaal").status == A                  # a typo is not forced to auto


def test_fuzzy_does_not_override_deterministic_results():
    check(run("Klifton"), R, ["clf"])                       # seeded alias: exact
    assert run("Klifton").method == "exact"
    assert run("block 5", "gli").method == "not_in_active_area"
    assert run("डीएचए फेज 6").method == "unsupported_script"


def test_fuzzy_named_places_are_not_limited_by_the_active_area():
    check(run("bukhari comercial", "btk"), R, ["dha-ph6-bukhari"])


def test_fuzzy_dedupes_by_location_before_the_margin():
    """Two near-identical aliases of one location must not create a tie with
    itself: the margin is measured against the next different location."""
    gazetteer = _with_local_alias("bukhari comercials", "dha-ph6-bukhari")  # synthetic
    result = run("bukhari comercial", gazetteer=gazetteer)
    check(result, R, ["dha-ph6-bukhari"])


def test_fuzzy_tie_between_locations_asks_in_stable_order():
    gazetteer = _with_local_alias("bukhari commercial", "dha-ph6-sehar")  # synthetic tie
    result = run("bukhari comercial", gazetteer=gazetteer)
    assert result.status == A and result.method == "fuzzy"
    assert [c.location_id for c in result.candidates][:2] == [
        "dha-ph6-bukhari", "dha-ph6-sehar"]                # equal score: display name
    assert result.candidates[0].score == result.candidates[1].score
    assert run("bukhari comercial", gazetteer=gazetteer) == result


def test_fuzzy_length_rule_blocks_auto_resolution():
    """A repeated word leaves the trigram set unchanged (score 1.0) but makes the
    span 10/27 = 37 % longer than the alias: asked, not auto-resolved (R-23 b)."""
    span, alias = "commercial bukhari commercial", "bukhari commercial"
    assert loc.similarity(span, alias) == 1
    assert loc.length_difference(span, alias) == Fraction(10, 27)
    result = run(span)
    assert result.status == A and result.method == "fuzzy"
    assert result.candidates[0].location_id == "dha-ph6-bukhari"


def test_fuzzy_context_aliases_need_their_area():
    data = copy.deepcopy(DATA)
    data["location_aliases.csv"].append({**data["location_aliases.csv"][0],  # synthetic
                                         "location_key": "btk-heights",
                                         "alias": "saahil tower", "context_key": "btk"})
    gazetteer = loc.Gazetteer.from_dataset(data)
    assert run("saahil towr", gazetteer=gazetteer).status == U   # context alias ineligible
    inside = run("saahil towr", "btk", gazetteer)
    assert inside.method == "fuzzy"
    assert "btk-heights" in [c.location_id for c in inside.candidates] + list(inside.nodes)


# --- scoped fuzzy below a recognised parent area (owner approval 2026-10-12) ----
# The rest of the span is compared only with that area's own descendants (the
# area's name removed from their keys); identifiers are kept exactly; never the
# area itself, never another area; a missing numbered child stays UNKNOWN.

SCOPED = [
    ("DHA bukhari comercial", R, ["dha-ph6-bukhari"]),        # 0.85 on "bukhari comercial"
    ("DHA zamzama comercial", A, ["dha-ph5-zamzama"]),
    ("DHA khadda markit", A, ["dha-ph5-khadda"]),
    ("Bahria sport city", A, ["btk-sports-city"]),            # 0.77: asks
    ("Bahria heigts", A, ["btk-heights"]),
    ("PECHS tariq rd", A, ["pec-tariq-road"]),
]


@pytest.mark.parametrize(("span", "status", "expected"), SCOPED)
def test_scoped_fuzzy_corrects_child_names(span, status, expected):
    result = run(span)
    assert result.method == "fuzzy" and result.status == status, result.to_dict()
    ids = list(result.nodes) if status == R else [c.location_id for c in result.candidates]
    assert ids == expected
    area = G.area_of(expected[0])
    assert all(G.is_within(i, area) and i != area for i in ids)   # never broadened


@pytest.mark.parametrize(("span", "hint"), [
    ("Bahria malir", "btk"),                 # nothing like it in Bahria
    ("Clifton bukhari comercial", "clf"),    # Bukhari is DHA's: not offered under Clifton
    ("Bahria precinct 12", "btk"),           # missing numbered child
    ("Gulshan block 1", "gli"),
    ("DHA phase 3", "dha"),
    ("DHA sector 13", "dha"),
    ("DHA City sektor 13", "dhc"),           # level word typo: identifiers never guessed
    ("Bahria precinct 27 heights", "btk"),
    ("DHA Defence City", "dha"),
])
def test_scoped_fuzzy_never_broadens_or_drops_identifiers(span, hint):
    result = run(span)
    check(result, U, [])
    assert result.area_hint == hint and result.method == "none"


def test_scoped_fuzzy_uses_context_aliases_of_that_area_only():
    data = copy.deepcopy(DATA)
    data["location_aliases.csv"].append({**data["location_aliases.csv"][0],  # synthetic
                                         "location_key": "btk-heights",
                                         "alias": "saahil tower", "context_key": "btk"})
    gazetteer = loc.Gazetteer.from_dataset(data)
    inside = run("Bahria saahil towr", gazetteer=gazetteer)
    assert inside.method == "fuzzy"
    assert "btk-heights" in list(inside.nodes) + [c.location_id for c in inside.candidates]
    check(run("Clifton saahil towr", gazetteer=gazetteer), U, [])


def test_display_names_are_fuzzy_only_not_aliases():
    """Hierarchical keys join parent and child names for fuzzy matching only."""
    assert ("north nazimabad block h", "nnz-bh", None) in G.fuzzy_keys()
    assert G.exact("north nazimabad block h") == frozenset()
    assert len(G.aliases) == 87
