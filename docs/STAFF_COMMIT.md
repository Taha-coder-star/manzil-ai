# Staff confirmation and commit (Task 9.2)

Status (2026-10-14): implemented on `feature/task-9-2-staff-commit`, stacked on PR #37 (8.2,
migration 016). Adds migration **017** (`staff_card`). Not applied to RDS, not deployed, no live
model run. All staff wording below is **proposed and needs owner approval**.

Sources: Execution Plan 9.2; Demo Scope S5-S8 and scenario 4 (4a-4c); Red-Team R-07, R-08,
R-10, R-11, R-14, R-36; RFC "commit_change is triggered only by a confirmation button";
`docs/STAFF_EXTRACTION.md` sections 4.2, 4.5, 6, 7.2, 8 and 13.

Owner decisions (2026-10-14): a typed `#undo CODE` shows a confirm card, while the Undo
**button** applies at once (within 30 minutes); Edit is deterministic (no LLM); the
reply-to-card map is a new table (017); the branch is stacked on #37.

## 1. Shape

| File | Content |
|---|---|
| `app/staff_actions.py` | Deterministic, no model: `split` (which messages of a staff turn are 9.2's), `respond` (Save / Edit / Cancel / Confirm, undo, pick, attach, corrections), `attach_prompt` (photo-only list), `register_cards` (the 017 map). |
| `app/staff_cards.py` | Reply-buttons cards (cover header, English labels, ⚠), list messages, commit replies, button / row id builders, notices. |
| `app/staff_normalise.py` | Pure additions: `next_code`, `changes_in_text`, `apply_correction`. |
| `app/staff_tools.py` | `draft_media`, `segment_media`, `media_rows`, `property_facts`, `draft_card`, `update_card`; the pick list carries the change. |
| `app/orchestrator.py` | `TurnMessage.context_wamid`; 9.2 split before the staff buffer; photo-only list; card registration in `_send`. |
| `migrations/017_staff_card.sql` | `staff_card (agency_id, dedupe_key, listing_draft_id XOR property_id)`, FORCE RLS, SELECT / INSERT only. |

The model's role is unchanged from 8.2: it only proposes (spans). Nothing it says writes a
listing; every write follows a tap whose id the backend re-validates.

## 2. Ids (client-supplied, so re-checked in code)

| Id | Meaning |
|---|---|
| `draft:save\|edit\|cancel:{draft}:{version}` | create draft card |
| `draft:confirm\|cancel:{draft}:{version}` | update draft card |
| `undo:{event}` | Undo on a Saved / Updated reply (30 minutes, same staff member) |
| `undoc:confirm\|cancel:{event}:{property version}` | confirm card of a typed `#undo CODE` |
| `pick:{CODE}:{p=PKR;s=status}` | a row of "Kaunsi property?" |
| `attach:{segment}:{d\|p}:{draft or listing}` | a row of "Ye photos kis listing ki hain?" |
| `attachc:confirm\|cancel:{segment or turn}:{listing}:{version}` | attach photos to a saved listing |

Strict regexes (`staff_actions.TAPS`); anything else falls through to the 5.2 listing-card
stub or the engine. Customer and broker turns never reach `staff_actions` (route check).

## 3. Rules

**Save** (one transaction, row lock on the draft):
1. Draft not open → no write. The same message again (a retried turn) gets the same reply. Another tap gets "already saved as X", "closed" or "stale".
2. A version different from the button → `staff_card_stale`.
3. `save_blockers` is recomputed from the stored fields (R-07, second layer after the hidden button), and the description is re-sanitised (R-36).
4. The code is the next number of the prefix over every status, allocated under `pg_advisory_xact_lock(agency:prefix)`. `UNIQUE (agency_id, listing_code)` is the backstop. A place with no coded ancestor → `staff_no_code`.
5. `property` is inserted ACTIVE, so it is in `v_public_listing` at once. Its fields come from the payload. `is_demo` is set when the agency already holds demo listings. `source_text`, `source_message_ids` (segment WAMIDs), `created_by_staff_id` and `last_confirmed_at` are set too.
6. Photos are written as `property_media` rows that reuse the stored inbound objects (EXIF already stripped). A repeated sha is skipped, and the first photo becomes the cover.
7. A `property_event 'created'` row is written, the draft becomes `saved`, and `payload.done` remembers the message, code and event.

**Confirm (update):**
- The listing must still be at `base_property_version` (R-14). Otherwise the draft is `superseded` and the reply is `staff_update_stale`.
- `check_update` runs again, then an optimistic `UPDATE … WHERE version = base`.
- The event is `updated` (price) or `status_changed`.

**Cancel:** the draft becomes `cancelled`, and a pending Edit on it is cleared.

**Edit:**
- The tap stores `staff_session.state.edit = {draft_id, version, until = now + 10 min}`.
- The next plain text (not `#…`, not "done", not a new listing description), or any text that replies to the draft's card, is the correction. It is taken before the staff buffer, so it never becomes a note or a segment.
- `apply_correction` sets each field the text states once and settles that field's conflict. Two values for one field open a new conflict.
- Location is set through "location / jagah …", or a bare place name that resolves.
- An inferred purpose is re-inferred. Missing fields, blockers, duplicates and the proposed code are recomputed.
- The draft gets version + 1, so its old buttons go stale. The applied message id goes into `payload.edits`, so a resumed turn does not apply it twice.

**Pick (description update, scenario 4c):**
- The list rows carry the change the **backend** read from the staff text (`changes_in_text`: exactly one status word, or a price word followed by exactly one amount that is not the find criterion), never one from the model.
- The tap re-validates the code and the change and creates the update draft (`propose_update_draft`, supersede rule). The reply is the Confirm card with the listing's cover and facts (R-11).
- A row without a change asks for it (`staff_pick_no_change`).

**Undo (R-11):**
- Undo applies only when the listing's version is still the event's `to_version`; otherwise `staff_undo_later_change`.
- A `created` event → status `withdrawn`. `updated` / `status_changed` → each `from` value is restored.
- It is written as a new `undone` event (`undoes_event_id`, version + 1).
- An event can be undone once. `undone` and `media_added` events are not undoable (cut list).
- The button works for 30 minutes, for the staff member who made the change. `#undo CODE` targets the listing's latest event, shows a confirm card, and has no time limit (any staff member of the agency).

**Photos (R-08, R-10):**
- A photo-only segment gets a list of this staff member's open create drafts and the live listings they created or changed in the last 7 days (5 + 5). With none, the 7.4 question stays.
- Photos sent in reply to a card, or picked from the list:
  - **draft** → they join `payload.photos` at once, version + 1, and the card is re-sent (no listing is written);
  - **saved listing** → a confirm card first, then `property_media` + `media_added`, version + 1, and the cover is set if missing.
- With several drafts in one message, photos are never assigned by guess.

**Reply-to-card map (017):** `register_cards` runs in `_send` before any staff card is sent. It records `turn:{id}:{seq}` → draft (from a draft button) or listing (Undo, attach confirm). Lookup: `message.context_wamid` → `outbound_message.provider_message_id` → `dedupe_key` → `staff_card`.

**Idempotency:** every write remembers its message (`payload.done.message_id`, `property_event.changes.message_id`, `payload.edits`, `payload.attached`). A retried turn re-sends the same reply under the same `turn:{id}:{seq}` key; a second tap writes nothing (double tap = no-op).

## 4. Tests

- `tests/test_staff_commit_db.py`, through the real worker on the seeded demo agency:
  - 4a: card with cover and Save → DH6-109 ACTIVE, in `v_public_listing`, 3 photos, cover set, `created` event;
  - a double tap;
  - Undo, the 30-minute expiry and `#undo` with its confirm card;
  - 4b: confirm, double tap, undo restores the price, stale version, undo after a later change;
  - 4c: list → pick → card with cover → SOLD, no longer public, DH6-101 untouched;
  - a forged pick;
  - Edit, a forged Save on a blocked draft, a stale button, a correction by reply;
  - reply-to-card photos with several drafts;
  - the photo-only list for a draft and for a listing, with a repeated confirm;
  - another staff member's tap; a customer's tap;
  - a retry after a failed send;
  - two concurrent Saves → DH6-109 / DH6-110;
  - another agency's draft id;
  - the 017 constraints, privileges and RLS.
- `tests/test_staff_cards.py`: card layout and buttons, the update card with facts, the list rows, commit replies, notices, and the pure rules (`next_code`, `changes_in_text`, `decode_changes`, tap ids, `#undo`, `apply_correction`).

## 5. Wording (proposed; needs owner approval)

**Layout (2026-10-16):** cards use WhatsApp `*bold*` for the head, the codes and the new values,
emoji line marks (📝 🏠 📍 💰 📷 ℹ️ ⚠️ ✅ ✏️ ↩) and blank lines between sections (facts / note and
photos / flags / ready line; `staff_cards._fit` never leaves two blank lines in a row). The
inferred-purpose star (`SALE*` + "* purpose…", which WhatsApp showed as a bullet) is now
"🏠 *HOUSE · SALE* (inferred)" + "ℹ️ Purpose price se samjha gaya". Changes read
"price PKR 8 crore → *PKR 7.8 crore*". The sentences below keep their words.

Card lines (`staff_cards.WORDING`): "⚠ Missing: {fields}. Edit dabayein aur likh kar bhejein.", "⚠ Check - {label}: {values}?", "Sab zaroori maloomat mil gayi. Save dabayein.", "Save ke liye maloomat poori karein (Edit).", "{code}: {changes}. Confirm?", "Kaunsi property? {n} listings milin. List se chunein.", "✅ Saved as {code}. Listing ab active hai.", "✅ {code} updated: {changes}.", "✅ {code} marked {STATUS}.", "Undo {code}: {changes}?", "↩ Undo ho gaya: …", "Ye {n} photos kis listing ki hain? List se chunein.", "{code}: {n} photos add karein?", "✅ {code}: {n} photos add ho gayin."

Notices (`staff_cards.NOTICES`, also in `turn_engine.TEMPLATES`):
- Cards and taps: `staff_card_stale`, `staff_card_closed`, `staff_already_saved`, `staff_already_updated`, `staff_draft_cancelled`, `staff_update_cancelled`, `staff_save_blocked`, `staff_no_code`.
- Edit and update: `staff_edit_prompt`, `staff_edit_unclear`, `staff_update_stale`.
- Undo: `staff_undo_expired`, `staff_undo_later_change`, `staff_undo_none`, `staff_undo_done_before`, `staff_undo_cancelled`.
- Attach and pick: `staff_attach_cancelled`, `staff_pick_no_change`.

## 6. Open items

1. **Deploy:** 8.2 and 9.2 ship together: migrations 016 + 017 after a snapshot, then Gemini on for the staff turn. The owner of each task deploys it (rule of 2026-10-14).
2. **Device check:** whether every photo of an album sent as a reply carries `context.id`. If only the first does, the others form a photo-only segment and get the list.
3. **Live:** scenario 4a-4c on the staff phone, plus a double tap and an Undo. The fixture tests are not a live run.
4. **Customer search:** seeing DH6-109 / 7.8 cr needs 10.1 (8.1 wiring). 9.2 proves it at `v_public_listing`.
5. **Pick-row changes:** they come from the staff text only, so "DHA 6 wala ghar 7.5 kar do" (no price word) gives a list without a change, and the tap then asks for one.
6. **Not undoable:** `media_added` (the cut list). Several changes in a row are undone newest first, with `#undo` each time.
7. **Rate limits:** no limit or ADMIN confirmation for bulk SOLD (R-13: production, not demo).
8. **Wording:** approval of sections 5 and 8.2 section 8; Q1 (ST-01 duplicate line on the 4a card) is still open.
9. **Edit window:** for 10 minutes after an Edit tap, the next plain text is the correction unless it reads as a new listing description (type word + amount or size, the 7.4 rule). A description then opens its own segment, so a correction such as "house 500 gaz" must be sent as a reply to the card. A text that states no field gets `staff_edit_unclear`, and the Edit stays open.
