import json
import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from app import health, webhook
from app.config import load_settings
from app.ingest import IngestError, ingest_events
from app.privacy import render_privacy_page
from app.routing import EnvSenderResolver, RoutingConfigError

logger = logging.getLogger(__name__)
logging.getLogger("uvicorn.access").addFilter(webhook.RedactVerifyTokenFilter())

app = FastAPI(title="Manzil AI")


@app.get("/live")
def live():
    return {"status": "running", "role": "web"}


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return render_privacy_page(load_settings().privacy_contact_email)


@app.get("/health")
def health_check():
    settings = load_settings()
    checks = {
        "database": health.check_database(settings.database_url),
        "bucket": health.check_bucket(
            settings.s3_bucket, settings.s3_region, settings.s3_endpoint_url
        ),
    }
    healthy = all(result == "ok" for result in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy", "checks": checks},
    )


@app.get("/webhook")
def webhook_verify(request: Request):
    settings = load_settings()
    params = request.query_params
    challenge = webhook.verify_challenge(
        params.get("hub.mode"),
        params.get("hub.verify_token"),
        params.get("hub.challenge"),
        settings.webhook_verify_token,
    )
    if challenge is None:
        if not settings.webhook_verify_token:
            logger.error("Webhook verification rejected: WEBHOOK_VERIFY_TOKEN not set")
        else:
            logger.warning("Webhook verification rejected")
        return Response(status_code=403)
    return PlainTextResponse(challenge)


@app.post("/webhook")
async def webhook_receive(request: Request):
    settings = load_settings()
    if not settings.meta_app_secret:
        # Fail closed and let Meta retry once configuration is fixed.
        logger.error("Webhook POST rejected: META_APP_SECRET not set")
        return Response(status_code=503)

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > webhook.MAX_BODY_BYTES:
        return Response(status_code=413)
    raw_body = await request.body()
    if len(raw_body) > webhook.MAX_BODY_BYTES:
        return Response(status_code=413)

    if not webhook.verify_signature(
        settings.meta_app_secret, raw_body, request.headers.get(webhook.SIGNATURE_HEADER)
    ):
        logger.warning("Webhook POST rejected: invalid or missing signature")
        return Response(status_code=401)

    try:
        envelope = json.loads(raw_body)
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or envelope.get("object") != "whatsapp_business_account":
        # Authentic but unusable; a retry would not fix it.
        logger.warning("Webhook POST ignored: unsupported payload")
        return JSONResponse({"status": "ignored"})

    events, skipped = webhook.extract_events(envelope)
    if skipped:
        logger.info("Webhook POST: %d unsupported items skipped", skipped)
    if not events:
        return JSONResponse({"status": "received"})

    try:
        if not settings.database_url:
            raise RoutingConfigError("DATABASE_URL is not set")
        resolver = EnvSenderResolver.from_config(settings.webhook_sender_agencies)
        result = await run_in_threadpool(
            ingest_events, settings.database_url, resolver, events
        )
    except (RoutingConfigError, IngestError, psycopg.Error) as exc:
        # Not durably accepted: answer non-2xx so Meta retries. Type only;
        # psycopg messages can include connection details.
        logger.error("Webhook POST not persisted: %s", type(exc).__name__)
        return Response(status_code=503)

    logger.info(
        "Webhook POST persisted: stored=%d duplicate=%d unknown_sender=%d inactive=%d",
        result.stored, result.duplicate, result.unknown_sender, result.inactive_agency,
    )
    return JSONResponse({"status": "received"})
