"""The migration history and the models must agree, and be reversible.

These are synchronous tests: Alembic's ``env.py`` drives an async engine with
``asyncio.run``, which cannot be nested inside pytest-asyncio's loop. The
``pg_engine`` fixture is requested for its side effect — it is what creates
and migrates the test database.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from arc.models import IN_PROGRESS_INDEX, TRANSCODE_EPISODE_INDEX, Base
from arc.models.settings import DEFAULT_SETTINGS
from tests.conftest import alembic_config

pytestmark = pytest.mark.pg


def _diff(url: str) -> list[Any]:
    """Autogenerate's view of "models minus database"."""

    def compare(connection: Connection) -> list[Any]:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "target_metadata": Base.metadata},
        )
        return compare_metadata(context, Base.metadata)

    async def run() -> list[Any]:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(compare)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _index_definition(url: str, table: str, name: str) -> str | None:
    """``pg_indexes.indexdef`` for one index, or ``None`` if it is not there."""

    async def run() -> str | None:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                found = await connection.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE tablename = :table AND indexname = :name"
                    ),
                    {"table": table, "name": name},
                )
                row = found.first()
                return None if row is None else str(row[0])
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _fetch_settings(url: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(text("SELECT key, value FROM settings"))
                return dict(rows.all())  # type: ignore[arg-type]
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_migrations_round_trip(pg_engine: AsyncEngine, test_database_url: str) -> None:
    """head → base → head. A downgrade that does not work is not a downgrade."""
    config = alembic_config(test_database_url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert _diff(test_database_url) == []


def test_models_and_migrations_agree(pg_engine: AsyncEngine, test_database_url: str) -> None:
    """Autogenerate must find nothing: the migration *is* the models.

    This catches a column added to a model without a migration — the failure
    mode that otherwise only shows up on someone else's machine.
    """
    diff = _diff(test_database_url)

    assert diff == [], f"models and migrations disagree: {diff}"


def test_settings_are_seeded(pg_engine: AsyncEngine, test_database_url: str) -> None:
    """The initial migration seeds every rule with its documented default."""
    seeded = _fetch_settings(test_database_url)

    assert seeded == dict(DEFAULT_SETTINGS)
    assert len(seeded) == 9
    # max_transcodes is env-only (arc.config), never a row here.
    assert "max_transcodes" not in seeded
    # The values the spec names explicitly (FR-A1, FR-A3, FR-T1, FR-T2).
    assert seeded["look_ahead_n"] == 2
    assert seeded["grace_days_g"] == 7
    assert seeded["unwatched_days_d"] == 21
    assert seeded["preferred_resolution"] == "1080p"
    assert seeded["sub_lang"] == "en"
    assert seeded["audio_lang"] == "ja"
    # The kill switch is seeded off: a fresh install acquires (spec §4.2).
    assert seeded["acquisition_paused"] is False


def test_the_history_is_one_squashed_root_and_a_straight_chain(test_database_url: str) -> None:
    """M3b squashed M1–M3's four revisions into one fresh initial schema.

    Nothing had shipped, and a chain whose first revision creates ``anime`` with
    the AniList id as its primary key and whose last one re-keys the table is
    harder to read than the schema it produces. What is asserted now is what
    survives that: exactly one root, one head, and no branches — so a second
    revision is a deliberate link on the end rather than a fork nobody noticed.
    """
    script = ScriptDirectory.from_config(alembic_config(test_database_url))
    revisions = list(script.walk_revisions())

    roots = [rev.revision for rev in revisions if rev.down_revision is None]
    assert roots == ["4d1c3479c036"]
    assert len(script.get_heads()) == 1
    # walk_revisions goes head to root, one step at a time: a straight chain.
    assert len(revisions) == len({rev.revision for rev in revisions})
    assert script.get_current_head() == revisions[0].revision


def test_the_transcode_episode_index_is_created_by_a_migration(
    pg_engine: AsyncEngine, test_database_url: str
) -> None:
    """The one index autogenerate cannot write for itself.

    ``latest_transcode_jobs`` matches on ``payload->>'episode_id'``, which is an
    expression, and the index is partial on ``type = 'transcode'``. Neither is
    something a column-level model declaration can express, so both are written
    out in the migration — and an index that exists only in a migration is an
    index a future squash can silently drop. This is the assertion that notices.
    """
    definition = _index_definition(test_database_url, "jobs", TRANSCODE_EPISODE_INDEX)

    assert definition is not None, f"{TRANSCODE_EPISODE_INDEX} is not on the jobs table"
    assert "payload ->> 'episode_id'" in definition
    assert "WHERE" in definition and "'transcode'" in definition


def test_the_in_progress_index_is_created_by_a_migration(
    pg_engine: AsyncEngine, test_database_url: str
) -> None:
    """The second index autogenerate cannot write for itself.

    ``continue_watching`` reads one user's *unfinished* rows newest first, so
    the index is ordered ``DESC`` and partial on ``completed = false`` —
    neither of which a column-level model declaration can express, so both are
    written out in the migration. An index that lives only in a migration is
    one a future squash can silently drop; this is what notices.
    """
    definition = _index_definition(test_database_url, "watch_progress", IN_PROGRESS_INDEX)

    assert definition is not None, f"{IN_PROGRESS_INDEX} is not on the watch_progress table"
    assert "user_id" in definition and "updated_at DESC" in definition
    assert "WHERE" in definition and "completed = false" in definition
