# Demo media (Task 4.4)

Fictional demo images for the 30 demo listings: four per listing (120), each visibly marked **DEMO**
(speculative scenes **DEMO / CONCEPT**), stored through the Task 4.1 processing and loaded into
`property_media` with the cover set where a listing has none. No migration; 001-009 unchanged.

## Status (2026-10-09)

| Part | State |
|---|---|
| Generation plan, 120 prompts (`demo/media/scenes.csv`, `docs/DEMO_MEDIA_PROMPTS.md`) | **Implemented**, validated |
| Intake manifest spec + validator (`demo/media/intake.csv`, `app/demo_media.py`) | **Implemented**; the committed intake is **empty** |
| Watermark, prepare, review sheets, transfer bundle (`app/demo_media.py`) | **Implemented and locally verified** on synthetic TEST FIXTURES |
| Loader with journal, cleanup, verify, fingerprint (`app/media_seed.py`) | **Implemented and locally verified** (disposable PostgreSQL, fake S3) |
| Operator WhatsApp send check (`app/media_send_check.py`) | **Implemented and verified with a fake Meta client** |
| Image generation and intake | **Pending** (owner: generator, credits, actual provenance) |
| Visual approval of every derivative | **Pending** (no real image exists yet; nothing has passed visual QA) |
| Production load, verification, optional send check | **Pending** (owner, after approval; runbook: docs/DEMO_MEDIA_RUNBOOK.md) |

Task 4.4 is not complete until real images are generated, reviewed, loaded and verified in production.

## 1. Rules

- **Images are AI-generated fictional scenes**, not photographs of the listed properties. Per file the intake
  records the actual generator, model, generation date, the prompt as submitted, the source-file sha256 and the
  licence / terms reference the operator checked. These values are never invented; a missing value blocks
  approval.
- **AI images are not free of licensing risk.** Terms can restrict use or change; ownership of AI output is
  legally unsettled; an output can resemble a real building, brand or protected work. Mitigations: fictional
  scenes, no people, no legible text, logos, plates or house numbers, no recognisable real places, a human review
  of every derivative, and the terms reference recorded per file. Residual risk remains.
- No existing image (listing photos, stock, web) is approved for reuse. Test fixtures are never deliverables:
  they are generated in pytest temporary folders and labelled `TEST FIXTURE`.
- **An image never adds a listing fact** and never changes a listing field (furnishing, status, features). Each
  scene lists the unrecorded D11 attributes it must not show (lawn, basement, annex, pool, solar, sea, park,
  corner, boulevard frontage, ...).
- **No factual maps or floor plans**: labels `map` and `floor_plan` are rejected.
- **Speculative scenes are concept renderings** (class `concept`, mark `DEMO / CONCEPT`): `under_construction`,
  `off_plan`, `grey_structure`, `possession_status = file`, and building types whose construction status is
  unknown. This covers PRJ-101 / PRJ-102 / PRJ-103, BTK-106 and DH8-102 (status stays unknown; its scenes are
  explicitly illustrative). No construction-progress photographs.
- **BTK-106** (property file, no allocated physical plot): four fictional undeveloped-landscape concepts:
  elevated open plain, ground-level rocky basin, oblique natural erosion channels and side-on rolling hills.
  Different landforms and compositions establish distinctness; lighting-only variants do not count. No
  allocated parcel, boundaries, actual site, approved layout, identifiable landmarks, roads, utilities,
  landscaping or guaranteed infrastructure/amenities. These are land/development-context views, not
  factual evidence of this file's location or terrain; all remain marked `DEMO / CONCEPT`.
- **PRJ-103** (off-plan shop): hypothetical shopfront, inward-facing empty retail volume, reverse interior
  perspective and oblique shopfront-opening view. The latter two show the shop itself, without material
  still-life or abstract lighting artwork. No promised fit-out or dimensions, building height, shared corridor,
  plaza, surrounding development or amenities. PRJ-101/102 remain concepts with
  no progress photography, balcony-door or walk-in-wardrobe claims. DH6-101 does not assert a storey count.
- **DHC-101** (on-ground plot): fictional vacant-land views, mark `DEMO`.
- **Interiors with unspecified furnishing are empty** (G-7). Only SHF-101 and BTK-105 (`furnished`) show
  furniture. Rooms keep one palette per listing so a listing's images are architecturally consistent.
- Bulk images stay **outside Git**. `prepare` accepts folders outside the repository or under the narrow
  excluded `.local/demo-media/` workspace; other repository folders remain blocked. `.gitignore` excludes
  that workspace and `.dockerignore` excludes `.local`. ZIP bundles still go outside the repository.
  Git holds the manifests, code, tests and docs.

## 2. Files and identity

`demo/media/scenes.csv` — the plan (one row per asset; 30 listings x 4):
`asset_id` (`<listing>/mNN`, stable logical identity of the scene slot), `listing_code`, `sort_order`
(0-3), `cover` (exactly one, at order 0), `label` (`exterior`, `lounge`, `kitchen`, `bedroom`, `bath`,
`view`, or blank = NULL), `scene_class` (`photo`, `concept`, `vacant_land`), `mark_corner` (`br`/`bl`/`tr`/`tl`
of the crop-safe region), `pilot`, `title`, `prompt`, `negative_prompt`, `fact_basis` (from
`demo/properties.csv`), `must_not_show`.

`demo/media/intake.csv` — one row per generated image (empty until generation):
`asset_id`, `source_file` (relative path of plain names inside the sources folder), `source_sha256`,
`generator`, `generator_model`, `generated_on`, `generation_prompt`, `licence_reference`, `prepared_sha256`,
`previous_prepared_sha256` (`;`-separated hashes of earlier versions), `review_status`
(`pending`/`approved`/`rejected`), `reviewed_by`, `reviewed_on`, `review_notes`.

Validation (`python -m app.demo_media validate`): 30 listings, 4-6 distinct scenes each, contiguous orders,
one cover at 0; labels allowed for the property type (no room labels on shops / offices / plots); scene class
fits the known construction state; pilot = DH6-101, PRJ-103, BTK-106; intake rows reference known assets,
complete provenance, ISO dates not in the future, unique source and prepared hashes (no duplicates, within or
across listings), approved rows reviewed and prepared. `--plan-only` checks the plan; without `--listings`
**all 30 listings must have approved assets**; `--listings` limits the scope for the pilot.

Four separate identities:

| Identity | Where | Purpose |
|---|---|---|
| Asset ID (`DH6-101/m01`) | manifests, journal, event `changes` | the logical scene slot |
| Content hash (`prepared_sha256`) | intake, `property_media.content_sha256` | exact bytes; `UNIQUE (agency_id, property_id, content_sha256)` |
| Row ID (`property_media.id`) | DB, private keys | random UUID per upload attempt, kept in the journal |
| Card token | public card key | random UUID per attempt (journal); never derived from the listing, asset or hash |

## 3. Generation and intake workflow (owner, Windows)

1. Generate each scene from `docs/DEMO_MEDIA_PROMPTS.md` (pilot first): 16:9 landscape, >= 1,280 px wide
   (1,920 x 1,080 or larger preferred), no text or watermark. Keep files unmodified in e.g.
   `E:\manzil-demo-media\sources\`.
   The manual pilot may instead use `E:\manzilai\.local\demo-media\sources\`, with preparation output in
   `E:\manzilai\.local\demo-media\work\`. Preserve downloaded originals unmodified. Record only actual
   provenance, never infer an undisclosed model, and leave review pending until the owner explicitly
   approves the processed derivatives. Inspection must distinguish technical checks from visual confidence
   (High/Medium/Low only after actually viewing the image) and list missing evidence.
2. Add a row per file to `demo/media/intake.csv` with the actual provenance, `review_status = pending`.
3. `python -m app.demo_media prepare --sources-dir E:\manzil-demo-media\sources --out-dir
   E:\manzil-demo-media\work --listings DH6-101,PRJ-103,BTK-106 --record` (writes `prepared_sha256`).
4. Review every sheet in `work\review\` and the per-asset derivatives (section 4). Set `review_status =
   approved` (or `rejected` and regenerate), `reviewed_by`, `reviewed_on`.
5. `python -m app.demo_media validate --listings ... --assets-dir E:\manzil-demo-media\work`, then
   `bundle` (section 6 of the runbook).

To replace an approved image: move its `prepared_sha256` to `previous_prepared_sha256`, reset the review, and
note that a loaded asset with new bytes is a **conflict** in the loader (G-5) — replacement needs a separately
reviewed policy.

## 4. Watermark and processing

- `prepare` first runs the unchanged `app.media.process_image` on the source (magic-byte MIME, Pillow format,
  50 MP limit, decompression-bomb guard), applies EXIF orientation, requires landscape aspect 1.5-1.95 and
  width >= 1,280, scales to <= 2,560 px, marks, re-encodes JPEG q95 with no metadata, and runs
  `process_image` again on the prepared bytes (its outputs are re-opened and rejected if any EXIF/XMP
  remains). The loader stores the prepared bytes through the same function.
- **Crop-safe region** = exactly what `ImageOps.fit(..., (1200, 628), centering=(0.5, 0.5))` keeps: for W x H,
  if W/H >= 1200/628 the box is (H x 1200/628) x H centred horizontally, else W x (W x 628/1200) centred
  vertically. Computed per image (`demo_media.crop_box`; a test proves it pixel-identical to `ImageOps.fit`).
- **Mark (watermark v1)**: a rounded black pill (alpha 190) with white text, height 9 % of the crop-safe height,
  inset 3.5 % of that height + 2 px from the chosen corner **of the crop-safe region** (never the source corner),
  Pillow's bundled scalable font (Aileron Regular; the run fails if it is not available), deterministic output.
- **Automatic check**: the mark box is mapped into the original, WhatsApp and card derivatives and must show a
  dark pill with light text in each; otherwise `prepare` fails.
- **Review sheets** (`work\review\<listing>_sheet.jpg`): per asset the card (1,200 x 628 scaled), the card at
  phone-carousel width (320 px), the WhatsApp image and the original, captioned with asset ID, order, label,
  class, review status, prepared hash and size. Individual derivatives are in `work\review\<listing>\`.
- **Visual review (every asset)**: DEMO (and CONCEPT where planned) readable in all derivatives, including the
  320 px card; scene matches its prompt and fact basis; nothing from `must_not_show`; no people, text, logos,
  plates, recognisable real places, warped geometry or construction progress; interiors consistent within the
  listing. Only then `approved`. After the production load each public card is inspected again.

## 5. Loader (`python -m app.media_seed`)

Reused unchanged: `media.process_image`, `media.S3Storage.put / delete / get`, `media.public_base_url`,
`media.CARD_CACHE_CONTROL`, `media.has_metadata`, `app.db.tenant_transaction`, `app.seed.LOGIN` (restricted-login
privilege check) and `DEMO_AGENCY_NAME`. The media utilities additionally require both `current_user` and
`session_user` to equal `manzil_app`; another restricted role is refused. Tenant writes still use
`tenant_transaction`, without weakening the existing privilege checks or changing the Task 4.3 seed.

Keys: `tenant/<agency>/property/<property>/<row id>/original.jpg` and `.../wa.jpg` (private), and
`card/<agency>/<card token>.jpg` (public; `Cache-Control: public, max-age=31536000, immutable`). Both prefixes
already exist; no IAM or bucket change is made or assumed.

`property_media` row: `id` (journal), `agency_id` = `current_agency_id()`, `property_id`, `original_key`,
`wa_image_key`, `card_image_key`, `content_sha256` = prepared input hash (not the stored original's hash:
`process_image` re-encodes the original JPEG), `mime_type` `image/jpeg`, `sort_order`, `kind` `image`, `label` (or NULL),
`public_url_card` = public base + card key, `exif_stripped` TRUE; `wa_media_id` / `wa_media_expires_at` NULL
(Task 4.2 fills them on first send); `source_inbound_media_id` NULL.

**Cover, version and audit (G-4).** Covers are set only where `cover_media_id` IS NULL, to the asset at order 0
of the same listing (the composite FK also enforces same agency and listing). docs/INVENTORY_SCHEMA.md defines
`property.version` as bumped by every committed change (no trigger; drafts use it for optimistic saves), so each
listing whose media set or cover changes gets `version + 1` and `updated_at = now()` once per run, with an
optimistic `WHERE version = <seen> AND cover_media_id IS NOT DISTINCT FROM <seen>`. **`last_confirmed_at` is
never touched**, nor any other listing column. The existing event type `media_added` (005 / 007) fits, so one
`property_event` row per changed listing is written (`actor_staff_id` NULL, `from_version` -> `to_version`,
`changes` = source, media IDs, asset IDs, cover). No new enum or migration. There was no existing audit helper
in application code.

**Rerun classification** (per asset, in the plan and again under the locks):

| Situation | Result |
|---|---|
| A row with the asset's content hash, same order, label, kind, keys, https card URL, `exif_stripped` | **unchanged** (no upload, no write) |
| A row with the content hash but another order / label / missing keys (staff edit, or same bytes in another slot) | conflict `incompatible_metadata` |
| A row holding an earlier hash of the asset (`previous_prepared_sha256`, or a committed journal attempt with another hash) | conflict `changed_asset` |
| The journal says the asset was loaded but its row is gone | conflict `removed_after_load` (not re-added) |
| The planned order is held by other media | conflict `slot_occupied` |
| None of the above | **insert** |
| Cover NULL / = the planned cover / another media | set / unchanged / conflict `cover_taken` |
| Cover NULL although the planned cover asset is already stored (staff cleared it) | conflict `cover_cleared` (not reset) |

Other media of a listing are preserved and counted. Any conflict stops the run before any upload (exit 2); no
reset of covers or order. Limitation: without the journal, a loaded row that staff deleted is indistinguishable
from a never-loaded one and is re-added (a test documents this); keep the journal with the release records.

**Phases and journal.** The journal (`--journal FILE`, JSON) records each upload attempt: asset, listing,
property, content hash, random row ID and card token, the three keys, derivative SHA-256 values, and a state. Every write is durable (temp
file, fsync, atomic rename, directory fsync on Linux); a `FILE.lock` (O_EXCL) keeps two applying runs off one
journal; a dry run never writes it. Keys in the journal must equal the keys derived from its IDs, so a tampered
journal's keys must match its IDs. Keep the journal trusted: this is consistency checking, not a signature
or proof against deliberate forgery. A journal is bound to its agency, its database connection fingerprint
(allowlisted libpq host, hostaddr, port, dbname, service; database name, schema and agency creation time, all
hashed without recording passwords or full connection strings) and its storage
(`s3://<bucket>`, plus a hash of `S3_ENDPOINT_URL` when set); a journal of another target is refused before
anything is reused, uploaded, deleted or saved (a dry run without S3 settings checks the database only).
This is not physical database identity: endpoint reuse, DNS changes or restoration at the same target/schema
can still match. Endpoint aliases intentionally fail closed. Legacy journals with the old database binding
are refused; do not edit them to bypass the check. Review recovery separately if any exist.

```
planned --(3 puts: original, wa, card)--> uploaded --(rows written)--> committing --COMMIT--> committed
   \______________________ cleanup (no row, unreferenced) __________________________/--> cleaned
```

1. Validate manifests and prepared files (hash, `process_image`); no connection is opened before the inputs pass.
2. Acquire the session advisory lock `(4404, hashtext(agency))` **before reading the journal**, for load and
   cleanup (including dry runs). Hold it through uploads, commit, deletion and final journal save; release it
   by closing the dedicated connection in `finally`. This coordinates different or copied journal paths.
   Reconcile the journal with the database under the same lock; the lock
   also waits for any transaction of an interrupted run to end, so an uncertain commit is resolved: row
   present -> `committed`; `committing` without a row -> `uploaded`. A dry run reconciles the same way but only
   in memory (the journal file is not written); `--apply` and `cleanup --apply` save the result.
3. Read-only plan (agency must be "Manzil Demo Realty (DEMO)"; listings must exist with `is_demo`); conflicts ->
   exit 2.
4. Journal all new attempts (fsync), then check each exact key with authenticated GET. Matching bytes are
   reused; incompatible existing bytes are never overwritten. Only a definite missing key can be put for
   a `planned` attempt. A missing `uploaded` object fails closed. Denial or unreadability is not absence.
   Read back all three objects and compare SHA-256 with the derivatives recomputed from hash-checked
   prepared input before the database phase. The session advisory lock spans I/O, but no transaction or
   listing row lock does. Storage `put` still uses the unchanged Task 4.1 implementation.
5. One tenant transaction (`lock_timeout` 10 s, `statement_timeout` 30 s): advisory lock, the listings
   `FOR UPDATE` (this also waits for in-flight staff media inserts, whose FK takes a key-share lock), full
   re-plan; any difference -> rollback, exit 2. Insert rows, covers, versions, events; journal `committing`; COMMIT;
   journal `committed`. The advisory lock serialises media loaders and cleanup; staff writes are handled by the row locks,
   the recheck and the unique constraints.

| Failure | State | Recovery |
|---|---|---|
| Before upload (validation, plan, conflict) | nothing written | fix input; rerun |
| During uploads / crash between derivative puts | attempts `planned`/`uploaded`, objects partly present | rerun `load --apply` (reuses keys) or `cleanup --apply` |
| Upload done, DB rollback (recheck, constraint, lock timeout) | attempts `uploaded`, no rows | rerun `load --apply` (verified reuse) or `cleanup --apply` |
| Missing completed upload or checksum mismatch | no rows committed; incompatible objects untouched | stop and review; do not force or overwrite; unreferenced attempts may be cleaned after ownership review |
| Lost connection at COMMIT | `committing`; error "commit outcome unknown" | rerun `load` (dry run) to reconcile under the lock; never clean before reconciling |
| Crash after COMMIT before the journal update | `committing` with rows | any later run reconciles to `committed`; cleanup deletes nothing |
| Concurrent loaders (different journals) | one commits; the other rolls back (or finds all unchanged) | `cleanup --apply` with the loser's journal |
| Journal missing | rows still match by content hash: exact rerun = unchanged | orphans of a lost journal cannot be proven ours and are not deleted (manual review); see limitations below |
| Journal unreadable / other agency / other database or bucket / inconsistent / locked | refused, nothing changed | inspect; use the journal of this target; remove a stale `.lock` only when no run is active |
| Interrupted run whose session is still open (lock held) | reconcile / apply fail with SQLSTATE 55P03 after 10 s | wait for the server to end the session, then rerun; never clean up while it is unresolved |

`cleanup` (dry run by default) reconciles first, then deletes only the three exact keys of `planned`/`uploaded`
attempts that have no row and are referenced by no `property_media` or `inbound_media` row, card first. It never
lists or deletes by prefix and never touches committed attempts. No bulk removal or reset exists (G-6).
The session lock remains held from reconciliation through the final delete/save: a copied journal cannot
race an active loader, including its upload phase. Random IDs isolate attempts from unrelated pre-existing
objects; references are checked for every candidate. The journal must remain trusted and belong to this target.
Before deleting any derivative of an attempt, cleanup checks all its present objects against the persisted
derivative hashes. Missing hashes or unexpected bytes keep the entire attempt for review; GET failures stop
deletion of that attempt. It does not remove a substituted object just because its key is in the journal.

**Limits:** other writers or external object deletion do not participate in this advisory lock. Authenticated
readback is a point-in-time check, not an atomic S3/PostgreSQL commit. External deletion or replacement after
readback can still break references; external writes between GET and PUT are not conditional-write protected.
Only this loader should write its random attempt keys. Exact reruns with already committed rows do no storage
work; run `verify --objects --assets-dir ...` to detect later object loss. Recompute derivatives with the same
Pillow/JPEG environment as the load; encoding changes fail closed and require review.
Session advisory locks require a dedicated database session (direct connection or session pooling, not
transaction pooling). A crashed client's session may keep its lock until the server closes the session;
contenders fail after the configured lock timeout rather than proceeding without coordination.

**If the journal is lost:** (1) objects of uploaded-but-uncommitted attempts are orphaned; their private objects
stay private, but an orphaned card stays publicly readable under its random token until removed by a reviewed
manual procedure (the IDs are not recoverable from the database); (2) a loaded row that staff deleted is re-added
by the next run (no `removed_after_load`); (3) a replaced image is detected only through
`previous_prepared_sha256`. Exact reruns still find nothing to do. Copy the journal off the instance after every
run.

`verify` (read-only) checks every planned asset is present and unchanged, covers at order 0, cache-field pairs,
key prefixes, card URL under the public base, at least four images per listing, and with `--objects --assets-dir DIR`
authenticates and reads every exact object. Each stored SHA-256 must match the actual `process_image` derivative
of its approved prepared input; metadata, dimensions and mark contrast are checked too. This establishes that
the private original and WhatsApp objects exist and are correct **before** anonymous denial is tested.
`--urls-out` writes asset, card URL and the public-form URLs of the private objects for the anonymous checks
(card 200; private objects denied with any 4xx). An anonymous 404 alone is never existence/privacy acceptance. `fingerprint`
prints listing counts per status, `v_public_listing` count, a hash of every listing column except
`cover_media_id`, `version`, `updated_at`, and a separate `last_confirmed_at` hash.

Output never contains connection strings, credentials, phone numbers or raw exception text (class, SQLSTATE and
constraint only).

## 6. WhatsApp send check (G-10, `python -m app.media_send_check`)

Operator CLI, not a chat command. Default preview reads the row (cache state, card URL, and with
`MEDIA_CHECK_RECIPIENT` whether a conversation and an open 24 h window exist) and sends nothing. `--send
--check-id <id>` sends through Task 4.2 `media.send_image` (upload-once ID cache, outbound dedupe key
`media-check:<id>:<row id>`, control gate R-28 kept, 24 h window, link fallback). The recipient is read from the
environment, normalised, and never printed; failures (Meta, S3, database) are reported by class / code
only. Verified with a fake Meta client only; a real send is a separately
authorised production step.

## 7. Tests (local)

- `tests/test_demo_media.py` (no database): plan rules on the real plan and 16 mutations of it, pack in sync,
  empty committed intake, 15 intake rules, duplicate content, pilot vs full scope, file hash checks and path
  traversal, crop geometry pixel-identical to `ImageOps.fit`, mark inside the crop-safe region for 4 corners x 3
  shapes, marks in all derivatives (and not detected when absent), source-corner loss, EXIF/GPS/XMP stripping,
  determinism, shape rejection, existing validation kept, font guard, prepare / bundle CLIs, journal durability /
  lock / tamper / target-binding checks, classification cases (incl. `cover_cleared`).
- `tests/test_media_seed.py` (disposable PostgreSQL; Task 4.3 seed, fake S3 / Meta): copied-journal concurrency
  during upload and deletion, connection target mismatch, exact runtime role, partial upload retries,
  missing/substituted derivatives and authenticated integrity checks; see
  docs/TASK_LOG.md for counts and results.
- `tests/demo_media_fixtures.py`: synthetic TEST FIXTURE generator (not a test module).
