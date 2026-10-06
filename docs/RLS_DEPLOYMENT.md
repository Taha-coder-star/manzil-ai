# Task 2.2 runtime-role deployment

AWS deployment was approved and completed on 2026-10-06. Migration 002 applied
and repeated successfully. Private runtime activation passed. Both recreated
runtime containers connected as manzil_app and confirmed FORCE RLS and zero rows
without tenant context across all nine tables. The public /health endpoint
returned HTTP 200 with database and bucket checks ok; Caddy, web, and worker
remained running. See ignored infrastructure notes for server metadata and the
selective browser-terminal transfer workflow. Do not merge without separate approval.

## Design and prerequisites

- Migration 002 creates or validates manzil_app, grants table DML and schema usage,
  and enables FORCE RLS for all nine tables. It grants no schema creation, table
  ownership, TRUNCATE, or migration-ledger access. Unexpected role memberships or
  existing policies abort the transaction; investigate rather than weakening checks.
- Run migrations as the existing schema/table owner with CREATEROLE, separately
  from the runtime login. No PostgreSQL superuser creation is needed. On RDS use
  the approved migration administrator; its exact permissions must be verified at
  deployment. Admin access to the migration ledger remains intact. FORCE RLS can
  block a non-bypass table owner's data access; administrative data operations need
  a separately reviewed workflow, not a runtime bypass grant.
- PostgreSQL's default database CONNECT is normally sufficient. If CONNECT was
  revoked, the administrator must grant it explicitly to manzil_app for this database.
- These policies trust the application's authorized tenant selection. The shared
  runtime login can set the tenant GUC; RLS is not protection against arbitrary SQL
  execution with stolen runtime credentials. Resolve tenant identity through trusted
  routing before calling tenant_transaction. Never accept a customer's agency UUID
  as authorization. Session-level tenant settings and nested transactions are rejected.
- Role creation is cluster-wide. Do not reuse manzil_app for unrelated applications
  or run concurrent test suites that change its test password on the same cluster.

## Approved deployment procedure

1. Transfer the reviewed release files with a selective archive through the approved
   browser terminal. Include app/db.py, deploy/activate_rls.py, migrations/002_tenant_rls.sql and any
   changed release files. Do not transfer .env, .local, test artifacts, or credentials.
   Keep migration 001 byte-for-byte unchanged and include it in the image.
   If the archive is extracted under umask 077, set only transferred source files
   to mode 0644 and their code directories to 0755 before building; appuser must
   read Python/SQL files. Do not relax .env or private administrator file modes.
2. Keep the migration administrator's DATABASE_URL in a separate owner-readable
   environment file outside the checkout and runtime compose configuration (mode
   0600 in a directory of mode 0700). Provision it privately; do not echo, paste into
   chat, put on a command line, or log it. The runtime .env must never contain this
   admin connection after the switch. Disable shell tracing and do not use commands
   that dump Docker environment configuration.
3. From the server checkout, build the image:

   ```sh
   docker compose -f deploy/compose.yml --env-file .env build
   ```

4. Run migrations with the separate administrator file (replace the file path only):

   ```sh
   docker run --rm --env-file /private/path/migrate.env --entrypoint python manzil-ai:latest -m app.migrate
   ```

   On the current deployed schema expect one applied, one skipped. Repeat the same
   command: expect zero applied, two skipped. Stop if checksums or permissions fail;
   never edit an applied migration or its ledger to bypass the failure.
5. Keep the original administrator URL as the only line in a protected migration
   environment file at `/home/ubuntu/.config/manzil/migrate.env` (directory mode
   0700, file mode 0600). Run `sudo python3 deploy/activate_rls.py` from the server
   checkout. The script creates a private backup of .env, generates a random
   password, hashes it client-side with SCRAM before ALTER ROLE, verifies a direct
   restricted login and missing-context zero rows, then replaces only the DATABASE_URL
   line atomically and sets .env to mode 0600. It prints no credentials or connection strings. Retain the
   protected admin file for future migrations; do not put its URL back into runtime
   .env. Keep provider variables and port configuration intact.
6. Recreate both runtime containers:

   ```sh
   docker compose -f deploy/compose.yml --env-file .env up -d --force-recreate web worker
   ```

7. Verify the actual runtime connection from each container. Run the following once
   with `web`, then again with `worker`; it prints identities and boolean flags only:

   ```sh
   docker compose -f deploy/compose.yml exec -T web python - <<'PY'
   import os
   import psycopg
   try:
       with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5) as conn:
           row = conn.execute('''
               SELECT session_user, current_user, rolsuper, rolbypassrls,
                      rolcreatedb, rolcreaterole
               FROM pg_roles WHERE rolname = current_user
           ''').fetchone()
           assert row == ('manzil_app', 'manzil_app', False, False, False, False)
           assert conn.execute('SELECT current_agency_id()').fetchone() == (None,)
           assert conn.execute('SELECT count(*) FROM agency').fetchone() == (0,)
           print(row)
   except Exception as exc:
       print('Runtime verification failed:', type(exc).__name__)
       raise SystemExit(1) from None
   PY
   ```

8. Check the public /health endpoint returns HTTP 200 and both database/bucket checks
   are ok. Confirm both containers remain running. A healthy SELECT 1 alone does not
   prove restricted credentials; retain the identity verification result too.

If the switch fails, retain diagnostics containing exception types only, stop further
deployment changes, and review the cause. Do not silently restore elevated runtime
credentials. This change deliberately leaves webhook routing and worker discovery
for later tasks (docs/OPEN_ISSUES.md).

## Local verification

Use a dedicated loopback PostgreSQL 18 database named manzil_schema_test. Configure
TEST_DATABASE_URL privately with its test administrator, set REQUIRE_DATABASE_TESTS=1,
then run `python -m ruff check app tests deploy` and `python -m pytest -q`. Tests create/drop
isolated schemas and provision random temporary manzil_app passwords, cleared on
fixture teardown. The cluster role remains for migration repeatability. CI requires
the database, so absent configuration fails rather than silently skipping checks.

References: [PostgreSQL RLS](https://www.postgresql.org/docs/18/ddl-rowsecurity.html),
[role creation and private password provisioning](https://www.postgresql.org/docs/18/sql-createrole.html),
[Psycopg transaction contexts](https://www.psycopg.org/psycopg3/docs/basic/transactions.html),
[RDS administrator permissions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Roles.rds_superuser.html).
