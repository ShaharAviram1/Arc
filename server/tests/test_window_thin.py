"""The deeper look-ahead for thinly seeded shows (FR-A1, owner 2026-10-20).

"Change look ahead back to 3 only on shows that have no seeders anymore": a
show whose newest fetched release (with a seeder figure) recorded fewer than
``slow_swarm_seeders`` seeders keeps ``look_ahead_slow`` episodes ahead instead
of ``look_ahead_n``. Derived at every reconciliation and stored nowhere. The
pure arithmetic first, then the reconciler, the demand rule and the no-cancel
rule against the database.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListStatus,
    Torrent,
    TorrentFile,
    TorrentKind,
    User,
    Want,
    WatchProgress,
)
from arc.services.acquisition.demand import demand
from arc.services.acquisition.holdings import replace_holdings
from arc.services.acquisition.names import QBIT_CANCEL
from arc.services.acquisition.qbit import QBIT_CANCELLED
from arc.services.acquisition.rules import (
    LOOK_AHEAD_KEY,
    LOOK_AHEAD_SLOW_KEY,
    SLOT_CAP_KEY,
    SLOW_SWARM_SEEDERS_KEY,
)
from arc.services.acquisition.wants import (
    _thin_shows,
    compute_wants,
    coverable,
    coverable_once_active,
    show_look_ahead,
    slot_view,
)
from arc.services.catalog.airing import FINISHED
from arc.services.catalog.cache import episodes_for
from arc.services.trips.cancel import cancel_trip
from arc.services.trips.create import create_trip
from tests.acquisition_helpers import (
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    set_setting,
)

#: A 24-minute episode, for the halfway reports.
LENGTH = 1440.0

#: The moment every "older" release was added; "newer" ones are an hour later.
EARLIER = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LATER = EARLIER + timedelta(hours=1)


# --- show_look_ahead(), pure ---------------------------------------------------


@pytest.mark.parametrize(
    ("n", "slow", "thin", "expected"),
    [
        (1, 3, True, 3),
        (1, 3, False, 1),
        (0, 3, True, 0),
        (3, 1, True, 3),
        (2, 2, True, 2),
    ],
)
def test_the_per_show_n(n: int, slow: int, thin: bool, expected: int) -> None:
    assert show_look_ahead(n, slow, thin=thin) == expected


# --- the database ------------------------------------------------------------

pg = pytest.mark.pg


@pytest.fixture
def statements(pg_engine: AsyncEngine) -> Iterator[list[str]]:
    """Every SQL statement the engine executes while the test runs."""
    recorded: list[str] = []

    def before(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        recorded.append(statement)

    event.listen(pg_engine.sync_engine, "before_cursor_execute", before)
    try:
        yield recorded
    finally:
        event.remove(pg_engine.sync_engine, "before_cursor_execute", before)


async def rules(
    session: AsyncSession, *, n: int = 1, slow: int = 3, seeders: int = 20, cap: int = 0
) -> None:
    await set_setting(session, LOOK_AHEAD_KEY, n)
    await set_setting(session, LOOK_AHEAD_SLOW_KEY, slow)
    await set_setting(session, SLOW_SWARM_SEEDERS_KEY, seeders)
    await set_setting(session, SLOT_CAP_KEY, cap)


async def a_show(
    session: AsyncSession, anilist_id: int, *, status: str = FINISHED
) -> tuple[Anime, list[Episode]]:
    anime = await make_anime(session, anilist_id=anilist_id, status=status, episodes=12)
    return anime, await make_episodes(session, anime, 12, aired_through=12)


async def single(
    session: AsyncSession,
    episode: Episode,
    seeders: int | None,
    *,
    at: datetime = EARLIER,
    tag: str = "a",
    qbit_state: str | None = "completed",
) -> Torrent:
    torrent = Torrent(
        episode_id=episode.id,
        info_hash=(tag * 40)[:40],
        seeders=seeders,
        added_at=at,
        qbit_state=qbit_state,
    )
    session.add(torrent)
    await session.flush()
    return torrent


async def batch(
    session: AsyncSession,
    episodes: list[Episode],
    seeders: int | None,
    *,
    at: datetime = EARLIER,
    tag: str = "b",
) -> Torrent:
    torrent = Torrent(
        episode_id=None,
        kind=TorrentKind.BATCH,
        info_hash=(tag * 40)[:40],
        seeders=seeders,
        added_at=at,
    )
    session.add(torrent)
    await session.flush()
    session.add_all(
        TorrentFile(
            torrent_id=torrent.id,
            file_index=index,
            path=f"pack/{episode.number:02}.mkv",
            size=1,
            episode_id=episode.id,
        )
        for index, episode in enumerate(episodes)
    )
    await session.flush()
    return torrent


async def wanted(session: AsyncSession, user: User) -> set[int]:
    rows = await session.scalars(
        select(Want.episode_id).where(Want.user_id == user.id, Want.dropped_at.is_(None))
    )
    return set(rows.all())


async def watched_to(session: AsyncSession, user: User, episode: Episode, fraction: float) -> None:
    session.add(
        WatchProgress(
            user_id=user.id,
            episode_id=episode.id,
            position_s=fraction * LENGTH,
            duration_s=LENGTH,
            completed=fraction >= 0.9,
        )
    )
    await session.flush()


def ids(rows: list[Episode], *numbers: int) -> set[int]:
    return {rows[number - 1].id for number in numbers}


@pg
async def test_a_thin_show_wants_three_ahead_at_n_1(db_session: AsyncSession) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976501)
    await single(db_session, rows[0], 5)
    user = await make_user(db_session, "thin-3@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5, 6, 7)


@pytest.mark.parametrize("seeders", [20, 200])
@pg
async def test_at_or_above_the_threshold_is_n(db_session: AsyncSession, seeders: int) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976502)
    await single(db_session, rows[0], seeders)
    user = await make_user(db_session, "thin-fat@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5)


@pg
async def test_a_threshold_of_zero_turns_it_off(db_session: AsyncSession) -> None:
    await rules(db_session, seeders=0)
    anime, rows = await a_show(db_session, 976503)
    await single(db_session, rows[0], 0)
    user = await make_user(db_session, "thin-off@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5)


@pg
async def test_a_show_with_no_release_yet_is_not_thin(db_session: AsyncSession) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976504)
    user = await make_user(db_session, "thin-none@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5)


@pg
async def test_a_batch_row_counts(db_session: AsyncSession) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976505)
    await batch(db_session, rows[:4], 2)
    user = await make_user(db_session, "thin-batch@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5, 6, 7)


@pg
async def test_the_newest_row_wins(db_session: AsyncSession) -> None:
    await rules(db_session)
    fat_last, fat_rows = await a_show(db_session, 976506)
    await single(db_session, fat_rows[0], 3, at=EARLIER, tag="c")
    await batch(db_session, fat_rows[1:4], 300, at=LATER, tag="d")
    thin_last, thin_rows = await a_show(db_session, 976507)
    await batch(db_session, thin_rows[:3], 300, at=EARLIER, tag="e")
    await single(db_session, thin_rows[3], 3, at=LATER, tag="f")

    assert await _thin_shows(db_session, {fat_last.id, thin_last.id}, below=20) == frozenset(
        {thin_last.id}
    )


@pg
async def test_a_tie_on_added_at_goes_to_the_higher_id(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 976508)
    await single(db_session, rows[0], 300, at=EARLIER, tag="g")
    await single(db_session, rows[1], 3, at=EARLIER, tag="h")

    assert await _thin_shows(db_session, {anime.id}, below=20) == frozenset({anime.id})


@pg
async def test_a_null_seeders_refetch_row_is_skipped(db_session: AsyncSession) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976509)
    await single(db_session, rows[0], 4, at=EARLIER, tag="i")
    # ``_refetch`` clears the figure and stamps the row now.
    await single(db_session, rows[1], None, at=LATER, tag="j")
    user = await make_user(db_session, "thin-refetch@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5, 6, 7)


@pg
async def test_a_batch_tombstone_is_ignored(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 976510)
    await single(db_session, rows[0], 4, at=EARLIER, tag="k")
    # An unreadable pack: a batch row with a figure and no files.
    db_session.add(
        Torrent(
            episode_id=None,
            kind=TorrentKind.BATCH,
            info_hash="l" * 40,
            seeders=500,
            added_at=LATER,
        )
    )
    await db_session.flush()

    assert await _thin_shows(db_session, {anime.id}, below=20) == frozenset({anime.id})


@pg
async def test_the_prefetch_reaches_p_plus_4(db_session: AsyncSession) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976511)
    await single(db_session, rows[0], 5)
    user = await make_user(db_session, "thin-prefetch@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await watched_to(db_session, user, rows[4], 0.6)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5, 6, 7, 8)


@pg
async def test_a_waiting_thin_show_gets_only_its_prefetch(db_session: AsyncSession) -> None:
    await rules(db_session, cap=1)
    airing, airing_rows = await a_show(db_session, 976512, status="RELEASING")
    thin, thin_rows = await a_show(db_session, 976513)
    await single(db_session, thin_rows[0], 5)
    user = await make_user(db_session, "thin-cap@arc.test")
    await make_entry(db_session, user, airing, progress=0)
    await make_entry(db_session, user, thin, progress=4)
    await watched_to(db_session, user, thin_rows[4], 0.6)

    await compute_wants(db_session)

    view = await slot_view(db_session, user.id)
    assert view.waiting == frozenset({thin.id})
    assert await wanted(db_session, user) == ids(airing_rows, 1) | ids(thin_rows, 8), (
        "p+4 is outside the cap; p+1 … p+3 wait for a slot"
    )


@pg
async def test_the_device_skip_does_not_slide(db_session: AsyncSession) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976514)
    await single(db_session, rows[0], 5)
    user = await make_user(db_session, "thin-device@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await replace_holdings(
        db_session,
        user_id=user.id,
        device_id="0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69",
        episode_ids=[rows[5].id],
        now=datetime.now(UTC),
    )

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5, 7), "6 skipped, 8 not added"


@pg
async def test_a_dormant_thin_show_stays_dormant(db_session: AsyncSession) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976515)
    await single(db_session, rows[0], 5)
    user = await make_user(db_session, "thin-dormant@arc.test")
    await make_entry(db_session, user, anime, progress=4, activated=False)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == set()


@pg
async def test_n_zero_wants_nothing_even_when_thin(db_session: AsyncSession) -> None:
    await rules(db_session, n=0)
    anime, rows = await a_show(db_session, 976516)
    await single(db_session, rows[0], 5)
    user = await make_user(db_session, "thin-zero@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == set()


@pg
async def test_a_slow_n_below_n_changes_nothing(db_session: AsyncSession) -> None:
    await rules(db_session, n=2, slow=1)
    anime, rows = await a_show(db_session, 976517)
    await single(db_session, rows[0], 5)
    user = await make_user(db_session, "thin-shallow@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5, 6)


@pg
async def test_coverable_includes_p_plus_3_on_a_thin_show(db_session: AsyncSession) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976518)
    await single(db_session, rows[0], 5)
    user = await make_user(db_session, "thin-demand@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    covered = await coverable(db_session, user.id)

    assert ids(rows, 5, 6, 7) <= covered
    assert rows[7].id not in covered


@pg
async def test_coverable_once_active_follows_the_same_rule(db_session: AsyncSession) -> None:
    await rules(db_session)
    thin, thin_rows = await a_show(db_session, 976519)
    await single(db_session, thin_rows[5], 5)
    fat, fat_rows = await a_show(db_session, 976520)
    await single(db_session, fat_rows[5], 50, tag="m")
    user = await make_user(db_session, "thin-next@arc.test")

    thin_cover = await coverable_once_active(
        db_session, user.id, thin, await episodes_for(db_session, thin.id), None
    )
    fat_cover = await coverable_once_active(
        db_session, user.id, fat, await episodes_for(db_session, fat.id), None
    )

    assert thin_cover == ids(thin_rows, 1, 2, 3)
    assert fat_cover == ids(fat_rows, 1)


async def cancel_jobs(session: AsyncSession) -> list[Job]:
    return list((await session.scalars(select(Job).where(Job.type == QBIT_CANCEL))).all())


@pg
async def test_an_in_flight_want_outlives_the_thin_swarm(db_session: AsyncSession) -> None:
    """The no-cancel rule (owner 2026-10-20): p+2 not started goes, p+3 in flight stays."""
    await rules(db_session)
    anime, rows = await a_show(db_session, 976521)
    await single(db_session, rows[0], 5, tag="n")
    user = await make_user(db_session, "thin-flight@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    assert await wanted(db_session, user) == ids(rows, 5, 6, 7)

    # Episode 7's search picked a release and it is downloading; 6 has not
    # been searched yet. Then a tracker scrape lifts 7's swarm to 100: the
    # newest row is fat, so the show is no longer thin.
    rows[6].state = EpisodeState.DOWNLOADING
    downloading = await single(db_session, rows[6], 5, at=LATER, tag="o", qbit_state="downloading")
    await compute_wants(db_session)
    assert await wanted(db_session, user) == ids(rows, 5, 6, 7), "still thin"
    downloading.seeders = 100
    await db_session.flush()

    result = await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5, 7)
    assert rows[5].state is EpisodeState.NOT_WANTED, "the not-started want went"
    assert rows[6].state is EpisodeState.DOWNLOADING
    assert downloading.qbit_state == "downloading"
    assert result.cancelled == 0
    assert await cancel_jobs(db_session) == []

    # No longer downloading (the bytes have landed): the next reconciliation
    # lets the row go as an ordinary out-of-window want.
    rows[6].state = EpisodeState.DOWNLOADED
    await db_session.flush()
    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5)


@pg
async def test_a_searching_want_goes_when_n_is_turned_down(db_session: AsyncSession) -> None:
    """A search is not protected: releasing it loses no bytes."""
    await rules(db_session, n=3, seeders=0)
    anime, rows = await a_show(db_session, 976522)
    user = await make_user(db_session, "thin-down@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    rows[6].state = EpisodeState.SEARCHING
    await set_setting(db_session, LOOK_AHEAD_KEY, 1)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5)
    assert rows[6].state is EpisodeState.NOT_WANTED


@pg
async def test_a_downloading_want_outlives_n_turned_down(db_session: AsyncSession) -> None:
    await rules(db_session, n=3, seeders=0)
    anime, rows = await a_show(db_session, 976527)
    user = await make_user(db_session, "thin-down-dl@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    rows[6].state = EpisodeState.DOWNLOADING
    downloading = await single(db_session, rows[6], 5, tag="v", qbit_state="downloading")
    await set_setting(db_session, LOOK_AHEAD_KEY, 1)

    result = await compute_wants(db_session)

    assert await wanted(db_session, user) == ids(rows, 5, 7)
    assert rows[6].state is EpisodeState.DOWNLOADING
    assert downloading.qbit_state == "downloading"
    assert result.cancelled == 0


@pg
async def test_n_zero_cancels_an_in_flight_download(db_session: AsyncSession) -> None:
    """N = 0 is the hard stop: the no-cancel rule does not apply."""
    await rules(db_session, n=3, seeders=0)
    anime, rows = await a_show(db_session, 976528)
    user = await make_user(db_session, "thin-stop@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    rows[6].state = EpisodeState.DOWNLOADING
    downloading = await single(db_session, rows[6], 5, tag="w", qbit_state="downloading")
    await set_setting(db_session, LOOK_AHEAD_KEY, 0)

    result = await compute_wants(db_session)

    assert await wanted(db_session, user) == set()
    assert rows[6].state is EpisodeState.NOT_WANTED
    assert downloading.qbit_state == QBIT_CANCELLED
    assert result.cancelled == 1
    assert len(await cancel_jobs(db_session)) == 1


@pytest.mark.parametrize("status", [ListStatus.ON_HOLD, ListStatus.DROPPED])
@pg
async def test_an_in_flight_want_is_shelved_and_cancelled_off_watching(
    db_session: AsyncSession, status: ListStatus
) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976529)
    await single(db_session, rows[0], 5, tag="x")
    user = await make_user(db_session, "thin-hold@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    rows[6].state = EpisodeState.DOWNLOADING
    downloading = await single(db_session, rows[6], 5, at=LATER, tag="y", qbit_state="downloading")

    entry.status = status
    await db_session.flush()
    result = await compute_wants(db_session)

    assert await wanted(db_session, user) == set()
    shelved = await db_session.get(Want, (user.id, rows[6].id))
    assert shelved is not None and shelved.dropped_at is not None
    assert rows[6].state is EpisodeState.NOT_WANTED
    assert downloading.qbit_state == QBIT_CANCELLED
    assert result.cancelled == 1


@pg
async def test_an_in_flight_demand_on_a_cap_held_show_is_left_as_found(
    db_session: AsyncSession,
) -> None:
    """The held-show branch is unchanged: its rows are none of the window's business."""
    await rules(db_session, cap=1)
    airing, airing_rows = await a_show(db_session, 976530, status="RELEASING")
    thin, thin_rows = await a_show(db_session, 976531)
    await single(db_session, thin_rows[0], 5, tag="z")
    user = await make_user(db_session, "thin-held@arc.test")
    await make_entry(db_session, user, airing, progress=0)
    await make_entry(db_session, user, thin, progress=4)
    await compute_wants(db_session)
    assert (await slot_view(db_session, user.id)).waiting == frozenset({thin.id})
    # p+3 is covered on a thin show, so it may be demanded; then it downloads.
    await demand(db_session, user_id=user.id, episode=thin_rows[6], now=datetime.now(UTC))
    await compute_wants(db_session)
    thin_rows[6].state = EpisodeState.DOWNLOADING
    downloading = await single(
        db_session, thin_rows[6], 5, at=LATER, tag="1", qbit_state="downloading"
    )
    await compute_wants(db_session)
    # The swarm grows: no longer thin, so p+3 leaves the covered set.
    downloading.seeders = 100
    await db_session.flush()

    result = await compute_wants(db_session)

    assert (await slot_view(db_session, user.id)).waiting == frozenset({thin.id})
    assert thin_rows[6].id in await wanted(db_session, user)
    assert thin_rows[6].state is EpisodeState.DOWNLOADING
    assert result.cancelled == 0


@pg
async def test_a_trip_only_in_flight_row_still_ends_with_its_trip(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The trip branch is unchanged: a trip's downloading row goes when the trip does."""
    await rules(db_session, seeders=0)
    anime, rows = await a_show(db_session, 976532)
    user = await make_user(db_session, "thin-trip@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    trip = (
        await create_trip(
            db_session, settings, user=user, anime_id=anime.id, count=3, now=datetime.now(UTC)
        )
    ).trip
    await compute_wants(db_session)
    row = await db_session.get(Want, (user.id, rows[6].id))
    assert row is not None and row.trip, "episode 7 is the trip's alone"
    rows[6].state = EpisodeState.DOWNLOADING
    downloading = await single(db_session, rows[6], 5, tag="2", qbit_state="downloading")

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=datetime.now(UTC))
    await compute_wants(db_session)

    assert rows[6].id not in await wanted(db_session, user)
    assert rows[6].state is EpisodeState.NOT_WANTED
    assert downloading.qbit_state == QBIT_CANCELLED


@pg
async def test_a_shared_in_flight_episode_only_one_window_shrank(
    db_session: AsyncSession,
) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976533)
    await single(db_session, rows[0], 5, tag="3")
    shrunk = await make_user(db_session, "thin-shrunk@arc.test")
    other = await make_user(db_session, "thin-other@arc.test")
    await make_entry(db_session, shrunk, anime, progress=4)
    await make_entry(db_session, other, anime, progress=6)
    await compute_wants(db_session)
    assert await wanted(db_session, shrunk) == ids(rows, 5, 6, 7)
    assert rows[6].id in await wanted(db_session, other)
    rows[6].state = EpisodeState.DOWNLOADING
    downloading = await single(db_session, rows[6], 5, at=LATER, tag="4", qbit_state="downloading")
    downloading.seeders = 100
    await db_session.flush()

    await compute_wants(db_session)

    assert await wanted(db_session, shrunk) == ids(rows, 5, 7), "kept: still downloading"
    assert await wanted(db_session, other) == ids(rows, 7), "the other window wants it anyway"

    rows[6].state = EpisodeState.DOWNLOADED
    await db_session.flush()
    await compute_wants(db_session)

    assert await wanted(db_session, shrunk) == ids(rows, 5)
    assert await wanted(db_session, other) == ids(rows, 7)


@pg
async def test_one_pack_spanning_two_shows_marks_both_thin(db_session: AsyncSession) -> None:
    first, first_rows = await a_show(db_session, 976534)
    second, second_rows = await a_show(db_session, 976535)
    await batch(db_session, [first_rows[0], second_rows[0]], 3, tag="5")

    found = await _thin_shows(db_session, {first.id, second.id}, below=20)

    assert found == frozenset({first.id, second.id})


@pg
async def test_an_in_flight_want_still_goes_when_the_entry_goes_dormant(
    db_session: AsyncSession,
) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976523)
    await single(db_session, rows[0], 5, tag="p")
    user = await make_user(db_session, "thin-shelve@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    rows[6].state = EpisodeState.DOWNLOADING
    downloading = await single(db_session, rows[6], 5, at=LATER, tag="q", qbit_state="downloading")

    entry.activated_at = None
    await db_session.flush()
    result = await compute_wants(db_session)

    assert await wanted(db_session, user) == set()
    assert rows[6].state is EpisodeState.NOT_WANTED
    assert result.cancelled == 1
    assert downloading.qbit_state != "downloading"


@pg
async def test_an_in_flight_want_the_user_watched_past_goes(db_session: AsyncSession) -> None:
    await rules(db_session)
    anime, rows = await a_show(db_session, 976524)
    await single(db_session, rows[0], 5, tag="r")
    user = await make_user(db_session, "thin-past@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    rows[6].state = EpisodeState.DOWNLOADING
    downloading = await single(db_session, rows[6], 5, at=LATER, tag="6", qbit_state="downloading")

    entry.progress = 7
    await db_session.flush()
    result = await compute_wants(db_session)

    assert rows[6].id not in await wanted(db_session, user)
    assert rows[6].state is EpisodeState.NOT_WANTED
    assert downloading.qbit_state == QBIT_CANCELLED
    assert result.cancelled == 1


@pg
async def test_thin_shows_is_one_statement(db_session: AsyncSession, statements: list[str]) -> None:
    anime, rows = await a_show(db_session, 976525)
    await single(db_session, rows[0], 5, tag="s")
    await batch(db_session, rows[1:3], 50, at=LATER, tag="t")
    other, other_rows = await a_show(db_session, 976526)
    await single(db_session, other_rows[0], 2, tag="u")
    statements.clear()

    found = await _thin_shows(db_session, {anime.id, other.id}, below=20)

    assert found == frozenset({other.id})
    assert len(statements) == 1

    statements.clear()
    assert await _thin_shows(db_session, {anime.id}, below=0) == frozenset()
    assert await _thin_shows(db_session, set(), below=20) == frozenset()
    assert statements == []
