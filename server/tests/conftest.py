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
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from arc.config import Settings
from arc.db import SessionFactory, create_session_factory
from arc.main import create_app
from arc.models import User, UserRole
from arc.services.auth import create_user

SERVER_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = SERVER_DIR / "alembic.ini"

#: Every state-changing call needs an allowed ``Origin`` (arc/api/csrf.py).
#: This is one of the dev origins, which are allowed whenever ENV is not prod.
ORIGIN = "http://localhost:5173"

#: The admin the ``admin_client`` fixture creates and signs in as.
ADMIN_EMAIL = "admin@arc.test"
ADMIN_PASSWORD = "adminadmin123"

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


# --- API against the real test database -------------------------------------
#
# The ``db_session`` fixture below wraps a test in a transaction that is rolled
# back, which is wrong for anything about *concurrent* transactions (the job
# queue, single-use invites) or about a client making several requests. These
# fixtures commit for real and clean up afterwards.


@pytest.fixture
async def api_factory(pg_engine: AsyncEngine) -> AsyncIterator[SessionFactory]:
    """Real (committing) sessions, with the account tables emptied afterwards.

    Order matters on the way out: ``sessions`` and ``invites`` both reference
    ``users``. ``sessions`` cascades and ``invites.created_by`` nulls out, but
    deleting explicitly and in order keeps the intent obvious.
    """
    factory = create_session_factory(pg_engine)
    try:
        yield factory
    finally:
        async with pg_engine.begin() as connection:
            for table in ("jobs", "sessions", "invites", "users"):
                await connection.execute(text(f"DELETE FROM {table}"))


@pytest.fixture
def api_app(settings: Settings, pg_engine: AsyncEngine, api_factory: SessionFactory) -> FastAPI:
    """An app wired to the test database.

    ``ASGITransport`` does not run the lifespan, which is what normally puts
    the engine and session factory on ``app.state`` (and what runs the admin
    bootstrap); they are set here instead. One app per test, so each gets a
    fresh login rate limiter.
    """
    app = create_app(settings)
    app.state.engine = pg_engine
    app.state.session_factory = api_factory
    return app


def api_transport(app: FastAPI, *, origin: str | None = ORIGIN) -> AsyncClient:
    """A client for ``app``, sending ``origin`` on every request by default.

    ``origin=None`` gives a client that sends no ``Origin`` at all — what a
    cross-site form post or a careless script looks like.
    """
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": origin} if origin else None,
    )


@pytest.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An anonymous HTTP client against the test database."""
    async with api_transport(api_app) as client:
        yield client


async def add_user(
    factory: SessionFactory,
    email: str,
    password: str,
    *,
    role: UserRole = UserRole.USER,
    is_active: bool = True,
) -> User:
    """Create an account directly, bypassing the invite flow."""
    async with factory() as session:
        user = await create_user(session, email, password, role=role)
        user.is_active = is_active
        await session.commit()
        return user


async def login(client: AsyncClient, email: str, password: str) -> AsyncClient:
    """Sign ``client`` in; the session cookie stays in its jar."""
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
async def admin_client(api_client: AsyncClient, api_factory: SessionFactory) -> AsyncClient:
    """An HTTP client signed in as an admin."""
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    return await login(api_client, ADMIN_EMAIL, ADMIN_PASSWORD)


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
