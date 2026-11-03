"""M21 T5: the retention anchor is the latest finish (FR-T1, FR-W7 item 4).

A rewatch pass never sets ``completed`` (FR-W7), so before M21 a pass finish
left retention with nothing but the old first watch to anchor on, and its row
— not completed, position at the end — read as a *partial* watch that held the
file for D days. The anchor is now ``max(greatest(completed_at, finished_at))``
and a row finished since its last report is not a partial: a pass finish at T
is deletable at T + G (6 h), like any completion.

The rule tests run against the frozen :data:`tests.retention_helpers.NOW`; the
demand and walk tests run the real reconciler, which reads the wall clock, so
they are built from the real one too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from arc.config import Settings
from arc.db import SessionFactory
from arc.main import create_app
from arc.models import Episode, EpisodeState, User, UserRole, Walk, Want, WatchProgress
from arc.services.acquisition.demand import demand
from arc.services.acquisition.wants import STALE_DROP_REASON, compute_wants
from arc.services.playback.progress import record_progress, unmark_watched
from arc.services.playback.rewatch import start as start_rewatch
from arc.services.retention.sweep import (
    HOLD_PARTIAL,
    HOLD_PLAYED,
    REASON_DROPPED,
    REASON_WATCHED,
    Deletable,
    candidates,
    floor_evictions,
    retention_plan,
)
from tests.acquisition_helpers import acquisition_settings, make_user
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login
from tests.retention_helpers import NOW, add_completion, days_ago, make_retained_episode

pytestmark = pytest.mark.pg

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
#: The watched grace (``watched_grace_hours``, default 6).
G = 6 * HOUR
#: D (``unwatched_days_d``, default 21): how long hold (b) keeps a partial watch.
D = timedelta(days=21)
GB = 1024**3
LENGTH = 1440.0


#: The deadline itself is exclusive (``due.at >= now`` keeps it, as since M10),
#: so "deletable at T + G" is checked a minute past it.


def ids(found: Sequence[Deletable]) -> list[int]:
    return [target.episode_id for target in found]


def ran_out(at: datetime) -> str:
    """The tail of a watched reason whose 6-hour grace ran out at ``at``."""
    end = at.astimezone(UTC)
    return f"the 6-hour grace period ran out at {end:%H:%M} UTC on {end.date().isoformat()}"


async def add_finish(
    session: AsyncSession, user: User, episode: Episode, *, at: datetime
) -> WatchProgress:
    """A normal-mode completion at ``at`` with its latest finish, as the backfill
    or a live completion leaves it. Built whole: setting ``finished_at`` on a
    flushed row would fire ``updated_at``'s ``onupdate`` with the wall clock."""
    row = WatchProgress(
        user_id=user.id,
        episode_id=episode.id,
        position_s=LENGTH,
        duration_s=LENGTH,
        completed=True,
        completed_at=at,
        finished_at=at,
        updated_at=at,
    )
    session.add(row)
    await session.flush()
    return row


async def on_demand(session: AsyncSession, user: User, episode: Episode, *, at: datetime) -> None:
    """Pressed play at ``at`` while it still needed fetching; it is ready now.

    A demand on a ``ready`` episode writes nothing (the player just opens it),
    so the press happens before the bytes land, as in life.
    """
    ready = episode.state
    episode.state = EpisodeState.NOT_WANTED
    await session.flush()
    await demand(session, user_id=user.id, episode=episode, now=at)
    episode.state = ready
    await session.flush()


async def play(
    session: AsyncSession, user: User, episode: Episode, fraction: float, *, at: datetime
) -> None:
    await record_progress(
        session,
        user_id=user.id,
        episode=episode,
        position_s=LENGTH * fraction,
        duration_s=LENGTH,
        now=at,
    )


async def pass_finish(
    session: AsyncSession, user: User, episode: Episode, *, at: datetime
) -> WatchProgress:
    """A rewatch pass opened an hour before ``at``, the episode finished in it at ``at``."""
    await start_rewatch(session, user_id=user.id, anime_id=episode.anime_id, now=at - HOUR)
    await play(session, user, episode, 0.3, at=at - 30 * MINUTE)
    await play(session, user, episode, 0.95, at=at)
    row = await session.get(WatchProgress, (user.id, episode.id))
    assert row is not None
    await session.refresh(row)
    # The pass left the flag alone: only the latest finish moved (FR-W7).
    assert row.finished_at == at
    return row


# --- 1. A rewatch pass finish is a fresh watched anchor -----------------------


async def test_a_rewatch_pass_finish_at_t_is_deletable_at_t_plus_g(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974801, ready_at=days_ago(2)
    )
    user = await make_user(db_session, "m21-ret-pass@arc.test")

    row = await pass_finish(db_session, user, episode, at=NOW)
    assert row.completed is False and row.completed_at is None

    before = await retention_plan(db_session, settings, now=NOW + G - MINUTE)
    assert before.deletable == [] and before.held == [], "not past its grace, and not held"

    found = await candidates(db_session, settings, now=NOW + G + MINUTE)
    assert ids(found) == [episode.id]
    assert found[0].reason == f"{REASON_WATCHED}; {ran_out(NOW + G)}"


async def test_a_pass_finish_anchors_over_an_old_normal_completion(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Completed months ago in normal mode, finished again in a pass at T: the anchor is T."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974802, ready_at=days_ago(2)
    )
    user = await make_user(db_session, "m21-ret-old@arc.test")
    await add_finish(db_session, user, episode, at=days_ago(120))

    row = await pass_finish(db_session, user, episode, at=NOW)
    assert row.completed is True and row.completed_at == days_ago(120), "the first watch stays"

    before = await retention_plan(db_session, settings, now=NOW + G - MINUTE)
    # Not past its grace at all — not merely held by the recent play (hold c).
    assert before.deletable == [] and before.held == []

    found = await candidates(db_session, settings, now=NOW + G + MINUTE)
    assert ids(found) == [episode.id]
    assert found[0].reason.endswith(ran_out(NOW + G))


# --- 2. A normal-mode completion anchors as before -----------------------------


async def test_a_normal_mode_completion_still_anchors_on_its_finish(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974803, ready_at=days_ago(2)
    )
    user = await make_user(db_session, "m21-ret-normal@arc.test")

    await play(db_session, user, episode, 0.95, at=NOW)
    row = await db_session.get(WatchProgress, (user.id, episode.id))
    assert row is not None
    await db_session.refresh(row)
    assert row.completed is True and row.completed_at == NOW and row.finished_at == NOW

    assert await candidates(db_session, settings, now=NOW + G - MINUTE) == []
    found = await candidates(db_session, settings, now=NOW + G + MINUTE)
    assert ids(found) == [episode.id]
    assert found[0].reason == f"{REASON_WATCHED}; {ran_out(NOW + G)}"


async def test_a_completion_row_without_a_finish_falls_back_to_completed_at(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """``greatest(completed_at, finished_at)`` ignores NULLs: a row the backfill never touched."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974804, ready_at=days_ago(2)
    )
    user = await make_user(db_session, "m21-ret-fallback@arc.test")
    row = await add_completion(db_session, user, episode, at=NOW - 7 * HOUR)
    assert row.finished_at is None

    found = await candidates(db_session, settings, now=NOW)
    assert ids(found) == [episode.id]
    assert found[0].reason.endswith(ran_out(NOW - HOUR))


# --- 3. A pass finish is not a partial watch ----------------------------------


async def test_a_pass_finish_is_not_a_partial_but_a_later_partial_replay_is(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    finished = await make_retained_episode(
        db_session, settings, anilist_id=974805, ready_at=days_ago(5)
    )
    replayed = await make_retained_episode(
        db_session, settings, anilist_id=974806, ready_at=days_ago(5)
    )
    user = await make_user(db_session, "m21-ret-partial@arc.test")
    await pass_finish(db_session, user, finished, at=days_ago(3))
    await pass_finish(db_session, user, replayed, at=days_ago(3))
    # Then started again from the top, an hour later, and left at 30 %.
    await play(db_session, user, replayed, 0.3, at=days_ago(3) + HOUR)
    row = await db_session.get(WatchProgress, (user.id, replayed.id))
    assert row is not None
    await db_session.refresh(row)
    assert row.completed is False and row.position_s > 0
    assert row.finished_at is not None and row.updated_at > row.finished_at

    plan = await retention_plan(db_session, settings, now=NOW)

    # Hold (b) is D days (21): only the genuine partial is still inside it.
    assert ids(plan.deletable) == [finished.id]
    assert [(item.episode_id, item.hold) for item in plan.held] == [(replayed.id, HOLD_PARTIAL)]


# --- The anchor per row and over users -----------------------------------------


def a_row(user: User, episode: Episode, **values: object) -> WatchProgress:
    """A ``watch_progress`` row built whole (see :func:`add_finish` for why)."""
    return WatchProgress(user_id=user.id, episode_id=episode.id, duration_s=LENGTH, **values)


async def test_the_anchor_is_the_newer_of_the_first_watch_and_the_latest_finish(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """``greatest``, not ``coalesce``: a ``finished_at`` older than ``completed_at``
    does not pull the anchor back (the moment ``GET /api/progress/completed`` reports)."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974814, ready_at=days_ago(30)
    )
    user = await make_user(db_session, "m21-ret-greatest@arc.test")
    db_session.add(
        a_row(
            user,
            episode,
            position_s=LENGTH,
            completed=True,
            completed_at=NOW,
            finished_at=days_ago(2),
            updated_at=NOW,
        )
    )
    await db_session.flush()

    before = await retention_plan(db_session, settings, now=NOW + G - MINUTE)
    # Not past its grace at all — an older anchor would show here as held (c).
    assert before.deletable == [] and before.held == []

    found = await candidates(db_session, settings, now=NOW + G + MINUTE)
    assert ids(found) == [episode.id]
    assert found[0].anchor == NOW


async def test_the_anchor_is_the_newest_finish_over_every_user(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A's old normal completion and B's newer pass finish, and the reverse: the
    episode goes G after the newer one, whoever made it."""
    settings = acquisition_settings(tmp_path)
    pass_newer = await make_retained_episode(
        db_session, settings, anilist_id=974815, ready_at=days_ago(200)
    )
    normal_newer = await make_retained_episode(
        db_session, settings, anilist_id=974816, ready_at=days_ago(200)
    )
    alice = await make_user(db_session, "m21-ret-two-a@arc.test")
    bob = await make_user(db_session, "m21-ret-two-b@arc.test")
    # Alice: an old normal completion of the first, a pass finish of the second.
    await add_finish(db_session, alice, pass_newer, at=days_ago(120))
    await pass_finish(db_session, alice, normal_newer, at=NOW - 4 * HOUR)
    # Bob: the newer of each — a pass finish of the first, a completion of the second.
    await pass_finish(db_session, bob, pass_newer, at=NOW)
    await add_finish(db_session, bob, normal_newer, at=NOW)

    # Past the older one's grace, inside the newer one's: neither goes.
    early = await retention_plan(db_session, settings, now=NOW - 4 * HOUR + G + MINUTE)
    assert early.deletable == [] and early.held == []
    assert await candidates(db_session, settings, now=NOW + G - MINUTE) == []

    found = await candidates(db_session, settings, now=NOW + G + MINUTE)
    assert sorted(ids(found)) == sorted([pass_newer.id, normal_newer.id])
    assert {target.anchor for target in found} == {NOW}


async def test_another_users_live_demand_keeps_a_pass_finished_episode(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    real = datetime.now(UTC).replace(microsecond=0)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974817, ready_at=real - timedelta(days=3)
    )
    alice = await make_user(db_session, "m21-ret-demand-a@arc.test")
    bob = await make_user(db_session, "m21-ret-demand-b@arc.test")
    db_session.add(
        a_row(
            alice,
            episode,
            position_s=LENGTH,
            completed=False,
            finished_at=real - 10 * HOUR,
            updated_at=real - 10 * HOUR,
        )
    )
    await db_session.flush()
    assert ids(await candidates(db_session, settings, now=real)) == [episode.id]

    await on_demand(db_session, bob, episode, at=real - HOUR)
    await compute_wants(db_session)
    want = await db_session.get(Want, (bob.id, episode.id))
    assert want is not None and want.demanded_at is not None and want.dropped_at is None

    plan = await retention_plan(db_session, settings, now=real + timedelta(days=3))
    assert plan.deletable == [] and plan.held == []


# --- Un-marks, replays and the partial boundary ---------------------------------


async def test_an_unmark_in_a_pass_falls_back_to_the_first_watch(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """The un-mark clears the pass's finish only; the old first watch anchors, so
    the episode is past its grace and kept only by hold (c) for the watched grace."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974818, ready_at=days_ago(200)
    )
    user = await make_user(db_session, "m21-ret-unmark@arc.test")
    first = days_ago(120)
    await add_finish(db_session, user, episode, at=first)
    finish = NOW - HOUR
    await pass_finish(db_session, user, episode, at=finish)
    unmarked = finish + MINUTE
    await unmark_watched(db_session, user_id=user.id, episode=episode, now=unmarked)
    row = await db_session.get(WatchProgress, (user.id, episode.id))
    assert row is not None
    await db_session.refresh(row)
    assert row.finished_at is None and row.completed is True and row.completed_at == first

    plan = await retention_plan(db_session, settings, now=unmarked + 30 * MINUTE)
    assert plan.deletable == []
    assert [(item.episode_id, item.hold, item.anchor) for item in plan.held] == [
        (episode.id, HOLD_PLAYED, first)
    ]

    found = await candidates(db_session, settings, now=unmarked + G + MINUTE)
    assert ids(found) == [episode.id]
    assert found[0].anchor == first


async def test_an_unmarked_pass_only_finish_reads_as_a_partial(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Never completed in normal mode, finished in a pass, then un-marked: no
    finish is left and the position is at the end, so hold (b) keeps it D days
    (the documented safe side)."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974819, ready_at=days_ago(30)
    )
    user = await make_user(db_session, "m21-ret-unmark-pass@arc.test")
    finish = days_ago(2)
    await pass_finish(db_session, user, episode, at=finish)
    unmarked = finish + MINUTE
    await unmark_watched(db_session, user_id=user.id, episode=episode, now=unmarked)
    row = await db_session.get(WatchProgress, (user.id, episode.id))
    assert row is not None
    await db_session.refresh(row)
    assert row.finished_at is None and row.completed is False and row.position_s > 0

    plan = await retention_plan(db_session, settings, now=NOW)
    assert plan.deletable == []
    assert [(item.episode_id, item.hold) for item in plan.held] == [(episode.id, HOLD_PARTIAL)]

    late = await retention_plan(db_session, settings, now=unmarked + D - MINUTE)
    assert [(item.episode_id, item.hold) for item in late.held] == [(episode.id, HOLD_PARTIAL)]
    assert ids(await candidates(db_session, settings, now=unmarked + D + MINUTE)) == [episode.id]


async def test_a_replayed_finish_older_than_the_last_sample_reads_as_a_partial(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Out-of-order offline replay: the row's finish is older than its last sample
    (``finished_at < updated_at``), so hold (b) keeps it D days — the documented
    safe side, pinned here. Built as the row such a replay leaves."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974820, ready_at=days_ago(30)
    )
    user = await make_user(db_session, "m21-ret-replay@arc.test")
    finish = days_ago(1)
    sample = finish + 10 * MINUTE
    db_session.add(
        a_row(
            user,
            episode,
            position_s=LENGTH,
            completed=False,
            finished_at=finish,
            updated_at=sample,
        )
    )
    await db_session.flush()

    plan = await retention_plan(db_session, settings, now=NOW)
    assert plan.deletable == []
    assert [(item.episode_id, item.hold) for item in plan.held] == [(episode.id, HOLD_PARTIAL)]
    assert ids(await candidates(db_session, settings, now=sample + D + MINUTE)) == [episode.id]


async def test_the_partial_boundary_is_strictly_after_the_finish(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """``updated_at`` equal to ``finished_at`` is a finish; one microsecond later is
    a partial."""
    settings = acquisition_settings(tmp_path)
    equal = await make_retained_episode(
        db_session, settings, anilist_id=974821, ready_at=days_ago(20)
    )
    later = await make_retained_episode(
        db_session, settings, anilist_id=974822, ready_at=days_ago(20)
    )
    user = await make_user(db_session, "m21-ret-boundary@arc.test")
    at = days_ago(2)
    db_session.add(
        a_row(user, equal, position_s=LENGTH, completed=False, finished_at=at, updated_at=at)
    )
    db_session.add(
        a_row(
            user,
            later,
            position_s=300.0,
            completed=False,
            finished_at=at,
            updated_at=at + timedelta(microseconds=1),
        )
    )
    await db_session.flush()

    plan = await retention_plan(db_session, settings, now=NOW)

    assert ids(plan.deletable) == [equal.id]
    assert [(item.episode_id, item.hold) for item in plan.held] == [(later.id, HOLD_PARTIAL)]


async def test_a_manual_mark_in_a_pass_is_a_finish(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974823, ready_at=days_ago(20)
    )
    user = await make_user(db_session, "m21-ret-mark@arc.test")
    mark = NOW - HOUR
    await start_rewatch(db_session, user_id=user.id, anime_id=episode.anime_id, now=mark - HOUR)
    await play(db_session, user, episode, 0.3, at=mark - 10 * MINUTE)
    await record_progress(
        db_session,
        user_id=user.id,
        episode=episode,
        position_s=LENGTH * 0.3,
        duration_s=LENGTH,
        now=mark,
        force_complete=True,
    )
    row = await db_session.get(WatchProgress, (user.id, episode.id))
    assert row is not None
    await db_session.refresh(row)
    assert row.completed is False and row.finished_at == mark and row.updated_at == mark

    before = await retention_plan(db_session, settings, now=mark + G - MINUTE)
    assert before.deletable == [] and before.held == []
    found = await candidates(db_session, settings, now=mark + G + MINUTE)
    assert ids(found) == [episode.id]
    assert found[0].reason == f"{REASON_WATCHED}; {ran_out(mark + G)}"


# --- 4. Demand lifecycle -------------------------------------------------------


async def test_a_demand_is_kept_while_live_then_goes_g_after_its_finish(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Demanded: never a candidate. Finished: the reconcile deletes the row and
    the finish anchors the watched grace (plan §2.2)."""
    settings = acquisition_settings(tmp_path)
    real = datetime.now(UTC).replace(microsecond=0)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974807, ready_at=real - timedelta(days=2)
    )
    user = await make_user(db_session, "m21-ret-demand@arc.test")
    await on_demand(db_session, user, episode, at=real - HOUR)
    await compute_wants(db_session)
    want = await db_session.get(Want, (user.id, episode.id))
    assert want is not None and want.demanded_at is not None and want.dropped_at is None

    far = await retention_plan(db_session, settings, now=real + timedelta(days=30))
    assert far.deletable == [] and far.held == [], "a live demand is never a candidate"

    finish = real - 30 * MINUTE
    await play(db_session, user, episode, 0.95, at=finish)
    await compute_wants(db_session)
    assert await db_session.get(Want, (user.id, episode.id)) is None, "deleted, not shelved"

    assert await candidates(db_session, settings, now=finish + G - MINUTE) == []
    found = await candidates(db_session, settings, now=finish + G + MINUTE)
    assert ids(found) == [episode.id]
    assert found[0].reason == f"{REASON_WATCHED}; {ran_out(finish + G)}"


async def test_a_stale_demand_follows_the_drop_anchor(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    real = datetime.now(UTC)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974808, ready_at=real - timedelta(days=90)
    )
    user = await make_user(db_session, "m21-ret-stale@arc.test")
    await on_demand(db_session, user, episode, at=real - timedelta(days=60))
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, episode.id))
    assert want is not None and want.dropped_at is not None
    assert want.drop_reason == STALE_DROP_REASON
    dropped = want.dropped_at

    grace = timedelta(days=7)
    assert await candidates(db_session, settings, now=dropped + grace - MINUTE) == []
    found = await candidates(db_session, settings, now=dropped + grace + MINUTE)
    assert ids(found) == [episode.id]
    assert found[0].reason.startswith(REASON_DROPPED)
    assert "7-day grace period" in found[0].reason


# --- 5. A walk want keeps the playing episode ----------------------------------


async def test_a_live_walk_want_keeps_a_watched_episode_past_its_grace(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    real = datetime.now(UTC)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=974809, number=7, ready_at=real - timedelta(days=10)
    )
    alice = await make_user(db_session, "m21-ret-walk-a@arc.test")
    bob = await make_user(db_session, "m21-ret-walk-b@arc.test")
    await add_finish(db_session, alice, episode, at=real - timedelta(days=3))
    assert ids(await candidates(db_session, settings, now=real)) == [episode.id]

    # Bob is walking the show, on episode 7 (the walk sits on the one before).
    db_session.add(Walk(user_id=bob.id, anime_id=episode.anime_id, after_number=6, touched_at=real))
    await db_session.flush()
    await compute_wants(db_session)
    want = await db_session.get(Want, (bob.id, episode.id))
    assert want is not None and want.dropped_at is None and want.demanded_at is None

    plan = await retention_plan(db_session, settings, now=real)
    assert plan.deletable == [] and plan.held == []


# --- 6. The floor ranks a pass finish like any completion ----------------------


async def test_the_floor_ranks_a_pass_finish_by_its_finish(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Least recently watched first; a pass finish three hours ago is a watch, not
    a partial inside the floor's 24-hour exclusion."""
    settings = acquisition_settings(tmp_path)
    week = await make_retained_episode(
        db_session, settings, anilist_id=974810, ready_at=days_ago(10)
    )
    rewatched = await make_retained_episode(
        db_session, settings, anilist_id=974811, ready_at=days_ago(10)
    )
    day = await make_retained_episode(
        db_session, settings, anilist_id=974812, ready_at=days_ago(10)
    )
    alice = await make_user(db_session, "m21-ret-floor@arc.test")
    await add_completion(db_session, alice, week, at=days_ago(7))
    await add_completion(db_session, alice, day, at=days_ago(1))
    # Only ever finished in a pass: ``completed`` is false on the row.
    await pass_finish(db_session, alice, rewatched, at=NOW - 3 * HOUR)

    found = await floor_evictions(db_session, settings, need_bytes=10 * GB, now=NOW)

    assert ids(found) == [week.id, day.id, rewatched.id]


# --- 7. The preview reports the pass finish ------------------------------------


@pytest.fixture
def retention_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def retention_app(
    retention_settings: Settings, pg_engine: AsyncEngine, api_factory: SessionFactory
) -> FastAPI:
    app = create_app(retention_settings)
    app.state.engine = pg_engine
    app.state.session_factory = api_factory
    return app


@pytest.fixture
async def admin(retention_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(retention_app) as client:
        yield await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


async def test_the_preview_reports_a_pass_finish_with_the_watched_reason_and_its_anchor(
    retention_settings: Settings, api_factory: SessionFactory, admin: AsyncClient
) -> None:
    real = datetime.now(UTC).replace(microsecond=0)
    finish = real - 7 * HOUR
    async with api_factory() as session:
        episode = await make_retained_episode(
            session, retention_settings, anilist_id=974813, ready_at=real - timedelta(days=2)
        )
        user = await make_user(session, "m21-ret-preview@arc.test")
        await add_finish(session, user, episode, at=real - timedelta(days=120))
        await pass_finish(session, user, episode, at=finish)
        await session.commit()
        episode_id = episode.id

    response = await admin.get("/api/retention/preview")

    assert response.status_code == 200, response.text
    (row,) = response.json()["episodes"]
    assert row["episode_id"] == episode_id
    assert row["reason"] == f"{REASON_WATCHED}; {ran_out(finish + G)}"
