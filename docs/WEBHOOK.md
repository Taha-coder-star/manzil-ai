# WhatsApp webhook (Task 2.3)

Callback URL: `https://<SITE_ADDRESS>/webhook` (demo: `https://98-85-18-90.sslip.io/webhook`).
Caddy already proxies every path to `web:8000`; no Caddyfile change is needed.

## Configuration (values only in the private server `.env`)

- `WEBHOOK_VERIFY_TOKEN`: random string you choose; enter the same value in the Meta dashboard.
  Separate from the app secret and the access token.
- `META_APP_SECRET`: Meta app secret, used only for `X-Hub-Signature-256`.

## Behaviour

- `GET /webhook`: `hub.mode=subscribe` and a matching `hub.verify_token` (constant-time
  compare) return `hub.challenge` as `text/plain` 200. Anything else, including an unset
  token, returns 403. A uvicorn access-log filter redacts `hub.verify_token`.
- `POST /webhook`, in order:
  1. no `META_APP_SECRET`: 503 (fail closed; Meta retries);
  2. body over 1 MB: 413;
  3. missing, malformed (not `sha256=<64 hex>`) or wrong signature: 401. HMAC-SHA256 is
     computed over the raw request bytes before any JSON parsing, so nothing is stored;
  4. authentic but not a `whatsapp_business_account` JSON object: 200 `ignored` (a retry
     cannot fix it);
  5. otherwise 200 `received`. **Task 2.4 adds persistence; until then, events are
     authenticated and discarded.** Do not point real traffic at it before 2.4.
- Logs contain outcomes and counts only; never tokens, signatures or payloads.

## Deploy and Meta verification (owner, with approval)

1. Generate a token privately (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
   and add `WEBHOOK_VERIFY_TOKEN=` to the server `.env`. Confirm `META_APP_SECRET` is set.
2. Transfer the changed files (`app/config.py`, `app/web.py`, `app/webhook.py`) with the
   selective-archive workflow in `docs/RLS_DEPLOYMENT.md`, rebuild, and recreate web and worker.
   No migration is needed.
3. Check from outside: a GET with a wrong token gives 403, and an unsigned POST gives 401.
4. Meta dashboard: App, then WhatsApp, then Configuration, then Webhook. Set the callback URL
   and verify token, then **Verify and save**. Subscribe the `messages` field after 2.4 is deployed.
