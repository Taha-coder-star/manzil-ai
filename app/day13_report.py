"""Fail-closed Day13 reports. Private input; allowlisted sanitized JSON/CSV output.

Evidence is never inferred from a successful deployment or a scripted ceiling.
Missing/timeout observations remain in latency denominators (infinite latency).
"""

import csv
import io
import math
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import mean

from app import bench_live, day13_bench, stt_bench
from app.bench_budget import Ledger, digest, load_policy, token_cost, token_upper_cost

HEX = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")


def quantiles(values):
    values = sorted(values)
    def at(q):
        value = stt_bench.percentile(values, q)
        return value if value is not None and math.isfinite(value) else None
    return {"n": len(values), "missing_or_failed": sum(not math.isfinite(v) for v in values),
            "p50": at(50), "p90": at(90)}


def cache_summary(attempts, policies):
    hit = eligible = unknown = ineligible = 0
    cached = inputs = 0
    for attempt in attempts:
        usage = attempt.get("usage") or {}
        inp, got = usage.get("input_tokens"), usage.get("cached_input_tokens")
        minimum = policies.get(attempt.get("model"), {}).get("cache_min_tokens")
        if type(inp) is not int or type(minimum) is not int:
            unknown += 1
            continue
        if inp < minimum:
            ineligible += 1
            continue
        eligible += 1
        if type(got) is not int or got < 0 or got > inp:
            unknown += 1
            continue
        hit += got > 0
        cached += got
        inputs += inp
    return {"calls": len(attempts), "eligible": eligible, "hit_calls": hit,
            "unknown": unknown, "ineligible": ineligible,
            "hit_rate": hit / eligible if eligible and not unknown else None,
            "cached_tokens": cached, "measured_input_tokens": inputs,
            "cached_input_ratio": cached / inputs if inputs else None,
            "padding_added": False}


def trace_rows(data, candidate):
    """Strict trace schema. Acknowledgements/echo/notices never satisfy substantive SLA."""
    rows, seen = [], set()
    if data.get("schema") != 1 or data.get("candidate") != candidate:
        raise ValueError("trace candidate mismatch")
    for raw in data.get("turns", []):
        key = (raw["repeat"], raw["item_id"])
        if key in seen or type(key[0]) is not int or not 1 <= key[0] <= 3:
            raise ValueError("duplicate trace or invalid repeat")
        seen.add(key)
        if not re.fullmatch(r"(?:CT|CV|VC|SC|ST)-\d{2}", key[1]):
            raise ValueError("trace uses an unknown item pseudonym")
        if raw["modality"] not in ("text", "voice", "control", "staff"):
            raise ValueError("invalid trace modality")
        received = raw.get("received_ms") or []
        if not received or any(type(t) not in (int, float) or not math.isfinite(t) for t in received):
            raise ValueError("trace needs persisted inbound receipt times")
        first, last = min(received), max(received)
        sends = raw.get("sends", [])
        for send in sends:
            if send.get("kind") not in ("echo", "notice", "substantive"):
                raise ValueError("outbound payload needs reviewed semantic classification")
            for field in ("accepted_ms", "visible_ms", "delivered_ms"):
                t = send.get(field)
                if t is not None and (type(t) not in (int, float) or not math.isfinite(t) or t < first):
                    raise ValueError("invalid outbound timestamp")
        answers = [s["accepted_ms"] for s in sends if s["kind"] == "substantive"
                   and s.get("accepted_ms") is not None]
        visible = [s["visible_ms"] for s in sends if s.get("visible_ms") is not None]
        delivered = [s["delivered_ms"] for s in sends if s["kind"] == "substantive"
                     and s.get("delivered_ms") is not None]
        duration = raw.get("duration_ms")
        if raw["modality"] == "voice" and (type(duration) is not int or duration <= 0):
            raise ValueError("voice trace needs actual audio duration")
        status_ok = raw.get("status") == "succeeded"
        primary = min(answers) - first if answers and status_ok else None
        row = {"repeat": key[0], "item_id": key[1], "modality": raw["modality"],
               "sla_eligible": raw["modality"] == "text" or (
                   raw["modality"] == "voice" and duration <= 30_000),
               "duration_ms": duration, "first_substantive_ms": primary,
               "last_receipt_substantive_ms": min(answers) - last if answers and status_ok else None,
               "last_substantive_ms": max(answers) - first if answers else None,
               "first_visible_ms": min(visible) - first if visible else None,
               "first_delivered_ms": min(delivered) - first if delivered else None}
        for stage in ("assembly", "queue", "stt", "model_tools", "database", "render", "outbound"):
            value = (raw.get("breakdown_ms") or {}).get(stage)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError("invalid stage measurement")
            row[stage + "_ms"] = value  # unavailable is NULL, never zero or an invented estimate
        rows.append(row)
    return rows


def latency_summary(rows):
    result = {}
    for modality in ("text", "voice"):
        repeats = []
        for repeat in range(1, 4):
            cohort = [r for r in rows if r["repeat"] == repeat and r["modality"] == modality
                      and r["sla_eligible"]]
            repeats.append({"repeat": repeat, **quantiles([
                r["first_substantive_ms"] if r["first_substantive_ms"] is not None else math.inf
                for r in cohort])})
        result[modality] = {"repeats": repeats,
                           "diagnostic_long_notes": sum(r["modality"] == modality
                                                       and not r["sla_eligible"] for r in rows)}
    return result


def rate(rows, test):
    return {"numerator": sum(bool(test(r)) for r in rows), "denominator": len(rows)}


def automatic_metrics(runs):
    """Exact denominators, separate repeat cohorts. Missing cases remain incorrect."""
    metrics = defaultdict(list)
    for repeat in range(3):
        records = {name: run["repeats"][repeat]["items"] for name, run in runs.items()}
        for name in ("text", "voice"):
            rows = records.get(name, [])
            metrics[f"{name}_exact"].append(rate(
                [r for r in rows if r.get("has_critical")],
                lambda r: r["status"] == "ran" and r.get("critical_exact")))
            results = [r for r in rows if r.get("gold_type") in ("results", "no_match")]
            metrics[f"{name}_result_sets"].append(rate(results, lambda r: r["status"] == "ran"
                                                       and r.get("end_to_end", {}).get("ok")))
        customers = records.get("text", []) + records.get("voice", [])
        ok = lambda r: r["status"] == "ran" and r.get("end_to_end", {}).get("ok")
        metrics["clarify_recall"].append(rate([r for r in customers if r["gold_type"] == "clarify"], ok))
        clear = [r for r in customers if r["gold_type"] in ("results", "no_match")]
        metrics["unnecessary_clarification"].append(rate(clear, lambda r: bool(
            r.get("end_to_end", {}).get("asked") or r.get("end_to_end", {}).get("question_buttons"))))
        metrics["price_normalisation"].append(rate([r for r in customers if r.get("has_budget")],
            lambda r: r["status"] == "ran" and r.get("budget_exact")))
        metrics["handoff_recall"].append(rate([r for r in customers if r["gold_type"] == "handoff"],
            lambda r: r["status"] == "ran" and "request_handoff" in r.get("tools", [])))
        metrics["false_handoff"].append(rate([r for r in customers if r["gold_type"] != "handoff"],
            lambda r: "request_handoff" in r.get("tools", [])))
        metrics["hidden_listings"].append({"value": sum(len(
            r.get("end_to_end", {}).get("hidden_shown", [])) for r in customers)})
        tools = customers + records.get("staff", [])
        metrics["correct_tool"].append(rate(tools, lambda r: r["status"] == "ran" and r.get("tools_ok")))
        voice = records.get("voice", [])
        for key, rows, flag in (("stt_exact", voice, "exact"),
                                ("stt_quiet_exact", [r for r in voice if r.get("noise") == "quiet"], "exact"),
                                ("stt_usable", voice, "usable")):
            metrics[key].append(rate(rows, lambda r, flag=flag: r["status"] == "ran" and r.get("stt", {}).get(flag)))
        times = [r.get("stt", {}).get("speech_ms") for r in voice]
        metrics["stt_p90_ms"].append({"value": quantiles([
            v if v is not None else math.inf for v in times])["p90"]})
        controls = records.get("controls", [])
        metrics["control_requirements"].append({"value": sum(
            not r.get("zero_requirements", False) for r in controls)})
        metrics["unconfirmed_writes"].append({"value": sum(
            r.get("listing_writes", 0) for r in records.get("staff", []))})
        staff_fields = [check for r in records.get("staff", [])
                        for check in r.get("fields", {}).values()]
        metrics["staff_fields"].append(rate(staff_fields, lambda c: c.get("ok")))
        scope = records.get("scope", [])
        follow = [r for r in scope if "follow_up" in r.get("metrics", [])]
        photos = [r for r in scope if "photos" in r.get("metrics", [])]
        metrics["context_retention"].append(rate(follow, ok))
        metrics["correct_images"].append(rate(photos, ok))
        metrics["repeat_images"].append({"value": sum(len(
            (r.get("end_to_end", {}).get("photos") or {}).get("repeats", [])) for r in photos)})
        paired = {i["id"]: i["paired_with"] for i in bench_live.load_set("voice") if i.get("paired_with")}
        text_by = {r["id"]: r for r in records.get("text", [])}
        voice_by = {r["id"]: r for r in voice}
        if len(paired) == 12 and all(v in voice_by and t in text_by for v, t in paired.items()):
            text_exact = sum(bool(text_by[t].get("critical_exact")) for t in paired.values())
            voice_exact = sum(bool(voice_by[v].get("critical_exact")) for v in paired)
            metrics["paired_gap"].append({"value": max(0, (text_exact - voice_exact) / 12),
                                          "text_correct": text_exact, "voice_correct": voice_exact,
                                          "denominator": 12})
    return dict(metrics)


def gate_result(gate, observations):
    if len(observations) != 3:
        return {"status": "unmeasured", "repeats": observations}
    values = []
    for obs in observations:
        value = obs.get("value")
        if "numerator" in obs:
            n, d = obs.get("numerator"), obs.get("denominator")
            if type(n) is not int or type(d) is not int or not 0 <= n <= d or d <= 0:
                value = None
            else:
                value = n / d
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return {"status": "unmeasured", "repeats": observations}
        values.append(value)
    lower = "min" in gate
    worst = min(values) if lower else max(values)
    passed = worst >= gate["min"] if lower else worst <= gate["max"]
    return {"status": "pass" if passed else "fail", "mean": mean(values),
            "worst_repeat": worst, "repeats": observations}


def validated_runs(paths, frozen_hash, blockers):
    runs, common = {}, None
    for name in bench_live.SETS:
        if name not in paths:
            blockers.append(f"missing formal {name} run")
            continue
        run = day13_bench.load(paths[name])
        ident = run.get("identity", {})
        binding = {k: ident.get(k) for k in ("commit", "bench_sha256", "source_sha256", "demo_sha256", "model")}
        day13 = ident.get("day13", {})
        binding["speech"] = day13.get("speech")
        binding["policy_sha256"] = day13.get("policy_sha256")
        good = (run.get("status") == "finished" and ident.get("mode") == "live"
                and ident.get("set") == name and day13.get("phase") == "formal"
                and day13.get("freeze_sha256") == frozen_hash
                and len(run.get("repeats", [])) == 3
                and run.get("repeats_planned") == 3
                and run.get("coverage", {}).get("full_set_complete") is True
                and not run.get("caps", {}).get("bound_exceeded"))
        good &= ident.get("bench_sha256") == {name: day13_bench.public_hash(
            day13_bench.benchmark.BENCH_DIR / name) for name in bench_live.IDENTITY_FILES}
        good &= bool(ident.get("source_sha256")) and all(
            isinstance(value, str) and HEX.fullmatch(value) for value in ident.get("source_sha256", {}).values())
        good &= (isinstance(ident.get("model"), dict) and bool(ident["model"].get("primary"))
                 and bool(ident["model"].get("failover")) and bool(ident["model"].get("thinking_level")))
        expected = {i["id"] for i in bench_live.load_set(name)}
        if good:
            for index, repeat in enumerate(run["repeats"], 1):
                rows = repeat.get("items", [])
                good &= (repeat.get("repeat") == index and len(rows) == len(expected)
                         and {r["id"] for r in rows} == expected
                         and all(r.get("status") == "ran" and r.get("metering_consistent") for r in rows))
                if name in ("voice", "controls"):
                    good &= all(r.get("fresh_stt") is True for r in rows)
        if not good:
            blockers.append(f"{name}: incomplete, unmetered, replayed, or wrong formal cohort")
            continue
        if common is None:
            common = binding
        elif common != binding:
            blockers.append("mixed source/model/gold configurations across sets")
            continue
        runs[name] = run
    return runs


def metering_metrics(runs, policy, *, shared_inquiries=False):
    metrics = defaultdict(list)
    for repeat in range(3):
        grouped = defaultdict(list)
        for name in ("text", "voice"):
            costs = []
            run_repeat = runs[name]["repeats"][repeat]
            for record in run_repeat["items"] + run_repeat.get("setup", []):
                events = record.get("metering")
                parts = []
                for event in events or []:
                    model = policy.get("billing_aliases", {}).get(event.get("model"), event.get("model"))
                    rates = policy["models"].get(model)
                    cost = token_cost(rates, event) if rates else None
                    if cost is None and rates:
                        cost = token_upper_cost(rates, event)
                    if event.get("provider") == "elevenlabs" and event.get("audio_seconds") is not None:
                        rates = policy.get("scribe")
                        if rates:
                            seconds = max(rates["minimum_seconds"], math.ceil(float(event["audio_seconds"])))
                            seconds = math.ceil(seconds / rates["round_to_seconds"]) * rates["round_to_seconds"]
                            cost = Decimal(seconds) * (Decimal(str(rates["per_hour"]))
                                + Decimal(str(rates["keyterms_per_hour"]))) / 3600
                    # Failure/no usage is not a zero-cost call. No metering is unknown.
                    parts.append(cost if event.get("outcome") == "succeeded" else None)
                cost = sum(parts, Decimal(0)) if parts and all(p is not None for p in parts) else None
                if record in run_repeat["items"]:
                    costs.append(cost)
                group = tuple(record.get("group", [record["id"]]))
                grouped[group if shared_inquiries else (name, group)].append(cost)
            metrics[f"{name}_cost_usd"].append({"value": float(max(costs))
                if costs and all(c is not None for c in costs) else None, "denominator": len(costs)})
        inquiries = [sum(cs, Decimal(0)) if all(c is not None for c in cs) else None
                     for cs in grouped.values()]
        metrics["inquiry_cost_usd"].append({"value": float(max(inquiries))
            if inquiries and all(c is not None for c in inquiries) else None,
            "denominator": len(inquiries)})
    return dict(metrics)


def artifacts_verified(entries, base):
    if not entries:
        return False
    for entry in entries:
        path = Path(base) / entry["path"]
        if not HEX.fullmatch(entry.get("sha256", "")) or not path.is_file() \
                or day13_bench.file_hash(path) != entry["sha256"]:
            return False
    return True


def report(bundle_path, out):
    bundle = day13_bench.load(bundle_path)
    base = Path(bundle_path).resolve().parent
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    blockers = []
    raw_candidate = bundle.get("candidate", {})
    patterns = {"commit": r"[0-9a-f]{40}", "image": r"sha256:[0-9a-f]{64}",
                "config_sha256": r"[0-9a-f]{64}"}
    candidate = {key: raw_candidate.get(key) if isinstance(raw_candidate.get(key), str)
                 and re.fullmatch(pattern, raw_candidate[key]) else None for key, pattern in patterns.items()}
    if not IMAGE.fullmatch(candidate.get("image") or "") or not HEX.fullmatch(
            candidate.get("config_sha256") or "") or not re.fullmatch(r"[0-9a-f]{40}", candidate.get("commit") or ""):
        blockers.append("candidate needs exact commit, image and non-secret config hash")
    frozen_hash = None
    frozen = None
    if bundle.get("freeze"):
        freeze_path = base / bundle["freeze"]
        frozen = day13_bench.check_freeze(freeze_path)
        frozen_hash = day13_bench.file_hash(freeze_path)
    else:
        blockers.append("reviewed gold/threshold freeze missing")
    paths = {k: base / v for k, v in bundle.get("runs", {}).items()}
    runs = validated_runs(paths, frozen_hash, blockers)
    if bundle.get("runtime"):
        runtime = day13_bench.load(base / bundle["runtime"])
        services = runtime.get("services", [])
        runtime_ok = (runtime.get("same_image_and_config") is True
                      and runtime.get("shared_writable_budget_mount") is True
                      and {r.get("service") for r in services} == {"deploy-web-1", "deploy-worker-1"}
                      and len(services) == 2)
        for service in services:
            config = service.get("config", {})
            runtime_ok &= (service.get("running") is True and service.get("budget_enabled") is True
                and service.get("budget_phase") == "formal" and service.get("image") == candidate["image"]
                and service.get("config_sha256") == candidate["config_sha256"] == digest(config)
                and config.get("AI_PROVIDER") == "gemini"
                and config.get("STT_PROVIDER") == "elevenlabs" and config.get("STT_FALLBACK") == "gemini")
            for run in runs.values():
                model = run["identity"]["model"]
                runtime_ok &= (config.get("LLM_PRIMARY_MODEL") == model["primary"]
                    and config.get("LLM_FAILOVER_MODEL") == model["failover"]
                    and config.get("LLM_THINKING_LEVEL_EFFECTIVE") == model["thinking_level"])
        if not runtime_ok:
            blockers.append("runtime candidate/config or shared formal budget guard mismatch")
    else:
        blockers.append("exact-image runtime config and shared budget preflight missing")
    if any(run["identity"]["commit"] != candidate.get("commit") for run in runs.values()):
        blockers.append("benchmark commit differs from deployed candidate")
    metrics = automatic_metrics(runs) if len(runs) == len(bench_live.SETS) else {}
    trace = []
    latency = {}
    if bundle.get("traces"):
        data = day13_bench.load(base / bundle["traces"])
        trace = trace_rows(data, candidate)
        if frozen:
            for row in trace:
                expected_audio = frozen.get("audio", {}).get(row["item_id"])
                if row["modality"] == "voice" and (not expected_audio
                        or row["duration_ms"] != expected_audio["duration_ms"]):
                    blockers.append("deployed voice cohort duration differs from frozen recordings")
        latency = latency_summary(trace)
        expected = {i["id"] for name in ("text", "voice") for i in bench_live.load_set(name)}
        for rep in range(1, 4):
            if not expected <= {r["item_id"] for r in trace if r["repeat"] == rep}:
                blockers.append(f"deployed trace repeat {rep} missing expected cases")
        for modality in ("text", "voice"):
            for q in (50, 90):
                metrics[f"{modality}_p{q}_ms"] = [
                    {"value": r[f"p{q}"]} for r in latency[modality]["repeats"]]
    else:
        blockers.append("deployed WhatsApp cohort missing (fake client timing cannot prove SLA)")
    policy = None
    budget = None
    if bundle.get("policy") and bundle.get("ledger"):
        policy = load_policy(base / bundle["policy"])
        budget = Ledger(base / bundle["ledger"], policy).summary()
        if budget["bound_exceeded"] or float(budget["charged_usd"]) > 10:
            blockers.append("budget bound exceeded")
        if budget["inflight_or_unbounded"]:
            blockers.append("provider charges remain unknown or inflight; reconcile billing")
        if policy.get("gold_sha256") != frozen_hash or any(
                run["identity"]["day13"]["policy_sha256"] != budget["policy_sha256"] for run in runs.values()):
            blockers.append("pricing/ledger/freeze cohort mismatch")
        if len(runs) == len(bench_live.SETS):
            metrics.update(metering_metrics(runs, policy))
        if trace:
            deployed = {name: {"repeats": [{"items": [
                {"id": t["item_id"], "metering": t.get("metering"),
                 "group": [t.get("inquiry_id") or t["item_id"]]}
                for t in data["turns"] if t["modality"] == name and t["repeat"] == rep]}
                for rep in range(1, 4)]} for name in ("text", "voice")}
            deployed_costs = metering_metrics(deployed, policy, shared_inquiries=True)
            if any(not re.fullmatch(r"I\d{1,4}", str(t.get("inquiry_id", "")))
                   for t in data["turns"] if t["modality"] in ("text", "voice")):
                deployed_costs["inquiry_cost_usd"] = []
            metrics.update(deployed_costs)  # actual deployed usage has priority over backend usage
    else:
        blockers.append("shared provider budget/pricing evidence missing")
    attempts = [a for run in runs.values() for rep in run["repeats"] for r in rep["items"]
                for a in r.get("attempts", []) if a.get("outcome") != "not_sent"]
    caching = cache_summary(attempts, (policy or {}).get("models", {}))
    if not attempts or caching["unknown"]:
        blockers.append("cache eligibility/hit metadata unverified")
    # Supplemental observations must name reviewer, cohort and existing hash-checked artifacts.
    if bundle.get("metrics"):
        supplied = day13_bench.load(base / bundle["metrics"])
        if supplied.get("candidate") != candidate or supplied.get("freeze_sha256") != frozen_hash:
            raise ValueError("supplemental metrics cohort mismatch")
        for key, observations in supplied.get("metrics", {}).items():
            if key in metrics:
                raise ValueError("supplemental metrics cannot replace automatic measurements")
            if all(o.get("repeat") == n and o.get("reviewed_by")
                   and o.get("coverage") == "all_applicable_cases"
                   and artifacts_verified(o.get("artifacts"), base)
                   for n, o in enumerate(observations, 1)):
                metrics[key] = observations
    gates = {}
    for gate in day13_bench.load(day13_bench.SPEC)["gates"]:
        gates[gate["id"]] = {**gate, **gate_result(gate, metrics.get(gate["id"], []))}
    rehearsals_ok = False
    if bundle.get("rehearsals"):
        rehearsals = day13_bench.load(base / bundle["rehearsals"])
        rows = rehearsals.get("items", [])
        expected = {(scenario, repeat, "android") for scenario in range(1, 6) for repeat in range(1, 4)}
        seen = {(r.get("scenario"), r.get("repeat"), r.get("device")) for r in rows
                if r.get("passed") is True and r.get("reviewed_by") and artifacts_verified(r.get("artifacts"), base)}
        rehearsals_ok = (rehearsals.get("candidate") == candidate and expected <= seen
                         and rehearsals.get("consecutive") is True
                         and rehearsals.get("web_carousel_passed") is True
                         and artifacts_verified(rehearsals.get("web_artifacts"), base))
    if not rehearsals_ok:
        blockers.append("three consecutive Android five-scenario rehearsals and Web carousel evidence missing")
    gaps = [key for key, g in gates.items() if g["status"] != "pass"]
    # A decision records the gap; it does not by itself waive a threshold or safety gate.
    decisions = day13_bench.load(base / bundle["decisions"]) if bundle.get("decisions") else []
    decisions_ok = all(d.get("gate") in gates and d.get("decision") and d.get("owner")
                       and d.get("source_permission") for d in decisions)
    if decisions and not decisions_ok:
        blockers.append("cut/fallback decisions need owner, rationale and source permission")
    accepted = []
    for decision in decisions if decisions_ok else []:
        key = decision["gate"]
        gate = gates[key]
        # 13.4 permits remaining measured gaps on an explicit cut list. It does
        # not waive missing runs, missing evidence, safety, or an unmeasured gate.
        if gate["status"] == "fail" and not gate.get("safety") \
                and decision.get("adopted") is True \
                and decision.get("source_permission") == "Day13 task 13.4" \
                and artifacts_verified(decision.get("artifacts"), base):
            accepted.append(key)
            gate["decision"] = "owner_adopted_cut_or_fallback"
    unresolved = [key for key in gaps if key not in accepted]
    result = {"schema": 1, "status": "incomplete" if blockers or unresolved else "complete_with_recorded_exceptions",
              "candidate": candidate, "freeze_sha256": frozen_hash, "blockers": sorted(set(blockers)),
              "gates": gates, "remaining_gaps": gaps, "accepted_gaps": accepted,
              "unresolved_gaps": unresolved, "latency": latency, "cache": caching,
              "budget": {k: v for k, v in (budget or {}).items() if k != "requests"},
              "exceptions": day13_bench.load(day13_bench.SPEC)["exceptions"],
              "decision_count": len(decisions), "scope_compliant_without_exceptions": False,
              "decision_file_sha256": day13_bench.file_hash(base / bundle["decisions"])
              if bundle.get("decisions") else None,
              "notes": ["Nearest-rank percentiles; first receipt to substantive Meta acceptance.",
                        "Costs are usage-based pricing estimates; missing fields use explicit conservative upper bounds, not invoice totals.",
                        "Long notes/controls diagnostic; failed/missing turns remain in coverage.",
                        "No Sonnet switch or always-on voice echo adopted automatically."]}
    day13_bench.write(out / "report.json", result)
    export_csv(out / "traces.csv", trace)
    lines = ["# Day13 benchmark run #2", "", f"Status: **{result['status']}**.", "",
             "iPhone remains untested under the owner-approved exception.", "", "| Gate | Status | Mean | Worst repeat |",
             "| --- | --- | --- | --- |"]
    lines += [f"| {key} | {g['status']} | {g.get('mean', '')} | {g.get('worst_repeat', '')} |"
              for key, g in gates.items()]
    lines += ["", "Blockers:", ""] + ["- " + b for b in result["blockers"]]
    (out / "report.md").write_bytes(("\n".join(lines) + "\n").encode())
    return {"status": result["status"], "blockers": result["blockers"], "remaining_gaps": gaps}


def export_csv(path, rows):
    if not rows:
        Path(path).write_bytes(b"")
        return
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    Path(path).write_bytes(buffer.getvalue().encode())
