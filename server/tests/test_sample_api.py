"""The "try episode 1" routes after M21 retired FR-A8 (a sample is a click on episode 1).

``POST /api/anime/{id}/sample`` answers 410 and writes nothing. ``DELETE`` stays
while the sample rows made before M21 are live, so its tests build such a row
by hand. Asserted on the JSON rather than on the schema objects: the shape is
the contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.api.anime import SAMPLE_NOT_FOUND, SAMPLE_RETIRED
from arc.db import SessionFactory
from arc.models import Episode, EpisodeState, Job, User, Want
from arc.services.acquisition import compute_wants
from arc.services.acquisition.demand import demand
from arc.services.acquisition.names import QBIT_CANCEL
from arc.services.acquisition.samples import SAMPLE_CANCELLED_REASON
from tests.acquisition_helpers import make_anime, make_episodes
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "sampler@arc.test"
USER_PASSWORD = "samplerpassword"

OTHER_EMAIL = "other-sampler@arc.test"
OTHER_PASSWORD = "otherpassword"


@pytest.fixture
async def user_client(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    """A signed-in ordinary account."""
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    async with api_transport(api_app) as client:
        yield await login(client, USER_EMAIL, USER_PASSWORD)


async def a_show(factory: SessionFactory, *, anilist_id: int, aired_through: int = 12) -> int:
    """A twelve-episode show with the first ``aired_through`` of them aired."""
    async with factory() as session:
        anime = await make_anime(session, anilist_id=anilist_id)
        await make_episodes(session, anime, 12, aired_through=aired_through)
        await session.commit()
        return anime.id


async def user_id_of(factory: SessionFactory, email: str) -> int:
    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        return user.id


async def leftover_sample(
    factory: SessionFactory, user_id: int, anime_id: int, *, state: EpisodeState
) -> int:
    """A live sample want on episode 1, as one made before M21 left it; its episode id."""
    async with factory() as session:
        first = await session.scalar(
            select(Episode).where(Episode.anime_id == anime_id, Episode.number == 1)
        )
        assert first is not None
        first.state = state
        session.add(Want(user_id=user_id, episode_id=first.id, sample=True))
        await session.commit()
        return first.id


async def test_an_anonymous_caller_cannot_sample_or_cancel(api_client: AsyncClient) -> None:
    assert (await api_client.post("/api/anime/1/sample")).status_code == 401
    assert (await api_client.delete("/api/anime/1/sample")).status_code == 401


async def test_sampling_is_gone(user_client: AsyncClient, api_factory: SessionFactory) -> None:
    """M21 (FR-A8 retired): 410 with a sentence, and nothing written."""
    anime_id = await a_show(api_factory, anilist_id=965001)

    response = await user_client.post(f"/api/anime/{anime_id}/sample")
    unknown = await user_client.post("/api/anime/999999/sample")

    assert response.status_code == unknown.status_code == 410
    assert response.json()["detail"] == SAMPLE_RETIRED
    async with api_factory() as session:
        assert list((await session.scalars(select(Want))).all()) == []
        assert list((await session.scalars(select(Job))).all()) == []


async def test_the_show_page_still_carries_a_leftover_sample(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965005)
    user_id = await user_id_of(api_factory, USER_EMAIL)
    episode_id = await leftover_sample(api_factory, user_id, anime_id, state=EpisodeState.WANTED)

    body = (await user_client.get(f"/api/anime/{anime_id}")).json()

    assert body["sample"]["episode_id"] == episode_id
    assert body["sample"]["state"] == EpisodeState.WANTED.value


async def test_cancelling_a_leftover_answers_204_and_drops_the_want(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965007)
    user_id = await user_id_of(api_factory, USER_EMAIL)
    episode_id = await leftover_sample(api_factory, user_id, anime_id, state=EpisodeState.WANTED)

    response = await user_client.delete(f"/api/anime/{anime_id}/sample")
    after = (await user_client.get(f"/api/anime/{anime_id}")).json()

    assert response.status_code == 204
    assert after["sample"] is None
    episode = next(row for row in after["episodes"] if row["id"] == episode_id)
    assert episode["state"] == EpisodeState.NOT_WANTED.value, "released with the cancel"
    async with api_factory() as session:
        want = await session.get(Want, (user_id, episode_id))
        assert want is not None, "kept as retention's grace anchor"
        assert want.dropped_at is not None
        assert want.drop_reason == SAMPLE_CANCELLED_REASON


async def test_cancelling_nothing_is_a_404(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965008)
    user_id = await user_id_of(api_factory, USER_EMAIL)

    first = await user_client.delete(f"/api/anime/{anime_id}/sample")
    await leftover_sample(api_factory, user_id, anime_id, state=EpisodeState.WANTED)
    await user_client.delete(f"/api/anime/{anime_id}/sample")
    again = await user_client.delete(f"/api/anime/{anime_id}/sample")

    assert first.status_code == 404
    assert first.json()["detail"] == SAMPLE_NOT_FOUND
    assert again.status_code == 404, "a sample already dropped is not one to cancel"


async def test_a_leftover_sample_never_fetches_more_than_the_one_episode(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965010)
    user_id = await user_id_of(api_factory, USER_EMAIL)
    await leftover_sample(api_factory, user_id, anime_id, state=EpisodeState.NOT_WANTED)

    async with api_factory() as session:
        await compute_wants(session)
        await session.commit()
        wants = list((await session.scalars(select(Want))).all())
        states = list(
            (await session.scalars(select(Episode.state).where(Episode.anime_id == anime_id))).all()
        )

    assert len(wants) == 1
    assert states.count(EpisodeState.WANTED) == 1
    assert set(states) == {EpisodeState.WANTED, EpisodeState.NOT_WANTED}


# --- FR-A14 (M21 plan 2.4): the sample cancel never stops an open demand -----------


async def test_cancelling_a_sample_leaves_another_users_demanded_download_running(
    api_app: FastAPI, user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965011)
    user_id = await user_id_of(api_factory, USER_EMAIL)
    episode_id = await leftover_sample(
        api_factory, user_id, anime_id, state=EpisodeState.DOWNLOADING
    )
    other = await add_user(api_factory, OTHER_EMAIL, OTHER_PASSWORD)
    async with api_factory() as session:
        session.add(Want(user_id=other.id, episode_id=episode_id, demanded_at=datetime.now(UTC)))
        await session.commit()

    response = await user_client.delete(f"/api/anime/{anime_id}/sample")

    assert response.status_code == 204
    async with api_factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None and episode.state is EpisodeState.DOWNLOADING
        assert (await session.scalars(select(Job).where(Job.type == QBIT_CANCEL))).all() == []
        theirs = await session.get(Want, (other.id, episode_id))
        assert theirs is not None and theirs.dropped_at is None


async def test_demanding_a_sampled_episode_makes_it_a_demand_the_cancel_cannot_reach(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The demand takes the row over (``sample`` cleared), so Cancel finds no sample."""
    anime_id = await a_show(api_factory, anilist_id=965012)
    user_id = await user_id_of(api_factory, USER_EMAIL)
    episode_id = await leftover_sample(
        api_factory, user_id, anime_id, state=EpisodeState.DOWNLOADING
    )
    async with api_factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        await demand(session, user_id=user_id, episode=episode, now=datetime.now(UTC))
        await session.commit()

    response = await user_client.delete(f"/api/anime/{anime_id}/sample")

    assert response.status_code == 404
    async with api_factory() as session:
        want = await session.get(Want, (user_id, episode_id))
        assert want is not None and want.dropped_at is None and want.sample is False
        assert want.demanded_at is not None
        episode = await session.get(Episode, episode_id)
        assert episode is not None and episode.state is EpisodeState.DOWNLOADING
