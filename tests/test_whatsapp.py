"""Task 3.2: WhatsApp payload builders, client error handling and the #demo samples.

No network: the client runs against httpx.MockTransport. All content is fictional.
"""

import logging

import httpx
import pytest

from app import echo, whatsapp

TOKEN = "EAA-test-token-never-logged"
TO = "923000000001"


def test_text_shape():
    assert whatsapp.text("Salaam") == {
        "type": "text", "text": {"body": "Salaam", "preview_url": False},
    }


def test_reply_buttons_shape():
    payload = whatsapp.reply_buttons(
        "Pick one", [("a", "More photos"), ("b", "Visit")], header="Results", footer="DEMO"
    )
    assert payload == {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "header": {"type": "text", "text": "Results"},
            "body": {"text": "Pick one"},
            "footer": {"text": "DEMO"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "a", "title": "More photos"}},
                {"type": "reply", "reply": {"id": "b", "title": "Visit"}},
            ]},
        },
    }


def test_reply_buttons_image_header():
    payload = whatsapp.reply_buttons(
        "Card", [("a", "Visit")], image_header={"link": "https://example.com/c.jpg"}
    )
    assert payload["interactive"]["header"] == {
        "type": "image", "image": {"link": "https://example.com/c.jpg"},
    }


@pytest.mark.parametrize("buttons", [
    [],
    [("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")],
    [("a", "x" * 21)],
    [("a", "One"), ("a", "Two")],
])
def test_reply_button_limits(buttons):
    with pytest.raises(ValueError):
        whatsapp.reply_buttons("Body", buttons)


def test_interactive_body_limit():
    with pytest.raises(ValueError):
        whatsapp.reply_buttons("x" * 1025, [("a", "Ok")])


def test_list_shape():
    payload = whatsapp.list_message(
        "Results", "View", [("Phase 6", [("r1", "#1 · DH6-101", "500 sq yd"), ("r2", "#2", None)])]
    )
    assert payload["interactive"] == {
        "type": "list",
        "body": {"text": "Results"},
        "action": {"button": "View", "sections": [{
            "title": "Phase 6",
            "rows": [
                {"id": "r1", "title": "#1 · DH6-101", "description": "500 sq yd"},
                {"id": "r2", "title": "#2"},
            ],
        }]},
    }


@pytest.mark.parametrize("sections", [
    [],
    [("S", [])],
    [("S", [(f"r{i}", "Row", None) for i in range(11)])],
    [("S", [("r1", "x" * 25, None)])],
    [("S", [("r1", "Row", "x" * 73)])],
    [("S", [("r1", "Row", None), ("r1", "Row", None)])],
    [(None, [("r1", "Row", None)]), ("Two", [("r2", "Row", None)])],
])
def test_list_limits(sections):
    with pytest.raises(ValueError):
        whatsapp.list_message("Body", "View", sections)


def test_list_button_label_limit():
    with pytest.raises(ValueError):
        whatsapp.list_message("Body", "x" * 21, [("S", [("r1", "Row", None)])])


def test_image_by_id_or_link():
    assert whatsapp.image(media_id="123", caption="DH6-101") == {
        "type": "image", "image": {"id": "123", "caption": "DH6-101"},
    }
    assert whatsapp.image(link="https://example.com/a.jpg") == {
        "type": "image", "image": {"link": "https://example.com/a.jpg"},
    }
    for kwargs in ({}, {"media_id": "1", "link": "https://example.com/a.jpg"},
                   {"link": "http://example.com/a.jpg"},
                   {"media_id": "1", "caption": "x" * 1025}):
        with pytest.raises(ValueError):
            whatsapp.image(**kwargs)


def test_location_and_cta():
    assert whatsapp.location(24.8, 67.03, name="Office (DEMO)") == {
        "type": "location", "location": {"latitude": 24.8, "longitude": 67.03,
                                         "name": "Office (DEMO)"},
    }
    with pytest.raises(ValueError):
        whatsapp.location(91, 0)
    assert whatsapp.cta_url("Talk to the agent", "Chat", "https://wa.me/923000000002") == {
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": "Talk to the agent"},
            "action": {"name": "cta_url", "parameters": {
                "display_text": "Chat", "url": "https://wa.me/923000000002",
            }},
        },
    }
    with pytest.raises(ValueError):
        whatsapp.cta_url("Body", "x" * 21, "https://example.com")
    with pytest.raises(ValueError):
        whatsapp.cta_url("Body", "Open", "javascript:alert(1)")


def make_client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return whatsapp.WhatsAppClient("100000000000001", TOKEN, "v25.0", http=http)


def test_send_posts_envelope_and_returns_wamid():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT1"}]})

    wamid = make_client(handler).send(TO, whatsapp.text("Hi"), reply_to="wamid.IN1")
    assert wamid == "wamid.OUT1"
    request = seen[0]
    assert str(request.url) == "https://graph.facebook.com/v25.0/100000000000001/messages"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert httpx.Response(200, content=request.content).json() == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": TO,
        "type": "text", "text": {"body": "Hi", "preview_url": False},
        "context": {"message_id": "wamid.IN1"},
    }


def test_mark_read_payload():
    seen = []

    def handler(request):
        seen.append(httpx.Response(200, content=request.content).json())
        return httpx.Response(200, json={"success": True})

    make_client(handler).mark_read("wamid.IN1", typing=True)
    assert seen == [{
        "messaging_product": "whatsapp", "status": "read", "message_id": "wamid.IN1",
        "typing_indicator": {"type": "text"},
    }]


def _raise(exc_type):
    def handler(request):
        raise exc_type("simulated", request=request)
    return handler


@pytest.mark.parametrize(("handler", "error", "code"), [
    (lambda r: httpx.Response(429, json={"error": {"code": 130429}}),
     whatsapp.RetryableSendError, "http_429_meta_130429"),
    (lambda r: httpx.Response(500), whatsapp.RetryableSendError, "http_500"),
    (lambda r: httpx.Response(400, json={"error": {"code": 131056}}),
     whatsapp.RetryableSendError, "http_400_meta_131056"),
    (lambda r: httpx.Response(400, json={"error": {"code": 131047}}),
     whatsapp.PermanentSendError, "http_400_meta_131047"),
    (lambda r: httpx.Response(401, json={"error": {"code": 190}}),
     whatsapp.PermanentSendError, "http_401_meta_190"),
    (lambda r: httpx.Response(200, json={}), whatsapp.AmbiguousSendError, "missing_message_id"),
    (_raise(httpx.ConnectError), whatsapp.RetryableSendError, "ConnectError"),
    (_raise(httpx.ConnectTimeout), whatsapp.RetryableSendError, "ConnectTimeout"),
    (_raise(httpx.ReadTimeout), whatsapp.AmbiguousSendError, "ReadTimeout"),
    (_raise(httpx.RemoteProtocolError), whatsapp.AmbiguousSendError, "RemoteProtocolError"),
])
def test_error_classification(handler, error, code):
    with pytest.raises(error) as caught:
        make_client(handler).send(TO, whatsapp.text("Hi"))
    assert type(caught.value) is error
    assert caught.value.code == code


def test_token_and_body_never_logged(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return httpx.Response(400, json={"error": {"code": 100, "message": "secret body"}})

    with pytest.raises(whatsapp.PermanentSendError):
        make_client(handler).send(TO, whatsapp.text("private customer text"))
    assert TOKEN not in caplog.text
    assert "private customer text" not in caplog.text
    assert "secret body" not in caplog.text
    assert TO not in caplog.text


def test_summary():
    assert whatsapp.summary(whatsapp.text("Hi")) == "Hi"
    assert whatsapp.summary(whatsapp.reply_buttons("Pick", [("a", "A")])) == "Pick"
    assert whatsapp.summary(whatsapp.image(media_id="1")) is None


def inbound_text(body):
    return {"from": TO, "id": "wamid.IN1", "type": "text", "text": {"body": body}}


def test_non_demo_text_gets_no_demo_reply():
    # Task 5.1: plain text is answered by the orchestrator, not echoed.
    assert echo.reply(inbound_text("hi")) == []
    assert echo.reply(inbound_text("demo buttons")) == []
    assert echo.is_demo("  #DEMO list") and not echo.is_demo("hi #demo")


@pytest.mark.parametrize(("command", "kind"), [
    ("#demo buttons", "button"), ("#DEMO list", "list"), ("#demo cta", "cta_url"),
])
def test_echo_demo_interactive(command, kind):
    payload = echo.reply(inbound_text(command), privacy_url="https://example.com/privacy")[0]
    assert payload["interactive"]["type"] == kind


def test_echo_demo_location_image_and_help():
    assert echo.reply(inbound_text("#demo location"))[0]["type"] == "location"
    image = echo.reply(inbound_text("#demo image"), image_link=lambda: "https://s3.example/x")[0]
    assert image["image"]["link"] == "https://s3.example/x"
    # Unconfigured samples and unknown commands fall back to text.
    for command in ("#demo image", "#demo cta", "#demo nope"):
        assert echo.reply(inbound_text(command))[0]["type"] == "text"


def test_demo_reply_ignores_other_message_types():
    tapped = {"type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": "demo_btn_2", "title": "Visit"},
    }}
    assert echo.reply(tapped) == []
    assert echo.reply({"type": "reaction", "reaction": {"emoji": "👍"}}) == []
    assert echo.reply({"type": "audio", "audio": {"id": "1"}}) == []
