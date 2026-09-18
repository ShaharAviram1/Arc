"""An episode's claim on one file inside a pack (FR-A11).

Three functions, and they live apart from
:mod:`arc.services.acquisition.batch` for the reason
:mod:`arc.services.acquisition.names` lives apart from the handlers: importing
this **reaches nothing**. The reconciler, the review's reject and the retention
deleter all have to give a file back, and all three are imported from packages
that ``batch`` itself imports through the parser and the ranker — so a
module-level import of ``batch`` from any of them closes a catalogue → library
→ acquisition circle that only fails on whichever module is imported second.
The rows and the queue are all this needs, so this is all it takes.

**What the three are for** is one sentence: a batch-backed episode must never
take the path a single takes. A cancel marks a ``torrents`` row ``cancelled``
and ``qbit_cancel`` deletes that hash *with its files*; retention deletes by
hash with ``deleteFiles`` as well. A pack's files belong to several episodes, so
either of those aimed at a batch would take somebody else's bytes. Giving the
row back takes exactly one file, and
:func:`~arc.services.acquisition.batch.disposition` — applied by the job queued
here — is the only thing that ever removes the torrent itself.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Torrent, TorrentFile, TorrentKind
from arc.services.acquisition.names import (
    QBIT_RESELECT,
    QBIT_RESELECT_PRIORITY,
    reselect_dedupe_key,
)
from arc.services.jobs.queue import enqueue


async def enqueue_reselect(session: AsyncSession, torrent_id: int) -> None:
    """Queue the client half of a selection change, deduplicated per torrent.

    Five callers write a ``torrent_files`` row and then this: the attach
    (:func:`~arc.services.acquisition.batch.claim_existing`), a want withdrawn
    (``wants.cancel_if_unwanted``), a rejected member
    (``reject.reject_download``), retention taking a file
    (``retention.delete.delete_episode_files``) and the poll's own stall
    handling. None of them may talk to qBittorrent — three of them are inside a
    transaction that must not span an HTTP call — so all of them write the
    decision and leave the request to
    :data:`~arc.services.acquisition.names.QBIT_RESELECT`.

    One line, in one place, because the priority and the dedupe key are part of
    the contract: the key is per *torrent*, so two episodes of one pack changing
    in the same moment are one selection to write.
    """
    await enqueue(
        session,
        QBIT_RESELECT,
        {"torrent_id": torrent_id},
        priority=QBIT_RESELECT_PRIORITY,
        dedupe_key=reselect_dedupe_key(torrent_id),
    )


async def live_claim(session: AsyncSession, episode_id: int) -> TorrentFile | None:
    """The batch file this episode is waiting on, if it is waiting on one.

    "The episode's live claim", and there is at most one anywhere — that is what
    ``ux_torrent_files_one_wanted_per_episode`` makes a database fact (FR-A11).
    It is the question ``wants.cancel_if_unwanted`` asks *before* it looks for a
    ``torrents`` row of its own: an episode downloading out of a pack has no row
    keyed on its id, because a batch's ``episode_id`` is null, and cancelling it
    by the old path would find nothing to stop.

    The torrent's ``qbit_state`` is deliberately **not** filtered on, unlike
    :func:`~arc.services.acquisition.batch.claim_existing`'s. A pack Arc has
    decided about should have no wanted rows left, and if one has survived — a
    stall that was interrupted, a re-selection the client refused — then
    un-wanting it is exactly the repair, and the job it queues no-ops on a
    decided pack by itself.
    """
    row: TorrentFile | None = await session.scalar(
        select(TorrentFile)
        .join(Torrent, Torrent.id == TorrentFile.torrent_id)
        .where(
            TorrentFile.episode_id == episode_id,
            TorrentFile.wanted.is_(True),
            Torrent.kind == TorrentKind.BATCH,
        )
        .order_by(TorrentFile.id)
        .limit(1)
    )
    return row


async def release_files(session: AsyncSession, rows: Iterable[TorrentFile]) -> tuple[int, ...]:
    """Give these files back to their pack, and queue the re-selection.

    ``episode_id`` is **kept** on the row. That is what makes a want given back
    cheap to change your mind about: ``batch.claim_existing`` serves the episode
    from the same pack again with no Nyaa request at all, which is FR-T3's
    "re-acquired" for free. The one caller that clears it is ``reject``, and it
    does so for the reason the single path marks a torrent ``rejected``: that
    file has been looked at and it was the wrong episode.

    Returns the torrent ids whose selection was queued, for the log lines. The
    rows are flushed **before** the jobs are queued, for the reason the
    reconciler flushes before ``enqueue_cancel``: the handler reads the rows and
    nothing else, and a job visible to a worker before the rows it is about
    would be a job that writes yesterday's selection.
    """
    listed = list(rows)
    if not listed:
        return ()
    for row in listed:
        row.wanted = False
    await session.flush()
    torrent_ids = tuple(sorted({row.torrent_id for row in listed}))
    for torrent_id in torrent_ids:
        await enqueue_reselect(session, torrent_id)
    return torrent_ids


__all__ = ["enqueue_reselect", "live_claim", "release_files"]
