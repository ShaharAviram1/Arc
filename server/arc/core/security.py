"""Primitives the auth stack is built on: hashes, tokens, and origin checks.

Three separate concerns live here because they share one property — they are
pure, have no database and no FastAPI in them, and are therefore the easiest
part of auth to test exhaustively:

* **Passwords** — Argon2id (``argon2-cffi`` defaults: m=64 MiB, t=3, p=4),
  with :func:`rehash_if_needed` so that raising the parameters later upgrades
  every hash on its owner's next successful login.
* **Tokens** — session cookies and invite links. Generated with
  :func:`generate_token` (32 bytes from ``secrets``) and stored only as a
  SHA-256 hex digest (:func:`hash_token`). A plain hash is right here and a
  password hash would be wrong: the token is 256 bits of uniform randomness,
  so there is no dictionary to attack, and the lookup happens on every single
  request — Argon2 per request would cost 50 ms of CPU for no gain.
* **Origins** — the CSRF check (architecture.md §7: SameSite=Lax *and* an
  origin check). See :func:`allowed_origins` and :func:`is_origin_allowed`.

Password policy is deliberately thin (spec §7 asks for Argon2, not for a
composition rule): at least :data:`MIN_PASSWORD_LENGTH` characters and at most
:data:`MAX_PASSWORD_LENGTH`. Length is the only requirement that reliably
buys entropy; character-class rules mostly buy ``Password1!``.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable
from functools import lru_cache
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

#: Password policy (spec §7). Nothing else is enforced.
MIN_PASSWORD_LENGTH = 10
#: Argon2 has no practical input limit, but an unbounded body is a cheap way
#: to make a server burn CPU: hashing a 10 MB "password" is a free DoS.
MAX_PASSWORD_LENGTH = 128

#: Bytes of entropy in a session or invite token. ``token_urlsafe`` renders
#: them as 43 URL-safe characters.
TOKEN_BYTES = 32

#: Origins the client is served from during development. Added to the allowed
#: set whenever ``ENV`` is not ``prod``: Vite serves the client on 5173 and
#: proxies to the API, and 8000 is uvicorn itself (so ``/docs`` can post).
DEV_ORIGINS: tuple[str, ...] = ("http://localhost:5173", "http://localhost:8000")

#: Ports a browser leaves out of an ``Origin`` header, so :func:`origin_of`
#: leaves them out too and ``https://x`` matches ``https://x:443``.
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

_hasher = PasswordHasher()


class PasswordPolicyError(ValueError):
    """A password that does not satisfy the policy. Rendered as a 422."""


def validate_password(password: str) -> str:
    """Return ``password`` unchanged, or raise :class:`PasswordPolicyError`."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
    return password


def hash_password(password: str) -> str:
    """Validate the policy, then return an Argon2id hash (salt included)."""
    return _hasher.hash(validate_password(password))


def verify_password(password_hash: str, password: str) -> bool:
    """Whether ``password`` matches ``password_hash``.

    Never raises: a malformed or truncated hash in the database is a failed
    login, not a 500. ``argon2-cffi`` signals every outcome with an exception,
    so they are all funnelled back into a bool here.
    ``VerifyMismatchError`` needs no mention: it is a ``VerificationError``.
    The bare comma is PEP 758 (Python 3.14) and is what ``ruff format`` emits
    at this target — the parenthesised spelling is reformatted away.
    """
    try:
        return bool(_hasher.verify(password_hash, password))
    except VerificationError, InvalidHashError:
        return False


def rehash_if_needed(password_hash: str, password: str) -> str | None:
    """A fresh hash when the stored one uses outdated parameters, else ``None``.

    Called on a *successful* login — the only moment the plaintext is in hand
    — so that raising the Argon2 cost parameters silently migrates users
    instead of needing a password reset. The policy is not re-checked: the
    password was already accepted once, and a policy that tightened later must
    not lock anybody out at the hashing step.
    """
    try:
        if not _hasher.check_needs_rehash(password_hash):
            return None
    except InvalidHashError:
        # Unreadable hash: it cannot have verified, so this is unreachable
        # from the login path. Replacing it is still the safe answer.
        pass
    return _hasher.hash(password)


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A real Argon2 hash of a random secret, for the unknown-email path.

    :func:`arc.services.auth.users.authenticate` verifies against this when no
    user matches, so that "no such account" and "wrong password" take the same
    time. Without it, login is a user-enumeration oracle: a missing account
    answers in microseconds, a real one in ~50 ms.

    Computed once per process, on first use rather than at import, so that
    merely importing the module does not cost an Argon2 round.
    """
    return _hasher.hash(secrets.token_urlsafe(TOKEN_BYTES))


def generate_token() -> str:
    """A new opaque token: 32 random bytes, URL-safe base64."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The 64-character SHA-256 hex digest stored for ``token``.

    This is what goes in ``sessions.id`` and ``invites.token_hash``; the raw
    token exists only in the user's cookie or invite link. A database leak
    therefore hands nobody a working session or invite.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def origin_of(url: str) -> str | None:
    """``https://arc.example/x?y`` → ``https://arc.example``; ``None`` if not a URL.

    Anything without both a scheme and a host — the literal ``null`` browsers
    send for an opaque origin, a bare path, an empty string — is ``None``, and
    therefore never matches an allowed origin.

    The result is *canonical*, so that two spellings of one origin compare
    equal: the scheme and host are lowercased (``HTTP://LOCALHOST`` is
    ``http://localhost``), a port that is the scheme's default is dropped
    (``https://x:443`` is ``https://x``), and any userinfo is discarded —
    ``https://arc.example@evil.test`` is an origin on ``evil.test``, and
    keeping the netloc verbatim would be the one way to get that wrong.
    """
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError:
        # A malformed netloc — an unbracketed IPv6 address, a non-numeric
        # port. Not a URL, so not an origin.
        return None
    if not scheme or not host:
        return None
    if ":" in host:  # IPv6, which ``hostname`` hands back without its brackets
        host = f"[{host}]"
    if port is None or port == DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def allowed_origins(public_url: str, *, is_prod: bool, extra: Iterable[str] = ()) -> frozenset[str]:
    """The origins a state-changing request may come from.

    Always the origin of ``PUBLIC_URL`` (where users actually reach Arc), plus
    anything in ``CORS_ALLOWED_ORIGINS``, plus :data:`DEV_ORIGINS` when this is
    not a production environment. The dev origins are *not* added in prod:
    that would let a page served from a developer's laptop drive a production
    session if it could also plant the cookie.
    """
    origins = {origin for url in (public_url, *extra) if (origin := origin_of(url))}
    if not is_prod:
        origins.update(DEV_ORIGINS)
    return frozenset(origins)


def is_origin_allowed(origin: str | None, referer: str | None, allowed: frozenset[str]) -> bool:
    """The CSRF decision for one request.

    ``Referer`` is a fallback for a *missing* ``Origin`` only, because a few
    browsers still omit ``Origin`` on same-origin form posts. When ``Origin``
    is present it is authoritative: an unparseable one (``null`` for an opaque
    origin, a sandboxed iframe, a ``data:`` document) is a refusal, not an
    invitation to look at a header the caller also controls. A request with
    neither is refused too — that is exactly what a cross-site form post from
    an old client looks like, and Arc's own client always sends one.
    """
    if origin:
        resolved = origin_of(origin)
    else:
        resolved = origin_of(referer) if referer else None
    return resolved is not None and resolved in allowed


__all__ = [
    "DEFAULT_PORTS",
    "DEV_ORIGINS",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "TOKEN_BYTES",
    "PasswordPolicyError",
    "allowed_origins",
    "dummy_hash",
    "generate_token",
    "hash_password",
    "hash_token",
    "is_origin_allowed",
    "origin_of",
    "rehash_if_needed",
    "validate_password",
    "verify_password",
]
