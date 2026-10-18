"""docs/release/fresh_search_switch.sh against stubbed docker / curl: a failed precondition stops
before anything changes; a failure after the switch rolls back by the recorded image ID (not the
tag); an unverifiable rollback asks for manual intervention. POSIX only (the stubs are bash)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "docs" / "release" / "fresh_search_switch.sh"
pytestmark = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                                reason="needs bash (runs in the Linux test image)")

PRE, NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64

DOCKER = r"""#!/usr/bin/env bash
S=$STATE
key() { echo "$1" | tr '/:' '__'; }
resolve() { if [[ $1 == sha256:* ]]; then echo "$1"; else cat "$S/tags/$(key "$1")" 2>/dev/null; fi; }
case "$1" in
  inspect) cat "$S/containers/$2" ;;
  image)
    id=$(resolve "$3") || exit 1
    [[ -n $id ]] || exit 1
    [[ -n ${PRE_GONE:-} && $3 == "$PRE_ID" ]] && exit 1
    [[ ${4:-} == --format ]] && echo "$id"; exit 0 ;;
  tag)
    id=$(resolve "$2"); [[ -n $id ]] || exit 1
    if [[ -n ${BREAK_PRE_FRESH:-} && $3 == manzil-ai:pre-fresh ]]; then id=sha256:garbage; fi
    echo "$id" > "$S/tags/$(key "$3")" ;;
  compose)
    shift; while [[ $1 == -f || $1 == --env-file ]]; do shift 2; done
    case "$1" in
      up) [[ -z ${FAIL_UP:-} || $(cat "$S/tags/manzil-ai_latest") != "$NEW_ID" ]] || exit 1
          [[ -z ${ALWAYS_FAIL_UP:-} ]] || exit 1
          [[ -n ${UP_STICK:-} ]] && exit 0
          cat "$S/tags/manzil-ai_latest" | tee "$S/containers/deploy-web-1" > "$S/containers/deploy-worker-1" ;;
      exec) cat > /dev/null; [[ -z ${FAIL_HASH:-} ]] || exit 1 ;;
      logs) [[ -n ${NO_WORKER:-} ]] || echo "Worker started: x" ;;
    esac ;;
esac
"""
CURL = r"""#!/usr/bin/env bash
on_new() { [[ $(cat "$STATE/containers/deploy-web-1") == "$NEW_ID" ]]; }
[[ -z ${ALWAYS_HEALTH_FAIL:-} ]] || exit 22
if [[ -n ${HEALTH_FAIL:-} ]] && on_new; then exit 22; fi
if [[ " $* " == *" -w "* ]]; then
  if [[ -n ${WEBHOOK_CODE:-} ]] && on_new; then echo -n "$WEBHOOK_CODE"; else echo -n 401; fi
fi
"""


CRLF, LF = bytes([13, 10]), bytes([10])


def script_copy(tmp_path):
    """The script with LF endings: a Windows checkout (core.autocrlf) may hold CRLF, which bash
    rejects; the committed blob, and so the file the owner runs, is LF."""
    copy = tmp_path / "fresh_search_switch.sh"
    copy.write_bytes(SCRIPT.read_bytes().replace(CRLF, LF))
    return copy


def setup(tmp_path, *, latest=PRE, web=PRE, worker=PRE):
    state = tmp_path / "state"
    for d in ("tags", "containers"):
        (state / d).mkdir(parents=True)
    (state / "tags" / "manzil-ai_latest").write_text(latest)
    (state / "tags" / "manzil-ai_fresh-abc1234").write_text(NEW)
    (state / "containers" / "deploy-web-1").write_text(web)
    (state / "containers" / "deploy-worker-1").write_text(worker)
    rel, app = tmp_path / "rel", tmp_path / "app"
    (app / "deploy").mkdir(parents=True)
    rel.mkdir()
    (rel / "PRE_ID").write_text(PRE)
    (rel / "SHA256SUMS").write_text("x  app/x.py\n")
    for name, body in (("docker", DOCKER), ("curl", CURL)):
        (tmp_path / name).write_text(body)
        (tmp_path / name).chmod(0o755)
    return state, rel, app


def run(tmp_path, *args, **env):
    state, rel, app = setup(tmp_path, **{k: env.pop(k) for k in ("latest", "web", "worker")
                                         if k in env})
    full = {**os.environ, "STATE": str(state), "REL": str(rel), "APP": str(app),
            "DOCKER": str(tmp_path / "docker"), "CURL": str(tmp_path / "curl"), "SLEEP": "0",
            "SITE": "example.test", "PRE_ID": PRE, "NEW_ID": NEW, **env}
    done = subprocess.run(["bash", str(script_copy(tmp_path)), *args], env=full,
                          capture_output=True, text=True, timeout=60, check=False)
    read = lambda p: (state / p).read_text().strip()
    return done, read


def test_a_clean_switch_puts_both_services_on_the_release_image(tmp_path):
    done, read = run(tmp_path, "switch", "abc1234")
    assert done.returncode == 0, done.stderr
    assert read("containers/deploy-web-1") == read("containers/deploy-worker-1") == NEW
    assert read("tags/manzil-ai_latest") == NEW and read("tags/manzil-ai_pre-fresh") == PRE


@pytest.mark.parametrize("override", [
    {"latest": NEW},                                    # the tag is not the recorded image
    {"web": NEW},                                       # a service is not on the recorded image
    {"worker": "sha256:" + "c" * 64},
])
def test_a_failed_precondition_stops_before_anything_changes(tmp_path, override):
    done, read = run(tmp_path, "switch", "abc1234", **override)
    assert done.returncode == 1 and "STOP" in done.stderr
    assert not (tmp_path / "state" / "tags" / "manzil-ai_pre-fresh").exists()
    assert read("tags/manzil-ai_latest") == override.get("latest", PRE)


def test_a_missing_release_image_or_record_stops(tmp_path):
    done, _ = run(tmp_path, "switch", "nope999")
    assert done.returncode == 1 and "does not exist" in done.stderr
    (tmp_path / "rel" / "PRE_ID").unlink()
    done = subprocess.run(["bash", str(script_copy(tmp_path)), "switch", "abc1234"],
                          capture_output=True, text=True, check=False,
                          env={**os.environ, "REL": str(tmp_path / "rel"),
                               "APP": str(tmp_path / "app"), "SITE": "x"})
    assert done.returncode == 1 and "PRE_ID" in done.stderr


@pytest.mark.parametrize("failure", [
    {"FAIL_UP": "1"}, {"FAIL_HASH": "1"}, {"HEALTH_FAIL": "1"}, {"WEBHOOK_CODE": "200"},
    {"NO_WORKER": "1"}, {"UP_STICK": "1"},
])
def test_a_failure_after_the_switch_rolls_back_by_the_recorded_id(tmp_path, failure):
    done, read = run(tmp_path, "switch", "abc1234", BREAK_PRE_FRESH="1", **failure)
    assert done.returncode == 1, done.stderr
    assert "ROLLING BACK" in done.stderr
    assert read("tags/manzil-ai_latest") == PRE                      # by id, though pre-fresh is garbage
    assert read("containers/deploy-web-1") == read("containers/deploy-worker-1") == PRE or \
        failure == {"UP_STICK": "1"}


@pytest.mark.parametrize("failure", [
    {"FAIL_UP": "1", "PRE_GONE": "1"},                 # the recorded image has been removed
    {"FAIL_UP": "1", "ALWAYS_FAIL_UP": "1"},           # the services cannot be recreated at all
    {"HEALTH_FAIL": "1", "ALWAYS_HEALTH_FAIL": "1"},   # the site does not answer even after
])
def test_a_rollback_that_cannot_be_verified_asks_for_intervention(tmp_path, failure):
    done, _ = run(tmp_path, "switch", "abc1234", **failure)
    assert done.returncode == 2 and "MANUAL INTERVENTION" in done.stderr


def test_the_rollback_command_uses_the_recorded_id_not_the_tag(tmp_path):
    done, read = run(tmp_path, "rollback", latest=NEW, web=NEW, worker=NEW)
    assert done.returncode == 0, done.stderr
    assert read("tags/manzil-ai_latest") == PRE
    assert read("containers/deploy-web-1") == read("containers/deploy-worker-1") == PRE


def test_bad_usage_stops(tmp_path):
    done, _ = run(tmp_path, "deploy")
    assert done.returncode == 1 and "usage" in done.stderr


# --- the baseline gate -------------------------------------------------------------------------

BASELINE = SCRIPT.with_name("fresh_search_baseline.sh")
IMAGE_CHECK = SCRIPT.with_name("fresh_search_image_check.sh")
WEB = "sha256:" + "d" * 64
KNOWN_TE = "7ab72a7b0916b33cb0f049dd95dc6718833ac3f2f7c12e274f5d7764f54e1d00"
KNOWN_BENCH = "0b6c65b199e94816a4e36d75681216b8acdc85c909e226d16feace2c75d6c30a"

# A stub of the docker commands the gate uses. `docker exec` without -i gives the command an empty
# stdin, as the real one does, so a missing -i makes the stdin-fed checksum checks fail here too.
BASELINE_DOCKER = r"""#!/usr/bin/env bash
S=$STATE
case "$1" in
  inspect) cat "$S/containers/$2" ;;
  diff) cat "$S/diff_$2" 2>/dev/null; exit 0 ;;
  exec)
    shift; interactive=""
    while [[ ${1:-} == -* ]]; do [[ $1 == -i ]] && interactive=1; shift; done
    cmd=${4:-}
    case "$cmd" in
      *"sha256sum -c -"*)
          in=""; [[ -n $interactive ]] && in=$(cat)
          [[ -n $in ]] || { echo "sha256sum: standard input: no properly formatted checksum lines found"; exit 1; }
          [[ $in == u* && -n ${BAD_UNCHANGED:-} ]] && { echo "app/a.py: FAILED"; exit 1; }
          [[ $in == b* && -n ${BAD_BASE:-} ]] && { echo "app/b.py: FAILED"; exit 1; }
          [[ $in == d* && -n ${BAD_DATA:-} ]] && { echo "demo/x.csv: FAILED"; exit 1; }
          exit 0 ;;
      *"if [ -e"*) f="$S/states/$(echo "$6" | tr '/' '_')"; if [[ -e $f ]]; then cat "$f"; else echo ABSENT; fi ;;
      *"find app"*) cat "$S/listing" ;;
    esac ;;
  run) [[ -z ${BAD_IMAGE:-} ]] ;;
esac
"""


def baseline(tmp_path, *, script=None, **env):
    state = tmp_path / "state"
    (state / "containers").mkdir(parents=True)
    (state / "states").mkdir()
    rel = tmp_path / "rel"
    rel.mkdir()
    (state / "containers" / "deploy-web-1").write_text(env.pop("web", WEB))
    (state / "containers" / "deploy-worker-1").write_text(env.pop("worker", WEB))
    (state / "listing").write_text(env.pop("listing", "app/a.py\napp/b.py\napp/benchmark.py"))
    for path, value in {"app/turn_engine.py": KNOWN_TE, "app/benchmark.py": KNOWN_BENCH,
                        **env.pop("states", {})}.items():
        (state / "states" / path.replace("/", "_")).write_text(value)
    if "diff" in env:
        (state / "diff_deploy-web-1").write_text(env.pop("diff"))
    (rel / "SHA256SUMS.unchanged").write_text("u  app/a.py\n")
    (rel / "SHA256SUMS.base").write_text("b  app/b.py\n")
    (rel / "SHA256SUMS.data").write_text("d  demo/x.csv\n")
    (rel / "FILES.expected").write_text("app/a.py\napp/b.py\napp/bench_live.py\napp/benchmark.py\n")
    (rel / "STATES.allowed").write_text(
        f"app/turn_engine.py {'0' * 64}\napp/turn_engine.py {KNOWN_TE}\n"
        f"app/benchmark.py {KNOWN_BENCH}\napp/bench_live.py ABSENT\n")
    if env.pop("no_manifest", False):
        (rel / "FILES.expected").unlink()
    (tmp_path / "docker").write_text(BASELINE_DOCKER)
    (tmp_path / "docker").chmod(0o755)
    copy = tmp_path / "baseline.sh"
    text = BASELINE.read_bytes().replace(CRLF, LF)
    copy.write_bytes(script(text) if script else text)
    full = {**os.environ, "STATE": str(state), "REL": str(rel), "DOCKER": str(tmp_path / "docker"),
            **env}
    done = subprocess.run(["bash", str(copy)], env=full, capture_output=True, text=True,
                          timeout=60, check=False)
    return done, rel / "PRE_ID"


def test_a_matching_baseline_records_the_image_id(tmp_path):
    done, pre = baseline(tmp_path)
    assert done.returncode == 0, done.stderr
    assert pre.read_text() == WEB


def test_forgetting_docker_exec_i_is_caught_by_the_stub(tmp_path):
    """The reported bug: without -i the stdin-fed manifest checks read nothing."""
    done, pre = baseline(tmp_path, script=lambda t: t.replace(b"exec -i ", b"exec "))
    assert done.returncode == 1 and "no properly formatted" in done.stderr
    assert not pre.exists()


@pytest.mark.parametrize("env, expect", [
    ({"worker": "sha256:" + "e" * 64}, "not on one image"),
    ({"diff": "C /app/app\nC /app/app/render.py\n"}, "changes under /app"),
    ({"BAD_UNCHANGED": "1"}, "SHA256SUMS.unchanged"),
    ({"BAD_BASE": "1"}, "SHA256SUMS.base"),
    ({"BAD_DATA": "1"}, "SHA256SUMS.data"),
    ({"listing": "app/a.py\napp/b.py\napp/benchmark.py\napp/extra.py"}, "extra file app/extra.py"),
    ({"listing": "app/a.py\napp/benchmark.py"}, "missing file app/b.py"),
    ({"states": {"app/turn_engine.py": "f" * 64}}, "app/turn_engine.py is in state " + "f" * 64),
    ({"states": {"app/benchmark.py": "9" * 64}}, "app/benchmark.py is in state " + "9" * 64),
    ({"states": {"app/bench_live.py": "8" * 64}}, "app/bench_live.py is in state " + "8" * 64),
    ({"BAD_IMAGE": "1"}, "does not match the expected build"),
    ({"no_manifest": True}, "is missing"),
])
def test_any_baseline_mismatch_stops_and_records_nothing(tmp_path, env, expect):
    done, pre = baseline(tmp_path, **env)
    assert done.returncode == 1 and "STOP" in done.stderr
    assert expect in done.stderr, done.stderr
    assert not pre.exists()


def test_the_gate_reports_every_mismatch_in_one_run(tmp_path):
    """Three different problems on the server: one run lists them all, with the hashes."""
    done, pre = baseline(tmp_path, BAD_UNCHANGED="1", BAD_BASE="1",
                         states={"app/benchmark.py": "9" * 64,
                                 "app/bench_live.py": "8" * 64})
    assert done.returncode == 1 and "8 mismatch(es)" in done.stderr   # 4 per container
    for expect in ("SHA256SUMS.unchanged", "SHA256SUMS.base", "9" * 64, "8" * 64):
        assert expect in done.stderr
    assert not pre.exists()


def test_a_benchmark_tool_absent_from_production_is_an_explained_state(tmp_path):
    done, _ = baseline(tmp_path)          # bench_live.py is ABSENT in the stub, allowed
    assert done.returncode == 0, done.stderr
    done, _ = baseline(tmp_path / "again", listing="app/a.py\napp/b.py\napp/benchmark.py\napp/bench_live.py",
                       states={"app/bench_live.py": "8" * 64})
    assert done.returncode == 1 and "bench_live.py is in state " + "8" * 64 in done.stderr


def test_pycache_changes_alone_are_not_an_in_place_edit(tmp_path):
    done, pre = baseline(tmp_path, diff="A /app/app/__pycache__/x.pyc\nC /tmp\n")
    assert done.returncode == 0, done.stderr
    assert pre.exists()


# --- the image check ---------------------------------------------------------------------------

IMAGE_DOCKER = r"""#!/usr/bin/env bash
S=$STATE
case "$1" in
  run)
    shift; while [[ $1 == --rm ]]; do shift; done
    if [[ $1 == --entrypoint ]]; then
      ep=$2; shift 2
      [[ $ep == id ]] && { echo "${RUN_USER:-appuser}"; exit 0; }
      [[ $ep == python ]] && { [[ -z ${BAD_IMPORT:-} ]]; exit; }
      # sh -c <cmd>: after -v <mount> <image> -c <cmd>
      cmd=${@: -1}
      case "$cmd" in
        *"sha256sum -c /rel/SHA256SUMS.unchanged"*) [[ -z ${BAD_UNCHANGED:-} ]] || { echo "app/a.py: FAILED"; exit 1; } ;;
        *"sha256sum -c /rel/SHA256SUMS.data"*) [[ -z ${BAD_DATA:-} ]] || { echo "demo/x.csv: FAILED"; exit 1; } ;;
        *"sha256sum -c /rel/SHA256SUMS"*) [[ -z ${BAD_RELEASE:-} ]] || { echo "app/render.py: FAILED"; exit 1; } ;;
        *"find app"*) cat "$S/listing" ;;
        *"sha256sum migrations"*) cat "$S/migrations" ;;
      esac
    fi ;;
esac
"""


def image_check(tmp_path, **env):
    state, rel = tmp_path / "state", tmp_path / "rel"
    state.mkdir()
    rel.mkdir()
    (state / "listing").write_text(env.pop("listing", "app/a.py\nmigrations/001_x.sql\n"))
    (state / "migrations").write_text(env.pop("migrations", f"{'1' * 64}  migrations/001_x.sql\n"))
    for name in ("SHA256SUMS", "SHA256SUMS.unchanged", "SHA256SUMS.data"):
        (rel / name).write_text("x  app/a.py\n")
    (rel / "FILES.release").write_text("app/a.py\nmigrations/001_x.sql\n")
    (rel / "STATES.allowed").write_text(f"migrations/001_x.sql {'1' * 64}\n")
    (tmp_path / "docker").write_text(IMAGE_DOCKER)
    (tmp_path / "docker").chmod(0o755)
    copy = tmp_path / "image_check.sh"
    copy.write_bytes(IMAGE_CHECK.read_bytes().replace(CRLF, LF))
    full = {**os.environ, "STATE": str(state), "REL": str(rel), "DOCKER": str(tmp_path / "docker"),
            **env}
    return subprocess.run(["bash", str(copy), "manzil-ai:fresh-abc"], env=full, capture_output=True,
                          text=True, timeout=60, check=False)


def test_the_image_check_passes_on_the_exact_tree(tmp_path):
    done = image_check(tmp_path)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("env, expect", [
    ({"BAD_RELEASE": "1"}, "SHA256SUMS:"),
    ({"BAD_UNCHANGED": "1"}, "SHA256SUMS.unchanged"),
    ({"BAD_DATA": "1"}, "SHA256SUMS.data"),
    ({"listing": "app/a.py\napp/zzz.py\nmigrations/001_x.sql\n"}, "extra file app/zzz.py"),
    ({"listing": "app/a.py\n"}, "missing file migrations/001_x.sql"),
    ({"migrations": f"{'2' * 64}  migrations/001_x.sql\n"}, "unexplained state " + "2" * 64),
    ({"RUN_USER": "root"}, "does not run as appuser"),
    ({"BAD_IMPORT": "1"}, "does not import"),
])
def test_the_image_check_stops_on_any_difference(tmp_path, env, expect):
    done = image_check(tmp_path, **env)
    assert done.returncode == 1 and expect in done.stderr, done.stderr
