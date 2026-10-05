"""Replaying what a device recorded while it had no connection (FR-S8).

The client keeps an on-device queue (``client/src/offline/outbox.ts``) of three
kinds of record, and ``POST /api/sync`` hands them here in the order the device
made them. Each one is applied through **the same service function the online
path uses** — :func:`~arc.services.playback.progress.record_progress` for a
position sample and for a completion, and
:func:`~arc.services.playback.progress.unmark_watched` for an un-mark — so
FR-S4, the MyAnimeList write log and the "never lowers except on an explicit
un-mark" guard hold here by construction rather than by a second copy of them.
There is no other road to MyAnimeList in this module, and there must never be.

**Why a replayed completion is user-originated.** It is a record that this
user watched this episode to the completion mark (or pressed "mark watched")
at a moment the device wrote down; the network being away at the time does
not make it any less the user's own act (owner, 2026-10-04). It takes the
identical ``force_complete`` path :func:`arc.api.playback.mark_watched` takes,
so it advances the list only upwards, logs one progress write carrying the
previous value, and — because ``newly_completed`` comes from the upsert's
``RETURNING old.completed`` — is idempotent: replayed twice, or replayed after
the server already had it, it changes nothing and logs nothing.

**Time.** Every ``at`` is the device's clock. It is first moved by the
device's skew — ``server_now − sent_at``, ``sent_at`` being the device's clock
when it sent the batch — and then clamped into ``[now − 30 days, now]``
(:func:`adjust_timestamp`). The shift makes "before" and "after" mean the
same thing on two devices whose clocks disagree; the clamp keeps a broken
clock (1971, next year) from writing a ``completed_at`` that would skew
retention's grace window or the continue-watching order.

**The merge rule, per kind** (``at`` as adjusted):

* *position* — ``stale`` when the server's row is at least as new
  (``watch_progress.updated_at >= at``): another device, the online reporter
  or an un-mark already said something later. Otherwise applied, with ``at``
  as the row's ``updated_at``. A sample past 90 % completes the episode
  exactly as an online report would.
* *completion* — ``stale`` when the user took the mark back at or after it
  (``watch_progress.unmarked_at >= at``, owner 2026-10-05): an offline rewatch
  must not override an un-mark made later elsewhere, and a flush retried after
  a lost response must not override one made in between. Otherwise applied;
  if the row is newer, its position is kept and only the flag is set. A newer
  completion leaves ``unmarked_at`` as history.
* *unmark* — ``stale`` when the episode was completed after it
  (``completed_at > at``), or when the list entry was changed after it
  (``list_entries.updated_at > at``, read once at the start of the batch so
  this device's own earlier records in the batch do not count): a later
  completion, or a list the user raised later, is the user's newer word.
  Otherwise FR-S4's un-mark rule, unchanged.

**A failure is not a verdict.** An item that raises inside its savepoint (a
deadlock, a dropped connection) is answered ``retry``: the client keeps it
pending and sends it again. ``rejected`` is only for what will never work —
an episode that no longer exists, a record that is not valid.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Episode, ListEntry, WatchProgress
from arc.services.playback.progress import record_progress, unmark_watched

log = logging.getLogger(__name__)

#: The most records one request may carry. A device offline for a week of
#: binge-watching produces a few dozen; the client sends in chunks of this.
MAX_BATCH: Final[int] = 200

#: How far back a device's timestamp is believed. A month offline is far
#: beyond any real trip; anything older is a broken clock, and is read as the
#: oldest moment Arc will believe rather than as 1971.
MAX_AGE: Final[timedelta] = timedelta(days=30)

#: An absolute floor under that, for a server whose own clock is wrong.
FLOOR: Final[datetime] = datetime(2024, 1, 1, tzinfo=UTC)

#: Why an item was refused for good. Short, stable strings: the client shows them.
REASON_NO_EPISODE: Final[str] = "episode no longer exists"
REASON_INVALID: Final[str] = "not a valid record"
#: Why an item should be sent again.
REASON_FAILED: Final[str] = "the server could not apply it just now"


class SyncKind(StrEnum):
    """The three things a device can have recorded offline."""

    POSITION = "position"
    COMPLETION = "completion"
    UNMARK = "unmark"


class SyncStatus(StrEnum):
    """What became of one record.

    ``applied`` and ``stale`` both mean "stop sending this": the server either
    took it or already knows something newer. ``rejected`` means the server
    can never use it, and the client keeps it and tells the user. ``retry``
    means it could not be applied this time; the client keeps it pending.
    """

    APPLIED = "applied"
    STALE = "stale"
    REJECTED = "rejected"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class SyncItem:
    """One validated record. Numbers are already checked finite and in range."""

    client_id: str
    kind: SyncKind
    episode_id: int
    at: datetime
    position_s: float | None = None
    duration_s: float | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    client_id: str | None
    status: SyncStatus
    reason: str | None = None


def _aware(at: datetime) -> datetime:
    """A naive timestamp is read as UTC: the client sends ``Z`` times."""
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def adjust_timestamp(at: datetime, *, sent_at: datetime | None, now: datetime) -> datetime:
    """The device's ``at`` on the server's clock, held inside the believable window.

    Shifted by the device's skew (``now − sent_at``) when the batch says when
    it was sent, then clamped into ``[max(now − MAX_AGE, FLOOR), now]``.
    """
    shifted = _aware(at)
    if sent_at is not None:
        shifted = shifted + (now - _aware(sent_at))
    oldest = max(now - MAX_AGE, FLOOR)
    return min(max(shifted, oldest), now)


def position_is_stale(stored_updated_at: datetime | None, at: datetime) -> bool:
    """Whether the server already holds something at least as new (FR-S8).

    A tie is stale: the same moment twice is a retried flush, and re-writing
    the row would be a write for no new information.
    """
    return stored_updated_at is not None and stored_updated_at >= at


def completion_is_stale(unmarked_at: datetime | None, at: datetime) -> bool:
    """Whether the user took the mark back at or after this completion (owner 2026-10-05)."""
    return unmarked_at is not None and unmarked_at >= at


def unmark_is_stale(
    completed_at: datetime | None, list_changed_at: datetime | None, at: datetime
) -> bool:
    """Whether a completion, or a change to the list, came after this un-mark."""
    if completed_at is not None and completed_at > at:
        return True
    return list_changed_at is not None and list_changed_at > at


def completion_values(
    *,
    item: SyncItem,
    row_position: float | None,
    row_duration: float | None,
    row_updated_at: datetime | None,
    rendition_duration: float,
) -> tuple[float, float]:
    """The position and duration a replayed completion writes.

    The completion's own numbers when the server has nothing newer; the row's
    when it does, so the flag is set without dragging a newer resume point
    back. A completion with no numbers (the mark-watched control, pressed
    offline) is FR-W3's manual mark: the rendition's length for both, ``0``
    when there is no rendition — exactly what ``mark_watched`` writes.
    """
    row_is_newer = row_updated_at is not None and row_updated_at > item.at
    if row_is_newer and row_position is not None:
        return row_position, row_duration or 0.0
    if item.position_s is not None and item.duration_s is not None:
        return item.position_s, item.duration_s
    return rendition_duration, rendition_duration


async def _locked_row(session: AsyncSession, user_id: int, episode_id: int) -> WatchProgress | None:
    """The row, re-read and locked for the rest of this item's savepoint."""
    return await session.get(
        WatchProgress,
        (user_id, episode_id),
        with_for_update=True,
        populate_existing=True,
    )


async def apply_item(
    session: AsyncSession,
    *,
    user_id: int,
    item: SyncItem,
    now: datetime,
    rendition_duration: float = 0.0,
    list_changed_at: datetime | None = None,
) -> SyncResult:
    """Apply one record through the online path's own functions.

    ``item.at`` must already be adjusted (:func:`adjust_timestamp`).
    ``list_changed_at`` is the show's list entry ``updated_at`` as it stood
    before this batch. Flushed, not committed.
    """
    episode = await session.get(Episode, item.episode_id)
    if episode is None:
        return SyncResult(item.client_id, SyncStatus.REJECTED, REASON_NO_EPISODE)

    at = item.at
    row = await _locked_row(session, user_id, episode.id)

    if item.kind is SyncKind.POSITION:
        if item.position_s is None or item.duration_s is None:
            return SyncResult(item.client_id, SyncStatus.REJECTED, REASON_INVALID)
        if position_is_stale(row.updated_at if row is not None else None, at):
            return SyncResult(item.client_id, SyncStatus.STALE)
        await record_progress(
            session,
            user_id=user_id,
            episode=episode,
            position_s=item.position_s,
            duration_s=item.duration_s,
            now=now,
            reported_at=at,
        )
        return SyncResult(item.client_id, SyncStatus.APPLIED)

    if item.kind is SyncKind.COMPLETION:
        if completion_is_stale(row.unmarked_at if row is not None else None, at):
            return SyncResult(item.client_id, SyncStatus.STALE)
        position_s, duration_s = completion_values(
            item=item,
            row_position=row.position_s if row is not None else None,
            row_duration=row.duration_s if row is not None else None,
            row_updated_at=row.updated_at if row is not None else None,
            rendition_duration=rendition_duration,
        )
        # ``force_complete`` — the manual mark's path, which is the online
        # path: one list advance that only goes up, one logged MAL write with
        # the previous value, nothing at all if it was already complete.
        await record_progress(
            session,
            user_id=user_id,
            episode=episode,
            position_s=position_s,
            duration_s=duration_s,
            now=now,
            force_complete=True,
            reported_at=at,
        )
        return SyncResult(item.client_id, SyncStatus.APPLIED)

    # SyncKind.UNMARK
    completed_at = row.completed_at if row is not None and row.completed else None
    if unmark_is_stale(completed_at, list_changed_at, at):
        return SyncResult(item.client_id, SyncStatus.STALE)
    await unmark_watched(session, user_id=user_id, episode=episode, now=now, reported_at=at)
    return SyncResult(item.client_id, SyncStatus.APPLIED)


async def _list_changed_at(
    session: AsyncSession, *, user_id: int, episode_ids: set[int]
) -> dict[int, datetime]:
    """``episode_id → list_entries.updated_at`` as it stands before the batch runs."""
    if not episode_ids:
        return {}
    rows = await session.execute(
        select(Episode.id, ListEntry.updated_at)
        .join(
            ListEntry,
            (ListEntry.anime_id == Episode.anime_id) & (ListEntry.user_id == user_id),
        )
        .where(Episode.id.in_(episode_ids))
    )
    return {episode_id: updated_at for episode_id, updated_at in rows.all()}


async def apply_batch(
    session: AsyncSession,
    *,
    user_id: int,
    items: Sequence[SyncItem],
    now: datetime,
    sent_at: datetime | None = None,
    rendition_durations: Mapping[int, float] | None = None,
) -> list[SyncResult]:
    """Apply ``items`` in the order given; one result per item, in that order.

    Each item runs inside its own savepoint, so one that raises is rolled back
    alone and answered ``retry`` while the rest of the batch stands. Flushed,
    not committed: the caller commits once.
    """
    durations = rendition_durations or {}
    unmarks = {item.episode_id for item in items if item.kind is SyncKind.UNMARK}
    list_changed = await _list_changed_at(session, user_id=user_id, episode_ids=unmarks)
    results: list[SyncResult] = []
    for raw in items:
        item = replace(raw, at=adjust_timestamp(raw.at, sent_at=sent_at, now=now))
        try:
            async with session.begin_nested():
                result = await apply_item(
                    session,
                    user_id=user_id,
                    item=item,
                    now=now,
                    rendition_duration=durations.get(item.episode_id, 0.0),
                    list_changed_at=list_changed.get(item.episode_id),
                )
        except Exception:
            log.exception(
                "offline record could not be applied; the device will send it again",
                extra={"user_id": user_id, "episode_id": item.episode_id, "kind": item.kind},
            )
            result = SyncResult(item.client_id, SyncStatus.RETRY, REASON_FAILED)
        results.append(result)
    return results


def needs_rendition(items: Sequence[SyncItem]) -> set[int]:
    """Episodes whose completion carries no numbers, so the rendition decides them."""
    return {
        item.episode_id
        for item in items
        if item.kind is SyncKind.COMPLETION and (item.position_s is None or item.duration_s is None)
    }


__all__ = [
    "FLOOR",
    "MAX_AGE",
    "MAX_BATCH",
    "REASON_FAILED",
    "REASON_INVALID",
    "REASON_NO_EPISODE",
    "SyncItem",
    "SyncKind",
    "SyncResult",
    "SyncStatus",
    "adjust_timestamp",
    "apply_batch",
    "apply_item",
    "completion_is_stale",
    "completion_values",
    "needs_rendition",
    "position_is_stale",
    "unmark_is_stale",
]
