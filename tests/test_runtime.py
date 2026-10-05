import subprocess
import sys

from fastapi.testclient import TestClient

from app.web import app

client = TestClient(app)


def test_web_liveness():
    response = client.get("/live")

    assert response.status_code == 200
    assert response.json() == {"status": "running", "role": "web"}


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