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

Deliberately *not* checked:

* ``POSTGRES_PASSWORD`` — deploy-only, not read by the app at all, and already
  fatal in Compose via ``${POSTGRES_PASSWORD:?…}``.
* ``BOOTSTRAP_ADMIN_EMAIL`` / ``BOOTSTRAP_ADMIN_PASSWORD`` — needed for exactly
  one boot and then deliberately blanked (services/auth/bootstrap.py). Warning
  about them forever would train an operator to ignore this list.
* ``ANTHROPIC_API_KEY`` — phase 2. Checked only when ``LLM_MATCH_SUGGESTIONS``
  is on, which is the one configuration that cannot work without it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from arc.config import Settings
from arc.core.security import origin_of

log = logging.getLogger(__name__)

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

    # Only when the feature that needs it is on: this is phase 2, and an
    # operator running phase 1 has no reason to hold an Anthropic key.
    if settings.llm_match_suggestions and is_placeholder(
        "anthropic_api_key", _value(settings, "anthropic_api_key")
    ):
        found.append(
            ConfigWarning(
                key="ANTHROPIC_API_KEY",
                message="not set while LLM_MATCH_SUGGESTIONS is on; match suggestions will fail",
            )
        )

    return found


def count(settings: Settings) -> int:
    """How many production configuration problems there are. Zero in dev."""
    return len(warnings(settings))


def log_warnings(settings: Settings, *, component: str) -> int:
    """Log one ERROR per problem and return how many there were.

    ``component`` names the process ("api", "worker") so that two containers
    complaining about the same key are still two distinct lines.
    """
    found = warnings(settings)
    for warning in found:
        log.error(
            "configuration problem: %s %s",
            warning.key,
            warning.message,
            extra={"component": component, "config_key": warning.key},
        )
    return len(found)


__all__ = [
    "LOCAL_HOSTS",
    "PLACEHOLDER_MARKERS",
    "ConfigWarning",
    "count",
    "is_local_origin",
    "is_placeholder",
    "log_warnings",
    "warnings",
]
