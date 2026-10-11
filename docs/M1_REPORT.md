# Gate M1: WhatsApp send / receive (Task 5.4)

Execution Plan section 8, Day 5: M1 passes when all five criteria are true on the live system.
Evidence below holds no phone numbers, tokens, connection strings or message text. Times are UTC.

Tools (read-only, piped into the running web container, no deploy needed):
- `python -m app.m1_status --agency-id <uuid> --since <time>`: recent turns (route, messages
  coalesced, replies), staff photos (keys, EXIF), inbound audio.
- `python -m app.m1_replay --agency-id <uuid>`: rebuilds the newest processed message's webhook body
  from its stored event (same metadata, contact, message and WAMID), signs it with `META_APP_SECRET`
  as Meta does and POSTs it to the server's own `/webhook` twice; compares event, job, message,
  outbound and turn counts before and after. Tested end to end in `tests/test_m1_replay.py`
  (including a control that a genuinely new message is counted, and a mutation check).
  Decision (owner, 2026-10-11): Meta's byte-identical original body is not stored, so the replay is
  the same event re-signed; it carries the same event key and job dedupe key, which deduplication uses.

| # | Criterion | Method | Evidence | Result | Date |
|---|---|---|---|---|---|
| (a) | A text from customer A gets a reply from the worker | Customer and broker phones send a text; worker stopped, 3 quick customer texts, worker started; `m1_status` | Owner-reported on the phones: customer text -> "Customer mode (stub)" reply; broker (Ahmed (DEMO), seeded `DEMO_BROKER_PHONE`) -> "Broker mode (stub)"; worker stopped, 3 quick customer texts, worker started -> one reply "3 messages in this turn". Status output for these turns not attached; the staff turn of (b) shows the worker replied (`replies sent 1`, read) | **PASS** (owner-reported) | 2026-10-11 |
| (b) | A staff photo is stored with derivatives, EXIF removed | Staff phone sends a camera photo; `m1_status`; anonymous GET of card URL and original key | Staff turn replied ("Staff mode (stub)"); `inbound_media` `stored`, `image/jpeg`, `exif_stripped=true`, three objects: `tenant/<agency>/inbound/<id>/original.jpg` and `wa.jpg` (private), `card/<agency>/<token>.jpg` (public); anonymous GET card **200**, original **403** | **PASS** | 2026-10-11 |
| (c) | A voice note's audio is stored | Team phone sends a voice note after the Task 5.3 deploy; `app.bench_manifest`; anonymous GET of the object | `stored`, `audio/ogg`, voice, 6.48 s, 14,698 bytes, key `tenant/<agency>/audio/<id>.ogg`; stub reply received; anonymous GET **403** (image `manzil-ai:5.3-deb94ba`, migration 012) | **PASS** | 2026-10-11 |
| (d) | Replaying a webhook creates no duplicate event or outbound message | `m1_replay` (2 signed replays of a processed message) | Newest processed message = the staff photo of (b) (type image). Replayed twice, correctly signed: HTTP [200, 200]; before / after: inbound_event 1 / 1, job 1 / 1, message 1 / 1, outbound_message (conversation) 27 / 27, conversation_turn 11 / 11; no new reply on the phone | **PASS** | 2026-10-11 |
| (e) | The carousel renders (or the fallback works) on Android, iPhone and Web | `docs/CAROUSEL_RUNBOOK.md` section 4 on each client | Owner-reported from a staff number: every runbook step works (`#demo carousel` renders with images, card taps answer with the right action and listing, `#demo card`, `#demo cards`, fallback flag); device models and per-client notes not recorded. First client also rendered on 2026-10-10 (Task 5.2) | **PASS** (owner-reported) | 2026-10-11 |

Verdict: **M1 PASSES** (2026-10-11, Day 6): (a)-(e) all PASS; (a) and (e) are owner-reported observations on the phones, (b)-(d) have recorded tool output. Production at the time: image `manzil-ai:5.3-deb94ba`, RDS migrations 001-012.
