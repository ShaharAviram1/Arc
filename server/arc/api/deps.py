"""Shared FastAPI dependencies.

M2+ adds the auth/current-user dependencies here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.db import get_session


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
