bash <<'DAY13_MIN_DIAG_BASH'
set -euo pipefail
sudo python3 - <<'DAY13_MIN_DIAG_PY'
import base64, hashlib, json, os, re, subprocess, sys, zlib
from datetime import UTC, datetime
from pathlib import Path

IMAGE = "sha256:976dff195d20f6f9adbf593cccccbef12b31184ffb7be9fd51bef07ac08b7b8a"
CONFIG = "c014e9e0af227d55fab199ff1cf2f45b6459c93f8da857972a55f0f43158be7a"
PLAN = "4bf375ec9e4c47b7b4e918606d04c3fbb3377fff15a94aba8b2826b90bbdf748"
SOURCE_SHA = "2d2fda1a126e1f308d81b9f6b4bb9d1c5a0f895fb0006e294b976a8adacdb742"
SOURCE = zlib.decompress(base64.b64decode("""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"""))
KEYS = ("AI_PROVIDER", "LLM_PRIMARY_MODEL", "LLM_FAILOVER_MODEL", "LLM_THINKING_LEVEL", "STT_PROVIDER", "STT_FALLBACK")
try:
    if hashlib.sha256(SOURCE).hexdigest() != SOURCE_SHA:
        raise ValueError("diagnostic checksum")
    directory = Path("/home/ubuntu/day13-closeout")
    session_path = directory / "effort-trial-7eefb9c/minimal-session.json"
    if directory.is_symlink() or session_path.is_symlink():
        raise ValueError("session path")
    session = json.loads(session_path.read_bytes())
    if any(session.get(k) != v for k, v in (("image", IMAGE), ("config_sha256", CONFIG), ("plan_sha256", PLAN))):
        raise ValueError("saved timing session differs")
    anchor = session["window_start_utc"]
    process = subprocess.run(["docker", "inspect", "deploy-web-1", "deploy-worker-1"], capture_output=True, check=True, timeout=20)
    containers = json.loads(process.stdout)
    if len(containers) != 2:
        raise ValueError("service count")
    for c in containers:
        if c["Image"] != IMAGE or c["State"].get("Running") is not True:
            raise ValueError("runtime image/state differs")
        env = dict(x.split("=", 1) for x in c["Config"].get("Env", []) if "=" in x)
        settings = {k: env.get(k) for k in KEYS}
        settings["LLM_THINKING_LEVEL_EFFECTIVE"] = env.get("LLM_THINKING_LEVEL", "low")
        if hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != CONFIG:
            raise ValueError("runtime config differs")
    process = subprocess.run(["docker", "exec", "-i", "-e", "DAY13_TIMING_ANCHOR=" + anchor,
                              "deploy-worker-1", "python", "-"], input=SOURCE, capture_output=True, timeout=60)
    if process.returncode:
        error = process.stderr.decode(errors="replace").strip()
        if re.fullmatch(r"STOP: scoped timing diagnostic failed \([A-Za-z]+, SQLSTATE=[A-Za-z0-9]+\); raw errors suppressed", error):
            print(error, file=sys.stderr)
        raise ValueError("read-only child failed")
    report = json.loads(process.stdout)
    lookup = report.pop("private_turn_lookup")
    # Examine only fixed structured log formats for the privately selected turn IDs.
    # No entire log line, arbitrary message, tool arguments or original UUID escapes.
    logs = subprocess.run(["docker", "logs", "--timestamps", "--since", anchor, "--tail", "2000", "deploy-worker-1"],
                          capture_output=True, timeout=20)
    text = (logs.stdout + logs.stderr).decode(errors="replace")
    report["engine_summaries"] = []
    report["provider_attempt_timings"] = []
    ep = re.compile('AI turn ([0-9a-f-]{36}): (customer|staff|broker) ([a-z_]+)/([a-z0-9_.:!-]+), (\\d+) provider call\\(s\\), tools \\[([A-Za-z0-9_!, .:-]*)\\], (\\d+) ms$')
    lp = re.compile('LLM call turn ([0-9a-f-]{36}) #(\\d+) ([a-z0-9_:.-]+) ([a-z0-9.-]+): ([a-z_]+)/([a-z0-9_.:!-]+), (\\d+) ms, tokens in=(None|\\d+) cached=(None|\\d+) out=(None|\\d+) thinking=(None|\\d+)$')
    for line in text.splitlines():
        m = ep.search(line)
        if m and m[1] in lookup:
            report["engine_summaries"].append({"attempt": lookup[m[1]], "route": m[2], "outcome": m[3],
                "detail": m[4], "provider_calls": int(m[5]), "tools": [x.strip() for x in m[6].split(",") if x.strip()],
                "engine_elapsed_ms": int(m[7])})
        m = lp.search(line)
        if m and m[1] in lookup:
            report["provider_attempt_timings"].append({"attempt": lookup[m[1]], "iteration": int(m[2]),
                "branch": m[3], "model": m[4], "outcome": m[5], "detail": m[6], "provider_ms": int(m[7]),
                "input_tokens": None if m[8] == "None" else int(m[8]),
                "cached_input_tokens": None if m[9] == "None" else int(m[9]),
                "output_tokens": None if m[10] == "None" else int(m[10]),
                "thinking_tokens": None if m[11] == "None" else int(m[11])})
    report["log_capture_succeeded"] = logs.returncode == 0
    report["cohort"] = "minimal-effort"
    report["plan_sha256"] = PLAN
    report["source_sha256"] = SOURCE_SHA
    report["image"] = IMAGE
    report["config_sha256"] = CONFIG
    report["limitations"] = ["Do not replace T02 first attempt with its resend.",
        "Provider/engine log timings are stage timings, not handset delivery latency.",
        "No log match is unknown, not evidence that a provider call never happened.",
        "An answer_candidate still needs substantive-response review; failure and interim are explicitly distinguished."]
    data = json.dumps(report, indent=2, allow_nan=False)
    if re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", data, re.I):
        raise ValueError("raw UUID redaction check")
    path = directory / ("minimal-comparison-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)
    print(data)
    print("DAY13-MINIMAL-COMPARISON-READONLY-COMPLETE; provider_calls=0 database_writes=0")
except (OSError, ValueError, KeyError, TypeError, zlib.error, subprocess.SubprocessError):
    print("STOP: scoped diagnostic failed; no source/provider/service/database change; raw errors suppressed", file=sys.stderr)
    sys.exit(2)

DAY13_MIN_DIAG_PY
DAY13_MIN_DIAG_BASH
