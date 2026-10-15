"""Task 6.1: the Gemini provider adapter (app.llm). No database, no credentials and no
internet: responses come from httpx.MockTransport, and the wall-clock deadline is
checked against a local loopback server that answers slowly or never.
"""

import json
import os
import socket
import threading
import time

import httpx
import pytest

from app import llm
from app.llm import GeminiClient, ProviderError

KEY = "test-key-not-a-secret"
TOOLS = [
    {"name": "search_properties", "description": "Search.", "parameters": None},
    {"name": "find_listings", "description": "Find.", "parameters": {
        "type": "object",
        "properties": {"description": {"type": "string", "minLength": 1, "maxLength": 500}},
        "required": ["description"],
    }},
]
CONTENTS = [{"role": "user", "parts": [{"text": "Current messages, oldest first:\n1. hi"}]}]


def candidate(parts, reason="STOP"):
    return {"candidates": [{"content": {"role": "model", "parts": parts},
                            "finishReason": reason}]}


def call_part(name="search_properties", args=None, call_id="call-1", signature="sig-1"):
    part = {"functionCall": {"name": name, "args": args or {}, "id": call_id}}
    if signature:
        part["thoughtSignature"] = signature
    return part


# --- response parsing ---------------------------------------------------------------

def test_text_answer():
    response = llm.parse_response(candidate([{"text": "anything"}]))
    assert (response.kind, response.detail, response.tool_calls) == ("text", "stop", ())


def test_tool_calls_keep_the_model_content_exactly():
    body = candidate([{"text": "thinking", "thought": True}, call_part()])
    body["usageMetadata"] = {"promptTokenCount": 4100, "cachedContentTokenCount": 4000,
                             "candidatesTokenCount": 12, "thoughtsTokenCount": 30}
    body["modelVersion"] = "gemini-test-001"
    response = llm.parse_response(body)
    assert response.kind == "tool_calls"
    assert response.tool_calls == (llm.ToolCall("search_properties", {}, "call-1"),)
    assert response.model_content == body["candidates"][0]["content"]
    assert response.usage == llm.Usage(4100, 12, 4000, 30)
    assert response.model_version == "gemini-test-001"


@pytest.mark.parametrize(("body", "kind", "detail"), [
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "refused", "blocked:safety"),
    ({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "candidates": []},
     "refused", "blocked:prohibited_content"),
    (candidate([], "SAFETY"), "refused", "refused:safety"),
    (candidate([call_part()], "SPII"), "refused", "refused:spii"),
    (candidate([call_part()], "MALFORMED_FUNCTION_CALL"), "malformed",
     "malformed:malformed_function_call"),
    (candidate([call_part()], "UNEXPECTED_TOOL_CALL"), "malformed",
     "malformed:unexpected_tool_call"),
    (candidate([call_part()], "MAX_TOKENS"), "truncated", "truncated:max_tokens"),
    (candidate([call_part()], "OTHER"), "other", "finish:other"),
    (candidate([call_part()], None), "other", "finish:missing"),
    ({"candidates": []}, "malformed", "malformed:no_candidate"),
    ([], "malformed", "malformed:not_an_object"),
    ({"candidates": [{"finishReason": "STOP", "content": {"parts": "x"}}]},
     "malformed", "malformed:no_parts"),
    (candidate([{"functionCall": "search_properties"}]), "malformed",
     "malformed:function_call"),
    (candidate([call_part(call_id=f"c{n}") for n in range(5)]), "malformed",
     "malformed:too_many_calls"),
])
def test_unusable_responses_never_carry_tool_calls(body, kind, detail):
    response = llm.parse_response(body)
    assert (response.kind, response.detail) == (kind, detail)
    assert response.tool_calls == ()
    assert len(detail) <= 64


@pytest.mark.parametrize("body", [
    {"candidates": [{"finishReason": "STOP", "content": {"role": "model"}}]},
    {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": []}}]},
    {"candidates": [{"finishReason": "STOP"}]},
])
def test_an_empty_stop_is_a_finished_answer_not_a_malformed_one(body):
    # Seen live 2026-10-14 (gemini-3.5-flash-lite, the answer after tool results).
    response = llm.parse_response(body)
    assert (response.kind, response.detail) == ("text", "stop_empty")
    assert response.tool_calls == ()


def test_usage_counts_are_validated():
    usage = llm.parse_usage({"promptTokenCount": True, "candidatesTokenCount": -1,
                             "cachedContentTokenCount": "5", "thoughtsTokenCount": 3})
    assert usage == llm.Usage(None, None, None, 3, invalid=(
        "promptTokenCount", "candidatesTokenCount", "cachedContentTokenCount"))
    # A cache count above the input count is dropped, not stored.
    assert llm.parse_usage({"promptTokenCount": 10, "cachedContentTokenCount": 11}) == \
        llm.Usage(10, None, None, None, invalid=("cachedContentTokenCount",))
    # Absent counts are unknown, not invalid.
    assert llm.parse_usage({}) == llm.Usage()


def test_usage_counts_must_fit_bigint():
    top = llm.MAX_TOKEN_COUNT
    usage = llm.parse_usage({"promptTokenCount": top, "candidatesTokenCount": top + 1,
                             "thoughtsTokenCount": 2.0})
    # Out of range or not an int: unknown (NULL), never clamped to an invented value.
    assert usage == llm.Usage(top, None, None, None,
                              invalid=("candidatesTokenCount", "thoughtsTokenCount"))


def test_unsafe_model_version_is_not_kept():
    body = candidate([{"text": "x"}])
    body["modelVersion"] = "bad version/../x"
    assert llm.parse_response(body).model_version is None


# --- request body -------------------------------------------------------------------

def test_body_puts_the_stable_prefix_first():
    body = llm.build_body("SYSTEM", TOOLS, CONTENTS)
    assert list(body) == ["systemInstruction", "tools", "toolConfig", "contents"]
    declarations = body["tools"][0]["functionDeclarations"]
    assert declarations[0] == {"name": "search_properties", "description": "Search."}
    assert declarations[1]["parameters"] == {
        "type": "OBJECT", "properties": {"description": {"type": "STRING"}},
        "required": ["description"],
    }
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}
    assert "tools" not in llm.build_body("SYSTEM", [], CONTENTS)


# --- HTTP (fake transport) ----------------------------------------------------------

def client_for(handler):
    return GeminiClient(KEY, transport=httpx.MockTransport(handler))


def generate(client, timeout_s=8.0):
    return client.generate("gemini-test", system="SYSTEM", tools=TOOLS, contents=CONTENTS,
                           timeout_s=timeout_s)


def test_key_goes_in_a_header_never_the_url():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=candidate([{"text": "x"}]))

    assert generate(client_for(handler)).kind == "text"
    request = seen[0]
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    )
    assert json.loads(request.content)["systemInstruction"] == {"parts": [{"text": "SYSTEM"}]}
    assert KEY not in repr(client_for(handler))


@pytest.mark.parametrize(("status", "failover"), [
    (429, True), (500, True), (503, True), (404, True), (408, True),
    (400, False), (401, False), (403, False),
])
def test_http_errors_say_whether_failover_may_help(status, failover):
    client = client_for(lambda request: httpx.Response(status, text=f"secret body {KEY}"))
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert (caught.value.code, caught.value.failover) == (f"http_{status}", failover)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(("error", "code"), [
    (httpx.ReadTimeout("slow"), "timeout"),
    (httpx.ConnectTimeout("slow"), "timeout"),
    (httpx.ConnectError("refused"), "transport:connecterror"),
    (httpx.RemoteProtocolError("bad"), "transport:remoteprotocolerror"),
])
def test_transport_failures_allow_failover(error, code):
    def handler(request):
        raise error

    with pytest.raises(ProviderError) as caught:
        generate(client_for(handler))
    assert (caught.value.code, caught.value.failover) == (code, True)


def test_invalid_json_and_oversized_bodies_are_rejected():
    with pytest.raises(ProviderError, match="invalid_json"):
        generate(client_for(lambda request: httpx.Response(200, text="{not json")))
    big = b"x" * (llm.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(ProviderError, match="response_too_large"):
        generate(client_for(lambda request: httpx.Response(200, content=big)))


def test_undecodable_and_absurdly_nested_bodies_are_provider_errors():
    gzip = {"content-encoding": "gzip"}
    with pytest.raises(ProviderError) as caught:
        # A lazy stream, so the decoding fails while the adapter reads the body.
        generate(client_for(lambda request: httpx.Response(
            200, headers=gzip, stream=httpx.ByteStream(b"not gzip"))))
    assert (caught.value.code, caught.value.failover) == ("request:decodingerror", False)
    nested = b"[" * 200_000 + b"]" * 200_000
    with pytest.raises(ProviderError) as caught:
        generate(client_for(lambda request: httpx.Response(200, content=nested)))
    assert (caught.value.code, caught.value.failover) == ("invalid_json", False)


def test_programming_errors_are_not_hidden():
    def handler(request):
        raise TypeError("a bug")

    with pytest.raises(TypeError):
        generate(client_for(handler))


def test_no_call_without_time_left():
    def handler(request):
        raise AssertionError("must not be called")

    with pytest.raises(ProviderError, match="deadline"):
        generate(client_for(handler), timeout_s=0)


# --- wall-clock deadline (real sockets on 127.0.0.1) ----------------------------------

class SlowServer:
    """A loopback HTTP server: 'silent' reads the request and never answers;
    'trickle' sends headers, then one body byte every 0.2 s; 'fast' answers at once.
    It records when the client's connection closed."""

    def __init__(self, mode):
        self.mode = mode
        self.closed_at = []
        self.release = threading.Event()
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1beta"

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise ConnectionResetError  # closed before the request arrived
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                length = int(next(line.split(b":")[1] for line in head.split(b"\r\n")
                                  if line.lower().startswith(b"content-length")))
                while len(body) < length:
                    body += conn.recv(65536)
                if self.mode == "fast":
                    payload = json.dumps(candidate([{"text": "x"}])).encode()
                    conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                                 b"content-length: " + str(len(payload)).encode()
                                 + b"\r\n\r\n" + payload)
                elif self.mode in ("trickle", "trickle_close"):
                    if self.mode == "trickle":
                        conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-length: 1000\r\n\r\n")
                    else:  # body framed by connection close: the cut looks like its end
                        conn.sendall(b"HTTP/1.1 200 OK\r\nconnection: close\r\n\r\n"
                                     b'{"candidates": [')
                    for _ in range(100):
                        if self.release.wait(0.2):
                            break
                        conn.sendall(b" ")
                conn.settimeout(10)
                if conn.recv(1) == b"":  # silent / after trickle: wait for the client
                    self.closed_at.append(time.monotonic())
            except OSError:
                self.closed_at.append(time.monotonic())

    def close(self):
        self.release.set()
        self.thread.join(timeout=15)
        self.sock.close()


@pytest.fixture
def slow_server(request):
    server = SlowServer(request.param)
    yield server
    server.close()


def timers():
    return [t for t in threading.enumerate() if isinstance(t, threading.Timer) and t.is_alive()]


@pytest.mark.parametrize("slow_server", ["silent"], indirect=True)
def test_a_silent_server_is_cut_off_at_the_deadline(slow_server):
    client = GeminiClient(KEY, base_url=slow_server.base_url)
    began = time.monotonic()
    with pytest.raises(ProviderError) as caught:
        generate(client, timeout_s=0.5)
    elapsed = time.monotonic() - began
    assert (caught.value.code, caught.value.failover) == ("timeout", True)
    assert 0.4 <= elapsed < 1.5
    assert timers() == []  # no watchdog left running
    # The request is not left running in the background: the server sees the close.
    slow_server.thread.join(timeout=5)
    assert slow_server.closed_at and slow_server.closed_at[0] - began < 1.5


@pytest.mark.parametrize("slow_server", ["trickle", "trickle_close"], indirect=True)
def test_a_trickling_body_cannot_outlast_the_deadline(slow_server):
    # Every byte arrives well within the read timeout, so httpx alone never times out;
    # the watchdog still ends the call at the 1 s deadline.
    client = GeminiClient(KEY, base_url=slow_server.base_url)
    began = time.monotonic()
    with pytest.raises(ProviderError, match="timeout"):
        generate(client, timeout_s=1.0)
    elapsed = time.monotonic() - began
    assert 0.9 <= elapsed < 2.0
    assert timers() == []
    slow_server.release.set()
    slow_server.thread.join(timeout=5)
    assert slow_server.closed_at


@pytest.mark.parametrize("slow_server", ["fast"], indirect=True)
def test_a_fast_answer_is_unaffected_and_the_connection_closed(slow_server):
    client = GeminiClient(KEY, base_url=slow_server.base_url)
    assert generate(client, timeout_s=2.0).kind == "text"
    assert timers() == []
    slow_server.thread.join(timeout=5)
    assert slow_server.closed_at  # no keep-alive: nothing pooled after the call


class TLSStallServer:
    """A loopback 'TLS' server that reads the ClientHello, then announces a 16 KiB
    handshake record and trickles it one byte every 0.2 s: the handshake never
    completes, yet every socket read returns data well within any read timeout."""

    def __init__(self):
        self.closed_at = []
        self.release = threading.Event()
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            try:
                conn.recv(65536)  # ClientHello
                conn.sendall(bytes([0x16, 0x03, 0x03, 0x40, 0x00]))
                while not self.release.wait(0.2):
                    conn.sendall(b"\x02")
            except OSError:
                self.closed_at.append(time.monotonic())

    def close(self):
        self.release.set()
        self.thread.join(timeout=15)
        self.sock.close()


def test_a_stalled_tls_handshake_is_cut_off_at_the_deadline(monkeypatch):
    # Socket timeouts far beyond the deadline: only the watchdog can end the call.
    monkeypatch.setattr(llm, "_timeout", lambda timeout_s: httpx.Timeout(30.0))
    server = TLSStallServer()
    try:
        client = GeminiClient(KEY, base_url=f"https://127.0.0.1:{server.port}/v1beta")
        began = time.monotonic()
        with pytest.raises(ProviderError) as caught:
            generate(client, timeout_s=1.0)
        elapsed = time.monotonic() - began
        assert (caught.value.code, caught.value.failover) == ("timeout", True)
        assert 0.9 <= elapsed < 2.0
        assert timers() == []
        server.thread.join(timeout=5)
        assert server.closed_at and server.closed_at[0] - began < 2.0
    finally:
        server.close()


class _Stream:
    def __init__(self, sock):
        self.sock = sock

    def get_extra_info(self, name):
        return self.sock if name == "socket" else None


def test_the_watchdog_shuts_only_its_own_socket():
    # The request's socket is closed by httpcore before the timer fires, and a new
    # connection may take over its descriptor number. The watchdog's shutdown goes
    # to its own duplicate (still open), so the new connection is untouched.
    ours, peer = socket.socketpair()
    watchdog = llm._Watchdog(0.2)
    watchdog.trace("connection.connect_tcp.complete", {"return_value": _Stream(ours)})
    ours.close()
    other, other_peer = socket.socketpair()
    try:
        deadline = time.monotonic() + 5
        while not watchdog.fired and time.monotonic() < deadline:
            time.sleep(0.01)
        watchdog.stop()  # joins the timer: its shutdown has run
        assert watchdog.fired
        peer.settimeout(2)
        assert peer.recv(1) == b""  # the request's connection was shut down
        other.sendall(b"x")
        other_peer.settimeout(2)
        assert other_peer.recv(1) == b"x"  # the other connection still works
    finally:
        for sock in (peer, other, other_peer):
            sock.close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc (Linux)")
@pytest.mark.parametrize("slow_server", ["silent"], indirect=True)
def test_a_cut_call_leaves_no_descriptor_behind(slow_server):
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(ProviderError, match="timeout"):
        generate(GeminiClient(KEY, base_url=slow_server.base_url), timeout_s=0.5)
    slow_server.thread.join(timeout=5)  # the server has closed its side as well
    assert len(os.listdir("/proc/self/fd")) == before


# --- Task 11.1: the L3 lead summary -----------------------------------------------------------

def _summary_body(text, reason="STOP"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": reason}],
            "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 9}}


def test_summary_body_has_no_tools_and_asks_for_json():
    body = llm.build_summary_body("system", "facts", "low")
    assert "tools" not in body
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "facts"}]}]


def test_parse_summary():
    result = llm.parse_summary(_summary_body(json.dumps({"summary": " Wants a visit. "})))
    assert (result.text, result.usage.input_tokens) == ("Wants a visit.", 30)
    for bad in (_summary_body("not json"), _summary_body(json.dumps({"x": 1})),
                _summary_body(json.dumps({"summary": "x"}), reason="MAX_TOKENS")):
        with pytest.raises(ProviderError):
            llm.parse_summary(bad)


def test_summarize_fails_over_only_on_retryable_statuses():
    def handler(request):
        return httpx.Response(503, json={})
    client = GeminiClient("test-key-not-a-secret", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as exc:
        client.summarize("m", system="s", text="t", timeout_s=5)
    assert exc.value.failover and exc.value.code == "http_503"
