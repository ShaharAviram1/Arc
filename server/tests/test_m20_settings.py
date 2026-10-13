"""M20's two new rules (T0): the write-path bounds and the lenient readers.

``watched_grace_hours`` (FR-T1, behaviour in M20 T8) and ``min_seeders_warn``
(FR-A16, behaviour in M20 T9) are seeded and editable now; nothing acts on them
yet. The validators are held to the write path's bar (refuse and say why); the
readers to the read path's (a missing or mangled row reads as the default and
logs a warning, never as 0).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import DEFAULT_SETTINGS, Setting, Want
from arc.services.acquisition.rules import MAX_MIN_SEEDERS_WARN, look_ahead_n, min_seeders_warn
from arc.services.acquisition.wants import compute_wants
from arc.services.retention.rules import MAX_WATCHED_GRACE_HOURS, watched_grace_hours
from arc.services.settings import SettingsInvalid, validate
from tests.acquisition_helpers import (
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    set_setting,
)


def test_the_defaults() -> None:
    assert DEFAULT_SETTINGS["look_ahead_n"] == 1
    assert DEFAULT_SETTINGS["watched_grace_hours"] == 6
    assert DEFAULT_SETTINGS["min_seeders_warn"] == 3
    assert MAX_WATCHED_GRACE_HOURS == 168
    assert MAX_MIN_SEEDERS_WARN == 100


# --- The write path ---------------------------------------------------------


def _refused(key: str, value: Any) -> str:
    with pytest.raises(SettingsInvalid) as caught:
        validate({key: value})
    return caught.value.errors[key]


@pytest.mark.parametrize("value", [0, 1, 6, 168])
def test_the_watched_grace_takes_0_to_168_hours(value: int) -> None:
    """0 is legal: "at the next hourly sweep"."""
    assert validate({"watched_grace_hours": value}) == {"watched_grace_hours": value}


@pytest.mark.parametrize("value", [169, -1, True, False, "6", 6.5, None, [6]])
def test_a_bad_watched_grace_is_refused(value: Any) -> None:
    assert _refused("watched_grace_hours", value)


@pytest.mark.parametrize("value", [0, 1, 3, 100])
def test_the_seeder_warning_takes_0_to_100(value: int) -> None:
    """0 is legal: never warn."""
    assert validate({"min_seeders_warn": value}) == {"min_seeders_warn": value}


@pytest.mark.parametrize("value", [101, -1, True, "3", 3.5, None])
def test_a_bad_seeder_warning_is_refused(value: Any) -> None:
    assert _refused("min_seeders_warn", value)


# --- The readers --------------------------------------------------------------


@pytest.mark.pg
async def test_watched_grace_hours_reads_the_row(db_session: AsyncSession) -> None:
    assert await watched_grace_hours(db_session) == 6
    await set_setting(db_session, "watched_grace_hours", 0)
    assert await watched_grace_hours(db_session) == 0
    await set_setting(db_session, "watched_grace_hours", 24)
    assert await watched_grace_hours(db_session) == 24


@pytest.mark.pg
async def test_watched_grace_hours_is_clamped_at_a_week(db_session: AsyncSession) -> None:
    await set_setting(db_session, "watched_grace_hours", 10_000)
    assert await watched_grace_hours(db_session) == MAX_WATCHED_GRACE_HOURS


@pytest.mark.pg
async def test_a_missing_watched_grace_row_reads_as_the_default(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(delete(Setting).where(Setting.key == "watched_grace_hours"))
    assert await watched_grace_hours(db_session) == 6


@pytest.mark.pg
@pytest.mark.parametrize("stored", ["6", True, -3, 2.5, [6], {"hours": 6}])
async def test_a_mangled_watched_grace_reads_as_the_default_and_says_so(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture, stored: Any
) -> None:
    await set_setting(db_session, "watched_grace_hours", stored)
    with caplog.at_level(logging.WARNING, logger="arc.services.retention.rules"):
        assert await watched_grace_hours(db_session) == 6
    assert any("using the default" in record.getMessage() for record in caplog.records)


@pytest.mark.pg
async def test_min_seeders_warn_reads_the_row(db_session: AsyncSession) -> None:
    assert await min_seeders_warn(db_session) == 3
    await set_setting(db_session, "min_seeders_warn", 0)
    assert await min_seeders_warn(db_session) == 0
    await set_setting(db_session, "min_seeders_warn", 500)
    assert await min_seeders_warn(db_session) == MAX_MIN_SEEDERS_WARN


@pytest.mark.pg
async def test_a_missing_seeder_warning_row_reads_as_the_default(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(delete(Setting).where(Setting.key == "min_seeders_warn"))
    assert await min_seeders_warn(db_session) == 3


@pytest.mark.pg
@pytest.mark.parametrize("stored", ["3", True, -1, 3.5, [3]])
async def test_a_mangled_seeder_warning_reads_as_the_default_and_says_so(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture, stored: Any
) -> None:
    await set_setting(db_session, "min_seeders_warn", stored)
    with caplog.at_level(logging.WARNING, logger="arc.services.acquisition.rules"):
        assert await min_seeders_warn(db_session) == 3
    assert any("using the default" in record.getMessage() for record in caplog.records)


# --- N's new default ----------------------------------------------------------


@pytest.mark.pg
async def test_the_default_window_is_one_episode(db_session: AsyncSession) -> None:
    """FR-A1 since M20: one unwatched episode ready per show (the halfway
    prefetch is M20 T4's)."""
    assert await look_ahead_n(db_session) == 1
    anime = await make_anime(db_session, anilist_id=962001)
    rows = await make_episodes(db_session, anime, 12, aired_through=9)
    user = await make_user(db_session, "m20-n1@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    wanted = await db_session.scalars(
        select(Want.episode_id).where(Want.user_id == user.id, Want.dropped_at.is_(None))
    )
    assert set(wanted.all()) == {rows[4].id}
