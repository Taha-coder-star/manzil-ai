# Manzil AI: project orientation as of 12 November 2026

This is a starting map, not proof of correctness. Verify the implementation.

Publication note: application baseline is commit7eefb9c. A later documentation
commit publishes this pack without changing application/migration bytes. Audit
the actual checkout HEAD and record it; do not mistake this baseline for the
current remote main/deployed Git identity. Day13 remains open by owner decision.
The evidence/day13 directory contains the reviewed closeout records; private
recordings and large raw historical audit exports remain outside Git.

## Product and original scope

Manzil AI is a WhatsApp-first real-estate assistant for Karachi agencies. Customers
search inventory using English/Roman Urdu text or voice, refine requirements,
request details/photos, and request a human broker. Agency staff propose inventory
changes by sending text/photos, then use confirmation controls to commit changes.
Brokers/admins receive and manage leads and own customer conversations during handoff.

The original project was a three-week, two-developer demo. Code has since received
extensive stabilization work. The owner now needs an independent assessment of the
built system for one real client and multiple independent clients. Demo completion,
tenant columns, passing tests and live demonstrations do not establish that readiness.

In this audit, **client** means an agency/business tenant, not an individual WhatsApp
customer or a Python HTTP client. State separately whether findings apply to:

1. One agency using the present architecture, with multiple staff/customers.
2. Several agencies sharing an application/database, with independent WhatsApp senders.
3. Separate application deployments per agency as a possible interim operating model.

## Architecture map

- Python application; FastAPI/Uvicorn web service and a separate PostgreSQL-backed
  worker runtime. Docker Compose runs web, worker and Caddy for HTTPS. Web and worker
  use the same application image. CI/container Python is 3.12; local `.venv` is 3.12.7.
- Meta WhatsApp Cloud API: webhook verification/signatures, event extraction,
  tenant routing, durable ingestion, jobs, turn assembly and outbound delivery/statuses.
- PostgreSQL/RDS stores tenants, roles, jobs, inventory, drafts, conversation state,
  requirements, search history, media history, handoffs and usage. Schema currently
  has migrations 001–020. Applied migration bytes must remain immutable.
- Runtime database role is intended to be restricted `manzil_app`, with FORCE RLS
  and transaction-local agency context. Trusted receiving `phone_number_id` routing
  chooses the agency; customer text is not tenant authorization. The shared runtime
  role can set tenant context: RLS does not defend against arbitrary SQL using stolen
  application credentials. Shared gazetteer tables are deliberate exceptions to tenancy.
- Queue claims, conversation advisory locks, leases/reaping, retries and fixed reply
  plans handle asynchronous work. Do not assume end-to-end exactly-once behavior:
  inspect external-send uncertainty and crash boundaries separately.
- S3 stores media. Private originals/audio and public card derivatives have different
  access rules. Image processing, EXIF stripping, dedupe and delayed fallbacks matter.
- Gemini is the LLM provider. Custom tools, deterministic normalizers/location resolver,
  persistent requirements, SQL search, templates/rendering and grounding constrain it.
  The model proposes tool operations; it is not inventory authority or staff authorization.
- ElevenLabs STT is primary, Gemini audio is fallback. Accepted speech enters the same
  customer pipeline; silence/unusable/ambiguous speech and echo policy have distinct paths.
- Staff draft/save/update/cancel/undo flows have deterministic authorization/version
  checks and audit events. Human handoff changes control state and must suppress AI
  inference/sending while a human owns the conversation.
- Day13 adds security/stale-listing checks and benchmark/cost tooling. Its optional
  shared budget ledger is not active in the current deployed demo.

## Read these source areas as connected flows

| Area | Entry points and primary implementation |
|---|---|
| Ingress and tenancy | `web.py`, `webhook.py`, `routing.py`, `ingest.py`, `db.py`, migrations |
| Work scheduling | `worker.py`, `queue.py`, `handlers.py`, `orchestrator.py`, `turn_assembly.py` |
| Inference and tools | `turn_engine.py`, `llm.py`, `ai_tools.py`, `customer_tools.py`, `staff_tools.py` |
| Customer state/search | `requirements.py`, `normalise.py`, `location.py`, `gazetteer.py`, `search.py`, `references.py` |
| Replies and media | `render.py`, `cards.py`, `customer_reply.py`, `freshness.py`, `outbound.py`, `whatsapp.py`, `media.py` |
| Voice | `audio.py`, `speech.py`, `stt.py`, `echo.py` |
| Staff writes | `staff_buffer.py`, `staff_drafts.py`, `staff_actions.py`, `staff_cards.py`, `staff_normalise.py` |
| Human control | `handoff.py`, `handoff_actions.py`, `handoff_alert.py`, send/turn control gates |
| Operations and evidence | `config.py`, `health.py`, `log_safety.py`, `privacy.py`, `provision_agency.py`, `migrate.py`, `deploy/`, CI, benchmark modules |

All Python paths in that table are under `app/`. Follow imports/callers rather than
restricting the audit to this starting list.

## Development history and what is built

- Foundation: webhook/routing, restricted tenant transactions, database queue,
  outbound logging, schema, media and demo dataset were implemented during Days 1–5.
- Days 6–10: provider/tool engine, normalization, STT, locations, requirement state,
  inventory extraction, search, guarded staff commits, rendering and customer/voice flows.
- Day11: lead creation, broker routing/actions, human control gates, timers and release.
- Stabilization in late October/November addressed live customer/staff/media/voice
  findings. Owner accepted some earlier broker evidence and an unavailable-iPhone
  exception. Do not turn those exceptions into fresh compatibility/load-test passes.
- Day12.1/12.2: customer burst assembly, bounded replanning and signed webhook replay.
  Commit `d908a86` is the local history checkpoint.
- Day12.3/12.4: failure/recovery handling and alerts; migration019. Commit `02304c2`.
- Day13.1/13.2: customer/staff tool isolation, log safety, stale-listing dispatch and
  re-confirmation; migration020 stores shown price snapshots. Commit `3c6df54`.
- Day13.3/13.4: latency/cost/budget and benchmark tooling in `93a71be`; approved
  ST-01 regression/gold alignment in `7eefb9c`. Tooling is implemented/deployed;
  formal acceptance is incomplete. ST-01 expects DH6-101/DH6-106 duplicate warnings;
  SC-18 uses the current Clifton neighbor graph with no invented edges.

This is a lineage of the local branch, not a claim that every related PR was merged.
Use `08_RECENT_HISTORY.txt` and real Git history where available. Do not infer current
remote PR status from old notes or local remote-tracking references.

## Current state: evidence-backed, not a fresh remote inspection

- Application baseline branch at preparation: `feature/task-13-benchmark-runtime`.
- Application baseline commit: `7eefb9c787ef39b8d24b84f420dbc13e335e90bd` (PR66 publication context).
- Owner returned successful CI evidence for push run42861430103 and PR run42861446643.
  This is historical exact-head evidence, not a test run performed during packaging.
- Latest owner runtime receipt: web/worker application image
  `sha256:976dff195d20f6f9adbf593cccccbef12b31184ffb7be9fd51bef07ac08b7b8a`;
  schema020, earlier owner checks confirmed fixture/health preservation.
- Provider config: Gemini primary `gemini-3.5-flash-lite`, failover `gemini-3.6-flash`,
  thinking `low`; ElevenLabs STT with Gemini fallback. Model names are reported runtime
  settings, not independent proof of future availability, pricing or model capabilities.
- Config hash after restore:
  `34d4b870796b7684567fb78682ca49930e24971dbad573359ea1bd4a08381d60`.
- Owner reports free API tiers. Optional budget guard remains OFF; incomplete pricing
  policy must not be activated or described as a hard production spending cap.
- The demo AWS runtime was restarted and deployed with prior approval. Latest owner
  receipt restored **low** at `2026-11-11T19:53:16.277923+00:00`; do not follow older
  "AWS intentionally stopped" or "minimal active" notes as present state. No AWS access,
  provider traffic, deployment or infrastructure change is authorized by this audit.
- Retained fixture checks previously found seed30/public29 and hidden-fixture protection.
  Existing role/contact assignments and real conversation/lead/media history must be preserved.

## Known evidence gaps: keep visible without letting them dominate the bug hunt

Day13 remains open. The owner has chosen to address its gaps later, not to waive them.
The current audit focuses on already-built behavior across the entire system.

Two planned minimal-effort text cases gave first substantive **Meta acceptance**:
T01 office11.534s and T02 house7.162s. Low T01 was11.929s; low original T02 failed
HTTP503 after successful search. Resends remain separate, not replacement successes.
An owner-identified accidental earlier input is excluded and retained as a protocol
deviation. Minimal was not adopted and was restored to low.

The T02 assumption notice at6.686s was not the answer. The carousel was accepted
before a coverless #3 card but delivered one second after it; this observation does
not prove an application-ordering bug. No carousel fallback occurred in that case.
Both minimal turns used three LLM calls. Blindly stopping after search could suppress
other valid actions; any proposed shortcut needs behavioral evidence.

For the fixed ten-text nearest-rank plan, those two minimal times force p90>=7.162s
even if the remaining eight were instantaneous. This is a bound, not a full measured
p90. Eight text and six voice cases remain unrun. Cached/thinking token counts were
null, not zero. Complete dollar-cost, current voice SLA and formal full Run2 evidence
are not established. No Sonnet/always-on-voice fallback was adopted by this experiment.

Historical closeout recovery found 500 of505 stored historical turns and46 genuine
customer Ogg notes; that is useful regression evidence, not a current-build formal
cohort. Do not replay the entire stabilization plan just to reconfirm accepted cases.

## Documentation authority and practical constraints

`docs/PROJECT_STATE.md`, `TASK_LOG.md` and `OPEN_ISSUES.md` are long chronological
documents, often with newest notes at the top. Older subsystem status headers, root
CLAUDE.md and handoffs may say "not deployed" about code subsequently deployed.
Use dated owner decisions for intended behavior; code/tests for actual behavior;
sanitized receipts for observed deployment. Report contradictions explicitly.

Windows project commands use `cmd /d /c`. Use repository `.venv\Scripts\python.exe`
if available; container/CI compatibility is Python3.12. DB checks need a disposable
local PostgreSQL database via TEST_DATABASE_URL, never RDS. Missing DB skips are not
passes. Only one suite at a time per test cluster: fixtures change a shared test-role
password. Fake external providers by default. Paid/live reproduction needs separate
authorization; inability to run it is a limitation, not permission to use production.

No application fixes, merges, pushes, deployments, applied-migration edits, inventory
resets, role/contact swaps or runtime budget activation are part of this audit request.
