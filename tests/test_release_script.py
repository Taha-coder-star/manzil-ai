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
WEB, WORKER_ID = "sha256:" + "d" * 64, "sha256:" + "d" * 64
KNOWN_TE = "7ab72a7b0916b33cb0f049dd95dc6718833ac3f2f7c12e274f5d7764f54e1d00"

BASELINE_DOCKER = r"""#!/usr/bin/env bash
S=$STATE
case "$1" in
  inspect) cat "$S/containers/$2" ;;
  diff) cat "$S/diff_$2" 2>/dev/null; exit 0 ;;
  exec)
    c=$2; cmd=${5:-}
    case "$cmd" in
      *"sha256sum -c -"*) in=$(cat)
          [[ $in == u* && -n ${BAD_UNCHANGED:-} ]] && exit 1
          [[ $in == b* && -n ${BAD_BASE:-} ]] && exit 1
          exit 0 ;;
      *"find app"*) cat "$S/listing" ;;
      *"sha256sum app/turn_engine.py"*) echo "${TE_HASH}  app/turn_engine.py" ;;
    esac ;;
  run) [[ -z ${BAD_IMAGE:-} ]] ;;
esac
"""


def baseline(tmp_path, **env):
    state = tmp_path / "state"
    (state / "containers").mkdir(parents=True)
    rel = tmp_path / "rel"
    rel.mkdir()
    (state / "containers" / "deploy-web-1").write_text(env.pop("web", WEB))
    (state / "containers" / "deploy-worker-1").write_text(env.pop("worker", WORKER_ID))
    (state / "listing").write_text(env.pop("listing", "app/a.py\napp/b.py"))
    if "diff" in env:
        (state / "diff_deploy-web-1").write_text(env.pop("diff"))
    (rel / "SHA256SUMS.unchanged").write_text("u  app/a.py\n")
    (rel / "SHA256SUMS.base").write_text("b  app/b.py\n")
    (rel / "FILES.expected").write_text("app/a.py\napp/b.py\n")
    (rel / "TURN_ENGINE.allowed").write_text(f"{'0' * 64}\n{KNOWN_TE}\n")
    if env.pop("no_manifest", False):
        (rel / "FILES.expected").unlink()
    (tmp_path / "docker").write_text(BASELINE_DOCKER)
    (tmp_path / "docker").chmod(0o755)
    copy = tmp_path / "baseline.sh"
    copy.write_bytes(BASELINE.read_bytes().replace(CRLF, LF))
    full = {**os.environ, "STATE": str(state), "REL": str(rel), "DOCKER": str(tmp_path / "docker"),
            "TE_HASH": KNOWN_TE, **env}
    done = subprocess.run(["bash", str(copy)], env=full, capture_output=True, text=True,
                          timeout=60, check=False)
    return done, rel / "PRE_ID"


def test_a_matching_baseline_records_the_image_id(tmp_path):
    done, pre = baseline(tmp_path)
    assert done.returncode == 0, done.stderr
    assert pre.read_text() == WEB


@pytest.mark.parametrize("env, why", [
    ({"worker": "sha256:" + "e" * 64}, "one image"),                  # web and worker differ
    ({"diff": "C /app/app\nC /app/app/turn_engine.py\n"}, "in-place edit"),
    ({"BAD_UNCHANGED": "1"}, "unchanged module"),
    ({"BAD_BASE": "1"}, "replaced module"),
    ({"listing": "app/a.py\napp/b.py\napp/extra.py"}, "extra file"),
    ({"listing": "app/a.py"}, "missing file"),
    ({"TE_HASH": "f" * 64}, "unknown turn_engine"),
    ({"BAD_IMAGE": "1"}, "image itself"),
    ({"no_manifest": True}, "missing manifest"),
])
def test_any_baseline_mismatch_stops_and_records_nothing(tmp_path, env, why):
    done, pre = baseline(tmp_path, **env)
    assert done.returncode == 1 and "STOP" in done.stderr, why
    assert not pre.exists(), why


def test_pycache_changes_alone_are_not_an_in_place_edit(tmp_path):
    done, pre = baseline(tmp_path, diff="A /app/app/__pycache__/x.pyc\nC /tmp\n")
    assert done.returncode == 0, done.stderr
    assert pre.exists()
