"""MyAnimeList OAuth 2.0 with PKCE: the link handshake and token refresh (FR-M1).

Three things live here and nothing else does — no database, no list logic, no
writes. That is deliberate: the handshake is the part with the security
properties, and it is easiest to reason about when it is a handful of pure
functions plus one HTTP client.

**PKCE, but MyAnimeList's dialect.** MAL supports only the ``plain`` challenge
method: the ``code_challenge`` sent to the authorize endpoint *is* the
``code_verifier`` sent to the token endpoint, unhashed. There is no S256
option to prefer, so Arc sends ``code_challenge_method=plain`` explicitly
rather than relying on the default, and the verifier is 86 characters of
``secrets.token_urlsafe`` — comfortably inside the 43–128 the spec allows, and
made of unreserved characters only, so it survives a URL round trip untouched.

**The state is the session, encrypted.** A link attempt has to survive a
redirect to a site Arc does not control and back, carrying two things: who
started it and which verifier belongs to it. The obvious implementation is a
table of pending attempts with an expiry and a sweep. Instead the state
parameter *is* that record — ``{user_id, verifier, issued_at, nonce}``,
serialised and encrypted with the app's Fernet key (:mod:`arc.core.crypto`).
Fernet is authenticated, so a state that comes back modified does not decrypt;
it is timestamped, so :data:`STATE_TTL_SECONDS` is enforced by the token
itself; and the ``nonce`` makes two links started in the same second by the
same user distinguishable. No table, no row to expire, nothing to clean up.

The callback still checks that the decrypted ``user_id`` is the user whose
session cookie arrived with the request. Encryption proves Arc issued the
state, not that this browser is the one it was issued to — a state pasted into
somebody else's browser would otherwise link the wrong account.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from urllib.parse import urlencode

import httpx

from arc.config import Settings
from arc.core.crypto import InvalidToken, decrypt_json, encrypt_json

#: MyAnimeList's OAuth endpoints. Not under ``api.myanimelist.net`` — the
#: authorize and token routes live on the main site — which is why this is a
#: setting of its own rather than something derived from ``MAL_API_URL``.
MAL_OAUTH_URL = "https://myanimelist.net/v1/oauth2"

#: Bytes of entropy in the PKCE verifier. ``token_urlsafe(64)`` renders them
#: as 86 unreserved characters; MAL accepts 43–128.
VERIFIER_BYTES = 64

#: The only challenge method MyAnimeList implements. Sent explicitly: it is
#: also the default, and a default that changes silently is not something to
#: depend on for an authorisation parameter.
CHALLENGE_METHOD = "plain"

#: How long a link attempt stays valid. Ten minutes is long enough to log in
#: to MyAnimeList and press "Allow", short enough that a state left in a
#: browser history is worthless.
STATE_TTL_SECONDS = 600

#: Timeout for one token request. Shorter than the catalogue's 15 s: a user is
#: waiting on the redirect, and a hung token exchange is a blank page.
TIMEOUT_SECONDS = 10.0

#: Refresh this far ahead of expiry. Five minutes covers a long-running import
#: that started with a nearly-expired token, so the refresh happens once at
#: the top rather than mid-page.
REFRESH_MARGIN = timedelta(minutes=5)

#: What Arc assumes when MAL omits ``expires_in``. It normally sends about a
#: month; an hour is a safe floor that simply means "refresh sooner".
DEFAULT_EXPIRES_IN = 3600


class MalOAuthError(RuntimeError):
    """A token request MyAnimeList refused or could not answer.

    ``retryable`` separates "MAL is having a moment" (a 5xx, a timeout) from
    "these credentials are dead" (a 400/401 on a refresh). Only the latter
    costs the user a re-link.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class InvalidState(ValueError):
    """The ``state`` that came back is not one Arc issued, or is too old."""


@dataclass(frozen=True, slots=True)
class LinkState:
    """What the ``state`` parameter carries across the redirect."""

    user_id: int
    verifier: str
    issued_at: datetime
    nonce: str


@dataclass(frozen=True, slots=True)
class MalTokens:
    """One token response, with the expiry worked out as an instant."""

    access_token: str
    refresh_token: str
    expires_at: datetime

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, now: datetime) -> Self:
        access = str(payload.get("access_token") or "")
        refresh = str(payload.get("refresh_token") or "")
        if not access or not refresh:
            raise MalOAuthError("token response was missing a token")
        try:
            expires_in = int(payload.get("expires_in") or DEFAULT_EXPIRES_IN)
        except TypeError, ValueError:
            expires_in = DEFAULT_EXPIRES_IN
        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at=now + timedelta(seconds=max(expires_in, 60)),
        )


def new_verifier() -> str:
    """A fresh PKCE verifier — which, on MAL, is also the challenge."""
    return secrets.token_urlsafe(VERIFIER_BYTES)


def encode_state(settings: Settings, *, user_id: int, verifier: str, now: datetime) -> str:
    """The opaque ``state`` for one link attempt."""
    return encrypt_json(
        settings,
        {
            "user_id": user_id,
            "verifier": verifier,
            "issued_at": now.isoformat(),
            "nonce": secrets.token_urlsafe(8),
        },
        # Stamped with the same instant it claims to have been issued at, so
        # that :data:`STATE_TTL_SECONDS` is enforced by the token rather than
        # by a comparison somebody could forget to make.
        at=now,
    )


def decode_state(settings: Settings, state: str) -> LinkState:
    """``state`` back into a :class:`LinkState`, or :class:`InvalidState`.

    Every way this can go wrong — forged, truncated, encrypted with a key that
    has since been rotated, older than :data:`STATE_TTL_SECONDS`, or shaped
    wrong — is one exception, because the callback's answer to all of them is
    the same: send the user back to the MAL page with an error.
    """
    try:
        payload = decrypt_json(settings, state, ttl=STATE_TTL_SECONDS)
    except (InvalidToken, ValueError, TypeError) as exc:
        raise InvalidState("state is not valid or has expired") from exc
    if not isinstance(payload, dict):
        raise InvalidState("state did not decode to an object")
    try:
        return LinkState(
            user_id=int(payload["user_id"]),
            verifier=str(payload["verifier"]),
            issued_at=datetime.fromisoformat(str(payload["issued_at"])),
            nonce=str(payload["nonce"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidState("state was missing a field") from exc


def authorize_url(settings: Settings, *, state: str, verifier: str) -> str:
    """Where to send the browser to ask for authorisation (FR-M1).

    ``redirect_uri`` is sent even though MAL makes it optional for an app with
    a single registered URI: it must then match the one used at the token
    endpoint, and sending it in both places is the only way the two cannot
    drift.
    """
    params = {
        "response_type": "code",
        "client_id": settings.require("mal_client_id"),
        "code_challenge": verifier,
        "code_challenge_method": CHALLENGE_METHOD,
        "state": state,
        "redirect_uri": settings.mal_redirect_uri,
    }
    return f"{settings.mal_oauth_url.rstrip('/')}/authorize?{urlencode(params)}"


class MalOAuthClient:
    """The two token calls, over one short-lived HTTP client."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.settings = settings
        self.url = settings.mal_oauth_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "arc/0.1 (self-hosted)"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _credentials(self) -> dict[str, str]:
        """``client_id``, plus ``client_secret`` when the app has one.

        MAL issues secretless (public) clients as well as confidential ones,
        and sending an empty ``client_secret`` to a public client is a 400.
        So the field is present only when it is set — which is also why
        ``MAL_CLIENT_SECRET`` is not read through ``require()`` here.
        """
        form = {"client_id": self.settings.require("mal_client_id")}
        secret = self.settings.mal_client_secret
        if secret is not None and secret.get_secret_value():
            form["client_secret"] = secret.get_secret_value()
        return form

    async def _token(self, form: dict[str, str], *, now: datetime) -> MalTokens:
        try:
            response = await self._http.post(f"{self.url}/token", data=form)
        except httpx.HTTPError as exc:
            raise MalOAuthError(f"{type(exc).__name__}: {exc}", retryable=True) from exc

        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise MalOAuthError(f"HTTP {response.status_code}", retryable=True)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise MalOAuthError(f"HTTP {response.status_code}: {_error_of(response)}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MalOAuthError("token response was not JSON") from exc
        if not isinstance(payload, dict):
            raise MalOAuthError("token response was not a JSON object")
        return MalTokens.from_payload(payload, now=now)

    async def exchange(self, *, code: str, verifier: str, now: datetime | None = None) -> MalTokens:
        """Authorisation code → tokens."""
        form = {
            **self._credentials(),
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": self.settings.mal_redirect_uri,
        }
        return await self._token(form, now=now or datetime.now(UTC))

    async def refresh(self, *, refresh_token: str, now: datetime | None = None) -> MalTokens:
        """Refresh token → a new pair. MAL rotates both."""
        form = {
            **self._credentials(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        return await self._token(form, now=now or datetime.now(UTC))


def _error_of(response: httpx.Response) -> str:
    """MAL's ``error``/``message`` body, trimmed, for the log and the user."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if not isinstance(body, dict):
        return str(body)[:200]
    parts = [str(body.get(key)) for key in ("error", "message", "hint") if body.get(key)]
    return "; ".join(parts)[:200] or response.text[:200]


__all__ = [
    "CHALLENGE_METHOD",
    "DEFAULT_EXPIRES_IN",
    "MAL_OAUTH_URL",
    "REFRESH_MARGIN",
    "STATE_TTL_SECONDS",
    "TIMEOUT_SECONDS",
    "VERIFIER_BYTES",
    "InvalidState",
    "LinkState",
    "MalOAuthClient",
    "MalOAuthError",
    "MalTokens",
    "authorize_url",
    "decode_state",
    "encode_state",
    "new_verifier",
]
