Read docs/PROJECT_STATE.md before starting any task.

- Use Windows CMD for local project commands (from PowerShell, invoke `cmd /d /c`).
- Local Python is 3.14; Docker and CI use Python 3.12. Keep code compatible with both.
- Run `.venv\Scripts\python.exe -m ruff check app tests` and `.venv\Scripts\python.exe -m pytest -q` from the repo root. CI uses `python` instead.
- Database tests require a disposable local PostgreSQL database through TEST_DATABASE_URL. Never point tests at production.
- Never print credentials or ask the user to paste them. Never store passwords, tokens, or connection strings in memory files.
- Never edit an applied migration; add a new numbered migration.
- Never touch AWS or merge without the user's approval.
- Preserve unrelated changes and stashes; do not restore the saved environment-example stash without a reason.
- After meaningful steps update docs/PROJECT_STATE.md and docs/TASK_LOG.md; leave accurate resume instructions at session end.
