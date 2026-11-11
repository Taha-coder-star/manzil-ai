"""Temporary, reversible effort trial on the already verified Day13 image.

Only Compose override files under the trial directory are written. .env, applied
SQL, application bytes, roles and inventory are never edited by this operator.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import zlib
from datetime import UTC, datetime
from pathlib import Path

IMAGE = "sha256:976dff195d20f6f9adbf593cccccbef12b31184ffb7be9fd51bef07ac08b7b8a"
TAG = "manzil-ai:day13-tuning-7eefb9c"
LOW = "34d4b870796b7684567fb78682ca49930e24971dbad573359ea1bd4a08381d60"
MINIMAL = "c014e9e0af227d55fab199ff1cf2f45b6459c93f8da857972a55f0f43158be7a"
LOW_PLAN = "6fb6b83f04f925012fb6692e095de83da38f4b793ffac12adc9d29686c985f7d"
PLAN_JSON = '{\n  "actor_suffix": "2614",\n  "baseline_configuration": "low",\n  "baseline_results_preserved": true,\n  "between_trials_seconds": 60,\n  "clear_command": "clear all",\n  "cohort": "day13_minimal_effort_comparison",\n  "comparison_first": [\n    "T01",\n    "T02"\n  ],\n  "config_sha256": "c014e9e0af227d55fab199ff1cf2f45b6459c93f8da857972a55f0f43158be7a",\n  "continue_remaining_cases_after_comparison_review": true,\n  "effort": "minimal",\n  "failed_or_missing": "infinity_no_replacement",\n  "formal_full_benchmark_replaced": false,\n  "full_cost_cache_gaps_waived": false,\n  "image": "sha256:976dff195d20f6f9adbf593cccccbef12b31184ffb7be9fd51bef07ac08b7b8a",\n  "original_low_window": "2026-11-11T18:51:55.834069+00:00",\n  "owner_approved": true,\n  "owner_direction": "Try the minimal-effort comparison",\n  "percentile_method": "nearest_rank",\n  "samples": [\n    {\n      "clear_first": true,\n      "id": "T01",\n      "message": "office on rent in Karachi, around 5 lakh per month",\n      "modality": "text"\n    },\n    {\n      "clear_first": false,\n      "id": "T02",\n      "message": "DHA Phase 6 mein 500 gaz ka ghar, budget aath crore tak",\n      "modality": "text"\n    },\n    {\n      "clear_first": false,\n      "id": "T03",\n      "message": "Property 2 ki aur pictures",\n      "modality": "text"\n    },\n    {\n      "clear_first": false,\n      "id": "T04",\n      "message": "Kuch sasta dikhao",\n      "modality": "text"\n    },\n    {\n      "clear_first": false,\n      "id": "T05",\n      "message": "Phase 8 mein bhi dekho",\n      "modality": "text"\n    },\n    {\n      "clear_first": true,\n      "id": "T06",\n      "message": "Lahore mein ghar chahiye",\n      "modality": "text"\n    },\n    {\n      "clear_first": true,\n      "id": "T07",\n      "message": "p6 mein ghar chahiye",\n      "modality": "text"\n    },\n    {\n      "clear_first": false,\n      "id": "T08",\n      "message": "DH6-105 dikhao",\n      "modality": "text"\n    },\n    {\n      "clear_first": false,\n      "id": "T09",\n      "message": "DH6-103 dikhao",\n      "modality": "text"\n    },\n    {\n      "clear_first": true,\n      "id": "T10",\n      "message": "ghar chahiye",\n      "modality": "text"\n    },\n    {\n      "clear_first": true,\n      "id": "V01",\n      "message": "DHA Phase 6 mein 500 gaz ka ghar khareedna hai, budget 8 crore tak.",\n      "modality": "voice"\n    },\n    {\n      "clear_first": false,\n      "id": "V02",\n      "message": "Kuch sasta dikhao.",\n      "modality": "voice"\n    },\n    {\n      "clear_first": false,\n      "id": "V03",\n      "message": "I want to buy a 500 square yard house in DHA Phase 6, budget up to 8 crore.",\n      "modality": "voice"\n    },\n    {\n      "clear_first": false,\n      "id": "V04",\n      "message": "Sirf Defence Phase 8 mein ghar dikhao, budget 8 crore tak.",\n      "modality": "voice"\n    },\n    {\n      "clear_first": true,\n      "id": "V05",\n      "message": "I need an office on rent in Karachi, around 5 lakh per month.",\n      "modality": "voice"\n    },\n    {\n      "clear_first": false,\n      "id": "V06",\n      "message": "Budget 6 lakh mahina tak kar do.",\n      "modality": "voice"\n    }\n  ],\n  "schema": 1,\n  "scope": "day13_small_timing_10text_6voice",\n  "targets_ms": {\n    "text_p50": 4000,\n    "text_p90": 7000,\n    "voice_p50": 7000,\n    "voice_p90": 10000\n  },\n  "timing_boundary": "earliest inbound server receipt to first substantive Meta acceptance"\n}\n'
PLAN_SHA = "4bf375ec9e4c47b7b4e918606d04c3fbb3377fff15a94aba8b2826b90bbdf748"
PREFLIGHT = zlib.decompress(base64.b64decode("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"))
KEYS = ("AI_PROVIDER", "LLM_PRIMARY_MODEL", "LLM_FAILOVER_MODEL", "LLM_THINKING_LEVEL", "STT_PROVIDER", "STT_FALLBACK")
IMAGE_ENV_KEYS_ALLOWED_TO_CHANGE = {"LLM_THINKING_LEVEL"}
APP = Path("/home/ubuntu/manzil-ai")
DIRECTORY = Path("/home/ubuntu/day13-closeout/effort-trial-7eefb9c")


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def config_hash(env):
    settings = {key: env.get(key) for key in KEYS}
    settings["LLM_THINKING_LEVEL_EFFECTIVE"] = env.get("LLM_THINKING_LEVEL", "low")
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def env_dict(values):
    return dict(entry.split("=", 1) for entry in values if "=" in entry)


def install_once(path, data):
    if path.is_symlink():
        raise RuntimeError("unsafe_trial_path")
    if path.exists():
        if path.read_bytes() != data:
            raise RuntimeError("existing_trial_file_differs")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


class Operator:
    def __init__(self, directory=DIRECTORY, app=APP):
        self.directory, self.app = directory, app
        self.parent = directory.parent
        self.session = directory / "minimal-session.json"
        self.receipt = directory / "trial-status.json"
        self.image_env = None
        self.compose_configs = {}

    def command(self, args, *, data=None, timeout=30):
        result = subprocess.run(args, cwd=self.app, input=data, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError("command_failed_" + args[0])
        return result.stdout + result.stderr if args[:2] == ["docker", "logs"] else result.stdout

    def compose(self, mode):
        return ["docker", "compose", "--project-name", "deploy", "-f", str(self.app / "deploy/compose.yml"),
                "-f", str(self.directory / (mode + ".override.json")), "--env-file", str(self.app / ".env")]

    def install_files(self):
        if self.directory.is_symlink() or self.parent.is_symlink():
            raise RuntimeError("unsafe_trial_directory")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for mode in ("minimal", "restore-low"):
            services = {}
            for service in ("web", "worker"):
                settings = {"image": TAG, "pull_policy": "never"}
                if mode == "minimal":
                    settings["environment"] = {"LLM_THINKING_LEVEL": "minimal"}
                services[service] = settings
            install_once(self.directory / (mode + ".override.json"), encoded({"services": services}))
        if hashlib.sha256(PLAN_JSON.encode()).hexdigest() != PLAN_SHA:
            raise RuntimeError("plan_checksum")
        install_once(self.directory / "timing-plan.approved.json", PLAN_JSON.encode())

    def state(self):
        rows = json.loads(self.command(["docker", "inspect", "deploy-web-1", "deploy-worker-1"]))
        if len(rows) != 2:
            raise RuntimeError("expected_two_services")
        for row in rows:
            if row["Image"] != IMAGE:
                raise RuntimeError("runtime_image_differs")
            labels = row["Config"].get("Labels") or {}
            if labels.get("com.docker.compose.project") != "deploy":
                raise RuntimeError("compose_project_differs")
            if row.get("Mounts") or row.get("HostConfig", {}).get("PortBindings"):
                raise RuntimeError("unexpected_web_worker_mounts_or_ports")
            env = env_dict(row["Config"].get("Env", []))
            if config_hash(env) not in (LOW, MINIMAL):
                raise RuntimeError("runtime_configuration_differs")
            if any(env.get(key) for key in ("DAY13_BUDGET_POLICY", "DAY13_BUDGET_LEDGER", "DAY13_BUDGET_PHASE")):
                raise RuntimeError("active_budget_override_must_be_preserved")
        return rows

    def validate_compose(self, current):
        image = json.loads(self.command(["docker", "image", "inspect", TAG]))
        if len(image) != 1 or image[0]["Id"] != IMAGE:
            raise RuntimeError("pinned_image_tag_differs")
        self.image_env = env_dict(image[0]["Config"].get("Env", []))
        for mode in ("restore-low", "minimal"):
            self.compose_configs[mode] = json.loads(self.command(self.compose(mode) + ["config", "--format", "json"]))
        low = self.compose_configs["restore-low"]
        minimal = self.compose_configs["minimal"]
        for service in ("web", "worker"):
            a, b = low["services"][service], minimal["services"][service]
            if a.get("image") != TAG or b.get("image") != TAG or a.get("pull_policy") != "never" or b.get("pull_policy") != "never":
                raise RuntimeError("compose_image_policy_differs")
            if a["environment"].get("LLM_THINKING_LEVEL") != "low" or b["environment"].get("LLM_THINKING_LEVEL") != "minimal":
                raise RuntimeError("base_env_is_not_low")
            a_other = {k: v for k, v in a.items() if k != "environment"}
            b_other = {k: v for k, v in b.items() if k != "environment"}
            if a_other != b_other or {k: v for k, v in a["environment"].items() if k not in IMAGE_ENV_KEYS_ALLOWED_TO_CHANGE} != {
                    k: v for k, v in b["environment"].items() if k not in IMAGE_ENV_KEYS_ALLOWED_TO_CHANGE}:
                raise RuntimeError("compose_changes_more_than_effort")
        for row in current:
            service = (row["Config"].get("Labels") or {}).get("com.docker.compose.service")
            if service not in ("web", "worker"):
                raise RuntimeError("unexpected_service")
            env = env_dict(row["Config"].get("Env", []))
            mode = "minimal" if config_hash(env) == MINIMAL else "restore-low"
            expected = {**self.image_env, **self.compose_configs[mode]["services"][service]["environment"]}
            if env != expected:
                raise RuntimeError("runtime_env_not_fully_preserved_by_compose")
            if row["Config"].get("Cmd") != self.compose_configs[mode]["services"][service].get("command"):
                raise RuntimeError("runtime_command_differs")

    def readiness(self):
        # This source sets READ ONLY and uses the restricted runtime role. A second
        # tenant transaction checks global demo work, so recreation starts quiet.
        script = ("import json,os,psycopg\nfrom app.db import tenant_transaction\n"
                  "ns={'__name__':'effort_readiness'}\nexec(" + repr(PREFLIGHT) + ",ns)\n"
                  "with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True,connect_timeout=10) as conn:\n"
                  " result=ns['inspect'](conn)\n"
                  " with tenant_transaction(conn,ns['AGENCY']):\n"
                  "  conn.execute(\"SET LOCAL statement_timeout='5s'\")\n"
                  "  work=conn.execute(\"SELECT count(*) FROM job WHERE agency_id=%s AND status IN ('pending','running') AND job_type NOT IN ('handoff_release','handoff_accept_timeout')\",(ns['AGENCY'],)).fetchone()[0]\n"
                  "  opened=conn.execute(\"SELECT count(*) FROM conversation_turn WHERE agency_id=%s AND status='open'\",(ns['AGENCY'],)).fetchone()[0]\n"
                  " result['global_processing_jobs']=work\n result['global_open_turns']=opened\n"
                  " result['ready']=result['ready'] and work==0 and opened==0\n"
                  " print(json.dumps(result))\n")
        result = json.loads(self.command(["docker", "exec", "-i", "deploy-worker-1", "python", "-"],
                                         data=script.encode(), timeout=45))
        if result.get("ready") is not True:
            raise RuntimeError("demo_actor_or_jobs_not_ready")
        return result

    def health(self, require_startup=False):
        for attempt in range(12):
            try:
                rows = self.state()
                if not all(r["State"].get("Running") is True and not r["State"].get("Restarting") for r in rows):
                    raise RuntimeError("services_not_running")
                with urllib.request.urlopen("https://98-85-18-90.sslip.io/health", timeout=5) as response:
                    health = json.load(response)
                if health.get("status") != "ok" or any(health.get("checks", {}).get(k) != "ok" for k in ("database", "bucket")):
                    raise RuntimeError("health_not_ready")
                if require_startup:
                    started = next(r["State"]["StartedAt"] for r in rows if r["Name"] == "/deploy-worker-1")
                    logs = self.command(["docker", "logs", "--since", started, "--tail", "200", "deploy-worker-1"])
                    # Docker may send logs to stderr; command() deliberately returns
                    # both streams only for docker logs, keeping them private.
                    if b"Worker started" not in logs:
                        raise RuntimeError("worker_startup_not_observed")
                return rows
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                if attempt == 11:
                    raise RuntimeError("health_or_worker_startup_failed") from None
                time.sleep(2)

    def save_status(self, phase, **extra):
        data = {"schema": 1, "phase": phase, "image": IMAGE, "baseline_config_sha256": LOW,
                "candidate_config_sha256": MINIMAL, "plan_sha256": PLAN_SHA,
                "operator_provider_calls": 0, "operator_database_writes": 0,
                "env_file_edited": False, "recorded_at": datetime.now(UTC).isoformat(), **extra}
        temporary = self.directory / ".trial-status.next.json"
        if temporary.is_symlink() or self.receipt.is_symlink():
            raise RuntimeError("unsafe_receipt_path")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(data))
        os.replace(temporary, self.receipt)
        return data

    def recreate(self, mode):
        self.command(self.compose(mode) + ["up", "-d", "--no-deps", "--no-build", "--force-recreate", "web", "worker"], timeout=90)

    def verified_mode(self, mode, require_startup=False):
        rows = self.health(require_startup)
        want = MINIMAL if mode == "minimal" else LOW
        if any(config_hash(env_dict(r["Config"].get("Env", []))) != want for r in rows):
            raise RuntimeError("effective_effort_did_not_match")
        self.validate_compose(rows)
        return self.readiness()

    def run(self, action):
        if action not in ("minimal", "restore-low"):
            raise RuntimeError("unknown_action")
        self.install_files()
        baseline_path = self.parent / "timing-active.json"
        if baseline_path.is_symlink():
            raise RuntimeError("unsafe_baseline_path")
        baseline = json.loads(baseline_path.read_bytes())
        if (baseline.get("image"), baseline.get("config_sha256"), baseline.get("plan_sha256")) != (IMAGE, LOW, LOW_PLAN):
            raise RuntimeError("original_low_baseline_differs")
        current = self.state()
        self.validate_compose(current)
        modes = {config_hash(env_dict(r["Config"].get("Env", []))) for r in current}
        if action == "minimal" and modes == {MINIMAL}:
            if not self.session.exists() or self.session.is_symlink():
                raise RuntimeError("minimal_running_without_preserved_session")
            session = json.loads(self.session.read_bytes())
            if session.get("plan_sha256") != PLAN_SHA:
                raise RuntimeError("existing_trial_plan_differs")
            self.verified_mode("minimal")
            self.save_status("minimal_active", window_start_utc=session["window_start_utc"], existing_anchor_retained=True)
            return {**session, "existing_anchor_retained": True}
        if action == "minimal" and self.session.exists():
            raise RuntimeError("completed_or_restored_trial_exists_do_not_restart_cohort")
        if action == "minimal":
            self.health()
            self.readiness()
        if action == "restore-low" and modes == {LOW}:
            self.verified_mode("restore-low")
            return self.save_status("low_restored", already_low=True)
        self.save_status("switching_to_" + action, original_low_window=baseline["window_start_utc"])
        try:
            self.recreate(action)
            ready = self.verified_mode(action, require_startup=True)
            if action == "minimal":
                session = {"schema": 1, "cohort": "day13_minimal_effort_comparison", "actor_suffix": "2614",
                    "image": IMAGE, "config_sha256": MINIMAL, "thinking_level": "minimal", "plan_sha256": PLAN_SHA,
                    "window_start_utc": ready["window_start_utc"], "original_low_window": baseline["window_start_utc"],
                    "https_health": "ok", "readiness": ready, "env_file_edited": False,
                    "operator_provider_calls": 0, "operator_database_writes": 0}
                install_once(self.session, encoded(session))
                self.save_status("minimal_active", window_start_utc=session["window_start_utc"])
                return session
            return self.save_status("low_restored", existing_trial_evidence_preserved=True)
        except Exception:
            # A partial recreation or failed post-check restores the known low
            # settings with this SAME image. Never call the old-image rollback.
            try:
                self.recreate("restore-low")
                self.verified_mode("restore-low", require_startup=True)
                self.save_status("switch_failed_low_restored", original_low_window=baseline["window_start_utc"])
            except Exception:
                self.save_status("restore_low_needs_operator_attention")
                raise RuntimeError("effort_switch_and_restore_failed_return_output") from None
            raise RuntimeError("effort_switch_failed_low_restored") from None


def main():
    import fcntl
    if os.geteuid() != 0:
        print("STOP: run effort operator with sudo", file=sys.stderr)
        return 2
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    operator = Operator()
    try:
        operator.install_files()
        lock_path = DIRECTORY / ".operator.lock"
        if lock_path.is_symlink():
            raise RuntimeError("unsafe_lock_path")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = operator.run(action)
        print(json.dumps(result, indent=2))
        print("DAY13-MINIMAL-TRIAL-READY" if action == "minimal" else "DAY13-LOW-EFFORT-RESTORED")
        return 0
    except Exception as exc:
        # Fixed operator codes only. Never show compose errors/config/env/logs.
        safe = str(exc) if type(exc) is RuntimeError and __import__("re").fullmatch(r"[a-z0-9_]+", str(exc)) else type(exc).__name__
        print("STOP: " + safe + "; raw errors/configuration/logs suppressed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
