# Schema vs reference specs (2026-10-08)

The full reference set (Final Architecture RFC v1.0, Red-Team Review R-01..R-39, Demo Scope, Execution
Plan, R11-R13 data model D11, R17/R18 state D7/D10, R6 handoff D3/D4, R4/R7/R9 UX/media D9/D14, R3
capture D5/D6, R16 LLM/STT, D12/D13 tenancy) was shared on 2026-10-08. It is kept outside the repo. Tasks
3.3 (migration 005) and the first draft of 3.4 (migration 006) were written without it. This file
compares the schema to those specs. Spec enum names are uppercase; the repo keeps lowercase CHECK
values (owner decision), so `ON_HOLD` here means `on_hold` in SQL.

| Part | Scope | Status |
|---|---|---|
| A (section 1) | Task 3.4: migration 006, conversation state / handoff schema | **Complete locally on feature/task-3-4-conversation-state** (verified; awaiting commit approval) |
| B (section 2) | Inventory gaps in applied migration 005 against D11 / R17 | **Migration 007** (`007_inventory_d11.sql`, branch feature/task-3-3-d11-alignment): G1-G15, G18, G19 done; G16 with Task 4.1, G17 with 7.4 / 9.2 |

## 1. Part A: Migration 006 (Task 3.4), complete

006 follows R17 section 5 (minimum persistent state), D4 / R6 and Red-Team R-28..R-31:

| Spec | 006 |
|---|---|
| conversation: control_state, preferred_language, last_modality, last_inbound_at | control_state, preferred_language, last_modality; last_inbound_at = existing last_customer_message_at |
| conversation: service_window_expires_at (R17 5, RFC 8 "window expiry") | **Not stored** (owner decision, deliberate departure): derived from last_customer_message_at by app/outbound.py so it cannot go stale. A later task needing a stored value must add update logic and tests |
| requirement_state: 15 slots with raw / source / confidence / confirmed / updated_at, versioned, 90-day reset | as specified; slot names and `source` enforced by CHECK |
| requirement_patch_log: version, patch[] of {slot, op, raw_span}, message_id; ops set / add / remove / clear / relative | as specified; each op must name a known slot and one of the five ops (the earlier extra `reset` op was removed: a 90-day reset is a set of `clear` ops) |
| search_run: conversation, requirement_version, filters_json, relaxations[], total_matches | as specified (`filters`) |
| shown_item: search_run_id, position, property_id, card_wamid, shown_at | card WAMID via `card_message_id -> message.provider_message_id` (same lookup, not stored twice) |
| focus: selected_property_id, selected_via, updated_at | as specified (`property_id`) |
| media_sent: property_id, property_media_id, sent_at | as specified + message_id |
| pending_clarification: slot, candidates[], asked_wamid, expires_at; re-ask once, expire after 2 unanswered turns (edge 11) | as specified (`asked_message_id`) + `unanswered_turns` |
| message: wamid, direction, modality, text_or_transcript, stt_meta, context_wamid | as specified (modality derived from message_type) |
| lead: contact, requirement snapshot, viewed and liked IDs, transcripts, reason, summary, broker; code L-104 | lead + **lead_property** (confirmed properties, several allowed for "Dono", R-31); viewed IDs = shown_item; contact = conversation; transcripts = message rows |
| handoff: broker, timestamps, Accept / Pass / Called, outcome Visit booked / Not interested / Deal, accept timer, 48 h release, one holding reply | as specified: assigned / accepted staff, accept_due_at, escalated_at, release_due_at, holding_reply_sent_at, called_at, outcome (+ outcome_at) |
| Send-time gate reads control_state (R-28) | **Deferred by owner decision**: the gate still reads `mode`; a CHECK keeps control_state consistent with it. Control-state integration is pending for the orchestration / handoff tasks (5.1, 11.2) |

Not in 006 on purpose (owned by other tasks): the per-contact carousel fallback flag (R-39, plan 5.2),
reassignment history for #pass / #assign (R-32, production), staff.areas_covered for broker routing
(RFC C13, plan 11.1), stored customer profile name for the lead card (taken from the webhook contact).

## 2. Part B: Migration 005 (Task 3.3, applied on RDS) gaps: follow-up task, not Task 3.4

Owner decision 2026-10-08: 005 is not modified and no inventory migration is added on the Task 3.4
branch. 005 is applied and immutable, so every item below needs a **new forward migration**. Several block the
next data task (4.3 seed: 30 listings, gazetteer, projects).

### Blocks the 4.3 seed or the demo scenarios

| # | Gap | Spec | Effect |
|---|---|---|---|
| G1 | location has no ZONE level (city > area > sub_area only) | D11 2: CITY > AREA > SUBAREA > ZONE (Bukhari / Badar Commercial, Tariq Road) | DH6-108 ("DHA Ph 6 > Bukhari Commercial", scenario 1) and DH5-101 cannot be placed |
| G2 | location has no `code` | D11 2: code for property IDs (DH1..DH8, D2X, D7X, DHC, CLF, BTK, GLI, PEC, NNZ, SHF) | the {AREA_CODE}-{seq} listing code (scenario 4a "DH6-109") has no source |
| G3 | property.construction_status lacks `grey_structure`, `off_plan`; project too | D11 1.1 / 1.2 | PRJ-103 (OFF_PLAN) cannot be seeded |
| G4 | no possession_status, expected_possession, installment fields, is_tenanted, current_rent, investment_tags | D11 1.1; R17 hard filters on construction / possession / investment flags | investment scenario rows (PRJ-101..103, CLF-102 tenanted, DHC-101 installments, BTK-106 FILE) and those filters |
| G5 | no nominal sq-yd bucket (`size_nominal_sqyd`); one area value only (no plot vs covered area) | D11 1.1 / 4.1 (Bahria "250 yd" = 272 actual; buckets) | "Bahria 10 marla" scenario matching on the bucket |
| G6 | property_type lacks `penthouse`, `showroom`, `warehouse`, `building`, `floor` | D11 1.1 | type mapping (APARTMENT ~ PENTHOUSE, R17 5) |
| G7 | property.status lacks `on_hold`, `archived` | D11 5 lifecycle; R-27 auto-hold; staff [Hold] button | staleness flow and Hold action |
| G8 | no property.last_confirmed_at | D11 1.1 / 5; R17 ranking (freshness 10%, ties by last_confirmed_at); R-27 lead card "last confirmed N days ago" | ranking and staleness |
| G9 | property_event types lack `reconfirmed` | D11 1.4 | staleness re-confirmation audit |

### Needed later in the plan (not blocking 4.3)

| # | Gap | Spec / task |
|---|---|---|
| G10 | location: display_name vs canonical_name, city_code, centroid lat/lng, is_commercial_zone, `level_label` (phase / precinct / block / sector / zone) | D11 2; R17 3.2 (bare-number resolution, 7.1); location messages use the area centroid |
| G11 | location_alias: `weight` (0-1), `kind` (official / common / misspelling / urdu_script / voice_stt) | D11 3.1 (7.1 resolver) |
| G12 | location_neighbor: `weight` | R17 3.2 (relaxation ladder) |
| G13 | property customer fields: title (<=60), description (<=600), furnishing, facing (multi), is_corner, features (controlled list), floor_no, total_floors, portion_level, parking_spaces, age/year_built, price_negotiable, rent deposit / advance months, maintenance, min_lease_months, available_from, address_detail (post-handoff) | D11 1.1 (renderer 9.1, known_fields R-05). `public_features` JSONB can carry some, but search filters and the view allowlist need typed columns for anything filtered |
| G14 | property internal fields: source_type, owner_name, owner_phone, floor_price_pkr, commission_pct / note, is_exclusive, assigned_broker_id, docs_status, visit_notes, source_message_ids, duplicate_group_id | D11 1.1 (internal-only; never in v_public_listing); R17 edge 14 |
| G15 | project: project_type, expected_completion, payment_plan_summary, amenities, approval_note (internal) | D11 1.2 |
| G16 | property_media: kind, label, public_url_card, wa_media_id, wa_media_expires_at, exif_stripped | D11 1.3 / D14 (tasks 4.1 / 4.2, as 005 already noted) |
| G17 | listing_draft: `superseded` status, conflicts[], confirmation-card WAMID (reply-to-card attach), segment grouping (1-5 drafts per segment) | Red-Team R-08, R-10, R-12, R-14 (tasks 7.4 / 8.2 / 9.2) |
| G18 | agency: city_default, marla_sqft (default 225), settings; staff: areas_covered | D11 1.5; RFC C13 / C15 (normalisers 6.2, routing 11.1) |
| G19 | v_public_listing must add every new customer-visible column (and keep internal ones out) | D11 customer-visible rule |

### Matches the specs (keep)

Shared gazetteer without RLS; context aliases (`context_parent_id`); trigram only on aliases; symmetric
neighbours; composite tenant FKs; FORCE RLS; append-only property_event with undo; one open update
draft per staff + listing (R-14); minimum-to-save rule on active rows (R-07); area conversions
(225 / 4500); integer PKR, monthly rent; v_public_listing ACTIVE-only allowlist with security_invoker.

### Follow-up task: done in migration 007 (see docs/INVENTORY_SCHEMA.md, Migration 007)

G13 split (owner decision 2026-10-08): fields search filters or ranks on are typed columns; display-only
facts are allowlisted public_features keys. G16 (media) and G17 (drafts) remain with their tasks.

Original scope:

A forward migration (numbered after 006, e.g. `007_inventory_d11.sql`) before Task 4.3: ZONE level + location code /
level_label / centroid / city_code / commercial flag; alias kind + weight; neighbour weight; property
status `on_hold` / `archived`, the D11 types, construction / possession / investment columns,
nominal bucket and plot / covered area, last_confirmed_at, the customer-visible D11 fields and the
internal-only fields; `reconfirmed` events; project fields; agency city_default / marla_sqft. It must
replace CHECK constraints on applied tables (drop + add in the new migration), recreate the view with
the extended allowlist, and keep every new internal column out of it. Media (G16) and draft (G17)
fields can stay with their own tasks.
