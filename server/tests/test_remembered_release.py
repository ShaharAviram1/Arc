"""The remembered release, the fast dead-swarm fallback, the seeder warning (M20 T9).

FR-T3: retention keeps a single's ``torrents`` row as ``evicted`` instead of
deleting it. FR-A6: the next want's ``search_release`` re-adds that release
before asking Nyaa anything, and a re-fetch whose swarm the tracker says is
empty is given up on after ``REFETCH_DEAD_MINUTES`` and searched for again at
once. FR-A16: an episode whose live torrent has fewer than ``min_seeders_warn``
seeders says so on its row.

Nyaa and qBittorrent are the in-memory stubs every acquisition test uses.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.api.anime_schemas import EpisodeOut
from arc.api.episode_extras import EpisodeRelease, few_seeders
from arc.db import SessionFactory
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    Torrent,
    TorrentFile,
    TorrentKind,
    Want,
)
from arc.services.acquisition.jobs import (
    REFETCH_DEAD_MINUTES,
    poll_qbit,
    refetch_stall_reason,
    search_release,
)
from arc.services.acquisition.names import SEARCH_RELEASE, SEARCH_RELEASE_PRIORITY
from arc.services.acquisition.qbit import (
    DECIDED_STATES,
    DELETE_ON_SIGHT,
    QBIT_EVICTED,
    QBIT_MISSING,
    QBIT_STALLED,
    TorrentInfo,
)
from arc.services.acquisition.states import transition
from arc.services.retention.delete import (
    REMEMBER_DAYS,
    delete_episode_files,
    prune_remembered,
    remembers,
)
from arc.services.retention.jobs import retention_sweep
from arc.services.retention.sweep import targets_for_episode
from arc.services.trips.names import TRIP_SEARCH_PRIORITY
from tests.acquisition_helpers import (
    acquisition_settings,
    make_anime,
    make_episodes,
    set_setting,
)
from tests.conftest import add_user, api_transport, login
from tests.retention_helpers import days_ago, make_retained_episode
from tests.test_acquisition_jobs import (
    BATCH_HASH,
    SUBSPLEASE_1080,
    Wired,
    _finished,
    _pack_names,
    context,
    wire,
)
from tests.test_retention import HASH, qbit_stub
from tests.test_retention import context as sweep_context

pytestmark = pytest.mark.pg

MINUTE = 60


# --- helpers ----------------------------------------------------------------


async def fetched_then_evicted(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
) -> tuple[Wired, Episode, Torrent]:
    """Searched (one Nyaa round), downloaded, ready, then swept by retention."""
    await set_setting(session, "preferred_groups", ["SubsPlease"])
    wired, episode = await wire(session, monkeypatch, tmp_path, anilist_id=anilist_id, email=email)
    await search_release(context(session, wired.settings, {"episode_id": episode.id}))
    torrent = await session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None and torrent.info_hash == SUBSPLEASE_1080
    assert wired.nyaa.queries, "the first fetch asked Nyaa"

    # Finished and prepared, as far as retention is concerned.
    wired.qbit.add_torrent(torrent.info_hash, progress=1.0, state="stoppedUP")
    torrent.progress = 1.0
    torrent.completed_at = datetime.now(UTC)
    episode.state = EpisodeState.READY
    await session.flush()

    targets = await targets_for_episode(session, wired.settings, episode.id)
    removed = await delete_episode_files(session, wired.settings, episode, targets)
    assert removed.acted
    assert episode.state is EpisodeState.NOT_WANTED
    return wired, episode, torrent


async def want_again(session: AsyncSession, episode: Episode) -> None:
    """What the reconciler does for a want that came back: ``not_wanted → wanted``."""
    transition(episode, EpisodeState.WANTED, reason="a user wants this episode")
    await session.flush()


def client_row(info_hash: str, **overrides: object) -> TorrentInfo:
    fields: dict[str, object] = {
        "hash": info_hash,
        "name": "release.mkv",
        "progress": 0.0,
        "state": "stalledDL",
        "time_active": 16 * MINUTE,
        "num_complete": 0,
        "num_incomplete": 0,
    }
    fields |= overrides
    return TorrentInfo(**fields)  # type: ignore[arg-type]


# --- qbit.py ----------------------------------------------------------------


def test_evicted_is_decided_but_not_deleted_on_sight() -> None:
    assert QBIT_EVICTED in DECIDED_STATES
    assert QBIT_EVICTED not in DELETE_ON_SIGHT


# --- retention keeps the row ------------------------------------------------


async def test_sweeping_a_single_keeps_its_row_as_the_remembered_release(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    stub = qbit_stub(monkeypatch)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=979001, ready_at=days_ago(30), info_hash=HASH
    )
    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    torrent.completed_at = days_ago(30)
    await db_session.flush()

    await retention_sweep(sweep_context(db_session, settings))

    assert stub.deleted == [{"hashes": HASH, "deleteFiles": "true"}], "files deleted"
    assert episode.state is EpisodeState.NOT_WANTED
    kept = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert kept is not None
    assert kept.qbit_state == QBIT_EVICTED
    assert kept.evicted_at is not None
    assert kept.progress == 0.0
    assert kept.completed_at is None


@pytest.mark.parametrize("state", sorted(DECIDED_STATES - {QBIT_EVICTED}))
async def test_a_decided_row_is_still_deleted(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """A stalled, rejected, cancelled or unreadable release is not worth remembering."""
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch)
    anilist_id = 979010 + sorted(DECIDED_STATES).index(state)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=anilist_id, ready_at=days_ago(30), info_hash=HASH
    )
    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    torrent.qbit_state = state
    await db_session.flush()

    await retention_sweep(sweep_context(db_session, settings))

    assert episode.state is EpisodeState.NOT_WANTED
    assert await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id)) is None


# --- search_release re-adds it ----------------------------------------------


async def test_the_next_want_re_adds_the_same_hash_without_asking_nyaa(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wired, episode, torrent = await fetched_then_evicted(
        db_session, monkeypatch, tmp_path, anilist_id=979020, email="t9-readd@arc.test"
    )
    assert torrent.qbit_state == QBIT_EVICTED
    asked = list(wired.nyaa.queries)
    adds = len(wired.qbit.added)
    await want_again(db_session, episode)

    with caplog.at_level(logging.INFO):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries == asked, "zero Nyaa requests on a remembered re-fetch"
    assert wired.nyaa.fetched == []
    assert len(wired.qbit.added) == adds + 1
    assert SUBSPLEASE_1080 in wired.qbit.added[-1]["urls"]
    assert wired.qbit.added[-1]["savepath"] == f"/data/downloads/{episode.id}"
    assert episode.state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state == "added"
    assert torrent.progress == 0.0
    assert torrent.evicted_at is not None, "kept: it marks the download as a re-fetch"
    rows = (await db_session.scalars(select(Torrent).where(Torrent.episode_id == episode.id))).all()
    assert len(rows) == 1, "the same row, not a second one"
    assert "re-fetching the remembered release" in caplog.text


async def test_a_remembered_hash_the_client_still_holds_is_handled(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The client never finished the eviction's delete: its add answers 409."""
    wired, episode, torrent = await fetched_then_evicted(
        db_session, monkeypatch, tmp_path, anilist_id=979021, email="t9-held@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.0, state="stalledDL")
    asked = list(wired.nyaa.queries)
    await want_again(db_session, episode)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries == asked
    assert episode.state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state == "added"


async def test_a_remembered_row_with_no_magnet_is_re_added_from_its_hash(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await fetched_then_evicted(
        db_session, monkeypatch, tmp_path, anilist_id=979022, email="t9-magnet@arc.test"
    )
    torrent.magnet = None
    await want_again(db_session, episode)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.added[-1]["urls"] == f"magnet:?xt=urn:btih:{SUBSPLEASE_1080}"
    assert episode.state is EpisodeState.DOWNLOADING


async def test_a_paused_search_does_not_re_add_the_remembered_release(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await fetched_then_evicted(
        db_session, monkeypatch, tmp_path, anilist_id=979023, email="t9-paused@arc.test"
    )
    await set_setting(db_session, "acquisition_paused", True)
    adds = len(wired.qbit.added)
    await want_again(db_session, episode)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert len(wired.qbit.added) == adds
    assert torrent.qbit_state == QBIT_EVICTED
    assert episode.state is EpisodeState.WANTED


# --- poll_qbit --------------------------------------------------------------


async def test_poll_never_marks_an_evicted_row_missing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await fetched_then_evicted(
        db_session, monkeypatch, tmp_path, anilist_id=979030, email="t9-missing@arc.test"
    )
    assert wired.qbit.torrents == []

    for _ in range(2):
        await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == QBIT_EVICTED
    assert torrent.qbit_state != QBIT_MISSING
    assert episode.state is EpisodeState.NOT_WANTED


async def refetching(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
    priority: int = SEARCH_RELEASE_PRIORITY,
) -> tuple[Wired, Episode, Torrent]:
    """A remembered release re-added, with the search job that did it on record."""
    wired, episode, torrent = await fetched_then_evicted(
        session, monkeypatch, tmp_path, anilist_id=anilist_id, email=email
    )
    await want_again(session, episode)
    session.add(
        Job(
            type=SEARCH_RELEASE,
            payload={"episode_id": episode.id},
            status=JobStatus.DONE,
            priority=priority,
        )
    )
    await session.flush()
    await search_release(context(session, wired.settings, {"episode_id": episode.id}))
    assert episode.state is EpisodeState.DOWNLOADING
    return wired, episode, torrent


async def queued_searches(session: AsyncSession, episode_id: int) -> list[Job]:
    rows = await session.scalars(
        select(Job)
        .where(Job.type == SEARCH_RELEASE, Job.status == JobStatus.PENDING)
        .order_by(Job.id)
    )
    return [job for job in rows.all() if job.payload.get("episode_id") == episode_id]


async def test_a_dead_re_fetch_at_minute_16_is_stalled_and_searched_at_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await refetching(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=979040,
        email="t9-dead@arc.test",
        priority=TRIP_SEARCH_PRIORITY,
    )
    wired.qbit.torrents.clear()
    wired.qbit.add_torrent(
        torrent.info_hash,
        state="stalledDL",
        time_active=16 * MINUTE,
        num_complete=0,
        num_incomplete=0,
    )
    before = datetime.now(UTC)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == QBIT_STALLED
    assert wired.qbit.deleted[-1] == {"hashes": torrent.info_hash, "deleteFiles": "true"}
    assert episode.state is EpisodeState.WANTED, "not unavailable: a search is queued"
    assert episode.unavailable_reason is None
    searches = await queued_searches(db_session, episode.id)
    assert len(searches) == 1
    assert searches[0].priority == TRIP_SEARCH_PRIORITY, "the same priority as before"
    assert searches[0].run_after is None or searches[0].run_after <= before + timedelta(
        seconds=5
    ), "at once, not in six hours"


async def test_a_dead_re_fetch_before_minute_15_is_left_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await refetching(
        db_session, monkeypatch, tmp_path, anilist_id=979041, email="t9-young@arc.test"
    )
    wired.qbit.torrents.clear()
    wired.qbit.add_torrent(
        torrent.info_hash,
        state="stalledDL",
        time_active=10 * MINUTE,
        num_complete=0,
        num_incomplete=0,
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == "stalledDL"
    assert episode.state is EpisodeState.DOWNLOADING
    assert await queued_searches(db_session, episode.id) == []


async def test_a_first_fetch_with_a_dead_swarm_keeps_the_six_hour_rule(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only a re-fetch is on the fast clock; a fresh pick waits as before."""
    await set_setting(db_session, "preferred_groups", ["SubsPlease"])
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=979042, email="t9-first@arc.test"
    )
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None and torrent.evicted_at is None
    wired.qbit.add_torrent(
        torrent.info_hash,
        state="stalledDL",
        time_active=16 * MINUTE,
        num_complete=0,
        num_incomplete=0,
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state == "stalledDL"


async def test_after_the_fallback_the_search_skips_the_dead_hash(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await refetching(
        db_session, monkeypatch, tmp_path, anilist_id=979043, email="t9-skip@arc.test"
    )
    dead = torrent.info_hash
    wired.qbit.torrents.clear()
    wired.qbit.add_torrent(
        dead, state="stalledDL", time_active=16 * MINUTE, num_complete=0, num_incomplete=0
    )
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    assert episode.state is EpisodeState.WANTED

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    # No remembered release is left, so this was an ordinary search (the
    # shared Nyaa client may answer it from its ten-minute cache).
    rows = (
        await db_session.scalars(
            select(Torrent).where(Torrent.episode_id == episode.id).order_by(Torrent.id)
        )
    ).all()
    assert len(rows) == 2 and rows[-1].info_hash != dead
    assert rows[-1].evicted_at is None, "a fresh pick, not a re-fetch"
    assert episode.state is EpisodeState.DOWNLOADING
    assert dead not in wired.qbit.added[-1]["urls"], "_pick skips the stalled hash"
    assert torrent.qbit_state == QBIT_STALLED


async def test_poll_writes_the_trackers_seeders(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=979044, email="t9-seeds@arc.test"
    )
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    assert torrent.seeders is not None and torrent.seeders > 0, "Nyaa's count on a first fetch"

    wired.qbit.add_torrent(torrent.info_hash, state="downloading", num_complete=-1)
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    nyaa_count = torrent.seeders
    assert nyaa_count is not None and nyaa_count > 0, "an unscraped tracker changes nothing"

    wired.qbit.torrents.clear()
    wired.qbit.add_torrent(torrent.info_hash, state="downloading", num_complete=2)
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    assert torrent.seeders == 2


DEAD = "the remembered release has no seeders left after 15 minutes"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({}, DEAD),
        ({"time_active": 14 * MINUTE}, None),
        # Somebody is on it but nothing came: no bytes, on the fast clock too.
        ({"num_complete": 1}, "no bytes after 15 minutes"),
        ({"num_incomplete": 1}, "no bytes after 15 minutes"),
        # Never scraped (a hash-only re-add with no trackers).
        ({"num_complete": None, "num_incomplete": None}, "no bytes after 15 minutes"),
        ({"dlspeed": 100}, None),
        ({"progress": 1.0}, None),
        # Any progress is left to the first-fetch thresholds.
        ({"progress": 0.9}, None),
        ({"progress": 0.01}, None),
        ({"state": "queuedDL"}, None),
        ({"state": "stoppedDL"}, None),
        ({"state": "metaDL"}, "no metadata after 15 minutes"),
        ({"state": "metaDL", "num_complete": None}, "no metadata after 15 minutes"),
        ({"time_active": None}, None),
    ],
)
def test_the_refetch_stall_rule(row: dict[str, object], expected: str | None) -> None:
    assert REFETCH_DEAD_MINUTES == 15
    assert refetch_stall_reason(client_row("c" * 40, **row)) == expected


async def test_a_re_fetch_with_no_metadata_at_minute_16_searches_at_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Trackers silent on an old magnet: never scraped, stuck in metaDL."""
    wired, episode, torrent = await refetching(
        db_session, monkeypatch, tmp_path, anilist_id=979045, email="t9-meta@arc.test"
    )
    wired.qbit.torrents.clear()
    wired.qbit.add_torrent(torrent.info_hash, state="metaDL", time_active=16 * MINUTE)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == QBIT_STALLED
    assert episode.state is EpisodeState.WANTED
    assert len(await queued_searches(db_session, episode.id)) == 1


async def test_a_re_fetch_with_no_bytes_and_an_unscraped_tracker_searches_at_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hash-only re-add (no ``tr=``) is never scraped: no bytes is enough."""
    wired, episode, torrent = await refetching(
        db_session, monkeypatch, tmp_path, anilist_id=979046, email="t9-nobytes@arc.test"
    )
    wired.qbit.torrents.clear()
    wired.qbit.add_torrent(torrent.info_hash, state="stalledDL", time_active=16 * MINUTE)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == QBIT_STALLED
    assert episode.state is EpisodeState.WANTED
    assert len(await queued_searches(db_session, episode.id)) == 1


async def test_any_stall_of_a_re_fetch_searches_at_once_not_unavailable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordinary rule's reasons (here the client's own error) take the fast branch."""
    wired, episode, torrent = await refetching(
        db_session, monkeypatch, tmp_path, anilist_id=979047, email="t9-error@arc.test"
    )
    wired.qbit.torrents.clear()
    wired.qbit.add_torrent(torrent.info_hash, state="error", progress=0.4, time_active=MINUTE)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == QBIT_STALLED
    assert episode.state is EpisodeState.WANTED
    assert len(await queued_searches(db_session, episode.id)) == 1


async def test_a_nearly_finished_re_fetch_is_not_deleted_by_one_empty_scrape(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await refetching(
        db_session, monkeypatch, tmp_path, anilist_id=979048, email="t9-ninety@arc.test"
    )
    wired.qbit.torrents.clear()
    wired.qbit.add_torrent(
        torrent.info_hash,
        state="stalledDL",
        progress=0.9,
        time_active=16 * MINUTE,
        num_complete=0,
        num_incomplete=0,
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == "stalledDL"
    assert episode.state is EpisodeState.DOWNLOADING
    assert await queued_searches(db_session, episode.id) == []


async def test_poll_leaves_an_evicted_row_the_client_still_holds_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Evicted rows are not even loaded: no progress write, no delete."""
    wired, episode, torrent = await fetched_then_evicted(
        db_session, monkeypatch, tmp_path, anilist_id=979049, email="t9-noload@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, state="stalledDL", progress=0.5, num_complete=1)
    deleted = list(wired.qbit.deleted)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == QBIT_EVICTED
    assert torrent.progress == 0.0
    assert wired.qbit.deleted == deleted


async def test_a_pack_holding_the_episode_wins_over_the_remembered_release(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``claim_existing`` first: no duplicate bytes for a file Arc already holds."""
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=979060, email="t9-pack@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    assert await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))

    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 11)
    )
    assert neighbour is not None
    remembered = Torrent(
        episode_id=neighbour.id,
        info_hash="f" * 40,
        magnet=f"magnet:?xt=urn:btih:{'f' * 40}",
        qbit_state=QBIT_EVICTED,
        evicted_at=datetime.now(UTC) - timedelta(days=1),
        progress=0.0,
    )
    db_session.add(remembered)
    neighbour.state = EpisodeState.WANTED
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    db_session.add(Want(user_id=want.user_id, episode_id=neighbour.id))
    await db_session.flush()
    adds = len(wired.qbit.added)

    await search_release(
        context(db_session, wired.settings, {"episode_id": neighbour.id}, job_id=2)
    )

    assert neighbour.state is EpisodeState.DOWNLOADING
    claim = await db_session.scalar(
        select(TorrentFile).where(TorrentFile.episode_id == neighbour.id)
    )
    assert claim is not None and claim.wanted, "served from the pack"
    assert len(wired.qbit.added) == adds, "the remembered single was not re-added"
    assert remembered.qbit_state == QBIT_EVICTED


async def test_poll_writes_the_trackers_seeders_for_a_pack(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=979061, email="t9-packseeds@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    pack = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert pack is not None
    for row in wired.qbit.torrents:
        if row["hash"] == BATCH_HASH:
            row |= {"num_complete": 4, "num_incomplete": 1, "state": "downloading"}

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert pack.seeders == 4


# --- retention: what is remembered, and for how long ------------------------


def single(
    *,
    state: str | None = "stoppedUP",
    completed: bool = True,
    kind: TorrentKind = TorrentKind.SINGLE,
) -> Torrent:
    return Torrent(
        episode_id=None if kind is TorrentKind.BATCH else 1,
        info_hash="a" * 40,
        qbit_state=state,
        kind=kind,
        completed_at=datetime.now(UTC) if completed else None,
    )


@pytest.mark.parametrize(
    ("torrent", "episode_state", "expected"),
    [
        (single(), EpisodeState.READY, True),
        (single(state=None), EpisodeState.READY, True),
        (single(state=QBIT_MISSING), EpisodeState.READY, True),
        (single(completed=False), EpisodeState.READY, False),
        (single(), EpisodeState.FAILED, False),
        (single(state=QBIT_STALLED), EpisodeState.READY, False),
        (single(kind=TorrentKind.BATCH), EpisodeState.READY, False),
    ],
)
def test_what_retention_remembers(
    torrent: Torrent, episode_state: EpisodeState, expected: bool
) -> None:
    assert remembers(torrent, episode_state) is expected


async def test_a_release_that_never_finished_is_not_remembered(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=979070, ready_at=days_ago(30), info_hash=HASH
    )
    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    torrent.completed_at = None
    await db_session.flush()

    await retention_sweep(sweep_context(db_session, settings))

    assert episode.state is EpisodeState.NOT_WANTED
    assert await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id)) is None


async def test_the_release_behind_a_failed_episode_is_not_remembered(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=979071,
        ready_at=days_ago(30),
        info_hash=HASH,
        state=EpisodeState.FAILED,
    )

    await retention_sweep(sweep_context(db_session, settings))

    assert episode.state is EpisodeState.NOT_WANTED
    assert await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id)) is None


async def remembered_row(session: AsyncSession, *, anilist_id: int, evicted_days: float) -> Torrent:
    anime = await make_anime(session, anilist_id=anilist_id, status="FINISHED")
    episodes = await make_episodes(session, anime, 1, aired_through=1)
    torrent = Torrent(
        episode_id=episodes[0].id,
        info_hash=f"{anilist_id:040x}",
        qbit_state=QBIT_EVICTED,
        evicted_at=datetime.now(UTC) - timedelta(days=evicted_days),
        progress=0.0,
    )
    session.add(torrent)
    await session.flush()
    return torrent


async def test_remembered_releases_are_forgotten_after_180_days(
    db_session: AsyncSession,
) -> None:
    assert REMEMBER_DAYS == 180
    old = await remembered_row(db_session, anilist_id=979080, evicted_days=181)
    young = await remembered_row(db_session, anilist_id=979081, evicted_days=179)
    old_id, young_id = old.id, young.id
    now = datetime.now(UTC)

    assert await prune_remembered(db_session, now=now, dry_run=True) == 1
    assert await db_session.get(Torrent, old_id) is not None, "a dry run deletes nothing"

    assert await prune_remembered(db_session, now=now, dry_run=False) == 1
    db_session.expunge_all()
    assert await db_session.get(Torrent, old_id) is None
    assert await db_session.get(Torrent, young_id) is not None


async def test_the_sweep_forgets_old_remembered_releases(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    old = await remembered_row(db_session, anilist_id=979082, evicted_days=200)
    old_id = old.id

    await retention_sweep(sweep_context(db_session, settings))

    db_session.expunge_all()
    assert await db_session.get(Torrent, old_id) is None


# --- the seeder warning (FR-A16) --------------------------------------------


def release(seeders: int | None, *, state: str | None = "downloading") -> EpisodeRelease:
    torrent = Torrent(
        episode_id=1,
        info_hash="d" * 40,
        seeders=seeders,
        qbit_state=state,
        kind=TorrentKind.SINGLE,
        manual=False,
    )
    return EpisodeRelease(torrent=torrent, progress=0.5)


@pytest.mark.parametrize(
    ("seeders", "threshold", "state", "expected"),
    [
        (2, 3, "downloading", True),
        (3, 3, "downloading", False),
        (2, 0, "downloading", False),
        (None, 3, "downloading", False),
        (2, 3, "stoppedUP", True),
        (2, 3, None, True),
        (2, 3, QBIT_EVICTED, False),
        (2, 3, QBIT_STALLED, False),
        (2, 3, QBIT_MISSING, False),
    ],
)
def test_few_seeders(
    seeders: int | None, threshold: int, state: str | None, expected: bool
) -> None:
    assert few_seeders(release(seeders, state=state), threshold) is expected


def test_few_seeders_with_no_release_is_false() -> None:
    assert few_seeders(None, 3) is False


def test_the_episode_payload_carries_the_flag_and_the_count() -> None:
    episode = Episode(id=5, anime_id=1, number=5, state=EpisodeState.DOWNLOADING)
    now = datetime.now(UTC)

    warned = EpisodeOut.from_episode(
        episode, now=now, anime_status="RELEASING", release=release(2), min_seeders_warn=3
    )
    assert warned.few_seeders is True
    assert warned.seeders == 2

    off = EpisodeOut.from_episode(
        episode, now=now, anime_status="RELEASING", release=release(2), min_seeders_warn=0
    )
    assert off.few_seeders is False
    assert off.seeders == 2


PASSWORD = "t9-password-123"


async def test_the_show_page_warns_and_zero_disables(
    api_app: FastAPI, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=979050, status="RELEASING")
        episodes = await make_episodes(session, anime, 3, aired_through=3)
        episodes[0].state = EpisodeState.DOWNLOADING
        session.add(
            Torrent(
                episode_id=episodes[0].id,
                info_hash="e" * 40,
                seeders=2,
                qbit_state="downloading",
                progress=0.3,
            )
        )
        await set_setting(session, "min_seeders_warn", 3)
        await session.commit()
        anime_id, episode_id = anime.id, episodes[0].id

    await add_user(api_factory, "t9-member@arc.test", PASSWORD)
    async with api_transport(api_app) as client:
        member: AsyncClient = await login(client, "t9-member@arc.test", PASSWORD)

        def row(payload: dict[str, object]) -> dict[str, object]:
            rows = payload["episodes"]
            assert isinstance(rows, list)
            return next(item for item in rows if item["id"] == episode_id)

        found = row((await member.get(f"/api/anime/{anime_id}")).json())
        assert found["few_seeders"] is True
        assert found["seeders"] == 2

        async with api_factory() as session:
            await set_setting(session, "min_seeders_warn", 0)
            await session.commit()
        found = row((await member.get(f"/api/anime/{anime_id}")).json())
        assert found["few_seeders"] is False

        async with api_factory() as session:
            await set_setting(session, "min_seeders_warn", 3)
            await session.commit()
