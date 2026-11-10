"""Read-only host preflight. Prints only explicitly non-secret Docker settings.

python3 day13_preflight.py > day13-runtime.json
No provider requests, DB mutations, AWS calls, container restarts or env dumps.
"""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime

KEYS = ("AI_PROVIDER", "LLM_PRIMARY_MODEL", "LLM_FAILOVER_MODEL", "LLM_THINKING_LEVEL",
        "STT_PROVIDER", "STT_FALLBACK")


def inspect_runtime(containers):
    result = []
    mounts = []
    for container in containers:
        env = dict(entry.split("=", 1) for entry in container["Config"].get("Env", []) if "=" in entry)
        settings = {key: env.get(key) for key in KEYS}
        # None/off is omission, not evidence of disabled provider thinking.
        settings["LLM_THINKING_LEVEL_EFFECTIVE"] = env.get("LLM_THINKING_LEVEL", "low")
        serialized = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
        result.append({"service": container["Name"].lstrip("/"), "image": container["Image"],
            "running": container["State"].get("Running") is True,
            "config": settings, "config_sha256": hashlib.sha256(serialized).hexdigest(),
            "budget_enabled": bool(env.get("DAY13_BUDGET_POLICY")),
            "budget_phase": env.get("DAY13_BUDGET_PHASE")})
        mounts.append(next((m.get("Source") for m in container.get("Mounts", [])
                            if m.get("Destination") == "/day13" and m.get("RW") is True), None))
    return {"schema": 1, "captured_at": datetime.now(UTC).isoformat(), "services": result,
            "same_image_and_config": len({(r["image"], r["config_sha256"]) for r in result}) == 1,
            "shared_writable_budget_mount": bool(mounts and all(mounts) and len(set(mounts)) == 1),
            "paid_calls": 0, "changes": 0}


def main():
    try:
        process = subprocess.run(["docker", "inspect", "deploy-web-1", "deploy-worker-1"],
                                 capture_output=True, check=True, timeout=20)
        result = inspect_runtime(json.loads(process.stdout))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("Day13 preflight failed; no settings printed", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
