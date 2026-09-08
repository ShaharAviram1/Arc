"""Symmetric encryption for secrets Arc has to be able to read back.

Password hashes are one-way and live in :mod:`arc.core.security`; this is the
other half — values Arc stores *and later uses*, which today means a user's
MyAnimeList OAuth tokens and the OAuth ``state`` parameter that carries a link
attempt across the redirect to myanimelist.net and back (architecture.md §7,
FR-M1).

Fernet, from ``cryptography``, is AES-128-CBC with an HMAC-SHA256 tag and a
timestamp, all wrapped in one URL-safe base64 token. Three properties matter
here and all three come free:

* **Authenticated.** A token that was tampered with fails to decrypt rather
  than decrypting to something else, which is what makes it safe to hand the
  OAuth state to a browser and trust what comes back.
* **Timestamped.** :meth:`Fernet.decrypt` takes a ``ttl``, so "this state is
  more than ten minutes old" is a property of the token rather than a
  timestamp Arc has to remember, compare, and clean up. That is what lets the
  OAuth flow be stateless — no table, no row to expire (FR-M1).
* **Rotatable.** ``FERNET_KEY`` is one key today; ``MultiFernet`` accepts a
  list, so rotation later is a config change and a re-encrypt pass, not a
  schema change.

The key is read through :meth:`arc.config.Settings.require`, so a deployment
without ``FERNET_KEY`` boots fine and only fails when somebody tries to link a
MAL account — the same lazy rule every other secret follows.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from arc.config import Settings

#: Re-exported so callers can catch "this ciphertext is not ours" without
#: importing ``cryptography`` themselves. Raised for a wrong key, a corrupted
#: token, a token past its ``ttl``, and anything that is not a Fernet token at
#: all — every one of which means the same thing to a caller: do not trust it.
__all__ = ["InvalidToken", "cipher", "decrypt", "decrypt_json", "encrypt", "encrypt_json"]


@lru_cache(maxsize=4)
def _cipher_for(key: str) -> Fernet:
    """One :class:`Fernet` per key.

    Cached because building it derives and validates the key material, and the
    token-refresh path decrypts on essentially every MAL call. Keyed by the
    key itself rather than by the settings object so that two ``Settings``
    instances with the same key (the app's and a job's) share one cipher.
    """
    return Fernet(key.encode("utf-8"))


def cipher(settings: Settings) -> Fernet:
    """The process cipher, or :class:`~arc.config.ConfigurationError`.

    ``ValueError`` from ``Fernet`` — a key that is not 32 url-safe base64
    encoded bytes — is left to propagate: it is a misconfiguration, and
    turning it into "unavailable" would hide it behind a retry loop.
    """
    return _cipher_for(settings.require("fernet_key"))


def encrypt(settings: Settings, value: str, *, at: datetime | None = None) -> str:
    """``value`` as a Fernet token, ready for a ``*_enc`` column.

    ``at`` stamps the token with an instant other than now. It exists because
    the timestamp Fernet embeds is what :func:`decrypt`'s ``ttl`` is measured
    against, so a caller that has its own notion of "when this was issued" —
    the OAuth state, whose ``issued_at`` must agree with its expiry — has to
    be able to set both from one clock.
    """
    data = value.encode("utf-8")
    box = cipher(settings)
    token = box.encrypt(data) if at is None else box.encrypt_at_time(data, int(at.timestamp()))
    return token.decode("ascii")


def decrypt(settings: Settings, token: str, *, ttl: int | None = None) -> str:
    """The plaintext behind ``token``; :class:`InvalidToken` if it is not ours.

    ``ttl`` is in seconds and is measured against the timestamp Fernet wrote
    into the token, so an expired one is refused here rather than by a clock
    comparison somewhere above.
    """
    return cipher(settings).decrypt(token.encode("ascii"), ttl=ttl).decode("utf-8")


def encrypt_json(settings: Settings, payload: object, *, at: datetime | None = None) -> str:
    """A JSON-serialisable object as one Fernet token."""
    return encrypt(settings, json.dumps(payload, separators=(",", ":")), at=at)


def decrypt_json(settings: Settings, token: str, *, ttl: int | None = None) -> object:
    """The object behind :func:`encrypt_json`.

    A token that decrypts but does not hold JSON raises :class:`InvalidToken`
    too: to a caller it is the same failure — the value cannot be trusted —
    and making it one exception means one ``except`` at the call site.
    """
    plain = decrypt(settings, token, ttl=ttl)
    try:
        return json.loads(plain)
    except ValueError as exc:  # pragma: no cover - only a forged token gets here
        raise InvalidToken from exc
