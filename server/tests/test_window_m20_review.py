"""M20 T4 review fixes: demand re-checked against the window, races, holdings
housekeeping, and the halfway prefetch outside the slot cap.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    Anime,
    DeviceHolding,
    Episode,
    EpisodeState,
    Job,
    User,
    Want,
    WatchProgress,
)
from arc.services.acquisition.demand import demand
from arc.services.acquisition.holdings import MAX_DEVICES, replace_holdings
from arc.services.acquisition.names import COMPUTE_WANTS
from arc.services.acquisition.rules import SLOT_CAP_KEY
from arc.services.acquisition.wants import (
    DEVICE_HOLDING_DAYS,
    _reconcile,
    compute_wants,
    coverable,
    slot_view,
)
from arc.services.catalog.airing import FINISHED
from arc.services.trips.cancel import cancel_trip
from arc.services.trips.create import create_trip
from tests.acquisition_helpers import (
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    set_setting,
)

pytestmark = pytest.mark.pg

LENGTH = 1440.0
DEVICE = "0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69"


def device(n: int) -> str:
    """A distinct, well-formed v4 UUID per ``n``."""
    return f"{n:08x}-5d3a-4c1b-9a8e-1f2d3c4b5a69"


async def a_show(
    session: AsyncSession, anilist_id: int, *, status: str = FINISHED
) -> tuple[Anime, list[Episode]]:
    anime = await make_anime(session, anilist_id=anilist_id, status=status, episodes=12)
    return anime, await make_episodes(session, anime, 12, aired_through=12)


async def wants_of(session: AsyncSession, user: User) -> dict[int, Want]:
    rows = await session.scalars(
        select(Want).where(Want.user_id == user.id, Want.dropped_at.is_(None))
    )
    return {want.episode_id: want for want in rows.all()}


# --- 1. a demand is re-checked against the window ------------------------------


async def test_a_demand_beyond_a_shrunk_window_goes(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 971001)
    user = await make_user(db_session, "rv-shrink@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await set_setting(db_session, "look_ahead_n", 3)
    await demand(db_session, user_id=user.id, episode=rows[6], now=datetime.now(UTC))
    await compute_wants(db_session)
    assert rows[6].id in await wants_of(db_session, user), "p+3 is covered while N = 3"

    await set_setting(db_session, "look_ahead_n", 1)
    await compute_wants(db_session)

    assert set(await wants_of(db_session, user)) == {rows[4].id}, "left the window: gone"


async def test_a_demanded_trip_episode_stays_a_trip_want_and_goes_with_the_trip(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await a_show(db_session, 971002)
    user = await make_user(db_session, "rv-trip@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    trip = (
        await create_trip(
            db_session, settings, user=user, anime_id=anime.id, count=3, now=datetime.now(UTC)
        )
    ).trip
    await compute_wants(db_session)
    assert (await wants_of(db_session, user))[rows[6].id].trip is True

    await demand(db_session, user_id=user.id, episode=rows[6], now=datetime.now(UTC))
    await compute_wants(db_session)
    want = (await wants_of(db_session, user))[rows[6].id]
    assert want.trip is True and want.demanded_at is not None, "still the trip's"

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=datetime.now(UTC))
    await compute_wants(db_session)

    assert rows[6].id not in await wants_of(db_session, user), "went with the trip"


async def test_an_earlier_demand_does_not_licence_another_outside_the_window(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 971003)
    user = await make_user(db_session, "rv-licence@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    db_session.add(Want(user_id=user.id, episode_id=rows[9].id, demanded_at=datetime.now(UTC)))
    await db_session.flush()

    assert rows[9].id not in await coverable(db_session, user.id)


# --- 2. the reconciler never deletes a demand it did not see ----------------------


async def test_reconcile_leaves_a_demand_newer_than_its_plan(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 971010)
    user = await make_user(db_session, "rv-race@arc.test")
    planned_at = datetime.now(UTC)
    db_session.add(
        Want(
            user_id=user.id,
            episode_id=rows[8].id,
            demanded_at=planned_at + timedelta(seconds=1),
        )
    )
    await db_session.flush()

    await _reconcile(db_session, {}, {(user.id, anime.id)}, now=planned_at, planned_at=planned_at)
    assert await db_session.get(Want, (user.id, rows[8].id)) is not None, "kept"

    await _reconcile(
        db_session,
        {},
        {(user.id, anime.id)},
        now=planned_at,
        planned_at=planned_at + timedelta(minutes=1),
    )
    assert await db_session.get(Want, (user.id, rows[8].id)) is None, "seen, and left the window"


async def test_two_demands_at_once_both_succeed(api_factory: SessionFactory) -> None:
    async with api_factory() as session:
        anime, rows = await a_show(session, 971011)
        user = await make_user(session, "rv-twice@arc.test")
        await make_entry(session, user, anime, progress=4)
        await session.commit()
        user_id, episode_id = user.id, rows[4].id

    async def press() -> None:
        async with api_factory() as session:
            episode = await session.get(Episode, episode_id)
            assert episode is not None
            await demand(session, user_id=user_id, episode=episode, now=datetime.now(UTC))
            await session.commit()

    await asyncio.gather(press(), press())

    async with api_factory() as session:
        want = await session.get(Want, (user_id, episode_id))
        assert want is not None and want.demanded_at is not None


async def test_demand_on_a_ready_episode_writes_nothing(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 971012)
    user = await make_user(db_session, "rv-ready@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    rows[4].state = EpisodeState.READY
    await db_session.flush()

    outcome = await demand(db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC))

    assert outcome.want is None and outcome.state is EpisodeState.READY
    assert await db_session.get(Want, (user.id, rows[4].id)) is None
    assert (await db_session.scalars(select(Job).where(Job.type == COMPUTE_WANTS))).all() == []


# --- 4. holdings: the race, the TTL purge, the cap ----------------------------------


async def test_two_first_reports_at_once_both_succeed(api_factory: SessionFactory) -> None:
    async with api_factory() as session:
        _anime, rows = await a_show(session, 971020)
        user = await make_user(session, "rv-report@arc.test")
        await session.commit()
        user_id, ids = user.id, [rows[4].id, rows[5].id]

    async def report() -> None:
        async with api_factory() as session:
            await replace_holdings(
                session, user_id=user_id, device_id=DEVICE, episode_ids=ids, now=datetime.now(UTC)
            )
            await session.commit()

    await asyncio.gather(report(), report())

    async with api_factory() as session:
        held = await session.scalars(
            select(DeviceHolding.episode_id).where(DeviceHolding.user_id == user_id)
        )
        assert sorted(held.all()) == sorted(ids)


async def test_a_report_purges_the_users_expired_rows(db_session: AsyncSession) -> None:
    _anime, rows = await a_show(db_session, 971021)
    user = await make_user(db_session, "rv-purge@arc.test")
    now = datetime.now(UTC)
    db_session.add(
        DeviceHolding(
            user_id=user.id,
            device_id=device(1),
            episode_id=rows[0].id,
            reported_at=now - timedelta(days=DEVICE_HOLDING_DAYS + 1),
        )
    )
    await db_session.flush()

    await replace_holdings(
        db_session, user_id=user.id, device_id=device(2), episode_ids=[rows[1].id], now=now
    )

    left = await db_session.execute(
        select(DeviceHolding.device_id).where(DeviceHolding.user_id == user.id)
    )
    assert left.scalars().all() == [device(2)]


async def test_at_most_ten_devices_are_kept_per_user(db_session: AsyncSession) -> None:
    _anime, rows = await a_show(db_session, 971022)
    user = await make_user(db_session, "rv-cap@arc.test")
    start = datetime.now(UTC) - timedelta(hours=1)
    for n in range(MAX_DEVICES + 1):
        await replace_holdings(
            db_session,
            user_id=user.id,
            device_id=device(n),
            episode_ids=[rows[0].id],
            now=start + timedelta(minutes=n),
        )

    kept = set(
        (
            await db_session.scalars(
                select(DeviceHolding.device_id).where(DeviceHolding.user_id == user.id)
            )
        ).all()
    )
    assert len(kept) == MAX_DEVICES
    assert device(0) not in kept, "the device that reported longest ago went first"


# --- 7. the halfway prefetch is outside the slot cap ---------------------------------


async def test_the_prefetch_is_fetched_with_k_full_and_takes_no_slot(
    db_session: AsyncSession,
) -> None:
    await set_setting(db_session, SLOT_CAP_KEY, 1)
    airing, airing_rows = await a_show(db_session, 971030, status="RELEASING")
    watched, watched_rows = await a_show(db_session, 971031)
    user = await make_user(db_session, "rv-prefetch@arc.test")
    await make_entry(db_session, user, airing, progress=0)
    await make_entry(db_session, user, watched, progress=4)
    # The show being watched: episode 5 has arrived and is half-way watched.
    watched_rows[4].state = EpisodeState.READY
    db_session.add(Want(user_id=user.id, episode_id=watched_rows[4].id))
    await db_session.flush()
    await compute_wants(db_session)
    assert airing_rows[0].id in await wants_of(db_session, user), "the airing show fetches"
    assert watched_rows[5].id not in await wants_of(db_session, user)

    db_session.add(
        WatchProgress(
            user_id=user.id,
            episode_id=watched_rows[4].id,
            position_s=0.51 * LENGTH,
            duration_s=LENGTH,
        )
    )
    await db_session.flush()
    await compute_wants(db_session)

    assert watched_rows[5].id in await wants_of(db_session, user), "fetched with K full"
    assert watched_rows[5].state is EpisodeState.WANTED
    # And with the prefetch in flight, the show neither waits nor occupies.
    await compute_wants(db_session)
    view = await slot_view(db_session, user.id)
    assert view.fetching == 1, "only the airing show holds the slot"
    assert watched.id not in view.waiting
    assert watched_rows[5].id in await wants_of(db_session, user)


async def test_the_prefetch_of_a_show_waiting_for_a_slot_is_still_fetched(
    db_session: AsyncSession,
) -> None:
    """N = 2 so the watched show has a hungry episode of its own and waits."""
    await set_setting(db_session, "look_ahead_n", 2)
    await set_setting(db_session, SLOT_CAP_KEY, 1)
    airing, _airing_rows = await a_show(db_session, 971032, status="RELEASING")
    watched, watched_rows = await a_show(db_session, 971033)
    user = await make_user(db_session, "rv-prefetch-wait@arc.test")
    await make_entry(db_session, user, airing, progress=0)
    await make_entry(db_session, user, watched, progress=4)
    watched_rows[4].state = EpisodeState.READY
    db_session.add(Want(user_id=user.id, episode_id=watched_rows[4].id))
    db_session.add(
        WatchProgress(
            user_id=user.id,
            episode_id=watched_rows[4].id,
            position_s=0.6 * LENGTH,
            duration_s=LENGTH,
        )
    )
    await db_session.flush()

    await compute_wants(db_session)

    wanted = await wants_of(db_session, user)
    assert watched_rows[5].id not in wanted, "p+2 is the window's, and the show waits"
    assert watched_rows[6].id in wanted, "p+3 is the prefetch: never refused"
    assert (await slot_view(db_session, user.id)).waiting == frozenset({watched.id})
