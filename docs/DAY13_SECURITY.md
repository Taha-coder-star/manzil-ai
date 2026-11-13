# Day 13 security checklist

Scope: tasks 13.1 and 13.2, local implementation only. AWS remains intentionally
stopped. No deployment, merge, production migration, live benchmark or fixture reset.

| Control | Evidence / verification |
| --- | --- |
| Raw-body signature rejection | `tests/test_webhook.py`: missing, malformed, wrong-secret and tampered-body cases; rejection occurs before ingest. |
| Customer/staff tool isolation | `tests/test_ai_tools.py`: declarations and execution allowlists; `tests/test_day13.py`: injected staff calls through the real customer worker and forged staff confirmation, with inventory/draft/audit assertions. |
| Tenant isolation | Existing restricted-login `tests/test_rls.py`, migration and tenant tests remain mandatory in CI (`REQUIRE_DATABASE_TESTS=1`). Migration 020 adds a column to the existing FORCE-RLS-protected shown_item table. |
| Log redaction | `app/log_safety.py`, installed by web import and runtime entrypoint: redact synthetic full phone numbers, credential query/assignment values, bearer tokens and exception text before handlers. `tests/test_day13.py` captures actual formatted logs. Logs must still avoid raw message bodies and provider responses at source. |
| Description sanitising | Existing `tests/test_staff_drafts.py` and `tests/test_staff_cards.py` cover instruction/contact/link/code removal, legitimate text preservation and correction sanitising. Existing staff commit tests exercise save revalidation. |
| Secret scan | Existing CI retains pinned, redacted gitleaks Git-history scanning with full checkout history. Day 12 exact-head CI passed on 02304c2. A new local gitleaks run is unavailable: Docker pipe access denied and GitHub binary download blocked by network policy. HEAD remains exactly 02304c2 (verified locally), so its existing Git-history scan evidence still applies. Manual review on 2026-11-10 of the new/changed application code, migration, tests and task documentation found no production credential values; test phones/tokens are fictional. New exact-head CI is still required before release; no new gitleaks execution is claimed. |

Final automated acceptance: **3,982 passed, 42 platform skips, 0 failures/errors**
over 4,024 unique cases in 83 files. `../output/day13-validation.json` reconciles the
partition XML and latest correction runs without double-counting. Ruff and changed
implementation whitespace checks pass. Automated local acceptance is recorded in
`../output/day13-*.xml` and corresponding logs. Interrupted concurrent attempts are not acceptance evidence: database fixtures
change a global test-role password, so runs must be serial within a cluster.

## Stale-listing behavior and compatibility

- New customer property replies carry an internal listing guard. Photos and factual
  answers re-read current public inventory at dispatch; answers/cards are rebuilt from
  public facts. A hidden listing gets the localized unavailable line and Similar button.
- Similar runs the existing saved-requirements search only after a tap. Current search
  clarification, budget relaxation and ACTIVE-only behavior are preserved.
- Property photos are checked before upload/presigning and again in the outbound path,
  including delayed link fallback. System/staff media retain their existing behavior.
- Migration 020 adds nullable `shown_item.rendered_price_pkr`. New search plans carry
  the rendered price into delivered history. Old rows/plans have no trustworthy price
  snapshot, so no old price or change is invented; a new search supplies a baseline.
- Changed prices produce a localized fixed update line before the property response,
  deduplicated per listing and turn. Guarded replies use current facts. Pending outbound
  audit payloads are refreshed to match a retry's actual content. Ambiguous sends are
  never blindly resent.
- Old stored detail payloads without guard metadata cannot be reconstructed reliably;
  the new answer guard applies to newly planned property replies. Existing raw property
  photo entries still pass through the outbound media guard.
- As with the existing control gate, a change committed after the final check but before
  the external send can cross a bounded HTTP-call window; no database lock is held over
  the provider request.

## Local re-confirm demo

Use fictional inventory in a disposable database. Mark one listing's
`last_confirmed_at` NULL/old, then send `#reconfirm DH6-101` as active staff. The prompt
shows code, type, purpose and price. Verify no property write before Confirm; Confirm
increments version, sets last_confirmed_at and records a `reconfirmed` event atomically.
Cancel and repeat taps do not write. A changed property version or non-ACTIVE listing
rejects confirmation. Draft ownership binds the tap to its staff member and tenant.
The worker acceptance tests exercise this demo without AI or paid services.

Re-confirmation uses existing update drafts and buttons, not a new model tool, scheduler
or bulk operation. Apply 020 before using this runtime against a new environment. Applied
migrations 001-019 are immutable. Live WhatsApp verification remains pending authorization
for a later restart/release; tasks 13.3 and 13.4 are not part of this change.
