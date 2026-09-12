"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from arc import __version__
from arc.api import (
    acquisition,
    anime,
    auth,
    catalog,
    catalogue,
    health,
    home,
    invites,
    jobs,
    mal,
    media,
    media_stream,
    playback,
    recs,
    retention,
    review,
    schedule,
    users,
)
from arc.api import list as list_api
from arc.api import settings as settings_api
from arc.api.auth import SessionRefreshMiddleware
from arc.api.csrf import OriginCheckMiddleware
from arc.config import Settings, get_settings
from arc.core import config_check
from arc.core.logging import setup_logging
from arc.core.security import allowed_origins
from arc.db import create_engine, create_session_factory
from arc.services.auth import LoginRateLimiter, RateLimitWindow, bootstrap_admin
from arc.services.catalog.factory import create_catalog

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database engine for the lifetime of the process.

    The engine and the session factory go on ``app.state`` so that
    :func:`arc.db.get_session` can reach them without a module-level global.
    A failed startup ping is logged, not raised: ``/api/health`` is a
    liveness probe with no database dependency, and an API that stays up
    while Postgres restarts is more useful than one that exits.
    """
    settings: Settings = app.state.settings
    log.info("api starting", extra={"env": settings.env, "version": __version__})

    # One ERROR line per production key that is missing or still holds an
    # example value (arc/core/config_check.py). Loud, but not fatal: an
    # operator mid-deploy is better served by an API that runs and complains
    # than by one that will not start. ``GET /api/health`` reports the count.
    config_check.log_warnings(settings, component="api")

    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        log.info("database connected", extra={"url": _safe_url(settings.database_url)})
    except (SQLAlchemyError, OSError) as exc:
        log.warning(
            "database not reachable at startup",
            extra={"url": _safe_url(settings.database_url), "error": str(exc)},
        )

    # First-boot admin (roadmap M2). Idempotent, and never overwrites an
    # existing account; a failure here is logged rather than raised, for the
    # same reason the ping above is — the API staying up is worth more than a
    # clean exit, and the operator can see the line either way.
    try:
        await bootstrap_admin(app.state.session_factory, settings)
    except (SQLAlchemyError, OSError) as exc:
        log.warning("bootstrap admin skipped", extra={"error": str(exc)})

    try:
        yield
    finally:
        await app.state.catalog.aclose()
        # Built lazily by the recommendations router, so it is usually absent.
        recs_model = getattr(app.state, "recs_model", None)
        closer = getattr(recs_model, "aclose", None)
        if callable(closer):
            await closer()
        await engine.dispose()
        log.info("database engine disposed")
        log.info("api stopped")


def _safe_url(url: str) -> str:
    """``postgresql+asyncpg://arc:pw@host/db`` → ``…@host/db`` for logging."""
    _, _, tail = url.rpartition("@")
    return tail or url


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Accepts an explicit ``Settings`` so tests can construct an app without
    touching the process-wide singleton.
    """
    settings = settings or get_settings()
    setup_logging(settings)

    # The interactive docs are a development tool: in production they publish
    # a complete, machine-readable map of the API — every route, every field,
    # every error — to anyone who asks, and Arc's own client never reads them.
    # Off means off: /docs, /redoc and /openapi.json all 404.
    published = not settings.is_prod

    app = FastAPI(
        title="Arc",
        version=__version__,
        summary="Self-hosted anime server",
        docs_url="/docs" if published else None,
        redoc_url="/redoc" if published else None,
        openapi_url="/openapi.json" if published else None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    #: One catalogue for the whole app: its AniList client's pacing then
    #: reflects the real request rate rather than one caller's guess at it
    #: (architecture.md §6), and its circuit breaker is shared, so an outage
    #: found by a search is not rediscovered — at the cost of a timeout — by
    #: the show page a second later (FR-C6). Built here rather than in the
    #: lifespan because ``ASGITransport`` does not run the lifespan and the
    #: routers must still find it; the lifespan closes it. Tests swap the
    #: attribute for a service over mock transports.
    #:
    #: ``wait_on_rate_limit=False`` because there is a user on the other end of
    #: every call this catalogue makes: a 429 falls straight through to MAL
    #: rather than holding the request open for AniList's ``Retry-After``
    #: (§6). The worker's catalogue keeps the wait.
    app.state.catalog = create_catalog(settings, wait_on_rate_limit=False)
    #: One limiter per app, so two apps in one process (a test suite) cannot
    #: exhaust each other's login budget.
    app.state.login_rate_limiter = LoginRateLimiter(
        per_ip=settings.login_rate_limit_per_ip,
        per_email=settings.login_rate_limit_per_email,
        window_seconds=settings.login_rate_window_seconds,
    )
    #: The two public invite routes get their own, looser per-IP budget. They
    #: are unauthenticated and they answer a question about a secret ("is this
    #: token good?"), which is exactly the shape of an endpoint worth grinding.
    app.state.invite_rate_limiter = RateLimitWindow(
        settings.invite_rate_limit_per_ip, settings.login_rate_window_seconds
    )

    origins = allowed_origins(
        settings.public_url, is_prod=settings.is_prod, extra=settings.extra_origins
    )
    # Starlette runs middleware in *reverse* order of addition: the last one
    # added is the outermost. So this list reads inside-out — session refresh
    # closest to the router, then the origin check, then CORS wrapping both.
    #
    # CORS belongs outermost so that *every* response leaves through it,
    # including the origin check's 403. Added the other way round, that one
    # response short-circuits before CORS ever sees it — which happens to be
    # invisible today only because the two share an allow-list, so a refused
    # origin would get no grant either way. The moment those lists differ, or
    # anything else answers early, the ordering is the difference between a
    # browser reporting the status Arc sent and reporting "blocked by CORS".
    app.add_middleware(SessionRefreshMiddleware, settings=settings)
    app.add_middleware(OriginCheckMiddleware, allowed=origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(invites.router)
    app.include_router(users.router)
    app.include_router(jobs.router)
    app.include_router(settings_api.router)
    app.include_router(anime.router)
    app.include_router(catalog.router)
    app.include_router(catalogue.router)
    app.include_router(list_api.router)
    app.include_router(schedule.router)
    app.include_router(home.router)
    app.include_router(review.router)
    app.include_router(acquisition.router)
    app.include_router(retention.router)
    app.include_router(media.router)
    app.include_router(playback.router)
    app.include_router(mal.router)
    app.include_router(recs.router)
    # Not under ``/api``: the streaming routes are proxied as their own prefix
    # and are asked for by a media element rather than by the client's query
    # layer (arc/api/media_stream.py). They take the same session dependency.
    app.include_router(media_stream.router)
    return app


app = create_app()
