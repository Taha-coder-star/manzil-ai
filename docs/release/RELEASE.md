# Release from a tag (audit WP3, TD-15 / TD-16)

Every production image is built from a tag on `main`, with the commit inside the image. Owner-run.
Nothing here prints a credential, a full phone number or a connection URL.

What the image carries since WP3:

| Item | Where |
|---|---|
| Commit | `docker build --build-arg GIT_SHA=...` → label `org.opencontainers.image.revision`, file `/app/REVISION`, `GET /live` → `"revision"` |
| Python packages | `requirements.lock` (exact versions with hashes), installed with `pip install --require-hashes` |
| Base images | `python:3.12-slim@sha256:...` (Dockerfile), `caddy:2@sha256:...` (`deploy/compose.yml`) |
| Migration ledger | `app/migrate.py` accepts the two recorded CRLF checksums of `001_schema_v1.sql` and `002_tenant_rls.sql` by exact value (`HISTORICAL_CHECKSUMS`); any other difference still stops the migrator |

The file-layer kit in this folder (`build_kit.sh`, `build_day1_kit.py`, `fresh_search_*.sh`,
`day1_baseline.sh`) stays until the first tag build has been verified in production; it is retired
after that, not before (TD-16). The kit cannot carry this release anyway: it refuses any change to
`Dockerfile` or `requirements.txt`.

## 0. Before the first tag release (once)

1. Merge PR #68 (WP1) and PR #69 (WP2) into `release/day13-security-staleness-baseline`, then the
   WP3 branch.
2. Pull request `release/day13-security-staleness-baseline` → `main` (no content conflict, TD-16).
   Wait for both CI checks.
3. Tag the merge commit on `main` and push the tag:
   ```bash
   git fetch origin && git checkout main && git pull --ff-only
   TAG=v2026.11.<n>                       # pick the name; one tag per release
   git tag -a "$TAG" -m "Release $TAG" && git push origin "$TAG"
   ```

## 1. Read-only ledger check (on EC2, admin `migrate.env`)

```bash
sudo docker run --rm --env-file /home/ubuntu/.config/manzil/migrate.env --entrypoint python \
  manzil-ai:latest -c "
import os, psycopg
with psycopg.connect(os.environ['DATABASE_URL']) as conn:
    conn.read_only = True
    for row in conn.execute(\"SELECT name, checksum FROM schema_migration WHERE name IN ('001_schema_v1.sql', '002_tenant_rls.sql') ORDER BY name\"):
        print(*row)"
```

Expect exactly:

```
001_schema_v1.sql ffc7c8c2759f48d2965153884e9e637d8142f0833bd7e03437373a30132d4d9d
002_tenant_rls.sql 5a98bcd6a561623df2d5ba541106a04f512c9c41916d628a7c9ae6ccb879e641
```

Any other value: stop. Do not edit the ledger and do not build; send the output.

## 2. Build from the tag (on EC2, as `ubuntu`)

```bash
TAG=v2026.11.<n>
B=/home/ubuntu/build-$TAG
git clone --depth 1 --branch "$TAG" https://github.com/Taha-coder-star/manzil-ai.git "$B"
cd "$B"
SHA=$(git rev-parse HEAD); echo "$SHA"                   # the tag's commit
git status --short                                         # empty
grep -c $'\r' migrations/*.sql | grep -v ':0$'             # no output (LF only)
sudo docker build --build-arg GIT_SHA="$SHA" -t "manzil-ai:$TAG" .
sudo docker image inspect "manzil-ai:$TAG" --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
sudo docker run --rm --entrypoint cat "manzil-ai:$TAG" /app/REVISION   # = $SHA
```

If the server cannot reach GitHub: clone in CloudShell, `git archive --format=tar.gz -o
src-$TAG.tar.gz "$TAG"`, copy it through the private S3 `staging/` prefix (the transfer used since
Task 4.4), unpack into `$B`, and use the `SHA` printed in CloudShell.

Record in the release note: tag, `$SHA`, the image ID printed above.

## 3. Before switching (all read-only)

(a) Ledger rows: section 1, same two values.

(b) Every migration in the new image against the ledger (prints names, short checksums and a
verdict; no URL, no credential):

```bash
sudo docker run --rm --env-file /home/ubuntu/.config/manzil/migrate.env --entrypoint python \
  "manzil-ai:$TAG" -c "
import os, psycopg
from app.migrate import HISTORICAL_CHECKSUMS, load_migrations
image = {n: c for n, c, _ in load_migrations()}
with psycopg.connect(os.environ['DATABASE_URL']) as conn:
    conn.read_only = True
    ledger = dict(conn.execute('SELECT name, checksum FROM schema_migration').fetchall())
bad = 0
for n in sorted(set(image) | set(ledger)):
    i, l = image.get(n), ledger.get(n)
    v = ('same' if i == l else 'allowed-crlf' if l in HISTORICAL_CHECKSUMS.get(n, ()) and i
         else 'PENDING' if l is None else 'MISSING-FILE' if i is None else 'MISMATCH')
    bad += v not in ('same', 'allowed-crlf')
    print(n, (i or '-')[:12], (l or '-')[:12], v)
print('problems:', bad)"
```

Expect `same` for every row except `allowed-crlf` for 001 and 002, and `problems: 0`. `PENDING` means
this release carries a new migration: stop, that release needs its own snapshot and migration
runbook. `MISMATCH` or `MISSING-FILE`: stop.

(c) The migrator from the new image reports nothing to apply:

```bash
sudo docker run --rm --env-file /home/ubuntu/.config/manzil/migrate.env -e PGOPTIONS='-c lock_timeout=3s' \
  --entrypoint python "manzil-ai:$TAG" -m app.migrate
```

Expect `Migrations complete: 0 applied, N skipped.` with N = the number of rows in (b). Anything
else: stop (the runner rolls back on any error).

## 4. Switch

```bash
cd /home/ubuntu/manzil-ai
PRE_ID=$(sudo docker image inspect manzil-ai:latest --format '{{.Id}}'); echo "$PRE_ID"   # record it
for c in deploy-web-1 deploy-worker-1; do sudo docker inspect $c --format '{{.Image}}'; done  # both = PRE_ID
sudo docker tag "$PRE_ID" manzil-ai:pre-$TAG
sudo docker tag "manzil-ai:$TAG" manzil-ai:latest
sudo docker compose -f deploy/compose.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker
```

`deploy/compose.yml` on the server is replaced by the tag's copy only if it differs (`diff
deploy/compose.yml $B/deploy/compose.yml`): the only WP3 change there is the caddy digest; if you
take it, recreate caddy in the same way (`... --force-recreate caddy`; a few seconds without HTTPS).

## 5. Verify

```bash
SITE=<public host, no scheme>
NEW_ID=$(sudo docker image inspect "manzil-ai:$TAG" --format '{{.Id}}')
for c in deploy-web-1 deploy-worker-1; do sudo docker inspect $c --format '{{.Image}}'; done   # both = NEW_ID
sudo docker compose -f deploy/compose.yml --env-file .env exec -T web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10).status)"   # 200
curl -fsS "https://$SITE/live"; echo                                                    # "revision": "<SHA>"
curl -s -o /dev/null -w '%{http_code}\n' -X POST "https://$SITE/webhook" -d '{}'        # 401
sudo docker compose -f deploy/compose.yml --env-file .env logs --since 3m worker | grep 'Worker started'
```

Then send one customer message from a test phone and check the reply arrives.

## 6. Roll back (any check in section 5 fails)

```bash
sudo docker tag "$PRE_ID" manzil-ai:latest
sudo docker compose -f deploy/compose.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker
for c in deploy-web-1 deploy-worker-1; do sudo docker inspect $c --format '{{.Image}}'; done   # both = PRE_ID
```

Roll back by the recorded ID, never by a tag name. No migration ran (section 3c applied nothing),
so the database needs no rollback.

## 7. Full acceptance (owner, first tag release)

- A second person clones the same tag on another machine and builds it with the same `GIT_SHA`:
  the installed packages (`docker run --rm --entrypoint pip <image> freeze`) and `/app/migrations`
  checksums equal those of the server build.
- Section 3 (b) and (c) against production, or against a database restored from the latest
  snapshot: `problems: 0`, then `0 applied`.
- `/live` in production shows the tag's commit.
- After that, the file-layer kit in `docs/release/` can be retired by its own change.

## 8. Refreshing the lock and the digests

The lock holds the versions the test suite ran on. To move them:

```bash
# throwaway container, nothing installed on the host; run from the repository root
docker run --rm -v "$PWD:/w" -w /w python:3.12-slim@sha256:<current Dockerfile digest> sh -c \
  'pip install -q pip-tools && pip-compile --generate-hashes --strip-extras --no-emit-index-url \
   -o requirements.lock requirements.txt'
```

WP3 generated it with an extra `-c tested-freeze.txt` (the `pip freeze` of the image the full
suite ran on), so every pin equals a tested version; do the same when you want that guarantee
(`-U` or `-P <package>==<version>` moves versions on purpose). Then build, run the full suite in an
image built from the new lock, and commit `requirements.lock` with `requirements.txt`.

Digests:

```bash
docker buildx imagetools inspect python:3.12-slim   # "Digest:" line = the multi-arch index digest
docker buildx imagetools inspect caddy:2
```

Replace the `@sha256:` part in `Dockerfile` / `deploy/compose.yml`, keep the tag in front for
readability, rebuild and rerun the suite. CI's `postgres:18` service stays unpinned.

## 9. After the release: stop the servers and delete this release's temporary snapshots

AWS CloudShell, identifiers as recorded in `docs/PROJECT_STATE.md` (check them first):

```bash
REGION=us-east-1; EC2_ID=i-05b165a0e4c9bf4a7; DB_ID=manzil-demo-db
aws ec2 stop-instances --region $REGION --instance-ids $EC2_ID
aws ec2 wait instance-stopped --region $REGION --instance-ids $EC2_ID
aws rds stop-db-instance --region $REGION --db-instance-identifier $DB_ID
aws rds describe-db-instances --region $REGION --db-instance-identifier $DB_ID --query 'DBInstances[0].DBInstanceStatus'   # stopping -> stopped
# A tag release without a migration needs no snapshot. Only for one you took for this release,
# once the release is accepted:
# aws rds delete-db-snapshot --region $REGION --db-snapshot-identifier <snapshot-name>
```

AWS starts a stopped RDS instance again by itself after 7 days.

## 10. Bring production back

```bash
REGION=us-east-1; EC2_ID=i-05b165a0e4c9bf4a7; DB_ID=manzil-demo-db
aws rds start-db-instance --region $REGION --db-instance-identifier $DB_ID
aws rds wait db-instance-available --region $REGION --db-instance-identifier $DB_ID
aws ec2 start-instances --region $REGION --instance-ids $EC2_ID
aws ec2 wait instance-status-ok --region $REGION --instance-ids $EC2_ID
aws ec2 describe-instances --region $REGION --instance-ids $EC2_ID --query 'Reservations[].Instances[].PublicIpAddress'
# if the public IP changed, SITE_ADDRESS and the Meta webhook URL must follow it; then on the instance:
cd /home/ubuntu/manzil-ai && sudo docker compose -f deploy/compose.yml --env-file .env up -d web worker caddy
# and repeat the section 5 checks
```
