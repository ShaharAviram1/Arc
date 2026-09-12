"""Production configuration warnings (roadmap M11).

Arc boots with defaults everywhere so that ``uv run uvicorn arc.main:app``
works on a fresh checkout with no ``.env`` at all (arc/config.py). That is the
right trade in development and exactly the wrong one in production, where the
same leniency means a stack can come up fully working, serve pages, and only
reveal — a week later, when somebody clicks "link MyAnimeList" — that
``FERNET_KEY`` was never set.

So: when ``ENV=prod``, every key the deployment genuinely needs is checked at
startup and each problem is logged as one ERROR line, by both the API and the
worker. **Logged, not raised.** An operator mid-deploy is better served by a
process that runs and complains than by one that will not start: a missing
``MAL_CLIENT_SECRET`` breaks the MAL link and nothing else, and refusing to
boot over it would take the player down too. ``GET /api/health`` reports the
*count* in production (:func:`count`) so a smoke test can assert zero — the
count only, never the details, because that endpoint is unauthenticated.

Two kinds of problem are treated the same way, because operationally they are
the same: a key that is **missing**, and one that still holds a **placeholder**
— the shipped ``.env.example`` value, or anything containing ``change-me``. A
production ``SECRET_KEY`` of ``dev-only-not-secret-change-me`` is not
configuration, it is a published secret.

There is a second, quieter level: :attr:`ConfigWarning.level` ``"warning"``.
It is for configuration that is genuinely optional but whose absence turns a
whole page off — today, the recommendation chain (M12): the primary provider's
key, a fallback provider named without one, and a model name that does not look
like the provider it is listed under. ``LLM_MATCH_SUGGESTIONS`` (M13) is the
other side of that same chain and is an **error**, because there the flag is an
operator saying the feature should be on. Which variable holds which key depends on
the provider (:meth:`Settings.recs_key_env`), and every message names both,
because the failure worth catching is a key set for a provider that is not in
the chain. A
deployment without recommendations is a valid, complete Arc, so this must not
be an ERROR and must not count towards the number ``/api/health`` publishes
(:func:`count` is errors only) — but an operator who *meant* to configure it
should see one line saying the page will answer 503 rather than discover it
from a user.

Deliberately *not* checked:

* ``POSTGRES_PASSWORD`` — deploy-only, not read by the app at all, and already
  fatal in Compose via ``${POSTGRES_PASSWORD:?…}``.
* ``BOOTSTRAP_ADMIN_EMAIL`` / ``BOOTSTRAP_ADMIN_PASSWORD`` — needed for exactly
  one boot and then deliberately blanked (services/auth/bootstrap.py). Warning
  about them forever would train an operator to ignore this list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from arc.config import Settings
from arc.core.security import origin_of

log = logging.getLogger(__name__)

#: How loudly a problem is reported. ``"error"`` is "this deployment is broken
#: and the operator has not noticed"; ``"warning"`` is "a feature is off, on
#: purpose or otherwise".
Level = Literal["error", "warning"]

#: Hosts that mean "this machine". A ``PUBLIC_URL`` on one of them is a
#: development leftover anywhere it is not development.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})

#: Substrings that mark a value as never having been filled in. Matched
#: case-insensitively against the whole value.
PLACEHOLDER_MARKERS = ("change-me", "changeme", "replace-me", "replaceme", "placeholder")

#: Values shipped in ``.env.example`` that are legitimate strings but are not
#: secrets: keyed by setting name so that ``admin`` is a fine *username* while
#: ``adminadmin`` is not a password.
SHIPPED_DEFAULTS = {
    "qbit_pass": frozenset({"adminadmin", "admin"}),
}


@dataclass(frozen=True, slots=True)
class ConfigWarning:
    """One production configuration problem, ready to log."""

    #: The environment variable, upper-cased, as an operator would set it.
    key: str
    #: What is wrong, in one sentence, and what it breaks.
    message: str
    #: ERROR by default — everything on this list was an error before the
    #: level existed, and a new entry should have to opt out of being one.
    level: Level = "error"


def _value(settings: Settings, name: str) -> str:
    """A setting as a plain string, secrets unwrapped, ``""`` when unset."""
    raw = getattr(settings, name, None)
    if raw is None:
        return ""
    getter = getattr(raw, "get_secret_value", None)
    return str(getter()) if callable(getter) else str(raw)


def is_placeholder(name: str, value: str) -> bool:
    """Whether ``value`` is missing or is still an example value."""
    text = value.strip()
    if not text:
        return True
    lowered = text.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    return lowered in SHIPPED_DEFAULTS.get(name, frozenset())


def _has_key(settings: Settings, provider: str) -> bool:
    """Whether ``provider``'s key is set and is not an example value."""
    return not is_placeholder(settings.recs_key_field(provider), settings.recs_key(provider))


def model_chain_configured(settings: Settings) -> bool:
    """Whether any ``(provider, model)`` entry could actually be called.

    The same rule as :func:`arc.services.recs.factory.chain_entries` — "an
    entry needs both a usable key and a model name" — restated rather than
    imported, because that module imports :func:`is_placeholder` from this one
    and importing it back would be a cycle. ``test_config_check`` asserts the
    two agree across a matrix of settings, which is what keeps the restatement
    honest.

    Two features read it. The recommendations page turns it into
    ``configured`` (via the factory's ``None``), and M13's match suggestions
    turn it into ``suggestions_enabled`` — ``LLM_MATCH_SUGGESTIONS`` alone is
    an intention, and it is only a working feature when something can answer.
    """
    if _has_key(settings, settings.recs_provider) and settings.recs_models:
        return True
    fallback = settings.recs_fallback_provider
    return bool(fallback and _has_key(settings, fallback) and settings.recs_fallback_models)


def is_local_origin(url: str) -> bool:
    """Whether ``url``'s origin points at the machine the process runs on.

    An unparseable URL counts as local: it is at least as broken as a localhost
    one, and the same log line is the right answer to both.
    """
    origin = origin_of(url)
    if origin is None:
        return True
    host = urlsplit(origin).hostname or ""
    return host in LOCAL_HOSTS or host.endswith(".localhost")


#: ``(setting name, what it breaks when it is missing)``. Order is the order
#: the lines appear in the log, which is roughly "most likely to be noticed".
_REQUIRED: tuple[tuple[str, str], ...] = (
    ("secret_key", "session and token signing falls back to a published example value"),
    ("fernet_key", "MyAnimeList tokens cannot be encrypted, so nobody can link an account"),
    ("mal_client_id", "MyAnimeList is unavailable as a catalogue fallback and for linking"),
    ("mal_client_secret", "the MyAnimeList OAuth handshake cannot complete"),
    ("qbit_pass", "Arc cannot authenticate to qBittorrent, so nothing is ever downloaded"),
)


def warnings(settings: Settings) -> list[ConfigWarning]:
    """Every production configuration problem, in a stable order.

    Empty outside production: the whole point of the dev defaults is that they
    are allowed to be defaults there.
    """
    if not settings.is_prod:
        return []

    found: list[ConfigWarning] = []

    for name, consequence in _REQUIRED:
        if is_placeholder(name, _value(settings, name)):
            found.append(
                ConfigWarning(
                    key=name.upper(),
                    message=f"not set (or still an example value); {consequence}",
                )
            )

    # Everything Arc hands a user — invite links, the MAL redirect — is built
    # from PUBLIC_URL, and its origin is the one the CSRF check accepts. A
    # production stack still pointing at localhost therefore sends out links
    # nobody can follow and refuses its own client's writes.
    if is_local_origin(settings.public_url):
        found.append(
            ConfigWarning(
                key="PUBLIC_URL",
                message=(
                    f"is a local address ({settings.public_url!r}); invite links will point "
                    "nowhere and the CSRF origin check will refuse the client's own writes"
                ),
            )
        )

    # MAL sends the browser here after the consent screen, and it must match
    # the redirect URI registered on the MAL application exactly. A localhost
    # one is the shipped default and is the single most common reason the
    # link flow dies on a real host.
    if is_local_origin(settings.mal_redirect_uri):
        found.append(
            ConfigWarning(
                key="MAL_REDIRECT_URI",
                message=(
                    f"is a local address ({settings.mal_redirect_uri!r}); set it to "
                    "https://<host>/api/mal/callback and register that on the MAL application"
                ),
            )
        )

    # TMDB (M15.5). A warning, never an error, for the same reason the
    # recommendation chain is one: a deployment with no key is a complete Arc
    # that renders AniList's artwork, and `/api/health`'s count must still
    # reach zero. It is worth a line because the absence is invisible — the
    # shows that would have gained a backdrop simply do not, and nothing on
    # the page says why.
    if is_placeholder("tmdb_api_key", _value(settings, "tmdb_api_key")):
        found.append(
            ConfigWarning(
                key="TMDB_API_KEY",
                message=(
                    "unset: key art and stills will not be enriched (a free key from "
                    "themoviedb.org/settings/api fills the backdrops, posters, episode "
                    "stills and credits AniList has not)"
                ),
                level="warning",
            )
        )

    # Match suggestions (M13) ride the same provider chain as the
    # recommendations (§5.2, §5.6), so the question is not "is there an
    # Anthropic key" — it is "can anything in the chain be called". An ERROR
    # rather than a warning: the flag says the feature is on, and it cannot
    # work. Silence it by configuring a provider or by turning the flag off,
    # both of which leave a finished deployment.
    if settings.llm_match_suggestions and not model_chain_configured(settings):
        found.append(
            ConfigWarning(
                key="LLM_MATCH_SUGGESTIONS",
                message=(
                    "is on but no model provider is configured (set RECS_PROVIDER's key, "
                    f"{settings.recs_key_env(settings.recs_provider)}, or a fallback "
                    "provider's); match suggestions will never be produced"
                ),
            )
        )

    # The recommendation chain (M12). Warnings, never errors: without a key
    # the page answers 503 and the rest of Arc is entirely unaffected, so a
    # deployment that never wanted recommendations is a finished product
    # rather than a broken one — and `/api/health`'s count must still reach
    # zero. Each line names the provider *and* the variable, because the whole
    # failure mode here is a key set for the provider that is not selected.
    if is_placeholder(
        settings.recs_key_field(settings.recs_provider),
        settings.recs_key(settings.recs_provider),
    ):
        found.append(
            ConfigWarning(
                key=settings.recs_key_env(settings.recs_provider),
                message=(
                    f"not set while RECS_PROVIDER is {settings.recs_provider!r}; "
                    "recommendations will answer 503 unless a fallback provider is configured"
                ),
                level="warning",
            )
        )

    # A fallback named but not usable is worth its own line: the operator has
    # said what should happen when the free tier runs out, and it will not.
    fallback = settings.recs_fallback_provider
    if fallback:
        if is_placeholder(settings.recs_key_field(fallback), settings.recs_key(fallback)):
            found.append(
                ConfigWarning(
                    key=settings.recs_key_env(fallback),
                    message=(
                        f"not set while RECS_FALLBACK_PROVIDER is {fallback!r}; "
                        "the fallback will be skipped and the page will 502 once the "
                        "primary provider's daily quota is spent"
                    ),
                    level="warning",
                )
            )
        if not settings.recs_fallback_models:
            found.append(
                ConfigWarning(
                    key="RECS_FALLBACK_MODEL",
                    message=(
                        f"is empty and {fallback!r} has no default; the fallback will be skipped"
                    ),
                    level="warning",
                )
            )

    # RECS_MODEL and RECS_PROVIDER are set independently and mean nothing
    # apart: asking Gemini for ``claude-opus-5`` is a 404 on the first run and
    # nothing before it. Checked per entry, because both are lists now. A
    # warning rather than an error because the prefixes are a heuristic — a
    # provider may ship a name that breaks the pattern — and because being
    # wrong here must not stop a boot.
    for label, provider, models in (
        ("RECS_MODEL", settings.recs_provider, settings.recs_models),
        ("RECS_FALLBACK_MODEL", fallback, settings.recs_fallback_models),
    ):
        prefix = MODEL_PREFIXES.get(provider or "")
        if not prefix:
            continue
        for model in models:
            if not model.lower().startswith(prefix):
                found.append(
                    ConfigWarning(
                        key=label,
                        message=(
                            f"lists {model!r}, which does not look like a {provider!r} "
                            f"model (expected a name starting {prefix!r}); "
                            "that entry will fail on its first run"
                        ),
                        level="warning",
                    )
                )

    return found


def errors(settings: Settings) -> list[ConfigWarning]:
    """Only the problems that mean the deployment is broken."""
    return [warning for warning in warnings(settings) if warning.level == "error"]


def count(settings: Settings) -> int:
    """How many production configuration *errors* there are. Zero in dev.

    Errors only, deliberately: this is the number ``/api/health`` publishes and
    a deploy smoke test asserts is zero, and a valid deployment without an
    Anthropic key must be able to reach zero.
    """
    return len(errors(settings))


#: What a model name is expected to start with, per provider. OpenRouter is
#: absent on purpose: it serves every vendor, so ``anthropic/claude-opus-5``
#: and ``google/gemini-3.5-flash`` are both right and there is nothing to
#: check.
MODEL_PREFIXES = {"gemini": "gemini", "anthropic": "claude"}


#: ``level`` → the logging level it is emitted at.
_LEVELS: dict[Level, int] = {"error": logging.ERROR, "warning": logging.WARNING}


def log_warnings(settings: Settings, *, component: str) -> int:
    """Log one line per problem and return how many were *errors*.

    ``component`` names the process ("api", "worker") so that two containers
    complaining about the same key are still two distinct lines. The return
    value matches :func:`count` — the callers use it as "how bad is this".
    """
    found = warnings(settings)
    for warning in found:
        log.log(
            _LEVELS[warning.level],
            "configuration problem: %s %s",
            warning.key,
            warning.message,
            extra={"component": component, "config_key": warning.key},
        )
    return sum(1 for warning in found if warning.level == "error")


__all__ = [
    "LOCAL_HOSTS",
    "PLACEHOLDER_MARKERS",
    "MODEL_PREFIXES",
    "ConfigWarning",
    "Level",
    "count",
    "errors",
    "is_local_origin",
    "is_placeholder",
    "log_warnings",
    "model_chain_configured",
    "warnings",
]
