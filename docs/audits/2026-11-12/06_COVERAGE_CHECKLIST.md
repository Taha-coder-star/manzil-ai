# Starting coverage checklist

This is an inventory, not completed audit coverage. Initialize every area UNREAD.
The auditor must record actual file-level depth, evidence and limitations in COVERAGE.csv.

## Ingress, identity, tenancy

- [ ] `app/web.py`
- [ ] `app/webhook.py`
- [ ] `app/routing.py`
- [ ] `app/ingest.py`
- [ ] `app/db.py`
- [ ] `app/provision_agency.py`

## Queue, orchestration, runtime

- [ ] `app/worker.py`
- [ ] `app/queue.py`
- [ ] `app/handlers.py`
- [ ] `app/orchestrator.py`
- [ ] `app/turn_assembly.py`
- [ ] `app/__main__.py`
- [ ] `app/__init__.py`

## LLM, tool and customer state

- [ ] `app/llm.py`
- [ ] `app/turn_engine.py`
- [ ] `app/ai_tools.py`
- [ ] `app/customer_tools.py`
- [ ] `app/requirements.py`
- [ ] `app/customer_intent.py`

## Normalization, locations, search

- [ ] `app/normalise.py`
- [ ] `app/location.py`
- [ ] `app/gazetteer.py`
- [ ] `app/search.py`
- [ ] `app/references.py`
- [ ] `app/details.py`

## Rendering, outbound, freshness

- [ ] `app/render.py`
- [ ] `app/grounding.py`
- [ ] `app/cards.py`
- [ ] `app/customer_reply.py`
- [ ] `app/freshness.py`
- [ ] `app/outbound.py`
- [ ] `app/whatsapp.py`

## Voice, media, human control

- [ ] `app/audio.py`
- [ ] `app/speech.py`
- [ ] `app/stt.py`
- [ ] `app/echo.py`
- [ ] `app/media.py`
- [ ] `app/handoff.py`
- [ ] `app/handoff_actions.py`
- [ ] `app/handoff_alert.py`
- [ ] `app/recovery.py`

## Staff inventory

- [ ] `app/staff_actions.py`
- [ ] `app/staff_buffer.py`
- [ ] `app/staff_cards.py`
- [ ] `app/staff_drafts.py`
- [ ] `app/staff_normalise.py`
- [ ] `app/staff_tools.py`

## Operations, seed, media utilities

- [ ] `app/config.py`
- [ ] `app/health.py`
- [ ] `app/log_safety.py`
- [ ] `app/privacy.py`
- [ ] `app/migrate.py`
- [ ] `app/seed.py`
- [ ] `app/demo_reset.py`
- [ ] `app/demo_dataset.py`
- [ ] `app/demo_media.py`
- [ ] `app/media_seed.py`
- [ ] `app/media_send_check.py`
- [ ] `app/card_regen.py`

## Benchmarks, trace and diagnostics

- [ ] `app/bench_budget.py`
- [ ] `app/bench_live.py`
- [ ] `app/bench_manifest.py`
- [ ] `app/benchmark.py`
- [ ] `app/day13_bench.py`
- [ ] `app/day13_report.py`
- [ ] `app/day13_trace.py`
- [ ] `app/staff_bench.py`
- [ ] `app/stt_bench.py`
- [ ] `app/m1_status.py`
- [ ] `app/m1_replay.py`

## Remaining supplied scope

- [ ] Every migration001–020: grants/RLS, constraints, indexes, schema/upgrade invariants.
- [ ] Every tracked deploy script, Caddy/Compose setting, Dockerfile and CI/config/dependency file.
- [ ] Every tracked test file and fixture, grouped by explicit file list and checked against implementation.
- [ ] bench/ gold, thresholds and decisions; demo/ CSV and policy contracts.
- [ ] docs/release executable scripts and profile; operator/deployment evidence boundaries.
- [ ] Relevant docs/ contracts and superseding owner decisions; history-driven bug regressions.
- [ ] End-to-end interactions and multi-tenant/same-customer/concurrent-worker scenarios.
- [ ] External evidence limits: deployed IAM/config, real load, backups/restore, provider limits, missing original PDFs.
