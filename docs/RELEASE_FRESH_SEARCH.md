# Fresh-search release runbook (prepared, not run)

Status: prepared and reviewed locally on `release/fresh-search`. **No production step has been run;
nothing is pushed or deployed; `card_regen` is out of scope.** Shutdown / cost-saving commands are
deliberately **not** in this file: see `docs/RELEASE_FRESH_SEARCH_COST.md`.

| Item | Value |
|---|---|
| Runtime candidate | `69820fe` on `release/fresh-search` (this runbook is the commit on top) |
| Release image | `manzil-ai:fresh-<M7>`, `M` = the merge commit that is finally released |
| Layered on | the image **currently running**, identified by the baseline gate (section 4) |
| Migrations | none (RDS keeps 001-017) |
| Caddy | unchanged (`caddy:2`, not recreated) |
| Snapshot | none: no schema change, rollback is code-only (the latest automated backup stays) |

## 0. What ships, and the corrected incident explanation

**Expected production build (P)** = `91b7cef` + hotfix `608f4d6` (voice update-only loop) + the greeting line
hand-patched into `turn_engine.py` on 2026-10-18 (`f4cc48d`'s one-line change). P is the commit
`fdd4de3` (tag `review/prod-expected-fdd4de3`), rebuilt locally; **it is not yet verified on the server**:
the baseline gate (section 4) is what verifies it.

Included on top of P (15 runtime files; `git log 91b7cef..release/fresh-search`):

| Commit(s) | PR | Purpose |
|---|---|---|
| `a0df4c9` | #45 | WhatsApp layout (bold codes / prices, spacing), uncropped card images (new cards only) |
| `6df8fa2` | #46 (merge) | size asked again on house <-> office switch (`91b7cef` is already in production) |
| `15dd521` `7e899f6` `6f9c988` | #47 | Devanagari words; re-infer a stale purpose only after a type switch / over an inferred one; a stated budget ends the "cheaper" exclusion |
| `f4cc48d` | #48 | the bare-greeting prompt rule (already hand-patched in production) |
| `6368cdd` | (this work) | filters from an earlier session, keep / new-search question, typed commands, greeting before the model, filter summaries in headers, "kharidna" |
| `8310b2e` | (hotfix `608f4d6`) | carried because it is on a separate branch, **not** in `main`; without it this release would remove it |
| `69820fe` | - | one test assertion updated for the new no-match wording |

**Incident, 2026-10-17/18 (test number). Observed on production** (read-only query of the real database, run on
the 18th): stored `purpose=rent` saved 2026-10-16 18:39; `property_types=house` and `size=500 gaz` saved 2026-10-17
22:06; `budget` 1.8-2.2 crore saved 18th 04:42 with **`period: monthly`**; the 04:41 "hi" produced a rejected
`set purpose` op; the reply sent was "DHA Phase 6 mein is budget mein 500 gaz ghar abhi available nahi."

**Replayed through code** (`incident-replay.txt`, same slots and patches through the merge code of each tree):

| Tree | 17th "ghar + aath crore tak" | 18th "aur budget 2 crore" |
|---|---|---|
| P (expected production) | purpose rent, period monthly | purpose rent, period **monthly** (matches the observed row) |
| candidate without a session layer (= PR #47 code) | identical | identical |
| candidate with the session layer | period null, purpose asked about | period null |

So: the observed behaviour is reproduced by P's code; **PR #47 does not change it** (the rent was stated, and a
flat -> house change is not a type switch). My earlier note that PR #47 fixed this was wrong. What fixes it is the
session layer in `6368cdd`, which was never in production. Not observed, only inferred: the exact ops of the 17th
22:06 patch (the query output was truncated) and the production image's hashes (gate, section 4).

## 1. Release gates (in this order; each must pass before the next)

1. **Independent diff review** of the review package (runtime, tests, docs diffs, this runbook, the incident
   replay, the rollback-compatibility run). Findings are fixed on the branch and the package regenerated.
2. **PR approval and merge, then verification of the final release artifact.** `main` must contain the hotfix
   (`608f4d6`'s change) and the 15 files must be byte-identical to the reviewed candidate (section 3). On `M`:
   re-run the four test batches (expected totals in section 2), Ruff, `git diff --check`, rebuild the kit, run the
   gate commands in section 3, compare hashes with the candidate's `SHA256SUMS`.
3. **Deployment** (sections 4-8): baseline gate, transfer, build, image checks, switch, health.
4. **Real WhatsApp smoke checks immediately afterwards** (section 9), with **rollback on any failure** (section 10).

There is no WhatsApp staging environment, so phone checks against the new code **cannot** be a pre-deployment
gate. Before deployment the evidence is automated: the test suite on disposable PostgreSQL, the incident replay,
and the rollback-compatibility run. The only pre-deployment phone action is **seeding**, which exercises the
*current* production code, not the new code (section 9.0).

## 2. Runtime change set and test evidence

`git diff --name-only 91b7cef <M> -- app migrations requirements.txt Dockerfile demo deploy .dockerignore` must print
exactly these 15 files (rehearsed on `69820fe`: **GATE OK**):

```
app/card_regen.py (new)    app/cards.py            app/customer_intent.py (new)
app/customer_tools.py      app/demo_media.py       app/handoff.py
app/handoff_actions.py     app/handoff_alert.py    app/media.py
app/normalise.py           app/render.py           app/requirements.py
app/search.py              app/staff_cards.py      app/turn_engine.py
```

No dependency, `Dockerfile`, migration, `demo/`, `deploy/` or compose change (Pillow is already in the image).
`app/card_regen.py` ships as inert code: nothing imports it and nothing runs it.

Candidate `69820fe`: 703 + 1,337 + 472 + 1,089 = **3,601 passed, 0 failed, 0 skipped** (= collect-only), Ruff clean,
disposable PostgreSQL 18.6, `REQUIRE_DATABASE_TESTS=1`, four batches run one at a time. Re-run on `M`.

## 3. Release artifact: build and verify (local, from the exact commit; never the E:\manzilai checkout)

The main checkout is CRLF (`core.autocrlf=true`); archive committed bytes with autocrlf off. The archive is made by
`git archive` of **named paths from a commit**, so it can contain only committed files of that commit.

```
M=<merge commit>; S=${M:0:7}; K=release-kit; mkdir -p $K
CH="app/card_regen.py app/cards.py app/customer_intent.py app/customer_tools.py app/demo_media.py \
 app/handoff.py app/handoff_actions.py app/handoff_alert.py app/media.py app/normalise.py \
 app/render.py app/requirements.py app/search.py app/staff_cards.py app/turn_engine.py"
git -c core.autocrlf=false archive --format=tar --prefix=release-fresh/ -o $K/manzil-fresh-$S.tar $M $CH
for f in $CH; do printf '%s  %s\n' "$(git -c core.autocrlf=false show $M:$f | sha256sum | cut -d' ' -f1)" $f; done > $K/SHA256SUMS
for f in $(git ls-tree -r --name-only $M app/ | grep '\.py$'); do case " $CH " in *" $f "*) ;; \
  *) printf '%s  %s\n' "$(git -c core.autocrlf=false show $M:$f | sha256sum | cut -d' ' -f1)" $f;; esac; done > $K/SHA256SUMS.unchanged
printf 'FROM manzil-ai:<RUNNING_TAG>\nCOPY release-fresh/app/ /app/app/\n' > $K/Dockerfile.release
( cd $K && sha256sum manzil-fresh-$S.tar > manzil-fresh-$S.tar.sha256 )
```

**Artifact checks (all must pass; rehearsed on `69820fe`, results in `artifact-verification.txt`):**

| Check | Command / result |
|---|---|
| Only intended files | `tar -tf` lists exactly the 15 paths + 2 directories; 15 regular files, no links, no absolute paths |
| Byte-exact | extracted files pass `sha256sum -c SHA256SUMS` (15 OK), which are the Git blobs' hashes |
| Nothing unrelated | `git status` clean; `git diff --check` clean; the path gate above prints only the 15 files |
| No secrets | regex scan of the tar contents (AWS / Google / Meta token shapes, private keys, credentialed URLs, quoted secrets): no hit. **gitleaks is not installed on this machine**: run it on the repository (`gitleaks detect`) as part of gate 2 |
| No env / data files | none of `.env`, `*.env`, `demo/`, `migrations/`, `*.pem` is in the archive |

## 4. Baseline gate on EC2 (read-only; stop on any mismatch)

```
cd /home/ubuntu/manzil-ai
docker compose -f deploy/compose.yml --env-file .env ps
for c in deploy-web-1 deploy-worker-1; do docker inspect $c --format '{{.Name}} {{.Config.Image}} {{.Image}}'; done
docker image ls manzil-ai --format '{{.Tag}} {{.ID}}' | head -20
```

Web and worker must show the same image ID. Record its tag as `<RUNNING_TAG>` and its ID as `<PRE_ID>`
(the 2026-10-17 hotfix runbook used base `manzil-ai:switch-91b7cef`; read the real one, do not assume).

```
docker run --rm --entrypoint sh -v "$PWD/SHA256SUMS.unchanged:/tmp/u:ro" <RUNNING_TAG> \
  -c 'cd /app && sha256sum -c /tmp/u'                       # every line OK, else STOP
docker run --rm --entrypoint sh <RUNNING_TAG> -c 'cd /app && sha256sum app/turn_engine.py'
```

The 15 files are all replaced, so they may be in any known state; `turn_engine.py` must be one of:

| sha256 of app/turn_engine.py | state |
|---|---|
| `d56191a8d449bc5837758936e6dce5689043ad2225e604430d902b0b64fe2617` | `91b7cef` |
| `8eb0b6256972b7e2d3f9987665e0a9c84d563439a7d0f84fb430c490bfb43777` | + hotfix `608f4d6` |
| `7ab72a7b0916b33cb0f049dd95dc6718833ac3f2f7c12e274f5d7764f54e1d00` | + hotfix + greeting line (clean) |

A hand-patched file will hash differently: then
`docker run --rm --entrypoint cat <RUNNING_TAG> /app/app/turn_engine.py | diff - <(git show 608f4d6:app/turn_engine.py)`
must show only the greeting-rule lines. Anything else, or any other of the 15 files not at the `91b7cef` hash,
**stops the release**.

## 5. Transfer (CloudShell -> private S3 staging -> EC2)

1. CloudShell upload: tar, `.tar.sha256`, `SHA256SUMS`, `SHA256SUMS.unchanged`, `Dockerfile.release`.
2. CloudShell: `aws s3 cp <file> s3://<bucket>/staging/release-fresh/` for each (private prefix; never `tenant/` or `card/`).
3. EC2, in `/home/ubuntu/release-fresh/`: `aws s3 cp` each down; `sha256sum -c manzil-fresh-$S.tar.sha256`; `tar -xf`;
   `( cd release-fresh && sha256sum -c ../SHA256SUMS )` (15 OK).
4. Delete the `staging/release-fresh/` objects once the image is built.

## 6. Build and image checks (no service touched yet)

```
docker build -f Dockerfile.release -t manzil-ai:fresh-$S .
docker run --rm --entrypoint sh -v "$PWD/SHA256SUMS:/tmp/S:ro" manzil-ai:fresh-$S -c 'cd /app && sha256sum -c /tmp/S'
for i in <RUNNING_TAG> manzil-ai:fresh-$S; do docker run --rm --entrypoint sh $i -c 'cd /app && find app migrations demo -type f -not -name "*.pyc" | sort | xargs sha256sum' > "files-$(echo $i | tr ':/' '__').txt"; done
diff files-<running>.txt files-manzil-ai_fresh-$S.txt     # only the 15 app/ lines (2 new, 13 changed)
docker run --rm --entrypoint python manzil-ai:fresh-$S -c "import app.web, app.worker, app.customer_intent, app.card_regen; print('ok')"
docker run --rm --entrypoint id manzil-ai:fresh-$S        # appuser, not root
```

Migration state is read-only and unchanged: the check of docs/RELEASE_10_2.md section 6 must still print `17 17 []`. Never run the migrator.

## 7. Switch (web and worker only; Caddy untouched) and pinning

Both services take the image from the **same tag**: `deploy/compose.yml` has `image: manzil-ai:latest` for `web` (it also
has `build: ..`, which `--no-build` disables) and for `worker` (`pull_policy: never`). The switch moves that one tag and recreates
both in one command, then proves by ID that both run the release image.

```
cd /home/ubuntu/manzil-ai
test "$(docker image inspect manzil-ai:latest --format '{{.Id}}')" = "<PRE_ID>" || echo "STOP: latest is not the recorded image"
docker tag manzil-ai:latest manzil-ai:pre-fresh                  # rollback tag
docker tag manzil-ai:fresh-$S manzil-ai:latest
docker compose -f deploy/compose.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker
WANT=$(docker image inspect manzil-ai:fresh-$S --format '{{.Id}}')
for c in deploy-web-1 deploy-worker-1; do
  [ "$(docker inspect $c --format '{{.Image}}')" = "$WANT" ] && echo "$c: release image" || echo "$c: MISMATCH -> roll back"; done
for s in web worker; do docker compose -f deploy/compose.yml --env-file .env exec -T $s sh -c 'cd /app && sha256sum -c -' < SHA256SUMS | grep -vc ': OK'; done   # 0, 0
docker compose -f deploy/compose.yml --env-file .env ps         # caddy unchanged
```

## 8. Health checks

- `curl -fsS https://<SITE_ADDRESS>/health` OK; `curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<SITE_ADDRESS>/webhook -d '{}'` = 401 (signature refused: the route is alive).
- `docker compose ... logs --since 5m worker`: `Worker started: ...`; no exit 78; no gazetteer error. `... logs web`: no traceback. Read no phone numbers, text or secrets.

## 9. WhatsApp smoke checks (after deployment; failure => section 10)

**9.0 Seed before deployment (exercises the old code, not the new).** For the stale-filter check, a test number X must
have stored filters and **no inbound message for more than 6 hours before the first post-deploy message**.
At least 7 h before the switch, send from X: `DHA Phase 6 mein 500 gaz ka ghar kiraye par chahiye`. Do not message from X again until 9.2.
(The session is computed from stored message times, so the gap counts while nothing is running.)

**9.1 Immediately after the switch (number Y, a second test number, any state):**
1. `salam` -> "Wa alaikum assalam! ..." (not "Shukriya!"); `hi` -> "Hello! ...".
2. `salam, DHA phase 6 mein flat chahiye` -> a normal request answer, not the greeting template.
3. A rent search: the header says purpose and period ("... kiraye par, 2 lakh tak mahana ...").
4. `nayi search` (nothing inherited) -> lists the filters, clears nothing; `purani requirements hata do` -> "...hata di hain...".
5. Voice: the ~8 s Roman Urdu note scenario of turn `4b1d4b0f` gets a search reply; the worker log shows `iteration_limit_search` at WARNING (hotfix kept).
6. A card carousel and a single card render (PR #45 layout).

**9.2 Number X (idle > 6 h, seeded in 9.0):**
7. `mera area hy dha phase 6` -> "Theek hai, note kar liya: DHA Phase 6." + "Pichli search mein 500 gaz ghar, kiraye par tha - kya ab bhi wohi chahiye?" [Haan, wohi] [Nayi search]; no listings.
8. `aur budget hy 2 crore` -> "... 2 crore ke aas paas." + the short reminder. Read-only check of the stored budget: `period` null (`stored_requirements.py`, phone masked).
9. Tap [Nayi search] -> the inferred-purchase note + a result / no-match line with no "500 gaz" and no "mahana".
10. Repeat 7-8 on a second idle number or after another 6 h gap: tap [Haan, wohi] -> rent kept, "mahana" in the header; try typed `haan`, `nahi`, `nahi, kharidna hai`.
11. An old, already-answered button -> "Ye sawal ab purana ho gaya hai", nothing changes.

**Roll back immediately** on: a traceback or exit 78; a failed turn; a reply that restores house / rent or labels an
unconfirmed budget monthly; the old greeting text; an unrendered card; web or worker not on the release image.

## 10. Rollback and compatibility

```
docker tag manzil-ai:pre-fresh manzil-ai:latest
docker compose -f deploy/compose.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker
```

then section 8 and the pinning loop of section 7 with `WANT` = `<PRE_ID>`. Keep `manzil-ai:pre-fresh`, `<RUNNING_TAG>`,
the kit and the `files-*.txt` lists until the smoke checks are done.

**Can the previous image read what this release wrote? Yes (tested).** `rollback-compat-run.txt`: states written by the
new code (open question with an unlabelled budget, after keep, after "nayi search", after a full reset, with their patch-log
rows) were installed under the *previous* code (`fdd4de3`); it then ran an update + search, a plain text, an unknown `keep:`
tap and a type change on each. All turns replied, none failed, the state was extended normally.

**What a code-only rollback leaves behind** (no schema is involved, so nothing blocks the old code):

| Persisted by this release | Effect after rollback |
|---|---|
| `requirement_state.slots[*].kept_at` (extra JSON key) | ignored by the old code |
| `updated_at` of slots = the customer's message time (not processing time) | harmless (a few seconds) |
| `requirement_patch_log` rows with call `keep` / `fresh`, `kind` keep / reset, `detail` kept / fresh_search, ops `set` / `clear` | valid under the table CHECK; the old hold logic reads them as "answered" |
| Slots cleared by "nayi search" / "purani requirements hata do" (incl. previous budget, excluded listings, soft prefs) | **stay cleared**; the previous values survive only in `requirement_patch_log` (`before`), not restored automatically |
| `pending_clarification` rows closed as `cancelled`; a purpose question opened by a fresh search | valid statuses; the old code answers an open purpose question as before |
| `search_run`, `shown_item`, `message`, `outbound_message`, `conversation_turn` rows, incl. sent messages with `keep:yes:<token>` buttons | history only |
| S3 | nothing (no card regeneration) |

Behavioural differences after rollback: (a) a conversation whose state is "inherited, question open" goes back to the old
behaviour: the stale filters are searched without a question and an unlabelled budget of a stored rent is shown as monthly;
(b) a tap on an earlier `keep:` button is not recognised and reaches the model as ordinary text ("Haan, wohi"), with no
data change by itself.

## 11. Known limits

- No staging environment: verification is on production with the owner's own numbers (section 9).
- The "question already sent" check scans one conversation's outbound rows of the last 7 days (2.5 ms at 5,003 rows); the
  session query is 4.4 ms at 10,005 messages (`query-plans-and-timings.txt`). Migration 018 is deferred until real history is larger.
- Existing card images are not rebuilt by this release (`card_regen` is out of scope; old cards keep the cropped layout).
