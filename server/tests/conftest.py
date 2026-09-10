"""Shared test fixtures.

The database fixtures are deliberately loud rather than lenient: if Postgres
is not running, tests marked ``pg`` fail with instructions instead of quietly
skipping, because a silent skip is how a broken schema reaches main. Set
``ARC_SKIP_PG_TESTS=1`` to opt out on purpose (a machine without Docker).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from arc.config import Settings
from arc.db import SessionFactory, create_session_factory
from arc.main import create_app
from arc.models import DEFAULT_SETTINGS, User, UserRole
from arc.services.acquisition import nyaa
from arc.services.auth import create_user
from arc.services.recs import factory as recs_factory

SERVER_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = SERVER_DIR / "alembic.ini"

#: Every state-changing call needs an allowed ``Origin`` (arc/api/csrf.py).
#: This is one of the dev origins, which are allowed whenever ENV is not prod.
ORIGIN = "http://localhost:5173"

#: The admin the ``admin_client`` fixture creates and signs in as.
ADMIN_EMAIL = "admin@arc.test"
ADMIN_PASSWORD = "adminadmin123"

#: A Fernet key for the tests, fixed rather than generated per run so that a
#: failure is reproducible and a ciphertext in a fixture stays readable. It is
#: a throwaway: nothing outside the test suite is ever encrypted with it.
TEST_FERNET_KEY = "3Yq8kK1kQfQ2s5w8n2Zx0aB6cD9eF1gH3iJ5kL7mN9o="

#: A database of its own, so a developer's `arc` dev data is never dropped by
#: a test run.
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://arc:arc@localhost:5432/arc_test"

NO_POSTGRES = """
PostgreSQL is not reachable at {url}.

Start it with:   make dev-db
(or set ARC_SKIP_PG_TESTS=1 to skip the tests that need a database)

Original error: {error}
"""


@pytest.fixture(autouse=True)
def _fresh_nyaa_client() -> Iterator[None]:
    """Give every test its own Nyaa client (arc/services/acquisition/nyaa.py).

    The client is deliberately process-wide in production — one pacing gap and
    one cache for every concurrent search — and that is precisely what a test
    suite must not inherit: the ten-minute cache would answer the next test
    from the previous test's fixture, and the ``httpx.AsyncClient`` inside it
    is bound to an event loop that ends with the test that built it.

    Dropped rather than closed: closing needs an ``await`` and this has to run
    for synchronous tests too. The transport underneath is a mock in every
    test that has one at all.
    """
    yield
    nyaa.reset_shared_client()


@pytest.fixture(autouse=True)
def _fresh_model_chain() -> Iterator[None]:
    """Give every test its own model chain (arc/services/recs/factory.py).

    Process-wide in production so the daily-quota cooldowns outlive one job,
    and for exactly that reason something a test must not inherit: a cooldown
    learned in one test would silently skip an entry in the next, and the SDK
    clients inside are bound to the event loop that built them.

    Dropped rather than closed, like the Nyaa client above: closing needs an
    ``await`` and this has to run for synchronous tests too. Nothing in the
    suite ever *calls* one — every test either has no provider key or replaces
    the chain with a fake — and the two that build a real object never reach
    ``build()``, so no SDK client and no socket is created either.
    """
    yield
    recs_factory.reset_shared_model()


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make every catalogue wait free, and record what it asked for.

    Each pause in the catalogue code — the AniList client's pacing gap and both
    its backoffs, MAL's retry pause, the reconciliation job's spacing — goes
    through a module-level ``_sleep`` precisely so a test can assert on the
    *durations* without paying them. Tests that want the real thing (the pacing
    test) simply do not ask for this fixture.
    """
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    for module in (
        "arc.services.anilist.client",
        "arc.services.mal.catalog",
        "arc.services.catalog.jobs",
    ):
        monkeypatch.setattr(f"{module}._sleep", fake_sleep)
    return recorded


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
        # Encryption is needed by anything that stores a MAL token or seals an
        # OAuth state (M9). Set here rather than per-test so a route that
        # reaches for it never fails on configuration in a test about
        # something else.
        fernet_key=TEST_FERNET_KEY,
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


#: Emptied before the session and after every test that uses ``api_factory``.
#: Order is no longer load-bearing — the cleanup is a single ``TRUNCATE …
#: CASCADE`` (:func:`_truncate`) — but the list is kept grouped by aggregate so
#: it stays readable, and ``CASCADE`` covers anything referencing these that
#: somebody forgets to add.
CLEANUP_TABLES = (
    "jobs",
    "rec_runs",
    "watch_progress",
    # Before ``anime``: ``mal_write_log.anime_id`` is RESTRICT on purpose (the
    # audit trail must outlive a cache prune), so the rows have to go first or
    # the delete below fails.
    "mal_write_log",
    "mal_links",
    "list_entries",
    # Both cascade from ``episodes``; listed for the same reason as the two
    # below, and because a want is keyed by a user as well as an episode.
    "wants",
    "torrents",
    # Both reference ``episodes``: ``media_files.episode_id`` nulls out and
    # ``renditions`` cascades, but deleting them first keeps the order the
    # same shape as the foreign keys and survives an ``ondelete`` changing.
    "media_files",
    "renditions",
    "episodes",
    "anime",
    "sessions",
    "invites",
    "users",
)


async def _reseed_settings(connection: Any) -> None:
    """Put ``settings`` back to exactly what the initial migration seeds.

    ``settings`` is deliberately **not** in :data:`CLEANUP_TABLES`: truncating
    it would leave a database with no seeded rules at all, which is not a state
    the application is ever in and not what ``test_migrations`` compares
    against. It is restored instead — anything a test added is deleted, and
    every seeded key is written back to its :data:`DEFAULT_SETTINGS` value.

    It belongs here rather than in a fixture per test file because the table is
    global state that outlives the transaction that wrote it, and every file
    that touches it would otherwise need a ``finally`` of its own — which is
    precisely what went wrong: the transcode tests set ``sub_lang`` to ``7`` and
    never put it back, and the only thing hiding it was that
    ``test_migrations.py`` happens to collate before ``test_transcode_jobs.py``.
    """
    await connection.execute(
        text("DELETE FROM settings WHERE NOT (key = ANY(:keys))"),
        {"keys": list(DEFAULT_SETTINGS)},
    )
    for key, value in DEFAULT_SETTINGS.items():
        await connection.execute(
            text(
                "INSERT INTO settings (key, value) VALUES (:key, CAST(:value AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"key": key, "value": json.dumps(value)},
        )


async def _truncate(connection: Any) -> None:
    """Empty every table the API tests write to, in one statement.

    ``TRUNCATE`` rather than a sequence of ``DELETE``s, and this is the whole
    fix for a class of cross-test failure. A ``DELETE`` list has to be in
    foreign-key order, and when one of them raises — a constraint the list has
    drifted out of step with — every table after it is left populated. That is
    how a ``users`` row survived a teardown and made every subsequent login in
    the session 401 on a duplicate email.

    One statement, ``CASCADE`` so the order does not matter, and
    ``RESTART IDENTITY`` so ids do not creep up across a long run.

    ``settings`` is the one table that is restored rather than emptied
    (:func:`_reseed_settings`).
    """
    tables = ", ".join(CLEANUP_TABLES)
    await connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    await _reseed_settings(connection)


@pytest.fixture(scope="session", autouse=True)
def clean_slate(test_database_url: str) -> None:
    """Empty the test database once, before anything runs.

    Belt to the per-test teardown's braces: a run killed part-way through — a
    Ctrl-C, a crash, an OOM — leaves rows behind that no ``finally`` will ever
    reach, and without this the *next* invocation inherits them and fails in a
    way that looks nothing like its cause.

    Deliberately silent when there is no database. It is autouse and
    session-scoped, so raising or skipping here would take the entire suite
    with it — including every test that needs no database at all. The pg
    fixtures complain loudly and specifically when they are actually asked for.
    """
    if os.environ.get("ARC_SKIP_PG_TESTS") == "1":
        return

    async def run() -> None:
        engine = create_async_engine(test_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await _truncate(connection)
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except OSError, SQLAlchemyError:
        return


@pytest.fixture
async def api_factory(pg_engine: AsyncEngine) -> AsyncIterator[SessionFactory]:
    """Real (committing) sessions, with the tables emptied afterwards.

    The cleanup is in a ``finally`` so it runs when the test *errors* as well
    as when it fails, and it is one ``TRUNCATE`` so it cannot half-succeed
    (:func:`_truncate`). Between the two, a test that blows up in the middle
    cannot leave rows for the next one to trip over.
    """
    factory = create_session_factory(pg_engine)
    try:
        yield factory
    finally:
        async with pg_engine.begin() as connection:
            await _truncate(connection)


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
