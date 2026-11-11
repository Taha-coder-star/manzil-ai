Continue as the principal engineer. Use the verified context and audit plan from
the previous phase. Conduct a comprehensive audit of the supplied Manzil AI system,
focused on defects in implemented behavior and its real-client reliability.

Work through the risk areas below and the rest of the repository. Do not stop after
the first plausible bugs or one batch of tests. Spend most of the available model
time tracing execution, validating failure mechanisms and checking interactions.
Keep findings/checkpoints durable so a context reset does not erase the investigation.

## Scope and authority

Audit app/, migrations/, deployment/configuration/CI, requirements/container setup,
seed/data contracts, benchmark/reporting/operator utilities, and the tests themselves.
Include tracked docs/release scripts. The supplied ZIP excludes large untracked local
release kits; list that boundary and any deployment question that needs those files.
When using the real workspace, inspect only specifically relevant additional release
artifacts; do not ingest raw private output logs/credentials/audio indiscriminately.

All application/production boundaries from the context prompt remain in force.
You may write focused proof tests under the audit-results folder or an isolated
review checkout, use synthetic fixtures and fake external providers, and execute
tests against a verified disposable local database. Preserve unrelated local work.
No product fixes or dependency upgrades during the audit; propose them after findings.
Never point tests at production or use real provider keys. Do not run operator CLIs,
seed/reset, migration/deploy/role-changing commands merely because a README lists them.

## Investigation priorities

1. **Tenant and identity isolation.** Follow authenticated webhook sender mapping to
   every query, job, cache, storage key, staff action, reference lookup, lead and
   outbound credential/phone selection. Check every tenant table/policy/view/constraint
   and global-table exception. Try two tenants with overlapping listing codes, the
   same customer phone, forged IDs, unknown/inactive senders and stale roles. Check
   GUC reset on pooled/reused/error connections and cross-tenant FK/write paths.
   Determine whether inbound multi-agency routing is actually matched by outbound
   credentials/sender selection, provisioning and worker agency discovery.
2. **Ingestion, queue and crash consistency.** Trace signature/body handling, durable
   acknowledgement, dedupe and transaction boundaries, leases, locks, retries, reaping,
   dead jobs, poison input and worker death. Test reordered/duplicated messages/statuses,
   same/different conversation races, concurrent workers and lost connections. Follow
   side effects across every commit and external-call boundary. Do not demand universal
   exactly-once delivery; prove where the current contract can lose, duplicate or strand work.
3. **Staff integrity and authorization.** Customer/broker attempts to invoke staff tools
   or forge buttons, draft ownership, confirmation versions, double/expired/replayed taps,
   concurrent code allocation and updates, Save/Edit/Cancel/Undo/reconfirm, attachment
   ownership and media races. Verify audit rows and materialized state remain coherent.
4. **Customer correctness and grounding.** Multi-turn extraction/patches and normalizers,
   missing/contradictory slots, negation, budget operators, inferred purpose, relative
   cheaper and only-area constraints, quoted references, out-of-scope/unknown locations,
   multilingual/transcript boundaries, numeric ambiguity and failure-to-clarify cases.
   Trace DB search filters/ranking/relaxations and public-only visibility through actual
   dispatch. Check stale/hidden/deleted inventory, price changes, stored old plans,
   fallback paths and TOCTOU windows. LLM text must not become staff authority or facts.
5. **Human handoff.** Lead/property selection, broker/admin routing, Accept/Pass/Called/
   done actions, escalation and release timers, competing taps, stale scheduled jobs,
   forwarding, closed service windows and recovery. Check turn-time and send-time
   gates during transitions so AI cannot talk over a human or silently lose customer work.
6. **Voice/media and external APIs.** Download/upload/presign limits, content validation,
   malformed Ogg/images, resource exhaustion, privacy/public derivative scope, unsafe
   URLs/SSRF where reachable, expiration/retries, no-speech/STT fallbacks, transcript
   reuse, truncation, echo/confirmation, and prompt/tool injection. Inspect actual HTTP
   timeout/error/response parsing and optional usage metadata handling.
7. **Cost, performance and contention.** Identify concrete avoidable calls, unbounded
   work, long locks/transactions, N+1 queries, hot-tenant fairness, global bottlenecks,
   exhaustion/backpressure and per-tenant cost isolation. Inspect reservation/metering
   crash behavior and disabled-versus-enabled guards. Verify benchmark p50/p90/cost/
   cache computations, sample selection, failed turns and repeated attempts. Known
   free-tier latency is evidence, not a universal diagnosis. Missing usage is not zero.
8. **Operations and handoff.** Examine migrations/upgrades/rollback compatibility,
   reproducible builds, dependency ranges, startup validation, permissions, health vs
   readiness, backups/restore evidence, deploy scripts, secret/log handling, alerts,
   external account dependence, retention/deletion and per-agency onboarding/offboarding.
   Assess what a non-author developer/operator can actually recover. Code review cannot
   prove deployed IAM, backup restorability or load capacity without external evidence.
9. **Tests as evidence.** Review assertions/fixtures and production code together.
   Look for mocked-away transactions/RLS, tests asserting obsolete behavior, missing
   negative cases, idealized providers, insufficient interleavings and skipped DB tests.
   A large green suite is supporting evidence, not an exemption from finding bugs.

These are investigation hypotheses, not findings. Do not invent a vulnerability
because it appears in the checklist. Look beyond the listed modules when callers,
schema, error paths or data flow lead elsewhere.

## Finding standard

For every proposed bug, trace the reachable path and establish the violated contract
before assigning severity. Check callers, schema constraints, authorization, cleanup,
retry paths and existing tests for a mitigation that invalidates your theory.

Use stable IDs MA-001, MA-002, etc. Each finding needs:

- A concrete title and type: confirmed bug / architectural risk / evidence gap /
  missing capability / accepted intentional behavior / unconfirmed hypothesis.
- Severity and confidence separately, plus affected single-tenant/shared-tenant/
  isolated-deployment modes. Reserve P0 for credible critical harm, not stylistic issues.
- Preconditions, exact trigger/input/interleaving, expected and actual behavior.
- Snapshot-relative source file/line references and a short cross-module causal chain.
- Impact: data loss/leak, unauthorized write, wrong reply, stuck work, availability,
  customer trust, cost, or operator recovery. Say what is realistically reachable.
- A minimal safe reproduction or counterexample; commands/test result where executed.
  Static-only evidence may confirm some bugs; label tests not run and what remains unknown.
- Existing test coverage and why it misses the issue; link related known issues to
  avoid duplicates. A new manifestation of a known issue can still matter.
- The smallest safe fix direction, regression test and any migration/compatibility risk.
- For uncertainty, a specific falsification check. If falsified, close the hypothesis
  with evidence instead of quietly dropping it or counting it as a bug.

Use P0 critical incident / P1 blocks client handoff / P2 important bounded defect /
P3 low impact. Architectural risks and readiness gaps can block handoff, but must not
inflate the confirmed-bug count. Existing accepted demo decisions can still be real
production limitations; label the distinction instead of declaring them code defects.

## Validation discipline

Run the repository's lint/baseline checks when the environment supports them; first
inspect test configuration so external traffic and real DB connections cannot occur.
Prefer focused reproduction for each finding, then appropriate subsystem/DB checks.
Use at most one full-suite baseline for this unchanged audit snapshot unless failures
or audit-test changes justify another run. No redundant live/manual stabilization
campaign or paid benchmark is requested. DB tests must run serially per local cluster;
record Python/Postgres versions, target category, command, exit status and skips.

When verifying tenancy, use the real restricted runtime role and genuine PostgreSQL
RLS, not only mocked query results. When proving a race, control the interleaving
with separate connections/barriers; one happy-path sequential test is insufficient.
For API paths, inject fakes for ambiguity/timeouts/status ordering; no paid calls.
If tooling is unavailable, preserve the reproduction and list that validation limit.

## Deliverables and stopping rule

Maintain FINDINGS.md plus a structured FINDINGS.json, COVERAGE.csv, TEST_EVIDENCE.md,
and CHECKPOINT.md. Keep a separate OPEN_HYPOTHESES.md and CLOSED_HYPOTHESES.md.
Do not put secrets, raw customer content, actual phone numbers or credentials in reports.

Audit every supplied executable/config/SQL area to a stated depth; trace critical
paths end to end. Review test files by subsystem and record coverage honestly.
Finish with the findings ranked by client impact, actual commands/results, exact
coverage and unresolved questions. If time expires, explicitly identify unread and
unvalidated areas and prioritize the remaining investigation; never claim full coverage.
Do not conclude with generic best practices or start implementing product fixes.
