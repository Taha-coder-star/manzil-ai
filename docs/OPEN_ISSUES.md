# Open issues

- Webhook routing uses the demo env map (Task 2.4). A DB/platform resolver is needed before onboarding several agencies; replace EnvSenderResolver only.
- Worker tenant discovery uses the env sender map (Task 3.1); it moves with the webhook resolver when a DB/platform resolver replaces EnvSenderResolver.
- Migrations 003 (job.conversation_key) and 004 (outbound delivery status) are not yet applied on RDS; apply them with the admin migrate.env before deploying the 3.1 worker / 3.2 gateway.
- Worker has no lease heartbeat: a job running longer than 300 s with no conversation key could be re-run by the reaper. Fine for the demo (status jobs are fast and idempotent).
- GEMINI_API_KEY and ELEVENLABS_API_KEY were missing on the server in the last reported check; partner delivery is pending. Do not claim installation or retesting.
- Meta system-user token was regenerated on 2026-10-08 (after the exposure) with no expiry. A non-expiring token must be rotated immediately if ever exposed (revoke all, then generate).
- The Meta app webhook subscribes 10 fields; only messages is handled (others are skipped with 200). Untick the rest to reduce noise.
- Remaining team-phone hello_world receipts are unconfirmed; new_lead_alert template approval was pending.
- Voice must reject silence before provider calls; current provider limits/models require verification when voice work starts. Demo data remains fictional and team-only.
- Local pre-commit secret hook deferred; CI scans full history with Gitleaks.
- Server checkout has no .git; deployment uses an approved selective archive through EC2 browser terminal, not git pull. See ignored local infrastructure notes.
- Send-time gate reads conversation.mode until Task 3.4 adds control_state (HANDOFF_PENDING must behave like HUMAN_OWNED); move the check in app/outbound.py then. A control change committed during the HTTP call itself is not seen (window bounded by one call).
- WhatsApp token is the env META_ACCESS_TOKEN for the demo; per-tenant encrypted tokens (C15, tenant_secret) are deferred.
- Outside the 24 h window session sends are recorded failed/outside_window; template fallback (new_lead_alert) arrives with handoff on Day 11.
- '#demo image' needs a fictional, DEMO-watermarked demo/echo-test.jpg in the bucket (upload needs AWS approval).
