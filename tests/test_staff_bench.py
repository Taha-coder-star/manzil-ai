"""Task 8.2: the 20-item FIXTURE benchmark (hand-written spans; backend rules only).

These tests pin what the runner reports and what it refuses to claim. They say nothing about
model accuracy: no model is called anywhere."""

import hashlib
import json
from pathlib import Path

import pytest

from app import staff_bench

BENCH = Path(staff_bench.BENCH_DIR)
GOLD_SHA256 = "7669513378d38fdf6280eda48de19889d67e66b9aa232b32e0252987927c8a40"


@pytest.fixture(scope="module")
def result():
    return staff_bench.run()


def test_the_result_is_labelled_a_fixture_run_and_never_a_model_score(result):
    assert (result["mode"], result["model"]) == ("fixture", None)
    assert "NOT a model benchmark" in result["banner"] and "hand" in result["banner"]
    with pytest.raises(staff_bench.BenchError, match="live"):
        staff_bench.run(mode="live")


def test_the_gold_file_is_untouched():
    raw = (BENCH / 'staff.json').read_bytes()
    assert hashlib.sha256(raw.replace(bytes((13, 10)), bytes((10,)))).hexdigest() == GOLD_SHA256


def test_every_span_is_a_substring_of_its_item_input():
    items = {i["id"]: i for i in json.loads((BENCH / "staff.json").read_text("utf-8"))}
    spans = json.loads((BENCH / "staff_spans.json").read_text("utf-8"))
    assert set(spans) - {"_note"} == set(items) and len(items) == 20

    def strings(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in ("tool", "field"):
                    continue
                yield from strings(inner)
        elif isinstance(value, list):
            for inner in value:
                yield from strings(inner)
        elif isinstance(value, str):
            yield value

    for item_id, spec in spans.items():
        if item_id == "_note":
            continue
        text = items[item_id]["input"].casefold()
        for span in strings(spec):
            if span.upper() == span and "-" in span and len(span) <= 8:
                continue  # a listing code named in a target (checked by the tool itself)
            assert " ".join(span.casefold().split()) in " ".join(text.split()), (item_id, span)


def test_the_fixture_score_and_what_misses(result):
    s = result["summary"]
    assert s["items_exact"] == {"exact": 19, "of": 20}
    assert s["by_kind"] == {"draft": {"exact": 11, "of": 11}, "update": {"exact": 5, "of": 6},
                            "disambiguate": {"exact": 3, "of": 3}}
    assert s["workflow"] == {"passed": 33, "of": 33}
    assert s["fields"]["of"] == 94 and s["fields"]["correct"] == 92
    # ST-15: "Sports City" is only similar to "Bahria Sports City"; the resolver does not
    # accept it, so the backend asks instead of guessing, and the gold target is not reached.
    assert s["failed_items"] == [{"id": "ST-15", "reason": "location_unclear"}]


def test_st01_duplicate_disagreement_is_reported_apart_not_hidden_or_tuned(result):
    [disagreement] = result["summary"]["known_gold_disagreements"]
    assert (disagreement["item"], disagreement["check"]) == ("ST-01", "duplicate_of")
    assert disagreement["gold"] == [] and disagreement["got"] == ["DH6-101", "DH6-106"]
    st01 = next(i for i in result["items"] if i["id"] == "ST-01")
    assert st01["exact"] is True  # the critical fields are right; the duplicate warning is extra
    assert st01["workflow"]["duplicate_of"]["known_disagreement"] is True
    assert st01["workflow"]["expected_code"]["ok"] and st01["workflow"]["save_allowed"]["ok"]


@pytest.mark.parametrize("item_id", ["ST-02", "ST-07", "ST-08", "ST-09", "ST-19", "ST-20"])
def test_the_items_the_plan_names_are_exact_with_the_right_workflow(result, item_id):
    item = next(i for i in result["items"] if i["id"] == item_id)
    assert item["exact"] and all(c["ok"] for c in item["workflow"].values())


def test_save_is_blocked_exactly_where_the_gold_says(result):
    allowed = {i["id"]: i["workflow"]["save_allowed"]["got"]
               for i in result["items"] if i["kind"] == "draft"}
    assert [k for k, v in allowed.items() if not v] == ["ST-07", "ST-08", "ST-09"]


def test_the_cli_writes_the_json_result(tmp_path, capsys):
    out = tmp_path / "staff.json"
    assert staff_bench.main(["--out", str(out)]) == 0
    saved = json.loads(out.read_text("utf-8"))
    assert saved["mode"] == "fixture" and saved["model"] is None
    assert "NOT a model benchmark" in capsys.readouterr().out
