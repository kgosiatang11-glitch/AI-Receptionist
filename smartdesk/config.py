"""Configuration for the SmartDesk AI platform.

Secrets are read from the environment only.  Nothing in this module is ever
serialised to the frontend; the API exposes derived, non-sensitive status
values instead (see ``smartdesk.api.channels``).
"""

from __future__ import annotations

import os


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class Config:
    """Base configuration shared by every environment."""

    # --- Database -------------------------------------------------------
    # Supabase connection string, e.g.
    #   postgresql+psycopg://postgres.<ref>:<password>@<host>:5432/postgres
    # When unset the application runs in LEGACY FILE MODE so that the existing
    # single-tenant deployment keeps working untouched.
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }

    # --- Supabase Auth ---------------------------------------------------
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
    SUPABASE_JWT_AUDIENCE = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
    SUPABASE_JWKS_URL = os.getenv("SUPABASE_JWKS_URL", "")

    # --- Platform --------------------------------------------------------
    PLATFORM_ADMIN_EMAILS = tuple(
        email.strip().lower()
        for email in os.getenv("PLATFORM_ADMIN_EMAILS", "").split(",")
        if email.strip()
    )
    DASHBOARD_ORIGINS = tuple(
        origin.strip()
        for origin in os.getenv(
            "DASHBOARD_ORIGINS", "http://localhost:5173"
        ).split(",")
        if origin.strip()
    )

    # --- Twilio ----------------------------------------------------------
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    # Signature validation is ON unless explicitly disabled (tests disable it).
    TWILIO_VALIDATE_SIGNATURE = _bool("TWILIO_VALIDATE_SIGNATURE", True)
    TWILIO_WEBHOOK_BASE_URL = os.getenv("TWILIO_WEBHOOK_BASE_URL", "")

    # --- OpenAI ----------------------------------------------------------
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "10"))

    # --- Guardrails ------------------------------------------------------
    MAX_INBOUND_MESSAGE_CHARS = _int("MAX_INBOUND_MESSAGE_CHARS", 1500)
    MAX_CONVERSATION_HISTORY = _int("MAX_CONVERSATION_HISTORY", 20)
    DEFAULT_MONTHLY_CONVERSATION_LIMIT = _int("MONTHLY_CONVERSATION_LIMIT", 500)

    # --- Legacy single-tenant fallback ----------------------------------
    STATE_DIR = os.getenv("STATE_DIR", ".")
    LEGACY_BUSINESS_NAME = os.getenv("BUSINESS_NAME", "SmartDesk AI")
    LEGACY_OWNER_WHATSAPP = os.getenv("OWNER_WHATSAPP", "")
    LEGACY_TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "")

    # --- Voice -----------------------------------------------------------
    VOICE_LISTEN_TIMEOUT_SECONDS = _int("VOICE_LISTEN_TIMEOUT_SECONDS", 30)
    VOICE_SPEECH_TIMEOUT_SECONDS = os.getenv("VOICE_SPEECH_TIMEOUT_SECONDS", "auto")
    TWILIO_VOICE_LANGUAGE = os.getenv("TWILIO_VOICE_LANGUAGE", "en-US")
    VOICE_MAX_SILENCE_REPROMPTS = _int("VOICE_MAX_SILENCE_REPROMPTS", 2)

    # --- Google Calendar (per-tenant OAuth) -----------------------------
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "")
    # Signs the OAuth "state" so a callback cannot be replayed against a
    # different tenant than the one that started the connect flow. Falls
    # back to the Supabase JWT secret so no extra secret is required unless
    # you want to rotate it independently.
    OAUTH_STATE_SECRET = os.getenv("OAUTH_STATE_SECRET", "")

    @property
    def google_calendar_configured(self) -> bool:
        return bool(
            self.GOOGLE_CLIENT_ID
            and self.GOOGLE_CLIENT_SECRET
            and self.GOOGLE_OAUTH_REDIRECT_URI
        )

    @property
    def database_enabled(self) -> bool:
        return bool(self.SQLALCHEMY_DATABASE_URI)


class TestConfig(Config):
    TESTING = True
    TWILIO_VALIDATE_SIGNATURE = False
