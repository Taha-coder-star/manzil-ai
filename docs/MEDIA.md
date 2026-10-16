# Media (Tasks 4.1 inbound and 4.2 outbound, 5.3 inbound audio; component C5)

Sources: RFC section 11 (media storage boundary), R4/R9 D14 (media storage and re-send), D11 1.3
(property_media), Red-Team R-36 (originals down-scaled on ingest). Migration 008, `app/media.py`,
`app/whatsapp.py` (`media_info` / `download_media` / `fetch_media`).

## Flow

1. `record_inbound` (app/handlers.py) logs the message. When it is an `image`, or a `document` with an
   image MIME type, **and** the sender is active `admin` / `agent` staff (the role comes from the staff
   allowlist, never from content), it inserts an `inbound_media` row (unique per message) and enqueues a
   `fetch_media` job (dedupe `media:<inbound_media_id>`), in the same transaction. Photos from customers and
   brokers are logged as messages only; they are not downloaded.
2. The `fetch_media` job runs at once (no conversation key). Each attempt asks Graph
   `GET /<version>/<media_id>?phone_number_id=...` for a fresh 5-minute URL, then downloads it with the
   access token. An expired URL (401/403/404/410) is re-queried once within the attempt; later attempts
   query again, since the media ID lives for days. The token is only sent to Meta hosts
   (`*.fbsbx.com`, `*.facebook.com`, `*.whatsapp.net`, `*.fbcdn.net`, https only). Meta's `sha256` is
   checked when present. Cap: 16 MB.
3. Validation: the magic bytes must be JPEG / PNG / WebP, match Meta's MIME type and Pillow's format;
   width x height <= 50 MP is checked from the header before decoding (decompression bombs).
4. Hash dedupe: the sha256 of the delivered bytes. If the agency already has a `stored` row with that
   hash, the new row becomes `duplicate`, points at it (`duplicate_of_id`) and copies its keys: nothing is
   uploaded. A partial unique index keeps one `stored` row per (agency, hash); a concurrent loser becomes
   `duplicate` and deletes its own objects.
5. Pillow builds three images with orientation applied and **no EXIF / GPS / XMP** (Pillow writes no
   metadata it is not given; each output is re-opened and checked, and a failure rejects the photo):
   - `original`: long edge <= 2,560 px (R-36); JPEG q92, or PNG for PNG input;
   - `wa`: JPEG, 8-bit RGB, long edge <= 1,600 px, < 5 MB (quality, then size, is lowered until it fits);
   - `card`: JPEG 1,200 x 628 (1.91:1). Since 2026-10-16 the whole photo is scaled to fit (contain) and
     centred on a neutral `CARD_BACKGROUND`, never cropped or stretched (`media.card_image`,
     `card_placement`); before, it was centre-cropped and portrait photos lost most of their height.
     Cards made before that are rebuilt once by `python -m app.card_regen --agency-id <uuid> [--apply]`
     under new keys `card/<agency_id>/v2/<uuid>.jpg` (card objects are immutable, so never
     overwritten; the old objects stay for a rollback). Task 5.2 (app/cards.py) uses a listing cover's
     `public_url_card` as the carousel card header and as the image header of the single and
     individual cards (link only; Meta documents no media-ID form for carousel headers).
6. Upload, then the row becomes `stored` with keys, size, dimensions, `public_url_card`, `exif_stripped`.

Outcomes: `stored` / `duplicate` (usable, CHECK-enforced keys + `exif_stripped`); `rejected` (not an image,
too large, gone at Meta: the job succeeds, no retry); `failed` (transient errors through the last of 3
attempts: the job is dead). Telling staff to resend a lost photo belongs to the staff flow (D14,
Tasks 7.x / 9.x), which reads these statuses.

## Bucket layout

| Object | Key | Access |
|---|---|---|
| original | `tenant/<agency_id>/inbound/<inbound_media_id>/original.jpg` (or `.png`) | private |
| WhatsApp image | `tenant/<agency_id>/inbound/<inbound_media_id>/wa.jpg` | private (Task 4.2 uploads it to Meta) |
| card | `card/<agency_id>/<card_token>.jpg` | **public read**, `Cache-Control: public, max-age=31536000, immutable` |

The RFC sketch is `tenant/<agency_id>/property/<property_id>/<media_id>/...`. Staff photos arrive before
the listing exists (the draft is saved later, Task 9.2), so objects are keyed by the inbound media ID and
`property_media` will reference them by key; nothing is moved. Public cards sit under their own `card/`
prefix so one bucket-policy statement covers exactly the public objects, and `card_token` is a random
UUID, so card URLs are unguessable and reveal no internal ID. Keys are stable, so a retried job overwrites
the same objects.

Public URL: `MEDIA_PUBLIC_BASE_URL` when set (CDN or non-AWS storage), otherwise
`https://<S3_BUCKET>.s3.<S3_REGION>.amazonaws.com`. The `fetch_media` handler is only registered when the
bucket and a public base are configured (and `META_ACCESS_TOKEN`); otherwise its jobs stay pending.

## AWS change needed for the done condition (owner approval required, not applied)

The bucket is private. For "card URL opens publicly":

1. Bucket `manzil-demo-media-284460774230` > Permissions > Block public access: untick only
   **"Block public access to buckets and objects granted through new public bucket or access point
   policies"** and **"... through any public bucket or access point policies"**. Keep both ACL options
   ticked. (If the account-level setting blocks public policies, the same two must be relaxed there.)
2. Bucket policy (read-only, `card/` only):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Sid": "PublicCardImagesOnly",
       "Effect": "Allow",
       "Principal": "*",
       "Action": "s3:GetObject",
       "Resource": "arn:aws:s3:::manzil-demo-media-284460774230/card/*"
     }]
   }
   ```

   No `s3:ListBucket`, so there is no directory listing.
3. Role `manzil-ec2-role` needs `s3:PutObject` and `s3:DeleteObject` on
   `arn:aws:s3:::manzil-demo-media-284460774230/tenant/*` and `.../card/*` (check what it has today).

Check: `curl -sI https://manzil-demo-media-284460774230.s3.us-east-1.amazonaws.com/card/<agency>/<token>.jpg`
returns 200, and the same request for a `tenant/...` key returns 403.

## Outbound images (Task 4.2, migration 009)

`media.send_image(conn, client, storage, agency, conversation, recipient, (table, id), dedupe_key)` sends one
stored image. The source is a `property_media` row (listing photos, D9 "More photos") or a stored / duplicate
`inbound_media` row (staff photos, before they are attached). A duplicate resolves to its stored copy, which
owns the objects and the cache.

1. **Cache** (D14 point 4): the `wa` derivative is uploaded once (`POST /<phone_number_id>/media`) and its ID is
   cached on the source row with `expires = upload + 30 days`. It is reused while more than 5 days remain, so it
   is re-uploaded lazily after ~25 days. Columns: `property_media.wa_media_id / wa_media_expires_at` (008, D11),
   `inbound_media.wa_upload_id / wa_upload_expires_at` (009; `inbound_media.wa_media_id` is the inbound ID).
   IDs are scoped to the agency's one business number (D3).
2. **Send** through `app.outbound.send_message` (dedupe, send-time gate R-28, 24 h window): no parallel sender.
   The outbound row records `media_source` / `media_source_id` (009).
3. **Fallback to the link** (presigned GET of the private `wa.jpg`, 1 hour), sent with dedupe key
   `<key>:link`:
   - the upload fails, or Meta refuses the ID at send time (any `PermanentSendError`): the cached ID is
     cleared, so the next send uploads afresh;
   - Meta accepts the message but its status webhook says `failed`: `reconcile_status` clears the ID and
     enqueues one `media_link_fallback` job, which resends by link. A failed link send is not retried.
   - Not when the send was cancelled by the control gate or is outside the 24 h window.
4. Images are sent one per call; callers send the next only after this returns (D14 point 5).

Echo-bot check (temporary, staff only): `#demo photo` sends the sender's newest stored photo back (uploaded the
first time, the cached ID after that); `#demo photo badid` first caches an ID Meta refuses, so the link
fallback runs. Customers get "Photo demo is for staff numbers only."

## Not in 4.1 / 4.2
- Attaching staged photos to a listing (`property_media` rows, `source_inbound_media_id`): Task 9.2.
- Customer voice audio: storage moved to Task 5.3 (below); speech itself (pre-checks, STT) is C4, Task 6.3
  (docs/SPEECH.md).
- Lifecycle rules, per-listing photo cap, storage metering (R-36): production.

## Inbound audio (Task 5.3, migration 012)

Sources: RFC voice data flow step 2 ("download within 5 min... store the audio under restricted retention"),
Execution Plan gate M1(c), R16 section 4 (benchmark voice notes "pulled through the real webhook / media
path"). `app/audio.py`.

1. `record_inbound` stages **every** inbound `audio` message, whoever sent it (customer, staff, broker):
   one `inbound_audio` row per message (Meta's `voice` flag kept in `is_voice`) and a `fetch_audio` job
   (dedupe `audio:<inbound_audio_id>`), in the same transaction as the message.
2. `fetch_audio` downloads through `WhatsAppClient.fetch_media` (same URL re-query, Meta-host allowlist,
   `sha256` check, 16 MB cap as images). Retries and dead-lettering follow `fetch_media`: a permanent
   Graph error or a file that is not audio -> `rejected`; exhausted retries -> `failed`.
3. The type comes from the magic bytes (Ogg, MP3, MP4/M4A, ADTS AAC, AMR); the declared MIME is not
   trusted (clients vary). Ogg files get `duration_ms` from the last granule position minus Opus pre-skip.
4. The bytes are stored **unchanged** at `tenant/<agency_id>/audio/<inbound_audio_id>.<ext>`: private
   (only `card/` is public), no cache header. `content_sha256` and `byte_size` are recorded. No hash
   dedupe: each voice note is its own record.

Not here: the < 1 s / > 120 s pre-checks, silence detection and STT (C4, Task 6.3, docs/SPEECH.md, which
reads the stored object or downloads in-turn through `audio.ensure_stored`); the retention purge (C14). Only `fetch_audio` needs the bucket; it is
registered even without a public media URL.

Production (2026-10-11, owner-run): RDS snapshot `manzil-pre-012-2026-10-11`; release image
`manzil-ai:5.3-deb94ba` = `manzil-ai:5.2-8f54bae` + `app/audio.py`, `app/bench_manifest.py`,
`app/handlers.py` and migration 012 (file hashes checked inside the image; 001-011 byte-identical to the
running image; runs as appuser), tagged `manzil-ai:latest` (rollback `manzil-ai:pre-5.3`). Migration 012:
`1 applied, 11 skipped`, repeat `0 applied, 12 skipped`. Live check (gate M1(c)): a team-phone voice note
got the stub reply and was stored (`audio/ogg`, `is_voice` true, 6.48 s, 14,698 bytes) under
`tenant/<agency>/audio/<id>.ogg`; an anonymous GET of that object returned 403. `app.bench_manifest`
listed it with no phone number. Note: `--since` is compared with WhatsApp time in UTC (Karachi is UTC+5).
