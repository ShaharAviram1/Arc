"""The player's endpoints over HTTP (FR-S2, FR-S3, FR-S4, FR-W3).

Three groups, and they are three different kinds of rule.

``/play`` is arithmetic against one row: where to seek, and what the episodes
either side are. ``/progress`` is the one place in Arc where an *automatic*
event changes a list entry and sets ``mal_dirty``, so most of these tests are
about what it does **not** do — never lower progress, never touch status, never
queue a second reconciliation, never un-complete something.
``/watched`` is FR-W3's manual mark, and its whole point is that it goes down
the same path as reaching ninety per cent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.db import SessionFactory
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    ListStatus,
    Rendition,
    UpdatedBy,
    User,
    WatchProgress,
)
from arc.services.acquisition.names import COMPUTE_WANTS
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "player@arc.test"
USER_PASSWORD = "player-password"

#: The real rendition's length, near enough: a 24-minute episode.
DURATION = 1420.0

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin the router's clock, so ``completed_at`` and ``aired`` are assertable.

    Every call in a test therefore shares one timestamp — which the race below
    depends on: ``newly_completed`` must not be an answer the clock could give.
    """
    monkeypatch.setattr("arc.api.playback.now", lambda: NOW)
    return NOW


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


async def add_show(
    factory: SessionFactory,
    *,
    anilist_id: int,
    title: str = "Play Show",
    count: int = 12,
    ready: tuple[int, ...] = (11,),
    duration: float | None = DURATION,
    status: str = "RELEASING",
    episode_count: int | None = -1,
) -> tuple[int, dict[int, int]]:
    """A show whose ``ready`` episodes have renditions; ``number → episode id``.

    ``status`` and ``episode_count`` are FR-W5's auto-complete conditions: the
    default is a show still airing with a known count, and the sentinel ``-1``
    means "the same as ``count``" so that only a test that cares about the
    difference has to say anything.
    """
    async with factory() as session:
        anime = Anime(
            anilist_id=anilist_id,
            summary_source="anilist",
            detail_source="anilist",
            title_romaji=title,
            format="TV",
            status=status,
            episodes=count if episode_count == -1 else episode_count,
        )
        session.add(anime)
        await session.flush()
        ids: dict[int, int] = {}
        for number in range(1, count + 1):
            episode = Episode(
                anime_id=anime.id,
                number=number,
                air_at=NOW - timedelta(weeks=count - number),
                state=EpisodeState.READY if number in ready else EpisodeState.WANTED,
            )
            session.add(episode)
            await session.flush()
            ids[number] = episode.id
            if number in ready:
                session.add(
                    Rendition(
                        episode_id=episode.id,
                        dir=f"/data/renditions/{episode.id}",
                        playlist_path=f"/data/renditions/{episode.id}/index.m3u8",
                        duration=duration,
                        ready_at=NOW,
                    )
                )
        await session.commit()
        return anime.id, ids


async def set_progress(
    factory: SessionFactory,
    user: User,
    episode_id: int,
    *,
    position_s: float,
    duration_s: float | None = DURATION,
    completed: bool = False,
) -> None:
    """Write a ``watch_progress`` row directly.

    Directly rather than through the endpoint because several of the resume
    cases are states the endpoint would never produce — a row at 95.5 % that is
    *not* completed exists only to isolate the ceiling of FR-S2 from the rule
    of FR-S4.
    """
    async with factory() as session:
        session.add(
            WatchProgress(
                user_id=user.id,
                episode_id=episode_id,
                position_s=position_s,
                duration_s=duration_s,
                completed=completed,
                completed_at=NOW if completed else None,
            )
        )
        await session.commit()


async def follow(
    factory: SessionFactory,
    user: User,
    anime_id: int,
    status: ListStatus = ListStatus.WATCHING,
    *,
    progress: int = 0,
) -> None:
    async with factory() as session:
        session.add(ListEntry(user_id=user.id, anime_id=anime_id, status=status, progress=progress))
        await session.commit()


async def entry_of(factory: SessionFactory, user: User, anime_id: int) -> ListEntry | None:
    async with factory() as session:
        return await session.get(ListEntry, (user.id, anime_id))


async def progress_of(factory: SessionFactory, user: User, episode_id: int) -> WatchProgress | None:
    async with factory() as session:
        return await session.get(WatchProgress, (user.id, episode_id))


async def wants_jobs(factory: SessionFactory) -> list[Job]:
    async with factory() as session:
        rows = await session.scalars(select(Job).where(Job.type == COMPUTE_WANTS))
        return list(rows.all())


async def progress_rows(
    factory: SessionFactory, user: User, episode_id: int
) -> list[WatchProgress]:
    """Every ``watch_progress`` row for one (user, episode), not just the first.

    The primary key makes "more than one" impossible, which is the point: this
    is here so the concurrency test asserts on rows rather than on the schema's
    promise about them.
    """
    async with factory() as session:
        rows = await session.scalars(
            select(WatchProgress).where(
                WatchProgress.user_id == user.id,
                WatchProgress.episode_id == episode_id,
            )
        )
        return list(rows.all())


# --- /play --------------------------------------------------------------------


async def test_an_episode_that_is_not_ready_cannot_be_played(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=950001)

    response = await client.get(f"/api/episodes/{ids[3]}/play")

    assert response.status_code == 404


async def test_an_unknown_episode_cannot_be_played(client: AsyncClient) -> None:
    assert (await client.get("/api/episodes/999999/play")).status_code == 404


async def test_play_carries_the_playlist_url_the_duration_and_the_neighbours(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    """One call, and the client knows what to load and what comes next (FR-S5)."""
    _anime_id, ids = await add_show(api_factory, anilist_id=950002, ready=(11, 12))

    response = await client.get(f"/api/episodes/{ids[11]}/play")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["playlist_url"] == f"/media/{ids[11]}/index.m3u8"
    assert body["duration"] == pytest.approx(DURATION)
    assert body["resume_position"] is None
    assert body["episode"]["id"] == ids[11]
    assert body["episode"]["number"] == 11
    assert body["episode"]["watched"] is False
    assert body["anime"]["title"]["preferred"] == "Play Show"
    assert body["previous"] == {"id": ids[10], "number": 10, "state": "wanted", "ready": False}
    assert body["next"] == {"id": ids[12], "number": 12, "state": "ready", "ready": True}


async def test_play_carries_the_episode_title_and_still(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The player's card renders the same shape the show page does (M15).

    ``PlayInfo.episode`` is an ``EpisodeOut``, so the still and the title come
    with it rather than needing a second call, and the show's key art rides
    along on the ``AnimeSummary``.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950009, ready=(11,))
    async with api_factory() as session:
        episode = await session.get(Episode, ids[11])
        assert episode is not None
        episode.title = "The Land Where Souls Rest"
        episode.still_url = "https://img.test/e11.jpg"
        anime = await session.get(Anime, anime_id)
        assert anime is not None
        anime.cover_large_url = "https://img.test/cover-xl.jpg"
        await session.commit()

    body = (await client.get(f"/api/episodes/{ids[11]}/play")).json()

    assert body["episode"]["title"] == "The Land Where Souls Rest"
    assert body["episode"]["still_url"] == "https://img.test/e11.jpg"
    assert body["anime"]["cover_large_url"] == "https://img.test/cover-xl.jpg"


async def test_play_of_an_episode_with_no_artwork_sends_nulls(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    """A row that predates M15, or a MAL-sourced show: nulls, not omissions."""
    _anime_id, ids = await add_show(api_factory, anilist_id=950010, ready=(11,))

    body = (await client.get(f"/api/episodes/{ids[11]}/play")).json()

    assert body["episode"]["title"] is None
    assert body["episode"]["still_url"] is None
    assert body["anime"]["cover_large_url"] is None


async def test_the_first_and_last_episodes_have_no_neighbour_on_one_side(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=950003, count=2, ready=(1, 2))

    first = (await client.get(f"/api/episodes/{ids[1]}/play")).json()
    last = (await client.get(f"/api/episodes/{ids[2]}/play")).json()

    assert first["previous"] is None
    assert first["next"]["number"] == 2
    assert last["previous"]["number"] == 1
    assert last["next"] is None


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        (5.0, None),  # inside the first ten seconds: the beginning (FR-S2)
        (10.0, None),  # the boundary itself is not "past" it
        (15.0, 15.0),
        (700.0, 700.0),
        (DURATION * 0.955, None),  # past 95 %: the episode is over
    ],
)
async def test_the_resume_position_follows_the_ten_second_and_ninety_five_rules(
    client: AsyncClient,
    api_factory: SessionFactory,
    user: User,
    position: float,
    expected: float | None,
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=950004)
    await set_progress(api_factory, user, ids[11], position_s=position)

    body = (await client.get(f"/api/episodes/{ids[11]}/play")).json()

    if expected is None:
        assert body["resume_position"] is None
    else:
        assert body["resume_position"] == pytest.approx(expected)


async def test_a_completed_episode_resumes_where_the_rewatch_stopped(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """A rewatch left half-way is a saved position, flag or no flag (FR-S2).

    The flag says the user finished this episode once; it does not say they
    are not eleven minutes into it right now. Opening the player at zero would
    throw away the only record of where they were.
    """
    _anime_id, ids = await add_show(api_factory, anilist_id=950005)
    await set_progress(api_factory, user, ids[11], position_s=700.0, completed=True)

    body = (await client.get(f"/api/episodes/{ids[11]}/play")).json()

    assert body["resume_position"] == pytest.approx(700.0)
    # Unchanged, and the point: the mark is still on the episode.
    assert body["episode"]["watched"] is True


async def test_a_completed_episode_watched_to_the_end_resumes_nowhere(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """The 95 % ceiling is what stops a finished episode resuming, not the flag."""
    _anime_id, ids = await add_show(api_factory, anilist_id=950017)
    await set_progress(api_factory, user, ids[11], position_s=DURATION, completed=True)

    body = (await client.get(f"/api/episodes/{ids[11]}/play")).json()

    assert body["resume_position"] is None
    assert body["episode"]["watched"] is True


async def test_another_users_position_is_not_mine(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    other = await add_user(api_factory, "other.player@arc.test", "other-password")
    _anime_id, ids = await add_show(api_factory, anilist_id=950006)
    await set_progress(api_factory, other, ids[11], position_s=700.0)

    body = (await client.get(f"/api/episodes/{ids[11]}/play")).json()

    assert body["resume_position"] is None


async def test_play_needs_a_session(api_app: FastAPI, api_factory: SessionFactory) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=950007)

    async with api_transport(api_app) as anon:
        assert (await anon.get(f"/api/episodes/{ids[11]}/play")).status_code == 401


# --- /progress ----------------------------------------------------------------


async def post_progress(
    client: AsyncClient, episode_id: int, position: float, duration: float = DURATION
) -> dict[str, object]:
    response = await client.post(
        "/api/progress",
        json={"episode_id": episode_id, "position_s": position, "duration_s": duration},
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def test_a_report_upserts_the_row_without_completing_it(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=950010)

    first = await post_progress(client, ids[11], 300.0)
    second = await post_progress(client, ids[11], 600.0)

    assert first == {"completed": False, "newly_completed": False, "list_progress": None}
    assert second["completed"] is False
    row = await progress_of(api_factory, user, ids[11])
    assert row is not None
    assert row.position_s == pytest.approx(600.0)
    assert row.duration_s == pytest.approx(DURATION)
    assert row.completed is False
    assert row.completed_at is None


async def test_eighty_nine_per_cent_is_not_watched(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The threshold is 90 %, and one per cent short of it is short of it."""
    _anime_id, ids = await add_show(api_factory, anilist_id=950011)

    body = await post_progress(client, ids[11], DURATION * 0.89)

    assert body["completed"] is False
    assert body["newly_completed"] is False


async def test_ninety_per_cent_completes_advances_the_list_and_queues_a_recompute(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-S4 and architecture.md §5.5 step 2, in one request."""
    anime_id, ids = await add_show(api_factory, anilist_id=950012)
    await follow(api_factory, user, anime_id, progress=10)

    body = await post_progress(client, ids[11], DURATION * 0.90)

    assert body == {"completed": True, "newly_completed": True, "list_progress": 11}
    row = await progress_of(api_factory, user, ids[11])
    assert row is not None and row.completed is True
    assert row.completed_at == NOW
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.progress == 11
    assert entry.updated_by is UpdatedBy.ARC
    assert entry.mal_dirty is True
    assert entry.status is ListStatus.WATCHING
    assert len(await wants_jobs(api_factory)) == 1


async def test_pressing_play_activates_a_dormant_entry(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-A9 counts Play, and counts it from the first report.

    Thirty seconds in is the user asking Arc for this show; waiting for 90 %
    would mean the next episode only started downloading after they had
    finished this one.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950033)
    await follow(api_factory, user, anime_id, progress=10)

    body = await post_progress(client, ids[11], 30.0)

    assert body == {"completed": False, "newly_completed": False, "list_progress": None}
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.activated_at is not None
    # And nothing else about the row moved: this is not a list change.
    assert entry.progress == 10
    assert entry.mal_dirty is False


async def test_pressing_play_on_an_unlisted_show_creates_no_entry(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """ "Pressed play" is not "is watching this show"; FR-S4 draws that line
    at the completion, and drawing it here would put a show on somebody's list
    — and into their MyAnimeList push queue — because they opened an episode.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950034)

    await post_progress(client, ids[1], 30.0)

    assert await entry_of(api_factory, user, anime_id) is None


async def test_completing_an_episode_activates_a_dormant_entry(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-A9: watching an episode is the plainest touch there is.

    A show a MyAnimeList import brought in starts fetching the moment its owner
    finishes an episode of it, with no button pressed.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950030)
    await follow(api_factory, user, anime_id, progress=10)
    assert (await entry_of(api_factory, user, anime_id)).activated_at is None

    await post_progress(client, ids[11], DURATION * 0.90)

    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.activated_at is not None


async def test_a_rewatch_activates_the_entry_even_though_nothing_advances(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """The stamp is about the user being here, not about the number moving."""
    anime_id, ids = await add_show(api_factory, anilist_id=950031)
    await follow(api_factory, user, anime_id, progress=11)

    body = await post_progress(client, ids[2], DURATION * 0.95)

    assert body["list_progress"] == 11, "never downwards (FR-M4)"
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.activated_at is not None


async def test_the_entry_a_completion_creates_is_activated(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-S4's auto-create is a user choice, so it is activated on the spot."""
    anime_id, ids = await add_show(api_factory, anilist_id=950032)

    await post_progress(client, ids[1], DURATION * 0.95)

    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.status is ListStatus.WATCHING
    assert entry.activated_at is not None


async def test_a_second_report_past_the_threshold_completes_nothing_new(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """``newly_completed`` is true exactly once, and the list moves once."""
    anime_id, ids = await add_show(api_factory, anilist_id=950013)
    await follow(api_factory, user, anime_id, progress=10)
    await post_progress(client, ids[11], DURATION * 0.90)

    body = await post_progress(client, ids[11], DURATION * 0.92)

    assert body == {"completed": True, "newly_completed": False, "list_progress": None}
    # One reconciliation for the burst, not one per report: the enqueue is
    # deduplicated on the job type, and the second report never reached it.
    assert len(await wants_jobs(api_factory)) == 1


async def test_scrubbing_back_after_finishing_does_not_un_watch_it(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """Completion is sticky (FR-S4); the position still follows the player."""
    anime_id, ids = await add_show(api_factory, anilist_id=950014)
    await follow(api_factory, user, anime_id, progress=10)
    await post_progress(client, ids[11], DURATION * 0.95)

    body = await post_progress(client, ids[11], 42.0)

    assert body["completed"] is True
    assert body["newly_completed"] is False
    row = await progress_of(api_factory, user, ids[11])
    assert row is not None
    assert row.position_s == pytest.approx(42.0)
    assert row.completed is True
    assert row.completed_at == NOW


async def test_a_rewatch_never_lowers_list_progress(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-M4: an automatic event never decreases what MAL will be told."""
    anime_id, ids = await add_show(api_factory, anilist_id=950015, ready=(3, 11))
    await follow(api_factory, user, anime_id, progress=11)

    body = await post_progress(client, ids[3], DURATION * 0.99)

    assert body == {"completed": True, "newly_completed": True, "list_progress": 11}
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.progress == 11
    assert entry.mal_dirty is False


async def test_finishing_a_show_off_your_list_adds_it_as_watching(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-W3's spirit: watching an episode is the statement that you are."""
    anime_id, ids = await add_show(api_factory, anilist_id=950016)

    body = await post_progress(client, ids[11], DURATION * 0.91)

    assert body["list_progress"] == 11
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.status is ListStatus.WATCHING
    assert entry.progress == 11
    assert entry.mal_dirty is True


async def test_finishing_the_last_episode_of_an_airing_show_leaves_the_status_alone(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-W5's first edge: a ``RELEASING`` show is never auto-completed.

    Its episode count is a projection — episode 13 of a "12-episode" season is
    an ordinary occurrence — so completing it would be a status Arc has to take
    back next Friday.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950017, count=2, ready=(2,))
    await follow(api_factory, user, anime_id, progress=1)

    body = await post_progress(client, ids[2], DURATION * 0.99)

    assert body["list_progress"] == 2
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.status is ListStatus.WATCHING


async def test_a_beacon_sends_text_plain_and_is_still_accepted(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """``navigator.sendBeacon`` cannot send JSON without a preflight (FR-S3)."""
    anime_id, ids = await add_show(api_factory, anilist_id=950018)
    await follow(api_factory, user, anime_id, progress=10)

    response = await client.post(
        "/api/progress",
        content=(
            f'{{"episode_id": {ids[11]}, "position_s": {DURATION * 0.95},'
            f' "duration_s": {DURATION}}}'
        ),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["newly_completed"] is True


async def test_a_report_without_an_origin_is_refused(
    api_app: FastAPI, api_factory: SessionFactory
) -> None:
    """The beacon relaxation is about the content type, never about CSRF."""
    _anime_id, ids = await add_show(api_factory, anilist_id=950019)
    await add_user(api_factory, "originless@arc.test", "originless-password")

    async with api_transport(api_app, origin=None) as http:
        await http.post(
            "/api/auth/login",
            json={"email": "originless@arc.test", "password": "originless-password"},
            headers={"Origin": "http://localhost:5173"},
        )
        response = await http.post(
            "/api/progress",
            json={"episode_id": ids[11], "position_s": 1.0, "duration_s": DURATION},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "origin not allowed"}


@pytest.mark.parametrize(
    "body",
    [
        {"episode_id": 1},
        {"episode_id": 1, "position_s": -1.0, "duration_s": 10.0},
        {"episode_id": 1, "position_s": 1.0, "duration_s": 0.0},
        {"episode_id": 1, "position_s": 1.0, "duration_s": 10.0, "extra": True},
        {"episode_id": "eleven", "position_s": 1.0, "duration_s": 10.0},
    ],
)
async def test_a_body_that_is_not_a_progress_report_is_422(
    client: AsyncClient, body: dict[str, object]
) -> None:
    assert (await client.post("/api/progress", json=body)).status_code == 422


async def test_a_body_that_is_not_json_at_all_is_422(client: AsyncClient) -> None:
    response = await client.post(
        "/api/progress", content="not json", headers={"Content-Type": "text/plain"}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "literal",
    [
        "NaN",  # Python's json accepts all three of these by default…
        "Infinity",
        "-Infinity",
        "1e400",  # …and this one is not a literal at all: it overflows to inf
        "-1e400",
    ],
)
@pytest.mark.parametrize("field", ["position_s", "duration_s"])
async def test_a_report_that_is_not_a_real_number_is_422(
    client: AsyncClient, api_factory: SessionFactory, field: str, literal: str
) -> None:
    """Neither infinity nor a NaN is JSON, and neither survives FR-S4's division.

    Written as raw text rather than through ``json=`` because ``json.dumps``
    produces exactly these literals for ``float('nan')`` — the point is what a
    caller can *send*, and a beacon sends bytes.
    """
    _anime_id, ids = await add_show(api_factory, anilist_id=950021)
    values = {"episode_id": str(ids[11]), "position_s": "1.0", "duration_s": f"{DURATION}"}
    values[field] = literal
    body = ", ".join(f'"{key}": {value}' for key, value in values.items())

    response = await client.post(
        "/api/progress", content=f"{{{body}}}", headers={"Content-Type": "text/plain"}
    )

    assert response.status_code == 422, response.text


async def test_a_nan_anywhere_in_the_body_is_refused_before_the_fields_are_read(
    client: AsyncClient,
) -> None:
    """``parse_constant`` fires on the literal, wherever in the body it sits."""
    response = await client.post(
        "/api/progress",
        content='{"episode_id": NaN, "position_s": 1.0, "duration_s": 10.0}',
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 422


async def test_an_episode_id_too_large_for_bigint_is_422_in_the_body_too(
    client: AsyncClient,
) -> None:
    """The body carries an id as surely as a path does (:data:`arc.api.deps.MAX_ID`)."""
    response = await client.post(
        "/api/progress",
        json={"episode_id": 99999999999999999999, "position_s": 1.0, "duration_s": DURATION},
    )

    assert response.status_code == 422


async def test_a_report_about_an_unknown_episode_is_404(client: AsyncClient) -> None:
    response = await client.post(
        "/api/progress",
        json={"episode_id": 999999, "position_s": 1.0, "duration_s": DURATION},
    )

    assert response.status_code == 404


#: How many players report at once in the race below. Twenty is far more than
#: a person has open and is well inside Postgres' connection budget; the number
#: only has to be large enough that a lost race would show up every run.
CONCURRENT_REPORTS = 20

#: And how many fresh episodes it is run against. Once could be luck.
CONCURRENT_ROUNDS = 3


async def test_simultaneous_reports_complete_an_episode_exactly_once(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """``newly_completed`` is once per (user, episode), including under a race.

    The shape is real: a phone and a laptop on the same account, or a beacon
    firing at ``pagehide`` while the ten-second timer fires too, put several
    completing reports on the wire at once. Everything that hangs off
    ``newly_completed`` — the next-episode prompt (FR-S5), the list advance,
    and in M9 a write to MyAnimeList — must happen once, so the flag has to be
    true for exactly one of them.

    Note the clock is pinned by the ``frozen`` fixture: every one of these
    twenty calls carries the *same* timestamp. That is deliberate. An answer
    derived from "did this call write ``completed_at``?" would call all twenty
    of them the winner; the answer has to come from the row each statement
    actually overwrote.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950022, ready=(9, 10, 11))
    await follow(api_factory, user, anime_id, progress=8)

    for number in (9, 10, 11)[:CONCURRENT_ROUNDS]:
        episode_id = ids[number]
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/progress",
                    json={
                        "episode_id": episode_id,
                        "position_s": DURATION * 0.95,
                        "duration_s": DURATION,
                    },
                )
                for _ in range(CONCURRENT_REPORTS)
            )
        )

        assert [r.status_code for r in responses] == [200] * CONCURRENT_REPORTS
        bodies = [r.json() for r in responses]
        assert all(body["completed"] is True for body in bodies), bodies
        winners = [body for body in bodies if body["newly_completed"]]
        assert len(winners) == 1, f"episode {number}: {len(winners)} winners in {bodies}"
        assert winners[0]["list_progress"] == number
        # One row, and one reconciliation for the whole burst — the enqueue is
        # deduplicated on the job type, so the count stays one across rounds.
        assert len(await progress_rows(api_factory, user, episode_id)) == 1
        assert len(await wants_jobs(api_factory)) == 1

    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 11


async def test_reporting_needs_a_session(api_app: FastAPI, api_factory: SessionFactory) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=950020)

    async with api_transport(api_app) as anon:
        response = await anon.post(
            "/api/progress",
            json={"episode_id": ids[11], "position_s": 1.0, "duration_s": DURATION},
        )

    assert response.status_code == 401


# --- /watched -----------------------------------------------------------------


async def test_marking_an_episode_watched_by_hand_advances_the_list(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-W3, treated exactly as FR-S4 is."""
    anime_id, ids = await add_show(api_factory, anilist_id=950030)
    await follow(api_factory, user, anime_id, progress=10)

    response = await client.post(f"/api/episodes/{ids[11]}/watched")

    assert response.status_code == 200, response.text
    assert response.json() == {"completed": True, "newly_completed": True, "list_progress": 11}
    row = await progress_of(api_factory, user, ids[11])
    assert row is not None
    assert row.completed is True
    # The rendition's duration, so the row is a real one and not a placeholder.
    assert row.position_s == pytest.approx(DURATION)
    assert row.duration_s == pytest.approx(DURATION)
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 11 and entry.mal_dirty is True
    assert len(await wants_jobs(api_factory)) == 1


async def test_an_episode_arc_never_prepared_can_still_be_marked_watched(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """Watched elsewhere, on a disc or a plane: the case FR-W3 exists for."""
    anime_id, ids = await add_show(api_factory, anilist_id=950031, ready=())
    await follow(api_factory, user, anime_id, progress=4)

    response = await client.post(f"/api/episodes/{ids[5]}/watched")

    assert response.status_code == 200
    assert response.json()["list_progress"] == 5
    row = await progress_of(api_factory, user, ids[5])
    assert row is not None
    assert row.completed is True
    # No rendition, so no honest duration to record.
    assert row.position_s == pytest.approx(0.0)
    assert row.duration_s == pytest.approx(0.0)


async def test_marking_an_episode_watched_twice_only_moves_things_once(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    anime_id, ids = await add_show(api_factory, anilist_id=950032)
    await follow(api_factory, user, anime_id, progress=10)
    await client.post(f"/api/episodes/{ids[11]}/watched")

    second = await client.post(f"/api/episodes/{ids[11]}/watched")

    assert second.json() == {"completed": True, "newly_completed": False, "list_progress": None}


async def test_un_marking_clears_the_flag_keeps_the_position_and_lowers_the_list(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-S4 as the owner revised it on 2026-09-13.

    Watching episode 11 raised the list to 11; taking the mark back lowers it
    to 10, because otherwise FR-W5 would keep calling the episode watched and
    the button would change nothing anybody could see. The position stays: this
    is "I had not finished it after all", not "I never opened it".
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950033)
    await follow(api_factory, user, anime_id, progress=10)
    await post_progress(client, ids[11], DURATION * 0.95)

    response = await client.delete(f"/api/episodes/{ids[11]}/watched")

    assert response.status_code == 200
    assert response.json() == {"completed": False, "newly_completed": False, "list_progress": 10}
    row = await progress_of(api_factory, user, ids[11])
    assert row is not None
    assert row.completed is False
    assert row.completed_at is None
    assert row.position_s == pytest.approx(DURATION * 0.95)
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.progress == 10
    assert entry.updated_by is UpdatedBy.ARC
    assert entry.mal_dirty is True


async def test_un_marking_an_episode_the_list_alone_vouches_for_still_lowers_it(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """The case the revision exists for: an imported list, no completion rows.

    Nine episodes watched according to MyAnimeList and nothing of Arc's own to
    clear. Under the old rule there was no way to correct that at all.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950042, ready=())
    await follow(api_factory, user, anime_id, progress=9)

    body = (await client.delete(f"/api/episodes/{ids[9]}/watched")).json()

    assert body["list_progress"] == 8
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.progress == 8
    # Nothing was fabricated on the way: there is still no row for episode 9.
    assert await progress_rows(api_factory, user, ids[9]) == []
    # The window moved back onto it, so the reconciliation is queued (FR-T3).
    assert len(await wants_jobs(api_factory)) == 1


@pytest.mark.parametrize(("progress", "number", "expected"), [(9, 4, 9), (9, 11, 9)])
async def test_un_marking_anything_but_the_latest_leaves_the_list_alone(
    client: AsyncClient,
    api_factory: SessionFactory,
    user: User,
    progress: int,
    number: int,
    expected: int,
) -> None:
    """One episode from the top, and only from the top.

    Below it, taking back episode 4 of a list that says 9 would have to claim
    something about 5…9 the viewer never said; above it there is nothing to
    lower. Both still clear whatever row Arc holds.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950043 + number, ready=())
    await follow(api_factory, user, anime_id, progress=progress)
    await set_progress(
        api_factory, user, ids[number], position_s=0.0, duration_s=0.0, completed=True
    )

    body = (await client.delete(f"/api/episodes/{ids[number]}/watched")).json()

    assert body["list_progress"] is None
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.progress == expected
    assert entry.mal_dirty is False
    row = await progress_of(api_factory, user, ids[number])
    assert row is not None and row.completed is False


async def test_un_marking_the_last_episode_leaves_a_completed_show_completed(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-W5's auto-complete is not undone by FR-S4's rollback (owner).

    The progress goes back to 1; the status stays ``completed``, because
    "completed" is a word the viewer owns (FR-W2) and Arc taking it off a show
    they still consider finished would be a claim nobody made.
    """
    anime_id, ids = await add_show(
        api_factory, anilist_id=950050, count=2, ready=(2,), status="FINISHED"
    )
    await follow(api_factory, user, anime_id, progress=1)
    await post_progress(client, ids[2], DURATION * 0.99)
    assert (await entry_of(api_factory, user, anime_id)) is not None

    body = (await client.delete(f"/api/episodes/{ids[2]}/watched")).json()

    assert body["list_progress"] == 1
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.progress == 1
    assert entry.status is ListStatus.COMPLETED


async def test_un_marking_on_a_show_off_your_list_creates_nothing(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """The un-mark never *adds* a list entry: there is no number to lower."""
    anime_id, ids = await add_show(api_factory, anilist_id=950051, ready=())

    body = (await client.delete(f"/api/episodes/{ids[4]}/watched")).json()

    assert body["list_progress"] is None
    assert await entry_of(api_factory, user, anime_id) is None


async def test_un_marking_something_never_watched_is_still_a_200(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=950034)

    response = await client.delete(f"/api/episodes/{ids[11]}/watched")

    assert response.status_code == 200
    assert response.json()["completed"] is False


async def test_marking_an_unknown_episode_is_404(client: AsyncClient) -> None:
    assert (await client.post("/api/episodes/999999/watched")).status_code == 404
    assert (await client.delete("/api/episodes/999999/watched")).status_code == 404


# --- Ids that no row can have -------------------------------------------------


#: One more than ``bigint`` holds, by two digits. Postgres refuses to bind it,
#: which used to surface as a 500 for what is plainly a malformed request.
TOO_LARGE_FOR_BIGINT = 99999999999999999999


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"/api/episodes/{TOO_LARGE_FOR_BIGINT}/play"),
        ("POST", f"/api/episodes/{TOO_LARGE_FOR_BIGINT}/watched"),
        ("DELETE", f"/api/episodes/{TOO_LARGE_FOR_BIGINT}/watched"),
        ("GET", f"/api/anime/{TOO_LARGE_FOR_BIGINT}"),
    ],
)
async def test_an_id_too_large_for_bigint_is_a_422_not_a_500(
    client: AsyncClient, method: str, path: str
) -> None:
    """Bounded at the edge (:data:`arc.api.deps.MAX_ID`), before any query."""
    response = await client.request(method, path)

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("bad", ["0", "-1"])
async def test_an_id_below_one_is_a_422_as_well(client: AsyncClient, bad: str) -> None:
    """Sequences start at 1; nothing hands out 0 or a negative id."""
    assert (await client.get(f"/api/episodes/{bad}/play")).status_code == 422


async def test_the_show_page_shows_the_tick_after_completion(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """``EpisodeOut.watched`` is per user, and FR-W5 is both of its halves.

    Marking 11 watched on a list that said 10 raises the progress to 11, so
    everything up to it is watched too — episode 10 by the list's word and
    episode 11 by Arc's own completion row. The episode above stays clear.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=950035)
    await follow(api_factory, user, anime_id, progress=10)
    await client.post(f"/api/episodes/{ids[11]}/watched")

    detail = (await client.get(f"/api/anime/{anime_id}")).json()

    rows = {row["number"]: row for row in detail["episodes"]}
    assert rows[11]["watched"] is True
    assert rows[11]["watched_source"] == "arc"
    assert rows[10]["watched"] is True
    assert rows[10]["watched_source"] == "progress"
    assert rows[12]["watched"] is False
    assert rows[12]["watched_source"] is None
    # And no rows were fabricated for 1..10: progress already says it.
    assert await progress_rows(api_factory, user, ids[10]) == []


# --- What "watched" means (FR-W5) --------------------------------------------


@pytest.mark.parametrize(
    ("progress", "completed", "number", "expected"),
    [
        (0, False, 4, None),  # neither
        (0, True, 4, "arc"),  # a completion under an untouched list
        (3, True, 4, "arc"),  # above the line, on Arc's own row
        (3, False, 4, None),  # above the line with nothing to say so
        (4, False, 4, "arc"),  # *at* the line: the un-mark lowers it to 3
        (4, True, 4, "arc"),  # …the completion row changes nothing here
        (7, False, 4, "progress"),  # under the line: watched, no undo
        (7, True, 4, "progress"),  # …and still no undo, row or no row
    ],
)
async def test_the_watched_matrix(
    client: AsyncClient,
    api_factory: SessionFactory,
    user: User,
    progress: int,
    completed: bool,
    number: int,
    expected: str | None,
) -> None:
    """The owner's rule of 2026-09-13 (revised the same day), as a table.

    ``arc`` is the value that means "actionable": at or above the list's
    progress, where the un-mark has either a row to clear or a number to lower.
    Under the progress it is ``progress`` whatever ``watch_progress`` holds,
    because the un-mark moves the line by one from the top and nothing there
    would change.
    """
    anime_id, ids = await add_show(api_factory, anilist_id=960000 + progress * 10 + number)
    if progress or completed:
        await follow(api_factory, user, anime_id, progress=progress)
    if completed:
        await set_progress(api_factory, user, ids[number], position_s=DURATION, completed=True)

    detail = (await client.get(f"/api/anime/{anime_id}")).json()

    row = next(item for item in detail["episodes"] if item["number"] == number)
    assert row["watched_source"] == expected
    assert row["watched"] is (expected is not None)


async def test_the_player_reads_the_same_definition(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-W5 in one place: ``/play`` must not have its own idea of watched."""
    anime_id, ids = await add_show(api_factory, anilist_id=950037)
    # Twelve, so episode 11 is *under* the line and reads as the half with no
    # undo — the case the player has to render as a word rather than a toggle.
    await follow(api_factory, user, anime_id, progress=12)

    body = (await client.get(f"/api/episodes/{ids[11]}/play")).json()

    assert body["episode"]["watched"] is True
    assert body["episode"]["watched_source"] == "progress"


async def test_marking_an_episode_below_progress_never_lowers_it(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-M4 through FR-W3's door: the manual mark is FR-S4's path exactly."""
    anime_id, ids = await add_show(api_factory, anilist_id=950038, ready=(3, 11))
    await follow(api_factory, user, anime_id, progress=7)

    body = (await client.post(f"/api/episodes/{ids[3]}/watched")).json()

    assert body == {"completed": True, "newly_completed": True, "list_progress": 7}
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.progress == 7
    # Nothing moved, so nothing is owed to MyAnimeList.
    assert entry.mal_dirty is False


# --- Auto-complete (FR-W5) ----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "episode_count", "before", "expected"),
    [
        # The whole point: a finished show whose last episode just landed.
        ("FINISHED", 2, ListStatus.WATCHING, ListStatus.COMPLETED),
        # Still airing: the count is a projection, so it is not an end.
        ("RELEASING", 2, ListStatus.WATCHING, ListStatus.WATCHING),
        # No count at all: there is no end to reach.
        ("FINISHED", None, ListStatus.WATCHING, ListStatus.WATCHING),
        # Already completed: nothing to claim, and nothing changes.
        ("FINISHED", 2, ListStatus.COMPLETED, ListStatus.COMPLETED),
        # On hold, and finished anyway — the progress is the statement.
        ("FINISHED", 2, ListStatus.ON_HOLD, ListStatus.COMPLETED),
        # And dropped (owner, 2026-09-13): watching the last episode of a show
        # you had dropped is the clearest thing anybody says about an entry,
        # and "dropped at 12/12" would be Arc keeping a state they moved past.
        ("FINISHED", 2, ListStatus.DROPPED, ListStatus.COMPLETED),
    ],
)
async def test_the_auto_complete_matrix(
    client: AsyncClient,
    api_factory: SessionFactory,
    user: User,
    status: str,
    episode_count: int | None,
    before: ListStatus,
    expected: ListStatus,
) -> None:
    """FR-W5's edges, decided by the owner on 2026-09-13."""
    anime_id, ids = await add_show(
        api_factory,
        anilist_id=961000 + abs(hash((status, episode_count, before.value))) % 9000,
        count=2,
        ready=(2,),
        status=status,
        episode_count=episode_count,
    )
    await follow(api_factory, user, anime_id, status=before, progress=1)

    await post_progress(client, ids[2], DURATION * 0.99)

    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.status is expected
    assert entry.progress == 2


async def test_a_partial_show_is_not_completed(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """Nine of twelve is not twelve, whatever the show's status says."""
    anime_id, ids = await add_show(api_factory, anilist_id=950039, ready=(9,), status="FINISHED")
    await follow(api_factory, user, anime_id, progress=8)

    await post_progress(client, ids[9], DURATION * 0.99)

    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.status is ListStatus.WATCHING


async def test_a_rewatch_of_the_last_episode_completes_nothing_new(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """Already at 2/2 and already completed: the mark must claim nothing."""
    anime_id, ids = await add_show(
        api_factory, anilist_id=950040, count=2, ready=(2,), status="FINISHED"
    )
    await follow(api_factory, user, anime_id, status=ListStatus.COMPLETED, progress=2)

    await client.post(f"/api/episodes/{ids[2]}/watched")

    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.status is ListStatus.COMPLETED
    assert entry.progress == 2
    # Nothing advanced, so nothing is dirty and nothing is owed to MAL.
    assert entry.mal_dirty is False


async def test_marking_the_last_episode_of_a_finished_show_completes_it(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """FR-W3 and FR-W5 together: the manual mark completes a show too."""
    anime_id, ids = await add_show(
        api_factory, anilist_id=950041, count=2, ready=(), status="FINISHED"
    )
    await follow(api_factory, user, anime_id, progress=1)

    await client.post(f"/api/episodes/{ids[2]}/watched")

    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None
    assert entry.status is ListStatus.COMPLETED
    assert entry.progress == 2
    assert entry.updated_by is UpdatedBy.ARC
    assert entry.mal_dirty is True


async def test_another_users_completion_is_not_on_my_show_page(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    other = await add_user(api_factory, "someone.else@arc.test", "else-password")
    anime_id, ids = await add_show(api_factory, anilist_id=950036)
    await set_progress(api_factory, other, ids[11], position_s=DURATION, completed=True)

    detail = (await client.get(f"/api/anime/{anime_id}")).json()

    assert all(row["watched"] is False for row in detail["episodes"])
