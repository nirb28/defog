"""App settings, read from environment variables or a local .env file."""

import json
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # development: relaxed defaults so the app runs with no setup.
    # production: refuses to start without real secrets.
    environment: Literal["development", "production"] = "development"

    # --- Addresses -----------------------------------------------------------------------
    # Where students open the web app (used in emails and after OAuth redirects).
    app_url: str = "http://localhost:5173"
    # Public address of this API, used to build OAuth redirect URIs.
    # In development the Vite server forwards /api to the backend, so this matches app_url.
    api_url: str = "http://localhost:5173"

    # --- Database (Supabase Postgres in production) --------------------------------------
    # Paste the Supabase connection string (see .env.example).
    database_url: str = ""
    database_pool_size: int = Field(default=5, ge=1, le=50)

    # --- Secrets ---------------------------------------------------------------------------
    # Signs short-lived OAuth state cookies. Any long random string.
    secret_key: str = ""
    # Fernet key that encrypts Canvas/Google tokens at rest.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = ""

    # --- Sessions ----------------------------------------------------------------------------
    session_days: int = Field(default=30, ge=1, le=365)
    access_token_minutes: int = Field(default=60, ge=5, le=24 * 60)
    refresh_token_days: int = Field(default=30, ge=1, le=365)
    # Where the mobile app receives Google sign-in results, e.g. defog://auth. Empty = off.
    mobile_redirect_uri: str = ""

    # --- Login protection ------------------------------------------------------------------
    login_max_failures: int = 5
    login_failure_window_minutes: int = 15
    password_breach_check: bool = True

    # --- Email ---------------------------------------------------------------------------------
    # console: print emails to the server log (development). smtp: send for real.
    email_backend: Literal["console", "smtp"] = "console"
    email_from: str = "Defog <no-reply@defog.fit>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True

    # --- Google (sign-in and Calendar share one OAuth client) ------------------------------
    google_client_id: str = ""
    google_client_secret: str = ""

    # --- Canvas ----------------------------------------------------------------------------------
    # Developer keys per school, as JSON:
    # {"https://canvas.upenn.edu": {"client_id": "...", "client_secret": "..."}}
    canvas_oauth_clients: Annotated[dict[str, dict[str, str]], NoDecode] = {}
    # Optional allowlist of Canvas hosts students may connect (comma-separated). Empty = any
    # public https host.
    canvas_allowed_hosts: Annotated[list[str], NoDecode] = []

    # --- Sync ------------------------------------------------------------------------------------
    # In-process loop. Turn it off on hosts that pause idle servers (e.g. Cloud Run) and call
    # POST /api/internal/sync on a schedule instead.
    background_sync_enabled: bool = True
    # Shared secret a scheduler sends in X-Sync-Secret to run one sync pass. Empty = the
    # endpoint is off. Generate: python -c "import secrets; print(secrets.token_urlsafe(48))"
    sync_trigger_secret: str = ""
    sync_interval_minutes: int = Field(default=90, ge=15)
    # The chatbot refreshes a source first if its data is older than this.
    chat_sync_stale_minutes: int = Field(default=60, ge=5)

    # --- AI provider -----------------------------------------------------------------------------
    #   mock      - canned replies and keyword-picked tools; no API key needed (local dev)
    #   anthropic - Claude via the Anthropic API
    #   openai    - any OpenAI-compatible API (OpenAI, Azure OpenAI, a local Ollama server)
    llm_provider: Literal["mock", "anthropic", "openai"] = "mock"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"

    openai_api_key: str = ""
    openai_base_url: str = ""  # leave empty for api.openai.com
    openai_model: str = ""

    # Limits that keep a single student from running up the AI bill.
    max_output_tokens: int = Field(default=1024, ge=64, le=8192)
    max_history_messages: int = Field(default=20, ge=1, le=100)
    max_message_chars: int = Field(default=8000, ge=100, le=50_000)
    max_tool_rounds: int = Field(default=6, ge=1, le=12)
    daily_token_limit: int = Field(default=200_000, ge=1000)

    # Browser origins allowed to call the API (comma-separated in .env).
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "https://defog.fit",
        "https://www.defog.fit",
    ]

    @field_validator("cors_origins", "canvas_allowed_hosts", mode="before")
    @classmethod
    def _split_lists(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("canvas_oauth_clients", mode="before")
    @classmethod
    def _parse_canvas_clients(cls, value: object) -> object:
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else {}
        if isinstance(value, dict):
            # Normalise keys to "https://host" so lookups match the student's input.
            return {k.strip().rstrip("/").lower(): v for k, v in value.items()}
        return value

    @field_validator("database_url")
    @classmethod
    def _require_database(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "DATABASE_URL is not set. Paste your Supabase connection string into backend/.env "
                "(Supabase dashboard -> Connect -> Session pooler)."
            )
        return value.strip()

    @field_validator("app_url", "api_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_production(self) -> "Settings":
        if self.environment == "production":
            missing = [
                name
                for name in ("secret_key", "token_encryption_key")
                if len(getattr(self, name)) < 32
            ]
            if self.sync_trigger_secret and len(self.sync_trigger_secret) < 32:
                missing.append("sync_trigger_secret")
            if missing:
                raise ValueError(
                    "In production these settings must be set to long random values: "
                    + ", ".join(name.upper() for name in missing)
                )
            if not self.api_url.startswith("https://") or not self.app_url.startswith("https://"):
                raise ValueError("In production APP_URL and API_URL must use https://")
        return self

    # --- Derived values ----------------------------------------------------------------------
    @property
    def cookie_secure(self) -> bool:
        return self.api_url.startswith("https://")

    @property
    def effective_secret_key(self) -> str:
        # A fixed development key keeps local sessions working across restarts.
        return self.secret_key or "dev-only-secret-key-change-me-in-production-0000"

    @property
    def effective_token_key(self) -> str:
        # Fixed development key (valid Fernet format). Production requires a real one.
        return self.token_encryption_key or "ZGV2LW9ubHkta2V5LWRvLW5vdC11c2UtaW4tcHJvZCE="

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def redirect_uri(self, path: str) -> str:
        return f"{self.api_url}{path}"

    def canvas_client_for(self, base_url: str) -> dict[str, str] | None:
        return self.canvas_oauth_clients.get(base_url.lower())

    @property
    def api_host(self) -> str:
        return urlsplit(self.api_url).hostname or ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
