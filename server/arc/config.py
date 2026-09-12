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

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["dev", "test", "prod"]

#: Which backend answers the recommendations page (§5.6). ``gemini`` and
#: ``openrouter`` are both OpenAI-compatible and share one implementation;
#: ``anthropic`` is the native SDK.
RecsProvider = Literal["gemini", "openrouter", "anthropic"]

#: The OpenAI-compatible endpoint each provider is reached on when its
#: ``*_BASE_URL`` override is unset. Anthropic is absent because it does not go
#: through that client at all.
PROVIDER_BASE_URLS: dict[str, str] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
}

#: ``provider -> the settings field its key lives in``. One place that knows
#: the mapping, so the factory, the config check and the operator-facing
#: message cannot drift apart.
PROVIDER_KEY_FIELDS: dict[str, str] = {
    "gemini": "gemini_api_key",
    "openrouter": "openrouter_api_key",
    "anthropic": "anthropic_api_key",
}

#: What a fallback provider gets when one is named with no model. Only
#: OpenRouter has an obvious answer. ``openai/gpt-5-mini`` on the owner's
#: measurement: it gave the best picks of three candidates compared against a
#: real watch list, and it is cheap enough to be a fallback rather than an
#: event.
FALLBACK_DEFAULT_MODELS: dict[str, str] = {"openrouter": "openai/gpt-5-mini"}


def split_models(raw: str) -> list[str]:
    """A comma-separated model list, trimmed, in order, without blanks.

    Whitespace is tolerated everywhere because this is an environment variable
    a person edits by hand: ``a, b ,c`` and ``a,b,c`` mean the same thing.
    """
    return [part.strip() for part in raw.split(",") if part.strip()]


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
    #: Where the OAuth handshake happens (M9). A separate setting rather than
    #: something derived from :attr:`mal_api_url`, because MAL puts authorize
    #: and token on the main site rather than on the API host — and because the
    #: test suite points it at a mock transport.
    mal_oauth_url: str = "https://myanimelist.net/v1/oauth2"
    #: How often every linked account's MyAnimeList list is re-imported
    #: (FR-M3 names six hours as the default).
    mal_import_interval_hours: float = Field(default=6.0, gt=0)

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

    # --- Offline catalogue (M15.5) ---------------------------------------
    #
    # Two public files, imported weekly, that make search and matching work
    # when nothing upstream answers (FR-C6, architecture.md §5.0a). Neither
    # needs a key; both are settings rather than constants so a test can point
    # them at a local file and an operator at a mirror.

    #: manami's ``anime-offline-database``, the zstd-compressed JSONL release
    #: asset. ``releases/latest/download`` rather than a pinned tag: the point
    #: of a weekly job is that it picks up the week's release.
    offline_manami_url: str = (
        "https://github.com/manami-project/anime-offline-database/releases/latest/download/"
        "anime-offline-database.jsonl.zst"
    )
    #: Fribb's ``anime-lists`` cross-id map, served raw from GitHub. It is a
    #: file in a git repository rather than a release, which is why its version
    #: comes from the response headers rather than from the payload.
    offline_fribb_url: str = (
        "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
    )
    #: How old the manami import may be before ``GET /api/catalogue/offline``
    #: calls it stale. Fourteen days is two missed weekly runs: one skipped
    #: release is a GitHub blip, two is a job that is not running.
    offline_catalogue_stale_days: int = Field(default=14, ge=1)

    # --- TMDB (M15.5) ----------------------------------------------------
    #: The free v3 API key from ``themoviedb.org/settings/api``. Unset means
    #: no enrichment at all: the nightly job logs one line and does nothing,
    #: and shows render whatever art AniList and MAL provided (FR-C6,
    #: architecture.md §5.8). Reached by id through the offline cross-id map,
    #: so nothing here is a URL — Arc never searches TMDB.
    #:
    #: A plain string rather than a ``SecretStr``, like :attr:`mal_client_id`
    #: and for the same reason: a v3 key authorises read-only catalogue calls
    #: and nothing about an account, which makes it an application identifier
    #: rather than a credential.
    tmdb_api_key: str | None = None

    # --- Recommendations: the model chain (M12, §5.6) --------------------
    #
    # Not one model but a *chain*, because production runs on Gemini's free
    # tier and each model there allows only about twenty requests a day for
    # the whole deployment. So the primary provider is tried across several
    # models in turn, and when the day's quota is gone everywhere a paid
    # fallback provider takes over. Every key is optional; a deployment that
    # wants no recommendations sets none and the page says so.

    #: The primary provider, and the models to try on it **in order**, as a
    #: comma-separated list (read through :attr:`recs_models`). Each name must
    #: be one that provider actually serves; a mismatch is a startup warning.
    #:
    #: The default order is deliberate. ``gemini-3.5-flash`` leads on
    #: measurement rather than novelty: 3.7 and 3.8 were both under "high
    #: demand" when this was chosen — early-terminated streams, a 503, one
    #: answer that took 47 s — while 3.5 answered the full forty-candidate
    #: prompt in about four seconds, twice. 3.6 and 2.5 follow as fresh daily
    #: quotas rather than as better models.
    recs_provider: RecsProvider = "gemini"
    recs_model: str = "gemini-3.5-flash,gemini-3.6-flash,gemini-2.5-flash"

    #: Where to go when every primary model is spent or unreachable. Empty
    #: means "nowhere": the chain ends, and the page answers 502 until the
    #: quota resets. ``openrouter`` is the intended value — it is paid, so it
    #: is the thing that keeps working on the day the free tier does not.
    recs_fallback_provider: RecsProvider | None = None
    #: The fallback's models, same comma-separated form
    #: (:attr:`recs_fallback_models`). Blank with an ``openrouter`` fallback
    #: means the documented default rather than nothing, because a fallback
    #: provider named without a model is almost certainly a half-finished
    #: edit rather than an intention.
    recs_fallback_model: str = ""

    @field_validator("recs_fallback_provider", mode="before")
    @classmethod
    def _blank_fallback_is_none(cls, value: object) -> object:
        """``RECS_FALLBACK_PROVIDER=`` means "no fallback", not a broken boot.

        Blanking a variable is how an operator turns something off, and
        pydantic would otherwise reject the empty string against the Literal.
        """
        return None if isinstance(value, str) and not value.strip() else value

    #: One key per provider, all optional. Per-provider rather than one shared
    #: ``LLM_API_KEY`` because the chain can hold two providers at once: the
    #: whole point is that Gemini and OpenRouter are configured *together*.
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None

    #: Endpoint overrides, for a proxy, a regional endpoint, or a test's mock
    #: transport. Unset means the provider's documented default.
    gemini_base_url: str | None = None
    openrouter_base_url: str | None = None

    # --- Anthropic -------------------------------------------------------
    #: Used by the ``anthropic`` provider (primary or fallback) and by M13's
    #: match suggestions. Unset is fine on a Gemini deployment.
    anthropic_api_key: SecretStr | None = None

    # --- Nyaa (M6) -------------------------------------------------------
    #: The Nyaa origin. Configurable for the same reason ``ANILIST_URL`` is:
    #: the test suite points it at a mock transport, and an operator may want
    #: a mirror. The RSS path is built by
    #: :mod:`arc.services.acquisition.nyaa`, not spelled out here.
    nyaa_url: str = "https://nyaa.si"

    # --- qBittorrent -----------------------------------------------------
    qbit_url: str = "http://localhost:8080"
    qbit_user: str | None = None
    qbit_pass: SecretStr | None = None
    #: The category every torrent Arc adds is filed under. It is also the
    #: filter ``torrents/info`` is polled with, so nothing a person added by
    #: hand in the same client is ever touched by Arc (FR-A5).
    qbit_category: str = "arc"
    #: Where qBittorrent writes downloads **as qBittorrent sees it**. It is a
    #: container path, and ``DATA_DIR/downloads`` is the same directory as the
    #: worker sees it; :func:`arc.services.acquisition.qbit.host_path` maps
    #: one onto the other. They differ whenever the client runs in its own
    #: container, which is every deployment (architecture.md §8).
    qbit_downloads_path: str = "/data/downloads"
    #: Whether Arc lets the client seed what it has downloaded. **False**, and
    #: that is a product decision rather than a preference (spec §9: seeding
    #: off, upload capped, torrent client behind a VPN). The worker enforces it
    #: on the client itself — a share-ratio limit of 0 whose action is "stop" —
    #: and ``poll_qbit`` stops any torrent that reached a seeding state before
    #: the policy did. Set it true only on a host where seeding is wanted and
    #: allowed; Arc then leaves both alone.
    qbit_seeding: bool = False
    #: Upload rate cap for the whole client, KiB/s, sent as qBittorrent's
    #: ``up_limit`` (which is bytes/s, so this is multiplied by 1024). It
    #: applies while a torrent is *downloading* too — BitTorrent gives back
    #: what you give — so it is a cap, not a switch: 0 would be unlimited in
    #: qBittorrent's own terms, and 512 KiB/s is enough to keep a swarm
    #: interested without the host uploading anything worth noticing.
    qbit_upload_limit_kib: int = Field(default=512, ge=0)

    # --- Media -----------------------------------------------------------
    data_dir: Path = Path("./data")
    #: Concurrent ffmpeg encodes in one worker process (FR-P1, §8). A property
    #: of the host's cores, not an editable rule, which is why it is here and
    #: not in the ``settings`` table.
    max_transcodes: int = Field(default=2, ge=1)

    # --- Transcode (M7) ---------------------------------------------------
    #: The binaries. Names, resolved on ``PATH``, or absolute paths — a host
    #: with a hardware-enabled build in ``/opt`` sets these rather than
    #: shadowing the system ffmpeg.
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    #: The video encoder. ``libx264`` is software H.264 and is what the
    #: deployment guide sizes the host for; swapping in ``h264_vaapi`` or
    #: ``h264_nvenc`` is how hardware encoding is enabled later (§8).
    ffmpeg_video_encoder: str = "libx264"
    #: x264's speed/quality trade-off, its quality target, and its content
    #: tuning. ``veryfast`` at CRF 20 was the original pair and the owner
    #: judged the result soft (M15 sign-off): at ``veryfast`` x264 gives up
    #: most of its analysis, which on flat anime gradients and line art shows
    #: up as smearing and banding rather than as noise. ``fast`` at CRF 19 with
    #: ``-tune animation`` costs roughly 1.5–2× real time on two cores instead
    #: of about 1×, which is still inside FR-P3's budget because an episode is
    #: prepared ahead of being watched, not while it is.
    #:
    #: All three are settings rather than constants so a smaller host can go
    #: back to ``veryfast`` without a code change (architecture.md §5.3).
    #: ``TRANSCODE_TUNE`` empty means "no ``-tune`` at all", which is what a
    #: live-action-heavy library wants.
    transcode_preset: str = "fast"
    transcode_crf: int = Field(default=19, ge=0, le=51)
    transcode_tune: str = "animation"
    #: An optional bitrate ceiling, in kbit/s, and the VBV buffer that goes
    #: with it. Unset by default — CRF alone is the right control for a library
    #: served off one box — and set only where the *network* is the constraint:
    #: a 1080p CRF 19 anime encode can peak well past 10 Mbit/s on an action
    #: sequence, which is more than some connections will stream. Setting the
    #: maxrate without a bufsize gets a bufsize of twice the maxrate, x264's
    #: usual one-second-at-double-rate window.
    transcode_maxrate_kbps: int | None = Field(default=None, ge=1)
    transcode_bufsize_kbps: int | None = Field(default=None, ge=1)
    #: Target HLS segment length in seconds (FR-P1: "~6 s"). Keyframes are
    #: forced onto this boundary, so raising it makes seeking coarser and
    #: lowering it makes the playlist longer.
    hls_segment_seconds: int = Field(default=6, ge=1, le=60)
    #: How long one ffmpeg may run before it is killed and the job retried.
    #: Three hours: a 24-minute episode is about forty minutes on the intended
    #: hardware at the default preset, and a film is still inside it.
    #: Deliberately *longer* than :attr:`worker_stale_after`, and that is not a
    #: mistake: the transcode handler pushes ``jobs.locked_at`` forward every
    #: ``HEARTBEAT_SECONDS`` from the moment it starts waiting for an encode
    #: slot until ffmpeg exits (:mod:`arc.services.media.jobs`), so the stale
    #: sweep never sees a job that is still working, however long it runs.
    #: ``worker_stale_after`` bounds silence, not work.
    transcode_timeout_seconds: float = Field(default=10800.0, gt=0)

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

    # --- Retention (M10) --------------------------------------------------
    #: When true the retention sweep logs everything it *would* delete and
    #: deletes nothing — no file, no row, no state change (FR-T1, FR-T3).
    #:
    #: An environment variable rather than a row in ``settings`` beside G and
    #: D, deliberately. G and D are rules an admin tunes from the UI; this is
    #: an operator's brake on a process that removes files, set before the
    #: worker starts and normally on only for the first night of a new
    #: deployment. A "delete nothing" switch that lives in a table Arc itself
    #: writes to is one bad migration away from being off.
    retention_dry_run: bool = False

    # --- Worker ----------------------------------------------------------
    #: Jobs the worker runs at once. Transcodes have their own, smaller cap
    #: (``max_transcodes``); this is the queue-wide limit.
    worker_concurrency: int = Field(default=2, ge=1)
    #: Seconds to wait before asking for work again when the queue was empty.
    worker_poll_interval: float = Field(default=1.0, gt=0)
    #: On shutdown, how long to let in-flight jobs finish before cancelling.
    worker_drain_timeout: float = Field(default=30.0, ge=0)
    #: Seconds a job may sit ``running`` **without saying anything** before the
    #: sweep assumes the worker holding it died and puts the row back. It is a
    #: bound on silence rather than on work: a job longer than this is safe as
    #: long as it heartbeats, which is why the three-hour ffmpeg timeout may
    #: exceed these two hours. A job that does *not* heartbeat must finish
    #: inside this window or it will be requeued underneath itself
    #: (:mod:`arc.services.media.jobs` is the one that does).
    worker_stale_after: float = Field(default=7200.0, gt=0)

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
    def recs_models(self) -> list[str]:
        """The primary provider's models, in the order they are tried."""
        return split_models(self.recs_model)

    @property
    def recs_fallback_models(self) -> list[str]:
        """The fallback provider's models, in order. Empty when there is none.

        A named fallback provider with no model falls back to the documented
        default where one exists (:data:`FALLBACK_DEFAULT_MODELS`), because
        naming a provider and no model is far more likely to be a half-finished
        edit than a deliberate "configure it but never use it".
        """
        if self.recs_fallback_provider is None:
            return []
        chosen = split_models(self.recs_fallback_model)
        if chosen:
            return chosen
        return split_models(FALLBACK_DEFAULT_MODELS.get(self.recs_fallback_provider, ""))

    def recs_key_field(self, provider: str) -> str:
        """The settings field ``provider``'s key lives in."""
        return PROVIDER_KEY_FIELDS[provider]

    def recs_key_env(self, provider: str) -> str:
        """The environment variable an operator would set for ``provider``."""
        return PROVIDER_KEY_FIELDS[provider].upper()

    def recs_key(self, provider: str) -> str:
        """``provider``'s key as a plain string, or ``""`` when it is unset."""
        raw = getattr(self, PROVIDER_KEY_FIELDS[provider], None)
        if raw is None:
            return ""
        getter = getattr(raw, "get_secret_value", None)
        return str(getter()) if callable(getter) else str(raw)

    def recs_base_url(self, provider: str) -> str | None:
        """``provider``'s endpoint, or ``None`` for anthropic.

        The override wins when set; otherwise the provider's documented
        default. Anthropic never uses this — it has its own client.
        """
        if provider == "anthropic":
            return None
        override = getattr(self, f"{provider}_base_url", None)
        return str(override) if override else PROVIDER_BASE_URLS[provider]

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
        """Where qBittorrent's finished files land, as an absolute path.

        Resolved, because this one is *compared* rather than merely opened:
        :func:`arc.services.acquisition.reject.episode_id_of` decides whether a
        media file is Arc's own by asking whether it sits under this directory,
        and a ``DATA_DIR`` of ``./data`` (the default) or a ``/tmp`` that is a
        symlink would otherwise make the same directory answer both ways
        depending on which side of the comparison it came from.
        """
        return (self.data_dir / "downloads").resolve()

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
        """Where prepared HLS output lives, as an absolute path.

        Resolved for the same reason as :attr:`downloads_dir`: the transcode
        handler encodes into a sibling temporary directory and renames it into
        place, and ``rename`` across what turns out to be two names for one
        directory is the kind of bug that only appears on the host whose
        ``DATA_DIR`` is relative.
        """
        return (self.data_dir / "renditions").resolve()

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
