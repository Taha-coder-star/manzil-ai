# Day 12.3 / 12.4 failure handling

Two retryable initial LLM failures fix the existing localized apology and enqueue
one `ai_recovery` job 30 seconds later in the same transaction. Migration 019
adds recovery state under FORCE RLS. The source turn and apology remain immutable.
Refusal and malformed/non-retryable errors never schedule recovery. The active
renderer remains deterministic; the optional grounding wrapper still regenerates
once and then uses its deterministic fallback.

Recovery uses the original tool identity, separate usage keys (the recovery job),
and `recovery:{source_turn}:{seq}` outbound keys. It rechecks role, control state,
reply window, generation, original substantive outbound attempts and newer inbound
events before inference and dispatch. Newer status events do not cancel it.
An open original turn defers recovery without consuming attempts. A failed original
turn cancels recovery. Recovery does not send another apology or chain AI retries.
Planning is at most once: interrupted planning alerts and ends; a stored plan
resumes transport without calling the model again.

STT exhaustion retains the existing `voice_failed` please-type message and speech
alert; unusable voice has no requirement effects. Download failures retain the
existing resend/type or staff missing-photo notice. Media downloads allow five
job attempts and re-query the media ID on each retry (including a fresh query on
expired URLs). Final-attempt lease expiry also closes pending media as failed.

Retryable WhatsApp send errors promote only that job's transport budget to five
total attempts, using the existing 5-second exponential backoff, 300-second cap,
and +/-20% jitter. Other jobs retain their configured budget (normally three).
Stored turn plans may resume beyond the three-attempt planning limit. Ambiguous
sends remain unknown and are never blindly resent; confirmed sequence keys dedupe
partial sends. Permanent send failures and closed windows remain terminal.

Every committed dead transition emits an ERROR `dead_letter` event with agency,
job ID/type, attempts, max attempts and sanitized reason. Orchestrator transitions
enqueue a deduplicated alert job transactionally; worker/reaper transitions log
after commit. Logs are best effort across process death and are not an exactly-once
external notification system. No raw content or phone numbers are included.

Release requires migration 019 before switching web/worker images. Compare all
existing migration bytes against the applied ledger; never replace 001-018.
Rollback restores the previous image while retaining 019 and its ledger entry.
No live fault injection, merge, or deployment is part of local implementation.
