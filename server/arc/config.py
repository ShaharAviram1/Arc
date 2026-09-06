"""Application configuration: environment variables to a typed ``Settings``.

Every value in architecture.md §9 lives here. Secrets default to ``None`` and
are validated lazily by :func:`require`, at the point the feature that needs
them is used, so that a bare ``uv run uvicorn arc.main:app`` works in dev
without a fully populated ``.env``.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["dev", "test", "prod"]


class ConfigurationError(RuntimeError):
    """Raised when a feature is used without the env var it requires."""


class Settings(BaseSettings):
    """Runtime settings, read from the environment (and ``.env`` in dev)."""

    model_config = SettingsConfigDict(
        # Both, so the root .env is found whether a process is started from the
        # repository root or from ``server/``.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General ---------------------------------------------------------
    env: Env = "dev"
    log_level: str = "INFO"
    public_url: str = "http://localhost:5173"

    # --- Database --------------------------------------------------------
    database_url: str = "postgresql+asyncpg://arc:arc@localhost:5432/arc"

    # --- Auth (M2) -------------------------------------------------------
    #: Lifetime of a login session. Expiry slides: any request more than a day
    #: after the last extension pushes it out again, so the value is "days of
    #: inactivity before you are logged out", not "days before you are".
    session_ttl_days: int = Field(default=30, ge=1)
    #: Login attempts allowed per client IP inside the window below.
    login_rate_limit_per_ip: int = Field(default=10, ge=1)
    #: Login attempts allowed per email address. Tighter than the IP budget:
    #: it is the one that matters when an attempt is spread over many hosts.
    login_rate_limit_per_email: int = Field(default=5, ge=1)
    #: Width of the rate-limit window, in seconds (default 15 minutes). Shared
    #: by the login budgets and the invite one below.
    login_rate_window_seconds: float = Field(default=900.0, gt=0)
    #: Requests to the two *public* invite routes allowed per client IP in the
    #: same window. Looser than login: following a link, reloading the page and
    #: submitting the form is several requests from one person. Tight enough
    #: that guessing at a 256-bit token is not worth starting.
    invite_rate_limit_per_ip: int = Field(default=20, ge=1)
    #: Extra browser origins allowed to make state-changing calls, as a comma
    #: separated list. ``PUBLIC_URL``'s own origin is always allowed, and the
    #: localhost dev origins are added automatically when ``ENV`` is not
    #: ``prod``; this is for the cases neither covers (a second hostname, a
    #: staging domain). Read through :attr:`extra_origins`.
    cors_allowed_origins: str = ""

    # --- Secrets (validated only when the feature is used) ---------------
    # Secret-valued settings are ``SecretStr`` so they cannot leak through a
    # repr, a log line, or ``model_dump_json()``. Read them via ``require()``.
    secret_key: SecretStr | None = None
    fernet_key: SecretStr | None = None

    # --- MyAnimeList -----------------------------------------------------
    #: Read-only catalogue calls (FR-C6) need nothing but this; the OAuth
    #: fields below are M9's. Unset means "no fallback": everything keeps
    #: working while AniList is up, and ``GET /api/catalog/status`` reports
    #: ``mal`` as unconfigured.
    mal_client_id: str | None = None
    mal_client_secret: SecretStr | None = None
    mal_redirect_uri: str = "http://localhost:8000/api/mal/callback"
    #: MAL API v2's base URL. Configurable for the same reason ``ANILIST_URL``
    #: is: the test suite and the local fixture servers point it at a mock.
    mal_api_url: str = "https://api.myanimelist.net/v2"

    # --- AniList (M3) ----------------------------------------------------
    #: The GraphQL endpoint. Configurable so the test suite can point it at a
    #: mock and an operator can put a caching proxy in front of it.
    anilist_url: str = "https://graphql.anilist.co"
    #: Minimum gap between two AniList requests. AniList documents 90/min but
    #: currently enforces 30; 700 ms keeps Arc under the real limit even when
    #: both worker slots are querying (architecture.md §6).
    anilist_min_interval_ms: int = Field(default=700, ge=0)

    # --- Catalogue fallback (M3b) ----------------------------------------
    #: How long a catalogue source is skipped after it fails (FR-C6). Long
    #: enough that an outage costs one timeout rather than one per request,
    #: short enough that a blip is over in five minutes.
    catalog_breaker_seconds: float = Field(default=300.0, ge=0)

    # --- Anthropic -------------------------------------------------------
    anthropic_api_key: SecretStr | None = None

    # --- qBittorrent -----------------------------------------------------
    qbit_url: str = "http://localhost:8080"
    qbit_user: str | None = None
    qbit_pass: SecretStr | None = None

    # --- Media -----------------------------------------------------------
    data_dir: Path = Path("./data")
    max_transcodes: int = Field(default=2, ge=1)

    # --- Worker ----------------------------------------------------------
    #: Jobs the worker runs at once. Transcodes have their own, smaller cap
    #: (``max_transcodes``); this is the queue-wide limit.
    worker_concurrency: int = Field(default=2, ge=1)
    #: Seconds to wait before asking for work again when the queue was empty.
    worker_poll_interval: float = Field(default=1.0, gt=0)
    #: On shutdown, how long to let in-flight jobs finish before cancelling.
    worker_drain_timeout: float = Field(default=30.0, ge=0)
    #: Seconds a job may sit ``running`` before the sweep assumes the worker
    #: holding it died and puts it back. Must comfortably exceed the longest a
    #: real job takes, or a slow job is requeued underneath itself.
    worker_stale_after: float = Field(default=900.0, gt=0)

    # --- Feature flags ---------------------------------------------------
    llm_match_suggestions: bool = False

    # --- First-boot admin bootstrap (M2) ---------------------------------
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # --- Derived ---------------------------------------------------------
    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def extra_origins(self) -> tuple[str, ...]:
        """``CORS_ALLOWED_ORIGINS`` split and cleaned.

        A plain comma-separated string rather than a ``list[str]`` field:
        pydantic-settings parses list-typed values from the environment as
        JSON, which would make ``CORS_ALLOWED_ORIGINS=https://a,https://b``
        a startup crash instead of the obvious thing.
        """
        return tuple(part.strip() for part in self.cors_allowed_origins.split(",") if part.strip())

    @property
    def session_ttl(self) -> timedelta:
        """``SESSION_TTL_DAYS`` as a ``timedelta``."""
        return timedelta(days=self.session_ttl_days)

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def renditions_dir(self) -> Path:
        return self.data_dir / "renditions"

    @property
    def fonts_dir(self) -> Path:
        return self.data_dir / "fonts"

    def require(self, name: str) -> str:
        """Return a secret setting, or raise if it is unset.

        Call this from the service that needs the value, never at import
        time — a missing MAL client id must not stop the API from booting.
        ``SecretStr`` fields are unwrapped, so callers always get a plain
        ``str`` and never have to touch ``get_secret_value()`` themselves.
        """
        value = getattr(self, name, None)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if not value:
            raise ConfigurationError(
                f"{name.upper()} is not configured; set it in the environment "
                f"or .env before using this feature."
            )
        return str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
