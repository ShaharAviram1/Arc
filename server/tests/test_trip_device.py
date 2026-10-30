"""A trip's device: the operator's re-home and racing claims (owner, 2026-10-30).

:mod:`arc.services.trips.device` (``canonical_device_id``, ``assign_device``),
the ``trip-device`` command, an "again" on an ended trip claiming nothing, and
two devices confirming an unclaimed trip at once.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from arc.cli import EXIT_FAILED, build_parser, cmd_trip_device
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
)
from arc.services.trips.create import (
    TRIP_NOT_ACTIVE,
    TRIP_OTHER_DEVICE,
    TripConflict,
    TripNotFound,
)
from arc.services.trips.deliver import ask_again, confirm_delivered
from arc.services.trips.device import assign_device, canonical_device_id
from tests.acquisition_helpers import acquisition_settings
from tests.conftest import add_user

pytestmark = pytest.mark.pg

IPAD = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
MAC = "0b9a8c7d-6e5f-4a3b-9c2d-1e0f2a3b4c5d"
V1 = "c232ab00-9414-11ec-b3c8-9e6bdeced846"


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return acquisition_settings(tmp_path)


async def a_trip(
    factory: SessionFactory,
    *,
    email: str,
    anilist_id: int,
    state: TripState = TripState.ACTIVE,
    device_id: str | None = None,
    row_state: TripEpisodeState = TripEpisodeState.PENDING,
) -> tuple[int, int, int]:
    """A user and a one-episode trip. Committed. Returns user, trip and episode ids."""
    user = await add_user(factory, email, "password12345")
    async with factory() as session:
        anime = Anime(anilist_id=anilist_id, title_romaji="Device Show", status="FINISHED")
        session.add(anime)
        await session.flush()
        episode = Episode(anime_id=anime.id, number=1, state=EpisodeState.NOT_WANTED)
        session.add(episode)
        await session.flush()
        trip = Trip(
            user_id=user.id,
            anime_id=anime.id,
            first_number=1,
            last_number=1,
            count=1,
            state=state,
            deadline_at=datetime.now(UTC) + timedelta(days=14),
            device_id=device_id,
        )
        session.add(trip)
        await session.flush()
        session.add(TripEpisode(trip_id=trip.id, episode_id=episode.id, state=row_state))
        await session.commit()
        return user.id, trip.id, episode.id


async def device_of(factory: SessionFactory, trip_id: int) -> str | None:
    async with factory() as session:
        trip = await session.get(Trip, trip_id)
        assert trip is not None
        return trip.device_id


# --- canonical_device_id ----------------------------------------------------


def test_canonical_device_id() -> None:
    assert canonical_device_id(IPAD.upper()) == IPAD
    assert canonical_device_id("{" + IPAD + "}") == IPAD
    for bad in (V1, "not-a-uuid", ""):
        with pytest.raises(ValueError):
            canonical_device_id(bad)


# --- assign_device ----------------------------------------------------------


async def test_the_operator_assigns_clears_and_reassigns(
    api_factory: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    _, trip_id, _ = await a_trip(api_factory, email="dev-assign@arc.test", anilist_id=988001)

    caplog.set_level(logging.INFO, logger="arc.services.trips.device")
    async with api_factory() as session:
        await assign_device(session, trip_id=trip_id, device_id=IPAD)
        await session.commit()
    assert await device_of(api_factory, trip_id) == IPAD
    assert "trip assigned to a device by the operator" in caplog.text

    async with api_factory() as session:
        await assign_device(session, trip_id=trip_id, device_id=MAC)
        await session.commit()
    assert await device_of(api_factory, trip_id) == MAC

    async with api_factory() as session:
        await assign_device(session, trip_id=trip_id, device_id=None)
        await session.commit()
    assert await device_of(api_factory, trip_id) is None


async def test_the_operator_cannot_assign_an_unknown_or_ended_trip(
    api_factory: SessionFactory,
) -> None:
    _, ended, _ = await a_trip(
        api_factory, email="dev-ended@arc.test", anilist_id=988002, state=TripState.FINISHED
    )
    async with api_factory() as session:
        with pytest.raises(TripNotFound):
            await assign_device(session, trip_id=99_999_999, device_id=IPAD)
        with pytest.raises(TripConflict, match=TRIP_NOT_ACTIVE):
            await assign_device(session, trip_id=ended, device_id=IPAD)
    assert await device_of(api_factory, ended) is None


# --- the command ------------------------------------------------------------


def args(**values: Any) -> Any:
    return type("Args", (), {"trip": None, "device": None, "clear": False, **values})()


def test_the_command_wants_a_device_or_clear() -> None:
    parser = build_parser()
    parsed = parser.parse_args(["trip-device", "--trip", "6", "--device", IPAD])
    assert (parsed.trip, parsed.device, parsed.clear) == (6, IPAD, False)
    assert parser.parse_args(["trip-device", "--trip", "6", "--clear"]).clear is True
    for bad in (
        ["trip-device", "--trip", "6"],
        ["trip-device", "--device", IPAD],
        ["trip-device", "--trip", "6", "--device", IPAD, "--clear"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


async def test_the_command_assigns_and_clears(
    api_factory: SessionFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    _, trip_id, _ = await a_trip(api_factory, email="dev-cmd@arc.test", anilist_id=988003)

    async with api_factory() as session:
        assert (
            await cmd_trip_device(session, settings, args(trip=trip_id, device=IPAD.upper())) == 0
        )
    assert await device_of(api_factory, trip_id) == IPAD
    assert f"trip {trip_id}: device {IPAD}" in capsys.readouterr().out

    async with api_factory() as session:
        assert await cmd_trip_device(session, settings, args(trip=trip_id, clear=True)) == 0
    assert await device_of(api_factory, trip_id) is None


async def test_the_command_refuses_a_bad_id_and_an_unknown_or_ended_trip(
    api_factory: SessionFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    _, trip_id, _ = await a_trip(
        api_factory, email="dev-cmd-bad@arc.test", anilist_id=988004, state=TripState.CANCELLED
    )
    async with api_factory() as session:
        missing = int(await session.scalar(select(Trip.id).order_by(Trip.id.desc())) or 0) + 1000
        assert await cmd_trip_device(session, settings, args(trip=trip_id, device=V1)) == (
            EXIT_FAILED
        )
        assert await cmd_trip_device(session, settings, args(trip=missing, device=IPAD)) == (
            EXIT_FAILED
        )
        assert await cmd_trip_device(session, settings, args(trip=trip_id, device=IPAD)) == (
            EXIT_FAILED
        )
    err = capsys.readouterr().err
    assert "not a version 4 UUID" in err
    assert f"no trip with id {missing}" in err
    assert f"trip {trip_id} is not active" in err
    assert await device_of(api_factory, trip_id) is None


# --- claims -----------------------------------------------------------------


async def test_again_on_an_ended_trip_claims_nothing(
    api_factory: SessionFactory, settings: Settings
) -> None:
    user_id, trip_id, episode_id = await a_trip(
        api_factory,
        email="dev-again-ended@arc.test",
        anilist_id=988005,
        state=TripState.FINISHED,
        row_state=TripEpisodeState.DELIVERED,
    )
    async with api_factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        with pytest.raises(TripConflict, match=TRIP_NOT_ACTIVE):
            await ask_again(
                session,
                settings,
                user=user,
                trip_id=trip_id,
                episode_id=episode_id,
                now=datetime.now(UTC),
                device_id=IPAD,
            )
        await session.commit()
    assert await device_of(api_factory, trip_id) is None


async def test_two_devices_confirming_an_unclaimed_trip_at_once_one_wins(
    api_factory: SessionFactory,
) -> None:
    user_id, trip_id, episode_id = await a_trip(
        api_factory, email="dev-race@arc.test", anilist_id=988006
    )

    async def confirm(device: str) -> str:
        async with api_factory() as session:
            user = await session.get(User, user_id)
            assert user is not None
            try:
                await confirm_delivered(
                    session,
                    user=user,
                    trip_id=trip_id,
                    episode_id=episode_id,
                    etag=None,
                    now=datetime.now(UTC),
                    device_id=device,
                )
            except TripConflict as exc:
                await session.rollback()
                return str(exc)
            await session.commit()
            return "ok"

    results = await asyncio.gather(confirm(IPAD), confirm(MAC))

    assert sorted(results) == sorted(["ok", TRIP_OTHER_DEVICE])
    winner = IPAD if results[0] == "ok" else MAC
    assert await device_of(api_factory, trip_id) == winner
    async with api_factory() as session:
        row = await session.get(TripEpisode, (trip_id, episode_id))
        assert row is not None and row.state is TripEpisodeState.DELIVERED
