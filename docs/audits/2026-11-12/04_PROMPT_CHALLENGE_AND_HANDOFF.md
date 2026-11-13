Perform the final independent challenge and readiness pass on your Manzil AI audit.
Do not merely rewrite the findings. Re-open source/test evidence and try to disprove
the important claims. Continue writing to the existing audit-results folder.

## Challenge the audit

1. For each P0/P1 and every cross-tenant/data-loss/unauthorized-write claim, trace the
   full path again. Check whether an upstream gate, immutable constraint, transaction,
   unique key, retry identity or later guard already prevents the proposed trigger.
   Downgrade/reject false positives explicitly. Validate severity against realistic
   preconditions rather than assuming a malicious database administrator.
2. Separate code defects from intentional owner-approved demo policy, unsupported
   features, environment/account limits, old deployment documentation and evidence
   gaps. A missing production control may block readiness without being a code bug.
3. Search for missed siblings: the same issue in another route, alternate media/card
   path, recovery job, delayed timer, staff confirmation or second tenant. Revisit
   both successful and failed/crashed execution paths. Do not stop at happy-path tests.
4. Verify snapshot, coverage and validation claims. A file skim is not traced/tested;
   skipped DB cases are not RLS evidence; historical CI is not current external-system
   validation. Mark unread areas, assumptions and unavailable original specifications.
5. Confirm reproductions use synthetic fixtures/fake providers/disposable DBs. Ensure
   no customer data, keys, live credentials or unsupported "all clear" claims escaped.

## Readiness decisions

Give separate verdicts for:

A. Existing demo operation.
B. One real agency, under explicitly stated expected load/operating assumptions.
C. Multiple independent agencies in one shared deployment/database.
D. Separate deployment/database per agency as an interim alternative, if relevant.

For each choose Ready / Ready with stated constraints / Not ready / Insufficient
evidence. State that this is an engineering assessment of a named snapshot, not a
guarantee. Do not label an untested architecture "ready" just because RLS exists.
Do not invent workload/capacity numbers; unknown load is an assumption/evidence gap.

Create a readiness matrix covering identity/onboarding, tenant isolation, credential
and sender selection, concurrency/fairness, data integrity, public/private media,
control/handoff, provider failures, cost isolation, health/alerts, backups/restore,
deployment/rollback, retention/access control and operator ownership. For each state:
code/evidence inspected, status, assumptions, blocking finding IDs or missing proof,
and the smallest acceptance check that would resolve the question.

Do not treat planned roadmap completion as the answer to this audit. Report genuine
unbuilt production capabilities separately, and explain when they prevent real-client
handoff despite working demo features. Day13 acceptance remains open; this audit does
not accept its deferred gaps or automatically replace its benchmark gates.

## Final handoff

Produce:

1. EXECUTIVE_REPORT.md: verdicts first, strongest evidence, top blockers and practical
   operating limits. Keep the owner summary to roughly 1–2 pages; link detailed evidence.
2. FINDINGS.md / FINDINGS.json: corrected, deduplicated register with severity,
   confidence, reproduced/static-only status, paths/lines and explicit rejected theories.
3. READINESS_MATRIX.md: the per-operating-mode matrix and unresolved external evidence.
4. COVERAGE.csv + TEST_EVIDENCE.md: actual audit depth, commands/results, skips and
   unreviewed areas. Include a final snapshot identity and any audit-only changes.
5. REMEDIATION_HANDOFF.md: immediate blockers, next important fixes, later hardening.
   For each confirmed issue specify affected modules, minimal patch approach, invariants
   to preserve, focused regression test and rollback/migration implications. Order fixes
   by risk and dependencies, not a forced rewrite or speculative new platform.
6. NEXT_AGENT_PROMPT.md: a self-contained prompt another coding agent can use to fix
   only the validated highest-priority bugs. Include snapshot, finding IDs, evidence,
   allowed scope, required tests, forbidden production actions and stop conditions.
   Do not authorize applying migrations, paid calls, deployment or merging.
7. CHECKPOINT.md: complete/remaining work, artifact paths and any necessary targeted
   questions. Ask only for evidence that materially changes a conclusion.

Do not implement fixes during this phase or pad the report with formatting/style
complaints. If no confirmed critical bug is found, say that plainly and distinguish
it from the confidence limits and deployment-readiness gaps. If audit coverage is
incomplete, lead with that practical limit rather than presenting a comprehensive verdict.

Conclude with the few actions that most reduce real-client risk, each tied to a
finding or missing proof. Make the result usable by a different engineer who has
never seen this conversation.
