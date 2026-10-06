"""Retention and the small offline copies (FR-P6, FR-T1, FR-T3).

Three rules: a copy goes with its episode; a source is never deleted while a
copy is being made from it; and a ready episode's copy that nobody has fetched
for ``offline_idle_days`` is deleted on its own, leaving the episode alone.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
)
from arc.services.jobs.registry import JobContext
from arc.services.media.names import OFFLINE_ENCODE, offline_path_for
from arc.services.retention.delete import delete_episode_files, delete_idle_copy
from arc.services.retention.jobs import delete_files, retention_sweep
from arc.services.retention.names import DELETE_EPISODE_FILES, RETENTION_SWEEP
from arc.services.retention.rules import offline_idle_days
from arc.services.retention.sweep import (
    allowed_roots,
    candidates,
    idle_copies,
    retained_usage,
    safe_path,
    targets_for_episode,
)
from tests.acquisition_helpers import acquisition_settings, make_user, set_setting
from tests.retention_helpers import NOW, add_want, days_ago, make_retained_episode

pytestmark = pytest.mark.pg

COPY = b"c" * 4096


def context(
    session: AsyncSession,
    settings: Settings,
    payload: dict[str, object] | None = None,
    *,
    job_type: str = RETENTION_SWEEP,
) -> JobContext:
    job = Job(type=job_type, payload=dict(payload or {}), status=JobStatus.RUNNING, attempts=1)
    job.id = 1
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("arc.jobs.test")
    )


async def add_copy(
    session: AsyncSession,
    settings: Settings,
    episode_id: int,
    *,
    state: OfflineCopyState = OfflineCopyState.READY,
    ready_at: datetime | None = None,
    last_served_at: datetime | None = None,
) -> Path:
    path = offline_path_for(settings, episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(COPY)
    session.add(
        OfflineCopy(
            episode_id=episode_id,
            state=state,
            size=len(COPY),
            codec="h264",
            ready_at=ready_at,
            last_served_at=last_served_at,
        )
    )
    await session.flush()
    return path


async def add_encode_job(
    session: AsyncSession, episode_id: int, status: JobStatus = JobStatus.RUNNING
) -> Job:
    job = Job(
        type=OFFLINE_ENCODE,
        payload={"episode_id": episode_id, "why": "request"},
        status=status,
    )
    session.add(job)
    await session.flush()
    return job


def test_the_offline_directory_is_a_retention_root(tmp_path: Path) -> None:
    settings = acquisition_settings(tmp_path)

    assert settings.offline_dir in allowed_roots(settings)
    inside = settings.offline_dir / "5.mp4"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    assert safe_path(inside, settings) == inside.resolve()
    assert safe_path(settings.offline_dir, settings) is None, "never the root itself"


async def test_the_idle_setting_defaults_to_seven_days(db_session: AsyncSession) -> None:
    assert await offline_idle_days(db_session) == 7
    await set_setting(db_session, "offline_idle_days", 0)
    assert await offline_idle_days(db_session) == 1, "a zero reads as one day"


# --- The copy goes with its episode -------------------------------------------


async def test_the_sweep_deletes_the_copy_with_its_episode(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=977001, ready_at=days_ago(30)
    )
    copy = await add_copy(db_session, settings, episode.id, ready_at=days_ago(30))

    (target,) = await candidates(db_session, settings, now=NOW)
    assert target.targets.offline_file == copy.resolve()
    assert target.targets.offline_copy is True
    assert target.bytes >= len(COPY) + 2048

    await retention_sweep(context(db_session, settings))

    assert not copy.exists()
    assert await db_session.get(OfflineCopy, episode.id) is None
    assert episode.state is EpisodeState.NOT_WANTED


async def test_the_manual_delete_takes_the_copy_too(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=977002, ready_at=days_ago(1)
    )
    copy = await add_copy(db_session, settings, episode.id)

    await delete_files(
        context(db_session, settings, {"episode_id": episode.id}, job_type=DELETE_EPISODE_FILES)
    )

    assert not copy.exists()
    assert await db_session.get(OfflineCopy, episode.id) is None


async def test_a_copy_row_whose_file_is_gone_is_still_cleared(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=977003, ready_at=days_ago(30)
    )
    (await add_copy(db_session, settings, episode.id)).unlink()

    targets = await targets_for_episode(db_session, settings, episode.id)
    assert targets.offline_file is None and targets.offline_copy is True
    removed = await delete_episode_files(db_session, settings, episode, targets)

    assert removed.offline_copies == 1 and removed.offline_file is None
    assert await db_session.get(OfflineCopy, episode.id) is None


async def test_a_symlinked_copy_is_not_followed(db_session: AsyncSession, tmp_path: Path) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=977004, ready_at=days_ago(30)
    )
    outside = tmp_path.parent / f"{tmp_path.name}-precious.txt"
    outside.write_text("keep me")
    link = offline_path_for(settings, episode.id)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    targets = await targets_for_episode(db_session, settings, episode.id)
    assert targets.offline_file is None
    await delete_episode_files(db_session, settings, episode, targets)

    assert outside.read_text() == "keep me"


# --- Never a source mid-encode ------------------------------------------------


@pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.PENDING])
async def test_a_source_is_never_deleted_while_a_copy_is_being_made(
    db_session: AsyncSession, tmp_path: Path, status: JobStatus
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=977010 + (status is JobStatus.PENDING),
        ready_at=days_ago(30),
    )
    await add_copy(db_session, settings, episode.id, state=OfflineCopyState.PREPARING)
    await add_encode_job(db_session, episode.id, status)
    source = settings.downloads_dir / str(episode.id)

    assert await candidates(db_session, settings, now=NOW) == []
    # And the deleter asks again itself, so the manual button cannot either.
    targets = await targets_for_episode(db_session, settings, episode.id)
    removed = await delete_episode_files(db_session, settings, episode, targets)

    assert removed.acted is False
    assert source.exists()
    assert await db_session.scalar(select(MediaFile.id).where(MediaFile.episode_id == episode.id))
    assert episode.state is EpisodeState.READY


async def test_a_preparing_row_with_no_live_job_does_not_pin_the_source(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A dead encode (its job failed or gone) must not hold a file for ever."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=977012, ready_at=days_ago(30)
    )
    await add_copy(db_session, settings, episode.id, state=OfflineCopyState.PREPARING)
    await add_encode_job(db_session, episode.id, JobStatus.FAILED)

    (target,) = await candidates(db_session, settings, now=NOW)
    assert target.episode_id == episode.id


# --- Idle copies ----------------------------------------------------------------


async def test_an_idle_copy_of_a_ready_episode_is_deleted_and_the_episode_kept(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    now = datetime.now(UTC)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=977020, ready_at=now - timedelta(days=1)
    )
    # Somebody still wants it, so the episode itself is not a candidate.
    user = await make_user(db_session, "idle@arc.test")
    await add_want(db_session, user, episode)
    copy = await add_copy(
        db_session,
        settings,
        episode.id,
        ready_at=now - timedelta(days=20),
        last_served_at=now - timedelta(days=8),
    )

    await retention_sweep(context(db_session, settings))

    assert not copy.exists()
    assert await db_session.get(OfflineCopy, episode.id) is None
    assert episode.state is EpisodeState.READY
    assert (settings.downloads_dir / str(episode.id)).exists(), "the source stays"


async def test_the_idle_period_counts_from_the_last_fetch_else_from_ready(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    idle = timedelta(days=7)
    served = await make_retained_episode(
        db_session, settings, anilist_id=977021, ready_at=days_ago(30)
    )
    fetched_lately = await make_retained_episode(
        db_session, settings, anilist_id=977022, ready_at=days_ago(30)
    )
    never_fetched = await make_retained_episode(
        db_session, settings, anilist_id=977023, ready_at=days_ago(30)
    )
    fresh = await make_retained_episode(
        db_session, settings, anilist_id=977024, ready_at=days_ago(30)
    )
    await add_copy(
        db_session, settings, served.id, ready_at=days_ago(20), last_served_at=days_ago(8)
    )
    await add_copy(
        db_session, settings, fetched_lately.id, ready_at=days_ago(20), last_served_at=days_ago(2)
    )
    await add_copy(db_session, settings, never_fetched.id, ready_at=days_ago(9))
    await add_copy(db_session, settings, fresh.id, ready_at=days_ago(3))

    found = await idle_copies(db_session, settings, now=NOW, idle=idle)

    assert [item.episode_id for item in found] == sorted([served.id, never_fetched.id])
    assert all(item.bytes == len(COPY) for item in found)


async def test_idle_rules_skip_copies_not_ready_and_episodes_not_ready(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    preparing = await make_retained_episode(
        db_session, settings, anilist_id=977030, ready_at=days_ago(30)
    )
    not_ready = await make_retained_episode(
        db_session, settings, anilist_id=977031, ready_at=days_ago(30), state=EpisodeState.MATCHED
    )
    encoding = await make_retained_episode(
        db_session, settings, anilist_id=977032, ready_at=days_ago(30)
    )
    await add_copy(
        db_session, settings, preparing.id, state=OfflineCopyState.FAILED, ready_at=days_ago(20)
    )
    # A trip's copy (M19 T3/T4) on an episode that is not ready: not this rule's.
    await add_copy(db_session, settings, not_ready.id, ready_at=days_ago(20))
    await add_copy(db_session, settings, encoding.id, ready_at=days_ago(20))
    await add_encode_job(db_session, encoding.id)

    assert await idle_copies(db_session, settings, now=NOW, idle=timedelta(days=7)) == []


async def test_a_dry_run_deletes_no_idle_copy(db_session: AsyncSession, tmp_path: Path) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=977040, ready_at=days_ago(30)
    )
    copy = await add_copy(db_session, settings, episode.id, ready_at=days_ago(20))
    (idle,) = await idle_copies(db_session, settings, now=NOW, idle=timedelta(days=7))

    assert await delete_idle_copy(db_session, settings, idle, dry_run=True) is True
    assert copy.exists()
    assert await db_session.get(OfflineCopy, episode.id) is not None


# --- How much is on the disk ----------------------------------------------------


async def test_the_disk_figure_counts_the_copies(db_session: AsyncSession, tmp_path: Path) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=977050, ready_at=days_ago(1)
    )
    before = await retained_usage(db_session, settings)
    await add_copy(db_session, settings, episode.id)
    # A copy still being made has no size and holds no finished file.
    other = await make_retained_episode(
        db_session, settings, anilist_id=977051, ready_at=days_ago(1)
    )
    await add_copy(db_session, settings, other.id, state=OfflineCopyState.PREPARING)
    row = await db_session.get(OfflineCopy, other.id)
    assert row is not None
    row.size = None
    await db_session.flush()

    after = await retained_usage(db_session, settings)

    assert before.offline_bytes == 0
    assert after.offline_bytes == len(COPY)
    assert after.total == after.sources + after.renditions + len(COPY)


async def test_an_episode_row_cascade_takes_the_copy_row(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=977060, ready_at=days_ago(1)
    )
    await add_copy(db_session, settings, episode.id)
    await db_session.delete(await db_session.get(Episode, episode.id))
    await db_session.flush()
    db_session.expunge_all()

    assert await db_session.get(OfflineCopy, episode.id) is None
