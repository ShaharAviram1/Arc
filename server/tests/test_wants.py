"""The acquisition window: who wants what, and what that starts (FR-A1, A2, W4).

Two halves. :func:`window` is pure, so the first half is a table of
progress/N/aired combinations read as the rule itself. The second half runs
:func:`compute_wants` against the database, which is where merging across
users, dropping on a status change and starting the searches actually happen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    DEFAULT_PRIORITY,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    ListStatus,
    Rendition,
    Want,
)
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
)
from arc.services.acquisition.rules import PAUSED_KEY
from arc.services.acquisition.states import transition
from arc.services.acquisition.wants import (
    REASON_NOT_WANTING,
    STALE_DROP_REASON,
    UNAVAILABLE_RETRY,
    WantsResult,
    compute_wants,
    window,
)
from tests.acquisition_helpers import (
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    set_setting,
)

pytestmark = pytest.mark.pg

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


# --- window(), pure ---------------------------------------------------------


def episodes_with(*, count: int, aired: int) -> list[Episode]:
    """``count`` episodes of which the first ``aired`` are in the past."""
    return [
        Episode(
            anime_id=1,
            number=number,
            air_at=NOW - timedelta(days=aired - number + 1)
            if number <= aired
            else NOW + timedelta(days=number - aired),
        )
        for number in range(1, count + 1)
    ]


@pytest.mark.parametrize(
    ("progress", "look_ahead", "aired", "expected"),
    [
        # The plain case: caught up on 4 of 12 aired, N = 2.
        (4, 2, 12, [5, 6]),
        # A user who has watched nothing wants the first N (FR-A1).
        (0, 2, 12, [1, 2]),
        # N = 1 and N = 3 move the far edge and nothing else.
        (4, 1, 12, [5]),
        (4, 3, 12, [5, 6, 7]),
        # N = 0 is "fetch nothing", not "fetch everything".
        (4, 0, 12, []),
        # Only aired episodes: 6 have aired, the user is on 5, so the window
        # p+1..p+2 is 6 and 7 and only 6 exists to be wanted.
        (5, 2, 6, [6]),
        # Fully caught up on an airing show: nothing until the next one airs.
        (6, 2, 6, []),
        # Ahead of the broadcast (watched elsewhere): still nothing.
        (9, 2, 6, []),
        # A finished show a user is working through: the window slides, it
        # never widens to the whole back catalogue.
        (0, 2, 24, [1, 2]),
        (20, 2, 24, [21, 22]),
        (23, 2, 24, [24]),
    ],
)
def test_the_window_is_the_next_n_aired_episodes(
    progress: int, look_ahead: int, aired: int, expected: list[int]
) -> None:
    picked = window(
        episodes_with(count=24, aired=aired),
        progress=progress,
        look_ahead=look_ahead,
        now=NOW,
        anime_status="RELEASING",
        next_airing=None,
    )

    assert [episode.number for episode in picked] == expected


def test_an_unaired_episode_does_not_hide_an_aired_one_behind_it() -> None:
    """The bound stops the loop, not the first unaired episode in the range.

    Air times are not always monotonic: a delayed episode 5 airing after 6 is
    ordinary enough (a special, a broadcast pushed a week), and skipping the
    rest of the window on the first future date would leave episode 6 unwanted
    until the delay resolved.
    """
    rows = episodes_with(count=6, aired=6)
    rows[4].air_at = NOW + timedelta(days=7)  # episode 5 has been pushed back

    picked = window(
        rows, progress=3, look_ahead=3, now=NOW, anime_status="RELEASING", next_airing=None
    )

    assert [episode.number for episode in picked] == [4, 6]


def test_an_episode_with_no_date_at_all_is_judged_by_the_boundary() -> None:
    """:mod:`arc.services.catalog.airing`'s rule, not a second opinion."""
    rows = episodes_with(count=6, aired=6)
    rows[1].air_at = None

    picked = window(
        rows, progress=0, look_ahead=3, now=NOW, anime_status="RELEASING", next_airing=None
    )

    assert [episode.number for episode in picked] == [1, 2, 3]


def test_a_finished_show_with_no_dates_counts_every_episode_as_aired() -> None:
    rows = [Episode(anime_id=1, number=n, air_at=None) for n in range(1, 13)]

    picked = window(
        rows, progress=3, look_ahead=2, now=NOW, anime_status="FINISHED", next_airing=None
    )

    assert [episode.number for episode in picked] == [4, 5]


def test_the_next_airing_blob_places_the_boundary_for_an_undated_run() -> None:
    """A releasing show whose back catalogue has no dates (FR-C6)."""
    rows = [Episode(anime_id=1, number=n, air_at=None) for n in range(1, 13)]

    picked = window(
        rows,
        progress=3,
        look_ahead=3,
        now=NOW,
        anime_status="RELEASING",
        next_airing={"episode": 7, "airingAt": int(NOW.timestamp()) + 3600},
    )

    assert [episode.number for episode in picked] == [4, 5, 6]


# --- compute_wants(), against the database ----------------------------------


async def wants_of(session: AsyncSession, user_id: int) -> set[int]:
    rows = await session.scalars(
        select(Want.episode_id).where(Want.user_id == user_id, Want.dropped_at.is_(None))
    )
    return set(rows.all())


async def search_jobs(session: AsyncSession) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == SEARCH_RELEASE).order_by(Job.id))
    return list(rows.all())


async def test_a_watching_show_wants_the_next_two_aired_episodes(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=960001)
    rows = await make_episodes(db_session, anime, 12, aired_through=9)
    user = await make_user(db_session, "one@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}
    assert result.added == 2


async def test_a_planned_show_at_zero_progress_wants_the_first_n(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=960002)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "planned@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.PLANNED, progress=0)

    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[0].id, rows[1].id}


async def test_look_ahead_n_is_read_from_settings(db_session: AsyncSession) -> None:
    await set_setting(db_session, "look_ahead_n", 4)
    anime = await make_anime(db_session, anilist_id=960003)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "n4@arc.test")
    await make_entry(db_session, user, anime, progress=2)

    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {row.id for row in rows[2:6]}


async def test_two_users_wanting_one_episode_are_two_rows_and_one_search(
    db_session: AsyncSession,
) -> None:
    """FR-A2: merged wants, one download."""
    anime = await make_anime(db_session, anilist_id=960004)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    alice = await make_user(db_session, "alice@arc.test")
    bob = await make_user(db_session, "bob@arc.test")
    await make_entry(db_session, alice, anime, progress=5)
    await make_entry(db_session, bob, anime, progress=5)

    await compute_wants(db_session)

    assert await wants_of(db_session, alice.id) == {rows[5].id, rows[6].id}
    assert await wants_of(db_session, bob.id) == {rows[5].id, rows[6].id}
    assert len(await search_jobs(db_session)) == 2, "one search per episode, not per user"


async def test_two_users_at_different_progress_merge_into_a_union(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=960005)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    alice = await make_user(db_session, "a2@arc.test")
    bob = await make_user(db_session, "b2@arc.test")
    await make_entry(db_session, alice, anime, progress=2)
    await make_entry(db_session, bob, anime, progress=5)

    await compute_wants(db_session)

    assert await wants_of(db_session, alice.id) == {rows[2].id, rows[3].id}
    assert await wants_of(db_session, bob.id) == {rows[5].id, rows[6].id}
    assert len(await search_jobs(db_session)) == 4


@pytest.mark.parametrize("status", [ListStatus.DROPPED, ListStatus.COMPLETED, ListStatus.ON_HOLD])
async def test_a_show_that_stops_being_watched_drops_its_wants(
    db_session: AsyncSession, status: ListStatus
) -> None:
    """FR-W4 — and on_hold with it, for the same reason.

    Dropped rather than deleted: the row is the moment the episode stopped
    being wanted, and FR-T1's grace period is counted from it. Deleting it
    would leave the sweep judging the files by their own age instead.
    """
    anime = await make_anime(db_session, anilist_id=960006)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, f"drop-{status.value}@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    assert await wants_of(db_session, user.id)

    entry.status = status
    await db_session.flush()
    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == set(), "no live want is left"
    assert result.shelved == 2
    assert result.removed == 0
    for row in rows[4:6]:
        want = await db_session.get(Want, (user.id, row.id))
        assert want is not None, "the row survives, as retention's anchor"
        assert want.dropped_at is not None
        assert want.drop_reason == REASON_NOT_WANTING


async def test_removing_the_show_from_the_list_drops_the_wants(
    db_session: AsyncSession,
) -> None:
    """A deleted list entry is "no longer watching" by another route."""
    anime = await make_anime(db_session, anilist_id=960007)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "gone@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)

    await db_session.delete(entry)
    await db_session.flush()
    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == set()
    assert result.shelved == 2
    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.drop_reason == REASON_NOT_WANTING


async def test_a_second_run_does_not_restamp_a_dropped_want(
    db_session: AsyncSession,
) -> None:
    """``dropped_at`` is when it stopped being wanted, not when it was noticed.

    Restamping it every quarter of an hour would push FR-T1's deletion date
    away for as long as the show sat on hold, which is precisely the state the
    grace period is supposed to be counting through.
    """
    anime = await make_anime(db_session, anilist_id=960025)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "onhold@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    entry.status = ListStatus.ON_HOLD
    await db_session.flush()
    await compute_wants(db_session)
    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is not None
    first = want.dropped_at

    result = await compute_wants(db_session)

    assert want.dropped_at == first
    assert (result.shelved, result.removed) == (0, 0)


async def test_a_show_that_comes_back_revives_its_dropped_wants(
    db_session: AsyncSession,
) -> None:
    """Re-watching an on-hold show fetches the episodes again (FR-W4)."""
    anime = await make_anime(db_session, anilist_id=960026)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "backagain@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    entry.status = ListStatus.ON_HOLD
    await db_session.flush()
    await compute_wants(db_session)
    assert await wants_of(db_session, user.id) == set()

    entry.status = ListStatus.WATCHING
    await db_session.flush()
    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}
    assert result.revived == 2
    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.drop_reason is None


async def test_watching_ahead_slides_the_window_and_deletes_what_fell_out(
    db_session: AsyncSession,
) -> None:
    """The other half of FR-W4: past the window is *deleted*, not tombstoned.

    The user watched it, so ``watch_progress.completed_at`` is the moment
    retention will measure from and the want row has nothing left to say.
    """
    anime = await make_anime(db_session, anilist_id=960008)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "slide@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}

    entry.progress = 6
    await db_session.flush()
    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[6].id, rows[7].id}
    assert await db_session.get(Want, (user.id, rows[4].id)) is None
    assert (result.removed, result.shelved) == (2, 0)


async def test_shrinking_the_window_removes_the_far_edge(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=960009)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "shrink@arc.test")
    await make_entry(db_session, user, anime, progress=3)
    await compute_wants(db_session)
    assert len(await wants_of(db_session, user.id)) == 2

    await set_setting(db_session, "look_ahead_n", 1)
    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[3].id}


async def test_a_re_wanted_episode_has_its_dropped_at_cleared(
    db_session: AsyncSession,
) -> None:
    """A row dropped for anything but FR-T2 comes back when the window covers it."""
    anime = await make_anime(db_session, anilist_id=960010)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "revive@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None
    want.dropped_at = datetime.now(UTC)
    want.drop_reason = "the show was on hold"
    await db_session.flush()

    result = await compute_wants(db_session)

    refreshed = await db_session.get(Want, (user.id, rows[4].id))
    assert refreshed is not None
    assert refreshed.dropped_at is None
    assert refreshed.drop_reason is None
    assert result.revived == 1


async def test_watch_progress_beats_a_stale_list_progress(db_session: AsyncSession) -> None:
    """The furthest *completed* episode is what the window counts from."""
    from arc.models import WatchProgress

    anime = await make_anime(db_session, anilist_id=960011)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "watched@arc.test")
    await make_entry(db_session, user, anime, progress=1)
    db_session.add(
        WatchProgress(
            user_id=user.id,
            episode_id=rows[6].id,
            position_s=1400.0,
            completed=True,
            completed_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[7].id, rows[8].id}


async def test_an_unfinished_watch_does_not_move_the_window(db_session: AsyncSession) -> None:
    from arc.models import WatchProgress

    anime = await make_anime(db_session, anilist_id=960012)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "started@arc.test")
    await make_entry(db_session, user, anime, progress=2)
    db_session.add(
        WatchProgress(user_id=user.id, episode_id=rows[6].id, position_s=90.0, completed=False)
    )
    await db_session.flush()

    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[2].id, rows[3].id}


# --- Stale wants (FR-T2) ----------------------------------------------------
#
# The clock is moved by backdating the episode rather than by waiting: an
# episode "became ready" when its rendition did, and ``state_changed_at`` is
# the fallback when there is no rendition row. Both are written directly here,
# which is what makes "22 days ago" mean 22 days.


async def ready_since(
    session: AsyncSession,
    episode: Episode,
    *,
    days: float,
    entry: ListEntry | None = None,
    rendition: bool = False,
) -> Episode:
    """Put ``episode`` in ``ready`` ``days`` ago, the user quiet ever since.

    The list entry is backdated with it, because the D window runs from the
    later of the two: an episode ready for a month whose owner changed their
    list this morning has not been abandoned, and FR-T2 is only about the ones
    that have.
    """
    moment = datetime.now(UTC) - timedelta(days=days)
    episode.state = EpisodeState.READY
    episode.state_changed_at = moment
    if entry is not None:
        entry.updated_at = moment
    if rendition:
        session.add(
            Rendition(
                episode_id=episode.id,
                dir=f"/data/renditions/{episode.id}",
                playlist_path="index.m3u8",
                ready_at=moment,
            )
        )
    await session.flush()
    return episode


@pytest.mark.parametrize("rendition", [False, True])
async def test_a_want_unwatched_for_d_days_is_dropped(
    db_session: AsyncSession, rendition: bool
) -> None:
    """FR-T2: 22 days ready and unwatched, with D at its default of 21."""
    anime = await make_anime(db_session, anilist_id=960030 + int(rendition))
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, f"stale-{rendition}@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await ready_since(db_session, rows[4], days=22, entry=entry, rendition=rendition)

    result = await compute_wants(db_session)

    dropped = await db_session.get(Want, (user.id, rows[4].id))
    assert dropped is not None
    assert dropped.dropped_at is not None
    assert dropped.drop_reason == STALE_DROP_REASON
    assert result.dropped == 1
    kept = await db_session.get(Want, (user.id, rows[5].id))
    assert kept is not None and kept.dropped_at is None, "the other half of the window is live"


async def test_a_want_unwatched_for_twenty_days_is_kept(db_session: AsyncSession) -> None:
    """D is a threshold, not a mood: a day short of it changes nothing."""
    anime = await make_anime(db_session, anilist_id=960032)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "notyet@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await ready_since(db_session, rows[4], days=20, entry=entry)

    result = await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is None
    assert result.dropped == 0


async def test_a_longer_d_holds_the_want(db_session: AsyncSession) -> None:
    """D is admin-editable (FR-T5) and read on every run."""
    await set_setting(db_session, "unwatched_days_d", 60)
    anime = await make_anime(db_session, anilist_id=960033)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "longd@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await ready_since(db_session, rows[4], days=22, entry=entry)

    assert (await compute_wants(db_session)).dropped == 0


async def test_an_episode_the_user_watched_is_never_dropped_as_stale(
    db_session: AsyncSession,
) -> None:
    """Watching it is what a want is *for*; the window then moves past it."""
    from arc.models import WatchProgress

    anime = await make_anime(db_session, anilist_id=960034)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "watched-stale@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await ready_since(db_session, rows[4], days=22, entry=entry)
    db_session.add(
        WatchProgress(
            user_id=user.id,
            episode_id=rows[4].id,
            position_s=1400.0,
            completed=True,
            completed_at=datetime.now(UTC) - timedelta(days=21),
        )
    )
    await db_session.flush()

    result = await compute_wants(db_session)

    assert result.dropped == 0
    assert await db_session.get(Want, (user.id, rows[4].id)) is None, (
        "the row is deleted by the window sliding, not tombstoned"
    )


async def test_a_stale_drop_survives_the_next_recompute(db_session: AsyncSession) -> None:
    """Otherwise the rule would be undone fifteen minutes after it applied."""
    anime = await make_anime(db_session, anilist_id=960035)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sticky@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await ready_since(db_session, rows[4], days=22, entry=entry)
    await compute_wants(db_session)

    second = await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is not None
    assert (second.revived, second.dropped) == (0, 0), "neither revived nor dropped twice"


async def test_touching_the_show_again_revives_a_stale_drop(db_session: AsyncSession) -> None:
    """An Arc-side change: ``list_entries.updated_at`` moves, ``updated_by`` is arc."""
    anime = await make_anime(db_session, anilist_id=960036)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "cameback@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await ready_since(db_session, rows[4], days=22, entry=entry)
    await compute_wants(db_session)
    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is not None

    # The user edits the show. In production ``updated_at`` is written by the
    # ORM's ``onupdate``; here it is set explicitly, because everything in one
    # test shares a transaction and Postgres's ``now()`` is that transaction's
    # start — which is *before* the drop this is meant to come after.
    entry.score = 8
    entry.updated_at = want.dropped_at + timedelta(minutes=1)
    await db_session.flush()

    result = await compute_wants(db_session)

    revived = await db_session.get(Want, (user.id, rows[4].id))
    assert revived is not None
    assert revived.dropped_at is None and revived.drop_reason is None
    assert result.revived == 1


async def test_a_mal_import_does_not_revive_a_stale_drop(db_session: AsyncSession) -> None:
    """The revival rule in full: an Arc-side action, and nothing else.

    A MAL import writes ``updated_by = mal`` and MAL's own timestamp, and it
    does that for a score as readily as for anything else — so a number typed
    into MyAnimeList months ago, about an episode the user has plainly not
    watched, would otherwise start the download again. The MAL-side changes
    that *should* bring a want back never come through this branch: progress
    moves the window, and a status change takes the show out of
    watching/planned and back (:func:`test_a_show_that_comes_back_revives_its_dropped_wants`).
    """
    from arc.models import UpdatedBy

    anime = await make_anime(db_session, anilist_id=960039)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "malscore@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await ready_since(db_session, rows[4], days=22, entry=entry)
    await compute_wants(db_session)
    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is not None

    # A MAL import: the user scored the show on MyAnimeList and nothing else.
    entry.score = 8
    entry.updated_by = UpdatedBy.MAL
    entry.updated_at = want.dropped_at + timedelta(days=1)
    await db_session.flush()

    result = await compute_wants(db_session)

    still = await db_session.get(Want, (user.id, rows[4].id))
    assert still is not None and still.dropped_at is not None
    assert still.drop_reason == STALE_DROP_REASON
    assert result.revived == 0
    assert rows[4].state is EpisodeState.READY, "and nothing was re-fetched"


async def test_putting_a_stale_show_on_hold_and_back_revives_it(
    db_session: AsyncSession,
) -> None:
    """The route a MAL-side status change takes back into the window.

    Leaving watching/planned re-labels the row: it is no longer "you did not
    watch this", it is "you are not watching this show". Coming back is then
    the ordinary revival, whichever side made either change — which is why the
    stale rule can insist on an Arc-side action without ever trapping a want
    the user has plainly come back to.
    """
    anime = await make_anime(db_session, anilist_id=960041)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "holdandback@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await ready_since(db_session, rows[4], days=22, entry=entry)
    await compute_wants(db_session)
    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.drop_reason == STALE_DROP_REASON
    dropped_at = want.dropped_at

    entry.status = ListStatus.ON_HOLD
    await db_session.flush()
    await compute_wants(db_session)
    assert want.drop_reason == REASON_NOT_WANTING
    assert want.dropped_at == dropped_at, "the grace period does not start over"

    entry.status = ListStatus.WATCHING
    await db_session.flush()
    result = await compute_wants(db_session)

    assert result.revived == 2, "the stale one and the one dropped with the show"
    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}


async def test_a_stale_drop_leaves_the_episode_ready(db_session: AsyncSession) -> None:
    """FR-T2 drops the *want*; the files are retention's business (FR-T1)."""
    anime = await make_anime(db_session, anilist_id=960037)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "stillready@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await ready_since(db_session, rows[4], days=30, entry=entry)

    await compute_wants(db_session)

    assert rows[4].state is EpisodeState.READY


async def test_only_ready_episodes_go_stale(db_session: AsyncSession) -> None:
    """An episode still downloading has not been offered to anybody yet."""
    anime = await make_anime(db_session, anilist_id=960038)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "downloading-stale@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    rows[4].state = EpisodeState.DOWNLOADING
    rows[4].state_changed_at = datetime.now(UTC) - timedelta(days=40)
    await db_session.flush()

    result = await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is None
    assert result.dropped == 0


# --- What the wants start ---------------------------------------------------


async def test_a_wanted_episode_moves_state_and_gets_a_search(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=960013)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "start@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    result = await compute_wants(db_session)

    assert rows[4].state is EpisodeState.WANTED
    assert result.started == 2
    assert {job.payload["episode_id"] for job in await search_jobs(db_session)} == {
        rows[4].id,
        rows[5].id,
    }


async def test_a_second_run_starts_nothing_new(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=960014)
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "idem@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)

    second = await compute_wants(db_session)

    assert (second.added, second.started, second.searches, second.removed) == (0, 0, 0, 0)
    assert len(await search_jobs(db_session)) == 2


@pytest.mark.parametrize(
    "state",
    [
        EpisodeState.DOWNLOADING,
        EpisodeState.DOWNLOADED,
        EpisodeState.MATCHED,
        EpisodeState.PREPARING,
        EpisodeState.READY,
    ],
)
async def test_an_episode_already_in_flight_is_left_alone(
    db_session: AsyncSession, state: EpisodeState
) -> None:
    anime = await make_anime(db_session, anilist_id=960015)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    rows[4].state = state
    user = await make_user(db_session, f"inflight-{state.value}@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert rows[4].state is state
    assert [job.payload["episode_id"] for job in await search_jobs(db_session)] == [rows[5].id]


async def test_an_episode_nobody_wants_any_more_goes_back_to_not_wanted(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=960016)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "release@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    assert rows[4].state is EpisodeState.WANTED

    entry.status = ListStatus.DROPPED
    await db_session.flush()
    result = await compute_wants(db_session)

    assert rows[4].state is EpisodeState.NOT_WANTED
    assert result.released == 2


async def test_a_searching_episode_nobody_wants_is_released_too(
    db_session: AsyncSession,
) -> None:
    """``searching`` is a dead end otherwise: no bytes, and nothing to end it."""
    anime = await make_anime(db_session, anilist_id=960021)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "searching@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    transition(rows[4], EpisodeState.SEARCHING)
    await db_session.flush()

    entry.status = ListStatus.DROPPED
    await db_session.flush()
    await compute_wants(db_session)

    assert rows[4].state is EpisodeState.NOT_WANTED


async def test_a_released_episode_is_acquired_again_when_the_show_comes_back(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=960022)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "readd@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    transition(rows[4], EpisodeState.SEARCHING)
    entry.status = ListStatus.DROPPED
    await db_session.flush()
    await compute_wants(db_session)
    assert rows[4].state is EpisodeState.NOT_WANTED

    entry.status = ListStatus.WATCHING
    await db_session.flush()
    result = await compute_wants(db_session)

    assert rows[4].state is EpisodeState.WANTED
    assert result.started == 2
    assert len(await search_jobs(db_session)) == 2, (
        "the searches queued before the show was dropped still stand, so the "
        "revival deduplicates onto them rather than queueing a second pair"
    )


async def test_a_downloading_episode_is_not_released_when_the_want_goes(
    db_session: AsyncSession,
) -> None:
    """The DoD's last step: the want goes, the download carries on."""
    anime = await make_anime(db_session, anilist_id=960017)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "keep@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    transition(rows[4], EpisodeState.SEARCHING)
    transition(rows[4], EpisodeState.DOWNLOADING)
    await db_session.flush()

    await db_session.delete(entry)
    await db_session.flush()
    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == set()
    assert rows[4].state is EpisodeState.DOWNLOADING


async def test_an_unavailable_episode_is_not_retried_before_its_time(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=960018)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    rows[4].state = EpisodeState.UNAVAILABLE
    rows[4].state_changed_at = datetime.now(UTC)
    rows[4].unavailable_reason = "no acceptable release found"
    user = await make_user(db_session, "unavail@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert rows[4].state is EpisodeState.UNAVAILABLE


async def test_an_unavailable_episode_is_retried_once_the_window_passes(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=960019)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    rows[4].state = EpisodeState.UNAVAILABLE
    rows[4].state_changed_at = datetime.now(UTC) - UNAVAILABLE_RETRY - timedelta(minutes=1)
    rows[4].unavailable_reason = "no acceptable release found"
    user = await make_user(db_session, "retry@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    assert rows[4].state is EpisodeState.WANTED
    assert rows[4].unavailable_reason is None


async def test_unaired_episodes_are_never_wanted(db_session: AsyncSession) -> None:
    """FR-A1's hard edge: nothing outside the window, and nothing unaired."""
    anime = await make_anime(db_session, anilist_id=960020)
    rows = await make_episodes(db_session, anime, 12, aired_through=6)
    user = await make_user(db_session, "unaired@arc.test")
    await make_entry(db_session, user, anime, progress=5)

    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[5].id}


# --- The pause switch -------------------------------------------------------


async def test_a_paused_reconciliation_does_nothing_at_all(db_session: AsyncSession) -> None:
    """Not "reconcile but skip the searches": nothing is read, nothing written."""
    anime = await make_anime(db_session, anilist_id=960021)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "paused@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await set_setting(db_session, PAUSED_KEY, True)

    result = await compute_wants(db_session)

    assert result.as_dict() == WantsResult().as_dict(), "a paused run reports nothing done"
    assert await wants_of(db_session, user.id) == set()
    assert rows[4].state is EpisodeState.NOT_WANTED
    assert await search_jobs(db_session) == []


async def test_a_pause_leaves_the_wants_it_found_exactly_as_they_were(
    db_session: AsyncSession,
) -> None:
    """The point of pausing: resuming finds the world the pause left behind.

    A show dropped while paused would ordinarily release its episode back to
    ``not_wanted`` and delete the want. Paused, both survive — and are only
    undone once acquisition is running again.
    """
    anime = await make_anime(db_session, anilist_id=960022)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "kept@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}

    entry.status = ListStatus.DROPPED
    await db_session.flush()
    await set_setting(db_session, PAUSED_KEY, True)
    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}
    assert rows[4].state is EpisodeState.WANTED

    # …and resuming applies what the pause held back.
    await set_setting(db_session, PAUSED_KEY, False)
    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == set()
    assert rows[4].state is EpisodeState.NOT_WANTED


async def test_a_non_boolean_pause_setting_is_ignored(db_session: AsyncSession) -> None:
    """One hand-edited row must not decide whether acquisition runs."""
    anime = await make_anime(db_session, anilist_id=960023)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "badflag@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await set_setting(db_session, PAUSED_KEY, "yes please")

    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}


async def test_the_searches_a_recompute_queues_sort_behind_the_default(
    db_session: AsyncSession,
) -> None:
    """FR-A1's burst must not overtake the work a person is waiting on."""
    anime = await make_anime(db_session, anilist_id=960024)
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "priority@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await compute_wants(db_session)

    jobs = await search_jobs(db_session)
    assert jobs and {job.priority for job in jobs} == {SEARCH_RELEASE_PRIORITY}
    assert SEARCH_RELEASE_PRIORITY > DEFAULT_PRIORITY


# --- The hook from the list endpoints ---------------------------------------


async def test_a_list_change_enqueues_a_recompute(db_session: AsyncSession) -> None:
    from arc.services.acquisition.names import enqueue_compute_wants

    await enqueue_compute_wants(db_session)
    await enqueue_compute_wants(db_session)

    rows = await db_session.scalars(select(Job).where(Job.type == COMPUTE_WANTS))
    assert len(list(rows.all())) == 1, "the second call deduplicates onto the first"
