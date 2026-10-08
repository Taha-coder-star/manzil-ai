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
    meta_access_token: str | None
    meta_api_version: str
    webhook_verify_token: str | None
    webhook_sender_agencies: str | None
    site_address: str | None
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
        meta_access_token=_optional("META_ACCESS_TOKEN"),
        meta_api_version=_optional("META_API_VERSION") or "v25.0",
        webhook_verify_token=_optional("WEBHOOK_VERIFY_TOKEN"),
        webhook_sender_agencies=_optional("WEBHOOK_SENDER_AGENCIES"),
        site_address=_optional("SITE_ADDRESS"),
        worker_concurrency=max(1, int(os.environ.get("WORKER_CONCURRENCY") or "2")),
        privacy_contact_email=_optional("PRIVACY_CONTACT_EMAIL"),
    )