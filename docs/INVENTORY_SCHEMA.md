# Inventory schema (Task 3.3, migration 005)

Source: 3-Week Execution Plan 3.3 / 4.3 / 7.1 / 8.1 / 9.2, Demo Scope C4, C5, C7, C12, S4-S8, M2.
The RFC data-model and Red-Team documents were not available; gaps are recorded as
assumptions in docs/DECISIONS.md. Schema only: no seeds, search, media processing,
save handler or Task 3.4 tables.

## Overview

| Table | Kind | Runtime (manzil_app) privileges | RLS |
|---|---|---|---|
| location | shared gazetteer | SELECT | none (no tenant column) |
| location_alias | shared gazetteer | SELECT | none |
| location_neighbor | shared gazetteer | SELECT | none |
| project | tenant | SELECT, INSERT, UPDATE | ENABLE + FORCE, tenant_isolation |
| property | tenant | SELECT, INSERT, UPDATE | ENABLE + FORCE, tenant_isolation |
| property_media | tenant | SELECT, INSERT, UPDATE, DELETE | ENABLE + FORCE, tenant_isolation |
| property_event | tenant, append-only | SELECT, INSERT | ENABLE + FORCE, tenant_isolation |
| listing_draft | tenant | SELECT, INSERT, UPDATE, DELETE | ENABLE + FORCE, tenant_isolation |
| staff_session | tenant | SELECT, INSERT, UPDATE, DELETE | ENABLE + FORCE, tenant_isolation |
| v_public_listing | view (security_invoker) | SELECT | base-table RLS applies |

PUBLIC has no privileges on any of them. tenant_isolation follows migration 002 exactly:
`FOR ALL TO manzil_app USING (agency_id = current_agency_id()) WITH CHECK (agency_id = current_agency_id())`.
Missing context returns zero rows; a malformed context raises an error (the UUID cast fails closed).

Status and enum values are lowercase TEXT with CHECK constraints, matching schema v1.
`updated_at` is maintained by application code (no triggers), as in schema v1.

## Shared gazetteer

Loaded by the migration/admin role (Task 4.3). Never written by the runtime role.

### location

| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| parent_id | UUID NULL | NULL only for a city |
| level | TEXT NOT NULL | `city`, `area`, `sub_area` |
| parent_level | TEXT NULL | level of the parent; set together with parent_id |
| name | TEXT NOT NULL | canonical display name, non-blank |
| in_coverage | BOOLEAN NOT NULL DEFAULT TRUE | FALSE lets the resolver answer OUT_OF_COVERAGE (e.g. a known Lahore area) instead of UNKNOWN |
| created_at, updated_at | TIMESTAMPTZ | |

- Hierarchy is strict: city (no parent) -> area -> sub_area. `CHECK` pins
  (level, parent_level) to those three combinations and `FOREIGN KEY (parent_id, parent_level)
  REFERENCES location (id, level)` proves the parent really has that level (`UNIQUE (id, level)`).
  Because levels strictly increase, cycles and self-parenting are impossible.
  The CHECK also prevents the MATCH SIMPLE gap (parent_id set while parent_level is NULL).
- Changing the level of a location that has children fails (the children's FK no longer matches).
- Names are unique per parent, case-insensitively, with `NULLS NOT DISTINCT`, so two
  top-level cities cannot share a name either. The same name under different parents is allowed
  (e.g. "Phase 8" under two different housing societies).
- Examples (illustrative, not seeded): Karachi (city) -> Clifton (area) -> Block 8 (sub_area);
  Karachi -> DHA -> Phase 6.

### location_alias

| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| location_id | UUID NOT NULL FK location | canonical target |
| alias | TEXT NOT NULL | as written (English spelling, Roman Urdu, Urdu script, STT variant) |
| alias_norm | TEXT GENERATED STORED | `lower()` of the alias with whitespace trimmed and collapsed |
| context_parent_id | UUID NULL FK location | alias only valid inside this area (e.g. "phase 8" inside DHA); NULL = global |
| created_at | TIMESTAMPTZ | |

- Ambiguity is supported on purpose: the same alias_norm may map to several locations
  (the resolver then returns AMBIGUOUS). Uniqueness is only
  `(location_id, alias_norm, context_parent_id) NULLS NOT DISTINCT`, which blocks exact duplicates
  (including duplicate global aliases).
- `context_parent_id <> location_id`. That the context is an ancestor of the target is a
  seed-quality rule (Task 4.3), not enforced by the database.
- Indexes: GIN `gin_trgm_ops` on alias_norm (trigram matching for Task 7.1), B-tree on
  (alias_norm, context_parent_id) for exact lookups, plus FK indexes.
- "Bare level + number in the active area" (7.1) is modelled as context aliases; no extra
  designator columns.

### location_neighbor

| Column | Type | Notes |
|---|---|---|
| location_a_id | UUID NOT NULL FK location | |
| location_b_id | UUID NOT NULL FK location | |
| created_at | TIMESTAMPTZ | |

- Adjacency is symmetric. Each pair is stored once in canonical order:
  `CHECK (location_a_id < location_b_id)` and `PRIMARY KEY (location_a_id, location_b_id)`.
  This rejects self-neighbours, duplicates and the reversed duplicate.
- Neighbours of X: `SELECT location_b_id WHERE location_a_id = X UNION SELECT location_a_id WHERE location_b_id = X`
  (index on location_b_id supports the second branch). No relationships are seeded here.

### pg_trgm

The migration installs `pg_trgm` only if it is absent, into the migration schema, and then looks up
the schema the extension actually lives in to build the index, so a pre-installed extension in another
schema (e.g. `public`) also works. pg_trgm is a trusted extension: creating it needs CREATE on the
database (or a superuser). If the extension is absent and the migration role lacks that privilege, the
migration stops with a clear error and rolls back. No broad privilege is granted by the migration or tests;
production can either let the migration owner create it or have a DBA pre-install it.
In tests the extension lives in the per-test schema and is dropped with it.

## Agency-owned tables

Every table has `agency_id UUID NOT NULL REFERENCES agency(id)` and `UNIQUE (agency_id, id)`.
Every tenant-to-tenant reference is a composite FK including agency_id, so a row of agency A
cannot point at a row of agency B. Nullable composite FKs use MATCH SIMPLE: when the referencing
id is NULL the FK is not checked (nothing is referenced); agency_id is never NULL.

### project

id, agency_id, name (non-blank, unique per agency case-insensitively), location_id NULL FK location,
developer_name NULL, construction_status NULL (`ready`, `under_construction`), is_demo, created_at, updated_at.

### property

| Column | Type | Null | Notes |
|---|---|---|---|
| listing_code | TEXT | NOT NULL | human ID `{AREA}-{seq}` (e.g. DH6-109), `^[A-Z0-9]+-[0-9]+$`, unique per agency |
| status | TEXT | NOT NULL | `active`, `reserved`, `sold`, `rented`, `withdrawn`; no default |
| purpose | TEXT | NULL | `sale`, `rent` |
| property_type | TEXT | NULL | `house`, `apartment`, `portion`, `office`, `shop`, `plot` |
| category | TEXT | NULL | `residential`, `commercial` |
| location_id | UUID | NULL | FK location (any level) |
| project_id | UUID | NULL | composite FK project |
| price_pkr | BIGINT | NULL | > 0; whole rupees; sale = total price, rent = per month |
| area_value | NUMERIC(12,2) | NULL | > 0; as stated by staff |
| area_unit | TEXT | NULL | `sq_ft`, `sq_yd`, `marla`, `kanal`; set together with area_value |
| area_sqft | NUMERIC GENERATED STORED | NULL | canonical comparison size (see conversions) |
| bedrooms, bathrooms | SMALLINT | NULL | >= 0 |
| construction_status | TEXT | NULL | `ready`, `under_construction` |
| latitude, longitude | NUMERIC(9,6) | NULL | both or neither; range-checked |
| public_features | JSONB | NOT NULL DEFAULT '{}' | customer-safe known facts only (C12 known_fields); an absent key means unknown |
| source_text | TEXT | NULL | internal: original staff description |
| internal_notes | TEXT | NULL | internal |
| is_demo | BOOLEAN | NOT NULL DEFAULT FALSE | fictional demo data marker |
| version | INTEGER | NOT NULL DEFAULT 1 | > 0; bumped by every committed change |
| cover_media_id | UUID | NULL | composite FK to this property's own media |
| created_by_staff_id | UUID | NULL | composite FK staff |
| created_at, updated_at | TIMESTAMPTZ | NOT NULL | |

Unknown facts are NULL; nothing is defaulted to a made-up value.

**Incomplete vs publishable.** Incomplete inventory lives in `listing_draft` (free-form payload, no
fact constraints). A `property` row is committed inventory. Only `status = 'active'` makes a listing
customer-visible, so the publication rule applies to active rows only (Demo Scope S4 minimum-to-save):

```
status <> 'active' OR (
  purpose, property_type, location_id, price_pkr, area_value IS NOT NULL
  AND (property_type NOT IN ('house','apartment','portion') OR bedrooms IS NOT NULL)
)
```

"+ beds for residential" is read as the built residential types (house, apartment, portion);
a residential plot has no bedrooms. Non-active rows (reserved, sold, rented, withdrawn) may keep
unknown facts as NULL; promoting such a row to active must satisfy the rule.

**Size conversions (Pakistan-specific assumption).** `area_sqft = area_value x factor`:
sq_ft 1, sq_yd 9, marla 225, kanal 4500 (20 marla). 225 sq ft per marla is the plan's figure
(Task 6.2 "marla 225"); other regions use different marla sizes (e.g. 272.25). The stated value and
unit are always kept, so the canonical figure can be recomputed by a later migration if needed.

### property_media

id, agency_id, property_id NOT NULL (composite FK property), original_key NOT NULL (bucket key),
wa_image_key NULL, card_image_key NULL, content_sha256 NULL (64 hex), mime_type NULL, sort_order (>= 0),
created_at.

- `UNIQUE (agency_id, property_id, id)` is the target of the cover FK.
- `UNIQUE (agency_id, property_id, sort_order) DEFERRABLE INITIALLY IMMEDIATE`
  (`property_media_order_key`): one order slot per image; reorder by `SET CONSTRAINTS property_media_order_key DEFERRED`.
- `UNIQUE (agency_id, property_id, content_sha256)`: hash dedupe per property.
- Media processing, derivatives and the wa_media_id cache are Tasks 4.1 / 4.2 and may add columns
  in a later migration.

**Cover image.** `property.cover_media_id` has
`FOREIGN KEY (agency_id, id, cover_media_id) REFERENCES property_media (agency_id, property_id, id) ON DELETE SET NULL (cover_media_id)`.

- The cover must be media of the same agency and the same property (another property's or another
  agency's media fails).
- property and property_media reference each other; the cover FK is added after property_media exists.
- Deleting the cover media sets only cover_media_id to NULL (PostgreSQL 15+ column list); agency_id and id are untouched.
- Moving the cover media to another property (UPDATE property_id) is rejected while it is the cover.
- Deleting a property requires deleting its media first; the runtime role cannot delete properties at all.

### property_event (append-only audit, undo source)

id, agency_id, property_id NOT NULL, event_type (`created`, `updated`, `status_changed`, `media_added`,
`media_removed`, `undone`), actor_staff_id NULL, listing_draft_id NULL, from_version NULL, to_version NOT NULL,
changes JSONB object (field -> {old, new}), undoes_event_id NULL, created_at.

- Runtime privileges are SELECT and INSERT only: no UPDATE, DELETE or TRUNCATE.
- `undoes_event_id` uses `(agency_id, property_id, undoes_event_id) -> property_event (agency_id, property_id, id)`,
  so an undo can only reverse an event of the same property.
- `from_version < to_version` when from_version is set; `created` events have no from_version.
- `undoes_event_id` is set exactly for `undone` events.
- Retention: kept indefinitely for the demo (assumption). The FK from events keeps their property and
  draft from being deleted.

### listing_draft

id, agency_id, staff_id NOT NULL (author), kind (`create`, `update`), target_property_id, base_property_version,
status (`open`, `saved`, `cancelled`), version (> 0, carried on Save/Edit/Cancel buttons as draft_id:version),
payload JSONB object, created_at, updated_at, closed_at.

- create: target_property_id and base_property_version are NULL. update: both NOT NULL.
- closed_at is NULL exactly while status = 'open'.
- `listing_draft_one_open_update_idx`: `UNIQUE (agency_id, staff_id, target_property_id) WHERE status = 'open' AND kind = 'update'`.
  One open update draft per staff member and target property; other staff may hold their own;
  any number of open create drafts is allowed.
- Photos for a draft that has no property yet are referenced from payload (bucket keys) until Save.

**Required save behaviour (future Task 9.2, not implemented here).** Saving an update draft must, in one
tenant transaction: `UPDATE property SET ..., version = version + 1, updated_at = now()
WHERE agency_id = current_agency_id() AND id = :target AND version = :base_property_version`; if 0 rows
changed, abort (the property moved on; refresh the card). Then mark the draft `saved` with
`WHERE id = :draft AND status = 'open' AND version = :draft_version` (a double tap updates 0 rows and is a no-op)
and insert the property_event. Saving a create draft inserts the property with version 1 and a `created` event.

### staff_session

id, agency_id, staff_id NOT NULL (one session per staff member, `UNIQUE (agency_id, staff_id)`),
focus_property_id NULL (composite FK property: the listing a reply or a later photo attaches to),
state JSONB object (staff buffer / segmentation context), last_activity_at, created_at, updated_at.

Temporary WhatsApp workflow context only: not authentication, no tokens or credentials.
Clean-up is by last_activity_at in the application. Staff roles stay `admin`, `agent`, `broker`.

## v_public_listing

```
CREATE VIEW v_public_listing WITH (security_invoker = true, security_barrier = true) AS
SELECT id, agency_id, listing_code, status, purpose, property_type, category, location_id, project_id,
       price_pkr, area_value, area_unit, area_sqft, bedrooms, bathrooms, construction_status,
       latitude, longitude, public_features, cover_media_id, version, updated_at
FROM property WHERE status = 'active';
```

- Explicit allowlist; `SELECT *` is not used, so new property columns never appear automatically.
- Excluded: source_text, internal_notes, created_by_staff_id, is_demo, created_at. Drafts, sessions and
  events are not joined.
- security_invoker: the caller's privileges and the property RLS policy apply, so the view only shows
  the current agency's active rows; without tenant context it is empty. A role with SELECT on the view but
  not on property gets a permission error, so the view cannot be used to bypass base-table rules.
- The view is read-only for the runtime: SELECT is the only privilege (no write-through).
- **Not a complete privilege boundary:** manzil_app can also read the full property table (staff flows
  need it). The view is the customer-safe allowlist that customer search (8.1) and answers must use.
  A separate customer-path role would be needed to make it a hard boundary.

**is_demo policy.** The view does not filter on is_demo, because the demo agency's listings are all
demo rows and must be searchable in the demo. Isolation of demo data comes from tenancy: demo listings
belong only to the demo agency, whose customers are allowlisted test phones, and demo images carry a
"DEMO" watermark. Policy: a production agency must never contain is_demo rows. This is not yet
database-enforced (it needs an agency-level demo flag); see docs/OPEN_ISSUES.md.

## Indexes

- property: active search `(agency_id, purpose, property_type, price_pkr) WHERE status = 'active'`,
  `(agency_id, location_id) WHERE status = 'active'`, `(agency_id, project_id)`, `(agency_id, created_by_staff_id)`.
- property_event: `(agency_id, property_id, created_at)`, `(agency_id, listing_draft_id)`.
- listing_draft: `(agency_id, staff_id) WHERE status = 'open'`, `(agency_id, target_property_id)`.
- staff_session: `(agency_id, focus_property_id)`. project: `(agency_id, location_id)`.
- Gazetteer: location (parent), alias (trigram, exact, location, context), neighbor (location_b_id).

## Tests

tests/test_inventory.py runs on the disposable PostgreSQL database as the restricted manzil_app login:
gazetteer hierarchy/uniqueness/ambiguity and read-only privileges; tenant RLS (select, insert, update,
delete, cross-tenant move, missing and invalid context); cross-tenant composite FKs; cover-media FK
including delete/move behaviour; publication rule and NULL facts; version; draft uniqueness; append-only
events; the public view's allowlist, ACTIVE filter, isolation and invoker semantics; exact privilege
matrix including PUBLIC; a catalog guard that every tenant-to-tenant FK starts with agency_id. tests/test_rls_migration.py covers pg_trgm with a non-superuser migration role.

## Migration 007: D11 alignment (Task 3.3 follow-up)

Additive forward migration for the 005 gaps in docs/SPEC_ALIGNMENT.md section 2 (G1-G15, G18, G19).
005 is unchanged. No row is rewritten by an UPDATE: every new column is nullable or has a constant
default (FORCE RLS would hide rows from the owner). Replaced 005 CHECKs are looked up by name and the
migration fails if one is missing.

- **location**: level `zone` under a `sub_area` (Ph 6 > Bukhari Commercial) or an `area` (PECHS >
  Tariq Road); nothing may sit below a zone. New `code` (unique listing-code prefix, `^[A-Z0-9]{2,4}$`),
  `display_name` (`name` stays canonical), `level_label` (phase / precinct / block / sector / zone /
  corridor, for bare-number resolution), `city_code` (city rows only), `centroid_lat/lng` (a pair),
  `is_commercial_zone`. Listing codes are not forced to match a location code (PRJ-..., zones).
- **location_alias**: `kind` (official / common / misspelling / urdu_script / voice_stt, default common),
  `weight` 0-1 (default 1). **location_neighbor**: `weight` (0, 1] (default 1).
- **property, typed (search filters, ranking, soft preferences)**: types + penthouse / showroom /
  warehouse / building / floor; status + on_hold / archived; construction + grey_structure / off_plan;
  `possession_status`, `expected_possession` (first of month), `installment_available`, `is_tenanted`,
  `investment_tags`; `size_nominal_sqyd` (Bahria 250 nominal with area_value 272 sq_yd),
  `covered_area_sqft`; `last_confirmed_at` (last confirmed **by staff**: rows that exist before 007 stay
  NULL = never confirmed; new rows default to now(); see below); `furnishing`, `facing[]`, `is_corner`, `features[]`, `portion_level` (portions only),
  `year_built`. Vocabulary arrays are checked by `text_set_within()` (subset, no NULLs, no repeats).
  Penthouses need bedrooms to be active.
- **property, display-only facts in public_features**: a key allowlist (title <= 60, description <= 600,
  floor_no, total_floors, parking_spaces, price_negotiable, rent deposit / advance months, maintenance,
  min lease, available_from, down payment, installment months / amount, current rent). Internal data
  cannot be parked there. Search never filters on these keys (owner decision 2026-10-08).
- **property, internal only** (never in a view): source_type, owner_name, owner_phone, floor_price_pkr,
  commission_pct / note, is_exclusive, assigned_broker_id (composite FK to staff), docs_status,
  visit_notes, address_detail, source_message_ids, duplicate_group_id. `created_by_staff_id` is D11's
  listed_by_staff_id.
- **property_event**: `reconfirmed` event type (records the current version with from_version NULL);
  `source_message_id`, composite FK to message, ON DELETE SET NULL.
- **project**: project_type, expected_completion (first of month), payment_plan_summary, amenities,
  approval_note (internal); construction + grey_structure / off_plan.
- **agency**: city_default (KHI), marla_sqft (225 default; only 225 or 272.25, D11 4.1), settings, and
  `UNIQUE (id, marla_sqft)`. **staff**: areas_covered (location IDs, not referential).
- **Views**: v_public_listing appends the typed customer columns (existing order and grants kept);
  new v_public_project (no approval_note, is_demo or created_at), security_invoker + security_barrier,
  SELECT only for manzil_app. Media columns (G16) come with Task 4.1; draft fields (G17) with 7.4 / 9.2.
- **Tests**: tests/test_inventory_d11.py (zone placement, codes and attributes, every new enum / array /
  JSON key rule, internal-field and cross-tenant FKs, reconfirmed events, project / agency / staff
  settings, both view allowlists and privileges, and an upgrade from 006 with existing rows as a
  non-superuser owner under FORCE RLS).

### last_confirmed_at contract (007)
Means "last confirmed by staff" (D11 5 staleness; R17 ranking freshness 10%, ties by
last_confirmed_at). 007 adds the column without a default (existing rows stay NULL) and then sets
`DEFAULT now()` in a separate statement, so only new rows get it. NULL = never confirmed: readers give
it the lowest freshness, sort it last on ties, treat it as due for the reconfirm prompt, and start the
45-day auto-hold clock from the prompt, never from NULL. The 9.2 save and the reconfirm action set it
explicitly; the 4.3 seed sets explicit fictional dates.

### Marla basis (007)
`area_sqft` no longer hardcodes 225. A marla or kanal size records the agency's convention in
`property.area_marla_sqft`; `area_sqft = area_value * (1 | 9 | area_marla_sqft | 20 * area_marla_sqft)`
for sq_ft / sq_yd / marla / kanal (`ALTER COLUMN ... SET EXPRESSION`, PostgreSQL 17+). Rules:
`property_area_marla_basis_check` (basis present exactly for marla / kanal) and the composite FK
`property_area_marla_basis_fkey (agency_id, area_marla_sqft) -> agency (id, marla_sqft)`, so the basis
must be the listing agency's current setting (tenant-scoped; RLS does not apply to FK checks). The FK is
NO ACTION: an agency cannot change marla_sqft while listings use the old basis (a deliberate
re-normalisation step). A pre-007 marla / kanal row has no basis and makes 007 fail and roll back.
Contract for the 6.2 normaliser (not implemented yet): inside the tenant transaction read
agency.marla_sqft and use it for customer queries ("10 marla" -> sq ft) and staff saves (store the raw
area_value / area_unit plus area_marla_sqft). 225 must not be hardcoded in application code.
