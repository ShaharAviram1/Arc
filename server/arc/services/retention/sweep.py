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

Completions are counted from ``watch_progress`` rather than from the ``wants``
rows, and that is not an oversight. A want is **deleted** the moment the user
watches past it (:mod:`arc.services.acquisition.wants` explains why), so by
the time everybody has finished an episode there is frequently no ``wants``
row left to read a user id out of. The rows that survive are exactly the
dropped ones. Taking the union — every completion, plus every drop — is what
makes "two users, one finished it a week ago and one finished it this morning"
come out as "not yet", which is the whole point of the rule.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState, MediaFile, Rendition, Torrent, Want, WatchProgress
from arc.services.media.names import output_dir_for
from arc.services.retention.rules import grace_period

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


# --- Path safety ------------------------------------------------------------


def allowed_roots(settings: Settings) -> tuple[Path, ...]:
    """The only directories retention may delete anything inside.

    Three, not two. ``renditions`` and ``downloads`` are FR-T3's own words;
    ``manual`` is there because FR-T3 also says a manually dropped file is
    deleted *itself* rather than with its directory, and the manual drop
    directory is precisely where such a file lives. Deleting a rendition of a
    file Arc may never delete would leave the loop half-closed.
    """
    return (settings.renditions_dir, settings.downloads_dir, settings.manual_dir)


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
    #: rows that carry them.
    torrent_hashes: tuple[str, ...] = ()
    torrent_ids: tuple[int, ...] = ()
    #: What all of that adds up to on disk.
    bytes: int = 0

    @property
    def empty(self) -> bool:
        """Whether there is nothing at all to remove."""
        return not (
            self.rendition_dir
            or self.rendition_id
            or self.source_dir
            or self.media_file_ids
            or self.loose_files
            or self.torrent_hashes
            or self.torrent_ids
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
) -> Targets:
    """Work out what deleting ``episode_id`` would remove. No I/O beyond stat.

    The rendition directory is taken from the id
    (:func:`~arc.services.media.names.output_dir_for`) *and* from the row's own
    ``dir``, because they can disagree: a rendition written before ``DATA_DIR``
    moved has an absolute path from the old layout. Whichever of the two is on
    disk and inside the roots is deleted; a row whose directory is neither is
    still removed, because a row pointing at nothing is worse than no row.
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

    return Targets(
        episode_id=episode_id,
        rendition_dir=rendition_dir,
        rendition_id=rendition.id if rendition is not None else None,
        source_dir=source_dir,
        media_file_ids=tuple(file_ids),
        loose_files=tuple(loose),
        torrent_hashes=tuple(torrent.info_hash for torrent in torrents if torrent.info_hash),
        torrent_ids=tuple(torrent.id for torrent in torrents),
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
    return build_targets(
        settings,
        episode_id,
        rendition=rendition,
        media_files=media_files,
        torrents=torrents,
    )


# --- The rule ---------------------------------------------------------------


@dataclass(slots=True)
class _Facts:
    """The per-episode numbers the rule is decided from."""

    active_wants: int = 0
    last_dropped: datetime | None = None
    last_completed: datetime | None = None
    rendition: Rendition | None = None
    media_files: list[MediaFile] = field(default_factory=list)
    torrents: list[Torrent] = field(default_factory=list)

    @property
    def anchor(self) -> tuple[datetime, str] | None:
        """When the grace period starts, and what that says about the episode."""
        moments = [
            (moment, reason)
            for moment, reason in (
                (self.last_completed, REASON_WATCHED),
                (self.last_dropped, REASON_DROPPED),
            )
            if moment is not None
        ]
        if not moments:
            return None
        return max(moments, key=lambda pair: pair[0])

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


def _reason(base: str, anchor: datetime, grace: timedelta) -> str:
    """The sentence a preview shows and the log line carries."""
    days = int(grace.total_seconds() // 86400)
    return f"{base}; the {days}-day grace period ran out on {(anchor + grace).date().isoformat()}"


async def _facts(session: AsyncSession) -> dict[int, _Facts]:
    """Everything the rule needs, for every episode that could be a candidate.

    Six set-based queries rather than six per episode: a library with a
    thousand ready episodes is an ordinary size, and an hourly sweep that made
    six thousand round trips would be the most expensive thing Arc does.
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

    completed = await session.execute(
        select(WatchProgress.episode_id, func.max(WatchProgress.completed_at))
        .where(
            WatchProgress.episode_id.in_(retained),
            WatchProgress.completed.is_(True),
            WatchProgress.completed_at.is_not(None),
        )
        .group_by(WatchProgress.episode_id)
    )
    for episode_id, moment in completed.all():
        facts[episode_id].last_completed = moment

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
        facts[torrent.episode_id].torrents.append(torrent)

    return facts


async def candidates(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    grace: timedelta | None = None,
) -> list[Deletable]:
    """Every episode the next sweep would delete, with its reason and size.

    Pure with respect to the database: nothing is written here, which is what
    lets ``GET /api/retention/preview`` call it directly.
    """
    moment = now or datetime.now(UTC)
    window = grace if grace is not None else await grace_period(session)
    facts = await _facts(session)

    episodes = (
        await session.scalars(
            select(Episode).where(Episode.state.in_(RETAINED_STATES)).order_by(Episode.id)
        )
    ).all()

    found: list[Deletable] = []
    for episode in episodes:
        fact = facts.get(episode.id, _Facts())
        if fact.active_wants:
            continue

        if episode.state is EpisodeState.READY and fact.rendition is None and not fact.media_files:
            # Playable according to the table and empty on the disk: an encode
            # whose rows were rolled back, a directory removed by hand. No
            # grace period — there is nothing to wait to be sure about — and
            # the "deletion" is the reset. Whatever build_targets does find
            # (an orphaned torrent, a download directory with no row) goes
            # with it.
            found.append(
                Deletable(
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
                    ),
                )
            )
            continue

        anchored = fact.anchor
        if anchored is not None:
            anchor, base = anchored
        else:
            # Nobody ever wanted it: the bytes' own age is the anchor. No
            # rendition and no indexed file either means there is nothing whose
            # age could be measured and nothing the deletion would remove.
            aged = fact.file_age_from
            if aged is None:
                continue
            anchor, base = aged, REASON_UNWANTED
        if anchor + window >= moment:
            continue

        targets = build_targets(
            settings,
            episode.id,
            rendition=fact.rendition,
            media_files=fact.media_files,
            torrents=fact.torrents,
        )
        if targets.empty:
            continue
        found.append(
            Deletable(
                episode_id=episode.id,
                anime_id=episode.anime_id,
                number=episode.number,
                state=episode.state,
                reason=_reason(base, anchor, window),
                targets=targets,
                anchor=anchor,
            )
        )
    return found


# --- How much is on the disk ------------------------------------------------


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
    sources = await session.scalar(
        select(func.coalesce(func.sum(MediaFile.size), 0))
        .join(Episode, Episode.id == MediaFile.episode_id)
        .where(Episode.state.in_(RETAINED_STATES))
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
    return int(sources or 0) + renditions


__all__ = [
    "PROTECTED_STATES",
    "REASON_DROPPED",
    "REASON_MANUAL",
    "REASON_NO_FILES",
    "REASON_UNWANTED",
    "REASON_WATCHED",
    "RETAINED_STATES",
    "RETENTION_REASON",
    "Deletable",
    "Targets",
    "allowed_roots",
    "build_targets",
    "candidates",
    "dir_size",
    "retained_bytes",
    "safe_path",
    "targets_for_episode",
]
