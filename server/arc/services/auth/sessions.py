"""Cookie sessions: create, resolve, delete, purge.

A session is a row in ``sessions`` whose primary key *is* ``sha256(token)``
(architecture.md §4). The raw token exists in exactly one place — the user's
``arc_session`` cookie — so a database dump contains no usable credential and
a lookup is a single indexed equality on the primary key.

Expiry is **sliding**: every request extends a session that is more than
:data:`EXTEND_AFTER` from its last extension, so somebody using Arc daily is
never logged out, while an abandoned session dies ``SESSION_TTL_DAYS`` after
its last use. The "more than a day old" condition is what keeps that cheap —
without it every request would issue an UPDATE.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.core.security import generate_token, hash_token
from arc.models import Session, User

#: Cookie the browser carries the session token in.
COOKIE_NAME = "arc_session"

#: How stale a session's expiry must be before a request refreshes it. One
#: day: long enough that a busy tab writes at most one UPDATE per day, short
#: enough that the effective TTL is indistinguishable from the configured one.
EXTEND_AFTER = timedelta(days=1)

#: Longest user-agent string kept, matching ``sessions.user_agent``.
USER_AGENT_LIMIT = 512


@dataclass(frozen=True, slots=True)
class NewSession:
    """A freshly issued session. ``token`` is returned exactly once."""

    token: str
    expires_at: datetime


async def create_session(
    db: AsyncSession,
    user_id: int,
    *,
    ttl: timedelta,
    user_agent: str | None = None,
) -> NewSession:
    """Insert a session row and return the raw token to put in a cookie.

    Flushed, not committed: logging in also touches the user row (a password
    rehash) and accepting an invite creates the user, and those must land in
    the same transaction as the session or not at all.
    """
    token = generate_token()
    expires_at = datetime.now(UTC) + ttl
    db.add(
        Session(
            id=hash_token(token),
            user_id=user_id,
            expires_at=expires_at,
            user_agent=user_agent[:USER_AGENT_LIMIT] if user_agent else None,
        )
    )
    await db.flush()
    return NewSession(token=token, expires_at=expires_at)


async def resolve_session(
    db: AsyncSession,
    token: str | None,
    *,
    ttl: timedelta,
    on_extend: Callable[[datetime], None] | None = None,
) -> User | None:
    """The signed-in user for ``token``, or ``None``.

    ``None`` covers every failure the same way — no cookie, a tampered or
    unknown token, an expired row, a deactivated account — because the caller
    turns all of them into the same 401 and there is nothing useful to tell
    the client apart from "log in again".

    Deactivation takes effect immediately: ``is_active`` is checked here, on
    every request, rather than only at login, so an admin switching a user off
    ends that user's existing sessions without having to delete rows.

    Commits when it extends the session. That is a write inside what is
    otherwise a read, and it is deliberate: the extension must survive even if
    the request it rode in on later raises.

    ``on_extend`` is called with the *new* expiry whenever the row is pushed
    out, and not otherwise. The caller needs it because the row is only half
    the session: the cookie carries its own ``Max-Age``, and a database row
    that lives for another 30 days behind a cookie the browser drops in one is
    a session that expires exactly as if nothing had been extended. The
    service does not set cookies itself — it has no response to set one on —
    so it reports the fact and :class:`arc.api.csrf.SessionRefreshMiddleware`
    turns it into a ``Set-Cookie``.
    """
    if not token:
        return None

    row = await db.execute(
        select(Session, User)
        .join(User, User.id == Session.user_id)
        .where(Session.id == hash_token(token))
    )
    found = row.first()
    if found is None:
        return None
    session_row: Session = found[0]
    user: User = found[1]

    now = datetime.now(UTC)
    if session_row.expires_at <= now or not user.is_active:
        return None

    if session_row.expires_at - now < ttl - EXTEND_AFTER:
        extended = now + ttl
        session_row.expires_at = extended
        await db.commit()
        if on_extend is not None:
            on_extend(extended)

    return user


async def delete_session(db: AsyncSession, token: str | None) -> bool:
    """Drop the row for ``token``. ``False`` if there was nothing to drop."""
    if not token:
        return False
    result = cast(
        "CursorResult[Any]",
        await db.execute(delete(Session).where(Session.id == hash_token(token))),
    )
    return bool(result.rowcount)


async def purge_expired(db: AsyncSession) -> int:
    """Delete every session past its expiry; returns how many went.

    Expired rows are already refused by :func:`resolve_session`, so this is
    housekeeping rather than a security control — it stops the table growing
    without bound. Run hourly by the worker's scheduler (``arc.worker``).
    """
    result = cast(
        "CursorResult[Any]",
        await db.execute(delete(Session).where(Session.expires_at <= datetime.now(UTC))),
    )
    await db.commit()
    return max(result.rowcount, 0)


__all__ = [
    "COOKIE_NAME",
    "EXTEND_AFTER",
    "USER_AGENT_LIMIT",
    "NewSession",
    "create_session",
    "delete_session",
    "purge_expired",
    "resolve_session",
]
