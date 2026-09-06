"""The catalogue jobs (FR-C5, FR-C6, FR-C7).

``catalog_refresh_all`` and ``catalog_pre_air`` pick *which* shows to refresh,
``catalog_reconcile`` picks which rows still need an AniList id, and
``catalog_season_sweep`` decides what "this season" means. Those choices are
the whole content of the jobs, so they are tested against real rows rather
than a mock repository.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.api import anime as anime_api
from arc.config import Settings
from arc.models import Anime, Episode, Job, JobStatus, ListEntry, ListStatus, User
from arc.services.anilist.client import parse_media
from arc.services.auth import create_user
from arc.services.catalog import Breaker, CatalogService, episodes_for, names
from arc.services.catalog import jobs as catalog_jobs
from arc.services.catalog.cache import upsert_summaries
from arc.services.catalog.jobs import (
    PRE_AIR,
    RECONCILE,
    REFRESH,
    REFRESH_ALL,
    SEASON_SWEEP,
    SPACING_SECONDS,
)
from arc.services.catalog.seasons import current_season, next_season, season_of
from arc.services.jobs import JobContext, enqueue, registered_types
from arc.services.mal.catalog import JST
from tests.anilist_mock import (
    FRIEREN_ID,
    RELEASING_ID,
    FakeAniList,
    frieren_fake,
    load,
    season_node,
    summary_of,
)
from tests.mal_mock import FRIEREN_MAL_ID, SEASON_NAME, SEASON_YEAR, FakeMal
from tests.mal_mock import frieren_fake as mal_frieren_fake

pytestmark = pytest.mark.pg


def epoch(when: datetime) -> int:
    return int(when.timestamp())


async def make_user(session: AsyncSession, email: str) -> User:
    user = await create_user(session, email, "a-long-enough-password")
    await session.flush()
    return user


async def add_anime(
    session: AsyncSession,
    anilist_id: int | None,
    *,
    mal_id: int | None = None,
    status: str = "FINISHED",
    next_airing_at: datetime | None = None,
    format: str | None = None,
    season: str | None = None,
    season_year: int | None = None,
) -> Anime:
    anime = Anime(
        anilist_id=anilist_id,
        mal_id=mal_id,
        title_romaji=f"Show {anilist_id or mal_id}",
        status=status,
        format=format,
        season=season,
        season_year=season_year,
    )
    if next_airing_at is not None:
        anime.next_airing = {"episode": 7, "airingAt": epoch(next_airing_at), "timeUntilAiring": 0}
    session.add(anime)
    await session.flush()
    return anime


async def follow(session: AsyncSession, user: User, anime: Anime, status: ListStatus) -> ListEntry:
    entry = ListEntry(user_id=user.id, anime_id=anime.id, status=status, progress=0)
    session.add(entry)
    await session.flush()
    return entry


def context(session: AsyncSession, settings: Settings, job_type: str) -> JobContext:
    job = Job(id=1, type=job_type, payload={}, status=JobStatus.RUNNING)
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("test.jobs")
    )


async def queued(session: AsyncSession, job_type: str) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == job_type).order_by(Job.id))
    return list(rows.all())


@pytest.fixture
def anilist() -> FakeAniList:
    """Frieren by id, by MAL id, by search, and in Fall 2023."""
    fake = frieren_fake()
    fake.seasons[(SEASON_YEAR, SEASON_NAME)] = [
        season_node(fake.media[FRIEREN_ID]["data"]["Media"])  # type: ignore[index]
    ]
    return fake


@pytest.fixture
def mal() -> FakeMal:
    return mal_frieren_fake()


@pytest.fixture
def catalog(
    anilist: FakeAniList, mal: FakeMal, monkeypatch: pytest.MonkeyPatch
) -> Iterator[CatalogService]:
    """Make every job's ``catalog_for`` yield one service over the fakes.

    The jobs build their own catalogue from settings, which is right in
    production and useless in a test; this replaces the factory rather than the
    handlers, so what runs is the handler's own code path.
    """
    service = CatalogService(anilist.source(), mal.source(), Breaker(300.0))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_catalog_for(settings: Settings):  # type: ignore[no-untyped-def]
        yield service

    monkeypatch.setattr("arc.services.catalog.jobs.catalog_for", fake_catalog_for)
    yield service


# --- Registration -----------------------------------------------------------


def test_every_handler_is_registered() -> None:
    assert {REFRESH, REFRESH_ALL, PRE_AIR, RECONCILE, SEASON_SWEEP} <= registered_types()


def test_the_api_and_the_worker_agree_on_the_job_type_and_dedupe_key() -> None:
    """The endpoint and the sweeps must queue the same work under one key.

    They used to hold private copies of both, so a change to either would have
    made a manual refresh and a swept one stop deduplicating against each
    other — two catalogue fetches for one show, silently.
    """
    assert anime_api.REFRESH_JOB == catalog_jobs.REFRESH == names.REFRESH == "catalog_refresh"
    assert (
        anime_api.refresh_dedupe_key(7)
        == catalog_jobs.dedupe_key(7)
        == names.dedupe_key(7)
        == "catalog_refresh:7"
    )
    # Not just equal strings: the same object, because there is one definition.
    assert anime_api.refresh_dedupe_key is names.dedupe_key
    assert catalog_jobs.dedupe_key is names.dedupe_key


# --- Seasons -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("month", "expected"),
    [
        (1, "WINTER"),
        (3, "WINTER"),
        (4, "SPRING"),
        (6, "SPRING"),
        (7, "SUMMER"),
        (9, "SUMMER"),
        (10, "FALL"),
        (12, "FALL"),
    ],
)
def test_a_date_maps_to_its_quarter(month: int, expected: str) -> None:
    assert season_of(datetime(2026, month, 15, tzinfo=UTC).date()) == (2026, expected)


def test_fall_rolls_into_the_next_years_winter() -> None:
    assert next_season(2026, "FALL") == (2027, "WINTER")
    assert next_season(2026, "WINTER") == (2026, "SPRING")


def test_the_current_season_is_measured_in_utc() -> None:
    assert current_season(datetime(2023, 11, 2, 23, 30, tzinfo=UTC)) == (2023, "FALL")


# --- catalog_refresh ---------------------------------------------------------


async def test_refresh_fetches_and_caches_one_show(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, anilist: FakeAniList
) -> None:
    row = await add_anime(db_session, FRIEREN_ID)

    ctx = context(db_session, settings, REFRESH)
    ctx.job.payload = {"anime_id": row.id}
    await catalog_jobs.catalog_refresh(ctx)

    anime = await db_session.get(Anime, row.id, populate_existing=True)
    assert anime is not None
    assert anime.episodes == 28
    assert anime.refreshed_at is not None
    assert anime.detail_source == "anilist"


async def test_refresh_ignores_the_cache_age(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, anilist: FakeAniList
) -> None:
    """The point of the job is that whatever is cached is not to be trusted."""
    row = await add_anime(db_session, FRIEREN_ID)
    ctx = context(db_session, settings, REFRESH)
    ctx.job.payload = {"anime_id": row.id}

    await catalog_jobs.catalog_refresh(ctx)  # fetch 1, row now fresh
    await catalog_jobs.catalog_refresh(ctx)  # fetch 2 anyway

    assert len([name for name, _ in anilist.calls if name == "media"]) == 2


async def test_refresh_replaces_estimated_dates_when_anilist_is_back(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    anilist: FakeAniList,
    mal: FakeMal,
    slept: list[float],
) -> None:
    """The second half of the FR-C6 round trip: MAL fills, AniList corrects."""
    anilist.disabled = True
    row = await add_anime(db_session, None, mal_id=FRIEREN_MAL_ID)
    ctx = context(db_session, settings, REFRESH)
    ctx.job.payload = {"anime_id": row.id}
    await catalog_jobs.catalog_refresh(ctx)

    episodes = await episodes_for(db_session, row.id)
    assert len(episodes) == 28
    assert all(episode.air_at_estimated for episode in episodes)

    anilist.disabled = False
    catalog.breaker.reset()
    row.anilist_id = FRIEREN_ID  # as the reconciliation job would have left it
    await db_session.flush()
    await catalog_jobs.catalog_refresh(ctx)

    episodes = await episodes_for(db_session, row.id)
    # Everything AniList publishes (5..28) loses its badge. Episodes 1–4 aired
    # as one broadcast and have no published slot, so they stay estimated — but
    # re-anchored to AniList's episode 5 rather than left where MAL put them.
    assert all(episode.air_at_estimated is False for episode in episodes[4:])
    assert all(episode.air_at_estimated for episode in episodes[:4])
    assert episodes[3].air_at is not None and episodes[4].air_at is not None
    assert episodes[3].air_at < episodes[4].air_at
    anime = await db_session.get(Anime, row.id, populate_existing=True)
    assert anime is not None and anime.detail_source == "anilist"


# --- catalog_refresh_all -----------------------------------------------------


async def test_the_daily_sweep_picks_followed_and_releasing_shows(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await make_user(db_session, "sweeper@arc.test")
    watching = await add_anime(db_session, 800001, status="FINISHED")
    dropped = await add_anime(db_session, 800002, status="FINISHED")
    releasing = await add_anime(db_session, 800003, status="RELEASING")
    await add_anime(db_session, 800004, status="FINISHED")  # nobody's, not airing
    await follow(db_session, user, watching, ListStatus.WATCHING)
    await follow(db_session, user, dropped, ListStatus.DROPPED)

    await catalog_jobs.catalog_refresh_all(context(db_session, settings, REFRESH_ALL))

    jobs = await queued(db_session, REFRESH)
    assert [job.payload["anime_id"] for job in jobs] == [watching.id, releasing.id]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ListStatus.WATCHING, True),
        (ListStatus.PLANNED, True),
        (ListStatus.ON_HOLD, True),
        (ListStatus.DROPPED, False),
        (ListStatus.COMPLETED, False),
    ],
)
async def test_only_states_that_still_generate_wants_are_swept(
    db_session: AsyncSession, settings: Settings, status: ListStatus, expected: bool
) -> None:
    """FR-W4: dropped and completed shows generate no wants, so no refresh."""
    user = await make_user(db_session, f"{status.value}@arc.test")
    anime = await add_anime(db_session, 800100, status="FINISHED")
    await follow(db_session, user, anime, status)

    await catalog_jobs.catalog_refresh_all(context(db_session, settings, REFRESH_ALL))

    assert bool(await queued(db_session, REFRESH)) is expected


async def test_the_daily_sweep_queues_one_job_per_show_across_users(
    db_session: AsyncSession, settings: Settings
) -> None:
    one = await make_user(db_session, "one@arc.test")
    two = await make_user(db_session, "two@arc.test")
    anime = await add_anime(db_session, 800200, status="RELEASING")
    await follow(db_session, one, anime, ListStatus.WATCHING)
    await follow(db_session, two, anime, ListStatus.PLANNED)

    await catalog_jobs.catalog_refresh_all(context(db_session, settings, REFRESH_ALL))

    assert len(await queued(db_session, REFRESH)) == 1


async def test_the_daily_sweep_is_idempotent(db_session: AsyncSession, settings: Settings) -> None:
    await add_anime(db_session, 800300, status="RELEASING")
    await add_anime(db_session, 800301, status="RELEASING")
    ctx = context(db_session, settings, REFRESH_ALL)

    await catalog_jobs.catalog_refresh_all(ctx)
    await catalog_jobs.catalog_refresh_all(ctx)

    assert len(await queued(db_session, REFRESH)) == 2


async def test_the_children_are_spaced_out(db_session: AsyncSession, settings: Settings) -> None:
    """Sixty shows must not become sixty catalogue requests in one minute."""
    for offset in range(4):
        await add_anime(db_session, 800400 + offset, status="RELEASING")

    await catalog_jobs.catalog_refresh_all(context(db_session, settings, REFRESH_ALL))

    jobs = await queued(db_session, REFRESH)
    gaps = [
        (later.run_after - earlier.run_after).total_seconds()
        for earlier, later in zip(jobs, jobs[1:], strict=False)
    ]
    assert gaps == [SPACING_SECONDS] * 3


async def test_an_already_queued_show_does_not_consume_a_spacing_slot(
    db_session: AsyncSession, settings: Settings
) -> None:
    await add_anime(db_session, 800500, status="RELEASING")
    await add_anime(db_session, 800501, status="RELEASING")
    ctx = context(db_session, settings, REFRESH_ALL)
    await catalog_jobs.catalog_refresh_all(ctx)

    await add_anime(db_session, 800502, status="RELEASING")
    await catalog_jobs.catalog_refresh_all(ctx)

    jobs = await queued(db_session, REFRESH)
    assert len(jobs) == 3
    # One new job, one new slot: the second sweep does not re-count the two it
    # skipped, so the newcomer lands one spacing gap past the queue's tail
    # rather than three.
    assert jobs[2].run_after == jobs[1].run_after + timedelta(seconds=SPACING_SECONDS)


async def test_a_sweep_starts_after_the_refreshes_already_queued(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Two sweeps firing together must not hand their children the same slots.

    The daily and the hourly sweep meet every four hours. Each counting its
    offsets from its own "now" would put both sets of refreshes on the same
    instants and double the request rate exactly when it is highest.
    """
    now = datetime.now(UTC)
    for offset in range(3):
        await enqueue(
            db_session,
            REFRESH,
            {"anime_id": 800600 + offset},
            run_after=now + timedelta(seconds=offset * SPACING_SECONDS),
            dedupe_key=catalog_jobs.dedupe_key(800600 + offset),
        )
    last_existing = (await queued(db_session, REFRESH))[-1].run_after

    created = [
        await add_anime(db_session, 800700 + offset, status="RELEASING") for offset in (0, 1)
    ]
    await catalog_jobs.catalog_refresh_all(context(db_session, settings, REFRESH_ALL))

    jobs = await queued(db_session, REFRESH)
    assert [job.payload["anime_id"] for job in jobs[3:]] == [row.id for row in created]
    assert jobs[3].run_after == last_existing + timedelta(seconds=SPACING_SECONDS)
    assert jobs[4].run_after == last_existing + timedelta(seconds=2 * SPACING_SECONDS)


# --- catalog_pre_air ---------------------------------------------------------


async def test_the_pre_air_sweep_picks_what_airs_soon_or_just_did(
    db_session: AsyncSession, settings: Settings
) -> None:
    now = datetime.now(UTC)
    soon = await add_anime(
        db_session, 801001, status="RELEASING", next_airing_at=now + timedelta(minutes=30)
    )
    just_aired = await add_anime(
        db_session, 801002, status="RELEASING", next_airing_at=now - timedelta(hours=2)
    )
    await add_anime(db_session, 801003, status="RELEASING", next_airing_at=now + timedelta(days=3))
    await add_anime(db_session, 801004, status="RELEASING", next_airing_at=now - timedelta(days=2))
    await add_anime(db_session, 801005, status="FINISHED")  # no next_airing at all

    await catalog_jobs.catalog_pre_air(context(db_session, settings, PRE_AIR))

    jobs = await queued(db_session, REFRESH)
    assert {job.payload["anime_id"] for job in jobs} == {soon.id, just_aired.id}


async def test_the_pre_air_sweep_shares_the_dedupe_key_with_the_daily_one(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Both sweeps hitting the same show must produce one refresh, not two."""
    now = datetime.now(UTC)
    await add_anime(
        db_session, 801100, status="RELEASING", next_airing_at=now + timedelta(minutes=10)
    )

    await catalog_jobs.catalog_refresh_all(context(db_session, settings, REFRESH_ALL))
    await catalog_jobs.catalog_pre_air(context(db_session, settings, PRE_AIR))

    assert len(await queued(db_session, REFRESH)) == 1


# --- catalog_reconcile -------------------------------------------------------


async def test_reconcile_attaches_the_anilist_id_to_a_mal_only_row(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, slept: list[float]
) -> None:
    """FR-C6: the repair pass that ends an outage's worth of orphan rows."""
    row = await add_anime(db_session, None, mal_id=FRIEREN_MAL_ID)

    await catalog_jobs.catalog_reconcile(context(db_session, settings, RECONCILE))

    refreshed = await db_session.get(Anime, row.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.anilist_id == FRIEREN_ID
    assert refreshed.mal_id == FRIEREN_MAL_ID


async def test_reconcile_leaves_rows_that_already_have_an_anilist_id(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, anilist: FakeAniList
) -> None:
    await add_anime(db_session, FRIEREN_ID, mal_id=FRIEREN_MAL_ID)

    await catalog_jobs.catalog_reconcile(context(db_session, settings, RECONCILE))

    assert anilist.calls == []  # nothing to ask about


async def test_reconcile_asks_anilist_specifically_not_the_fallback(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, mal: FakeMal
) -> None:
    """Letting MAL answer would return the record Arc already has and learn nothing."""
    await add_anime(db_session, None, mal_id=FRIEREN_MAL_ID)

    await catalog_jobs.catalog_reconcile(context(db_session, settings, RECONCILE))

    assert mal.calls == []


async def test_reconcile_is_skipped_while_anilist_is_open(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, anilist: FakeAniList
) -> None:
    """Fifty lookups against a source that is down is fifty timeouts and no ids."""
    await add_anime(db_session, None, mal_id=FRIEREN_MAL_ID)
    catalog.breaker.record_failure("anilist", "still down")

    await catalog_jobs.catalog_reconcile(context(db_session, settings, RECONCILE))

    assert anilist.calls == []


async def test_reconcile_stops_when_anilist_goes_down_mid_run(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    anilist: FakeAniList,
    slept: list[float],
) -> None:
    """The rest of the batch would fail the same way; the next hour retries."""
    for offset in range(4):
        await add_anime(db_session, None, mal_id=900000 + offset)
    anilist.disabled = True

    await catalog_jobs.catalog_reconcile(context(db_session, settings, RECONCILE))

    assert len(anilist.calls) == 1
    assert catalog.breaker.is_open("anilist") is True


async def test_reconcile_paces_itself(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, slept: list[float]
) -> None:
    """Fifty lookups in a burst is over AniList's real rate limit."""
    for offset in range(3):
        await add_anime(db_session, None, mal_id=900100 + offset)

    await catalog_jobs.catalog_reconcile(context(db_session, settings, RECONCILE))

    assert slept == [catalog_jobs.RECONCILE_SPACING_SECONDS] * 2  # gaps, not per row


async def test_reconcile_caps_a_run(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, slept: list[float]
) -> None:
    for offset in range(catalog_jobs.RECONCILE_LIMIT + 5):
        await add_anime(db_session, None, mal_id=910000 + offset)

    await catalog_jobs.catalog_reconcile(context(db_session, settings, RECONCILE))

    assert len(slept) == catalog_jobs.RECONCILE_LIMIT - 1


async def test_reconcile_refuses_to_duplicate_an_anilist_id(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    caplog: pytest.LogCaptureFixture,
    slept: list[float],
) -> None:
    """Two rows cannot hold one AniList id; a clash is a thing to report."""
    await add_anime(db_session, FRIEREN_ID)  # already holds it, under a different mal id
    orphan = await add_anime(db_session, None, mal_id=FRIEREN_MAL_ID)

    with caplog.at_level("WARNING"):
        await catalog_jobs.catalog_reconcile(context(db_session, settings, RECONCILE))

    refreshed = await db_session.get(Anime, orphan.id, populate_existing=True)
    assert refreshed is not None and refreshed.anilist_id is None
    assert "already in use" in caplog.text


# --- catalog_season_sweep ----------------------------------------------------


async def test_the_season_sweep_caches_the_current_season(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-C7: the schedule has to survive an outage of *both* sources."""
    monkeypatch.setattr(
        "arc.services.catalog.jobs.current_season", lambda: (SEASON_YEAR, SEASON_NAME)
    )

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    rows = list((await db_session.scalars(select(Anime))).all())
    assert FRIEREN_ID in {row.anilist_id for row in rows}
    assert all(row.summary_source == "anilist" for row in rows)
    # Summaries only: opening one still triggers a full fetch.
    assert all(row.refreshed_at is None for row in rows)


async def test_the_season_sweep_also_does_the_next_season(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    anilist: FakeAniList,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "arc.services.catalog.jobs.current_season", lambda: (SEASON_YEAR, SEASON_NAME)
    )

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    asked = [(v["seasonYear"], v["season"]) for name, v in anilist.calls if name == "season"]
    assert asked == [(2023, "FALL"), (2024, "WINTER")]


async def test_the_season_sweep_falls_back_to_mal(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    anilist: FakeAniList,
    monkeypatch: pytest.MonkeyPatch,
    slept: list[float],
) -> None:
    anilist.disabled = True
    monkeypatch.setattr(
        "arc.services.catalog.jobs.current_season", lambda: (SEASON_YEAR, SEASON_NAME)
    )

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    rows = list((await db_session.scalars(select(Anime))).all())
    assert FRIEREN_MAL_ID in {row.mal_id for row in rows}
    assert all(row.summary_source == "mal" for row in rows)
    assert all(row.anilist_id is None for row in rows)


async def test_the_season_sweep_records_the_next_broadcast(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    anilist: FakeAniList,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A season row with no next broadcast has no weekday (FR-C3, FR-C7).

    The sweep writes summaries, and ``next_airing`` is the one field the season
    query asks for beyond them — precisely so the schedule can be rendered from
    the pre-cache alone when both sources are down.
    """
    releasing = load("media_999001_releasing")["data"]["Media"]
    anilist.seasons[(SEASON_YEAR, SEASON_NAME)] = [season_node(releasing)]
    monkeypatch.setattr(
        "arc.services.catalog.jobs.current_season", lambda: (SEASON_YEAR, SEASON_NAME)
    )

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    row = await db_session.scalar(select(Anime).where(Anime.anilist_id == RELEASING_ID))
    assert row is not None
    assert row.next_airing == releasing["nextAiringEpisode"]
    assert row.format == "TV"
    assert (row.season, row.season_year) == ("WINTER", 2026)
    # Still a summary: opening the show must still trigger a full fetch.
    assert row.refreshed_at is None


async def test_a_search_result_never_blanks_a_cached_broadcast(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    anilist: FakeAniList,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The search query does not ask for ``nextAiringEpisode``; its null is not news."""
    releasing = load("media_999001_releasing")["data"]["Media"]
    anilist.seasons[(SEASON_YEAR, SEASON_NAME)] = [season_node(releasing)]
    monkeypatch.setattr(
        "arc.services.catalog.jobs.current_season", lambda: (SEASON_YEAR, SEASON_NAME)
    )
    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    # …and now the same show arrives from a search, which carries no slot.
    await upsert_summaries(db_session, [parse_media(summary_of(releasing), full=False)])

    row = await db_session.scalar(select(Anime).where(Anime.anilist_id == RELEASING_ID))
    assert row is not None
    assert row.next_airing == releasing["nextAiringEpisode"]


#: A MAL season entry for a show that is on the air, with a Friday slot. Built
#: here rather than captured: the fixtures are all from Fall 2023 and have
#: therefore finished airing, and this is the one case the synthesised slot
#: exists for.
MAL_AIRING_NODE = {
    "node": {
        "id": 999003,
        "title": "Kin'youbi no Ban",
        "media_type": "tv",
        "status": "currently_airing",
        "num_episodes": 12,
        "start_date": "2023-10-06",
        "start_season": {"year": SEASON_YEAR, "season": SEASON_NAME.lower()},
        "broadcast": {"day_of_the_week": "friday", "start_time": "23:00"},
        "main_picture": {"large": "https://img.test/friday.jpg"},
    }
}


async def test_a_mal_season_row_lands_on_its_broadcast_weekday(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    anilist: FakeAniList,
    mal: FakeMal,
    monkeypatch: pytest.MonkeyPatch,
    slept: list[float],
) -> None:
    """The sweep runs through MAL exactly when AniList is down (FR-C6, FR-C7).

    MAL publishes no ``nextAiringEpisode``, so without a synthesised slot every
    row it writes would be a show the schedule cannot place — an empty week for
    the whole of an outage, which is the state FR-C7 exists to prevent.
    """
    anilist.disabled = True
    mal.seasons[(SEASON_YEAR, SEASON_NAME)] = {"data": [MAL_AIRING_NODE], "paging": {}}
    monkeypatch.setattr(
        "arc.services.catalog.jobs.current_season", lambda: (SEASON_YEAR, SEASON_NAME)
    )

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    row = await db_session.scalar(select(Anime).where(Anime.mal_id == 999003))
    assert row is not None
    assert row.summary_source == "mal"
    assert row.next_airing is not None
    # The weekday is Japanese, and it is in the future: this is the *next*
    # broadcast, not the premiere.
    at = datetime.fromtimestamp(row.next_airing["airingAt"], JST)
    assert at.weekday() == 4
    assert at.time() == time(23, 0)
    assert at > datetime.now(UTC)
    # …and it names no episode, because MAL does not know which one is next.
    assert row.next_airing["episode"] is None
    assert row.next_airing["estimated"] is True


async def test_a_finished_mal_season_row_gets_no_slot(
    db_session: AsyncSession,
    settings: Settings,
    catalog: CatalogService,
    anilist: FakeAniList,
    monkeypatch: pytest.MonkeyPatch,
    slept: list[float],
) -> None:
    """Frieren finished airing in 2024; it has no next broadcast."""
    anilist.disabled = True
    monkeypatch.setattr(
        "arc.services.catalog.jobs.current_season", lambda: (SEASON_YEAR, SEASON_NAME)
    )

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    row = await db_session.scalar(select(Anime).where(Anime.mal_id == FRIEREN_MAL_ID))
    assert row is not None
    assert row.next_airing is None


async def test_the_season_sweep_survives_a_season_nobody_can_answer(
    db_session: AsyncSession,
    settings: Settings,
    anilist: FakeAniList,
    monkeypatch: pytest.MonkeyPatch,
    slept: list[float],
) -> None:
    """One dead season must not cost the other one."""
    service = CatalogService(anilist.source(), FakeMal(fail_with=503).source(), Breaker(0.0))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_catalog_for(settings: Settings):  # type: ignore[no-untyped-def]
        yield service

    monkeypatch.setattr("arc.services.catalog.jobs.catalog_for", fake_catalog_for)
    monkeypatch.setattr(
        "arc.services.catalog.jobs.current_season", lambda: (SEASON_YEAR, SEASON_NAME)
    )
    anilist.seasons.pop((2024, "WINTER"), None)  # AniList has it; MAL is down

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    rows = list((await db_session.scalars(select(Anime))).all())
    assert FRIEREN_ID in {row.anilist_id for row in rows}


# --- The season sweep's follow-up detail fetches ------------------------------
#
# The season query carries ``nextAiringEpisode`` and nothing else about airing,
# so a show between broadcasts comes out of the sweep with no slot and no
# episodes — and the schedule has nothing to place it with (FR-C3). Only the
# detail query asks for ``airingSchedule``, so those rows are followed up one
# at a time.


@pytest.fixture
def empty_seasons(anilist: FakeAniList, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sweep that caches nothing, so only the seeded rows are candidates."""
    anilist.seasons.clear()
    monkeypatch.setattr(
        "arc.services.catalog.jobs.current_season", lambda: (SEASON_YEAR, SEASON_NAME)
    )


async def add_season_row(
    session: AsyncSession, anilist_id: int, *, format: str | None = "TV", **kwargs: object
) -> Anime:
    """A current-season row, as the sweep's summaries would leave it."""
    return await add_anime(
        session,
        anilist_id,
        status="RELEASING",
        format=format,
        season=SEASON_NAME,
        season_year=SEASON_YEAR,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_the_sweep_queues_a_detail_fetch_for_every_unplaceable_row(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, empty_seasons: None
) -> None:
    """Three weekly-format rows with neither a slot nor an episode: three refreshes."""
    rows = [
        await add_season_row(db_session, 803000 + offset, format=format)
        for offset, format in enumerate(("TV", "TV_SHORT", "ONA"))
    ]

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    jobs = await queued(db_session, REFRESH)
    assert [job.payload["anime_id"] for job in jobs] == [row.id for row in rows]
    gaps = [
        (later.run_after - earlier.run_after).total_seconds()
        for earlier, later in zip(jobs, jobs[1:], strict=False)
    ]
    assert gaps == [SPACING_SECONDS] * 2


async def test_the_sweep_leaves_the_rows_the_schedule_can_already_place(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, empty_seasons: None
) -> None:
    """A slot or a single dated episode is enough; a film has no weekday to find."""
    await add_season_row(db_session, 803100, next_airing_at=datetime.now(UTC) + timedelta(days=2))
    with_episodes = await add_season_row(db_session, 803101)
    db_session.add(Episode(anime_id=with_episodes.id, number=1))
    await add_season_row(db_session, 803102, format="MOVIE")
    await add_season_row(db_session, 803103, format=None)
    # Next season is not current: its rows wait for the sweep that follows it.
    await add_anime(
        db_session,
        803104,
        status="RELEASING",
        format="TV",
        season=next_season(SEASON_YEAR, SEASON_NAME)[1],
        season_year=next_season(SEASON_YEAR, SEASON_NAME)[0],
    )
    await db_session.flush()

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    assert await queued(db_session, REFRESH) == []


async def test_the_sweeps_follow_up_is_capped(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, empty_seasons: None
) -> None:
    """A brand-new season is two hundred rows; five minutes of queue is enough."""
    for offset in range(catalog_jobs.SEASON_DETAIL_LIMIT + 5):
        await add_season_row(db_session, 803200 + offset)

    await catalog_jobs.catalog_season_sweep(context(db_session, settings, SEASON_SWEEP))

    assert len(await queued(db_session, REFRESH)) == catalog_jobs.SEASON_DETAIL_LIMIT


async def test_the_follow_up_does_not_duplicate_a_queued_refresh(
    db_session: AsyncSession, settings: Settings, catalog: CatalogService, empty_seasons: None
) -> None:
    """Same job type, same dedupe key: two sweeps are one refresh."""
    await add_season_row(db_session, 803300)
    ctx = context(db_session, settings, SEASON_SWEEP)

    await catalog_jobs.catalog_season_sweep(ctx)
    await catalog_jobs.catalog_season_sweep(ctx)

    assert len(await queued(db_session, REFRESH)) == 1
