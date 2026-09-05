"""Origin checking for state-changing requests (architecture.md §7).

``SameSite=Lax`` already stops a cross-site *form* post from carrying the
session cookie in any current browser, but it is one control and it is the
browser's, not ours. This middleware is the second: every ``POST``, ``PUT``,
``PATCH`` or ``DELETE`` under ``/api/`` must arrive with an ``Origin`` (or,
failing that, a ``Referer``) whose origin is one Arc serves.

Nothing is exempt — login included. A browser sends ``Origin`` on every
cross-origin request and on every same-origin request with a non-``GET``
method, so Arc's own client is never affected.

**Non-browser clients must send ``Origin`` themselves.** ``curl``, a script,
or an integration calling the API needs ``-H 'Origin: <PUBLIC_URL origin>'``
on anything that writes; without it the answer is 403 ``origin not allowed``.
Reads (``GET``, ``HEAD``, ``OPTIONS``) are unaffected — they change nothing,
and requiring a header there would break ``/docs`` and every link.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from arc.core.security import is_origin_allowed

#: Methods that may change state and therefore need an origin.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Only the API is protected. Static assets and the client bundle are served
#: by Caddy and never mutate anything.
PROTECTED_PREFIX = "/api/"

DENIED = "origin not allowed"


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Refuse state-changing API calls from an origin Arc does not serve."""

    def __init__(self, app: ASGIApp, allowed: frozenset[str]) -> None:
        super().__init__(app)
        self.allowed = allowed

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in UNSAFE_METHODS and request.url.path.startswith(PROTECTED_PREFIX):
            if not is_origin_allowed(
                request.headers.get("origin"),
                request.headers.get("referer"),
                self.allowed,
            ):
                return JSONResponse({"detail": DENIED}, status_code=403)
        return await call_next(request)


__all__ = ["DENIED", "PROTECTED_PREFIX", "UNSAFE_METHODS", "OriginCheckMiddleware"]
