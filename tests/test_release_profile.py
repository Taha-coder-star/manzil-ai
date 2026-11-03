"""docs/release/map_production_hashes.py: pasted production hashes are mapped to Git blobs, and a
profile with an unexplained state is refused. Hermetic: a throw-away repository."""

import hashlib
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git (absent from the test image)")

SPEC = importlib.util.spec_from_file_location(
    "mapper", Path(__file__).resolve().parents[1] / "docs" / "release" / "map_production_hashes.py")
mapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mapper)


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def repo(tmp_path):
    def run(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], check=True, capture_output=True)
    run("init", "-q")
    (tmp_path / "app").mkdir()
    versions = [b"v1\n", b"v2\n", b"v3\n"]
    for n, body in enumerate(versions, 1):
        (tmp_path / "app" / "render.py").write_bytes(body)
        (tmp_path / "app" / "same.py").write_bytes(b"same\n")
        run("add", "-A")
        run("commit", "-q", "-m", f"render v{n}")
    return tmp_path, versions


def tree(web, worker, diff=""):
    def section(name, files):
        return f"== {name}\n" + "".join(f"{sha(b)}  {p}\n" for p, b in files.items()) \
            + "-- docker diff /app\n" + diff
    return section("deploy-web-1", web) + section("deploy-worker-1", worker)


def run_mapper(tmp_path, text, *extra):
    prod = tmp_path / "prod-tree.txt"
    prod.write_text(text)
    return mapper.main([str(prod), "--repo", str(tmp_path), "--merge", "HEAD", *extra])


def test_a_file_at_an_older_version_is_mapped_to_its_commit(repo, capsys):
    path, versions = repo
    files = {"app/render.py": versions[0], "app/same.py": b"same\n"}
    assert run_mapper(path, tree(files, files), "--profile") == 0
    out = capsys.readouterr().out
    assert "mapped    app/render.py" in out and "render v1" in out
    assert f"app/render.py {sha(versions[0])}" in out


def test_content_no_commit_ever_had_is_unmapped_and_fails(repo, capsys):
    path, _ = repo
    files = {"app/render.py": b"hand edited\n", "app/same.py": b"same\n"}
    assert run_mapper(path, tree(files, files)) == 1
    assert "UNMAPPED  app/render.py" in capsys.readouterr().out


def test_web_and_worker_disagreeing_or_an_in_place_edit_fails(repo, capsys):
    path, versions = repo
    web = {"app/render.py": versions[2], "app/same.py": b"same\n"}
    worker = {"app/render.py": versions[1], "app/same.py": b"same\n"}
    assert run_mapper(path, tree(web, worker)) == 1
    assert "DIFFERENT app/render.py" in capsys.readouterr().out
    assert run_mapper(path, tree(web, web, diff="C /app/app/render.py\n")) == 1


def test_an_extra_file_fails(repo, capsys):
    path, versions = repo
    files = {"app/render.py": versions[2], "app/same.py": b"same\n", "app/extra.py": b"x\n"}
    assert run_mapper(path, tree(files, files)) == 1
    assert "EXTRA     app/extra.py" in capsys.readouterr().out


def profile(tmp_path, *lines):
    p = tmp_path / "profile.txt"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def check(tmp_path, *lines):
    return mapper.main(["--check-profile", profile(tmp_path, *lines), "--repo", str(tmp_path)])


def test_a_profile_state_must_be_a_repository_blob_absent_or_marked_diffed(repo, capsys):
    path, versions = repo
    ok = f"app/render.py {sha(versions[0])}  # render v1"
    assert check(path, "# comment", ok, "app/gone.py ABSENT") == 0
    unknown = f"app/render.py {sha(b'nobody wrote this')}"
    assert check(path, unknown) == 1 and "UNEXPLAINED" in capsys.readouterr().out
    assert check(path, unknown + "  # diffed by hand against 608f4d6, greeting lines only") == 0
    assert check(path, "app/render.py not-a-hash") == 1
