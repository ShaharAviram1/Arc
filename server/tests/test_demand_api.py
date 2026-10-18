"""Play on demand and device holdings over HTTP (FR-A14, FR-A15, M20 T4).

* ``POST /api/episodes/{id}/demand`` — the route matrix: in the window, not in
  it, held on a device, paused, short of disk, the demo account.
* ``PUT /api/devices/{device_id}/holdings`` — replace semantics, the UUID rule,
  the demo refusal, the size limit.
* ``GET /api/episodes/{id}/play`` — the ``pending`` answer for a wanted episode
  that is not ready, and ``next.fetchable``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    DeviceHolding,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    Rendition,
    Torrent,
    Trip,
    TripEpisode,
    TripEpisodeState,
    User,
    Want,
)
from arc.services.acquisition import rules
from arc.services.acquisition.demand import NOT_IN_WINDOW, NOT_IN_WINDOW_MESSAGE
from arc.services.acquisition.names import DEMAND_PRIORITY, SEARCH_RELEASE
from arc.services.acquisition.rules import PAUSED_KEY
from arc.services.catalog.airing import FINISHED
from tests.acquisition_helpers import (
    fake_free_space,
    make_anime,
    make_entry,
    make_episodes,
    set_setting,
)
from tests.conftest import add_user, api_transport, login
from tests.mal_helpers import link_user, log_rows

pytestmark = pytest.mark.pg

USER_EMAIL = "demander@arc.test"
USER_PASSWORD = "demander-password"
DEMO_EMAIL = "demo-demander@arc.test"
DEMO_PASSWORD = "demo-demander-password"

DEVICE = "0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69"


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def demo_client(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    demo = await add_user(api_factory, DEMO_EMAIL, DEMO_PASSWORD)
    async with api_factory() as session:
        row = await session.get(User, demo.id)
        assert row is not None
        row.is_demo = True
        await session.commit()
    async with api_transport(api_app) as http:
        yield await login(http, DEMO_EMAIL, DEMO_PASSWORD)


async def followed_show(
    factory: SessionFactory, user_id: int, *, anilist_id: int, progress: int = 4
) -> list[int]:
    """A finished twelve-episode show the user is watching at ``progress``; episode ids."""
    async with factory() as session:
        anime = await make_anime(session, anilist_id=anilist_id, status=FINISHED, episodes=12)
        rows = await make_episodes(session, anime, 12, aired_through=12)
        user = await session.get(User, user_id)
        assert user is not None
        await make_entry(session, user, anime, progress=progress)
        await session.commit()
        return [row.id for row in rows]


async def user_id_of(factory: SessionFactory, email: str) -> int:
    async with factory() as session:
        found = await session.scalar(select(User.id).where(User.email == email))
        assert found is not None
        return found


# --- POST /api/episodes/{id}/demand --------------------------------------------------


async def test_an_anonymous_caller_cannot_demand(api_client: AsyncClient) -> None:
    assert (await api_client.post("/api/episodes/1/demand")).status_code == 401


async def test_an_unknown_episode_is_a_404(client: AsyncClient) -> None:
    assert (await client.post("/api/episodes/999999999/demand")).status_code == 404


async def test_demanding_the_window_episode_answers_202(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975001)

    response = await client.post(f"/api/episodes/{ids[4]}/demand")

    assert response.status_code == 202, response.text
    assert response.json() == {"state": "wanted", "live": False, "message": None}
    async with api_factory() as session:
        want = await session.get(Want, (user.id, ids[4]))
        assert want is not None and want.demanded_at is not None
        search = await session.scalar(select(Job).where(Job.type == SEARCH_RELEASE))
        assert search is not None and search.priority == DEMAND_PRIORITY


async def test_demanding_outside_the_window_is_a_409_with_a_sentence(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975002)

    response = await client.post(f"/api/episodes/{ids[8]}/demand")

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": NOT_IN_WINDOW, "message": NOT_IN_WINDOW_MESSAGE}
    async with api_factory() as session:
        assert await session.get(Want, (user.id, ids[8])) is None


async def test_demanding_an_episode_held_on_a_device_answers_202(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975003)
    held = await client.put(f"/api/devices/{DEVICE}/holdings", json={"episode_ids": [ids[4]]})
    assert held.status_code == 204

    response = await client.post(f"/api/episodes/{ids[4]}/demand")

    assert response.status_code == 202
    async with api_factory() as session:
        want = await session.get(Want, (user.id, ids[4]))
        assert want is not None and want.demanded_at is not None


async def test_demanding_while_paused_is_a_409(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975004)
    async with api_factory() as session:
        await set_setting(session, PAUSED_KEY, True)
        await session.commit()

    response = await client.post(f"/api/episodes/{ids[4]}/demand")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "acquisition_paused" and detail["message"]
    async with api_factory() as session:
        assert await session.get(Want, (user.id, ids[4])) is None, "nothing written"


async def test_demanding_while_storage_is_held_is_a_409(
    client: AsyncClient,
    user: User,
    api_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975005)
    fake_free_space(monkeypatch, rules, 1)

    response = await client.post(f"/api/episodes/{ids[4]}/demand")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "storage_held"


async def test_the_demo_account_may_demand(
    demo_client: AsyncClient, api_factory: SessionFactory
) -> None:
    demo_id = await user_id_of(api_factory, DEMO_EMAIL)
    ids = await followed_show(api_factory, demo_id, anilist_id=975006)

    response = await demo_client.post(f"/api/episodes/{ids[4]}/demand")

    assert response.status_code == 202


async def test_demand_needs_an_allowed_origin(
    api_app: FastAPI, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975007)
    async with api_transport(api_app) as http:
        await login(http, USER_EMAIL, USER_PASSWORD)
        response = await http.post(
            f"/api/episodes/{ids[4]}/demand", headers={"Origin": "https://evil.example"}
        )

    assert response.status_code == 403


# --- Next season: the touch, then the demand (FR-S5 item 3, FR-A9, 2026-10-18) -------


async def sequel_show(
    factory: SessionFactory, user_id: int, *, anilist_id: int, entry: str
) -> list[int]:
    """A finished, freshly cached show; ``entry`` is ``absent`` or ``dormant``. Episode ids.

    Fresh so ``PUT /api/list`` serves the cached row without asking a source.
    """
    async with factory() as session:
        anime = await make_anime(session, anilist_id=anilist_id, status=FINISHED, episodes=12)
        anime.refreshed_at = datetime.now(UTC)
        anime.detail_source = "anilist"
        rows = await make_episodes(session, anime, 12, aired_through=12)
        if entry == "dormant":
            user = await session.get(User, user_id)
            assert user is not None
            await make_entry(session, user, anime, activated=False)
        await session.commit()
        return [anime.id, *(row.id for row in rows)]


@pytest.mark.parametrize(
    ("entry", "touch"),
    [("absent", {"status": "watching"}), ("dormant", {})],
)
async def test_after_the_touch_episode_one_is_covered_in_the_same_flow(
    client: AsyncClient, user: User, api_factory: SessionFactory, entry: str, touch: dict[str, str]
) -> None:
    """No wait for the scheduled ``compute_wants``: the demand computes coverage itself.

    Before the press the sequel's episode 1 is outside every window (not on the
    list, or an untouched import), so a bare demand is refused. The player's
    press is the list write and then the demand, and the demand right after
    the write is accepted.
    """
    anime_id, *ids = await sequel_show(
        api_factory, user.id, anilist_id=975050 + int(entry == "dormant"), entry=entry
    )

    refused = await client.post(f"/api/episodes/{ids[0]}/demand")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == NOT_IN_WINDOW

    written = await client.put(f"/api/list/{anime_id}", json=touch)
    assert written.status_code == 200, written.text
    assert written.json()["dormant"] is False
    assert written.json()["status"] == "watching"

    accepted = await client.post(f"/api/episodes/{ids[0]}/demand")
    assert accepted.status_code == 202, accepted.text
    async with api_factory() as session:
        want = await session.get(Want, (user.id, ids[0]))
        assert want is not None and want.demanded_at is not None


@pytest.mark.parametrize("linked", [True, False])
async def test_the_press_writes_to_mal_only_what_the_user_changed(
    client: AsyncClient,
    user: User,
    api_factory: SessionFactory,
    settings: Settings,
    linked: bool,
) -> None:
    """FR-M7 on the press's list write: an add is one status write, a re-send is none.

    The absent sequel's ``{status: watching}`` is an explicit status change, so a
    MAL-linked user gets exactly one queued status row (progress 0 → 0 is not a
    change) and an unlinked user none. The dormant watching entry's ``{}`` moves
    nothing, so it queues nothing for anyone.
    """
    if linked:
        await link_user(api_factory, settings, user_id=user.id)
    absent_id, *_ = await sequel_show(
        api_factory, user.id, anilist_id=975060 + 2 * int(linked), entry="absent"
    )
    dormant_id, *_ = await sequel_show(
        api_factory, user.id, anilist_id=975061 + 2 * int(linked), entry="dormant"
    )

    resent = await client.put(f"/api/list/{dormant_id}", json={})
    assert resent.status_code == 200, resent.text
    assert await log_rows(api_factory, user.id) == []

    added = await client.put(f"/api/list/{absent_id}", json={"status": "watching"})
    assert added.status_code == 200, added.text
    logged = await log_rows(api_factory, user.id)
    if linked:
        assert [(row.anime_id, row.field, row.old_value, row.new_value) for row in logged] == [
            (absent_id, "status", None, "watching")
        ]
    else:
        assert logged == []


# --- PUT /api/devices/{device_id}/holdings -------------------------------------------


async def test_holdings_replace_the_devices_set(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975010)

    first = await client.put(
        f"/api/devices/{DEVICE}/holdings", json={"episode_ids": [ids[4], ids[5]]}
    )
    second = await client.put(
        f"/api/devices/{DEVICE.upper()}/holdings", json={"episode_ids": [ids[5]]}
    )

    assert first.status_code == second.status_code == 204
    async with api_factory() as session:
        rows = await session.execute(
            select(DeviceHolding.device_id, DeviceHolding.episode_id).where(
                DeviceHolding.user_id == user.id
            )
        )
        assert set(rows.all()) == {(DEVICE, ids[5])}, "one device, however it is spelt"

    emptied = await client.put(f"/api/devices/{DEVICE}/holdings", json={"episode_ids": []})
    assert emptied.status_code == 204
    async with api_factory() as session:
        left = await session.scalars(
            select(DeviceHolding.episode_id).where(DeviceHolding.user_id == user.id)
        )
        assert left.all() == []


@pytest.mark.parametrize(
    "device_id",
    [
        "not-a-uuid-at-all-not-a-uuid-at-all",
        # A version-1 UUID: well formed, but not what a device generates.
        "6fa459ea-ee8a-11ca-a0b5-0800200c9a66",
        "00000000-0000-0000-0000-000000000000",
    ],
)
async def test_a_device_id_must_be_a_version_4_uuid(client: AsyncClient, device_id: str) -> None:
    response = await client.put(f"/api/devices/{device_id}/holdings", json={"episode_ids": []})

    assert response.status_code == 422


async def test_holdings_are_limited_to_500(client: AsyncClient) -> None:
    response = await client.put(
        f"/api/devices/{DEVICE}/holdings", json={"episode_ids": list(range(1, 502))}
    )

    assert response.status_code == 422


async def test_the_demo_account_has_no_holdings(demo_client: AsyncClient) -> None:
    response = await demo_client.put(f"/api/devices/{DEVICE}/holdings", json={"episode_ids": []})

    assert response.status_code == 403


async def test_an_anonymous_caller_cannot_report_holdings(api_client: AsyncClient) -> None:
    response = await api_client.put(f"/api/devices/{DEVICE}/holdings", json={"episode_ids": []})

    assert response.status_code == 401


# --- GET /api/episodes/{id}/play -------------------------------------------------------


async def test_play_on_a_wanted_downloading_episode_is_pending(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975020)
    async with api_factory() as session:
        episode = await session.get(Episode, ids[4])
        assert episode is not None
        episode.state = EpisodeState.DOWNLOADING
        session.add(Want(user_id=user.id, episode_id=ids[4], demanded_at=datetime.now(UTC)))
        session.add(
            Torrent(
                episode_id=ids[4],
                info_hash="d" * 40,
                qbit_state="downloading",
                progress=0.43,
                seeders=12,
            )
        )
        await session.commit()

    response = await client.get(f"/api/episodes/{ids[4]}/play")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["playlist_url"] is None
    assert body["offline_only"] is False
    assert body["pending"] == {"state": "downloading", "progress": 0.43, "seeders": 12}
    # Not worked out while this one is pending (the poll would pay for it).
    assert body["next"]["id"] == ids[5]
    assert body["next"]["fetchable"] is False


async def test_play_on_an_unwanted_episode_that_is_not_ready_is_still_a_404(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975021)

    response = await client.get(f"/api/episodes/{ids[8]}/play")

    assert response.status_code == 404


async def test_play_on_a_ready_episode_carries_no_pending(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975022)
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
        entry = await session.get(ListEntry, (user.id, episode.anime_id))
        assert entry is not None
        await session.commit()

    response = await client.get(f"/api/episodes/{ids[4]}/play")

    assert response.status_code == 200
    body = response.json()
    assert body["pending"] is None
    assert body["playlist_url"] == f"/media/{ids[4]}/index.m3u8"
    # Before half-way the next one is not covered, and demand would refuse it.
    assert body["next"]["fetchable"] is False
    assert (await client.post(f"/api/episodes/{ids[5]}/demand")).status_code == 409


async def test_next_becomes_fetchable_once_the_user_is_half_way(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """``fetchable`` and ``demand`` agree, on real progress (no simulated halfway)."""
    ids = await followed_show(api_factory, user.id, anilist_id=975023)
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
    report = await client.post(
        "/api/progress", json={"episode_id": ids[4], "position_s": 800.0, "duration_s": 1420.0}
    )
    assert report.status_code == 200

    body = (await client.get(f"/api/episodes/{ids[4]}/play")).json()

    assert body["next"]["fetchable"] is True
    assert (await client.post(f"/api/episodes/{ids[5]}/demand")).status_code == 202


async def delivered_trip(
    factory: SessionFactory, user_id: int, episode_id: int, *, demanded: bool
) -> None:
    """A delivered trip copy of ``episode_id`` and a live want on it (demanded or not)."""
    async with factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        now = datetime.now(UTC)
        trip = Trip(
            user_id=user_id,
            anime_id=episode.anime_id,
            first_number=episode.number,
            last_number=episode.number,
            count=1,
            deadline_at=now + timedelta(days=14),
        )
        session.add(trip)
        await session.flush()
        session.add(
            TripEpisode(
                trip_id=trip.id,
                episode_id=episode_id,
                state=TripEpisodeState.DELIVERED,
                delivered_at=now,
            )
        )
        session.add(
            Want(user_id=user_id, episode_id=episode_id, demanded_at=now if demanded else None)
        )
        await session.commit()


async def test_a_demand_outranks_the_trip_copy_on_play(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """The "watch it on the Mac" case: pending, not offline-only."""
    ids = await followed_show(api_factory, user.id, anilist_id=975030)
    await delivered_trip(api_factory, user.id, ids[4], demanded=True)

    body = (await client.get(f"/api/episodes/{ids[4]}/play")).json()

    assert body["offline_only"] is False
    assert body["pending"] is not None and body["playlist_url"] is None


async def test_without_a_demand_the_trip_copy_is_offline_only(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    ids = await followed_show(api_factory, user.id, anilist_id=975031)
    await delivered_trip(api_factory, user.id, ids[4], demanded=False)

    body = (await client.get(f"/api/episodes/{ids[4]}/play")).json()

    assert body["offline_only"] is True
    assert body["pending"] is None


@pytest.mark.parametrize("demanded", [True, False], ids=["pending", "offline_only"])
async def test_a_not_ready_episode_offers_no_subtitle_track(
    client: AsyncClient, user: User, api_factory: SessionFactory, demanded: bool
) -> None:
    """FR-S10: nothing to stream, so nothing to switch — even when a remux row
    with a text track is still lying around for the episode."""
    ids = await followed_show(api_factory, user.id, anilist_id=975040 + int(demanded))
    await delivered_trip(api_factory, user.id, ids[4], demanded=demanded)
    async with api_factory() as session:
        session.add(
            Rendition(
                episode_id=ids[4],
                dir=f"/data/renditions/{ids[4]}",
                playlist_path=f"/data/renditions/{ids[4]}/index.m3u8",
                duration=1420.0,
                kind="remux",
                text_subtitles=True,
                subtitle_lang="en",
            )
        )
        await session.commit()

    response = await client.get(f"/api/episodes/{ids[4]}/play")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["playlist_url"] is None
    if demanded:
        assert body["pending"] is not None
    else:
        assert body["offline_only"] is True
    assert body["subtitles"] is None
