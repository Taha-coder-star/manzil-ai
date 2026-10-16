# Benchmark Run #1 (Task 10.3) — READINESS / OFFLINE EVIDENCE ONLY (no model run yet)

**Status: INCOMPLETE.** Offline/fixture baseline only. No model, STT or provider run has happened.
Task 10.3 is not complete: the live text and staff runs, and the whole voice set, are still NOT RUN.

> Update 2026-10-16: the runners for the live text run, staff live mode and the voice-control gate now exist (`app/bench_live.py`). Their dry-run results, the updated readiness matrix and the open decisions are in `docs/BENCH_LIVE.md`. This file stays as the 2026-10-16 readiness evidence; the triage list below is extended there (CT-09 render_failed, the clarify items, CT-27 order).

- Commit: `09a9ec70005cb21987c64bac2b4a1811c804fa88` (main, merge of PR #42), branch `feature/task-10-3-benchmark`.
- Date: 2026-10-16. Run by: Claude coordinator + 3 read-only investigation agents.
- Bench file sha256:
  - `bench/customer_text.json` e658bd2b82c55c19a9d55f636539adfe9ad169c5061149258d3f29e12dc73720
  - `bench/customer_voice.json` 6ec276586de7fdda0fac8c4b3f3b3af971744ac95107d910447dbe63e97181ae
  - `bench/voice_controls.json` 3175ad9ef12cbe8b979f56cf24e0722436fff9202e745790f0d3942203ba5109
  - `bench/staff.json` ac7281424f33029380b5301ec3cfdbbe1b7b361dd7bf4809522519c8e823c25e
  - `bench/staff_spans.json` 59ccc7dad22850ef639d7b2be198b953abcce3fccd04fb028298b8398ecc660e
- No production, AWS, Meta/WhatsApp or paid provider access. Database runs used only the disposable local `manzil-schema-test` container.

## 1. Acceptance criteria (sourced)

**What 10.3 has to produce.** Execution Plan p7, row 10.3: "Benchmark run #1 (text, voice, staff) → triage list | Both | 10.1, 10.2, 9.2 | Report vs R16 / Scope thresholds produced; every failing category has a ticket | PAR".
- Run #1 is not a pass/fail gate.
- The gate is run #2 on Day 13 (p8): "All Scope §6 thresholds met, or the remaining gaps are on the cut list with a decision recorded". Gate M6 depends on run #2.

The thresholds come from R16 §5 (p6), `D:\manzil AI\Manzil_AI_R16_LLM_Voice_STT_Evaluation_D8_D15.pdf`, and Demo Scope §6 (p7–9).

| Set | Metric | Threshold | Denominator |
|---|---|---|---|
| Text | Critical-field exact match (B) | ≥ 95 % | 30 customer texts |
| Text | Price normalisation exact | ≥ 98 % | price items (≥ 12) |
| Text | Clarify on designed-ambiguous items / unnecessary questions | ≥ 90 % / ≤ 10 % | "ambiguous subset" (not defined, see §6) |
| Text | Acceptable result set (D) | ≥ 90 % | 30 |
| Text | Latency (E) | p50 ≤ 4 s, p90 ≤ 7 s | "all runs" |
| Voice | A: all critical slots transcribed | ≥ 85 % overall, ≥ 90 % quiet | 30 notes (15 quiet) |
| Voice | A: hallucinated content on controls | 0 | 5 controls |
| Voice | Language/script usable | ≥ 95 % | 30 |
| Voice | B: end-to-end exact match | ≥ 90 %, gap to paired text ≤ 7 points | 30 (12 paired) |
| Voice | Acceptable result set | ≥ 85 % | 30 |
| Voice | Latency | p50 ≤ 7 s, p90 ≤ 10 s; STT step alone p90 ≤ 3 s | all runs |
| Staff | Drafts correct | ≥ 95 % (repo rule: items_exact/20, so ≥ 19) | 20 staff messages |
| Staff | Listing write without confirmation / update applied to wrong property | 0 / 0 | 20 |
| All | Schema-valid 100 %; correct tool ≥ 95 %; invented IDs/prices 0; hidden listings shown 0; handoff recall ≥ 95 %, false handoffs ≤ 5 %; reply language ≥ 95 % | — | per set |
| All | Cost | ≤ $0.06 text turn, ≤ $0.07 voice turn, ≤ $0.60 per inquiry (R16); "tracked, not gating" (Scope) | — |

- **Procedure** (R16 §4.4–4.5): run each LLM step 3 times and report the mean and the worst run. The 1–5 tone rubric uses 2 raters and 20 samples, with no threshold. WER/CER is reported but does not gate.
- **Repo harness rules** (no source PDF backs these):
  - Voice A is scored over all 30 items. A missing recording or an error counts as wrong (`docs/STT_BENCHMARK.md:20-35`).
  - Without sha256-bound verified gold the result is INDICATIVE (`docs/STT_BENCHMARK.md:81-92`).
- **M3** (Execution Plan §8 p12) reads: "Scenario 2 passes: transcript critical slots correct, result set {DH6-101, DH6-102, DH7-101}, Roman Urdu reply, ≤ 10 s".
  - Scenario 2 (Scope p6) is a fresh voice note, "…500 gaz ka ghar … aath crore tak". The ≤ 10 s is measured from receipt.
  - The owner's pass used a reused conversation. It does not show a fresh customer, the DB linkage or latency. **M3 is not passed.**

## 2. Dataset / readiness matrix

| Set | Required (R16 §4) | Gold items present | Gold verified | Recordings | Live harness | Runnable now |
|---|---|---|---|---|---|---|
| Customer text | 30 | 30 (CT-01..30), mix matches §4.1 | no sign-off record (hand-derived, commit d7e8972) | n/a | **none** (validator and backend gold checks only) | offline backend checks only |
| Customer voice | 30 (≥ 6 speakers, 3 phones, 15/10/5 noise) | 30 planned scripts (CV-01..30), plan meets matrix | **0/30** (`voice_gold_verified.json` absent) | **0/30** (`voice_manifest.csv` absent) | `app.stt_bench run --confirm-live` exists | selftest/plan only |
| Voice controls | 5 | 5 (VC-01..05) | 0/5 | **0/5** | zero-requirement evaluation path is "unverified" | no |
| Staff | 20 (6/3/4/2/3/2) | 20 (ST-01..20), mix matches §4.3 | no sign-off; ST-01 `duplicate_of` disputed (Q1) | n/a | **fixture only**, `run(mode="live")` raises `BenchError` | fixture |
| Scope extras | 20 scripted follow-up turns; Scenarios 2–3 + 10 photo requests | 4 follow-ups (CT-27..30), 1 photo item | — | — | none | **missing** |

Case isolation:
- Each staff fixture item gets fresh draft context.
- Customer chains CT-13→27→28→29 and CT-30 (with `shown`) carry their state as gold data only. A live runner needs a fresh conversation per item, plus chain replay.
- The DB gold sweep reuses one conversation (`requirement_version=1`). It is fine for checking search rules, but it is not a per-case isolated run.

## 3. Results (offline / fixture only — not model scores)

| Check | Result |
|---|---|
| `app.benchmark` validator (counts, coverage minimums, spans, listing IDs vs `demo/`, hidden listings, phone-like digits) | valid; control 5, text 30, voice 30, staff 20 |
| Pure bench tests: `test_benchmark`, `test_staff_bench`, `test_location_bench`, `test_normalise_bench` | 216 passed (agent, Python 3.14 venv) |
| Bench batch on disposable PG, `REQUIRE_DATABASE_TESTS=1` (above four + `test_stt_bench` + `test_voice_flow_db`) | **357 passed**, 0 skipped, 7m17s |
| `test_search_db.py` (includes gold search sweep over all bench search slots) | **12 passed** |
| Staff fixture (`app.staff_bench`), hand-written spans → real normalisers/resolver/duplicate rules | 19/20 exact (draft 11/11, update 5/6, disambiguate 3/3), fields 92/94, workflow 33/33. **Fail: ST-15 `location_unclear`.** Disagreement: ST-01 duplicate_of |
| Voice `stt_bench selftest` (scripts vs own gold, scorer check) | 30/30 |
| Voice `stt_bench validate` | fails: `voice_manifest.csv not found` (expected) |

**NOT RUN** (these stay in the denominators):

| Item | Count | Why |
|---|---|---|
| Text, live model | 0/30 | no runner, no spend approval |
| Text: latency, cost, 3× repeat, tone rubric | 0/30 | same |
| Voice A (STT) | 0/30 scored, 0/30 recorded | no recordings, no verified gold |
| Voice B/D/E end-to-end | 0/30 | same |
| Controls | 0/5 | no recordings; gate evaluation unverified |
| Staff, live model | 0/20 | live mode not implemented |
| Scope follow-ups / photos | 0/20, 0/10+ | items not in the bench |

## 4. Reproducible commands (from the worktree at 09a9ec7)

```sh
# offline (keys and DATABASE_URL unset)
python -m app.benchmark
python -m app.staff_bench --out <private>/staff_fixture.json
python -m app.stt_bench selftest
python -m app.stt_bench plan
python -m app.stt_bench validate            # fails until the manifest exists
```

```powershell
# DB batches, one at a time, disposable container only (see local test recipe)
cmd /d /c 'docker run --rm --memory 1g --network container:manzil-schema-test -v "<worktree>:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e TEST_DATABASE_URL=postgresql://manzil_test@127.0.0.1:5432/manzil_schema_test -e REQUIRE_DATABASE_TESTS=1 --entrypoint python manzil-ai:test -m pytest -q -p no:cacheprovider tests/test_benchmark.py tests/test_staff_bench.py tests/test_location_bench.py tests/test_normalise_bench.py tests/test_stt_bench.py tests/test_voice_flow_db.py'
# then the same with tests/test_search_db.py
```

## 5. Provider-run plan (needs owner approval; nothing executed)

| Run | Calls | Cost |
|---|---|---|
| Voice STT (A): `stt_bench run --confirm-live --max-requests 140 --out .local/stt-bench/run1` | 35 Scribe + 35 Gemini audio = 70 min, 140 max with retries; 347 s audio per provider | Scribe ≈ $0.03 (R16 vendor claim, $0.22/h + $0.05/h keyterms, unverified); Gemini audio price not in repo, so it stays **unknown** until a dated `--prices-file` exists |
| Text live (B/C/D/E) | ≈ 30 × 1–3 rounds + chain replay ≈ 36–120 primary calls; × 3 repeats per R16 ≈ 110–360; failover can double it | unknown (no Gemini price in repo) |
| Staff live | 20 × 1–3 ≈ 20–60; × 3 ≈ 60–180 | unknown |

Prerequisites:
- Confirm on the account that `gemini-3.5-flash-lite` and `gemini-3.6-flash` exist.
- A dated price file.
- The owner's spend cap.
- The live text runner and staff live mode, which are new code and outside this baseline.
- Voice also needs the recording plan in §7.

## 6. Local triage list

| # | Category | Severity | Evidence |
|---|---|---|---|
| T1 | No live customer-text runner | blocker for run #1 | only validator plus backend gold tests exist |
| T2 | Staff live mode not implemented; doc ahead of code | blocker | `app/staff_bench.py` raises `BenchError` for live; `docs/STAFF_EXTRACTION.md` §10 documents `--mode` and run id/git sha logging |
| T3 | Voice recordings, manifest and verified gold missing | blocker | 0/35 recorded, 0/35 verified |
| T4 | Control-gate evaluation path unverified | blocker for A (controls) | `docs/STT_BENCHMARK.md:94-106` |
| T5 | ST-15 "Sports City" location not resolved | fixture failure (1/20) | staff_bench `failed_items` |
| T6 | ST-01 `duplicate_of` gold vs RFC rule | gold dispute | owner Q1 |
| T7 | Scope §6 sets missing: 20 scripted follow-ups, 10 photo requests | coverage gap | bench has 4 / 1 |
| T8 | Undefined denominators: ambiguous subset, false-handoff base, latency sample | spec gap | R16/Scope |
| T9 | Model mismatch: R16 names Claude/GPT candidates; repo uses Gemini + Scribe; model pin unverified | decision | `docs/AI_TURN_ENGINE.md:65-66` |
| T10 | Doc labels: `docs/BENCHMARK.md:77` calls 10.3 "run #2"; Scope staff metric "drafts" vs 20 mixed items | doc fix | — |
| T11 | M3 not shown on a fresh customer; DB linkage and ≤ 10 s unmeasured | gate open | owner report |

## 7. GitHub issue drafts (not published)

1. **[bench] Live customer-text runner for R16 §4.1.** Drive `TurnEngine` with a fresh conversation per CT item, replay CT-13→27→28→29 and CT-30. Score critical slots, tool, outcome, result set, language and latency against gold. Repeat 3× and report the mean and the worst run. Acceptance: report JSON pinned to git sha + bench sha256.
2. **[bench] Implement `staff_bench --mode live`.** Live Gemini extraction on the seeded disposable PostgreSQL, with run id + git sha + TASK_LOG line, as `docs/STAFF_EXTRACTION.md` §10 describes.
3. **[voice] Record and verify the 7.2 formal set (30 + 5).** Follow `docs/STT_RECORDING_CHECKLIST.md`. Acceptance: `stt_bench validate` passes with gold_verified = 35.
4. **[voice] Control-gate evaluation via the extraction path.** VC-01..05 must yield zero requirements; replace the "unverified" status.
5. **[staff] ST-15: resolve "Sports City" or change the gold.** Needs an owner decision on whether it is a gazetteer gap or the intended `location_unclear`.
6. **[gold] Decide owner Q1 (ST-01 duplicate_of).**
7. **[bench] Add the Scope §6 follow-up (20 turns) and photo (10 + Scenarios 2–3) sets**, or put them on the cut list with a recorded decision.
8. **[spec] Define the R16 denominators** (ambiguous subset, false handoffs, latency sample) and record which LLM/STT candidates run #1 covers.
9. **[docs] Fix the run #1/#2 labels in `docs/BENCHMARK.md`** and the staff metric wording.
10. **[gate] M3 fresh-customer acceptance.** New number, Scenario 2 voice note, DB-linked result set, timed from receipt.

## 8. Next actions for recordings and gold

1. **Owner:**
   - Get consent from 6 speakers (S1–S6).
   - Use 3 phones (android_low, android_mid, iphone) on the test allowlist.
   - Set `STT_PROVIDER=off` while recording.
2. **Record:** 35 WhatsApp notes per the session plan in `docs/STT_RECORDING_CHECKLIST.md`, 5 items + ≤ 1 control per speaker, about 6 min of speech in total.
3. **Export:** run `python -m app.bench_manifest --agency-id … --since … --until …` in the worker container.
   - Label item_id/speaker/phone/noise to make `bench/voice_manifest.csv`.
   - Download the audio privately as `<item_id>.ogg`.
   - Run `stt_bench validate --manifest … --audio-dir …`.
4. **Verify:** a non-speaker listener writes `bench/voice_gold_verified.json` (35 entries) and a second person reviews it.
5. **Text and staff gold:** add a sign-off record (reviewer, date, sha256) and answer Q1.
6. Then approve spend and run §5.
