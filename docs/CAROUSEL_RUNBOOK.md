# Task 5.2 carousel: status and production runbook

Design and rules: docs/DECISIONS.md (Task 5.2 entries), docs/STATE_SCHEMA.md (migration 011),
app/cards.py. This file separates what was verified locally from what is still pending in
production. docs/PROJECT_STATE.md and docs/TASK_LOG.md are not updated by Task 5.2: on `main` they
currently start with pasted chat text and need their own clean-up first.

## 1. Status (2026-10-10)

### Verified locally (worktree, branch feature/task-5-2-carousel, uncommitted)

- Full suite in Docker (manzil-ai:test, Python 3.12) against the disposable PostgreSQL 18.6
  container, `REQUIRE_DATABASE_TESTS=1`, worktree mounted by its absolute path:
  - before the final-review fixes: **1067 passed, 0 skipped, 0 failed**;
  - after them (stable turn plan, carousel_fallback job, docs): **1076 passed, 0 skipped,
    0 failed** (the count matches `pytest --collect-only` of the worktree).
- Ruff, `git diff --check` and Gitleaks 8.24.2 (changed and new files) clean.
- Migration 011 applied only to disposable test schemas. Migrations 001-010 are byte-identical
  to `origin/main`.
- All WhatsApp sends in tests go to a fake client. No Meta, S3, AWS or RDS call was made.

### Pending (each needs the owner's explicit approval)

| Step | State |
|---|---|
| Commit, PR, review, merge | not started |
| RDS snapshot, then migration 011 on RDS (section 3) | **not applied**; RDS has 001-010 |
| Release image with Task 5.2 code; web / worker replaced | not built, not deployed |
| Pilot cover rows: DH6-101 and PRJ-103 each have `cover_media_id` set, the cover row has `card_image_key` and `public_url_card`, 4 media rows each | reported by the owner after the Task 4.4 load; **not verified** |
| Anonymous checks: both cover card URLs answer `200 image/jpeg`; the 16 private copies answer 4xx (docs/DEMO_MEDIA_RUNBOOK.md) | **not done / not recorded** |
| Device test on Android, iPhone, WhatsApp Web (section 4) | **not done** (gate M1(e)) |

## 2. Send outcomes (what the code does)

| Outcome of a send | Result |
|---|---|
| sent | next message of the plan |
| cancelled (send-time control gate) or failed outside the 24 h window | the turn stops; nothing else is sent |
| **unknown** (read timeout, dropped connection, missing message id, a crash mid-send) | **never resent and never fallen back**: Meta may have delivered it and the Cloud API has no idempotency key. The turn stops, so later messages of the same plan (a coverless card, the overflow list) are not sent. The row stays `unknown` with no WAMID, so no status webhook can match it. Check by hand on the phone. |
| carousel refused at once (HTTP 4xx) | its cards (same summary, texts, images, buttons) are sent in the same turn under `turn:{id}:{seq}:fallback:{n}`, then the plan continues |
| retryable (throttling, 5xx, connect failure) | the job retries; the turn resumes with its stored plan (`conversation_turn.reply_plan`) and the same keys |
| carousel accepted, then a status webhook says `failed` before any `delivered` | one `carousel_fallback` job sends its cards under `carousel-fallback:{outbound id}:{n}` |
| carousel delivered, then `failed` | nothing is resent (the phone already has it) |
| a fallback card fails later | nothing more: cards never fall back again |

Every message, fallback cards included, goes through app/outbound.py: send-time gate, 24 h window,
and at most once per dedupe key. The per-contact flag is never set automatically: a phone that
cannot render a carousel sends no signal, so staff set it with `#demo fallback on`.

## 3. Migration 011 on RDS

What it does: three columns, one CHECK, one FK and one partial index on `conversation`, and one
nullable JSONB column on `conversation_turn`. No row is rewritten or updated.

Lock scope (held until the single migration transaction commits, app/migrate.py):

- `conversation`: ACCESS EXCLUSIVE (ALTER TABLE ADD COLUMN / ADD CONSTRAINT; the CHECK and FK
  validation scans run under it), then SHARE for the non-concurrent CREATE INDEX.
- `staff`: SHARE ROW EXCLUSIVE as the FK target (reads allowed, writes blocked).
- `conversation_turn`: ACCESS EXCLUSIVE (ADD COLUMN).

Webhook ingest (conversation upsert), the worker (turns, outbound gate) and staff edits block
while it runs. Tables are small, so the hold should be well under a second; the real risk is
queueing behind a long transaction. Runtime statement_timeout is 5 s; Meta retries webhooks;
jobs retry.

Steps (same pattern as migrations 007 and 010):

1. Quiet window, no demo traffic.
2. RDS snapshot (for example `manzil-pre-011-<date>`); wait until it is available.
3. Build the release from a clean worktree or `git archive`, never from the main checkout
   (001-004 are CRLF there). Compare the migration checksums in the image with RDS
   `schema_migration` (001 / 002 CRLF bytes are kept).
4. Run the migrator with the admin migrate.env and `PGOPTIONS='-c lock_timeout=3s'` so it fails
   fast instead of stalling. Expect 1 applied, 10 skipped.
5. A lock timeout or failed CHECK rolls everything back: investigate and retry.
6. Replace web / worker with the release image; check `/health` and a staff `#demo buttons`.

Rollback: code that does not know the new columns ignores them, so the previous image keeps
working on a migrated database. Dropping the columns needs a new migration; never edit 011 after
it is applied.

## 4. Device test (gate M1(e))

On each client (Android, iPhone, WhatsApp Web / Desktop), from an allowlisted staff phone:

1. `#demo fallback off`, then `#demo carousel`: record rendered / not rendered, crop of the
   1.91:1 card image, and that the DEMO mark is visible.
2. Tap More photos and Visit / Details on each card: the stub names the right action and listing.
   Record the webhook's interactive type for a carousel tap (not documented by Meta; the code
   reads `interactive.<type>.id`).
3. `#demo card`: one image card with three buttons.
4. `#demo cards`: summary text plus one image card per listing.
5. On a client that does not render the carousel: `#demo fallback on`, then `#demo carousel`
   must arrive as individual cards; `#demo fallback status` confirms.
6. If Meta refuses the carousel (outbound row `failed` with an http_ code), check
   `CAROUSEL_CARD_TYPE` in app/whatsapp.py first (docs/OPEN_ISSUES.md, Task 5.2 entry).

Keep screenshots outside Git, or remove phone numbers first.
