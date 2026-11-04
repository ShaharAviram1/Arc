"""M21 end to end: demand on a completed show, a rewatch pass, the demand cap race (T10).

Three scenarios, each through the real API (a signed-in ``httpx`` client on the
real app) and the real job handlers (claimed and run as the worker does), with
Nyaa and qBittorrent stubbed as in :mod:`tests.slow.test_m20_flow` — never a
real torrent — and MyAnimeList linked so that "no write" is never vacuous.

1. **Demand on a completed show** (FR-A14, FR-T1). A ``completed`` list entry,
   every episode aired, nothing wanted. Play on episode 7 answers pending with
   ``demanded: false``; the demand answers 202 and ``demanded: true``; the
   search adds the single, the live remux (real ffmpeg) serves the growing
   playlist while it downloads, the download lands, is matched and adopted;
   the completion finishes it; the next ``compute_wants`` **deletes** its want
   (not shelved); retention keeps the files until the watched grace has run
   out and not a minute longer. ``mal_write_log`` has no row and no ``mal_*``
   job is queued at any point, and the list entry never changes. Episode 7
   rather than 3 because the Nyaa fixture and the cloned library file are
   episode 7's.
2. **A rewatch pass** (FR-W7). A show on the list at 1 / 3, every episode
   ready. ``PUT /rewatch`` opens a pass at position 0; the window then plans
   from episode 1 (the list window is suspended); episodes 1 → 3 are played
   to the mark through ``POST /api/progress``, each answered in the pass, and
   the window follows the pass; the pass ends on the last episode with
   ``rewatch_ended``; the credits of the last episode, and a report on another
   pass episode, inside ``TAIL_GRACE`` write nothing either — the list entry
   and the write log never change in the pass. Past ``TAIL_GRACE`` the same
   report is a new watch in normal mode: the list moves to 2 and the write
   log gets its row, so the bound is real.
3. **The cap race** (FR-A14). ``demand_max_open`` = 3: two open demands and
   five concurrent presses on fresh episodes → exactly one accepted; three
   open and five more concurrent presses → none accepted, every refusal a 409
   ``demand_cap`` listing the three open ones; one withdrawn → one more 202.

Scenario 1 needs real ffmpeg and a SubsPlease 1080p MKV under
``data/downloads`` (the M20 flow's sample) and skips without them; 2 and 3 use
no media at all (ready episodes are rows and a few bytes on disk), so they run
on a fresh checkout too.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select, update

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    REWATCH_END_FINISHED,
    Anime,
    Episode,
    EpisodeState,
    ListEntry,
    ListStatus,
    Rendition,
    Rewatch,
    User,
    Want,
    WatchProgress,
)
from arc.services.acquisition import jobs as acquisition_jobs  # noqa: F401  (handlers)
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition import qbit as qbit_module
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.demand import DEMAND_CAP, demand_cap_message
from arc.services.acquisition.names import COMPUTE_WANTS, POLL_QBIT, SEARCH_RELEASE
from arc.services.catalog import Breaker, CatalogService
from arc.services.library import jobs as library_jobs
from arc.services.library import prematch as prematch_module
from arc.services.library.names import MATCH_FILE
from arc.services.media import live as live_module
from arc.services.media import remux as remux_module
from arc.services.media.names import REMUX, REMUX_LIVE, STAGE_LIVE_DONE
from arc.services.media.remux import KEY_ADOPTED
from arc.services.media.transcode import reset_semaphore
from arc.services.playback.rewatch import TAIL_GRACE
from arc.services.retention import floor as retention_floor
from arc.services.retention import jobs as retention_jobs  # noqa: F401  (handlers)
from arc.services.retention.sweep import candidates
from tests.acquisition_helpers import (
    NyaaStub,
    QbitStub,
    acquisition_settings,
    fake_free_space,
    force_transport,
    make_anime,
    make_entry,
    make_episodes,
    set_setting,
)
from tests.anilist_mock import FRIEREN_ID, FakeAniList, frieren_fake
from tests.conftest import TEST_FERNET_KEY, add_user, api_transport, login
from tests.mal_helpers import link_user
from tests.mal_mock import frieren_fake as mal_frieren_fake
from tests.retention_helpers import write_rendition_dir, write_source_file
from tests.slow.test_m20_flow import (
    FEED,
    FILE_NAME,
    STEP_PIECES,
    SUBSPLEASE_1080,
    Download,
    enqueue_one,
    episode_row,
    mal_trace,
    run_next,
    segment_names,
)
from tests.test_live_slow import LIBRARY

pytestmark = [pytest.mark.slow, pytest.mark.pg]

EMAIL = "m21-flow@arc.test"
PASSWORD = "m21-flow-password"
GB = 1024**3
#: The watched grace (``watched_grace_hours``, default 6).
G = timedelta(hours=6)
MINUTE = timedelta(minutes=1)
#: A ready episode's length in the media-free scenarios.
LENGTH = 1420.0
#: Concurrent presses in the cap race, each from a client of its own.
RACERS = 5

#: The whole file must stay well inside the time a slow test is allowed.
BUDGET_SECONDS = 180


# --- Fixtures ----------------------------------------------------------------------


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    """A ``DATA_DIR`` of the test's own; login limits wide enough for the racers."""
    overrides: dict[str, Any] = {
        "database_url": test_database_url,
        "fernet_key": TEST_FERNET_KEY,
        "login_rate_limit_per_email": 50,
        "login_rate_limit_per_ip": 50,
    }
    real = shutil.which("ffmpeg")
    if real is not None:
        overrides["ffmpeg_bin"] = real
    return acquisition_settings(tmp_path / "data", **overrides)


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield http


@pytest.fixture
def disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plenty of disk: neither the demand's storage hold nor the floor acts."""
    fake_free_space(monkeypatch, acquisition_rules, 90 * GB)
    fake_free_space(monkeypatch, retention_floor, 90 * GB)


@pytest.fixture
def media_stubs(monkeypatch: pytest.MonkeyPatch, disk: None) -> tuple[NyaaStub, QbitStub]:
    """Nyaa and qBittorrent in memory, the catalogue faked, the waits shortened.

    The M20 flow's stubs; skipped without its sample file or ffmpeg.
    """
    if LIBRARY is None:
        pytest.skip("no SubsPlease 1080p MKV in data/downloads, or no ffmpeg")
    nyaa = NyaaStub({"Sousou no Frieren - 07": FEED})
    qbit = QbitStub()

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep)
    monkeypatch.setattr(
        nyaa_module.NyaaClient,
        "__init__",
        force_transport(nyaa_module.NyaaClient, nyaa.transport()),
    )
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, qbit.transport()),
    )

    anilist: FakeAniList = frieren_fake()
    anilist.search["sousou no frieren"] = anilist.search["frieren"]
    service = CatalogService(anilist.source(), mal_frieren_fake().source(), Breaker(300.0))

    class _Holder:
        async def __aenter__(self) -> CatalogService:
            return service

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(prematch_module, "catalog_for", lambda settings: _Holder())
    monkeypatch.setattr(library_jobs, "catalog_for", lambda settings: _Holder())

    reset_semaphore()
    monkeypatch.setattr(live_module, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(live_module, "PAYLOAD_SECONDS", 0.0)
    monkeypatch.setattr(remux_module, "LIVE_ADOPT_POLL_SECONDS", 0.02)
    return nyaa, qbit


@pytest.fixture
def qbit_stub(monkeypatch: pytest.MonkeyPatch, disk: None) -> QbitStub:
    """qBittorrent in memory for the media-free scenarios (nothing reaches it)."""
    qbit = QbitStub()
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, qbit.transport()),
    )
    return qbit


# --- Helpers ---------------------------------------------------------------------


async def compute(factory: SessionFactory, settings: Settings) -> None:
    """Queue one ``compute_wants`` and run it, as the worker would."""
    await enqueue_one(factory, COMPUTE_WANTS)
    await run_next(factory, settings, COMPUTE_WANTS)


async def live_wants(factory: SessionFactory, user_id: int) -> dict[int, Want]:
    """The user's live (not shelved) wants, by episode number."""
    async with factory() as session:
        rows = await session.execute(
            select(Episode.number, Want)
            .join(Want, Want.episode_id == Episode.id)
            .where(Want.user_id == user_id, Want.dropped_at.is_(None))
        )
        return {number: want for number, want in rows.all()}


async def want_row(factory: SessionFactory, user_id: int, episode_id: int) -> Want | None:
    async with factory() as session:
        found: Want | None = await session.get(Want, (user_id, episode_id))
        return found


def entry_facts(entry: ListEntry) -> tuple[Any, ...]:
    """Everything about a list entry that a write would move."""
    return (
        entry.status,
        entry.progress,
        entry.score,
        entry.mal_dirty,
        entry.updated_at,
        entry.updated_by,
        entry.activated_at,
    )


async def entry_now(factory: SessionFactory, user_id: int, anime_id: int) -> tuple[Any, ...]:
    async with factory() as session:
        entry = await session.get(ListEntry, (user_id, anime_id))
        assert entry is not None
        return entry_facts(entry)


async def assert_no_mal(factory: SessionFactory) -> None:
    """No ``mal_write_log`` row and no ``mal_*`` job: nothing for MyAnimeList."""
    assert await mal_trace(factory) == ([], [])


async def report(
    client: AsyncClient, episode_id: int, position_s: float, duration_s: float = LENGTH
) -> dict[str, Any]:
    response = await client.post(
        "/api/progress",
        json={"episode_id": episode_id, "position_s": position_s, "duration_s": duration_s},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def make_ready(factory: SessionFactory, settings: Settings, episode_id: int) -> None:
    """A playable episode without ffmpeg: its rows and a few bytes on disk."""
    async with factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        episode.state = EpisodeState.READY
        directory = write_rendition_dir(settings, episode_id)
        write_source_file(settings, episode_id)
        session.add(
            Rendition(
                episode_id=episode_id,
                dir=str(directory),
                playlist_path=str(directory / "index.m3u8"),
                duration=LENGTH,
                ready_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()


# --- 1. Demand on a completed show ------------------------------------------------


async def test_a_demand_on_a_completed_show_end_to_end(
    api_factory: SessionFactory,
    settings: Settings,
    client: AsyncClient,
    media_stubs: tuple[NyaaStub, QbitStub],
) -> None:
    started = monotonic()
    nyaa, qbit = media_stubs
    assert LIBRARY is not None

    # --- 0. A completed show, MyAnimeList linked, nothing wanted -----------------------
    user = await add_user(api_factory, EMAIL, PASSWORD)
    await link_user(api_factory, settings, user_id=user.id)
    async with api_factory() as session:
        anime = Anime(
            anilist_id=FRIEREN_ID,
            title_romaji="Sousou no Frieren",
            title_english="Frieren: Beyond Journey's End",
            status="FINISHED",
            format="TV",
            episodes=28,
            summary_source="anilist",
            detail_source="anilist",
            refreshed_at=datetime.now(UTC),
        )
        session.add(anime)
        await session.flush()
        episodes = await make_episodes(session, anime, 28)  # every one has aired
        owner = await session.get(User, user.id)
        assert owner is not None
        await make_entry(session, owner, anime, status=ListStatus.COMPLETED, progress=28)
        await set_setting(session, "preferred_groups", ["SubsPlease"])
        await session.commit()
        episode_id = episodes[6].id
        anime_id = anime.id
    entry_before = await entry_now(api_factory, user.id, anime_id)
    await login(client, EMAIL, PASSWORD)

    await compute(api_factory, settings)
    assert await live_wants(api_factory, user.id) == {}, "a completed show wants nothing"

    # --- 1. Play, then the demand -------------------------------------------------------
    play = await client.get(f"/api/episodes/{episode_id}/play")
    assert play.status_code == 200, play.text
    pending = play.json()["pending"]
    assert pending is not None and pending["demanded"] is False
    assert pending["state"] == EpisodeState.NOT_WANTED

    response = await client.post(f"/api/episodes/{episode_id}/demand")
    assert response.status_code == 202, response.text
    play = (await client.get(f"/api/episodes/{episode_id}/play")).json()
    assert play["pending"]["demanded"] is True
    demanded = await want_row(api_factory, user.id, episode_id)
    assert demanded is not None and demanded.demanded_at is not None
    assert demanded.dropped_at is None

    # --- 2. Searched once, added sequentially --------------------------------------------
    await run_next(api_factory, settings, SEARCH_RELEASE, episode_id=episode_id)
    assert nyaa.queries, "the fetch asked Nyaa"
    added = qbit.added[-1]
    assert SUBSPLEASE_1080 in added["urls"]
    assert added["sequentialDownload"] == "true"
    assert (await episode_row(api_factory, episode_id)).state is EpisodeState.DOWNLOADING

    source = settings.downloads_dir / str(episode_id) / FILE_NAME
    source.parent.mkdir(parents=True)
    cloned = subprocess.run(["cp", "-c", str(LIBRARY), str(source)], check=False)
    if cloned.returncode != 0:
        pytest.skip("cannot clone the library file (not APFS?)")
    source.chmod(0o444)
    download = Download(qbit, source, episode_id)
    await assert_no_mal(api_factory)

    # --- 3. Live play while it downloads --------------------------------------------------
    live_task = asyncio.create_task(
        run_next(api_factory, settings, REMUX_LIVE, episode_id=episode_id)
    )
    seen = 0
    deadline = monotonic() + 90
    while monotonic() < deadline:
        assert not live_task.done() or live_task.exception() is None
        playlist = await client.get(f"/media/{episode_id}/video.m3u8")
        if playlist.status_code == 200:
            assert "#EXT-X-PLAYLIST-TYPE:EVENT" in playlist.text
            seen = len(segment_names(playlist.text, "seg_"))
            if seen >= 2 and download.calls * STEP_PIECES >= download.cap:
                break
        await asyncio.sleep(0.2)
    assert seen >= 2, "the live playlist grew while downloading"
    play = (await client.get(f"/api/episodes/{episode_id}/play")).json()
    assert play["playlist_url"] == f"/media/{episode_id}/index.m3u8"
    assert play["live"] is not None and play["live"]["ended"] is False
    expected = play["live"]["expected_duration"]
    assert (await client.get(f"/media/{episode_id}/seg_00000.m4s")).status_code == 200
    watching = await report(client, episode_id, 120.0, expected)
    assert watching["completed"] is False and watching["rewatching"] is False
    await assert_no_mal(api_factory)

    # --- 4. Lands → matched → adopted: the ready playlist --------------------------------
    download.finish()
    live_job = await asyncio.wait_for(live_task, timeout=90)
    assert live_job.payload["stage"] == STAGE_LIVE_DONE
    await enqueue_one(api_factory, POLL_QBIT)
    await run_next(api_factory, settings, POLL_QBIT)
    await run_next(api_factory, settings, MATCH_FILE)
    remux_job = await run_next(api_factory, settings, REMUX, episode_id=episode_id)
    assert remux_job.payload[KEY_ADOPTED] is True
    assert (await episode_row(api_factory, episode_id)).state is EpisodeState.READY
    play = (await client.get(f"/api/episodes/{episode_id}/play")).json()
    assert play["live"] is None and play["pending"] is None
    duration = play["duration"]
    vod = await client.get(f"/media/{episode_id}/video.m3u8")
    assert vod.status_code == 200 and vod.text.rstrip().endswith("#EXT-X-ENDLIST")
    await assert_no_mal(api_factory)

    # --- 5. The completion finishes it, and writes nothing to MAL -------------------------
    finished = await report(client, episode_id, 0.95 * duration, duration)
    assert finished["completed"] is True and finished["rewatching"] is False
    assert finished["list_progress"] == 28, "never lowered, never moved"
    await assert_no_mal(api_factory)
    async with api_factory() as session:
        progress = await session.get(WatchProgress, (user.id, episode_id))
        assert progress is not None and progress.finished_at is not None
        finished_at = progress.finished_at

    # --- 6. The next compute_wants deletes the want: not shelved ---------------------------
    await compute(api_factory, settings)
    assert await want_row(api_factory, user.id, episode_id) is None, "deleted, not shelved"
    # The walk (FR-A1, M21): the look-ahead now counts from the finished episode.
    assert set(await live_wants(api_factory, user.id)) == {8}
    await assert_no_mal(api_factory)

    # --- 7. Retention: kept for the watched grace, deletable just past it -----------------
    async with api_factory() as session:
        early = await candidates(session, settings, now=finished_at + G - MINUTE)
        late = await candidates(session, settings, now=finished_at + G + MINUTE)
    assert episode_id not in {target.episode_id for target in early}
    assert episode_id in {target.episode_id for target in late}

    # --- 8. The list entry never moved ---------------------------------------------------
    assert await entry_now(api_factory, user.id, anime_id) == entry_before
    await assert_no_mal(api_factory)

    elapsed = monotonic() - started
    assert elapsed < BUDGET_SECONDS, f"the flow took {elapsed:.0f}s"


# --- 2. A rewatch pass -------------------------------------------------------------


async def test_a_rewatch_pass_end_to_end(
    api_factory: SessionFactory,
    settings: Settings,
    client: AsyncClient,
    qbit_stub: QbitStub,
) -> None:
    user = await add_user(api_factory, EMAIL, PASSWORD)
    await link_user(api_factory, settings, user_id=user.id)
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=982101, status="FINISHED", episodes=3)
        episodes = await make_episodes(session, anime, 3)
        owner = await session.get(User, user.id)
        assert owner is not None
        # On the list at 1 / 3: a normal-mode finish of 2 or 3 *would* write.
        await make_entry(session, owner, anime, status=ListStatus.WATCHING, progress=1)
        await set_setting(session, "look_ahead_n", 2)
        await session.commit()
        anime_id = anime.id
        ids = {row.number: row.id for row in episodes}
    for episode_id in ids.values():
        await make_ready(api_factory, settings, episode_id)
    entry_before = await entry_now(api_factory, user.id, anime_id)
    await login(client, EMAIL, PASSWORD)

    await compute(api_factory, settings)
    assert set(await live_wants(api_factory, user.id)) == {2, 3}, "the list window, N = 2"

    # --- On: position 0, the window from episode 1, the list window suspended ---------
    opened = await client.put(f"/api/anime/{anime_id}/rewatch")
    assert opened.status_code == 200, opened.text
    assert opened.json()["position"] == 0 and opened.json()["total"] == 3
    await compute(api_factory, settings)
    assert set(await live_wants(api_factory, user.id)) == {1, 2}, "N = 2 from the pass"

    # --- Episodes 1 → 3 to the mark, each in the pass --------------------------------
    for number in (1, 2, 3):
        half = await report(client, ids[number], 0.5 * LENGTH)
        assert half["rewatching"] is True and half["completed"] is False
        done = await report(client, ids[number], 0.95 * LENGTH)
        assert done["rewatching"] is True
        assert done["completed"] is True and done["newly_completed"] is True
        assert done["list_progress"] is None, "the list is not touched in a pass"
        assert done["rewatch_ended"] is (number == 3)
        assert await entry_now(api_factory, user.id, anime_id) == entry_before
        await assert_no_mal(api_factory)
        if number < 3:
            await compute(api_factory, settings)
            assert set(await live_wants(api_factory, user.id)) == set(
                range(number + 1, min(number + 2, 3) + 1)
            ), f"the window follows the pass after episode {number}"
            assert await want_row(api_factory, user.id, ids[number]) is None, (
                "a finished pass episode's want is deleted, not shelved"
            )

    # --- The pass ended on the last episode ---------------------------------------------
    async with api_factory() as session:
        passes = list(
            (await session.scalars(select(Rewatch).where(Rewatch.user_id == user.id))).all()
        )
        assert len(passes) == 1
        assert passes[0].ended_at is not None and passes[0].end_reason == REWATCH_END_FINISHED
        rows = (
            await session.scalars(select(WatchProgress).where(WatchProgress.user_id == user.id))
        ).all()
        # A pass never sets the first-watch flag; it stamps the latest finish.
        assert all(not row.completed and row.finished_at is not None for row in rows)
    off = await client.get(f"/api/anime/{anime_id}")
    assert off.status_code == 200 and off.json()["rewatch"] is None

    # --- The credits of the last episode, inside TAIL_GRACE: still nothing -------------
    tail = await report(client, ids[3], 0.98 * LENGTH)
    assert tail["rewatching"] is True and tail["list_progress"] is None
    assert tail["newly_completed"] is False
    async with api_factory() as session:
        last = await session.get(WatchProgress, (user.id, ids[3]))
        assert last is not None and last.completed is False
    assert await entry_now(api_factory, user.id, anime_id) == entry_before
    await assert_no_mal(api_factory)

    # --- Another pass episode inside TAIL_GRACE is still the pass's tail ----------------
    other = await report(client, ids[2], 0.97 * LENGTH)
    assert other["rewatching"] is True and other["list_progress"] is None
    assert other["newly_completed"] is False
    assert await entry_now(api_factory, user.id, anime_id) == entry_before
    await assert_no_mal(api_factory)

    # --- Past TAIL_GRACE it is a new watch, in normal mode: the list and MAL move -------
    shift = TAIL_GRACE + timedelta(hours=1)
    async with api_factory() as session:
        await session.execute(
            update(Rewatch)
            .where(Rewatch.user_id == user.id)
            .values(started_at=Rewatch.started_at - shift, ended_at=Rewatch.ended_at - shift)
        )
        await session.execute(
            update(WatchProgress)
            .where(WatchProgress.user_id == user.id)
            .values(
                finished_at=WatchProgress.finished_at - shift,
                updated_at=WatchProgress.updated_at - shift,
            )
        )
        await session.commit()
    normal = await report(client, ids[2], 0.98 * LENGTH)
    assert normal["rewatching"] is False
    assert normal["newly_completed"] is True and normal["list_progress"] == 2
    assert await entry_now(api_factory, user.id, anime_id) != entry_before
    async with api_factory() as session:
        entry = await session.get(ListEntry, (user.id, anime_id))
        assert entry is not None and entry.progress == 2 and entry.mal_dirty is True
    writes, mal_jobs = await mal_trace(api_factory)
    assert [(row.field, row.old_value, row.new_value) for row in writes] == [("progress", 1, 2)]
    assert mal_jobs, "the normal-mode completion queued its push"


# --- 3. The cap race ---------------------------------------------------------------


async def test_the_demand_cap_holds_under_concurrent_presses(
    api_factory: SessionFactory,
    api_app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    qbit_stub: QbitStub,
) -> None:
    user = await add_user(api_factory, EMAIL, PASSWORD)
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=982102, status="FINISHED", episodes=16)
        episodes = await make_episodes(session, anime, 16)
        await set_setting(session, "demand_max_open", 3)
        await session.commit()
        ids = [row.id for row in episodes]
    await login(client, EMAIL, PASSWORD)

    async def press(racer: AsyncClient, episode_id: int) -> Response:
        return await racer.post(f"/api/episodes/{episode_id}/demand")

    def refused_with_the_three(response: Response, open_ids: list[int]) -> None:
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == DEMAND_CAP
        assert detail["message"] == demand_cap_message(3)
        assert sorted(row["episode_id"] for row in detail["open"]) == sorted(open_ids)

    async def open_demands() -> list[int]:
        async with api_factory() as session:
            rows = await session.scalars(
                select(Want.episode_id).where(
                    Want.user_id == user.id,
                    Want.dropped_at.is_(None),
                    Want.demanded_at.is_not(None),
                )
            )
            return sorted(rows.all())

    for episode_id in ids[:2]:
        assert (await press(client, episode_id)).status_code == 202

    racers: list[AsyncClient] = []
    try:
        for _ in range(RACERS):
            racer = api_transport(api_app)
            racers.append(racer)
            await login(racer, EMAIL, PASSWORD)

        # One place left, five presses at once: exactly one gets it.
        fresh = ids[2 : 2 + RACERS]
        answers = await asyncio.gather(
            *(press(racer, episode_id) for racer, episode_id in zip(racers, fresh, strict=True))
        )
        accepted = [ep for ep, got in zip(fresh, answers, strict=True) if got.status_code == 202]
        assert len(accepted) == 1, [got.status_code for got in answers]
        open_now = await open_demands()
        assert open_now == sorted([*ids[:2], *accepted])
        for got in answers:
            if got.status_code != 202:
                refused_with_the_three(got, open_now)

        # Full: five more at once, none accepted beyond the cap.
        more = ids[2 + RACERS : 2 + 2 * RACERS]
        answers = await asyncio.gather(
            *(press(racer, episode_id) for racer, episode_id in zip(racers, more, strict=True))
        )
        for got in answers:
            refused_with_the_three(got, open_now)
        assert await open_demands() == open_now
    finally:
        for racer in racers:
            await racer.aclose()

    # Withdraw one: one more press is accepted.
    withdrawn = await client.delete(f"/api/episodes/{open_now[0]}/demand")
    assert withdrawn.status_code == 204
    again = await press(client, ids[-1])
    assert again.status_code == 202, again.text
    assert await open_demands() == sorted([*open_now[1:], ids[-1]])
