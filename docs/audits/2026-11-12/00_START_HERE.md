# Manzil AI independent system audit — 12 November 2026

Start here when auditing this Git branch. The owner wants bugs and reliability
weaknesses in the built system investigated, with separate readiness judgments for
one real agency and multiple independent agencies. Remaining roadmap features are
context. Day13 stays open; no known acceptance gap is waived.

## Run the audit

Give a repository-connected Claude session access to this checkout and paste:

```text
Read docs/audits/2026-11-12/00_START_HERE.md and 01_PROJECT_BRIEF.md in that folder.
Execute 02_PROMPT_CONTEXT.md, 03_PROMPT_AUDIT.md and
04_PROMPT_CHALLENGE_AND_HANDOFF.md in order. Complete each phase's artifacts
before advancing; do not wait for another instruction between these phases.
Follow every evidence, scope, validation and production-action boundary.
```

1. [Context intake](02_PROMPT_CONTEXT.md): source-grounded architecture/history and coverage plan.
2. [Code audit](03_PROMPT_AUDIT.md): reachable bugs, safe proofs and cross-module failure paths.
3. [Challenge and readiness](04_PROMPT_CHALLENGE_AND_HANDOFF.md): falsify findings, assess operating modes and hand off fixes.

[Project brief](01_PROJECT_BRIEF.md), [coverage checklist](06_COVERAGE_CHECKLIST.md)
and [artifact templates](07_AUDIT_TRACKER_TEMPLATE.md) support these prompts.
Write findings/checkpoints outside product source, under output/claude-audit-results
or another local review-results folder. An audit is not yet performed by publishing this pack.

## State and snapshot boundaries

Application baseline is7eefb9c; this branch adds documentation/audit inputs only.
The latest owner receipt restored low effort on image976dff195d20, schema020.
Current Git branch/HEAD, remote main, and deployed image are separate identities;
the auditor must establish its actual reviewed snapshot. No fresh remote runtime
inspection or GitHub merge verification is implied by these notes.

Relevant sanitized Day13 records are in [evidence/day13](evidence/day13/README.md).
They retain failed first attempts, extra/accidental inputs, unknown token fields,
and acceptance gaps. Historical evidence is not a current formal benchmark pass.
The original local source ZIP and its manifest remain local; use the full checkout
for this GitHub edition. INPUT_MANIFEST.json hashes the published audit inputs.
08_RECENT_HISTORY.txt is a dated local lineage excerpt, not all Git history.

Read latest PROJECT_STATE/TASK_LOG notes and dated owner decisions. Historical
subsystem headers may describe old deployment states. Original RFC/red-team/Scope/
execution-plan PDFs are external and not bundled; unavailable evidence stays unknown.

No .env, provider keys, private audio, screenshots, raw customer records, large
release ZIPs or unreviewed local test directories are included in this publication.
No app fixes, paid calls, production access, migrations, deployment, merge or AWS
changes are authorized by an audit prompt. Use synthetic data and disposable local DBs.

## Resume after a context reset

```text
Resume the Manzil AI audit from the current audit-results/CHECKPOINT.md. Re-read
the project brief, coverage ledger, findings and open/closed hypotheses. Confirm
the source snapshot. Continue the exact pending investigation; do not restart
intake, repeat unchanged tests or drop rejected hypotheses. Preserve all scope
and production-action boundaries.
```
