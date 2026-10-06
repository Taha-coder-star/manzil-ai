import hashlib
import hmac
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import webhook
from app.web import app

client = TestClient(app)

SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ENVELOPE = {
    "object": "whatsapp_business_account",
    "entry": [{"id": "0", "changes": [{"field": "messages", "value": {}}]}],
}


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", SECRET)
    monkeypatch.setenv("WEBHOOK_VERIFY_TOKEN", VERIFY_TOKEN)


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(body, signature):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[webhook.SIGNATURE_HEADER] = signature
    return client.post("/webhook", content=body, headers=headers)


def verify_params(**overrides):
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": VERIFY_TOKEN,
        "hub.challenge": "1158201444",
    }
    params.update(overrides)
    return {key: value for key, value in params.items() if value is not None}


def test_get_returns_exact_challenge_as_plain_text():
    response = client.get("/webhook", params=verify_params())

    assert response.status_code == 200
    assert response.text == "1158201444"
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    "overrides",
    [
        {"hub.verify_token": "wrong"},
        {"hub.mode": "unsubscribe"},
        {"hub.verify_token": None},
        {"hub.mode": None},
        {"hub.challenge": None},
    ],
)
def test_get_rejects_invalid_or_missing_params(overrides):
    response = client.get("/webhook", params=verify_params(**overrides))

    assert response.status_code == 403
    assert "1158201444" not in response.text


def test_get_rejects_when_token_not_configured(monkeypatch):
    monkeypatch.delenv("WEBHOOK_VERIFY_TOKEN")

    assert client.get("/webhook", params=verify_params()).status_code == 403


def test_post_accepts_valid_signature_on_raw_bytes():
    # Non-canonical spacing: the signature must cover the bytes as sent.
    body = b'{"object": "whatsapp_business_account",   "entry": []}'

    response = post(body, sign(body))

    assert response.status_code == 200
    assert response.json() == {"status": "received"}


def test_post_accepts_uppercase_hex_signature():
    body = json.dumps(ENVELOPE).encode()

    assert post(body, "sha256=" + sign(body)[7:].upper()).status_code == 200


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "sha256=",
        "sha256=nothex" + "0" * 57,
        "sha1=" + "0" * 40,
        "0" * 64,
        "sha256=" + "0" * 63,
        "sha256=" + "0" * 64,
    ],
)
def test_post_rejects_missing_malformed_or_bad_signature(signature):
    assert post(json.dumps(ENVELOPE).encode(), signature).status_code == 401


def test_post_rejects_signature_from_other_secret():
    body = json.dumps(ENVELOPE).encode()

    assert post(body, sign(body, "other-secret")).status_code == 401


def test_post_rejects_tampered_body():
    body = json.dumps(ENVELOPE).encode()
    tampered = json.dumps(ENVELOPE, indent=1).encode()

    assert post(tampered, sign(body)).status_code == 401


def test_post_fails_closed_without_app_secret(monkeypatch):
    monkeypatch.delenv("META_APP_SECRET")
    body = json.dumps(ENVELOPE).encode()

    assert post(body, sign(body)).status_code == 503


def test_post_rejects_oversized_body():
    body = b" " * (webhook.MAX_BODY_BYTES + 1)

    assert post(body, sign(body)).status_code == 413


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"object": "page"}'])
def test_post_ignores_authentic_unsupported_payload(body):
    response = post(body, sign(body))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


def test_secrets_never_logged(caplog):
    body = json.dumps(ENVELOPE).encode()
    with caplog.at_level("DEBUG"):
        post(body, "sha256=" + "0" * 64)
        client.get("/webhook", params=verify_params(**{"hub.verify_token": "wrong"}))

    app_logs = " ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("app")
    )
    assert app_logs
    assert SECRET not in app_logs
    assert VERIFY_TOKEN not in app_logs
    assert "wrong" not in app_logs


def test_access_log_redacts_verify_token():
    # Shape of Meta's real request: every parameter is sent dotted and underscored.
    path = (
        f"/webhook?hub.mode=subscribe&hub.challenge=7&hub.verify_token={VERIFY_TOKEN}"
        f"&hub_mode=subscribe&hub_challenge=7&hub_verify_token={VERIFY_TOKEN}"
        f"&hub%2Everify_token={VERIFY_TOKEN}"
    )
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", path, "1.1", 200), None,
    )

    webhook.RedactVerifyTokenFilter().filter(record)

    message = record.getMessage()
    assert VERIFY_TOKEN not in message
    assert "hub.verify_token=[REDACTED]&hub_mode" in message
    assert "hub_verify_token=[REDACTED]&" in message
    assert "hub%2Everify_token=[REDACTED] " in message
