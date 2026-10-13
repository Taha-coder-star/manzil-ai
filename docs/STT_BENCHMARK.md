# STT benchmark (Task 7.2)

Sources: R16 section 4.2 (voice set + controls), 4.4 step 1, 4.5, 5 criterion A
(`D:\manzil AI\Manzil_AI_R16_LLM_Voice_STT_Evaluation_D8_D15.pdf`), Execution Plan 7.2 (done condition:
"report shows critical-slot accuracy per vendor; primary confirmed (>= 85 %) or swapped; controls produce 0
requirements"), Demo Scope section 6, Red-Team R-02 / R-03 (purpose is a voice-critical slot), CLAUDE.md
section 1 and DECISIONS.md (Task 6.3): Gemini audio replaces GPT Transcribe; no OpenAI dependency.
Code: `app/stt_bench.py`, `tests/test_stt_bench.py`. Set and recording protocol: `docs/BENCHMARK.md`.

**Status (2026-10-13): the harness is ready; 7.2 acceptance is pending.** There are no recordings, no
`bench/voice_manifest.csv`, no verified gold and no live provider results. Mocked tests and the 6.3 smoke tests
do not count. The control gate cannot be decided by this harness (below).

## Criterion A, exactly

R16 section 5, row A: **"Voice items with all critical slots (price, size + unit, location, purpose) correctly
transcribed: >= 85 % overall; >= 90 % on quiet recordings."** Section 4.5: "critical-slot scoring is automatic:
normalised values against gold"; WER / CER are reported, not gating; "an STT candidate failing A can't be primary".

- **Unit**: one voice item, correct only if *every gold critical slot* is correct. Critical slots =
  `purpose`, `budget` (price), `size` (value + unit), `location`. Property type and beds are listed in R16
  criterion 1 but not in A; they are not scored.
- **Denominator**: all 30 voice items (CV-01..30), always. A missing recording, an error, a request that was
  never made, a bad Ogg, a timeout all count as incorrect. Nothing is dropped. Quiet = the 15 items whose
  *manifest* noise is `quiet`.
- **Items with no critical slot** (CV-27 "ghar chahiye", CV-29 a visit request, CV-30 a handoff request): there
  is nothing to match, but they are *not* free points. They stay in the 30 and pass only on **successful processing
  with no invented requirement**: a request was made and answered, the transcript is non-empty, the application
  would accept it (`app.speech.check_transcript` / `speech_present`), and it carries no price, size, location or
  purpose. They fail on: no audio, no request made (budget, missing recording), provider error, empty or
  `no_speech` output, a rejected transcript (loop, implausible rate, bad script) or any invented critical slot
  (e.g. "rent pe", "do crore", "Clifton"). Intent words (visit, agent) are not scored: R16 A has no intent slot.
- **Two numbers per provider**: the *official* result is over all 30 (this is what R16's thresholds apply to);
  the *27-item diagnostic* (items with critical gold slots only, with its quiet subset) is shown beside it and is
  never used for a verdict. Thresholds (85 % / 90 %) and the gold are unchanged.
- **Normalised values against gold**: the scorer runs the transcript through the merged deterministic code and
  compares to the gold slot values:
  - price: `normalise.parse_budget(transcript, purpose=<gold purpose>)`, compared on `mode`, `min`, `max`,
    `period` (the gold's keys except `raw`). Several amounts, or none, is a failure. The gold purpose is only used
    for the period default, so a purpose error is not charged twice.
  - size: `normalise.find_areas` (explicit unit) then `parse_area`, compared on value, unit, sqft; none or
    several different sizes fail.
  - purpose: `parse_purpose(transcript, source="voice")`; when the gold purpose is `inferred` and no word is
    spoken, `infer_purpose(<amount found in the transcript>, <gold property type category>)`. The price slot's
    period default uses the same transcript-derived purpose, never the gold purpose. The only gold input to
    extraction is the gold *property type* (a context for the residential / commercial thresholds and the default
    size unit); it is not a scored slot.
  - location: **harness rule (R16 does not define span finding)**, chosen from the transcript alone:
    `find_locations(text, gazetteer)` has no gold parameter, and a test spies on `location.resolve` to show it is
    asked the same questions whatever the gold. Every window of up to 7 words goes through `location.resolve`;
    deterministic (non-fuzzy) RESOLVED / AMBIGUOUS windows are taken longest first without overlap. A fuzzy
    AMBIGUOUS offer ("karao" -> Karachi 0.4) is not a stated place and is ignored. **Several distinct places fail**
    (`several_locations`: a wrong, an invented or an extra one); none fails (`missing`); the same place twice is one.
    Gold RESOLVED needs the same node list; gold AMBIGUOUS (CV-26) needs AMBIGUOUS with the same candidates, so a
    transcript that *adds* "Clifton" to "Block six" fails. On an item without a location slot a stated place is an
    invented requirement.
  - Event tags like `(music)` are removed from Scribe text first (the application does the same).
- **Self-check**: `python -m app.stt_bench selftest` scores each planned script as if it were the transcript:
  30 / 30. A disagreement there would be a scorer-vs-gold finding for review, never a gold edit (BENCHMARK.md).
- **Script**: Devanagari / Arabic-script output is scored as it is (the normalisers and resolver do not read it, 7.1
  decision) and is *also* counted per vendor in the report (`script`). The 6.3 live finding (Scribe writes
  Devanagari for Roman Urdu speech) therefore shows up as lost slots and as a script count, not as a hidden
  normalisation. Transliteration, `language_code=ur` for Scribe and Gemini-as-primary are variants the owner can
  ask for; they change the request settings, so they get their own cache keys.

Recording: `docs/STT_RECORDING_CHECKLIST.md` (generated by `python -m app.stt_bench checklist`; a test keeps it
current) lists the 35 recordings and how each becomes an audio file, a manifest row and a verified transcript.

## What stays separate

| Layer | Meaning | Where |
|---|---|---|
| planned script | what the volunteer was asked to say (`bench/customer_voice.json`) | gold status `planned` |
| verified transcript | what a human confirmed was said on *that* recording (sha256-bound) | gold status `verified` |
| raw vendor result | Scribe text / Gemini `{speech_present, transcript, language}` as returned | cache, criterion A score |
| application view | `app.speech.check_transcript` / `no_speech` on that raw result | `app_view` (counts per provider) |
| chain replay | the real `SpeechService._primary` / `_fallback` logic run on the recorded raw results (Scribe -> Gemini), no extra request | `pipeline_replay`, never in a vendor score |
| control transcript screen | do the raw control transcripts still carry a price / size / location / purpose? | `controls[].signals` |
| control gate | zero requirements from the extraction path | **unverified** |

Frozen gold: `bench/voice_gold_verified.json` (does not exist yet). One entry per recording:

```json
{"schema": 1, "items": {"CV-01": {"audio_sha256": "<of the recording>", "verified_by": "R1",
  "verified_on": "2026-10-20", "as_scripted": true}}}
```

A deviation is `as_scripted: false` plus `gold_transcript {roman, native}` and a full `gold_extraction`
(validated with the same slot / span checks as committed gold, `benchmark.check_slots`). The entry is rejected when
its `audio_sha256` is not the manifest's. Anything not verified is scored against the planned script and the
report says INDICATIVE. The gold file's sha256 is part of the run identity: changing it in a used output directory
is refused (start a new directory), so gold cannot drift to suit a result.

## Controls

R16 4.2 expects "graceful fallback, zero hallucinated requirements" on 2 silent / near-silent notes, 1 music, 1
Punjabi, 1 over 120 s. Demo Scope section 6 states the gate as "silence / music controls producing requirements: 0".
"Requirement" is what the requirement-extraction path produces from the transcript (turn engine + requirement state,
6.1 / 8.1, wired by 7.3 / 8.x). That path is not in this task, and a blank transcript alone is not proof (a wrong
`speech_present`, an LLM given a non-empty text, or Gemini on a silent note could all still create slots). So:

- reported per provider: raw outcome, `app_view` (e.g. `no_speech`), word count, and `signals` = which slot kinds
  the transcript still contains (a hallucination detector on text, not the gate);
- reported once: `control_gate.status = "unverified"`, with the reason;
- 7.2 is **not complete** until the control gate is run through the documented path (a follow-up once 7.3 / 8.1 can
  take a transcript and return requirement patches), or the owner records another evaluation path.

The 120 s control is sent cut to 120 s at an Ogg page boundary, as `app.speech` does. Notes under 1 s are a
pre-check outcome (`too_short`, no request).

## Providers and request settings (both are the production adapters; nothing production changes)

| | Scribe | Gemini |
|---|---|---|
| client | `app.stt.ScribeClient` | `app.llm.GeminiClient.transcribe` |
| model | `scribe_v2` | `LLM_PRIMARY_MODEL` (owner's pin: `gemini-3.5-flash-lite`; unverified for the account) |
| settings | `tag_audio_events=true`, `timestamps_granularity=word`, auto language (no `language_code`), keyterms | `temperature 0`, `LLM_THINKING_LEVEL` (default `low`, `off` omits), structured `{speech_present, transcript, language}`, `TRANSCRIBE_INSTRUCTION` |
| keyterms | domain terms + gazetteer (rank, term), <= 100, <= 5 words, Latin | none |
| version in the cache key | sha256 of the keyterm list + the `app/stt.py` source | sha256 of the request body (prompt, schema, thinking) |
| credentials | `ELEVENLABS_API_KEY` | `GEMINI_API_KEY` |

Keyterms are rebuilt from the committed demo dataset with the runtime's SQL ordering (`app.speech.keyterms`).
If the production database collates ties differently, or the Precinct 6 seed has not been applied there, the lists
differ at the 100-term cut; pass the real list with `--keyterms-file` and record it (its sha256 is in the run
identity). Both vendors' keys never reach a file, log or report. Production is untouched: no setting is read or
written; `STT_PROVIDER` stays as it is.

## Caching, retries, failures

- Cache entry key = sha256(sha256 of the audio **actually sent**, provider config key); config key = provider,
  model, settings and fingerprint. Entries repeat their inputs and are ignored when they differ. A different model,
  thinking level, keyterm list, prompt or adapter source is a different key, so a stale result is never reused.
- Only successes are cached. A failed sample is retried by the next `run` over the same directory; its earlier
  attempts stay in `journal.jsonl` (ids, codes, ms; no transcripts).
- `--max-attempts` (default 2): retried only on `timeout`, `transport:*`, `http_429`, `http_5xx`, with 1 s / 2 s ...
  back-off; other errors (401, malformed, refusal) are final at once. `--max-requests` caps total live requests; what
  it leaves undone is `not_attempted` and counts as incorrect.
- A run directory is bound to its manifest, gold, bench files, providers and attempt limit (`run.json`); a
  mismatch is refused.

## Files

`<out>/`: `run.json` (identity: commit, manifest / gold / bench sha256, provider configs), `cache/`,
`journal.jsonl`, `outcomes.json`, `private_detail.json` (per-item, **private**: carries speaker / phone labels and
scores, no transcripts, but keep it with the audio), `report.json` and `report.md` (public-safe: counts, ids, codes).
Transcripts live only in `cache/`. Default location suggestion: `.local/stt-bench/<run-id>/` (git-ignored). Audio
goes in a separate private `--audio-dir` as `<item_id>.ogg`, never in Git (DECISIONS: private bucket until
benchmark run #2).

## Usage

Unit cost is not asserted anywhere: R16's "$0.22 / audio hour" and "keyterm +$0.05/h" are marked vendor claims
there and no Gemini audio price is recorded in the repo. A cost is shown only when `--prices-file` supplies a price
with `source` and `verified_on`. The report always carries metered requests, failed requests, audio seconds sent
and Gemini tokens. Free-tier note (SPEECH.md): Scribe's free allowance is about 12 min / month; one full run sends
about 5.8 min of audio (planned targets, 347 s), a run with every retry up to 11.6 min.

## Commands

See the task report; summary: `validate` (manifest + files + gold), `selftest`, `plan` (dry run, no key needed
without a manifest), `run ... --confirm-live`, `report` (re-scores from the cache, makes no request).
