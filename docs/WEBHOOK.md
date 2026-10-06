# WhatsApp webhook (Tasks 2.3 and 2.4)

Callback URL: `https://<SITE_ADDRESS>/webhook` (demo: `https://98-85-18-90.sslip.io/webhook`).
Caddy already proxies every path to `web:8000`; no Caddyfile change is needed.

## Configuration (values only in the private server `.env`)

- `WEBHOOK_VERIFY_TOKEN`: random string you choose; enter the same value in the Meta dashboard.
  Separate from the app secret and the access token.
- `META_APP_SECRET`: Meta app secret, used only for `X-Hub-Signature-256`.
- `WEBHOOK_SENDER_AGENCIES`: `phone_number_id=agency_uuid[,phone_number_id=agency_uuid]` (2.4 routing).
- `DATABASE_URL`: the runtime `manzil_app` login (already set by Task 2.2).

## Behaviour

- `GET /webhook`: `hub.mode=subscribe` and a matching `hub.verify_token` (constant-time
  compare) return `hub.challenge` as `text/plain` 200. Anything else, including an unset
  token, returns 403. A uvicorn access-log filter redacts the verify token. Meta sends every parameter twice (`hub.verify_token` and `hub_verify_token`); both forms, and `hub%2Everify_token`, are redacted.
- `POST /webhook`, in order:
  1. no `META_APP_SECRET`: 503 (fail closed; Meta retries);
  2. body over 1 MB: 413;
  3. missing, malformed (not `sha256=<64 hex>`) or wrong signature: 401. HMAC-SHA256 is
     computed over the raw request bytes before any JSON parsing, so nothing is stored;
  4. authentic but not a `whatsapp_business_account` JSON object: 200 `ignored` (a retry
     cannot fix it);
  5. otherwise every supported event is persisted (below), then 200 `received`.
- Logs contain outcomes and counts only; never tokens, signatures or payloads.

## Persistence (Task 2.4)

**Routing.** For each `entry[].changes[]` with `field == "messages"`, the receiving
`value.metadata.phone_number_id` is resolved through `EnvSenderResolver` (`app/routing.py`)
from `WEBHOOK_SENDER_AGENCIES`, **before** any tenant transaction. Inside
`tenant_transaction`, `verify_agency` re-checks the agency row: the same
`meta_phone_number_id` and `is_active`. The resolver is a protocol, so a DB or platform
resolver can replace it later without changing the pipeline. No RLS exceptions are used.

**Keys** (stable across Meta retries):

| Event | `inbound_event.event_key` | `job.dedupe_key` | `job.job_type` |
|---|---|---|---|
| Incoming message (any type) | `wa:msg:{wamid}` | `process_message:{wamid}` | `process_message` |
| Status update | `wa:status:{wamid}:{status}` | `reconcile_status:{wamid}:{status}` | `reconcile_status` |

sent, delivered, read and failed share a WAMID, so the status is part of the key. Status
jobs have their own type, so they never enter the message / AI path (Task 3.2 consumes them).

**Atomicity.** One SQL statement per event inserts the event with
`ON CONFLICT (agency_id, event_key) DO NOTHING` and inserts the job only from the
newly inserted event row (`ON CONFLICT (agency_id, dedupe_key) DO NOTHING`). There is no
check-then-insert step. A concurrent duplicate waits on the unique index and then does
nothing. An event without its job raises and rolls back. 200 is returned only after commit.

**Stored payload.** The single message or status object, `metadata`, and the matching
`contacts[]` entry; media IDs stay inside the message object. Not the whole envelope.

**Responses**

| Case | Response | Effect |
|---|---|---|
| Stored, or a duplicate of a stored event | 200 | Exactly one event + job per key |
| Unknown `phone_number_id` | 200 | Dropped and logged (last 4 digits only); never a default agency |
| Mapped agency is inactive | 200 | Dropped and logged |
| Mapped agency row missing or owns a different sender | 503 | Configuration error; Meta retries |
| `WEBHOOK_SENDER_AGENCIES` / `DATABASE_URL` unset or malformed | 503 | Meta retries |
| Database error (job insert, timeout, connection) | 503 | Transaction rolled back; Meta retries |
| Non-`messages` fields; items without `id` / `status` | 200 | Skipped and counted in the log |

**Batches and mixed tenants.** Events are grouped per agency; each group commits in its own
transaction. If any group fails, the response is 503. Meta then retries the whole
envelope, and groups that already committed dedupe through the keys.

**Provisioning.** FORCE RLS blocks even the table owner, so the agency row is created with
the runtime login inside a tenant transaction for a new id:

```sh
sudo docker compose -f deploy/compose.yml --env-file .env exec -T web   python -m app.provision_agency --name "Manzil Demo Realty (DEMO)" --slug manzil-demo
```

It reads `META_PHONE_NUMBER_ID` / `META_WABA_ID` from the container environment and prints
the `WEBHOOK_SENDER_AGENCIES=...` line to add to `.env`.

## Deploy and Meta verification (owner, with approval)

1. Generate a token privately (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
   and add `WEBHOOK_VERIFY_TOKEN=` to the server `.env`. Confirm `META_APP_SECRET` is set.
2. Transfer the changed files (`app/config.py`, `app/web.py`, `app/webhook.py`) with the
   selective-archive workflow in `docs/RLS_DEPLOYMENT.md`, rebuild, and recreate web and worker.
   No migration is needed.
3. Check from outside: a GET with a wrong token gives 403, and an unsigned POST gives 401.
4. Meta dashboard: App, then WhatsApp, then Configuration, then Webhook. Set the callback URL
   and verify token, then **Verify and save**. After Task 2.4 is deployed and provisioned, subscribe the `messages` field and send a test message.

## Operational notes

- The verify token is never the Meta access token (`EAA...`). Entering the access token there sends it to our server in the URL.
- While the Meta app is unpublished, Meta delivers only dashboard **Test** webhooks. Its sample uses a fake `phone_number_id`, so the expected log line is `stored=0 ... unknown_sender=1`. Real messages need the app published.
