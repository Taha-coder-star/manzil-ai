import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    port: int
    database_url: str | None
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint_url: str | None


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
    )
