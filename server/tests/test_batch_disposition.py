"""Keep, stop or delete: what becomes of a pack (FR-A11).

:func:`~arc.services.acquisition.batch.disposition` is one decision with four
callers — a want withdrawn, a want attached, retention taking a file, a rejected
member — and it is a *value* rather than an action, so the table of cases here
is the rule. Nothing in this module talks to qBittorrent: what is asserted is
the answer, and ``jobs.qbit_reselect``'s own tests assert that the answer is
carried out.

The case that earns the function is **STOP**: a pack nothing wants *this
minute* whose show is still on somebody's list is an asset, because the next
episode attaches to it for zero Nyaa requests (``claim_existing``). Deleting it
would cost that episode a fourteen-gigabyte metadata fetch and a fresh pick, and
keeping it costs a stopped torrent and a row.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import ListStatus, Torrent, TorrentFile, TorrentKind
from arc.services.acquisition.batch import Disposition, disposition
from tests.acquisition_helpers import make_anime, make_entry, make_episodes, make_user

pytestmark = pytest.mark.pg


async def _pack(
    session: AsyncSession,
    *,
    anilist_id: int,
    email: str,
    status: ListStatus | None = ListStatus.WATCHING,
    wanted: bool = False,
    claimed: bool = True,
) -> Torrent:
    """A batch of two files, with the show on somebody's list or not.

    ``claimed`` is whether the files map to episodes at all. A pack whose names
    the parser could make nothing of, or whose episodes have since gone from the
    catalogue (``torrent_files.episode_id`` is ``SET NULL``), has rows that name
    none — and a pack holding nothing identifiable can never be wanted again.
    """
    anime = await make_anime(session, anilist_id=anilist_id)
    episodes = await make_episodes(session, anime, 2)
    user = await make_user(session, email)
    if status is not None:
        await make_entry(session, user, anime, status=status)

    torrent = Torrent(
        kind=TorrentKind.BATCH, episode_id=None, info_hash=f"{anilist_id:040d}", qbit_state="added"
    )
    session.add(torrent)
    await session.flush()
    for index, episode in enumerate(episodes):
        session.add(
            TorrentFile(
                torrent_id=torrent.id,
                file_index=index,
                path=f"pack/{episode.number:02d}.mkv",
                size=1024,
                episode_id=episode.id if claimed else None,
                wanted=wanted and index == 0,
            )
        )
    await session.flush()
    return torrent


async def test_a_pack_something_still_wants_is_kept(db_session: AsyncSession) -> None:
    """Bytes are owed to somebody, so there is nothing to decide."""
    torrent = await _pack(db_session, anilist_id=963100, email="disp1@arc.test", wanted=True)

    assert await disposition(db_session, torrent) is Disposition.KEEP


async def test_a_pack_nobody_wants_yet_whose_show_is_watching_is_stopped(
    db_session: AsyncSession,
) -> None:
    """The one that makes a pack an asset: the next episode is one ``filePrio`` away."""
    torrent = await _pack(db_session, anilist_id=963101, email="disp2@arc.test")

    assert await disposition(db_session, torrent) is Disposition.STOP


async def test_a_planned_show_keeps_its_pack_too(db_session: AsyncSession) -> None:
    """``planned`` generates wants, so it can want this pack again."""
    torrent = await _pack(
        db_session, anilist_id=963102, email="disp3@arc.test", status=ListStatus.PLANNED
    )

    assert await disposition(db_session, torrent) is Disposition.STOP


async def test_a_completed_show_ends_its_packs_life(db_session: AsyncSession) -> None:
    """Nothing it holds can be wanted again, so it goes with its files."""
    torrent = await _pack(
        db_session, anilist_id=963103, email="disp4@arc.test", status=ListStatus.COMPLETED
    )

    assert await disposition(db_session, torrent) is Disposition.DELETE


async def test_a_show_nobody_lists_at_all_ends_it_too(db_session: AsyncSession) -> None:
    """An entry removed from every list is the same answer as a finished one."""
    torrent = await _pack(db_session, anilist_id=963104, email="disp5@arc.test", status=None)

    assert await disposition(db_session, torrent) is Disposition.DELETE


async def test_an_on_hold_show_keeps_its_pack_stopped(db_session: AsyncSession) -> None:
    """Wider than the reconciler's two statuses, deliberately (owner, 2026-09-18).

    ``on_hold`` generates no wants — Arc fetches nothing more for it — but this
    is the other question: a paused viewer comes back, and a stopped pack with
    every file at priority 0 is on nobody's disk beyond the files it has already
    fetched, which retention measures per episode. So it is kept.
    """
    torrent = await _pack(
        db_session, anilist_id=963105, email="disp6@arc.test", status=ListStatus.ON_HOLD
    )

    assert await disposition(db_session, torrent) is Disposition.STOP


async def test_a_dropped_show_ends_its_packs_life_too(db_session: AsyncSession) -> None:
    """The other half of the owner's line: dropped is a decision, on hold is a pause."""
    torrent = await _pack(
        db_session, anilist_id=963107, email="disp8@arc.test", status=ListStatus.DROPPED
    )

    assert await disposition(db_session, torrent) is Disposition.DELETE


async def test_a_pack_whose_files_map_to_no_episode_is_deleted(
    db_session: AsyncSession,
) -> None:
    """Even with the show being watched: there is nothing here to serve it with."""
    torrent = await _pack(db_session, anilist_id=963106, email="disp7@arc.test", claimed=False)

    assert await disposition(db_session, torrent) is Disposition.DELETE


async def test_a_pack_with_no_file_rows_at_all_is_deleted(db_session: AsyncSession) -> None:
    """The tombstone shape (``mark_unreadable``): a row and not one file."""
    torrent = Torrent(
        kind=TorrentKind.BATCH, episode_id=None, info_hash="f" * 40, qbit_state="unreadable"
    )
    db_session.add(torrent)
    await db_session.flush()

    assert await disposition(db_session, torrent) is Disposition.DELETE


async def test_a_pack_the_library_has_a_file_from_is_only_stopped(
    db_session: AsyncSession,
) -> None:
    """``DELETE`` means ``deleteFiles=true``, and those bytes are somebody's.

    The reachable case is a **rejected** member: ``reject_download`` clears the
    row's episode — so nothing the pack holds maps to a list entry any more —
    but leaves ``completed_at``, because the file is on the disk and in
    somebody's review queue. Deleting the pack would take it away underneath
    them and leave a ``media_files`` row pointing at nothing. Retention removes
    those bytes per episode (FR-T1), and when it does it clears the stamp, which
    is what lets a later disposition reach ``DELETE`` at all.
    """
    torrent = await _pack(db_session, anilist_id=963108, email="disp9@arc.test", claimed=False)
    row = await db_session.scalar(
        select(TorrentFile).where(TorrentFile.torrent_id == torrent.id).order_by(TorrentFile.id)
    )
    assert row is not None
    assert await disposition(db_session, torrent) is Disposition.DELETE, "the case without it"

    row.completed_at = datetime.now(UTC)
    await db_session.flush()

    assert await disposition(db_session, torrent) is Disposition.STOP
