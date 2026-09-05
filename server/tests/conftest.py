"""Shared test fixtures.

The database fixtures are deliberately loud rather than lenient: if Postgres
is not running, tests marked ``pg`` fail with instructions instead of quietly
skipping, because a silent skip is how a broken schema reaches main. Set
``ARC_SKIP_PG_TESTS=1`` to opt out on purpose (a machine without Docker).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from arc.config import Settings
from arc.main import create_app

SERVER_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = SERVER_DIR / "alembic.ini"

#: A database of its own, so a developer's `arc` dev data is never dropped by
#: a test run.
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://arc:arc@localhost:5432/arc_test"

NO_POSTGRES = """
PostgreSQL is not reachable at {url}.

Start it with:   make dev-db
(or set ARC_SKIP_PG_TESTS=1 to skip the tests that need a database)

Original error: {error}
"""


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    """Settings for tests, isolated from any developer ``.env``.

    ``database_url`` is pinned to the throwaway test database rather than left
    at the field default (``…/arc``): anything built from these settings — an
    app, an engine, a service — must not be one careless ``commit()`` away
    from writing to a developer's dev database.
    """
    return Settings(  # type: ignore[call-arg]
        env="test",
        database_url=test_database_url,
        _env_file=None,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- Database ---------------------------------------------------------------


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Where the pg-marked tests run. Never the dev or production database."""
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def alembic_config(url: str) -> Config:
    """An Alembic config pointed at ``url``.

    ``env.py`` only falls back to ``DATABASE_URL`` when nothing has set
    ``sqlalchemy.url``, so setting it here keeps migrations off the dev
    database no matter what the environment says.
    """
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(SERVER_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


async def _ensure_database(url: str) -> None:
    """Create the test database if it is missing.

    Connects to the ``postgres`` maintenance database, because ``CREATE
    DATABASE`` cannot run from inside the database being created.
    """
    parsed = make_url(url)
    name = parsed.database
    assert name, "TEST_DATABASE_URL must name a database"

    admin = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
    )
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            # No parameters in DDL; the name comes from our own config, and
            # asyncpg's quote_ident equivalent is a plain format here.
            await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()


@pytest.fixture(scope="session")
def pg_engine(test_database_url: str) -> Iterator[AsyncEngine]:
    """A migrated test database, once per session.

    Synchronous on purpose: it runs ``alembic upgrade head``, and Alembic's
    async ``env.py`` calls ``asyncio.run``, which cannot be nested inside a
    running event loop.

    ``NullPool`` matters — pytest-asyncio gives each test its own event loop,
    and an asyncpg connection pooled from a previous, now-closed loop is
    unusable. With no pool, every test opens its own connection.
    """
    if os.environ.get("ARC_SKIP_PG_TESTS") == "1":
        pytest.skip("ARC_SKIP_PG_TESTS=1")

    try:
        asyncio.run(_ensure_database(test_database_url))
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.fail(NO_POSTGRES.format(url=test_database_url, error=exc), pytrace=False)

    command.upgrade(alembic_config(test_database_url), "head")

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        asyncio.run(engine.dispose())


@pytest.fixture
async def db_session(pg_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    Tests may call ``await session.commit()``; ``join_transaction_mode``
    turns that into a savepoint release inside the outer transaction, so the
    database is untouched once the test ends and tests cannot see each
    other's rows.
    """
    async with pg_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()
