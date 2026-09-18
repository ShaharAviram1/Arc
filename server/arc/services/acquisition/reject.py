"""What happens to an episode when its download turns out to be wrong.

One rule, and it closes the last dead end in the state machine (spec §6).

Arc downloads a release for episode 7, hands the file to the matcher with
"this is episode 7 of show 12" as a prior, and the episode sits in
``matching`` while the matcher decides. Usually it links and the episode moves
on. Sometimes the matcher is not sure enough and the file goes to the review
queue — and the episode stays ``matching``, correctly, because a person may
still confirm it.

But if that person presses **ignore**, they have said the file is not this
episode, and nothing else was ever going to write that row: the search job is
finished, the torrent is complete, ``poll_qbit`` only looks at episodes that
are downloading. The episode would stay ``matching`` for ever — no file, no
retry, and a show page saying it is being matched.

So an ignore that lands on a file Arc itself downloaded moves the episode to
``unavailable``. That is the state with a story attached (FR-A7) *and* the
state the daily retry picks up
(:data:`~arc.services.acquisition.wants.UNAVAILABLE_RETRY`), so the next
reconciliation asks Nyaa again — which is exactly right, because the release
Arc chose was the wrong file and a different one may not be.

**Only files Arc downloaded.** A file dropped into the manual directory and
ignored says nothing about any episode; it is somebody's mislabelled extra.
The test is the one qBittorrent's save path already encodes: every download
goes to ``downloads/<episode_id>/`` and no other file does
(:func:`arc.services.acquisition.qbit.save_path_for`), so the directory name
*is* the episode id, and a ``torrents`` row for that episode is the
confirmation that Arc put it there.

**A batch member is the same story told from the other end** (FR-A11). Its
path is ``downloads/batch/<info hash>/…`` and :func:`episode_id_of` answers
``None`` for it *by design* — a pack holding episodes 1 to 26 under a directory
named for one of them is the one shape that could attribute another episode's
file to the wrong episode, so every id-from-path inference fails closed. The
episode is read from the ``torrent_files`` row instead
(:func:`batch_member_of`), which is where the parser wrote it at pick time, and
the ending is byte for byte the single's: ``unavailable``, :data:`WRONG_FILE`,
and FR-A6's daily retry. What differs is what is marked. The shared torrent is
**never** ``rejected`` — that would strand the files of every other episode in
the pack, and the client is perfectly happy with the torrent in any case — so
the *row* is un-wanted and its ``episode_id`` cleared, which is the batch's
version of what ``rejected`` does for a single: this file has been looked at and
it is not that episode, so the retry must not be served it again.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Episode, EpisodeState, MediaFile, Torrent, TorrentFile, TorrentKind
from arc.services.acquisition.claims import release_files
from arc.services.acquisition.qbit import BATCH_DIR, QBIT_REJECTED
from arc.services.acquisition.states import transition

log = logging.getLogger(__name__)

#: The sentence the show page shows for it (FR-A7).
WRONG_FILE = "downloaded file was not this episode"

# ``QBIT_REJECTED`` — what the ``torrents`` row is marked — is defined in
# :mod:`arc.services.acquisition.qbit` beside the other three values Arc writes
# into that column and the :data:`~arc.services.acquisition.qbit.DECIDED_STATES`
# set they form, and is re-exported here because this is where it is *decided*.
# It is not a qBittorrent state: the client is perfectly happy with that
# torrent, and the column is where "what became of this download" is read from.


def episode_id_of(path: str, *, downloads_dir: Path) -> int | None:
    """The episode a downloaded file belongs to, from its directory name.

    ``None`` for anything that is not under ``downloads_dir`` or whose first
    segment there is not a number — a manual drop, or a file somebody moved.
    """
    try:
        relative = Path(path).resolve().relative_to(downloads_dir.resolve())
    except ValueError:
        return None
    if not relative.parts:
        return None
    try:
        return int(relative.parts[0])
    except ValueError:
        return None


def batch_member_of(path: str, *, downloads_dir: Path) -> tuple[str, str] | None:
    """``(info hash, the file's path inside the torrent)``, or ``None`` (FR-A11).

    :func:`episode_id_of`'s counterpart for the other layout Arc writes.
    ``downloads/batch/<info hash>/<whatever the torrent calls the file>`` is
    where :func:`~arc.services.acquisition.qbit.batch_save_path_for` puts a
    pack, and the two halves this returns are exactly the pair that identifies
    one ``torrent_files`` row: the hash is the torrent's identity and the
    remainder is the row's ``path``, which is stored relative to the save path.

    Nothing is inferred beyond the layout. A path outside the downloads
    directory, one whose first segment is not ``batch``, and one that names a
    directory but no file inside it all answer ``None`` — the same fail-closed
    reading :func:`episode_id_of` gives anything it does not recognise.
    """
    try:
        relative = Path(path).resolve().relative_to(downloads_dir.resolve())
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 3 or parts[0] != BATCH_DIR:
        return None
    return parts[1], "/".join(parts[2:])


async def batch_file_of(
    session: AsyncSession, path: str, *, downloads_dir: Path
) -> TorrentFile | None:
    """The ``torrent_files`` row a file on disk *is*, or ``None`` (FR-A11).

    :func:`batch_member_of` reads the layout and this reads the row, and the
    two halves are kept together because two callers need the pair and neither
    may re-derive it: this module, to end a rejected member's story, and
    :func:`arc.services.library.jobs._expected_episode`, to tell the suggestion
    model which episode Arc believed it was fetching. A second implementation of
    "which row is this file" is how the two would one day disagree about a pack
    whose save path somebody moved.

    ``episode_id`` is deliberately **not** filtered on. "There is a row and it
    claims nothing" and "there is no row at all" are different facts, and only
    the caller knows which of them matters to it.
    """
    found = batch_member_of(path, downloads_dir=downloads_dir)
    if found is None:
        return None
    info_hash, inside = found
    row: TorrentFile | None = await session.scalar(
        select(TorrentFile)
        .join(Torrent, Torrent.id == TorrentFile.torrent_id)
        .where(
            Torrent.info_hash == info_hash,
            Torrent.kind == TorrentKind.BATCH,
            TorrentFile.path == inside,
        )
        .limit(1)
    )
    return row


async def _reject_batch_member(
    session: AsyncSession, media_file: MediaFile, *, downloads_dir: Path
) -> Episode | None:
    """The same ending for a file that came out of a pack (FR-A11).

    The row is resolved by the path and nothing else: the hash names the
    torrent, the remainder names the file, and the episode is the one the
    parser wrote on that row at pick time. A row that claims no episode — an
    extra, or one whose episode has since gone from the catalogue — is not
    something to reason about, exactly as a directory Arc did not download into
    is not.

    Two writes, and the second is the one worth arguing about. The row stops
    being wanted, so ``qbit_reselect`` sets its priority back to 0 and
    ``batch.disposition`` decides whether the pack is still worth holding; and
    its ``episode_id`` is **cleared**, so that the ``unavailable`` retry cannot
    be served the identical file straight back by ``batch.claim_existing``.
    That clearing is the batch's whole equivalent of marking a single
    ``rejected``, and it is as narrow as it can be: one row, one episode, and
    the pack keeps serving everybody else.
    """
    row = await batch_file_of(session, media_file.path, downloads_dir=downloads_dir)
    if row is None or row.episode_id is None:
        return None
    episode = await session.get(Episode, row.episode_id)
    if episode is None or episode.state is not EpisodeState.MATCHING:
        return None

    transition(episode, EpisodeState.UNAVAILABLE, reason=WRONG_FILE)
    await release_files(session, (row,))
    row.episode_id = None
    await session.flush()
    log.info(
        "a downloaded file from a batch was rejected in review",
        extra={
            "media_file_id": media_file.id,
            "episode_id": episode.id,
            "torrent_id": row.torrent_id,
            "file_index": row.file_index,
            "path": row.path,
        },
    )
    return episode


async def reject_download(
    session: AsyncSession, media_file: MediaFile, *, downloads_dir: Path
) -> Episode | None:
    """Mark the episode this rejected file was downloaded for unavailable.

    Returns the episode when it moved, ``None`` when there was nothing to move
    — a manual file, an episode already past ``matching`` because another file
    linked to it, or a directory Arc did not download into. Flushes but does
    not commit; the caller owns the transaction.
    """
    episode_id = episode_id_of(media_file.path, downloads_dir=downloads_dir)
    if episode_id is None:
        # Either not Arc's at all, or a batch member — whose directory is named
        # for the pack's hash precisely so that this answers ``None`` (FR-A11).
        return await _reject_batch_member(session, media_file, downloads_dir=downloads_dir)
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.state is not EpisodeState.MATCHING:
        return None

    torrents = list(
        (await session.scalars(select(Torrent).where(Torrent.episode_id == episode_id))).all()
    )
    if not torrents:
        # The file is in an episode's download directory but Arc never chose a
        # release for it. Not something this should reason about.
        return None

    transition(episode, EpisodeState.UNAVAILABLE, reason=WRONG_FILE)
    for torrent in torrents:
        # Every attempt at this episode, because an episode in ``matching``
        # has had exactly one file delivered and this is it: whatever else was
        # tried, none of it produced the episode.
        torrent.qbit_state = QBIT_REJECTED
    await session.flush()
    log.info(
        "a downloaded file was rejected in review",
        extra={
            "media_file_id": media_file.id,
            "episode_id": episode.id,
            "torrents": [torrent.info_hash for torrent in torrents],
        },
    )
    return episode


__all__ = [
    "QBIT_REJECTED",
    "WRONG_FILE",
    "batch_file_of",
    "batch_member_of",
    "episode_id_of",
    "reject_download",
]
