# Open issues

- Webhook sender-to-agency routing needs a trusted bootstrap mechanism under RLS. Missing context intentionally returns no rows; do not weaken policies or grant BYPASSRLS.
- Worker tenant discovery and later job claiming (Task 3.1) need a trusted discovery mechanism under RLS.
- GEMINI_API_KEY and ELEVENLABS_API_KEY were missing on the server in the last reported check; partner delivery is pending. Do not claim installation or retesting.
- Meta system-user token reportedly expires 2026-12-04; verify privately and arrange renewal before expiry.
- Task 2.3: code done (see docs/WEBHOOK.md); WEBHOOK_VERIFY_TOKEN still needs private server configuration, deployment and Meta dashboard verification. Task 2.4 (idempotent ingestion) not started; until then authenticated POSTs are acknowledged and discarded, so do not subscribe the messages field yet.
- Remaining team-phone hello_world receipts are unconfirmed; new_lead_alert template approval was pending.
- Voice must reject silence before provider calls; current provider limits/models require verification when voice work starts. Demo data remains fictional and team-only.
- Local pre-commit secret hook deferred; CI scans full history with Gitleaks.
- Server checkout has no .git; deployment uses an approved selective archive through EC2 browser terminal, not git pull. See ignored local infrastructure notes.
