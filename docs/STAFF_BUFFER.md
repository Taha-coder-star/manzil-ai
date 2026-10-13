# Staff buffer (Task 7.4)

Status (2026-10-14): on `feature/task-7-4-staff-buffer` (from `origin/main` 5472481), PR for
review. Deployed pre-review (owner decision) as `manzil-ai:7.4-288b8e7` with migration 015
(snapshot `manzil-pre-015-2026-10-13`). Live checks 2026-10-13 with `AI_PROVIDER=stub`: text + 3
photos -> one segment reply; two listings -> two segments; photos only -> photo-only question;
listing + "done" -> reply at once; staff "hi" -> immediate stub. One finding, fixed after the
deploy (not yet live): a photo album also delivers a `type=unsupported` message, which got its
own reply; the staff route now ignores `unsupported` and `reaction` messages.

Sources: Execution Plan 7.4 (done condition: text + 3 photos -> 1 segment with 3 photos;
two listings back-to-back -> 2 segments; a photo-only segment is flagged), Red-Team R-08,
R-09, R-10, R3 staff pipeline step 3 ("groups text + photos from the same sender, 60-90 s of
quiet, or 'done'"), RFC C14 ("staff photo-buffer flush" timed job), the D2 handoff for 7.4
and 8.1. Code: `app/staff_buffer.py`, hooks in `app/orchestrator.py` and `app/handlers.py`,
migration `015_staff_segment.sql`, tests `tests/test_staff_buffer.py`.

## Flow

1. A staff (admin / agent) message arrives as usual: webhook -> `process_message` -> one
   coalesced turn (`app.orchestrator`). The `#demo` samples and card taps work as before.
2. `staff_buffer.absorb` classifies each message of the turn, in WhatsApp order, and puts
   descriptions, photos and notes into the staff member's open segment. Only commands,
   taps, other message types and a note with no open segment are answered at once, as a
   normal staff turn. A turn whose messages were all buffered is closed with no reply.
3. Every new item bumps the segment `version` and queues `staff_segment_flush` 75 s later
   (`QUIET_SECONDS`, the middle of R-09's 60-90 s) with that version. An older timer finds
   a newer version and does nothing.
4. A segment closes on its timer (`timer`), on "done" / "bas" / "بس" (`done`), or when the
   next description arrives (`next_description`). Closing freezes its items, sets `kind`
   (`description`, or `photo_only` + flag) and queues `staff_segment_answer`.
5. `staff_segment_answer` waits for photos still downloading (`app.media`; at most 2 x 15 s),
   then flags `media_pending` / `media_failed`, and runs **one staff turn** whose messages
   are the segment's items (`conversation_turn.staff_segment_id`). The reply is sent like
   any turn (`turn:{id}:{seq}` keys, send-time gate):
   - photo-only: `staff_photo_only` ("Ye photos kis listing ki hain? ..."), no provider call;
   - otherwise the AI engine's staff route (L2, `AI_PROVIDER=gemini`) or the stub
     "Listing segment (stub) · N text(s), M photo(s) · drafts come with Task 8.2";
   - plus `staff_media_missing` when a photo is missing or failed (never silently dropped).

All jobs use the staff member's WhatsApp ID as `conversation_key`, so they are serialised
with that staff member's `process_message` jobs by the queue's conversation lock.

## Classification (`classify_text`, pure)

| Class | Rule | Examples |
|---|---|---|
| done | the whole message, punctuation removed, is done / bas / بس | "Bas!", "DONE." |
| command | starts with `#`; contains a listing code (`DH6-102`); or an update phrase (wala / wale / wali, kar do, ho gaya, sold, rented, reserved, withdrawn, on hold, undo) unless it is also a description with "new / naya / nayi" | "#DH6-102 price 7.8 crore", "DHA 6 wala ghar sold" |
| description | a property type word (`requirements.parse_types`: ghar, flat, villa, office, ...) **and** an amount or a size (`normalise.find_money` / `find_areas`) | "New house DHA Phase 6, 500 yard, ... 8 crore" |
| note | any other text | "photos neeche hain", "ok" |
| ignored | message type `unsupported` (WhatsApp sends one next to a multi-photo album, seen live) or `reaction` | no reply, not in a segment |

A photo is an image staged by `app.media` (an `inbound_media` row); its caption is
classified the same way (a property-like caption makes it description + photo, any other
caption is a note). Staff voice notes and other types are answered as normal turns (staff
voice is out of scope). Places are not used: the gazetteer is only loaded with
`AI_PROVIDER=gemini`. All 20 staff benchmark items are classified correctly (ST-01..09,
ST-19, ST-20 description; ST-10..18 command; tested).

A description sent with no type word or no amount ("ghar DHA 6 mein") is a note: it joins
an open segment, or is answered as a staff turn. Known limit, accepted for the demo.

## Segment contract for 8.2 / 9.2

- `staff_segment`: `status` open / flushed, `kind` description / photo_only, `version`,
  `last_item_at`, `flush_due_at`, `flushed_at`, `flush_reason`, `flags[]`. One open
  segment per staff member.
- `staff_segment_item`: `message_id` (unique: a message belongs to at most one segment),
  `position`, `text_role` description / note / NULL, `is_photo`. Photos are found through
  `inbound_media.message_id` (stored keys, status).
- 8.2's `draft_listing` reads a flushed segment (description texts in order, photo
  `inbound_media` ids in order); with several drafts it must not auto-assign photos (R-08).
- 9.2's attach flow (R-10) reads `photo_only` segments: the list of the staff member's last
  5 drafts / listings, or reply-to-card.

## Replay and crash safety

- A redelivered message is one `message` row (WAMID dedupe) and one item (unique
  `message_id`); `absorb` skips messages already in a segment, so a resumed turn changes
  nothing twice.
- Stale flush jobs (older version, or a closed segment) are no-ops. "done" twice closes once.
- The answer turn is unique per segment. A crash leaves it open; the retry (or the staff
  member's next `process_message` job) resumes it and sends under the same keys.
- State is in the database: a restarted worker continues the open segment.

## Behaviour change

Staff no longer get one stub reply per description or photo: they get one reply per
segment, up to 75 s after the last item (or at once after "done" / the next listing).
Commands and stray notes are still answered immediately.

## Open

- Template wording (`staff_photo_only`, `staff_media_missing`) awaits owner approval.
- 8.2 replaces the stub reply with draft extraction; 9.2 the photo-only list.
- Segment and item rows are kept (no retention job yet; staff_session clean-up is 14.x).
