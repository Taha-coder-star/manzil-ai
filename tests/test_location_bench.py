"""Task 7.1: the resolver agrees with every location gold slot in bench/ (customer
text and voice items). Per docs/BENCHMARK.md a disagreement is a finding for
review, never a silent gold edit: list it in KNOWN_DIFFERENCES with the reason.
Offline; dataset keys are the IDs."""

import pytest

from app import benchmark, demo_dataset
from app import location as loc

KNOWN_DIFFERENCES = {}  # item id -> reason

G = loc.Gazetteer.from_dataset(demo_dataset.load())
ITEMS = {item["id"]: item for items in benchmark.load().values() for item in items}


def _active_area(item):
    """A follow-up's active location: the gold location of the item it follows
    (CT-29 follows CT-28 -> CT-27 -> CT-13, DHA Phase 6)."""
    seen = set()
    while item.get("context") and item["id"] not in seen:
        seen.add(item["id"])
        item = ITEMS[item["context"]["after"]]
        nodes = ((item.get("gold_extraction") or {}).get("location") or {}).get("value")
        if nodes:
            return nodes[0]
    return None


def _cases():
    return [pytest.param(item, id=item["id"]) for item in ITEMS.values()
            if (item.get("gold_extraction") or {}).get("location")
            and item["id"] not in KNOWN_DIFFERENCES]


def test_gold_has_location_slots():
    assert len(_cases()) == 51


@pytest.mark.parametrize("item", _cases())
def test_location_matches_gold(item):
    gold = item["gold_extraction"]["location"]
    result = loc.resolve(gold["raw"], _active_area(item), G)
    assert result.status == gold["status"], result.to_dict()
    assert result.raw == gold["raw"]
    if gold["status"] == loc.AMBIGUOUS:
        assert sorted(c.location_id for c in result.candidates) == sorted(gold["candidates"])
    else:
        assert list(result.nodes) == gold["value"]


def test_follow_up_context_is_found():
    assert _active_area(ITEMS["CT-29"]) == "dha-ph6"
    assert _active_area(ITEMS["CT-13"]) is None
