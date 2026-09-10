"""Liveness endpoint. Deliberately has no database dependency."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from arc import __version__
from arc.api.deps import SettingsDep
from arc.core import config_check

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    version: str
    env: str
    #: How many production configuration keys are missing or still hold an
    #: example value (arc/core/config_check.py). Present in production only,
    #: and a bare count: this endpoint is unauthenticated, and "FERNET_KEY is
    #: unset" is a sentence worth keeping off a public URL. The details are in
    #: the startup log, one ERROR line each. ``0`` is the healthy answer, and
    #: is what a post-deploy smoke test should assert.
    config_warnings: int | None = None


@router.get(
    "/api/health",
    response_model=Health,
    response_model_exclude_none=True,
    summary="Liveness probe",
)
async def health(settings: SettingsDep) -> Health:
    return Health(
        status="ok",
        version=__version__,
        env=settings.env,
        # ``count`` is zero outside production by construction; ``None`` keeps
        # the key out of the dev response entirely rather than showing a 0 that
        # would read as "checked and fine".
        config_warnings=config_check.count(settings) if settings.is_prod else None,
    )
