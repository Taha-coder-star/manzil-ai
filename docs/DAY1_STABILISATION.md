# Day 1 stabilisation: operator checklist

Status: local preparation in progress on `fix/day1-stabilisation`, based on `26ca5cc`.
No Day 1 live pass has been recorded. User operates AWS/phones; Codex prepares and analyses;
Taha reviews fixes and remains the merge partner. Do not merge without review.

## First: unblock local verification

Start Docker Desktop. In a terminal, from the repo root:

```powershell
docker run -d --rm --name manzil-test-pg -e POSTGRES_USER=manzil_test -e POSTGRES_PASSWORD=local_test_only -e POSTGRES_DB=manzil_schema_test -p 127.0.0.1:55432:5432 postgres:18
$env:TEST_DATABASE_URL='postgresql://manzil_test:local_test_only@127.0.0.1:55432/manzil_schema_test'
$env:REQUIRE_DATABASE_TESTS='1'
cmd /d /c ".venv\Scripts\python.exe -m pytest -q tests/test_demo_reset.py tests/test_staff_commit_db.py tests/test_staff_turn_db.py tests/test_staff_update_db.py tests/test_staff_find.py"
```

If the named container already exists, inspect it; do not create a second database test runner.
Never use production DATABASE_URL for tests. Only one pytest process against this database.
Full suite and Linux release shell tests go through CI.

## Baseline: owner-run, before release packaging

The known region is us-east-1, RDS is manzil-demo-db, EC2 Name is manzil-demo-server.
In CloudShell inspect state first:

```bash
aws rds describe-db-instances --region us-east-1 --db-instance-identifier manzil-demo-db --query DBInstances[].DBInstanceStatus --output text
aws ec2 describe-instances --region us-east-1 --filters Name=tag:Name,Values=manzil-demo-server --query Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress] --output table
```

If stopped, start RDS and wait for available, then start the single matching stopped EC2:

```bash
aws rds start-db-instance --region us-east-1 --db-instance-identifier manzil-demo-db
aws rds wait db-instance-available --region us-east-1 --db-instance-identifier manzil-demo-db
IID=$(aws ec2 describe-instances --region us-east-1 --filters Name=tag:Name,Values=manzil-demo-server Name=instance-state-name,Values=stopped --query Reservations[].Instances[].InstanceId --output text)
test -n "$IID" && test "$(wc -w <<< "$IID")" -eq 1 && aws ec2 start-instances --region us-east-1 --instance-ids "$IID"
```

Use EC2 Instance Connect as ubuntu. Run the generated local
`Handoffs/codex/day1-baseline-paste.txt`: it transfers a checksummed, read-only script
using quote-free lines ending in ` #`. Return its output, plus the `/health` status read inside
the web container (public `/health` is 404 since audit WP2; command in `docs/RELEASE_10_2.md` §8).
It prints source hashes, container differences, provider, inventory/draft/job/role counts;
it does not print credentials, phone numbers or message text.
Expected: matching healthy web/worker; no unexplained /app changes.
If the obsolete task61-exit78-probe exists, stop it with:

```bash
sudo docker update --restart=no task61-exit78-probe #
sudo docker stop task61-exit78-probe #
```

Verify the Elastic IP association and Meta webhook. Confirm all five recipients and the
customer/staff/broker/admin role map; broker must be active on a separate phone.
Do not paste phone numbers into git. Confirm current free quotas in AI Studio.

## Release and reset gate

Current historical build_kit.sh/profile are for the old fresh-search release. Do not run them
against the Day 1 candidate. After baseline evidence and review/merge, Codex must generate
a new kit FROM the verified running image, containing exactly the changed app files from
that merged commit. Verify Git LF archive bytes, archive/image hashes, all app/data files,
unchanged migration bytes (including historical 001/002), runtime imports and appuser.
No migration is planned. Save rollback by actual image ID, not just a mutable tag.
The builder is `docs/release/build_day1_kit.py`. After target is reviewed and merged,
run from this repo (new output directory, full merged target SHA):

```powershell
cmd /d /c ".venv\Scripts\python.exe docs/release/build_day1_kit.py --base 9331869 --target MERGED_SHA --out .local/day1-release"
```

Use base 9331869 only if the fresh hashes prove the live tree matches that baseline.
The builder refuses unmerged targets, migration/deploy/data changes and deletions. It
retains the tested baseline/image/switch gates, with generated current manifests and
explicit absent states for newly added modules. Layer only on the gate's recorded image.
A concrete release paste file must be produced after these inputs exist; do not build from
EC2's stale checkout or deploy the unreviewed working tree.

Before the first reset: take a uniquely named manual RDS snapshot and wait until available.
No phone traffic during reset; let pending jobs drain first. The corrected script must be
in the deployed image. In EC2:

```bash
sudo docker exec deploy-web-1 python -m app.demo_reset --agency-id d7c5f8c7-8d83-4b41-9131-895e13d8aa21 #
```

Read and retain this dry-run report. Any non-demo property/project refuses the whole reset.
Only if its changes are expected, run:

```bash
sudo docker exec deploy-web-1 python -m app.demo_reset --agency-id d7c5f8c7-8d83-4b41-9131-895e13d8aa21 --apply #
```

Retain timestamp, release commit, and apply output (owner-approved demo audit exception;
no property_event is added). Rerun the baseline/status script. Expect 30 seeded listings,
27 ACTIVE, DH6-102=80000000, no open drafts, next new listing DH6-109. TEST-* rows may
remain withdrawn: total physical row count need not equal 30. Reset starts former/inactive
staff numbers fresh as customers; active staff keep their WhatsApp windows.

## Live test sequence and case mapping

Use `AI_PROVIDER=stub` for foundation checks; switch worker to Gemini for staff extraction
and customer visibility checks, then return to stub. Provider edits/recreation will be in
the release paste runbook; never print the rest of .env. Record UTC start time for m1_status.

| Block | Cases / action | Done when | Status |
|---|---|---|---|
| F1 M1 | Queue 3 customer texts while worker stopped, restart; staff camera photo; team voice; run m1_status then m1_replay | One 3-message turn, media stored, EXIF stripped, public card 200/private original 403, voice private, replay no new replies/counts | Pending |
| F2 roles | Customer/staff/broker/admin; broker plain `hi` | Correct routes and broker command help | Pending |
| F3 media | #demo carousel; #demo card; #demo cards; #demo fallback on/off; list >5 and coverless fixtures from CAROUSEL_RUNBOOK | 2-5 carousel cards, single, list, coverless, fallback on Android/iPhone/Web; record every shape/client | Pending |
| F4 C1-C3 | Existing S-01/02/03; appendix S-01/02/03 | Text +3 photos -> card; no write; Save -> DH6-109 ACTIVE; second Save no new listing | Pending |
| F4 C4 | Existing S-07/08; appendix S-04 | DH6-102 price 8cr ->7.8cr only after Confirm; double Confirm no-op; customer sees new price | Pending |
| F4 C5 | Existing S-12; appendix S-05 | Undo restores 8cr once; typed #undo asks confirmation before writing | Pending |
| F4 C6 | Existing S-09; appendix S-06 | DHA 6 wala ghar sold -> ACTIVE list (no DH6-106); choose109; confirm SOLD; disappears for customer | Pending |
| F4 C7 | Existing photo cases; appendix S-07 | Reply album to update card; confirm attaches all photos once; record device context behavior | Pending |
| F4 incomplete | Existing S-04/05/06 | Phase7 ghar 500gaz sale -> Missing Price/Bedrooms, no Save; Edit fills them, old buttons stale; Cancel writes nothing | Pending |
| F5 regressions | Existing new S-17..20 (appendix S-08..11) | Each fixed/still open/cut with owner and evidence | Pending |

M1 tools (replace SINCE_UTC with recorded ISO time, e.g. 2026-11-03T09:00:00+00:00):

```bash
sudo docker exec deploy-web-1 python -m app.m1_status --agency-id d7c5f8c7-8d83-4b41-9131-895e13d8aa21 --since SINCE_UTC #
sudo docker exec deploy-web-1 python -m app.m1_replay --agency-id d7c5f8c7-8d83-4b41-9131-895e13d8aa21 #
```

Retain per-case input, exact reply, before/after DB state, client and time. Customer visibility
checks require Gemini and a fresh customer conversation. Reset after staff testing and
verify the clean baseline again. Do not mark it passed from local fixture tests.

## Findings and exit

Local review findings, not live failures:

| ID | Finding | Severity / status | Owner |
|---|---|---|---|
| W-01 | Reset could rewrite non-demo inventory | High; local fix, DB tests/review/deploy/live pending | Codex + Taha review |
| W-02 | Reset preserved inactive staff's customer state | High; local fix, DB tests/review/deploy/live pending | Codex + Taha review |
| W-03 | Scenario4c included reserved candidates | Scope defect; local fix, review/deploy/live pending | Codex + Taha review |
| W-04 | Existing incomplete draft case marked Not Working | Untriaged; card unit test passes; worker/live rerun pending | Owner + Codex |

Record new live findings in the single working Weekend_Stabilisation_Plan.md table, same
session, with exact message/reply. Fix Blockers/Highs only; Mediums have named owners.
A fix is done after reviewed, merged, deployed, failing case rerun live.

- [ ] F1-F5 complete with evidence; 0 wrong writes/hidden customer listings/invented facts.
- [ ] Scenario4a/b/c, duplicate taps, Undo/#undo, Edit/Cancel/photo attachment pass live.
- [ ] Four staff findings have dispositions/owners; no Day1 Blockers/Highs remain.
- [ ] Main and deployed application match; latest reset checked and reports retained.
- [ ] Status/handoff updated; no Day2 work started.
- [ ] After testing only: stop EC2 and RDS; clear this release's S3 staging prefix.

After testing, CloudShell:

```bash
IID=$(aws ec2 describe-instances --region us-east-1 --filters Name=tag:Name,Values=manzil-demo-server Name=instance-state-name,Values=running --query Reservations[].Instances[].InstanceId --output text)
test -n "$IID" && test "$(wc -w <<< "$IID")" -eq 1 && aws ec2 stop-instances --region us-east-1 --instance-ids "$IID"
aws rds stop-db-instance --region us-east-1 --db-instance-identifier manzil-demo-db
aws rds describe-db-snapshots --region us-east-1 --db-instance-identifier manzil-demo-db --snapshot-type manual --query DBSnapshots[].DBSnapshotIdentifier --output table
```

Delete only a specific superseded manual snapshot after checking its exact name; never
automated backups or the current recovery snapshot. Concrete snapshot/staging cleanup
commands are added to the release runbook once their names are known. RDS auto-restarts
after seven days. The start commands above are the restart procedure.

## Local verification evidence (2026-11-03)

- 134 targeted staff/reset/benchmark tests passed; 57 DB tests skipped (no configured local DB).
- 4 packaging tests passed: committed bytes, checksums/new files, unmerged rejection, migration/deletion rejection.
- Ruff app/tests/deploy/new builder passed; baseline script bash syntax and git diff --check passed.
- Docker was started by owner, but sandbox user cannot access its named pipe. Owner asked to run DB checks in their terminal; results pending.
- CI/review/deployment/live checks have not run in this session.

### Owner-run database check: passed

119 passed in 116.64s (0:01:56), no skips/failures, on disposable postgres:18 at
127.0.0.1:55432 with REQUIRE_DATABASE_TESTS=1. Owner-reported command:

```powershell
cmd /d /c ".venv\Scripts\python.exe -m pytest -q tests/test_demo_reset.py tests/test_staff_commit_db.py tests/test_staff_turn_db.py tests/test_staff_update_db.py tests/test_staff_find.py"
```

This supersedes the pending DB/worker verification above. It covers incomplete-draft
worker behavior but does not close S-04 live. The prior interrupted attempt occurred before
the container existed. No extra local retest is needed without a code change or new failure.
Next gates: push, draft PR, CI, Taha review, merge, production baseline, deployment, live checks.
