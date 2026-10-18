"""Filters from an earlier session (owner decision 2026-10-18), offline: the recorded
2026-10-17/18 test conversation replayed through the pure merge. Database, tap and worker
behaviour is in tests/test_fresh_search_db.py. All data is fictional."""

from datetime import UTC, datetime, timedelta

import pytest

from app import demo_dataset, location, requirements
from app.requirements import (
    SESSION_GAP,
    Context,
    Patch,
    Source,
    entry_time,
    inherited_slots,
    merge_patch,
    verify_ops,
)


@pytest.fixture(scope="module")
def gz():
    return location.Gazetteer.from_dataset(demo_dataset.load())


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def entry(value, when, *, raw="x", source="text", **extra):
    return {"value": value, "raw": raw, "source": source, "confidence": 1.0, "confirmed": True,
            "updated_at": when.isoformat(), **extra}


def ctx_for(gz, text, now, session_start):
    names = sorted(p.display_name for p in gz.places.values() if p.level == "area")
    return Context(gz, requirements.id_map(gz), "KHI", "Karachi", 225,
                   [Source("m0", "text", text, False)], None, "turn-1", "c0", now,
                   areas=names, session_start=session_start)


def run(gz, slots, patch, text, now, session_start):
    ctx = ctx_for(gz, text, now, session_start)
    return merge_patch(ctx, slots, patch, verify_ops(patch, ctx.sources))


def op(slot, kind, span):
    return {"slot": slot, "op": kind, "raw_span": span}


RENT_TEST = at(16, 18, 39)         # "office on rent in Karachi, around 5 lakh per month"
SESSION_17 = at(17, 18, 54)        # first message after a 23 h gap
HOUSE_TURN = at(17, 22, 6)         # "DHA Phase 6 mein 500 gaz ka ghar, budget aath crore tak"
SESSION_18 = at(18, 4, 41)         # "hi" after 6 h 34 min of silence
TODAY = at(18, 4, 42)


def stored_on_17th():
    """The slots as the recorded conversation left them before the 17th's house request."""
    return {
        "purpose": entry("rent", RENT_TEST, raw="on rent"),
        "category": entry("residential", at(17, 22, 6)),
        "property_types": entry(["apartment"], at(17, 22, 6), raw="flat"),
        "beds_min": entry(3, at(17, 22, 6), raw="3 bed"),
    }


HOUSE_TEXT = "DHA Phase 6 mein 500 gaz ka ghar, budget aath crore tak"
HOUSE_PATCH = [op("location", "set", "DHA Phase 6"), op("property_types", "set", "ghar"),
               op("size", "set", "500 gaz"), op("budget", "set", "aath crore tak")]


# --- the session boundary and what counts as inherited ---------------------------------

def test_the_session_gap_is_six_hours():
    assert SESSION_GAP == timedelta(hours=6)
    assert SESSION_18 - HOUSE_TURN > SESSION_GAP           # 04:41 on the 18th: a new session
    assert HOUSE_TURN - SESSION_17 < SESSION_GAP           # 22:06 on the 17th: the same one


def test_a_slot_is_inherited_when_both_its_times_precede_the_session():
    kept = (SESSION_17 + timedelta(days=1)).isoformat()
    slots = {"purpose": entry("rent", RENT_TEST),
             "size": entry({"value": 500}, SESSION_17 + timedelta(hours=3)),
             "budget": entry({"max": 1}, RENT_TEST, kept_at=kept)}
    assert inherited_slots(slots, SESSION_17) == ["purpose"]       # size is fresh, budget kept
    assert inherited_slots(slots, None) == []                      # no session known: asks nothing
    assert entry_time({"updated_at": "garbage"}) is None
    assert inherited_slots({"size": {"value": 1}}, SESSION_17) == []   # no time: not inherited


def test_backend_only_and_unmapped_slots_are_never_asked_about():
    slots = {"previous_budget": entry({"max": 1}, RENT_TEST),
             "excluded_property_ids": entry(["a"], RENT_TEST),
             "unmapped_prefs": entry([{"raw": "Hi"}], RENT_TEST)}
    assert inherited_slots(slots, TODAY) == []


# --- why "aath crore" kept rent, and the fix --------------------------------------------

def test_without_a_session_the_old_purpose_labels_a_new_amount_as_monthly_rent(gz):
    """The recorded bug: rent stated on the 16th, then a house request with a budget and no
    purpose word. Flat -> house is residential -> residential, so no type switch; the rent
    was stated, not inferred. So neither stale-purpose rule applied and the period came
    from the stored purpose."""
    merge = run(gz, stored_on_17th(), HOUSE_PATCH, HOUSE_TEXT, HOUSE_TURN, None)
    assert merge.slots["purpose"]["value"] == "rent"
    assert merge.slots["budget"]["value"]["period"] == "monthly"


def test_an_inherited_purpose_neither_labels_the_amount_nor_is_replaced(gz):
    merge = run(gz, stored_on_17th(), HOUSE_PATCH, HOUSE_TEXT, HOUSE_TURN, SESSION_17)
    budget = merge.slots["budget"]["value"]
    assert budget["max"] == 80_000_000 and budget["period"] is None   # unlabelled
    assert merge.slots["purpose"]["value"] == "rent"                  # untouched: asked about
    assert "purpose_inferred" not in {n.key for n in merge.notices}
    assert inherited_slots(merge.slots, SESSION_17) == ["purpose"]
    # what the customer just stated is theirs, not inherited
    assert {"location", "property_types", "size", "budget"} <= set(merge.slots)


def test_the_recorded_18th_sequence_keeps_the_new_area_and_budget_unlabelled(gz):
    """04:41 "mera area hy dha phase 6", 04:42 "aur budget hy 2 crore", over the slots the
    17th left (rent from the 16th, house and 500 gaz from the 17th)."""
    slots = {
        "purpose": entry("rent", RENT_TEST, raw="on rent"),
        "category": entry("residential", HOUSE_TURN),
        "property_types": entry(["house"], HOUSE_TURN, raw="ghar"),
        "size": entry({"mode": "target", "sqft": 4500, "unit": "sq_yd", "value": 500},
                      HOUSE_TURN, raw="500 gaz"),
        "beds_min": entry(3, at(17, 22, 6), raw="3 bed"),
    }
    first = run(gz, slots, [op("location", "set", "dha phase 6")],
                "mera area hy dha phase 6", at(18, 4, 41), SESSION_18)
    second = run(gz, first.slots, [op("budget", "set", "2 crore")],
                 "aur budget hy 2 crore", TODAY, SESSION_18)
    budget = second.slots["budget"]["value"]
    assert budget["period"] is None                              # not "monthly"
    assert (budget["min"], budget["max"]) == (18_000_000, 22_000_000)   # the approved +/-10 %
    assert second.slots["location"]["raw"] == "dha phase 6"
    assert inherited_slots(second.slots, SESSION_18) == [
        "purpose", "category", "property_types", "size", "beds_min"]


def test_a_purpose_the_customer_states_now_is_not_inherited_and_labels_the_budget(gz):
    merge = run(gz, stored_on_17th(),
                [op("purpose", "set", "khareedna"), op("budget", "set", "2 crore")],
                "khareedna hai, 2 crore", TODAY, SESSION_18)
    assert merge.slots["purpose"]["value"] == "sale"
    assert merge.slots["budget"]["value"]["period"] == "one_time"


# --- the two answers --------------------------------------------------------------------

def _patch(gz, slots, session_start, now=TODAY):
    return Patch(ctx_for(gz, "x", now, session_start), slots)


def test_keeping_the_purpose_gives_the_unlabelled_budget_its_period(gz):
    slots = {"purpose": entry("rent", RENT_TEST),
             "budget": entry({"min": 18_000_000, "max": 22_000_000, "mode": "around",
                              "period": None}, TODAY)}
    patch = _patch(gz, slots, SESSION_18)
    patch._resolve_purpose()                                  # still inherited: nothing
    assert patch.slots["budget"]["value"]["period"] is None
    slots["purpose"]["kept_at"] = TODAY.isoformat()           # what keep_inherited writes
    patch = _patch(gz, slots, SESSION_18)
    patch._resolve_purpose()
    assert patch.slots["budget"]["value"]["period"] == "monthly"


def test_a_fresh_start_infers_the_purpose_of_the_amount_that_lost_it(gz):
    slots = {"budget": entry({"min": 18_000_000, "max": 22_000_000, "mode": "around",
                              "stated_pkr": 20_000_000, "period": None}, TODAY, raw="2 crore"),
             "category": entry("residential", TODAY)}
    patch = _patch(gz, slots, SESSION_18)
    patch.budget_given = True
    patch._resolve_purpose()
    assert patch.slots["purpose"]["value"] == "sale"
    assert patch.slots["purpose"]["source"] == "inferred"
    assert patch.slots["budget"]["value"]["period"] == "one_time"
    assert [n.key for n in patch.merge.notices] == ["purpose_inferred"]


# --- question versions and explicit replacements ----------------------------------------

def test_the_question_token_changes_with_the_inherited_filters_and_their_times():
    slots = {"purpose": entry("rent", RENT_TEST), "size": entry({"value": 500}, HOUSE_TURN)}
    names = ["purpose", "size"]
    token = requirements.inherited_token(slots, names, "conv-1")
    assert len(token) == 8 and token == requirements.inherited_token(slots, names, "conv-1")
    assert token != requirements.inherited_token(slots, ["purpose"], "conv-1")  # another set
    assert token != requirements.inherited_token(slots, names, "conv-2")        # another chat
    said_again = {**slots, "size": entry({"value": 500}, HOUSE_TURN + timedelta(minutes=1))}
    assert token != requirements.inherited_token(said_again, names, "conv-1")   # said again
    assert requirements.inherited_token(slots, [], "conv-1") == ""


def test_nahi_kharidna_hai_keeps_the_new_amount_and_gives_it_the_new_purpose(gz):
    first = run(gz, stored_on_17th(), [op("budget", "set", "2 crore")], "budget 2 crore",
                TODAY, SESSION_18)
    assert first.slots["budget"]["value"]["period"] is None
    second = run(gz, first.slots, [op("purpose", "set", "kharidna")], "nahi, kharidna hai",
                 TODAY + timedelta(minutes=1), SESSION_18)
    assert second.slots["purpose"]["value"] == "sale"
    assert second.slots["budget"]["value"]["period"] == "one_time"
    assert "previous_budget" not in second.slots                # not moved aside
    assert inherited_slots(second.slots, SESSION_18) == ["category", "property_types", "beds_min"]


def test_an_inherited_budget_is_still_moved_aside_by_a_purpose_switch(gz):
    slots = {"purpose": entry("rent", RENT_TEST),
             "budget": entry({"max": 200_000, "period": "monthly"}, RENT_TEST)}
    merge = run(gz, slots, [op("purpose", "set", "kharidna")], "kharidna hai", TODAY, SESSION_18)
    assert "budget" not in merge.slots and "previous_budget" in merge.slots   # a rent figure


def test_slots_are_stamped_with_the_message_time_not_the_processing_time(gz):
    late = TODAY + timedelta(hours=7)                           # processed 7 h after it was said
    ctx = ctx_for(gz, "dha phase 6", late, SESSION_18)
    ctx.stamp = TODAY
    merge = merge_patch(ctx, {}, [op("location", "set", "dha phase 6")],
                        verify_ops([op("location", "set", "dha phase 6")], ctx.sources))
    assert merge.slots["location"]["updated_at"] == TODAY.isoformat()
