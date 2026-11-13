import subprocess
import sys

from fastapi.testclient import TestClient

from app import web
from app.web import app

client = TestClient(app)


def test_web_liveness():
    response = client.get("/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "running",
        "role": "web",
        "revision": web.read_revision(),
    }


def test_web_liveness_reports_the_image_revision(tmp_path, monkeypatch):
    revision_file = tmp_path / "REVISION"
    assert web.read_revision(revision_file) == "unknown"
    revision_file.write_text("0123abc\n", encoding="utf-8")
    monkeypatch.setattr(web, "REVISION", web.read_revision(revision_file))

    assert client.get("/live").json()["revision"] == "0123abc"


def test_unknown_role_is_rejected():
    result = subprocess.run(
        [sys.executable, "-m", "app", "invalid-role"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid choice" in result.stderr