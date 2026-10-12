"""Speech-to-text provider client (Task 6.3, component C4): ElevenLabs Scribe v2.

POST /v1/speech-to-text (multipart) with model_id scribe_v2, the gazetteer keyterms
and tag_audio_events (so music / noise come back as audio events, not words). No
language_code: speech is code-mixed, so the language is auto-detected (API
reference read 2026-10-12). Keyterms: at most MAX_KEYTERMS, because more than 100
makes every request bill at least 20 s, which the free tier (~12 min / month)
cannot afford; each under 50 characters, at most 5 words, none of <>{}[]\\.

The call runs under the same wall-clock deadline as the Gemini adapter
(app.llm.post_with_deadline). The key goes in the xi-api-key header, never in a URL
or a log. Errors carry only sanitised codes (app.llm.ProviderError); transcripts are
never logged.
"""

import json
from dataclasses import dataclass

import httpx

from app.llm import ProviderError, post_with_deadline

SCRIBE_BASE_URL = "https://api.elevenlabs.io/v1"
SCRIBE_MODEL = "scribe_v2"
MAX_KEYTERMS = 100
MAX_RESPONSE_BYTES = 2_000_000
_FILE_NAMES = {
    "audio/ogg": "voice.ogg", "audio/mpeg": "voice.mp3", "audio/mp4": "voice.m4a",
    "audio/aac": "voice.aac", "audio/amr": "voice.amr",
}


@dataclass(frozen=True)
class ScribeResult:
    """A Scribe transcription. text is untrusted user content: never logged."""

    text: str
    language_code: str | None
    language_probability: float | None
    word_count: int  # tokens of type "word"
    event_count: int  # tokens of type "audio_event" (music, noise, ...)


def parse_scribe(data):
    """A ScribeResult from the response body; ProviderError when unusable."""
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise ProviderError("malformed:no_text", failover=False)
    words = data.get("words")
    words = words if isinstance(words, list) else []
    kinds = [word.get("type") for word in words if isinstance(word, dict)]
    language = data.get("language_code")
    language = language.strip().lower() if isinstance(language, str) else ""
    probability = data.get("language_probability")
    if isinstance(probability, bool) or not isinstance(probability, int | float):
        probability = None
    return ScribeResult(
        text=data["text"].strip(), language_code=language or None,
        language_probability=float(probability) if probability is not None else None,
        word_count=kinds.count("word"), event_count=kinds.count("audio_event"),
    )


class ScribeClient:
    """One Scribe v2 attempt per call. transport is for tests only."""

    provider = "elevenlabs"
    model = SCRIBE_MODEL

    def __init__(self, api_key, *, base_url=SCRIBE_BASE_URL, transport=None):
        if not api_key:
            raise ValueError("an API key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            transport=transport, limits=httpx.Limits(max_keepalive_connections=0),
        )

    def __repr__(self):
        return "ScribeClient(<redacted>)"

    def close(self):
        self._http.close()

    def transcribe(self, data, mime_type, keyterms, *, timeout_s):
        """Returns a ScribeResult; raises ProviderError (code http_<status>, timeout,
        transport:..., malformed:...). Speech (app.speech) tries the fallback on any
        of them, whatever the failover flag says."""
        if timeout_s <= 0:
            raise ProviderError("deadline", failover=False)
        fields = {
            "model_id": SCRIBE_MODEL,
            "tag_audio_events": "true",
            "timestamps_granularity": "word",
        }
        terms = list(keyterms)[:MAX_KEYTERMS]
        if terms:
            fields["keyterms"] = terms
        status, raw = post_with_deadline(
            self._http, f"{self._base_url}/speech-to-text", timeout_s,
            headers={"xi-api-key": self._api_key},
            files={"file": (_FILE_NAMES.get(mime_type, "voice.bin"), data, mime_type)},
            data=fields, max_bytes=MAX_RESPONSE_BYTES,
        )
        if status != 200:
            raise ProviderError(f"http_{status}", failover=True)
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            raise ProviderError("invalid_json", failover=True) from None
        return parse_scribe(body)
