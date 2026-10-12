# Speech service (Task 6.3, component C4)

Sources: RFC section 4 (Voice Data Flow), R16 section 7 (runtime voice failure / fallback),
Red-Team R-01 / R-02 (download + STT inside the serialised turn), voice addendum V1 / V5,
D17 / D18. CLAUDE.md section 1 overrides the RFC's GPT Transcribe fallback with **Gemini
audio** and requires a **silence check before the fallback** (Gemini invented speech for
silence). Code: `app/speech.py` (orchestration), `app/stt.py` (Scribe v2 client),
`app/llm.py` (`GeminiClient.transcribe`), `app/audio.py` (in-turn download, Opus helpers).

## Flow

A customer turn that passed the control gate and holds voice notes is transcribed by
`SpeechService.transcribe_turn`, called from `app.orchestrator._answer` after mark-read
(with the typing indicator, VOICE A5) and before any reply is chosen. Staff voice is out of
scope (Demo Scope section 3) and gated turns are not transcribed (no AI output; saves free
minutes). For each voice note, in the turn's order:

| Step | What happens | Result |
|---|---|---|
| 1 | `message.stt_meta` already set (a resumed turn) | reuse; no second paid call |
| 2 | `audio.ensure_stored`: the stored object, or download now if `fetch_audio` has not run | unusable audio -> `download_failed` |
| 3 | duration < 1 s | `too_short` (no call) |
| 3 | duration > 120 s | only the first 120 s are sent (`truncate_ogg`, page boundary, EOS + CRC) -> `truncated` |
| 3 | Ogg Opus with `activity_ratio` < `MIN_ACTIVITY_RATIO` | `silent` (no call) |
| 4 | Scribe v2 (6 s), keyterms, `tag_audio_events` | |
| 5 | sanity checks | `no_speech` / `unsupported_language` end here; `implausible_rate`, `bad_script`, `repetition` and any error go to step 6 |
| 6 | Gemini audio (6 s), once, structured `{speech_present, transcript, language}` | the same checks; `speech_present=false` -> `no_speech` |
| 7 | store `message.transcript` (spoken script, D18) + `stt_meta`; `conversation.last_modality='voice'` | |
| 8 | one `usage_event` per attempt (operation `stt`, `audio_seconds`, `outcome_detail`) | |

The process_message job enqueues `fetch_audio` while it records the message, so the turn
normally runs first and downloads the audio itself (R-02); the job then finds the row
stored and does nothing. Both write the same object and only update a `pending` row.

A transcribed note's `TurnMessage.text` becomes the transcript, so the turn engine, the
history (`COALESCE(text_body, transcript)`) and the R-22 raw-span check treat it exactly
like typed text (FR-C13: one pipeline). `Turn.voice` holds the statuses.

## Replies (R-21 templates, wording pending owner approval)

| Status | Template |
|---|---|
| `too_short`, `silent`, `no_speech`, `download_failed` | `voice_retry`: "Awaaz clear nahi aayi. Dobara bhej dein ya likh kar bhej dein." |
| `failed` (both providers, or the cut failed) | `voice_failed`: "Maazrat, awaaz samajh nahi aayi. Please apni baat likh kar bhej dein." + an ERROR log `ALERT speech failed` (C16 hook) |
| `unsupported_language` | `voice_language`: "Please Urdu ya English mein bolein ya likh kar bhej dein." |
| `truncated` | `voice_truncated`: "Voice note lamba tha; sirf pehle 2 minute suni gayi hain." |

A turn with nothing usable left (no typed text, no transcript) gets only the notice and no
LLM call. Otherwise the normal reply (engine or stub) is sent, then the notice. With
`AI_PROVIDER=stub` the stub reply names the statuses: `Customer mode (stub) · 1 message
in this turn · voice: transcribed`.

## stt_meta

`status`, `audio_id`, `mime_type`, `duration_ms`, `truncated`, `activity_ratio`,
`keyterms` (count), `provider` / `model` (the one whose transcript was used),
`primary` and `fallback` (`{provider, model, outcome, detail, ms}`), `fallback_reason`,
`rejected_transcript` (the primary's text when it failed a sanity check; R16 7.7 "both
transcripts"), `speech_ms`. Transcripts are customer data (privacy deletion: OPEN_ISSUES).
Logs hold only codes, ids and timings, never transcripts, audio or phone numbers.

## Settings

| Variable | Values |
|---|---|
| `STT_PROVIDER` | `off` (default: voice gets the 6.1 "please type" template) or `elevenlabs` (needs `ELEVENLABS_API_KEY`, `S3_BUCKET`, migration 013) |
| `STT_FALLBACK` | `gemini` (needs `GEMINI_API_KEY` + `LLM_PRIMARY_MODEL`) or `off`; empty = `gemini` when both are set |

Unusable settings stop the worker with exit code 78 (never a silent fallback), as for
`AI_PROVIDER`. Speech is independent of `AI_PROVIDER`: production can transcribe with the
stub replies.

## Limits and keyterms

- Budget: 6 s primary + 6 s fallback, at most `SPEECH_BUDGET_SECONDS` (20 s) of STT per
  turn; separate from the turn engine's 15 s. R16: STT p90 <= 3 s for a 30 s note, voice
  end-to-end p90 <= 10 s; measure in the benchmark.
- Keyterms: the domain vocabulary (DHA, marla, crore, sawa, precinct, ...) first, then the
  shared gazetteer (areas, `voice_stt` / `common` / `official` aliases, sub-areas), Latin
  script only, < 50 characters, <= 5 words, cached 10 minutes, **capped at 100**: above
  100, Scribe bills every request at least 20 s, which the free tier (~12 min / month)
  cannot afford. The 35 benchmark notes alone use most of that month.
- Sanity thresholds (`app/speech.py`): > 6 words / s; > 20 % letters outside Latin /
  Arabic-Urdu / Devanagari; a 1-4 word sequence 5 times in a row, or one token > 50 % of a
  12+ token transcript; a detected language outside ur / en / hi with probability >= 0.7.

## Silence pre-check (calibration pending)

No decoder is used (owner decision 2026-10-12: no new dependency). `audio.activity_ratio`
is the share of Opus packets larger than `SILENT_PACKET_BYTES` (10); a note below
`MIN_ACTIVITY_RATIO` (0.05) is `silent`. Both values are **provisional**: they only catch
clearly empty notes. Scribe returning no words (or only audio events) is the second guard
and also never reaches Gemini. The residual risk: a silent note on which Scribe *errors*
goes to Gemini, guarded only by `speech_present` and the instruction.

Calibrate when the controls are recorded (Day 7): for each stored `silent`, `near_silent`
and a few quiet speech notes, read `stt_meta.activity_ratio` (every processed note records
it), or compute it offline with `audio.activity_ratio(open(path, "rb").read())`. Then set
the threshold between the controls and the quietest speech and record the values here.

## Live acceptance (team phones only; free tiers may train on data)

1. `STT_PROVIDER=elevenlabs`, restart the worker. Cust B sends a voice note:
   `stt_meta.status=transcribed`, `provider=elevenlabs`, transcript stored; the stub reply
   ends `voice: transcribed`.
2. Forced primary error: set an invalid `ELEVENLABS_API_KEY`, restart, send a note:
   `stt_meta.primary.detail=http_401`, `provider=gemini`. Restore the key and restart.
3. Silent control: "Awaaz clear nahi aayi. Dobara bhej dein ..."; no `stt` usage row when
   the pre-check catches it (`status=silent`), or a single Scribe row (`no_speech`) and no
   Gemini row.

Out of scope here: the voice-critical echo-confirm (10.2), voice-then-text ordering tests
(12.2), the fault-injection suite (13.3), the STT benchmark (Day 8), staff voice.
