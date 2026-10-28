"""Re-making a trip's copies with the current encode settings (owner, 2026-10-28).

:mod:`arc.services.trips.remake` and the ``remake-copies`` command, plus the
``etag`` the trip view carries so a device can tell a re-made copy from the
one it holds, and the late-confirmation rule in
:func:`~arc.services.trips.deliver.confirm_delivered`. Built on
:mod:`tests.test_trip_delivery`'s helpers: a trip whose episodes are
trip-only, each with a ready copy on disk.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.cli import EXIT_FAILED, build_parser, cmd_remake_copies
from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    OfflineCopy,
    OfflineCopyState,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    WatchProgress,
)
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.names import COMPUTE_WANTS
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.acquisition.wants import compute_wants
from arc.services.media.download import stat_etag
from arc.services.media.names import OFFLINE_ENCODE, offline_path_for
from arc.services.trips.create import TRIP_NOT_ACTIVE, TripConflict
from arc.services.trips.deliver import confirm_delivered
from arc.services.trips.remake import leaves_alone, remake_stale
from arc.services.trips.view import trip_facts
from tests.acquisition_helpers import acquisition_settings, fake_free_space
from tests.test_trip_delivery import jobs_of, row_of, show, trip_with_copies

pytestmark = pytest.mark.pg


def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    return acquisition_settings(tmp_path)


def args(**values: Any) -> Any:
    return type("Args", (), {"trip": None, "all": False, **values})()


async def record(
    session: AsyncSession, settings: Settings, episodes: list[Episode], *, stale: bool
) -> None:
    """Write what each copy was made with: today's ``OFFLINE_*``, or not (CRF + 4)."""
    for episode in episodes:
        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None
        copy.codec = settings.offline_codec
        copy.height = settings.offline_height
        copy.crf = settings.offline_crf + (4 if stale else 0)
        copy.audio_bitrate = settings.offline_audio_bitrate
    await session.flush()


async def deliver(
    session: AsyncSession, trip: Trip, episode: Episode, *, etag: str | None = None
) -> TripEpisode:
    user = await session.get(User, trip.user_id)
    assert user is not None
    return await confirm_delivered(
        session, user=user, trip_id=trip.id, episode_id=episode.id, etag=etag, now=now()
    )


async def forget_compute_wants(session: AsyncSession) -> None:
    await session.execute(delete(Job).where(Job.type == COMPUTE_WANTS))
    await session.flush()


def counts(report: Any) -> tuple[int, int, int, int, int]:
    return (report.stale, report.repended, report.cleared, report.held, report.skipped)


async def new_copy(session: AsyncSession, settings: Settings, episode: Episode) -> str:
    """The copy made again, as the encode leaves it: other bytes, a new ETag."""
    path = offline_path_for(settings, episode.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"n" * 8192)
    info = os.stat(path)
    etag = stat_etag(info.st_size, info.st_mtime_ns)
    session.add(
        OfflineCopy(
            episode_id=episode.id,
            state=OfflineCopyState.READY,
            size=info.st_size,
            etag=etag,
            codec=settings.offline_codec,
            height=settings.offline_height,
            crf=settings.offline_crf,
            audio_bitrate=settings.offline_audio_bitrate,
            ready_at=now(),
        )
    )
    await session.flush()
    return etag


# --- Pure -------------------------------------------------------------------


def test_only_a_live_row_is_put_back() -> None:
    def row(state: TripEpisodeState, *, released: bool = False) -> TripEpisode:
        return TripEpisode(
            trip_id=1, episode_id=1, state=state, released_at=now() if released else None
        )

    assert not leaves_alone(row(TripEpisodeState.PENDING))
    assert not leaves_alone(row(TripEpisodeState.PENDING), watched=True)
    assert not leaves_alone(row(TripEpisodeState.DELIVERED))
    assert leaves_alone(row(TripEpisodeState.DELIVERED), watched=True)
    assert leaves_alone(row(TripEpisodeState.DELIVERED, released=True))
    assert leaves_alone(row(TripEpisodeState.CANCELLED))
    assert leaves_alone(row(TripEpisodeState.EXPIRED))


def test_the_command_is_reachable_and_wants_a_target() -> None:
    parser = build_parser()

    assert parser.parse_args(["remake-copies", "--trip", "4"]).trip == 4
    assert parser.parse_args(["remake-copies", "--all"]).all is True
    with pytest.raises(SystemExit):
        parser.parse_args(["remake-copies"])
    with pytest.raises(SystemExit):
        parser.parse_args(["remake-copies", "--trip", "4", "--all"])


# --- remake_stale -----------------------------------------------------------


async def test_a_delivered_stale_copy_goes_and_its_row_is_pending_again(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990101, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r1@arc.test", count=2)
    await record(db_session, settings, episodes[:1], stale=True)
    await deliver(db_session, trip, episodes[0])
    await forget_compute_wants(db_session)
    path = offline_path_for(settings, episodes[0].id)
    assert path.exists()
    at = now()

    report = await remake_stale(db_session, settings, trip=trip, now=at)

    assert counts(report) == (1, 1, 0, 0, 0)
    row = await row_of(db_session, trip, episodes[0])
    assert row.state is TripEpisodeState.PENDING
    assert row.delivered_at is None
    assert row.released_at is None
    assert row.available_at == at, "the copy-days clock restarts"
    assert await db_session.get(OfflineCopy, episodes[0].id, populate_existing=True) is None
    assert not path.exists()
    assert len(await jobs_of(db_session, COMPUTE_WANTS)) == 1


async def test_a_pending_stale_copy_goes_and_its_clock_restarts(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990102, count=3)
    _, trip = await trip_with_copies(
        db_session, settings, episodes, "r2@arc.test", count=2, at=now() - timedelta(days=13)
    )
    await record(db_session, settings, episodes[:2], stale=True)
    await forget_compute_wants(db_session)
    at = now()

    report = await remake_stale(db_session, settings, trip=trip, now=at)

    assert counts(report) == (2, 0, 2, 0, 0)
    for episode in episodes[:2]:
        row = await row_of(db_session, trip, episode)
        assert row.state is TripEpisodeState.PENDING
        assert row.available_at == at
        assert await db_session.get(OfflineCopy, episode.id, populate_existing=True) is None
    # Once, for the whole trip.
    assert len(await jobs_of(db_session, COMPUTE_WANTS)) == 1


async def test_a_copy_made_with_the_current_settings_is_left_alone(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990103, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r3@arc.test", count=2)
    await record(db_session, settings, episodes[:1], stale=False)
    # Episode 1's row recorded nothing (never started): not stale either.
    await forget_compute_wants(db_session)
    before = (await row_of(db_session, trip, episodes[0])).available_at

    report = await remake_stale(db_session, settings, trip=trip, now=now())

    assert counts(report) == (0, 0, 0, 0, 0)
    assert (await row_of(db_session, trip, episodes[0])).available_at == before
    for episode in episodes[:2]:
        assert await db_session.get(OfflineCopy, episode.id) is not None
    assert await jobs_of(db_session, COMPUTE_WANTS) == []


async def test_the_languages_do_not_make_a_copy_stale(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A copy's ``settings_key`` carries the languages; the remake ignores it."""
    episodes = await show(db_session, 990113, count=2)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r13@arc.test", count=1)
    await record(db_session, settings, episodes[:1], stale=False)
    copy = await db_session.get(OfflineCopy, episodes[0].id)
    assert copy is not None
    copy.settings_key = "0123456789abcdef"  # whatever the rules said that day
    await db_session.flush()

    assert counts(await remake_stale(db_session, settings, trip=trip, now=now())) == (
        0,
        0,
        0,
        0,
        0,
    )


async def test_a_changed_setting_makes_a_current_copy_stale(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The real case: copies made at 720p H.264, the host now at 1080p HEVC."""
    episodes = await show(db_session, 990104, count=2)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r4@arc.test", count=1)
    await record(db_session, settings, episodes[:1], stale=False)
    changed = settings.model_copy(
        update={"offline_codec": "hevc", "offline_height": 1080, "offline_crf": 28}
    )

    report = await remake_stale(db_session, changed, trip=trip, now=now())

    assert (report.stale, report.cleared) == (1, 1)


async def test_an_episode_the_device_let_go_of_or_watched_is_never_put_back(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990105, count=5)
    user, trip = await trip_with_copies(db_session, settings, episodes, "r5@arc.test", count=5)
    await record(db_session, settings, episodes[:5], stale=True)
    # 0: delivered, then deleted on the device. 1: cancelled. 2: expired.
    # 3: delivered and watched. 4: still pending, so the trip stays active.
    await deliver(db_session, trip, episodes[0])
    (await row_of(db_session, trip, episodes[0])).released_at = now()
    (await row_of(db_session, trip, episodes[1])).state = TripEpisodeState.CANCELLED
    (await row_of(db_session, trip, episodes[2])).state = TripEpisodeState.EXPIRED
    await deliver(db_session, trip, episodes[3])
    db_session.add(
        WatchProgress(user_id=user.id, episode_id=episodes[3].id, position_s=1400.0, completed=True)
    )
    await db_session.flush()
    await forget_compute_wants(db_session)

    report = await remake_stale(db_session, settings, trip=trip, now=now())

    assert counts(report) == (5, 0, 1, 0, 4)
    kept = await row_of(db_session, trip, episodes[0])
    assert kept.state is TripEpisodeState.DELIVERED and kept.released_at is not None
    assert (await row_of(db_session, trip, episodes[1])).state is TripEpisodeState.CANCELLED
    assert (await row_of(db_session, trip, episodes[2])).state is TripEpisodeState.EXPIRED
    assert (await row_of(db_session, trip, episodes[3])).state is TripEpisodeState.DELIVERED
    for episode in episodes[:4]:
        assert await db_session.get(OfflineCopy, episode.id) is not None
        assert offline_path_for(settings, episode.id).exists()
    assert await db_session.get(OfflineCopy, episodes[4].id, populate_existing=True) is None


async def test_a_copy_a_device_is_fetching_is_held(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990114, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r14@arc.test", count=2)
    await record(db_session, settings, episodes[:2], stale=True)
    fetching = await db_session.get(OfflineCopy, episodes[0].id)
    assert fetching is not None
    fetching.last_served_at = now() - timedelta(minutes=1)
    await db_session.flush()
    before = (await row_of(db_session, trip, episodes[0])).available_at

    report = await remake_stale(db_session, settings, trip=trip, now=now())

    assert counts(report) == (2, 0, 1, 1, 0)
    assert await db_session.get(OfflineCopy, episodes[0].id) is not None
    assert offline_path_for(settings, episodes[0].id).exists()
    assert (await row_of(db_session, trip, episodes[0])).available_at == before


async def test_a_queued_encode_is_dropped_and_a_running_one_left_alone(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990106, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r6@arc.test", count=2)
    await record(db_session, settings, episodes[:2], stale=True)
    queued = Job(
        type=OFFLINE_ENCODE,
        payload={"episode_id": episodes[0].id, "why": "trip"},
        status=JobStatus.PENDING,
    )
    running = Job(
        type=OFFLINE_ENCODE,
        payload={"episode_id": episodes[1].id, "why": "trip"},
        status=JobStatus.RUNNING,
    )
    db_session.add_all([queued, running])
    await db_session.flush()
    available = (await row_of(db_session, trip, episodes[1])).available_at

    report = await remake_stale(db_session, settings, trip=trip, now=now())

    assert counts(report) == (2, 0, 1, 0, 1)
    await db_session.refresh(queued)
    await db_session.refresh(running)
    assert queued.status is JobStatus.CANCELLED
    assert running.status is JobStatus.RUNNING
    assert await db_session.get(OfflineCopy, episodes[0].id, populate_existing=True) is None
    assert await db_session.get(OfflineCopy, episodes[1].id, populate_existing=True) is not None
    assert (await row_of(db_session, trip, episodes[1])).available_at == available


async def test_another_users_trip_on_the_same_episode_follows(
    db_session: AsyncSession, settings: Settings
) -> None:
    """One copy, two trips: the named trip's remake re-pends the other's row too."""
    episodes = await show(db_session, 990115, count=3)
    _, mine = await trip_with_copies(db_session, settings, episodes, "r15a@arc.test", count=2)
    _, theirs = await trip_with_copies(db_session, settings, episodes, "r15b@arc.test", count=2)
    await record(db_session, settings, episodes[:1], stale=True)
    await deliver(db_session, theirs, episodes[0])
    at = now()

    report = await remake_stale(db_session, settings, trip=mine, now=at)

    assert counts(report) == (1, 0, 1, 0, 0), "the named trip's own rows only"
    other = await row_of(db_session, theirs, episodes[0])
    assert other.state is TripEpisodeState.PENDING
    assert other.delivered_at is None
    assert other.available_at == at
    assert await db_session.get(OfflineCopy, episodes[0].id, populate_existing=True) is None


async def test_an_ended_trip_is_refused(db_session: AsyncSession, settings: Settings) -> None:
    episodes = await show(db_session, 990107, count=2)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r7@arc.test", count=1)
    await record(db_session, settings, episodes[:1], stale=True)
    trip.state = TripState.FINISHED
    await db_session.flush()

    with pytest.raises(TripConflict) as refused:
        await remake_stale(db_session, settings, trip=trip, now=now())

    assert str(refused.value) == TRIP_NOT_ACTIVE
    assert await db_session.get(OfflineCopy, episodes[0].id) is not None


async def test_a_removed_trip_only_copy_brings_the_want_back(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Pending row, no copy, source long gone: the reconciler wants it again."""
    episodes = await show(db_session, 990116, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r16@arc.test", count=2)
    await record(db_session, settings, episodes[:1], stale=True)
    assert episodes[0].state is EpisodeState.NOT_WANTED

    await remake_stale(db_session, settings, trip=trip, now=now())
    await compute_wants(db_session, settings=settings)

    episode = await db_session.get(Episode, episodes[0].id, populate_existing=True)
    assert episode is not None
    assert episode.state is EpisodeState.WANTED
    # And the episode whose copy was current stays put.
    untouched = await db_session.get(Episode, episodes[1].id, populate_existing=True)
    assert untouched is not None and untouched.state is EpisodeState.NOT_WANTED


# --- The late confirmation (the race) -----------------------------------------


async def test_a_late_confirmation_of_the_old_copy_leaves_the_row_waiting(
    db_session: AsyncSession, settings: Settings
) -> None:
    """remake → old-ETag confirm → still pending → new copy → new-ETag confirm."""
    episodes = await show(db_session, 990117, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r17@arc.test", count=2)
    await record(db_session, settings, episodes[:1], stale=True)
    old = await db_session.get(OfflineCopy, episodes[0].id)
    assert old is not None and old.etag is not None
    old_etag = old.etag

    await remake_stale(db_session, settings, trip=trip, now=now())
    await forget_compute_wants(db_session)
    settles = len(await jobs_of(db_session, "offline_settle"))

    # The device finished the old copy just before and confirms it now.
    row = await deliver(db_session, trip, episodes[0], etag=old_etag)

    assert row.state is TripEpisodeState.PENDING
    assert row.delivered_at is None
    assert len(await jobs_of(db_session, "offline_settle")) == settles
    assert await jobs_of(db_session, COMPUTE_WANTS) == []
    trip_now = await db_session.get(Trip, trip.id, populate_existing=True)
    assert trip_now is not None and trip_now.state is TripState.ACTIVE

    # The copy is made again: offered with its new ETag.
    new_etag = await new_copy(db_session, settings, episodes[0])
    facts = {r.episode_id: r for r in (await trip_facts(db_session, settings, trip)).episodes}
    assert facts[episodes[0].id].phase == "available"
    assert facts[episodes[0].id].etag == new_etag != old_etag

    # The device replaced its copy and confirms the new one.
    row = await deliver(db_session, trip, episodes[0], etag=new_etag)
    assert row.state is TripEpisodeState.DELIVERED
    assert row.delivered_at is not None


async def test_a_confirmation_without_an_etag_or_of_another_row_is_as_before(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990118, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r18@arc.test", count=2)

    row = await deliver(db_session, trip, episodes[0], etag=None)

    assert row.state is TripEpisodeState.DELIVERED


# --- The trip view's etag -----------------------------------------------------


async def test_the_view_carries_the_copys_etag_only_while_it_can_be_fetched(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990108, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r8@arc.test", count=2)
    copy = await db_session.get(OfflineCopy, episodes[0].id)
    assert copy is not None and copy.etag is not None
    await db_session.delete(copy)  # episode 0: no copy, nothing to fetch
    await db_session.flush()

    facts = {row.episode_id: row for row in (await trip_facts(db_session, settings, trip)).episodes}

    assert facts[episodes[0].id].etag is None
    second = await db_session.get(OfflineCopy, episodes[1].id)
    assert second is not None
    assert facts[episodes[1].id].fetchable
    assert facts[episodes[1].id].etag == second.etag


# --- The command --------------------------------------------------------------


async def test_the_command_remakes_one_trip_and_says_what_it_did(
    db_session: AsyncSession, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    episodes = await show(db_session, 990109, count=4)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r9@arc.test", count=3)
    await record(db_session, settings, episodes[:2], stale=True)
    await deliver(db_session, trip, episodes[0])
    await db_session.commit()

    assert await cmd_remake_copies(db_session, settings, args(trip=trip.id)) == 0

    out = capsys.readouterr().out
    assert f"trip {trip.id}: 2 stale, 1 re-pended, 1 cleared, 0 held, 0 skipped" in out
    # Run again: nothing stale is left.
    assert await cmd_remake_copies(db_session, settings, args(trip=trip.id)) == 0
    assert f"trip {trip.id}: 0 stale" in capsys.readouterr().out


async def test_the_command_covers_every_active_trip_with_all(
    db_session: AsyncSession, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    first = await show(db_session, 990110, count=2)
    second = await show(db_session, 990111, count=2)
    _, one = await trip_with_copies(db_session, settings, first, "r10@arc.test", count=1)
    _, two = await trip_with_copies(db_session, settings, second, "r11@arc.test", count=1)
    await record(db_session, settings, [first[0], second[0]], stale=True)
    await db_session.commit()

    assert await cmd_remake_copies(db_session, settings, args(all=True)) == 0

    lines = capsys.readouterr().out.splitlines()
    assert f"trip {one.id}: 1 stale, 0 re-pended, 1 cleared, 0 held, 0 skipped" in lines
    assert f"trip {two.id}: 1 stale, 0 re-pended, 1 cleared, 0 held, 0 skipped" in lines


async def test_the_command_refuses_an_unknown_or_ended_trip(
    db_session: AsyncSession, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    episodes = await show(db_session, 990112, count=2)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r12@arc.test", count=1)
    trip.state = TripState.CANCELLED
    await db_session.commit()
    missing = int(await db_session.scalar(select(Trip.id).order_by(Trip.id.desc())) or 0) + 1000

    assert await cmd_remake_copies(db_session, settings, args(trip=missing)) == EXIT_FAILED
    trip_id = trip.id
    assert await cmd_remake_copies(db_session, settings, args(trip=trip_id)) == EXIT_FAILED
    err = capsys.readouterr().err
    assert f"no trip with id {missing}" in err
    assert f"trip {trip_id} is not active" in err
