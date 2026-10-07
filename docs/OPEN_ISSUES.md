# Open issues

- Webhook routing uses the demo env map (Task 2.4). A DB/platform resolver is needed before onboarding several agencies; replace EnvSenderResolver only.
- Worker tenant discovery uses the env sender map (Task 3.1); it moves with the webhook resolver when a DB/platform resolver replaces EnvSenderResolver.
- Migration 003 (job.conversation_key) is not yet applied on RDS; apply it with the admin migrate.env before deploying the 3.1 worker or the ingest change.
- Worker has no lease heartbeat: a job running longer than 300 s with no conversation key could be re-run by the reaper. Fine for the demo (status jobs are fast and idempotent).
- GEMINI_API_KEY and ELEVENLABS_API_KEY were missing on the server in the last reported check; partner delivery is pending. Do not claim installation or retesting.
- Meta system-user token was regenerated on 2026-10-08 (after the exposure) with no expiry. A non-expiring token must be rotated immediately if ever exposed (revoke all, then generate).
- The Meta app webhook subscribes 10 fields; only messages is handled (others are skipped with 200). Untick the rest to reduce noise.
- Remaining team-phone hello_world receipts are unconfirmed; new_lead_alert template approval was pending.
- Voice must reject silence before provider calls; current provider limits/models require verification when voice work starts. Demo data remains fictional and team-only.
- Local pre-commit secret hook deferred; CI scans full history with Gitleaks.
- Server checkout has no .git; deployment uses an approved selective archive through EC2 browser terminal, not git pull. See ignored local infrastructure notes.
