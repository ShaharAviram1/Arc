"""Several active trips per user, one per show (FR-A12, owner 2026-10-28).

Each trip's delivery, "again", cancel and expiry must leave the user's other
trips exactly as they were, and the copy size estimate the show page checks
the device's room with comes from the server's own copies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    OfflineCopy,
    OfflineCopyState,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    Want,
)
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.acquisition.wants import compute_wants
from arc.services.media.copies import offline_options, settings_key
from arc.services.media.jobs import language_rules
from arc.services.trips.cancel import cancel_trip
from arc.services.trips.create import (
    TRIP_OTHER_DEVICE,
    TripConflict,
    active_trips,
    create_trip,
)
from arc.services.trips.deliver import (
    ask_again,
    confirm_delivered,
    device_may_act,
    release_delivered,
)
from arc.services.trips.estimate import ESTIMATE_SAMPLE, copy_estimate_bytes
from arc.services.trips.hooks import stamp_available
from arc.services.trips.names import FALLBACK_COPY_BYTES
from arc.services.trips.rules import delivered_to, pending_episode_ids
from arc.services.trips.sweep import sweep_trips
from tests.acquisition_helpers import (
    acquisition_settings,
    fake_free_space,
    make_anime,
    make_episodes,
    make_user,
)
from tests.test_trip_delivery import ready_copy

pytestmark = pytest.mark.pg


def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    return acquisition_settings(tmp_path)


async def show(session: AsyncSession, anilist_id: int, count: int = 4) -> list[Episode]:
    anime = await make_anime(session, anilist_id=anilist_id, status="FINISHED", episodes=count)
    return await make_episodes(session, anime, count, aired_through=count)


async def two_trips(
    session: AsyncSession, settings: Settings, email: str, base: int
) -> tuple[User, Trip, list[Episode], Trip, list[Episode]]:
    """One user, two shows, a two-episode trip on each whose copies are waiting."""
    user = await make_user(session, email)
    first = await show(session, base)
    second = await show(session, base + 1)
    trips = []
    for rows in (first, second):
        trip = (
            await create_trip(
                session, settings, user=user, anime_id=rows[0].anime_id, count=2, now=now()
            )
        ).trip
        for episode in rows[:2]:
            episode.state = EpisodeState.NOT_WANTED
            await ready_copy(session, settings, episode.id)
            await stamp_available(session, episode.id, now=now())
        trips.append(trip)
    await session.flush()
    return user, trips[0], first, trips[1], second


async def states(session: AsyncSession, trip: Trip) -> dict[int, TripEpisodeState]:
    rows = await session.execute(
        select(TripEpisode.episode_id, TripEpisode.state)
        .where(TripEpisode.trip_id == trip.id)
        .execution_options(populate_existing=True)
    )
    return {episode_id: state for episode_id, state in rows.all()}


async def test_delivering_one_trip_leaves_the_other_alone(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, one, first, two, second = await two_trips(
        db_session, settings, "many-deliver@arc.test", 986001
    )

    for episode in first[:2]:
        await confirm_delivered(
            db_session, user=user, trip_id=one.id, episode_id=episode.id, etag=None, now=now()
        )

    await db_session.refresh(one)
    await db_session.refresh(two)
    assert one.state is TripState.FINISHED, "its last confirmation ends it"
    assert two.state is TripState.ACTIVE
    assert set((await states(db_session, two)).values()) == {TripEpisodeState.PENDING}
    assert (await delivered_to(db_session, [user.id]))[user.id] == {e.id for e in first[:2]}
    assert await pending_episode_ids(db_session) >= {e.id for e in second[:2]}
    assert [trip.id for trip in await active_trips(db_session, user.id)] == [two.id]

    await release_delivered(
        db_session, user=user, trip_id=one.id, episode_id=first[0].id, now=now()
    )
    assert (await delivered_to(db_session, [user.id]))[user.id] == {first[1].id}


async def test_again_on_one_trip_re_pends_only_its_own_row(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, one, first, two, second = await two_trips(
        db_session, settings, "many-again@arc.test", 986003
    )
    await confirm_delivered(
        db_session, user=user, trip_id=one.id, episode_id=first[0].id, etag=None, now=now()
    )
    await confirm_delivered(
        db_session, user=user, trip_id=two.id, episode_id=second[0].id, etag=None, now=now()
    )

    await ask_again(
        db_session, settings, user=user, trip_id=one.id, episode_id=first[0].id, now=now()
    )

    assert (await states(db_session, one))[first[0].id] is TripEpisodeState.PENDING
    assert (await states(db_session, two))[second[0].id] is TripEpisodeState.DELIVERED


async def test_cancelling_one_trip_keeps_the_others_wants(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, one, first, two, second = await two_trips(
        db_session, settings, "many-cancel@arc.test", 986005
    )
    await compute_wants(db_session)

    await cancel_trip(db_session, user=user, trip_id=one.id, now=now())
    await compute_wants(db_session)

    await db_session.refresh(two)
    assert two.state is TripState.ACTIVE
    assert set((await states(db_session, two)).values()) == {TripEpisodeState.PENDING}
    assert set((await states(db_session, one)).values()) == {TripEpisodeState.CANCELLED}
    live = set(
        (
            await db_session.scalars(
                select(Want.episode_id).where(Want.user_id == user.id, Want.dropped_at.is_(None))
            )
        ).all()
    )
    assert live == {e.id for e in second[:2]}, "the other trip's wants stay, the cancelled go"
    for episode in second[:2]:
        copy = await db_session.get(OfflineCopy, episode.id, populate_existing=True)
        assert copy is not None and copy.state is OfflineCopyState.READY


async def test_the_sweep_ends_one_trip_and_keeps_the_other(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, one, first, two, _ = await two_trips(db_session, settings, "many-sweep@arc.test", 986007)
    at = now()
    rows = await db_session.scalars(select(TripEpisode).where(TripEpisode.trip_id == one.id))
    for row in rows.all():
        row.available_at = None
    one.deadline_at = at - timedelta(minutes=1)
    await db_session.flush()

    swept = await sweep_trips(db_session, now=at)

    assert swept.ended == {one.id: TripState.EXPIRED}
    assert sorted(swept.expired_rows) == sorted((one.id, e.id) for e in first[:2])
    await db_session.refresh(two)
    assert two.state is TripState.ACTIVE
    assert [trip.id for trip in await active_trips(db_session, user.id)] == [two.id]
    # The show whose trip ended may have another at once, beside the other one.
    again = await create_trip(
        db_session, settings, user=user, anime_id=first[0].anime_id, count=1, now=now()
    )
    assert [trip.id for trip in await active_trips(db_session, user.id)] == [
        two.id,
        again.trip.id,
    ]


# --- The copy size estimate -------------------------------------------------


async def current_key(session: AsyncSession, settings: Settings) -> str:
    sub_lang, audio_lang = await language_rules(session)
    return settings_key(offline_options(settings), sub_lang=sub_lang, audio_lang=audio_lang)


async def copies(
    session: AsyncSession, episodes: list[Episode], sizes: list[int], key: str, at: datetime
) -> None:
    for offset, (episode, size) in enumerate(zip(episodes, sizes, strict=True)):
        session.add(
            OfflineCopy(
                episode_id=episode.id,
                state=OfflineCopyState.READY,
                size=size,
                settings_key=key,
                ready_at=at + timedelta(minutes=offset),
            )
        )
    await session.flush()


async def test_the_estimate_falls_back_when_no_copy_was_made_this_way(
    db_session: AsyncSession, settings: Settings
) -> None:
    assert await copy_estimate_bytes(db_session, settings) == FALLBACK_COPY_BYTES
    rows = await show(db_session, 986010, count=2)
    await copies(db_session, rows, [10, 20], "0123456789abcdef", now())

    assert await copy_estimate_bytes(db_session, settings) == FALLBACK_COPY_BYTES


async def test_the_estimate_is_the_median_of_the_newest_current_copies(
    db_session: AsyncSession, settings: Settings
) -> None:
    key = await current_key(db_session, settings)
    rows = await show(db_session, 986011, count=ESTIMATE_SAMPLE + 6)
    at = now()
    # Five old copies far larger than the rest fall outside the newest twenty.
    await copies(db_session, rows[:5], [900_000_000] * 5, key, at - timedelta(days=30))
    newest = [200_000_000 + 1_000_000 * index for index in range(ESTIMATE_SAMPLE)]
    await copies(db_session, rows[5 : 5 + ESTIMATE_SAMPLE], newest, key, at)
    # A preparing row and a copy made another way do not count.
    db_session.add(
        OfflineCopy(
            episode_id=rows[-1].id,
            state=OfflineCopyState.PREPARING,
            size=5,
            settings_key=key,
            ready_at=at + timedelta(days=1),
        )
    )
    await db_session.flush()

    assert await copy_estimate_bytes(db_session, settings) == 209_500_000


async def test_the_estimate_of_an_odd_count_is_the_middle_copy(
    db_session: AsyncSession, settings: Settings
) -> None:
    key = await current_key(db_session, settings)
    rows = await show(db_session, 986012, count=3)
    await copies(db_session, rows, [254_000_000, 227_000_000, 300_000_000], key, now())

    assert await copy_estimate_bytes(db_session, settings) == 254_000_000


# --- Each trip is its own device's (owner, 2026-10-30) ----------------------

IPAD = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
MAC = "0b9a8c7d-6e5f-4a3b-9c2d-1e0f2a3b4c5d"


def test_device_may_act() -> None:
    assert device_may_act(None, None)
    assert device_may_act(None, IPAD)
    assert device_may_act(IPAD, IPAD)
    assert not device_may_act(IPAD, MAC)
    assert not device_may_act(IPAD, None)


async def test_two_devices_each_keep_their_own_trip(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await make_user(db_session, "many-devices@arc.test")
    first = await show(db_session, 986201)
    second = await show(db_session, 986202)
    one = (
        await create_trip(
            db_session,
            settings,
            user=user,
            anime_id=first[0].anime_id,
            count=2,
            now=now(),
            device_id=IPAD,
        )
    ).trip
    two = (
        await create_trip(
            db_session,
            settings,
            user=user,
            anime_id=second[0].anime_id,
            count=2,
            now=now(),
            device_id=MAC,
        )
    ).trip
    assert (one.device_id, two.device_id) == (IPAD, MAC)
    for episode in [*first[:2], *second[:2]]:
        episode.state = EpisodeState.NOT_WANTED
        await ready_copy(db_session, settings, episode.id)
        await stamp_available(db_session, episode.id, now=now())
    await db_session.flush()

    with pytest.raises(TripConflict, match=TRIP_OTHER_DEVICE):
        await confirm_delivered(
            db_session,
            user=user,
            trip_id=one.id,
            episode_id=first[0].id,
            etag=None,
            now=now(),
            device_id=MAC,
        )
    with pytest.raises(TripConflict, match=TRIP_OTHER_DEVICE):
        await ask_again(
            db_session,
            settings,
            user=user,
            trip_id=two.id,
            episode_id=second[0].id,
            now=now(),
            device_id=IPAD,
        )
    assert set((await states(db_session, one)).values()) == {TripEpisodeState.PENDING}

    await confirm_delivered(
        db_session,
        user=user,
        trip_id=one.id,
        episode_id=first[0].id,
        etag=None,
        now=now(),
        device_id=IPAD,
    )
    await confirm_delivered(
        db_session,
        user=user,
        trip_id=two.id,
        episode_id=second[0].id,
        etag=None,
        now=now(),
        device_id=MAC,
    )
    with pytest.raises(TripConflict, match=TRIP_OTHER_DEVICE):
        await release_delivered(
            db_session,
            user=user,
            trip_id=one.id,
            episode_id=first[0].id,
            now=now(),
            device_id=MAC,
        )
    assert (await delivered_to(db_session, [user.id]))[user.id] == {first[0].id, second[0].id}

    # Cancelling stays the user's, from any device: it names none.
    await cancel_trip(db_session, user=user, trip_id=two.id, now=now())
    assert [trip.id for trip in await active_trips(db_session, user.id)] == [one.id]
