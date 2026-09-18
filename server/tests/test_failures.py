"""Watch Now's own-failures banner, over HTTP (FR-W6).

The whole requirement is the word "own": a failure is on this list because
*this* viewer is waiting for that episode or *this* viewer's write did not
land. So most of these tests are about what is **not** there — somebody else's
broken episode, a want the viewer has dropped, a write that succeeded — and
the two that are about presence check the sentence a person actually reads.

The seeding helpers come from :mod:`tests.test_schedule_api` for the reason
:mod:`tests.test_home_api` borrows them: the failures ride on the same
``/api/home`` response and a second copy of "make a show with weekly episodes"
is a second place for the fixtures to drift.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import event, select

from arc.db import SessionFactory
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListStatus,
    MalWriteCause,
    MalWriteLog,
    MalWriteStatus,
    User,
    UserRole,
    Want,
)
from arc.services.catalog.failures import (
    MAL_LIMIT,
    MAX_FAILURES,
    NO_RELEASE,
    RETRY_CLAUSE,
    failures_for_user,
)
from arc.services.media.names import TRANSCODE
from tests.conftest import add_user, api_transport, login
from tests.test_schedule_api import add_anime, add_episodes, follow

pytestmark = pytest.mark.pg

USER_EMAIL = "failures@arc.test"
USER_PASSWORD = "failures-password"

#: The frozen present, as in :mod:`tests.test_home_api`: every ``since`` on
#: this list is a real timestamp and the order is what it decides.
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

#: Episode 1 of the show under test, six weeks back, so the low-numbered ones
#: have aired. Nothing here depends on the air dates — a failure is about the
#: file, not the broadcast — but an episode nothing dated is a different test.
FIRST_AIRED = NOW - timedelta(weeks=6)

FFMPEG_TAIL = (
    "ffmpeg exited 1\n"
    "[libx264 @ 0x55] cannot open display\n"
    "frame= 1200 fps=0.0 q=-1.0 Lsize=N/A time=00:00:50.00 bitrate=N/A speed=0x\n"
)


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr("arc.api.home.now", lambda: NOW)
    return NOW


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


async def failures(client: AsyncClient) -> list[dict[str, Any]]:
    response = await client.get("/api/home")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    rows: list[dict[str, Any]] = body["failures"]
    return rows


async def a_show(factory: SessionFactory, *, title: str, anilist_id: int, count: int = 12) -> int:
    anime_id = await add_anime(
        factory, title=title, anilist_id=anilist_id, status="RELEASING", episodes=count
    )
    await add_episodes(factory, anime_id, count=count, first_at=FIRST_AIRED)
    return anime_id


async def episode_of(factory: SessionFactory, anime_id: int, number: int) -> Episode:
    async with factory() as session:
        found = await session.scalar(
            select(Episode).where(Episode.anime_id == anime_id, Episode.number == number)
        )
        assert found is not None
        return found


async def stop_episode(
    factory: SessionFactory,
    episode_id: int,
    *,
    state: EpisodeState,
    at: datetime,
    unavailable_reason: str | None = None,
    error_tail: str | None = None,
    job_finished_at: datetime | None = None,
) -> None:
    """Put one episode into a stopped state, the way the pipeline would."""
    async with factory() as session:
        row = await session.get(Episode, episode_id)
        assert row is not None
        row.state = state
        row.state_changed_at = at
        row.unavailable_reason = unavailable_reason
        if error_tail is not None:
            session.add(
                Job(
                    type=TRANSCODE,
                    payload={"episode_id": episode_id, "error_tail": error_tail},
                    status=JobStatus.FAILED,
                    finished_at=job_finished_at or at,
                )
            )
        await session.commit()


async def want(
    factory: SessionFactory,
    user: User,
    episode_id: int,
    *,
    dropped: bool = False,
    sample: bool = False,
) -> None:
    async with factory() as session:
        session.add(
            Want(
                user_id=user.id,
                episode_id=episode_id,
                sample=sample,
                dropped_at=NOW - timedelta(days=1) if dropped else None,
                drop_reason="stale" if dropped else None,
            )
        )
        await session.commit()


async def mal_row(
    factory: SessionFactory,
    user: User,
    anime_id: int,
    *,
    status: MalWriteStatus,
    at: datetime,
    field: str = "progress",
    old_value: Any = 4,
    new_value: Any = 5,
    error: str | None = "MAL said 500",
) -> int:
    async with factory() as session:
        row = MalWriteLog(
            user_id=user.id,
            anime_id=anime_id,
            field=field,
            old_value=old_value,
            new_value=new_value,
            cause=MalWriteCause.WATCH,
            status=status,
            error=error,
            created_at=at,
        )
        session.add(row)
        await session.commit()
        return row.id


# --- Episode failures --------------------------------------------------------


async def test_a_broken_transcode_i_am_waiting_for_is_a_failure(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """FR-P4 on the page the viewer opens, with the sentence and not the wall."""
    anime_id = await a_show(api_factory, title="Broken", anilist_id=930001)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory,
        episode.id,
        state=EpisodeState.FAILED,
        at=NOW - timedelta(hours=2),
        error_tail=FFMPEG_TAIL,
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=6)
    await want(api_factory, user, episode.id)

    rows = await failures(client)

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "episode"
    assert row["state"] == "failed"
    assert row["episode_id"] == episode.id
    assert row["episode_number"] == 7
    assert row["anime"]["title"]["preferred"] == "Broken"
    # The first line names the failure and is what survives the trim.
    assert row["reason"].startswith("ffmpeg exited 1")
    assert len(row["reason"]) <= 160
    assert row["since"] is not None


async def test_an_unavailable_episode_says_arc_keeps_looking(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """FR-A6: giving up on *this* attempt is not giving up."""
    anime_id = await a_show(api_factory, title="Nowhere", anilist_id=930002)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory, episode.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(days=1)
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=6)
    await want(api_factory, user, episode.id)

    rows = await failures(client)

    assert [row["reason"] for row in rows] == [NO_RELEASE]
    assert rows[0]["state"] == "unavailable"


async def test_an_unavailable_episode_keeps_the_reason_it_recorded(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """A stall has a better answer than "no release found" (FR-A6, 2026-09-13)."""
    anime_id = await a_show(api_factory, title="Stalled", anilist_id=930003)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory,
        episode.id,
        state=EpisodeState.UNAVAILABLE,
        at=NOW - timedelta(hours=6),
        unavailable_reason="no seeders after 6 hours",
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=6)
    await want(api_factory, user, episode.id)

    rows = await failures(client)

    assert rows[0]["reason"] == f"no seeders after 6 hours{RETRY_CLAUSE}"


async def test_a_sample_i_asked_for_is_mine_too(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """FR-A8: one episode somebody asked to try, with no list entry at all."""
    anime_id = await a_show(api_factory, title="Sampled", anilist_id=930004)
    episode = await episode_of(api_factory, anime_id, 1)
    await stop_episode(
        api_factory, episode.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(hours=1)
    )
    await want(api_factory, user, episode.id, sample=True)

    rows = await failures(client)

    assert len(rows) == 1
    assert rows[0]["episode_id"] == episode.id
    # No list entry, so no badge — and the row still knows which show it is.
    assert rows[0]["anime"]["list_status"] is None


async def test_a_dropped_want_is_not_a_failure(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """FR-T2 dropped it, so nobody is waiting; retention keeps the row, not the
    banner."""
    anime_id = await a_show(api_factory, title="Given up", anilist_id=930005)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory,
        episode.id,
        state=EpisodeState.FAILED,
        at=NOW - timedelta(hours=2),
        error_tail=FFMPEG_TAIL,
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=6)
    await want(api_factory, user, episode.id, dropped=True)

    assert await failures(client) == []


async def test_an_episode_nobody_wants_is_nobodys_failure(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """On the list is not the same as waiting for it: the want is the rule."""
    anime_id = await a_show(api_factory, title="Unwanted", anilist_id=930006)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory, episode.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(hours=2)
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=6)

    assert await failures(client) == []


async def test_another_users_broken_episode_is_not_mine(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    other = await add_user(api_factory, "not.me@arc.test", "other-password")
    anime_id = await a_show(api_factory, title="Theirs", anilist_id=930007)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory,
        episode.id,
        state=EpisodeState.FAILED,
        at=NOW - timedelta(hours=2),
        error_tail=FFMPEG_TAIL,
    )
    await follow(api_factory, other, anime_id, ListStatus.WATCHING, progress=6)
    await want(api_factory, other, episode.id)

    assert await failures(client) == []


async def test_an_episode_in_flight_is_not_a_failure(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """Downloading, preparing and ready are not news; §6's two dead ends are."""
    anime_id = await a_show(api_factory, title="Working", anilist_id=930008)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)
    for number, state in ((1, EpisodeState.DOWNLOADING), (2, EpisodeState.PREPARING)):
        episode = await episode_of(api_factory, anime_id, number)
        await stop_episode(api_factory, episode.id, state=state, at=NOW - timedelta(hours=1))
        await want(api_factory, user, episode.id)

    assert await failures(client) == []


async def test_a_failure_with_no_transcode_job_still_says_something(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """The job row may have been swept; "it broke" is still worth reading."""
    anime_id = await a_show(api_factory, title="Silent", anilist_id=930009)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory, episode.id, state=EpisodeState.FAILED, at=NOW - timedelta(hours=3)
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=6)
    await want(api_factory, user, episode.id)

    rows = await failures(client)

    assert rows[0]["reason"] == "preparing this episode failed; an admin can retry it"


# --- MyAnimeList write failures ---------------------------------------------


async def test_a_failed_mal_write_of_mine_is_a_failure(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """FR-M6, on the page the viewer opens rather than only on the sync page."""
    anime_id = await a_show(api_factory, title="Logged", anilist_id=930010)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=5)
    log_id = await mal_row(
        api_factory, user, anime_id, status=MalWriteStatus.FAILED, at=NOW - timedelta(minutes=10)
    )

    rows = await failures(client)

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "mal"
    assert row["key"] == f"mal:{log_id}"
    assert row["log_id"] == log_id
    assert row["field"] == "progress"
    assert row["old_value"] == 4
    assert row["new_value"] == 5
    assert row["reason"] == "MAL said 500"
    assert row["anime"]["title"]["preferred"] == "Logged"
    assert row["episode_id"] is None


async def test_a_mal_write_that_landed_is_not_a_failure(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, title="Fine", anilist_id=930011)
    await mal_row(
        api_factory, user, anime_id, status=MalWriteStatus.OK, at=NOW - timedelta(minutes=5)
    )
    await mal_row(
        api_factory,
        user,
        anime_id,
        status=MalWriteStatus.PENDING,
        at=NOW - timedelta(minutes=4),
        field="score",
        error=None,
    )
    await mal_row(
        api_factory,
        user,
        anime_id,
        status=MalWriteStatus.SKIPPED,
        at=NOW - timedelta(minutes=3),
        field="status",
    )

    assert await failures(client) == []


async def test_another_users_failed_write_is_not_mine(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    other = await add_user(api_factory, "not.me@arc.test", "other-password")
    anime_id = await a_show(api_factory, title="Theirs", anilist_id=930012)
    await mal_row(
        api_factory, other, anime_id, status=MalWriteStatus.FAILED, at=NOW - timedelta(minutes=1)
    )

    assert await failures(client) == []


async def test_a_failed_write_with_no_error_still_says_something(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, title="Wordless", anilist_id=930013)
    await mal_row(
        api_factory,
        user,
        anime_id,
        status=MalWriteStatus.FAILED,
        at=NOW - timedelta(minutes=2),
        error=None,
    )

    rows = await failures(client)

    assert rows[0]["reason"] == "the update did not go through"


async def test_the_mal_half_is_capped(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """A push that failed on a whole list must not be the entire banner."""
    anime_id = await a_show(api_factory, title="Every row", anilist_id=930014)
    for minute in range(MAL_LIMIT + 5):
        await mal_row(
            api_factory,
            user,
            anime_id,
            status=MalWriteStatus.FAILED,
            at=NOW - timedelta(minutes=minute),
            error=f"attempt {minute}",
        )

    rows = await failures(client)

    assert len(rows) == MAL_LIMIT
    # Newest first: minute 0 is the most recent.
    assert rows[0]["reason"] == "attempt 0"


# --- Order, cap and keys -----------------------------------------------------


async def test_both_kinds_are_ordered_newest_first_together(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """One question — "what is broken?" — so one order (FR-W6)."""
    anime_id = await a_show(api_factory, title="Mixed", anilist_id=930015)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)
    old = await episode_of(api_factory, anime_id, 1)
    recent = await episode_of(api_factory, anime_id, 2)
    await stop_episode(
        api_factory, old.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(days=3)
    )
    await stop_episode(
        api_factory, recent.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(minutes=1)
    )
    await want(api_factory, user, old.id)
    await want(api_factory, user, recent.id)
    await mal_row(
        api_factory, user, anime_id, status=MalWriteStatus.FAILED, at=NOW - timedelta(hours=2)
    )

    rows = await failures(client)

    assert [(row["kind"], row.get("episode_number")) for row in rows] == [
        ("episode", 2),
        ("mal", None),
        ("episode", 1),
    ]


async def test_the_whole_list_is_capped(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, title="All broken", anilist_id=930016, count=30)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)
    for number in range(1, 26):
        episode = await episode_of(api_factory, anime_id, number)
        await stop_episode(
            api_factory,
            episode.id,
            state=EpisodeState.UNAVAILABLE,
            at=NOW - timedelta(minutes=number),
        )
        await want(api_factory, user, episode.id)

    rows = await failures(client)

    assert len(rows) == MAX_FAILURES


async def test_a_key_is_stable_until_the_failure_changes(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """What the client's dismissal hangs on: same failure, same key; new
    failure on the same episode, new key (so a dismissal cannot hide it)."""
    anime_id = await a_show(api_factory, title="Again", anilist_id=930017)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory, episode.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(days=1)
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=6)
    await want(api_factory, user, episode.id)

    first = (await failures(client))[0]["key"]
    again = (await failures(client))[0]["key"]
    assert first == again

    # The daily retry found a release, it downloaded, and the transcode broke:
    # a different state at a different moment, and therefore a different row.
    await stop_episode(
        api_factory,
        episode.id,
        state=EpisodeState.FAILED,
        at=NOW - timedelta(minutes=5),
        error_tail=FFMPEG_TAIL,
    )
    changed = (await failures(client))[0]["key"]

    assert changed != first
    assert changed.startswith(f"episode:{episode.id}:failed:")


async def test_a_second_unavailable_day_is_a_new_failure(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """FR-A6 retries daily, so yesterday's dismissal must not be for ever."""
    anime_id = await a_show(api_factory, title="Daily", anilist_id=930018)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory, episode.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(days=2)
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=6)
    await want(api_factory, user, episode.id)

    yesterday = (await failures(client))[0]["key"]

    await stop_episode(
        api_factory, episode.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(hours=1)
    )

    assert (await failures(client))[0]["key"] != yesterday


async def test_an_empty_banner_is_an_empty_list(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """The ordinary answer, and the one the client draws nothing for."""
    anime_id = await a_show(api_factory, title="Quiet", anilist_id=930019)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=3)

    assert await failures(client) == []


async def test_the_banner_needs_a_session(api_app: FastAPI) -> None:
    async with api_transport(api_app) as anonymous:
        response = await anonymous.get("/api/home")
    assert response.status_code == 401


async def test_an_admin_sees_their_own_failures_and_nothing_more(
    api_app: FastAPI, api_factory: SessionFactory
) -> None:
    """No global view here: the Admin jobs tab is where the queue lives."""
    admin = await add_user(api_factory, "boss@arc.test", "boss-password", role=UserRole.ADMIN)
    other = await add_user(api_factory, "member@arc.test", "member-password")
    mine = await a_show(api_factory, title="Mine", anilist_id=930020)
    theirs = await a_show(api_factory, title="Theirs", anilist_id=930021)
    my_episode = await episode_of(api_factory, mine, 7)
    their_episode = await episode_of(api_factory, theirs, 7)
    for episode_id in (my_episode.id, their_episode.id):
        await stop_episode(
            api_factory, episode_id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(hours=1)
        )
    await want(api_factory, admin, my_episode.id)
    await want(api_factory, other, their_episode.id)

    async with api_transport(api_app) as http:
        client = await login(http, "boss@arc.test", "boss-password")
        rows = await failures(client)

    assert [row["anime"]["title"]["preferred"] for row in rows] == ["Mine"]


async def test_the_show_is_named_even_when_nothing_detailed_it(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """The row carries an ordinary ``AnimeSummary``, so an undetailed show
    sends the same empties every other card on this page sends."""
    anime_id = await a_show(api_factory, title="Bare", anilist_id=930022)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory, episode.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(hours=1)
    )
    await want(api_factory, user, episode.id, sample=True)

    row = (await failures(client))[0]

    assert row["anime"]["genres"] == []
    assert row["anime"]["backdrop_url"] is None
    assert row["anime"]["id"] == anime_id


async def test_the_failures_cost_one_query_per_kind(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """Three broken episodes, one SELECT for all of them (FR-W6).

    Asked of the service rather than over HTTP, because the rest of
    ``/api/home`` reads ``jobs`` for its own reasons (the transcode percentages
    and FR-A7's next-search times) and this is a claim about *these* two
    queries: one for the wants, one for the log, and one more for the transcode
    jobs only where something came back ``failed``.
    """
    anime_id = await a_show(api_factory, title="Counted", anilist_id=930023)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)
    for number in (1, 2, 3):
        episode = await episode_of(api_factory, anime_id, number)
        await stop_episode(
            api_factory,
            episode.id,
            state=EpisodeState.FAILED,
            at=NOW - timedelta(minutes=number),
            error_tail=FFMPEG_TAIL,
        )
        await want(api_factory, user, episode.id)

    counted: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *rest: object) -> None:
        counted.append(statement.lower())

    engine = api_factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with api_factory() as session:
            row = await session.get(User, user.id)
            assert row is not None
            counted.clear()
            rows = await failures_for_user(session, row)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(rows) == 3
    assert len(counted) == 3, counted
    assert len([one for one in counted if "join wants" in one]) == 1
    assert len([one for one in counted if "from mal_write_log" in one]) == 1
    # One lookup for every broken episode's job, not one each (``DISTINCT ON``
    # in :func:`~arc.services.media.names.latest_transcode_jobs`).
    assert len([one for one in counted if "from jobs" in one]) == 1


async def test_a_page_with_nothing_broken_pays_for_two_queries(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """The ordinary case: no ``failed`` episode, so no transcode lookup."""
    anime_id = await a_show(api_factory, title="Quiet too", anilist_id=930029)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=3)

    counted: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *rest: object) -> None:
        counted.append(statement.lower())

    engine = api_factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with api_factory() as session:
            row = await session.get(User, user.id)
            assert row is not None
            counted.clear()
            assert await failures_for_user(session, row) == []
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(counted) == 2, counted
    assert [one for one in counted if "from jobs" in one] == []


async def test_the_failures_do_not_disturb_the_shelves(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """A broken episode is a failure and not a "ready to watch" (FR-W1)."""
    anime_id = await a_show(api_factory, title="Both", anilist_id=930025)
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory, episode.id, state=EpisodeState.FAILED, at=NOW - timedelta(hours=1)
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=6)
    await want(api_factory, user, episode.id)

    response = await client.get("/api/home")
    body: dict[str, Any] = response.json()

    assert len(body["failures"]) == 1
    assert body["ready_to_watch"] == []
    assert body["continue_watching"] == []


async def test_the_cached_show_is_what_names_the_row(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """The English title wins where the cache has one, like every other card."""
    anime_id = await a_show(api_factory, title="Romaji only", anilist_id=930028)
    async with api_factory() as session:
        row = await session.get(Anime, anime_id)
        assert row is not None
        row.title_english = "English name"
        await session.commit()
    episode = await episode_of(api_factory, anime_id, 7)
    await stop_episode(
        api_factory, episode.id, state=EpisodeState.UNAVAILABLE, at=NOW - timedelta(hours=1)
    )
    await want(api_factory, user, episode.id, sample=True)

    assert (await failures(client))[0]["anime"]["title"]["preferred"] == "English name"
