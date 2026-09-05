"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from arc import __version__
from arc.api import health, jobs
from arc.config import Settings, get_settings
from arc.core.logging import setup_logging
from arc.db import create_engine, create_session_factory

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

    try:
        yield
    finally:
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

    app = FastAPI(
        title="Arc",
        version=__version__,
        summary="Self-hosted anime server",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(jobs.router)
    return app


app = create_app()
