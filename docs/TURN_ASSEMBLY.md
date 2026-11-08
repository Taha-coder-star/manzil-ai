# Day 12: turn assembly and replay

Customer text jobs wait for 1.5 seconds of quiet, with a four-second cap from
the first pending event's server receipt. A deliberate queue deferral releases
the lease and conversation advisory lock and refunds the claim attempt. Audio,
taps, staff, brokers, open resumed turns, and human-controlled conversations
bypass this wait. Provider timestamps continue to determine message order.

Before dispatch, the worker records pending sibling events. A customer turn can
absorb new messages and clear its unsent fixed plan once. Migration 018 stores
the replan count, original message IDs, and dispatch-started boundary. Replanning
retains committed requirements; original input is context only and new input is
the source of new verified requirement operations. This prevents applying a
relative request twice. Existing search audits are retained; shown and media
history are still written only by the sender.

SYSTEM plans, any turn with an outbound attempt (including an interim notice),
and plans whose dispatch boundary has committed cannot be superseded. A crash
after that boundary resumes the existing plan and send keys. New messages after
the single replan remain pending for the next turn. The control-state gate still
runs independently on every send. A transport failure never clears a stored plan.

## Verification

`tests/test_turn_assembly.py` covers timing boundaries, retry-budget preservation,
coalescing, single replanning, SYSTEM preservation, dispatch crash recovery,
context-only old input, and canonical versus shuffled burst membership.
`tests/test_webhook_replay.py` exercises signed HTTP, real ingest and workers,
fake model and speech providers, search, voice correction, taps, handoff, reversed
statuses and twice-shuffled duplicate session delivery. Same-database snapshots
retain embedded IDs and timestamps as a stronger duplicate-replay assertion.
Fresh burst comparison projects membership and business values. A separate test
builds sequential isolated schemas and compares the full canonical and shuffled
session snapshots, mapping UUID references to stable aliases and removing volatile
JSON timestamp fields while retaining relationships, ordering and business values.

Legacy flow fixtures explicitly use a zero debounce window; the dedicated timing
module restores production values. CI already discovers all tests and requires
disposable PostgreSQL. Never run this suite against RDS.

Run from the repository root using Windows CMD:

```
.venv\Scripts\python.exe -m ruff check app tests deploy
.venv\Scripts\python.exe -m pytest -q
```

Database validation requires the dedicated loopback `manzil_schema_test` database
through `TEST_DATABASE_URL`, with `REQUIRE_DATABASE_TESTS=1`. Do not print the URL.
Use a Git archive with task edits for verification if the old local migration
files have CRLF bytes; do not rewrite applied migrations or their checksums.

## Release

Owner-run verification completed: full Python 3.12/PostgreSQL suite **3977 passed**,
zero failures/errors/skips, one existing Starlette deprecation warning, 846.86s.
Focused regression suite previously passed 28 tests in 18.54s. Ruff passes.
Database-backed acceptance tests for 12.1 and 12.2 pass; live checks remain pending.

No production operation is included in these changes. Migration 018 must precede
starting the updated worker; startup validates its columns. Obtain owner approval
before application or any AWS change. An older worker can run with these additive
columns for rollback. Preserve current deployment and stabilisation evidence.
After review and approved deployment, record team-phone burst and voice-correction
evidence and text/voice latency cohorts. Offline tests do not establish p90 gates.
