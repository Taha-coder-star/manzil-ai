# Fresh-search release runbook (prepared, not run)

Status: prepared locally on `release/fresh-search`; **no production step has been run**. Owner-run,
after the independent diff review and the PR merge. Nothing here is pushed or deployed.

| Item | Value |
|---|---|
| Release candidate | `69820fe` on `release/fresh-search` = `6368cdd` (fresh-search work) + `8310b2e` (cherry-pick of hotfix `608f4d6`) + a one-line test fix |
| Release image | `manzil-ai:fresh-<M7>`, `M` = the merge commit that is finally released (its tree must equal the candidate's for the 15 files below) |
| Layered on | the image **currently running** (step 3 identifies it); not rebuilt from a checkout |
| Migrations | none (RDS keeps 001-017). New customer-state keys (`kept_at`) are extra JSON in `requirement_state.slots` |
| Caddy | unchanged (`caddy:2`, not recreated) |
| Snapshot | none needed (no schema change, rollback is code-only). Skipped to save cost; the latest automated backup stays |

## 0. Decisions and facts the owner must see first

1. **The hotfix must travel with this release.** Production runs `91b7cef` + hotfix `608f4d6`
   (the voice update-only loop) + the hand-patched greeting line. `608f4d6` is on
   `hotfix/switch-91b7cef-voice-update-loop` and is **not** in `main` or in `6368cdd`. Releasing
   `6368cdd` alone would silently remove it. The candidate carries it (clean cherry-pick; its test
   needed one wording update, committed as `69820fe`).
2. **The release is bigger than the fresh-search work.** Production is at `91b7cef`; `main` has
   since merged PR #45 (WhatsApp layout, uncropped card images), #46 (size on type switch) and #47
   (Devanagari words, stale-purpose re-inference, "cheaper" exclusion). They are in the candidate
   because the fresh-search commit sits on top of them. Per-file: 15 runtime files change, listed in
   section 1. The alternative (a narrow release of only the fresh-search work on `91b7cef`) needs a
   new branch and conflict resolution in `render.py`, `requirements.py`, `normalise.py`,
   `customer_tools.py`: not prepared; say so if you prefer it.
3. **Root cause correction.** The 2026-10-18 failure ("aath crore tak" kept rent) happened on the
   `91b7cef` build. PR #47's re-inference fix (`7e899f6`) was not deployed then. The fresh-search work
   adds the session / question layer on top; both reach production with this release.
4. **Still pending before go:** independent diff review; real WhatsApp verification (section 9);
   merge of the PR(s). Demo Scope scenario 2 wording is approved (2026-10-18).
5. **Not part of this release:** `python -m app.card_regen` (rebuilds existing card images from PR #45;
   writes S3 and RDS rows). Decide separately; until it runs, old cards keep the cropped layout.

## 1. Runtime change set (re-check at release time)

`git diff --name-only 91b7cef <M> -- app migrations requirements.txt Dockerfile demo deploy .dockerignore`
must print exactly these 15 files (rehearsed on `69820fe`: **GATE OK**):

```
app/card_regen.py (new)    app/cards.py            app/customer_intent.py (new)
app/customer_tools.py      app/demo_media.py       app/handoff.py
app/handoff_actions.py     app/handoff_alert.py    app/media.py
app/normalise.py           app/render.py           app/requirements.py
app/search.py              app/staff_cards.py      app/turn_engine.py
```

No dependency, `Dockerfile`, migration, `demo/`, `deploy/` or compose change; Pillow is already in the
image. Anything else in that diff stops the runbook (plan a full rebuild).

Tests on the candidate (disposable PostgreSQL 18.6, `REQUIRE_DATABASE_TESTS=1`, four batches run one
at a time): 703 + 1,337 + 472 + 1,089 = **3,601 passed, 0 failed, 0 skipped** (= collect-only 3,601).
Ruff clean. Re-run on `M` if `M`'s tree differs from the candidate.

## 2. Release kit (local, from the exact commit; never the E:\manzilai checkout)

The main checkout is CRLF (`core.autocrlf=true`); archive committed bytes with autocrlf off.

```
M=<merge commit>; S=${M:0:7}; K=release-kit; mkdir -p $K
CH="app/card_regen.py app/cards.py app/customer_intent.py app/customer_tools.py app/demo_media.py \
 app/handoff.py app/handoff_actions.py app/handoff_alert.py app/media.py app/normalise.py \
 app/render.py app/requirements.py app/search.py app/staff_cards.py app/turn_engine.py"
git -c core.autocrlf=false archive --format=tar --prefix=release-fresh/ -o $K/manzil-fresh-$S.tar $M $CH
for f in $CH; do printf '%s  %s\n' "$(git -c core.autocrlf=false show $M:$f | sha256sum | cut -d' ' -f1)" $f; done > $K/SHA256SUMS
# every other app module: must already match in production (baseline gate)
for f in $(git ls-tree -r --name-only $M app/ | grep '\.py$'); do case " $CH " in *" $f "*) ;; \
  *) printf '%s  %s\n' "$(git -c core.autocrlf=false show $M:$f | sha256sum | cut -d' ' -f1)" $f;; esac; done > $K/SHA256SUMS.unchanged
printf 'FROM manzil-ai:<RUNNING_TAG>\nCOPY release-fresh/app/ /app/app/\n' > $K/Dockerfile.release
( cd $K && sha256sum manzil-fresh-$S.tar > manzil-fresh-$S.tar.sha256 )
tar -xOf $K/manzil-fresh-$S.tar release-fresh/app/requirements.py | sha256sum   # = its SHA256SUMS line
```

Rehearsed on `69820fe` (kit in `E:\manzilai-fresh-review\kit-rehearsal`): 15 + 46 checksum lines. File hashes
depend only on content, so they hold for `M` when the 15 files are unchanged. Also copy
`stored_requirements.py` (read-only state check from the earlier review) for step 9.

## 3. Baseline gate on EC2 (read-only; stop on any mismatch)

```
cd /home/ubuntu/manzil-ai
docker compose -f deploy/compose.yml --env-file .env ps
for c in deploy-web-1 deploy-worker-1; do docker inspect $c --format '{{.Name}} {{.Config.Image}} {{.Image}}'; done
docker image ls manzil-ai --format '{{.Tag}} {{.ID}}' | head -20      # note the running tag and ID
```

Web and worker must be on one image. Record its tag as `<RUNNING_TAG>` and ID. (The 2026-10-17 hotfix
runbook used base `manzil-ai:switch-91b7cef`, ID `sha256:60f94ec1...876bfda`; the hotfix and the
greeting patch were layered after it: do not assume, read it.)

Check the 46 modules this release does **not** change, and see which state the 15 are in:

```
docker run --rm --entrypoint sh -v "$PWD/SHA256SUMS.unchanged:/tmp/u:ro" <RUNNING_TAG> \
  -c 'cd /app && sha256sum -c /tmp/u'                     # every line OK, else STOP
docker run --rm --entrypoint sh <RUNNING_TAG> -c 'cd /app && sha256sum app/turn_engine.py app/requirements.py app/customer_tools.py'
```

Allowed states of the 15 files (they are all replaced): all at `91b7cef`, or `turn_engine.py` at one of

| turn_engine.py sha256 | state |
|---|---|
| `d56191a8d449bc5837758936e6dce5689043ad2225e604430d902b0b64fe2617` | `91b7cef` |
| `8eb0b6256972b7e2d3f9987665e0a9c84d563439a7d0f84fb430c490bfb43777` | + hotfix `608f4d6` |
| `7ab72a7b0916b33cb0f049dd95dc6718833ac3f2f7c12e274f5d7764f54e1d00` | + hotfix + greeting line (clean) |

A hand-patched file (nano / sed on 2026-10-18) will have another hash: then
`docker run --rm --entrypoint cat <RUNNING_TAG> /app/app/turn_engine.py | diff - <(git show 608f4d6:app/turn_engine.py)`
must show only the greeting-rule lines (`A greeting or thanks alone ...`). Any other difference, or any
other file of the 15 not at `91b7cef`, STOPS the release and goes back to the owner.

## 4. Transfer (CloudShell -> private S3 staging -> EC2)

1. CloudShell upload: `manzil-fresh-$S.tar`, `.tar.sha256`, `SHA256SUMS`, `SHA256SUMS.unchanged`, `Dockerfile.release`, `stored_requirements.py`.
2. CloudShell: `aws s3 cp <file> s3://<bucket>/staging/release-fresh/` for each (private prefix only; never `tenant/` or `card/`).
3. EC2, in `/home/ubuntu/release-fresh/`: `aws s3 cp` each down; `sha256sum -c manzil-fresh-$S.tar.sha256`; `tar -xf`; `( cd ... && sha256sum -c SHA256SUMS )` from the extracted dir (15 OK).
4. Delete the `staging/release-fresh/` objects once the image is built.

## 5. Build and image checks (no service touched yet)

```
docker build -f Dockerfile.release -t manzil-ai:fresh-$S .
docker run --rm --entrypoint sh -v "$PWD/SHA256SUMS:/tmp/S:ro" manzil-ai:fresh-$S -c 'cd /app && sha256sum -c /tmp/S'
for i in <RUNNING_TAG> manzil-ai:fresh-$S; do docker run --rm --entrypoint sh $i -c 'cd /app && find app migrations demo -type f -not -name "*.pyc" | sort | xargs sha256sum' > "files-$(echo $i | tr ':/' '__').txt"; done
diff files-<running>.txt files-manzil-ai_fresh-$S.txt      # only the 15 app/ lines (2 new files, 13 changed)
docker run --rm --entrypoint python manzil-ai:fresh-$S -c "import app.web, app.worker, app.customer_intent, app.card_regen; print('ok')"
docker run --rm --entrypoint id manzil-ai:fresh-$S         # appuser, not root
```

## 6. Migration state (read-only; nothing to apply)

Same check as docs/RELEASE_10_2.md section 6 using `migrate.env`: expect `17 17 []`. Do not run the migrator.

## 7. Switch (web and worker only; Caddy untouched)

```
cd /home/ubuntu/manzil-ai
docker tag manzil-ai:latest manzil-ai:pre-fresh                 # rollback tag (must be <RUNNING_TAG>'s image)
docker tag manzil-ai:fresh-$S manzil-ai:latest
docker compose -f deploy/compose.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker
docker compose -f deploy/compose.yml --env-file .env ps       # web, worker recreated; caddy unchanged
```

## 8. Health checks

- `curl -fsS https://<SITE_ADDRESS>/health` OK; `curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<SITE_ADDRESS>/webhook -d '{}'` = 401 (signature refused: the route is alive).
- `docker compose ... logs --since 5m worker`: `Worker started: ...`, no exit 78, no gazetteer error; `... logs web`: no traceback. Read no phone numbers, text or secrets.

## 9. Acceptance on a real phone (pending; the owner's two test numbers)

**Why two phases:** the inherited-filter check needs a number idle for more than 6 hours. Seed it before
the shutdown block, finish it after the servers come back (the session is computed from stored message
times, so the gap counts while the servers are stopped).

Phase A (now, ~15 min):
1. `salam` -> "Wa alaikum assalam! ..." (not "Shukriya!"); `hi` -> "Hello! ...".
2. `salam, DHA phase 6 mein flat chahiye` -> a normal request answer, no greeting template.
3. Typed commands on a number with fresh filters: `nayi search` clears nothing and lists the filters; `purani requirements hata do` -> "...hata di hain..."; then ask again from scratch.
4. Results / no-match headers state purpose and the monthly period for a rent search ("... kiraye par, 2 lakh tak mahana ..."), and the cards use the new WhatsApp layout (PR #45).
5. Voice: the ~8 s Roman Urdu note scenario from turn `4b1d4b0f` gets a search reply, worker log has `iteration_limit_search` at WARNING (hotfix kept).
6. Seed for phase B on test number X: send `DHA Phase 6 mein 500 gaz ka ghar kiraye par chahiye`; leave it idle.

Phase B (after more than 6 h idle, services back up):
7. `mera area hy dha phase 6` -> "Theek hai, note kar liya: DHA Phase 6." + "Pichli search mein 500 gaz ghar, kiraye par tha - kya ab bhi wohi chahiye?" with [Haan, wohi] [Nayi search]; no listings.
8. `aur budget hy 2 crore` -> "... 2 crore ke aas paas." + the short reminder; read-only check shows the budget has `period` null (`stored_requirements.py`, phone masked).
9. Tap [Nayi search] -> inferred-purchase note + a no-match / results line with no "500 gaz", no "mahana". On a second idle test: tap [Haan, wohi] -> rent kept, "mahana" in the header. Try a typed `haan` and `nahi`, and `nahi, kharidna hai` (purpose set, question about the rest).
10. Tap an old [Haan] after answering -> "Ye sawal ab purana ho gaya hai", nothing changes.

Stop and roll back (section 10) on: a traceback, a reply that restores house / rent, a monthly label on an
unconfirmed budget, the old greeting text, or a card that does not render.

## 10. Rollback (code only; no schema or data to undo)

```
docker tag manzil-ai:pre-fresh manzil-ai:latest
docker compose -f deploy/compose.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker
```

then section 8. The old code ignores the extra `kept_at` key and does not know the `keep:` buttons: a tap on an already-sent question gets the old stub reply and changes no data.
Keep `manzil-ai:pre-fresh`, `<RUNNING_TAG>`, the kit and the `files-*.txt` lists until
acceptance is complete.

## 11. Known limits and follow-ups

- No staging environment: verification is on production with the owner's own numbers.
- The "question already sent" check scans one conversation's outbound rows of the last 7 days
  (2.5 ms at 5,003 rows); the session query is 4.4 ms at 10,005 messages. Migration 018 (index on
  `outbound_message(agency_id, conversation_id, created_at)`) is deferred until real history is larger.
- Existing card images are not rebuilt by this release (`card_regen`, section 0.5).
- Slot times are now the customer's message time; older slots keep their processing times (differences of seconds).

## 12. After acceptance: cost block (owner runs; AWS budget is $10)

Fill `REGION`, `EC2_ID`, `RDS_ID` from `aws ec2 describe-instances` / `aws rds describe-db-instances`
(do not guess). Delete only snapshots made for this release, by name, with the owner's yes (none are
planned); never the latest automated backup.

**Shut down:**

```
REGION=<REGION>; EC2_ID=<i-...>; RDS_ID=<db-identifier>
aws s3 rm s3://<bucket>/staging/release-fresh/ --recursive            # staging only
aws rds describe-db-snapshots --db-instance-identifier $RDS_ID --region $REGION --query 'DBSnapshots[].[DBSnapshotIdentifier,SnapshotCreateTime,SnapshotType]' --output table
# aws rds delete-db-snapshot --db-snapshot-identifier <NAMED_RELEASE_SNAPSHOT> --region $REGION    # only if one exists
aws ec2 stop-instances --instance-ids $EC2_ID --region $REGION
aws rds stop-db-instance --db-instance-identifier $RDS_ID --region $REGION
```

**Bring production back:**

```
aws rds start-db-instance --db-instance-identifier $RDS_ID --region $REGION
aws rds wait db-instance-available --db-instance-identifier $RDS_ID --region $REGION
aws ec2 start-instances --instance-ids $EC2_ID --region $REGION
aws ec2 wait instance-running --instance-ids $EC2_ID --region $REGION
aws ec2 describe-instances --instance-ids $EC2_ID --region $REGION --query 'Reservations[].Instances[].PublicIpAddress'
# then on the instance:
cd /home/ubuntu/manzil-ai && docker compose -f deploy/compose.yml --env-file .env up -d web worker caddy
docker compose -f deploy/compose.yml --env-file .env ps
curl -fsS https://<SITE_ADDRESS>/health
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<SITE_ADDRESS>/webhook -d '{}'      # 401 = alive
```

Cautions: a stopped RDS instance **restarts by itself after 7 days**; a stopped EC2 instance gets a new
public IP unless an Elastic IP is attached (the Meta webhook URL / DNS depends on it: re-check
`<SITE_ADDRESS>` resolves to the new IP before testing); confirm the $10 budget alert exists in the AWS console.
