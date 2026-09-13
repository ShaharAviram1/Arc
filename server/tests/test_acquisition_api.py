"""The acquisition API: the show page's fields and the admin controls (FR-A7).

``EpisodeOut`` is what the client renders an acquisition badge from, so the
tests assert on the JSON rather than on the schema object: the shape is the
contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from arc.api import acquisition as acquisition_api
from arc.db import SessionFactory
from arc.models import EpisodeState, Job, ListEntry, ListStatus, Torrent, User, UserRole, Want
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    COMPUTE_WANTS_PRIORITY,
    POLL_QBIT,
    SEARCH_RELEASE,
)
from arc.services.acquisition.rules import BYTES_PER_GB, PAUSED_KEY, is_paused
from tests.acquisition_helpers import (
    fake_free_space,
    make_anime,
    make_entry,
    make_episodes,
    make_user,
)
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


async def show_with_entry(
    factory: SessionFactory,
    *,
    anilist_id: int,
    status: str,
    activated: bool,
    email: str,
) -> int:
    """A show with one list entry belonging to ``email``. Returns the anime id."""
    await add_user(factory, email, USER_PASSWORD)
    async with factory() as session:
        anime = await make_anime(session, anilist_id=anilist_id, status=status)
        await make_episodes(session, anime, 6, aired_through=6)
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        entry = ListEntry(
            user_id=user.id,
            anime_id=anime.id,
            status=ListStatus.WATCHING,
            progress=0,
            activated_at=datetime.now(UTC) if activated else None,
        )
        session.add(entry)
        await session.commit()
        return anime.id


# --- ListEntryOut: dormant imports (FR-A9) ----------------------------------


async def test_the_show_page_says_an_imported_entry_is_dormant(
    api_app, api_factory: SessionFactory
) -> None:
    """What the hero's note and its "Fetch this show" button read (FR-A9)."""
    email = "dormant-show@arc.test"
    anime_id = await show_with_entry(
        api_factory, anilist_id=963020, status="FINISHED", activated=False, email=email
    )

    async with api_transport(api_app) as client:
        await login(client, email, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    assert body["list_entry"]["dormant"] is True
    assert body["list_entry"]["activated_at"] is None


async def test_an_airing_show_is_never_dormant(api_app, api_factory: SessionFactory) -> None:
    """FR-A9's exception, derived rather than stored: nothing is written when a
    show starts broadcasting, and the note disappears anyway."""
    email = "airing-show@arc.test"
    anime_id = await show_with_entry(
        api_factory, anilist_id=963021, status="RELEASING", activated=False, email=email
    )

    async with api_transport(api_app) as client:
        await login(client, email, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    assert body["list_entry"]["dormant"] is False
    assert body["list_entry"]["activated_at"] is None


async def test_a_touched_entry_is_not_dormant(api_app, api_factory: SessionFactory) -> None:
    email = "touched-show@arc.test"
    anime_id = await show_with_entry(
        api_factory, anilist_id=963022, status="FINISHED", activated=True, email=email
    )

    async with api_transport(api_app) as client:
        await login(client, email, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    assert body["list_entry"]["dormant"] is False
    assert body["list_entry"]["activated_at"] is not None


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


# --- The search diagnostic (FR-A7, 2026-09-14) ------------------------------


async def test_a_searching_episode_carries_what_the_last_search_asked_and_saw(
    api_app, api_factory: SessionFactory
) -> None:
    """The row's own sentence — "6 forms, 0 results, next try 23:26" — as JSON.

    ``next_at`` is the queued job's ``run_after`` and not a column: FR-A6's
    retry schedule lives in the job row, so a row that says ``Searching`` has
    nothing else to read "Arc will look again at" off.
    """
    searched_at = datetime(2026, 9, 14, 17, 20, tzinfo=UTC)
    next_at = datetime(2026, 9, 14, 23, 26, tzinfo=UTC)
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=963010)
        episodes = await make_episodes(session, anime, 4, aired_through=4)
        episode = episodes[1]
        episode.state = EpisodeState.SEARCHING
        episode.last_search_at = searched_at
        episode.last_search_forms = 6
        episode.last_search_results = 0
        session.add(
            Job(
                type=SEARCH_RELEASE,
                payload={"episode_id": episode.id},
                run_after=next_at,
            )
        )
        await session.commit()
        anime_id, episode_id = anime.id, episode.id
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    row = next(item for item in body["episodes"] if item["id"] == episode_id)
    assert row["search"]["forms"] == 6
    assert row["search"]["results"] == 0
    assert row["search"]["at"].startswith("2026-09-14T17:20")
    assert row["search"]["next_at"].startswith("2026-09-14T23:26")
    others = [item for item in body["episodes"] if item["id"] != episode_id]
    assert all(item["search"] is None for item in others), "never searched, so nothing to say"


async def test_a_ready_episode_says_nothing_about_its_old_search(
    api_app, api_factory: SessionFactory
) -> None:
    """The file is here: what the search did three days ago is history."""
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=963011)
        episodes = await make_episodes(session, anime, 4, aired_through=4)
        episode = episodes[1]
        episode.state = EpisodeState.READY
        episode.last_search_at = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
        episode.last_search_forms = 8
        episode.last_search_results = 20
        await session.commit()
        anime_id, episode_id = anime.id, episode.id
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    row = next(item for item in body["episodes"] if item["id"] == episode_id)
    assert row["search"] is None


async def test_an_unavailable_episode_keeps_its_search_summary(
    api_app, api_factory: SessionFactory
) -> None:
    """The state somebody is most likely to be staring at (FR-A6's daily retry)."""
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=963012)
        episodes = await make_episodes(session, anime, 4, aired_through=4)
        episode = episodes[1]
        episode.state = EpisodeState.UNAVAILABLE
        episode.unavailable_reason = "no acceptable release found"
        episode.last_search_at = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
        episode.last_search_forms = 10
        episode.last_search_results = 3
        await session.commit()
        anime_id, episode_id = anime.id, episode.id
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    row = next(item for item in body["episodes"] if item["id"] == episode_id)
    assert row["search"] == {
        "at": row["search"]["at"],
        "forms": 10,
        "results": 3,
        "next_at": None,
    }
    assert row["unavailable_reason"] == "no acceptable release found"


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


# --- The pause switch -------------------------------------------------------
#
# ``settings`` is not in ``CLEANUP_TABLES`` (conftest) — it is seeded by the
# initial migration and compared key-for-key by ``test_migrations`` — so a test
# that pauses acquisition has to put the row back itself. The fixture is what
# makes that unmissable rather than something each test remembers.


@pytest.fixture
async def restore_pause(pg_engine: AsyncEngine) -> AsyncIterator[None]:
    """Leave ``acquisition_paused`` false however the test ends."""
    try:
        yield
    finally:
        async with pg_engine.begin() as connection:
            await connection.execute(
                text("UPDATE settings SET value = 'false'::jsonb WHERE key = :key"),
                {"key": PAUSED_KEY},
            )


async def test_a_non_admin_cannot_pause_acquisition(api_app, api_factory: SessionFactory) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD, role=UserRole.USER)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        assert (await client.post("/api/acquisition/pause")).status_code == 403
        assert (await client.get("/api/acquisition/status")).status_code == 403


async def test_an_anonymous_caller_cannot_read_the_status(api_client: AsyncClient) -> None:
    assert (await api_client.get("/api/acquisition/status")).status_code == 401
    assert (await api_client.post("/api/acquisition/pause")).status_code == 401
    assert (await api_client.post("/api/acquisition/resume")).status_code == 401


async def test_pausing_writes_the_setting_and_says_so(
    admin_client: AsyncClient, api_factory: SessionFactory, restore_pause: None
) -> None:
    response = await admin_client.post("/api/acquisition/pause")

    assert response.status_code == 200
    assert response.json() == {"paused": True}
    async with api_factory() as session:
        assert await is_paused(session) is True


async def test_pausing_twice_is_the_same_as_pausing_once(
    admin_client: AsyncClient, restore_pause: None
) -> None:
    await admin_client.post("/api/acquisition/pause")

    assert (await admin_client.post("/api/acquisition/pause")).json() == {"paused": True}


async def test_resuming_clears_the_setting_and_queues_a_recompute(
    admin_client: AsyncClient, api_factory: SessionFactory, restore_pause: None
) -> None:
    await admin_client.post("/api/acquisition/pause")

    response = await admin_client.post("/api/acquisition/resume")

    assert response.status_code == 200
    assert response.json() == {"paused": False}
    async with api_factory() as session:
        assert await is_paused(session) is False
        rows = await session.scalars(select(Job).where(Job.type == COMPUTE_WANTS))
        queued = list(rows.all())
    assert len(queued) == 1, "resuming acts on the window rather than waiting for the tick"
    assert queued[0].priority == COMPUTE_WANTS_PRIORITY


async def test_the_status_reports_the_flag_and_what_it_is_holding(
    admin_client: AsyncClient,
    api_factory: SessionFactory,
    restore_pause: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The disk is stubbed so the two new figures are the same on every machine
    # (FR-T6). 50 GB free against the seeded 10 GB floor is "not held".
    fake_free_space(monkeypatch, acquisition_api, 50 * BYTES_PER_GB)
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=963007)
        episodes = await make_episodes(session, anime, 6, aired_through=6)
        user = await make_user(session, "status@arc.test")
        await make_entry(session, user, anime, progress=2)
        episodes[2].state = EpisodeState.SEARCHING
        episodes[3].state = EpisodeState.DOWNLOADING
        session.add(Want(user_id=user.id, episode_id=episodes[2].id))
        session.add(Want(user_id=user.id, episode_id=episodes[3].id))
        # Dropped wants are not live and must not be counted.
        session.add(
            Want(
                user_id=user.id,
                episode_id=episodes[4].id,
                dropped_at=datetime.now(UTC),
                drop_reason="unwatched",
            )
        )
        await session.commit()

    await admin_client.post("/api/acquisition/pause")
    body = (await admin_client.get("/api/acquisition/status")).json()

    assert body == {
        "paused": True,
        "storage_held": False,
        "free_bytes": 50 * BYTES_PER_GB,
        "min_free_bytes": 10 * BYTES_PER_GB,
        "active_wants": 2,
        "searching": 1,
        "downloading": 1,
        "dormant_entries": 0,
        "waiting_shows": 0,
        "slot_cap_k": 5,
        # Nothing is ``ready`` and nothing has a size, so M10's disk figure is
        # zero here; :mod:`tests.test_retention_api` is where it is exercised.
        "retained_bytes": 0,
    }


async def test_the_status_of_an_idle_unpaused_arc(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_free_space(monkeypatch, acquisition_api, 50 * BYTES_PER_GB)

    assert (await admin_client.get("/api/acquisition/status")).json() == {
        "paused": False,
        "storage_held": False,
        "free_bytes": 50 * BYTES_PER_GB,
        "min_free_bytes": 10 * BYTES_PER_GB,
        "active_wants": 0,
        "searching": 0,
        "downloading": 0,
        "dormant_entries": 0,
        "waiting_shows": 0,
        "slot_cap_k": 5,
        "retained_bytes": 0,
    }


async def test_the_status_says_when_the_disk_is_holding_acquisition(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-T6, from the same rule and the same measurement as the guard itself.

    A status page that could disagree with the guard would be worse than no
    status page.
    """
    fake_free_space(monkeypatch, acquisition_api, 1 * BYTES_PER_GB)

    body = (await admin_client.get("/api/acquisition/status")).json()

    assert body["storage_held"] is True
    assert body["free_bytes"] == 1 * BYTES_PER_GB
    assert body["min_free_bytes"] == 10 * BYTES_PER_GB
    assert body["paused"] is False, "a hold is not a pause; nobody pressed anything"


async def test_an_unmeasurable_disk_reads_as_zero_and_not_held(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly as the guard treats it: no answer is not a small answer."""
    fake_free_space(monkeypatch, acquisition_api, None)

    body = (await admin_client.get("/api/acquisition/status")).json()

    assert body["free_bytes"] == 0
    assert body["storage_held"] is False


async def test_the_status_counts_dormant_imports(
    admin_client: AsyncClient, api_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-A9's figure: what an admin looks at after a MyAnimeList import.

    Three entries, one of each kind: never touched on a finished show (dormant),
    never touched on an airing one (the exception), and touched (live).
    """
    fake_free_space(monkeypatch, acquisition_api, 50 * BYTES_PER_GB)
    async with api_factory() as session:
        finished = await make_anime(session, anilist_id=963010, status="FINISHED")
        airing = await make_anime(session, anilist_id=963011, status="RELEASING")
        touched = await make_anime(session, anilist_id=963012, status="FINISHED")
        user = await make_user(session, "dormant-count@arc.test")
        await make_entry(session, user, finished, activated=False)
        await make_entry(session, user, airing, activated=False)
        await make_entry(session, user, touched)
        # On hold is not watching/planned, so it is not an import waiting to be
        # woken up — it is FR-W4's "no wants" for a different reason.
        held_show = await make_anime(session, anilist_id=963013, status="FINISHED")
        await make_entry(session, user, held_show, status=ListStatus.ON_HOLD, activated=False)
        await session.commit()

    body = (await admin_client.get("/api/acquisition/status")).json()

    assert body["dormant_entries"] == 1


async def test_the_status_counts_the_shows_waiting_for_a_slot(
    admin_client: AsyncClient, api_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-A10's figure, and the fourth reason acquisition can look idle.

    Seven activated shows under the default cap of five: five may fetch and two
    wait. The count is the reconciler's own, so the panel cannot say "nothing
    is waiting" while the next tick holds two shows back.
    """
    fake_free_space(monkeypatch, acquisition_api, 50 * BYTES_PER_GB)
    async with api_factory() as session:
        user = await make_user(session, "waiting-count@arc.test")
        for index in range(7):
            anime = await make_anime(
                session, anilist_id=963020 + index, romaji=f"Waiting {index}", status="FINISHED"
            )
            await make_episodes(session, anime, 4, aired_through=4)
            await make_entry(session, user, anime, status=ListStatus.PLANNED)
        await session.commit()

    body = (await admin_client.get("/api/acquisition/status")).json()

    assert body["waiting_shows"] == 2
    assert body["slot_cap_k"] == 5


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
