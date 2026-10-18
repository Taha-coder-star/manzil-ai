# Fresh-search release runbook (prepared, not run)

Status: prepared and reviewed locally on `release/fresh-search`. **No production step has been run;
nothing is pushed or deployed; `card_regen` is out of scope.** Shutdown / cost-saving commands are
deliberately **not** in this file: see `docs/RELEASE_FRESH_SEARCH_COST.md`.

| Item | Value |
|---|---|
| Runtime candidate | `release/fresh-search`: see `COMMITS.md` for the exact tip; runtime commits `69820fe` + review fixes `f8b8044` |
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
| `f8b8044` | (review fixes) | negated / questioning reset commands never mutate state; the question token and its "already asked" lookup are bound to the session; every inherited filter is described in the question |

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
   artifact checks in section 3, compare hashes with the candidate's `SHA256SUMS`.
3. **Deployment** (sections 4-8): transfer, baseline gate, build, image checks, switch (with automatic rollback), health.
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

## 3. Release kit (local, from the exact commit; never the E:\manzilai checkout)

The main checkout is CRLF (`core.autocrlf=true`); archive committed bytes with autocrlf off. Everything in the kit is
read from **named paths of a commit** (`git archive` / `git show`), so it can only contain committed files of that commit.

```
M=<merge commit>; S=${M:0:7}; K=release-kit; mkdir -p $K
CH="app/card_regen.py app/cards.py app/customer_intent.py app/customer_tools.py app/demo_media.py \
 app/handoff.py app/handoff_actions.py app/handoff_alert.py app/media.py app/normalise.py \
 app/render.py app/requirements.py app/search.py app/staff_cards.py app/turn_engine.py"
git -c core.autocrlf=false archive --format=tar --prefix=release-fresh/ -o $K/manzil-fresh-$S.tar $M $CH
h() { git -c core.autocrlf=false show "$1:$2" | sha256sum | cut -d' ' -f1; }
for f in $CH; do printf '%s  %s\n' "$(h $M $f)" $f; done > $K/SHA256SUMS                 # the 15 release files
# the modules this release does not change, and the 12 it replaces that exist in 91b7cef (turn_engine.py has 3 allowed states)
for f in $(git ls-tree -r --name-only $M app/ | grep '\.py$'); do case " $CH " in *" $f "*) ;; \
  *) printf '%s  %s\n' "$(h $M $f)" $f;; esac; done > $K/SHA256SUMS.unchanged
for f in $CH; do case $f in app/turn_engine.py|app/card_regen.py|app/customer_intent.py) ;; \
  *) printf '%s  %s\n' "$(h 91b7cef $f)" $f;; esac; done > $K/SHA256SUMS.base
git ls-tree -r --name-only 91b7cef app/ | grep '\.py$' | LC_ALL=C sort > $K/FILES.expected
printf '%s\n' d56191a8d449bc5837758936e6dce5689043ad2225e604430d902b0b64fe2617 \
  8eb0b6256972b7e2d3f9987665e0a9c84d563439a7d0f84fb430c490bfb43777 \
  7ab72a7b0916b33cb0f049dd95dc6718833ac3f2f7c12e274f5d7764f54e1d00 > $K/TURN_ENGINE.allowed
for f in fresh_search_baseline.sh fresh_search_switch.sh; do git -c core.autocrlf=false show $M:docs/release/$f > $K/$f; done
printf 'FROM manzil-ai:<RUNNING_TAG>\nCOPY release-fresh/app/ /app/app/\n' > $K/Dockerfile.release
( cd $K && sha256sum * > ../KIT.sha256 && mv ../KIT.sha256 KIT.sha256 )              # integrity of every kit file
```

`<RUNNING_TAG>` is only needed for the build; the baseline gate itself works on the running containers.

**Artifact checks (all must pass; rehearsed on the tip commit, results in `evidence/artifact-verification.txt`):**

| Check | Command / result |
|---|---|
| Only intended files | `tar -tf` lists exactly the 15 paths + 2 directories; 15 regular files, no links, no absolute paths |
| Byte-exact | extracted files pass `sha256sum -c SHA256SUMS` (15 OK), the Git blobs' hashes |
| Nothing unrelated | `git status` clean; `git diff --check` clean; `git diff --name-only 91b7cef $M -- app migrations requirements.txt Dockerfile demo deploy .dockerignore` prints exactly the 15 |
| No secrets | regex scan of the kit (AWS / Google / Meta token shapes, private keys, credentialed URLs, quoted secrets): no hit. **gitleaks is not installed on this machine**: run it on the repository (`gitleaks detect`) as part of gate 2 |
| No env / data files | none of `.env`, `*.env`, `demo/`, `migrations/`, `*.pem` is in the archive |
| Scripts | `bash -n` on both; their stub tests (`tests/test_release_script.py`, 27 cases) pass on `M` |

## 4. Transfer (before anything is checked or changed on the server)

One directory is used from here on: `REL=/home/ubuntu/release-fresh`.

1. CloudShell upload every file of the kit (tar, `.tar.sha256`, `SHA256SUMS*`, `FILES.expected`, `TURN_ENGINE.allowed`,
   `fresh_search_*.sh`, `Dockerfile.release`, `KIT.sha256`).
2. CloudShell: `aws s3 cp <file> s3://<bucket>/staging/release-fresh/` for each (private prefix; never `tenant/` or `card/`).
3. EC2: `mkdir -p $REL && cd $REL`, `aws s3 cp` each file down, then
   `sha256sum -c KIT.sha256` (all OK), `sha256sum -c manzil-fresh-$S.tar.sha256`, `tar -xf manzil-fresh-$S.tar`,
   `( cd release-fresh && sha256sum -c ../SHA256SUMS )` (15 OK), `chmod +x fresh_search_*.sh`.
4. Delete the `staging/release-fresh/` objects after the build in section 6.

## 5. Baseline gate on EC2 (read-only; stops on any mismatch)

```
cd /home/ubuntu/release-fresh
sudo --preserve-env=REL REL=$PWD ./fresh_search_baseline.sh
```

It checks the **running** web and worker containers, not only the image, and writes `$REL/PRE_ID` (the image ID both run)
only if everything passes:

- web and worker run one image ID;
- `docker diff` shows no change under `/app` (other than `__pycache__`) since the container started: no in-place edit;
- inside each running container: the 46 unchanged modules and the 12 replaced modules at their `91b7cef` versions
  match their manifests; the list of `app/**/*.py` equals `FILES.expected` (no extra or missing module, so no
  `card_regen.py` / `customer_intent.py` yet); `app/turn_engine.py` hashes to one of the three allowed states;
- the image itself passes the same two manifests.

| allowed sha256 of app/turn_engine.py | state |
|---|---|
| `d56191a8d449bc5837758936e6dce5689043ad2225e604430d902b0b64fe2617` | `91b7cef` |
| `8eb0b6256972b7e2d3f9987665e0a9c84d563439a7d0f84fb430c490bfb43777` | + hotfix `608f4d6` |
| `7ab72a7b0916b33cb0f049dd95dc6718833ac3f2f7c12e274f5d7764f54e1d00` | + hotfix + greeting line (clean) |

A hand-patched `turn_engine.py` will hash differently and the gate stops. Only then compare by hand:
`docker exec deploy-web-1 cat /app/app/turn_engine.py | diff - <(git show 608f4d6:app/turn_engine.py)` must show only the
greeting-rule lines. If it does, append that hash (not before) to `TURN_ENGINE.allowed` and re-run. Any other
difference, or any other failed check, **stops the release**.

## 6. Build and image checks (no service touched yet)

```
cd /home/ubuntu/release-fresh
PRE=$(cat PRE_ID); sed -i "s#manzil-ai:<RUNNING_TAG>#$PRE#" Dockerfile.release       # build on the recorded ID, not a tag
docker build -f Dockerfile.release -t manzil-ai:fresh-$S .
docker run --rm --entrypoint sh -v "$PWD/SHA256SUMS:/tmp/S:ro" manzil-ai:fresh-$S -c 'cd /app && sha256sum -c /tmp/S'
for i in $PRE manzil-ai:fresh-$S; do docker run --rm --entrypoint sh $i -c 'cd /app && find app migrations demo -type f -not -name "*.pyc" | sort | xargs sha256sum' > "files-$(echo $i | tr ':/' '__').txt"; done
diff files-sha256_*.txt files-manzil-ai_fresh-$S.txt      # only the 15 app/ lines (2 new, 13 changed)
docker run --rm --entrypoint python manzil-ai:fresh-$S -c "import app.web, app.worker, app.customer_intent, app.card_regen; print('ok')"
docker run --rm --entrypoint id manzil-ai:fresh-$S         # appuser, not root
```

Migration state is read-only and unchanged: the check of docs/RELEASE_10_2.md section 6 must still print `17 17 []`.
Never run the migrator.

## 7. Switch (web and worker only; Caddy untouched), pinning, stop and rollback behaviour

```
cd /home/ubuntu/release-fresh
sudo --preserve-env=SITE,REL SITE=<SITE_ADDRESS> REL=$PWD ./fresh_search_switch.sh switch $S
```

The script (`docs/release/fresh_search_switch.sh`, `set -Eeuo pipefail`, tested against stubs) works like this:

- **Preconditions stop before anything changes** (exit 1): `$REL/PRE_ID` present; `manzil-ai:latest` is the recorded ID;
  web and worker both run the recorded ID; the release image exists and differs.
- **Switch:** tags the recorded ID as `manzil-ai:pre-fresh`, moves `manzil-ai:latest` to the release image ID, recreates web
  and worker in one command (`--no-deps --no-build`; `web` has `build: ..` and `worker` has `pull_policy: never`, so nothing
  is rebuilt or pulled).
- **Pinning:** both services use the single tag `manzil-ai:latest`. After the recreate the script requires, for **web and
  worker**: container image ID equals the release image ID, `sha256sum -c SHA256SUMS` passes **inside the running
  container**, `/health` answers, `POST /webhook` returns 401, and the worker log shows `Worker started`.
- **Any failure after the switch triggers an explicit rollback by the recorded image ID** (never by the `pre-fresh` tag,
  which the tests deliberately corrupt): re-tags the ID to `latest`, recreates both, requires both on the recorded ID and the
  site answering. Exit 1 = rolled back; **exit 2 = the rollback itself could not be verified: manual intervention**.
- Exit 0 means both services run the release image.

## 8. After the script

`docker compose -f deploy/compose.yml --env-file .env ps` (caddy unchanged); read `... logs --since 5m worker` and `... web` for
tracebacks or exit 78, without copying phone numbers, text or secrets.

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

Automatic during the switch (section 7). Manual, later: `sudo --preserve-env=SITE,REL SITE=<SITE_ADDRESS> REL=/home/ubuntu/release-fresh ./fresh_search_switch.sh rollback`,
which uses `$REL/PRE_ID`; by hand: `docker tag $(cat $REL/PRE_ID) manzil-ai:latest` then the same `up -d --no-deps --no-build --force-recreate web worker`.
Check that both containers report the recorded ID (`docker inspect <c> --format '{{.Image}}'`). Keep `manzil-ai:pre-fresh`,
the recorded image, the kit and the `files-*.txt` lists until the smoke checks are done.

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
