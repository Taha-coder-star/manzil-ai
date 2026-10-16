"""LLM provider adapter (Task 6.1, component C6): Gemini generateContent over httpx.

Stateless: every call sends the system instruction, the tool declarations and the
whole conversation built by app.turn_engine; nothing is stored at the provider by
this code (no cachedContents, no Interactions API). Implicit prompt caching is the
provider's own and is only reported (usageMetadata.cachedContentTokenCount).

Deadline: an httpx read timeout bounds each socket read, not the whole request, so
every call also runs under a watchdog. The request runs in the calling worker
thread. Once the TCP connection is up (httpx's documented "trace" request
extension, event connect_tcp.complete) the watchdog takes its own dup() of the
socket, and a threading.Timer shuts that dup down at the deadline. shutdown() acts
on the connection, not on the descriptor, so the TLS handshake, the request upload
or the response read fails at once in the calling thread. The watchdog closes its
dups only after the timer is cancelled and joined, so a shutdown can never reach a
descriptor number that another connection has reused. Each call uses its own
connection (no keep-alive): nothing is left running or pooled after it returns.

Not covered by the watchdog: DNS resolution and the TCP connect itself (before
connect_tcp.complete). They are bounded only by the resolver and by the connect
timeout, min(CONNECT_TIMEOUT_SECONDS, deadline), per resolved address; when they
take longer than the deadline, the call ends that much later.

Task 6.3: GeminiClient.transcribe is the STT fallback (one inline audio part,
structured JSON output, no tools); post_with_deadline is shared with app.stt.

The API key goes in the x-goog-api-key header, never in the URL. Errors carry only
a sanitised code (status, exception class, finish reason), never a response body.
Never log prompts, message text, tool arguments or the key.
"""

import base64
import json
import re
import socket
import threading
from dataclasses import dataclass, field

import httpx

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
CONNECT_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 1_000_000
# generationConfig.thinkingConfig.thinkingLevel (ai.google.dev thinking guide, read
# 2026-10-12: Gemini 3 defaults to "high"; gemini-3.5-flash-lite and gemini-3.6-flash
# accept minimal / low / medium / high). Low keeps the p90 latency targets in reach.
THINKING_LEVELS = ("minimal", "low", "medium", "high")
DEFAULT_THINKING_LEVEL = "low"
# More function calls than this in one response is treated as malformed.
MAX_TOOL_CALLS = 4
# usage_event token columns are BIGINT; a count outside this range is not stored.
MAX_TOKEN_COUNT = 2**63 - 1

# Candidate.finishReason / promptFeedback.blockReason (ai.google.dev API reference,
# read 2026-10-12). A refusal is answered with the refusal template, never failed over.
REFUSAL_REASONS = frozenset({
    "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION",
    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
})
MALFORMED_REASONS = frozenset({
    "MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS",
    "MISSING_THOUGHT_SIGNATURE", "MALFORMED_RESPONSE",
})
# Gemini Schema keys sent with a declaration; local-only checks (maxLength, ...) are
# enforced by app.ai_tools at execution time.
_SCHEMA_KEYS = ("description", "enum", "minimum", "maximum", "minItems", "maxItems")
_CODE = re.compile(r"[^a-z0-9_]+")
_MODEL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Task 6.3 transcription (STT fallback): a small thinking budget for latency (Gemini 3
# defaults to high; CLAUDE.md section 1). app.speech passes LLM_THINKING_LEVEL, so
# 'off' (None) leaves it out here too.
TRANSCRIBE_THINKING_LEVEL = DEFAULT_THINKING_LEVEL
TRANSCRIBE_INSTRUCTION = (
    "You transcribe one WhatsApp voice note from a customer of a real-estate agency in "
    "Karachi, Pakistan. Write exactly what is said, word for word, in the script it is "
    "spoken in (Roman Urdu or Urdu script for Urdu, English for English; mixed speech "
    "stays mixed). Never translate, summarise, correct or complete it, and never add "
    "words that are not spoken. Keep numbers, prices, sizes and place names as spoken. "
    "If there is no clear human speech (silence, noise, music), set speech_present to "
    "false and transcript to an empty string. language: the main spoken language as an "
    "ISO 639-1 code (ur, en, hi, ...), or an empty string."
)
SUMMARY_SCHEMA = {
    "type": "OBJECT",
    "properties": {"summary": {"type": "STRING"}},
    "required": ["summary"],
}
TRANSCRIBE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "speech_present": {"type": "BOOLEAN"},
        "transcript": {"type": "STRING"},
        "language": {"type": "STRING"},
    },
    "required": ["speech_present", "transcript", "language"],
}


class ProviderError(Exception):
    """A provider call that produced no usable response. failover says whether the
    configured failover model may be tried (timeouts, transport errors, 404, 429, 5xx)."""

    def __init__(self, code, *, failover):
        super().__init__(code)
        self.code = code
        self.failover = failover


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    thinking_tokens: int | None = None
    # usageMetadata fields that were present but unusable, stored as NULL (fixed
    # field names only, for the log).
    invalid: tuple = ()


@dataclass(frozen=True)
class ToolCall:
    name: object  # untrusted: app.ai_tools validates it before anything runs
    args: object
    call_id: str | None


@dataclass(frozen=True)
class LLMResponse:
    """kind: text | tool_calls | refused | malformed | truncated | other. The model's
    prose is deliberately not exposed: replies are rendered from templates (R-21)."""

    kind: str
    detail: str
    tool_calls: tuple = ()
    # The candidate's content exactly as received (parts, function-call ids, thought
    # signatures), to be sent back unchanged to the same model in the next iteration.
    model_content: dict | None = None
    usage: Usage = field(default_factory=Usage)
    model_version: str | None = None


def code(value, prefix=""):
    """A log- and database-safe diagnostic code (usage_event.outcome_detail)."""
    text = _CODE.sub("_", str(value).lower()).strip("_") or "unknown"
    return (prefix + text)[:64]


def valid_count(value):
    """A token count fit for a BIGINT column: a non-negative int, not a bool."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= MAX_TOKEN_COUNT)


_USAGE_FIELDS = (
    ("input_tokens", "promptTokenCount"),
    ("output_tokens", "candidatesTokenCount"),
    ("cached_input_tokens", "cachedContentTokenCount"),
    ("thinking_tokens", "thoughtsTokenCount"),
)


def parse_usage(metadata):
    """Token counts from usageMetadata. An absent count is unknown (None). A present
    but invalid one (not an int, a bool, negative, beyond BIGINT, a cache count above
    the input count) is also stored as unknown and named in Usage.invalid; nothing
    is clamped or invented."""
    metadata = metadata if isinstance(metadata, dict) else {}
    counts, invalid = {}, []
    for name, key in _USAGE_FIELDS:
        value = metadata.get(key)
        counts[name] = value if value is not None and valid_count(value) else None
        if value is not None and counts[name] is None:
            invalid.append(key)
    cached, total = counts["cached_input_tokens"], counts["input_tokens"]
    if cached is not None and total is not None and cached > total:
        # Inconsistent report: keep the input count, drop the impossible cache count.
        counts["cached_input_tokens"] = None
        invalid.append("cachedContentTokenCount")
    return Usage(**counts, invalid=tuple(invalid))


def parse_response(data):
    """Classify a generateContent response body (already JSON-decoded)."""
    if not isinstance(data, dict):
        return LLMResponse("malformed", "malformed:not_an_object")
    usage = parse_usage(data.get("usageMetadata"))
    version = data.get("modelVersion")
    version = version if isinstance(version, str) and _MODEL_VERSION.fullmatch(version) else None

    def result(kind, detail, **kwargs):
        return LLMResponse(kind, detail, usage=usage, model_version=version, **kwargs)

    feedback = data.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        return result("refused", code(feedback["blockReason"], "blocked:"))
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return result("malformed", "malformed:no_candidate")
    candidate = candidates[0]
    reason = candidate.get("finishReason")
    if reason in REFUSAL_REASONS:
        return result("refused", code(reason, "refused:"))
    if reason in MALFORMED_REASONS:
        return result("malformed", code(reason, "malformed:"))
    if reason == "MAX_TOKENS":
        return result("truncated", "truncated:max_tokens")
    if reason != "STOP":
        return result("other", code(reason or "missing", "finish:"))

    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not parts and (content is None or isinstance(content, dict)):
        # A STOP with no parts (seen live 2026-10-14 on gemini-3.5-flash-lite after tool
        # results): the model has nothing more to say. Replies are templates and tool
        # payloads, never model prose, so this ends the turn like an empty text answer.
        return result("text", "stop_empty", model_content={"role": "model", "parts": []})
    if not isinstance(parts, list) or not all(isinstance(part, dict) for part in parts):
        return result("malformed", "malformed:no_parts")
    calls = []
    for part in parts:
        if "functionCall" not in part:
            continue
        call = part["functionCall"]
        if not isinstance(call, dict):
            return result("malformed", "malformed:function_call")
        call_id = call.get("id")
        calls.append(ToolCall(
            name=call.get("name"), args=call.get("args", {}),
            call_id=call_id if isinstance(call_id, str) else None,
        ))
    if len(calls) > MAX_TOOL_CALLS:
        return result("malformed", "malformed:too_many_calls")
    model_content = dict(content)
    model_content.setdefault("role", "model")
    if calls:
        return result("tool_calls", "tool_calls", tool_calls=tuple(calls),
                      model_content=model_content)
    return result("text", "stop", model_content=model_content)


def gemini_schema(schema):
    """Our tool parameter schema (lowercase JSON types) as a Gemini Schema object."""
    out = {"type": schema["type"].upper()}
    for key in _SCHEMA_KEYS:
        if key in schema:
            out[key] = schema[key]
    if "properties" in schema:
        out["properties"] = {name: gemini_schema(sub) for name, sub in schema["properties"].items()}
        if schema.get("required"):
            out["required"] = list(schema["required"])
    if "items" in schema:
        out["items"] = gemini_schema(schema["items"])
    return out


def build_body(system, tools, contents, thinking_level=None):
    """The request body: the stable prefix (system instruction, tools, tool config)
    first, then the per-turn contents, then the generation config (thinking level;
    omitted when None)."""
    declarations = []
    for tool in tools:
        declaration = {"name": tool["name"], "description": tool["description"]}
        if tool.get("parameters") is not None:
            declaration["parameters"] = gemini_schema(tool["parameters"])
        declarations.append(declaration)
    body = {"systemInstruction": {"parts": [{"text": system}]}}
    if declarations:
        body["tools"] = [{"functionDeclarations": declarations}]
        body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
    body["contents"] = contents
    if thinking_level is not None:
        body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": thinking_level}}
    return body


@dataclass(frozen=True)
class Summary:
    """The L3 lead summary (Task 11.1): broker-facing text, checked by the caller
    against the lead's facts before use. Never logged."""

    text: str
    usage: Usage = field(default_factory=Usage)
    model_version: str | None = None


@dataclass(frozen=True)
class Transcription:
    """A Gemini transcription (Task 6.3). The transcript is untrusted user content:
    never logged."""

    speech_present: bool
    transcript: str
    language: str | None
    usage: Usage = field(default_factory=Usage)
    model_version: str | None = None


def build_transcribe_body(data, mime_type, thinking_level=TRANSCRIBE_THINKING_LEVEL):
    """generateContent body for one inline audio part (Gemini audio understanding)."""
    config = {
        "temperature": 0,
        "responseMimeType": "application/json",
        "responseSchema": TRANSCRIBE_SCHEMA,
    }
    if thinking_level:
        config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    return {
        "systemInstruction": {"parts": [{"text": TRANSCRIBE_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime_type,
                            "data": base64.b64encode(data).decode("ascii")}},
            {"text": "Transcribe this voice note."},
        ]}],
        "generationConfig": config,
    }


def parse_transcription(data):
    """A Transcription from a generateContent body; ProviderError when it is unusable
    (refused, truncated, not the requested JSON)."""
    result, usage, version = _json_candidate(data)
    if (not isinstance(result, dict) or not isinstance(result.get("speech_present"), bool)
            or not isinstance(result.get("transcript"), str)):
        raise ProviderError("malformed:transcription", failover=False)
    language = result.get("language")
    language = language.strip().lower() if isinstance(language, str) else ""
    return Transcription(result["speech_present"], result["transcript"].strip(),
                         language or None, usage, version)


def build_summary_body(system, text, thinking_level=None):
    """generateContent body of the L3 lead summary (Task 11.1): no tools, JSON only."""
    config = {"temperature": 0, "responseMimeType": "application/json",
              "responseSchema": SUMMARY_SCHEMA}
    if thinking_level:
        config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    return {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": config}


def parse_summary(data):
    """A Summary from a generateContent body; ProviderError when it is unusable."""
    result, usage, version = _json_candidate(data)
    if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
        raise ProviderError("malformed:summary", failover=False)
    return Summary(result["summary"].strip(), usage, version)


def _json_candidate(data):
    """(the JSON object a structured-output call returned, Usage, model version)."""
    if not isinstance(data, dict):
        raise ProviderError("malformed:not_an_object", failover=False)
    usage = parse_usage(data.get("usageMetadata"))
    version = data.get("modelVersion")
    version = version if isinstance(version, str) and _MODEL_VERSION.fullmatch(version) else None
    feedback = data.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise ProviderError(code(feedback["blockReason"], "blocked:"), failover=False)
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise ProviderError("malformed:no_candidate", failover=False)
    candidate = candidates[0]
    reason = candidate.get("finishReason")
    if reason != "STOP":
        prefix = "refused:" if reason in REFUSAL_REASONS else "finish:"
        raise ProviderError(code(reason or "missing", prefix), failover=False)
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise ProviderError("malformed:no_parts", failover=False)
    text = "".join(part["text"] for part in parts if isinstance(part, dict)
                   and isinstance(part.get("text"), str) and not part.get("thought"))
    try:
        result = json.loads(text)
    except (ValueError, RecursionError):
        raise ProviderError("malformed:not_json", failover=False) from None
    return result, usage, version


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _Watchdog:
    """Shuts the connection of one request down when its deadline passes.

    It shuts down only sockets it owns: a dup() of each TCP socket, taken at
    connect_tcp.complete (before TLS wraps and detaches the original). The dups are
    closed in stop(), after the timer has been cancelled and joined, so the timer
    only ever sees descriptors that are still the watchdog's own."""

    def __init__(self, seconds):
        self._lock = threading.Lock()
        self._sockets = []
        self.fired = False
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def trace(self, event, info):
        if not event.endswith("connect_tcp.complete"):
            return
        stream = info.get("return_value")
        sock = stream.get_extra_info("socket") if stream is not None else None
        if sock is None:
            return
        try:
            own = sock.dup()
        except OSError:
            # No descriptor left for the watchdog: end the call now rather than run
            # it without a deadline (it fails as a transport error).
            _shutdown(sock)
            return
        with self._lock:
            self._sockets.append(own)
            fired = self.fired
        if fired:
            _shutdown(own)

    def _fire(self):
        with self._lock:
            self.fired = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown(sock)

    def stop(self):
        self._timer.cancel()
        self._timer.join()
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            sock.close()


def _timeout(timeout_s):
    """httpx timeouts of one call. Reads and writes may use the whole deadline (the
    watchdog ends the call); connect (the TCP connect and, in httpcore, the TLS
    handshake) is capped because the TCP connect is outside the watchdog."""
    return httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_SECONDS, timeout_s))


class GeminiClient:
    """generateContent with a hard per-call deadline. transport is for tests only.

    One httpx.Client per instance, built here so its TLS setup (~0.25 s) is not
    spent inside a call's deadline; keep-alive is off, so every call opens (and
    closes) its own connection and the watchdog always sees its socket. Shared by
    the worker threads (httpx's connection pool is locked)."""

    def __init__(self, api_key, *, base_url=GEMINI_BASE_URL, transport=None):
        if not api_key:
            raise ValueError("an API key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            transport=transport, limits=httpx.Limits(max_keepalive_connections=0),
        )

    def __repr__(self):
        return "GeminiClient(<redacted>)"

    def close(self):
        self._http.close()

    def generate(self, model, *, system, tools, contents, timeout_s, thinking_level=None):
        """One attempt, finished (or abandoned and closed) within timeout_s seconds.
        Returns an LLMResponse; raises ProviderError."""
        if timeout_s <= 0:
            raise ProviderError("deadline", failover=False)
        body = json.dumps(build_body(system, tools, contents, thinking_level),
                          ensure_ascii=False).encode()
        status, raw = self._post(f"{self._base_url}/models/{model}:generateContent",
                                 body, timeout_s)
        if status == 200:
            try:
                data = json.loads(raw)
            except (ValueError, RecursionError):  # RecursionError: absurd nesting depth
                raise ProviderError("invalid_json", failover=False) from None
            return parse_response(data)
        retryable = status in (404, 408, 429) or status >= 500
        raise ProviderError(f"http_{status}", failover=retryable)

    def _post(self, url, body, timeout_s):
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        return post_with_deadline(self._http, url, timeout_s, headers=headers, content=body)

    def transcribe(self, model, data, mime_type, *, timeout_s,
                   thinking_level=TRANSCRIBE_THINKING_LEVEL):
        """Task 6.3 STT fallback: one audio transcription attempt (structured output,
        no tools). Returns a Transcription; raises ProviderError."""
        if timeout_s <= 0:
            raise ProviderError("deadline", failover=False)
        body = json.dumps(build_transcribe_body(data, mime_type, thinking_level)).encode()
        status, raw = self._post(f"{self._base_url}/models/{model}:generateContent",
                                 body, timeout_s)
        if status != 200:
            raise ProviderError(f"http_{status}", failover=False)
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            raise ProviderError("invalid_json", failover=False) from None
        return parse_transcription(payload)

    def summarize(self, model, *, system, text, timeout_s, thinking_level=None):
        """Task 11.1 L3: one lead-summary attempt (structured output, no tools).
        Returns a Summary; raises ProviderError (failover on 404 / 408 / 429 / 5xx)."""
        return _summarize(self, model, system, text, timeout_s, thinking_level)


def _summarize(client, model, system, text, timeout_s, thinking_level):
    if timeout_s <= 0:
        raise ProviderError("deadline", failover=False)
    body = json.dumps(build_summary_body(system, text, thinking_level),
                      ensure_ascii=False).encode()
    status, raw = client._post(f"{client._base_url}/models/{model}:generateContent",
                               body, timeout_s)
    if status != 200:
        raise ProviderError(f"http_{status}", failover=status in (404, 408, 429)
                            or status >= 500)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise ProviderError("invalid_json", failover=False) from None
    return parse_summary(payload)


def post_with_deadline(http, url, timeout_s, *, headers, content=None, files=None,
                       data=None, max_bytes=MAX_RESPONSE_BYTES):
    """POST under the watchdog deadline (module docstring); (status, body bytes).
    Shared by the Gemini and STT clients (app.stt). Raises ProviderError."""
    timeout = _timeout(timeout_s)
    watchdog = _Watchdog(timeout_s)
    completed = False
    try:
        with http.stream(
            "POST", url, content=content, files=files, data=data, headers=headers,
            timeout=timeout, extensions={"trace": watchdog.trace},
        ) as response:
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw += chunk
                if len(raw) > max_bytes:
                    raise ProviderError("response_too_large", failover=False)
            # A body cut short by the watchdog can look complete (EOF): the
            # deadline decides, even if the last byte arrived just in time.
            completed = not watchdog.fired
            status = response.status_code
    except httpx.TimeoutException:
        raise ProviderError("timeout", failover=True) from None
    except httpx.TransportError as exc:
        if watchdog.fired:
            raise ProviderError("timeout", failover=True) from None
        raise ProviderError(code(type(exc).__name__, "transport:"), failover=True) from None
    except httpx.RequestError as exc:
        # The other request errors (DecodingError, TooManyRedirects): no usable
        # body. Programming errors (httpx.StreamError, InvalidURL, TypeError, ...)
        # are not RequestErrors and still propagate.
        if watchdog.fired:
            raise ProviderError("timeout", failover=True) from None
        raise ProviderError(code(type(exc).__name__, "request:"), failover=False) from None
    finally:
        watchdog.stop()
    if not completed:
        raise ProviderError("timeout", failover=True)
    return status, bytes(raw)
