"""Shared FastAPI dependencies.

M2+ adds the session/current-user dependencies here; M1 adds the DB session.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from arc.config import Settings


def get_app_settings(request: Request) -> Settings:
    """The settings the app was built with (not the global singleton).

    Reading them off ``app.state`` keeps routers testable: ``create_app``
    can be handed a ``Settings`` instance and the whole app follows it.
    """
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
