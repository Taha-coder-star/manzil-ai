# Day13.3 and 13.4 execution

Implementation is available; formal acceptance is **pending**. No paid calls,
new deployment, merge or AWS start/stop has been performed by this implementation.
Day13.1/13.2 remains deployed at `c42e91b03f6e` / schema020. Applied SQL001-020
is unchanged. The owner's read-only EC2 preflight confirmed both services use
`gemini-3.5-flash-lite`, failover `gemini-3.6-flash`, effort `low`, ElevenLabs STT
and Gemini STT fallback. It also confirmed the deployed services have no shared
budget guard. Do not run the paid cohort on that image under a claimed hard cap.

## Recorded scope and gold decisions

- Keep Gemini. Provider budget US$10: tuning/collection $2, formal $7, retries $1.
- Full formal run; 30 text, 30 voice, five controls, 20 staff, Scope follow-ups
  and photos, three independent repeats. The actual Scope harness has 29 turns:
  eight referenced text cases plus 21 additional cases, covering 20 follow-ups
  and ten extra photo requests. Setup is retained and separately identified.
- At least six speakers, three actual Android phones, 15 quiet / 10 moderate /
  five street speech notes. iPhone is unavailable by the owner's explicit
  exception. Android and Web evidence do not establish iPhone compatibility.
- The owner approved SC-18 using the current neighbour graph (`no_match`),
  without adding Clifton edges. ST-01 now expects duplicate warnings for
  DH6-101/DH6-106 under the RFC rule; Save remains allowed. These decisions were
  made before paid results; see `bench/day13_gold_decisions.json`.
- Fresh worker validation caught CV-30's obsolete `agent_request` tool reason.
  Gold now uses the existing production enum `agent` for the same intent; the
  offline benchmark validator rejects invalid handoff arguments before any spend.
- No always-on voice echo or Sonnet fallback has been adopted. A measured fallback
  needs an explicit owner decision. `off` omits thinking configuration; it does
  not disable model thinking. Compare supported `low` and `minimal` first.

## Collection and freeze

From Windows CMD, from the repository root:

```bat
.venv\Scripts\python.exe -m app.day13_bench pack --out ../output/day13-recordings
```

The pack contains 35 assignments, 12 paired text references, recording and
verified-gold templates, a review queue and a read-only runtime preflight. It
contains no audio and no fabricated verified transcripts. Use its README to
collect actual WhatsApp voice notes, export their stored metadata with
`app.bench_manifest`, and privately retrieve exact Ogg bytes as `<item_id>.ogg`.
Keep private speaker/phone mappings, audio, storage keys, transcripts and DSNs
outside Git. The pipeline must have bounded providers before collecting paid
STT/model traffic; the collection phase uses the same $2 tuning allocation.

Listen to every recording and bind verified gold to its SHA256, reviewer and
date. If it deviates, reconcile the committed gold/tools/results against the
actual recording before freezing, then confirm it is as scripted against that
reviewed version. Planned scripts never count as verified transcripts. Add the
accepted 81-second item's ID to `review.long_note_exceptions`; retain it for
quality and diagnostic latency. Do not replace it solely for duration.

After the full behavior review, fill the private review JSON and run:

```bat
.venv\Scripts\python.exe -m app.day13_bench freeze --review PRIVATE/review.json --manifest PRIVATE/voice_manifest.csv --verified-gold PRIVATE/voice_gold_verified.json --out PRIVATE/freeze.json
```

Freeze pins public gold/threshold/decision files and the private recording
manifest/gold hashes. Actual audio bytes, duration, size, Ogg format and real
webhook provenance are checked again before every paid voice run. A changed
source, model, effort, gold or fixture starts another cohort; do not pool it into
the formal repeats. CV-29 gets its documented CT setup conversation through real
model turns; setup costs remain in inquiry totals and the shared budget.

## Provider budget and prices

`app.bench_budget` uses SQLite `BEGIN IMMEDIATE`, integer microdollars and durable
pre-request reservations. Every Gemini generation, continuation, failover,
transcription and lead summary shares the ledger with Scribe. Unknown/failed
requests retain their reservation; crashes never reset spend. Exact reported
tokens release unused reservations. Missing cached/thinking metadata remains
unknown; conservative charges use full input price / the combined output cap,
not fabricated zero usage. Ledger rows distinguish those upper estimates from
fully priced metadata. A reported bound violation freezes all later requests.

Byte admission limits reject oversized requests. Optional `countTokens` checks
the identical request against a lower input admission ceiling. It is disabled in
the template to avoid adding a provider round trip to each measured turn.
Currency reservation in either mode uses the
**full verified model input context limit**, not an arbitrary margin around a
token-count estimate, plus the enforced combined output/thinking cap. One default
candidate must be verified; `candidateCount` is omitted for Gemini 3.x. Built-in
paid tools and explicit cached content are refused. The application uses custom
functions and implicit caching. No prompt padding is added.

Fill a dated policy from `prices.template.json`, including applicable account
rates/taxes, context limits, model effort support, billing aliases if returned
model versions differ, output cap, Scribe duration rounding/minimum and keyterm
surcharge. Use the highest applicable rate, not free-tier credits. Policy rates
must be verified within 14 days. No paid dispatch is allowed with an incomplete
policy. `prices-draft.json` in output is research only until account verification
and remaining fields are completed.

Sources checked 2026-11-10: [Gemini prices](https://ai.google.dev/gemini-api/docs/pricing),
[Flash-Lite limits](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite),
[Flash limits](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash),
[thinking support](https://ai.google.dev/gemini-api/docs/thinking),
[implicit caching](https://ai.google.dev/gemini-api/docs/caching),
[token counting](https://ai.google.dev/gemini-api/docs/tokens),
[Scribe pricing](https://elevenlabs.io/pricing/api),
[Scribe API](https://elevenlabs.io/docs/api-reference/speech-to-text/convert).
Flash-Lite cache eligibility and account-specific pricing must be confirmed;
do not infer a minimum from a different model. Provider account spending outside
these guarded clients is outside the ledger and must be accounted for separately.

### Optional deployed cohort guard

Normal runtime behavior is unchanged unless explicitly enabled. A new tested image
must be deployed before using these controls; the existing image cannot acquire
them through environment changes alone. Bind the **same host directory** at
`/day13` in both web and worker, writable by appuser. Do not use two container-local
copies or reuse different ledgers between local and EC2 runs. Run paid phases
serially in one place or move the authoritative ledger while all other clients
are stopped; copying an active ledger forks its budget.

```text
DAY13_BUDGET_POLICY=/day13/prices.json
DAY13_BUDGET_LEDGER=/day13/budget.sqlite
DAY13_BUDGET_PHASE=tuning
DAY13_GOLD_FREEZE=/day13/freeze.json
```

Collection before audio gold exists uses `DAY13_BUDGET_COLLECTION=true` and phase
`tuning` only. Disable collection mode for scored runs. Pricing/bounds remain fixed
in the ledger; updating its gold hash between reviewed cohorts keeps all earlier
spend. Partial settings, missing models or invalid policy refuse startup. Verify
both services, image/config, budget flags and shared bind-mount identity with
`deploy/day13_preflight.py` before any paid cohort. The benchmark source fixtures
are public files included explicitly in Docker; private inputs are mounted.

The opt-in deployment file is `deploy/compose.day13-budget.yml`. Once the tested
image and verified private policy are installed, use both Compose files on every
recreation while the cohort is active:

```sh
export DAY13_BUDGET_DIR=/home/ubuntu/day13-budget
export DAY13_BUDGET_PHASE=tuning
export DAY13_BUDGET_COLLECTION=true
docker compose -f deploy/compose.yml -f deploy/compose.day13-budget.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker
```

Keep the override on subsequent restarts; omitting it would remove enforcement.
Do not activate this with the incomplete draft policy. Shipping the new code can
precede activation and preserves the existing Gemini/low settings.

## Tuning and formal runner

Use a disposable loopback `manzil_schema_test` database through
`TEST_DATABASE_URL`. Never give `bench_live` the production database. WhatsApp is
a fake recording client in this harness. Set API keys privately in the process
environment. Example (POSIX shell; replace private paths and commit):

```sh
python -m app.bench_live live text --out PRIVATE/low-text --only CT-01,CT-13,CT-25 \
  --primary gemini-3.5-flash-lite --failover gemini-3.6-flash \
  --thinking-level low --stt-fallback gemini-3.5-flash-lite \
  --confirm-live --max-calls 30 --day13-policy PRIVATE/prices.json \
  --budget-ledger PRIVATE/budget.sqlite --phase tuning --freeze PRIVATE/freeze.json
```

Run the same paired text/voice sample with `minimal`, changing no other variable.
Voice adds `--manifest`, `--verified-gold`, `--audio-dir`; it invokes fresh Scribe
and Gemini calls through production SpeechService/worker, never an STT replay.
Use `app.day13_bench compare --baseline .../result.json --candidate .../result.json
--out .../comparison.json`. It checks cohorts and reports quality, requests,
provider time and backend latency without adopting a setting. Choose effort only
after reviewing quality and cost; no safety/ranking/requirements policies change.

For the frozen candidate, run `text`, `voice`, `controls`, `staff`, `scope` with
`--phase formal --repeats 3` and the **same** policy, ledger, models, effort, source
and frozen gold. Supply the STT fallback ID even on text/staff/scope so identity
matches. Budget exhausted means incomplete; never reset the ledger or remove
unexecuted items. The old `--prices-file/--spend-cap-usd` route remains for legacy
workflows and does not qualify as a Day13 shared-budget formal run.

## Deployed timing and cost evidence

Run a separate real WhatsApp text/voice cohort on the exact frozen image/config.
Use the same guarded ledger. Backend fake-client measurements exclude queue poll
and Meta transport and cannot establish the SLA. Export with the restricted login:

```sh
python -m app.day13_trace --agency-id PRIVATE_UUID --mapping PRIVATE/mapping.csv \
  --candidate PRIVATE/candidate.json --out PRIVATE/traces.json
```

Private mapping columns: `turn_id,item_id,repeat,modality,kinds,inquiry_id`.
`kinds` classifies **every** outbound row in creation order as semicolon-separated
`echo`, `notice` or `substantive`. Check classification against the conversation;
a clarification is substantive, a generic acknowledgement is not. `inquiry_id`
uses I1..I9999, without phone numbers. Export is a read-only tenant transaction;
it emits no phone numbers, raw UUIDs, message bodies, prompts or transcripts.
It includes turn and linked handoff-summary metering rows.

Primary latency: earliest persisted inbound event receipt to first substantive
Meta acceptance recorded in `outbound_message.sent_at`. Also report last receipt,
last substantive, client-visible and delivery times where measured. Database
acceptance and a screenshot do not establish first client visibility. Missing
stage timings (queue/model+tools/DB/render/outbound) remain NULL; assembly and STT
are exported where persisted. Use guarded-provider ledger durations to investigate
provider time; do not present inferred zeroes as measurements.

Nearest-rank p50/p90 include failures/missing replies as infinite observations,
with their counts retained. Voice <=30 s is the SLA cohort; longer notes and
controls remain separate diagnostics. Three repeats are reported separately,
with mean/worst-repeat gate values. Deployed per-turn/inquiry costs take priority
over backend costs when trace metering is supplied. Token metadata gaps use
explicit conservative upper estimates; completely missing metering is unknown.

## Acceptance report and remaining human evidence

Populate `bundle.template.json` with exact candidate commit/image/config hash,
freeze, runtime preflight JSON, the five result.json paths, traces, pricing policy and authoritative
ledger. Paths resolve relative to the bundle. Add supplemental metrics for gates
requiring human review (aliases, assertion/fact safety, wrong-property/takeover,
lead-card completeness, next-query price/status, schema and language). Each of
three observations needs `repeat`, `reviewed_by`,
`coverage: "all_applicable_cases"`, numerator/denominator (or a measured value),
and existing artifact paths with SHA256. Supplemental values cannot replace
automatic gates. Artifacts remain private; reports export counts only.

Android rehearsal JSON needs the same candidate, `consecutive: true`, all five
scenarios for repeats1-3 with `device: "android"`, `passed: true`, reviewer and
hash-checked artifacts; plus `web_carousel_passed` and `web_artifacts`. iPhone is
always shown as untested. Cost/latency targets and all other gates are versioned
in `bench/day13_thresholds.json`; exact denominators are retained in JSON.

```bat
.venv\Scripts\python.exe -m app.day13_bench report --bundle PRIVATE/bundle.json --out ../output/day13-run2
```

Outputs: `report.json`, `report.md`, sanitized `traces.csv`. Incomplete exits3;
invalid evidence exits2. A complete deployment, dry run, partial set, replayed
STT, mixed source/model, missing billing/coverage or missing rehearsal cannot
silently close Day13. A measured non-safety gap may be recorded in a separate
decision list: gate, owner, rationale, `source_permission: "Day13 task 13.4"`,
`adopted: true` and hash-checked decision evidence. It remains visible as an
accepted gap; unmeasured gates and safety failures cannot be cut. No fallback is
adopted from an empty decision file. Closure is explicitly with recorded
exceptions, never an unqualified all-device Scope pass.
