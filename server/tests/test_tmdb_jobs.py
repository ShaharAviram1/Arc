"""The TMDB jobs: who gets swept, what happens without a key, and the dedupe.

The sweep's whole content is the SELECT — which followed shows still have a
hole TMDB could fill, and which of them the id map can actually reach — so it
is tested against real rows rather than a mock repository, the same way the
catalogue sweeps are.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    OfflineId,
    Rendition,
    User,
    WatchProgress,
)
from arc.services.auth import create_user
from arc.services.catalog import jobs as catalog_jobs
from arc.services.catalog.credits import STUDIO_ROLE
from arc.services.catalog.seasons import current_season, next_season
from arc.services.jobs import JobContext, registered_types
from arc.services.tmdb import client as tmdb_client
from arc.services.tmdb import jobs as tmdb_jobs
from arc.services.tmdb.names import TMDB_ENRICH, TMDB_ENRICH_ALL, dedupe_key
from tests.tmdb_mock import (
    API_KEY,
    FRIEREN_ANILIST_ID,
    FRIEREN_MAL_ID,
    FRIEREN_TV_ID,
    URL,
    FakeTmdb,
)

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _fresh_breaker() -> Any:
    tmdb_client.reset_breaker()
    yield
    tmdb_client.reset_breaker()


@pytest.fixture
def tmdb_settings(settings: Settings) -> Settings:
    """Settings with a key set, so the handlers do not skip themselves."""
    return settings.model_copy(update={"tmdb_api_key": API_KEY})


@pytest.fixture
def fake_tmdb(monkeypatch: pytest.MonkeyPatch) -> FakeTmdb:
    """Point ``TmdbClient.from_settings`` at the recorded fixtures."""
    fake = FakeTmdb()

    def build(cls: type[tmdb_client.TmdbClient], settings: Settings) -> tmdb_client.TmdbClient:
        return cls(API_KEY, url=URL, transport=fake.transport(), min_interval=0.0)

    monkeypatch.setattr(tmdb_client.TmdbClient, "from_settings", classmethod(build))
    monkeypatch.setattr(tmdb_jobs.TmdbClient, "from_settings", classmethod(build))
    return fake


def context(session: AsyncSession, settings: Settings, job_type: str, **payload: Any) -> JobContext:
    job = Job(id=1, type=job_type, payload=dict(payload), status=JobStatus.RUNNING)
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("test.tmdb")
    )


async def add_show(session: AsyncSession, **kwargs: Any) -> Anime:
    defaults: dict[str, Any] = {
        "anilist_id": FRIEREN_ANILIST_ID,
        "mal_id": FRIEREN_MAL_ID,
        "title_romaji": "Sousou no Frieren",
        "episodes": 28,
        "season": "FALL",
        "season_year": 2023,
        "studio": "MADHOUSE",
        "detail_source": "mal",
        "summary_source": "mal",
    }
    anime = Anime(**(defaults | kwargs))
    session.add(anime)
    await session.flush()
    return anime


async def add_mapping(session: AsyncSession, **kwargs: Any) -> OfflineId:
    defaults: dict[str, Any] = {
        "anilist_id": FRIEREN_ANILIST_ID,
        "mal_id": FRIEREN_MAL_ID,
        "tmdb_tv_id": FRIEREN_TV_ID,
        "tmdb_season": 1,
        "type": "TV",
    }
    row = OfflineId(**(defaults | kwargs))
    session.add(row)
    await session.flush()
    return row


async def follow(session: AsyncSession, anime: Anime, *, email: str = "watcher@arc.test") -> User:
    user = await create_user(session, email, "a-long-enough-password")
    await session.flush()
    session.add(ListEntry(user_id=user.id, anime_id=anime.id, status=ListStatus.WATCHING))
    await session.flush()
    return user


async def add_episodes(session: AsyncSession, anime: Anime, count: int, **kwargs: Any) -> None:
    aired = datetime.now(UTC) - timedelta(days=30)
    for number in range(1, count + 1):
        session.add(Episode(anime_id=anime.id, number=number, air_at=aired, **kwargs))
    await session.flush()


async def queued(session: AsyncSession, job_type: str) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == job_type).order_by(Job.id))
    return list(rows.all())


# --- Registration -----------------------------------------------------------


def test_both_handlers_are_registered() -> None:
    assert {TMDB_ENRICH, TMDB_ENRICH_ALL} <= registered_types()


# --- One show ---------------------------------------------------------------


async def test_enrich_fills_the_holes_and_leaves_anilist_alone(
    db_session: AsyncSession, tmdb_settings: Settings, fake_tmdb: FakeTmdb
) -> None:
    anime = await add_show(db_session, banner_url="https://anilist.example/banner.jpg")
    await add_mapping(db_session)
    await add_episodes(db_session, anime, 3)

    await tmdb_jobs.tmdb_enrich(context(db_session, tmdb_settings, TMDB_ENRICH, anime_id=anime.id))

    assert anime.banner_url == "https://anilist.example/banner.jpg"
    assert anime.cover_large_url is not None
    assert anime.credits is not None
    assert any(entry["role"] == "Director" for entry in anime.credits)
    rows = list((await db_session.scalars(select(Episode).order_by(Episode.number))).all())
    assert rows[0].title == "The Journey's End"
    assert rows[0].still_url is not None
    # Three requests: the series, its season, its crew. Not six.
    assert len(fake_tmdb.calls) == 3


async def test_enrich_is_idempotent(
    db_session: AsyncSession, tmdb_settings: Settings, fake_tmdb: FakeTmdb
) -> None:
    anime = await add_show(db_session)
    await add_mapping(db_session)
    await add_episodes(db_session, anime, 2)
    ctx = context(db_session, tmdb_settings, TMDB_ENRICH, anime_id=anime.id)

    await tmdb_jobs.tmdb_enrich(ctx)
    first = (anime.banner_url, anime.cover_large_url, list(anime.credits or []))
    await tmdb_jobs.tmdb_enrich(ctx)

    assert (anime.banner_url, anime.cover_large_url, list(anime.credits or [])) == first


async def test_enrich_without_a_key_is_a_no_op(
    db_session: AsyncSession, settings: Settings, fake_tmdb: FakeTmdb
) -> None:
    anime = await add_show(db_session)
    await add_mapping(db_session)
    await tmdb_jobs.tmdb_enrich(context(db_session, settings, TMDB_ENRICH, anime_id=anime.id))
    assert anime.cover_large_url is None
    assert fake_tmdb.calls == []


async def test_enrich_without_a_mapping_is_a_no_op(
    db_session: AsyncSession, tmdb_settings: Settings, fake_tmdb: FakeTmdb
) -> None:
    """The id map is the only route to TMDB; AniList publishes no id."""
    anime = await add_show(db_session)
    await tmdb_jobs.tmdb_enrich(context(db_session, tmdb_settings, TMDB_ENRICH, anime_id=anime.id))
    assert anime.cover_large_url is None
    assert fake_tmdb.calls == []


async def test_a_mapping_with_no_tmdb_id_does_not_count(
    db_session: AsyncSession, tmdb_settings: Settings, fake_tmdb: FakeTmdb
) -> None:
    anime = await add_show(db_session)
    await add_mapping(db_session, tmdb_tv_id=None, tmdb_movie_id=None, tmdb_season=None)
    await tmdb_jobs.tmdb_enrich(context(db_session, tmdb_settings, TMDB_ENRICH, anime_id=anime.id))
    assert fake_tmdb.calls == []


async def test_a_stale_mapped_id_is_skipped_not_failed(
    db_session: AsyncSession, tmdb_settings: Settings, fake_tmdb: FakeTmdb
) -> None:
    """Fribb's file is a weekly snapshot, and ids do get merged away."""
    anime = await add_show(db_session)
    await add_mapping(db_session, tmdb_tv_id=999999)
    await tmdb_jobs.tmdb_enrich(context(db_session, tmdb_settings, TMDB_ENRICH, anime_id=anime.id))
    assert anime.cover_large_url is None


async def test_a_rate_limit_opens_the_breaker_and_raises(
    db_session: AsyncSession,
    tmdb_settings: Settings,
    fake_tmdb: FakeTmdb,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(tmdb_client, "_sleep", no_wait)
    anime = await add_show(db_session)
    await add_mapping(db_session)
    fake_tmdb.status[f"/tv/{FRIEREN_TV_ID}"] = 429

    with pytest.raises(tmdb_client.TmdbUnavailable):
        await tmdb_jobs.tmdb_enrich(
            context(db_session, tmdb_settings, TMDB_ENRICH, anime_id=anime.id)
        )
    assert tmdb_client.breaker().is_open(tmdb_client.SOURCE)


async def test_an_unknown_anime_id_is_a_no_op(
    db_session: AsyncSession, tmdb_settings: Settings, fake_tmdb: FakeTmdb
) -> None:
    await tmdb_jobs.tmdb_enrich(context(db_session, tmdb_settings, TMDB_ENRICH, anime_id=999_999))
    assert fake_tmdb.calls == []


# --- The sweep --------------------------------------------------------------


async def test_the_sweep_queues_a_followed_show_with_a_hole(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    anime = await add_show(db_session)
    await add_mapping(db_session)
    await follow(db_session, anime)

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))

    jobs = await queued(db_session, TMDB_ENRICH)
    assert [job.payload["anime_id"] for job in jobs] == [anime.id]
    assert jobs[0].payload["dedupe_key"] == dedupe_key(anime.id)


async def test_the_sweep_skips_a_show_nobody_follows(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    anime = await add_show(db_session)
    await add_mapping(db_session)
    assert anime.id
    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_the_sweep_skips_a_show_the_id_map_cannot_reach(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    anime = await add_show(db_session)
    await follow(db_session, anime)
    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_the_sweep_skips_a_show_with_nothing_missing(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    anime = await add_show(
        db_session,
        banner_url="b",
        cover_large_url="c",
        credits=[{"role": STUDIO_ROLE, "name": "Madhouse"}, {"role": "Director", "name": "X"}],
    )
    await add_mapping(db_session)
    await follow(db_session, anime)
    await add_episodes(db_session, anime, 2, title="t", still_url="s")

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_a_studio_only_credits_list_still_counts_as_missing(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """What a MAL detail fetch leaves behind is a hole, not an answer."""
    anime = await add_show(
        db_session,
        banner_url="b",
        cover_large_url="c",
        credits=[{"role": STUDIO_ROLE, "name": "Madhouse"}],
    )
    await add_mapping(db_session)
    await follow(db_session, anime)
    await add_episodes(db_session, anime, 1, title="t", still_url="s")

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert len(await queued(db_session, TMDB_ENRICH)) == 1


async def test_thin_anilist_credits_are_not_a_hole(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """Otherwise the same row is fetched and written-nothing every night."""
    anime = await add_show(
        db_session,
        detail_source="anilist",
        banner_url="b",
        cover_large_url="c",
        credits=[{"role": STUDIO_ROLE, "name": "Madhouse"}],
    )
    await add_mapping(db_session)
    await follow(db_session, anime)
    await add_episodes(db_session, anime, 1, title="t", still_url="s")

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_an_unaired_episode_without_a_still_is_not_a_hole(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    anime = await add_show(
        db_session,
        banner_url="b",
        cover_large_url="c",
        credits=[{"role": STUDIO_ROLE, "name": "M"}, {"role": "Director", "name": "X"}],
    )
    await add_mapping(db_session)
    await follow(db_session, anime)
    session_now = datetime.now(UTC) + timedelta(days=7)
    db_session.add(Episode(anime_id=anime.id, number=1, air_at=session_now))
    await db_session.flush()

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


# --- Watched, not followed (owner, 2026-09-12) -------------------------------


async def watch(
    session: AsyncSession, episode: Episode, *, email: str = "offlist@arc.test"
) -> User:
    """A user part-way through an episode of a show that is on no list."""
    user = await create_user(session, email, "a-long-enough-password")
    await session.flush()
    session.add(WatchProgress(user_id=user.id, episode_id=episode.id, position_s=120.0))
    await session.flush()
    return user


async def one_episode(session: AsyncSession, anime: Anime, **kwargs: Any) -> Episode:
    episode = Episode(
        anime_id=anime.id, number=1, air_at=datetime.now(UTC) - timedelta(days=30), **kwargs
    )
    session.add(episode)
    await session.flush()
    return episode


async def test_a_show_watched_off_list_is_swept_in_full(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """Arc plays what it holds; a list entry is not what makes a show watched."""
    anime = await add_show(db_session)
    await add_mapping(db_session)
    episode = await one_episode(db_session, anime)
    await watch(db_session, episode)

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))

    jobs = await queued(db_session, TMDB_ENRICH)
    assert [job.payload["anime_id"] for job in jobs] == [anime.id]
    # In full: the missing still is the whole reason this show is here.
    assert "art_only" not in jobs[0].payload


async def test_a_show_with_a_ready_episode_is_swept_in_full(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """The file is here and transcoded: somebody is one click from playing it."""
    anime = await add_show(db_session)
    await add_mapping(db_session)
    await one_episode(db_session, anime, state=EpisodeState.READY)

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))

    jobs = await queued(db_session, TMDB_ENRICH)
    assert [job.payload["anime_id"] for job in jobs] == [anime.id]
    assert "art_only" not in jobs[0].payload


async def test_a_finished_rendition_counts_as_being_in_the_library(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """The other record of the same fact, for a row whose state moved on."""
    anime = await add_show(db_session)
    await add_mapping(db_session)
    episode = await one_episode(db_session, anime, state=EpisodeState.MATCHED)
    db_session.add(
        Rendition(
            episode_id=episode.id,
            dir="renditions/1",
            playlist_path="renditions/1/index.m3u8",
            ready_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))

    assert [job.payload["anime_id"] for job in await queued(db_session, TMDB_ENRICH)] == [anime.id]


async def test_a_show_nobody_watches_or_holds_is_still_skipped(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """The widened rule is three ways in, not an open door."""
    anime = await add_show(db_session)
    await add_mapping(db_session)
    await one_episode(db_session, anime, state=EpisodeState.PREPARING)

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


# --- The Home shelves' on-demand stills --------------------------------------


async def test_the_shelves_queue_a_full_enrichment_for_a_card_with_no_still(
    db_session: AsyncSession,
) -> None:
    anime = await add_show(db_session)
    await add_mapping(db_session)

    assert await tmdb_jobs.enqueue_episode_stills(db_session, [anime.id]) == 1

    jobs = await queued(db_session, TMDB_ENRICH)
    assert [job.payload["anime_id"] for job in jobs] == [anime.id]
    # A still is the point, and an art-only run fetches none.
    assert "art_only" not in jobs[0].payload
    assert jobs[0].payload["dedupe_key"] == dedupe_key(anime.id)


async def test_the_shelves_do_not_queue_a_second_job_for_one_show(
    db_session: AsyncSession,
) -> None:
    """Every visit to the page calls this; the queue must not grow with it."""
    anime = await add_show(db_session)
    await add_mapping(db_session)

    assert await tmdb_jobs.enqueue_episode_stills(db_session, [anime.id, anime.id]) == 1
    assert await tmdb_jobs.enqueue_episode_stills(db_session, [anime.id]) == 0
    assert len(await queued(db_session, TMDB_ENRICH)) == 1


async def test_the_shelves_skip_a_show_the_id_map_cannot_reach(
    db_session: AsyncSession,
) -> None:
    anime = await add_show(db_session)
    assert await tmdb_jobs.enqueue_episode_stills(db_session, [anime.id]) == 0
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_the_shelves_are_bounded_by_their_own_size(
    db_session: AsyncSession,
) -> None:
    for index in range(1, 5):
        await add_show(db_session, anilist_id=index, mal_id=index)
        await add_mapping(db_session, anilist_id=index, mal_id=index, tmdb_tv_id=index)
    ids = [anime_id for anime_id in (await db_session.scalars(select(Anime.id))).all()]

    assert await tmdb_jobs.enqueue_episode_stills(db_session, ids, limit=2) == 2
    assert len(await queued(db_session, TMDB_ENRICH)) == 2


async def test_the_shelves_queue_nothing_for_an_empty_page(
    db_session: AsyncSession,
) -> None:
    assert await tmdb_jobs.enqueue_episode_stills(db_session, []) == 0


async def test_the_sweep_without_a_key_queues_nothing(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime = await add_show(db_session)
    await add_mapping(db_session)
    await follow(db_session, anime)
    await tmdb_jobs.tmdb_enrich_all(context(db_session, settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_the_sweep_spaces_its_children_out(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    first = await add_show(db_session)
    second = await add_show(db_session, anilist_id=2, mal_id=2, title_romaji="Another")
    await add_mapping(db_session)
    await add_mapping(db_session, anilist_id=2, mal_id=2, tmdb_tv_id=2)
    await follow(db_session, first)
    await follow(db_session, second, email="second@arc.test")

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))

    jobs = await queued(db_session, TMDB_ENRICH)
    assert len(jobs) == 2
    gap = (jobs[1].run_after - jobs[0].run_after).total_seconds()
    assert gap == pytest.approx(tmdb_jobs.SPACING_SECONDS)


async def test_the_sweep_does_not_queue_a_second_job_for_one_show(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    anime = await add_show(db_session)
    await add_mapping(db_session)
    await follow(db_session, anime)
    ctx = context(db_session, tmdb_settings, TMDB_ENRICH_ALL)

    await tmdb_jobs.tmdb_enrich_all(ctx)
    await tmdb_jobs.tmdb_enrich_all(ctx)

    assert len(await queued(db_session, TMDB_ENRICH)) == 1


# --- The on-demand trigger --------------------------------------------------


async def test_enqueue_enrichment_dedupes_against_a_pending_job(
    db_session: AsyncSession,
) -> None:
    anime = await add_show(db_session)
    first = await tmdb_jobs.enqueue_enrichment(db_session, anime.id)
    second = await tmdb_jobs.enqueue_enrichment(db_session, anime.id)
    assert first is not None
    assert second is None
    assert len(await queued(db_session, TMDB_ENRICH)) == 1


async def test_a_refresh_queues_an_enrichment_for_a_followed_show_with_a_hole(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """The on-demand trigger inside ``catalog_refresh`` (M15.5 bullet 4).

    Called directly rather than through the whole refresh job: the fetch above
    it is AniList's and is covered by ``test_catalog_jobs``; what is being
    asserted here is which rows the hook picks, and it is the only caller.
    """
    anime = await add_show(db_session)
    await follow(db_session, anime)
    ctx = context(db_session, tmdb_settings, "catalog_refresh")

    await catalog_jobs._maybe_enrich(ctx, anime)

    jobs = await queued(db_session, TMDB_ENRICH)
    assert [job.payload["anime_id"] for job in jobs] == [anime.id]


async def test_a_refresh_queues_nothing_for_a_show_nobody_follows(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    anime = await add_show(db_session)
    await catalog_jobs._maybe_enrich(context(db_session, tmdb_settings, "catalog_refresh"), anime)
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_a_refresh_queues_nothing_when_the_row_is_complete(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    anime = await add_show(db_session, banner_url="b", cover_large_url="c")
    await follow(db_session, anime)
    await add_episodes(db_session, anime, 2, title="t", still_url="s")
    await catalog_jobs._maybe_enrich(context(db_session, tmdb_settings, "catalog_refresh"), anime)
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_a_refresh_does_not_queue_a_second_enrichment(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """A show refreshed hourly must not queue an enrichment an hour."""
    anime = await add_show(db_session)
    await follow(db_session, anime)
    ctx = context(db_session, tmdb_settings, "catalog_refresh")

    await catalog_jobs._maybe_enrich(ctx, anime)
    await catalog_jobs._maybe_enrich(ctx, anime)

    assert len(await queued(db_session, TMDB_ENRICH)) == 1


# --- The season pass and art-only mode --------------------------------------


async def season_show(session: AsyncSession, **kwargs: Any) -> Anime:
    """A show of the season Arc is in right now. Nobody follows it."""
    year, season = current_season()
    return await add_show(session, season=season, season_year=year, **kwargs)


async def test_the_sweep_queues_a_season_show_nobody_follows_art_only(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """The fix for the owner's hero: an unfollowed season show gets its art."""
    anime = await season_show(db_session)
    await add_mapping(db_session)

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))

    jobs = await queued(db_session, TMDB_ENRICH)
    assert [job.payload["anime_id"] for job in jobs] == [anime.id]
    assert jobs[0].payload["art_only"] is True


async def test_the_season_pass_reaches_next_season_too(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    year, season = next_season(*current_season())
    anime = await add_show(db_session, season=season, season_year=year)
    await add_mapping(db_session)

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))

    jobs = await queued(db_session, TMDB_ENRICH)
    assert [job.payload["anime_id"] for job in jobs] == [anime.id]


async def test_the_season_pass_skips_a_show_the_id_map_cannot_reach(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    await season_show(db_session)
    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_the_season_pass_skips_a_show_that_already_has_key_art(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """Missing credits and stills are not the season pass's business."""
    await season_show(db_session, banner_url="b", cover_large_url="c")
    await add_mapping(db_session)
    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_a_show_out_of_season_and_unfollowed_is_not_swept(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    await add_show(db_session, season="FALL", season_year=2023)
    await add_mapping(db_session)
    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_the_followed_pass_comes_first_and_is_never_art_only(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """A season show somebody watches is worth three requests, not one."""
    popular = await season_show(db_session, popularity=500_000)
    await add_mapping(db_session)
    watched = await add_show(
        db_session, anilist_id=2, mal_id=2, title_romaji="Watched", popularity=1
    )
    await add_mapping(db_session, anilist_id=2, mal_id=2, tmdb_tv_id=2)
    await follow(db_session, watched)

    candidates = await tmdb_jobs.sweep_candidates(db_session)
    assert candidates == [(watched.id, False), (popular.id, True)]


async def test_a_followed_season_show_is_swept_once_in_full(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    anime = await season_show(db_session)
    await add_mapping(db_session)
    await follow(db_session, anime)

    await tmdb_jobs.tmdb_enrich_all(context(db_session, tmdb_settings, TMDB_ENRICH_ALL))

    jobs = await queued(db_session, TMDB_ENRICH)
    assert len(jobs) == 1
    assert "art_only" not in jobs[0].payload


async def test_the_season_pass_takes_the_most_popular_first(
    db_session: AsyncSession, tmdb_settings: Settings
) -> None:
    """The hero picks a season by popularity, so a capped sweep must too."""
    quiet = await season_show(db_session, popularity=None)
    middling = await season_show(db_session, anilist_id=2, mal_id=2, popularity=10)
    loud = await season_show(db_session, anilist_id=3, mal_id=3, popularity=99)
    await add_mapping(db_session)
    await add_mapping(db_session, anilist_id=2, mal_id=2, tmdb_tv_id=2)
    await add_mapping(db_session, anilist_id=3, mal_id=3, tmdb_tv_id=3)

    candidates = await tmdb_jobs.sweep_candidates(db_session)
    assert [anime_id for anime_id, _ in candidates] == [loud.id, middling.id, quiet.id]


async def test_an_art_only_enrichment_is_one_request_and_no_stills(
    db_session: AsyncSession, tmdb_settings: Settings, fake_tmdb: FakeTmdb
) -> None:
    anime = await season_show(db_session)
    await add_mapping(db_session)
    await add_episodes(db_session, anime, 3)

    await tmdb_jobs.tmdb_enrich(
        context(db_session, tmdb_settings, TMDB_ENRICH, anime_id=anime.id, art_only=True)
    )

    assert anime.banner_url is not None
    assert anime.cover_large_url is not None
    assert anime.credits is None
    rows = list((await db_session.scalars(select(Episode).order_by(Episode.number))).all())
    assert [row.still_url for row in rows] == [None, None, None]
    assert [row.title for row in rows] == [None, None, None]
    # One request: the series. Not its season and not its crew.
    assert len(fake_tmdb.calls) == 1


# --- The Home hero's on-demand trigger --------------------------------------


async def test_the_hero_queues_art_for_an_unfollowed_season_show(
    db_session: AsyncSession,
) -> None:
    anime = await season_show(db_session)
    await add_mapping(db_session)

    assert await tmdb_jobs.enqueue_hero_art(db_session) == 1

    jobs = await queued(db_session, TMDB_ENRICH)
    assert [job.payload["anime_id"] for job in jobs] == [anime.id]
    assert jobs[0].payload["art_only"] is True
    assert jobs[0].payload["dedupe_key"] == dedupe_key(anime.id)


async def test_the_hero_does_not_queue_a_second_job_for_one_show(
    db_session: AsyncSession,
) -> None:
    """Every visit to the page calls this; the queue must not grow with it."""
    await season_show(db_session)
    await add_mapping(db_session)

    assert await tmdb_jobs.enqueue_hero_art(db_session) == 1
    assert await tmdb_jobs.enqueue_hero_art(db_session) == 0
    assert len(await queued(db_session, TMDB_ENRICH)) == 1


async def test_the_hero_leaves_a_show_that_has_any_key_art_alone(
    db_session: AsyncSession,
) -> None:
    """A poster is enough to frame a hero with; only nothing at all is not."""
    await season_show(db_session, cover_large_url="c")
    await add_mapping(db_session)
    assert await tmdb_jobs.enqueue_hero_art(db_session) == 0
    assert await queued(db_session, TMDB_ENRICH) == []


async def test_the_hero_skips_a_show_the_id_map_cannot_reach(
    db_session: AsyncSession,
) -> None:
    await season_show(db_session)
    assert await tmdb_jobs.enqueue_hero_art(db_session) == 0


async def test_the_hero_is_bounded_by_its_own_size(
    db_session: AsyncSession,
) -> None:
    for index in range(1, 5):
        await season_show(db_session, anilist_id=index, mal_id=index, popularity=index)
        await add_mapping(db_session, anilist_id=index, mal_id=index, tmdb_tv_id=index)

    assert await tmdb_jobs.enqueue_hero_art(db_session, limit=2) == 2
    assert len(await queued(db_session, TMDB_ENRICH)) == 2
