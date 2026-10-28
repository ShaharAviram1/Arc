"""Re-making a trip's copies with the current encode settings (owner, 2026-10-28).

:mod:`arc.services.trips.remake` and the ``remake-copies`` command, plus the
``etag`` the trip view now carries so a device can tell a re-made copy from
the one it holds. Built on :mod:`tests.test_trip_delivery`'s helpers: a trip
whose episodes are trip-only, each with a ready copy on disk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.cli import EXIT_FAILED, build_parser, cmd_remake_copies
from arc.config import Settings
from arc.models import (
    Episode,
    Job,
    JobStatus,
    OfflineCopy,
    OfflineCopyState,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
)
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.names import COMPUTE_WANTS
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.media.names import OFFLINE_ENCODE, offline_path_for
from arc.services.media.offline import current_settings_key
from arc.services.trips.create import TRIP_NOT_ACTIVE, TripConflict
from arc.services.trips.deliver import confirm_delivered
from arc.services.trips.remake import leaves_alone, remake_stale
from arc.services.trips.view import trip_facts
from tests.acquisition_helpers import acquisition_settings, fake_free_space
from tests.test_trip_delivery import (
    jobs_of,
    row_of,
    show,
    trip_with_copies,
)

pytestmark = pytest.mark.pg

#: A key no current setting produces: a copy made "some other way".
STALE_KEY = "0123456789abcdef"


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    return acquisition_settings(tmp_path)


def now() -> datetime:
    return datetime.now(UTC)


def args(**values: Any) -> Any:
    return type("Args", (), {"trip": None, "all": False, **values})()


async def key_copies(
    session: AsyncSession, settings: Settings, episodes: list[Episode], key: str | None
) -> None:
    """Stamp each episode's copy row with ``key`` (``"current"`` = what now makes)."""
    for episode in episodes:
        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None
        copy.settings_key = (
            await current_settings_key(session, settings, episode.id) if key == "current" else key
        )
    await session.flush()


async def deliver(session: AsyncSession, trip: Trip, episode: Episode) -> None:
    user = await session.get(User, trip.user_id)
    assert user is not None
    await confirm_delivered(
        session, user=user, trip_id=trip.id, episode_id=episode.id, etag=None, now=now()
    )


async def forget_compute_wants(session: AsyncSession) -> None:
    await session.execute(delete(Job).where(Job.type == COMPUTE_WANTS))
    await session.flush()


# --- Pure -------------------------------------------------------------------


def test_only_a_live_row_is_put_back() -> None:
    def row(state: TripEpisodeState, *, released: bool = False) -> TripEpisode:
        return TripEpisode(
            trip_id=1, episode_id=1, state=state, released_at=now() if released else None
        )

    assert not leaves_alone(row(TripEpisodeState.PENDING))
    assert not leaves_alone(row(TripEpisodeState.DELIVERED))
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
    await key_copies(db_session, settings, episodes[:1], STALE_KEY)
    await deliver(db_session, trip, episodes[0])
    await forget_compute_wants(db_session)
    path = offline_path_for(settings, episodes[0].id)
    assert path.exists()

    report = await remake_stale(db_session, settings, trip=trip, now=now())

    assert (report.stale, report.repended, report.cleared, report.skipped) == (1, 1, 0, 0)
    row = await row_of(db_session, trip, episodes[0])
    assert row.state is TripEpisodeState.PENDING
    assert row.delivered_at is None
    assert row.released_at is None
    assert row.available_at is None
    assert await db_session.get(OfflineCopy, episodes[0].id, populate_existing=True) is None
    assert not path.exists()
    assert len(await jobs_of(db_session, COMPUTE_WANTS)) == 1


async def test_a_pending_stale_copy_goes_and_its_clock_is_cleared(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990102, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r2@arc.test", count=2)
    await key_copies(db_session, settings, episodes[:2], STALE_KEY)
    assert (await row_of(db_session, trip, episodes[0])).available_at is not None
    await forget_compute_wants(db_session)

    report = await remake_stale(db_session, settings, trip=trip, now=now())

    assert (report.stale, report.repended, report.cleared, report.skipped) == (2, 0, 2, 0)
    for episode in episodes[:2]:
        row = await row_of(db_session, trip, episode)
        assert row.state is TripEpisodeState.PENDING
        assert row.available_at is None
        assert await db_session.get(OfflineCopy, episode.id, populate_existing=True) is None
    # Once, for the whole trip.
    assert len(await jobs_of(db_session, COMPUTE_WANTS)) == 1


async def test_a_copy_made_with_the_current_settings_is_left_alone(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990103, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r3@arc.test", count=2)
    await key_copies(db_session, settings, episodes[:1], "current")
    await key_copies(db_session, settings, episodes[1:2], None)  # never started: not stale
    await forget_compute_wants(db_session)
    before = (await row_of(db_session, trip, episodes[0])).available_at

    report = await remake_stale(db_session, settings, trip=trip, now=now())

    assert (report.stale, report.repended, report.cleared, report.skipped) == (0, 0, 0, 0)
    assert (await row_of(db_session, trip, episodes[0])).available_at == before
    for episode in episodes[:2]:
        assert await db_session.get(OfflineCopy, episode.id) is not None
    assert await jobs_of(db_session, COMPUTE_WANTS) == []


async def test_a_changed_setting_makes_a_current_copy_stale(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The real case: copies made at 720p H.264, the host now at 1080p HEVC."""
    episodes = await show(db_session, 990104, count=2)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r4@arc.test", count=1)
    await key_copies(db_session, settings, episodes[:1], "current")
    changed = settings.model_copy(
        update={"offline_codec": "hevc", "offline_height": 1080, "offline_crf": 28}
    )

    report = await remake_stale(db_session, changed, trip=trip, now=now())

    assert (report.stale, report.cleared) == (1, 1)


async def test_an_episode_the_device_let_go_of_is_never_put_back(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990105, count=4)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r5@arc.test", count=4)
    await key_copies(db_session, settings, episodes[:4], STALE_KEY)
    # 0: delivered, then deleted on the device (watched). 1: cancelled.
    # 2: expired. 3: still pending, so the trip stays active.
    await deliver(db_session, trip, episodes[0])
    released = await row_of(db_session, trip, episodes[0])
    released.released_at = now()
    (await row_of(db_session, trip, episodes[1])).state = TripEpisodeState.CANCELLED
    (await row_of(db_session, trip, episodes[2])).state = TripEpisodeState.EXPIRED
    await db_session.flush()
    await forget_compute_wants(db_session)

    report = await remake_stale(db_session, settings, trip=trip, now=now())

    assert (report.stale, report.repended, report.cleared, report.skipped) == (4, 0, 1, 3)
    kept = await row_of(db_session, trip, episodes[0])
    assert kept.state is TripEpisodeState.DELIVERED
    assert kept.released_at is not None
    assert (await row_of(db_session, trip, episodes[1])).state is TripEpisodeState.CANCELLED
    assert (await row_of(db_session, trip, episodes[2])).state is TripEpisodeState.EXPIRED
    for episode in episodes[:3]:
        assert await db_session.get(OfflineCopy, episode.id) is not None
        assert offline_path_for(settings, episode.id).exists()
    assert await db_session.get(OfflineCopy, episodes[3].id, populate_existing=True) is None


async def test_a_queued_encode_is_dropped_and_a_running_one_left_alone(
    db_session: AsyncSession, settings: Settings
) -> None:
    episodes = await show(db_session, 990106, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r6@arc.test", count=2)
    await key_copies(db_session, settings, episodes[:2], STALE_KEY)
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
    running_copy = await db_session.get(OfflineCopy, episodes[1].id)
    assert running_copy is not None
    running_copy.state = OfflineCopyState.PREPARING
    available = (await row_of(db_session, trip, episodes[1])).available_at
    await db_session.flush()

    report = await remake_stale(db_session, settings, trip=trip, now=now())

    assert (report.stale, report.cleared, report.skipped) == (2, 1, 1)
    await db_session.refresh(queued)
    await db_session.refresh(running)
    assert queued.status is JobStatus.CANCELLED
    assert running.status is JobStatus.RUNNING
    assert await db_session.get(OfflineCopy, episodes[0].id, populate_existing=True) is None
    assert await db_session.get(OfflineCopy, episodes[1].id, populate_existing=True) is not None
    assert (await row_of(db_session, trip, episodes[1])).available_at == available


async def test_an_ended_trip_is_refused(db_session: AsyncSession, settings: Settings) -> None:
    episodes = await show(db_session, 990107, count=2)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r7@arc.test", count=1)
    await key_copies(db_session, settings, episodes[:1], STALE_KEY)
    trip.state = TripState.FINISHED
    await db_session.flush()

    with pytest.raises(TripConflict) as refused:
        await remake_stale(db_session, settings, trip=trip, now=now())

    assert str(refused.value) == TRIP_NOT_ACTIVE
    assert await db_session.get(OfflineCopy, episodes[0].id) is not None


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
    episodes = await show(db_session, 990109, count=3)
    _, trip = await trip_with_copies(db_session, settings, episodes, "r9@arc.test", count=2)
    await key_copies(db_session, settings, episodes[:2], STALE_KEY)
    await deliver(db_session, trip, episodes[0])
    await db_session.commit()

    assert await cmd_remake_copies(db_session, settings, args(trip=trip.id)) == 0

    out = capsys.readouterr().out
    assert f"trip {trip.id}: 2 stale, 1 re-pended, 1 cleared, 0 skipped" in out
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
    await key_copies(db_session, settings, [first[0], second[0]], STALE_KEY)
    await db_session.commit()

    assert await cmd_remake_copies(db_session, settings, args(all=True)) == 0

    lines = capsys.readouterr().out.splitlines()
    assert f"trip {one.id}: 1 stale, 0 re-pended, 1 cleared, 0 skipped" in lines
    assert f"trip {two.id}: 1 stale, 0 re-pended, 1 cleared, 0 skipped" in lines


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
