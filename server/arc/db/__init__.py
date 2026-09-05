"""Database plumbing: declarative base, async engine, session factory.

The engine and the session factory live on ``app.state`` (created in
:func:`arc.main._lifespan`) rather than at module level, so that tests and the
worker can build their own without touching a global. :func:`get_session` is
the FastAPI dependency that hands routers a session for the request.

Every constraint and index is named through ``NAMING_CONVENTION`` so that
Alembic autogenerate produces stable, reversible migrations: without it,
Postgres invents names for unique/check constraints and a later ``drop`` has
nothing reliable to point at.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Final

from fastapi import Request
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from arc.config import Settings

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every Arc model.

    Import :mod:`arc.models` (not this module) when you need a fully
    populated ``Base.metadata``; the models package imports every aggregate.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


type SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine. Creating it does not open a connection."""
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Session factory bound to ``engine``.

    ``expire_on_commit=False`` so that attributes stay readable after a
    commit; otherwise every access after ``await session.commit()`` would
    trigger a lazy refresh, which is an error outside a greenlet context.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, closed at the end.

    The session is not committed here; a router that writes commits
    explicitly, so a request that raises leaves nothing half-written.
    """
    factory: SessionFactory = request.app.state.session_factory
    async with factory() as session:
        yield session


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "SessionFactory",
    "create_engine",
    "create_session_factory",
    "get_session",
]
