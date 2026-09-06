"""The acquisition API: the show page's fields and the admin controls (FR-A7).

``EpisodeOut`` is what the client renders an acquisition badge from, so the
tests assert on the JSON rather than on the schema object: the shape is the
contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.db import SessionFactory
from arc.models import EpisodeState, Job, Torrent, UserRole, Want
from arc.services.acquisition.names import COMPUTE_WANTS, POLL_QBIT, SEARCH_RELEASE
from tests.acquisition_helpers import make_anime, make_entry, make_episodes, make_user
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "member@arc.test"
USER_PASSWORD = "memberpassword"


async def show_with_torrent(factory: SessionFactory) -> tuple[int, int, int]:
    """A show whose episode 7 is downloading at 42 %. Returns ids."""
    async with factory() as session:
        anime = await make_anime(session, anilist_id=963001)
        episodes = await make_episodes(session, anime, 12, aired_through=7)
        episode = episodes[6]
        episode.state = EpisodeState.DOWNLOADING
        session.add(
            Torrent(
                episode_id=episode.id,
                info_hash="a" * 40,
                title="[SubsPlease] Sousou no Frieren - 07 (1080p) [24356E19].mkv",
                group="SubsPlease",
                resolution="1080p",
                seeders=155,
                trusted=False,
                progress=0.42,
                qbit_state="downloading",
            )
        )
        await session.commit()
        return anime.id, episode.id, episodes[7].id


# --- EpisodeOut -------------------------------------------------------------


async def test_a_downloading_episode_reports_its_progress_and_release(
    api_app, api_factory: SessionFactory
) -> None:
    anime_id, episode_id, _ = await show_with_torrent(api_factory)
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    episode = next(row for row in body["episodes"] if row["id"] == episode_id)
    assert episode["state"] == "downloading"
    assert episode["download_progress"] == pytest.approx(0.42)
    assert episode["release"] == {
        "group": "SubsPlease",
        "resolution": "1080p",
        "title": "[SubsPlease] Sousou no Frieren - 07 (1080p) [24356E19].mkv",
        "seeders": 155,
    }
    assert episode["unavailable_reason"] is None


async def test_an_episode_with_no_torrent_carries_nulls(
    api_app, api_factory: SessionFactory
) -> None:
    anime_id, _, other_id = await show_with_torrent(api_factory)
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    episode = next(row for row in body["episodes"] if row["id"] == other_id)
    assert episode["download_progress"] is None
    assert episode["release"] is None
    assert episode["unavailable_reason"] is None


async def test_progress_is_not_reported_once_the_episode_is_past_downloading(
    api_app, api_factory: SessionFactory
) -> None:
    """A permanent 100 % on a ready episode reads as "still working"."""
    anime_id, episode_id, _ = await show_with_torrent(api_factory)
    async with api_factory() as session:
        from arc.models import Episode

        episode = await session.get(Episode, episode_id)
        assert episode is not None
        episode.state = EpisodeState.MATCHED
        await session.commit()
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    episode_json = next(row for row in body["episodes"] if row["id"] == episode_id)
    assert episode_json["download_progress"] is None
    assert episode_json["release"] is not None, "the release survives the download"


async def test_an_unavailable_episode_carries_its_reason(
    api_app, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=963002)
        episodes = await make_episodes(session, anime, 4, aired_through=4)
        episodes[2].state = EpisodeState.UNAVAILABLE
        episodes[2].unavailable_reason = "no acceptable release found"
        await session.commit()
        anime_id, episode_id = anime.id, episodes[2].id
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    episode = next(row for row in body["episodes"] if row["id"] == episode_id)
    assert episode["state"] == "unavailable"
    assert episode["unavailable_reason"] == "no acceptable release found"


# --- Admin endpoints --------------------------------------------------------


async def test_a_non_admin_cannot_force_a_search(api_app, api_factory: SessionFactory) -> None:
    _, episode_id, _ = await show_with_torrent(api_factory)
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD, role=UserRole.USER)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        response = await client.post(f"/api/episodes/{episode_id}/search")

    assert response.status_code == 403


async def test_an_anonymous_caller_gets_401(api_client: AsyncClient) -> None:
    assert (await api_client.post("/api/acquisition/compute-wants")).status_code == 401
    assert (await api_client.get("/api/acquisition/wants")).status_code == 401


async def test_forcing_a_search_answers_202_with_the_job(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    _, episode_id, _ = await show_with_torrent(api_factory)

    response = await admin_client.post(f"/api/episodes/{episode_id}/search")

    assert response.status_code == 202
    body = response.json()
    assert body["type"] == SEARCH_RELEASE
    assert body["payload"]["episode_id"] == episode_id
    assert body["status"] == "pending"


async def test_forcing_the_same_search_twice_queues_one_job(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    _, episode_id, _ = await show_with_torrent(api_factory)

    first = await admin_client.post(f"/api/episodes/{episode_id}/search")
    second = await admin_client.post(f"/api/episodes/{episode_id}/search")

    assert first.json()["id"] == second.json()["id"]
    async with api_factory() as session:
        rows = await session.scalars(select(Job).where(Job.type == SEARCH_RELEASE))
        assert len(list(rows.all())) == 1


async def test_compute_wants_answers_202_and_deduplicates(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    first = await admin_client.post("/api/acquisition/compute-wants")
    second = await admin_client.post("/api/acquisition/compute-wants")

    assert first.status_code == 202
    assert first.json()["type"] == COMPUTE_WANTS
    assert first.json()["id"] == second.json()["id"]


async def test_polling_can_be_forced(admin_client: AsyncClient) -> None:
    response = await admin_client.post("/api/acquisition/poll")

    assert response.status_code == 202
    assert response.json()["type"] == POLL_QBIT


async def test_the_wants_list_shows_the_episode_and_its_state(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=963003, romaji="Mushishi", english=None)
        episodes = await make_episodes(session, anime, 6, aired_through=6)
        user = await make_user(session, "wanter@arc.test")
        await make_entry(session, user, anime, progress=2)
        episodes[2].state = EpisodeState.SEARCHING
        session.add(Want(user_id=user.id, episode_id=episodes[2].id))
        await session.commit()
        expected_episode = episodes[2].id
        expected_user = user.id

    response = await admin_client.get("/api/acquisition/wants")

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0] == {
        "user_id": expected_user,
        "user_email": "wanter@arc.test",
        "episode_id": expected_episode,
        "episode_number": 3,
        "anime_id": anime.id,
        "anime_title": "Mushishi",
        "state": "searching",
        "unavailable_reason": None,
    }


async def test_a_dropped_want_is_not_listed(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=963004)
        episodes = await make_episodes(session, anime, 4, aired_through=4)
        user = await make_user(session, "dropped@arc.test")
        session.add(
            Want(
                user_id=user.id,
                episode_id=episodes[0].id,
                dropped_at=datetime.now(UTC),
                drop_reason="unwatched",
            )
        )
        await session.commit()

    assert (await admin_client.get("/api/acquisition/wants")).json() == []


# --- The list hook ----------------------------------------------------------


async def test_adding_a_show_to_the_list_queues_a_recompute(
    db_session: AsyncSession,
) -> None:
    """``set_list_entry`` is what the ``PUT /api/list/{id}`` router calls."""
    from arc.models import ListStatus
    from arc.services.catalog.lists import set_list_entry

    anime = await make_anime(db_session, anilist_id=963005)
    user = await make_user(db_session, "hooked@arc.test")

    class Stub:
        """A catalogue that is up but is never asked anything.

        ``ensure_anime`` refreshes a row older than a day and skips a MAL-filled
        one while AniList is healthy; a row written just now by AniList is
        neither, so the fetch never happens and the stub only has to answer
        ``healthy``.
        """

        def healthy(self, source: str) -> bool:
            return True

        async def media(self, anime_id: int) -> None:  # pragma: no cover
            raise AssertionError("the cached row is fresh enough")

    anime.detail_source = "anilist"
    anime.refreshed_at = datetime.now(UTC)
    await db_session.flush()

    await set_list_entry(
        db_session,
        Stub(),  # type: ignore[arg-type]
        user_id=user.id,
        anime_id=anime.id,
        status=ListStatus.WATCHING,
    )

    rows = await db_session.scalars(select(Job).where(Job.type == COMPUTE_WANTS))
    assert len(list(rows.all())) == 1


async def test_removing_a_show_from_the_list_queues_a_recompute(
    db_session: AsyncSession,
) -> None:
    from arc.services.catalog.lists import remove_list_entry

    anime = await make_anime(db_session, anilist_id=963006)
    user = await make_user(db_session, "unhooked@arc.test")
    await make_entry(db_session, user, anime)

    assert await remove_list_entry(db_session, user_id=user.id, anime_id=anime.id) is True

    rows = await db_session.scalars(select(Job).where(Job.type == COMPUTE_WANTS))
    assert len(list(rows.all())) == 1
