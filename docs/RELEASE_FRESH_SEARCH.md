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
exactly these 15 files (the build script enforces it); three more files are replaced because production lags (section 3):

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

## 3. Release kit v2: 18 files (local, from the exact commit; never the E:\manzilai checkout)

**Why 18, not 15.** The real baseline gate (2026-10-18) found production different from `91b7cef` in three offline
benchmark tools: `app/bench_live.py` missing, `app/benchmark.py` and `app/staff_bench.py` older. They are Task 10.3 tooling
(`b7cf3d3`, 2026-10-16), unchanged between `91b7cef` and the merge commit `46c44d8`, and no earlier layered release shipped
them (each release layered only the runtime files it changed). Production's versions are expected to be those of `7f5cd3d`
(the 10.1 base: `benchmark.py` `0b6c65b1...`, `staff_bench.py` `1bd84df4...`, `bench_live.py` not yet existing). That is a
**hypothesis until the gate confirms it by hash**: the gate accepts exactly those blobs (or ABSENT for `bench_live.py`) and
stops, printing the real hash, on anything else. No web or worker module imports them (`bench_live` imports `benchmark`,
`staff_bench`, `stt_bench`; `staff_bench` imports `bench_live` lazily inside one function; nothing in `web`/`worker`/`handlers`
does), so the old versions are harmless today, and replacing them makes `app/` byte-identical to the tested commit.

Between `7f5cd3d` and `46c44d8` there is no change in `migrations/`, `demo/`, `requirements.txt` or the `Dockerfile`
(`git diff 7f5cd3d 46c44d8 -- migrations demo requirements.txt Dockerfile` is empty), so replacing the 18 `app/` files is
sufficient for the image's `app/`, `migrations/` and `demo/` trees to equal the tested commit. Dependencies are the image's
own (unchanged requirements). The image check in section 6 proves the tree equality on the assembled image.

Build, in Git Bash from a clean checkout of the repository (the script pins `core.autocrlf=false` on every git call, stops if
the changed-file set vs `91b7cef` is not exactly the expected 15, and verifies the archive against the Git blobs):

```
bash docs/release/build_kit.sh <M> <outdir>        # e.g. 46c44d8; scripts are taken from HEAD (or a third argument)
```

It writes: the 18-file tar, `SHA256SUMS` (18), `SHA256SUMS.unchanged` (43 other app modules), `SHA256SUMS.base` (12 replaced
modules at their `91b7cef` versions), `SHA256SUMS.data` (22 migrations 005+/demo files), `FILES.expected` (the `91b7cef`
file list), `FILES.release` (the merge commit's file list), `STATES.allowed`, the three scripts, `Dockerfile.release`,
`KIT.sha256` and `manzil-fresh-<S>-kit.zip`.

`STATES.allowed` is the only place an unusual production state is permitted, one line per path and hash (or `ABSENT`):
`turn_engine.py` x3 (`91b7cef`; + hotfix `608f4d6`; + hotfix + the greeting line), `benchmark.py` and `staff_bench.py` at
`7f5cd3d`, `bench_live.py` ABSENT, and migrations 001-004 as Git LF blob or CRLF working-copy form (historical RDS checksums
for 001/002 are CRLF while Git stores LF).

**Artifact checks (rehearsed on `46c44d8`; the build script enforces the first four):**

| Check | Result |
|---|---|
| changed set vs `91b7cef` | exactly the expected 15 files; the 3 benchmark tools are identical between `91b7cef` and `M` |
| archive | exactly the 18 paths, bytes equal to the Git blobs |
| text files | no CR byte in any manifest, script or Dockerfile |
| counts | 18 + 43 = 61 app modules = every `.py` of `M`; no path in two manifests |
| no secrets | regex scan of the kit; **gitleaks is not installed on this machine**: run it on the repository |
| scripts | `bash -n`; 42 stub tests; run for real against Docker (section 11) |

## 4. Transfer (before anything is checked or changed on the server)

One directory from here on: `REL=/home/ubuntu/release-fresh`. The ZIP is flat and has no mode bits: run scripts with `bash`.

1. CloudShell: upload `manzil-fresh-<S>-kit.zip`; `aws s3 cp manzil-fresh-<S>-kit.zip s3://<bucket>/staging/release-fresh/` (private prefix; never `tenant/` or `card/`).
2. EC2: `mkdir -p $REL && cd $REL && aws s3 cp s3://<bucket>/staging/release-fresh/manzil-fresh-<S>-kit.zip . && python3 -m zipfile -e manzil-fresh-<S>-kit.zip .`
3. `sha256sum -c KIT.sha256` (every line OK), `sha256sum -c manzil-fresh-<S>.tar.sha256`, `tar -xf manzil-fresh-<S>.tar`,
   `( cd release-fresh && sha256sum -c ../SHA256SUMS | grep -vc ': OK' )` prints 0.

## 5. Baseline gate on EC2 (read-only; stops on any mismatch)

```
cd /home/ubuntu/release-fresh
sudo --preserve-env=REL REL=$PWD bash ./fresh_search_baseline.sh
```

The gate checks the **running** web and worker containers and the image, **collects every mismatch** and prints them all with
the actual hashes (one run is enough to see everything), then exits 1; it writes `$REL/PRE_ID` only on a clean pass:

- web and worker run one image ID; `docker diff` shows no change under `/app` (other than `__pycache__`) = no in-place edit;
- inside each running container (`docker exec -i`, so the manifests really reach the checker): the 43 unchanged modules, the 12
  replaced modules at their `91b7cef` versions and the 22 data files match; every path in `STATES.allowed` is in one of its
  listed states; the file list has no extra file and no missing file except those marked ABSENT;
- the image itself passes the three manifests.

A hand-patched `turn_engine.py` hashes differently and is reported; compare it by hand
(`docker exec deploy-web-1 cat /app/app/turn_engine.py | diff - <(git show 608f4d6:app/turn_engine.py)` must show only the
greeting-rule lines) and only then add its hash to `STATES.allowed`. For a benchmark tool whose hash is not the `7f5cd3d`
blob, send the printed hash: it is mapped to a commit before anything is accepted. Never edit a manifest to make a check pass.

## 6. Build and image check (no service touched yet)

```
cd /home/ubuntu/release-fresh; PRE=$(cat PRE_ID); S=<S>
sudo docker tag "$PRE" manzil-ai:base-$S            # FROM needs a tag, not an image id
test "$(sudo docker image inspect manzil-ai:base-$S --format '{{.Id}}')" = "$PRE" && echo "base = recorded image"
sudo docker build -f Dockerfile.release -t manzil-ai:fresh-$S .
sudo --preserve-env=REL REL=$PWD bash ./fresh_search_image_check.sh manzil-ai:fresh-$S
```

The image check proves the assembled image equals the tested commit: the 18 released files, the 43 other modules and the 22
data files match their manifests; the file list equals `FILES.release`; migrations 001-004 are in an explained state; the user
is `appuser`; the runtime and the three benchmark modules import. It prints every problem it finds and exits 1.
Migration state is read-only and unchanged: the check of docs/RELEASE_10_2.md section 6 must still print `17 17 []`. Never run the migrator.

## 7. Switch (web and worker only; Caddy untouched), pinning, stop and rollback behaviour

```
cd /home/ubuntu/release-fresh
sudo --preserve-env=SITE,REL SITE=<SITE_ADDRESS> REL=$PWD bash ./fresh_search_switch.sh switch $S
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

Automatic during the switch (section 7). Manual, later: `sudo --preserve-env=SITE,REL SITE=<SITE_ADDRESS> REL=/home/ubuntu/release-fresh bash ./fresh_search_switch.sh rollback`,
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

## 12. Validation of the v2 kit against real Docker (local, 2026-10-18)

Nothing in production was touched. A reference image was built from `46c44d8` (LF export, repo `Dockerfile`); a **pseudo-production**
image was made from it by putting back the `91b7cef` versions of the 12 files, `turn_engine.py` at the hotfix + greeting state,
the `7f5cd3d` benchmark tools, and removing `card_regen.py`, `customer_intent.py`, `bench_live.py`. Then, with real Docker and Compose:

1. `fresh_search_baseline.sh` on two containers of the pseudo-production image: **OK**, `PRE_ID` written. With an in-place edit
   (`render.py`, `benchmark.py` appended, `cards.py` deleted) it listed all four problems with the hashes and wrote nothing.
2. Image built `FROM manzil-ai:base-<S>` (a `FROM sha256:...` is rejected by Docker: found and fixed here), then
   `fresh_search_image_check.sh`: **OK**; the same check on the pseudo-production image fails with 5 problems.
3. `fresh_search_switch.sh switch` through a real Compose project named `deploy` (containers `deploy-web-1`/`deploy-worker-1`):
   a tampered `SHA256SUMS` made the in-container check fail and the script **rolled back by the recorded ID**; with the good
   kit it switched, 18/18 files verified inside both running containers (`compose exec -T` forwards stdin); a second switch
   stopped at its precondition; `rollback` returned both to the recorded ID.
