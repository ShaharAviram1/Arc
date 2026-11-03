"""Rewatch mode: a pass through a show that leaves the list alone (FR-W7, M21).

The owner's words of 2026-11-03: "a rewatch path, that will follow the same
logic of normal shows, but not changing the mal entry." A per-show switch,
kept by the server per user, open on any show with an aired episode whatever
its list status. While it is on, the show is in a **pass** — one
``rewatches`` row with ``ended_at`` null — and:

* a progress report on the show records the position and, at the mark, the
  episode's *latest finish* (``watch_progress.finished_at``), and **nothing
  else**: no list progress, no entry created or activated (FR-A9), no
  auto-complete (FR-W5), no MyAnimeList write. That branch lives in
  :func:`arc.services.playback.progress.record_progress`, which asks
  :func:`rewatch_at` which pass (if any) was open when the report was recorded;
* the pass's episodes are the ones whose latest finish is at or after its
  start (:func:`pass_finished`), and its *position* is the highest of their
  numbers (:func:`pass_position`) — there is no cursor column to drift;
* it ends when the user turns it off (:func:`stop`) or on finishing the last
  episode of the pass (:func:`pass_end_number`): the show's last episode when
  it has finished airing and its count is known, otherwise the newest aired
  one (owner-accepted 2026-11-03). A pass never reopens by itself.

Ended rows are kept: an offline report replayed later (FR-S8) is judged by the
pass that was open when the device recorded it — ``started_at <= at <
coalesce(ended_at, ∞)`` — so a replay arriving after the pass ended is still
free of list and MAL writes and does not reopen it, and one recorded before the
pass began is normal mode.

**Nothing here imports** :mod:`arc.services.playback.progress`: that module
asks this one which mode a report is in, so the dependency runs one way.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import ColumnElement, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    REWATCH_END_FINISHED,
    REWATCH_END_OFF,
    Anime,
    Episode,
    ListEntry,
    Rewatch,
    WatchProgress,
)
from arc.models.tracking import OPEN_REWATCH_PREDICATE
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.catalog.airing import FINISHED, aired_episodes


class RewatchError(Exception):
    """A pass that cannot be started; the message is a sentence a person reads."""


class NothingAired(RewatchError):
    """The show has no aired episode, so there is nothing to rewatch."""


#: Shown on the show page when the switch is pressed on a show with nothing aired.
NOTHING_AIRED_DETAIL = "This show has no aired episode to rewatch yet."


def pass_end_number(
    *, anime_status: str | None, episode_count: int | None, newest_aired: int
) -> int:
    """The number whose finish ends a pass (pure; FR-W7 item 4).

    The show's episode count when it has **finished airing and the count is
    known**; otherwise the newest aired episode (owner-accepted 2026-11-03: a
    pass on an airing show ends where the broadcast has got to).
    """
    if anime_status == FINISHED and episode_count:
        return int(episode_count)
    return newest_aired


def in_pass(finished_at: datetime | None, started_at: datetime) -> bool:
    """Whether a latest finish belongs to the pass that started at ``started_at`` (pure)."""
    return finished_at is not None and finished_at >= started_at


async def newest_aired_number(session: AsyncSession, anime: Anime, *, now: datetime) -> int:
    """The highest aired episode number of ``anime``, or 0 when nothing has aired.

    The catalogue's own rule (:func:`~arc.services.catalog.airing.aired_episodes`),
    read from there rather than re-derived, so the show page, acquisition and a
    pass agree on what has aired.
    """
    episodes = list(
        (
            await session.scalars(
                select(Episode).where(Episode.anime_id == anime.id).order_by(Episode.number)
            )
        ).all()
    )
    aired = aired_episodes(
        episodes, now=now, anime_status=anime.status, next_airing=anime.next_airing
    )
    return max((episode.number for episode in aired), default=0)


async def end_number(session: AsyncSession, anime: Anime, *, now: datetime) -> int:
    """:func:`pass_end_number` for ``anime`` as it stands at ``now``."""
    return pass_end_number(
        anime_status=anime.status,
        episode_count=anime.episodes,
        newest_aired=await newest_aired_number(session, anime, now=now),
    )


async def open_rewatch(session: AsyncSession, *, user_id: int, anime_id: int) -> Rewatch | None:
    """The user's open pass on the show, if there is one."""
    found: Rewatch | None = await session.scalar(
        select(Rewatch)
        .where(
            Rewatch.user_id == user_id,
            Rewatch.anime_id == anime_id,
            Rewatch.ended_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    return found


async def open_rewatches(session: AsyncSession, *, user_id: int) -> list[Rewatch]:
    """Every open pass of one user, oldest first."""
    rows = await session.scalars(
        select(Rewatch)
        .where(Rewatch.user_id == user_id, Rewatch.ended_at.is_(None))
        .order_by(Rewatch.started_at, Rewatch.id)
    )
    return list(rows.all())


async def rewatch_at(
    session: AsyncSession, *, user_id: int, anime_id: int, at: datetime
) -> Rewatch | None:
    """The pass that was open at ``at``: ``started_at <= at < coalesce(ended_at, ∞)``.

    Ended passes included — that is what judges an offline replay by the
    moment the device recorded it (FR-S8, FR-W7 item 4). The latest-started
    match wins should two ever overlap (they cannot while the partial unique
    index holds and :func:`stop` stamps ``now``).
    """
    found: Rewatch | None = await session.scalar(
        select(Rewatch)
        .where(
            Rewatch.user_id == user_id,
            Rewatch.anime_id == anime_id,
            Rewatch.started_at <= at,
            or_(Rewatch.ended_at.is_(None), Rewatch.ended_at > at),
        )
        .order_by(Rewatch.started_at.desc(), Rewatch.id.desc())
        .limit(1)
        # Fresh column values: ``stop``/``finish`` end passes with a Core UPDATE.
        .execution_options(populate_existing=True)
    )
    return found


async def finished_pass_tail(
    session: AsyncSession,
    *,
    user_id: int,
    episode: Episode,
    at: datetime,
    old_position_s: float | None,
    old_duration_s: float | None,
    old_finished_at: datetime | None,
    completion_fraction: float,
) -> Rewatch | None:
    """The pass a report *after* its end still belongs to, as the tail of its last episode.

    A pass ends the moment its last episode crosses the mark, and the player
    keeps reporting through the credits. Without this, those later reports are
    normal mode — the episode has no ``completed`` row from the pass — and the
    first of them would advance the list to the end, auto-complete the show
    and write both to MyAnimeList: exactly what FR-W7 says a pass never does.

    So a report recorded after the user's latest pass on the show ended
    ``finished`` is still judged in that pass when the episode was finished in
    it (its latest finish is at or after the pass's start) and the row was
    still at or past the mark before this report — the player has not gone
    back under the mark since. Scrubbing back under the mark, or opening the
    episode afresh, ends the tail; a manual mark never reaches here.
    """
    if old_finished_at is None or old_position_s is None or not old_duration_s:
        return None
    if old_duration_s <= 0 or old_position_s / old_duration_s < completion_fraction:
        return None
    found: Rewatch | None = await session.scalar(
        select(Rewatch)
        .where(
            Rewatch.user_id == user_id,
            Rewatch.anime_id == episode.anime_id,
            Rewatch.ended_at.is_not(None),
            Rewatch.ended_at <= at,
        )
        .order_by(Rewatch.started_at.desc(), Rewatch.id.desc())
        .limit(1)
        # Fresh column values: ``stop``/``finish`` end passes with a Core UPDATE.
        .execution_options(populate_existing=True)
    )
    if found is None or found.end_reason != REWATCH_END_FINISHED:
        return None
    if not in_pass(old_finished_at, found.started_at):
        return None
    return found


async def start(session: AsyncSession, *, user_id: int, anime_id: int, now: datetime) -> Rewatch:
    """Open a pass on the show, or return the one already open (idempotent).

    Refused with :class:`NothingAired` for a show with no aired episode.
    Raises :class:`LookupError` for an unknown show. Two requests racing are
    settled by the partial unique index: the loser's insert does nothing and
    it returns the winner's pass. Queues a reconciliation — the list window is
    suspended for the pass and its own window starts at episode 1 (FR-W7
    item 1). Flushed, not committed.
    """
    existing = await open_rewatch(session, user_id=user_id, anime_id=anime_id)
    if existing is not None:
        return existing
    anime = await session.get(Anime, anime_id)
    if anime is None:
        raise LookupError(anime_id)
    if await newest_aired_number(session, anime, now=now) <= 0:
        raise NothingAired(NOTHING_AIRED_DETAIL)

    statement = (
        pg_insert(Rewatch)
        .values(user_id=user_id, anime_id=anime_id, started_at=now)
        .on_conflict_do_nothing(
            index_elements=[Rewatch.user_id, Rewatch.anime_id],
            index_where=text(OPEN_REWATCH_PREDICATE),
        )
        .returning(Rewatch.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is None:
        # Another request opened it between the read above and this insert.
        raced = await open_rewatch(session, user_id=user_id, anime_id=anime_id)
        if raced is None:  # pragma: no cover - the index said one is open
            raise RewatchError("the open pass vanished")
        return raced
    await enqueue_compute_wants(session)
    opened = await session.get(Rewatch, inserted)
    assert opened is not None
    return opened


async def stop(
    session: AsyncSession,
    *,
    user_id: int,
    anime_id: int,
    now: datetime,
    reason: str = REWATCH_END_OFF,
) -> bool:
    """End the open pass, if there is one (idempotent). Whether one was ended.

    The list window comes back, so a reconciliation is queued when a pass
    actually ended. Flushed, not committed.
    """
    ended = await session.scalar(
        update(Rewatch)
        .where(
            Rewatch.user_id == user_id,
            Rewatch.anime_id == anime_id,
            Rewatch.ended_at.is_(None),
        )
        .values(ended_at=now, end_reason=reason)
        .returning(Rewatch.id)
    )
    if ended is None:
        return False
    await enqueue_compute_wants(session)
    return True


async def finish(session: AsyncSession, rewatch: Rewatch, *, at: datetime) -> bool:
    """End ``rewatch`` as ``finished`` at ``at``, unless it has already ended.

    Called by the progress path when the pass's last episode is finished. A
    pass that was already ended (off, or by a concurrent report) is left as it
    is: a pass never reopens and never ends twice.
    """
    ended = await session.scalar(
        update(Rewatch)
        .where(Rewatch.id == rewatch.id, Rewatch.ended_at.is_(None))
        .values(ended_at=at, end_reason=REWATCH_END_FINISHED)
        .returning(Rewatch.id)
    )
    if ended is None:
        return False
    await session.refresh(rewatch)
    await enqueue_compute_wants(session)
    return True


def _pass_rows(rewatch: Rewatch) -> tuple[ColumnElement[bool], ...]:
    return (
        WatchProgress.user_id == rewatch.user_id,
        Episode.anime_id == rewatch.anime_id,
        WatchProgress.finished_at >= rewatch.started_at,
    )


async def pass_finished(
    session: AsyncSession, rewatch: Rewatch, *, episode_ids: Collection[int] | None = None
) -> frozenset[int]:
    """The episode ids finished in the pass: latest finish at or after its start."""
    statement = (
        select(WatchProgress.episode_id)
        .join(Episode, Episode.id == WatchProgress.episode_id)
        .where(*_pass_rows(rewatch))
    )
    if episode_ids is not None:
        if not episode_ids:
            return frozenset()
        statement = statement.where(WatchProgress.episode_id.in_(set(episode_ids)))
    return frozenset((await session.scalars(statement)).all())


async def pass_position(session: AsyncSession, rewatch: Rewatch) -> int:
    """The highest episode number finished in the pass, 0 at the start."""
    found = await session.scalar(
        select(func.max(Episode.number))
        .select_from(WatchProgress)
        .join(Episode, Episode.id == WatchProgress.episode_id)
        .where(*_pass_rows(rewatch))
    )
    return int(found or 0)


@dataclass(frozen=True, slots=True)
class RewatchSummary:
    """What the show page says about an open pass: "Rewatching · ep X / Y"."""

    started_at: datetime
    position: int
    finished_in_pass: int
    total: int


async def summary(session: AsyncSession, rewatch: Rewatch, *, now: datetime) -> RewatchSummary:
    """The pass's start, position, number of finished episodes and last number."""
    anime = await session.get(Anime, rewatch.anime_id)
    total = 0 if anime is None else await end_number(session, anime, now=now)
    return RewatchSummary(
        started_at=rewatch.started_at,
        position=await pass_position(session, rewatch),
        finished_in_pass=len(await pass_finished(session, rewatch)),
        total=total,
    )


class WatchView(NamedTuple):
    """One user's watched state of one show, as FR-W5 reads it (M21).

    ``completed_ids`` are the episodes Arc holds a finish for and
    ``list_progress`` the list's number, to be fed to
    :func:`~arc.services.playback.watched.watched_source` per episode. In an
    open pass they are the pass's finishes and 0, so ``watched_source`` answers
    ``arc`` for pass episodes only and the list counts for nothing until the
    pass ends (FR-W5, FR-W7 item 2).
    """

    completed_ids: frozenset[int]
    list_progress: int
    rewatch: Rewatch | None = None


async def watch_view(
    session: AsyncSession,
    *,
    user_id: int,
    anime_id: int,
    episode_ids: Collection[int] | None = None,
) -> WatchView:
    """``(completed_ids, list_progress)`` for one user and show, pass-aware.

    ``episode_ids`` narrows ``completed_ids`` to the episodes a page asks about;
    ``None`` reads the whole show.
    """
    rewatch = await open_rewatch(session, user_id=user_id, anime_id=anime_id)
    if rewatch is not None:
        return WatchView(await pass_finished(session, rewatch, episode_ids=episode_ids), 0, rewatch)
    statement = (
        select(WatchProgress.episode_id)
        .join(Episode, Episode.id == WatchProgress.episode_id)
        .where(
            WatchProgress.user_id == user_id,
            Episode.anime_id == anime_id,
            WatchProgress.completed.is_(True),
        )
    )
    completed: frozenset[int] = frozenset()
    if episode_ids is None or episode_ids:
        if episode_ids is not None:
            statement = statement.where(WatchProgress.episode_id.in_(set(episode_ids)))
        completed = frozenset((await session.scalars(statement)).all())
    entry = await session.get(ListEntry, (user_id, anime_id))
    return WatchView(completed, entry.progress if entry is not None else 0)


__all__ = [
    "NOTHING_AIRED_DETAIL",
    "NothingAired",
    "RewatchError",
    "RewatchSummary",
    "WatchView",
    "end_number",
    "finish",
    "finished_pass_tail",
    "in_pass",
    "newest_aired_number",
    "open_rewatch",
    "open_rewatches",
    "pass_end_number",
    "pass_finished",
    "pass_position",
    "rewatch_at",
    "start",
    "stop",
    "summary",
    "watch_view",
]
