# Task 10.2 release runbook (prepared, not run)

Status: prepared locally; **no production step has been run**. Owner-run after the PR is reviewed and
merged and the owner approves the 10.2 policy and wording (docs/VOICE_TURN.md). Production stays on
`manzil-ai:10.1-7f5cd3d` until then.

| Item | Value |
|---|---|
| Base / rollback image | `manzil-ai:10.1-7f5cd3d`, ID `sha256:5eb0a158c2dfd3b598b5e29820a7700956e990b2d7ee6896abd97bd5c223b7a8` |
| Base commit | `7f5cd3d` (10.1 release, merged PR #41) |
| Release image | `manzil-ai:10.2-<M7>`, where `<M>` is the exact merge commit of the 10.2 PR on `main` |
| Migrations | none (RDS keeps 001-017; checksums compared read-only, step 6) |
| Caddy | unchanged (`caddy:2`, not recreated) |

## 1. Runtime changes relative to 7f5cd3d (re-check at release time)

Computed on the 10.2 branch (`git diff --name-status 7f5cd3d`): the only runtime files that change
are six modified Python modules:

```
M app/ai_tools.py   M app/customer_tools.py   M app/normalise.py
M app/render.py     M app/requirements.py     M app/turn_engine.py
```

No change to `requirements.txt` (dependencies), `Dockerfile`, `migrations/`, `demo/` (assets and
data), `deploy/` or any other runtime path; no deletion, no new runtime file, no new import outside
`app` (requirements.py now imports the existing `app.cards`). Tests and docs are not in the image.
So the release is a layer on the 10.1 image that replaces these six files.

**Gate before building (local, after merge):** with `M` the merge commit,

```
git fetch origin
git diff --name-status 7f5cd3d M -- app migrations requirements.txt Dockerfile demo deploy .dockerignore
```

must list exactly the six `M app/...` lines above. Anything else (another PR merged in between, a
dependency, a migration, an asset, a deletion) stops this runbook: plan a full rebuild instead.

## 2. Release context (local, from the exact merge commit; never the E:\manzilai checkout)

The main checkout has CRLF working copies (docs/PROJECT_STATE.md); `core.autocrlf=true` would also
convert LF in a plain `git archive` on Windows. Archive committed bytes with autocrlf off:

```
M=<merge commit>; S=${M:0:7}
git -c core.autocrlf=false archive --format=tar --prefix=release-10-2/ -o manzil-10.2-$S.tar "$M" \
  app/ai_tools.py app/customer_tools.py app/normalise.py app/render.py app/requirements.py \
  app/turn_engine.py
# expected checksums straight from the Git blobs (byte-exact)
for f in app/ai_tools.py app/customer_tools.py app/normalise.py app/render.py \
         app/requirements.py app/turn_engine.py; do
  printf '%s  %s\n' "$(git -c core.autocrlf=false show "$M:$f" | sha256sum | cut -d' ' -f1)" "$f"
done > SHA256SUMS
tar -xOf manzil-10.2-$S.tar release-10-2/app/requirements.py | sha256sum   # equals its SHA256SUMS line
sha256sum manzil-10.2-$S.tar > manzil-10.2-$S.tar.sha256
```

`Dockerfile.release` (written next to the tar, not committed):

```
FROM manzil-ai:10.1-7f5cd3d
COPY release-10-2/app/ /app/app/
```

## 3. Transfer (established route: CloudShell -> private S3 staging -> EC2)

1. Upload `manzil-10.2-$S.tar`, `manzil-10.2-$S.tar.sha256`, `SHA256SUMS`, `Dockerfile.release`
   with the CloudShell upload.
2. CloudShell: `aws s3 cp <file> s3://<bucket>/staging/release-10-2/` for each (private prefix; only
   `card/*` is public). Never `tenant/` or `card/`.
3. EC2 (Instance Connect, inside `/home/ubuntu/release-10-2/`): `aws s3 cp` each object down;
   `sha256sum -c manzil-10.2-$S.tar.sha256`; `tar -xf manzil-10.2-$S.tar`;
   `sha256sum -c SHA256SUMS` from inside `release-10-2/` (every line OK).
4. Delete the four `staging/release-10-2/` objects once the image is built.

## 4. Build on EC2 (no service touched yet)

```
docker image inspect manzil-ai:10.1-7f5cd3d --format '{{.Id}}'   # must be sha256:5eb0a158...b7a8
docker build -f Dockerfile.release -t manzil-ai:10.2-$S .
```

## 5. Image checks

```
# the six files are the merged bytes
docker run --rm --entrypoint sh -v "$PWD/SHA256SUMS:/tmp/SHA256SUMS:ro" manzil-ai:10.2-$S \
  -c 'cd /app && sha256sum -c /tmp/SHA256SUMS'
# nothing else differs from the base image
for i in manzil-ai:10.1-7f5cd3d manzil-ai:10.2-$S; do
  docker run --rm --entrypoint sh $i -c 'cd /app && find app migrations demo -type f \
    -not -name "*.pyc" | sort | xargs sha256sum' > "files-$(echo $i | tr ':/' '__').txt"
done
diff files-manzil-ai_10.1-7f5cd3d.txt files-manzil-ai_10.2-$S.txt   # only the six app/ lines
# imports, as appuser
docker run --rm --entrypoint python manzil-ai:10.2-$S -c "import app.web, app.worker; print('ok')"
docker run --rm --entrypoint id manzil-ai:10.2-$S                   # uid of appuser, not root
```

## 6. Migration checksums (read-only)

```
docker run --rm --env-file /home/ubuntu/.config/manzil/migrate.env --entrypoint python \
  manzil-ai:10.2-$S -c "
import os, psycopg
from app.migrate import load_migrations
image = {n: c for n, c, _ in load_migrations()}
with psycopg.connect(os.environ['DATABASE_URL']) as conn:
    rds = dict(conn.execute('SELECT name, checksum FROM schema_migration').fetchall())
print(len(image), len(rds), sorted(n for n in image if rds.get(n) != image[n]))"
```

Expect `17 17 []`. Any mismatch or missing name stops the release (do not run the migrator; never
edit migration files). Print no URLs or credentials.

## 7. Switch (web and worker only; Caddy untouched)

```
cd /home/ubuntu/manzil-ai
docker image inspect manzil-ai:latest --format '{{.Id}}'   # record; must be the 10.1 image ID
docker tag manzil-ai:latest manzil-ai:pre-10.2             # rollback tag
docker tag manzil-ai:10.2-$S manzil-ai:latest
docker compose -f deploy/compose.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker
docker compose -f deploy/compose.yml --env-file .env ps     # web, worker on the new image; caddy unchanged
```

## 8. Health and worker checks

- `curl -fsS https://<SITE_ADDRESS>/health` returns OK.
- `docker compose ... logs --since 5m worker`: `Worker started: ...` with the job types, no
  `Worker not started` (exit 78) and no gazetteer error.
- `docker compose ... logs --since 5m web`: no traceback.
- No secrets, phone numbers or transcripts in the logs read.

## 9. Acceptance: customer B, scenario 2

docs/VOICE_TURN.md "Live acceptance": clean start (no stored requirements, no open question, no
open type / size / budget question, i.e. a fresh number or a reset done by the owner's own
procedure), one ~8 s Roman Urdu voice note, the read-only checks of transcript, slots and shown
codes, and the latency query (receipt -> first reply <= 10 s). Record the result; M3 is not claimed
until it passes on the phone.

## 10. Rollback to 10.1

No schema or data migration to undo. 10.1 ignores the extra JSON keys 10.2 writes
(`confirmed_by`, `uncertain`, the `echo` call entries, the new ask details):

```
docker tag manzil-ai:pre-10.2 manzil-ai:latest
docker compose -f deploy/compose.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker
```

then steps 8's checks. Keep `manzil-ai:10.1-7f5cd3d`, `manzil-ai:pre-10.2`, the release archives and
the RDS snapshot `manzil-pre-10-1-20261015-1623` until 10.2 is accepted.
