# Demo media production runbook (Task 4.4)

Owner-run, after review and approval. Nothing here has been executed. Placeholders: `<AGENCY_ID>` (the demo
agency, recorded in the ignored local notes), `<EC2_HOST>`, `<KEY.pem>`, `<COMMIT>`, `<ZIP_SHA256>`. Never paste
credentials, phone numbers or connection strings into a terminal transcript, a report or Git.

Order: pilot (DH6-101, PRJ-103, BTK-106) end to end, review, then the full set (all 30 listings) with the same
steps and no `--listings`.

## 0. Preconditions

- Every asset in scope is `approved` in `demo/media/intake.csv`, committed and merged; on Windows:
  `python -m app.demo_media validate --listings DH6-101,PRJ-103,BTK-106 --assets-dir E:\manzil-demo-media\work`
  (full set: no `--listings`) prints `valid`.
- Production is still on the Task 4.3 state (30 listings, 27 public). Do **not** rerun the data seed.
- The web / worker containers are not replaced or restarted for this task.

## 1. Asset bundle (Windows)

```
python -m app.demo_media bundle --assets-dir E:\manzil-demo-media\work ^
  --out E:\manzil-demo-media\task44-pilot.zip --listings DH6-101,PRJ-103,BTK-106
```

The ZIP holds `prepared/<asset>.jpg` (12 for the pilot, 120 for the full set), `SHA256SUMS`, and copies of
`manifest/scenes.csv` and `manifest/intake.csv`. Record the printed ZIP sha256 in the release notes. The loader
re-checks every file against the committed `prepared_sha256`, so a wrong or altered file is refused.

## 2. Utility image (code only; no assets inside)

Verify the current production base first, on EC2:

```
sudo docker image inspect manzil-ai:latest --format '{{.Id}} {{.Created}}'
sudo docker image inspect manzil-ai:4.3-cd8b738 --format '{{.Id}}'
sudo docker ps --format '{{.Names}} {{.Image}}'
```

Expected from the Task 4.3 record: web / worker on `manzil-ai:latest` = `manzil-ai:4.2-f65094d`
(`sha256:e9d2f46e...`), seed utility `manzil-ai:4.3-cd8b738` = that base + `app/demo_dataset.py`,
`app/seed.py`, `demo/`. If anything differs, stop and review.

Build context on Windows from the merged commit, **never from the main checkout's working tree** (its
migrations 001-004 are CRLF):

```
git -c core.autocrlf=false archive --format=tar -o E:\manzil-demo-media\task44-code-<COMMIT>.tar <COMMIT> ^
  app/demo_media.py app/media_seed.py app/media_send_check.py demo/media
```

`Dockerfile.task44` on EC2 (layered; migrations and running services untouched):

```
FROM manzil-ai:4.3-cd8b738
COPY app/demo_media.py app/media_seed.py app/media_send_check.py /app/app/
COPY demo/media /app/demo/media
```

```
mkdir -p ~/task44/build && tar -xf ~/task44/task44-code-<COMMIT>.tar -C ~/task44/build
cp ~/task44/Dockerfile.task44 ~/task44/build/Dockerfile
sudo docker build -t manzil-ai:4.4-media-<COMMIT> ~/task44/build
sudo docker run --rm --entrypoint sh manzil-ai:4.4-media-<COMMIT> -c \
  'sha256sum /app/migrations/*.sql && python -m app.demo_media validate --plan-only && id'
```

The migration checksums must equal those of `manzil-ai:4.3-cd8b738` (the layer adds no SQL). The new modules
only use `app/media.py`, `app/seed.py` and `app/demo_dataset.py` as they are in that image.

## 3. Transfer (G-2)

Primary — SCP / WinSCP (route not yet confirmed: needs the instance key and SSH access from your IP):

```
scp -i <KEY.pem> E:\manzil-demo-media\task44-pilot.zip E:\manzil-demo-media\task44-code-<COMMIT>.tar ^
  ubuntu@<EC2_HOST>:/home/ubuntu/task44/
```

On EC2:

```
cd ~/task44 && sha256sum task44-pilot.zip          # must equal <ZIP_SHA256>
mkdir -p assets journal && unzip -q task44-pilot.zip -d assets
(cd assets && sha256sum -c SHA256SUMS)              # every line OK
```

Alternative — private S3 staging, **only after** verifying the bucket policy (only `card/*` public), the
instance role's Get/Delete on the chosen private key, and your own upload method (e.g. CloudShell upload +
`aws s3 cp`). No new prefix, IAM or bucket-policy change is assumed; if the existing policy does not allow it,
do not use this route. Delete the staged object after the load. No Base64 terminal transfer of image sets.

## 4. Environment (names only; values stay in files)

The loader needs `DATABASE_URL` (the restricted runtime login), `S3_BUCKET`, `S3_REGION`, optional
`S3_ENDPOINT_URL` and `MEDIA_PUBLIC_BASE_URL`; the optional send check also needs `META_ACCESS_TOKEN`
(`META_API_VERSION` optional) and `MEDIA_CHECK_RECIPIENT`. The Task 4.3 seed env file does **not** contain the
bucket or Meta settings. Use the runtime file read-only:

```
ENVFILE=/home/ubuntu/manzil-ai/.env
grep -cE '^(DATABASE_URL|S3_BUCKET|S3_REGION)=' "$ENVFILE"    # prints a count only; expect 3
AGENCY=<AGENCY_ID>
IMG=manzil-ai:4.4-media-<COMMIT>
RUN="sudo docker run --rm --env-file $ENVFILE --user $(id -u):$(id -g) \
  -v $HOME/task44/assets:/assets:ro -v $HOME/task44/journal:/journal --entrypoint python $IMG"
```

## 5. Backup / recovery checkpoint (G-11)

1. RDS snapshot before the first apply (console or CLI by the owner), e.g. `manzil-pre-task44-<date>`; wait for
   `available`.
2. An RDS snapshot does **not** cover S3. Objects written by the loader are listed in the journal
   (`~/task44/journal/task44-media.json`); copy it off the instance after every run and keep it with the release
   records. Uncommitted objects are removed with `cleanup`; there is no bulk removal of committed media (G-6).
3. Record the before-state:

```
$RUN -m app.media_seed fingerprint --agency-id $AGENCY | tee ~/task44/fingerprint-before.json
```

Expected: `"listings": 30`, `"public_listings": 27`, statuses active 27 / rented 1 / reserved 1 / sold 1.

## 6. Dry run, apply, rerun

```
$RUN -m app.media_seed load --agency-id $AGENCY --assets-dir /assets \
  --journal /journal/task44-media.json --listings DH6-101,PRJ-103,BTK-106
```

Expected: `Assets: 0 unchanged, 12 to insert; covers to set: 3; other media preserved: 0`, no CONFLICT lines,
`Outcome: dry run: nothing written`. Any CONFLICT: stop and review (exit 2).

```
$RUN -m app.media_seed load --agency-id $AGENCY --assets-dir /assets \
  --journal /journal/task44-media.json --listings DH6-101,PRJ-103,BTK-106 --apply
```

Expected: `Uploaded 12 asset(s) (3 objects each); inserted 12 row(s); covers set 3`, `Outcome: committed`.
Repeat the dry run: `12 unchanged, 0 to insert; covers to set: 0`.

## 7. Verification

```
$RUN -m app.media_seed verify --agency-id $AGENCY --listings DH6-101,PRJ-103,BTK-106 \
  --objects --assets-dir /assets --urls-out /journal/pilot-urls.tsv
$RUN -m app.media_seed fingerprint --agency-id $AGENCY | tee ~/task44/fingerprint-after.json
diff <(jq -S . ~/task44/fingerprint-before.json) <(jq -S . ~/task44/fingerprint-after.json) && echo same
```

- `verify` prints `Verify: OK` (rows 12, objects 36; full set: 120 / 360 and all 30 listings with >= 4 images,
  cover = order 0).
- This must first establish authenticated existence and exact derivative checksums for each original,
  WhatsApp image and card, using approved prepared files. The stored original is re-encoded by
  `process_image`; its checksum is not the prepared file checksum. Stop on any missing, unreadable or
  mismatched object. Use the same Pillow/JPEG processing environment as the load.
- The fingerprint is identical before and after: counts, statuses, every listing column except cover / version /
  updated_at, and `last_confirmed_at` unchanged.
- Public / private, anonymous requests (curl sends no AWS credentials):

```
cut -f2 ~/task44/journal/pilot-urls.tsv | xargs -n1 curl -s -o /dev/null -w '%{http_code} %{content_type}\n' | sort | uniq -c
cut -f3,4 ~/task44/journal/pilot-urls.tsv | tr '\t' '\n' | xargs -n1 curl -s -o /dev/null -w '%{http_code}\n' | sort | uniq -c
```

  Pass: every card line is `200 image/jpeg` (12 for the pilot). Every private original / WhatsApp URL (24) is
  denied: any 4xx is a pass (S3 normally answers 403 for a private key without list permission; a CDN or proxy may
  answer 404); any 2xx or 3xx is a failure, so stop and review the bucket policy before going further.
  A 4xx only counts after authenticated verification of that exact key succeeds. An anonymous 404 alone
  could mean the object is absent. A 5xx or network error is inconclusive and requires a successful recheck.

- Visual: open each card URL in a browser (or copy the cards back with scp) and confirm DEMO (and CONCEPT for
  PRJ-103 / BTK-106) is readable. Record the result in the release notes.

## 8. Optional WhatsApp delivery check (separately authorised)

1. From the recipient phone, message the business number (opens the 24 h window and the conversation).
2. Preview (sends nothing): `$RUN -m app.media_send_check --agency-id $AGENCY --listing DH6-101`
   with the recipient passed privately:

```
read -rs MEDIA_CHECK_RECIPIENT && export MEDIA_CHECK_RECIPIENT
RUNR="${RUN/--env-file/-e MEDIA_CHECK_RECIPIENT --env-file}"
$RUNR -m app.media_send_check --agency-id $AGENCY --listing DH6-101
```

   Expected: `Recipient: set (not shown); conversation: yes; 24 h window open: yes`.
3. Send once: `$RUNR -m app.media_send_check --agency-id $AGENCY --listing DH6-101 --send --check-id pilot-1`
   -> `Sent: sent via id; ID cached now: yes`. Rerunning the same `--check-id` does not send again. The phone
   shows the image with DEMO.

## 9. Recovery

| Message | Action |
|---|---|
| `CONFLICT ...` (exit 2) | nothing written; review the listing (staff media, cover, changed asset); do not force |
| `upload failed for ...` | rerun the same `--apply` (reuses the journaled keys) or `cleanup` |
| `Outcome: rolled back: the database changed ...` | rerun the dry run to see why; then `--apply` again or `cleanup` |
| `database error (... 55P03 ...)` | a lock is held (another load, or an interrupted session still open); wait and rerun |
| `commit outcome unknown` | rerun the **dry run** first (reconciles under the lock, in memory only); then rerun `--apply` (saves the reconciled journal; reuses uploads). Do not run cleanup before the outcome is known; `cleanup --apply` itself reconciles under the lock first |
| `journal ... is locked by another run` | ensure no load is running (`sudo docker ps`); then remove `task44-media.json.lock` |
| `journal ... was written for another database / storage target` | wrong journal or wrong env file; nothing changed. Use the journal of this target (or a new file for a genuinely new target); never edit the target by hand |
| `CONFLICT ... cover_cleared` | staff cleared a loaded cover; the loader does not reset it. Review with the owner |
| `uploaded ... object missing` / `checksum mismatch` | stop; no new rows committed, incompatible objects not overwritten. Review ownership before cleanup/reload; preserve journal and source assets |

The database binding includes a secret-free connection-target fingerprint. An alias or new endpoint fails
closed even if the database and agency timestamp match. Endpoint reuse/restoration at the same endpoint can
still match; it does not prove physical identity. Old-format target bindings require separate recovery review.
The session advisory lock coordinates load and cleanup across copied journals, spanning uploads and deletes
without listing row locks during I/O. External object deletion/replacement after readback remains possible.

Cleanup of unfinished attempts (dry run, then apply):

```
$RUN -m app.media_seed cleanup --agency-id $AGENCY --journal /journal/task44-media.json
$RUN -m app.media_seed cleanup --agency-id $AGENCY --journal /journal/task44-media.json --apply
```

It deletes only the exact keys of attempts without a row and referenced by no row. To undo a committed load,
stop and prepare a separately reviewed procedure (not part of Task 4.4).

## 10. Full set

After the pilot is accepted: generate, review and approve the remaining 108 images, bundle without `--listings`,
transfer, and repeat sections 5-7 without `--listings` (expected: 108 to insert, 27 covers, 12 unchanged; then
`verify` OK for 120 rows / 360 objects across all 30 listings, hidden CLF-104 / DH6-105 / DH6-106 included).
