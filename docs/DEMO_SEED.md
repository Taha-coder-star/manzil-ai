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

## Resetting between test runs

`python -m app.demo_reset` puts the demo agency back to the dataset. It needs only
`DATABASE_URL` (the runtime login) and refuses any agency but the demo one. A dry run is the
default; `--apply` writes, in one transaction. It lists each listing by code, and phone numbers
are never printed.

```
python -m app.demo_reset --agency-id <demo-agency-uuid>            # dry run: read the plan
python -m app.demo_reset --agency-id <demo-agency-uuid> --apply    # write
python -m app.seed --agency-id <demo-agency-uuid>                  # check: all +0, no conflicts
```

Exit codes: 0 = plan clean / applied; 2 = conflicts (nothing written); 1 = error.

**Read the dry run first: every listing outside the dataset is retired, whoever added it.** Take
an RDS snapshot before the first `--apply`, and run it while nobody is messaging the demo number.

| What | Reset does | Why |
|---|---|---|
| Listing outside the dataset | Code becomes `TEST-<n>`, status WITHDRAWN, old code kept in `internal_notes` | The runtime cannot delete listings; the next `DH6` code is DH6-109 again |
| Seeded listing that changed | Dataset values back, `last_confirmed_at` = `created_at` minus the policy's days, version + 1 | An old Undo or draft stops with "later change" |
| Staff photo on a seeded listing | Row deleted (a cover it became is cleared); the S3 objects stay | Loaded demo photos have no `source_inbound_media_id` and are kept |
| Customer conversation | `customer_wa_id` becomes `retired-<id>`, closed; open lead and handoff closed | The phone's next message starts a new conversation with no shown or sent history |
| Staff and broker conversations | Kept | A new one would have a closed WhatsApp window (template path) |
| Open listing drafts, staff sessions | Cancelled, deleted | No half-finished work carries over |

Limits:
- No `property_event` is written, because "updated" is undoable and `#undo` would put the test
  value back. A listing's version can therefore move without an event; the older events stay.
- Lead numbers keep rising (`L-` codes do not restart).
- A customer's carousel fallback flag and language preference go with the old conversation.
- A pending or running job is a conflict: wait a moment and rerun. A handoff timer job is not,
  because it does nothing once its handoff is closed.
- A retired listing keeps its photos, and a reset never inserts: a missing seeded listing is a
  conflict, so run `app.seed` first.

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
checks, and secret-free output. `tests/test_demo_reset.py` covers the reset the same way.

## Day 1 reset review (2026-11-03)

The owner approved a migration-free demo exception: reset writes no property_event; keep
a dated dry-run/apply report and released commit instead. This is not a privacy deletion
workflow. Before changing anything (including dry run), non-demo property/project inventory
now refuses the operation. Former/inactive staff numbers reset as customers, consistent
with runtime routing; only active staff conversations retain their WhatsApp windows.
See DAY1_STABILISATION.md for snapshot, quiet-window and verification gates.
