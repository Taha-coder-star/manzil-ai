# Onboarding an agency

Audit WP5 (MA-008, MA-016, routing half of MA-009). Decisions: BD-5 A (the operator runs
one command per agency, per staff member and per area list), BD-6 shared (one deployment
and database for every agency, isolated by row-level security), BD-7 A (every agency
number sits in the operator's WhatsApp Business Account; one token), BD-13 (any admin or
agent may change any listing of their agency; a leaver's pending leads go to an admin).

Nothing here needs a restart of web or worker, except loading new areas (the worker
reads the gazetteer at start).

## Credentials the commands use

| Variable | Login | Used by |
|---|---|---|
| `DATABASE_URL` | runtime `manzil_app` | `provision_agency` (agency row), `manage_staff` |
| `SEED_OWNER_DATABASE_URL` | schema owner (the migration login) | `provision_agency` (route), `load_gazetteer` |

Run each command inside a container of the released image with both env files, for
example `sudo docker run --rm --env-file .env --env-file ~/.config/manzil/task43-seed.env
--entrypoint python manzil-ai:latest -m app.<command> ...`. Never print either file.
Commands print phone numbers as their last four digits only.

## Order

The order closes the window in which messages to a new number are dropped
(`ALERT unknown_sender` in the web log).

0. **Once, after migration 021 is live: route the existing demo agency.** While
   `sender_route` is empty the runtime uses `WEBHOOK_SENDER_AGENCIES`; the first route
   switches routing to the table, so the live agency must be in it first.

   ```
   python -m app.provision_agency --agency-id <demo agency uuid> --phone-number-id <its id>          # plan
   python -m app.provision_agency --agency-id <demo agency uuid> --phone-number-id <its id> --apply
   ```

1. **Areas** (only if the agency works outside the areas already loaded). Prepare
   `locations.csv`, `location_aliases.csv`, `location_neighbors.csv` in the `demo/`
   format (every location needs its official alias; `docs/INVENTORY_SCHEMA.md`).

   ```
   python -m app.load_gazetteer --dir <folder>            # plan; exit 2 = conflicts, nothing written
   python -m app.load_gazetteer --dir <folder> --apply
   ```

   Then restart the worker so it reads the new gazetteer.

2. **Agency and route.** `--apply` creates the agency (runtime login, inside RLS) and then
   writes its route (owner login) after checking the agency owns the number. If the
   second step fails, rerun with `--agency-id` and the printed id.

   ```
   python -m app.provision_agency --name "Acme Realty" --slug acme --phone-number-id <id> --waba-id <waba>
   python -m app.provision_agency --name "Acme Realty" --slug acme --phone-number-id <id> --waba-id <waba> --apply
   ```

   The agency's name appears in customer text ("Main Acme Realty ka AI assistant hoon").
   `#demo` staff commands work only for the agency named `Manzil Demo Realty (DEMO)`.

3. **Staff.** At least one admin; brokers receive leads.

   ```
   python -m app.manage_staff --agency-id <uuid> add --name "Sara" --phone 923001234567 --role admin
   python -m app.manage_staff --agency-id <uuid> add --name "Bilal" --phone 923001234568 --role broker
   python -m app.manage_staff --agency-id <uuid> list
   ```

   A new staff member is recognised from their next message. Brokers added this way have
   no `areas_covered`; they get leads on listings assigned to them, else the least-loaded
   broker or admin is chosen.

4. **Meta connection last.** Register the number in the operator's WhatsApp Business
   Account and point it at the same webhook. Within a minute of step 2 web and worker
   serve it; send a test message from a team phone and expect a reply.

## Leavers

```
python -m app.manage_staff --agency-id <uuid> deactivate --phone 923001234568
```

Their pending leads pass to the first active admin (escalated; the cards reach the admin
with their next message). Leads they had already accepted are listed: they stay with the
leaver until released (48 h without activity) or closed, so tell the admin. With pending
leads and no other active admin, nothing is written.

## Rollback

Routing: delete the rows from `sender_route` (owner login) and the runtime falls back to
`WEBHOOK_SENDER_AGENCIES` within a minute. Migration 021 is additive.
