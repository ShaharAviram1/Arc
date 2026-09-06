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
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Episode, EpisodeState, MediaFile, Torrent
from arc.services.acquisition.states import transition

log = logging.getLogger(__name__)

#: The sentence the show page shows for it (FR-A7).
WRONG_FILE = "downloaded file was not this episode"

#: What the ``torrents`` row is marked. Not a qBittorrent state — the client
#: is perfectly happy with that torrent — but this column is where "what
#: became of this download" is read from, and "a person rejected it" is the
#: answer retention (M10) needs when it decides what to delete.
QBIT_REJECTED = "rejected"


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
        return None
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


__all__ = ["QBIT_REJECTED", "WRONG_FILE", "episode_id_of", "reject_download"]
