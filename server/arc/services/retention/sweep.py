"""Which episodes may be deleted, and what deleting one would remove (FR-T1).

This module decides; :mod:`arc.services.retention.delete` acts. The split is
what makes ``GET /api/retention/preview`` and the sweep itself the *same*
answer — the preview is not a second implementation of the rule, it is the
rule with the deletion left out.

**The rule (FR-T1).** An episode with bytes behind it — ``ready``, or
``downloaded``/``matched``/``failed`` with a file — may go when no live want
remains *and* the grace period G has run out. What G is measured from depends
on how the episode stopped being wanted:

* somebody watched it. ``watch_progress.completed_at`` is written once, at the
  moment the episode was finished, and never moved again (FR-T1 in
  :class:`arc.models.WatchProgress`), so the grace runs from the *last*
  completion — the second user to finish it is the one who decides.
* somebody's **list progress passed it** (FR-W5, owner 2026-09-13). An episode
  at or below a user's ``list_entries.progress`` is watched by that user
  whether or not Arc has a completion row for it: marking episode 9 watched
  writes one row and raises progress to 9, and the eight episodes under it
  must not sit on the disk for ever waiting for rows that were deliberately
  not written. The anchor is the **list entry's** ``updated_at``, which is an
  approximation on purpose — the column moves for any change to the row, a
  score or a six-hourly import that found something new included, and no
  column records "when progress passed episode 4". It errs late, which for a
  deletion is the right direction to err, and at G = 7 days the error is a
  day or two on an episode nobody is going to watch. It is also **clamped to
  the age of the bytes** (:attr:`_Facts.progress_anchor`): an imported
  ``updated_at`` can be months older than a file an admin re-fetched this
  morning, and without the clamp the next hourly sweep would delete that file
  before anybody could play it.
* the wants were dropped — as stale (FR-T2), or because the show stopped being
  watching/planned (FR-W4). ``wants.dropped_at`` is that moment, and the last
  drop is what counts, for the same reason.
* nobody ever wanted it: a file dropped into the manual directory, an admin's
  own fetch. Then there is no human moment to measure from and the file's own
  age is used — ``renditions.ready_at``, or the source file's ``created_at``.

**One episode is swept with no grace period at all**: a ``ready`` one with no
rendition row and no indexed file (:data:`REASON_NO_FILES`). It is not holding
any bytes to be careful about — it is a row that says "playable" over an empty
directory, and every second it stays that way is a show page offering a play
button that 404s. The sweep deletes nothing for it and resets it to
``not_wanted``, which is exactly what lets acquisition fetch it again.

**The "every user who wanted it has completed it" half of FR-T1 needs no
separate test**, and that is worth stating because it looks like an omission:
the reconciler deletes a live want the moment the user's progress — which is
itself ``max(list progress, completions)`` there
(:mod:`arc.services.acquisition.wants`) — passes the episode. So "no live
want" already means "everybody who wanted it is done with it", by both halves
of FR-W5's rule, and this module only has to decide *when* that happened.

Completions are counted from ``watch_progress`` rather than from the ``wants``
rows, and that is not an oversight. A want is **deleted** the moment the user
watches past it (:mod:`arc.services.acquisition.wants` explains why), so by
the time everybody has finished an episode there is frequently no ``wants``
row left to read a user id out of. The rows that survive are exactly the
dropped ones. Taking the union — every completion, plus every drop — is what
makes "two users, one finished it a week ago and one finished it this morning"
come out as "not yet", which is the whole point of the rule.

**Two graces, chosen by the anchor's kind (M20, owner 2026-10-13).** A
*watched* anchor — a completion, or list progress passing the episode — uses
``watched_grace_hours`` (default 6): Arc is a cache in front of the indexer,
and a watched episode leaves the same evening. A drop and "nobody ever wanted
it" keep G days.

**Four holds keep an episode whose grace has run out** (FR-T1, M20 T8), each
reported by the preview (:func:`retention_plan`):

* the show is **pinned** by any user (FR-T8);
* somebody has a **partial watch** on it (not completed, position > 0) updated
  within D days — a user with the show on hold has no live want, but is
  halfway through this episode;
* somebody **played** it within ``watched_grace_hours`` —
  ``max(watch_progress.updated_at)``, which covers a rewatch, because
  ``completed_at`` is written once;
* its offline copy was **served** within the last ten minutes: a device is
  downloading it right now.

A replayed offline completion carries the device's own time, possibly hours
ago, so the next sweep may delete at once. That is correct: the user watched
it.

**The disk floor evicts as a last resort** (FR-T6, :func:`floor_evictions`):
grace and pins are ignored, least recently watched first; a live want, an
encode, a copy being served, a pending trip copy, a partial watch within
24 hours and a play within 30 minutes are never touched.

**Nothing in flight is ever a candidate.** ``searching``, ``downloading``,
``matching`` and ``preparing`` are excluded by not being in
:data:`RETAINED_STATES` at all: an episode being encoded has a job holding its
source file open, and an episode mid-download has a torrent whose files
qBittorrent is still writing.

**Paths are checked here, not only at the point of deletion.** Every directory
this proposes is resolved and tested against ``DATA_DIR``'s own roots, and a
symlink is refused outright rather than followed. A preview that lists
something the deleter would then refuse would be a preview of the wrong thing,
and the resolution is cheap.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    ListEntry,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    Rendition,
    ShowPin,
    Torrent,
    TorrentFile,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    Want,
    WatchProgress,
)
from arc.services.media.copies import COPY_WRITER_TYPES, encoding_episode_ids, served_recently
from arc.services.media.names import offline_path_for, output_dir_for
from arc.services.retention.rules import grace_period, unwatched_period, watched_grace_hours
from arc.services.trips.rules import held_episode_ids

log = logging.getLogger(__name__)

#: The states an episode may have bytes in. ``ready`` is the ordinary one;
#: the other three are the ways a download can stop short of playable and
#: still have left a file on the disk (FR-T1's "source + rendition").
RETAINED_STATES: Final[frozenset[EpisodeState]] = frozenset(
    {
        EpisodeState.READY,
        EpisodeState.DOWNLOADED,
        EpisodeState.MATCHED,
        EpisodeState.FAILED,
    }
)

#: And the states retention must never touch, whatever else is true: work is
#: in flight and the files are being written or read right now. Spelled out
#: rather than derived, because "everything not in RETAINED_STATES" would also
#: include ``not_wanted``, which is where a swept episode *lands*.
PROTECTED_STATES: Final[frozenset[EpisodeState]] = frozenset(
    {
        EpisodeState.SEARCHING,
        EpisodeState.DOWNLOADING,
        EpisodeState.MATCHING,
        EpisodeState.PREPARING,
    }
)

#: The reason halves, completed by :func:`_reason` with the grace date.
REASON_WATCHED = "everyone who wanted it has watched it"
REASON_DROPPED = "every want on it was dropped"
REASON_UNWANTED = "nobody wants it and nobody ever did"
REASON_MANUAL = "an admin asked for it"

#: And the one reason that carries no grace date, because there is nothing to
#: give a grace period to: a ``ready`` episode with no rendition row and no
#: indexed file is not holding any bytes, it is a row claiming to be playable
#: that would 404 the moment somebody pressed play. It is swept for the state
#: change alone (FR-T3), which puts it back where acquisition can fetch it
#: again.
REASON_NO_FILES = "no files on disk"

#: What the state change is logged as (spec §6: ``ready → (retention) →
#: not_wanted``).
RETENTION_REASON = "retention"

#: The floor eviction's reason (FR-T6, M20 T8), completed by
#: :func:`floor_evictions` with the least-recently-watched moment.
REASON_FLOOR = "free space is below the floor"

#: The four holds (FR-T1, M20 T8): why an episode past its grace is kept.
HOLD_PINNED = "the show is pinned"
HOLD_PARTIAL = "somebody is part-way through it"
HOLD_PLAYED = "somebody played it recently"
HOLD_SERVING = "a device is downloading its offline copy"

#: How recently an offline copy must have been served to count as being
#: downloaded right now (hold d). ``last_served_at`` is written at most every
#: five minutes (:data:`arc.services.media.copies.SERVED_TOUCH_SECONDS`), so a
#: download in progress always has a stamp younger than this.
COPY_SERVING_WINDOW: Final[timedelta] = timedelta(minutes=10)

#: Hold (d) has a ceiling: a device fetching in a loop must not hold an
#: episode for ever. It lapses this long after the episode's grace ran out
#: (or after the copy was made, if later). Arc records no "first served"
#: moment, so the deadline stands in for it. The same six hours as the trip
#: settle's ``FETCH_CEILING`` (:mod:`arc.services.trips.settle`, which cannot
#: be imported here: it imports the deleter, which imports this module).
COPY_SERVING_CEILING: Final[timedelta] = timedelta(hours=6)

#: Somebody played the episode this recently: it is being watched right now.
#: The floor never evicts such an episode, and hold (c)'s window is never
#: shorter than this, whatever ``watched_grace_hours`` says (0 included) — a
#: completion at 90 % drops the want while the last minutes are still playing.
PLAYING_WINDOW: Final[timedelta] = timedelta(minutes=30)

#: A partial watch this recent is never evicted, even under the floor.
FLOOR_PARTIAL_WINDOW: Final[timedelta] = timedelta(hours=24)


# --- Path safety ------------------------------------------------------------


def allowed_roots(settings: Settings) -> tuple[Path, ...]:
    """The only directories retention may delete anything inside.

    Five. ``renditions`` and ``downloads`` are FR-T3's own words; ``manual`` is
    there because FR-T3 also says a manually dropped file is deleted *itself*
    rather than with its directory, and the manual drop directory is precisely
    where such a file lives. ``offline`` holds the small copies (FR-P6), which
    go with their episode and, on their own, when nobody has fetched them for
    ``offline_idle_days``. ``live`` holds a progressive remux while its
    source downloads (M20 T6); what is left there is swept by
    :func:`~arc.services.media.live.sweep_live_leftovers`.
    """
    return (
        settings.renditions_dir,
        settings.downloads_dir,
        settings.manual_dir,
        settings.offline_dir,
        settings.live_dir,
    )


def _within(path: Path, roots: tuple[Path, ...]) -> Path | None:
    """``path`` resolved, if it is safely inside one of ``roots``.

    Three refusals, each of which has ruined somebody's afternoon somewhere:

    * a **symlink** — following one is how a delete inside ``DATA_DIR``
      removes something that is not in ``DATA_DIR`` at all. Refused before it
      is resolved, so a link pointing back inside the root is refused too:
      Arc never creates one, so its presence means something else made it.
    * a path that resolves **outside** every root, which is what a hand-edited
      ``renditions.dir`` or ``media_files.path`` looks like.
    * a root **itself**. ``renditions_dir`` is not one episode's directory,
      and an ``episode_id`` that arrived as an empty string would name it.
    """
    if path.is_symlink():
        log.warning("refusing to delete a symlink", extra={"path": str(path)})
        return None
    resolved = path.resolve()
    for root in roots:
        base = root.resolve()
        if resolved == base:
            continue
        if resolved.is_relative_to(base):
            return resolved
    log.warning(
        "refusing to delete a path outside the data directories",
        extra={"path": str(path), "roots": [str(root) for root in roots]},
    )
    return None


def safe_path(path: Path, settings: Settings) -> Path | None:
    """:func:`_within` against :func:`allowed_roots`. Public for the deleter."""
    return _within(path, allowed_roots(settings))


def _file_size(path: Path, recorded: int | None) -> int:
    """A file's size on disk, falling back to what the ingest recorded."""
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - a file that vanished mid-plan
        return recorded or 0


def dir_size(path: Path) -> int:
    """Bytes under ``path``, symlinks not followed and errors ignored.

    Housekeeping arithmetic: a file that vanishes mid-walk (a concurrent
    sweep, a torrent being moved) must not fail the job that was only trying
    to say how much it freed.
    """
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:  # pragma: no cover - a file that vanished mid-walk
                continue
    return total


# --- What deleting one episode would remove ---------------------------------


@dataclass(frozen=True, slots=True)
class Targets:
    """Everything one episode's deletion touches (FR-T3).

    Built before anything is deleted so that the preview, the log line and the
    deleter all describe the same set of things.
    """

    episode_id: int
    #: The rendition directory, resolved and checked, when it is on disk.
    rendition_dir: Path | None = None
    #: The ``renditions`` row id, whether or not its directory still exists —
    #: a row pointing at a directory somebody removed by hand is exactly the
    #: kind of leftover this must clear.
    rendition_id: int | None = None
    #: ``downloads/<episode id>``, when it exists: the whole torrent payload.
    source_dir: Path | None = None
    #: ``media_files`` rows to delete, source directory and manual drops alike.
    media_file_ids: tuple[int, ...] = ()
    #: Individual files to unlink — a manual drop, or a source that sits
    #: somewhere other than under its episode's download directory.
    loose_files: tuple[Path, ...] = ()
    #: qBittorrent hashes to remove *with their data*, and the ``torrents``
    #: rows that carry them. **Empty for a batch-backed episode**, and that is
    #: the design rather than an omission: a batch's ``episode_id`` is null, so
    #: the query that fills these cannot reach one, and deleting a pack by hash
    #: with its files would take several other episodes' bytes (FR-A11).
    torrent_hashes: tuple[str, ...] = ()
    torrent_ids: tuple[int, ...] = ()
    #: The ``torrent_files`` rows this episode's file is claimed by (FR-A11).
    #: Not deleted — the row is the pack's record of what it holds, and keeping
    #: its ``episode_id`` is what lets a later want be served from the same pack
    #: with no Nyaa request (FR-T3). They are **un-wanted before the file is
    #: unlinked**, so that a start for another episode cannot re-fetch what
    #: retention has just removed.
    torrent_file_ids: tuple[int, ...] = ()
    #: The small offline copy (FR-P6): its file, resolved and checked, when it
    #: is on disk, and whether there is an ``offline_copies`` row to delete —
    #: a row whose file has gone is a leftover like any other.
    offline_file: Path | None = None
    offline_copy: bool = False
    #: What all of that adds up to on disk.
    bytes: int = 0

    @property
    def empty(self) -> bool:
        """Whether there is nothing at all to remove.

        :attr:`torrent_file_ids` is deliberately not counted. A pack's claim on
        an episode is not bytes on the disk and is not a reason to sweep one: an
        episode whose only trace is a not-wanted row in somebody's season pack
        has nothing to free, and giving the row back would be work done for no
        space.
        """
        return not (
            self.rendition_dir
            or self.rendition_id
            or self.source_dir
            or self.media_file_ids
            or self.loose_files
            or self.torrent_hashes
            or self.torrent_ids
            or self.offline_file
            or self.offline_copy
        )


@dataclass(frozen=True, slots=True)
class Deletable:
    """One episode the next sweep would delete, and why."""

    episode_id: int
    anime_id: int
    number: int
    state: EpisodeState
    reason: str
    targets: Targets
    #: The moment the grace period was counted from; null for the file-age
    #: case, where the anchor is the file itself.
    anchor: datetime | None = None
    #: For an episode whose grace has run out but which is kept anyway: the
    #: hold that keeps it (``HOLD_*``). Null for everything the sweep deletes.
    hold: str | None = None

    @property
    def bytes(self) -> int:
        return self.targets.bytes


def build_targets(
    settings: Settings,
    episode_id: int,
    *,
    rendition: Rendition | None,
    media_files: list[MediaFile],
    torrents: list[Torrent],
    torrent_files: Sequence[TorrentFile] = (),
    offline_copy: OfflineCopy | None = None,
) -> Targets:
    """Work out what deleting ``episode_id`` would remove. No I/O beyond stat.

    The rendition directory is taken from the id
    (:func:`~arc.services.media.names.output_dir_for`) *and* from the row's own
    ``dir``, because they can disagree: a rendition written before ``DATA_DIR``
    moved has an absolute path from the old layout. Whichever of the two is on
    disk and inside the roots is deleted; a row whose directory is neither is
    still removed, because a row pointing at nothing is worse than no row.

    **A batch-backed episode needs no case of its own here** (FR-A11), and that
    is worth saying out loud because it is the whole of why retention is safe
    for packs. Its file lives in ``downloads/batch/<hash>/``, so
    ``downloads/<episode id>`` does not exist and the file falls into
    :attr:`Targets.loose_files` — unlinked one file at a time and charged at its
    own ``media_files.size``, which is per-episode accounting already correct.
    Its ``torrents`` row has a null ``episode_id``, so ``torrents`` is empty and
    the client is never told to delete the pack. *Delete the file, keep the
    torrent* falls out of the data model rather than being coded for; the only
    addition is ``torrent_files``, which names the claim the deleter gives back.
    """
    roots = allowed_roots(settings)

    rendition_dir: Path | None = None
    for candidate in _rendition_dirs(settings, episode_id, rendition):
        if candidate.exists() or candidate.is_symlink():
            rendition_dir = _within(candidate, roots)
            if rendition_dir is not None:
                break

    source_dir_raw = settings.downloads_dir / str(episode_id)
    source_dir: Path | None = None
    if source_dir_raw.exists() or source_dir_raw.is_symlink():
        source_dir = _within(source_dir_raw, roots)

    loose: list[Path] = []
    file_ids: list[int] = []
    total = 0
    for media_file in media_files:
        file_ids.append(media_file.id)
        path = Path(media_file.path)
        if source_dir is not None and path.resolve().is_relative_to(source_dir):
            continue  # goes with the directory, and is counted with it below
        checked = _within(path, roots) if (path.exists() or path.is_symlink()) else None
        if checked is not None:
            loose.append(checked)
            total += _file_size(checked, media_file.size)

    # The *directory* is the truth about the source bytes: a ``media_files``
    # row knows about the episode, not about the sample and the ``.nfo`` beside
    # it, and its ``size`` is null for a file that was never probed.
    for directory in (source_dir, rendition_dir):
        if directory is not None:
            total += dir_size(directory)

    # The small copy goes with its episode (FR-P6). Its path is the id's, like
    # the rendition's; a row is deleted whether or not the file is still there.
    offline_raw = offline_path_for(settings, episode_id)
    offline_file: Path | None = None
    if offline_raw.exists() or offline_raw.is_symlink():
        offline_file = _within(offline_raw, roots)
        if offline_file is not None:
            total += _file_size(offline_file, offline_copy.size if offline_copy else None)

    return Targets(
        episode_id=episode_id,
        rendition_dir=rendition_dir,
        rendition_id=rendition.id if rendition is not None else None,
        source_dir=source_dir,
        media_file_ids=tuple(file_ids),
        loose_files=tuple(loose),
        torrent_hashes=tuple(torrent.info_hash for torrent in torrents if torrent.info_hash),
        torrent_ids=tuple(torrent.id for torrent in torrents),
        torrent_file_ids=tuple(row.id for row in torrent_files),
        offline_file=offline_file,
        offline_copy=offline_copy is not None,
        bytes=total,
    )


def _rendition_dirs(settings: Settings, episode_id: int, rendition: Rendition | None) -> list[Path]:
    """``output_dir_for`` first, then the row's own ``dir`` if it differs."""
    dirs = [output_dir_for(settings, episode_id)]
    if rendition is not None and rendition.dir:
        stored = Path(rendition.dir)
        if stored != dirs[0]:
            dirs.append(stored)
    return dirs


async def targets_for_episode(
    session: AsyncSession, settings: Settings, episode_id: int
) -> Targets:
    """:func:`build_targets` for one episode, loading its rows (FR-T4)."""
    rendition = await session.scalar(select(Rendition).where(Rendition.episode_id == episode_id))
    media_files = list(
        (await session.scalars(select(MediaFile).where(MediaFile.episode_id == episode_id))).all()
    )
    torrents = list(
        (await session.scalars(select(Torrent).where(Torrent.episode_id == episode_id))).all()
    )
    torrent_files = list(
        (
            await session.scalars(
                select(TorrentFile)
                .where(TorrentFile.episode_id == episode_id)
                .order_by(TorrentFile.id)
            )
        ).all()
    )
    return build_targets(
        settings,
        episode_id,
        rendition=rendition,
        media_files=media_files,
        torrents=torrents,
        torrent_files=torrent_files,
        offline_copy=await session.get(OfflineCopy, episode_id),
    )


# --- The rule ---------------------------------------------------------------


@dataclass(slots=True)
class _Facts:
    """The per-episode numbers the rule is decided from."""

    active_wants: int = 0
    last_dropped: datetime | None = None
    last_completed: datetime | None = None
    #: The latest ``list_entries.updated_at`` of a user whose progress covers
    #: this episode (FR-W5). Watched, on the list's word rather than on a
    #: completion row's — see the module docstring for why the column is an
    #: approximation and why erring late is the right way to err.
    last_progress: datetime | None = None
    rendition: Rendition | None = None
    media_files: list[MediaFile] = field(default_factory=list)
    torrents: list[Torrent] = field(default_factory=list)
    #: The batch claims on this episode (FR-A11). Empty for everything that is
    #: not batch-backed, which is every episode written before FR-A11 and most
    #: of them since.
    torrent_files: list[TorrentFile] = field(default_factory=list)
    #: The small offline copy, when there is one (FR-P6). It goes with the
    #: episode; on its own it is never a reason to keep one, but one being
    #: downloaded right now is (:attr:`copy_serving`).
    offline_copy: OfflineCopy | None = None
    #: ``max(watch_progress.updated_at)`` over every user (hold c). It moves on
    #: every progress sample, so it is the last time anybody played the
    #: episode — a rewatch included, which ``completed_at`` (written once)
    #: cannot see.
    last_played: datetime | None = None
    #: The latest ``updated_at`` of a partial watch: not completed, position
    #: above zero. :attr:`partial_recent` is it measured against D.
    last_partial: datetime | None = None
    #: Somebody has a partial watch updated within D days (hold b).
    partial_recent: bool = False
    #: Any user has pinned the show (hold a, FR-T8).
    pinned: bool = False
    #: The offline copy was served within :data:`COPY_SERVING_WINDOW` (hold d):
    #: a device is downloading it right now.
    copy_serving: bool = False

    def deadline(self, *, watched: timedelta, dropped: timedelta) -> _Deadline | None:
        """The latest of every anchor's own deadline (FR-T1, M20 T8).

        Each anchor runs its own grace — a completion or the progress anchor
        ``watched`` (hours), a drop ``dropped`` (G days) — and the episode may
        go only once **all** of them have run out. Taking the latest *anchor*
        and its kind would let a completion seven hours ago cut short a drop
        eight hours ago that still has days to run. ``None`` when there is no
        anchor at all (the file-age case is the caller's).
        """
        found: list[_Deadline] = []
        for moment, base, span, hours in (
            (self.last_completed, REASON_WATCHED, watched, True),
            # Also "watched": FR-W5 makes the two one fact, and a preview that
            # distinguished them would be telling an admin about Arc's table
            # layout rather than about the episode.
            (self.progress_anchor, REASON_WATCHED, watched, True),
            (self.last_dropped, REASON_DROPPED, dropped, False),
        ):
            if moment is not None:
                found.append(_Deadline(moment + span, base, moment, span, hours))
        if not found:
            return None
        return max(found, key=lambda item: item.at)

    @property
    def progress_anchor(self) -> datetime | None:
        """:attr:`last_progress`, but never earlier than the bytes themselves.

        The clamp is the difference between a rule and a trap. A list imported
        from MyAnimeList stamps ``updated_at`` once and then sits there, so on
        an episode at or below that progress the raw anchor is *months* old —
        and an admin who re-fetched that episode (FR-T3, FR-T4) would watch the
        next hourly sweep delete it before anybody could play it. Taking the
        later of the two says what was actually meant: the grace period is time
        to change your mind about *this file*, and a file that landed an hour
        ago has not had it yet.

        Only the progress anchor needs this. A completion and a drop are
        moments in the life of the episode a user was looking at, and both are
        rewritten when the episode comes back; a list entry's timestamp is
        about the show.
        """
        if self.last_progress is None:
            return None
        aged = self.file_age_from
        return self.last_progress if aged is None else max(self.last_progress, aged)

    @property
    def file_age_from(self) -> datetime | None:
        """The fallback anchor: how old the bytes themselves are."""
        moments = [
            moment
            for moment in (
                self.rendition.ready_at if self.rendition is not None else None,
                *(media_file.created_at for media_file in self.media_files),
            )
            if moment is not None
        ]
        return max(moments) if moments else None

    @property
    def watched_order(self) -> datetime | None:
        """The floor eviction's key — least recently watched first (FR-T6).

        ``max(last_played, last_completed)``, then ``last_dropped``, then the
        file's age: a year-old episode rewatched yesterday was watched
        yesterday, and is not the first to go (owner's call, M20 T8 review).
        """
        watched = [moment for moment in (self.last_played, self.last_completed) if moment]
        if watched:
            return max(watched)
        for moment in (self.last_dropped, self.file_age_from):
            if moment is not None:
                return moment
        return None

    def playing_now(self, now: datetime) -> bool:
        """Somebody played it within :data:`PLAYING_WINDOW`."""
        return self.last_played is not None and self.last_played + PLAYING_WINDOW > now

    def hold(
        self, now: datetime, played_window: timedelta, deadline: datetime | None = None
    ) -> str | None:
        """The hold that keeps this episode past its grace, if any (M20 T8).

        Checked in the order a reader would ask: a pin is the user's explicit
        word, then somebody watching it, then a device fetching it. Hold (c)'s
        window is never shorter than :data:`PLAYING_WINDOW`; hold (d) lapses
        :data:`COPY_SERVING_CEILING` after ``deadline`` (or after the copy was
        made, if later).
        """
        if self.pinned:
            return HOLD_PINNED
        if self.partial_recent:
            return HOLD_PARTIAL
        window = max(played_window, PLAYING_WINDOW)
        if self.last_played is not None and self.last_played + window > now:
            return HOLD_PLAYED
        if self.copy_serving:
            started = [
                moment
                for moment in (
                    deadline,
                    self.offline_copy.ready_at if self.offline_copy is not None else None,
                )
                if moment is not None
            ]
            if not started or max(started) + COPY_SERVING_CEILING > now:
                return HOLD_SERVING
        return None


@dataclass(frozen=True, slots=True)
class _Deadline:
    """One anchor's deadline: when its grace runs out, and how to say so."""

    at: datetime
    base: str
    anchor: datetime
    span: timedelta
    hours: bool


def _reason(base: str, anchor: datetime, grace: timedelta, *, hours: bool = False) -> str:
    """The sentence a preview shows and the log line carries.

    ``hours`` is the watched form (M20): "the 6-hour grace period ran out at
    14:00 UTC on 2026-06-01". The day form is unchanged for drops and for
    "nobody ever wanted it".
    """
    end = (anchor + grace).astimezone(UTC)
    if hours:
        count = int(grace.total_seconds() // 3600)
        return (
            f"{base}; the {count}-hour grace period ran out at "
            f"{end:%H:%M} UTC on {end.date().isoformat()}"
        )
    days = int(grace.total_seconds() // 86400)
    return f"{base}; the {days}-day grace period ran out on {end.date().isoformat()}"


async def _facts(
    session: AsyncSession, *, now: datetime, partial_window: timedelta
) -> dict[int, _Facts]:
    """Everything the rule needs, for every episode that could be a candidate.

    Set-based queries rather than a handful per episode: a library with a
    thousand ready episodes is an ordinary size, and an hourly sweep that made
    six thousand round trips would be the most expensive thing Arc does.

    ``partial_window`` is how recent a partial watch must be to set
    :attr:`_Facts.partial_recent` (D for the sweep, 24 hours for the floor);
    :attr:`_Facts.copy_serving` is measured against ``now``.
    """
    facts: dict[int, _Facts] = defaultdict(_Facts)
    retained = select(Episode.id).where(Episode.state.in_(RETAINED_STATES))

    active = await session.execute(
        select(Want.episode_id, func.count())
        .where(Want.episode_id.in_(retained), Want.dropped_at.is_(None))
        .group_by(Want.episode_id)
    )
    for episode_id, count in active.all():
        facts[episode_id].active_wants = count

    dropped = await session.execute(
        select(Want.episode_id, func.max(Want.dropped_at))
        .where(Want.episode_id.in_(retained), Want.dropped_at.is_not(None))
        .group_by(Want.episode_id)
    )
    for episode_id, moment in dropped.all():
        facts[episode_id].last_dropped = moment

    # One pass over ``watch_progress`` (it has no index on ``episode_id``) for
    # three facts: the last completion (the watched anchor), and for holds b
    # and c the last time anybody played it at all and the last partial watch.
    # ``updated_at`` moves on every progress sample.
    progress = await session.execute(
        select(
            WatchProgress.episode_id,
            func.max(WatchProgress.completed_at).filter(
                WatchProgress.completed.is_(True), WatchProgress.completed_at.is_not(None)
            ),
            func.max(WatchProgress.updated_at),
            func.max(WatchProgress.updated_at).filter(
                WatchProgress.completed.is_(False), WatchProgress.position_s > 0
            ),
        )
        .where(WatchProgress.episode_id.in_(retained))
        .group_by(WatchProgress.episode_id)
    )
    for episode_id, completed_at, played_at, partial_at in progress.all():
        fact = facts[episode_id]
        fact.last_completed = completed_at
        fact.last_played = played_at
        fact.last_partial = partial_at
        fact.partial_recent = partial_at is not None and partial_at > now - partial_window

    # FR-W5's half of "watched": every episode at or below somebody's list
    # progress, anchored on that entry's own ``updated_at``. The join is on
    # the show and the number rather than on the episode id — there is no row
    # anywhere saying "this user watched episode 4", which is exactly the
    # point of the rule — and it is one query for the whole sweep like every
    # other fact here.
    passed = await session.execute(
        select(Episode.id, func.max(ListEntry.updated_at))
        .join(ListEntry, ListEntry.anime_id == Episode.anime_id)
        .where(Episode.id.in_(retained), Episode.number <= ListEntry.progress)
        .group_by(Episode.id)
    )
    for episode_id, moment in passed.all():
        facts[episode_id].last_progress = moment

    for rendition in (
        await session.scalars(select(Rendition).where(Rendition.episode_id.in_(retained)))
    ).all():
        facts[rendition.episode_id].rendition = rendition

    for media_file in (
        await session.scalars(select(MediaFile).where(MediaFile.episode_id.in_(retained)))
    ).all():
        if media_file.episode_id is not None:
            facts[media_file.episode_id].media_files.append(media_file)

    for torrent in (
        await session.scalars(select(Torrent).where(Torrent.episode_id.in_(retained)))
    ).all():
        # ``episode_id`` is nullable since FR-A11's batches, and ``IN`` never
        # matches a null, so the query cannot return one — the guard is here to
        # say so to the reader and the type checker, as it is for media files.
        if torrent.episode_id is not None:
            facts[torrent.episode_id].torrents.append(torrent)

    # The other half of that sentence (FR-A11): a batch-backed episode has no
    # ``torrents`` row of its own, and this is where its claim is. Nothing is
    # deleted through it — the pack stays and so does the row — but the file is
    # given back before it is unlinked, and the preview names it.
    for claim in (
        await session.scalars(select(TorrentFile).where(TorrentFile.episode_id.in_(retained)))
    ).all():
        if claim.episode_id is not None:
            facts[claim.episode_id].torrent_files.append(claim)

    for copy in (
        await session.scalars(select(OfflineCopy).where(OfflineCopy.episode_id.in_(retained)))
    ).all():
        facts[copy.episode_id].offline_copy = copy
        facts[copy.episode_id].copy_serving = served_recently(
            copy, now=now, interval=COPY_SERVING_WINDOW.total_seconds()
        )

    # Hold a (FR-T8): a pin by any user covers every episode of the show.
    pinned = await session.execute(
        select(Episode.id).where(
            Episode.id.in_(retained),
            Episode.anime_id.in_(select(ShowPin.anime_id)),
        )
    )
    for (episode_id,) in pinned.all():
        facts[episode_id].pinned = True

    return facts


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """What the next sweep deletes, and what it would but for a hold (FR-T4)."""

    deletable: list[Deletable]
    #: Past their grace, kept by one of the four holds (M20 T8). Each carries
    #: its :attr:`Deletable.hold`.
    held: list[Deletable]


async def retention_plan(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    grace: timedelta | None = None,
    watched_grace: timedelta | None = None,
) -> RetentionPlan:
    """The rule, read-only: :func:`candidates` plus the episodes a hold keeps.

    ``grace`` is G (drops and "nobody ever wanted it"); ``watched_grace`` is
    the watched anchor's grace (``watched_grace_hours``). Both are read from
    ``settings`` when not given. Nothing is written here, which is what lets
    ``GET /api/retention/preview`` call it directly.
    """
    moment = now or datetime.now(UTC)
    window = grace if grace is not None else await grace_period(session)
    watched_window = (
        watched_grace
        if watched_grace is not None
        else timedelta(hours=await watched_grace_hours(session))
    )
    facts = await _facts(session, now=moment, partial_window=await unwatched_period(session))
    # A copy, a transcode or a remux (M20) is reading the source right now
    # (FR-P6). The episode
    # waits for the next sweep; the deleter asks again before it acts.
    encoding = await encoding_episode_ids(session)

    episodes = (
        await session.scalars(
            select(Episode).where(Episode.state.in_(RETAINED_STATES)).order_by(Episode.id)
        )
    ).all()

    found: list[Deletable] = []
    held: list[Deletable] = []
    for episode in episodes:
        fact = facts.get(episode.id, _Facts())
        if fact.active_wants or episode.id in encoding:
            continue

        if episode.state is EpisodeState.READY and fact.rendition is None and not fact.media_files:
            # Playable according to the table and empty on the disk: an encode
            # whose rows were rolled back, a directory removed by hand. No
            # grace period — there is nothing to wait to be sure about — and
            # the "deletion" is the reset. Whatever build_targets does find
            # (an orphaned torrent, a download directory with no row) goes
            # with it. Only a copy a device is fetching right now holds it.
            reset = Deletable(
                episode_id=episode.id,
                anime_id=episode.anime_id,
                number=episode.number,
                state=episode.state,
                reason=REASON_NO_FILES,
                targets=build_targets(
                    settings,
                    episode.id,
                    rendition=None,
                    media_files=[],
                    torrents=fact.torrents,
                    torrent_files=fact.torrent_files,
                    offline_copy=fact.offline_copy,
                ),
                hold=HOLD_SERVING if fact.copy_serving else None,
            )
            (held if fact.copy_serving else found).append(reset)
            continue

        # The grace by the anchor's kind (M20): hours after watching, G days
        # after a drop or for a file nobody ever wanted — and every anchor's
        # deadline must have passed, not only the latest anchor's.
        due = fact.deadline(watched=watched_window, dropped=window)
        if due is None:
            # Nobody ever wanted it: the bytes' own age is the anchor. No
            # rendition and no indexed file either means there is nothing whose
            # age could be measured and nothing the deletion would remove.
            aged = fact.file_age_from
            if aged is None:
                continue
            due = _Deadline(aged + window, REASON_UNWANTED, aged, window, False)
        if due.at >= moment:
            continue

        targets = build_targets(
            settings,
            episode.id,
            rendition=fact.rendition,
            media_files=fact.media_files,
            torrents=fact.torrents,
            torrent_files=fact.torrent_files,
            offline_copy=fact.offline_copy,
        )
        if targets.empty:
            continue
        hold = fact.hold(moment, watched_window, due.at)
        item = Deletable(
            episode_id=episode.id,
            anime_id=episode.anime_id,
            number=episode.number,
            state=episode.state,
            reason=_reason(due.base, due.anchor, due.span, hours=due.hours),
            targets=targets,
            anchor=due.anchor,
            hold=hold,
        )
        (found if hold is None else held).append(item)
    return RetentionPlan(deletable=found, held=held)


async def candidates(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    grace: timedelta | None = None,
    watched_grace: timedelta | None = None,
) -> list[Deletable]:
    """Every episode the next sweep would delete, with its reason and size.

    :func:`retention_plan` without the held list. Pure with respect to the
    database.
    """
    plan = await retention_plan(
        session, settings, now=now, grace=grace, watched_grace=watched_grace
    )
    return plan.deletable


# --- The disk floor (FR-T6, M20 T8) -----------------------------------------


async def floor_evictions(
    session: AsyncSession,
    settings: Settings,
    *,
    need_bytes: int,
    now: datetime | None = None,
    exclude: frozenset[int] = frozenset(),
) -> list[Deletable]:
    """What to evict, least recently watched first, to free ``need_bytes``.

    ``exclude`` names episodes already accounted for — what the normal pass
    of the same sweep deletes (or, in a dry run, would have deleted).

    The last resort under the disk floor (FR-T6, owner 2026-10-13). Grace and
    pins are **ignored**: a full disk stops every new fetch, and a pinned show
    nobody has watched for a month is the right thing to give up first. Never
    evicted, whatever the floor says:

    * anything outside :data:`RETAINED_STATES` — every
      :data:`PROTECTED_STATES` episode is being written or read;
    * an episode with a **live want** — somebody is about to watch it, and a
      pending trip copy holds one too;
    * an encode in flight, a copy being served (hold d), the copy of an
      active trip's pending episode;
    * a partial watch updated within :data:`FLOOR_PARTIAL_WINDOW`;
    * anything played within :data:`PLAYING_WINDOW` — a rewatch, or the last
      minutes after a completion at 90 % dropped the want.

    Ordered by :attr:`_Facts.watched_order` ascending —
    ``max(last_played, last_completed)``, else ``last_dropped``, else the
    file's age — and cut at the first prefix whose bytes reach ``need_bytes``.
    Read-only, like :func:`candidates`; the caller deletes one at a time and
    measures the disk again after each.
    """
    if need_bytes <= 0:
        return []
    moment = now or datetime.now(UTC)
    facts = await _facts(session, now=moment, partial_window=FLOOR_PARTIAL_WINDOW)
    encoding = await encoding_episode_ids(session)
    trip_pending = set(
        (
            await session.scalars(
                select(TripEpisode.episode_id)
                .join(Trip, Trip.id == TripEpisode.trip_id)
                .where(
                    Trip.state == TripState.ACTIVE,
                    TripEpisode.state == TripEpisodeState.PENDING,
                )
            )
        ).all()
    )
    episodes = (
        await session.scalars(
            select(Episode).where(Episode.state.in_(RETAINED_STATES)).order_by(Episode.id)
        )
    ).all()

    ranked: list[tuple[datetime, Deletable]] = []
    for episode in episodes:
        if episode.state in PROTECTED_STATES:  # pragma: no cover - not retained
            continue
        if episode.id in exclude:
            continue
        fact = facts.get(episode.id, _Facts())
        if (
            fact.active_wants
            or episode.id in encoding
            or episode.id in trip_pending
            or fact.copy_serving
            or fact.partial_recent
            or fact.playing_now(moment)
        ):
            continue
        targets = build_targets(
            settings,
            episode.id,
            rendition=fact.rendition,
            media_files=fact.media_files,
            torrents=fact.torrents,
            torrent_files=fact.torrent_files,
            offline_copy=fact.offline_copy,
        )
        if targets.empty or targets.bytes <= 0:
            continue
        order = fact.watched_order
        key = order if order is not None else datetime.max.replace(tzinfo=UTC)
        reason = (
            f"{REASON_FLOOR}; least recently watched ({order.date().isoformat()})"
            if order is not None
            else f"{REASON_FLOOR}; least recently watched"
        )
        ranked.append(
            (
                key,
                Deletable(
                    episode_id=episode.id,
                    anime_id=episode.anime_id,
                    number=episode.number,
                    state=episode.state,
                    reason=reason,
                    targets=targets,
                    anchor=order,
                ),
            )
        )

    ranked.sort(key=lambda pair: (pair[0], pair[1].episode_id))
    return _prefix([item for _, item in ranked], need_bytes)


async def in_use_now(session: AsyncSession, episode_id: int, *, now: datetime) -> bool:
    """Whether somebody is playing the episode or a device is fetching its copy.

    Asked by the floor eviction immediately before each deletion, next to the
    live-want check: the plan is minutes old by the time the last episode in
    it is reached.
    """
    played = await session.scalar(
        select(func.max(WatchProgress.updated_at)).where(WatchProgress.episode_id == episode_id)
    )
    if played is not None and played + PLAYING_WINDOW > now:
        return True
    copy = await session.get(OfflineCopy, episode_id, populate_existing=True)
    return copy is not None and served_recently(
        copy, now=now, interval=COPY_SERVING_WINDOW.total_seconds()
    )


def _prefix(ranked: list[Deletable], need_bytes: int) -> list[Deletable]:
    """The shortest head of ``ranked`` whose bytes reach ``need_bytes``."""
    chosen: list[Deletable] = []
    total = 0
    for item in ranked:
        if total >= need_bytes:
            break
        chosen.append(item)
        total += item.bytes
    return chosen


# --- Idle offline copies (FR-P6) ---------------------------------------------


#: What the idle rule's deletions are logged as.
REASON_IDLE_COPY = "nobody has fetched this offline copy"


@dataclass(frozen=True, slots=True)
class IdleCopy:
    """One ready episode's offline copy that has gone unfetched too long."""

    episode_id: int
    #: The file, resolved and checked, or ``None`` when it is already gone.
    path: Path | None
    #: ``last_served_at``, else ``ready_at``: what the idle period ran from.
    anchor: datetime
    bytes: int = 0


async def idle_copies(
    session: AsyncSession, settings: Settings, *, now: datetime, idle: timedelta
) -> list[IdleCopy]:
    """Ready episodes' copies nobody has fetched for ``idle`` (FR-P6, FR-T7).

    Counted from the last time the media route served any of the file, else
    from when it was made. A ready episode only: the copy of an episode that is
    not ``ready`` belongs to a trip, and trips have rules of their own (M19
    T4). A copy whose episode has an encode in flight is left alone, like its
    source. So is the copy of a ready episode an active trip is still waiting
    on (FR-A12): the trip's own clock (``trip_copy_days``, 14) is longer than
    the idle one (7), and the device has yet to collect it. And so is one an
    active trip's device holds unwatched (owner, 2026-10-30,
    :func:`~arc.services.trips.rules.held_episode_ids`): it is kept so a
    device that loses its file can fetch it again.
    """
    anchor = func.coalesce(OfflineCopy.last_served_at, OfflineCopy.ready_at)
    waited_on = (
        select(TripEpisode.episode_id)
        .join(Trip, Trip.id == TripEpisode.trip_id)
        .where(Trip.state == TripState.ACTIVE, TripEpisode.state == TripEpisodeState.PENDING)
    )
    rows = (
        await session.execute(
            select(OfflineCopy, anchor)
            .join(Episode, Episode.id == OfflineCopy.episode_id)
            .where(
                OfflineCopy.state == OfflineCopyState.READY,
                Episode.state == EpisodeState.READY,
                anchor.is_not(None),
                anchor < now - idle,
                OfflineCopy.episode_id.notin_(waited_on),
            )
            .order_by(OfflineCopy.episode_id)
        )
    ).all()
    if not rows:
        return []
    ids = [copy.episode_id for copy, _ in rows]
    encoding = await encoding_episode_ids(session, ids, types=COPY_WRITER_TYPES)
    encoding |= await held_episode_ids(session, ids)
    roots = allowed_roots(settings)
    found: list[IdleCopy] = []
    for copy, moment in rows:
        if copy.episode_id in encoding:
            continue
        raw = offline_path_for(settings, copy.episode_id)
        path = _within(raw, roots) if (raw.exists() or raw.is_symlink()) else None
        found.append(
            IdleCopy(
                episode_id=copy.episode_id,
                path=path,
                anchor=moment,
                bytes=_file_size(path, copy.size) if path is not None else 0,
            )
        )
    return found


# --- How much is on the disk ------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetainedUsage:
    """What Arc is holding for retained episodes, split in two (FR-T4).

    The split is the point: sources are torrent payloads that retention will
    delete on schedule, renditions are the browser-ready output that costs a
    third as much and is what a viewer actually needs. An admin deciding
    whether to buy more disk is deciding between those two numbers.
    """

    sources: int
    renditions: int
    #: How many episodes the figures cover.
    episodes: int
    #: The small offline copies on disk (FR-P6), from ``offline_copies.size``.
    #: Every ready copy, not only those of retained episodes: a trip's copy
    #: (M19 T3) sits on the disk for an episode that is not ``ready`` at all.
    offline_bytes: int = 0

    @property
    def total(self) -> int:
        return self.sources + self.renditions + self.offline_bytes


async def retained_usage(session: AsyncSession, settings: Settings) -> RetainedUsage:
    """:func:`retained_bytes`, itemised, plus the episode count.

    One implementation for both: a disk page that could disagree with the
    figure on the acquisition page would be worse than one number.
    """
    sources = await session.scalar(
        select(func.coalesce(func.sum(MediaFile.size), 0))
        .join(Episode, Episode.id == MediaFile.episode_id)
        .where(Episode.state.in_(RETAINED_STATES))
    )
    episodes = await session.scalar(
        select(func.count()).select_from(Episode).where(Episode.state.in_(RETAINED_STATES))
    )
    directories = [
        Path(stored) if stored else output_dir_for(settings, episode_id)
        for episode_id, stored in (
            await session.execute(
                select(Rendition.episode_id, Rendition.dir)
                .join(Episode, Episode.id == Rendition.episode_id)
                .where(Episode.state.in_(RETAINED_STATES))
            )
        ).all()
    ]

    def measure() -> int:
        return sum(dir_size(directory) for directory in directories)

    renditions = await asyncio.to_thread(measure) if directories else 0
    offline = await session.scalar(
        select(func.coalesce(func.sum(OfflineCopy.size), 0)).where(
            OfflineCopy.state == OfflineCopyState.READY
        )
    )
    return RetainedUsage(
        sources=int(sources or 0),
        renditions=renditions,
        episodes=int(episodes or 0),
        offline_bytes=int(offline or 0),
    )


async def retained_bytes(session: AsyncSession, settings: Settings) -> int:
    """Bytes Arc is currently holding for retained episodes (FR-T4).

    Sources come from ``media_files.size``, which the ingest already recorded,
    so that half of the answer is one ``SUM``. Renditions have no size column —
    HLS output is a few hundred segments whose total nothing writes down — so
    that half is measured on the disk, on a worker thread, and only for the
    directories of episodes that are actually retained.

    Each rendition is measured at its **own** ``dir`` when the row has one, and
    only at :func:`~arc.services.media.names.output_dir_for` when it does not.
    The two disagree for anything written before ``DATA_DIR`` moved, and this
    is the number an admin compares against the disk before deciding whether to
    delete something: a rendition parked under the old layout reported as zero
    bytes is precisely the case that makes the page misleading. It is the same
    pair :func:`build_targets` weighs up when it works out what a deletion
    would free.
    """
    return (await retained_usage(session, settings)).total


__all__ = [
    "COPY_SERVING_CEILING",
    "COPY_SERVING_WINDOW",
    "FLOOR_PARTIAL_WINDOW",
    "HOLD_PARTIAL",
    "HOLD_PINNED",
    "HOLD_PLAYED",
    "HOLD_SERVING",
    "PLAYING_WINDOW",
    "PROTECTED_STATES",
    "REASON_DROPPED",
    "REASON_FLOOR",
    "REASON_IDLE_COPY",
    "REASON_MANUAL",
    "REASON_NO_FILES",
    "REASON_UNWANTED",
    "REASON_WATCHED",
    "RETAINED_STATES",
    "RETENTION_REASON",
    "Deletable",
    "IdleCopy",
    "RetainedUsage",
    "RetentionPlan",
    "Targets",
    "allowed_roots",
    "build_targets",
    "candidates",
    "dir_size",
    "floor_evictions",
    "idle_copies",
    "in_use_now",
    "retained_bytes",
    "retained_usage",
    "retention_plan",
    "safe_path",
    "targets_for_episode",
]
