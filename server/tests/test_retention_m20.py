"""Delete after watching, the four holds, and the disk-floor eviction (M20 T8).

FR-T1 as rewritten on 2026-10-13: a *watched* anchor uses
``watched_grace_hours`` (6), a drop and "nobody ever wanted it" keep G days
(7); a pin, a recent partial watch, a recent play and a copy being served
each hold an episode past its grace. FR-T6: under the disk floor the sweep
evicts least recently watched first, grace and pins ignored, never a live
want.

The rule tests run :func:`candidates` against the frozen :data:`NOW`; the job
tests run the real handler against the wall clock with ``disk_usage`` mocked.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.api import retention as retention_api
from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    OfflineCopy,
    OfflineCopyState,
    ShowPin,
    User,
    WatchProgress,
)
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.jobs.registry import JobContext
from arc.services.media.names import output_dir_for
from arc.services.pins import pinned_usage
from arc.services.retention import floor as retention_floor
from arc.services.retention import jobs as retention_jobs
from arc.services.retention.jobs import retention_sweep
from arc.services.retention.names import RETENTION_SWEEP
from arc.services.retention.rules import watched_grace_hours
from arc.services.retention.sweep import (
    HOLD_PARTIAL,
    HOLD_PINNED,
    HOLD_PLAYED,
    HOLD_SERVING,
    REASON_FLOOR,
    Deletable,
    candidates,
    floor_evictions,
    retention_plan,
    targets_for_episode,
)
from arc.services.storage import DiskUsage
from tests.acquisition_helpers import acquisition_settings, make_user, set_setting
from tests.retention_helpers import (
    NOW,
    add_completion,
    add_partial,
    add_want,
    days_ago,
    make_retained_episode,
)
from tests.test_retention import set_progress_entry

pytestmark = pytest.mark.pg

HOUR = timedelta(hours=1)
GB = 1024**3


def hours_ago(hours: float, *, now: datetime = NOW) -> datetime:
    return now - hours * HOUR


async def a_watched_episode(
    session: AsyncSession,
    settings: Settings,
    *,
    anilist_id: int,
    watched: datetime,
    email: str,
    ready_at: datetime | None = None,
) -> tuple[Episode, User]:
    """An episode one user finished at ``watched``."""
    episode = await make_retained_episode(
        session, settings, anilist_id=anilist_id, ready_at=ready_at or watched - 2 * HOUR
    )
    user = await make_user(session, email)
    await add_completion(session, user, episode, at=watched)
    return episode, user


def ids(found: list) -> list[int]:  # type: ignore[type-arg]
    return [target.episode_id for target in found]


# --- The grace by anchor kind -----------------------------------------------


async def test_the_watched_grace_defaults_to_six_hours(db_session: AsyncSession) -> None:
    assert await watched_grace_hours(db_session) == 6


async def test_completed_five_hours_ago_is_kept(db_session: AsyncSession, tmp_path: Path) -> None:
    settings = acquisition_settings(tmp_path)
    await a_watched_episode(
        db_session, settings, anilist_id=972001, watched=hours_ago(5), email="m20-5h@arc.test"
    )

    assert await candidates(db_session, settings, now=NOW) == []


async def test_completed_seven_hours_ago_goes_with_an_hours_reason(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode, _ = await a_watched_episode(
        db_session, settings, anilist_id=972002, watched=hours_ago(7), email="m20-7h@arc.test"
    )

    found = await candidates(db_session, settings, now=NOW)

    assert ids(found) == [episode.id]
    # NOW is 12:00 UTC; finished at 05:00, so six hours ran out at 11:00.
    assert found[0].reason == (
        "everyone who wanted it has watched it; "
        "the 6-hour grace period ran out at 11:00 UTC on 2026-06-01"
    )


async def test_list_progress_also_uses_the_hours_grace(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """FR-W5's watched (progress passed it) is a watched anchor too."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=972003, number=4, ready_at=days_ago(3)
    )
    user = await make_user(db_session, "m20-progress@arc.test")
    await set_progress_entry(db_session, user, episode, progress=9, updated_at=hours_ago(8))

    found = await candidates(db_session, settings, now=NOW)

    assert ids(found) == [episode.id]
    assert "6-hour grace period" in found[0].reason


async def test_a_drop_two_days_ago_keeps_g_days(db_session: AsyncSession, tmp_path: Path) -> None:
    """A drop is not a watch: G (7 days) still applies."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=972004, ready_at=days_ago(10)
    )
    user = await make_user(db_session, "m20-drop@arc.test")
    await add_want(db_session, user, episode, dropped_at=days_ago(2), drop_reason="stale")

    assert await candidates(db_session, settings, now=NOW) == []

    later = await candidates(db_session, settings, now=NOW + timedelta(days=6))
    assert ids(later) == [episode.id]
    assert "7-day grace period" in later[0].reason


async def test_nobody_ever_wanted_keeps_g_days(db_session: AsyncSession, tmp_path: Path) -> None:
    settings = acquisition_settings(tmp_path)
    await make_retained_episode(db_session, settings, anilist_id=972005, ready_at=days_ago(2))

    assert await candidates(db_session, settings, now=NOW) == []


# --- The four holds -----------------------------------------------------------


async def test_a_show_pinned_by_another_user_is_kept(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode, _ = await a_watched_episode(
        db_session, settings, anilist_id=972010, watched=days_ago(3), email="m20-pin-a@arc.test"
    )
    other = await make_user(db_session, "m20-pin-b@arc.test")
    db_session.add(ShowPin(user_id=other.id, anime_id=episode.anime_id))
    await db_session.flush()

    plan = await retention_plan(db_session, settings, now=NOW)

    assert plan.deletable == []
    assert [(item.episode_id, item.hold) for item in plan.held] == [(episode.id, HOLD_PINNED)]
    assert "6-hour grace period" in plan.held[0].reason


async def test_another_user_part_way_through_holds_it(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """User B at 40 % three days ago, with no live want (show on hold)."""
    settings = acquisition_settings(tmp_path)
    episode, _ = await a_watched_episode(
        db_session, settings, anilist_id=972011, watched=days_ago(4), email="m20-part-a@arc.test"
    )
    bob = await make_user(db_session, "m20-part-b@arc.test")
    await add_partial(db_session, bob, episode, at=days_ago(3), position_s=576.0)

    plan = await retention_plan(db_session, settings, now=NOW)

    assert plan.deletable == []
    assert [item.hold for item in plan.held] == [HOLD_PARTIAL]


async def test_a_partial_watch_older_than_d_does_not_hold(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode, _ = await a_watched_episode(
        db_session, settings, anilist_id=972012, watched=days_ago(30), email="m20-old-a@arc.test"
    )
    bob = await make_user(db_session, "m20-old-b@arc.test")
    await add_partial(db_session, bob, episode, at=days_ago(22))

    assert ids(await candidates(db_session, settings, now=NOW)) == [episode.id]


async def test_a_rewatch_within_the_grace_holds_it(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """``completed_at`` is written once; ``updated_at`` moves on a rewatch."""
    settings = acquisition_settings(tmp_path)
    episode, alice = await a_watched_episode(
        db_session, settings, anilist_id=972013, watched=days_ago(5), email="m20-re@arc.test"
    )
    await db_session.execute(
        update(WatchProgress)
        .where(WatchProgress.user_id == alice.id, WatchProgress.episode_id == episode.id)
        .values(updated_at=hours_ago(2))
    )

    plan = await retention_plan(db_session, settings, now=NOW)

    assert plan.deletable == []
    assert [item.hold for item in plan.held] == [HOLD_PLAYED]


async def test_a_copy_served_five_minutes_ago_holds_it(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode, _ = await a_watched_episode(
        db_session, settings, anilist_id=972014, watched=hours_ago(7), email="m20-copy@arc.test"
    )
    db_session.add(
        OfflineCopy(
            episode_id=episode.id,
            state=OfflineCopyState.READY,
            size=10,
            codec="h264",
            ready_at=hours_ago(8),
            last_served_at=NOW - timedelta(minutes=5),
        )
    )
    await db_session.flush()

    plan = await retention_plan(db_session, settings, now=NOW)
    assert plan.deletable == []
    assert [item.hold for item in plan.held] == [HOLD_SERVING]

    # Fifteen minutes later nobody is fetching it any more.
    later = await candidates(db_session, settings, now=NOW + timedelta(minutes=15))
    assert ids(later) == [episode.id]


# --- Floor eviction: the ranking ---------------------------------------------


async def test_floor_evictions_ignore_grace_and_pins_but_never_a_live_want(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    pinned, owner = await a_watched_episode(
        db_session, settings, anilist_id=972020, watched=days_ago(30), email="m20-f1@arc.test"
    )
    db_session.add(ShowPin(user_id=owner.id, anime_id=pinned.anime_id))
    recent, _ = await a_watched_episode(
        db_session, settings, anilist_id=972021, watched=hours_ago(3), email="m20-f2@arc.test"
    )
    newest, _ = await a_watched_episode(
        db_session, settings, anilist_id=972022, watched=hours_ago(1), email="m20-f3@arc.test"
    )
    wanted = await make_retained_episode(
        db_session, settings, anilist_id=972023, ready_at=days_ago(90)
    )
    await add_want(db_session, owner, wanted)
    partial, _ = await a_watched_episode(
        db_session, settings, anilist_id=972024, watched=days_ago(60), email="m20-f4@arc.test"
    )
    watcher = await make_user(db_session, "m20-f5@arc.test")
    await add_partial(db_session, watcher, partial, at=hours_ago(20))
    await db_session.flush()

    everything = await floor_evictions(db_session, settings, need_bytes=10 * GB, now=NOW)
    assert ids(everything) == [pinned.id, recent.id, newest.id]
    assert all(item.reason.startswith(REASON_FLOOR) for item in everything)

    one = await floor_evictions(db_session, settings, need_bytes=1, now=NOW)
    assert ids(one) == [pinned.id]
    assert await floor_evictions(db_session, settings, need_bytes=0, now=NOW) == []


# --- The job ------------------------------------------------------------------


def context(session: AsyncSession, settings: Settings) -> JobContext:
    job = Job(type=RETENTION_SWEEP, payload={}, status=JobStatus.RUNNING, attempts=1)
    job.id = 1
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("arc.jobs.test")
    )


def mock_disk(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    episodes: list[int],
    *,
    free_gb: float,
) -> None:
    """Free space = ``free_gb`` plus 1 GB for each of ``episodes`` deleted."""

    def fake(path: Path) -> DiskUsage:
        gone = sum(
            1 for episode_id in episodes if not output_dir_for(settings, episode_id).exists()
        )
        free = int((free_gb + gone) * GB)
        return DiskUsage(total=100 * GB, used=100 * GB - free, free=free)

    patch_disk(monkeypatch, fake)


def patch_disk(monkeypatch: pytest.MonkeyPatch, fake: Callable[[Path], DiskUsage]) -> None:
    """Every place the sweep and its preview measure the disk."""
    monkeypatch.setattr(acquisition_rules, "disk_usage", fake)
    monkeypatch.setattr(retention_floor, "disk_usage", fake)


async def floor_scene(session: AsyncSession, settings: Settings) -> dict[str, Episode]:
    """Four episodes against the wall clock, none of them due for the sweep."""
    now = datetime.now(UTC)
    owner = await make_user(session, "m20-job-owner@arc.test")
    scene: dict[str, Episode] = {}
    # Pinned and watched a month ago: the least recently watched.
    scene["pinned"] = await make_retained_episode(
        session, settings, anilist_id=972030, ready_at=now - timedelta(days=31)
    )
    await add_completion(session, owner, scene["pinned"], at=now - timedelta(days=30))
    session.add(ShowPin(user_id=owner.id, anime_id=scene["pinned"].anime_id))
    # Unpinned, watched four hours ago: inside the grace.
    scene["older"] = await make_retained_episode(
        session, settings, anilist_id=972031, ready_at=now - timedelta(days=1)
    )
    await add_completion(session, owner, scene["older"], at=now - 4 * HOUR)
    # Unpinned, watched an hour ago.
    scene["newer"] = await make_retained_episode(
        session, settings, anilist_id=972032, ready_at=now - timedelta(days=1)
    )
    await add_completion(session, owner, scene["newer"], at=now - HOUR)
    # The oldest file of all, with a live want: never evicted.
    scene["wanted"] = await make_retained_episode(
        session, settings, anilist_id=972033, ready_at=now - timedelta(days=90)
    )
    await add_want(session, owner, scene["wanted"])
    await set_setting(session, "min_free_gb", 10)
    await session.commit()
    return scene


async def test_under_the_floor_the_sweep_evicts_lrw_until_a_gib_above(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Free 9 GB with floor 10: evict until 11 GB; never the wanted one."""
    settings = acquisition_settings(tmp_path)
    scene = await floor_scene(db_session, settings)
    mock_disk(monkeypatch, settings, [episode.id for episode in scene.values()], free_gb=9)

    await retention_sweep(context(db_session, settings))

    for name in ("pinned", "older"):
        await db_session.refresh(scene[name])
        assert scene[name].state is EpisodeState.NOT_WANTED, name
        assert not output_dir_for(settings, scene[name].id).exists(), name
    for name in ("newer", "wanted"):
        await db_session.refresh(scene[name])
        assert scene[name].state is EpisodeState.READY, name
        assert output_dir_for(settings, scene[name].id).exists(), name


async def test_above_the_floor_nothing_is_evicted(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    scene = await floor_scene(db_session, settings)
    mock_disk(monkeypatch, settings, [episode.id for episode in scene.values()], free_gb=50)

    await retention_sweep(context(db_session, settings))

    for episode in scene.values():
        await db_session.refresh(episode)
        assert episode.state is EpisodeState.READY
        assert output_dir_for(settings, episode.id).exists()


async def test_a_dry_run_under_the_floor_deletes_nothing(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path, retention_dry_run=True)
    scene = await floor_scene(db_session, settings)
    mock_disk(monkeypatch, settings, [episode.id for episode in scene.values()], free_gb=9)

    await retention_sweep(context(db_session, settings))

    for episode in scene.values():
        await db_session.refresh(episode)
        assert episode.state is EpisodeState.READY
        assert output_dir_for(settings, episode.id).exists()


async def test_the_preview_is_what_the_sweep_deletes(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletable and held as the preview says, then the sweep takes exactly the
    deletable ones (disk above the floor)."""
    settings = acquisition_settings(tmp_path)
    now = datetime.now(UTC)
    alice = await make_user(db_session, "m20-pv-a@arc.test")
    bob = await make_user(db_session, "m20-pv-b@arc.test")

    gone = await make_retained_episode(
        db_session, settings, anilist_id=972040, ready_at=now - timedelta(days=2)
    )
    await add_completion(db_session, alice, gone, at=now - 7 * HOUR)
    fresh = await make_retained_episode(
        db_session, settings, anilist_id=972041, ready_at=now - timedelta(days=2)
    )
    await add_completion(db_session, alice, fresh, at=now - 5 * HOUR)
    pinned = await make_retained_episode(
        db_session, settings, anilist_id=972042, ready_at=now - timedelta(days=2)
    )
    await add_completion(db_session, alice, pinned, at=now - timedelta(days=1))
    db_session.add(ShowPin(user_id=bob.id, anime_id=pinned.anime_id))
    partial = await make_retained_episode(
        db_session, settings, anilist_id=972043, ready_at=now - timedelta(days=5)
    )
    await add_completion(db_session, alice, partial, at=now - timedelta(days=4))
    await add_partial(db_session, bob, partial, at=now - timedelta(days=3))
    await db_session.commit()
    all_ids = [gone.id, fresh.id, pinned.id, partial.id]
    mock_disk(monkeypatch, settings, all_ids, free_gb=50)

    plan = await retention_plan(db_session, settings, now=now)
    assert ids(plan.deletable) == [gone.id]
    assert {item.episode_id: item.hold for item in plan.held} == {
        pinned.id: HOLD_PINNED,
        partial.id: HOLD_PARTIAL,
    }

    await retention_sweep(context(db_session, settings))

    deleted = [
        episode_id for episode_id in all_ids if not output_dir_for(settings, episode_id).exists()
    ]
    assert deleted == ids(plan.deletable)


async def test_pinned_usage_counts_a_pinned_shows_retained_bytes(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode, user = await a_watched_episode(
        db_session, settings, anilist_id=972050, watched=days_ago(3), email="m20-usage@arc.test"
    )
    db_session.add(ShowPin(user_id=user.id, anime_id=episode.anime_id))
    await db_session.flush()

    (show,) = await pinned_usage(db_session, settings)
    held = (await retention_plan(db_session, settings, now=NOW)).held

    assert (show.anime_id, show.users, show.episodes) == (episode.anime_id, 1, 1)
    assert show.bytes == held[0].bytes > 0


# --- Review fixes (M20 T8 review) ---------------------------------------------


async def test_a_completion_after_a_drop_does_not_cut_the_drops_grace(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """B drops at T−8h, A completes at T−7h: kept until the drop's G days."""
    settings = acquisition_settings(tmp_path)
    episode, _ = await a_watched_episode(
        db_session, settings, anilist_id=972060, watched=hours_ago(7), email="m20-dc-a@arc.test"
    )
    bob = await make_user(db_session, "m20-dc-b@arc.test")
    await add_want(db_session, bob, episode, dropped_at=hours_ago(8), drop_reason="stale")

    assert await candidates(db_session, settings, now=NOW) == []

    later = await candidates(db_session, settings, now=hours_ago(8) + timedelta(days=7, hours=1))
    assert ids(later) == [episode.id]
    assert later[0].reason.startswith("every want on it was dropped; the 7-day grace period")


async def test_a_zero_hour_grace_still_holds_a_play_for_thirty_minutes(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """``watched_grace_hours = 0``: completed at 90 %, the last minutes playing."""
    settings = acquisition_settings(tmp_path)
    await set_setting(db_session, "watched_grace_hours", 0)
    episode, alice = await a_watched_episode(
        db_session, settings, anilist_id=972061, watched=hours_ago(1), email="m20-zero@arc.test"
    )
    await db_session.execute(
        update(WatchProgress)
        .where(WatchProgress.user_id == alice.id, WatchProgress.episode_id == episode.id)
        .values(updated_at=NOW - timedelta(minutes=10))
    )

    plan = await retention_plan(db_session, settings, now=NOW)
    assert plan.deletable == []
    assert [item.hold for item in plan.held] == [HOLD_PLAYED]
    assert "0-hour grace period" in plan.held[0].reason

    later = await candidates(db_session, settings, now=NOW + timedelta(minutes=25))
    assert ids(later) == [episode.id]


async def test_a_copy_hold_lapses_six_hours_after_the_deadline(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A device fetching in a loop cannot hold an episode for ever."""
    settings = acquisition_settings(tmp_path)
    episode, _ = await a_watched_episode(
        db_session, settings, anilist_id=972062, watched=days_ago(2), email="m20-loop@arc.test"
    )
    db_session.add(
        OfflineCopy(
            episode_id=episode.id,
            state=OfflineCopyState.READY,
            size=10,
            codec="h264",
            ready_at=days_ago(2),
            last_served_at=NOW - timedelta(minutes=5),
        )
    )
    await db_session.flush()

    assert ids(await candidates(db_session, settings, now=NOW)) == [episode.id]


async def test_the_floor_skips_an_episode_played_in_the_last_half_hour(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    old, _ = await a_watched_episode(
        db_session, settings, anilist_id=972063, watched=days_ago(300), email="m20-pl-a@arc.test"
    )
    playing, bob = await a_watched_episode(
        db_session, settings, anilist_id=972064, watched=days_ago(400), email="m20-pl-b@arc.test"
    )
    # Bob is rewatching the oldest one right now.
    await db_session.execute(
        update(WatchProgress)
        .where(WatchProgress.user_id == bob.id, WatchProgress.episode_id == playing.id)
        .values(updated_at=NOW - timedelta(minutes=5))
    )

    found = await floor_evictions(db_session, settings, need_bytes=10 * GB, now=NOW)
    assert ids(found) == [old.id]


async def test_floor_order_is_least_recently_watched_including_rewatches(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A year-old completion rewatched yesterday goes after a week-old one."""
    settings = acquisition_settings(tmp_path)
    rewatched, alice = await a_watched_episode(
        db_session, settings, anilist_id=972065, watched=days_ago(365), email="m20-lrw-a@arc.test"
    )
    await db_session.execute(
        update(WatchProgress)
        .where(WatchProgress.user_id == alice.id, WatchProgress.episode_id == rewatched.id)
        .values(updated_at=days_ago(1))
    )
    week, _ = await a_watched_episode(
        db_session, settings, anilist_id=972066, watched=days_ago(7), email="m20-lrw-b@arc.test"
    )

    found = await floor_evictions(db_session, settings, need_bytes=10 * GB, now=NOW)
    assert ids(found) == [week.id, rewatched.id]


async def test_the_floor_loop_asks_again_before_each_eviction(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Played or fetched since the plan was made: not evicted."""
    settings = acquisition_settings(tmp_path)
    scene = await floor_scene(db_session, settings)
    mock_disk(monkeypatch, settings, [episode.id for episode in scene.values()], free_gb=9)
    stale = await floor_evictions(db_session, settings, need_bytes=10 * GB, now=datetime.now(UTC))
    assert ids(stale)[:2] == [scene["pinned"].id, scene["older"].id]

    # Since the plan: somebody pressed play on the first, a device started
    # fetching the second's copy.
    await db_session.execute(
        update(WatchProgress)
        .where(WatchProgress.episode_id == scene["pinned"].id)
        .values(updated_at=datetime.now(UTC))
    )
    db_session.add(
        OfflineCopy(
            episode_id=scene["older"].id,
            state=OfflineCopyState.READY,
            size=10,
            codec="h264",
            ready_at=datetime.now(UTC) - HOUR,
            last_served_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    async def the_stale_plan(*args: object, **kwargs: object) -> list[Deletable]:
        return stale

    monkeypatch.setattr(retention_jobs, "floor_plan", the_stale_plan)
    await retention_sweep(context(db_session, settings))

    for name, state in (
        ("pinned", EpisodeState.READY),
        ("older", EpisodeState.READY),
        ("newer", EpisodeState.NOT_WANTED),
    ):
        await db_session.refresh(scene[name])
        assert scene[name].state is state, name


async def test_the_preview_lists_the_floor_evictions_the_sweep_makes(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview's ``floor`` is exactly what the sweep then evicts."""
    settings = acquisition_settings(tmp_path)
    scene = await floor_scene(db_session, settings)
    sizes = {
        episode.id: (await targets_for_episode(db_session, settings, episode.id)).bytes
        for episode in scene.values()
    }
    # Just under the 10 GB floor, and the disk gains exactly what is deleted:
    # the plan's byte counts and the measurements agree, so the sweep evicts
    # the whole plan (the files are KiB, the need is over a GiB) and nothing
    # else — never the episode with a live want.
    base = 10 * BYTES_PER_GB - 1000

    def fake(path: Path) -> DiskUsage:
        gone = sum(
            size
            for episode_id, size in sizes.items()
            if not output_dir_for(settings, episode_id).exists()
        )
        free = base + gone
        return DiskUsage(total=100 * GB, used=100 * GB - free, free=free)

    patch_disk(monkeypatch, fake)

    body = await retention_api.preview(db_session, settings)
    planned = [row.episode_id for row in body.floor]
    assert planned == [scene["pinned"].id, scene["older"].id, scene["newer"].id]
    assert all(row.reason.startswith(REASON_FLOOR) for row in body.floor)

    await retention_sweep(context(db_session, settings))

    evicted = [
        episode_id for episode_id in sizes if not output_dir_for(settings, episode_id).exists()
    ]
    assert evicted == planned


async def test_a_dry_run_floor_does_not_count_the_normal_pass_twice(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under RETENTION_DRY_RUN the normal pass's episodes are excluded from the
    floor plan, and their bytes count as freed."""
    settings = acquisition_settings(tmp_path, retention_dry_run=True)
    now = datetime.now(UTC)
    alice = await make_user(db_session, "m20-dry@arc.test")
    due = await make_retained_episode(
        db_session, settings, anilist_id=972067, ready_at=now - timedelta(days=3)
    )
    await add_completion(db_session, alice, due, at=now - timedelta(days=2))
    await set_setting(db_session, "min_free_gb", 10)
    await db_session.commit()

    def nine_gb(path: Path) -> DiskUsage:
        return DiskUsage(total=100 * GB, used=91 * GB, free=9 * GB)

    patch_disk(monkeypatch, nine_gb)
    seen: list[frozenset[int]] = []
    real = retention_floor.floor_evictions

    async def spy(*args: Any, **kwargs: Any) -> list[Deletable]:
        seen.append(kwargs["exclude"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(retention_floor, "floor_evictions", spy)
    await retention_sweep(context(db_session, settings))

    assert seen == [frozenset({due.id})]
    await db_session.refresh(due)
    assert due.state is EpisodeState.READY


async def test_a_no_files_reset_held_by_a_copy_being_sent_is_listed_as_held(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=972068, with_rendition=False, with_source=False
    )
    db_session.add(
        OfflineCopy(
            episode_id=episode.id,
            state=OfflineCopyState.READY,
            size=10,
            codec="h264",
            ready_at=days_ago(1),
            last_served_at=NOW - timedelta(minutes=5),
        )
    )
    await db_session.flush()

    plan = await retention_plan(db_session, settings, now=NOW)
    assert plan.deletable == []
    assert [(item.episode_id, item.hold) for item in plan.held] == [(episode.id, HOLD_SERVING)]

    later = await candidates(db_session, settings, now=NOW + timedelta(minutes=15))
    assert ids(later) == [episode.id]
