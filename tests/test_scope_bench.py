"""Task 10.3: the Demo Scope section 6 conversation set (bench/scope_conversations.json) is
valid against demo/ and the source counts, references the R16 text items without changing
them, and the validator catches broken gold. Offline; no database."""

import copy
import hashlib

import pytest

from app import benchmark

# The R16 text set is referenced by the scope set, never edited (sha256 of the committed file).
CUSTOMER_TEXT_SHA256 = "e658bd2b82c55c19a9d55f636539adfe9ad169c5061149258d3f29e12dc73720"


@pytest.fixture(scope="module")
def committed():
    return benchmark.load_scope(), benchmark.load()["customer_text"], benchmark.load_demo()


@pytest.fixture
def scope(committed):
    return copy.deepcopy(committed[0])


def errors(scope, committed):
    found = []
    benchmark.check_scope(committed[2], scope, committed[1], found)
    return "\n".join(found)


def item(scope, item_id):
    return next(i for i in scope["items"] if i["id"] == item_id)


def test_the_committed_scope_set_is_valid_and_covers_the_source_counts(scope, committed):
    assert errors(scope, committed) == ""
    items = benchmark.scope_items(scope, committed[1])
    turns = [i for i in items if i["scope_role"] == "turn"]
    follow = [i for i in turns if "follow_up" in i["metrics"]]
    photos = [i for i in turns if "photos" in i["metrics"]]
    assert len(follow) == 20 and {i["follow_up_kind"] for i in follow} == benchmark.SCOPE_KINDS
    assert len([i for i in photos if i["id"] != "CT-27"]) == 10 and "CT-27" in {
        i["id"] for i in photos}
    assert [i["id"] for i in turns if i.get("gold_status") == "unresolved"] == ["SC-18"]


def test_the_r16_text_items_are_referenced_unchanged(committed):
    raw = (benchmark.BENCH_DIR / "customer_text.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CUSTOMER_TEXT_SHA256
    by_id = {i["id"]: i for i in committed[1]}
    for ref_item in benchmark.scope_items(committed[0], committed[1])[:8]:
        original = by_id[ref_item["id"]]
        assert {k: ref_item[k] for k in original} == original


@pytest.mark.parametrize(("item_id", "change", "message"), [
    ("SC-04", lambda i: i["expected_outcome"].update(ids=["DH8-101", "DH8-102"]),
     "clear matches ['DH6-103'] are not expected"),
    ("SC-10", lambda i: i["expected_outcome"].update(ids=["PEC-101", "SHF-101"]),
     "SHF-101 does not fit the request"),
    ("SC-16", lambda i: i["expected_outcome"].update(ids=["CLF-101", "CLF-104"]),
     "hidden listings ['CLF-104'] expected"),
    ("SC-11", lambda i: i["expected_outcome"]["search"].update(
        purpose="rent", budget_min=None, budget_max=600000), "no_match but ['PEC-101']"),
    ("SC-01", lambda i: i["expected_outcome"].update(listing="DH8-101"),
     "position 1 is not DH8-101"),
    ("SC-03", lambda i: i.update(quote={"listing": "DH8-102"}), "the quoted card is not DH8-101"),
    ("SC-02", lambda i: i["expected_outcome"].update(listing="DH8-101"),
     "'aur' continues SC-01"),
    ("SC-12", lambda i: i.update(input="Is ki pictures bhejo"), "BTK-104 is not named"),
    ("SC-05", lambda i: i["context"].update(shown=["DH8-101", "DH8-102"]),
     "does not follow SC-04"),
    ("SC-09", lambda i: i["expected_tools"][0]["args"]["patch"][0].update(raw_span="7 lakh"),
     "patch span '7 lakh' not in input"),
    ("SC-18", lambda i: i.update(note="no reason given"), "must say why"),
    ("SC-06", lambda i: i.pop("source"), "a source are required"),
])
def test_broken_scope_gold_is_caught(scope, committed, item_id, change, message):
    change(item(scope, item_id))
    assert message in errors(scope, committed)


def test_coverage_below_the_source_counts_is_caught(scope, committed):
    scope["items"] = [i for i in scope["items"] if i["id"] not in ("SC-19", "SC-12")]
    found = errors(scope, committed)
    assert "19 follow-up turns, Demo Scope asks 20" in found
    assert "9 extra photo requests" in found


def test_an_unknown_reference_is_caught(scope, committed):
    scope["refs"].append({"id": "CT-99", "role": "setup"})
    assert "scope ref 'CT-99' is not a customer_text item" in errors(scope, committed)


def test_the_cli_validates_the_scope_set_too(capsys):
    assert benchmark.main([]) == 0
    out = capsys.readouterr().out
    assert '"scope_refs": 8' in out and '"scope_turns": 21' in out and "valid" in out
