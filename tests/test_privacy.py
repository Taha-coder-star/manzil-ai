from fastapi.testclient import TestClient

from app.web import app

client = TestClient(app)


def test_privacy_page_is_public_html_with_deletion_section(monkeypatch):
    monkeypatch.setenv("PRIVACY_CONTACT_EMAIL", "privacy@example.com")

    response = client.get("/privacy")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="deletion"' in response.text
    assert "mailto:privacy@example.com" in response.text
    assert "do not use WhatsApp messages to train" in response.text


def test_privacy_page_without_contact_email(monkeypatch):
    monkeypatch.delenv("PRIVACY_CONTACT_EMAIL", raising=False)

    response = client.get("/privacy")

    assert response.status_code == 200
    assert "mailto:" not in response.text
    assert "agency you contacted" in response.text


def test_privacy_contact_email_is_escaped(monkeypatch):
    monkeypatch.setenv("PRIVACY_CONTACT_EMAIL", '"><script>x</script>')

    assert "<script>x" not in client.get("/privacy").text
