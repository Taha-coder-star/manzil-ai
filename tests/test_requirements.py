"""Task 7.3: the requirement-state merge, offline (no database, no LLM, no network).

The gazetteer is the seeded demo dataset (dataset keys are the ids); every customer
sentence is fictional. Database, replay and worker behaviour is in
tests/test_requirements_db.py.
"""

import json
from datetime import UTC, datetime

import pytest

from app import ai_tools, demo_dataset, location, normalise, requirements
from app.requirements import (
    Context,
    Evidence,
    Pending,
    Shown,
    Source,
    expire,
    merge_patch,
    verify_ops,
    verify_span,
)

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
MSG = "00000000-0000-0000-0000-00000000000a"


@pytest.fixture(scope="module")
def gz():
    return location.Gazetteer.from_dataset(demo_dataset.load())


def make_ctx(gz, texts, *, pending=None, shown=None, call="c0"):
    sources = [Source(f"m{i}", kind, text, weak) for i, (kind, text, weak) in enumerate(
        t if len(t) == 3 else (*t, False) for t in texts)]
    names = sorted(p.display_name for p in gz.places.values() if p.level == "area")
    return Context(gz, requirements.id_map(gz), "KHI", "Karachi", 225, sources, pending,
                   "turn-1", call, NOW, shown=(lambda: shown), areas=names)


def run(gz, slots, patch, texts, *, pending=None, shown=None):
    """Merge one patch; texts = [(kind, text)] or [(kind, text, weak)]."""
    ctx = make_ctx(gz, texts, pending=pending, shown=shown)
    return merge_patch(ctx, slots, patch, verify_ops(patch, ctx.sources))


def op(slot, kind, span):
    return {"slot": slot, "op": kind, "raw_span": span}


def loc(*nodes, mode="SUBTREE", confirmed=True, raw="x"):
    return {"value": {"nodes": list(nodes), "mode": mode, "city": "KHI",
                      "status": "RESOLVED", "raw": raw, "terms": []},
            "raw": raw, "source": "text", "confidence": 1.0, "confirmed": confirmed,
            "updated_at": NOW.isoformat()}


def slot_entry(value, source="text"):
    return {"value": value, "raw": "x", "source": source, "confidence": 1.0,
            "confirmed": True, "updated_at": NOW.isoformat()}


def statuses(merge):
    return [(e["slot"], e["status"], e.get("detail")) for e in merge.entries]


def value(merge, slot):
    return merge.slots[slot]["value"]


def keys(merge):
    return {n["key"] for e in merge.entries for n in e.get("notices", ())}


def pending_for(gz, ids, *, op_="set", raw="block 6", turn="turn-0"):
    origin = {"op": op_, "raw": raw, "source": "text", "weak": False, "turn": turn,
              "call": "c0"}
    return Pending("p1", "location", [{"location_id": i, "score": 1.0, "name": i,
                                       "origin": origin} for i in ids], NOW, NOW, 0)


# --- R-22: raw spans must be in the actual turn ----------------------------------------

@pytest.mark.parametrize("span", [
    "DHA phase 6", "dha PHASE 6", "  DHA   phase\n6 ", "phase 6",
])
def test_a_span_that_is_in_a_message_is_verified_whatever_its_case_and_spacing(span):
    assert verify_span(span, [Source("m", "text", "Mujhe DHA Phase 6 mein ghar chahiye")])


@pytest.mark.parametrize("span", [
    "", "   ", "DHA phase 8", "Clifton", "DHA Phase 6 mein ghar chahiye aur", "ph6",
    "DHA\u200b Phase 6",            # a zero-width space is not whitespace to the check
    "ＤＨＡ Phase 6",                # full-width letters: no normalisation can widen a match
])
def test_adversarial_spans_are_not_in_the_turn(span):
    assert verify_span(span, [Source("m", "text", "Mujhe DHA Phase 6 mein ghar chahiye")]) is None


def test_a_span_cannot_straddle_two_messages():
    sources = [Source("m1", "text", "DHA phase 6"), Source("m2", "text", "8 crore tak")]
    assert verify_span("6 8 crore", sources) is None     # the joined turn text would pass
    assert verify_span("phase 6", sources).message_id == "m1"


def test_typed_text_wins_over_a_voice_transcript_with_the_same_words():
    sources = [Source("v", "voice", "Clifton block 5", True), Source("t", "text", "Clifton")]
    assert verify_span("Clifton", sources).kind == "text"
    assert verify_span("block 5", sources).kind == "voice"


def test_only_the_accepted_transcript_counts_not_the_rejected_one(gz):
    # SpeechService puts only an accepted transcript into the turn; a message without one
    # contributes no source at all, so the rejected text can never carry a span.
    merge = run(gz, {}, [op("location", "set", "Clifton")], [("text", "kuch aur")])
    assert statuses(merge) == [("location", "rejected", "span_not_in_turn")]
    assert merge.slots == {}


def test_a_span_not_in_the_turn_changes_nothing_and_asks_nothing(gz):
    before = {"purpose": slot_entry("sale")}
    merge = run(gz, before, [op("location", "set", "Clifton")], [("text", "hello")])
    assert merge.slots == before and not merge.changed
    assert [e["status"] for e in merge.entries] == ["rejected"]


# --- the location slot -------------------------------------------------------------------

def test_a_resolved_location_is_stored_with_its_method_and_key(gz):
    merge = run(gz, {}, [op("location", "set", "Clifton")], [("text", "Clifton mein ghar")])
    entry = merge.slots["location"]
    assert entry["value"]["nodes"] == ["clf"] and entry["value"]["mode"] == "SUBTREE"
    assert entry["value"]["status"] == "RESOLVED" and entry["value"]["city"] == "KHI"
    assert entry["method"] == "exact" and entry["key"] == "clifton"
    assert entry["confirmed"] is True and entry["source"] == "text"
    assert keys(merge) == {"requirements_saved"} - {"x"} or "requirements_saved" in {
        n.key for n in merge.notices}


def test_set_replaces_and_add_extends_the_union(gz):
    state = {"location": loc("dha-ph6", mode="EXACT")}
    added = run(gz, state, [op("location", "add", "Phase 8")], [("text", "Phase 8 bhi dekho")])
    assert value(added, "location")["nodes"] == ["dha-ph6", "dha-ph8"]
    replaced = run(gz, state, [op("location", "set", "Phase 8")],
                   [("text", "Phase 8 mein dikhao")])
    assert value(replaced, "location")["nodes"] == ["dha-ph8"]


def test_adding_a_node_already_there_keeps_one_copy(gz):
    state = {"location": loc("dha-ph6", mode="EXACT")}
    merge = run(gz, state, [op("location", "add", "DHA Phase 6")], [("text", "DHA Phase 6 bhi")])
    assert value(merge, "location")["nodes"] == ["dha-ph6"]


def test_the_active_area_is_derived_from_the_stored_location(gz):
    ids = requirements.id_map(gz)
    assert requirements.active_area(gz, loc("dha-ph6")["value"], ids) == "dha"
    assert requirements.active_area(gz, loc("clf")["value"], ids) == "clf"
    assert requirements.active_area(gz, loc("khi")["value"], ids) is None   # city only
    assert requirements.active_area(gz, loc("dha-ph6", "clf-b5")["value"], ids) is None
    assert requirements.active_area(gz, loc("dha-ph6", "khi")["value"], ids) == "dha"
    assert requirements.active_area(gz, None, ids) is None


def test_a_stale_node_the_gazetteer_no_longer_knows_never_sets_the_context(gz):
    ids = requirements.id_map(gz)
    assert requirements.active_area(gz, loc("removed-node")["value"], ids) is None
    state = {"location": loc("removed-node")}
    merge = run(gz, state, [op("location", "set", "block 6")], [("text", "block 6")])
    # No context: "block 6" is valid in three areas, so the customer is asked.
    assert statuses(merge) == [("location", "clarify", None)]


def test_bare_levels_resolve_in_the_stored_area_only(gz):
    state = {"location": loc("dha-ph6", mode="EXACT")}
    merge = run(gz, state, [op("location", "set", "Phase 8")], [("text", "Phase 8")])
    assert value(merge, "location")["nodes"] == ["dha-ph8"]
    assert merge.slots["location"]["method"] == "level_in_area"
    clifton = {"location": loc("clf")}
    unknown = run(gz, clifton, [op("location", "set", "phase 8")], [("text", "phase 8")])
    assert statuses(unknown) == [("location", "unknown", None)]
    assert unknown.slots == clifton                       # nothing changed


def test_unknown_area_hint_is_wording_only_and_never_the_context(gz):
    state = {"location": loc("clf")}
    first = run(gz, state, [op("location", "set", "phase 8")], [("text", "phase 8")])
    assert first.slots == state and keys(first) == {"location_unknown_in_area"}
    notice = next(n for n in first.notices if n.key == "location_unknown_in_area")
    assert dict(notice.params) == {"area": "Clifton"}
    # A second turn starts from the same stored state: the hint left no trace.
    again = run(gz, first.slots, [op("location", "set", "block 5")], [("text", "block 5")])
    assert value(again, "location")["nodes"] == ["clf-b5"]


def test_bahria_precinct_12_is_unknown_and_changes_nothing(gz):
    state = {"location": loc("dha-ph6", mode="EXACT")}
    merge = run(gz, state, [op("location", "set", "Bahria precinct 12")],
                [("text", "Bahria precinct 12")])
    assert merge.slots == state
    assert statuses(merge) == [("location", "unknown", None)]
    assert keys(merge) == {"location_unknown_in_area"}
    assert merge.effects == []


def test_unknown_names_list_the_covered_areas(gz):
    merge = run(gz, {}, [op("location", "set", "Malir Cantt")], [("text", "Malir Cantt")])
    notice = merge.notices[0]
    assert notice.key == "location_unknown"
    assert "Clifton" in dict(notice.params)["areas"]
    assert merge.slots == {}


def test_other_cities_are_out_of_coverage_and_never_stored(gz):
    merge = run(gz, {}, [op("location", "set", "Bahria Lahore")], [("text", "Bahria Lahore")])
    assert statuses(merge) == [("location", "out_of_coverage", None)]
    assert merge.slots == {} and keys(merge) == {"location_out_of_coverage"}
    notice = merge.notices[0]
    assert dict(notice.params) == {"city": "Karachi", "other": "Lahore"}


def test_karachi_is_served_and_lahore_declined_in_the_same_reply(gz):
    merge = run(gz, {}, [op("location", "set", "Clifton"), op("location", "add", "Lahore")],
                [("text", "Clifton aur Lahore mein dekhna hai")])
    assert value(merge, "location")["nodes"] == ["clf"]
    assert [s for _, s, _ in statuses(merge)] == ["applied", "out_of_coverage"]
    assert {n.key for n in merge.notices} == {"requirements_saved",
                                              "location_out_of_coverage"}


def test_dha_and_dha_city_stay_distinct(gz):
    city = run(gz, {}, [op("location", "set", "DHA City")], [("text", "DHA City mein plot")])
    assert value(city, "location")["nodes"] == ["dhc"]
    phase = run(gz, {}, [op("location", "set", "DHA phase 6")], [("text", "DHA phase 6")])
    assert value(phase, "location")["nodes"] == ["dha-ph6"]
    # The context of DHA City never turns "sector 13" or "DHA" into the other area.
    state = {"location": loc("dhc")}
    sector = run(gz, state, [op("location", "set", "sector 13")], [("text", "sector 13")])
    assert value(sector, "location")["nodes"] == ["dhc-s13"]
    dha = run(gz, state, [op("location", "set", "DHA")], [("text", "DHA")])
    assert value(dha, "location")["nodes"] == ["dha"]
    wrong = run(gz, state, [op("location", "set", "DHA sector 13")], [("text", "DHA sector 13")])
    assert wrong.slots == state            # "DHA sector 13" is never DHA City's
    defence = run(gz, {}, [op("location", "set", "Defence City")], [("text", "Defence City")])
    assert defence.slots == {} and keys(defence) <= {"location_unknown"}


def test_two_places_in_one_patch_are_a_union_not_a_replacement(gz):
    merge = run(gz, {}, [op("location", "set", "Clifton"), op("location", "set", "DHA")],
                [("text", "Clifton ya DHA")])
    assert value(merge, "location")["nodes"] == ["clf", "dha"]


def test_a_second_ambiguity_in_one_patch_waits_for_the_first_answer(gz):
    merge = run(gz, {}, [op("location", "set", "block 6"), op("location", "add", "P6")],
                [("text", "block 6 ya P6")])
    assert [e["status"] for e in merge.entries] == ["clarify", "ask"]
    assert len([e for e in merge.effects if e[0] == "open"]) == 1


# --- ambiguity and the pending clarification -------------------------------------------

def test_an_ambiguous_location_opens_one_question_and_changes_no_slot(gz):
    state = {"purpose": slot_entry("sale")}
    merge = run(gz, state, [op("location", "set", "block 6")], [("text", "block 6 chahiye")])
    assert merge.slots == state
    (effect,) = merge.effects
    assert effect[0] == "open" and effect[1] == "location"
    ids = [c["location_id"] for c in effect[2]]
    assert ids == ["clf-b6", "gli-b6", "pec-b6"]
    assert all(c["origin"]["raw"] == "block 6" and c["origin"]["op"] == "set"
               and c["origin"]["turn"] == "turn-1" for c in effect[2])
    assert keys(merge) == {"location_ambiguous"}
    options = dict(merge.notices[0].params)["options"]
    assert "Clifton Block 6" in options and " ya " in options


def test_p6_without_context_asks_dha_or_bahria(gz):
    merge = run(gz, {}, [op("location", "set", "P6")], [("text", "P6 mein ghar")])
    ids = {c["location_id"] for c in merge.effects[0][2]}
    assert ids == {"dha-ph6", "btk-p6"}


def test_one_fuzzy_candidate_is_a_valid_runtime_question(gz):
    # app.benchmark's ">= 2 candidates" rule is a gold-data rule, not a runtime rule.
    result = location.resolve("zamzama comercial", None, gz)
    assert result.status == location.AMBIGUOUS and len(result.candidates) == 1
    merge = run(gz, {}, [op("location", "set", "zamzama comercial")],
                [("text", "zamzama comercial mein dukaan")])
    (effect,) = merge.effects
    assert [c["location_id"] for c in effect[2]] == ["dha-ph5-zamzama"]
    assert keys(merge) == {"location_confirm"}
    assert merge.slots == {}


@pytest.mark.parametrize(("answer", "chosen"), [
    ("DHA", "dha-ph6"), ("Defence", "dha-ph6"), ("Phase 6", "dha-ph6"),
    ("Bahria", "btk-p6"), ("Bahria Town", "btk-p6"), ("Precinct 6", "btk-p6"),
])
def test_an_answer_names_one_stored_candidate(gz, answer, chosen):
    pending = pending_for(gz, ["dha-ph6", "btk-p6"], raw="P6")
    merge = run(gz, {}, [op("location", "set", answer)], [("text", answer)], pending=pending)
    assert value(merge, "location")["nodes"] == [chosen]
    assert merge.slots["location"]["method"] == "clarified"
    assert merge.slots["location"]["confirmed"] is True
    assert ("close", "p1", "resolved") in merge.effects
    assert value(merge, "location")["terms"][0]["raw"] == "P6"


def test_the_answer_applies_the_question_s_own_op(gz):
    pending = pending_for(gz, ["dha-ph6", "btk-p6"], op_="add", raw="P6")
    state = {"location": loc("clf")}
    merge = run(gz, state, [op("location", "set", "DHA")], [("text", "DHA")], pending=pending)
    assert value(merge, "location")["nodes"] == ["clf", "dha-ph6"]


@pytest.mark.parametrize("answer", ["Karachi", "kuch bhi", "haan", "DHA wala"])
def test_an_answer_that_names_no_single_candidate_leaves_the_question_open(gz, answer):
    pending = pending_for(gz, ["dha-ph6", "btk-p6"], raw="P6")
    merge = run(gz, {}, [op("location", "set", answer)], [("text", answer)], pending=pending)
    assert "location" not in merge.slots
    assert not any(e[0] == "close" for e in merge.effects)


def test_naming_an_area_that_still_holds_several_options_asks_again(gz):
    pending = pending_for(gz, ["dha-ph6", "dha-ph7"], raw="phase")
    merge = run(gz, {}, [op("location", "set", "DHA")], [("text", "DHA")], pending=pending)
    assert merge.slots == {} and merge.effects == []
    assert statuses(merge) == [("location", "ask", "still_ambiguous")]
    assert keys(merge) == {"location_reask"}


def test_a_place_the_question_did_not_offer_supersedes_it(gz):
    pending = pending_for(gz, ["dha-ph6", "btk-p6"], raw="P6")
    merge = run(gz, {}, [op("location", "set", "Clifton")], [("text", "Clifton")],
                pending=pending)
    assert value(merge, "location")["nodes"] == ["clf"]
    assert ("close", "p1", "cancelled") in merge.effects


def test_a_new_ambiguity_replaces_the_open_question(gz):
    pending = pending_for(gz, ["dha-ph6", "btk-p6"], raw="P6")
    merge = run(gz, {}, [op("location", "set", "block 6")], [("text", "block 6")],
                pending=pending)
    assert ("close", "p1", "cancelled") in merge.effects
    assert [e[0] for e in merge.effects].count("open") == 1


def test_other_slots_keep_accumulating_while_a_question_is_open(gz):
    pending = pending_for(gz, ["dha-ph6", "btk-p6"], raw="P6")
    merge = run(gz, {}, [op("property_types", "set", "house"), op("beds_min", "set", "3 bed")],
                [("text", "house 3 bed")], pending=pending)
    assert value(merge, "property_types") == ["house"] and value(merge, "beds_min") == 3
    assert not any(e[0] == "close" for e in merge.effects)


# --- expiry -------------------------------------------------------------------------------

def test_expiry_falls_back_to_the_one_area_the_candidates_share(gz):
    pending = pending_for(gz, ["dha-ph6-bukhari", "dha-ph6-ittehad"], raw="comercial")
    ctx = make_ctx(gz, [])
    slots, entries, notices, effects, changed = expire(ctx, {}, pending, "unanswered")
    assert changed and value_of(slots, "location")["nodes"] == ["dha"]
    assert value_of(slots, "location")["mode"] == "SUBTREE"
    assert slots["location"]["source"] == "inferred" and slots["location"]["confirmed"] is False
    assert slots["location"]["echo"] == ["fallback"]
    assert effects == [("close", "p1", "expired")]
    assert [n.key for n in notices] == ["location_expired"]
    assert entries[0]["kind"] == "expiry" and entries[0]["detail"] == "unanswered"


def value_of(slots, slot):
    return slots[slot]["value"]


def test_expiry_across_areas_leaves_the_location_as_it_was(gz):
    pending = pending_for(gz, ["dha-ph6", "btk-p6"], raw="P6")
    state = {"location": loc("clf")}
    ctx = make_ctx(gz, [])
    slots, entries, notices, effects, changed = expire(ctx, state, pending, "unanswered")
    assert not changed and slots == state
    assert [(e["status"], e["kind"], e["slot"], e["op"]) for e in entries] == [
        ("expired", "expiry", "location", "clear")]
    assert entries[0]["notices"] == [{"key": "location_expired_none", "params": {}}]
    assert [n.key for n in notices] == ["location_expired_none"]
    assert effects == [("close", "p1", "expired")]


def test_expiry_of_an_add_question_extends_the_stored_location(gz):
    pending = pending_for(gz, ["dha-ph6-bukhari"], op_="add", raw="bukhari")
    state = {"location": loc("clf")}
    slots, *_ = expire(make_ctx(gz, []), state, pending, "timeout")
    assert value_of(slots, "location")["nodes"] == ["clf", "dha"]


# --- purpose, budget, size, types ---------------------------------------------------------

def test_explicit_purpose_budget_and_type_are_stored_with_provenance(gz):
    text = "house on rent, around 5 lakh per month"
    merge = run(gz, {}, [op("purpose", "set", "on rent"), op("property_types", "set", "house"),
                         op("budget", "set", "around 5 lakh per month")], [("text", text)])
    assert value(merge, "purpose") == "rent" and merge.slots["purpose"]["confirmed"] is True
    assert value(merge, "property_types") == ["house"]
    assert value(merge, "category") == "residential"
    assert merge.slots["category"]["source"] == "inferred"
    budget = value(merge, "budget")
    assert (budget["min"], budget["max"], budget["period"]) == (450_000, 550_000, "monthly")
    json.dumps(merge.slots)
    json.dumps(merge.entries)


def test_dependencies_between_ops_do_not_depend_on_their_order(gz):
    text = "500 gaz ka ghar, 8 crore tak, buy"
    ops = [op("budget", "set", "8 crore tak"), op("size", "set", "500 gaz"),
           op("property_types", "set", "ghar"), op("purpose", "set", "buy")]
    merge = run(gz, {}, ops, [("text", text)])
    assert value(merge, "budget")["period"] == "one_time"
    assert value(merge, "size")["nominal_sqyd"] == 500
    assert value(merge, "purpose") == "sale"


def test_a_budget_without_purpose_infers_it_as_inferred_and_unconfirmed(gz):
    merge = run(gz, {}, [op("budget", "set", "8 crore")], [("text", "8 crore ka ghar")])
    purpose = merge.slots["purpose"]
    assert purpose["value"] == "sale" and purpose["source"] == "inferred"
    assert purpose["confirmed"] is False and purpose["echo"] == ["inferred"]
    assert "purpose_inferred" in {n.key for n in merge.notices}
    assert value(merge, "budget")["period"] == "one_time"


def test_a_monthly_marker_is_explicit_rent_not_an_inference(gz):
    merge = run(gz, {}, [op("budget", "set", "50 hazar per month")],
                [("text", "50 hazar per month")])
    purpose = merge.slots["purpose"]
    assert purpose["value"] == "rent" and purpose["confirmed"] is True
    assert purpose["source"] == "text"


def test_an_amount_between_the_thresholds_asks_the_purpose(gz):
    merge = run(gz, {}, [op("budget", "set", "30 lakh")], [("text", "30 lakh")])
    assert "purpose" not in merge.slots
    (effect,) = merge.effects
    assert effect[:2] == ("open", "purpose")
    assert "purpose_ask" in {n.key for n in merge.notices}


def test_an_explicit_purpose_answer_closes_the_purpose_question(gz):
    pending = Pending("p2", "purpose", [{"value": "sale", "origin": {}},
                                        {"value": "rent", "origin": {}}], NOW, NOW, 0)
    merge = run(gz, {}, [op("purpose", "set", "kiraye pe")], [("text", "kiraye pe")],
                pending=pending)
    assert value(merge, "purpose") == "rent"
    assert ("close", "p2", "resolved") in merge.effects


def test_buy_to_rent_moves_the_budget_and_asks_unless_an_amount_is_given(gz):
    state = {"purpose": slot_entry("sale"), "budget": slot_entry(
        {"min": None, "max": 80_000_000, "mode": "max", "period": "one_time"}),
        "property_types": slot_entry(["house"]), "location": loc("dha-ph6")}
    merge = run(gz, state, [op("purpose", "set", "rent pe")], [("text", "rent pe bhi mil sakta hai?")])
    assert value(merge, "purpose") == "rent"
    assert "budget" not in merge.slots
    assert merge.slots["previous_budget"]["value"]["max"] == 80_000_000
    assert merge.slots["previous_budget"]["moved_by"] == "purpose_switch"
    assert value(merge, "location")["nodes"] == ["dha-ph6"]
    assert value(merge, "property_types") == ["house"]
    assert "budget_ask_rent" in {n.key for n in merge.notices}
    given = run(gz, state, [op("purpose", "set", "rent"), op("budget", "set", "3 lakh mahana")],
                [("text", "rent, 3 lakh mahana")])
    assert value(given, "budget")["max"] is None or value(given, "budget")["period"] == "monthly"
    assert value(given, "budget")["period"] == "monthly"
    assert given.slots["previous_budget"]["value"]["max"] == 80_000_000
    assert "budget_ask_rent" not in {n.key for n in given.notices}


HOUSE_SEARCH = {
    "purpose": slot_entry("sale"), "property_types": slot_entry(["house"]),
    "location": loc("dha-ph6"), "beds_min": slot_entry(4),
    "size": slot_entry({"value": 500, "unit": "sq_yd", "sqft": 4500, "nominal_sqyd": 500}),
    "budget": slot_entry({"min": None, "max": 80_000_000, "mode": "max",
                          "period": "one_time"})}
OFFICE_TEXT = "office on rent in karachi around 5 lakh per month"


def test_a_house_to_office_switch_drops_the_old_size_and_asks_for_it(gz):
    # Live 2026-10-16: the house search's 500 sq yd made "office ... 5 lakh" find nothing.
    merge = run(gz, HOUSE_SEARCH, [op("property_types", "set", "office"),
                                   op("purpose", "set", "on rent"),
                                   op("budget", "set", "around 5 lakh per month")],
                [("text", OFFICE_TEXT)])
    assert value(merge, "property_types") == ["office"]
    assert "size" not in merge.slots and "beds_min" not in merge.slots
    [ask] = [e for e in merge.entries if e["status"] == "ask"]
    assert (ask["slot"], ask["detail"]) == ("size", "type_switch")
    assert merge.entries[-1] is ask   # the newest size entry: open_asks holds the search
    assert "size_ask" in {n.key for n in merge.notices}


def test_a_switch_that_states_a_new_size_keeps_it_and_asks_nothing(gz):
    merge = run(gz, HOUSE_SEARCH, [op("property_types", "set", "office"),
                                   op("size", "set", "1500 sq ft")],
                [("text", "office chahiye 1500 sq ft")])
    assert value(merge, "size")["sqft"] == 1500
    assert "beds_min" not in merge.slots
    assert not [e for e in merge.entries if e["status"] == "ask"]


def test_a_switch_within_homes_or_a_first_search_keeps_or_asks_no_size(gz):
    flat = run(gz, HOUSE_SEARCH, [op("property_types", "set", "flat")], [("text", "flat bhi")])
    assert value(flat, "size")["value"] == 500 and value(flat, "beds_min") == 4
    first = run(gz, {}, [op("property_types", "set", "office"),
                         op("budget", "set", "around 5 lakh per month")],
                [("text", OFFICE_TEXT)])
    assert not [e for e in first.entries if e["status"] == "ask"]   # Demo Scope scenario 1


def test_the_same_purpose_again_does_not_touch_the_budget(gz):
    state = {"purpose": slot_entry("sale"),
             "budget": slot_entry({"min": None, "max": 8, "mode": "max", "period": "one_time"})}
    merge = run(gz, state, [op("purpose", "set", "buy")], [("text", "buy")])
    assert "budget" in merge.slots and "previous_budget" not in merge.slots


def test_a_budget_replaces_the_budget(gz):
    state = {"purpose": slot_entry("sale"),
             "budget": slot_entry({"min": None, "max": 80_000_000, "mode": "max",
                                   "period": "one_time"})}
    merge = run(gz, state, [op("budget", "set", "10 crore kar do")],
                [("text", "budget 10 crore kar do")])
    assert value(merge, "budget")["max"] is not None
    assert value(merge, "budget")["stated_pkr"] == 100_000_000


def test_cheaper_uses_the_shown_prices_excludes_them_and_skips_budget_relaxation(gz):
    shown = Shown(("a", "b", "c"), (75_000_000, 69_000_000, 72_000_000), (500, 500, 500))
    state = {"purpose": slot_entry("sale")}
    merge = run(gz, state, [op("budget", "relative", "Kuch sasta")],
                [("text", "Kuch sasta dikhao")], shown=shown)
    budget = value(merge, "budget")
    assert budget["max"] == 66_930_000 and budget["min"] is None
    assert budget["no_budget_relaxation"] is True and budget["relative"] == "cheaper"
    assert merge.slots["excluded_property_ids"]["value"] == ["a", "b", "c"]
    assert merge.entries[0]["excluded"] == 3
    again = run(gz, merge.slots, [op("budget", "relative", "kuch sasta")],
                [("text", "kuch sasta")], shown=Shown(("c", "d"), (60_000_000, 61_000_000), (1, 1)))
    assert value(again, "budget")["max"] == 58_200_000
    assert again.slots["excluded_property_ids"]["value"] == ["a", "b", "c", "d"]


def test_a_relative_phrase_with_no_prior_search_invents_no_amount(gz):
    merge = run(gz, {}, [op("budget", "relative", "kuch sasta")], [("text", "kuch sasta")])
    assert "budget" not in merge.slots
    assert statuses(merge) == [("budget", "ask", "no_prior_search")]
    assert keys(merge) == {"budget_ask"}


def test_higher_and_premium_use_the_formulas(gz):
    state = {"purpose": slot_entry("sale"), "budget": slot_entry(
        {"min": None, "max": 80_000_000, "mode": "max", "period": "one_time",
         "stated_pkr": 80_000_000})}
    higher = run(gz, state, [op("budget", "relative", "thoda upar")], [("text", "thoda upar")])
    assert value(higher, "budget")["max"] == 88_000_000
    shown = Shown(("a",), (70_000_000,), (500,))
    premium = run(gz, state, [op("budget", "relative", "aur mehnga")], [("text", "aur mehnga")],
                  shown=shown)
    assert value(premium, "budget")["min"] == 72_100_000
    assert value(premium, "budget")["max"] == 80_000_000
    none = run(gz, {}, [op("budget", "relative", "thoda upar")], [("text", "thoda upar")])
    assert none.slots == {} and keys(none) == {"budget_ask"}


def test_an_absolute_amount_with_a_relative_label_is_just_the_amount(gz):
    merge = run(gz, {}, [op("budget", "relative", "8 crore tak")], [("text", "8 crore tak")])
    assert value(merge, "budget")["max"] == 80_000_000


def test_sizes_use_the_type_and_ask_when_the_unit_is_unclear(gz):
    house = run(gz, {}, [op("property_types", "set", "house"), op("size", "set", "500 yards")],
                [("text", "house 500 yards")])
    assert value(house, "size")["sqft"] == 4500 and value(house, "size")["nominal_sqyd"] == 500
    bare = run(gz, {}, [op("size", "set", "400 ka")], [("text", "400 ka plot")])
    assert "size" not in bare.slots and keys(bare) == {"size_unit_ask"}
    typed = run(gz, {}, [op("property_types", "set", "plot"), op("size", "set", "400 ka")],
                [("text", "plot 400 ka")])
    assert value(typed, "size")["unit"] == "sq_yd"


def test_a_marla_follows_the_agency_basis(gz):
    ctx = make_ctx(gz, [("text", "10 marla")])
    ctx.marla_sqft = normalise_marla()
    merge = merge_patch(ctx, {}, [op("size", "set", "10 marla")],
                        verify_ops([op("size", "set", "10 marla")], ctx.sources))
    assert value(merge, "size")["sqft"] == 2722 or value(merge, "size")["sqft"] == 2723


def normalise_marla():
    from decimal import Decimal
    return Decimal("272.25")


def test_property_types_set_add_remove_and_category(gz):
    state = {"property_types": slot_entry(["house"])}
    added = run(gz, state, [op("property_types", "add", "flat bhi")], [("text", "flat bhi")])
    assert value(added, "property_types") == ["house", "apartment"]
    removed = run(gz, added.slots, [op("property_types", "remove", "house")],
                  [("text", "house nahi")])
    assert value(removed, "property_types") == ["apartment"]
    replaced = run(gz, state, [op("property_types", "set", "shops")], [("text", "shops")])
    assert value(replaced, "property_types") == ["shop"]
    assert value(replaced, "category") == "commercial"
    mixed = run(gz, {}, [op("property_types", "set", "shop ya house")],
                [("text", "shop ya house")])
    assert "category" not in mixed.slots
    unknown = run(gz, {}, [op("property_types", "set", "godown")], [("text", "godown")])
    assert statuses(unknown) == [("property_types", "rejected", "unparsed")]


def test_counts_are_plain_numbers_and_implausible_ones_are_rejected(gz):
    ok = run(gz, {}, [op("beds_min", "set", "3 bed"), op("baths_min", "set", "do bath")],
             [("text", "3 bed, do bath")])
    assert value(ok, "beds_min") == 3 and value(ok, "baths_min") == 2
    bad = run(gz, {}, [op("beds_min", "set", "300 bed")], [("text", "300 bed")])
    assert statuses(bad) == [("beds_min", "rejected", "unparsed")]


def test_clear_and_remove_drop_what_the_customer_withdrew(gz):
    state = {"budget": slot_entry({"max": 1}), "location": loc("dha-ph6", "dha-ph8")}
    cleared = run(gz, state, [op("budget", "clear", "budget")], [("text", "budget nahi")])
    assert "budget" not in cleared.slots and "location" in cleared.slots
    gone = run(gz, state, [op("location", "remove", "Phase 8")], [("text", "Phase 8 nahi")])
    assert value(gone, "location")["nodes"] == ["dha-ph6"]
    whole = run(gz, state, [op("location", "remove", "DHA")], [("text", "DHA nahi")])
    assert "location" not in whole.slots


def test_preferences_without_a_vocabulary_are_kept_as_words_never_as_filters(gz):
    merge = run(gz, {}, [op("soft_prefs", "set", "park facing"),
                         op("construction", "set", "dhang ka")],
                [("text", "park facing, dhang ka")])
    assert [p["raw"] for p in value(merge, "unmapped_prefs")] == ["park facing", "dhang ka"]
    assert "soft_prefs" not in merge.slots and "construction" not in merge.slots
    assert "pref_unmapped" in {n.key for n in merge.notices}


@pytest.mark.parametrize(("slot", "span", "expected"), [
    ("construction", "Under construction", ["under_construction"]),
    ("construction", "grey structure", ["grey_structure"]),
    ("construction", "off-plan", ["off_plan"]),
    ("construction", "ready to move", ["ready"]),
    ("possession", "on ground", ["on_ground"]),
    ("possession", "file", ["file"]),
    ("possession", "possession wala", ["possession"]),
    ("possession", "balloted ya allotted", ["balloted", "allotted"]),
    ("investment", "investment ke liye", {"intent": True, "tags": []}),
    ("investment", "rental income ke liye investment",
     {"intent": True, "tags": ["rental_income"]}),
    ("investment", "capital gain aur pre launch",
     {"intent": True, "tags": ["capital_gain", "pre_launch"]}),
    ("investment", "qiston par", {"intent": True, "tags": ["installments"]}),
])
def test_construction_possession_and_investment_use_the_d11_vocabulary(gz, slot, span, expected):
    merge = run(gz, {}, [op(slot, "set", span)], [("text", span)])
    assert value(merge, slot) == expected


def test_vocabulary_slots_add_remove_and_clear(gz):
    state = {"construction": slot_entry(["ready"]),
             "investment": slot_entry({"intent": True, "tags": ["rental_income"]})}
    added = run(gz, state, [op("construction", "add", "off plan")], [("text", "off plan bhi")])
    assert value(added, "construction") == ["ready", "off_plan"]
    removed = run(gz, state, [op("construction", "remove", "ready")], [("text", "ready nahi")])
    assert "construction" not in removed.slots
    tag = run(gz, state, [op("investment", "add", "capital gain")], [("text", "capital gain")])
    assert value(tag, "investment")["tags"] == ["rental_income", "capital_gain"]
    gone = run(gz, state, [op("investment", "clear", "investment")], [("text", "investment nahi")])
    assert "investment" not in gone.slots


def test_more_beds_is_one_more_than_the_stored_minimum_and_never_invented(gz):
    state = {"beds_min": slot_entry(3)}
    merge = run(gz, state, [op("beds_min", "relative", "ek aur bedroom")],
                [("text", "ek aur bedroom chahiye")])
    assert value(merge, "beds_min") == 4 and merge.slots["beds_min"]["relative"] == "more_beds"
    asked = run(gz, {}, [op("beds_min", "relative", "one more bedroom")],
                [("text", "one more bedroom")])
    assert asked.slots == {} and keys(asked) == {"beds_ask"}
    label = run(gz, state, [op("beds_min", "relative", "kuch")], [("text", "kuch")])
    assert label.slots == state
    top = run(gz, {"beds_min": slot_entry(20)}, [op("beds_min", "relative", "more bedrooms")],
              [("text", "more bedrooms")])
    assert value(top, "beds_min") == 20


def test_budget_up_is_the_higher_relative_op(gz):
    state = {"purpose": slot_entry("sale"), "budget": slot_entry(
        {"min": None, "max": 50_000_000, "mode": "max", "period": "one_time"})}
    for phrase in ("budget barha do", "thoda upar"):
        merge = run(gz, state, [op("budget", "relative", phrase)], [("text", phrase)])
        assert value(merge, "budget")["max"] == 55_000_000
        assert value(merge, "budget")["relative"] == "higher"


# --- plausibility (R17 4.7 / edge 15) ------------------------------------------------------------

BAND = (60_000_000, 90_000_000)     # active DHA 6 sale houses, fictional


def plaus(gz, slots, patch, texts, band=BAND, prev=None):
    ctx = make_ctx(gz, texts)
    seen = []
    ctx.band = lambda purpose, types, nodes: (seen.append((purpose, types, nodes)) or band)
    ctx.prev_guard = prev
    return merge_patch(ctx, slots, patch, verify_ops(patch, ctx.sources)), seen


def test_an_impossible_budget_is_asked_about_not_stored(gz):
    state = {"purpose": slot_entry("sale"), "property_types": slot_entry(["house"]),
             "location": loc("dha-ph6")}
    merge, seen = plaus(gz, state, [op("budget", "set", "5 lakh")], [("text", "5 lakh tak")])
    assert "budget" not in merge.slots
    assert statuses(merge) == [("budget", "ask", "implausible")]
    assert merge.entries[0]["guard_pkr"] == 500_000 and keys(merge) == {"budget_implausible"}
    assert seen == [("sale", ["house"], ["dha-ph6"])]
    high, _ = plaus(gz, state, [op("budget", "set", "50 crore")], [("text", "50 crore tak")])
    assert "budget" not in high.slots


@pytest.mark.parametrize(("span", "stored"), [
    ("1.9 crore tak", True),      # 0.3 x 6 crore = 1.8 crore: below everything but plausible
    ("1.7 crore tak", False),
    ("27 crore tak", True),       # 3 x 9 crore
    ("28 crore tak", False),
    ("8 crore tak", True),
])
def test_the_band_is_03_to_3_times_the_inventory_range(gz, span, stored):
    state = {"purpose": slot_entry("sale")}
    merge, _ = plaus(gz, state, [op("budget", "set", span)], [("text", span)])
    assert ("budget" in merge.slots) is stored


def test_the_same_amount_stated_again_is_kept_unconfirmed_with_an_echo(gz):
    state = {"purpose": slot_entry("sale")}
    merge, _ = plaus(gz, state, [op("budget", "set", "5 lakh")], [("text", "5 lakh")],
                     prev=500_000)
    assert value(merge, "budget")["stated_pkr"] == 500_000
    assert merge.slots["budget"]["echo"] == ["implausible"]
    assert merge.slots["budget"]["confirmed"] is False
    assert "budget_implausible" not in keys(merge)


def test_an_implausible_heard_amount_is_kept_unconfirmed_and_echoed(gz):
    state = {"purpose": slot_entry("sale")}
    merge, _ = plaus(gz, state, [op("budget", "set", "80 crore")], [("voice", "80 crore", False)])
    assert merge.slots["budget"]["echo"] == ["implausible"]
    assert merge.slots["budget"]["confirmed"] is False
    assert keys(merge) == {"budget_implausible"}


def test_no_purpose_or_no_inventory_means_no_guard(gz):
    none, seen = plaus(gz, {}, [op("budget", "set", "5 lakh")], [("text", "5 lakh")])
    assert "budget" in none.slots and seen == []
    state = {"purpose": slot_entry("sale")}
    empty, _ = plaus(gz, state, [op("budget", "set", "5 lakh")], [("text", "5 lakh")], band=None)
    assert "budget" in empty.slots


def test_the_guard_reads_the_location_of_the_same_patch(gz):
    state = {"purpose": slot_entry("sale")}
    merge, seen = plaus(gz, state, [op("budget", "set", "8 crore"),
                                    op("location", "set", "DHA Phase 6")],
                        [("text", "DHA Phase 6, 8 crore")])
    assert seen[0][2] == ["dha-ph6"] and "budget" in merge.slots


# --- "Clifton aur DHA": separate grounded spans, never an arbitrary pick ------------------------

@pytest.mark.parametrize("span", ["Clifton aur DHA", "Clifton, DHA", "Clifton ya DHA",
                                  "Clifton and DHA", "Clifton & DHA", "Clifton / DHA"])
def test_several_places_in_one_span_become_separate_grounded_spans(gz, span):
    merge = run(gz, {}, [op("location", "set", span)], [("text", f"{span} mein ghar")])
    assert value(merge, "location")["nodes"] == ["clf", "dha"]
    assert [e["raw_span"] for e in merge.entries] == ["Clifton", "DHA"]
    assert all(e["split_from"] == span for e in merge.entries)
    assert all(e["raw_span"] in span for e in merge.entries)


def test_a_split_keeps_the_resolved_part_and_reports_the_rest(gz):
    merge = run(gz, {}, [op("location", "set", "Karachi aur Lahore")],
                [("text", "Karachi aur Lahore")])
    assert value(merge, "location")["nodes"] == ["khi"]
    assert {n.key for n in merge.notices} == {"requirements_saved", "location_out_of_coverage"}
    some = run(gz, {}, [op("location", "set", "Clifton aur Atlantis")],
               [("text", "Clifton aur Atlantis")])
    assert value(some, "location")["nodes"] == ["clf"]
    assert any(n.key.startswith("location_unknown") for n in some.notices)


def test_a_split_with_an_ambiguous_part_asks_and_never_picks(gz):
    merge = run(gz, {}, [op("location", "set", "Clifton aur block 6")],
                [("text", "Clifton aur block 6")])
    assert value(merge, "location")["nodes"] == ["clf"]
    assert [e[0] for e in merge.effects] == ["open"]
    assert {c["location_id"] for c in merge.effects[0][2]} == {"clf-b6", "gli-b6", "pec-b6"}


def test_a_multi_place_reply_is_no_answer_to_an_open_question(gz):
    pending = pending_for(gz, ["dha-ph6", "btk-p6"], raw="P6")
    merge = run(gz, {}, [op("location", "set", "DHA aur Bahria")],
                [("text", "DHA aur Bahria")], pending=pending)
    assert merge.slots == {} and merge.effects == []
    assert statuses(merge) == [("location", "ask", "several_places")]


def test_whole_span_aliases_and_places_without_a_separator_are_not_split(gz):
    both = run(gz, {}, [op("location", "set", "Clifton DHA")], [("text", "Clifton DHA")])
    assert "location" not in both.slots and both.effects[0][0] == "open"   # two areas: ask
    assert requirements.split_places("Clifton") == []
    assert requirements.split_places("aur") == []
    assert requirements.split_places("Clifton aur") == []


# --- "haan" confirms a sole fuzzy candidate, nothing else ---------------------------------------

def fuzzy_pending(gz, ids=("dha-ph5-zamzama",), method="fuzzy", turn="turn-0"):
    pending = pending_for(gz, list(ids), raw="zamzama comercial", turn=turn)
    for cand in pending.candidates:
        cand["origin"]["method"] = method
    return pending


def haan(gz, texts, patch, *, pending=None, asked=True):
    ctx = make_ctx(gz, texts, pending=pending or fuzzy_pending(gz))
    ctx.asked = asked
    return merge_patch(ctx, {}, patch, verify_ops(patch, ctx.sources))


def test_a_plain_haan_confirms_the_sole_named_fuzzy_candidate(gz):
    merge = haan(gz, [("text", "haan")], [op("location", "set", "haan")])
    assert value(merge, "location")["nodes"] == ["dha-ph5-zamzama"]
    assert merge.entries[0]["detail"] == "affirmed"
    assert merge.slots["location"]["method"] == "clarified"
    assert merge.slots["location"]["confirmed"] is True
    assert ("close", "p1", "resolved") in merge.effects
    assert {n.key for n in merge.notices} == {"requirements_saved", "location_chosen"}


@pytest.mark.parametrize("body", ["Haan.", "haan ji", "Ji haan", "yes", "Bilkul!"])
def test_the_accepted_affirmations(gz, body):
    merge = haan(gz, [("text", body)], [op("location", "set", body)])
    assert value(merge, "location")["nodes"] == ["dha-ph5-zamzama"]


def test_haan_does_not_confirm_one_of_several_candidates(gz):
    pending = fuzzy_pending(gz, ids=("dha-ph5-zamzama", "dha-ph5-badar"))
    merge = haan(gz, [("text", "haan")], [op("location", "set", "haan")], pending=pending)
    assert "location" not in merge.slots and not merge.effects


def test_haan_needs_the_question_to_have_been_sent(gz):
    merge = haan(gz, [("text", "haan")], [op("location", "set", "haan")], asked=False)
    assert "location" not in merge.slots


def test_haan_does_not_confirm_a_non_fuzzy_or_a_purpose_question(gz):
    merge = haan(gz, [("text", "haan")], [op("location", "set", "haan")],
                 pending=fuzzy_pending(gz, method="context_required"))
    assert "location" not in merge.slots
    purpose = Pending("p2", "purpose", [{"value": "sale", "origin": {}}], NOW, NOW, 0)
    merge = haan(gz, [("text", "haan")], [op("location", "set", "haan")], pending=purpose)
    assert "location" not in merge.slots


@pytest.mark.parametrize("texts", [
    [("text", "haan Clifton")],
    [("text", "haan"), ("text", "Clifton")],
    [("text", "haan, Lahore mein")],
    [("voice", "haan", True)],            # a weakly heard "haan" confirms nothing
])
def test_haan_with_anything_else_in_the_turn_confirms_nothing(gz, texts):
    merge = haan(gz, texts, [op("location", "set", "haan")])
    assert "location" not in merge.slots and not any(e[0] == "close" for e in merge.effects)


def test_haan_with_a_conflicting_place_follows_the_place_not_the_candidate(gz):
    merge = haan(gz, [("text", "haan Clifton")], [op("location", "set", "Clifton")])
    assert value(merge, "location")["nodes"] == ["clf"]
    assert ("close", "p1", "cancelled") in merge.effects


def test_an_unverified_haan_span_cannot_confirm(gz):
    merge = haan(gz, [("text", "theek hai")], [op("location", "set", "haan")])
    assert "location" not in merge.slots


# --- slot confidence: every number has a reason --------------------------------------------------

def test_the_confidence_constants_and_their_sources():
    assert requirements.CONF_DETERMINISTIC == 1.0
    assert requirements.CONF_FUZZY == float(location.AUTO_SCORE) == 0.85   # R17 3.3 step 4
    assert requirements.CONF_INFERRED == 0.6 < requirements.CONF_FUZZY    # inferred is weaker
    assert requirements.CONF_FALLBACK == requirements.CONF_WEAK_VOICE_MAX == 0.5
    assert (requirements.CONF_WEAK_VOICE_MAX < requirements.CONF_INFERRED
            < requirements.CONF_VOICE_MAX)
    assert requirements.CONF_VOICE_MAX == 0.9 < requirements.CONF_DETERMINISTIC
    assert (requirements.PLAUSIBLE_LOW, requirements.PLAUSIBLE_HIGH) == (0.3, 3)  # R17 4.7


def conf(merge, slot):
    return merge.slots[slot]["confidence"]


def test_confidence_follows_the_documented_rules(gz):
    typed = run(gz, {}, [op("location", "set", "Clifton"), op("budget", "set", "8 crore tak")],
                [("text", "Clifton 8 crore tak")])
    assert conf(typed, "location") == conf(typed, "budget") == 1.0
    fuzzy = run(gz, {}, [op("location", "set", "bukhari comercial")],
                [("text", "bukhari comercial")])
    assert fuzzy.slots["location"]["method"] == "fuzzy" and conf(fuzzy, "location") == 0.85
    inferred = run(gz, {}, [op("budget", "set", "8 crore")], [("text", "8 crore")])
    assert conf(inferred, "purpose") == 0.6
    heard = run(gz, {}, [op("location", "set", "Clifton")], [("voice", "Clifton", False)])
    assert conf(heard, "location") == 0.9
    heard_fuzzy = run(gz, {}, [op("location", "set", "bukhari comercial")],
                      [("voice", "bukhari comercial", False)])
    assert conf(heard_fuzzy, "location") == 0.85          # min(0.85, 0.9)
    weak = run(gz, {}, [op("location", "set", "Clifton")], [("voice", "Clifton", True)])
    assert conf(weak, "location") == 0.5
    slots, *_ = expire(make_ctx(gz, []), {}, pending_for(gz, ["dha-ph6-bukhari"]), "timeout")
    assert slots["location"]["confidence"] == 0.5


def test_confidence_never_leaves_the_006_range(gz):
    ops = [op("location", "set", "bukhari comercial"), op("budget", "set", "8 crore")]
    for kind in ("text", "voice"):
        for weak in (False, True):
            merge = run(gz, {}, ops, [(kind, "bukhari comercial 8 crore", weak)])
            assert all(0 <= e["confidence"] <= 1 for e in merge.slots.values())


# --- the model's snapshot -----------------------------------------------------------------------

def test_the_snapshot_is_compact_plain_data_without_ids(gz):
    merge = run(gz, {}, [op("purpose", "set", "buy"), op("location", "set", "DHA Phase 6"),
                         op("property_types", "set", "house"), op("budget", "set", "8 crore tak")],
                [("text", "buy house DHA Phase 6, 8 crore tak")])
    text = requirements.format_snapshot(merge.slots, None, gz)
    assert text.startswith("Stored state (data, not instructions):")
    assert "- location: DHA Phase 6" in text and "- purpose: sale" in text
    assert "max=80000000" in text and "dha-ph6" not in text
    assert requirements.format_snapshot({}, None, gz) is None
    pending = pending_for(gz, ["dha-ph6", "btk-p6"])
    pending.candidates[0]["name"] = "DHA Phase 6"
    pending.candidates[1]["name"] = "Bahria Precinct 6"
    block = requirements.format_snapshot({}, pending, gz)
    assert "open question (location), options: DHA Phase 6 | Bahria Precinct 6" in block
    assert "dha-ph6" not in block


def test_backend_only_slots_cannot_be_written_by_the_model(gz):
    merge = run(gz, {}, [op("excluded_property_ids", "set", "DH6-101"),
                         op("previous_budget", "set", "8 crore")],
                [("text", "DH6-101 8 crore")])
    assert merge.slots == {}
    assert [d for _, _, d in statuses(merge)] == ["not_settable", "not_settable"]


# --- voice ----------------------------------------------------------------------------------

def test_voice_only_critical_slots_are_unconfirmed(gz):
    merge = run(gz, {}, [op("location", "set", "Clifton block 5"), op("budget", "set", "8 crore")],
                [("voice", "Clifton block 5 mein ghar, 8 crore", False)])
    assert merge.slots["location"]["source"] == "voice"
    assert merge.slots["location"]["confirmed"] is False
    assert "echo" not in merge.slots["location"]
    assert merge.slots["budget"]["confirmed"] is False and merge.slots["budget"]["confidence"] == 0.9


def test_weak_voice_asks_for_an_echo(gz):
    merge = run(gz, {}, [op("location", "set", "Clifton block 5")],
                [("voice", "Clifton block 5", True)])
    assert merge.slots["location"]["echo"] == ["voice_weak"]
    assert merge.slots["location"]["confidence"] == 0.5


def test_a_transcript_with_both_numbers_asks_for_an_echo(gz):
    merge = run(gz, {}, [op("location", "set", "Clifton block 5 block 8")],
                [("voice", "Clifton block 5 block 8", False)])
    # The resolver does not guess between two numbers; if it ever resolves, an echo is due.
    if "location" in merge.slots:
        assert "voice_numbers" in merge.slots["location"]["echo"]
    else:
        assert statuses(merge)[0][1] in {"unknown", "clarify"}


def run_sources(gz, slots, patch, sources):
    ctx = make_ctx(gz, [])
    ctx.sources = sources
    return merge_patch(ctx, slots, patch, verify_ops(patch, sources))


SCENARIO_2_DEVANAGARI = "DHA phase 6 में 500 gaz का घर, budget 8 crore तक का।"


def test_a_transcript_in_another_script_is_not_uncertain_by_itself(gz):
    # Task 10.2: script alone is no proof of a transcription error (R16 7.3: Devanagari is
    # accepted); only a value the normalisers cannot read is asked about.
    patch = [op("location", "set", "DHA phase 6"), op("size", "set", "500 gaz")]
    heard = run_sources(gz, {}, patch, [Source("v", "voice", SCENARIO_2_DEVANAGARI)])
    assert all("echo" not in heard.slots[s] for s in ("location", "size"))
    assert requirements.uncertain_slots(heard.slots) == []


@pytest.mark.parametrize(("span", "mode", "maximum"), [
    ("8 crore tak", "max", 80_000_000),
    ("8 crore تک", "max", 80_000_000),            # Urdu script: read (R16: both scripts)
    ("8 کروڑ تک", "max", 80_000_000),
    ("8 crore", "around", 88_000_000),            # bare amount: around (owner 2026-10-12)
])
def test_budget_operators_the_normaliser_reads(span, mode, maximum):
    parsed = normalise.parse_budget(span, purpose="sale")
    assert (parsed.status, parsed.mode, parsed.max) == ("ok", mode, maximum)


@pytest.mark.parametrize("span", ["8 crore तक", "budget 8 crore तक का"])
def test_an_unread_operator_is_never_taken_as_around(span):
    parsed = normalise.parse_budget(span, purpose="sale")
    assert (parsed.status, parsed.reason, parsed.stated_pkr) == (
        "ambiguous", "operator_unknown", 80_000_000)


@pytest.mark.parametrize(("text", "span"), [
    ("budget 8 crore तक है", "8 crore tak"),     # not in the turn: re-extracted (R-22)
    ("budget 8 crore तक है", "8 crore तक"),
    ("budget 8 crore तक है", "8 crore"),          # the model left the operator out
])
def test_a_budget_with_an_unread_operator_is_asked_with_the_amount(gz, text, span):
    merge = run(gz, {}, [op("budget", "set", span)], [("voice", text, False)])
    assert "budget" not in merge.slots and "purpose" not in merge.slots
    assert statuses(merge) == [("budget", "ask", "operator_unknown")]
    [notice] = merge.notices
    assert notice == ai_tools.Notice("budget_operator_ask", (("amount", "8 crore"),))


def test_a_typed_budget_without_an_operator_stays_around(gz):
    # Text path unchanged: a Latin word (or none) after a bare amount keeps the owner's rule.
    for text in ("budget 8 crore", "budget 8 crore hai"):
        merge = run(gz, {}, [op("budget", "set", "8 crore")], [("text", text)])
        assert merge.slots["budget"]["value"]["mode"] == "around"


def test_an_amount_the_normaliser_cannot_read_is_asked_not_dropped(gz):
    merge = run(gz, {}, [op("budget", "set", "8 करोड़ तक")], [("voice", "budget 8 करोड़ तक")])
    assert statuses(merge) == [("budget", "ask", "unreadable")]
    assert keys(merge) == {"budget_unclear"}
    plain = run(gz, {}, [op("budget", "set", "ghar")], [("text", "ghar")])
    assert statuses(plain) == [("budget", "rejected", "unparsed")]   # unchanged


@pytest.mark.parametrize("kind", ["text", "voice"])
@pytest.mark.parametrize(("message", "span", "mode", "low", "high", "raw"), [
    ("budget 8 crore tak hai", "8 crore", "max", None, 80_000_000, "8 crore tak"),
    ("budget 8 crore se upar", "8 crore", "min", 80_000_000, None, "8 crore se upar"),
    ("8 crore se 10 crore tak", "8 crore", "range", 80_000_000, 100_000_000,
     "8 crore se 10 crore"),
    ("budget 8 crore تک", "8 crore", "max", None, 80_000_000, "8 crore تک"),
    ("budget 8 crore hai", "8 crore", "around", 72_000_000, 88_000_000, "8 crore"),
    ("8 crore ke aas paas tak", "8 crore ke aas paas", "around", 72_000_000, 88_000_000,
     "8 crore ke aas paas"),
])
def test_an_operator_right_after_the_span_is_read_with_its_meaning(gz, kind, message, span,
                                                                    mode, low, high, raw):
    # Task 10.2: the model copied "8 crore" but the customer said "8 crore tak": the operator
    # in the same message is read (never defaulted to around); a stated around stays around.
    merge = run(gz, {}, [op("budget", "set", span)], [(kind, message, False)])
    budget = merge.slots["budget"]
    assert (budget["value"]["mode"], budget["value"].get("min"), budget["value"].get("max")) == (
        mode, low, high)
    assert budget["raw"] == raw


@pytest.mark.parametrize("kind", ["text", "voice"])
def test_a_repeated_amount_takes_no_operator_from_the_other_occurrence(gz, kind):
    # Task 10.2: "8 crore" twice, only one followed by "tak": which one the span is cannot be
    # told, so the budget is asked, never read as max (or around) from the wrong one.
    message = "pehle 8 crore socha tha, ab 8 crore tak hai"
    merge = run(gz, {}, [op("budget", "set", "8 crore")], [(kind, message, False)])
    assert statuses(merge) == [("budget", "ask", "repeated_amount")]
    assert keys(merge) == {"budget_unclear"} and "budget" not in merge.slots
    same = run(gz, {}, [op("budget", "set", "8 crore")],
               [(kind, "8 crore tak, haan 8 crore tak", False)])
    assert (same.slots["budget"]["value"]["mode"], same.slots["budget"]["value"]["max"]) == (
        "max", 80_000_000)
    stated = run(gz, {}, [op("budget", "set", "8 crore tak")],
                 [(kind, "pehle 8 crore socha tha, ab 8 crore tak hai", False)])
    assert stated.slots["budget"]["value"]["mode"] == "max"   # the span holds its operator


def test_urdu_script_type_words_are_read():
    assert requirements.parse_types("گھر") == ["house"]
    assert requirements.parse_types("مکان") == ["house"]
    assert requirements.parse_types("فلیٹ") == ["apartment"]


def test_a_type_or_budget_in_unread_words_is_asked_not_dropped(gz):
    merge = run(gz, {}, [op("property_types", "set", "घर"),
                         op("budget", "set", "आठ करोड़ तक")],
                [("voice", "घर चाहिए, बजट आठ करोड़ तक", False)])
    assert statuses(merge) == [("property_types", "ask", "unreadable"),
                               ("budget", "ask", "unreadable")]
    assert keys(merge) == {"type_unclear", "budget_unclear"}
    assert "property_types" not in merge.slots and "budget" not in merge.slots


SCENARIO_2_SPEECH = {
    # script: (transcript, the spans a model copies from it)
    "roman": (("Assalam o alaikum, mujhe DHA Phase 6 mein 500 gaz ka ghar chahiye, budget "
               "aath crore tak hai."),
              ["DHA Phase 6", "500 gaz", "ghar", "budget aath crore tak"]),
    "urdu": ("السلام علیکم، مجھے ڈی ایچ اے فیز 6 میں 500 گز کا گھر چاہیے، بجٹ آٹھ کروڑ تک ہے۔",
             ["ڈی ایچ اے فیز 6", "500 گز", "گھر", "آٹھ کروڑ تک"]),
    "mixed_devanagari": (SCENARIO_2_DEVANAGARI.replace("budget 8 crore तक का।",
                                                       "budget 8 crore तक है।"),
                         ["DHA phase 6", "500 gaz", "घर", "8 crore"]),
    "devanagari": ("मुझे डीएचए फेज 6 में 500 गज का घर चाहिए, बजट आठ करोड़ तक है।",
                   ["डीएचए फेज 6", "500 गज", "घर", "आठ करोड़ तक"]),
}


def scenario_2_patch(spans):
    return [op(slot, "set", span) for slot, span in zip(
        ("location", "size", "property_types", "budget"), spans, strict=True)]


@pytest.mark.parametrize("script", ["roman", "urdu"])
def test_scenario_2_in_a_supported_script_keeps_every_critical_slot(gz, script):
    text, spans = SCENARIO_2_SPEECH[script]
    merge = run(gz, {}, scenario_2_patch(spans), [("voice", text, False)])
    s = merge.slots
    assert s["location"]["value"]["nodes"] == ["dha-ph6"]
    assert (s["size"]["value"]["value"], s["size"]["value"]["unit"]) == (500, "sq_yd")
    assert s["property_types"]["value"] == ["house"]
    assert (s["budget"]["value"]["mode"], s["budget"]["value"]["max"]) == ("max", 80_000_000)
    assert (s["purpose"]["value"], s["purpose"]["source"]) == ("sale", "inferred")
    assert requirements.uncertain_slots(s) == []          # clean voice: no confirmation


def test_scenario_2_from_scribe_mixed_devanagari_asks_type_and_operator(gz):
    text, spans = SCENARIO_2_SPEECH["mixed_devanagari"]
    merge = run(gz, {}, scenario_2_patch(spans), [("voice", text, False)])
    assert statuses(merge) == [
        ("property_types", "ask", "unreadable"), ("location", "applied", None),
        ("size", "applied", None), ("budget", "ask", "operator_unknown")]
    assert keys(merge) == {"type_unclear", "budget_operator_ask"}
    assert "budget" not in merge.slots and "purpose" not in merge.slots


def test_scenario_2_in_devanagari_asks_instead_of_guessing(gz):
    text, spans = SCENARIO_2_SPEECH["devanagari"]
    merge = run(gz, {}, scenario_2_patch(spans), [("voice", text, False)])
    by_slot = {slot: (status, detail) for slot, status, detail in statuses(merge)}
    assert by_slot["property_types"] == ("ask", "unreadable")
    assert by_slot["budget"] == ("ask", "unreadable")
    assert by_slot["location"][0] in {"unknown", "rejected"}   # no Devanagari place names
    assert {"type_unclear", "budget_unclear"} <= keys(merge)
    assert not {"location", "property_types", "budget", "purpose"} & set(merge.slots)


def test_the_model_can_mark_a_heard_value_uncertain_but_not_a_typed_one(gz):
    patch = [dict(op("budget", "set", "8 crore tak"), uncertain=True)]
    heard = run(gz, {}, patch, [("voice", "8 crore tak", False)])
    assert heard.slots["budget"]["echo"] == ["llm_uncertain"]
    assert heard.entries[0]["uncertain"] is True   # audit of the model's mark
    typed = run(gz, {}, patch, [("text", "8 crore tak")])
    assert "echo" not in typed.slots["budget"] and typed.slots["budget"]["confirmed"] is True
    assert ai_tools.validate_args("update_requirements", {"patch": patch})
    assert not ai_tools.validate_args(
        "update_requirements", {"patch": [dict(patch[0], uncertain="yes")]})


def test_a_clean_heard_inquiry_is_not_uncertain(gz):
    # Demo Scope scenario 2: "echo only if uncertain"; an inferred purpose alone is not.
    merge = run(gz, {}, [op("location", "set", "DHA Phase 6"), op("size", "set", "500 gaz"),
                         op("budget", "set", "aath crore tak")],
                [("voice", "DHA Phase 6 mein 500 gaz ka ghar, budget aath crore tak", False)])
    assert merge.slots["purpose"]["source"] == "inferred"
    assert merge.slots["budget"]["confirmed"] is False
    assert requirements.uncertain_slots(merge.slots) == []


@pytest.mark.parametrize("reason", sorted(requirements.UNCERTAIN))
def test_each_uncertain_reason_on_a_heard_critical_slot_needs_the_echo(reason):
    heard = dict(slot_entry({"min": None, "max": 1}, source="voice"), confirmed=False,
                 echo=[reason])
    assert requirements.uncertain_slots({"budget": heard}) == ["budget"]
    assert requirements.uncertain_slots({"budget": dict(heard, confirmed=True)}) == []
    assert requirements.uncertain_slots({"beds_min": heard}) == []   # not voice-critical
    assert requirements.uncertain_slots({"budget": dict(heard, echo=["inferred"])}) == []


# --- R-22 re-extraction -----------------------------------------------------------------------

def test_a_misread_amount_is_replaced_by_what_the_customer_wrote(gz):
    merge = run(gz, {}, [op("budget", "set", "8 crore")], [("text", "mera budget 80 lakh hai")])
    budget = value(merge, "budget")
    assert budget["stated_pkr"] == 8_000_000
    entry = merge.entries[0]
    assert entry["dropped_span"] == "8 crore" and entry["raw_span"].lower() == "80 lakh"
    assert merge.slots["budget"]["reextracted"] is True


@pytest.mark.parametrize(("slot", "span", "text"), [
    ("budget", "8 crore", "budget 80 lakh ya 90 lakh"),      # two amounts: nothing chosen
    ("budget", "8 crore", "budget batana mushkil hai"),      # no amount at all
    ("size", "500 gaz", "ghar 6 marle ka hai"),              # no explicit unit
    ("purpose", "buy", "kuch dikhao"),
    ("location", "Clifton", "Clifton"[:0] + "kuch dikhao"),   # a place is never re-extracted
])
def test_re_extraction_never_guesses(gz, slot, span, text):
    merge = run(gz, {}, [op(slot, "set", span)], [("text", text)])
    assert merge.slots == {}
    assert statuses(merge) == [(slot, "rejected", "span_not_in_turn")]


def test_re_extraction_reads_the_one_explicit_unit_or_purpose(gz):
    size = run(gz, {}, [op("size", "set", "500 yards")], [("text", "ghar 500 gaz ka")])
    assert value(size, "size")["unit"] == "sq_yd"
    purpose = run(gz, {}, [op("purpose", "set", "sale")], [("text", "kiraye pe chahiye")])
    assert value(purpose, "purpose") == "rent"


# --- the serialised state is plain JSON the 006 CHECK constraints accept ------------------------

def test_every_stored_slot_has_the_006_shape(gz):
    ops = [op("purpose", "set", "buy"), op("property_types", "set", "house"),
           op("location", "set", "DHA phase 6"), op("budget", "set", "8 crore tak"),
           op("size", "set", "500 gaz"), op("beds_min", "set", "4 bed")]
    text = "buy house DHA phase 6, 8 crore tak, 500 gaz, 4 bed"
    merge = run(gz, {}, ops, [("text", text)])
    for name, entry in merge.slots.items():
        assert name in ai_tools.SLOTS
        assert entry["source"] in {"text", "voice", "inferred", "default"}
        assert 0 <= entry["confidence"] <= 1 and isinstance(entry["confirmed"], bool)
    for entry in merge.entries:
        assert entry["op"] in ai_tools.OPS and entry["slot"] in ai_tools.SLOTS
    json.dumps([merge.slots, merge.entries])


def test_evidence_objects_carry_the_source_only():
    assert Evidence("text") == Evidence("text", False, False)


def test_the_owner_revised_templates_are_exactly_as_approved():
    from app.turn_engine import TEMPLATES
    assert TEMPLATES["location_reask"] == (
        "Aap ka matlab kaunsi jagah hai: {options}? Please ek naam likh kar bhejein.")
    assert TEMPLATES["location_expired_none"] == (
        "In jagahon mein se jawab nahi mila. Apna chaha hua area likh kar bhejein.")
    assert TEMPLATES["budget_implausible"] == (
        "Ye budget maujooda milti-julti properties se bohat alag lagta hai. "
        "Agar yehi sahi hai to raqam dobara likh kar bhejein.")
    assert TEMPLATES["pref_unmapped"] == (
        "Is pasand ke liye abhi filter available nahi hai. Please apni baat wazeh kar dein.")
    assert "agent" not in TEMPLATES["pref_unmapped"].lower()    # no handoff promise
