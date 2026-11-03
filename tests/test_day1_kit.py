"""Packaging gates: unmerged refs and unsupported layers cannot produce a kit."""
import hashlib
import importlib.util
import subprocess
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "day1_kit", Path(__file__).resolve().parents[1] / "docs/release/build_day1_kit.py")
kit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kit)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(["git", "-c", "user.name=test", "-c",
                                        "user.email=test@example.invalid", *args])
    git("init", "-q")
    for name in ("app/a.py", "app/b.py", "migrations/001_schema.sql", "demo/data.csv",
                 "docs/release/fresh_search_baseline.sh", "docs/release/fresh_search_image_check.sh",
                 "docs/release/fresh_search_switch.sh"):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"original\n")
    def commit():
        git("add", ".")
        git("commit", "-qm", "test")
        return git("rev-parse", "HEAD").decode().strip()
    base = commit()
    (tmp_path / "app/a.py").write_bytes(b"changed\n")
    (tmp_path / "app/new.py").write_bytes(b"new\n")
    target = commit()
    git("update-ref", "refs/remotes/origin/main", target)
    return tmp_path, git, commit, base, target


def test_kit_uses_committed_bytes_and_records_absent_files(repo):
    root, _, _, base, target = repo
    (root / "app/a.py").write_bytes(b"uncommitted garbage")
    out = root / "kit"
    short = kit.build(base, target, out)
    with tarfile.open(out / f"manzil-fresh-{short}.tar") as archive:
        assert archive.extractfile("release-fresh/app/a.py").read() == b"changed\n"
        assert len([x for x in archive.getmembers() if x.isfile()]) == 2
    states = (out / "STATES.allowed").read_text()
    assert "app/new.py ABSENT" in states
    assert hashlib.sha256(b"original\r\n").hexdigest() in states
    assert "app/b.py" in (out / "SHA256SUMS.unchanged").read_text()
    for line in (out / "KIT.sha256").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("change", ["unmerged", "migration", "deletion"])
def test_unsafe_candidates_produce_no_output(repo, change):
    root, git, commit, base, target = repo
    if change == "unmerged":
        git("update-ref", "refs/remotes/origin/main", base)
    elif change == "migration":
        (root / "migrations/001_schema.sql").write_bytes(b"edited migration")
        target = commit()
        git("update-ref", "refs/remotes/origin/main", target)
    else:
        (root / "app/b.py").unlink()
        target = commit()
        git("update-ref", "refs/remotes/origin/main", target)
    out = root / "kit"
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        kit.build(base, target, out)
    assert not out.exists()
