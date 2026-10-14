"""Task 8.2 fixture benchmark: the 20 staff items of bench/staff.json run through the real
staff extraction rules with HAND-WRITTEN SPANS (bench/staff_spans.json).

    python -m app.staff_bench [--out result.json]

What this measures: the backend rules (span checks, normalisers, resolver, conflicts,
duplicates, codes, candidate matching, update checks) over the demo dataset, in memory.
What it does NOT measure: any model. The spans are written by a person from the inputs, so a
score here says nothing about live Gemini accuracy; a live run is a separate, owner-approved
step and its results must carry mode "live" (refused here). The ST-01 duplicate gold is
reported on its own, as a known disagreement, and the gold file is never touched.

Critical fields (docs/STAFF_EXTRACTION.md section 10): a draft item is exact when purpose,
property type, location, size (value, unit and nominal bucket when the gold has one), price
and bedrooms (when required) all equal the gold; an update item when the target listing and
every change equal it; a disambiguation item when the gold candidates are all offered, nothing
outside candidates + allowed is, and no target was chosen. Workflow checks (missing list,
save_allowed, duplicates, expected code) are reported separately.
"""

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

from app import benchmark, demo_dataset, location, staff_drafts
from app import staff_normalise as sn

BENCH_DIR = Path(__file__).resolve().parent.parent / "bench"
SPANS_FILE = "staff_spans.json"
MODE = "fixture"
BANNER = ("FIXTURE RUN - spans were written by hand from the inputs. This measures the backend "
          "rules only; it is NOT a model benchmark and says nothing about live Gemini accuracy.")
# Gold values this code disagrees with on purpose; reported, never hidden or tuned away.
KNOWN_GOLD_DISAGREEMENTS = {
    ("ST-01", "duplicate_of"): (
        "DH6-101 (78 lakh, 4 bed / 5 bath, 500 yd, same phase) and the reserved DH6-106 are "
        "closer to ST-01 than BTK-101 is to ST-02, which the gold does flag; the RFC rule "
        "(same location + size + price within tolerance) flags both. Owner question Q1."),
}
MISSING_NAMES = {"location": "location_key"}


class BenchError(Exception):
    pass


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rows():
    rows = []
    for code, p in benchmark.load_demo()["properties"].items():
        nominal = p["size_nominal_sqyd"]
        sqft = (Decimal(nominal) * 9 if nominal is not None
                else Decimal(p["area_value"]) * benchmark.SQFT_PER_UNIT[p["area_unit"]])
        rows.append({"id": code, "version": 1, "code": code, "status": p["status"],
                     "purpose": p["purpose"], "property_type": p["property_type"],
                     "location_id": p["location_key"], "sqft": sqft,
                     "price_pkr": p["price_pkr"], "bedrooms": p["bedrooms"]})
    return rows


def _check(gold, got):
    return {"gold": gold, "got": got, "ok": gold == got}


def _draft_item(item, calls, rows, gazetteer):
    text, gold = item["input"], item["expected_outcome"]
    staff = staff_drafts.context([text], {1}, True)
    result = sn.normalise_drafts(calls[0]["args"], staff, gazetteer, marla_sqft=225,
                                 listings=rows)
    if "error" in result:
        return {"exact": False, "failure": result["error"], "fields": {}, "workflow": {}}
    draft = result["drafts"][0]
    f, want = draft.fields, gold["fields"]
    size = f.get("size") or {}
    got = {"purpose": (f.get("purpose") or {}).get("value"),
           "property_type": (f.get("property_type") or {}).get("value"),
           "location_key": (f.get("location") or {}).get("location_id"),
           "price_pkr": (f.get("price_pkr") or {}).get("value"),
           "area_value": size.get("value"), "area_unit": size.get("unit"),
           "bedrooms": (f.get("bedrooms") or {}).get("value")}
    if "size_nominal_sqyd" in want:
        got["size_nominal_sqyd"] = size.get("nominal_sqyd")
    slots = ["purpose", "property_type", "location_key", "price_pkr", "area_value",
             "area_unit", "size_nominal_sqyd"]
    if want.get("property_type") in sn.BEDROOM_TYPES or "bedrooms" in want:
        slots.append("bedrooms")
    fields = {slot: _check(want.get(slot), got.get(slot)) for slot in slots if slot in got}
    names = [MISSING_NAMES.get(m, m) for m in draft.missing]
    codes = [d["code"] for d in draft.duplicates if "code" in d]
    workflow = {
        "missing": _check(sorted(gold["missing"]), sorted(names)),
        "save_allowed": _check(gold["save_allowed"], not draft.blockers()),
        "duplicate_of": _check(gold["duplicate_of"], codes),
    }
    if "expected_code" in gold:
        workflow["expected_code"] = _check(gold["expected_code"], draft.proposed_code)
    return {"exact": all(c["ok"] for c in fields.values()), "failure": None,
            "fields": fields, "workflow": workflow,
            "blockers": list(draft.blockers()), "warnings": list(draft.warnings)}


def _target(rows, code):
    return next((r for r in rows if r["code"] == code), None)


def _update_item(item, spec, rows, gazetteer):
    """find_listings / propose_update calls of one item, run as the staff binding runs them."""
    text, gold = item["input"], item["expected_outcome"]
    staff = staff_drafts.context([text], (), False)
    found = proposal = failure = None
    for call in spec["calls"]:
        if call["tool"] == "find_listings":
            verified = staff_drafts.verify_find_listings(call["args"], staff)
            if "error" in verified:
                failure = verified["error"]
                break
            found = sn.find_candidates(verified["spans"], rows, gazetteer)
            if "error" in found:
                failure, found = found["error"], None
                break
        else:
            verified = staff_drafts.verify_propose_update(call["args"], staff)
            failure = verified.get("error") or sn.resolve_target(verified, found)
            parsed = sn.parse_changes(verified["verified"]) if not failure else {}
            failure = failure or parsed.get("error")
            if failure:
                break
            prop = _target(rows, verified["target"])
            checked = sn.check_update(prop, parsed["changes"]) if prop else {
                "error": "target_not_found"}
            if "error" in checked:
                failure = checked["error"]
                break
            proposal = {"code": verified["target"], "changes": parsed["changes"]}
    if gold["type"] == "update":
        want = {"code": gold["listing"], "changes": gold["changes"]}
        return {"exact": proposal == want, "failure": failure,
                "fields": {"target_and_changes": _check(want, proposal)}, "workflow": {}}
    codes = [r["code"] for r in (found or {}).get("candidates", [])]
    pending = sn.parse_changes([c for c in spec.get("pending_changes", [])
                                if c["span"].lower() in text.lower()])
    allowed = set(gold["candidates"]) | set(gold.get("allowed", []))
    ok = (bool(codes) and set(gold["candidates"]) <= set(codes) <= allowed
          and proposal is None and pending.get("changes") == gold["changes"])
    return {"exact": ok, "failure": failure,
            "fields": {"candidates": _check(gold["candidates"], codes),
                       "changes": _check(gold["changes"], pending.get("changes")),
                       "no_target_chosen": _check(True, proposal is None)},
            "workflow": {}}


def run(bench_dir=BENCH_DIR, *, mode=MODE):
    """The fixture result of all 20 items (a dict, JSON-ready)."""
    if mode != MODE:
        raise BenchError("only fixture results can be produced here; a live model run is a "
                         "separate, owner-approved step")
    bench_dir = Path(bench_dir)
    items = json.loads((bench_dir / "staff.json").read_text("utf-8"))
    spans = json.loads((bench_dir / SPANS_FILE).read_text("utf-8"))
    rows = _rows()
    gazetteer = location.Gazetteer.from_dataset(demo_dataset.load())
    out = []
    for item in items:
        spec = spans[item["id"]]
        kind = item["expected_outcome"]["type"]
        result = (_draft_item(item, spec["calls"], rows, gazetteer) if kind == "draft"
                  else _update_item(item, spec, rows, gazetteer))
        out.append({"id": item["id"], "kind": kind, **result})
    return {"mode": mode, "model": None, "banner": BANNER,
            "bench_sha256": _sha256(bench_dir / "staff.json"),
            "spans_sha256": _sha256(bench_dir / SPANS_FILE),
            "summary": _summary(out), "items": out}


def _summary(items):
    disagreements, workflow_ok, workflow_total = [], 0, 0
    for item in items:
        for name, check in item["workflow"].items():
            known = KNOWN_GOLD_DISAGREEMENTS.get((item["id"], name))
            if known and not check["ok"]:
                disagreements.append({"item": item["id"], "check": name, "gold": check["gold"],
                                      "got": check["got"], "reason": known})
                check["known_disagreement"] = True
                continue
            workflow_total += 1
            workflow_ok += check["ok"]
    fields = [c for item in items for c in item["fields"].values()]

    def exact(kind):
        chosen = [i for i in items if i["kind"] == kind]
        return {"exact": sum(i["exact"] for i in chosen), "of": len(chosen)}

    return {
        "items_exact": {"exact": sum(i["exact"] for i in items), "of": len(items)},
        "by_kind": {k: exact(k) for k in ("draft", "update", "disambiguate")},
        "fields": {"correct": sum(c["ok"] for c in fields), "of": len(fields)},
        "workflow": {"passed": workflow_ok, "of": workflow_total},
        "known_gold_disagreements": disagreements,
        "failed_items": [{"id": i["id"], "reason": i["failure"]} for i in items
                         if not i["exact"]],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", help="write the JSON result here")
    args = parser.parse_args(argv)
    result = run()
    print(BANNER)
    print(json.dumps(result["summary"], indent=2, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, default=str), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
