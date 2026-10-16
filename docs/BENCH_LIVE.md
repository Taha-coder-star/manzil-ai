# Benchmark runners (Task 10.3): text, scope conversations, staff, voice controls

**Status (2026-10-16): ready for review. No model run has happened.**
Benchmark run #1 is not scored yet: live runs need owner spend approval, and the voice set needs
recordings. `docs/BENCHMARK_RUN_1.md` is readiness / offline evidence only.

- Base: `main` @ `09a9ec70005cb21987c64bac2b4a1811c804fa88`, branch `feature/task-10-3-benchmark`,
  uncommitted.
- No production access, provider call, WhatsApp send, product-code change or edit to an existing
  gold item.

| Area | Files |
|---|---|
| Code | `app/bench_live.py` (new); `app/staff_bench.py` (shared scorers, `--mode`, scored denominators); `app/benchmark.py` (scope-set validator) |
| Data | `bench/scope_conversations.json` (new). Unchanged R16 files. All sha256 are of the repository bytes (CRLF read as LF, as runs record them): scope_conversations `a70137768077…09ac4ca`, customer_text `077f17e7…98f48f`, staff `76695133…7c8a40`, staff_spans `099920d8…5390cf`, voice_controls `c2b2bb0a…a56e1`, customer_voice `f9018d5d…d433579` |
| Tests | `tests/test_bench_live.py`, `tests/test_bench_live_db.py`, `tests/test_scope_bench.py` |

## 1. What runs, and what is real

Every case goes through the production path: webhook event store → worker → orchestrator →
`TurnEngine` → 7.3 requirements → 8.1 search → 9.1 renderer → customer reply path / 8.2 staff
tools / `app.speech` (controls). Each case group runs on a **fresh migrated schema** in the
dedicated loopback `manzil_schema_test` DB:
- the restricted `manzil_app` login, with RLS on;
- the demo agency seeded by `app.seed`, with fictional staff phones.

The schema is dropped afterwards. WhatsApp is a recording fake. Only the model changes between
modes:

| Mode | Model | Meaning |
|---|---|---|
| `plan` | none (no DB) | case groups and request bounds |
| `dry-run` | scripted, offline. Text / scope: the gold tool calls. Staff: the hand-written spans. Controls: no tool | **backend ceiling**; `model: null`; NOT a model score |
| `live` | production `GeminiClient` behind the spend guard | the only mode that may be called a model measurement |

| Set | Source | Grouping |
|---|---|---|
| `text` | R16 4.1, 30 items | CT-13 → CT-27 → CT-28 → CT-29 → CT-30 share one conversation; every other item has its own schema |
| `scope` | Demo Scope §6 p7 | 5 chains seeded by existing text items. Media fixture: 8 fictional photos per listing with cached WhatsApp ids, inserted by the schema owner as tests do. Production holds media for DH6-101 and PRJ-103 only |
| `staff` | R16 4.3, 20 items | one schema per item, because open drafts would feed the next item's duplicate check |
| `controls` | R16 4.2, 5 items | one schema each; recorded STT results are replayed through `app.speech` |

**Identity in `<out>/result.json`** (under `.local/bench-live/`, which is git-ignored):
- run id;
- **commit**, with its source. git is used when it can tell; otherwise `--commit` /
  `BENCH_GIT_COMMIT` (a worktree mounted into a container). A stated commit that contradicts git is
  refused, and live mode refuses an unknown commit;
- bench and source sha256 (which pin the exact code), demo hash;
- mode, models, thinking level, engine limits, repeats, concurrency (1), caps, media fixture.

**Per item:**
- extraction: `requirement_state` slots, or the stored `listing_draft`;
- end_to_end: this turn's shown listings; questions asked (turn-engine notices and search-gate
  questions); images with the listing each belongs to, and repeats;
- for position photos, `displayed_target` (the card the customer actually saw at that position);
- quote card found;
- tools called;
- every provider attempt (model, outcome, latency, tokens, estimated cost, the tool results carried
  back as name / status / error only), checked against the engine's own `usage_event` rows;
- reply texts and latency.

**Summaries:** NOT_RUN and cap-stopped items stay in every denominator as incorrect.

**End-to-end rules** (what the customer actually got; `reply_flags` is recorded on every item):

| Outcome | Passes only when |
|---|---|
| results | this turn's search shows every required ID, nothing off-criteria, no hidden listing |
| no_match | a search ran; nothing was shown; the reply is visible and carries the honest no-match wording (`render.TEXT` `no_match` / `no_match_cheaper`, either language); it contains no inappropriate template |
| clarify | no search ran; a **customer-visible** question was sent (a turn-engine or search-gate question, or question buttons); no inappropriate template. A `pending_clarification` row alone does not count |
| photos (scope) | images were sent, every one belongs to the target listing, and none repeats. A repeat is an image already sent in the conversation **or sent twice in the same reply**; both count in `photo_repeats` |
| handoff | blocked by 11.x today (section 7) |

"Inappropriate" means the failure or refusal template, the generic `customer_default` prompt, or a
"not ready" stub (`search_unavailable`, `requirements_unavailable`, `requirements_unclear`). That
rule decides no_match and clarify, as asked. For the other outcomes it is recorded in
`reply_flags` only. The photo-reply prefix (section 7) is therefore visible but not folded into the
photo score.

**Coverage** (`coverage` per repeat and for the run, in `result.json`):
- `requested`, `filtered_out` (by `--only`), `executed` (completed) and `missing` (with reason:
  `not_run`, `stopped_by_cap`, `no_recording_or_stt_result`, …);
- `partial`, `subset_complete` (every requested case completed) and `full_set_complete` (every
  case of the set requested and completed). A missing recording or STT result, a cap stop, or any
  filter prevents a full-set completion.

`result.json` is written after every repeat with `status: in_progress` and `repeats_planned`, and
at the end with `status: finished`. An unknown or empty `--only` is refused (exit 2). A run that
completes no case exits 3, even though its `result.json` is still written.

**Latency boundary:** from the inbound message being stored to the first payload handed to the
fake client. It excludes Meta's webhook delivery, the queue poll and the send to Meta, so it is a
lower bound on R16 E's "inbound → first reply". Staff segment turns are timed from the end of the
7.4 quiet window (75 s, skipped by advancing `job.available_at`). Immediate staff turns keep their
own start.

## 2. Budget controls (live)

`live` refuses to start unless it has all of:
- `--confirm-live`, `--max-calls N`, `--spend-cap-usd X`;
- `--prices-file F` with `source`, `verified_on`, `currency: USD`, a positive
  `reserve_usd_per_call`, and per-model input / cached-input / output prices;
- `GEMINI_API_KEY`;
- `--primary` and `--failover`, each with a price;
- a known commit.

Before **every** request (primary, failover and continuation attempts alike) the guard checks:
- requests started < N;
- spent + in-flight reservations + the next reservation ≤ X.

When a cap would be crossed, nothing is sent: the item becomes `stopped_by_cap`, the rest
`not_run`, and the run is marked incomplete.

**Guarantee:** the request cap is hard. The currency figure is an **estimate**: no output-token
limit is sent, thinking tokens are priced as output (an assumption), and an attempt with
unreported tokens, or a failed one, is charged its reservation. `currency_cap_guaranteed: false`
always.

## 3. Commands (from the worktree)

```sh
python -m app.benchmark                        # R16 sets + scope set validation (no DB)
python -m app.bench_live plan text|scope|staff|controls
python -m app.staff_bench                      # fixture run (backend rules, in memory)

# dry-run on the disposable DB (never alongside pytest DB runs: both set the manzil_app
# password and share manzil_schema_test)
C=$(git rev-parse HEAD)
docker run --rm --memory 1g --network container:manzil-schema-test -v "<worktree>:/workspace" \
  -w /workspace -e BENCH_GIT_COMMIT=$C \
  -e TEST_DATABASE_URL=postgresql://manzil_test@127.0.0.1:5432/manzil_schema_test \
  --entrypoint python manzil-ai:test -m app.bench_live dry-run scope --out .local/bench-live/dry-scope

# live (after owner approval, a dated prices file and a model-ID check; R16 4.4: 3 repeats)
python -m app.bench_live live text --repeats 3 --out .local/bench-live/run1-text --confirm-live \
  --max-calls 450 --spend-cap-usd <X> --prices-file <dated.json> \
  --primary gemini-3.5-flash-lite --failover gemini-3.6-flash
```

Requests per repeat (`plan`; min / typical / max):

| Set | min / typical / max |
|---|---|
| text | 30 / 60 / 150 |
| scope | 29 / 58 / 145 (8 referenced + 21 new turns) |
| staff | 20 / 40 / 100 |
| controls | 0 / 10 / 25 |

Cost is unknown until a prices file exists. The model IDs are the owner's pin and are unverified
for the account.

## 4. Demo Scope coverage added (`bench/scope_conversations.json`)

The source is Demo Scope p7:
- "Correct target for position / quote / 'Phase 8 bhi' / 'sasta' / budget / location / buy ↔ rent
  follow-ups", 95 % on **20 scripted follow-up turns**;
- "Images sent belong to the referenced property / repeats", 100 % / 0 on **Scenarios 2–3 + 10
  extra 'photos' requests** within a conversation.

| Count | Items |
|---|---|
| 20 follow-up turns | CT-27, CT-28, CT-29 (referenced unchanged) + SC-01, 03–11, 13, 16–21 |
| by kind | position 4 · quote 3 · add_location ("bhi") 2 · cheaper ("sasta") 2 · budget 4 · location 2 · purpose switch 3 |
| photo requests | Scenario 3a (CT-27) + 10 new (SC-01, 02, 03, 07, 08, 12, 14, 15, 17, 21): position, quote, focus ("is wali"), listing ID, "aur" continuation |
| setup turns (not counted) | CT-13, CT-01, CT-07, CT-04, CT-11, referenced unchanged |

**CT-30 is not referenced.** Handoff is not one of the Scope follow-up kinds, and it is blocked by
11.x (section 7).

**Gold sources and checks:**
- Every SC item carries `gold_status` and a `source` citation: R17/R18 p3 (ranking), p4 (active
  area), p6 §3 items 4–5 (purpose switch, relative money), p7 §5 (shown_item / focus / media_sent),
  p8 (follow-up table); Demo Scope p5–p6.
- `app.benchmark.check_scope` checks every item against `demo/` with the same independent R17
  filters used for the R16 sets:
  - spans are in the input;
  - `context.shown` follows the previous turn;
  - result IDs fit, with no clear match missing and no hidden listing;
  - each photo target matches its position, quote, ID, focus or continuation;
  - the counts above.
- `tests/test_scope_bench.py` proves the checks catch 12 kinds of broken gold.

**Unresolved:**
- **SC-18** ("Khareedni hai dukaan, 2 crore tak" after Clifton Block 5 + 8). The gold is
  `no_match`, but **Clifton neighbour expansion is an open owner decision**. With Blk 2/4/5 ↔ 8/9
  neighbours, CLF-102 becomes a relaxed result. Reported with and without it.
- **Notes, not unresolved:**
  - SC-04 / SC-20 assume a budget change keeps the "sasta" exclusions (R18 §5 keeps
    `excluded_property_ids` as its own slot).
  - SC-07 uses only position 1, because positions 2 and 3 tie on score.
- No second-person verification of the new gold yet.

## 5. Offline validation (2026-10-16)

- **Full suite, first round:** 3340 collected = 3340 passed, 0 failed / errors / skipped.
  - 5 sequential memory-capped batches on the disposable DB, `REQUIRE_DATABASE_TESTS=1`,
    Python 3.12.15.
  - **Not repeated** for this round. This round's changes are confined to the bench modules, the
    new data file and docs; no product module changed.
- **Review round 2 checks:** every test module importing a changed module (14 files) plus
  `test_search_db`: **927 passed**, 0 failed / errors / skipped (Python 3.12, DB).
- **Round 3 checks** (the runner findings below; only `app/bench_live.py` and
  `app/staff_bench.py` changed):
  - `test_bench_live.py`, `test_bench_live_db.py`, `test_staff_bench.py`, `test_scope_bench.py`,
    `test_benchmark.py`: **125 passed**, 0 failed / errors / skipped (Python 3.12, DB).
  - The offline subset on the host: 84 passed.
- **Final focused run** (after the run-identity hashes moved to repository bytes, CRLF read
  as LF, so Windows and Linux CI agree): the same five modules, **126 passed**, 0 failed /
  errors / skipped (Python 3.12, DB). Result files written before this change recorded
  Windows working-copy hashes.
  - `python -m app.benchmark`: valid (R16 sets + scope set).
  - Ruff (`app tests deploy`) clean; `git diff --check` clean.
- **The four test warnings** (one per batch ab, ac, ad, ae) are one third-party deprecation:
  - `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install
    httpx2 instead`.
  - It is raised when `fastapi.testclient` is imported by `test_health`, `test_ingest`,
    `test_m1_replay`, `test_privacy`, `test_runtime` and `test_webhook`. Batch aa contains none of
    these and had no warning.
  - Cause: the image resolves `fastapi 0.143.0` / `starlette 1.7.0` / `httpx 0.28.1` because
    `requirements.txt` allows `fastapi>=0.115,<1` (starlette not pinned).
  - Not caused by 10.3, not fixed here. A follow-up should pin starlette or adopt its replacement
    client.
- **Gitleaks** (repository route: CI `zricethezav/gitleaks:v8.24.2`, default config, local image):
  - `git /repo --redact --log-opts=09a9ec7…` (CI's git mode on the base): **no leaks** (75 commits).
  - `dir` mode on exactly the review files: section 11.

## 6. Dry-run results (scripted model; backend ceiling, NOT model scores)

| Set | Result |
|---|---|
| text (30) | extraction critical 27/27; price normalisation 21/21; tools 30/30; **end-to-end 23/30** (23/29 excluding blocked CT-30); clarify 1/5; unnecessary questions 0/23; hidden 0. Under the round 3 rules every no-match item (CT-18, CT-28) still sends an honest no-match reply. Full set complete, nothing missing |
| scope (20 + 11) | **follow-up targets 19/20** (18/19 excluding unresolved SC-18); every kind full except position 3/4 (CT-27); **photos 10/11**, 0 repeats (across turns and within replies); setup turns 5/5; no-match items SC-11 / SC-18 / SC-19 pass with honest wording. Photo replies carry `reply_flags.inappropriate = [customer_default]` (section 7). Full set complete |
| staff (20) | **19/20 exact**; fields 92/94; workflow 33/33 scored + 1 unscored (ST-01 duplicate_of); ST-15 fails; 0 listing writes. Denominators come from the item specifications, so an all-unrun set reports 0/20, 0/94, 0/33 (+1 unscored). Full set complete |
| controls (5) | **NOT RUN** (no recordings). A run reports every item missing (`no_recording_or_stt_result`), is never full-set complete, and exits 3. The scorer is tested on fictional audio |

## 7. Diagnoses (product unchanged; reproducible with the dry-run commands above)

| Case | Failing boundary | Source contract | Classification |
|---|---|---|---|
| **CT-09** "North Nazimabad mein 400 ka portion kiraye pe chahiye, 90 hazar tak" (gold NNZ-101) | `customer_tools.search_properties` → `render.render_search` → `grounding._check_features`. The regex `\bnorth\b` for the feature `north_open` matches the gazetteer name "North Nazimabad" in the results line. The `GroundingError` reads "Violation(kind='feature', text='North', detail='north_open is not true in a rendered listing')". The search ran (1 match), but the reply became `search_unavailable` | R-21 / D4: the validator rejects facts not in the DB; a location is a DB fact. Place names are not masked before the feature check (`turn_engine.render_customer` already notes the same misfire risk for "Sea View" in notes) | **Product defect** (grounding false positive). Any place name containing north / south / east / west / sea view can trip it |
| **CT-22, 23, 24, 26** (clarify) | The gold scripts only `update_requirements`, which answers "requirements noted". Questions come only from the 8.1 search gate (`search_properties` → `clarification_needed`). Repro with `search_properties` added: CT-22 / 24 / 26 ask the purpose question, CT-23 asks "Aap kis area mein property dekh rahe hain?" | R16 5 B: "clarifying question asked on designed-ambiguous items ≥ 90 %"; R17 2.1 gate (the search asks); the gold `expected_tools` omit the search call | **Contract gap, needs an owner decision**: the gold tool lists vs a product that asks only when searching. A live model that also calls search would pass. CT-26 gold also expects the type question or a conversion echo (R17 §3 item 2), and neither happens; the product asks purpose only |
| **CT-27** "Property 2 ki aur pictures" | The engine resolves position 2 against the cards as shown: DH6-102, DH6-101, DH7-101. It sends DH6-101's photos, which is the card shown at #2 (`displayed_target` = DH6-101) | R17/R18 p3 step 8 ranks "under budget and close to max is best", so 8.0 crore precedes 7.8; `docs/SEARCH.md` records this order. Demo Scope p5 lists DH6-101 first but judges membership, and step 3a writes "#2 · DH6-10x" | **Gold defect / unresolved expectation**: the gold's order follows the Scope listing, not R17. The product targets the right card |
| **CT-30** handoff | `request_handoff` is the 6.1 stub (`handoff_unavailable`) | D4 / R6 handoff: Task 11.x, owned by Developer 2 | **Blocked by 11.x.** Counted incorrect in `end_to_end_exact` (23/30). `end_to_end_exact_excluding_blocked` = 23/29. `handoff_recall` scores tool selection only. Not in the scope set |
| **ST-15** "Sports City wala plot ab 1.5 crore ka hai" (gold BTK-106 → 15,000,000) | `find_listings`: "Sports City" vs "Bahria Sports City" scores 0.63 < 0.85, so `location_unclear`; `propose_update` → `target_not_in_text`. Reply: "Kya aap Bahria Sports City keh rahe hain? …" | `docs/LOCATION_RESOLVER.md` auto rule; `docs/STAFF_EXTRACTION.md` §16 says "used, not asked again", §17 / code ask | **Product behaviour vs a doc conflict**; owner decision. Not fixed |

**New product defect found by the scope run.** A photo reply is prefixed with the generic
`customer_default` note ("Shukriya! Property dhoondne ke liye apna area, budget aur property ki
qisam … likh kar bhejein" / "Thanks! To find a property, tell me the area…").
- `turn_engine.render_customer` adds the note when `customer_reply.visible(payloads)` is empty, but
  image entries are internal (`customer_reply.is_internal`), so a photos-only reply counts as empty.
- Repro: any scope photo turn (e.g. SC-07). Not fixed.

**Fixture note (not a defect):** cards say "photos coming soon", because the fixture has no
`card_image_key` and no public media URL. Carousel covers are therefore not measured. The images
metric uses photo-request turns only.

## 8. Runner defects found and fixed (separate from product defects)

Round 3 (review findings):
1. no_match passed on "searched and nothing shown" alone. It now needs a visible, honest no-match
   reply without failure, default or stub templates.
2. clarify passed on a `pending_clarification` row alone. It now needs a customer-visible question
   without an inappropriate template.
3. Staff NOT_RUN / stopped_by_cap / no_draft items contributed no field or workflow checks, so the
   denominators shrank. Expected checks now come from each item's specification
   (`staff_bench.expected_checks`) and are kept as failed. ST-01's disputed assertion stays
   unscored.
4. Saved runs did not record coverage; a single `complete` flag mixed subset and full-set
   completion; an unknown `--only` id silently ran nothing. Coverage is now saved; subset and
   full-set completion are separate; unknown or empty ids are refused; a run with nothing completed
   exits 3.
5. Photo repeats were counted only across turns. Duplicates inside one reply now count too and fail
   the check.

Round 2:

6. Immediate staff turns were recorded with latency 0. They now keep their own start.
7. The question detector missed the 8.1 search-gate questions (`render.TEXT`: location / type /
   generic / which place / confirm place). It now finds them (`search_gate:*`).
8. ST-01's disputed check was excluded only when it *disagreed* with gold. It is now unscored
   whatever the result.
9. The run identity recorded `commit: unknown` in the container. It is now pinned (git, or a stated
   commit checked against git).
10. Context deviations compared order even for items that use no position (SC-10 tie). Order is now
   checked only for position items.
11. The staff disagreement text called DH6-101 "78 lakh"; it is 7.8 crore. The same wording remains
   in `tests/test_staff_codes.py` and `docs/STAFF_EXTRACTION.md` F2, both untouched.

Earlier rounds fixed: the latency percentile scale, shown items not limited to the turn's own
search, template prefix false positives, follow-up location gold, the investment slot shape,
NOT_RUN outside denominators, and metering of capped attempts.

## 9. ST-01 (disputed; only the duplicate assertion is unscored)

- **Input:** "New house DHA Phase 6, 500 yard, 4 bed 5 bath, demand 8 crore, available, owner direct"
- **Gold** (unchanged):
  - sale, house, `dha-ph6`, PKR 80,000,000;
  - 500 sq_yd (nominal 500), 4 bed, 5 bath;
  - `missing: []`, `save_allowed: true`, **`duplicate_of: []`**, `expected_code: DH6-109`.
- **Observed:** every critical field matches. The card says "Milti-julti listing pehle se hai:
  DH6-101, DH6-106".
- **Readings:**
  - Gold: Demo Scope p7 step 4a/4b card shows no duplicate line.
  - Code: RFC p6 "duplicate check (same location + size + price ± tolerance)", with
    `docs/STAFF_EXTRACTION.md` §4.4 tolerances (size ±10 %, price ±5 %). DH6-101 is 7.8 crore
    (2.5 %), DH6-106 is 7.9 crore (1.25 %, reserved). ST-02's gold flags BTK-101 at 4.5 %. Owner
    question Q1.
- **Scored denominators** (`scored_denominators`):

  | Measure | Scored | Unscored |
  |---|---|---|
  | items | 20 (ST-01 counted on its fields) | — |
  | critical fields | 94 (ST-01's 8 included) | — |
  | workflow checks | 33 | **1: ST-01 duplicate_of**, listed with `agrees_with_gold` |

## 10. Readiness matrix

| Set | Cases | Gold signed off | Runner | Runnable now | Missing for run #1 |
|---|---|---|---|---|---|
| Customer text | 30 | no sign-off | `bench_live text` | dry-run ✔ | spend approval, prices file, model check; owner decisions (section 12) |
| Scope conversations | 20 follow-ups + 11 photo turns (+5 setup) | derived from sources; SC-18 unresolved; no second reviewer | `bench_live scope` | dry-run ✔ | same, plus a reviewer for the new gold |
| Staff | 20 | no sign-off; ST-01 Q1 open | `bench_live staff` / `staff_bench --mode live` | fixture ✔ dry-run ✔ | same, plus Q1 |
| Voice (30) | 30 scripts | 0/30 verified | `stt_bench run` (7.2) | selftest / plan | recordings, manifest, verified gold, prices |
| Voice controls | 5 | n/a | `bench_live controls` | NOT RUN | recordings + a 7.2 STT run |
| Voice end-to-end (R16 B / D / E) | 30 | — | none yet | — | a voice runner over `bench_live` |

## 11. Review package

- **Review diff:** `.local/bench-live/review-10-3.diff` (git-ignored). It holds the tracked
  changes plus every new file. It leaves out `graphify-out/` (a generated graph, not part of 10.3)
  and the `.local/` results.
- **Gitleaks `dir` scan of the review files** (same image `zricethezav/gitleaks:v8.24.2`, default
  config, `--redact`), run over the 13 review files plus the diff: **no leaks found** (~475 KB).

## 12. Decisions needed (owner)

1. **CT-27 / position gold.** Should positions follow R17 ranking, or the Scope listing order? It
   changes CT-27's gold and CT-30's context.
2. **Clarify items.** Should the backend ask after `update_requirements` alone, or should the gold
   tool lists include `search_properties`? Also CT-26's type / conversion echo.
3. **CT-09.** Mask gazetteer names before the feature check. This is a product fix outside the
   baseline.
4. **Photo replies prefixed with `customer_default`.** Product fix outside the baseline.
5. **SC-18 / Clifton neighbours**, and whether a budget change clears "sasta" exclusions
   (SC-04 / SC-20).
6. **ST-01 (Q1)** and **ST-15** (resolver vs §16).
7. **CT-30.** Keep it in run #1 as blocked-and-incorrect, or move it to the cut list until 11.x.
8. **Denominators R16 leaves open:** the ambiguous subset (here the 5 clarify items), the
   false-handoff base, the latency sample.
9. **The starlette deprecation** (pin or replace the test client) and the **second-person review**
   of the new scope gold.

## 13. Recording checklist (35 voice cases)

The checklist is generated: `docs/STT_RECORDING_CHECKLIST.md` (`python -m app.stt_bench
checklist`), covering 30 items and 5 controls, S1–S6, three phone classes and the noise mix.

Order of work:
1. **Owner prep:** consent from 6 speakers; 3 phones on the Meta test list; a private sheet;
   `STT_PROVIDER=off` while recording.
2. **Record** the 35 hold-to-record notes.
3. **Export:** `app.bench_manifest` → `bench/voice_manifest.csv` + private `<item_id>.ogg` →
   `stt_bench validate`.
4. **Verify:** a non-speaker writes `bench/voice_gold_verified.json`; a second person reviews it.
5. **With approved spend:** `stt_bench run --confirm-live`, then
   `bench_live live controls --stt-run … --audio-dir …`.

## 14. Source conflicts

- `docs/STAFF_EXTRACTION.md` §16 says a similarity location is "used, not asked again"; §17 and the
  code ask.
- R16 names Claude / GPT candidates; the repo uses Gemini + Scribe (5.3/5.4 handoff decision).
- R16 E measures "inbound → first reply"; the runner's lower bound excludes Meta delivery.
- Demo Scope p5 lists scenario 2 as DH6-101, DH6-102, DH7-101; R17 p3 ranks DH6-102 first (CT-27).
- `docs/BENCHMARK.md:77` calls 10.3 "run #2"; the Execution Plan makes it run #1.
