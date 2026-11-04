"""A person picks one file out of a season pack Arc could not read (M21, FR-A4).

When the only releases for a finished show's episode are packs and the parser
cannot tell which of a pack's files is the wanted episode — no file claims it,
or several do — Arc does not guess (CLAUDE.md, the matching non-negotiable).
Before M21 such a pack was deleted and remembered as unreadable. Now the
**best** such pack is kept in the client, stopped with nothing selected, and a
person is asked (architecture.md §5.2 step 4):

* :func:`open_pick` — the refusal path's half, called by
  ``jobs._take_batch``: ``torrent_files`` rows for every file (the parser's
  reading in ``episode_id``, nothing wanted), the pack
  :data:`~arc.services.acquisition.qbit.QBIT_AWAITING_PICK`, and a
  :class:`~arc.models.PackPick` with the video files as candidates;
* :func:`pick` — "This is episode N": the one file is claimed for the episode
  and the pack is re-selected and started through ``qbit_reselect``;
* :func:`dismiss` — the pack becomes the ``unreadable`` tombstone it would have
  been without a pick, and ``qbit_forget_pack`` deletes it with its files;
* :func:`reconcile` — a pick left pending ``D`` days, or whose pack has gone
  from the client, is dismissed on its own.

Nothing here applies a reading or a suggestion: the candidates carry the
parser's numbers for a person to read, and only :func:`pick` — a person's
request — claims a file. Nothing here talks to qBittorrent either; the two
client halves are jobs (:data:`~arc.services.acquisition.names.QBIT_RESELECT`
and :data:`~arc.services.acquisition.names.QBIT_FORGET_PACK`).
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    PACK_PICK_DISMISSED,
    PACK_PICK_PICKED,
    PACK_PICK_REASON,
    Episode,
    EpisodeState,
    PackPick,
    Torrent,
    TorrentFile,
    Want,
)
from arc.services.acquisition.batch import UNREADABLE_BATCH, FilePlan, record_files
from arc.services.acquisition.claims import enqueue_reselect, live_claim
from arc.services.acquisition.demand import (
    ACQUISITION_PAUSED,
    ACQUISITION_PAUSED_MESSAGE,
    STORAGE_HELD,
    STORAGE_HELD_MESSAGE,
)
from arc.services.acquisition.names import (
    QBIT_FORGET_PACK,
    QBIT_FORGET_PACK_PRIORITY,
    forget_pack_dedupe_key,
)
from arc.services.acquisition.qbit import (
    DECIDED_STATES,
    QBIT_AWAITING_PICK,
    QBIT_MISSING,
    QBIT_UNREADABLE,
)
from arc.services.acquisition.rules import is_paused, is_storage_held
from arc.services.acquisition.states import transition
from arc.services.jobs.queue import enqueue

log = logging.getLogger(__name__)

#: The two refusals of :func:`~arc.services.acquisition.batch.plan_files` that
#: are about **identification** — a person reading the file names can answer
#: them. "Too many files" (``jobs._add_stopped``) is not one: Arc never read a
#: selection out of such a pack, so there is nothing to show.
_CLAIM_MARK: Final[str] = " files claim episode "
_NONE_MARK: Final[str] = "no file in the batch is episode "

#: ``qbit_state`` a picked pack goes back to: a live, undecided value the poll
#: overwrites with the client's own on its next pass, as for a fresh add.
LIVE_AFTER_PICK: Final[str] = "added"

#: Why :func:`reconcile` dismissed a pick, for the log.
STALE: Final[str] = "nobody picked a file in time"
GONE: Final[str] = "the pack is gone from the torrent client"

#: The 409 codes :func:`pick` and :func:`dismiss` answer with, each with the
#: sentence the Review page shows.
NOT_PENDING = "pick_resolved"
NOT_PENDING_MESSAGE = "This pack has already been picked or dismissed."
NOT_WAITING = "pack_not_waiting"
NOT_WAITING_MESSAGE = "This pack is no longer waiting for a pick."
NOT_WANTED = "not_wanted"
NOT_WANTED_MESSAGE = "Nobody wants this episode any more, so nothing was fetched."
MOVED_ON = "episode_moved_on"
MOVED_ON_MESSAGE = "This episode is already on its way from another release."
NOT_A_CANDIDATE = "not_a_candidate"
NOT_A_CANDIDATE_MESSAGE = "That file is not one of this pack's video files."

#: The episode states a pick may start a download from: resting and waiting
#: for it, or woken by the daily revival with its search not yet running.
PICKABLE_STATES: Final[frozenset[EpisodeState]] = frozenset(
    {EpisodeState.UNAVAILABLE, EpisodeState.WANTED}
)


class PickConflict(Exception):
    """A pick or a dismiss the current state refuses; ``code`` is the 409's."""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class PickNotFound(LookupError):
    """No pack pick with that id."""


# --- Pure rules ---------------------------------------------------------------


def identification_refusal(reason: str | None) -> bool:
    """Whether a plan's refusal is about which file is the episode. Pure.

    "k files claim episode n: …" and "no file in the batch is episode n" are;
    anything else — "41 files, over 40", a read-back the client disagreed
    with, an empty listing — is not.
    """
    if not reason:
        return False
    if reason.startswith(_NONE_MARK):
        return True
    count, mark, _ = reason.partition(_CLAIM_MARK)
    return bool(mark) and count.isdigit()


def pick_eligible(reason: str | None, *, permanent: bool) -> bool:
    """Whether a refused pack may be **held** for a person's pick (FR-A4). Pure.

    Only a **permanent** refusal for identification. Holding is provisional:
    ``jobs._take_batch`` tombstones the held pack after all when a later
    candidate is started, so a pick opens only when the search chose nothing.
    """
    return permanent and identification_refusal(reason)


def stale(created_at: datetime, *, now: datetime, period: timedelta) -> bool:
    """Whether a pending pick has waited ``period`` (D, floored at a day). Pure."""
    return now - created_at >= max(period, timedelta(days=1))


# --- Reads ----------------------------------------------------------------------


async def pending_for(session: AsyncSession, episode_id: int) -> PackPick | None:
    """The episode's pending pick, if it has one (oldest first)."""
    found: PackPick | None = await session.scalar(
        select(PackPick)
        .where(PackPick.episode_id == episode_id, PackPick.resolved_at.is_(None))
        .order_by(PackPick.id)
        .limit(1)
    )
    return found


async def awaiting_ids(session: AsyncSession, episode_ids: Collection[int]) -> set[int]:
    """Which of these episodes have a pending pick."""
    if not episode_ids:
        return set()
    rows = await session.scalars(
        select(PackPick.episode_id).where(
            PackPick.episode_id.in_(set(episode_ids)), PackPick.resolved_at.is_(None)
        )
    )
    return set(rows.all())


async def picked_file(
    session: AsyncSession, *, torrent_id: int, file_index: int, episode_id: int
) -> PackPick | None:
    """The resolved pick that chose this very file for this episode, if any.

    What the poll's hand-off asks when a pack file lands (§5.2 step 4): a file
    a person picked is linked as confirmed, never re-matched by the guesser.
    """
    found: PackPick | None = await session.scalar(
        select(PackPick)
        .where(
            PackPick.torrent_id == torrent_id,
            PackPick.episode_id == episode_id,
            PackPick.resolution == PACK_PICK_PICKED,
            PackPick.picked_file_index == file_index,
        )
        .order_by(PackPick.id.desc())
        .limit(1)
    )
    return found


async def pending_count(session: AsyncSession) -> int:
    """How many picks are waiting, for the review badge."""
    total = await session.scalar(
        select(func.count()).select_from(PackPick).where(PackPick.resolved_at.is_(None))
    )
    return int(total or 0)


# --- The refusal path ---------------------------------------------------------


async def open_pick(
    session: AsyncSession,
    *,
    torrent: Torrent,
    episode: Episode,
    plan: FilePlan,
    candidates: list[dict[str, Any]],
) -> PackPick:
    """Hold this refused pack for a person (``jobs._take_batch``'s refusal path).

    The caller has already written priority 0 to every file and left the pack
    stopped. Here: one ``torrent_files`` row per file — every file, not only
    the videos, because ``qbit_reselect`` writes nothing to a listing its rows
    do not match one for one — carrying the parser's reading where it was
    unambiguous and ``wanted = false``; the pack
    :data:`~arc.services.acquisition.qbit.QBIT_AWAITING_PICK`; and the pick.
    The episode's state is the caller's (it moves to ``unavailable`` with
    :data:`~arc.models.PACK_PICK_REASON` once the search has chosen nothing).
    """
    await record_files(session, torrent, plan)
    torrent.qbit_state = QBIT_AWAITING_PICK
    row = PackPick(
        torrent_id=torrent.id,
        episode_id=episode.id,
        reason=plan.refused_reason or "",
        candidates=candidates,
    )
    session.add(row)
    await session.flush()
    log.info(
        "a pack waits for a person to pick the episode's file",
        extra={
            "pack_pick_id": row.id,
            "torrent_id": torrent.id,
            "hash": torrent.info_hash,
            "episode_id": episode.id,
            "reason": row.reason,
            "candidates": len(candidates),
        },
    )
    return row


# --- A person's answer --------------------------------------------------------


async def _load(session: AsyncSession, pick_id: int) -> PackPick:
    row = await session.get(PackPick, pick_id, with_for_update=True, populate_existing=True)
    if row is None:
        raise PickNotFound(pick_id)
    return row


async def _has_live_want(session: AsyncSession, episode_id: int) -> bool:
    found = await session.scalar(
        select(Want.user_id)
        .where(Want.episode_id == episode_id, Want.dropped_at.is_(None))
        .limit(1)
    )
    return found is not None


async def pick(
    session: AsyncSession,
    pick_id: int,
    *,
    file_index: int,
    user_id: int | None,
    settings: Settings | None,
) -> PackPick:
    """ "This is episode N": claim one file of the pack and fetch it (FR-A4).

    The caller holds ``lock_wants`` (the reconciler's lock), so no want can be
    withdrawn between the check below and the claim. Everything is re-read
    under row locks; any of these refuses with :class:`PickConflict` and
    changes nothing: the pick is resolved, the pack is not
    ``awaiting_pick`` any more, nobody wants the episode, the episode is
    already on its way (or searching), the file is not a candidate.

    Then the file's row carries the episode and the claim (``wanted``; the
    one-claim index is checked first), the pack goes back to a live state, the
    episode to ``downloading`` through the machine's own edges, the pick is
    stamped, and ``qbit_reselect`` writes the selection and starts the pack —
    its M20.4 cool-downs and the poll's start rule apply from there. Never
    ``cancelled``: that deletes a pack with its files.

    **A pick is fetching**, so it obeys the same two brakes as a demand
    (``demand.demand`` step 5) and a manual release: while acquisition is
    paused or held for disk space (FR-T6) it is refused with
    ``acquisition_paused`` / ``storage_held`` before anything is written.
    ``settings`` locates the disk to measure; ``None`` (no data directory)
    skips only the hold.

    And **only the person's answer stays** on the pack: every other row's
    parser reading is cleared, so once the pack is live again
    ``claim_existing`` cannot attach a later episode to a file nobody
    confirmed (the matching non-negotiable). The readings remain in the
    pick's ``candidates`` for display.
    """
    row = await _load(session, pick_id)
    if row.resolved_at is not None:
        raise PickConflict(NOT_PENDING, NOT_PENDING_MESSAGE)
    if await is_paused(session):
        raise PickConflict(ACQUISITION_PAUSED, ACQUISITION_PAUSED_MESSAGE)
    if settings is not None and await is_storage_held(session, settings):
        raise PickConflict(STORAGE_HELD, STORAGE_HELD_MESSAGE)
    torrent = await session.get(
        Torrent, row.torrent_id, with_for_update=True, populate_existing=True
    )
    if torrent is None or torrent.qbit_state != QBIT_AWAITING_PICK:
        raise PickConflict(NOT_WAITING, NOT_WAITING_MESSAGE)
    if file_index not in {candidate.get("file_index") for candidate in row.candidates}:
        raise PickConflict(NOT_A_CANDIDATE, NOT_A_CANDIDATE_MESSAGE, status=422)
    episode = await session.get(
        Episode, row.episode_id, with_for_update=True, populate_existing=True
    )
    if episode is None or not await _has_live_want(session, episode.id):
        raise PickConflict(NOT_WANTED, NOT_WANTED_MESSAGE)
    if episode.state not in PICKABLE_STATES or await live_claim(session, episode.id) is not None:
        raise PickConflict(MOVED_ON, MOVED_ON_MESSAGE)
    file_row = await session.scalar(
        select(TorrentFile).where(
            TorrentFile.torrent_id == torrent.id, TorrentFile.file_index == file_index
        )
    )
    if file_row is None:
        raise PickConflict(NOT_WAITING, NOT_WAITING_MESSAGE)

    await session.execute(
        update(TorrentFile)
        .where(TorrentFile.torrent_id == torrent.id, TorrentFile.id != file_row.id)
        .values(episode_id=None)
        .execution_options(synchronize_session="fetch")
    )
    file_row.episode_id = episode.id
    file_row.wanted = True
    file_row.completed_at = None
    file_row.progress = None
    torrent.qbit_state = LIVE_AFTER_PICK
    torrent.wanted_bytes = file_row.size
    if episode.state is EpisodeState.UNAVAILABLE:
        transition(episode, EpisodeState.WANTED, reason="a person picked the pack's file")
    transition(episode, EpisodeState.SEARCHING, reason="a person picked the pack's file")
    transition(episode, EpisodeState.DOWNLOADING, reason=f"downloading {torrent.title}")

    row.resolved_at = datetime.now(UTC)
    row.resolution = PACK_PICK_PICKED
    row.picked_file_index = file_index
    row.resolved_by = user_id
    await session.flush()
    await enqueue_reselect(session, torrent.id)
    log.info(
        "a person picked a pack's file",
        extra={
            "pack_pick_id": row.id,
            "torrent_id": torrent.id,
            "episode_id": episode.id,
            "file_index": file_index,
            "path": file_row.path,
            "user_id": user_id,
        },
    )
    return row


async def _forget(session: AsyncSession, row: PackPick, *, user_id: int | None, why: str) -> None:
    """Resolve ``row`` dismissed and turn its pack into the tombstone.

    Exactly what the refusal did before M21: the pack's row becomes
    :data:`~arc.services.acquisition.qbit.QBIT_UNREADABLE` with no
    ``torrent_files`` rows, so no later search fetches it again, and
    ``qbit_forget_pack`` removes it from the client **with its files** (it
    never fetched a byte). The episode, if it is still resting on this pick's
    sentence, says the pre-M21 one instead and is left to FR-A6's daily retry.
    """
    row.resolved_at = datetime.now(UTC)
    row.resolution = PACK_PICK_DISMISSED
    row.resolved_by = user_id
    torrent = await session.get(
        Torrent, row.torrent_id, with_for_update=True, populate_existing=True
    )
    if torrent is not None and torrent.qbit_state == QBIT_AWAITING_PICK:
        torrent.qbit_state = QBIT_UNREADABLE
        await session.execute(delete(TorrentFile).where(TorrentFile.torrent_id == torrent.id))
        await session.flush()
        await enqueue(
            session,
            QBIT_FORGET_PACK,
            {"torrent_id": torrent.id},
            priority=QBIT_FORGET_PACK_PRIORITY,
            dedupe_key=forget_pack_dedupe_key(torrent.id),
        )
    episode = await session.get(Episode, row.episode_id)
    if (
        episode is not None
        and episode.state is EpisodeState.UNAVAILABLE
        and episode.unavailable_reason == PACK_PICK_REASON
        and await pending_for(session, episode.id) is None
    ):
        # Not a state change — the episode stays ``unavailable`` and the
        # reconciler's daily revival (``wants.UNAVAILABLE_RETRY``) picks it up
        # as before — only the sentence that no longer applies.
        episode.unavailable_reason = UNREADABLE_BATCH
    await session.flush()
    log.info(
        "a pack pick was dismissed",
        extra={
            "pack_pick_id": row.id,
            "torrent_id": row.torrent_id,
            "episode_id": row.episode_id,
            "user_id": user_id,
            "why": why,
        },
    )


async def dismiss(session: AsyncSession, pick_id: int, *, user_id: int | None) -> PackPick:
    """ "Dismiss": forget the pack as unreadable (FR-A4). 409 once resolved."""
    row = await _load(session, pick_id)
    if row.resolved_at is not None:
        raise PickConflict(NOT_PENDING, NOT_PENDING_MESSAGE)
    await _forget(session, row, user_id=user_id, why="dismissed by a person")
    return row


# --- Reconciliation -------------------------------------------------------------

#: How old a pick must be before "its pack is not in the client's listing" is
#: believed. A qBittorrent that has just restarted answers the WebUI before it
#: has loaded its torrents, and its first listings are short.
GONE_AFTER: Final[timedelta] = timedelta(hours=1)

#: Why :func:`reconcile` dismissed a pick nobody can make any more.
UNWANTED: Final[str] = "nobody wants the episode any more"
ELSEWHERE: Final[str] = "the episode is on its way from another release"


@dataclass(frozen=True, slots=True)
class Reconciled:
    """What :func:`reconcile` dismissed, for the log line."""

    stale: int = 0
    gone: int = 0
    dead: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"stale_picks": self.stale, "gone_picks": self.gone, "dead_picks": self.dead}


async def awaiting_hashes(session: AsyncSession) -> list[str]:
    """The info hashes of every pack waiting for a pick (pending ones)."""
    rows = await session.scalars(
        select(Torrent.info_hash)
        .join(PackPick, PackPick.torrent_id == Torrent.id)
        .where(PackPick.resolved_at.is_(None), Torrent.qbit_state == QBIT_AWAITING_PICK)
        .distinct()
    )
    return list(rows.all())


async def _live_single(session: AsyncSession, episode_id: int) -> bool:
    """Whether the episode has a single of its own Arc has not decided about."""
    found = await session.scalar(
        select(Torrent.id)
        .where(
            Torrent.episode_id == episode_id,
            or_(
                Torrent.qbit_state.is_(None),
                Torrent.qbit_state.not_in(sorted(DECIDED_STATES | {QBIT_MISSING})),
            ),
        )
        .limit(1)
    )
    return found is not None


async def dead_reason(session: AsyncSession, episode_id: int) -> str | None:
    """Why a pending pick on this episode can never succeed, or ``None``.

    :func:`pick` would refuse it for good: nobody wants the episode, or it is
    out of :data:`PICKABLE_STATES` **and** on its way from something else — a
    live claim on another pack, or a single the daily revival found. An
    episode merely ``searching`` (the revival's search running) is neither.
    """
    if not await _has_live_want(session, episode_id):
        return UNWANTED
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.state in PICKABLE_STATES:
        return None
    if await live_claim(session, episode_id) is not None or await _live_single(session, episode_id):
        return ELSEWHERE
    return None


def gone_from(
    info_hash: str, *, created_at: datetime, now: datetime, live: Collection[str] | None
) -> bool:
    """Whether a held pack counts as gone from the client. Pure.

    ``live`` is the client's listing, lower-case; ``None`` or **empty** means
    unknown (unreachable, or a client that has just restarted and listed
    nothing yet), and so does a pick younger than :data:`GONE_AFTER`.
    """
    if not live or now - created_at < GONE_AFTER:
        return False
    return info_hash.lower() not in live


async def reconcile(
    session: AsyncSession,
    *,
    now: datetime,
    period: timedelta,
    live_hashes: Collection[str] | None,
    age_rule: bool = True,
) -> Reconciled:
    """Dismiss the picks nobody will make (FR-A4; architecture.md §5.1).

    Run by ``compute_wants`` — the job that revives ``unavailable`` episodes —
    every pass, after it has taken ``lock_wants`` (the routes' order: the want
    lock, then the pick's row). In this order, each dismissal with its reason:

    * **dead** — :func:`dead_reason`: nobody wants the episode, or it is on its
      way from another release, so Review and the badge do not show a pick
      that can only be refused;
    * **gone** — the pack is not in the client's listing (:func:`gone_from`).
      The batch poll skips ``awaiting_pick`` (a decided state), so this is the
      only place that notices a pack deleted by hand;
    * **stale** — pending ``period`` (D, floored at a day; :func:`stale`).
      ``age_rule=False`` skips it: while acquisition is paused or held nobody
      can act on a pick, so its clock is not held against it.

    Idempotent: a resolved pick is never looked at again.
    """
    rows = list(
        (
            await session.scalars(
                select(PackPick)
                .where(PackPick.resolved_at.is_(None))
                .order_by(PackPick.id)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if not rows:
        return Reconciled()
    hashes: dict[int, tuple[str, str | None]] = {
        torrent_id: (info_hash, state)
        for torrent_id, info_hash, state in (
            await session.execute(
                select(Torrent.id, Torrent.info_hash, Torrent.qbit_state).where(
                    Torrent.id.in_({row.torrent_id for row in rows})
                )
            )
        ).all()
    }
    live = None if not live_hashes else {value.lower() for value in live_hashes}
    stale_count = gone_count = dead_count = 0
    for row in rows:
        why = await dead_reason(session, row.episode_id)
        if why is not None:
            await _forget(session, row, user_id=None, why=why)
            dead_count += 1
            continue
        found = hashes.get(row.torrent_id)
        if (
            found is not None
            and found[1] == QBIT_AWAITING_PICK
            and gone_from(found[0], created_at=row.created_at, now=now, live=live)
        ):
            await _forget(session, row, user_id=None, why=GONE)
            gone_count += 1
            continue
        if age_rule and stale(row.created_at, now=now, period=period):
            await _forget(session, row, user_id=None, why=STALE)
            stale_count += 1
    return Reconciled(stale=stale_count, gone=gone_count, dead=dead_count)


__all__ = [
    "ELSEWHERE",
    "GONE",
    "GONE_AFTER",
    "UNWANTED",
    "dead_reason",
    "gone_from",
    "LIVE_AFTER_PICK",
    "MOVED_ON",
    "NOT_A_CANDIDATE",
    "NOT_PENDING",
    "NOT_WAITING",
    "NOT_WANTED",
    "PICKABLE_STATES",
    "STALE",
    "PickConflict",
    "PickNotFound",
    "Reconciled",
    "awaiting_hashes",
    "awaiting_ids",
    "dismiss",
    "identification_refusal",
    "open_pick",
    "pending_count",
    "pending_for",
    "pick",
    "pick_eligible",
    "picked_file",
    "reconcile",
    "stale",
]
