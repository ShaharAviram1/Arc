"""Login, logout, and "who am I" (spec §2, roadmap M2).

The cookie is set and cleared in exactly one place — :func:`set_session_cookie`
and :func:`clear_session_cookie` — so the flags that make it safe
(``HttpOnly``, ``SameSite=Lax``, ``Secure`` in production) cannot drift apart
between the routes that issue it. :class:`SessionRefreshMiddleware` lives here
for the same reason: it re-issues that cookie, so it must use those flags.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from arc.api.deps import SESSION_REFRESHED_UNTIL, CurrentUser, SessionDep, SettingsDep
from arc.api.schemas import UserOut
from arc.config import Settings
from arc.core.security import MAX_PASSWORD_LENGTH
from arc.models import User
from arc.services.auth import (
    COOKIE_NAME,
    authenticate,
    create_session,
    delete_session,
    normalize_email,
)
from arc.services.auth.ratelimit import LoginRateLimiter

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: One message for "no such account", "wrong password" and "deactivated".
#: Telling them apart is a free user-enumeration and account-status oracle.
INVALID_CREDENTIALS = "invalid credentials"
RATE_LIMITED = "too many login attempts"

MAX_EMAIL_LENGTH = 320


class LoginRequest(BaseModel):
    """Credentials. ``email`` is matched case-insensitively.

    The address is not format-validated: an address that does not exist fails
    the same way a wrong password does, and a 422 here would tell a caller
    which of the two it was without even a lookup.
    """

    # `extra="forbid"`: a field the server does not know is a client bug or a
    # probe, and answering 422 is cheaper than silently ignoring it.
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=MAX_EMAIL_LENGTH)
    #: Deliberately *not* `max_length`-bounded. Pydantic's 422 for a length
    #: violation echoes the offending value back in `input`, which would put
    #: the caller's password in the response body and in any log that keeps
    #: bodies. The length is checked in the handler instead (see `login`).
    password: str = Field(min_length=1)


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Attach the session cookie.

    ``HttpOnly`` keeps it away from any script (so an XSS cannot lift it),
    ``SameSite=Lax`` is the first half of the CSRF defence (the origin check in
    :mod:`arc.api.csrf` is the second), and ``Secure`` is set in production
    only — a dev server on plain ``http://localhost`` would otherwise never
    receive the cookie back.
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=int(settings.session_ttl.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.is_prod,
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    """Expire the cookie. The attributes must match the ones it was set with."""
    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.is_prod,
        path="/",
    )


class SessionRefreshMiddleware(BaseHTTPMiddleware):
    """Re-issue the session cookie whenever this request slid the row forward.

    A session has two halves that expire independently: the ``sessions`` row
    and the cookie's ``Max-Age``. :func:`arc.services.auth.resolve_session`
    pushes the row out on a request more than a day past its last extension,
    and without this the cookie would still die at ``SESSION_TTL_DAYS`` after
    *login* — sliding expiry that does not actually slide.

    Runs after the handler because the decision is made inside it: the
    dependency records the new expiry on ``request.state`` (see
    :data:`arc.api.deps.SESSION_REFRESHED_UNTIL`) and this reads it off the way
    out. A request that did not extend anything sets no header at all, so the
    common case costs one ``getattr``.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)

        until = getattr(request.state, SESSION_REFRESHED_UNTIL, None)
        if until is None:
            return response
        token = request.cookies.get(COOKIE_NAME)
        if not token:  # pragma: no cover - nothing can be extended without one
            return response
        # A handler that issued its own session cookie (login, invite accept)
        # has the newer token; never overwrite it with the one that arrived.
        if any(
            value.startswith(f"{COOKIE_NAME}=") for value in response.headers.getlist("set-cookie")
        ):
            return response

        set_session_cookie(response, token, self.settings)
        return response


def client_ip(request: Request) -> str:
    """The address the rate limiter counts against.

    Behind a reverse proxy this is only the *visitor's* address if uvicorn was
    told to trust ``X-Forwarded-For``; otherwise it is the proxy, and every
    user shares one budget. ``deploy/docker-compose.yml`` therefore runs the
    api with ``--forwarded-allow-ips`` set to the *frontend* compose subnet,
    which only Caddy is attached to. It must never be ``*``: uvicorn then
    takes the leftmost ``X-Forwarded-For`` entry, which is whatever the
    visitor's own browser put there — a free way to spoof an address, evade
    the budget, and lock somebody else out of it.
    """
    return request.client.host if request.client else "unknown"


@router.post(
    "/login",
    response_model=UserOut,
    summary="Sign in with email and password",
    responses={
        401: {"description": INVALID_CREDENTIALS},
        429: {"description": RATE_LIMITED},
    },
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> User:
    limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    ip = client_ip(request)
    email = normalize_email(body.email)

    wait = limiter.retry_after(ip=ip, email=email)
    if wait is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=RATE_LIMITED,
            # Rounded up: a client that obeys a rounded-down value is refused
            # a second time, which looks like the limit never lifting.
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
    # Counted as soon as the attempt is admitted — before the credentials are
    # looked at, and whatever the outcome — so that a wrong password costs a
    # slot and a correct one cannot be used to reset the budget between
    # guesses.
    limiter.record(ip=ip, email=email)

    # No stored password is longer than the policy allows, so an over-long one
    # cannot match anything: refuse it as bad credentials rather than paying
    # Argon2 for an answer that is already known. Not a 422 — that would echo
    # the password back in the validation error.
    if len(body.password) > MAX_PASSWORD_LENGTH:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    user = await authenticate(session, body.email, body.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    issued = await create_session(
        session,
        user.id,
        ttl=settings.session_ttl,
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    set_session_cookie(response, issued.token, settings)
    return user


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Sign out (idempotent)",
)
async def logout(request: Request, session: SessionDep, settings: SettingsDep) -> Response:
    """Delete the session row and clear the cookie.

    Always 204, even with no cookie or a stale one: "log me out" has no
    failure mode worth reporting, and a client tidying up after an expired
    session should not have to handle an error.
    """
    await delete_session(session, request.cookies.get(COOKIE_NAME))
    await session.commit()

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response


@router.get("/me", response_model=UserOut, summary="The signed-in user")
async def me(user: CurrentUser) -> User:
    return user


__all__ = [
    "INVALID_CREDENTIALS",
    "MAX_EMAIL_LENGTH",
    "RATE_LIMITED",
    "SessionRefreshMiddleware",
    "clear_session_cookie",
    "client_ip",
    "router",
    "set_session_cookie",
]
