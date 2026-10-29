"""Redoing a trip's copies from a freshly fetched source (owner, 2026-10-29).

:mod:`arc.services.trips.redo`, the ``redo-copies`` command, and the one
automatic redo the ``offline_encode`` handler makes when its source turns out
broken (:mod:`arc.services.media.offline`). The production case: trip 5's
copies were encoded from half-downloaded pack files; one came out 9.9 MB and
was delivered, the rest failed, and nothing in the app could start them again.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.cli import EXIT_FAILED, build_parser, cmd_redo_copies
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    ReviewState,
    Torrent,
    TorrentFile,
    TorrentKind,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    Want,
    WatchProgress,
)
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.names import COMPUTE_WANTS, QBIT_RESELECT, SEARCH_RELEASE
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.acquisition.wants import REASON_TRIP_ENDED, compute_wants
from arc.services.jobs.registry import JobContext
from arc.services.library.link import link
from arc.services.media.names import OFFLINE_ENCODE, REMUX, TRANSCODE, offline_path_for
from arc.services.media.offline import NO_SOURCE_DURATION, SOURCE_GONE_MESSAGE, offline_encode
from arc.services.media.transcode import TranscodeError, reset_semaphore
from arc.services.trips.create import TripConflict, create_trip
from arc.services.trips.names import TRIP_RELEASE
from arc.services.trips.redo import (
    SKIP_BUSY,
    SKIP_ENCODING,
    SKIP_ENDED,
    SKIP_NOT_IN_TRIP,
    SKIP_RELEASED,
    SKIP_WATCHED,
    redo_after_broken_source,
    redo_copies,
    reset_copy,
    skip_reason,
)
from tests.acquisition_helpers import (
    acquisition_settings,
    fake_free_space,
    make_anime,
    make_episodes,
    make_user,
)
from tests.media_helpers import OFFLINE_PROBE, SOURCE_PROBE, install_fake_ffmpeg, write_script
from tests.retention_helpers import write_source_file

pytestmark = pytest.mark.pg

FINISHED = "FINISHED"


def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh_semaphore() -> None:
    reset_semaphore()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    return acquisition_settings(tmp_path)


async def a_trip(
    session: AsyncSession, anilist_id: int, email: str, settings: Settings, *, count: int = 3
) -> tuple[User, Trip, list[Episode]]:
    anime = await make_anime(session, anilist_id=anilist_id, status=FINISHED, episodes=count + 1)
    episodes = await make_episodes(session, anime, count + 1, aired_through=count + 1)
    user = await make_user(session, email)
    trip = (
        await create_trip(session, settings, user=user, anime_id=anime.id, count=count, now=now())
    ).trip
    # The trip queues its own reconciliation; what is asserted is the redo's.
    await session.execute(delete(Job).where(Job.type == COMPUTE_WANTS))
    await session.flush()
    return user, trip, episodes


async def pack_source(
    session: AsyncSession, settings: Settings, episode: Episode, *, info_hash: str
) -> tuple[MediaFile, TorrentFile, Path]:
    """The trip 5 shape: a pack member linked to the episode, ``matched``."""
    torrent = Torrent(
        kind=TorrentKind.BATCH, episode_id=None, info_hash=info_hash, qbit_state="stalledUP"
    )
    session.add(torrent)
    await session.flush()
    path = settings.downloads_dir / f"pack-{info_hash[:4]}" / f"{episode.number:02d}.mkv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * 2048)
    claim = TorrentFile(
        torrent_id=torrent.id,
        file_index=episode.number,
        path=f"pack/{episode.number:02d}.mkv",
        size=2048,
        episode_id=episode.id,
        wanted=True,
        completed_at=now(),
        progress=1.0,
    )
    media = MediaFile(
        episode_id=episode.id, path=str(path.resolve()), size=2048, review_state=ReviewState.AUTO
    )
    session.add_all([claim, media])
    episode.state = EpisodeState.MATCHED
    await session.flush()
    return media, claim, path


async def failed_copy(
    session: AsyncSession,
    settings: Settings,
    episode: Episode,
    *,
    media_file_id: int | None = None,
    leftover: bool = False,
) -> OfflineCopy:
    copy = OfflineCopy(
        episode_id=episode.id,
        state=OfflineCopyState.FAILED,
        error="ffmpeg finished but the offline copy is not usable: it is 3.0s long",
        media_file_id=media_file_id,
        codec="h264",
    )
    session.add(copy)
    if leftover:
        path = offline_path_for(settings, episode.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 1024)
    await session.flush()
    return copy


async def row_of(session: AsyncSession, trip: Trip, episode: Episode) -> TripEpisode:
    row = await session.get(TripEpisode, (trip.id, episode.id), populate_existing=True)
    assert row is not None
    return row


async def pending_jobs(session: AsyncSession, job_type: str) -> list[Job]:
    rows = await session.scalars(
        select(Job).where(Job.type == job_type, Job.status == JobStatus.PENDING).order_by(Job.id)
    )
    return list(rows.all())


async def deliver(session: AsyncSession, trip: Trip, episode: Episode) -> None:
    """A confirmation three hours ago (the settle has since run)."""
    row = await row_of(session, trip, episode)
    row.state = TripEpisodeState.DELIVERED
    row.delivered_at = now() - timedelta(hours=3)
    row.available_at = now() - timedelta(hours=4)
    await session.flush()


# --- Pure -------------------------------------------------------------------


def test_the_skip_rules() -> None:
    def row(state: TripEpisodeState, *, released: bool = False) -> TripEpisode:
        return TripEpisode(
            trip_id=1, episode_id=1, state=state, released_at=now() if released else None
        )

    live = {"watched": False, "encoding": False}
    assert skip_reason(row(TripEpisodeState.PENDING), **live) is None
    assert skip_reason(row(TripEpisodeState.DELIVERED), **live) is None
    assert skip_reason(row(TripEpisodeState.CANCELLED), **live) == SKIP_ENDED
    assert skip_reason(row(TripEpisodeState.EXPIRED), **live) == SKIP_ENDED
    assert skip_reason(row(TripEpisodeState.DELIVERED, released=True), **live) == SKIP_RELEASED
    assert skip_reason(row(TripEpisodeState.PENDING), watched=True, encoding=False) == SKIP_WATCHED
    assert skip_reason(row(TripEpisodeState.PENDING), watched=False, encoding=True) == SKIP_ENCODING


def test_a_reset_copy_keeps_nothing_of_the_old_one() -> None:
    at = now()
    copy = OfflineCopy(
        episode_id=1,
        state=OfflineCopyState.READY,
        error="old",
        media_file_id=4,
        size=9_900_000,
        etag='"abc"',
        ready_at=at - timedelta(days=1),
        codec="hevc",
    )

    reset_copy(copy, now=at)

    assert copy.state is OfflineCopyState.QUEUED
    assert (copy.error, copy.media_file_id, copy.size, copy.etag, copy.ready_at) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert copy.redone_at == at


def test_the_command_is_reachable_and_wants_a_choice() -> None:
    parser = build_parser()

    parsed = parser.parse_args(["redo-copies", "--trip", "5", "--episodes", "1,3,4"])
    assert (parsed.trip, parsed.episodes, parsed.failed) == (5, [1, 3, 4], False)
    assert parser.parse_args(["redo-copies", "--trip", "5", "--failed"]).failed is True
    for bad in (
        ["redo-copies", "--trip", "5"],
        ["redo-copies", "--failed"],
        ["redo-copies", "--trip", "5", "--failed", "--episodes", "1"],
        ["redo-copies", "--trip", "5", "--episodes", "one"],
        ["redo-copies", "--trip", "5", "--episodes", "0"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


def test_the_command_is_listed_by_the_cli() -> None:
    """Run as the operator runs it: a subprocess, ``python -m arc.cli``."""
    server = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, "-m", "arc.cli", "redo-copies", "--help"],
        cwd=server,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(server)},
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "--episodes" in done.stdout and "--failed" in done.stdout


# --- redo_copies ------------------------------------------------------------


async def test_a_failed_copy_is_redone_and_its_pack_file_given_back(
    db_session: AsyncSession, settings: Settings
) -> None:
    _, trip, episodes = await a_trip(db_session, 991001, "redo1@arc.test", settings)
    media, claim, source = await pack_source(db_session, settings, episodes[0], info_hash="d" * 40)
    await failed_copy(db_session, settings, episodes[0], media_file_id=media.id, leftover=True)
    copy_path = offline_path_for(settings, episodes[0].id)
    assert copy_path.exists()
    at = now()

    report = await redo_copies(db_session, settings, trip=trip, now=at, failed=True)

    assert report.redone == (episodes[0].id,)
    assert report.sources_removed == 1
    assert report.skipped == ()
    copy = await db_session.get(OfflineCopy, episodes[0].id, populate_existing=True)
    assert copy is not None
    assert copy.state is OfflineCopyState.QUEUED
    assert (copy.error, copy.media_file_id, copy.size, copy.etag, copy.ready_at) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert copy.redone_at == at
    assert not copy_path.exists(), "the broken copy's file goes"
    assert not source.exists(), "the source goes"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MediaFile)
            .where(MediaFile.episode_id == episodes[0].id)
        )
        == 0
    )
    await db_session.refresh(claim)
    assert claim.wanted is False and claim.completed_at is None, "the pack's file is given back"
    assert [job.payload["torrent_id"] for job in await pending_jobs(db_session, QBIT_RESELECT)] == [
        claim.torrent_id
    ]
    assert episodes[0].state is EpisodeState.NOT_WANTED
    assert len(await pending_jobs(db_session, COMPUTE_WANTS)) == 1
    assert (await row_of(db_session, trip, episodes[0])).available_at == at

    # The pending trip row wants it again: not_wanted → wanted, searched.
    await compute_wants(db_session)
    await db_session.refresh(episodes[0])
    assert episodes[0].state is EpisodeState.WANTED
    assert await pending_jobs(db_session, SEARCH_RELEASE)


async def test_failed_selects_only_failed_copies(
    db_session: AsyncSession, settings: Settings
) -> None:
    _, trip, episodes = await a_trip(db_session, 991002, "redo2@arc.test", settings)
    await failed_copy(db_session, settings, episodes[0])
    db_session.add(OfflineCopy(episode_id=episodes[1].id, state=OfflineCopyState.READY))
    # Not the trip's: a failed copy of the episode after the trip.
    await failed_copy(db_session, settings, episodes[3])
    await db_session.flush()

    report = await redo_copies(db_session, settings, trip=trip, now=now(), failed=True)

    assert report.redone == (episodes[0].id,)
    ready = await db_session.get(OfflineCopy, episodes[1].id, populate_existing=True)
    assert ready is not None and ready.state is OfflineCopyState.READY
    other = await db_session.get(OfflineCopy, episodes[3].id, populate_existing=True)
    assert other is not None and other.state is OfflineCopyState.FAILED


async def test_a_delivered_copyless_episode_is_pending_again(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Trip 5 episode 1: delivered, the settle took the copy, no source, not_wanted."""
    user, trip, episodes = await a_trip(db_session, 991003, "redo3@arc.test", settings)
    await deliver(db_session, trip, episodes[0])
    episodes[0].state = EpisodeState.NOT_WANTED
    # Production's episode 843: its want shelved when the row was delivered.
    shelved = await db_session.scalar(
        select(Want).where(Want.user_id == user.id, Want.episode_id == episodes[0].id)
    )
    if shelved is None:
        shelved = Want(user_id=user.id, episode_id=episodes[0].id, trip=True)
        db_session.add(shelved)
    shelved.dropped_at = now() - timedelta(hours=2)
    shelved.drop_reason = REASON_TRIP_ENDED
    await db_session.flush()
    at = now()

    report = await redo_copies(
        db_session, settings, trip=trip, now=at, episode_ids=[episodes[0].id]
    )

    assert report.redone == (episodes[0].id,)
    assert (report.sources_removed, report.repended) == (0, 1)
    row = await row_of(db_session, trip, episodes[0])
    assert row.state is TripEpisodeState.PENDING
    assert row.delivered_at is None and row.released_at is None
    assert row.available_at == at
    assert await db_session.get(OfflineCopy, episodes[0].id) is None, "no row is made up"
    assert len(await pending_jobs(db_session, COMPUTE_WANTS)) == 1

    await compute_wants(db_session)
    await db_session.refresh(episodes[0])
    assert episodes[0].state is EpisodeState.WANTED, "the trip wants it again"
    want = await db_session.scalar(
        select(Want).where(Want.user_id == user.id, Want.episode_id == episodes[0].id)
    )
    assert want is not None and want.dropped_at is None and want.trip, "revived"
    assert await pending_jobs(db_session, SEARCH_RELEASE)


async def test_the_skips(db_session: AsyncSession, settings: Settings) -> None:
    user, trip, episodes = await a_trip(db_session, 991004, "redo4@arc.test", settings, count=4)
    (await row_of(db_session, trip, episodes[0])).state = TripEpisodeState.CANCELLED
    await deliver(db_session, trip, episodes[1])
    (await row_of(db_session, trip, episodes[1])).released_at = now()
    db_session.add(
        WatchProgress(user_id=user.id, episode_id=episodes[2].id, position_s=1400.0, completed=True)
    )
    db_session.add(
        Job(
            type=OFFLINE_ENCODE,
            payload={"episode_id": episodes[3].id, "why": "trip"},
            status=JobStatus.RUNNING,
        )
    )
    for episode in episodes[:4]:
        await failed_copy(db_session, settings, episode)
    await db_session.flush()
    stranger = await make_anime(db_session, anilist_id=991005, status=FINISHED, episodes=1)
    outsider = (await make_episodes(db_session, stranger, 1, aired_through=1))[0]

    report = await redo_copies(
        db_session,
        settings,
        trip=trip,
        now=now(),
        episode_ids=[outsider.id, *(episode.id for episode in episodes[:4])],
    )

    assert report.redone == ()
    assert dict(report.skipped) == {
        outsider.id: SKIP_NOT_IN_TRIP,
        episodes[0].id: SKIP_ENDED,
        episodes[1].id: SKIP_RELEASED,
        episodes[2].id: SKIP_WATCHED,
        episodes[3].id: SKIP_ENCODING,
    }
    for episode in episodes[:4]:
        copy = await db_session.get(OfflineCopy, episode.id, populate_existing=True)
        assert copy is not None and copy.state is OfflineCopyState.FAILED
    assert await pending_jobs(db_session, COMPUTE_WANTS) == []


async def test_an_ended_trip_is_refused(db_session: AsyncSession, settings: Settings) -> None:
    _, trip, episodes = await a_trip(db_session, 991006, "redo6@arc.test", settings)
    trip.state = TripState.CANCELLED
    await db_session.flush()

    with pytest.raises(TripConflict):
        await redo_copies(db_session, settings, trip=trip, now=now(), episode_ids=[episodes[0].id])


async def test_another_users_delivered_row_is_pending_again_too(
    db_session: AsyncSession, settings: Settings
) -> None:
    _, trip, episodes = await a_trip(db_session, 991007, "redo7@arc.test", settings, count=2)
    other_user = await make_user(db_session, "redo7b@arc.test")
    other = (
        await create_trip(
            db_session,
            settings,
            user=other_user,
            anime_id=episodes[0].anime_id,
            count=2,
            now=now(),
        )
    ).trip
    await deliver(db_session, trip, episodes[0])
    await deliver(db_session, other, episodes[0])

    report = await redo_copies(
        db_session, settings, trip=trip, now=now(), episode_ids=[episodes[0].id]
    )

    assert report.repended == 1, "the named trip's count"
    assert (await row_of(db_session, other, episodes[0])).state is TripEpisodeState.PENDING


async def test_a_ready_episode_keeps_its_source_and_queues_its_copy(
    db_session: AsyncSession, settings: Settings
) -> None:
    _, trip, episodes = await a_trip(db_session, 991008, "redo8@arc.test", settings)
    source = write_source_file(settings, episodes[0].id)
    db_session.add(MediaFile(episode_id=episodes[0].id, path=str(source.resolve()), size=2048))
    episodes[0].state = EpisodeState.READY
    await failed_copy(db_session, settings, episodes[0])

    report = await redo_copies(db_session, settings, trip=trip, now=now(), failed=True)

    assert report.redone == (episodes[0].id,) and report.sources_removed == 0
    assert source.exists()
    assert episodes[0].state is EpisodeState.READY
    assert [
        job.payload["episode_id"] for job in await pending_jobs(db_session, OFFLINE_ENCODE)
    ] == [episodes[0].id]


async def test_the_automatic_redo_needs_a_pending_row_and_an_unspent_retry(
    db_session: AsyncSession, settings: Settings
) -> None:
    _, trip, episodes = await a_trip(db_session, 991009, "redo9@arc.test", settings)
    spent = await failed_copy(db_session, settings, episodes[0])
    spent.redone_at = now() - timedelta(hours=1)
    await failed_copy(db_session, settings, episodes[1])
    await deliver(db_session, trip, episodes[1])
    await failed_copy(db_session, settings, episodes[2])

    assert (
        await redo_after_broken_source(db_session, settings, episode_id=episodes[0].id, now=now())
        is None
    ), "the one retry is spent"
    assert (
        await redo_after_broken_source(db_session, settings, episode_id=episodes[1].id, now=now())
        is None
    ), "no pending row: the device has a copy"
    redone = await redo_after_broken_source(
        db_session, settings, episode_id=episodes[2].id, now=now()
    )
    assert redone is not None and redone.redone == (episodes[2].id,)


# --- The command -----------------------------------------------------------


def args(**values: Any) -> Any:
    return type("Args", (), {"trip": None, "episodes": None, "failed": False, **values})()


async def test_the_command_maps_numbers_and_reports(
    db_session: AsyncSession, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    _, trip, episodes = await a_trip(db_session, 991010, "redo10@arc.test", settings)
    await failed_copy(db_session, settings, episodes[0])
    (await row_of(db_session, trip, episodes[2])).state = TripEpisodeState.CANCELLED
    await db_session.commit()

    code = await cmd_redo_copies(db_session, settings, args(trip=trip.id, episodes=[1, 3, 9]))

    assert code == 0
    out = capsys.readouterr().out
    assert (
        f"trip {trip.id}: 1 redone (episodes 1), 0 sources removed, 0 re-pended, 2 skipped" in out
    )
    assert "episode 9: not in this trip" in out
    assert "episode 3: ended" in out
    copy = await db_session.get(OfflineCopy, episodes[0].id, populate_existing=True)
    assert copy is not None and copy.state is OfflineCopyState.QUEUED


async def test_the_command_redoes_failed_copies(
    db_session: AsyncSession, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    _, trip, episodes = await a_trip(db_session, 991011, "redo11@arc.test", settings)
    await failed_copy(db_session, settings, episodes[1])
    await db_session.commit()

    assert await cmd_redo_copies(db_session, settings, args(trip=trip.id, failed=True)) == 0
    assert f"trip {trip.id}: 1 redone (episodes 2)" in capsys.readouterr().out


async def test_the_command_refuses_an_unknown_or_ended_trip(
    db_session: AsyncSession, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    _, trip, _ = await a_trip(db_session, 991012, "redo12@arc.test", settings)
    trip.state = TripState.FINISHED
    await db_session.commit()
    missing = int(await db_session.scalar(select(Trip.id).order_by(Trip.id.desc())) or 0) + 1000

    assert await cmd_redo_copies(db_session, settings, args(trip=missing, failed=True)) == (
        EXIT_FAILED
    )
    trip_id = trip.id
    assert await cmd_redo_copies(db_session, settings, args(trip=trip_id, failed=True)) == (
        EXIT_FAILED
    )
    err = capsys.readouterr().err
    assert f"no trip with id {missing}" in err
    assert f"trip {trip_id} is not active" in err


# --- The encode's own redo, and the way back --------------------------------


def context(session: AsyncSession, settings: Settings, job: Job) -> JobContext:
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("arc.jobs.test")
    )


async def running_job(session: AsyncSession, episode_id: int) -> Job:
    job = Job(
        type=OFFLINE_ENCODE,
        payload={"episode_id": episode_id, "why": "trip"},
        status=JobStatus.RUNNING,
        attempts=1,
        locked_by="test:1",
        locked_at=now() - timedelta(minutes=5),
    )
    session.add(job)
    await session.commit()
    return job


async def a_trip_episode_with_a_source(
    session: AsyncSession, settings: Settings, anilist_id: int
) -> tuple[Trip, Episode, Path]:
    """A pending trip episode, ``matched`` to a pack member on disk. Committed."""
    _, trip, episodes = await a_trip(session, anilist_id, f"auto{anilist_id}@arc.test", settings)
    _, _, source = await pack_source(session, settings, episodes[0], info_hash=f"{anilist_id:040d}")
    await session.commit()
    return trip, episodes[0], source


async def assert_redone(
    session: AsyncSession, settings: Settings, episode_id: int, source: Path
) -> None:
    copy = await session.get(OfflineCopy, episode_id, populate_existing=True)
    assert copy is not None
    assert copy.state is OfflineCopyState.QUEUED and copy.error is None
    assert copy.redone_at is not None
    assert not source.exists(), "the broken source is removed"
    episode = await session.get(Episode, episode_id, populate_existing=True)
    assert episode is not None and episode.state is EpisodeState.NOT_WANTED
    assert len(await pending_jobs(session, COMPUTE_WANTS)) == 1


def no_duration_probe() -> dict[str, Any]:
    return {"format": {"format_name": "matroska,webm"}, "streams": SOURCE_PROBE["streams"]}


BROKEN = ("no-duration", "short", "unreadable", "missing")


@pytest.mark.parametrize("broken", BROKEN)
async def test_a_broken_source_is_fetched_again_once(
    api_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broken: str,
) -> None:
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    settings = acquisition_settings(tmp_path)
    bin_dir = tmp_path / "bin"
    if broken == "no-duration":
        install_fake_ffmpeg(bin_dir, monkeypatch, source_probe=no_duration_probe())
    elif broken == "short":
        install_fake_ffmpeg(
            bin_dir, monkeypatch, offline_probe={**OFFLINE_PROBE, "format": {"duration": "3.0"}}
        )
    else:
        install_fake_ffmpeg(bin_dir, monkeypatch)
        if broken == "unreadable":
            write_script(bin_dir, "ffprobe", "import sys\n")
    async with api_factory() as session:
        _, episode, source = await a_trip_episode_with_a_source(
            session,
            settings,
            991100 + ["no-duration", "short", "unreadable", "missing"].index(broken),
        )
        if broken == "missing":
            source.unlink()
        job = await running_job(session, episode.id)

        await offline_encode(context(session, settings, job))  # ends, never raises

        await assert_redone(session, settings, episode.id, source)
        await session.refresh(job)
        error = job.payload["error_tail"]
        expected = {
            "no-duration": NO_SOURCE_DURATION,
            "short": "3.0s long",
            "unreadable": "ffprobe returned nothing",
            "missing": SOURCE_GONE_MESSAGE,
        }[broken]
        assert expected in error


async def test_an_encoder_failure_never_redoes(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, fail=True)
    async with api_factory() as session:
        _, episode, source = await a_trip_episode_with_a_source(session, settings, 991110)
        job = await running_job(session, episode.id)
        with pytest.raises(TranscodeError):
            await offline_encode(context(session, settings, job))
        await session.rollback()

        copy = await session.get(OfflineCopy, episode.id, populate_existing=True)
        assert copy is not None and copy.state is OfflineCopyState.FAILED
        assert copy.redone_at is None
        assert source.exists()
        await session.refresh(episode)
        assert episode.state is EpisodeState.MATCHED
        assert await pending_jobs(session, COMPUTE_WANTS) == []


async def test_a_wrong_codec_is_not_the_sources_fault(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    wrong = {
        **OFFLINE_PROBE,
        "streams": [
            {**OFFLINE_PROBE["streams"][0], "codec_name": "hevc"},
            OFFLINE_PROBE["streams"][1],
        ],
    }
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, offline_probe=wrong)
    async with api_factory() as session:
        _, episode, source = await a_trip_episode_with_a_source(session, settings, 991111)
        await offline_encode(context(session, settings, await running_job(session, episode.id)))

        copy = await session.get(OfflineCopy, episode.id, populate_existing=True)
        assert copy is not None and copy.state is OfflineCopyState.FAILED
        assert copy.redone_at is None and source.exists()


async def test_the_redone_copy_is_made_from_the_new_source_and_a_second_failure_stays(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The way back: redo → a new file is linked → the encode makes a ready copy."""
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, source_probe=no_duration_probe())
    async with api_factory() as session:
        trip, episode, source = await a_trip_episode_with_a_source(session, settings, 991120)
        first = await running_job(session, episode.id)
        await offline_encode(context(session, settings, first))
        await assert_redone(session, settings, episode.id, source)
        first.status = JobStatus.DONE  # what the runner writes once the handler returns
        await session.commit()

        # The new download lands and the matcher links it.
        fresh = settings.downloads_dir / "again" / "01.mkv"
        fresh.parent.mkdir(parents=True, exist_ok=True)
        fresh.write_bytes(b"1" * 4096)
        media = MediaFile(path=str(fresh.resolve()), size=4096)
        session.add(media)
        await session.flush()
        await link(
            session,
            media,
            anime_id=episode.anime_id,
            episode_number=episode.number,
            review_state=ReviewState.AUTO,
        )
        await session.commit()
        queued = await pending_jobs(session, OFFLINE_ENCODE)
        assert [job.payload["episode_id"] for job in queued] == [episode.id]

        # A good source this time: the queued row is made into a ready copy.
        install_fake_ffmpeg(tmp_path / "bin2", monkeypatch)
        second = queued[0]
        second.status = JobStatus.RUNNING
        await session.commit()
        await offline_encode(context(session, settings, second))

        copy = await session.get(OfflineCopy, episode.id, populate_existing=True)
        assert copy is not None and copy.state is OfflineCopyState.READY
        assert copy.media_file_id == media.id
        assert offline_path_for(settings, episode.id).is_file()
        row = await session.get(TripEpisode, (trip.id, episode.id), populate_existing=True)
        assert row is not None and row.state is TripEpisodeState.PENDING
        assert row.available_at is not None
        # The copy hook ran: the trip-only episode's source is released.
        assert [job.payload["episode_id"] for job in await pending_jobs(session, TRIP_RELEASE)] == [
            episode.id
        ]

        # A second broken source: failed, and no second redo.
        copy.state = OfflineCopyState.QUEUED
        copy.size = None
        copy.etag = None
        await session.commit()
        offline_path_for(settings, episode.id).unlink()
        install_fake_ffmpeg(tmp_path / "bin3", monkeypatch, source_probe=no_duration_probe())
        await offline_encode(context(session, settings, await running_job(session, episode.id)))

        copy = await session.get(OfflineCopy, episode.id, populate_existing=True)
        assert copy is not None and copy.state is OfflineCopyState.FAILED
        assert fresh.exists(), "the second failure keeps its source"
        await session.refresh(episode)
        assert episode.state is EpisodeState.MATCHED


# --- Fix loop: the source first ---------------------------------------------


async def test_a_busy_source_leaves_the_copy_and_the_retry_alone(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The deleter refuses an episode in flight: nothing changes, the retry is not spent."""
    _, trip, episodes = await a_trip(db_session, 991201, "redo-busy@arc.test", settings)
    media, _, source = await pack_source(db_session, settings, episodes[0], info_hash="e" * 40)
    episodes[0].state = EpisodeState.DOWNLOADING
    await failed_copy(db_session, settings, episodes[0], media_file_id=media.id, leftover=True)
    await deliver(db_session, trip, episodes[0])

    report = await redo_copies(db_session, settings, trip=trip, now=now(), failed=True)

    assert report.redone == ()
    assert report.skipped == ((episodes[0].id, SKIP_BUSY),)
    copy = await db_session.get(OfflineCopy, episodes[0].id, populate_existing=True)
    assert copy is not None and copy.state is OfflineCopyState.FAILED
    assert copy.redone_at is None
    assert offline_path_for(settings, episodes[0].id).exists()
    assert source.exists()
    assert (await row_of(db_session, trip, episodes[0])).state is TripEpisodeState.DELIVERED
    assert await pending_jobs(db_session, COMPUTE_WANTS) == []


@pytest.mark.parametrize("reader", [REMUX, TRANSCODE])
async def test_any_source_reader_counts_as_encoding(
    db_session: AsyncSession, settings: Settings, reader: str
) -> None:
    _, trip, episodes = await a_trip(
        db_session, 991202 + (reader == TRANSCODE), f"redo-{reader}@arc.test", settings
    )
    await failed_copy(db_session, settings, episodes[0])
    db_session.add(
        Job(type=reader, payload={"episode_id": episodes[0].id}, status=JobStatus.PENDING)
    )
    await db_session.flush()

    report = await redo_copies(db_session, settings, trip=trip, now=now(), failed=True)

    assert report.skipped == ((episodes[0].id, SKIP_ENCODING),)
