"""``GET /api/episodes/{id}/play`` for any aired episode (FR-A14, M21 T4).

* Any aired, not-ready episode the caller has no want on answers ``pending``
  with ``demanded: false`` — a completed show, an unlisted one, a dormant
  entry — and writes nothing (the player posts the demand). 404 only for an
  unknown or un-aired episode.
* The order is unchanged: an open demand (``demanded: true``) outranks the
  trip copy, the trip copy (``offline_only``) outranks another live want.
* ``pending.note`` says "plays when its download finishes" for a pack, a live
  refusal and an MP4, and nothing for a plain single; ``awaiting_pick`` is
  false until T7.
* ``next.fetchable`` is aired ∧ not ready ∧ nothing would refuse the press:
  false while paused, held, or at the cap (unless already demanded), and
  false for a ready neighbour.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from arc.db import SessionFactory
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListStatus,
    Rendition,
    Rewatch,
    Torrent,
    TorrentFile,
    TorrentKind,
    Trip,
    TripEpisode,
    TripEpisodeState,
    User,
    Want,
    WatchProgress,
)
from arc.services.acquisition import rules
from arc.services.acquisition.demand import PLAYS_WHEN_DOWNLOADED_MESSAGE
from arc.services.acquisition.rules import DEMAND_MAX_OPEN_KEY, PAUSED_KEY
from arc.services.catalog.airing import FINISHED
from arc.services.media.live import REFUSED_NOT_MKV
from arc.services.media.names import REMUX_LIVE, STAGE_LIVE_REFUSED
from tests.acquisition_helpers import (
    fake_free_space,
    make_anime,
    make_entry,
    make_episodes,
    set_setting,
)
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "play-any@arc.test"
USER_PASSWORD = "play-any-password"


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
    activated: bool = True,
    aired_through: int = 10,
) -> list[int]:
    """A twelve-episode show, 1..``aired_through`` aired; episode ids by index."""
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
        return [row.id for row in rows]


async def make_ready(factory: SessionFactory, episode_id: int) -> None:
    async with factory() as session:
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


async def set_state(factory: SessionFactory, episode_id: int, state: EpisodeState) -> None:
    async with factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        episode.state = state
        await session.commit()


async def add_demands(factory: SessionFactory, user_id: int, *episode_ids: int) -> None:
    async with factory() as session:
        for episode_id in episode_ids:
            session.add(Want(user_id=user_id, episode_id=episode_id, demanded_at=datetime.now(UTC)))
        await session.commit()


async def counts(factory: SessionFactory) -> tuple[int, int]:
    """(wants, jobs): a read must leave both alone."""
    async with factory() as session:
        wants = await session.scalar(select(func.count()).select_from(Want))
        jobs = await session.scalar(select(func.count()).select_from(Job))
        return int(wants or 0), int(jobs or 0)


async def play(client: AsyncClient, episode_id: int) -> dict[str, Any]:
    response = await client.get(f"/api/episodes/{episode_id}/play")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- pending for any aired episode ------------------------------------------------


@pytest.mark.parametrize(
    ("anilist_id", "status", "activated"),
    [
        (977001, ListStatus.COMPLETED, True),
        (977002, None, True),
        (977003, ListStatus.PLANNED, False),
    ],
    ids=["completed-show", "unlisted-show", "dormant-entry"],
)
async def test_an_aired_episode_nobody_asked_for_is_pending_and_not_demanded(
    client: AsyncClient,
    user: User,
    api_factory: SessionFactory,
    anilist_id: int,
    status: ListStatus | None,
    activated: bool,
) -> None:
    ids = await show(
        api_factory,
        user.id,
        anilist_id=anilist_id,
        status=status,
        progress=12 if status is ListStatus.COMPLETED else 0,
        activated=activated,
    )
    before = await counts(api_factory)

    body = await play(client, ids[2])

    assert body["playlist_url"] is None
    assert body["offline_only"] is False
    assert body["live"] is None
    assert body["pending"] == {
        "state": "not_wanted",
        "progress": None,
        "seeders": None,
        "demanded": False,
        "note": None,
        "awaiting_pick": False,
    }
    assert body["episode"]["id"] == ids[2]
    assert await counts(api_factory) == before, "a read writes no want and queues nothing"


async def test_an_unaired_episode_is_a_404(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await show(api_factory, user.id, anilist_id=977010, status=None)

    assert (await client.get(f"/api/episodes/{ids[10]}/play")).status_code == 404
    assert (await client.get(f"/api/episodes/{ids[11]}/play")).status_code == 404


async def test_an_unknown_episode_is_a_404(client: AsyncClient) -> None:
    assert (await client.get("/api/episodes/999999999/play")).status_code == 404


async def test_an_announced_shows_episode_is_a_404(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """NOT_YET_RELEASED never has an aired episode, whatever its dates say."""
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=977011, status="NOT_YET_RELEASED", episodes=12)
        rows = await make_episodes(session, anime, 12, aired_through=3)
        await session.commit()
        first = rows[0].id

    assert (await client.get(f"/api/episodes/{first}/play")).status_code == 404


# --- the order ----------------------------------------------------------------------


async def test_an_open_demand_is_pending_and_demanded(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await show(api_factory, user.id, anilist_id=977020, status=None)
    await add_demands(api_factory, user.id, ids[5])

    body = await play(client, ids[5])

    assert body["pending"]["demanded"] is True


async def test_a_window_want_is_pending_and_not_demanded(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """A live want that is not a demand (the window's): the player still demands once."""
    ids = await show(api_factory, user.id, anilist_id=977021)
    async with api_factory() as session:
        session.add(Want(user_id=user.id, episode_id=ids[4]))
        await session.commit()

    body = await play(client, ids[4])

    assert body["pending"] is not None
    assert body["pending"]["demanded"] is False


async def trip_copy(factory: SessionFactory, user_id: int, episode_id: int) -> None:
    async with factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        at = datetime.now(UTC)
        trip = Trip(
            user_id=user_id,
            anime_id=episode.anime_id,
            first_number=episode.number,
            last_number=episode.number,
            count=1,
            deadline_at=at + timedelta(days=14),
        )
        session.add(trip)
        await session.flush()
        session.add(
            TripEpisode(
                trip_id=trip.id,
                episode_id=episode_id,
                state=TripEpisodeState.DELIVERED,
                delivered_at=at,
            )
        )
        await session.commit()


async def test_the_trip_copy_outranks_the_new_aired_branch(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """No want at all, but the device holds the copy: ``offline_only``, as before."""
    ids = await show(api_factory, user.id, anilist_id=977022, status=None)
    await trip_copy(api_factory, user.id, ids[3])

    body = await play(client, ids[3])

    assert body["offline_only"] is True
    assert body["pending"] is None


async def test_an_open_demand_outranks_the_trip_copy(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await show(api_factory, user.id, anilist_id=977023, status=None)
    await trip_copy(api_factory, user.id, ids[3])
    await add_demands(api_factory, user.id, ids[3])

    body = await play(client, ids[3])

    assert body["offline_only"] is False
    assert body["pending"]["demanded"] is True


# --- pending.note -------------------------------------------------------------------


async def downloading_single(
    factory: SessionFactory, episode_id: int, *, title: str, info_hash: str
) -> None:
    async with factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        episode.state = EpisodeState.DOWNLOADING
        session.add(
            Torrent(
                episode_id=episode_id,
                info_hash=info_hash,
                title=title,
                qbit_state="downloading",
                progress=0.2,
                seeders=9,
            )
        )
        await session.commit()


async def test_a_plain_single_has_no_note(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await show(api_factory, user.id, anilist_id=977030, status=None)
    await downloading_single(
        api_factory, ids[4], title="[SubsPlease] Show - 05 (1080p).mkv", info_hash="a1" * 20
    )

    pending = (await play(client, ids[4]))["pending"]

    assert pending["state"] == "downloading"
    assert pending["progress"] == pytest.approx(0.2)
    assert pending["note"] is None
    assert pending["awaiting_pick"] is False


async def test_an_mp4_release_has_the_note(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await show(api_factory, user.id, anilist_id=977031, status=None)
    await downloading_single(
        api_factory, ids[4], title="[Group] Show - 05 [1080p].MP4", info_hash="a2" * 20
    )

    assert (await play(client, ids[4]))["pending"]["note"] == PLAYS_WHEN_DOWNLOADED_MESSAGE


async def test_a_live_refusal_of_this_download_has_the_note(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """The live remux looked at the file and refused it (here: not Matroska)."""
    ids = await show(api_factory, user.id, anilist_id=977032, status=None)
    await downloading_single(
        api_factory, ids[4], title="[Group] Show - 05 [1080p]", info_hash="a3" * 20
    )
    await add_demands(api_factory, user.id, ids[4])
    async with api_factory() as session:
        session.add(
            Job(
                type=REMUX_LIVE,
                payload={
                    "episode_id": ids[4],
                    "stage": STAGE_LIVE_REFUSED,
                    "reason": REFUSED_NOT_MKV,
                },
                status=JobStatus.DONE,
            )
        )
        await session.commit()

    pending = (await play(client, ids[4]))["pending"]

    assert pending["demanded"] is True
    assert pending["note"] == PLAYS_WHEN_DOWNLOADED_MESSAGE


async def test_a_pack_backed_episode_has_the_note(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await show(api_factory, user.id, anilist_id=977033, status=None)
    async with api_factory() as session:
        episode = await session.get(Episode, ids[6])
        assert episode is not None
        episode.state = EpisodeState.DOWNLOADING
        torrent = Torrent(
            episode_id=None,
            info_hash="a4" * 20,
            kind=TorrentKind.BATCH,
            title="[Judas] Show [BD 1080p]",
            seeders=41,
            progress=0.9,
            qbit_state="downloading",
        )
        session.add(torrent)
        await session.flush()
        session.add(
            TorrentFile(
                torrent_id=torrent.id,
                file_index=6,
                path="[Judas] Show - 07.mkv",
                size=1_000_000_000,
                episode_id=ids[6],
                wanted=True,
                progress=0.3,
            )
        )
        await session.commit()

    pending = (await play(client, ids[6]))["pending"]

    assert pending["progress"] == pytest.approx(0.3), "the file's own share"
    assert pending["note"] == PLAYS_WHEN_DOWNLOADED_MESSAGE


# --- next.fetchable -----------------------------------------------------------------


async def opened_on_ready(factory: SessionFactory, user_id: int, anilist_id: int) -> list[int]:
    """Episode 5 ready, 6 not: the player opened on 5."""
    ids = await show(factory, user_id, anilist_id=anilist_id, status=None)
    await make_ready(factory, ids[4])
    return ids


async def test_next_is_fetchable_when_nothing_refuses(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await opened_on_ready(api_factory, user.id, 977040)

    body = await play(client, ids[4])

    assert body["next"]["id"] == ids[5]
    assert body["next"]["fetchable"] is True
    assert body["previous"]["fetchable"] is True


async def test_a_ready_neighbour_is_not_fetchable(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await opened_on_ready(api_factory, user.id, 977041)
    await make_ready(api_factory, ids[5])

    body = await play(client, ids[4])

    assert body["next"]["ready"] is True
    assert body["next"]["fetchable"] is False


async def test_an_unaired_neighbour_is_not_fetchable(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await show(api_factory, user.id, anilist_id=977042, status=None)
    await make_ready(api_factory, ids[9])

    body = await play(client, ids[9])

    assert body["next"]["id"] == ids[10]
    assert body["next"]["fetchable"] is False


async def test_next_is_not_fetchable_while_paused(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await opened_on_ready(api_factory, user.id, 977043)
    async with api_factory() as session:
        await set_setting(session, PAUSED_KEY, True)
        await session.commit()

    body = await play(client, ids[4])

    assert body["next"]["fetchable"] is False


async def test_a_downloading_next_is_fetchable_while_paused(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """A pause refuses only an episode still to be fetched (the demand's rule)."""
    ids = await opened_on_ready(api_factory, user.id, 977044)
    await set_state(api_factory, ids[5], EpisodeState.DOWNLOADING)
    async with api_factory() as session:
        await set_setting(session, PAUSED_KEY, True)
        await session.commit()

    body = await play(client, ids[4])

    assert body["next"]["fetchable"] is True


async def test_next_is_not_fetchable_while_storage_is_held(
    client: AsyncClient,
    user: User,
    api_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await opened_on_ready(api_factory, user.id, 977045)
    fake_free_space(monkeypatch, rules, 1)

    body = await play(client, ids[4])

    assert body["next"]["fetchable"] is False


async def test_next_is_not_fetchable_at_the_cap_unless_already_demanded(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await opened_on_ready(api_factory, user.id, 977046)
    other = await show(api_factory, user.id, anilist_id=977047, status=None)
    async with api_factory() as session:
        await set_setting(session, DEMAND_MAX_OPEN_KEY, 2)
        await session.commit()
    await add_demands(api_factory, user.id, other[0], other[1])

    body = await play(client, ids[4])
    assert body["next"]["fetchable"] is False, "a new demand would be refused for the cap"

    # The next episode itself is one of the open demands: pressing it restamps.
    async with api_factory() as session:
        found = await session.get(Want, (user.id, other[1]))
        assert found is not None
        await session.delete(found)
        await session.commit()
    await add_demands(api_factory, user.id, ids[5])

    body = await play(client, ids[4])
    assert body["next"]["fetchable"] is True


# --- the episode's watched fields in a pass ----------------------------------------


async def test_in_a_pass_the_players_episode_reads_the_pass(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """List at 12, episode 3 finished long before the pass: not watched in it (FR-W5)."""
    ids = await show(
        api_factory, user.id, anilist_id=977050, status=ListStatus.COMPLETED, progress=12
    )
    await make_ready(api_factory, ids[2])
    started = datetime.now(UTC) - timedelta(days=1)
    async with api_factory() as session:
        session.add(
            WatchProgress(
                user_id=user.id,
                episode_id=ids[2],
                position_s=1420.0,
                duration_s=1420.0,
                completed=True,
                completed_at=started - timedelta(days=40),
                finished_at=started - timedelta(days=40),
            )
        )
        await session.commit()
    before = (await play(client, ids[2]))["episode"]
    assert (before["watched"], before["watched_source"]) == (True, "progress")

    async with api_factory() as session:
        episode = await session.get(Episode, ids[2])
        assert episode is not None
        session.add(Rewatch(user_id=user.id, anime_id=episode.anime_id, started_at=started))
        await session.commit()

    during = (await play(client, ids[2]))["episode"]
    assert (during["watched"], during["watched_source"]) == (False, None)
