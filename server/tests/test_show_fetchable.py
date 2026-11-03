"""The show page's play-on-demand and rewatch fields (FR-A14, FR-W5, FR-W7; M21 T4).

* ``EpisodeOut.fetchable`` is aired ∧ not ready on **any** show, whatever the
  list (M20's window rule is retired): false on a ready or un-aired row.
* ``EpisodeOut.demanded`` and ``AnimeDetail.demand`` — the caller's open
  demands, ``demand_max_open`` and the refusal a click on the page would get
  now (the cap, a pause, a storage hold).
* ``AnimeDetail.rewatch`` and the watched fields read through
  ``rewatch.watch_view``: in an open pass only the pass's finishes count.
* ``sample`` is always null (FR-A8 retired).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from arc.db import SessionFactory
from arc.models import (
    Episode,
    EpisodeState,
    ListStatus,
    Rendition,
    Rewatch,
    User,
    Want,
    WatchProgress,
)
from arc.services.acquisition import rules
from arc.services.acquisition.demand import (
    ACQUISITION_PAUSED,
    ACQUISITION_PAUSED_MESSAGE,
    DEMAND_CAP,
    STORAGE_HELD,
    demand_cap_message,
)
from arc.services.acquisition.rules import DEMAND_MAX_OPEN_KEY, PAUSED_KEY
from arc.services.catalog.airing import FINISHED
from tests.acquisition_helpers import (
    fake_free_space,
    make_anime,
    make_entry,
    make_episodes,
    set_setting,
)
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
    factory: SessionFactory,
    user_id: int,
    *,
    anilist_id: int,
    status: ListStatus | None = ListStatus.WATCHING,
    progress: int = 4,
    aired_through: int = 12,
    activated: bool = True,
) -> tuple[int, list[int]]:
    """A finished twelve-episode show; on the list at ``progress`` unless ``status`` is None."""
    async with factory() as session:
        # Still airing while some episode has not: a FINISHED show has aired
        # every episode, whatever the dates say.
        anime = await make_anime(
            session,
            anilist_id=anilist_id,
            status=FINISHED if aired_through >= 12 else "RELEASING",
            episodes=12,
        )
        rows = await make_episodes(session, anime, 12, aired_through=aired_through)
        if status is not None:
            owner = await session.get(User, user_id)
            assert owner is not None
            await make_entry(
                session, owner, anime, status=status, progress=progress, activated=activated
            )
        await session.commit()
        return anime.id, [row.id for row in rows]


async def make_ready(factory: SessionFactory, *episode_ids: int) -> None:
    async with factory() as session:
        for episode_id in episode_ids:
            episode = await session.get(Episode, episode_id)
            assert episode is not None
            episode.state = EpisodeState.READY
            session.add(
                Rendition(
                    episode_id=episode_id,
                    dir=f"/data/renditions/{episode_id}",
                    playlist_path=f"/data/renditions/{episode_id}/index.m3u8",
                    duration=1420.0,
                    ready_at=datetime.now(UTC),
                )
            )
        await session.commit()


async def demand_rows(factory: SessionFactory, user_id: int, *episode_ids: int) -> None:
    """Open demands, oldest first (a second apart)."""
    async with factory() as session:
        base = datetime.now(UTC) - timedelta(minutes=10)
        for offset, episode_id in enumerate(episode_ids):
            session.add(
                Want(
                    user_id=user_id,
                    episode_id=episode_id,
                    demanded_at=base + timedelta(seconds=offset),
                )
            )
        await session.commit()


def column(payload: dict[str, Any], key: str) -> dict[int, Any]:
    return {row["id"]: row[key] for row in payload["episodes"]}


async def detail(client: AsyncClient, anime_id: int) -> dict[str, Any]:
    response = await client.get(f"/api/anime/{anime_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- fetchable: aired ∧ not ready -------------------------------------------------


@pytest.mark.parametrize(
    ("anilist_id", "status", "activated"),
    [
        (976110, ListStatus.WATCHING, True),
        (976111, ListStatus.COMPLETED, True),
        (976112, ListStatus.DROPPED, True),
        (976113, ListStatus.PLANNED, False),
        (976114, None, True),
    ],
    ids=["watching", "completed", "dropped", "dormant", "unlisted"],
)
async def test_every_aired_not_ready_row_is_fetchable_whatever_the_list(
    client: AsyncClient,
    user: User,
    api_factory: SessionFactory,
    anilist_id: int,
    status: ListStatus | None,
    activated: bool,
) -> None:
    anime_id, ids = await show(
        api_factory,
        user.id,
        anilist_id=anilist_id,
        status=status,
        aired_through=10,
        activated=activated,
    )
    await make_ready(api_factory, ids[2])

    flags = column(await detail(client, anime_id), "fetchable")

    assert flags[ids[0]] is True, "behind the progress"
    assert flags[ids[8]] is True, "well past any window"
    assert flags[ids[2]] is False, "ready: nothing to fetch"
    assert flags[ids[10]] is False, "not aired"
    assert flags[ids[11]] is False, "not aired"


async def test_the_show_page_plans_wants_only_for_the_slot_note(
    client: AsyncClient,
    user: User,
    api_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coverage helpers are gone: ``fetchable`` never plans the caller's wants.

    A dropped show the caller holds a live want on (M20 planned there, for
    ``coverable``) plans nothing; a watching show plans exactly once — FR-A10's
    slot note (``slot_view``), which is the only planner left on this page.
    """
    from arc.services.acquisition import wants

    calls: list[object] = []
    original = wants._plan_wants

    async def counted(*args: object, **kwargs: object) -> object:
        calls.append(kwargs.get("user_ids"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(wants, "_plan_wants", counted)
    dropped_id, dropped = await show(
        api_factory, user.id, anilist_id=976170, status=ListStatus.DROPPED
    )
    async with api_factory() as session:
        session.add(Want(user_id=user.id, episode_id=dropped[6]))
        await session.commit()

    body = await detail(client, dropped_id)

    assert column(body, "wanted_by_me")[dropped[6]] is True
    assert any(column(body, "fetchable").values())
    assert calls == []

    watching_id, _watching = await show(api_factory, user.id, anilist_id=976169)
    await detail(client, watching_id)

    assert len(calls) == 1


# --- demanded and AnimeDetail.demand ----------------------------------------------


async def test_open_demands_are_listed_with_the_cap_and_flag_their_rows(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id, ids = await show(api_factory, user.id, anilist_id=976171, status=None)
    other_id, other = await show(api_factory, user.id, anilist_id=976172)
    await demand_rows(api_factory, user.id, other[7], ids[2])

    body = await detail(client, anime_id)

    assert body["demand"]["max"] == 3
    assert body["demand"]["refusal"] is None
    listed = [(row["episode_id"], row["anime_id"], row["number"]) for row in body["demand"]["open"]]
    assert listed == [(other[7], other_id, 8), (ids[2], anime_id, 3)]
    assert all(row["title"] for row in body["demand"]["open"])
    demanded = column(body, "demanded")
    assert demanded[ids[2]] is True
    assert sum(demanded.values()) == 1, "only this show's own rows"


async def test_a_demand_finished_since_the_press_is_not_open(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id, ids = await show(api_factory, user.id, anilist_id=976173, status=None)
    await demand_rows(api_factory, user.id, ids[2], ids[3])
    async with api_factory() as session:
        session.add(
            WatchProgress(
                user_id=user.id,
                episode_id=ids[2],
                position_s=1420.0,
                duration_s=1420.0,
                completed=True,
                completed_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()

    body = await detail(client, anime_id)

    assert [row["episode_id"] for row in body["demand"]["open"]] == [ids[3]]
    demanded = column(body, "demanded")
    assert demanded[ids[2]] is False and demanded[ids[3]] is True


async def test_at_the_cap_the_page_carries_the_cap_refusal(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id, ids = await show(api_factory, user.id, anilist_id=976174)
    await demand_rows(api_factory, user.id, ids[6], ids[7], ids[8])

    body = await detail(client, anime_id)

    assert body["demand"]["refusal"] == {"code": DEMAND_CAP, "message": demand_cap_message(3)}
    assert len(body["demand"]["open"]) == 3


async def test_a_cap_of_zero_never_refuses(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id, ids = await show(api_factory, user.id, anilist_id=976175)
    async with api_factory() as session:
        await set_setting(session, DEMAND_MAX_OPEN_KEY, 0)
        await session.commit()
    await demand_rows(api_factory, user.id, ids[5], ids[6], ids[7], ids[8])

    body = await detail(client, anime_id)

    assert body["demand"]["max"] == 0
    assert body["demand"]["refusal"] is None


async def test_while_paused_the_page_carries_the_pause(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id, _ids = await show(api_factory, user.id, anilist_id=976176)
    async with api_factory() as session:
        await set_setting(session, PAUSED_KEY, True)
        await session.commit()

    body = await detail(client, anime_id)

    assert body["demand"]["refusal"] == {
        "code": ACQUISITION_PAUSED,
        "message": ACQUISITION_PAUSED_MESSAGE,
    }


async def test_while_storage_is_held_the_page_carries_the_hold(
    client: AsyncClient,
    user: User,
    api_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anime_id, _ids = await show(api_factory, user.id, anilist_id=976177)
    fake_free_space(monkeypatch, rules, 1)

    body = await detail(client, anime_id)

    assert body["demand"]["refusal"]["code"] == STORAGE_HELD


async def test_sample_is_always_null(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """FR-A8 is retired: a live sample want made before M21 is not reported."""
    anime_id, ids = await show(api_factory, user.id, anilist_id=976178, status=None)
    async with api_factory() as session:
        session.add(Want(user_id=user.id, episode_id=ids[0], sample=True))
        await session.commit()

    body = await detail(client, anime_id)

    assert body["sample"] is None


# --- rewatch: the pass's watched marks --------------------------------------------


async def test_in_an_open_pass_only_the_pass_finishes_are_watched(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """List at 12 (completed), episode 2 finished before the pass, episode 1 in it.

    The marks start again from episode 1: 1 is ``arc`` (Unwatch clears the
    pass's finish), 2 and 5 are not watched, and the hero's pick — the first
    unwatched episode — is episode 2.
    """
    anime_id, ids = await show(
        api_factory, user.id, anilist_id=976180, status=ListStatus.COMPLETED, progress=12
    )
    started = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)
    async with api_factory() as session:
        session.add(Rewatch(user_id=user.id, anime_id=anime_id, started_at=started))
        for episode_id, finished in (
            (ids[0], started + timedelta(hours=1)),
            (ids[1], started - timedelta(days=30)),
        ):
            session.add(
                WatchProgress(
                    user_id=user.id,
                    episode_id=episode_id,
                    position_s=1420.0,
                    duration_s=1420.0,
                    completed=True,
                    completed_at=started - timedelta(days=60),
                    finished_at=finished,
                )
            )
        await session.commit()

    body = await detail(client, anime_id)

    watched = column(body, "watched")
    sources = column(body, "watched_source")
    assert watched[ids[0]] is True and sources[ids[0]] == "arc"
    assert watched[ids[1]] is False and sources[ids[1]] is None
    assert watched[ids[4]] is False and sources[ids[4]] is None
    first_unwatched = next(row for row in body["episodes"] if not row["watched"])
    assert first_unwatched["id"] == ids[1]
    rewatch = body["rewatch"]
    assert rewatch is not None
    assert datetime.fromisoformat(rewatch["started_at"]) == started
    assert (rewatch["position"], rewatch["finished_in_pass"], rewatch["total"]) == (1, 1, 12)
    # The list is untouched and still says so.
    assert body["list_entry"]["progress"] == 12


async def test_outside_a_pass_the_marks_are_the_lists(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id, ids = await show(api_factory, user.id, anilist_id=976181, progress=4)
    started = datetime.now(UTC) - timedelta(days=3)
    async with api_factory() as session:
        # An ended pass changes nothing.
        session.add(
            Rewatch(
                user_id=user.id,
                anime_id=anime_id,
                started_at=started,
                ended_at=started + timedelta(days=1),
                end_reason="off",
            )
        )
        session.add(
            WatchProgress(
                user_id=user.id,
                episode_id=ids[6],
                position_s=1420.0,
                duration_s=1420.0,
                completed=True,
                completed_at=started,
                finished_at=started,
            )
        )
        await session.commit()

    body = await detail(client, anime_id)

    sources = column(body, "watched_source")
    assert [sources[ids[n]] for n in range(8)] == [
        "progress",
        "progress",
        "progress",
        "arc",
        None,
        None,
        "arc",
        None,
    ]
    assert body["rewatch"] is None


async def test_another_users_pass_leaves_this_users_marks_alone(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    other = await add_user(api_factory, "show-pass-other@arc.test", "other-password")
    anime_id, ids = await show(api_factory, user.id, anilist_id=976182, progress=4)
    async with api_factory() as session:
        session.add(
            Rewatch(
                user_id=other.id,
                anime_id=anime_id,
                started_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()

    body = await detail(client, anime_id)

    sources = column(body, "watched_source")
    assert [sources[ids[n]] for n in range(5)] == ["progress", "progress", "progress", "arc", None]
    assert body["rewatch"] is None
