"""Taking a batch, file by file (FR-A4's exception, FR-A11).

A **finished** show whose search ends with no acceptable single at all may take
a batch that covers the wanted episode and download **only that episode's
file** (spec §4.2 FR-A4, architecture.md §5.1a). ``nyaa`` decides which packs
are worth considering; this module is everything that happens after that — the
pieces ``search_release`` needs and one sentence to show a user when none of
them worked.

**:func:`plan_files` is pure, and it is where the byte guarantee is decided.**
The client is handed the ``.torrent`` itself rather than a magnet precisely so
that the file list exists before anything moves, and this is the function that
reads it: one :class:`~arc.services.acquisition.qbit.FileInfo` per file in,
``episode number → the file that holds it`` out. Its rules are the filter's
rules one level down — the same parser, the same season agreement, the same
absolute reading, the same title floor — with two differences that matter:

* the *release name* has already carried the show's identity past
  :data:`~arc.services.acquisition.nyaa.TITLE_THRESHOLD`, so a file only has to
  carry a number. A file whose parsed title says something else (a bonus OVA of
  another show inside the pack) is dropped, but a file that names no title at
  all is not held against the pack;
* **exactly one** file for the episode the search is *for*. Zero files, or two
  (a v1 beside a v2, two directories, a sample named like the episode), and the
  plan is **refused** — never resolved by size, version or position, because
  that is a guess and "never guess which file" is the non-negotiable this whole
  path is written around. A refused plan means the batch is deleted from the
  client before it has fetched a byte and the episode keeps FR-A6's ordinary
  retry, ending :data:`UNREADABLE_BATCH`. An episode that was only riding along
  is *dropped* instead: the pack is still the answer for the episode Arc came
  for, and the free rider has a search of its own.

**:func:`claim_existing` is why the second episode is free.** It runs *before
Nyaa is asked at all*: if some batch already in the client holds a file for this
episode, Arc turns that file on and starts the torrent again rather than
searching. Episode 11 of *Kimetsu no Yaiba* then costs zero Nyaa requests and
zero new bytes of overhead, which is the whole of "one batch, several wants".

**The two tables are written in two halves, and the order is the race.**
:func:`reserve_batch` inserts the ``torrents`` row — no episode of its own,
which is what makes every query written before FR-A11 skip a shared torrent
rather than delete it with its files — **before** anything is written to the
client's selection, so that the loser of two searches racing on one pack raises
:class:`BatchTaken` rather than wiping the winner's selection with its own
``filePrio 0``. :func:`record_files` then writes the sizes and one
``torrent_files`` row per file, each carrying the episode it holds, whether Arc
asked for it, and the priority Arc actually wrote. :func:`mark_unreadable`
leaves the third kind of row: a tombstone for a pack whose contents Arc will
never be able to read, so the pick's "a hash with a row was already tried" skips
it instead of fetching fourteen gigabytes of metadata again every six hours.

**:func:`disposition` is the one decision about what becomes of a pack.** Four
paths take a file away from a batch — a want withdrawn, retention, a rejected
member, a poll that gave up — and each of them knows only why *its* file went.
Whether the torrent is then kept, stopped or deleted with its files is one
question about all of them, so it is answered once, here, and applied by the
``qbit_reselect`` job. :func:`listing_mismatch` is that job's gate: the rows
were written when the pack was picked, and priorities are written to indices, so
a listing that no longer matches them is a listing nothing is written against.

How those four paths take the file *away* is
:mod:`arc.services.acquisition.claims`, which is three functions and no
imports worth speaking of — the reconciler, the review's reject and the
retention deleter all need them, and all three are reached from packages this
module imports, so they cannot import this one back.

Nothing here talks to qBittorrent. The call sequence lives in
``jobs._take_batch``, because it is the handler that holds the client; what is
here is the arithmetic, the rows, and the decisions that must not be spread
across three modules.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    ListEntry,
    ListStatus,
    Torrent,
    TorrentFile,
    TorrentKind,
)
from arc.services.acquisition.claims import enqueue_reselect
from arc.services.acquisition.nyaa import (
    TITLE_THRESHOLD,
    Ranked,
    anime_season,
    anime_titles,
    title_score,
)
from arc.services.acquisition.qbit import (
    DECIDED_STATES,
    FILE_OFF,
    FILE_ON,
    QBIT_MISSING,
    QBIT_UNREADABLE,
    FileInfo,
)
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.acquisition.states import transition
from arc.services.library.parser import VIDEO_EXTENSIONS, ParsedName, parse

log = logging.getLogger(__name__)

#: The sentence a user is shown when every batch on offer was refused (FR-A6,
#: FR-A7, the plan's D5). It says both halves of what happened, because either
#: on its own is misleading: there *were* releases, and none of them could be
#: read. A missing file is visible and fixable; the wrong episode plays as
#: though it were right.
UNREADABLE_BATCH: Final[str] = "only batch releases, and their files could not be identified"

#: What a batch's ``torrent_files`` row says when a later want attaches to it
#: (:func:`claim_existing`). Written on the *episode*, so the show page's row
#: explains a download that started without a search.
ATTACHED: Final[str] = "attached to a batch arc already has"

#: The file kinds a batch member may be. Everything else — ``nc`` (a creditless
#: opening), ``batch`` (a multi-episode blob inside a pack), ``movie``,
#: ``special``, ``unknown`` (fonts, ``sample.mkv``, a bare ``07.mkv`` anitopy
#: can make nothing of) — maps to no episode and stays at
#: :data:`~arc.services.acquisition.qbit.FILE_OFF`. That is how extras are
#: excluded without a rule of their own, and it fails closed: a file Arc cannot
#: read is a file Arc does not fetch.
EPISODE_KINDS: Final[frozenset[str]] = frozenset({"episode"})

#: The list states that make a pack worth keeping once nothing in it is wanted
#: (:func:`disposition`): the two that generate wants, **and ``on_hold``**
#: (owner, 2026-09-18). That third one is deliberately wider than
#: :data:`~arc.services.acquisition.wants.WANTING_STATUSES` and the difference
#: is the point — the reconciler is answering "should Arc fetch more of this?",
#: to which ``on_hold`` is no, and this is answering "could this pack ever be
#: wanted again?", to which a paused viewer is yes. A stopped pack with every
#: file at priority 0 costs nothing on disk beyond the files it has already
#: fetched, which are retention's to measure and delete per episode (FR-T1), so
#: keeping it is the cheap side of the bet: the alternative charges the day the
#: show comes off hold a fourteen-gigabyte metadata fetch and a fresh pick.
#:
#: Spelled out here rather than imported from the reconciler because the
#: reconciler will import *this* module to enqueue the re-selection, and a
#: three-name rule is better repeated than made into an import cycle.
LIVE_STATUSES: Final[tuple[ListStatus, ...]] = (
    ListStatus.WATCHING,
    ListStatus.PLANNED,
    ListStatus.ON_HOLD,
)

#: The ``qbit_state`` values :func:`claim_existing` will not attach to. Arc's
#: own four decisions plus :data:`~arc.services.acquisition.qbit.QBIT_MISSING`
#: — which is not a decision but is the same answer to the same question: this
#: torrent is not going to complete another episode's file, so an episode that
#: attached to it would sit ``downloading`` for ever.
UNATTACHABLE_STATES: Final[frozenset[str]] = DECIDED_STATES | {QBIT_MISSING}


def start_downloading(episode: Episode, reason: str) -> None:
    """Move an episode a batch now holds to ``downloading``.

    The state machine has no ``wanted → downloading`` edge, and it is right not
    to: an episode goes looking before it finds. A batch is the one place where
    an episode that has not started looking is nonetheless about to have bytes
    arriving — the second want a pack picks up for free — so it takes the two
    edges it would have taken anyway, one after the other, rather than the
    machine growing a shortcut that only this path would ever use.
    """
    if episode.state is EpisodeState.WANTED:
        transition(episode, EpisodeState.SEARCHING, reason=reason)
    transition(episode, EpisodeState.DOWNLOADING, reason=reason)


def size_label(size: int) -> str:
    """``1181116006`` → ``"1.1 GB"``.

    Binary GB, like :data:`~arc.services.acquisition.rules.BYTES_PER_GB` and
    like ``df``. For the log line that proves FR-A4's spirit is intact: "26
    files, 2 wanted, 1.1 GB of 14.8 GB" is the sentence a reader judges this
    whole feature by.
    """
    return f"{size / BYTES_PER_GB:.1f} GB"


# --- Planning which files to fetch ------------------------------------------


@dataclass(frozen=True, slots=True)
class FilePlan:
    """What :func:`plan_files` made of one batch's contents (FR-A11)."""

    #: The show the plan was read against, so :func:`record_files` can resolve
    #: :attr:`episodes`' numbers to episode ids without being handed the row
    #: again.
    anime_id: int
    #: Every file in the torrent, in the order the client listed them. All of
    #: them get a ``torrent_files`` row: the ones Arc did not ask for are the
    #: record of what it declined, and the rows are what a later want attaches
    #: to (:func:`claim_existing`).
    files: tuple[FileInfo, ...]
    #: ``episode number → the file that holds it``, for **every** episode of
    #: this show the pack's file names identify — not only the wanted ones,
    #: because the not-wanted rows are exactly what makes the second want free.
    #: A number two files claim is absent: ambiguity maps to nothing.
    episodes: Mapping[int, FileInfo]
    #: The episode numbers this plan turns **on**, sorted. Empty when the plan
    #: was refused.
    wanted: tuple[int, ...]
    #: The free riders that asked and were not served: episodes somebody wants
    #: whose file this pack turned out not to hold, or held twice. They are
    #: *dropped*, not refused — the pack is still the answer for the episode
    #: the search is for, and each of these has a search of its own (FR-A6).
    dropped: tuple[int, ...] = ()
    #: Why this batch cannot be used, or ``None``. A plan with a reason is
    #: never acted on: the caller deletes the torrent it had added and tries
    #: the next candidate.
    refused_reason: str | None = None

    @property
    def indices(self) -> tuple[int, ...]:
        """Every file index in the torrent — what is turned **off** first."""
        return tuple(sorted(info.index for info in self.files))

    @property
    def wanted_files(self) -> tuple[FileInfo, ...]:
        """The files to fetch, in episode order."""
        return tuple(self.episodes[number] for number in self.wanted)

    @property
    def wanted_indices(self) -> tuple[int, ...]:
        """Their indices — what is turned back **on**, and nothing else."""
        return tuple(sorted(info.index for info in self.wanted_files))

    @property
    def wanted_bytes(self) -> int:
        """The sum of the selected files.

        **The only size figure any rule, log or reservation may use for a
        batch** (FR-A11): the point of the exception is that the 14 GB pack
        costs the 1.1 GB of the episodes Arc asked for.
        """
        return sum(info.size for info in self.wanted_files)

    @property
    def total_size(self) -> int:
        """The whole payload. Reported and logged, never reasoned with."""
        return sum(info.size for info in self.files)


def _member_number(parsed: ParsedName, *, season: int | None, offset: int | None) -> int | None:
    """Which episode of the entry this file is, or ``None`` for "not one".

    The single-episode half of :func:`~arc.services.acquisition.nyaa.acceptable`
    read from the other end: that function asks "is this release the episode I
    want?", and this one asks "which episode is this file?" of the same
    ingredients in the same order.

    * the kind must be ``episode`` (:data:`EPISODE_KINDS`) and the extension a
      video one — a ``.nfo`` that names a number is not the episode;
    * a file that names a **season** must name this one. One that names none is
      this entry's season by the same convention the filter uses;
    * and the **absolute** reading applies where the caller passed an offset:
      ``Jujutsu Kaisen - 25`` inside a ``25 ~ 47`` pack is episode 1 of a second
      season that follows 24. Only on a file that names no season — one that
      does has already answered the question the arithmetic is asking — and only
      above the prequel's own total, so the reading can never collide with the
      prequel's numbering.
    """
    if parsed.kind not in EPISODE_KINDS or parsed.episode is None:
        return None
    if not parsed.extension or parsed.extension not in VIDEO_EXTENSIONS:
        return None
    absolute = offset is not None and parsed.season is None and parsed.episode > offset
    if not absolute and (parsed.season or 1) != (season or 1):
        return None
    return parsed.episode - offset if absolute and offset is not None else parsed.episode


def plan_files(
    anime: Anime,
    wanted_numbers: Collection[int],
    files: Sequence[FileInfo],
    *,
    required: int,
    offset: int | None = None,
) -> FilePlan:
    """Map a batch's files to episodes, or refuse it. Pure (FR-A11).

    ``wanted_numbers`` are the episodes this batch is being taken **for** and
    ``required`` is the one the search is *for* (``jobs`` builds the first with
    :func:`targets`; the second is the episode holding the job). The difference
    between them is the whole of the refusal rule:

    * ``required`` must be identified by **exactly one** file. Zero, or two —
      a v1 beside a v2, two directories, a sample named like the episode — and
      the plan is **refused**: never resolved by size, version or position,
      because that is a guess, and the pack is then deleted before it has
      fetched anything (the plan's D5, and FR-A4's "where *the* wanted
      episode's file cannot be identified");
    * every **other** wanted number is a free rider, and a free rider the pack
      turns out not to hold is *dropped* (:attr:`FilePlan.dropped`), not a
      reason to refuse. It has a `search_release` of its own, which will find
      its own pack or attach to this one later; refusing a pack that holds the
      episode Arc came for because it does not also hold next week's would
      leave both episodes unfetched.

    ``offset``, when given, is the numbering the **release** is written in
    rather than the entry's own: ``Jujutsu Kaisen - 25`` in a ``25 ~ 47`` pack
    is episode 1 of a second season that follows 24. The caller picks it with
    :func:`plan_offset`, and the reason it is not simply the entry's offset is
    that a pack accepted per-season must be *read* per-season — otherwise season
    two's own episode 25 would be read as its episode 1, which is the one
    mistake this function must not make.

    Identification is deliberately generous about the title and strict about
    everything else. The release name cleared the title floor already, so a
    file that names no title (``- 10 [1080p].mkv``) is taken at its number,
    while a file whose title *does* say something and scores below
    :data:`~arc.services.acquisition.nyaa.TITLE_THRESHOLD` is dropped — that is
    the bonus OVA of another franchise a pack sometimes carries. Extras,
    creditless openings, samples, fonts and ``.nfo``s map to nothing through
    :func:`_member_number` and stay at
    :data:`~arc.services.acquisition.qbit.FILE_OFF`.
    """
    titles = anime_titles(anime)
    season = anime_season(anime)

    claims: dict[int, list[FileInfo]] = {}
    for info in files:
        # **The basename only, directory components discarded.** The same call
        # ``library/jobs._parsed_of`` makes, so the parser corpus governs this
        # too — and it means a pack's own directory names are not evidence:
        # ``Season 2/- 01.mkv`` parses as episode 1 with no season named, and
        # what saves it from being read as this entry's episode 1 is not the
        # directory but the **exactly one claim** rule below, since a
        # complete-franchise pack holding both seasons has a ``Season 1/- 01``
        # beside it and the number is then ambiguous and refused.
        parsed = parse(PurePosixPath(info.name).name, path=True)
        number = _member_number(parsed, season=season, offset=offset)
        if number is None or number < 1:
            continue
        if parsed.title_key:
            similarity = title_score(parsed.title_key, titles)
            if similarity < TITLE_THRESHOLD:
                log.debug(
                    "a file inside a batch is another show",
                    extra={
                        "path": info.name,
                        "title_key": parsed.title_key,
                        "similarity": round(similarity, 2),
                    },
                )
                continue
        claims.setdefault(number, []).append(info)

    episodes = {number: found[0] for number, found in claims.items() if len(found) == 1}

    def why(number: int) -> str | None:
        """Why this episode has no file, or ``None`` when it has exactly one."""
        holders = claims.get(number, [])
        if len(holders) > 1:
            names = ", ".join(sorted(info.name for info in holders))
            return f"{len(holders)} files claim episode {number}: {names}"
        return None if holders else f"no file in the batch is episode {number}"

    refused = why(required)
    if refused is not None:
        return FilePlan(
            anime_id=anime.id,
            files=tuple(files),
            episodes=episodes,
            wanted=(),
            refused_reason=refused,
        )

    wanted: list[int] = []
    dropped: list[int] = []
    for number in sorted(set(wanted_numbers) | {required}):
        reason = why(number)
        if reason is None:
            wanted.append(number)
            continue
        dropped.append(number)
        # One line, because "why did episode 8 not come with episode 7?" is a
        # question somebody will ask of a pack that served one of them.
        log.info(
            "a batch does not hold an episode that asked to ride along",
            extra={"anime_id": anime.id, "number": number, "reason": reason},
        )

    return FilePlan(
        anime_id=anime.id,
        files=tuple(files),
        episodes=episodes,
        wanted=tuple(wanted),
        dropped=tuple(dropped),
    )


def plan_offset(chosen: Ranked, entry_offset: int | None) -> int | None:
    """Which numbering to read a pack's files in (FR-A4's absolute rule).

    Three cases, and they are three different claims about the release:

    * a pack accepted by the **absolute** reading of its own range carries that
      reading into its files (``25 ~ 47`` → ``- 25`` is episode 1);
    * a pack whose range is the entry's own numbering is read that way, offset
      or no offset — a group that numbered a season pack 1–26 means 1–26, and
      shifting it would read episode 25 as episode 1;
    * a pack that names **no range at all** is the interesting one. It claimed
      nothing, so there is nothing to read its files against except the entry —
      and a complete-series pack of an absolutely numbered second season is
      exactly the shape unnamed packs come in, so the *entry's* offset applies.
      A file that names a season, or whose number is at or below the prequel's
      total, is unaffected either way (:func:`_member_number`).
    """
    if chosen.candidate.offset:
        return chosen.candidate.offset
    if chosen.candidate.covers:
        return None
    return entry_offset or None


def targets(chosen: Ranked, number: int, attachable: Collection[int]) -> tuple[int, ...]:
    """Which episodes to take this batch for: the searched one, and the free ride.

    The episode being searched for is always in, and so is every other episode
    of the show that somebody wants right now **and** that this release's own
    name says it holds. That second half is the "one batch, several wants"
    payoff taken at the only moment it is free — the files are already being
    selected — and it is bounded by the name rather than by hope: a pack of
    episodes 1–13 is not asked to produce episode 20, so it is not refused for
    failing to.

    A pack that names **no** range at all claims nothing and possibly
    everything, and its coverage is settled by its file list rather than its
    name (:attr:`~arc.services.acquisition.nyaa.Candidate.covers` is empty), so
    every wanted episode is asked of it. If it turns out to hold only some of
    them the plan is refused and the next candidate is tried, which is the
    right way round: a complete-series pack holds all of them.
    """
    span = set(chosen.candidate.covers)
    if not span:
        return tuple(sorted({number, *attachable}))
    shift = chosen.candidate.offset
    return tuple(sorted({number} | {other for other in attachable if other + shift in span}))


def verify_selection(
    before: Sequence[FileInfo], after: Sequence[FileInfo], wanted: Collection[int]
) -> str | None:
    """Whether the client's file list still says what Arc wrote. ``None`` is good.

    Step 6 of the add sequence and the gate the byte guarantee rests on. The
    torrent is still stopped when this runs, so a disagreement costs nothing:
    it is deleted with its files and the next candidate is tried.

    Three ways to disagree, and each of them is a reason not to start a
    fourteen-gigabyte torrent:

    * a file **outside** the wanted set has a priority above
      :data:`~arc.services.acquisition.qbit.FILE_OFF`. That is the one that
      matters — it is bytes nobody asked for;
    * a **wanted** file is off, which means the second write did not land and
      starting the torrent would fetch nothing at all;
    * the names or the sizes moved between the two listings, or the count did.
      The plan was read off the first listing, so a client that now describes
      the torrent differently has invalidated the mapping the selection was
      built from.
    """
    if len(before) != len(after):
        return f"the client listed {len(before)} files and then {len(after)}"
    wanted_indices = {int(index) for index in wanted}
    first = {info.index: info for info in before}
    for info in after:
        original = first.get(info.index)
        if original is None:
            return f"file {info.index} was not in the first listing"
        if original.name != info.name or original.size != info.size:
            return f"file {info.index} changed name or size between the two listings"
        if info.index in wanted_indices:
            if not info.wanted:
                return f"file {info.index} was asked for and is not selected"
        elif info.wanted:
            return f"file {info.index} is selected and was not asked for"
    return None


def listing_mismatch(rows: Sequence[TorrentFile], listed: Sequence[FileInfo]) -> str | None:
    """Whether the client still describes the pack Arc's rows describe. ``None`` is good.

    :func:`verify_selection`'s sibling, and the gate the **re-selection** rests
    on (``jobs.qbit_reselect``). That one compares two listings taken seconds
    apart; this one compares a listing taken today against rows written when the
    pack was picked, which may have been last week — so what it is really
    checking is that ``torrent_files`` is still an accurate map of the torrent
    the client is holding under this hash.

    A disagreement means the indices Arc is about to write priorities to are not
    the files Arc thinks they are, and ``filePrio`` takes nothing but an index:
    writing 1 to the wrong index would fetch a file nobody asked for, and
    writing 0 to the wrong one would leave the file somebody is waiting for off.
    Neither is a thing to do on a guess, so the caller writes **nothing** and
    says so at ``ERROR`` — this cannot happen without something outside Arc
    having replaced the torrent, and a person should look at it.

    The count is compared as well as the names, because a listing with files
    Arc has no row for is a pack whose extra files would never be turned *off*.
    """
    if len(rows) != len(listed):
        return f"arc recorded {len(rows)} files and the client lists {len(listed)}"
    by_index = {info.index: info for info in listed}
    for row in rows:
        info = by_index.get(row.file_index)
        if info is None:
            return f"the client does not list file {row.file_index}"
        if info.name != row.path:
            return f"file {row.file_index} is {info.name!r} and arc recorded {row.path!r}"
    return None


# --- Writing and attaching --------------------------------------------------


async def claim_existing(session: AsyncSession, episode: Episode) -> TorrentFile | None:
    """Attach this episode to a batch already in the client, if one holds it.

    Checked **before Nyaa is asked at all**, which is the whole point: a batch
    taken for episode 10 of *Kimetsu no Yaiba* already holds episode 11, so
    episode 11 costs zero Nyaa requests, zero new bytes of overhead and no
    second copy of a fourteen-gigabyte pack. The row was written with
    ``wanted = false`` at pick time and turning it on is the entire operation.

    Its caller checks ``batch_fallback`` first (FR-D2): an admin who turns the
    switch off means "fetch no more of this", and enabling a file is fetching
    more. What keeps running is what is already claimed — a pack mid-download
    finishes the episodes it holds claims for, because stranding those bytes is
    not what a kill switch is for.

    Only a row whose torrent Arc has **not** decided about
    (:data:`~arc.services.acquisition.qbit.DECIDED_STATES`) and which is not
    :data:`~arc.services.acquisition.qbit.QBIT_MISSING`: a pack that stalled,
    was cancelled, was rejected, could not be read, or simply is not in the
    client any more is not a pack to serve another episode from — attaching to
    one would leave the episode ``downloading`` against a torrent nothing will
    ever complete, and the ordinary search is the right answer instead.

    **The row's history is reset with the claim**, and that is not tidiness.
    A row Arc has had before carries ``completed_at`` and ``progress`` from the
    last time its file arrived — and retention may since have unlinked that
    file. Left standing, ``completed_at`` tells ``qbit_reselect`` there is
    nothing to fetch (so it stops the pack instead of starting it), tells the
    poll the pack is settled (so it never asks for a file list again) and tells
    ``_complete_file`` to hand over a path that is not there, once a minute, for
    ever — an episode stranded in ``downloaded`` holding the one live claim that
    stops it ever being fetched again. Turning the file back on means asking for
    it again, so what is known about the last copy goes with the decision.

    The client is not touched here. Writing the priority and starting the
    torrent again is :data:`~arc.services.acquisition.names.QBIT_RESELECT`'s
    job, for the two reasons ``qbit_cancel`` is a job: a handler must not hold
    its transaction across an HTTP call, and an unreachable client must not be
    able to fail the decision — which is already in the database by the time the
    job runs.

    The pack is locked before the claim is written (``FOR UPDATE``), against the
    one race that could lose it: ``qbit_reselect`` deciding DELETE on the very
    pack this is attaching to. The lock orders the two, and whichever runs
    second sees what the first did — a delete that got there first leaves no
    torrent to find, and an attach that got there first is a wanted row the
    disposition reads as KEEP.
    """
    row = await session.scalar(
        select(TorrentFile)
        .join(Torrent, Torrent.id == TorrentFile.torrent_id)
        .where(
            TorrentFile.episode_id == episode.id,
            TorrentFile.wanted.is_(False),
            Torrent.kind == TorrentKind.BATCH,
            or_(Torrent.qbit_state.is_(None), Torrent.qbit_state.not_in(UNATTACHABLE_STATES)),
        )
        .order_by(TorrentFile.id)
        .limit(1)
    )
    if row is None:
        return None
    torrent = await session.get(Torrent, row.torrent_id, with_for_update=True)
    if torrent is None or torrent.qbit_state in UNATTACHABLE_STATES:
        # Decided about, or deleted outright, between the two statements.
        return None

    row.wanted = True
    # Whatever was known about the last copy of this file is not known about
    # the one being asked for now (see above).
    row.completed_at = None
    row.progress = None
    start_downloading(episode, ATTACHED)
    await session.flush()
    await enqueue_reselect(session, row.torrent_id)
    log.info(
        "an episode attached to a batch arc already has",
        extra={
            "episode_id": episode.id,
            "torrent_id": row.torrent_id,
            "file_index": row.file_index,
            "path": row.path,
        },
    )
    return row


async def _episode_ids(
    session: AsyncSession, anime_id: int, numbers: Collection[int]
) -> dict[int, int]:
    """``episode number → id`` for one show, in one query."""
    if not numbers:
        return {}
    rows = await session.execute(
        select(Episode.number, Episode.id).where(
            Episode.anime_id == anime_id, Episode.number.in_(sorted(set(numbers)))
        )
    )
    return {number: episode_id for number, episode_id in rows.all()}


class BatchTaken(RuntimeError):
    """Another search reserved this pack first (:func:`reserve_batch`)."""


async def reserve_batch(
    session: AsyncSession,
    *,
    ranked: Ranked,
    save_path: str | None,
    info_hash: str,
    state: str = "added",
) -> Torrent:
    """Claim a pack in the database **before** its selection is written.

    This is the order the whole race turns on, and it is the single path's own
    order (``jobs._record_torrent`` inserts before it adds the magnet). Two
    searches for two episodes of one finished show run on two worker slots;
    both pass the "a hash with a row was already tried" check, because neither
    has committed. If the selection came first, the loser would run ``filePrio
    0`` over **every** index of a torrent the winner had already selected files
    in and **started** — wiping the winner's selection and leaving an episode
    downloading nothing — and would only find out it had lost when it came to
    write its rows. Reserving first means the loser's flush raises *here*,
    before it has touched the client's selection at all.

    ``torrents.info_hash`` is unique, so the database is the arbiter either
    way; the explicit lookup is so the loser fails by name rather than on a
    flush, and :class:`BatchTaken` is what the caller catches to retry —
    whereupon :func:`claim_existing` attaches to the winner's row instead of
    adding the pack a second time.

    ``episode_id`` is **null** and ``kind`` is ``batch``, which is not a detail
    but the design: every query keyed on that column — the reconciler's cancel,
    ``qbit_cancel``, ``reject_download``, retention's hashes, ``poll_qbit``'s
    join — then skips this row by default, and each of them would otherwise
    delete a torrent several episodes share, with its files.

    The sizes and the ``torrent_files`` rows are **not** written here: they are
    facts about a selection that has not been made yet
    (:func:`record_files`). ``state`` is ``added`` for a real reservation and
    :data:`~arc.services.acquisition.qbit.QBIT_UNREADABLE` for the tombstone a
    refused pack leaves behind (:func:`mark_unreadable`).
    """
    existing = await session.scalar(select(Torrent.id).where(Torrent.info_hash == info_hash))
    if existing is not None:
        raise BatchTaken(
            f"{info_hash} was recorded as torrent {existing} while this batch was being added"
        )

    item = ranked.item
    torrent = Torrent(
        kind=TorrentKind.BATCH,
        episode_id=None,
        info_hash=info_hash,
        magnet=item.magnet,
        title=item.title,
        group=ranked.candidate.group,
        resolution=ranked.candidate.resolution,
        seeders=item.seeders,
        trusted=item.trusted,
        save_path=save_path,
        qbit_state=state,
        progress=0.0,
    )
    session.add(torrent)
    await session.flush()
    return torrent


async def mark_unreadable(
    session: AsyncSession, *, ranked: Ranked, info_hash: str, reason: str
) -> Torrent | None:
    """Leave a tombstone for a pack Arc will never be able to read (FR-A11).

    :data:`~arc.services.acquisition.qbit.QBIT_UNREADABLE`, no
    ``torrent_files`` rows, no episode. It exists because the pick skips any
    hash that already has a row: without it, the same unidentifiable
    fourteen-gigabyte pack would be fetched from Nyaa, added, read and deleted
    again every six hours, for **every** episode of the show, for the whole of
    FR-A6's fortnight.

    Only ever for a reason that cannot change — files that could not be
    identified, more files than Arc will read a selection out of, a blob whose
    hash was not the one the feed advertised. A read-back the client disagreed
    with and a Nyaa that would not answer leave no row at all, because those
    are true today and may not be tomorrow.

    ``None`` when the hash already has a row, which is not a failure: somebody
    else reserved it in the meantime and their row is the better record.
    """
    try:
        torrent = await reserve_batch(
            session,
            ranked=ranked,
            # No save path: the pack has been deleted from the client and
            # nothing of it is on disk, so a path here would be a claim about
            # a directory that does not exist.
            save_path=None,
            info_hash=info_hash,
            state=QBIT_UNREADABLE,
        )
    except BatchTaken:
        return None
    log.info(
        "a batch was recorded as unreadable so it is not tried again",
        extra={"hash": info_hash, "title": ranked.item.title, "reason": reason},
    )
    return torrent


async def record_files(session: AsyncSession, torrent: Torrent, plan: FilePlan) -> None:
    """Write the ``torrent_files`` rows and the two sizes (FR-A11).

    The other half of :func:`reserve_batch`, run once the selection has been
    written to the client and verified: one row per file, carrying the episode
    the parser read out of its name, whether Arc asked for it, and the priority
    Arc actually wrote. ``wanted_bytes`` beside ``total_size`` because the first
    is the figure any rule may read and the second is only ever reported.
    """
    torrent.total_size = plan.total_size
    torrent.wanted_bytes = plan.wanted_bytes

    ids = await _episode_ids(session, plan.anime_id, plan.episodes)
    number_of = {info.index: number for number, info in plan.episodes.items()}
    wanted = set(plan.wanted)
    for info in plan.files:
        number = number_of.get(info.index)
        selected = number is not None and number in wanted
        session.add(
            TorrentFile(
                torrent_id=torrent.id,
                file_index=info.index,
                path=info.name,
                size=info.size,
                episode_id=None if number is None else ids.get(number),
                wanted=selected,
                priority=FILE_ON if selected else FILE_OFF,
            )
        )
    await session.flush()


# --- What becomes of a pack ------------------------------------------------


class Disposition(StrEnum):
    """What should happen to a batch now that its selection has changed."""

    #: Something in it is still wanted. Leave it running.
    KEEP = "keep"
    #: Nothing is wanted now, but the pack may serve this show again.
    STOP = "stop"
    #: Nothing here will ever be wanted. Remove it with its files.
    DELETE = "delete"


async def disposition(session: AsyncSession, torrent: Torrent) -> Disposition:
    """Keep, stop or delete this pack (FR-A11). One decision, three callers.

    ``qbit_reselect`` is the only place a batch's run state changes after it was
    added, and it is called from a want withdrawn, a want attached, retention
    taking a file and a rejected member. Each of those knows why *its* file went
    away and none of them knows what the others left behind, so the question
    "is this pack still worth holding?" is answered here or it is answered three
    times differently.

    **KEEP** while any file is wanted: bytes are owed to somebody.

    **STOP** is the interesting one, and it is what makes a pack an asset rather
    than a liability. Nothing in it is wanted *now*, but a file it holds belongs
    to an episode of a show somebody still has ``watching``, ``planned`` or
    ``on_hold`` (:data:`LIVE_STATUSES`) — so the next episode of that show is
    very likely to want a file that is already selected-and-off in a torrent the
    client already holds, and :func:`claim_existing` will serve it for zero Nyaa
    requests. A stopped torrent costs nothing but the row; deleting it would cost
    the next episode a fourteen-gigabyte metadata fetch and a fresh pick. An
    ``on_hold`` entry is included on the owner's call (2026-09-18) even though it
    generates no wants: a paused viewer comes back, and nothing about a stopped
    pack is on the disk except the files it has already fetched, which are
    retention's to delete per episode (FR-T1).

    **A pack that has handed the library a file is never deleted either**, and
    this one is a safety rule rather than a judgement. ``DELETE`` means
    ``deleteFiles=true``, and a row with a ``completed_at`` says those bytes
    were given to an episode — which may be ``ready`` and playing, or sitting in
    somebody's review queue after a member was rejected (``reject`` clears the
    row's episode but not its ``completed_at``, precisely so that this stays
    true). Deleting the pack would leave a ``media_files`` row pointing at
    nothing. So it is stopped and kept, and those bytes are retention's to
    remove per episode (FR-T1) — the same argument ``_poll_batch``'s stall makes
    with the same words, because it is the same argument.

    **DELETE** when none of that holds: every list entry for every show the pack
    holds is ``completed``, ``dropped`` or gone, or no file in it maps to an
    episode at all (a pack whose contents the parser could make nothing of, or
    whose episodes have since been deleted from the catalogue —
    ``torrent_files.episode_id`` is ``SET NULL``). Then the torrent goes with its
    files, and the ``torrents`` row with it, because nothing is left to remember:
    unlike a stalled or rejected release there is nothing wrong with this pack,
    so barring it from a future pick would be wrong (``qbit_cancel``'s own
    reasoning about deleting a cancelled row).

    The rows, then one question about the lists. Nothing here talks to the
    client, and the answer is a value rather than an action, so the tests that
    pin the rule are the rule.
    """
    rows = list(
        (
            await session.scalars(select(TorrentFile).where(TorrentFile.torrent_id == torrent.id))
        ).all()
    )
    if any(row.wanted for row in rows):
        return Disposition.KEEP
    if any(row.completed_at is not None for row in rows):
        # The library has bytes out of this pack. Whatever the lists say, they
        # are not this function's to delete (see above).
        return Disposition.STOP

    held = [row.episode_id for row in rows if row.episode_id is not None]
    if not held:
        return Disposition.DELETE

    live = await session.scalar(
        select(ListEntry.user_id)
        .join(Episode, Episode.anime_id == ListEntry.anime_id)
        .where(Episode.id.in_(held), ListEntry.status.in_(LIVE_STATUSES))
        .limit(1)
    )
    return Disposition.STOP if live is not None else Disposition.DELETE


__all__ = [
    "ATTACHED",
    "EPISODE_KINDS",
    "LIVE_STATUSES",
    "UNATTACHABLE_STATES",
    "UNREADABLE_BATCH",
    "BatchTaken",
    "Disposition",
    "FilePlan",
    "claim_existing",
    "disposition",
    "listing_mismatch",
    "mark_unreadable",
    "plan_files",
    "plan_offset",
    "record_files",
    "reserve_batch",
    "size_label",
    "start_downloading",
    "targets",
    "verify_selection",
]
