"""Application configuration: environment variables to a typed ``Settings``.

Every value in architecture.md §9 lives here. Secrets default to ``None`` and
are validated lazily by :func:`require`, at the point the feature that needs
them is used, so that a bare ``uv run uvicorn arc.main:app`` works in dev
without a fully populated ``.env``.
"""

from __future__ import annotations

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

    # --- Secrets (validated only when the feature is used) ---------------
    # Secret-valued settings are ``SecretStr`` so they cannot leak through a
    # repr, a log line, or ``model_dump_json()``. Read them via ``require()``.
    secret_key: SecretStr | None = None
    fernet_key: SecretStr | None = None

    # --- MyAnimeList -----------------------------------------------------
    mal_client_id: str | None = None
    mal_client_secret: SecretStr | None = None
    mal_redirect_uri: str = "http://localhost:8000/api/mal/callback"

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
