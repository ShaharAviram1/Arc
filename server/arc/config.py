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

    # --- Library: ingest and matching (M5) --------------------------------
    #: How often the worker walks the download and manual-drop directories
    #: (FR-L1). Two minutes: a torrent that finishes is handed over by
    #: qBittorrent in M6, so this scan exists for files that appear without
    #: Arc being told — a manual drop, a restart mid-download — and two
    #: minutes is well inside "before anyone notices".
    library_scan_interval_seconds: float = Field(default=120.0, gt=0)
    #: A file younger than this is skipped and picked up on the next scan. A
    #: cheap stand-in for "the size stopped changing": a copy in progress is
    #: still being written to, and indexing it would store a size and a probe
    #: that are both wrong (FR-L1).
    library_settle_seconds: float = Field(default=60.0, ge=0)
    #: Video extensions the scanner picks up, comma separated. Read through
    #: :attr:`video_extensions`.
    video_extensions: str = "mkv,mp4,avi,ts,webm"
    #: Confidence at or above which a match is linked without asking anybody
    #: (FR-L4, architecture.md §5.2). Nothing below this is ever auto-linked;
    #: that is a non-negotiable, not a tuning knob.
    match_auto_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    #: Below this a candidate is not worth showing a human either, and the
    #: file becomes a "no good candidates" review item.
    match_min_candidate: float = Field(default=0.40, ge=0.0, le=1.0)
    #: The *title* similarity an automatic link needs on top of the confidence
    #: (FR-L4). Confidence is a weighted sum, so a title that is merely close
    #: can be carried over the threshold by a season, a format and an episode
    #: number that all agree — which is how ``Kaijuu 9-gou`` would be linked to
    #: *Kaijuu 8-gou*. Above this value, or an exact ``title_key`` match, and
    #: nothing else: below it the file goes to review whatever the sum says.
    #: Higher than :data:`~arc.services.library.matcher.NON_EXACT_CEILING` by
    #: default, which makes the default rule "the file must literally name the
    #: show"; lower it to let close spellings link themselves.
    match_min_title_for_auto: float = Field(default=0.92, ge=0.0, le=1.0)
    #: How many *new* files one scan pass indexes before it stops and leaves
    #: the rest for the next one (FR-L1). A first scan of an existing library
    #: is thousands of files and one ffprobe each; without a cap the job would
    #: run for longer than ``WORKER_STALE_AFTER`` and be requeued underneath
    #: itself. The next pass is two minutes away, so a big library is indexed
    #: over a few passes rather than in one that never finishes.
    library_scan_batch: int = Field(default=200, ge=1)
    #: How many files are indexed between commits inside one pass. Small
    #: enough that a slow probe cannot leave a long-running transaction open,
    #: large enough that the commit is not the expensive part.
    library_scan_commit_every: int = Field(default=25, ge=1)

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
    def manual_dir(self) -> Path:
        """The "drop a file in here" directory (FR-L1)."""
        return self.data_dir / "manual"

    @property
    def library_dirs(self) -> tuple[Path, ...]:
        """Everything the ingest scan walks, in order."""
        return (self.downloads_dir, self.manual_dir)

    @property
    def video_extensions_set(self) -> frozenset[str]:
        """``VIDEO_EXTENSIONS`` split, lowercased, dots stripped.

        A comma-separated string rather than a ``list[str]`` field for the
        same reason ``CORS_ALLOWED_ORIGINS`` is: pydantic-settings parses a
        list-typed value from the environment as JSON, and
        ``VIDEO_EXTENSIONS=mkv,mp4`` would be a startup crash.
        """
        return frozenset(
            part.strip().lstrip(".").lower()
            for part in self.video_extensions.split(",")
            if part.strip()
        )

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
