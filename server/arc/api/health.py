"""Liveness endpoint. Deliberately has no database dependency."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from arc import __version__
from arc.api.deps import SettingsDep

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    version: str
    env: str


@router.get("/api/health", response_model=Health, summary="Liveness probe")
async def health(settings: SettingsDep) -> Health:
    return Health(status="ok", version=__version__, env=settings.env)
