"""Task 6.2: the normalisers agree with the hand-derived R16 gold values in bench/
(budget, size and purpose slots of the customer items). Per docs/BENCHMARK.md a
disagreement is a finding for review, never a silent gold edit: list it in
KNOWN_DIFFERENCES with the reason. Offline."""

import pytest

from app import benchmark
from app import normalise as n

KNOWN_DIFFERENCES = {}  # item id -> reason


def _items():
    for items in benchmark.load().values():
        for item in items:
            if item.get("gold_extraction"):
                yield item


def _text(item):
    return item.get("input") or (item.get("gold_transcript") or {}).get("roman") or ""


def _type(gold):
    types = (gold.get("property_types") or {}).get("value") or [None]
    return types[0]


def _cases(slot):
    return [pytest.param(item, id=item["id"]) for item in _items()
            if slot in item["gold_extraction"] and item["id"] not in KNOWN_DIFFERENCES]


@pytest.mark.parametrize("item", _cases("budget"))
def test_budget_matches_gold(item):
    gold = item["gold_extraction"]
    expected = gold["budget"]
    purpose = (gold.get("purpose") or {}).get("value")
    result = n.parse_budget(expected["raw"], purpose=purpose).to_dict()
    assert {k: result.get(k) for k in expected} == expected


@pytest.mark.parametrize("item", _cases("size"))
def test_size_matches_gold(item):
    gold = item["gold_extraction"]
    expected = gold["size"]
    result = n.parse_area(expected["raw"], property_type=_type(gold)).to_dict()
    assert result["status"] == "ok"
    assert {k: result.get(k) for k in expected} == expected


@pytest.mark.parametrize("item", _cases("purpose"))
def test_purpose_matches_gold(item):
    gold = item["gold_extraction"]
    expected = gold["purpose"]
    explicit = n.parse_purpose(_text(item))
    if expected["source"] == "inferred":
        assert explicit.status == "none"
        amount = n.parse_budget(gold["budget"]["raw"]).stated_pkr if "budget" in gold else None
        assert n.infer_purpose(amount, n.category_of(_type(gold))).value == expected["value"]
    else:
        assert (explicit.value, explicit.raw) == (expected["value"], expected["raw"])


def test_gold_coverage():
    """The cross-check covers every budget, size and purpose slot in the set."""
    counts = {slot: len(_cases(slot)) for slot in ("budget", "size", "purpose")}
    assert counts["budget"] >= 40 and counts["size"] >= 15 and counts["purpose"] >= 40
