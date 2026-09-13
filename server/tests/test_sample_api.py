"""The two "try episode 1" routes, and the field they put on a show page (FR-A8).

Asserted on the JSON rather than on the schema objects, for the reason
:mod:`tests.test_acquisition_api` gives: the shape is the contract, and the
show page's button is written against exactly these three keys and these three
sentences.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.api.anime import ANIME_NOT_FOUND, SAMPLE_NOT_FOUND
from arc.db import SessionFactory
from arc.models import Anime, Episode, EpisodeState, Job, ListStatus, User, Want
from arc.services.acquisition import compute_wants
from arc.services.acquisition.names import COMPUTE_WANTS
from arc.services.acquisition.samples import (
    ALREADY_FOLLOWING_DETAIL,
    NO_EPISODES_DETAIL,
    NOT_AIRED_DETAIL,
    SAMPLE_CANCELLED_REASON,
)
from tests.acquisition_helpers import make_anime, make_entry, make_episodes
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "sampler@arc.test"
USER_PASSWORD = "samplerpassword"

OTHER_EMAIL = "other-sampler@arc.test"
OTHER_PASSWORD = "otherpassword"


@pytest.fixture
async def user_client(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    """A signed-in **ordinary** account: sampling is nobody's privilege."""
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


async def sampler_id(factory: SessionFactory) -> int:
    """The id of the account :func:`user_client` signed in."""
    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == USER_EMAIL))
        assert user is not None
        return user.id


async def test_an_anonymous_caller_cannot_sample_or_cancel(api_client: AsyncClient) -> None:
    assert (await api_client.post("/api/anime/1/sample")).status_code == 401
    assert (await api_client.delete("/api/anime/1/sample")).status_code == 401


async def test_sampling_an_unknown_show_is_a_404(user_client: AsyncClient) -> None:
    response = await user_client.post("/api/anime/999999/sample")

    assert response.status_code == 404
    assert response.json()["detail"] == ANIME_NOT_FOUND


async def test_sampling_answers_202_with_the_episode_it_will_fetch(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965001)

    response = await user_client.post(f"/api/anime/{anime_id}/sample")

    assert response.status_code == 202
    body = response.json()
    assert body["episode_number"] == 1
    assert isinstance(body["episode_id"], int)
    assert body["requested_at"] is not None
    # The route starts the search itself, so the answer already says what the
    # episode row will say (FR-A7): no fifteen-minute "Not fetched".
    assert body["state"] == EpisodeState.WANTED.value

    user_id = await sampler_id(api_factory)
    async with api_factory() as session:
        want = await session.get(Want, (user_id, body["episode_id"]))
        assert want is not None
        assert want.sample is True
        assert want.dropped_at is None
        queued = list((await session.scalars(select(Job).where(Job.type == COMPUTE_WANTS))).all())
        assert len(queued) == 1, "the reconciler does the fetching, not the request"


async def test_sampling_a_show_with_no_episodes_is_a_409(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=965002)
        await session.commit()
        anime_id = anime.id

    response = await user_client.post(f"/api/anime/{anime_id}/sample")

    assert response.status_code == 409
    assert response.json()["detail"] == NO_EPISODES_DETAIL


async def test_sampling_an_unaired_first_episode_is_a_409(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965003, aired_through=0)

    response = await user_client.post(f"/api/anime/{anime_id}/sample")

    assert response.status_code == 409
    assert response.json()["detail"] == NOT_AIRED_DETAIL


async def test_sampling_a_show_already_being_followed_is_a_409(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965004)
    async with api_factory() as session:
        user = await session.scalar(select(User).where(User.email == USER_EMAIL))
        anime = await session.get(Anime, anime_id)
        assert user is not None and anime is not None
        await make_entry(session, user, anime, status=ListStatus.WATCHING)
        await session.commit()

    response = await user_client.post(f"/api/anime/{anime_id}/sample")

    assert response.status_code == 409
    assert response.json()["detail"] == ALREADY_FOLLOWING_DETAIL


async def test_the_show_page_carries_the_callers_own_sample(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965005)

    before = (await user_client.get(f"/api/anime/{anime_id}")).json()
    posted = (await user_client.post(f"/api/anime/{anime_id}/sample")).json()
    after = (await user_client.get(f"/api/anime/{anime_id}")).json()

    assert before["sample"] is None
    assert after["sample"] == posted, "the same three facts and the same state"
    episode = next(row for row in after["episodes"] if row["id"] == posted["episode_id"])
    assert episode["state"] == posted["state"] == EpisodeState.WANTED.value


async def test_another_users_sample_is_not_on_your_show_page(
    api_app: FastAPI, user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """A want is per user; so is the pill on the button."""
    anime_id = await a_show(api_factory, anilist_id=965006)
    await user_client.post(f"/api/anime/{anime_id}/sample")

    await add_user(api_factory, OTHER_EMAIL, OTHER_PASSWORD)
    async with api_transport(api_app) as other:
        await login(other, OTHER_EMAIL, OTHER_PASSWORD)
        body = (await other.get(f"/api/anime/{anime_id}")).json()

    assert body["sample"] is None


async def test_cancelling_answers_204_and_drops_the_want(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965007)

    posted = (await user_client.post(f"/api/anime/{anime_id}/sample")).json()
    response = await user_client.delete(f"/api/anime/{anime_id}/sample")
    after = (await user_client.get(f"/api/anime/{anime_id}")).json()

    assert response.status_code == 204
    assert after["sample"] is None
    episode = next(row for row in after["episodes"] if row["id"] == posted["episode_id"])
    assert episode["state"] == EpisodeState.NOT_WANTED.value, "released with the cancel"
    user_id = await sampler_id(api_factory)
    async with api_factory() as session:
        want = await session.get(Want, (user_id, posted["episode_id"]))
        assert want is not None, "kept as retention's grace anchor"
        assert want.dropped_at is not None
        assert want.drop_reason == SAMPLE_CANCELLED_REASON


async def test_cancelling_nothing_is_a_404(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=965008)

    first = await user_client.delete(f"/api/anime/{anime_id}/sample")
    await user_client.post(f"/api/anime/{anime_id}/sample")
    await user_client.delete(f"/api/anime/{anime_id}/sample")
    again = await user_client.delete(f"/api/anime/{anime_id}/sample")

    assert first.status_code == 404
    assert first.json()["detail"] == SAMPLE_NOT_FOUND
    assert again.status_code == 404, "a sample already dropped is not one to cancel"


async def test_sampling_twice_is_one_want(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Idempotent: the button is pressed twice by people, not by programs."""
    anime_id = await a_show(api_factory, anilist_id=965009)

    first = (await user_client.post(f"/api/anime/{anime_id}/sample")).json()
    second = (await user_client.post(f"/api/anime/{anime_id}/sample")).json()

    assert first["episode_id"] == second["episode_id"]
    async with api_factory() as session:
        assert len(list((await session.scalars(select(Want))).all())) == 1


async def test_a_sample_never_fetches_more_than_the_one_episode(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The non-negotiable: a sample is one episode, whatever else has aired."""
    anime_id = await a_show(api_factory, anilist_id=965010)

    await user_client.post(f"/api/anime/{anime_id}/sample")

    async with api_factory() as session:
        await compute_wants(session)
        await session.commit()
        wants = list((await session.scalars(select(Want))).all())
        states = list((await session.scalars(select(Episode.state))).all())

    assert len(wants) == 1
    assert states.count(EpisodeState.WANTED) == 1
    assert set(states) == {EpisodeState.WANTED, EpisodeState.NOT_WANTED}
