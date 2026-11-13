"""Day13 collection, reviewed gold freeze and acceptance evidence checks (no paid calls).

python -m app.day13_bench pack --out ../output/day13-recordings
python -m app.day13_bench freeze --review FILE --manifest FILE --verified-gold FILE --out FILE
python -m app.day13_bench report --bundle FILE --out DIR
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from app import benchmark, staff_bench, stt_bench

SPEC = benchmark.BENCH_DIR / "day13_thresholds.json"
GOLD_FILES = (*benchmark.FILES.values(), "staff_spans.json", "scope_conversations.json",
              "day13_thresholds.json", "day13_gold_decisions.json")


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def public_hash(path):
    """Git text bytes, independent of Windows core.autocrlf checkout conversion."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load(path):
    return json.loads(Path(path).read_text("utf-8"))


def write(path, data):
    stt_bench.write_json_atomic(Path(path), data)


def pack(out):
    """Only assignments and templates. Never fabricate audio/provenance/verified gold."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    voice, controls = stt_bench.bench_items()
    fields = ["item_id", "speaker", "phone", "android_device", "noise", "target_seconds",
              "paired_with", "script", "note"]
    rows = []
    for n, item in enumerate((*voice, *controls)):
        planned = item["recording"]
        device = n % 3 + 1
        rows.append({"item_id": item["id"], "speaker": f"S{n % 6 + 1}",
                     "phone": "android_low" if device == 1 else "android_mid",
                     "android_device": f"A{device}", "noise": planned["noise"],
                     "target_seconds": planned["target_seconds"],
                     "paired_with": item.get("paired_with", ""),
                     "script": item.get("script") or "", "note": item.get("note", "")})
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    (out / "assignments.csv").write_bytes(buf.getvalue().encode("utf-8-sig"))
    manifest_fields = list(stt_bench.REQUIRED_COLUMNS) + ["android_device", "storage_key", "origin"]
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, manifest_fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows({k: r.get(k, "") for k in manifest_fields} for r in rows)
    (out / "manifest.template.csv").write_bytes(buf.getvalue().encode())
    write(out / "verified-gold.template.json", {"schema": stt_bench.SCHEMA,
        "items": {r["item_id"]: {"audio_sha256": "", "verified_by": "", "verified_on": "",
                                "as_scripted": None} for r in rows}})
    write(out / "review.template.json", {"reviewed_by": "", "reviewed_on": "",
        "behavior_reviewed": False, "decisions": load(benchmark.BENCH_DIR / "day13_gold_decisions.json")["decisions"],
        "long_note_exceptions": [],
        "instructions": "Resolve gold/approved-behavior conflicts BEFORE any paid run. "
                        "Decision entries: item, prior behavior, approved behavior, reason, evidence."})
    unresolved = [i["id"] for i in benchmark.load_scope()["items"] if i.get("gold_status") == "unresolved"]
    write(out / "review-queue.json", {"unresolved_gold": unresolved,
        "known_staff_disagreements": sorted({item for item, _ in staff_bench.KNOWN_GOLD_DISAGREEMENTS}),
        "review_required": ["CT-30/CV handoff now uses the completed 11.x path, not a stub",
                            "ST-15: Sports City currently clarifies; review expected target against approved alias behavior",
                            "Purpose switch with a same-message budget keeps that budget",
                            "Cheaper exclusions and location graph match current approved fixtures"],
        "decision": "Record source-backed behavior decisions before freeze; no result-driven gold edits"})
    write(out / "profile.json", load(SPEC))
    bounds = {"input_per_mtok": None, "cached_input_per_mtok": None,
        "output_per_mtok": None, "max_input_tokens": None, "max_output_tokens": None,
        "model_input_token_limit": None,
        "max_request_bytes": None, "count_margin_tokens": None, "cache_min_tokens": None,
        "bounds_source": "https://ai.google.dev/gemini-api/docs/tokens",
        "count_tokens_free": False, "output_includes_thinking": False,
        "single_candidate_default_verified": False, "count_before_generate": False}
    bounds["thinking_levels"] = []
    write(out / "prices.template.json", {"schema": 1, "currency": "USD",
        "source": "https://ai.google.dev/gemini-api/docs/pricing", "verified_on": "",
        "account_verified": False, "gold_sha256": "", "models": {"REPLACE_RUNTIME_MODEL": bounds},
        "scribe": {"source": "https://elevenlabs.io/pricing/api", "per_hour": None,
                   "keyterms_per_hour": None, "minimum_seconds": None, "round_to_seconds": None},
        "instructions": "Do not use free tier as zero marginal price. Verify actual model IDs, "
                        "all applicable rate tiers, keyterm surcharge, limits, countTokens and thinking. "
                        "Use the same ledger for collection, tuning, formal runs and necessary retries."})
    write(out / "bundle.template.json", {"schema": 1,
        "candidate": {"commit": "", "image": "", "config_sha256": ""}, "freeze": None,
        "runs": {}, "traces": None, "policy": None, "ledger": None,
        "runtime": None, "metrics": None, "rehearsals": None, "decisions": None})
    blank_candidate = {"commit": "", "image": "", "config_sha256": ""}
    write(out / "metrics.template.json", {"schema": 1, "candidate": blank_candidate,
        "freeze_sha256": "", "metrics": {gate["id"]: [
            {"repeat": rep, "reviewed_by": "", "coverage": "all_applicable_cases",
             "numerator": None, "denominator": None, "artifacts": []}
            for rep in range(1, 4)] for gate in load(SPEC)["gates"] if gate["source"] == "manual"}})
    write(out / "rehearsals.template.json", {"schema": 1, "candidate": blank_candidate,
        "consecutive": False, "web_carousel_passed": False, "web_artifacts": [], "items": [
            {"scenario": scenario, "repeat": rep, "device": "android", "passed": None,
             "reviewed_by": "", "artifacts": []} for rep in range(1, 4) for scenario in range(1, 6)]})
    write(out / "decisions.template.json", [])
    preflight = (Path(__file__).resolve().parent.parent / "deploy/day13_preflight.py").read_bytes()
    (out / "runtime-preflight.sh").write_bytes(b"sudo python3 - <<'DAY13_READONLY_PY'\n" + preflight
                                              + b"\nDAY13_READONLY_PY\n")
    (out / "README.md").write_bytes(COLLECTION.encode())
    return {"speech": len(voice), "controls": len(controls),
            "paired_text": sum(bool(i.get("paired_with")) for i in voice),
            "noise": dict(Counter(r["noise"] for r in rows[:len(voice)])),
            "status": "assignments_only_not_acceptance_evidence"}


COLLECTION = """# Day13 recording collection

Use assignments.csv: 30 speech notes plus 5 controls, six consenting speakers,
three distinct Android phones A1/A2/A3 (A1 low-end; A2/A3 mid-range). Keep the
private person/device mapping outside Git. iPhone is unavailable by the owner's
explicit exception; it remains untested. Preserve 15 quiet, 10 moderate and 5
street speech notes. Read naturally; do not use synthetic or uploaded audio.

Send each as an actual WhatsApp voice note. Keep a private item-to-message mapping.
Use app.bench_manifest on EC2 to export stored media metadata, then match each item
to that export. Retrieve the exact stored Ogg bytes privately as CV-01.ogg etc.
Do not send credentials, actual phone numbers, or raw audio to Git. A manifest
needs stored status, is_voice=true, audio/ogg, measured duration, byte size,
SHA256, original tenant/.../audio/...ogg storage key, and android_device A1/A2/A3.
The template intentionally leaves these evidence fields empty.

Aim for natural 3-30 second speech notes where possible. Longer notes remain in
quality scoring but are diagnostic for latency; never silently remove them. The
previously accepted 81-second note can be recorded in review.long_note_exceptions
by item ID without demanding a replacement solely for duration. Over-120-second
control remains a separate truncation/control test.

Listen to every file before benchmarking. In verified-gold.template.json, enter
the exact audio hash, reviewer and date. Set as_scripted=true only if it matches.
For deviations follow docs/STT_BENCHMARK.md: actual transcript and full extraction;
also reconcile expected results/tools before freezing. Do not relabel a planned
script as verified. Twelve paired text cases are listed in assignments.csv.

Review all gold against approved product behavior, record decisions in review,
then freeze the files with app.day13_bench. No paid calls precede that freeze.
Keep final manifests/gold/audio private; share sanitized reports only.

Formal device evidence: five Scope scenarios, three consecutive rehearsals on
Android, plus WhatsApp Web carousel. Record scenario/repeat, candidate image and
config hash, timestamp, result and private screenshot/video hash. iPhone untested.
"""


def freeze(review_path, manifest_path, gold_path, out):
    review = load(review_path)
    if not review.get("reviewed_by") or not review.get("reviewed_on") \
            or review.get("behavior_reviewed") is not True or not isinstance(review.get("decisions"), list):
        raise ValueError("gold needs a named, dated behavior review and recorded decisions")
    date.fromisoformat(review["reviewed_on"])
    data = {"schema": 1, "spec": load(SPEC), "review": review,
            "bench_sha256": {p: public_hash(benchmark.BENCH_DIR / p) for p in GOLD_FILES},
            "manifest_sha256": file_hash(manifest_path), "verified_gold_sha256": file_hash(gold_path)}
    unresolved = {i["id"] for i in benchmark.load_scope()["items"] if i.get("gold_status") == "unresolved"}
    unresolved |= {item for item, _ in staff_bench.KNOWN_GOLD_DISAGREEMENTS}
    resolved = {d.get("item") for d in review["decisions"]
                if d.get("approved_behavior") and d.get("reason") and d.get("evidence")}
    if not unresolved <= resolved:
        raise ValueError("unresolved gold needs a source-backed decision: " + ", ".join(sorted(unresolved - resolved)))
    # Structural evidence must be valid; file byte checks run again before every paid call cohort.
    checked, _ = recordings(manifest_path, None, gold_path, data, need_audio=False)
    data["audio"] = {key: {"sha256": row.sha256, "duration_ms": row.duration_ms}
                     for key, row in checked.rows.items()}
    write(out, data)
    return {"status": "frozen", "sha256": file_hash(out)}


def check_freeze(path):
    data = load(path)
    if data.get("schema") != 1 or data.get("spec") != load(SPEC) \
            or set(data.get("bench_sha256", {})) != set(GOLD_FILES):
        raise ValueError("unsupported or changed threshold freeze")
    for name, expected in data["bench_sha256"].items():
        if public_hash(benchmark.BENCH_DIR / name) != expected:
            raise ValueError("gold changed after freeze; review and start a new cohort")
    review = data.get("review", {})
    if not review.get("reviewed_by") or not review.get("reviewed_on") \
            or review.get("behavior_reviewed") is not True or not isinstance(review.get("decisions"), list):
        raise ValueError("freeze lacks the completed behavior review")
    date.fromisoformat(review["reviewed_on"])
    for key in ("manifest_sha256", "verified_gold_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", data.get(key, "")):
            raise ValueError("freeze lacks recording evidence hashes")
    return data


def recordings(manifest_path, audio_dir, gold_path, frozen, *, need_audio=True):
    if not manifest_path or not gold_path or (need_audio and not audio_dir):
        raise ValueError("recordings need manifest, audio directory and verified gold")
    if file_hash(manifest_path) != frozen["manifest_sha256"] \
            or file_hash(gold_path) != frozen["verified_gold_sha256"]:
        raise ValueError("recording manifest or verified gold differs from freeze")
    text = Path(manifest_path).read_text("utf-8-sig")
    check = stt_bench.validate_manifest(text, audio_dir=audio_dir)
    # These two explicit substitutions apply only to this Day13 profile. Never
    # relax the original R16 validator or silently claim iPhone coverage.
    blockers = [b for b in check.blockers if not b.startswith("phone classes ")
                and not b.startswith("audio files not checked")
                and "note(s) outside" not in b]
    raw = list(csv.DictReader(io.StringIO(text)))
    speech_ids = {i["id"] for i in stt_bench.bench_items()[0]}
    devices = {r.get("android_device") for r in raw if r["item_id"] in speech_ids}
    if len(devices - {None, ""}) < 3 or any(not re.fullmatch(r"A[1-9]", d or "") for d in devices):
        blockers.append("three distinct Android device pseudonyms A1..A9 required")
    if any(r.phone not in ("android_low", "android_mid") for r in check.rows.values()):
        blockers.append("Day13 exception requires Android recordings")
    exceptions = frozen["review"].get("long_note_exceptions", [])
    for row in check.rows.values():
        if row.item_id in speech_ids and (row.duration_ms < 3000 or
                (row.duration_ms > 45_000 and row.item_id not in exceptions)):
            blockers.append(f"{row.item_id}: nonstandard duration needs a recorded diagnostic decision")
    if {row.phone for row in check.rows.values() if row.item_id in speech_ids} \
            != {"android_low", "android_mid"}:
        blockers.append("preserve low-end and mid-range Android coverage")
    verified = stt_bench.load_verified(gold_path)
    voice, controls = stt_bench.bench_items()
    gold, problems = stt_bench.build_gold((*voice, *controls), verified, check.rows)
    if any(g.status != "verified" for g in gold.values()):
        blockers.append("every recording requires human-verified audio-bound gold")
    # Expected tools/outcomes cannot be guessed after hearing a deviating recording.
    if any(g.deviates for g in gold.values()):
        blockers.append("deviating recordings require reconciled committed gold before freeze")
    issues = check.errors + blockers + problems
    if issues:
        raise ValueError("recording preflight: " + "; ".join(issues))
    return check, gold


def compare(baseline_path, candidate_path, out):
    """Tuning comparison only. Never pool low/minimal cohorts or auto-adopt a change."""
    from app.day13_report import quantiles
    baseline, candidate = load(baseline_path), load(candidate_path)
    a, b = baseline["identity"], candidate["identity"]
    for key in ("commit", "bench_sha256", "source_sha256", "demo_sha256", "set"):
        if a.get(key) != b.get(key):
            raise ValueError("tuning comparison changed source, gold or case set")
    if any(r.get("status") != "finished" or r["identity"].get("mode") != "live"
           or not r.get("coverage", {}).get("subset_complete")
           or r["identity"].get("day13", {}).get("phase") != "tuning" for r in (baseline, candidate)):
        raise ValueError("tuning comparison requires completed live tuning subsets")
    if a["model"]["primary"] != b["model"]["primary"] or a["model"]["failover"] != b["model"]["failover"]:
        raise ValueError("compare effort with the same Gemini models")
    if any(a["day13"].get(k) != b["day13"].get(k) for k in ("freeze_sha256", "policy_sha256", "speech")):
        raise ValueError("tuning comparison changed recording gold, speech model or pricing/bounds")
    if baseline["coverage"]["requested"] != candidate["coverage"]["requested"] \
            or len(baseline["repeats"]) != len(candidate["repeats"]):
        raise ValueError("compare the same cases and repeat count")
    result = {"schema": 1, "task": "13.3", "status": "owner_review_required", "cohorts": [],
        "adopted": False, "boundary": "backend/fake Meta; not a deployed SLA measurement"}
    for run in (baseline, candidate):
        rows = [r for rep in run["repeats"] for r in rep["items"]]
        attempts = [a for r in rows for a in r.get("attempts", [])]
        all_rows = rows + [r for rep in run["repeats"] for r in rep.get("setup", [])]
        request_ids = {rid for r in all_rows for rid in r.get("budget_request_ids", [])}
        ledger_rows = [r for r in run.get("caps", {}).get("requests", []) if r["id"] in request_ids]
        result["cohorts"].append({"thinking_level": run["identity"]["model"]["thinking_level"],
            "cases": len(rows), "critical_exact": sum(bool(r.get("critical_exact")) for r in rows),
            "critical_denominator": sum(bool(r.get("has_critical")) for r in rows),
            "end_to_end_exact": sum(bool(r.get("end_to_end", {}).get("ok")) for r in rows),
            "latency_ms": quantiles([r.get("latency_ms") if r.get("latency_ms") is not None else float("inf")
                                     for r in rows]),
            "provider_requests": sum(a.get("outcome") != "not_sent" for a in attempts),
            "provider_ms": sum(a.get("latency_ms", 0) for a in attempts),
            "cohort_charged_usd": sum(r["charged"] for r in ledger_rows) / 1_000_000
            if request_ids and len(ledger_rows) == len(request_ids) else None,
            "ledger_cumulative_charged_usd": run.get("caps", {}).get("charged_usd")})
    write(out, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--out", required=True)
    p = sub.add_parser("freeze")
    for arg in ("review", "manifest", "verified-gold", "out"):
        p.add_argument("--" + arg, required=True)
    p = sub.add_parser("report")
    p.add_argument("--bundle", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("compare")
    for arg in ("baseline", "candidate", "out"):
        p.add_argument("--" + arg, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            result = pack(args.out)
        elif args.command == "freeze":
            result = freeze(args.review, args.manifest, args.verified_gold, args.out)
        elif args.command == "compare":
            result = compare(args.baseline, args.candidate, args.out)
        else:
            from app.day13_report import report
            result = report(args.bundle, args.out)
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") != "incomplete" else 3
    except (ValueError, OSError, KeyError, stt_bench.BenchError) as exc:
        print(f"day13_bench: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
