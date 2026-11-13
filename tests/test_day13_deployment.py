"""Render Compose offline with fictional settings; no Docker daemon or providers."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("enabled", [False, True])
def test_budget_override_is_explicit_and_shares_one_ledger(tmp_path, enabled):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI not installed")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name in ("compose.yml", "compose.day13-budget.yml"):
        shutil.copyfile(ROOT / "deploy" / name, deploy / name)
    (tmp_path / ".env").write_text("SITE_ADDRESS=demo.invalid\nAI_PROVIDER=gemini\n", "utf-8")
    shared = tmp_path / "shared-budget"
    shared.mkdir()
    env = dict(os.environ, DAY13_BUDGET_DIR=str(shared), DAY13_BUDGET_PHASE="tuning",
               DAY13_BUDGET_COLLECTION="true")
    args = [docker, "compose", "--env-file", str(tmp_path / ".env"),
            "-f", str(deploy / "compose.yml")]
    if enabled:
        args += ["-f", str(deploy / "compose.day13-budget.yml")]
    process = subprocess.run([*args, "config", "--format", "json"], env=env,
                             capture_output=True, text=True, timeout=30, check=False)
    assert process.returncode == 0, "fictional Compose config did not render"
    services = json.loads(process.stdout)["services"]
    for name in ("web", "worker"):
        settings = services[name]["environment"]
        assert settings["AI_PROVIDER"] == "gemini"
        if enabled:
            assert settings["DAY13_BUDGET_LEDGER"] == "/day13/budget.sqlite"
            assert settings["DAY13_BUDGET_PHASE"] == "tuning"
            assert settings["DAY13_BUDGET_COLLECTION"] == "true"
        else:
            assert "DAY13_BUDGET_POLICY" not in settings
    if enabled:
        volumes = [next(v for v in services[n]["volumes"] if v["target"] == "/day13")
                   for n in ("web", "worker")]
        assert volumes[0]["source"] == volumes[1]["source"]
        assert all(v.get("read_only") is not True for v in volumes)
