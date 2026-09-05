"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from arc import __version__
from arc.api import health
from arc.config import Settings, get_settings
from arc.core.logging import setup_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.info("api starting", extra={"env": settings.env, "version": __version__})
    # TODO(M1): create the async engine/sessionmaker here and dispose on exit.
    yield
    log.info("api stopped")


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
    return app


app = create_app()
