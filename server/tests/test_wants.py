"""The acquisition window: who wants what, and what that starts (FR-A1, A2, W4).

Two halves. :func:`window` is pure, so the first half is a table of
progress/N/aired combinations read as the rule itself. The second half runs
:func:`compute_wants` against the database, which is where merging across
users, dropping on a status change and starting the searches actually happen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    DEFAULT_PRIORITY,
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    ListStatus,
    Rendition,
    Torrent,
    User,
    Want,
)
from arc.services.acquisition import rules
from arc.services.acquisition.dormancy import REASON_DORMANT
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    QBIT_CANCEL,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
    cancel_dedupe_key,
)
from arc.services.acquisition.qbit import QBIT_CANCELLED, QBIT_REJECTED, QBIT_STALLED
from arc.services.acquisition.rules import (
    BYTES_PER_GB,
    MIN_FREE_KEY,
    PAUSED_KEY,
    SLOT_CAP_KEY,
)
from arc.services.acquisition.slots import SlotShow, assign_slots
from arc.services.acquisition.states import transition
from arc.services.acquisition.wants import (
    REASON_NOT_WANTING,
    STALE_DROP_REASON,
    UNAVAILABLE_RETRY,
    WantsResult,
    compute_wants,
    slot_view,
    window,
)
from arc.services.catalog.airing import FINISHED, RELEASING
from tests.acquisition_helpers import (
    acquisition_settings,
    fake_free_space,
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


def test_an_episode_dated_after_a_later_one_is_wanted_with_it() -> None:
    """A non-monotonic date is a typo, and the window reads it as one.

    Broadcasts do not overtake each other: an episode 5 dated a week after
    episode 6 already aired is the source contradicting itself, so
    :mod:`arc.services.catalog.airing` takes the sibling's date and the episode
    is wanted like any other aired one. Leaving it out was the old behaviour,
    and it left a hole in the middle of a user's downloads until the source
    fixed itself.
    """
    rows = episodes_with(count=6, aired=6)
    rows[4].air_at = NOW + timedelta(days=7)  # episode 5, dated after episode 6

    picked = window(
        rows, progress=3, look_ahead=3, now=NOW, anime_status="RELEASING", next_airing=None
    )

    assert [episode.number for episode in picked] == [4, 5, 6]


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


# --- Cancelling a download nobody wants (2026-09-13) ------------------------


async def downloading_episode(
    session: AsyncSession,
    *,
    anilist_id: int,
    email: str,
    info_hash: str,
) -> tuple[ListEntry, Episode, Torrent]:
    """A wanted episode taken as far as ``downloading``, with its torrent row."""
    anime = await make_anime(session, anilist_id=anilist_id)
    rows = await make_episodes(session, anime, 12, aired_through=12)
    user = await make_user(session, email)
    entry = await make_entry(session, user, anime, progress=4)
    await compute_wants(session)
    transition(rows[4], EpisodeState.SEARCHING)
    transition(rows[4], EpisodeState.DOWNLOADING)
    torrent = Torrent(episode_id=rows[4].id, info_hash=info_hash, qbit_state="downloading")
    session.add(torrent)
    await session.flush()
    return entry, rows[4], torrent


async def anime_of(session: AsyncSession, episode: Episode) -> Anime:
    found = await session.get(Anime, episode.anime_id)
    assert found is not None
    return found


async def cancel_jobs(session: AsyncSession) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == QBIT_CANCEL).order_by(Job.id))
    return list(rows.all())


async def test_a_download_nobody_wants_any_more_is_cancelled(
    db_session: AsyncSession,
) -> None:
    """Reversed 2026-09-13: it used to run to completion for nobody."""
    entry, episode, torrent = await downloading_episode(
        db_session, anilist_id=960017, email="keep@arc.test", info_hash="1" * 40
    )

    await db_session.delete(entry)
    await db_session.flush()
    result = await compute_wants(db_session)

    assert episode.state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED
    assert result.cancelled == 1
    jobs = await cancel_jobs(db_session)
    assert [job.payload["episode_id"] for job in jobs] == [episode.id]
    assert jobs[0].payload["dedupe_key"] == cancel_dedupe_key(episode.id)


async def test_another_users_want_keeps_the_download_going(
    db_session: AsyncSession,
) -> None:
    """FR-A2 merges wants, so one user backing out is not the last word."""
    entry, episode, torrent = await downloading_episode(
        db_session, anilist_id=960023, email="cancel-mine@arc.test", info_hash="2" * 40
    )
    other = await make_user(db_session, "cancel-theirs@arc.test")
    await make_entry(db_session, other, await anime_of(db_session, episode), progress=4)
    await compute_wants(db_session)

    await db_session.delete(entry)
    await db_session.flush()
    result = await compute_wants(db_session)

    assert episode.state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state == "downloading"
    assert result.cancelled == 0
    assert await cancel_jobs(db_session) == []


#: The legal route from ``downloading`` to each state with bytes on the disk.
LANDED: list[tuple[EpisodeState, ...]] = [
    (EpisodeState.DOWNLOADED,),
    (EpisodeState.DOWNLOADED, EpisodeState.MATCHING),
    (EpisodeState.DOWNLOADED, EpisodeState.MATCHING, EpisodeState.MATCHED),
    (
        EpisodeState.DOWNLOADED,
        EpisodeState.MATCHING,
        EpisodeState.MATCHED,
        EpisodeState.PREPARING,
    ),
    (
        EpisodeState.DOWNLOADED,
        EpisodeState.MATCHING,
        EpisodeState.MATCHED,
        EpisodeState.PREPARING,
        EpisodeState.READY,
    ),
]


@pytest.mark.parametrize("route", LANDED, ids=lambda route: route[-1].value)
async def test_bytes_that_have_landed_are_retentions_not_the_reconcilers(
    db_session: AsyncSession, route: tuple[EpisodeState, ...]
) -> None:
    """FR-T1 owns a file on disk; a want going away is not a delete."""
    state = route[-1]
    index = LANDED.index(route)
    entry, episode, torrent = await downloading_episode(
        db_session,
        anilist_id=960100 + index,
        email=f"landed-{state.value}@arc.test",
        info_hash=f"{index + 5:x}" * 40,
    )
    for step in route:
        transition(episode, step)
    await db_session.flush()

    await db_session.delete(entry)
    await db_session.flush()
    result = await compute_wants(db_session)

    assert episode.state is state
    assert torrent.qbit_state == "downloading", "and the client was told nothing"
    assert result.cancelled == 0


async def test_the_reconciliation_commits_its_other_work_alongside_a_cancel(
    db_session: AsyncSession,
) -> None:
    """Nothing here talks to qBittorrent, so a client that is down cannot fail it.

    The delete is a ``qbit_cancel`` job with the runner's backoff behind it —
    which is the whole reason the reconciler does not make the call itself.
    """
    entry, episode, torrent = await downloading_episode(
        db_session, anilist_id=960024, email="cancel-other@arc.test", info_hash="3" * 40
    )
    elsewhere = await make_anime(db_session, anilist_id=960025)
    other_rows = await make_episodes(db_session, elsewhere, 12, aired_through=12)
    second = await make_user(db_session, "cancel-elsewhere@arc.test")
    await make_entry(db_session, second, elsewhere, progress=0)

    await db_session.delete(entry)
    await db_session.flush()
    result = await compute_wants(db_session)

    assert episode.state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED
    assert result.cancelled == 1
    assert other_rows[0].state is EpisodeState.WANTED, "the rest of the sweep ran"
    assert await wants_of(db_session, second.id) == {other_rows[0].id, other_rows[1].id}


async def test_a_rejected_attempt_beside_the_live_one_is_not_cancelled(
    db_session: AsyncSession,
) -> None:
    """Should-fix 4: ``qbit_cancel`` deletes with files, and that file is in review.

    An episode can reach ``downloading`` with an older row beside the live one
    — a release whose delivered file a person ignored in review, which the
    client is still holding for them. Marking it ``cancelled`` would hand it to
    a job that deletes it off the disk.
    """
    entry, episode, live = await downloading_episode(
        db_session, anilist_id=960027, email="cancel-rejected@arc.test", info_hash="7" * 40
    )
    older = Torrent(episode_id=episode.id, info_hash="8" * 40, qbit_state=QBIT_REJECTED)
    stalled = Torrent(episode_id=episode.id, info_hash="9" * 40, qbit_state=QBIT_STALLED)
    db_session.add_all([older, stalled])
    await db_session.flush()

    await db_session.delete(entry)
    await db_session.flush()
    result = await compute_wants(db_session)

    assert result.cancelled == 1
    assert live.qbit_state == QBIT_CANCELLED
    assert older.qbit_state == QBIT_REJECTED, "the reviewed file is not this cancel's to delete"
    assert stalled.qbit_state == QBIT_STALLED


async def test_a_second_reconciliation_does_not_queue_a_second_cancel(
    db_session: AsyncSession,
) -> None:
    entry, episode, _ = await downloading_episode(
        db_session, anilist_id=960026, email="cancel-twice@arc.test", info_hash="4" * 40
    )

    await db_session.delete(entry)
    await db_session.flush()
    await compute_wants(db_session)
    second = await compute_wants(db_session)

    assert second.cancelled == 0, "the episode is already not_wanted"
    assert len(await cancel_jobs(db_session)) == 1


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


# --- Dormant imports (FR-A9, 2026-09-13) ------------------------------------


async def test_a_dormant_imported_entry_wants_nothing(db_session: AsyncSession) -> None:
    """FR-A9: an import is a baseline, not a request.

    The production failure this rule comes from: 414 wants at once, most of
    them shows planned years ago whose releases have no seeders left.
    """
    anime = await make_anime(db_session, anilist_id=960200, status=FINISHED)
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "dormant@arc.test")
    await make_entry(db_session, user, anime, progress=0, activated=False)

    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == set()
    assert result.added == 0
    assert await search_jobs(db_session) == []


async def test_an_airing_dormant_show_still_fetches(db_session: AsyncSession) -> None:
    """FR-A9's one exception, and the reason weekly use keeps working.

    A user whose whole list arrived by import should not have to press
    something to get tonight's episode of a show that is broadcasting.
    """
    anime = await make_anime(db_session, anilist_id=960201, status=RELEASING)
    rows = await make_episodes(db_session, anime, 12, aired_through=9)
    user = await make_user(db_session, "airing-dormant@arc.test")
    await make_entry(db_session, user, anime, progress=4, activated=False)

    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}


@pytest.mark.parametrize("status", [ListStatus.WATCHING, ListStatus.PLANNED])
async def test_touching_the_show_in_arc_starts_the_fetching(
    db_session: AsyncSession, status: ListStatus
) -> None:
    """Both wanting statuses, because both arrive from an import."""
    anime = await make_anime(db_session, anilist_id=960202, status=FINISHED)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, f"touch-{status.value}@arc.test")
    entry = await make_entry(db_session, user, anime, status=status, activated=False)
    await compute_wants(db_session)
    assert await wants_of(db_session, user.id) == set()

    entry.activated_at = NOW
    await db_session.flush()
    await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[0].id, rows[1].id}
    assert rows[0].state is EpisodeState.WANTED


async def test_an_entry_going_dormant_shelves_its_wants_with_its_own_reason(
    db_session: AsyncSession,
) -> None:
    """Shelved, not deleted — retention still needs the moment (FR-T1).

    The reason is :data:`REASON_DORMANT` rather than
    :data:`REASON_NOT_WANTING` because they are different facts about the row:
    the show has not been dropped, it has never been picked up. This is also
    what the first reconciliation after the deploy does to production's 414.
    """
    anime = await make_anime(db_session, anilist_id=960203, status=FINISHED)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "shelve-dormant@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}

    entry.activated_at = None
    await db_session.flush()
    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == set()
    assert result.shelved == 2
    assert result.removed == 0
    for row in rows[4:6]:
        want = await db_session.get(Want, (user.id, row.id))
        assert want is not None, "the row survives, as retention's anchor"
        assert want.drop_reason == REASON_DORMANT
    assert rows[4].state is EpisodeState.NOT_WANTED


async def test_a_dormant_drop_revives_unconditionally_when_the_user_touches_it(
    db_session: AsyncSession,
) -> None:
    """Unlike FR-T2's stale drop, which needs an Arc action *later* than it.

    Pressing "Fetch this show" is the whole condition, and the stamp it writes
    is what this reads.
    """
    anime = await make_anime(db_session, anilist_id=960204, status=FINISHED)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "revive-dormant@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    entry.activated_at = None
    await db_session.flush()
    await compute_wants(db_session)
    assert await wants_of(db_session, user.id) == set()

    entry.activated_at = NOW
    await db_session.flush()
    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}
    assert result.revived == 2
    assert result.added == 0, "the shelved rows came back rather than new ones"


async def test_a_download_for_a_dormant_entry_is_cancelled(
    db_session: AsyncSession,
) -> None:
    """The 2018 torrents that held every download slot, undone.

    A dormant entry's (user, show) is not in ``wanting``, so the episode goes
    the same way as any other want that has gone away: back to ``not_wanted``,
    the torrent marked cancelled and a ``qbit_cancel`` queued to remove it with
    its partial files.
    """
    anime = await make_anime(db_session, anilist_id=960205, status=FINISHED)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "dormant-dl@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    transition(rows[4], EpisodeState.SEARCHING)
    transition(rows[4], EpisodeState.DOWNLOADING)
    torrent = Torrent(episode_id=rows[4].id, info_hash="d" * 40, qbit_state="downloading")
    db_session.add(torrent)
    await db_session.flush()

    entry.activated_at = None
    await db_session.flush()
    result = await compute_wants(db_session)

    assert rows[4].state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED
    assert result.cancelled == 1
    jobs = await cancel_jobs(db_session)
    assert [job.payload["episode_id"] for job in jobs] == [rows[4].id]


async def test_an_airing_dormant_show_keeps_its_download(
    db_session: AsyncSession,
) -> None:
    """The other half of the same rule: airing is never dormant."""
    anime = await make_anime(db_session, anilist_id=960206, status=RELEASING)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "airing-dl@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    transition(rows[4], EpisodeState.SEARCHING)
    transition(rows[4], EpisodeState.DOWNLOADING)
    torrent = Torrent(episode_id=rows[4].id, info_hash="e" * 40, qbit_state="downloading")
    db_session.add(torrent)
    await db_session.flush()

    entry.activated_at = None
    await db_session.flush()
    result = await compute_wants(db_session)

    assert rows[4].state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state == "downloading"
    assert result.cancelled == 0
    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}


# --- The storage guard (FR-T6, 2026-09-13) ----------------------------------

#: A disk with a gigabyte left, against the default 10 GB floor.
NEARLY_FULL = 1 * BYTES_PER_GB


def held(monkeypatch: pytest.MonkeyPatch, free: int | None = NEARLY_FULL) -> None:
    """Make the guard see ``free`` bytes left on the data volume."""
    fake_free_space(monkeypatch, rules, free)


async def test_a_held_reconciliation_starts_no_search(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FR-T6: the wants are computed, the searching is not started."""
    anime = await make_anime(db_session, anilist_id=960210)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "held@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    held(monkeypatch)

    result = await compute_wants(db_session, settings=acquisition_settings(tmp_path))

    assert await wants_of(db_session, user.id) == {rows[4].id, rows[5].id}, "still wanted"
    assert result.added == 2
    assert result.started == 0
    assert await search_jobs(db_session) == []
    assert rows[4].state is EpisodeState.NOT_WANTED


async def test_a_hold_still_cancels_a_download_nobody_wants(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Because cancelling is what *frees* space — the pause's opposite.

    A hold that refused to do this would be holding the disk full.
    """
    entry, episode, torrent = await downloading_episode(
        db_session, anilist_id=960211, email="held-cancel@arc.test", info_hash="f" * 40
    )
    held(monkeypatch)

    await db_session.delete(entry)
    await db_session.flush()
    result = await compute_wants(db_session, settings=acquisition_settings(tmp_path))

    assert episode.state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED
    assert result.cancelled == 1


async def test_a_hold_still_releases_an_episode_nobody_wants(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    anime = await make_anime(db_session, anilist_id=960212)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "held-release@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    assert rows[4].state is EpisodeState.WANTED
    held(monkeypatch)

    await db_session.delete(entry)
    await db_session.flush()
    result = await compute_wants(db_session, settings=acquisition_settings(tmp_path))

    assert rows[4].state is EpisodeState.NOT_WANTED
    assert result.released == 2, "both of the window's episodes came back"


async def test_a_disk_above_the_floor_holds_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordinary case, asserted so the guard cannot be on by accident."""
    anime = await make_anime(db_session, anilist_id=960213)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "notheld@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    held(monkeypatch, 50 * BYTES_PER_GB)

    result = await compute_wants(db_session, settings=acquisition_settings(tmp_path))

    assert result.started == 2
    assert rows[4].state is EpisodeState.WANTED


async def test_a_floor_of_zero_turns_the_guard_off(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty disk and no reserve asked for: fetch anyway (FR-T6)."""
    anime = await make_anime(db_session, anilist_id=960215)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "nofloor@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await set_setting(db_session, MIN_FREE_KEY, 0)
    held(monkeypatch, 0)

    result = await compute_wants(db_session, settings=acquisition_settings(tmp_path))

    assert result.started == 2
    assert rows[4].state is EpisodeState.WANTED


async def test_a_measurement_that_fails_never_holds(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "I could not read the filesystem" is not a small number (FR-T6).

    Treating it as one would stop Arc fetching for ever on a renamed data
    directory, with nothing to lift it.
    """
    anime = await make_anime(db_session, anilist_id=960214)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "nodir@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    held(monkeypatch, None)

    result = await compute_wants(db_session, settings=acquisition_settings(tmp_path))

    assert result.started == 2
    assert rows[4].state is EpisodeState.WANTED


async def test_no_settings_means_no_measurement_and_no_hold(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every production caller passes one; a caller that cannot, is not held."""
    anime = await make_anime(db_session, anilist_id=960216)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "nosettings@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    held(monkeypatch, 0)

    result = await compute_wants(db_session)

    assert result.started == 2
    assert rows[4].state is EpisodeState.WANTED


# --- The per-user slot cap (FR-A10, 2026-09-13) ------------------------------
#
# Two halves again. :func:`assign_slots` is pure, so the admission rule is a
# table; the rest runs ``compute_wants`` with more shows than slots, which is
# where "hungry", "fetching" and "waiting" actually come from.


def slot_show(
    anime_id: int,
    *,
    airing: bool = False,
    hours_ago: int = 0,
    fetching: bool = False,
    hungry: bool = True,
) -> SlotShow:
    return SlotShow(
        anime_id=anime_id,
        airing=airing,
        updated_at=NOW - timedelta(hours=hours_ago),
        fetching=fetching,
        hungry=hungry,
    )


@pytest.mark.parametrize(
    ("shows", "k", "admitted", "waiting"),
    [
        # K = 0 is no cap at all — the opposite of what 0 means for N.
        ([slot_show(1), slot_show(2), slot_show(3)], 0, [1, 2, 3], []),
        # The plain case: the most recently touched entries get the slots.
        ([slot_show(1), slot_show(2, hours_ago=1), slot_show(3, hours_ago=2)], 2, [1, 2], [3]),
        # And the input order does not decide it: the same three, oldest first.
        ([slot_show(3, hours_ago=2), slot_show(2, hours_ago=1), slot_show(1)], 2, [1, 2], [3]),
        # Airing beats recency, however long ago the entry was touched: the
        # weekly episode is the one a person notices missing.
        ([slot_show(1), slot_show(2, airing=True, hours_ago=99)], 1, [2], [1]),
        # Occupants keep their slot even when there are more of them than K —
        # lowering the cap never cancels a download to make room.
        (
            [
                slot_show(1, fetching=True),
                slot_show(2, fetching=True),
                slot_show(3, fetching=True),
                slot_show(4, hours_ago=1),
            ],
            2,
            [1, 2, 3],
            [4],
        ),
        # But they do count against K, so one occupant leaves one free slot.
        (
            [slot_show(1, fetching=True), slot_show(2, hours_ago=1), slot_show(3, hours_ago=2)],
            2,
            [1, 2],
            [3],
        ),
        # Ties keep the caller's order, which is the reconciler's (user, anime).
        ([slot_show(7), slot_show(8), slot_show(9)], 2, [7, 8], [9]),
        # A show with nothing to fetch is in neither list: it is not being held
        # back, it is up to date. This is the all-``ready`` show whose slot the
        # next run gives away — and the sample-only show, whose one want never
        # makes it an occupant (``fetching`` is false for it by construction).
        ([slot_show(1, fetching=True), slot_show(2, hungry=False)], 5, [1], []),
        # The same show under a full cap: still not waiting.
        (
            [
                slot_show(1, fetching=True),
                slot_show(2, hungry=False),
                slot_show(3, hours_ago=1),
            ],
            1,
            [1],
            [3],
        ),
    ],
)
def test_assign_slots_admits_occupants_then_airing_then_the_newest(
    shows: list[SlotShow], k: int, admitted: list[int], waiting: list[int]
) -> None:
    got_admitted, got_waiting = assign_slots(shows, k)

    assert [show.anime_id for show in got_admitted] == admitted
    assert [show.anime_id for show in got_waiting] == waiting


async def slot_shows(
    session: AsyncSession,
    user: User,
    count: int,
    *,
    base: int,
    airing: frozenset[int] = frozenset(),
    activated: frozenset[int] | None = None,
    episodes: int = 4,
) -> list[tuple[Anime, list[Episode]]]:
    """``count`` planned shows for one user, **most recently touched first**.

    Index 0 is the newest entry, so the first K of them are the ones the cap
    admits when nothing is airing and nothing is fetching yet. Every episode
    has aired, which keeps the window out of the way of the rule under test.
    """
    moment = datetime.now(UTC)
    rows: list[tuple[Anime, list[Episode]]] = []
    for index in range(count):
        anime = await make_anime(
            session,
            anilist_id=base + index,
            romaji=f"Slot Show {index}",
            english=None,
            status=RELEASING if index in airing else FINISHED,
            episodes=episodes,
        )
        made = await make_episodes(session, anime, episodes, aired_through=episodes)
        entry = await make_entry(
            session,
            user,
            anime,
            status=ListStatus.PLANNED,
            activated=activated is None or index in activated,
        )
        entry.updated_at = moment - timedelta(hours=index)
        rows.append((anime, made))
    await session.flush()
    return rows


async def test_only_k_shows_fetch_at_once(db_session: AsyncSession) -> None:
    """FR-A10: seven shows, a cap of five, and two of them wait.

    The production shape this comes from is the other half of the 414-want
    afternoon: the imports that *were* activated all asked for their next two
    episodes at once, and one disk and three download slots cannot answer that.
    """
    user = await make_user(db_session, "cap@arc.test")
    shows = await slot_shows(db_session, user, 7, base=960300)

    result = await compute_wants(db_session)

    wanted = await wants_of(db_session, user.id)
    assert wanted == {episode.id for _, episodes in shows[:5] for episode in episodes[:2]}, (
        "the five most recently touched entries, two episodes each"
    )
    assert result.waiting == 2
    assert result.added == 10


async def test_a_cap_of_zero_is_no_cap(db_session: AsyncSession) -> None:
    """0 means unlimited, unlike N's 0, which means "fetch nothing"."""
    await set_setting(db_session, SLOT_CAP_KEY, 0)
    user = await make_user(db_session, "nocap@arc.test")
    shows = await slot_shows(db_session, user, 7, base=960310)

    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {
        episode.id for _, episodes in shows for episode in episodes[:2]
    }
    assert result.waiting == 0


async def test_a_show_whose_episodes_are_all_ready_frees_its_slot(
    db_session: AsyncSession,
) -> None:
    """A show waiting to be *watched* is not fetching, so it holds nothing.

    The heart of why a slot is defined against the wants and not against
    ``list_entries``: if a user who lets three episodes pile up kept their
    slots, the cap would stop the rest of their list for as long as they took
    to get round to them.
    """
    user = await make_user(db_session, "freed@arc.test")
    shows = await slot_shows(db_session, user, 7, base=960320)
    await compute_wants(db_session)
    assert (await wants_of(db_session, user.id)).isdisjoint(
        {episode.id for _, episodes in shows[5:] for episode in episodes}
    ), "the last two shows are waiting"

    # The third show's two episodes arrive and are prepared.
    for episode in shows[2][1][:2]:
        episode.state = EpisodeState.READY
        episode.state_changed_at = datetime.now(UTC)
    await db_session.flush()

    result = await compute_wants(db_session)

    wanted = await wants_of(db_session, user.id)
    assert {episode.id for episode in shows[5][1][:2]} <= wanted, "the sixth show starts"
    assert {episode.id for episode in shows[6][1][:2]}.isdisjoint(wanted), "the seventh still waits"
    assert {episode.id for episode in shows[2][1][:2]} <= wanted, (
        "and the ready episodes keep their wants — retention's grace runs from a "
        "completion, not from a cap"
    )
    assert result.waiting == 1


async def test_lowering_k_cancels_nothing(db_session: AsyncSession) -> None:
    """A cap limits *starting*. Five shows over a cap of three keep going."""
    user = await make_user(db_session, "lowered@arc.test")
    shows = await slot_shows(db_session, user, 7, base=960330)
    await compute_wants(db_session)
    before = await wants_of(db_session, user.id)

    await set_setting(db_session, SLOT_CAP_KEY, 3)
    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == before, "nothing gained, nothing lost"
    assert result.cancelled == 0
    assert result.released == 0
    assert result.removed == 0
    assert result.shelved == 0
    assert result.waiting == 2
    assert all(
        episode.state is EpisodeState.WANTED
        for _, episodes in shows[:5]
        for episode in episodes[:2]
    )


async def test_an_airing_show_takes_a_free_slot_before_a_newer_one(
    db_session: AsyncSession,
) -> None:
    """Airing first, then recency — the order FR-A10 spells out."""
    user = await make_user(db_session, "airing-slot@arc.test")
    shows = await slot_shows(db_session, user, 6, base=960340, airing=frozenset({5}))

    await compute_wants(db_session)

    wanted = await wants_of(db_session, user.id)
    assert {episode.id for episode in shows[5][1][:2]} <= wanted, "the oldest entry, but airing"
    assert {episode.id for episode in shows[4][1][:2]}.isdisjoint(wanted)


async def test_another_users_shows_do_not_take_your_slots(db_session: AsyncSession) -> None:
    """The cap is per user, so a full list next door costs you nothing."""
    first = await make_user(db_session, "mine@arc.test")
    shows = await slot_shows(db_session, first, 7, base=960350)
    second = await make_user(db_session, "theirs@arc.test")
    # The two shows the first user is waiting on are the second user's whole
    # list, so they are hers to fetch whatever the first user's cap says.
    for anime, _ in shows[5:]:
        entry = await make_entry(db_session, second, anime, status=ListStatus.PLANNED)
        entry.updated_at = datetime.now(UTC)
    await db_session.flush()

    result = await compute_wants(db_session)

    theirs = {episode.id for _, episodes in shows[5:] for episode in episodes[:2]}
    assert await wants_of(db_session, second.id) == theirs
    assert (await wants_of(db_session, first.id)).isdisjoint(theirs), (
        "and the download she started does not admit his waiting show either"
    )
    assert result.waiting == 2


async def test_a_dormant_entry_does_not_take_a_slot(db_session: AsyncSession) -> None:
    """FR-A9 comes first: an untouched import is not a show waiting for room."""
    user = await make_user(db_session, "dormant-slot@arc.test")
    shows = await slot_shows(db_session, user, 7, base=960360, activated=frozenset({5, 6}))

    result = await compute_wants(db_session)

    assert await wants_of(db_session, user.id) == {
        episode.id for _, episodes in shows[5:] for episode in episodes[:2]
    }
    assert result.waiting == 0


async def test_a_sample_on_a_waiting_show_still_starts_its_search(
    db_session: AsyncSession,
) -> None:
    """FR-A8 is one episode somebody asked for; the cap is not its business."""
    user = await make_user(db_session, "sample-slot@arc.test")
    shows = await slot_shows(db_session, user, 6, base=960370)
    await compute_wants(db_session)
    _, waiting_episodes = shows[5]
    assert (await wants_of(db_session, user.id)).isdisjoint(
        {episode.id for episode in waiting_episodes}
    )

    db_session.add(Want(user_id=user.id, episode_id=waiting_episodes[0].id, sample=True))
    await db_session.flush()
    result = await compute_wants(db_session)

    live = await wants_of(db_session, user.id)
    jobs = await search_jobs(db_session)
    assert waiting_episodes[0].id in live
    assert waiting_episodes[0].state is EpisodeState.WANTED
    assert any(job.payload["episode_id"] == waiting_episodes[0].id for job in jobs)
    assert waiting_episodes[1].id not in live, (
        "and the window behind it is still waiting: a sample is one episode"
    )
    assert result.waiting == 1


async def test_a_stale_drop_on_a_waiting_show_is_left_alone(db_session: AsyncSession) -> None:
    """FR-A10 must not touch FR-T2's record, in either direction.

    The row the review found: a want dropped for going unwatched sits on a
    ``ready`` episode, so its show is not an occupant; the rest of its window
    is hungry, so it competes for a slot and can lose. Contributing only the
    *live* rows of a waiting show would have left this key out of ``desired``
    while the show was still in ``wanting`` — and the reconciler deletes such a
    row. That would have thrown away the ``dropped_at`` retention measures
    FR-T1's grace from (files deleted days early) and handed the row back live
    the moment a slot freed, skipping the Arc-side touch FR-T2's revival needs.
    """
    user = await make_user(db_session, "stale-slot@arc.test")
    shows = await slot_shows(db_session, user, 7, base=960380)
    _, waiting_episodes = shows[6]
    # Episode 1 of the last show arrived a long time ago and was dropped for
    # going unwatched; episode 2 is what makes the show hungry.
    dropped_at = datetime.now(UTC) - timedelta(days=2)
    waiting_episodes[0].state = EpisodeState.READY
    db_session.add(
        Want(
            user_id=user.id,
            episode_id=waiting_episodes[0].id,
            dropped_at=dropped_at,
            drop_reason=STALE_DROP_REASON,
        )
    )
    await db_session.flush()

    result = await compute_wants(db_session)

    row = await db_session.get(Want, (user.id, waiting_episodes[0].id))
    assert row is not None, "the row is not deleted"
    assert row.dropped_at == dropped_at, "and not restamped"
    assert row.drop_reason == STALE_DROP_REASON, "and not relabelled"
    assert result.waiting == 2
    assert result.shelved == 0
    assert result.removed == 0


async def test_a_slot_does_not_revive_a_stale_drop(db_session: AsyncSession) -> None:
    """And when the show is admitted, the drop still waits for the user.

    A slot freeing up is not the user coming back to the show, which is the
    only thing FR-T2 accepts (``updated_by == arc``, later than the drop) — so
    the entry is quiet from before the drop, as an abandoned show is.
    """
    await set_setting(db_session, SLOT_CAP_KEY, 1)
    user = await make_user(db_session, "stale-admitted@arc.test")
    [(anime, episodes)] = await slot_shows(db_session, user, 1, base=960390)
    dropped_at = datetime.now(UTC) - timedelta(days=2)
    entry = await db_session.get(ListEntry, (user.id, anime.id))
    assert entry is not None
    entry.updated_at = dropped_at - timedelta(days=1)
    episodes[0].state = EpisodeState.READY
    db_session.add(
        Want(
            user_id=user.id,
            episode_id=episodes[0].id,
            dropped_at=dropped_at,
            drop_reason=STALE_DROP_REASON,
        )
    )
    await db_session.flush()

    result = await compute_wants(db_session)

    row = await db_session.get(Want, (user.id, episodes[0].id))
    assert row is not None
    assert row.dropped_at == dropped_at, "admitted, and still dropped"
    assert result.waiting == 0, "one show, a cap of one: nothing is waiting"
    assert result.revived == 0


async def test_unfindable_shows_do_not_hold_slots_for_ever(db_session: AsyncSession) -> None:
    """FR-A6 gave up on these; FR-A10 must not let them freeze the list.

    Five shows whose episodes are all ``unavailable`` — no seeders left, which
    is exactly the 2018-planned shape production arrived with — would otherwise
    occupy every slot a user has for as long as the shows stayed on their list.
    """
    user = await make_user(db_session, "unfindable@arc.test")
    shows = await slot_shows(db_session, user, 6, base=960400)
    await compute_wants(db_session)
    for _, episodes in shows[:5]:
        for episode in episodes[:2]:
            episode.state = EpisodeState.UNAVAILABLE
            episode.state_changed_at = datetime.now(UTC) - timedelta(days=3)
    await db_session.flush()

    result = await compute_wants(db_session)

    wanted = await wants_of(db_session, user.id)
    assert {episode.id for episode in shows[5][1][:2]} <= wanted, "the sixth show is admitted"
    assert result.waiting == 0
    # And the ones that gave up keep their wants, so FR-A6's daily retry runs.
    assert {episode.id for _, episodes in shows[:5] for episode in episodes[:2]} <= wanted


@pytest.mark.parametrize("state", [EpisodeState.UNAVAILABLE, EpisodeState.FAILED])
async def test_a_settled_episode_holds_no_slot(
    db_session: AsyncSession, state: EpisodeState
) -> None:
    """``ready``, ``unavailable`` and ``failed`` are all somebody else's problem."""
    await set_setting(db_session, SLOT_CAP_KEY, 1)
    user = await make_user(db_session, f"settled-{state.value}@arc.test")
    shows = await slot_shows(db_session, user, 2, base=960410)
    await compute_wants(db_session)
    for episode in shows[0][1][:2]:
        episode.state = state
        episode.state_changed_at = datetime.now(UTC) - timedelta(days=3)
    await db_session.flush()

    await compute_wants(db_session)

    assert {episode.id for episode in shows[1][1][:2]} <= await wants_of(db_session, user.id)


# --- slot_view(), what the show page reads -----------------------------------


async def test_slot_view_answers_the_cap_for_one_user(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "view@arc.test")
    shows = await slot_shows(db_session, user, 7, base=960420)
    await compute_wants(db_session)

    view = await slot_view(db_session, user.id)

    assert view.waiting == {shows[5][0].id, shows[6][0].id}
    assert view.fetching == 5
    assert view.cap == 5
    assert view.paused is False
    assert view.held is False


async def test_slot_view_says_when_acquisition_is_paused(db_session: AsyncSession) -> None:
    """The page must not promise "when one of them finishes" while nothing runs."""
    user = await make_user(db_session, "view-paused@arc.test")
    await slot_shows(db_session, user, 7, base=960430)
    await compute_wants(db_session)
    await set_setting(db_session, PAUSED_KEY, True)

    view = await slot_view(db_session, user.id)

    assert view.paused is True
    assert view.held is False
    assert len(view.waiting) == 2, "the cap's answer is still the cap's answer"


async def test_slot_view_says_when_the_disk_is_holding_acquisition(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user = await make_user(db_session, "view-held@arc.test")
    await slot_shows(db_session, user, 7, base=960440)
    await compute_wants(db_session)
    held(monkeypatch, 1 * BYTES_PER_GB)

    view = await slot_view(db_session, user.id, settings=acquisition_settings(tmp_path))

    assert view.held is True
    assert view.paused is False


async def test_slot_view_without_settings_reports_no_hold(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No measurement, no hold — the same answer a failed measurement gives."""
    user = await make_user(db_session, "view-nosettings@arc.test")
    await slot_shows(db_session, user, 2, base=960450)
    held(monkeypatch, 1 * BYTES_PER_GB)

    view = await slot_view(db_session, user.id)

    assert view.held is False


async def test_a_waiting_show_still_drops_a_want_the_user_watched(
    db_session: AsyncSession,
) -> None:
    """FR-A10's one exception: the ending that takes nothing away.

    A live want on an episode the user has watched past is the ordinary "left
    the window" delete, and it is safe for the ordinary reason — the completion
    is in ``watch_progress``, which is what retention measures FR-T1's grace
    from. Leaving it would pin the files to the disk until the show next won a
    slot, because a live want makes the sweep skip an episode: a cap keeping
    files alive is further from its job than anything else it could do.
    Everything else the held show holds is still untouched.
    """
    user = await make_user(db_session, "watched-waiting@arc.test")
    shows = await slot_shows(db_session, user, 7, base=960460, episodes=8)
    anime, episodes = shows[6]
    entry = await db_session.get(ListEntry, (user.id, anime.id))
    assert entry is not None
    # Watched four of eight, so episodes 5 and 6 are the window it is hungry
    # for and loses the slot race over. Its rows: episode 3 watched past,
    # episode 4 carrying a stale drop, episode 1 a sample. Both of the last two
    # are ``ready``, which is what keeps the show from being an occupant — a
    # live want on anything in flight would have earned it a slot outright.
    entry.progress = 4
    # Written by hand with the progress: ``updated_at`` carries ``onupdate``,
    # so touching the row would otherwise stamp it *now* and make the oldest
    # entry on the list the newest — which would win it a slot and test
    # nothing.
    entry.updated_at = datetime.now(UTC) - timedelta(hours=99)
    dropped_at = datetime.now(UTC) - timedelta(days=2)
    episodes[2].state = EpisodeState.READY
    episodes[3].state = EpisodeState.READY
    db_session.add_all(
        [
            Want(user_id=user.id, episode_id=episodes[2].id),
            Want(
                user_id=user.id,
                episode_id=episodes[3].id,
                dropped_at=dropped_at,
                drop_reason=STALE_DROP_REASON,
            ),
            Want(user_id=user.id, episode_id=episodes[0].id, sample=True),
        ]
    )
    await db_session.flush()

    result = await compute_wants(db_session)

    assert await db_session.get(Want, (user.id, episodes[2].id)) is None, "watched past, deleted"
    assert result.removed == 1
    stale = await db_session.get(Want, (user.id, episodes[3].id))
    assert stale is not None and stale.dropped_at == dropped_at, "the stale drop is untouched"
    sample = await db_session.get(Want, (user.id, episodes[0].id))
    assert sample is not None and sample.dropped_at is None, "and so is the sample"
    assert result.shelved == 0
    assert result.waiting == 2
