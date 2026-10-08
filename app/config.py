import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    port: int
    database_url: str | None
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint_url: str | None
    meta_app_secret: str | None
    webhook_verify_token: str | None
    webhook_sender_agencies: str | None
    worker_concurrency: int
    privacy_contact_email: str | None


def _optional(name):
    value = os.environ.get(name, "").strip()
    return value or None


def load_settings():
    return Settings(
        port=int(os.environ.get("PORT", "8000")),
        database_url=_optional("DATABASE_URL"),
        s3_bucket=_optional("S3_BUCKET"),
        s3_region=_optional("S3_REGION"),
        s3_endpoint_url=_optional("S3_ENDPOINT_URL"),
        meta_app_secret=_optional("META_APP_SECRET"),
        webhook_verify_token=_optional("WEBHOOK_VERIFY_TOKEN"),
        webhook_sender_agencies=_optional("WEBHOOK_SENDER_AGENCIES"),
        worker_concurrency=max(1, int(os.environ.get("WORKER_CONCURRENCY") or "2")),
        privacy_contact_email=_optional("PRIVACY_CONTACT_EMAIL"),
    )