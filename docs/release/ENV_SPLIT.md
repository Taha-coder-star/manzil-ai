# Proposed: one env file per container (audit MA-014, TD-18)

**Status: proposal only. Not applied.** `deploy/compose.yml` still gives web and worker the same
`../.env`. Nobody read or wrote a real `.env` to prepare this; the lists come from the code
(2026-11-13, branch `fix/wp2-ops-quick-wins`).

## Why

Today the web container, the only one reachable from the internet, also holds the Meta send
token and the Gemini and ElevenLabs keys it never uses. A bug in web would expose them. Each
container should hold only what it reads.

## What each container reads

From `app/config.py::load_settings` and the direct `os.environ` reads in `app/`, followed per
role: web = `app/web.py` (privacy, health, webhook verify and receive, ingest, routing);
worker = `app/worker.py`, `app/handlers.py` and the modules they call.

| Variable | web | worker | Read by |
|---|---|---|---|
| `DATABASE_URL` | yes | yes | ingest; every job |
| `WEBHOOK_SENDER_AGENCIES` | yes | yes | sender routing; the worker's agency list |
| `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL` | yes | yes | web: `/health` bucket check; worker: media, audio |
| `META_APP_SECRET` | yes | no | webhook signature check |
| `WEBHOOK_VERIFY_TOKEN` | yes | no | webhook GET verification |
| `PRIVACY_CONTACT_EMAIL` | yes | no | `/privacy` |
| `PORT` | yes | no | uvicorn (default 8000; Caddy proxies `web:8000`) |
| `META_ACCESS_TOKEN`, `META_API_VERSION` | no | yes | sending messages |
| `WORKER_CONCURRENCY` | no | yes | worker threads |
| `MEDIA_PUBLIC_BASE_URL` | no | yes | public card image URLs |
| `SITE_ADDRESS` | no (Caddy gets it by interpolation) | yes | privacy link in messages; Caddy site address |
| `AI_PROVIDER`, `GEMINI_API_KEY`, `LLM_PRIMARY_MODEL`, `LLM_FAILOVER_MODEL`, `LLM_THINKING_LEVEL` | no | yes | turn engine, L3 summary, STT fallback |
| `STT_PROVIDER`, `STT_FALLBACK`, `ELEVENLABS_API_KEY` | no | yes | speech |
| `HANDOFF_ACCEPT_MINUTES`, `LEAD_ALERT_TEMPLATE`, `LEAD_ALERT_LANGUAGE`, `LEAD_ALERT_PARAMS` | no | yes | handoff timers and broker alert |
| `META_APP_ID`, `META_WABA_ID`, `META_PHONE_NUMBER_ID` | no | no | operator tools only (`provision_agency`), not the runtime |

Not affected: the `DAY13_*` variables of `deploy/compose.day13-budget.yml` are set by that file's
own `environment:` block for both services. S3 access uses the instance's AWS credentials chain,
not variables listed here; if the real `.env` holds `AWS_*` variables, both containers that use
S3 need them (decide after the names check below).

## Proposed `deploy/compose.yml` change (not applied)

```diff
   web:
     build: ..
     image: manzil-ai:latest
     command: ["web"]
-    env_file: ../.env
+    env_file: ../.env.web
     restart: unless-stopped

   worker:
     image: manzil-ai:latest
     pull_policy: never
     command: ["worker"]
-    env_file: ../.env
+    env_file: ../.env.worker
     restart: unless-stopped
```

`.env` stays: `docker compose --env-file .env` still uses it for interpolation (Caddy's
`SITE_ADDRESS`) and the release scripts pass it.

## Order on the server (owner, EC2, in `/home/ubuntu/manzil-ai`)

Nothing below prints a value.

1. Names only, to see whether the real file holds anything the table does not list:
   ```bash
   grep -v '^\s*#' .env | grep '=' | cut -d= -f1 | sort -u
   ```
   Any name not in the table: decide which container needs it before going on.
2. Create the two files from `.env`, readable by the owner only:
   ```bash
   umask 077
   grep -E '^(DATABASE_URL|WEBHOOK_SENDER_AGENCIES|S3_BUCKET|S3_REGION|S3_ENDPOINT_URL|META_APP_SECRET|WEBHOOK_VERIFY_TOKEN|PRIVACY_CONTACT_EMAIL|PORT)=' .env > .env.web
   grep -E '^(DATABASE_URL|WEBHOOK_SENDER_AGENCIES|S3_BUCKET|S3_REGION|S3_ENDPOINT_URL|META_ACCESS_TOKEN|META_API_VERSION|WORKER_CONCURRENCY|MEDIA_PUBLIC_BASE_URL|SITE_ADDRESS|AI_PROVIDER|GEMINI_API_KEY|LLM_PRIMARY_MODEL|LLM_FAILOVER_MODEL|LLM_THINKING_LEVEL|STT_PROVIDER|STT_FALLBACK|ELEVENLABS_API_KEY|HANDOFF_ACCEPT_MINUTES|LEAD_ALERT_TEMPLATE|LEAD_ALERT_LANGUAGE|LEAD_ALERT_PARAMS)=' .env > .env.worker
   cut -d= -f1 .env.web; echo ---; cut -d= -f1 .env.worker      # names only
   ```
3. Back up the compose file, apply the diff above, then
   `docker compose -f deploy/compose.yml --env-file .env config >/dev/null && echo valid`.
4. `docker compose -f deploy/compose.yml --env-file .env up -d --no-deps --no-build --force-recreate web worker`
5. Verify: worker log `Worker started`, internal health 200 (`docs/RELEASE_10_2.md` §8), public
   `/live` 200, `POST /webhook` 401, one customer message answered; and that web holds no send
   or provider key:
   `docker compose -f deploy/compose.yml --env-file .env exec -T web sh -c 'env | cut -d= -f1 | grep -E "META_ACCESS_TOKEN|GEMINI|ELEVENLABS" || echo none'`
6. Rollback: restore the backed-up compose file and repeat step 4. `.env` was never changed.

Keep `.env`, `.env.web` and `.env.worker` in step: a changed secret must be changed in each file
that holds it.
