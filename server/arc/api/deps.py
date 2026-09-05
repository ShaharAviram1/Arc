"""Shared FastAPI dependencies: settings, session, and who is calling.

Three levels of access, used as annotations on a route or as a router-wide
``dependencies=[…]``:

* :data:`OptionalUser` — the signed-in user, or ``None``.
* :data:`CurrentUser` — 401 ``not authenticated`` if there is nobody.
* :data:`AdminUser` — additionally 403 ``admin required`` for a non-admin.

Every ``/api`` route except ``/api/health``, the login/logout pair, and the
two public invite routes takes one of the latter two (spec §7: "all API and
media routes require a session").
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.db import get_session
from arc.models import User, UserRole
from arc.services.auth import COOKIE_NAME, resolve_session

NOT_AUTHENTICATED = "not authenticated"
ADMIN_REQUIRED = "admin required"

#: ``request.state`` attribute set to the new expiry when this request slid a
#: session's row forward. Read by
#: :class:`arc.api.csrf.SessionRefreshMiddleware`, which re-issues the cookie.
SESSION_REFRESHED_UNTIL = "session_refreshed_until"

#: Marker for "the cookie has not been looked up yet on this request".
_UNSET = object()


def get_app_settings(request: Request) -> Settings:
    """The settings the app was built with (not the global singleton).

    Reading them off ``app.state`` keeps routers testable: ``create_app``
    can be handed a ``Settings`` instance and the whole app follows it.
    """
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]

#: One database session per request, from the factory the lifespan built.
#: Routers that write must commit; nothing here commits for them.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_optional_user(
    request: Request, session: SessionDep, settings: SettingsDep
) -> User | None:
    """Resolve the session cookie once per request, caching on ``request.state``.

    The cache matters because a route can depend on this transitively more
    than once (``AdminUser`` on the router *and* ``CurrentUser`` on the
    handler); without it each would be a separate query, and each could
    separately extend the session's expiry.

    When the session *is* extended, the new expiry is recorded on
    ``request.state`` so the cookie can be re-issued with a matching
    ``Max-Age`` on the way out (:data:`SESSION_REFRESHED_UNTIL`).
    """
    cached = getattr(request.state, "auth_user", _UNSET)
    if cached is not _UNSET:
        return cached  # type: ignore[return-value]

    def refreshed(until: datetime) -> None:
        setattr(request.state, SESSION_REFRESHED_UNTIL, until)

    user = await resolve_session(
        session,
        request.cookies.get(COOKIE_NAME),
        ttl=settings.session_ttl,
        on_extend=refreshed,
    )
    request.state.auth_user = user
    return user


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


async def get_current_user(user: OptionalUser) -> User:
    """The signed-in user, or 401."""
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=NOT_AUTHENTICATED)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_admin_user(user: CurrentUser) -> User:
    """The signed-in user if they are an admin, else 403.

    401 and 403 are kept distinct on purpose: the client retries the first by
    sending the user to the login page, and must not do that for the second.
    """
    if user.role is not UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ADMIN_REQUIRED)
    return user


AdminUser = Annotated[User, Depends(get_admin_user)]


__all__ = [
    "ADMIN_REQUIRED",
    "NOT_AUTHENTICATED",
    "SESSION_REFRESHED_UNTIL",
    "AdminUser",
    "CurrentUser",
    "OptionalUser",
    "SessionDep",
    "SettingsDep",
    "get_admin_user",
    "get_app_settings",
    "get_current_user",
    "get_optional_user",
]
