# Day 13: bounded search completion and honest cost estimates

The saved low/minimal trials showed an extra model round after search had already
rendered the answer. That round adds latency and can fail after a successful search.
This change ends a customer turn immediately after that search only when:

- The actual customer text is a bounded, single-property-type search. Unknown
  wording, conjunctions, questions, multiline turns and additional actions keep
  the existing model loop. This predicate does not extract or normalize any slot.
- The binding is the real customer binding, the tool history contains only
  successful requirement updates/searches, and the last successful tool is search.
- The search has rendered payloads and marked results/no-match ready. Staff tools,
  stubs, failed/empty results, clarification-only and handoff paths do not qualify.

Extraction still uses the model and the existing grounded span normalizers. The
search, renderer/validator, stored reply plan, send-time control gate and answer-time
availability checks are unchanged. A normal update then search can finish after two
calls; when the model supplies both tools in one round it can finish after one.
Other wording retains its existing behavior. No 7-second SLA is inferred from this
reduction, and previously failed low/minimal cohorts remain failed/incomplete.

`bench_live.call_cost` now requires valid input, output, thinking and cached-input
counts, including a cache count no greater than input. Omitted fields return an
unknown estimate rather than substituting zero. Explicit zero remains priceable.
The existing estimated spend guard reserves unknown calls; it is not a guaranteed
billing cap. Runtime usage parsing already retains absent fields as NULL, so no
schema or artificial zero-value backfill is introduced.

## Validation and release boundary

Focused validation initially passed 217 tests with one platform skip, including
worker/database searches, renderer behavior and provider parsing. New integration
cases explicitly cover two-call completion and compound-request continuation.
The full run covered 4,125 cases: 4,076 passed, 42 platform skips, seven failures
from old scripted expectations and a missing local CI role name. After correcting
the harness/setup, the final affected-path recheck passed all 185 cases, including
all seven earlier failures, full webhook replay and privilege denial. Application
bytes did not change between these runs. The initial full-suite exit code was not
zero; its failures remain in the evidence. Exact-candidate Linux CI is still required.
See the final local validation receipt outside the repo.
Scripted turn helpers remove only their own unused terminal STOP response, after
all preceding scripted tool calls have executed. They do not discard remaining
tool calls. A disposable local cluster must also contain the CI role name
`manzil_test` for the SET ROLE denial check; the runtime login is not made a member.

No migration, provider/model/effort change, paid-account change or data reset is
part of this patch. The last recorded deployment remains the low-effort image
976dff195d20, schema020, until a new verified deployment receipt is returned.

After code review and exact-commit CI, deploy through the retained schema020
release procedure using a NEW package pinned to the current image/config. Do not
run historical tuning scripts that pin an older base image. Preserve the retained
image and rollback configuration. Verify both services, health, model/effort,
schema/fixtures and safe send gates before registering a new measurement run.

Register the candidate's diagnostic office/house checks first, then only useful
remaining text/voice coverage. Keep failures and resends distinct; use receipt to
first substantive Meta acceptance, excluding notices/echoes. Never append new
measurements to closed low/minimal cohorts. A small diagnostic batch is not the
original full Run2. Missing cache/cost/voice or formal coverage stays a named gap.

Day13 remains OPEN until results support closure or the owner explicitly accepts
a concrete conditional demo closeout. No exception has been accepted by this patch.

## 13 November: exact-head CI helper correction

Both CI runs for3e24e4e executed the full Linux suite:4,124 passed and one failed,
`test_after_the_handoff_the_ai_stays_silent`. The customer holding-reply assertion
passed, but the shared test helper had removed the unused STOP response used as
a no-model sentinel, including for gated turns with no scripted tools.

Cleanup now requires explicitly scripted tool calls. Zero-tool/gated turns retain
the sentinel. The handoff test also checks that the actual provider-request count
does not change. This preserves and strengthens the gate assertion; no application,
provider, runtime, schema or deployment change is part of the correction.

The focused correction suite passed128tests with no skips (handoff, send gate,
customer/search holds, replay and completion guards); lint passed. Prior CI logs
and validation receipts are retained. Publish the new test/docs-only head and
require both fresh CI events before any deployment or Day13 closeout decision.
