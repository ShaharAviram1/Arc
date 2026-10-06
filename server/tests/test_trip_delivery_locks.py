"""Trip writers that must not cross (review of M19 T4).

Two real sessions on committed rows: one holds its locks with the transaction
open while the other runs, and the second must wait and then see the first's
result rather than overwrite it. "Waiting" is asserted as "not finished after
a short pause", which only ever errs towards a false pass, never a flake.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

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
from arc.services.trips.deliver import ask_again, confirm_delivered
from arc.services.trips.sweep import TripSweep, sweep_trips
from tests.acquisition_helpers import acquisition_settings
from tests.conftest import add_user

pytestmark = pytest.mark.pg

#: How long the second writer is given to (wrongly) get through.
PAUSE = 0.4


async def a_trip(
    factory: SessionFactory,
    *,
    email: str,
    anilist_id: int,
    state: TripEpisodeState,
    available_at: datetime,
    delivered_at: datetime | None = None,
) -> tuple[int, int, int]:
    """A user, a one-episode active trip with its row in ``state``. Committed."""
    user = await add_user(factory, email, "password12345")
    async with factory() as session:
        anime = Anime(anilist_id=anilist_id, title_romaji="Lock Show", status="FINISHED")
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
            deadline_at=datetime.now(UTC) + timedelta(days=14),
        )
        session.add(trip)
        await session.flush()
        session.add(
            TripEpisode(
                trip_id=trip.id,
                episode_id=episode.id,
                state=state,
                available_at=available_at,
                delivered_at=delivered_at,
            )
        )
        await session.commit()
        return user.id, trip.id, episode.id


async def sweep_in(factory: SessionFactory) -> TripSweep:
    async with factory() as session:
        swept = await sweep_trips(session, now=datetime.now(UTC))
        await session.commit()
        return swept


async def test_the_sweep_does_not_expire_a_row_being_confirmed(
    api_factory: SessionFactory,
) -> None:
    user_id, trip_id, episode_id = await a_trip(
        api_factory,
        email="lock-confirm@arc.test",
        anilist_id=987001,
        state=TripEpisodeState.PENDING,
        available_at=datetime.now(UTC) - timedelta(days=20),  # expiry due
    )
    async with api_factory() as confirming:
        user = await confirming.get(User, user_id)
        assert user is not None
        await confirm_delivered(
            confirming,
            user=user,
            trip_id=trip_id,
            episode_id=episode_id,
            etag=None,
            now=datetime.now(UTC),
        )
        sweeping = asyncio.create_task(sweep_in(api_factory))
        await asyncio.sleep(PAUSE)
        assert not sweeping.done(), "the sweep waits for the confirmation"
        await confirming.commit()

    swept = await sweeping

    assert swept.expired_rows == [], "the confirmation is not overwritten"
    async with api_factory() as session:
        row = await session.get(TripEpisode, (trip_id, episode_id))
        assert row is not None and row.state is TripEpisodeState.DELIVERED
        trip = await session.get(Trip, trip_id)
        assert trip is not None and trip.state is TripState.FINISHED


async def test_the_sweep_does_not_end_a_trip_being_asked_again(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    user_id, trip_id, episode_id = await a_trip(
        api_factory,
        email="lock-again@arc.test",
        anilist_id=987002,
        state=TripEpisodeState.DELIVERED,
        available_at=now,
        delivered_at=now - timedelta(hours=2),
    )
    settings: Settings = acquisition_settings(tmp_path)
    async with api_factory() as asking:
        user = await asking.get(User, user_id)
        assert user is not None
        await ask_again(
            asking, settings, user=user, trip_id=trip_id, episode_id=episode_id, now=now
        )
        sweeping = asyncio.create_task(sweep_in(api_factory))
        await asyncio.sleep(PAUSE)
        assert not sweeping.done(), "the sweep waits for the trip lock"
        await asking.commit()

    swept = await sweeping

    assert swept.ended == {}, "the re-pended row keeps its trip active"
    async with api_factory() as session:
        state = await session.scalar(select(Trip.state).where(Trip.id == trip_id))
        assert state is TripState.ACTIVE
