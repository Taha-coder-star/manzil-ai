# Demo seed loader (Task 4.3)

`python -m app.seed` loads the validated `demo/` dataset (docs/DEMO_DATASET.md) into an
existing demo agency. A dry run is the default; `--apply` writes. Nothing here has been run
against production; that needs separate approval.

## Inputs

| Input | Meaning |
|---|---|
| `--agency-id <uuid>` | The existing agency named "Manzil Demo Realty (DEMO)". The loader never creates an agency (use `app.provision_agency`). |
| `--apply` | Write missing rows. Without it: read-only plan. |
| `--demo-dir <path>` | Dataset directory; default `demo/` next to `app/` (`/app/demo` in the image). |
| `SEED_OWNER_DATABASE_URL` | Schema-owner login (the migration login): writes the shared gazetteer. |
| `DATABASE_URL` | Runtime `manzil_app` login: writes staff, projects and listings under FORCE RLS. |
| `DEMO_ADMIN_PHONE`, `DEMO_STAFF_PHONE`, `DEMO_BROKER_PHONE` | Staff phones (names from `demo/staff.csv`). Country code first; spaces, dashes, brackets and a leading `+` are removed, and the result must be 10-15 digits (WhatsApp wa_id form). |

All inputs (dataset, phones, agency id, both URLs, both logins) are checked before anything
is written. Both URLs must reach the same database and schema; the runtime login must not be
a superuser, have BYPASSRLS or own the tables, and the owner login must be able to write the
gazetteer. Phone numbers, URLs and raw database errors are never printed (errors show the
class, SQLSTATE and constraint name only).

## Examples (placeholders only)

```
set SEED_OWNER_DATABASE_URL=<schema-owner-url>
set DATABASE_URL=<runtime-manzil_app-url>
set DEMO_ADMIN_PHONE=<admin-phone>
set DEMO_STAFF_PHONE=<staff-phone>
set DEMO_BROKER_PHONE=<broker-phone>

python -m app.seed --agency-id <demo-agency-uuid>            # dry run: full plan, no writes
python -m app.seed --agency-id <demo-agency-uuid> --apply    # insert missing rows
python -m app.demo_dataset                                   # validate the files only
```

Exit codes: 0 = plan clean / applied; 2 = conflicts (nothing written by the conflicting
phase); 1 = error (inputs, connection, wrong agency, or a rolled-back phase).

Output, per phase: `+N` rows to insert (inserted with `--apply`), `=N` rows already present
and left unchanged, and each conflict.

## How it runs

1. **Preflight (always).** The full plan for both phases in read-only transactions that
   are rolled back. Gazetteer rows that do not exist yet are resolved in memory, so a dry run
   on an empty database still plans every listing. Any conflict or error stops here, before
   either phase writes.
2. **Gazetteer phase** (`--apply`): owner connection, one transaction, advisory lock
   `60172043`; the plan is rebuilt under the lock; missing locations, aliases and neighbours
   are inserted; commit.
3. **Tenant phase** (`--apply`): runtime login through `app.db.tenant_transaction`, one
   transaction, advisory lock `(4301, hashtext(agency_id))`; the agency is checked; the plan
   is rebuilt under the lock; missing staff, projects and listings are inserted; commit.

The two phases are **not globally atomic**. If the tenant phase fails or finds a conflict
after the gazetteer phase committed, the gazetteer rows stay (they are shared and correct);
fix the cause and rerun. Concurrent runs are serialised by the locks and do not duplicate rows.

## Rules

- **Insert missing rows only.** Identities: location (parent, lower(name)) and code; alias
  (location, alias_norm, context) with a NULL context matched as NULL; neighbour (ordered
  pair); staff (agency, digits of the phone, as `app.media.staff_id_for` matches);
  project (agency, lower(name)); listing (agency, listing_code). Names and alias norms are
  normalised by the database itself (`lower`, the `alias_norm` expression).
- **Existing rows are never updated.** A field the dataset specifies that differs on an
  existing row (a listing staff edited, for example) is a conflict: it is reported, the row
  stays as it is, and that phase inserts nothing. Blank dataset cells are unknown and are
  not compared. Timestamps (`created_at`, `updated_at`, `last_confirmed_at`) are never
  compared.
- **Staff.** A row whose phone matches is reused unchanged (name, role, phone) when its role
  is the expected one and it is active. Conflicts: the phone belongs to a different role or
  an inactive row; two rows match the same phone; or no row matches but an active row with
  that role exists (the variable probably holds the wrong number). Production: the existing
  agent (the owner's phone) is the dataset's agent, so `DEMO_STAFF_PHONE` must be that number.
- **Freshness.** Set on first insertion only: DH6-104 gets the insert time minus 35 days,
  the other listings the insert time (`demo/freshness_policy.json`).
- **No audit events.** The seed does not write `property_event` rows.

## Supplying demo/ to the loader

- The image now copies `demo/` (`Dockerfile`: `COPY demo ./demo`), and CI runs
  `python -m app.demo_dataset` inside the built image.
- Production releases are layered on the running image (docs/OPEN_ISSUES.md: the next
  release builds FROM manzil-ai:4.2-f65094d; never build from the E:\manzilai main checkout,
  whose 001-004 are CRLF). The seed layer adds only `app/demo_dataset.py`, `app/seed.py` and
  `demo/` (to `/app/app/` and `/app/demo/`); no migration file changes, so the RDS checksum
  comparison is unaffected. Building, shipping and running it are separate approvals.
- For a one-off run without a new image, `--demo-dir` can point at a mounted copy of `demo/`.

## Tests

`tests/test_seed.py` (needs `TEST_DATABASE_URL`; each test migrates a new schema in the
disposable `manzil_schema_test` database): fresh agency, existing agent reuse, rerun, dry
run on an empty database, edited listing, conflicts found on recheck, forced insert failures
in each phase, gazetteer committed then tenant failure and recovery, wrong agency, staff
role / phone conflicts, cross-tenant isolation, three concurrent runs, login and same-database
checks, and secret-free output.
