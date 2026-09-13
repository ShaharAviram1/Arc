"""The "try episode 1" sample: the one want nobody's list justifies (FR-A8).

Two halves, like :mod:`tests.test_wants`. The first exercises the two writers
in :mod:`arc.services.acquisition.samples` — what they refuse and what they
write — and the second runs :func:`compute_wants` over the rows they leave
behind, because a sample only means anything if the reconciler agrees to keep
it fifteen minutes later.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListStatus,
    OfflineId,
    Torrent,
    Want,
    WatchProgress,
)
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    QBIT_CANCEL,
    SEARCH_RELEASE,
    search_dedupe_key,
)
from arc.services.acquisition.qbit import QBIT_CANCELLED
from arc.services.acquisition.samples import (
    SAMPLE_CANCELLED_REASON,
    AlreadyFollowing,
    NoEpisodes,
    NotAired,
    cancel_sample,
    request_sample,
    sample_for,
)
from arc.services.acquisition.wants import (
    REASON_NOT_WANTING,
    STALE_DROP_REASON,
    compute_wants,
)
from arc.services.catalog.airing import FINISHED, RELEASING
from arc.services.jobs.queue import DEDUPE_FIELD
from arc.services.tmdb.names import TMDB_ENRICH
from arc.services.tmdb.names import dedupe_key as tmdb_dedupe_key
from tests.acquisition_helpers import make_anime, make_entry, make_episodes, make_user
from tests.tmdb_mock import API_KEY

pytestmark = pytest.mark.pg

#: A deployment with a TMDB key, and one without. ``request_sample`` reads
#: nothing off these but ``tmdb_api_key`` — the enrichment it queues is gated
#: on it (§5.8) — and nothing here builds an engine from one, so the database
#: url is pinned to a name no engine could reach by accident rather than left
#: at the field default, which is a developer's dev database.
TMDB_ON = Settings(  # type: ignore[call-arg]
    env="test",
    database_url="postgresql+asyncpg://unused:unused@unused/unused",
    tmdb_api_key=API_KEY,
    _env_file=None,
)
TMDB_OFF = TMDB_ON.model_copy(update={"tmdb_api_key": None})


def now() -> datetime:
    return datetime.now(UTC)


async def live_wants(session: AsyncSession, user_id: int) -> set[int]:
    rows = await session.scalars(
        select(Want.episode_id).where(Want.user_id == user_id, Want.dropped_at.is_(None))
    )
    return set(rows.all())


async def episode_states(session: AsyncSession, anime_id: int) -> dict[int, EpisodeState]:
    """``episode number → state``, read back rather than off the ORM objects."""
    rows = await session.execute(
        select(Episode.number, Episode.state).where(Episode.anime_id == anime_id)
    )
    return {number: state for number, state in rows.all()}


async def recompute_jobs(session: AsyncSession) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == COMPUTE_WANTS))
    return list(rows.all())


async def search_jobs(session: AsyncSession) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == SEARCH_RELEASE).order_by(Job.id))
    return list(rows.all())


async def enrich_jobs(session: AsyncSession) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == TMDB_ENRICH).order_by(Job.id))
    return list(rows.all())


async def map_to_tmdb(session: AsyncSession, anime: Anime, *, tmdb_tv_id: int = 37854) -> None:
    """Make the offline cross-id map able to reach this show (§5.8).

    Without a row here TMDB is unreachable for it — AniList publishes no TMDB
    id — and every on-demand enqueue is expected to queue nothing at all.
    """
    session.add(
        OfflineId(anilist_id=anime.anilist_id, tmdb_tv_id=tmdb_tv_id, tmdb_season=1, type="TV")
    )
    await session.flush()


# --- request_sample ---------------------------------------------------------


async def test_a_sample_wants_the_first_episode_and_nothing_else(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=964001)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sample-one@arc.test")

    requested = await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert requested.want.episode_id == rows[0].id
    assert requested.episode.number == 1, "the row the response names"
    assert requested.want.sample is True
    assert requested.want.dropped_at is None
    assert await live_wants(db_session, user.id) == {rows[0].id}


async def test_a_sample_changes_no_list_entry(db_session: AsyncSession) -> None:
    """FR-A8's whole point: deciding without committing, so no MAL write."""
    from arc.models import ListEntry

    anime = await make_anime(db_session, anilist_id=964002)
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sample-nolist@arc.test")

    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert await db_session.get(ListEntry, (user.id, anime.id)) is None


async def test_a_sample_queues_a_reconciliation(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=964003)
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sample-job@arc.test")

    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert len(await recompute_jobs(db_session)) == 1


async def test_a_show_with_no_episodes_cannot_be_sampled(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=964004)
    user = await make_user(db_session, "sample-empty@arc.test")

    with pytest.raises(NoEpisodes):
        await request_sample(
            db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
        )


async def test_an_unaired_first_episode_cannot_be_sampled(db_session: AsyncSession) -> None:
    """A sample is not a licence to fetch ahead of the broadcast (FR-A1)."""
    anime = await make_anime(db_session, anilist_id=964005)
    await make_episodes(db_session, anime, 3, aired_through=0)
    user = await make_user(db_session, "sample-unaired@arc.test")

    with pytest.raises(NotAired):
        await request_sample(
            db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
        )

    assert await live_wants(db_session, user.id) == set()


@pytest.mark.parametrize("status", [ListStatus.WATCHING, ListStatus.PLANNED])
async def test_a_show_already_being_followed_cannot_be_sampled(
    db_session: AsyncSession, status: ListStatus
) -> None:
    """The window covers episode 1 already; a second row would only be litter."""
    anime = await make_anime(db_session, anilist_id=964006 + int(status is ListStatus.PLANNED))
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, f"sample-following-{status.value}@arc.test")
    await make_entry(db_session, user, anime, status=status)

    with pytest.raises(AlreadyFollowing):
        await request_sample(
            db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
        )


@pytest.mark.parametrize(
    ("status", "anilist_id"),
    [
        (ListStatus.ON_HOLD, 964010),
        (ListStatus.DROPPED, 964011),
        (ListStatus.COMPLETED, 964012),
    ],
)
async def test_a_show_on_the_list_but_not_followed_can_be_sampled(
    db_session: AsyncSession, status: ListStatus, anilist_id: int
) -> None:
    """FR-W4's statuses generate no window, so there is nothing to collide with."""
    anime = await make_anime(db_session, anilist_id=anilist_id)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, f"sample-unfollowed-{status.value}@arc.test")
    await make_entry(db_session, user, anime, status=status)

    want = (
        await request_sample(
            db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
        )
    ).want

    assert (want.episode_id, want.sample) == (rows[0].id, True)


async def test_sample_for_reads_the_live_want_and_nothing_else(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=964020)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sample-for@arc.test")
    assert await sample_for(db_session, user_id=user.id, anime_id=anime.id) is None

    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    found = await sample_for(db_session, user_id=user.id, anime_id=anime.id)
    assert found is not None and found.episode_id == rows[0].id

    await cancel_sample(db_session, user_id=user.id, anime_id=anime.id, now=now())
    assert await sample_for(db_session, user_id=user.id, anime_id=anime.id) is None


async def test_a_list_want_pressed_as_a_sample_becomes_one(db_session: AsyncSession) -> None:
    """The row is keyed (user, episode); there is only ever one of it (FR-A2).

    A user who was watching the show, dropped it — leaving the reconciler's
    tombstone on episode 1 — and then pressed "try episode 1" gets that row
    back, as a sample this time.
    """
    anime = await make_anime(db_session, anilist_id=964021)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sample-reuse@arc.test")
    entry = await make_entry(db_session, user, anime, progress=0)
    await compute_wants(db_session)
    entry.status = ListStatus.DROPPED
    await db_session.flush()
    await compute_wants(db_session)
    shelved = await db_session.get(Want, (user.id, rows[0].id))
    assert shelved is not None and shelved.drop_reason == REASON_NOT_WANTING

    want = (
        await request_sample(
            db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
        )
    ).want

    assert want is shelved, "one (user, episode) row, reused"
    assert (want.sample, want.dropped_at, want.drop_reason) == (True, None, None)


# --- cancel_sample ----------------------------------------------------------


async def test_cancelling_drops_the_want_with_its_own_reason(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=964030)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "cancel@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert await cancel_sample(db_session, user_id=user.id, anime_id=anime.id, now=now()) is True

    want = await db_session.get(Want, (user.id, rows[0].id))
    assert want is not None, "kept, as retention's grace anchor (FR-T1)"
    assert want.dropped_at is not None
    assert want.drop_reason == SAMPLE_CANCELLED_REASON


async def test_cancelling_nothing_says_so(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=964031)
    await make_episodes(db_session, anime, 4, aired_through=4)
    user = await make_user(db_session, "cancel-nothing@arc.test")

    assert await cancel_sample(db_session, user_id=user.id, anime_id=anime.id, now=now()) is False


async def test_cancelling_releases_the_episode_nobody_else_wants(
    db_session: AsyncSession,
) -> None:
    """The reconciler does the releasing; cancel only has to queue it."""
    anime = await make_anime(db_session, anilist_id=964032)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "cancel-release@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    await compute_wants(db_session)
    await db_session.refresh(rows[0])
    assert rows[0].state is EpisodeState.WANTED

    await cancel_sample(db_session, user_id=user.id, anime_id=anime.id, now=now())

    await db_session.refresh(rows[0])
    assert rows[0].state is EpisodeState.NOT_WANTED, "released by the cancel itself"
    # And the tick behind it finds nothing left to do, rather than a second
    # opinion about the same episode.
    result = await compute_wants(db_session)
    await db_session.refresh(rows[0])
    assert (rows[0].state, result.released) == (EpisodeState.NOT_WANTED, 0)


async def test_cancelling_leaves_an_episode_somebody_else_still_wants(
    db_session: AsyncSession,
) -> None:
    """FR-A2 merges wants; one user backing out is not the last word."""
    anime = await make_anime(db_session, anilist_id=964033)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    sampler = await make_user(db_session, "cancel-merged@arc.test")
    watcher = await make_user(db_session, "still-wants@arc.test")
    await make_entry(db_session, watcher, anime, progress=0)
    await request_sample(
        db_session, settings=TMDB_ON, user_id=sampler.id, anime_id=anime.id, now=now()
    )
    await compute_wants(db_session)

    await cancel_sample(db_session, user_id=sampler.id, anime_id=anime.id, now=now())
    result = await compute_wants(db_session)

    await db_session.refresh(rows[0])
    assert rows[0].state is EpisodeState.WANTED
    assert result.released == 0
    assert await live_wants(db_session, watcher.id) == {rows[0].id, rows[1].id}


# --- compute_wants over samples ---------------------------------------------


async def test_the_reconciler_keeps_a_sample_on_a_show_nobody_follows(
    db_session: AsyncSession,
) -> None:
    """The rule FR-A8 turns on: no list entry, and the row survives anyway."""
    anime = await make_anime(db_session, anilist_id=964040)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "keep-sample@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    first = await compute_wants(db_session)
    second = await compute_wants(db_session)

    assert await live_wants(db_session, user.id) == {rows[0].id}
    assert (first.shelved, first.removed) == (0, 0)
    assert (second.shelved, second.removed, second.revived) == (0, 0, 0)
    want = await db_session.get(Want, (user.id, rows[0].id))
    assert want is not None and want.dropped_at is None


async def test_a_sample_starts_one_search_and_nothing_more(db_session: AsyncSession) -> None:
    """Never more than that one episode: the rest of the show stays resting."""
    anime = await make_anime(db_session, anilist_id=964041)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "one-search@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    result = await compute_wants(db_session)

    # The request already moved episode 1 (below), so the tick has one want to
    # account for and nothing to start.
    assert (result.wanted, result.started) == (1, 0)
    states = await episode_states(db_session, anime.id)
    assert states[1] is EpisodeState.WANTED
    assert set(states[number] for number in range(2, 13)) == {EpisodeState.NOT_WANTED}
    assert len(rows) == 12
    assert len(await search_jobs(db_session)) == 1, "one episode asked for, one search"


async def test_a_cancelled_sample_is_not_revived_by_the_next_tick(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=964042)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "cancel-sticks@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    await compute_wants(db_session)
    await cancel_sample(db_session, user_id=user.id, anime_id=anime.id, now=now())
    want = await db_session.get(Want, (user.id, rows[0].id))
    assert want is not None and want.dropped_at is not None
    dropped_at = want.dropped_at

    result = await compute_wants(db_session)

    assert want.dropped_at == dropped_at, "not restamped either"
    assert want.drop_reason == SAMPLE_CANCELLED_REASON, "and not re-labelled"
    assert (result.revived, result.shelved) == (0, 0)


async def test_a_sample_on_a_watched_show_is_governed_by_the_window(
    db_session: AsyncSession,
) -> None:
    """Once the show is being watched, episode 1 is the window's business.

    The user sampled it, liked it, added it as watching and watched episode 1:
    the row leaves the window like any other want and is **deleted**, because
    ``watch_progress`` is the better record of the same fact.
    """
    anime = await make_anime(db_session, anilist_id=964043)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sampled-then-watched@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    await compute_wants(db_session)
    await make_entry(db_session, user, anime, progress=1)

    result = await compute_wants(db_session)

    assert await db_session.get(Want, (user.id, rows[0].id)) is None
    assert result.removed == 1
    assert await live_wants(db_session, user.id) == {rows[1].id, rows[2].id}


async def test_a_sample_in_the_window_of_a_followed_show_is_left_live(
    db_session: AsyncSession,
) -> None:
    """And while it *is* in the window, the two accounts of it agree."""
    anime = await make_anime(db_session, anilist_id=964044)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sampled-then-planned@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    await make_entry(db_session, user, anime, status=ListStatus.PLANNED, progress=0)

    await compute_wants(db_session)

    assert await live_wants(db_session, user.id) == {rows[0].id, rows[1].id}
    want = await db_session.get(Want, (user.id, rows[0].id))
    assert want is not None and want.sample is True, "still the user's own request"


async def test_an_ignored_sample_goes_stale_like_any_other_want(
    db_session: AsyncSession,
) -> None:
    """FR-T2 is a sample's intended end: ready, unwatched, D days, dropped."""
    anime = await make_anime(db_session, anilist_id=964045)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "stale-sample@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    rows[0].state = EpisodeState.READY
    rows[0].state_changed_at = now() - timedelta(days=22)
    await db_session.flush()

    result = await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[0].id))
    assert want is not None
    assert want.drop_reason == STALE_DROP_REASON
    assert result.dropped == 1


async def test_a_stale_sample_is_not_revived_by_the_next_tick(
    db_session: AsyncSession,
) -> None:
    """Otherwise the drop would be undone a quarter of an hour after it applied."""
    anime = await make_anime(db_session, anilist_id=964046)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "stale-sample-sticks@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    rows[0].state = EpisodeState.READY
    rows[0].state_changed_at = now() - timedelta(days=22)
    await db_session.flush()
    await compute_wants(db_session)
    want = await db_session.get(Want, (user.id, rows[0].id))
    assert want is not None and want.dropped_at is not None
    dropped_at = want.dropped_at

    result = await compute_wants(db_session)

    assert want.dropped_at == dropped_at
    assert want.drop_reason == STALE_DROP_REASON, "not re-labelled as 'no longer watching'"
    assert (result.revived, result.dropped, result.shelved) == (0, 0, 0)


async def test_pressing_the_button_again_after_a_stale_drop_revives_the_sample(
    db_session: AsyncSession,
) -> None:
    """Coming back is the user's to declare, and this is the declaration.

    Only the user's own press clears a stale drop; nothing in the reconciler
    does. The episode here is not on the disk any more (retention has been
    through it), which is the case this happens in — a sample whose file is
    still ready needs no reviving to be watched.
    """
    anime = await make_anime(db_session, anilist_id=964047)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sample-again@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    rows[0].state = EpisodeState.READY
    rows[0].state_changed_at = now() - timedelta(days=22)
    await db_session.flush()
    await compute_wants(db_session)
    rows[0].state = EpisodeState.NOT_WANTED
    rows[0].state_changed_at = now()
    await db_session.flush()

    want = (
        await request_sample(
            db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
        )
    ).want

    assert (want.dropped_at, want.drop_reason) == (None, None)
    assert (await episode_states(db_session, anime.id))[1] is EpisodeState.WANTED, (
        "and it goes looking for it again"
    )
    assert len(await search_jobs(db_session)) == 1
    result = await compute_wants(db_session)
    assert await live_wants(db_session, user.id) == {rows[0].id}
    assert (result.shelved, result.dropped) == (0, 0), "the tick leaves the revival alone"


# --- One sample per (user, show) --------------------------------------------


async def test_a_second_press_answers_the_same_sample(db_session: AsyncSession) -> None:
    """Idempotent, and idempotent *cheaply*: the second press writes nothing."""
    anime = await make_anime(db_session, anilist_id=964050)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "press-twice@arc.test")

    first = await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    second = await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert second.want is first.want
    assert second.episode.number == 1
    assert await live_wants(db_session, user.id) == {rows[0].id}
    assert len(await recompute_jobs(db_session)) == 1, "nothing to reconcile the second time"


async def test_a_lower_numbered_episode_appearing_later_does_not_open_a_second_sample(
    db_session: AsyncSession,
) -> None:
    """The catalogue publishing a special as episode 0 must not split the sample.

    "The lowest-numbered episode" is only stable while the episode list is, and
    a second live row would leave Cancel closing one of two — which is how a
    want survives a user who has plainly said no.
    """
    anime = await make_anime(db_session, anilist_id=964051)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "special-later@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    db_session.add(
        Episode(
            anime_id=anime.id,
            number=0,
            air_at=rows[0].air_at,
            state=EpisodeState.NOT_WANTED,
        )
    )
    await db_session.flush()
    again = await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert again.want.episode_id == rows[0].id, "the sample the user already has"
    assert await live_wants(db_session, user.id) == {rows[0].id}


async def test_cancelling_closes_every_live_sample_on_the_show(
    db_session: AsyncSession,
) -> None:
    """Belt and braces: one press of Cancel leaves nothing wanting the show."""
    anime = await make_anime(db_session, anilist_id=964052)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "cancel-all@arc.test")
    # Two live samples, as only a hand-written row can produce today.
    db_session.add(Want(user_id=user.id, episode_id=rows[0].id, sample=True))
    db_session.add(Want(user_id=user.id, episode_id=rows[1].id, sample=True))
    await db_session.flush()

    assert await cancel_sample(db_session, user_id=user.id, anime_id=anime.id, now=now()) is True

    assert await live_wants(db_session, user.id) == set()
    for row in rows[:2]:
        want = await db_session.get(Want, (user.id, row.id))
        assert want is not None and want.drop_reason == SAMPLE_CANCELLED_REASON


# --- Watching a sample ------------------------------------------------------


async def test_a_watched_sample_on_a_completed_show_is_shelved(
    db_session: AsyncSession,
) -> None:
    """The one ending nothing else provides (FR-S4 keeps an existing status).

    A rewatch-sample on a show the user has already completed: the show never
    joins ``wanting``, and FR-T2 will not drop a want whose user watched the
    episode. So the reconciler shelves it, and retention's grace runs from the
    completion instead of the bytes being pinned for good.
    """
    anime = await make_anime(db_session, anilist_id=964053)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "rewatch-sample@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    await compute_wants(db_session)
    db_session.add(
        WatchProgress(
            user_id=user.id,
            episode_id=rows[0].id,
            position_s=1400.0,
            completed=True,
            completed_at=now(),
        )
    )
    await db_session.flush()

    result = await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[0].id))
    assert want is not None
    assert want.dropped_at is not None, "no longer wanted, so no longer pinning the file"
    assert want.drop_reason == REASON_NOT_WANTING
    assert result.shelved == 1
    assert await live_wants(db_session, user.id) == set()

    second = await compute_wants(db_session)
    assert second.revived == 0, "and the next tick does not bring it back"
    assert want.dropped_at is not None


async def test_watching_a_sample_of_an_unlisted_show_hands_it_to_the_window(
    db_session: AsyncSession,
) -> None:
    """The ordinary path, unchanged: FR-S4 adds it as watching and N takes over."""
    anime = await make_anime(db_session, anilist_id=964054)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sample-then-watch@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    await compute_wants(db_session)
    # What ``progress.py`` does on a 90 % report for a show with no entry.
    db_session.add(
        WatchProgress(
            user_id=user.id,
            episode_id=rows[0].id,
            position_s=1400.0,
            completed=True,
            completed_at=now(),
        )
    )
    await make_entry(db_session, user, anime, status=ListStatus.WATCHING, progress=1)

    result = await compute_wants(db_session)

    assert await db_session.get(Want, (user.id, rows[0].id)) is None, "deleted, not tombstoned"
    assert result.removed == 1
    assert await live_wants(db_session, user.id) == {rows[1].id, rows[2].id}


# --- What the routes do to the episode, there and then ----------------------
#
# The first version of this only enqueued ``compute_wants``, which meant the
# show page said "Not fetched" for up to fifteen minutes after somebody pressed
# the button — and for ever while acquisition was paused. Both writers now act
# on the episode through the reconciler's own helpers.


async def test_a_request_starts_the_search_immediately(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=964060)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "start-now@arc.test")

    requested = await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert requested.episode.state is EpisodeState.WANTED, "what the response reports"
    assert (await episode_states(db_session, anime.id))[1] is EpisodeState.WANTED
    queued = await search_jobs(db_session)
    assert len(queued) == 1
    assert queued[0].payload["episode_id"] == rows[0].id
    assert queued[0].payload[DEDUPE_FIELD] == search_dedupe_key(rows[0].id), (
        "one search per episode, however many people want it (FR-A2)"
    )
    assert len(await recompute_jobs(db_session)) == 1, "and the rest is still reconciled"


async def test_a_request_looks_again_at_an_unavailable_episode_today(
    db_session: AsyncSession,
) -> None:
    """The retry gate is for the sweep, not for a person who has just asked.

    ``UNAVAILABLE_RETRY`` keeps the fifteen-minute reconciliation from
    re-opening a search every tick for something Nyaa has not had for a
    fortnight (FR-A6). An explicit request is the one case where looking again
    is exactly what was asked for.
    """
    anime = await make_anime(db_session, anilist_id=964061)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "retry-now@arc.test")
    rows[0].state = EpisodeState.UNAVAILABLE
    rows[0].state_changed_at = now() - timedelta(hours=2)
    rows[0].unavailable_reason = "no acceptable release found"
    await db_session.flush()

    requested = await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert requested.episode.state is EpisodeState.WANTED
    assert len(await search_jobs(db_session)) == 1


async def test_a_request_leaves_an_episode_already_in_flight_alone(
    db_session: AsyncSession,
) -> None:
    """Anything past ``wanted`` has work or bytes behind it (spec §6)."""
    anime = await make_anime(db_session, anilist_id=964062)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "in-flight@arc.test")
    rows[0].state = EpisodeState.DOWNLOADING
    await db_session.flush()

    requested = await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert requested.episode.state is EpisodeState.DOWNLOADING
    assert await search_jobs(db_session) == [], "nothing to search for: it is coming"
    assert await live_wants(db_session, user.id) == {rows[0].id}


async def test_cancelling_releases_the_episode_immediately(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=964063)
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "release-now@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    assert (await episode_states(db_session, anime.id))[1] is EpisodeState.WANTED

    await cancel_sample(db_session, user_id=user.id, anime_id=anime.id, now=now())

    assert (await episode_states(db_session, anime.id))[1] is EpisodeState.NOT_WANTED


async def test_cancelling_leaves_an_episode_another_user_wants(
    db_session: AsyncSession,
) -> None:
    """FR-A2 again: one user backing out is not the last word."""
    anime = await make_anime(db_session, anilist_id=964064)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    sampler = await make_user(db_session, "release-merged@arc.test")
    watcher = await make_user(db_session, "keeps-wanting@arc.test")
    await make_entry(db_session, watcher, anime, progress=0)
    await request_sample(
        db_session, settings=TMDB_ON, user_id=sampler.id, anime_id=anime.id, now=now()
    )
    await compute_wants(db_session)

    await cancel_sample(db_session, user_id=sampler.id, anime_id=anime.id, now=now())

    assert (await episode_states(db_session, anime.id))[1] is EpisodeState.WANTED
    assert await live_wants(db_session, watcher.id) == {rows[0].id, rows[1].id}


async def test_cancelling_stops_a_download_nobody_else_wants(db_session: AsyncSession) -> None:
    """Owner 2026-09-13: Cancel stops it now, rather than at the next tick.

    Pressing Cancel while the sample was downloading used to let it run to the
    end and transcode for nobody. It now goes through the reconciler's own
    ``cancel_if_unwanted``, so the episode returns to ``not_wanted`` and a
    ``qbit_cancel`` job removes the torrent and its partial files.
    """
    anime = await make_anime(db_session, anilist_id=964065)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "mid-download@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    rows[0].state = EpisodeState.DOWNLOADING
    torrent = Torrent(episode_id=rows[0].id, info_hash="e" * 40, qbit_state="downloading")
    db_session.add(torrent)
    await db_session.flush()

    await cancel_sample(db_session, user_id=user.id, anime_id=anime.id, now=now())

    assert (await episode_states(db_session, anime.id))[1] is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED
    queued = await db_session.scalars(select(Job).where(Job.type == QBIT_CANCEL))
    assert [job.payload["episode_id"] for job in queued.all()] == [rows[0].id]


async def test_cancelling_leaves_a_download_another_user_wants(db_session: AsyncSession) -> None:
    """FR-A2 again: one user backing out is not the last word on the bytes."""
    anime = await make_anime(db_session, anilist_id=964075)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    sampler = await make_user(db_session, "mid-download-mine@arc.test")
    other = await make_user(db_session, "mid-download-theirs@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=sampler.id, anime_id=anime.id, now=now()
    )
    db_session.add(Want(user_id=other.id, episode_id=rows[0].id))
    rows[0].state = EpisodeState.DOWNLOADING
    torrent = Torrent(episode_id=rows[0].id, info_hash="f" * 40, qbit_state="downloading")
    db_session.add(torrent)
    await db_session.flush()

    await cancel_sample(db_session, user_id=sampler.id, anime_id=anime.id, now=now())

    assert (await episode_states(db_session, anime.id))[1] is EpisodeState.DOWNLOADING
    assert torrent.qbit_state == "downloading"
    assert (await db_session.scalars(select(Job).where(Job.type == QBIT_CANCEL))).all() == []


async def test_cancelling_does_not_touch_bytes_that_have_landed(
    db_session: AsyncSession,
) -> None:
    """From ``downloaded`` on, the file is retention's to measure (FR-T1)."""
    anime = await make_anime(db_session, anilist_id=964076)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "landed-sample@arc.test")
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    rows[0].state = EpisodeState.READY
    torrent = Torrent(episode_id=rows[0].id, info_hash="d" * 40, qbit_state="stoppedUP")
    db_session.add(torrent)
    await db_session.flush()

    await cancel_sample(db_session, user_id=user.id, anime_id=anime.id, now=now())

    assert (await episode_states(db_session, anime.id))[1] is EpisodeState.READY
    assert torrent.qbit_state == "stoppedUP"
    assert (await db_session.scalars(select(Job).where(Job.type == QBIT_CANCEL))).all() == []


# --- The pictures, asked for with the episode (§5.8) ------------------------
#
# A sampled show's episode still used to arrive with the nightly TMDB sweep, or
# whenever the viewer next opened Watch Now — whose shelves are the only other
# thing that asks. The show page never did (owner, 2026-09-13).


async def test_a_request_queues_the_shows_tmdb_enrichment(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=964070)
    await make_episodes(db_session, anime, 12, aired_through=12)
    await map_to_tmdb(db_session, anime)
    user = await make_user(db_session, "stills-please@arc.test")

    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    queued = await enrich_jobs(db_session)
    assert len(queued) == 1
    assert queued[0].payload["anime_id"] == anime.id
    assert queued[0].payload[DEDUPE_FIELD] == tmdb_dedupe_key(anime.id)
    assert queued[0].payload.get("art_only", False) is False, (
        "a still is the whole point, and an art-only run fetches none"
    )


async def test_a_request_queues_no_enrichment_when_the_art_is_already_in(
    db_session: AsyncSession,
) -> None:
    """Nothing to fetch is nothing to queue: the helper's own gate."""
    anime = await make_anime(db_session, anilist_id=964071)
    anime.banner_url = "https://image.tmdb.test/banner.jpg"
    anime.backdrop_url = "https://image.tmdb.test/backdrop.jpg"
    anime.cover_large_url = "https://image.tmdb.test/poster.jpg"
    episodes = await make_episodes(db_session, anime, 12, aired_through=12)
    for episode in episodes:
        episode.still_url = f"https://image.tmdb.test/still-{episode.number}.jpg"
    await map_to_tmdb(db_session, anime)
    await db_session.flush()
    user = await make_user(db_session, "already-filled@arc.test")

    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert await enrich_jobs(db_session) == []


async def test_a_request_queues_no_enrichment_for_an_unmapped_show(
    db_session: AsyncSession,
) -> None:
    """A job that could only log "no TMDB id" is not worth a row."""
    anime = await make_anime(db_session, anilist_id=964072)
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "unmapped@arc.test")

    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert await enrich_jobs(db_session) == []
    assert len(await search_jobs(db_session)) == 1, "the sample itself is unaffected"


async def test_a_request_queues_no_enrichment_without_a_tmdb_key(
    db_session: AsyncSession,
) -> None:
    """A keyless deployment queues nothing at all (owner, 2026-09-13).

    The handler would skip itself anyway, but a press of the button would then
    write a job row whose only outcome is one INFO line — and on a deployment
    with no key every row is a hole for ever, so nothing would ever stop.
    """
    anime = await make_anime(db_session, anilist_id=964074)
    await make_episodes(db_session, anime, 12, aired_through=12)
    await map_to_tmdb(db_session, anime)
    user = await make_user(db_session, "no-key@arc.test")

    await request_sample(
        db_session, settings=TMDB_OFF, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert await enrich_jobs(db_session) == []
    assert len(await search_jobs(db_session)) == 1, "the sample itself is unaffected"


async def test_a_second_press_does_not_queue_a_second_enrichment(
    db_session: AsyncSession,
) -> None:
    """One key per show, first caller wins — and the second press re-asks."""
    anime = await make_anime(db_session, anilist_id=964073)
    await make_episodes(db_session, anime, 12, aired_through=12)
    await map_to_tmdb(db_session, anime)
    user = await make_user(db_session, "press-twice-art@arc.test")

    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert len(await enrich_jobs(db_session)) == 1


@pytest.mark.parametrize("status", [ListStatus.WATCHING, ListStatus.PLANNED])
async def test_a_dormant_entry_can_be_sampled(db_session: AsyncSession, status: ListStatus) -> None:
    """FR-A9 changed what the refusal is about: the window, not the status.

    A dormant watching/planned entry has no window — nothing is being fetched
    for it — so "you are already following this show; the next episodes are
    fetched automatically" would be false, and one episode is a smaller thing
    to ask for than the whole show.
    """
    anime = await make_anime(
        db_session,
        anilist_id=964070 + int(status is ListStatus.PLANNED),
        status=FINISHED,
    )
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, f"sample-dormant-{status.value}@arc.test")
    entry = await make_entry(db_session, user, anime, status=status, activated=False)

    sample = await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert sample.episode.id == rows[0].id
    assert sample.want.sample is True
    # And the entry stays dormant: one episode is what was asked for.
    # Activating would have handed the show the whole N-episode window.
    assert entry.activated_at is None


async def test_a_sample_on_a_dormant_entry_is_the_only_thing_fetched(
    db_session: AsyncSession,
) -> None:
    """The reconciler treats the pair as not-wanting, so it is read off the row.

    Exactly the unlisted-show path: one live want on episode 1 and nothing
    from the window, even though the entry says ``watching``.
    """
    anime = await make_anime(db_session, anilist_id=964072, status=FINISHED)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sample-dormant-window@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.WATCHING, activated=False)
    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    await compute_wants(db_session)

    assert await live_wants(db_session, user.id) == {rows[0].id}


@pytest.mark.parametrize("status", [ListStatus.WATCHING, ListStatus.PLANNED])
async def test_an_airing_dormant_entry_is_still_refused(
    db_session: AsyncSession, status: ListStatus
) -> None:
    """FR-A9's exception means an airing show *does* have a window."""
    anime = await make_anime(
        db_session,
        anilist_id=964074 + int(status is ListStatus.PLANNED),
        status=RELEASING,
    )
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, f"sample-airing-{status.value}@arc.test")
    await make_entry(db_session, user, anime, status=status, activated=False)

    with pytest.raises(AlreadyFollowing):
        await request_sample(
            db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
        )


async def test_a_sample_never_activates_the_entry(db_session: AsyncSession) -> None:
    """On any status: "Try episode 1" is a request for one episode.

    The show starts fetching properly when the user says so — a status, a
    completion, or the Show page's "Fetch this show".
    """
    anime = await make_anime(db_session, anilist_id=964076, status=FINISHED)
    await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "sample-no-activate@arc.test")
    entry = await make_entry(db_session, user, anime, status=ListStatus.DROPPED, activated=False)

    await request_sample(
        db_session, settings=TMDB_ON, user_id=user.id, anime_id=anime.id, now=now()
    )

    assert entry.activated_at is None
