import os
import re
from dataclasses import dataclass, field

from app.llm import DEFAULT_THINKING_LEVEL, THINKING_LEVELS


@dataclass(frozen=True)
class Settings:
    port: int
    database_url: str | None
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint_url: str | None
    media_public_base_url: str | None
    meta_app_secret: str | None
    meta_access_token: str | None
    meta_api_version: str
    webhook_verify_token: str | None
    webhook_sender_agencies: str | None
    site_address: str | None
    worker_concurrency: int
    privacy_contact_email: str | None
    # Task 6.1 AI turn engine (C6). "stub" keeps the Task 5.1 stub replies; "gemini"
    # must be selected explicitly and then needs the key and both model IDs.
    ai_provider: str = "stub"
    gemini_api_key: str | None = field(default=None, repr=False)
    llm_primary_model: str | None = None
    llm_failover_model: str | None = None
    # Thinking level for every call; None (LLM_THINKING_LEVEL=off) leaves it out of the
    # request, a kill switch if a model rejects the field.
    llm_thinking_level: str | None = DEFAULT_THINKING_LEVEL


AI_PROVIDERS = ("stub", "gemini")
# A model ID becomes part of the request path, so only plain ID characters pass.
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9.-]{1,63}")


class ConfigurationError(Exception):
    """A configuration problem; the message names settings, never their values."""


def check_ai_settings(settings):
    """Raise ConfigurationError unless the AI settings are usable. An explicit
    AI_PROVIDER=gemini never falls back to the stub when something is missing."""
    provider = getattr(settings, "ai_provider", "stub")
    if provider not in AI_PROVIDERS:
        raise ConfigurationError("AI_PROVIDER must be one of: " + ", ".join(AI_PROVIDERS))
    if provider == "stub":
        return
    missing = [name for name, value in (
        ("GEMINI_API_KEY", settings.gemini_api_key),
        ("LLM_PRIMARY_MODEL", settings.llm_primary_model),
        ("LLM_FAILOVER_MODEL", settings.llm_failover_model),
    ) if not value]
    if missing:
        raise ConfigurationError("AI_PROVIDER=gemini requires " + ", ".join(missing))
    for name, value in (("LLM_PRIMARY_MODEL", settings.llm_primary_model),
                        ("LLM_FAILOVER_MODEL", settings.llm_failover_model)):
        if not MODEL_ID.fullmatch(value):
            raise ConfigurationError(f"{name} is not a valid model ID")
    level = getattr(settings, "llm_thinking_level", DEFAULT_THINKING_LEVEL)
    if level is not None and level not in THINKING_LEVELS:
        raise ConfigurationError("LLM_THINKING_LEVEL must be one of: "
                                 + ", ".join((*THINKING_LEVELS, THINKING_OFF)))


THINKING_OFF = "off"


def thinking_level_setting():
    """LLM_THINKING_LEVEL: unset or empty -> the default (low); 'off' -> None."""
    value = (os.environ.get("LLM_THINKING_LEVEL") or "").strip().lower()
    if not value:
        return DEFAULT_THINKING_LEVEL
    return None if value == THINKING_OFF else value


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
        media_public_base_url=_optional("MEDIA_PUBLIC_BASE_URL"),
        meta_app_secret=_optional("META_APP_SECRET"),
        meta_access_token=_optional("META_ACCESS_TOKEN"),
        meta_api_version=_optional("META_API_VERSION") or "v25.0",
        webhook_verify_token=_optional("WEBHOOK_VERIFY_TOKEN"),
        webhook_sender_agencies=_optional("WEBHOOK_SENDER_AGENCIES"),
        site_address=_optional("SITE_ADDRESS"),
        worker_concurrency=max(1, int(os.environ.get("WORKER_CONCURRENCY") or "2")),
        privacy_contact_email=_optional("PRIVACY_CONTACT_EMAIL"),
        ai_provider=(_optional("AI_PROVIDER") or "stub").lower(),
        gemini_api_key=_optional("GEMINI_API_KEY"),
        llm_primary_model=_optional("LLM_PRIMARY_MODEL"),
        llm_failover_model=_optional("LLM_FAILOVER_MODEL"),
        llm_thinking_level=thinking_level_setting(),
    )