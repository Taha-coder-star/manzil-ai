# Open issues

- Webhook routing uses the demo env map (Task 2.4). A DB/platform resolver is needed before onboarding several agencies; replace EnvSenderResolver only.
- Worker tenant discovery and later job claiming (Task 3.1) need a trusted discovery mechanism under RLS.
- GEMINI_API_KEY and ELEVENLABS_API_KEY were missing on the server in the last reported check; partner delivery is pending. Do not claim installation or retesting.
- Meta system-user token was regenerated on 2026-10-08 (after the exposure); record its expiry and arrange renewal before it.
- The Meta app webhook subscribes 10 fields; only messages is handled (others are skipped with 200). Untick the rest to reduce noise.
- Remaining team-phone hello_world receipts are unconfirmed; new_lead_alert template approval was pending.
- Voice must reject silence before provider calls; current provider limits/models require verification when voice work starts. Demo data remains fictional and team-only.
- Local pre-commit secret hook deferred; CI scans full history with Gitleaks.
- Server checkout has no .git; deployment uses an approved selective archive through EC2 browser terminal, not git pull. See ignored local infrastructure notes.
