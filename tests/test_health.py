from fastapi.testclient import TestClient

from app import health
from app.web import app

client = TestClient(app)


def _stub_checks(monkeypatch, database, bucket):
    monkeypatch.setattr(health, "check_database", lambda url: database)
    monkeypatch.setattr(health, "check_bucket", lambda bucket_, region, endpoint: bucket)


def test_health_ok_when_database_and_bucket_reachable(monkeypatch):
    _stub_checks(monkeypatch, "ok", "ok")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "bucket": "ok"},
    }


def test_health_fails_when_database_unreachable(monkeypatch):
    _stub_checks(monkeypatch, "unreachable", "ok")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "unreachable"


def test_health_fails_when_bucket_unreachable(monkeypatch):
    _stub_checks(monkeypatch, "ok", "unreachable")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"]["bucket"] == "unreachable"


def test_health_fails_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"] == {
        "database": "not_configured",
        "bucket": "not_configured",
    }


def test_database_check_reports_unreachable_server():
    result = health.check_database(
        "postgresql://user:secret@127.0.0.1:1/db?connect_timeout=1"
    )

    assert result == "unreachable"
