# Demo dataset (Task 4.3, checkpoint 1)

Offline only: the files in `demo/` and the validator `app/demo_dataset.py`. Nothing here
reads or writes a database. Unresolved items are in [DEMO_DATASET_GAPS.md](DEMO_DATASET_GAPS.md).

Validate: `.venv\Scripts\python.exe -m app.demo_dataset` (exit 0 = valid).
Tests: `tests/test_demo_dataset.py`.

## Sources

| Source | Pages | Used for |
|---|---|---|
| D11 (R11/R12/R13 Demo Data Model) | p1, p5-7, p11-14 | gazetteer, codes, aliases, 30 listings, 2 projects, staff roles, staleness |
| Demo Scope v1.0 | p4-7 | DH8-101 / DH8-102 overrides, staff, scenario result sets |
| R17/R18 | p3-5 | neighbour pairs, context aliases, "Klifton", budget and size filters |

The PDFs were read with `pdftotext -table` (column layout kept). No page could be rendered
as an image on this machine, so the Urdu-script source text was not visually checked and no
Urdu alias is included yet (see gaps). The validator accepts text in any script, Urdu
included; it does not try to detect reversed Urdu by banning characters.

## Files

| File | Rows | Identity used by a later loader |
|---|---|---|
| `locations.csv` | 52 (1 city, 8 areas, 32 sub-areas, 11 zones) | `key` -> (parent, lower(name)); `code` is unique |
| `location_aliases.csv` | 85 (37 sourced + 48 official; 7 with a context) | (location, alias_norm, context) |
| `location_neighbors.csv` | 3 | unordered pair |
| `projects.csv` | 2 | (agency, lower(name)); `key` is the D11 id PRJ-P1 / PRJ-P2 |
| `properties.csv` | 30 (27 active) | (agency, listing_code) |
| `staff.csv` | 3 | (agency, phone); the phone comes from `phone_ref` at run time |
| `freshness_policy.json` | 1 adopted listing | insertion-time rule |

Every row has a `source`. `derived` names the fields that are mechanical derivations, and
`override` (listings) names the Demo Scope change. Blank cells are unknown facts (NULL).
Arrays use `;`. Columns `pf_*` become `public_features` keys (all on the 007 allowlist).

## Source facts

- 30 listings exactly as D11 p11-14, with Demo Scope p5: DH8-101 is now 500 sq yd, 4/5,
  7.4 crore, active; D2X-101 is replaced by DH8-102 (500 sq yd, 3/4, 6.9 crore, active).
- Hidden: DH6-105 sold, DH6-106 reserved, CLF-104 rented. All others active.
- Projects: PRJ-P1 Saahil View Residency (DEMO), DHA Ph 8, apartment tower, Dec 2028,
  "30% down + 36 monthly"; PRJ-P2 Gulshan Trade Centre (DEMO), Gulshan Block 13, commercial
  mall, off plan, "25% down + 24 monthly".
- Gazetteer: the eight D11 p6 areas; DHA Phases 1, 2, 2 Ext, 4, 5, 6, 7, 7 Ext, 8; the ten
  named Ph 5 / Ph 6 commercial zones; DCK sectors 8 and 13; Clifton Blocks 1-9; Bahria
  Precincts 1, 10, 27, 31, Bahria Heights, Bahria Sports City; Gulshan Blocks 6 and 13; PECHS
  Blocks 2 and 6 and the Tariq Road zone; North Nazimabad Blocks H and L; Shahrah-e-Faisal.
- Codes DH1-DH8 (no Phase 3 is listed, so there is no DH3 row; DH3 is not treated as an
  invalid code), D2X, D7X, DHC, CLF, BTK, GLI, PEC, NNZ, SHF. D2X stays although D2X-101 is gone.
- Aliases: the D11 p7 example aliases (8 locations), R17's context aliases `p6` and
  `phase 6` -> DHA Phase 6 inside DHA, and R17's `klifton` -> Clifton.
- Neighbours: DHA Ph 5-6, 6-7, 7-8 (R17 p3).
- Bahria "250 yd" houses store 272 sq yd actual with nominal 250 (D11 p1, p13; 007).
- Staff: one admin, one staff (`agent` in the schema), one broker "Ahmed (DEMO)".

## Mechanical derivations

- Prices: crore x 10,000,000, lakh x 100,000, to whole rupees (rent is monthly).
- `display_name`: area = its name; numbered sub-area = area stem + name, using the stems in
  the D11 examples ("DHA Phase 6", "Clifton Block 5", "Bahria Precinct 27",
  "Gulshan-e-Iqbal Block 13", "PECHS Block 6", "North Nazimabad Block H",
  "DHA City Karachi Sector 8"); named sub-areas and zones = their name.
- Phase codes DH1, DH2, DH4-DH8 expand D11's range "DH1…DH8" over the listed phases.
- `level_label` from the level word in the name (phase, block, precinct, sector, zone);
  Shahrah-e-Faisal uses 007's `corridor`.
- `category` from type: house / apartment / portion residential; office / shop commercial.
- `size_nominal_sqyd` = the sq-yd size for houses and plots (all are D11 buckets).
- "Notable" text mapped to typed columns: west open / park facing / road facing / sea facing
  -> `facing`; corner -> `is_corner`; basement, annex, lawn, lift, main boulevard, gas,
  KWSB water -> `features` (generator -> `generator_backup`); furnished -> `furnishing`;
  owner direct -> `source_type`; possession / file / on ground -> `possession_status`;
  tenanted @ 1.2 lakh -> `is_tenanted` + `current_rent_pkr` 120,000; 4th floor -> `floor_no`
  4; 2 parking -> `parking_spaces` 2; 3 months advance -> `rent_advance_months` 3.
- Official aliases (48 rows, `kind=official`, weight 1, `derived` = "alias (canonical
  name)"): one per location, the normalised canonical name from `locations.csv`. These are
  not quoted from the PDFs. A name used by more than one location (Block 2: Clifton, PECHS;
  Block 6: Clifton, Gulshan, PECHS) gets its parent area as context; a unique name has none.
  Deduplicated on the schema identity (location, alias_norm, context): four names already
  had a sourced alias with that identity (`dha city karachi`, `bahria town karachi`,
  `precinct 27`, `bukhari commercial`), so the sourced row is kept with its provenance and
  no official row is added. A global official `phase 6` sits beside R17's DHA-context
  `phase 6`; they are different identities.
- Project units: location = the project's location; PRJ-102 copies PRJ-101's plan ("same
  plan"); PRJ-103 takes PRJ-P2's 24 monthly instalments; PRJ-P1 is `under_construction`
  because both its units are.

## Decisions taken in this checkpoint

- Weights: aliases and neighbours use the schema default 1 because no source gives a weight.
  R17's 0.6 is a ranking factor for neighbour results, not an adjacency weight.
- Alias `kind` uses the schema default `common`; no source classifies the examples.
- Staff phones are symbolic (`env:DEMO_ADMIN_PHONE`, `env:DEMO_STAFF_PHONE`,
  `env:DEMO_BROKER_PHONE`); the dataset holds no phone numbers, and the validator rejects
  phone-like text in every file. Staff are matched by (agency, phone). `if_exists =
  reuse_unchanged`: an existing row is reused with its name, role and phone untouched (on
  production the existing agent is the dataset's agent; no fourth staff row). The names
  "Admin (DEMO)" / "Staff (DEMO)" apply only to rows the loader creates (disposable test
  databases, or a demo agency without them).
- Freshness (owner decision 2026-10-09, not source-backed): DH6-104 is inserted with
  `last_confirmed_at` = load time minus 35 days; every other new listing gets load time. A
  rerun never changes `last_confirmed_at` on an existing listing.
- Reruns insert missing rows only and never update existing ones, so staff edits made
  after the first load (price, status, reconfirmation) are preserved.
- DH6-109 is reserved for scenario 4a (the Phase 6 sequence ends at 108).

## Scenario filters (data checks, not search)

Rules: active only; "max X" allows X x 1.05; "around X" is X +/- 10%; house size is the
nominal bucket +/- 10%; neighbours come from `location_neighbors.csv`.

| Check | Filter | Expected |
|---|---|---|
| Scenario 1 | rent office, 4.5-5.5 lakh | DH6-108, SHF-101, PEC-101 |
| Scenario 2 exact | sale house, Ph 6 subtree, 450-550 yd, <= 8 cr x 1.05 | DH6-101, DH6-102 |
| Scenario 2 budget +10% | same, <= 8.8 cr x 1.05 | DH6-101, DH6-102 |
| Scenario 2 neighbour step | same, Ph 6 neighbours (Ph 5, Ph 7) | DH7-101 |
| Scenario 3b | Ph 6 + neighbours, <= 7.5 cr x 0.97 x 1.05, shown excluded | none |
| Scenario 3c | Ph 6 + Ph 8, same budget, shown excluded | DH8-101, DH8-102 (DH8-102 in budget) |
| Scenario 4c | active houses in Ph 6 | DH6-101 to DH6-104 |
| D11 Clifton shop rent | rent shop, Clifton subtree | CLF-101 |
| D11 Bahria 10 marla | sale house, Bahria, nominal exactly 250 | BTK-103, BTK-104 (D11's expectation; R17's +/- 10% would also admit BTK-102, see gaps) |
| D11 under construction | under construction or off plan | PRJ-101, PRJ-102, PRJ-103 |
| Demo Scope rent coverage | purpose rent (any status) | the eight listed rent IDs |

These filters only show that the data can produce the scripted sets. They do not verify
how the future search will behave.

## Loader requirements (implemented in checkpoint 2: app/seed.py, docs/DEMO_SEED.md)

From the current code and schema:

- **Two phases, two connections.** The gazetteer (location, location_alias,
  location_neighbor) is shared and has no RLS; `manzil_app` may only SELECT it (005), so
  the gazetteer phase runs on the schema-owner (migration) connection. Staff, projects and
  properties are tenant tables under FORCE RLS (002, 005) and are written through
  `app.db.tenant_transaction(conn, agency_id)` on the runtime login. The phases are separate
  transactions and are **not** globally atomic: a committed gazetteer phase stays if the
  tenant phase later fails. A rerun then finds the gazetteer complete and continues.
- **Conflicts before inserts, per phase.** Each phase first reads every identity it would
  write (location (parent, lower(name)) and code; alias (location, alias_norm, context);
  neighbour pair; staff (agency, phone); project (agency, lower(name)); property (agency,
  listing_code)) and compares existing rows with the dataset. Any difference is a conflict,
  reported before the phase inserts anything. Missing rows are inserted; matching rows are
  left alone.
- **Rollback of the whole phase.** A conflict, or any error while inserting (including a
  unique-constraint violation from a concurrent writer), raises inside the phase's single
  transaction, so the whole tenant phase (or gazetteer phase) is rolled back.
  `tenant_transaction` already wraps one `conn.transaction()`, which rolls back on an
  exception.
- **Dry run.** Runs the same reads and conflict checks and reports what would be inserted,
  inside a transaction that is always rolled back; no persistent writes.
- **Packaging.** The image copies `demo/` (Dockerfile); see docs/DEMO_SEED.md.
