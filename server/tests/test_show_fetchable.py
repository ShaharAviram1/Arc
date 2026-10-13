"""``fetchable`` on the show page's episode rows (FR-A14, M20 T5).

The show page offers play on a not-ready episode exactly when ``POST
/api/episodes/{id}/demand`` would accept it: the caller's window covers it
(:func:`arc.services.acquisition.wants.coverable`). A ready row is never
fetchable, and a show the caller does not follow offers nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from arc.db import SessionFactory
from arc.models import Episode, EpisodeState, Rendition, User
from arc.services.catalog.airing import FINISHED
from tests.acquisition_helpers import make_anime, make_entry, make_episodes
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "show-fetchable@arc.test"
USER_PASSWORD = "show-fetchable-password"


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


async def show(
    factory: SessionFactory, user_id: int, *, anilist_id: int, follow: bool = True
) -> tuple[int, list[int]]:
    """A finished twelve-episode show, followed at progress 4 unless ``follow`` is false."""
    async with factory() as session:
        anime = await make_anime(session, anilist_id=anilist_id, status=FINISHED, episodes=12)
        rows = await make_episodes(session, anime, 12, aired_through=12)
        if follow:
            owner = await session.get(User, user_id)
            assert owner is not None
            await make_entry(session, owner, anime, progress=4)
        await session.commit()
        return anime.id, [row.id for row in rows]


def fetchable(payload: dict[str, Any]) -> dict[int, bool]:
    return {row["id"]: row["fetchable"] for row in payload["episodes"]}


async def test_the_window_episode_is_fetchable_and_one_beyond_is_not(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id, ids = await show(api_factory, user.id, anilist_id=976101)

    response = await client.get(f"/api/anime/{anime_id}")

    assert response.status_code == 200, response.text
    flags = fetchable(response.json())
    assert flags[ids[4]] is True
    assert flags[ids[8]] is False
    assert flags[ids[0]] is False
    # The flag and the route agree.
    assert (await client.post(f"/api/episodes/{ids[8]}/demand")).status_code == 409


async def test_a_ready_episode_is_never_fetchable(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id, ids = await show(api_factory, user.id, anilist_id=976102)
    async with api_factory() as session:
        episode = await session.get(Episode, ids[4])
        assert episode is not None
        episode.state = EpisodeState.READY
        session.add(
            Rendition(
                episode_id=ids[4],
                dir=f"/data/renditions/{ids[4]}",
                playlist_path=f"/data/renditions/{ids[4]}/index.m3u8",
                duration=1420.0,
                ready_at=datetime.now(UTC),
            )
        )
        await session.commit()

    flags = fetchable((await client.get(f"/api/anime/{anime_id}")).json())

    assert flags[ids[4]] is False


async def test_coverable_is_not_planned_when_every_row_is_ready(
    client: AsyncClient,
    user: User,
    api_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def spy(session: object, user_id: int, *, now: datetime | None = None) -> frozenset[int]:
        calls.append(user_id)
        return frozenset()

    monkeypatch.setattr("arc.api.anime.coverable", spy)
    anime_id, ids = await show(api_factory, user.id, anilist_id=976104)

    # A not-ready row on a followed show: the plan runs (the spy is wired).
    assert (await client.get(f"/api/anime/{anime_id}")).status_code == 200
    assert calls == [user.id]

    async with api_factory() as session:
        for episode_id in ids:
            episode = await session.get(Episode, episode_id)
            assert episode is not None
            episode.state = EpisodeState.READY
        await session.commit()
    calls.clear()

    response = await client.get(f"/api/anime/{anime_id}")

    assert response.status_code == 200
    assert calls == []
    assert not any(fetchable(response.json()).values())


async def test_a_show_the_caller_does_not_follow_offers_nothing(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id, _ = await show(api_factory, user.id, anilist_id=976103, follow=False)

    flags = fetchable((await client.get(f"/api/anime/{anime_id}")).json())

    assert not any(flags.values())
