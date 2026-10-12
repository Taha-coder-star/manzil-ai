"""Speech service (Task 6.3, component C4; RFC 4 Voice Data Flow, R16 section 7,
Red-Team R-01 / R-02, D17 / D18).

app.orchestrator calls SpeechService.transcribe_turn for a customer turn that
passed the control gate and holds voice notes, inside the serialised turn (the
worker holds the conversation lock), before any reply is chosen. Each voice note,
in the turn's order:

1. Already done (message.stt_meta set, e.g. a resumed turn): reuse the stored
   result; nothing is paid for twice.
2. The audio (app.audio.ensure_stored): downloaded now if the fetch_audio job has
   not run yet. Unusable audio -> download_failed.
3. Pre-checks, before any paid call: < 1 s -> too_short; > 120 s -> only the first
   120 s are sent (truncated). There is no audio-level silence check: WhatsApp
   notes are near-constant-bitrate Opus (~120 ms packets of 220-330 bytes whether
   silent or not, measured live 2026-10-12), so packet sizes cannot show silence
   and no decoder is used (owner decision). Silence is Scribe's no_speech.
4. Primary STT: ElevenLabs Scribe v2 with the gazetteer keyterms (app.stt).
5. Sanity checks (check_transcript): no speech, an unsupported language, an
   implausible word rate, a script outside Latin / Arabic-Urdu / Devanagari, a
   repeated-token loop.
6. Fallback, once: Gemini audio (app.llm.GeminiClient.transcribe) on a primary
   error, timeout or garbage (rate, script, loop). Never after no_speech or
   unsupported_language, and never after a pre-check: Gemini has invented speech
   for silence (CLAUDE.md), so Scribe's verdict on silence is final. A silent note
   on which Scribe errors does reach Gemini, guarded by speech_present.
7. The result is stored on the message (transcript in the spoken script, D18; NULL
   unless transcribed) with stt_meta, and conversation.last_modality = 'voice'.
8. Every provider attempt writes one usage_event row (operation 'stt',
   audio_seconds), keyed turn:{turn}:job:{job}:{attempt}:stt:{message}:{label}.

The turn goes on with each transcribed note's text replaced by its transcript, so
the turn engine (and R-22 raw-span checks) treat it exactly like typed text
(FR-C13: one pipeline). Turn.voice holds the statuses; notice_texts turns them into
the approved templates (R-21) for notes that could not be used.

Time: each call is bounded (PRIMARY_TIMEOUT_SECONDS / FALLBACK_TIMEOUT_SECONDS) and
all of them by SPEECH_BUDGET_SECONDS per turn. This is separate from the turn
engine's 15 s budget, which starts after speech.

Never log transcripts, audio, keyterm-matched text or phone numbers: only codes,
ids and timings. Both providers failing logs an error (the C16 alert hook).
"""

import dataclasses
import logging
import re
import threading
import time
from collections import Counter

import psycopg
from psycopg.types.json import Jsonb

from app import audio
from app.config import stt_fallback
from app.db import tenant_transaction
from app.llm import DEFAULT_THINKING_LEVEL, GeminiClient, ProviderError, Usage
from app.stt import MAX_KEYTERMS, ScribeClient
from app.turn_engine import PERSISTENT_SQLSTATE_CLASSES, UsageWriteError

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
OPERATION = "stt"
MIN_DURATION_MS = 1_000
MAX_DURATION_MS = 120_000
# R16 section 7: primary timeout 6 s, then the fallback.
PRIMARY_TIMEOUT_SECONDS = 6.0
FALLBACK_TIMEOUT_SECONDS = 6.0
SPEECH_BUDGET_SECONDS = 20.0
MIN_ATTEMPT_SECONDS = 1.0
# Sanity checks (R16 section 7.3).
MAX_WORDS_PER_SECOND = 6.0
MAX_FOREIGN_SCRIPT_SHARE = 0.2
REPEAT_RUN = 5  # the same 1-4 word sequence this many times in a row is a loop
DOMINANT_TOKEN_SHARE = 0.5  # one token above this share of a long transcript
DOMINANT_MIN_TOKENS = 12
SUPPORTED_LANGUAGES = frozenset({"ur", "urd", "en", "eng", "hi", "hin"})
MIN_LANGUAGE_PROBABILITY = 0.7
STORED_TRANSCRIPT_CHARS = 4_000
REJECTED_TRANSCRIPT_CHARS = 1_000
KEYTERM_TTL_SECONDS = 600

# Statuses (stt_meta.status, Turn.voice). transcribed / truncated carry a transcript.
TRANSCRIBED, TRUNCATED = "transcribed", "truncated"
USABLE = frozenset({TRANSCRIBED, TRUNCATED})
NO_FALLBACK_REASONS = frozenset({"no_speech", "unsupported_language"})

# Approved templates (Roman Urdu, R-21; wording pending owner approval like 6.1's).
TEMPLATES = {
    "voice_retry": "Awaaz clear nahi aayi. Dobara bhej dein ya likh kar bhej dein.",
    "voice_failed": "Maazrat, awaaz samajh nahi aayi. Please apni baat likh kar bhej dein.",
    "voice_language": "Please Urdu ya English mein bolein ya likh kar bhej dein.",
    "voice_truncated": "Voice note lamba tha; sirf pehle 2 minute suni gayi hain.",
}
NOTICES = {
    "too_short": "voice_retry", "no_speech": "voice_retry",
    "download_failed": "voice_retry", "failed": "voice_failed",
    "unsupported_language": "voice_language", TRUNCATED: "voice_truncated",
}

# Domain vocabulary for keyterm biasing (R16: DHA, marla, crore, precinct, ...),
# first in the list; the gazetteer fills the rest up to app.stt.MAX_KEYTERMS.
DOMAIN_TERMS = (
    "DHA", "Bahria Town", "Clifton", "Gulshan-e-Iqbal", "PECHS", "North Nazimabad",
    "Shahrah-e-Faisal", "DHA City", "phase", "precinct", "block", "sector",
    "marla", "kanal", "gaz", "square yard", "square feet", "crore", "lakh", "hazar",
    "sawa", "saade", "paune", "dedh", "dhai", "kiraya", "kiraye", "bungalow", "kothi",
    "portion", "flat", "apartment", "plot", "office", "shop", "budget",
)
_KEYTERM = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .'&-]{0,48}")
_TOKEN = re.compile(r"\w+", re.UNICODE)
# Scribe writes audio events into the text as "(music)", "(background noise)", ...
_EVENT_TAG = re.compile(r"\([^()]{1,40}\)")
_SCRIPT_RANGES = (
    (0x0041, 0x024F), (0x1E00, 0x1EFF),  # Latin
    (0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),  # Arabic / Urdu
    (0xFB50, 0xFDFF), (0xFE70, 0xFEFF),  # Arabic presentation forms
    (0x0900, 0x097F),  # Devanagari (accepted: same language, R16 7.3)
)


def _known_script(char):
    point = ord(char)
    return any(low <= point <= high for low, high in _SCRIPT_RANGES)


def _has_loop(tokens):
    """A 1-4 token sequence repeated REPEAT_RUN times in a row, or one token
    dominating a long transcript (an STT hallucination pattern)."""
    for size in range(1, 5):
        for i in range(len(tokens) - size * REPEAT_RUN + 1):
            gram = tokens[i:i + size]
            if all(tokens[i + k * size:i + (k + 1) * size] == gram
                   for k in range(1, REPEAT_RUN)):
                return True
    if len(tokens) >= DOMINANT_MIN_TOKENS:
        _, top = Counter(tokens).most_common(1)[0]
        if top / len(tokens) > DOMINANT_TOKEN_SHARE:
            return True
    return False


def check_transcript(text, *, word_count=None, duration_ms=None, language=None,
                     language_probability=None):
    """None when the transcript is usable, else the reason code (R16 section 7.3):
    no_speech, unsupported_language, implausible_rate, bad_script or repetition."""
    tokens = [token.lower() for token in _TOKEN.findall(text or "")]
    words = word_count if word_count is not None else len(tokens)
    if not tokens or words == 0:
        return "no_speech"
    if (language and language not in SUPPORTED_LANGUAGES
            and (language_probability is None
                 or language_probability >= MIN_LANGUAGE_PROBABILITY)):
        return "unsupported_language"
    letters = [char for char in text if char.isalpha()]
    if letters:
        foreign = sum(1 for char in letters if not _known_script(char))
        if foreign / len(letters) > MAX_FOREIGN_SCRIPT_SHARE:
            return "bad_script"
    if (duration_ms and duration_ms >= MIN_DURATION_MS
            and words / (duration_ms / 1000) > MAX_WORDS_PER_SECOND):
        return "implausible_rate"
    if _has_loop(tokens):
        return "repetition"
    return None


def notice_texts(statuses):
    """The approved notices for a turn's voice statuses, each once, in order."""
    keys = [NOTICES[status] for status in statuses if status in NOTICES]
    return [TEMPLATES[key] for key in dict.fromkeys(keys)]


# --- keyterms ---------------------------------------------------------------------

_keyterm_cache = {"expires": 0.0, "terms": None}
_keyterm_lock = threading.Lock()


def _keyterm_ok(term):
    return bool(_KEYTERM.fullmatch(term)) and len(term.split()) <= 5


def build_keyterms(names, limit):
    """Domain vocabulary first, then names, deduplicated case-insensitively and
    filtered to the API rules (< 50 characters, <= 5 words, plain Latin)."""
    terms, seen = [], set()
    for term in (*DOMAIN_TERMS, *names):
        term = " ".join((term or "").split())
        key = term.lower()
        if key in seen or not _keyterm_ok(term):
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def keyterms(conn, agency_id, *, clock=time.monotonic):
    """Keyterms from the shared Karachi gazetteer (read-only for the runtime): areas,
    STT / common aliases, then sub-areas. Cached in-process for KEYTERM_TTL_SECONDS."""
    now = clock()
    with _keyterm_lock:
        if _keyterm_cache["terms"] is not None and _keyterm_cache["expires"] > now:
            return _keyterm_cache["terms"]
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        rows = conn.execute(
            """
            SELECT term FROM (
                SELECT COALESCE(display_name, name) AS term,
                       CASE level WHEN 'area' THEN 1 ELSE 4 END AS rank
                FROM location WHERE in_coverage AND level <> 'city'
                UNION ALL
                SELECT a.alias, CASE a.kind WHEN 'voice_stt' THEN 2 WHEN 'common' THEN 3
                                            ELSE 5 END
                FROM location_alias a JOIN location l ON l.id = a.location_id
                WHERE l.in_coverage AND a.kind IN ('voice_stt', 'common', 'official')
            ) t ORDER BY rank, term
            """
        ).fetchall()
    terms = build_keyterms([row[0] for row in rows], MAX_KEYTERMS)
    with _keyterm_lock:
        _keyterm_cache.update(expires=now + KEYTERM_TTL_SECONDS, terms=terms)
    return terms


# --- the service ------------------------------------------------------------------

@dataclasses.dataclass
class _Attempt:
    provider: str
    model: str
    outcome: str  # succeeded | failed (the provider call, as in usage_event)
    detail: str
    ms: int
    usage: Usage = dataclasses.field(default_factory=Usage)

    def meta(self):
        return {"provider": self.provider, "model": self.model, "outcome": self.outcome,
                "detail": self.detail, "ms": self.ms}


class SpeechService:
    """C4. primary: app.stt.ScribeClient; fallback: app.llm.GeminiClient or None."""

    def __init__(self, primary, storage, *, fallback=None, fallback_model=None,
                 thinking_level=DEFAULT_THINKING_LEVEL, clock=time.monotonic):
        self.primary = primary
        self.storage = storage
        self.fallback = fallback if fallback_model else None
        self.fallback_model = fallback_model
        # LLM_THINKING_LEVEL (Task 6.1 follow-up); None leaves it out of the request.
        self.thinking_level = thinking_level
        self._clock = clock

    def transcribe_turn(self, conn, job, turn, client):
        """The turn with its voice notes transcribed (text = transcript) and
        Turn.voice set to their statuses, in the turn's order."""
        audio_ids = [m.id for m in turn.messages if m.type == "audio"]
        if not audio_ids:
            return turn
        start = self._clock()
        with tenant_transaction(conn, turn.agency_id):
            conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            done = {row[0]: (row[1], row[2]) for row in conn.execute(
                "SELECT id, transcript, stt_meta FROM message "
                "WHERE id = ANY(%s) AND stt_meta IS NOT NULL",
                (audio_ids,),
            )}
        messages, statuses = [], []
        for message in turn.messages:
            if message.type != "audio":
                messages.append(message)
                continue
            if message.id in done:
                transcript, meta = done[message.id]
                status = meta.get("status", "failed")
            else:
                status, transcript = self._transcribe(conn, job, turn, message, client,
                                                      start)
            statuses.append(status)
            if status in USABLE and transcript:
                message = dataclasses.replace(message, text=transcript)
            messages.append(message)
        logger.info("Speech turn %s: %d voice note(s) [%s], %d ms", turn.id,
                    len(statuses), ", ".join(statuses),
                    int((self._clock() - start) * 1000))
        return dataclasses.replace(turn, messages=tuple(messages), voice=tuple(statuses))

    def _transcribe(self, conn, job, turn, message, client, start):
        began = self._clock()
        stored = audio.ensure_stored(conn, turn.agency_id, message.id, client, self.storage)
        meta = {"audio_id": str(stored.id) if stored else None}
        status, transcript = self._chain(conn, job, turn, message, stored, meta, start)
        meta["status"] = status
        meta["speech_ms"] = int((self._clock() - began) * 1000)
        self._store(conn, turn, message, status, transcript, meta)
        return status, transcript

    def _chain(self, conn, job, turn, message, stored, meta, start):
        if stored is None:
            return "download_failed", None
        data, duration = stored.data, stored.duration_ms
        meta.update(mime_type=stored.mime_type, duration_ms=duration, truncated=False)
        if duration is not None and duration < MIN_DURATION_MS:
            return "too_short", None
        if duration is not None and duration > MAX_DURATION_MS:
            cut = (audio.truncate_ogg(data, MAX_DURATION_MS)
                   if stored.mime_type == "audio/ogg" else None)
            if cut is None:
                meta["error"] = "truncate_failed"
                return "failed", None
            data, duration = cut, audio.ogg_duration_ms(cut) or MAX_DURATION_MS
            meta["truncated"] = True
        done = TRUNCATED if meta["truncated"] else TRANSCRIBED
        seconds = duration / 1000 if duration is not None else None

        terms = keyterms(conn, turn.agency_id, clock=self._clock)
        meta["keyterms"] = len(terms)
        text, reason, attempt = self._primary(data, stored.mime_type, terms, duration,
                                              start)
        self._meter(conn, job, turn, message, "primary", attempt, seconds)
        meta["primary"] = attempt.meta()
        if reason is None:
            meta.update(provider=attempt.provider, model=attempt.model)
            return done, text
        if reason in NO_FALLBACK_REASONS:
            return reason, None
        if text:
            meta["rejected_transcript"] = text[:REJECTED_TRANSCRIPT_CHARS]
        meta["fallback_reason"] = reason
        if self.fallback is None:
            return self._failed(turn, meta)
        text, reason, attempt = self._fallback(data, stored.mime_type, duration, start)
        if attempt is None:
            meta["fallback"] = None
            return self._failed(turn, meta)
        self._meter(conn, job, turn, message, "fallback", attempt, seconds)
        meta["fallback"] = attempt.meta()
        if reason is None:
            meta.update(provider=attempt.provider, model=attempt.model)
            return done, text
        if reason in NO_FALLBACK_REASONS:
            return reason, None
        return self._failed(turn, meta)

    def _failed(self, turn, meta):
        primary = (meta.get("primary") or {}).get("detail")
        fallback = (meta.get("fallback") or {}).get("detail")
        logger.error("ALERT speech failed for turn %s audio %s: primary=%s fallback=%s",
                     turn.id, meta.get("audio_id"), primary, fallback or "none")
        return "failed", None

    def _timeout(self, cap, start):
        return min(cap, start + SPEECH_BUDGET_SECONDS - self._clock())

    def _primary(self, data, mime, terms, duration, start):
        """(transcript or None, rejection reason or None, _Attempt)."""
        began = self._clock()
        provider, model = self.primary.provider, self.primary.model
        timeout = self._timeout(PRIMARY_TIMEOUT_SECONDS, start)
        if timeout < MIN_ATTEMPT_SECONDS:
            return None, "budget_exhausted", _Attempt(provider, model, "failed",
                                                      "budget_exhausted", 0)
        try:
            result = self.primary.transcribe(data, mime, terms, timeout_s=timeout)
        except ProviderError as exc:
            ms = int((self._clock() - began) * 1000)
            return None, exc.code, _Attempt(provider, model, "failed", exc.code, ms)
        ms = int((self._clock() - began) * 1000)
        text = " ".join(_EVENT_TAG.sub(" ", result.text).split())
        reason = check_transcript(
            text, word_count=result.word_count or None, duration_ms=duration,
            language=result.language_code, language_probability=result.language_probability,
        )
        if result.word_count == 0 and result.event_count:
            reason = "no_speech"  # only audio events (music, noise)
        return (text or None), reason, _Attempt(
            provider, model, "succeeded", reason or "transcribed", ms)

    def _fallback(self, data, mime, duration, start):
        """(transcript or None, rejection reason or None, _Attempt or None when there
        is no time left for an attempt)."""
        timeout = self._timeout(FALLBACK_TIMEOUT_SECONDS, start)
        if timeout < MIN_ATTEMPT_SECONDS:
            return None, "budget_exhausted", None
        began = self._clock()
        model = self.fallback_model
        try:
            result = self.fallback.transcribe(model, data, mime, timeout_s=timeout,
                                              thinking_level=self.thinking_level)
        except ProviderError as exc:
            ms = int((self._clock() - began) * 1000)
            return None, exc.code, _Attempt("gemini", model, "failed", exc.code, ms)
        ms = int((self._clock() - began) * 1000)
        if not result.speech_present:
            reason = "no_speech"
        else:
            reason = check_transcript(result.transcript, duration_ms=duration,
                                      language=result.language)
        return (result.transcript or None), reason, _Attempt(
            "gemini", result.model_version or model, "succeeded", reason or "transcribed",
            ms, result.usage)

    def _meter(self, conn, job, turn, message, label, attempt, seconds):
        key = f"turn:{turn.id}:job:{job.id}:{job.attempts}:stt:{message.id}:{label}"
        logger.info("STT call turn %s %s %s/%s: %s/%s, %d ms", turn.id, label,
                    attempt.provider, attempt.model, attempt.outcome, attempt.detail,
                    attempt.ms)
        usage = attempt.usage
        try:
            with tenant_transaction(conn, turn.agency_id):
                conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                conn.execute(
                    """
                    INSERT INTO usage_event (
                        agency_id, job_id, provider, model, operation, request_key,
                        input_tokens, output_tokens, cached_input_tokens, thinking_tokens,
                        audio_seconds, outcome, outcome_detail
                    )
                    VALUES (current_agency_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s)
                    """,
                    (job.id, attempt.provider, attempt.model, OPERATION, key,
                     usage.input_tokens, usage.output_tokens, usage.cached_input_tokens,
                     usage.thinking_tokens, seconds, attempt.outcome,
                     attempt.detail[:64]),
                )
        except psycopg.Error as exc:
            sqlstate = exc.sqlstate or ""
            if sqlstate[:2] not in PERSISTENT_SQLSTATE_CLASSES:
                raise
            logger.error("STT usage row for turn %s not written (SQLSTATE %s); turn stopped",
                         turn.id, sqlstate)
            raise UsageWriteError(f"usage_event_{sqlstate.lower()}") from None

    def _store(self, conn, turn, message, status, transcript, meta):
        if status not in USABLE:
            transcript = None
        elif transcript:
            transcript = transcript[:STORED_TRANSCRIPT_CHARS]
        with tenant_transaction(conn, turn.agency_id):
            conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            conn.execute(
                "UPDATE message SET transcript = %s, stt_meta = %s "
                "WHERE id = %s AND stt_meta IS NULL",
                (transcript, Jsonb(meta), message.id),
            )
            conn.execute(
                "UPDATE conversation SET last_modality = 'voice', updated_at = now() "
                "WHERE id = %s",
                (turn.conversation_id,),
            )


def build_speech(settings, storage):
    """The configured SpeechService, or None for STT_PROVIDER=off. Call
    app.config.check_stt_settings first."""
    if getattr(settings, "stt_provider", "off") != "elevenlabs":
        return None
    fallback = model = None
    if stt_fallback(settings) == "gemini":
        fallback, model = GeminiClient(settings.gemini_api_key), settings.llm_primary_model
    return SpeechService(ScribeClient(settings.elevenlabs_api_key), storage,
                         fallback=fallback, fallback_model=model,
                         thinking_level=getattr(settings, "llm_thinking_level",
                                                DEFAULT_THINKING_LEVEL))
