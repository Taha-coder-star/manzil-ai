bash <<'DAY13_MIN_SEND_REVIEW_BASH'
set -euo pipefail
sudo python3 - <<'DAY13_MIN_SEND_REVIEW_PY'
import base64, hashlib, json, os, re, subprocess, sys, zlib
from datetime import UTC, datetime
from pathlib import Path

IMAGE = "sha256:976dff195d20f6f9adbf593cccccbef12b31184ffb7be9fd51bef07ac08b7b8a"
CONFIG = "c014e9e0af227d55fab199ff1cf2f45b6459c93f8da857972a55f0f43158be7a"
PLAN = "4bf375ec9e4c47b7b4e918606d04c3fbb3377fff15a94aba8b2826b90bbdf748"
SOURCE_SHA = "b8ba014615b0e47f45cc3ffe4e46bdb797f4d92d4b9f34a3f918353e698c3938"
SOURCE = zlib.decompress(base64.b64decode("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"))
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
    path = directory / ("minimal-send-review-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)
    print(data)
    print("DAY13-MINIMAL-SEND-REVIEW-READONLY-COMPLETE; provider_calls=0 database_writes=0")
except (OSError, ValueError, KeyError, TypeError, zlib.error, subprocess.SubprocessError):
    print("STOP: scoped diagnostic failed; no source/provider/service/database change; raw errors suppressed", file=sys.stderr)
    sys.exit(2)

DAY13_MIN_SEND_REVIEW_PY
DAY13_MIN_SEND_REVIEW_BASH
