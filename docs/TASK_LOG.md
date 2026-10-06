# Task log

- 2026-10-06: Published server activation helper as commit 9ca4c6a; GitHub Actions run 37438232576 passed. Regenerated selective seven-file archive and sent EC2 terminal transfer/checksum/migration-001 comparison commands. Awaiting the user's safe verification output before extraction. No AWS mutation or merge.

- 2026-10-06: Added deploy/activate_rls.py for private server-side role password generation, SCRAM verifier creation, restricted login validation, protected admin backup, and atomic runtime .env switch. Added parser/URL tests and changed disposable RLS tests to use client-side SCRAM. Ruff, 103 local tests, 103 Docker Python 3.12 tests, and 34-file Gitleaks scan passed. Deployment archive and remote CI still need refresh; AWS remains read-only.

- 2026-10-06: AWS read-only preflight via user's EC2 terminal: sudo Docker works; web/worker/Caddy running; public schema, manzil_admin has CREATEROLE and owns agency, one migration recorded. Adding a private activation helper to generate a password on the server, hash it client-side, verify restricted login, and atomically switch runtime .env. No AWS mutation yet; final tests/CI and release archive regeneration pending.

- 2026-10-06: Committed Task 2.2 and approved memory files as 34494fb, pushed feature/task-2-2-rls, and opened draft PR #4. GitHub Actions run 37436313894 passed. Prepared selective, credential-free release archive in ignored .local/task-2-2-release.b64. AWS deployment awaits the user's authenticated EC2 Instance Connect terminal; no server/database changes or merge occurred.

- 2026-10-06: User approved including memory files in Task 2.2 branch and deploying Task 2.2 to AWS. Beginning branch publication and deployment; no merge approval given. Verification results from prior session remain valid pending remote CI and live checks.

- 2026-10-06: Final validation: Ruff passed; 98/98 tests passed on Python 3.14 and 98/98 on Docker Python 3.12 with PostgreSQL 18, no skips. Upgrade preserved rows; permission errors are credential-safe. Rebuilt image, verified packaging, web/worker and /live HTTP 200; removed temporary containers. Gitleaks found no secrets. Migration 001 unchanged; git diff --check passed; nothing staged/committed. Memory approval, remote CI, and authorized AWS runtime switch remain pending.

- 2026-10-06: Expanded hardening coverage passed: 96 tests on Python 3.14 and 96 in the Python 3.12 Docker image, no skips. Verified SQL/helper packaging, temporary web /live HTTP 200, and both runtime roles running from the same image; removed temporary containers. Gitleaks history (9 commits) and current 32-file scan found no leaks. Final review added a schema-v1 upgrade/data-preservation test and safe migration-permission guidance; rerun pending.

- 2026-10-06: Implemented forced tenant RLS, restricted login, validated transaction-local helper, migration test updates, and CI database requirement. Installed missing declared local dependencies. Ruff passed; 84 PostgreSQL-backed tests passed with no skips (one upstream deprecation warning). Added deployment instructions and excluded .local from Docker build context. Docker/runtime verification pending.

- 2026-10-06: Added .local/ to Git info/exclude and confirmed ignored status. Manually scanned 26 tracked/proposed files without exposing values; only blank/example settings and known test credentials found. Docker access succeeded after permission retry; disposable PostgreSQL is ready. Drafted migration 002 and tenant transaction helper; migration 001 untouched. Tests pending.

- 2026-10-06: Read handoff and inspected branch, Git status/history, app, migrations, tests, workflow, Dockerfile, dependency and deployment files. Clean feature/task-2-2-rls at 8cdcd92; only migration 001 exists. Python/Ruff/pytest versions verified. Docker access denied by sandbox; Gitleaks not installed. Created draft project memory before coding; approval to include memory in branch is pending. No tests run yet; no AWS changes.
