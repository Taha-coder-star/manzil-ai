# Conversation state and handoff schema (Task 3.4, migration 006)

Source: 3-Week Execution Plan 3.4, R17/R18 section 5 (minimum persistent conversation state, D10),
R6 (D4 handoff), Red-Team R-04 / R-28...R-31, RFC v1.0 sections 7-8, Demo Scope scenarios 3 and 5, the
3.1/3.2 handoff, and the owner's Task 3.4 decisions of 2026-10-08. The reference documents were shared
after the first draft; 006 was then aligned to them (docs/SPEC_ALIGNMENT.md lists every remaining
deviation). Schema only: no state machine, search, handlers, transcription or booking code. The
WhatsApp send gate is unchanged.

## Overview

| Table | Purpose | Runtime (manzil_app) privileges | RLS |
|---|---|---|---|
| requirement_state | customer requirements, one versioned row per conversation | SELECT, INSERT, UPDATE | ENABLE + FORCE, tenant_isolation |
| requirement_patch_log | append-only history of requirement patches | SELECT, INSERT | ENABLE + FORCE, tenant_isolation |
| search_run | one executed search | SELECT, INSERT | ENABLE + FORCE, tenant_isolation |
| shown_item | listings shown by a search, by position | SELECT, INSERT | ENABLE + FORCE, tenant_isolation |
| focus | the listing a customer conversation is about | SELECT, INSERT, UPDATE | ENABLE + FORCE, tenant_isolation |
| media_sent | listing photos already sent in a conversation | SELECT, INSERT, UPDATE | ENABLE + FORCE, tenant_isolation |
| pending_clarification | open question to the customer | SELECT, INSERT, UPDATE | ENABLE + FORCE, tenant_isolation |
| lead | customer lead for brokers | SELECT, INSERT, UPDATE | ENABLE + FORCE, tenant_isolation |
| lead_property | the confirmed properties of a lead | SELECT, INSERT | ENABLE + FORCE, tenant_isolation |
| handoff | one handoff attempt (accept timer, ownership, release) | SELECT, INSERT, UPDATE | ENABLE + FORCE, tenant_isolation |
| next_lead_number() | lead number allocator (function) | EXECUTE | runs as the caller, under RLS |

No table grants DELETE, TRUNCATE, REFERENCES or TRIGGER to the runtime, and PUBLIC has nothing.
Rows end by status, never by deletion. tenant_isolation is exactly the 002 policy:
`FOR ALL TO manzil_app USING (agency_id = current_agency_id()) WITH CHECK (agency_id = current_agency_id())`.

Every table has `agency_id UUID NOT NULL REFERENCES agency(id)` and `UNIQUE (agency_id, id)`, and
every reference to another tenant row is a composite FK that starts with agency_id (24 of them; a
catalog test enforces this). References that must stay inside one conversation also carry
conversation_id, e.g. `(agency_id, conversation_id, message_id) -> message(agency_id, conversation_id, id)`.
Lowercase CHECK vocabularies, NULL for unknown, application-maintained `updated_at`, as in 001-005.

## Existing tables (ALTER)

### conversation

| Column | Type | Notes |
|---|---|---|
| control_state | TEXT NOT NULL DEFAULT 'ai_active' | `ai_active`, `handoff_pending`, `human_owned`, `closed` |
| preferred_language | TEXT NULL | `english`, `roman_urdu`, `urdu`; NULL = unknown |
| last_modality | TEXT NULL | `text`, `voice`; NULL = unknown |
| carousel_fallback | BOOLEAN NOT NULL DEFAULT false | Task 5.2, migration 011: results go out as individual cards, not a carousel |
| carousel_fallback_set_at | TIMESTAMPTZ NULL | last change of the flag (either direction) |
| carousel_fallback_set_by | UUID NULL | staff member who changed it; composite FK `(agency_id, carousel_fallback_set_by)` -> staff |

**Carousel fallback (Task 5.2, migration 011).** A conversation is one row per `(agency_id,
customer_wa_id)` for the contact's whole life: 001's UNIQUE key, ingestion only upserts
(app/handlers.py), and `closed` is a control_state of the same row. So the flag is per contact,
survives a closed conversation and covers all of the contact's devices (phone and WhatsApp Web share
it). Typed columns follow preferred_language / last_modality rather than a key in `conversation.state`
(owner decision 2026-10-10). CHECK `conversation_carousel_fallback_set_check`: a set flag, or a
recorded setter, has a change time. The runtime keeps its table-level grants (002); FORCE RLS is
unchanged. Only staff `#demo fallback on|off` writes it in 5.2, and only on the sender's own
conversation; a client that fails to render a carousel sends no signal, so nothing sets it
automatically.

`mode` (`ai`, `human`, `paused`) and `ai_muted_until` are unchanged, and **the send gate in
app/outbound.py still reads `mode`**. control_state is the foundation for Task 11.2.

**Backfill mapping**

| mode | control_state | Reason |
|---|---|---|
| ai | ai_active | same meaning |
| human | human_owned | a human owns it; the AI is silent |
| paused | human_owned | never an AI state; resumes only by an explicit human/app action. `mode` keeps `paused`, so nothing is lost |

No existing row maps to `handoff_pending` (that requires an open handoff with an accept timer) or to
`closed` (a future "reopen on new message" rule could silently re-enable AI). `ai_muted_until` is a
temporary, orthogonal mute and is not mapped; no code reads it today.

**Backfill under FORCE RLS.** The migration owner has no policy, so with FORCE it sees zero rows and a
plain UPDATE would silently change nothing. The migration lifts FORCE (`NO FORCE ROW LEVEL SECURITY`),
updates, aborts if any `mode <> 'ai'` row is still `ai_active`, and restores FORCE, all in the migration
transaction. ALTER TABLE holds an ACCESS EXCLUSIVE lock until commit, so no other session can see the
intermediate state. tests/test_rls_migration.py runs this as a non-superuser schema owner (as on RDS)
with live rows; superuser test logins bypass RLS and would hide the problem.

**Gate consistency.** `conversation_control_state_gate_check: CHECK (control_state = 'ai_active' OR mode <> 'ai')`.
A conversation that control_state silences can never look AI-enabled to today's mode-based gate, so a
future handoff writer cannot forget `mode`. The converse is allowed on purpose: setting `mode` alone
still silences an `ai_active` conversation (the existing behaviour, and test_gateway's path). When the
gate moves to control_state (a later task), it should cancel AI messages if **any** of
`mode <> 'ai'`, `control_state <> 'ai_active'` or `ai_muted_until > now()` holds.

No `service_window_expires_at` (owner decision; deliberate departure from R17 5 / RFC 8): the 24 h
window is derived from `last_customer_message_at` by app/outbound.py, and a stored copy that nothing
maintains would be a second, stale source of truth. A later task that needs a stored value must add
reliable update logic and tests with it. Control-state integration (the gate reading control_state)
is pending for the orchestration / handoff tasks (5.1, 11.2).

### message

| Column | Type | Notes |
|---|---|---|
| modality | TEXT GENERATED ALWAYS AS (...) STORED | `text` for message_type text, `voice` for audio, else NULL; read-only |
| transcript | TEXT NULL | sensitive; only on inbound audio |
| stt_meta | JSONB NULL (object) | provider metadata; only on inbound audio |
| context_wamid | TEXT NULL, non-blank | WAMID this message quotes (WhatsApp context.id) |

modality is derived so rows written by the unchanged `record_inbound` are labelled correctly; a plain
`DEFAULT 'text'` would mislabel voice notes. Generated columns are explicitly STORED (PostgreSQL 18
defaults to VIRTUAL). Other new columns are NULL until their owning task writes them; nothing is
partially backfilled, so old and new rows stay consistent.

context_wamid is written for inbound messages from Task 5.2 (app/handlers.py, a non-blank
`context.id`: a swipe-reply or a button tap); rows stored earlier keep NULL.

### agency

`next_lead_number INTEGER NOT NULL DEFAULT 1 CHECK (> 0)`: the per-agency lead counter.

## New tables

### requirement_state

id, agency_id, conversation_id (UNIQUE per agency: one per conversation), version (>= 0, default 0 =
nothing captured), slots JSONB, created_at, updated_at.

- slots: `slot -> {value, raw, source, confidence, confirmed, updated_at}`; an absent slot is unknown.
  A CHECK allows only these slot names: purpose, category, property_types, location, budget,
  previous_budget, size, beds_min, baths_min, construction, possession, investment, soft_prefs,
  unmapped_prefs, excluded_property_ids. Each slot must be an object with `source` in
  `text`/`voice`/`inferred`/`default`; `confidence`, when present, is a number in 0..1; `confirmed`,
  when present, is boolean. Adding a slot needs a migration.
- Optimistic concurrency contract (8.1): `UPDATE ... SET slots = ..., version = version + 1 WHERE id = :id
  AND version = :expected`; 0 rows means a concurrent writer won. The patch log's
  `UNIQUE (conversation, version)` backs this up.
- 90-day idle reset (R17 5) is a patch of `clear` ops to a new version, not a DELETE. The lead stays.
- `excluded_property_ids` holds ids inside JSONB (not FK-checked); it is only used to exclude results
  under RLS, never to fetch.

### requirement_patch_log (append-only)

id, agency_id, conversation_id, version (> 0, the version the patch produced), ops JSONB,
source_message_id NULL, created_at.

- `UNIQUE (agency_id, conversation_id, version)`; FK `(agency_id, conversation_id) -> requirement_state`.
- source message: `(agency_id, conversation_id, source_message_id) -> message` (same conversation).
- ops: non-empty array of R17 RequirementPatch items `{slot, op, raw_span, ...}`: `op` in set, add,
  remove, clear, relative (R17 2.1) and `slot` one of the 15 slot names. Old values for undo
  ("pehle wala budget") go in the op. The 90-day reset is logged as `clear` ops.

### search_run (append-only)

id, agency_id, conversation_id, requirement_version, filters JSONB object, relaxations JSONB array,
total_matches (>= 0), created_at.

- `(agency_id, conversation_id, requirement_version) -> requirement_patch_log(agency_id, conversation_id, version)`:
  a search always names a version that exists for its own conversation.
- filters must describe customer-visible criteria only (search reads v_public_listing).
- "Latest search only, positions valid 24 h" (10.1) is a query on `created_at`
  (index `(agency_id, conversation_id, created_at DESC)`).

### shown_item (append-only)

id, agency_id, conversation_id, search_run_id, position (> 0), property_id, card_message_id NULL, shown_at.

- `UNIQUE (search_run, position)` and `UNIQUE (search_run, property)`.
- `(agency_id, conversation_id, search_run_id) -> search_run(...)`, card message in the same conversation.
- The card's WAMID is `message.provider_message_id` of card_message_id (set when the send lands), so a
  quoted card (message.context_wamid) resolves with one join and the WAMID is not stored twice.
- Rendering re-reads the listing through v_public_listing at answer time (10.1, 13.2).

### focus

id, agency_id, conversation_id (UNIQUE: one per conversation), property_id NULL, selected_via NULL
(`button`, `position`, `quote`, `description`), created_at, updated_at.
`CHECK ((property_id IS NULL) = (selected_via IS NULL))`. Clearing focus sets both to NULL.
Customer-conversation focus only; staff working context stays in staff_session.focus_property_id.

### media_sent

id, agency_id, conversation_id, property_id, property_media_id NULL, message_id NULL, sent_at.

- `(agency_id, property_id, property_media_id) -> property_media(agency_id, property_id, id)`: the photo
  belongs to the recorded listing. `ON DELETE SET NULL (property_media_id)`: staff can still delete a
  photo; the record that something was sent stays.
- `UNIQUE (agency_id, conversation_id, property_media_id)`: each photo at most once per conversation
  ("aur pictures" without repeats); `INSERT ... ON CONFLICT DO NOTHING` claims the next unsent photo.
  A deliberate resend is out of scope.

### pending_clarification

id, agency_id, conversation_id, slot (`location`, `purpose`, `target`), candidates JSONB array,
asked_message_id NULL (same conversation), unanswered_turns (>= 0), status (`open`, `resolved`,
`expired`, `cancelled`), expires_at, created_at, updated_at, closed_at.

- unanswered_turns implements R17 edge 11: a customer turn that does not answer keeps the item, re-asks
  once, and after 2 unanswered turns the item expires (location falls back to area / SUBTREE level).

- **One open clarification per conversation**: partial unique index `WHERE status = 'open'`.
- `(status = 'open') = (closed_at IS NULL)`, `expires_at > created_at`.
- Expiry is time-based; the application must close an expired row (`status = 'expired'`) before opening a
  new one, in the same transaction. Search must not run while one is open (8.1).

### lead

id, agency_id, conversation_id, lead_number, code (generated), status (`open`, `closed`),
requirement_version NULL, requirement_snapshot JSONB object, last_customer_message_id NULL,
reason NULL, summary NULL, assigned_staff_id NULL, created_at, updated_at, closed_at.

- `UNIQUE (agency_id, lead_number)`; `code = 'L-' || lead_number`, at least two digits (L-07, L-10,
  L-123), generated so it can never disagree with the number.
- **One open lead per conversation** (partial unique `WHERE status = 'open'`); this is also the upsert
  target for "lead upsert" (11.1).
- Lead packet (R6 / RFC 7 / R-30): contact = the conversation's customer_wa_id; requirements =
  requirement_snapshot (+ requirement_version, which references the same conversation's patch log);
  viewed properties = shown_item of the conversation; confirmed properties = lead_property; last
  message and transcripts = referenced message rows (not copied, so customer text is not duplicated).
  The customer's WhatsApp profile name comes from the stored webhook contact.
- reason and summary (LLM, 11.1) are internal, broker-facing text.

**Lead number allocation contract.** Call `next_lead_number()` in the same tenant transaction that
inserts the new lead, and only when a new lead is really being inserted:

```sql
-- inside tenant_transaction; first check for an open lead (SELECT ... FOR UPDATE)
INSERT INTO lead (agency_id, conversation_id, lead_number, ...)
VALUES (current_agency_id(), :conversation, next_lead_number(), ...);
```

- The function runs `UPDATE agency SET next_lead_number = next_lead_number + 1 WHERE id =
  current_agency_id() RETURNING next_lead_number - 1` as the caller (SECURITY INVOKER, RLS applies).
- The agency row lock serialises concurrent allocations until commit; a rollback returns the number,
  so numbering is gapless. Agencies have independent counters.
- Without tenant context it returns NULL and the NOT NULL lead_number makes the insert fail.
- `MAX(lead_number) + 1` is not used. `UNIQUE (agency_id, lead_number)` is the final backstop.
- Do not call it in the VALUES list of an `INSERT ... ON CONFLICT DO UPDATE`: the number would be
  consumed even when the conflict path updates an existing lead.

### lead_property (append-only)

id, agency_id, lead_id, property_id, created_at; `UNIQUE (agency_id, lead_id, property_id)`; composite
FKs to lead and property. The property (or properties: "[Dono]", R-31) the customer confirmed for the
visit / handoff; several are allowed, each once.

### handoff

id, agency_id, conversation_id, lead_id, status (`pending`, `accepted`, `completed`, `released`,
`cancelled`), assigned_staff_id NULL, accepted_by_staff_id NULL, requested_at, accept_due_at,
escalated_at, accepted_at, release_due_at, holding_reply_sent_at, called_at, outcome NULL
(`visit_booked`, `not_interested`, `deal`), outcome_at, closed_at, created_at, updated_at.

- `(agency_id, conversation_id, lead_id) -> lead(agency_id, conversation_id, id)`: the lead belongs to
  the same conversation.
- **One open handoff per conversation**: partial unique `WHERE status IN ('pending', 'accepted')`.
- `(status IN ('pending','accepted')) = (closed_at IS NULL)`; accepted_at and accepted_by_staff_id are
  set together; `accepted` and `completed` require them; `pending` forbids them; all timestamps are
  >= requested_at and due times are after it.
- Status vs control_state (11.2 keeps them together in one transaction): pending -> handoff_pending,
  accepted -> human_owned, completed / released / cancelled -> ai_active (or closed). `#pass` and the
  ADMIN escalation (R-29) reassign assigned_staff_id and set escalated_at while pending.
- Broker buttons (R6, RFC 7): [Accept] sets accepted_*; [Pass] / accept timeout reassign
  assigned_staff_id (next broker, then ADMIN, R-29) and set escalated_at; [Called] sets called_at; the
  outcome buttons set outcome + outcome_at together (ROI tracking). Reassignment history (R-32) is a
  production item.
- Holding reply, one per handoff: `UPDATE handoff SET holding_reply_sent_at = now() WHERE id = :id AND
  holding_reply_sent_at IS NULL`; send only if 1 row changed. Holding replies use
  `send_message(..., ai_generated=False)` (exempt from the gate).
- Timers (accept timeout, 48 h release) are delayed jobs (`enqueue_job(..., delay_seconds=)`) with
  dedupe keys such as `handoff_accept_timeout:{handoff_id}`; accept_due_at / release_due_at record the
  deadlines for display and checks.

## Not enforced by the database (application contracts)

- control_state and the open handoff's status stay in step (11.2, one transaction).
- An expired clarification is closed before a new one opens.
- requirement_state.version equals the newest patch-log version for the conversation.
- lead.assigned_staff_id / handoff.assigned_staff_id are brokers or admins (staff.role).
- JSONB ids (excluded_property_ids, candidates) are not referential.

## Privacy

transcript, stt_meta, requirement slots and patch history (raw customer wording), lead summary /
snapshot, lead_property and handoff rows are customer data. They are tenant-isolated (FORCE RLS), the runtime cannot
delete them, and none of them may be logged (log ids and error codes only, as the existing pipeline
does). Broker-facing fields (reason, summary, snapshot) are never shown to customers. Retention and
privacy deletion need a separately reviewed admin workflow (docs/OPEN_ISSUES.md).

## Tests

tests/test_state.py (restricted manzil_app login): exact privilege matrix incl. PUBLIC and the
allocator; FORCE RLS and the single tenant policy; two-tenant / missing / unknown context; insert under
another or no tenant; cross-tenant moves; cross-tenant references on insert (23) and update; a catalog
guard that all 24 tenant FKs start with agency_id; no DELETE/TRUNCATE; append-only histories; every
CHECK, partial unique index and same-conversation FK; slot and op validation; lead numbering
(sequential, per agency, gapless on rollback, NULL without context, concurrent allocations serialised); two-connection races to open a lead, handoff or clarification for one conversation (one open row remains; the loser fails if the winner commits and succeeds if it rolls back);
holding-reply once; broker Called / outcome; several properties per lead; the unchanged webhook -> record_inbound path with the new columns.
tests/test_migrations.py upgrades schema v1 with ai/human/paused conversations and messages;
tests/test_rls_migration.py repeats the 005 -> 006 upgrade as a non-superuser owner under FORCE RLS.

## Conversation turns (Task 5.1, migration 010)

Component C3 (RFC flow steps 2-3, Red-Team R-01). Code: `app/orchestrator.py`, called by the
`process_message` handler (`app/handlers.py`).

| Object | Purpose | Runtime (manzil_app) privileges | RLS |
|---|---|---|---|
| conversation_turn | one coalesced turn: routing + control snapshot, status open -> replied / gated / failed (014: job will not retry, or 3 attempts incl. crashes; attempts column) | SELECT, INSERT, UPDATE | ENABLE + FORCE, tenant_isolation |
| message.turn_id | the turn that consumed an inbound message (NULL = not in a turn) | (message grants) | (message) |

- Keys: `UNIQUE (agency_id, id)` and `(agency_id, conversation_id, id)`; message.turn_id is a
  composite FK `(agency_id, conversation_id, turn_id)`, so a message can only join a turn of its own
  conversation and tenant. trigger_job_id -> job, staff_id -> staff, both agency-scoped.
- One open turn per conversation (partial unique index). CHECKs: route matches sender_role
  (customer -> customer, broker -> broker, admin / agent -> staff); staff_id is NULL exactly for
  customers; completed_at is set exactly when status is not open; turn_id only on inbound messages.
- Lifecycle per job: record the trigger and every pending sibling message; in one transaction
  (conversation row FOR UPDATE) resume an open turn or assemble a new one from eligible messages
  (turn_id NULL and job = trigger or pending), point them at it and mark absorbed sibling jobs
  succeeded / 'coalesced'; gate; send with dedupe keys turn:{turn_id}:{seq}; close the turn. A
  crash before close leaves the turn open and the retry (or the next job) resumes it with the same
  messages and dedupe keys, so nothing is consumed or sent twice.
- Turn contract for responders (`orchestrator.Turn`): agency / conversation / turn ids, sender
  WhatsApp id, sender_role, route, staff_id, control_state and mode snapshots, and the ordered
  messages (id, WAMID, type, text, media id, WhatsApp timestamp, and from Task 5.2 the tapped
  reply id of an interactive message (`interactive.<type>.id`) or of a carousel quick reply
  (`button.payload`), read from the stored webhook event because the message log
  keeps only the title).
- `reply_plan` (migration 011, Task 5.2): JSONB array, NULL until planned. The staff card commands
  store their payloads here once, before the first send; a retried or resumed turn sends exactly
  this list under the same `turn:{id}:{seq}` keys (docs/DECISIONS.md). Grants and RLS are the 010
  ones.


## Staff segments (Task 7.4, migration 015)

- `staff_segment`: one staff member's buffered listing input. `status` open / flushed,
  `kind` description / photo_only (set when flushed), `version` (bumped per item; flush
  jobs carry it), `last_item_at`, `flush_due_at`, `flushed_at`, `flush_reason` timer / done /
  next_description, `flags[]` photo_only / media_pending / media_failed. At most one open
  segment per (agency, staff).
- `staff_segment_item`: the segment's messages in order (`position`), `text_role`
  description / note / NULL, `is_photo`. `UNIQUE (agency_id, message_id)`: a message is in at
  most one segment.
- `conversation_turn.staff_segment_id`: the one staff turn that answers a flushed segment;
  its messages are the segment's items (they keep the `turn_id` of the turns that buffered
  them). Unique per segment; route must be staff.
- FORCE RLS, `tenant_isolation`, `manzil_app` SELECT / INSERT / UPDATE (no DELETE).
  Contract and flow: docs/STAFF_BUFFER.md.
