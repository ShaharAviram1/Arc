"""The acquisition handlers: wants, search, poll, cancel, policy (architecture.md §5.1).

``compute_wants`` decides *what*; ``search_release`` decides *which release*
and starts it; ``poll_qbit`` watches it finish and hands the file to the
library. Between them they cover FR-A1 through FR-A6. The other two are
housekeeping alongside them: ``qbit_apply_policy`` writes Arc's no-seeding,
capped-upload, bounded-queue policy to the client (spec §9) and ``poll_qbit``
stops anything that got past it, and ``qbit_cancel`` carries out the removal
the reconciler decided on when the last want on a download went away.

All of them are idempotent, and each is idempotent for a different reason.
``compute_wants`` reconciles a whole table against the lists, so a second run
finds nothing to do. ``search_release`` refuses to act on an episode that is
not ``wanted`` or ``searching``, and qBittorrent answers ``Ok.`` to a magnet it
already holds. ``poll_qbit`` reads the client's live state and writes what it
says, and the ``media_files.path`` unique constraint is what keeps a
re-delivered file from being indexed twice. ``qbit_cancel`` deletes by hash,
and a hash the client no longer holds is dropped by the category check.
(``qbit_apply_policy`` writes the same fixed values every time, which is
idempotence for free.)

**A torrent that is going nowhere is given up on** (:func:`stall_reason`).
``poll_qbit`` used to react only to a torrent that had *vanished* from the
client, so a dead magnet — ``metaDL`` with no seeders, which is what a 2018
upload looks like — held a download slot until somebody noticed. Now it is
removed with its files, the row is marked ``stalled``, and the episode goes
``unavailable`` onto FR-A6's ordinary retry schedule. A release Arc has tried
is never chosen again (:func:`_pick`), so the retry looks for something else.
The clock is the client's own ``time_active`` rather than the row's age,
because Arc now bounds the client's queue: a torrent can wait a day in
``queuedDL`` and its first active minute must not look like a day of failure.

**The retry schedule (FR-A6) lives in the job payload, not in a column.**
A search that finds nothing requeues *itself* with a delay and carries
``attempts_started_at`` forward, so "how long have we been looking for this?"
is answered by the job that is doing the looking. The alternative — a column on
``episodes`` — would be a migration for a number only this handler reads, and
it would have to be cleared by everything else that touches the row. A search
that arrives with no such value — the daily revival of an episode that already
gave up — reads it off the last search this episode had instead, so the
fortnight is cumulative rather than restarted every morning (:func:`_started_at`).

**The pause switch stops the two that fetch.** ``acquisition_paused`` in
``settings`` (:func:`arc.services.acquisition.rules.is_paused`) makes
``compute_wants`` a no-op and turns ``search_release`` into "put me back on the
queue in fifteen minutes". ``poll_qbit`` keeps running: pausing means *stop
fetching more*, not *abandon the download that is already 80 % of the way in*,
and a torrent that finishes during a pause still reaches the library and still
becomes something to watch.

**The storage guard stops the same two, and lifts itself** (FR-T6,
2026-09-13). While free space on the data volume is under ``min_free_gb``
(:func:`arc.services.acquisition.rules.is_storage_held`) ``compute_wants``
still reconciles — dropping, shelving and cancelling all *free* space — but
starts no search, and ``search_release`` requeues itself on the same
:data:`PAUSED_RETRY` it uses while paused, with a log line naming the disk
rather than the switch. ``poll_qbit`` and the transcodes are untouched:
finishing what has already landed is how the source becomes deletable. Nobody
has to resume it; the next tick after retention frees room starts fetching
again.

**A finished show with nothing but batches takes one, file by file** (FR-A4's
exception, FR-A11, owner 2026-09-18). Where ``search_release`` ends with no
acceptable single at all and the show has finished airing, ``Search.batches``
holds the packs that cover the episode and :func:`_take_batch` runs the
eight-call sequence that makes one of them safe: the ``.torrent`` is fetched
from Nyaa, added **stopped**, every file is turned off, the identified ones are
turned back on, the selection is read back and verified, and only then is the
torrent started. Not one byte of an unwanted file can ever be fetched, because
there is no instant at which one is selected and the torrent is running. A
failure or a refusal anywhere before the start deletes the pack while it holds
nothing, and with every candidate refused the episode takes FR-A6's ordinary
retry with :data:`~arc.services.acquisition.batch.UNREADABLE_BATCH` as its
reason. ``batch_fallback`` in ``settings`` is the switch: off, and this handler
is byte-identical to what it was before the feature existed.

**A pack's episodes finish one file at a time.** ``poll_qbit`` keeps its
original query — every row that names an episode, which is every single — and
adds a second loop for the batch rows (:func:`_poll_batch`), both reading the
same one ``torrents/info`` answer. There an episode is complete when **its**
file is, so the per-file progress comes from ``torrents/files`` (one extra
request per *in-flight* pack, and none at all for a settled one) and each file
that arrives takes the ordinary hand-off for its own episode, with its own path
rather than :func:`largest_video` — which inside a season pack is another
episode. A pack whose every asked-for file is in is **stopped and kept**, never
deleted: the next episode of that show attaches to it for nothing.
``qbit_reselect`` is where a pack's selection changes after the pick and the one
place a pack is ever removed, through
:func:`~arc.services.acquisition.batch.disposition`.

**qBittorrent being down is not "unavailable".** FR-A6's ``unavailable`` means
*no acceptable release exists*, which is a statement about Nyaa. A client that
cannot be reached raises, the runner retries with backoff, and the episode
stays ``searching`` — which is what the show page then says (FR-A7).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import cast

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    MediaFile,
    Torrent,
    TorrentFile,
    TorrentKind,
    Want,
)
from arc.services.acquisition import batch, claims
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    POLL_QBIT,
    QBIT_CANCEL,
    QBIT_POLICY,
    QBIT_RESELECT,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
    search_dedupe_key,
)
from arc.services.acquisition.nyaa import NyaaUnavailable, Ranked, search_for_episode
from arc.services.acquisition.qbit import (
    DECIDED_STATES,
    DELETE_ON_SIGHT,
    FILE_OFF,
    FILE_ON,
    MAX_TORRENT_FILES,
    QBIT_MISSING,
    QBIT_STALLED,
    QBIT_UNREADABLE,
    SEEDING_STATES,
    FileInfo,
    QbitClient,
    QbitError,
    QbitUnavailable,
    TorrentInfo,
    batch_save_path_for,
    host_path,
)
from arc.services.acquisition.rules import (
    batch_fallback,
    is_paused,
    is_storage_held,
    load_rules,
)
from arc.services.acquisition.states import transition
from arc.services.acquisition.wants import NOBODY_WANTS, QBIT_CANCELLED
from arc.services.acquisition.wants import compute_wants as reconcile_wants
from arc.services.jobs.queue import enqueue, find_active
from arc.services.jobs.registry import JobContext, register
from arc.services.library import ingest
from arc.services.library.names import MATCH_FILE, match_dedupe_key

log = logging.getLogger(__name__)

#: How often to look again on the day an episode aired (FR-A6). A release
#: usually appears within an hour of the broadcast, and half-hourly is the
#: difference between "ready when I sit down" and "ready tomorrow".
AIR_DAY_RETRY = timedelta(minutes=30)

#: And afterwards. Six hours: nothing that has not appeared within a day of
#: airing appears in a hurry.
LATER_RETRY = timedelta(hours=6)

#: How recently an episode must have aired to count as "air day" (FR-A6).
AIR_DAY_WINDOW = timedelta(hours=24)

#: How long the search keeps trying before the episode is flagged
#: ``unavailable`` (FR-A6).
GIVE_UP_AFTER = timedelta(days=14)

#: The sentence a user is shown when it does (FR-A7).
NO_RELEASE = "no acceptable release found"

#: And when the client forgot about a download Arc had started.
REMOVED_FROM_CLIENT = "removed from the torrent client"

#: How long a torrent may ask the swarm for its metadata before it is a stall
#: (the default; ``STALL_METADATA_MINUTES`` overrides it). A magnet with any
#: seeders at all answers in seconds.
STALL_METADATA_AFTER = timedelta(minutes=60)

#: And how long a torrent that has its metadata may fetch nothing (the
#: default; ``STALL_NO_BYTES_HOURS`` overrides it).
STALL_NO_BYTES_AFTER = timedelta(hours=6)

#: The three sentences a stalled episode is shown (FR-A6, FR-A7). They name the
#: threshold rather than the state, because "no seeders after 6 hours" tells a
#: user both what happened and that Arc is going to try again — and they name
#: the *right* one of the three: "no seeders" is a statement about the swarm and
#: is only made when the tracker actually said so, while a torrent that has
#: fetched nothing from a swarm that does exist gets "no bytes".
NO_METADATA = "no metadata after {age}"
NO_SEEDERS = "no seeders after {age}"
NO_BYTES = "no bytes after {age}"

#: The two sentences for a download the *client* has given up on, rather than
#: one that is merely going nowhere. Both are shown to a user (FR-A7) and both
#: take FR-A6's ordinary retry, because a different release may work where this
#: one did not.
CLIENT_ERROR = "the torrent client reported an error"
FILES_GONE = "the downloaded files are missing"

#: The states a torrent is in while qBittorrent is asking the swarm for its
#: metadata: a magnet that has not become a torrent yet.
METADATA_STATES: frozenset[str] = frozenset({"metaDL", "forcedMetaDL"})

#: And the two the client reports when it has stopped trying: ``error`` (it
#: could not write, could not read, or the tracker rejected it) and
#: ``missingFiles`` (the data it was seeding or checking is not on the disk any
#: more). Neither is a matter of waiting — no threshold applies — and neither
#: was a stall before today, which was a gap for singles and became a hole with
#: batches (2026-09-18): retention unlinking one episode's file out of a
#: *running* pack is exactly how a torrent reaches ``missingFiles``, and every
#: other episode in that pack would otherwise sit ``downloading`` for ever
#: against a client that had stopped.
ERROR_STATES: frozenset[str] = frozenset({"error", "missingFiles"})

#: And the states that mean "the client is trying to download this **now**".
#: An allow-list, deliberately: most of the other states are ones where making
#: no progress is correct. ``queuedDL`` is a torrent waiting its turn behind
#: :attr:`~arc.config.Settings.qbit_max_active_downloads` — with four hundred
#: wants that is most of them, for hours — and ``stoppedDL``/``pausedDL`` is
#: somebody's own decision (production has 297 torrents deliberately stopped).
#: Checking, moving and allocating are busy, and an errored torrent is a
#: different problem. None of those is a stall, and a deny-list would have to
#: be right about every state qBittorrent ever adds.
ACTIVE_DL_STATES: frozenset[str] = frozenset({"downloading", "forcedDL", "stalledDL"})

#: And the states a torrent **Arc has stopped** comes back as: 5.x's two names
#: and 4.x's two. The third allow-list in this module and the only one that is
#: about a batch (FR-A11): a pack whose every wanted file is in is stopped and
#: kept, and "already stopped, with nothing left to fetch" is the condition
#: :func:`_poll_batch` reads to make **no** ``torrents/files`` request at all. A
#: settled pack therefore costs one row of the listing Arc was fetching anyway,
#: for as long as it sits there waiting to serve the next episode.
SETTLED_STATES: frozenset[str] = frozenset({"stoppedUP", "pausedUP", "stoppedDL", "pausedDL"})

#: Payload key holding when the *first* search for this episode ran.
STARTED_KEY = "attempts_started_at"

#: How long a search waits before looking again while acquisition is paused.
#: A quarter of an hour, matching the ``compute_wants`` tick: a resumed Arc is
#: doing its own reconciliation on that period anyway, so a pending search
#: coming back sooner would only occupy a slot to discover it is still paused.
PAUSED_RETRY = timedelta(minutes=15)

#: Episode states ``poll_qbit`` will run the completion hand-off from. Both,
#: so that an episode which reached ``downloaded`` before its file was
#: readable is retried on the next poll rather than stranded there.
COMPLETABLE: frozenset[EpisodeState] = frozenset(
    {EpisodeState.DOWNLOADING, EpisodeState.DOWNLOADED}
)

#: The tags a batch is filed under in the client (FR-A11). Deliberately no
#: ``episode:`` tag, unlike a single's: a batch belongs to no single episode,
#: and a tag naming one would be the same lie the null ``episode_id`` exists to
#: avoid. The ``arc`` tag and the category are what mark it as Arc's.
BATCH_TAGS = "arc,batch"

#: States ``search_release`` will act on. Anything else means the episode has
#: moved on — the file is downloading, or somebody dropped the show between
#: the enqueue and the claim — and the job is a no-op.
SEARCHABLE: frozenset[EpisodeState] = frozenset({EpisodeState.WANTED, EpisodeState.SEARCHING})


@dataclass(frozen=True, slots=True)
class Handoff:
    """What ``poll_qbit`` found when a torrent finished."""

    path: Path
    media_file_id: int
    created: bool


def _parse_started(raw: object) -> datetime | None:
    """One ``attempts_started_at`` value out of a job payload."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _started_at(ctx: JobContext, episode_id: int, *, now: datetime) -> datetime:
    """When the search for this episode *first* ran (FR-A6's 14-day window).

    The payload first: a retry carries the value forward, so a chain of
    retries all share the moment the first of them ran.

    A revival does not. When an episode gives up and goes ``unavailable``, the
    daily retry (:data:`~arc.services.acquisition.wants.UNAVAILABLE_RETRY`) is
    what keeps looking, and each revival queues a *fresh* ``search_release``
    with nothing but an episode id — so read it off the last search this
    episode had, whatever became of that job. Without this the fortnight
    restarts every day and the give-up never happens again; with it the window
    is cumulative per acquisition attempt, an episode that has been sought for
    a month says so in the log, and the daily retry (which is the policy)
    keeps running for exactly as long as somebody still wants it.
    """
    from_payload = _parse_started(ctx.payload.get(STARTED_KEY))
    if from_payload is not None:
        return from_payload

    started = cast(ColumnElement[str], Job.payload[STARTED_KEY].astext)
    previous = await ctx.session.scalar(
        select(started)
        .where(
            Job.type == SEARCH_RELEASE,
            Job.id != ctx.job.id,
            cast(ColumnElement[str], Job.payload["episode_id"].astext) == str(episode_id),
            started.is_not(None),
        )
        .order_by(Job.id.desc())
        .limit(1)
    )
    earlier = _parse_started(previous)
    if earlier is None:
        return now
    ctx.log.info(
        "this episode has been searched for before",
        extra={"episode_id": episode_id, "searching_since": earlier.isoformat()},
    )
    return earlier


def retry_delay(episode: Episode, *, now: datetime) -> timedelta:
    """Half an hour on air day, six hours after that (FR-A6)."""
    if episode.air_at is not None and timedelta(0) <= now - episode.air_at <= AIR_DAY_WINDOW:
        return AIR_DAY_RETRY
    return LATER_RETRY


async def _has_live_want(session: AsyncSession, episode_id: int) -> bool:
    found = await session.scalar(
        select(Want.user_id)
        .where(Want.episode_id == episode_id, Want.dropped_at.is_(None))
        .limit(1)
    )
    return found is not None


@register(COMPUTE_WANTS)
async def compute_wants(ctx: JobContext) -> None:
    """Recompute every user's acquisition window (FR-A1, FR-A2, FR-W4, FR-A9).

    ``ctx.settings`` goes through so the reconciler can measure free space and
    hold the *starting* half while the disk is under the floor (FR-T6). It is
    the only caller that can: everything else enqueues this job.
    """
    result = await reconcile_wants(ctx.session, settings=ctx.settings)
    ctx.log.info("wants computed", extra={"job_id": ctx.job.id, **result.as_dict()})


async def _pick(ctx: JobContext, episode: Episode, ranked: list[Ranked]) -> Ranked | None:
    """The best-ranked release this episode may actually claim.

    **A release Arc has already tried is never tried again**, whichever episode
    tried it and whatever became of it. Two reasons, and they arrive from
    opposite directions.

    ``torrents.info_hash`` is unique, and one hash is one download in
    qBittorrent saved under one episode's directory. A release whose hash
    already belongs to *another* episode cannot be taken: the row would keep
    pointing at the first episode, the save path would be the first episode's,
    and this episode would end up with a torrent it does not own and a file it
    never sees. That is what a batch offered as a single episode looks like.

    And a hash belonging to *this* episode is an attempt that has already
    happened and did not work — it stalled, a person rejected the file, it was
    cancelled, or it was removed from the client — because the whole of
    ``search_release`` is one transaction: a run that crashed before its commit
    left no row at all. Choosing it again would delete the files, re-add the
    same dead magnet and wait another six hours for the same answer. The next
    candidate down is usually the same episode from another group, and an
    episode with nothing left to try follows the ordinary "no release yet"
    path (FR-A6) — which is how it gets a fresh look tomorrow, by which time
    Nyaa may have something new.
    """
    for entry in ranked:
        owner = await ctx.session.scalar(
            select(Torrent.episode_id).where(Torrent.info_hash == entry.item.info_hash)
        )
        if owner is not None:
            ctx.log.info(
                "skipping a release arc has already tried",
                extra={
                    "episode_id": episode.id,
                    "hash": entry.item.info_hash,
                    "held_by_episode_id": owner,
                    "title": entry.item.title,
                },
            )
            continue
        if entry.dubbed:
            # The ranker puts every dub behind every subbed candidate, so
            # reaching one here means there was no subbed release at all — or
            # every one of them has already been tried. Worth a line of its
            # own: a dub is a file somebody can watch and the right answer
            # when there is nothing else, and it is also the first thing to
            # look at when a user says the audio is wrong (FR-A3).
            ctx.log.info(
                "choosing an english dub for lack of anything else",
                extra={
                    "episode_id": episode.id,
                    "title": entry.item.title,
                    "group": entry.candidate.group,
                    "candidates": len(ranked),
                },
            )
        return entry
    return None


async def _prequel_offset(session: AsyncSession, anime: Anime) -> int | None:
    """How many episodes ran before this entry, from the cached rows (FR-A4).

    The database half of :func:`~arc.services.acquisition.nyaa.absolute_offset`
    and the only reason it needs one: a relation blob carries external ids and
    a title, never an episode count, so the count has to be read off the
    prequel's own ``anime`` row. A prequel Arc has not cached is what makes the
    whole rule decline — the walk is written that way on purpose — so a miss
    here is an ordinary answer rather than a failure.

    AniList id first, MAL id second, and never both in one ``OR``: two ids that
    disagree would fetch whichever row the planner reached first, which is
    exactly the kind of "nearly right" answer an offset must not be built on.

    **Nothing here may fail a search.** ``relations`` is JSONB written by
    whichever source answered, and a blob carrying ``"anilist_id": "n/a"`` is a
    malformed row rather than an emergency: an entry Arc cannot compute an
    offset for is the ordinary, documented outcome, and letting a ``ValueError``
    out of it would put the *episode* back on the retry schedule over a field
    the search never needed. So an id that is not a number is skipped without a
    query being issued, and anything else that goes wrong is caught once, logged
    at ``WARNING`` with the entry on it, and answered ``None``.
    """

    def _id(value: object) -> int | None:
        """One external id off a relation blob, or ``None`` if it is not one."""
        if isinstance(value, bool) or not isinstance(value, int | str):
            return None
        try:
            return int(value)
        except ValueError:
            return None

    async def resolve(anilist_id: int | None, mal_id: int | None) -> Anime | None:
        row: Anime | None = None
        anilist = _id(anilist_id)
        if anilist is not None:
            row = await session.scalar(select(Anime).where(Anime.anilist_id == anilist))
        mal = _id(mal_id)
        if row is None and mal is not None:
            row = await session.scalar(select(Anime).where(Anime.mal_id == mal))
        return row

    try:
        return await nyaa_module.absolute_offset(anime, resolve)
    except Exception:  # noqa: BLE001 - a bad relation blob is not a failed search
        log.warning(
            "could not work out an absolute numbering offset",
            extra={"anime_id": anime.id},
            exc_info=True,
        )
        return None


async def _record_torrent(session: AsyncSession, episode: Episode, chosen: Ranked) -> Torrent:
    """The ``torrents`` row for the chosen release, reused if it exists.

    ``info_hash`` is unique, and :func:`_pick` passes over every hash that
    already has a row — so in the ordinary case there is nothing here to reuse
    and this inserts. The lookup stays for one race: two searches for two
    episodes can rank the same release, and the other one's row can be
    committed in the moment between this episode's :func:`_pick` and this
    query. A row that belongs to *this* episode is taken over (it cannot
    happen through ``_pick``, and taking it is right if it ever does); a row
    that belongs to another episode raises, so the job retries and the next
    attempt's ``_pick`` sees the row and chooses something else — which is a
    better ending than this episode going ``downloading`` against a torrent
    filed under somebody else's id.
    """
    item = chosen.item
    torrent = await session.scalar(select(Torrent).where(Torrent.info_hash == item.info_hash))
    if torrent is None:
        torrent = Torrent(episode_id=episode.id, info_hash=item.info_hash)
        session.add(torrent)
    elif torrent.episode_id != episode.id:
        raise RuntimeError(
            f"{item.info_hash} was taken for episode {torrent.episode_id} while episode "
            f"{episode.id} was choosing it"
        )
    torrent.magnet = item.magnet
    torrent.title = item.title
    torrent.group = chosen.candidate.group
    torrent.resolution = chosen.candidate.resolution
    torrent.seeders = item.seeders
    torrent.trusted = item.trusted
    await session.flush()
    return torrent


async def _schedule_retry(
    ctx: JobContext, episode: Episode, *, now: datetime, reason: str = NO_RELEASE
) -> None:
    """Requeue this search, or give up and flag the episode (FR-A6).

    ``reason`` is the sentence the show page shows once the fortnight is up
    (FR-A7), and there are two of them. :data:`NO_RELEASE` is the ordinary one.
    :data:`~arc.services.acquisition.batch.UNREADABLE_BATCH` is what a finished
    show gets when packs *were* offered and none of their files could be
    identified — a different thing to be told, and the only one of the two a
    user could act on.
    """
    started = await _started_at(ctx, episode.id, now=now)
    if now - started >= GIVE_UP_AFTER:
        transition(episode, EpisodeState.UNAVAILABLE, reason=reason)
        ctx.log.warning(
            "giving up on an episode",
            extra={
                "episode_id": episode.id,
                "searching_since": started.isoformat(),
                "reason": reason,
            },
        )
        return

    delay = retry_delay(episode, now=now)
    key = search_dedupe_key(episode.id)
    # ``exclude_job_id``: this job is *running* under the very key it is about
    # to queue under, so the ordinary dedupe would find the caller and the
    # retry would be silently dropped. Any *other* row under the key is a real
    # dedupe hit and this run has nothing to add.
    queued = await enqueue(
        ctx.session,
        SEARCH_RELEASE,
        {"episode_id": episode.id, STARTED_KEY: started.isoformat()},
        priority=SEARCH_RELEASE_PRIORITY,
        run_after=now + delay,
        dedupe_key=key,
        exclude_job_id=ctx.job.id,
    )
    if queued.payload.get(STARTED_KEY) != started.isoformat():
        ctx.log.info(
            "a search is already queued for this episode", extra={"episode_id": episode.id}
        )
        return
    ctx.log.info(
        "no release yet, retrying later",
        extra={
            "episode_id": episode.id,
            "reason": reason,
            "retry_in_s": int(delay.total_seconds()),
            "searching_since": started.isoformat(),
            "give_up_at": (started + GIVE_UP_AFTER).isoformat(),
        },
    )


#: The two lines :func:`_requeue_paused` can log. One function because the
#: behaviour is identical — requeue, touch nothing, ask Nyaa nothing — and the
#: only thing an operator needs from the log is *which* brake is on, since one
#: of them they pressed and the other lifts itself (FR-T6).
PAUSED_LOG = "acquisition paused; search requeued without touching nyaa"
HELD_LOG = "acquisition held by free space; search requeued without touching nyaa"


async def _requeue_paused(
    ctx: JobContext, episode_id: int, *, now: datetime, message: str = PAUSED_LOG
) -> None:
    """Put this search back on the queue, unchanged, for a quarter of an hour.

    Requeued rather than failed or dropped: a pause is temporary, and the job
    row *is* the record that this episode is still owed a search. Nothing about
    the episode is touched — no state change, no ``torrents`` row, no request
    to Nyaa — so a resume finds exactly the world the pause left behind. A
    storage hold (FR-T6) is the same shape with a different cause, so it is the
    same function with ``message`` naming the disk instead of the switch.

    ``attempts_started_at`` rides along when the payload has one, so a pause in
    the middle of FR-A6's fortnight does not restart it. The 14-day clock does
    keep running while paused, which is the honest reading of "we have been
    unable to get this episode since": an admin who pauses for a fortnight has
    genuinely not acquired it, and the daily revival
    (:data:`~arc.services.acquisition.wants.UNAVAILABLE_RETRY`) is what picks
    the search back up once acquisition is running again.

    ``exclude_job_id`` for the same reason :func:`_schedule_retry` passes it:
    this job is *running* under the very key it is queueing under.
    """
    payload: dict[str, object] = {"episode_id": episode_id}
    started = ctx.payload.get(STARTED_KEY)
    if isinstance(started, str):
        payload[STARTED_KEY] = started
    await enqueue(
        ctx.session,
        SEARCH_RELEASE,
        payload,
        priority=SEARCH_RELEASE_PRIORITY,
        run_after=now + PAUSED_RETRY,
        dedupe_key=search_dedupe_key(episode_id),
        exclude_job_id=ctx.job.id,
    )
    ctx.log.info(
        message,
        extra={
            "episode_id": episode_id,
            "retry_in_s": int(PAUSED_RETRY.total_seconds()),
        },
    )


async def _wanted_episodes(session: AsyncSession, anime_id: int) -> list[Episode]:
    """Every episode of this show somebody still wants, lowest number first.

    One query, two readers. The **numbers** order the batch candidates (FR-A11:
    of two packs that both hold the episode, the one that also holds the others
    somebody is waiting for wins), and the **rows** are what a chosen pack
    attaches — so asking twice would be asking the same question of the same
    table a second later, with the answers free to disagree.
    """
    rows = await session.scalars(
        select(Episode)
        .join(Want, Want.episode_id == Episode.id)
        .where(Episode.anime_id == anime_id, Want.dropped_at.is_(None))
        .distinct()
        .order_by(Episode.number)
    )
    return list(rows.all())


async def _attachable(session: AsyncSession, episodes: Sequence[Episode]) -> dict[int, Episode]:
    """``number → episode`` for the ones a batch may be taken for as free riders.

    Two conditions, and each of them is a row this must not write twice.

    **``wanted`` only, not ``searching``.** An episode that is already
    ``searching`` has a ``search_release`` of its own, quite possibly running on
    the other worker slot this instant — and that job may be about to add a
    *single* for it. Attaching it here as a free rider would leave it with a
    single and a batch claim at once, two downloads of one episode and a
    ``torrent_files`` row nobody will ever clear. An episode that is merely
    ``wanted`` has no job in flight that can do that: the search it is owed is
    still on the queue, and it will find this batch through
    :func:`~arc.services.acquisition.batch.claim_existing` for nothing when it
    runs. The episode this job is *for* is added back by the caller, because it
    is `searching` by construction and it is the one episode whose search this
    demonstrably is.

    And it must not already hold a live claim on some other batch's file, which
    is an invariant the database enforces
    (``ux_torrent_files_one_wanted_per_episode``) and therefore one worth not
    walking into: a unique-index violation here would fail the search for the
    episode that *was* being looked for.
    """
    free = [episode for episode in episodes if episode.state is EpisodeState.WANTED]
    if not free:
        return {}
    claimed = set(
        (
            await session.scalars(
                select(TorrentFile.episode_id).where(
                    TorrentFile.episode_id.in_([episode.id for episode in free]),
                    TorrentFile.wanted.is_(True),
                )
            )
        ).all()
    )
    return {episode.number: episode for episode in free if episode.id not in claimed}


async def _abandon(
    ctx: JobContext,
    qbit: QbitClient,
    torrent: Torrent,
    reason: str,
    *,
    permanent: bool,
) -> None:
    """Delete a batch that was added and then refused, with its files.

    It is still **stopped** when this runs, so there are no files and no bytes
    to speak of — the delete is what stops an empty directory and a torrent
    nobody will ever start from accumulating in the client.

    What becomes of the reserved row is the whole of the decision here, and it
    turns on whether the answer could be different tomorrow.

    ``permanent`` — the files could not be identified, or there were more of
    them than Arc will read a selection out of — keeps the row as a
    :data:`~arc.services.acquisition.qbit.QBIT_UNREADABLE` tombstone, because
    the pick skips any hash that already has one. Without it this same pack is
    fetched, added, read and deleted again in six hours, and again six hours
    after that, for every episode of the show, for a fortnight.

    Anything else — a read-back the client disagreed with, a listing that came
    back empty — **deletes** the row. Those are statements about the client's
    behaviour this minute rather than about the pack, and barring the release
    for ever over one would be the same mistake as barring a cancelled
    download's release (``qbit_cancel``'s own reasoning).
    """
    await qbit.delete([torrent.info_hash], delete_files=True)
    if permanent:
        torrent.qbit_state = QBIT_UNREADABLE
    else:
        await ctx.session.delete(torrent)
    await ctx.session.flush()
    ctx.log.warning(
        "a batch was refused and deleted before anything was fetched",
        extra={"hash": torrent.info_hash, "reason": reason, "remembered": permanent},
    )


@dataclass(frozen=True, slots=True)
class BatchAttempt:
    """What :func:`_take_batch` did, for the retry to read.

    ``refused`` is the count that decides which sentence the episode ends with
    (FR-A7). A candidate Arc **read and could not use** is worth telling a user
    about: "there were only packs and I could not read them" is a different and
    more actionable fact than "nothing was found". A candidate that was merely
    *skipped* — a hash that already has a row, a ``.torrent`` Nyaa would not
    hand over — is not: nothing was read, so the ordinary no-release sentence is
    the honest one.
    """

    started: bool
    refused: int


async def _take_batch(
    ctx: JobContext,
    episode: Episode,
    anime: Anime,
    batches: list[Ranked],
    *,
    attachable: Mapping[int, Episode],
    offset: int | None,
) -> BatchAttempt:
    """Take the best batch whose files Arc can read (FR-A4's exception, FR-A11).

    The calls of architecture.md §5.1a, in this order and for these reasons:

    1. ``NyaaClient.torrent_file`` — the metadata is in hand *before* the
       client is told anything;
    2. ``torrents/add`` **multipart**, ``stopped``, under
       ``<downloads>/batch/<hash>`` — metadata present at add time is the only
       reason the selection can be written at all, and stopped means nothing
       moves. The client has to confirm the **hash**, not just the success:
       everything after this is keyed on a string that came out of a feed;
    3. :func:`~arc.services.acquisition.batch.reserve_batch` — the row, flushed,
       **before the client's selection is touched**. This is the step the
       concurrent-pick race turns on and the reason it is here rather than at
       the end (see that function);
    4. ``torrents/stop`` — belt and braces, and idempotent. ``add_file``
       reports success for a torrent the client already holds *in any run
       state*: a crash after the start with the row rolled back, an operator's
       own add of the same pack, or a build honouring neither ``stopped`` nor
       ``paused``. Each of those would otherwise download at full speed for the
       two round trips it takes to write the selection;
    5. ``torrents/files`` — the client's own indices, names and sizes, which
       are what ``filePrio`` takes and therefore the only authority here. A
       listing it cannot fully parse raises rather than coming back short;
    6. :func:`~arc.services.acquisition.batch.plan_files` — pure, and the one
       decision about *which* file is which episode;
    7. ``filePrio 0`` for **every** index. All files off first, so any instant
       between here and the next call leaves fewer files selected, never more;
    8. ``filePrio 1`` for the identified ones, and nothing else;
    9. ``torrents/files`` again, read back and verified
       (:func:`~arc.services.acquisition.batch.verify_selection`) — the gate the
       byte guarantee rests on, and free, because the torrent is still stopped;
    10. ``torrents/start``, and only now may a byte arrive.
        :func:`~arc.services.acquisition.batch.record_files` and the state
        changes land in this handler's own transaction afterwards, as they do
        for a single.

    Three endings other than success, and they differ in what they *remember*.
    A pack whose files could not be identified, or which has more files than
    Arc will read a selection out of, is deleted and its row kept as a
    tombstone — that answer cannot change, and without the row the same pack is
    fetched again every six hours for every episode of the show. A read-back
    the client disagreed with is deleted with its row, because that is a
    statement about the client this minute. And a Nyaa that would not hand over
    the ``.torrent`` leaves nothing at all, having added nothing.

    An **exception** from qBittorrent is left to propagate: the runner retries
    the whole search, ``add_file`` is idempotent for a torrent the client
    already holds, and a stopped torrent with no selection has fetched nothing
    in the meantime — which is a better shape than a cleanup path that needs the
    same unreachable client to work. A :class:`~arc.services.acquisition.batch.BatchTaken`
    propagates for the same reason and with a better ending: the retry's
    :func:`~arc.services.acquisition.batch.claim_existing` attaches to the
    winner's pack.

    A hash that already has a ``torrents`` row is skipped for the reason
    :func:`_pick` skips one: it was tried. A batch *in flight* is not reached
    through here at all but through
    :func:`~arc.services.acquisition.batch.claim_existing`, before Nyaa is
    asked.
    """
    # The process-wide client, as the search itself uses: the ``.torrent``
    # fetch is one more paced request and has to queue behind the searches.
    nyaa = nyaa_module.shared_client(ctx.settings.nyaa_url)
    refused = 0
    async with QbitClient.from_settings(ctx.settings) as qbit:
        for chosen in batches:
            info_hash = chosen.item.info_hash
            owner = await ctx.session.scalar(
                select(Torrent.id).where(Torrent.info_hash == info_hash)
            )
            if owner is not None:
                ctx.log.info(
                    "skipping a batch arc has already tried",
                    extra={
                        "episode_id": episode.id,
                        "hash": info_hash,
                        "title": chosen.item.title,
                    },
                )
                continue

            wanted_for = batch.targets(chosen, episode.number, attachable)
            try:
                blob = await nyaa.torrent_file(chosen.torrent_url)
            except NyaaUnavailable as exc:
                # Not an error for the episode, and nothing to remember: it is
                # one candidate Arc could not fetch *today*, and the next one
                # down is usually the same pack from another group.
                ctx.log.warning(
                    "could not fetch a batch's .torrent",
                    extra={
                        "episode_id": episode.id,
                        "url": chosen.torrent_url,
                        "error": str(exc),
                    },
                )
                continue

            save_path = batch_save_path_for(
                info_hash, downloads_path=ctx.settings.qbit_downloads_path
            )
            try:
                await qbit.add_file(
                    blob,
                    save_path=save_path,
                    info_hash=info_hash,
                    tags=BATCH_TAGS,
                    stopped=True,
                )
            except QbitUnavailable:
                raise
            except QbitError as exc:
                # The client did not end up holding the hash the feed
                # advertised, which is a fact about this feed item and will not
                # be different tomorrow.
                await batch.mark_unreadable(
                    ctx.session, ranked=chosen, info_hash=info_hash, reason=str(exc)
                )
                refused += 1
                continue

            torrent = await batch.reserve_batch(
                ctx.session, ranked=chosen, save_path=save_path, info_hash=info_hash
            )
            await qbit.stop([info_hash])

            try:
                listed = await qbit.files(info_hash)
            except QbitUnavailable:
                raise
            except QbitError as exc:
                # A listing the client could not describe in full (a file with
                # no usable name). A pack Arc cannot enumerate is a pack Arc
                # cannot turn every file of *off*, so it is never started — and
                # that is a fact about this torrent's contents, not about
                # today, so it is remembered.
                await _abandon(ctx, qbit, torrent, str(exc), permanent=True)
                refused += 1
                continue
            if not listed:
                # A client that has the metadata and lists no files has not
                # told Arc anything to act on. Transient, so no tombstone: the
                # next attempt may well get the list.
                await _abandon(ctx, qbit, torrent, "the client listed no files", permanent=False)
                continue
            if len(listed) > MAX_TORRENT_FILES:
                await _abandon(
                    ctx,
                    qbit,
                    torrent,
                    f"{len(listed)} files, over {MAX_TORRENT_FILES}",
                    permanent=True,
                )
                refused += 1
                continue

            plan = batch.plan_files(
                anime,
                wanted_for,
                listed,
                required=episode.number,
                offset=batch.plan_offset(chosen, offset),
            )
            if plan.refused_reason is not None:
                await _abandon(ctx, qbit, torrent, plan.refused_reason, permanent=True)
                refused += 1
                continue

            await qbit.file_priority(info_hash, plan.indices, FILE_OFF)
            await qbit.file_priority(info_hash, plan.wanted_indices, FILE_ON)
            try:
                written = await qbit.files(info_hash)
            except QbitUnavailable:
                raise
            except QbitError as exc:
                # The selection is written and the torrent is still stopped, so
                # a read-back Arc cannot read is the same answer as one it
                # disagrees with: take the pack away rather than start it.
                await _abandon(ctx, qbit, torrent, str(exc), permanent=False)
                continue
            disagreement = batch.verify_selection(listed, written, plan.wanted_indices)
            if disagreement is not None:
                await _abandon(ctx, qbit, torrent, disagreement, permanent=False)
                continue

            await qbit.start([info_hash])

            await batch.record_files(ctx.session, torrent, plan)
            for number in plan.wanted:
                target = attachable.get(number)
                if target is not None:
                    batch.start_downloading(target, f"downloading {chosen.item.title}")
            await ctx.session.flush()
            ctx.log.info(
                "batch chosen",
                extra={
                    "job_id": ctx.job.id,
                    "episode_id": episode.id,
                    "anime_id": anime.id,
                    "number": episode.number,
                    "torrent_id": torrent.id,
                    "savepath": save_path,
                    "files": len(plan.files),
                    "wanted": len(plan.wanted),
                    "episodes": list(plan.wanted),
                    "not_held": list(plan.dropped),
                    # Both figures, side by side and in human units: this one
                    # line is what proves FR-A4's spirit is intact.
                    "selected": (
                        f"{batch.size_label(plan.wanted_bytes)} of "
                        f"{batch.size_label(plan.total_size)}"
                    ),
                    "wanted_bytes": plan.wanted_bytes,
                    "total_size": plan.total_size,
                    "candidates": len(batches),
                    **nyaa_module.as_dict(chosen),
                },
            )
            return BatchAttempt(started=True, refused=refused)
    return BatchAttempt(started=False, refused=refused)


@register(SEARCH_RELEASE)
async def search_release(ctx: JobContext) -> None:
    """Find a release for one episode and start it downloading (FR-A3..A6).

    Every attempt that actually reaches Nyaa also stamps what it asked and saw
    on the episode — ``last_search_at``, ``last_search_forms``,
    ``last_search_results`` — which is what the show page's row reads (FR-A7).
    A paused or storage-held run stamps nothing, because it asked nothing.

    **This handler is allowed to be slow** (2026-09-18). A finished show's
    search asks its title forms and then, where those found too little, the
    group-narrowed forms
    (:func:`~arc.services.acquisition.nyaa.group_queries`), up to
    :data:`~arc.services.acquisition.nyaa.MAX_REQUESTS` requests in total and
    two seconds apart, so one attempt can take some forty seconds where it used
    to take twenty. Nothing in the queue reads that as stuck:
    ``WORKER_STALE_AFTER`` is two hours and bounds *silence* rather than work,
    the runner puts no timeout on a handler at all, and the worker's heartbeat
    is its scheduler's own thirty-second tick — which keeps ticking, because
    every wait in here is an ``await`` on the same event loop. What it does
    spend is one of ``WORKER_CONCURRENCY``'s two slots for that much longer,
    which is why the paging stops at the first pool worth ranking.
    """
    episode_id = int(ctx.payload["episode_id"])
    now = datetime.now(UTC)

    if await is_paused(ctx.session):
        # Before the episode is even loaded: while paused this handler must
        # read nothing it might act on and write nothing but its own retry.
        await _requeue_paused(ctx, episode_id, now=now)
        return
    if await is_storage_held(ctx.session, ctx.settings):
        # FR-T6, and for the same reason in the same place: a search that
        # cannot be allowed to fetch must not touch the episode either, or a
        # full disk would leave rows saying "looking for a release" on behalf
        # of a search that never ran.
        await _requeue_paused(ctx, episode_id, now=now, message=HELD_LOG)
        return

    episode = await ctx.session.get(Episode, episode_id)
    if episode is None:
        ctx.log.info("episode went away before it was searched", extra={"id": episode_id})
        return
    if episode.state not in SEARCHABLE:
        ctx.log.info(
            "episode is past searching",
            extra={"episode_id": episode_id, "state": episode.state.value},
        )
        return
    if not await _has_live_want(ctx.session, episode_id):
        # The last want went away while this was queued. Release the episode
        # here rather than leaving it to ``compute_wants``: this job is the one
        # holding it, and an episode left ``searching`` by a search that
        # returned is looked for by nobody and says so to no one (FR-A7).
        transition(episode, EpisodeState.NOT_WANTED, reason=NOBODY_WANTS)
        await ctx.session.flush()
        ctx.log.info("nobody wants this episode any more", extra={"episode_id": episode_id})
        return

    anime = await ctx.session.get(Anime, episode.anime_id)
    if anime is None:  # pragma: no cover - the foreign key forbids it
        raise RuntimeError(f"episode {episode_id} has no anime row")

    transition(episode, EpisodeState.SEARCHING, reason="looking for a release")
    await ctx.session.flush()

    # The kill switch, read once and used twice (FR-D2, FR-A11). It gates the
    # attach as well as the pick: enabling a file in a pack Arc already holds
    # is still fetching more, and "fetch no more of this" is what an admin who
    # turns it off means. What it does not stop is the episodes a pack already
    # has claims for — those bytes are in flight and stranding them is not what
    # a kill switch is for.
    fallback = await batch_fallback(ctx.session)
    if fallback:
        # Before Nyaa is asked anything at all (FR-A11): a batch Arc already
        # has may hold this episode's file, and turning that file on is the
        # whole of what this search would otherwise spend a dozen paced
        # requests finding out. Episode 11 of a pack taken for episode 10 costs
        # nothing.
        claimed = await batch.claim_existing(ctx.session, episode)
        if claimed is not None:
            ctx.log.info(
                "episode attached to a batch already in the client",
                extra={
                    "job_id": ctx.job.id,
                    "episode_id": episode.id,
                    "anime_id": anime.id,
                    "number": episode.number,
                    "torrent_id": claimed.torrent_id,
                    "path": claimed.path,
                },
            )
            return

    rules = await load_rules(ctx.session, anime.id)
    # The process-wide client, not one of this job's own: the pacing gap and
    # the ten-minute cache are only worth anything if every concurrent search
    # goes through the same instance. It is never closed here — it outlives
    # the job (:func:`arc.services.acquisition.nyaa.shared_client`).
    nyaa = nyaa_module.shared_client(ctx.settings.nyaa_url)
    # Absolute numbering (FR-A4, 2026-09-17), read off the catalogue before the
    # first request: it changes both what is asked for and what is accepted,
    # and ``None`` — an entry with no prequel chain Arc can add up — is the
    # ordinary answer and the behaviour every search had before it existed.
    offset = await _prequel_offset(ctx.session, anime)
    # Said out loud on every search, because "why did it ask for `- 25`?" and
    # "why did it *not*?" are the same question about this one number, and the
    # rule's ordinary answer is to decline (FR-A4).
    ctx.log.info(
        "absolute numbering applies" if offset else "absolute numbering does not apply",
        extra={
            "episode_id": episode.id,
            "anime_id": anime.id,
            "number": episode.number,
            "offset": offset,
        },
    )
    # The episodes a batch could actually be taken for, worked out once and
    # used twice (FR-A11): they order the batch candidates — a pack that also
    # holds the *others* is worth more than one that does not — and they are the
    # free riders a chosen pack selects. Deliberately the **attachable** ones
    # rather than every live want: ranking a pack higher for covering an episode
    # that is already downloading, or that has a search of its own in flight,
    # would be preferring it for something it will never be asked to do. Empty
    # is neutral, so nothing about a single's search changes.
    attachable = dict(await _attachable(ctx.session, await _wanted_episodes(ctx.session, anime.id)))
    # The episode this job is *for* is always one of them, whatever the query
    # above made of it: it is ``searching`` by now and its want was checked at
    # the top.
    attachable.setdefault(episode.number, episode)
    found = await search_for_episode(
        nyaa,
        anime,
        episode.number,
        rules,
        offset=offset,
        wanted_numbers=tuple(sorted(attachable)),
    )
    ranked = found.ranked
    # Every attempt, before anything is decided about it: the pair of numbers
    # is the diagnostic for a row that says ``Searching`` and nothing else
    # (FR-A7), and it is worth exactly as much on the attempt that fails as on
    # the one that succeeds. Written even when the episode then goes
    # ``unavailable``, which is where somebody is most likely to read it.
    episode.last_search_at = now
    episode.last_search_forms = found.forms
    episode.last_search_results = found.results
    await ctx.session.flush()
    # ``requests`` is the third number and the only one that is not on the row
    # (FR-A4, 2026-09-18): how many times Nyaa was actually asked, which is
    # more than ``forms`` exactly when a finished show had to fall back on the
    # group-narrowed forms. "7 forms, 13 requests" is the sentence that says
    # this search went looking, and it is the number to read if a search ever
    # feels slow, since every request is another two seconds of pacing.
    ctx.log.info(
        "nyaa search finished",
        extra={
            "episode_id": episode.id,
            "anime_id": anime.id,
            "number": episode.number,
            "forms": found.forms,
            "requests": found.requests,
            "results": found.results,
            "kept": found.kept,
        },
    )

    chosen = await _pick(ctx, episode, ranked) if ranked else None
    if chosen is None:
        # No single, and nothing left to try. A **finished** show may still
        # have a batch that covers the episode (FR-A4's exception, FR-A11);
        # ``Search.batches`` is empty for every airing show, every film and
        # every search that found a single, so this branch is unreachable for
        # any of them and the ordinary retry below is what they take.
        if found.batches and fallback:
            attempt = await _take_batch(
                ctx, episode, anime, found.batches, attachable=attachable, offset=offset
            )
            if attempt.started:
                return
            # Packs were offered and at least one was read and refused: a
            # different thing to be told than "nothing was found" (FR-A7).
            # Where every candidate was merely *skipped* — already tried, or a
            # ``.torrent`` Nyaa would not hand over — nothing was read, and the
            # ordinary sentence is the honest one.
            await _schedule_retry(
                ctx,
                episode,
                now=now,
                reason=batch.UNREADABLE_BATCH if attempt.refused else NO_RELEASE,
            )
            return
        if found.batches:
            ctx.log.info(
                "batch fallback is off; the batches on offer were not considered",
                extra={"episode_id": episode.id, "batches": len(found.batches)},
            )
        await _schedule_retry(ctx, episode, now=now)
        return

    torrent = await _record_torrent(ctx.session, episode, chosen)

    async with QbitClient.from_settings(ctx.settings) as qbit:
        save_path = await qbit.add(
            chosen.item.magnet, episode_id=episode.id, info_hash=chosen.item.info_hash
        )

    torrent.qbit_state = "added"
    torrent.progress = 0.0
    transition(episode, EpisodeState.DOWNLOADING, reason=f"downloading {chosen.item.title}")
    await ctx.session.flush()
    ctx.log.info(
        "release chosen",
        extra={
            "job_id": ctx.job.id,
            "episode_id": episode.id,
            "anime_id": anime.id,
            "number": episode.number,
            "savepath": save_path,
            "candidates": len(ranked),
            **nyaa_module.as_dict(chosen),
        },
    )


# --- poll_qbit --------------------------------------------------------------


def _plural(count: int, unit: str) -> str:
    """``6, "hour"`` → ``"6 hours"``, and ``1, "hour"`` → ``"1 hour"``."""
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def stall_reason(
    info: TorrentInfo,
    *,
    metadata_after: timedelta = STALL_METADATA_AFTER,
    no_bytes_after: timedelta = STALL_NO_BYTES_AFTER,
) -> str | None:
    """Why this torrent is never going to finish, or ``None`` if it might.

    Pure, so the table of states, times and swarm counts in the tests *is* the
    rule.

    **The clock is ``time_active``, not the row's age.** qBittorrent reports how
    long it has actually been *working on* a torrent, and that is the only
    figure this may measure against now that Arc bounds the client's queue: a
    torrent sits in ``queuedDL`` behind
    :attr:`~arc.config.Settings.qbit_max_active_downloads` for as long as it
    takes, then starts downloading already nine hours old with nothing fetched.
    Against ``torrents.added_at`` that is a stall on its first poll, and
    ``stalledDL`` — which is simply "no bytes this instant" — would have made
    it one; against ``time_active`` it is thirty seconds into its first
    attempt. A client that does not report the field stalls nothing.

    Three ways to fail, then:

    * still in :data:`METADATA_STATES` after ``metadata_after`` of *activity* —
      a magnet whose swarm never answered. This is what held production's three
      download slots for a whole day: 2018 uploads with nothing behind them,
      sitting in ``metaDL`` for ever because a magnet with no peers has nothing
      to time out against;
    * the tracker says the swarm is **empty** (:attr:`TorrentInfo.dead_swarm` —
      ``num_complete`` and ``num_incomplete`` both scraped and both zero)
      after ``no_bytes_after``, whatever the progress: a swarm that emptied at
      60 % is as final as one that was never there;
    * nothing fetched at all after ``no_bytes_after``.

    And one way to have failed already: :data:`ERROR_STATES` — ``error`` or
    ``missingFiles`` — which is the client saying it has stopped, not that it is
    making no progress. No threshold applies to those, because there is nothing
    to wait for; what makes them reachable at all is retention unlinking one
    episode's file out of a pack the client is still running (2026-09-18).

    Three things are **not** a stall, and each of them would be a bug:

    * a torrent that has finished — there is nothing left to wait for, whatever
      state the client has since put it in;
    * a state outside :data:`ACTIVE_DL_STATES` and :data:`ERROR_STATES` — queued
      behind the client's own download limit, stopped by a person, checking,
      moving;
    * anything with ``dlspeed`` above zero. Bytes are arriving *right now*,
      which settles the question whatever the history says.

    Note what is **not** consulted: ``num_seeds``/``num_leechs``, the peers
    this client happens to be connected to this instant. Those are reported as
    0 all the time on healthy torrents between announces, and reading them as
    an empty swarm would delete a 60 %-complete download.
    """
    if info.complete:
        return None
    if info.state in ERROR_STATES:
        # Before the clock and before the speed: neither says anything about a
        # client that has stopped, and a torrent whose files are gone can even
        # be reported with bytes still moving for the instant after.
        return FILES_GONE if info.state == "missingFiles" else CLIENT_ERROR
    if info.time_active is None or info.dlspeed > 0:
        return None
    active = timedelta(seconds=info.time_active)
    if info.state in METADATA_STATES:
        if active >= metadata_after:
            return NO_METADATA.format(
                age=_plural(int(metadata_after.total_seconds() // 60), "minute")
            )
        return None
    if info.state not in ACTIVE_DL_STATES or active < no_bytes_after:
        return None
    hours = _plural(int(no_bytes_after.total_seconds() // 3600), "hour")
    if info.dead_swarm:
        return NO_SEEDERS.format(age=hours)
    if info.progress <= 0.0:
        return NO_BYTES.format(age=hours)
    return None


def largest_video(root: Path, extensions: frozenset[str]) -> Path | None:
    """The biggest video file under ``root``, which is the episode.

    A release directory holds the episode plus, sometimes, a sample, a
    ``.nfo``, and cover art. "Biggest video" is the rule the whole scene has
    used for twenty years and it is right for the same reason it always was: a
    sample is a tenth the size, and there is only ever one episode.

    The walk is :func:`arc.services.library.ingest.walk`, the same one the
    library scan uses, so both agree about what is invisible: a ``.``/``@``
    directory is pruned rather than descended into. A NAS drops
    ``.Trash-1000`` and ``@eaDir`` next to the file it has just written, and a
    thumbnail of the episode inside one of those must never be mistaken for
    the episode.
    """
    best: tuple[int, Path] | None = None
    if not root.exists():
        return None
    candidates = [root] if root.is_file() else list(ingest.walk(root))
    for path in candidates:
        if not path.is_file():
            continue
        if path.suffix.lower().lstrip(".") not in extensions:
            continue
        if ingest.is_partial(path) or path.name.startswith("."):
            continue
        try:
            size = path.stat().st_size
        except OSError:  # pragma: no cover - a file that vanished mid-walk
            continue
        if best is None or size > best[0]:
            best = (size, path)
    return None if best is None else best[1]


async def _hand_off(
    session: AsyncSession,
    settings: Settings,
    path: Path,
    *,
    anime_id: int,
    number: int,
) -> Handoff:
    """Index the finished file and queue its match, with the prior (FR-L3).

    ``expected`` is the whole point of the hand-off: Arc knows which episode it
    asked for, and passing that belief to the matcher is what lets a release
    with a badly written title still link itself instead of landing in the
    review queue. It is a *prior*, not an instruction — the matcher weighs it
    (``PRIOR_WEIGHT``) and refuses to apply it to a candidate whose title says
    something else.

    The library scan may have got there first: it runs every two minutes and
    an unlucky interleaving indexes the file before qBittorrent reports the
    torrent complete. In that case the row exists and its ``match_file`` job
    does not carry the prior, so the prior is written into the pending job's
    payload rather than lost.
    """
    expected = [anime_id, number]
    media_file = await ingest.ingest_file(session, settings, path, expected=expected)
    if media_file is not None:
        return Handoff(path=path, media_file_id=media_file.id, created=True)

    existing = await session.scalar(select(MediaFile).where(MediaFile.path == str(path.resolve())))
    if existing is None:  # pragma: no cover - ingest_file only returns None when it exists
        raise QbitError(f"{path} could not be indexed")

    key = match_dedupe_key(existing.id)
    queued = await find_active(session, MATCH_FILE, key)
    if queued is None:
        await enqueue(
            session,
            MATCH_FILE,
            {"media_file_id": existing.id, "expected": expected},
            dedupe_key=key,
        )
    elif queued.payload.get("expected") != expected:
        # Reassigned rather than mutated in place: SQLAlchemy tracks JSONB by
        # identity, and an in-place update would never be written.
        queued.payload = {**queued.payload, "expected": expected}
    return Handoff(path=path, media_file_id=existing.id, created=False)


async def _complete(ctx: JobContext, episode: Episode, torrent: Torrent, info: TorrentInfo) -> None:
    """A torrent finished: mark it, index the file, queue the match.

    Split from the state change on purpose. ``downloaded`` records what
    qBittorrent said; the hand-off needs the *file*, and the file can be a
    moment behind — a container filesystem the worker sees over a bind mount,
    a move out of an incomplete directory, a rename. So an episode that
    reaches ``downloaded`` without a readable file stays there and the next
    poll tries again: :func:`poll_qbit` looks at ``downloaded`` episodes as
    well as ``downloading`` ones precisely so that this can retry, and the
    ``downloaded → downloaded`` transition is a no-op.
    """
    torrent.completed_at = torrent.completed_at or info.completed_at or datetime.now(UTC)
    transition(episode, EpisodeState.DOWNLOADED, reason=f"{info.name} finished downloading")

    reported = info.content_path or info.save_path
    if not reported:
        raise QbitError(f"qbittorrent reported no path for {info.hash}")
    root = host_path(
        reported,
        downloads_path=ctx.settings.qbit_downloads_path,
        host_downloads=ctx.settings.downloads_dir,
    )
    video = largest_video(root, ctx.settings.video_extensions_set)
    if video is None:
        ctx.log.warning(
            "a finished torrent has no video file yet, will look again next poll",
            extra={"episode_id": episode.id, "path": str(root), "hash": info.hash},
        )
        return

    handoff = await _hand_off(
        ctx.session,
        ctx.settings,
        video,
        anime_id=episode.anime_id,
        number=episode.number,
    )
    transition(episode, EpisodeState.MATCHING, reason="handed to the matcher")
    ctx.log.info(
        "download handed to the library",
        extra={
            "episode_id": episode.id,
            "media_file_id": handoff.media_file_id,
            "indexed_here": handoff.created,
            "path": str(video),
            "expected": [episode.anime_id, episode.number],
        },
    )


@dataclass(frozen=True, slots=True)
class BatchPoll:
    """What one batch's poll decided, for the caller to add up (FR-A11).

    The two hashes are *returned* rather than acted on, because the calls they
    ask for are made after the session has been flushed — the same order the
    single path's stall deletion is in, and for the same reason: the rows are
    what the next poll reads, so they have to be true before the client is told
    anything.
    """

    #: Rows whose progress was written from the client's answer.
    synced: int = 0
    #: Files handed to the library by this poll.
    finished: int = 0
    #: Episodes this poll had to give up on (the pack stalled or vanished).
    vanished: int = 0
    #: The hash to delete **with its files**, or ``None``. Only ever a pack
    #: that has handed the library nothing: the files of one that has are the
    #: library's, and retention deletes those per episode (FR-T1).
    delete: str | None = None
    #: The hash to stop, or ``None``. Two reasons and the same request: every
    #: file Arc asked for is in, or the pack is being given up on while holding
    #: a file something else is using.
    stop: str | None = None
    #: The torrent id whose selection needs writing again, or ``None``: the
    #: client's answer disagreed with the rows, and **the rows are the
    #: instruction** (:func:`_sync_batch_files`).
    reselect: int | None = None


async def _batch_files(
    session: AsyncSession, torrents: Sequence[Torrent]
) -> dict[int, list[TorrentFile]]:
    """``torrent id → its file rows``, in index order, in one query.

    One query for every batch rather than one per batch: the poll runs every
    sixty seconds and the point of the second loop is that it costs a bounded
    amount of work whatever is in flight.
    """
    if not torrents:
        return {}
    rows = await session.scalars(
        select(TorrentFile)
        .where(TorrentFile.torrent_id.in_([torrent.id for torrent in torrents]))
        .order_by(TorrentFile.file_index)
    )
    found: dict[int, list[TorrentFile]] = {}
    for row in rows.all():
        found.setdefault(row.torrent_id, []).append(row)
    return found


async def _claimed_episodes(
    session: AsyncSession, files: Mapping[int, Sequence[TorrentFile]]
) -> dict[int, Episode]:
    """``episode id → episode`` for every episode a batch is holding a file for.

    Only the **wanted** rows: a pack records the episode of every file it can
    identify, and twenty-six rows nobody asked for are twenty-six episodes this
    poll has nothing to say about.
    """
    ids = {
        row.episode_id
        for rows in files.values()
        for row in rows
        if row.wanted and row.episode_id is not None
    }
    if not ids:
        return {}
    rows = await session.scalars(select(Episode).where(Episode.id.in_(ids)))
    return {episode.id: episode for episode in rows.all()}


async def _complete_file(
    ctx: JobContext,
    torrent: Torrent,
    row: TorrentFile,
    episode: Episode,
    info: TorrentInfo,
) -> bool:
    """One file inside a batch is in: hand **it** to the library (FR-A11).

    :func:`_complete`'s counterpart, and the difference between them is the one
    thing a pack changes about the hand-off: :func:`largest_video` is **not**
    used. Inside a season pack the biggest video file is another episode, so the
    path is built from the row the parser wrote at pick time — the torrent's own
    save path plus the file's own name — and ``host_path`` refuses it if that
    does not land under the configured downloads root, exactly as it does for a
    single.

    Everything after that is the single path unchanged: ``downloaded`` records
    what the client said, the file is indexed with ``expected`` so the matcher's
    prior applies (FR-L3, and the episode it names is the one
    ``torrent_files.episode_id`` says — a prior, never a link), and ``matching``
    is the hand-over. A file the worker cannot see yet leaves the episode at
    ``downloaded`` and the next poll tries again, which is why this is retried
    from the row's ``completed_at`` rather than from the client's progress.
    """
    reported = torrent.save_path or info.save_path
    if not reported:
        raise QbitError(f"qbittorrent reported no path for {info.hash}")
    path = host_path(
        str(PurePosixPath(reported) / row.path),
        downloads_path=ctx.settings.qbit_downloads_path,
        host_downloads=ctx.settings.downloads_dir,
    )
    name = PurePosixPath(row.path).name
    transition(episode, EpisodeState.DOWNLOADED, reason=f"{name} finished downloading")
    if not path.is_file():
        ctx.log.warning(
            "a finished batch file is not readable yet, will look again next poll",
            extra={"episode_id": episode.id, "path": str(path), "hash": info.hash},
        )
        return False

    handoff = await _hand_off(
        ctx.session,
        ctx.settings,
        path,
        anime_id=episode.anime_id,
        number=episode.number,
    )
    transition(episode, EpisodeState.MATCHING, reason="handed to the matcher")
    ctx.log.info(
        "a batch file was handed to the library",
        extra={
            "episode_id": episode.id,
            "torrent_id": torrent.id,
            "file_index": row.file_index,
            "media_file_id": handoff.media_file_id,
            "indexed_here": handoff.created,
            "path": str(path),
            "expected": [episode.anime_id, episode.number],
        },
    )
    return True


def _give_up_on(
    ctx: JobContext,
    rows: Sequence[TorrentFile],
    episodes: Mapping[int, Episode],
    *,
    reason: str,
) -> int:
    """Un-want these rows and tell their episodes why (FR-A6, FR-A7).

    The shared ending of the two ways a pack fails — it stalled, or it is gone
    from the client — and it is only ever given the rows that are **wanted and
    not complete**. A file that has already arrived and been handed off is left
    exactly as it is: its episode is past ``downloading`` and its claim is a
    record of where its bytes came from.

    The un-want is not bookkeeping. ``ux_torrent_files_one_wanted_per_episode``
    lets an episode hold **one** live claim anywhere, so a row left wanted
    against a torrent Arc has given up on is a row that would refuse the episode
    its next pack — whether or not that torrent is about to be deleted, which is
    the caller's decision and goes either way (a pack holding a file the library
    already has is *stopped and kept*, not deleted). Nothing is written to the
    client here at all: this function writes rows, and the caller makes whatever
    single request it has decided on after the flush.
    """
    given_up = 0
    for row in rows:
        row.wanted = False
        episode = None if row.episode_id is None else episodes.get(row.episode_id)
        if episode is None:
            continue
        if episode.state in COMPLETABLE:
            transition(episode, EpisodeState.UNAVAILABLE, reason=reason)
            given_up += 1
            ctx.log.warning(
                "an episode a batch was fetching has been given up on",
                extra={
                    "episode_id": episode.id,
                    "torrent_id": row.torrent_id,
                    "file_index": row.file_index,
                    "reason": reason,
                },
            )
    return given_up


def _sync_batch_files(
    ctx: JobContext,
    torrent: Torrent,
    rows: Sequence[TorrentFile],
    listed: Mapping[int, FileInfo],
    *,
    now: datetime,
) -> tuple[int, bool]:
    """Write every file's own progress, and say whether the selection drifted.

    Matched by ``file_index``, which is the client's own numbering and the only
    thing ``filePrio`` ever took — never by position in the answer.

    **The poll is the reconciler for a pack's selection** (2026-09-18, and it
    supersedes the earlier "a file switched off in the Web UI is left off"): any
    file whose ``wanted`` disagrees with what the client says is selected is
    logged at ``WARNING`` and the pack is handed back to ``qbit_reselect``,
    whose instruction is the rows. Both directions matter and one of them is a
    real bug rather than an operator:
    :func:`~arc.services.jobs.queue.enqueue` deduplicates against *running* jobs
    as well as pending ones, so a want withdrawn while a re-selection for that
    torrent is mid-flight is dropped on the floor — and the running job then
    writes priority 1 for a row that has just been un-wanted, with nothing left
    in the queue to correct it. Once a minute, one query, is what makes that
    self-healing. The other direction is the operator's, and this is the change
    of mind: Arc says what it is fetching, says so in the log when it has to
    say it twice, and an operator who wants a file left alone has Arc's own
    controls for it.
    """
    synced = 0
    drifted = False
    for row in rows:
        info = listed.get(row.file_index)
        if info is None:
            ctx.log.warning(
                "the client does not list a file arc recorded for this batch",
                extra={
                    "torrent_id": torrent.id,
                    "hash": torrent.info_hash,
                    "file_index": row.file_index,
                    "path": row.path,
                },
            )
            continue
        row.progress = info.progress
        synced += 1
        if row.wanted != info.wanted:
            drifted = True
            ctx.log.warning(
                "a batch's selection in the client disagrees with arc's rows",
                extra={
                    "torrent_id": torrent.id,
                    "hash": torrent.info_hash,
                    "file_index": row.file_index,
                    "episode_id": row.episode_id,
                    "wanted": row.wanted,
                    "priority": info.priority,
                },
            )
        if not row.wanted:
            continue
        if row.completed_at is None and info.complete:
            row.completed_at = now
    return synced, drifted


async def _poll_batch(
    ctx: JobContext,
    qbit: QbitClient,
    torrent: Torrent,
    *,
    info: TorrentInfo | None,
    rows: Sequence[TorrentFile],
    episodes: Mapping[int, Episode],
    metadata_after: timedelta,
    no_bytes_after: timedelta,
) -> BatchPoll:
    """One batch against what the client says about it (FR-A11).

    The second loop of the poll, and it differs from the first in one idea: for
    a pack, **an episode is complete when its own file is**, not when the
    torrent is. So the per-file progress comes from ``torrents/files`` — one
    extra request per *in-flight* batch, and none at all for a settled one —
    and each file that crosses :data:`~arc.services.acquisition.qbit.COMPLETE_PROGRESS`
    takes the ordinary hand-off for *its* episode
    (:func:`_complete_file`). The torrent is complete when every file Arc asked
    for is, and is then **stopped and kept**: the next episode of the same show
    attaches to it for nothing (FR-A11), which is the whole reason a finished
    pack is not deleted here. What deletes one is
    :func:`~arc.services.acquisition.batch.disposition`, through
    ``qbit_reselect``.

    The two failures are the single path's, applied to the rows rather than to
    one episode. :func:`stall_reason` is **unchanged** and reads the batch's own
    ``torrents/info`` row — the pack was started only after its selection was
    written, so it never sits in ``metaDL`` and ``time_active`` clocks it
    exactly as it clocks a magnet — and a pack that has vanished from the client
    is the same statement one step further on. Either way it is the **wanted and
    not yet complete** rows that are given up on (:func:`_give_up_on`); a file
    already handed off is left where it is.

    **And a stall only deletes what the library is not already using** (owner,
    2026-09-18). For a single, "the files go with the torrent" is safe because
    the only file is the one that never arrived. A pack is not like that: one of
    its episodes can be ``ready`` and playing while another's swarm dies, and
    ``deleteFiles=true`` would take the first one's file out from under the
    library — a ``media_files`` row pointing at nothing. So a pack holding **any
    completed row** is *stopped* and kept with its ``stalled`` row, and what
    becomes of those bytes is retention's per-episode decision (FR-T1) and
    :func:`~arc.services.acquisition.batch.disposition`'s. Only a pack that has
    handed nothing over is deleted with its files, which is the ordinary case
    and the one the single path's argument actually covers.
    """
    now = datetime.now(UTC)
    wanted = [row for row in rows if row.wanted]
    # Whether anything in this pack has already been handed to the library. Read
    # off the rows rather than from this poll's listing, so it stays true on the
    # poll after a stall, when no listing is asked for at all.
    handed_off = any(row.completed_at is not None for row in rows)

    if info is None:
        # Gone from the client, and not by Arc's hand (a ``stalled`` or
        # ``unreadable`` row is filtered out of the query, and ``cancelled``
        # belongs to ``qbit_cancel``). Same ending as a single's, one row at a
        # time: the episodes waiting on a file that is never coming are told,
        # and an episode past ``downloading`` is left alone.
        #
        # **This branch writes ``missing`` before the decided check below**,
        # deliberately and unlike the single loop, and it is what recovers the
        # one race a pack has that a single does not: a want attaching to a pack
        # in the instant between its stall being written and the delete landing.
        # The attach leaves a wanted row on a torrent that is already gone, and
        # the episode would sit ``downloading`` against it for ever; coming
        # through here re-decides it from what the client actually says now.
        was = torrent.qbit_state
        torrent.qbit_state = QBIT_MISSING
        given_up = _give_up_on(
            ctx,
            [row for row in wanted if row.completed_at is None],
            episodes,
            reason=REMOVED_FROM_CLIENT,
        )
        if given_up:
            ctx.log.warning(
                "a batch arc was downloading is gone from the client",
                extra={"torrent_id": torrent.id, "hash": torrent.info_hash, "episodes": given_up},
            )
        elif was != QBIT_MISSING:
            ctx.log.info(
                "a batch arc had finished with is gone from the client",
                extra={"torrent_id": torrent.id, "hash": torrent.info_hash},
            )
        return BatchPoll(vanished=given_up)

    torrent.progress = info.progress
    if torrent.qbit_state in DECIDED_STATES:
        # Arc decided about this pack and the row is the record of it, so the
        # live state must not overwrite it. Only ``stalled`` reaches here (the
        # query filters the other three out), and it is here for the reason the
        # single path has the same branch: last poll's request may not have
        # landed, and a pack still running is a pack still fetching. Which
        # request it was is the same question as above — a pack the library is
        # drawing on is stopped, never deleted, however often this is asked.
        if torrent.qbit_state not in DELETE_ON_SIGHT:
            return BatchPoll()
        if handed_off:
            return BatchPoll(stop=None if info.state in SETTLED_STATES else info.hash)
        return BatchPoll(delete=info.hash)
    torrent.qbit_state = info.state

    # The one request this loop can make, and the condition under which it does
    # not: a pack with every wanted file in, sitting stopped where the last poll
    # left it, has nothing to tell Arc that Arc does not already know.
    synced = 0
    drifted = False
    if any(row.completed_at is None for row in wanted) or info.state not in SETTLED_STATES:
        listed = {info_row.index: info_row for info_row in await qbit.files(torrent.info_hash)}
        synced, drifted = _sync_batch_files(ctx, torrent, rows, listed, now=now)
    reselect = torrent.id if drifted else None

    # The hand-off is driven by the **row**, not by this listing: an episode
    # whose file was complete last poll but not yet readable is still sitting in
    # ``downloaded``, and reading it off ``completed_at`` is what lets the retry
    # happen without asking the client for a file list it has already given.
    finished = 0
    for row in wanted:
        if row.completed_at is None or row.episode_id is None:
            continue
        episode = episodes.get(row.episode_id)
        if episode is None or episode.state not in COMPLETABLE:
            continue
        if await _complete_file(ctx, torrent, row, episode, info):
            finished += 1

    pending = [row for row in wanted if row.completed_at is None]
    if wanted and not pending:
        # Complete, which for a pack means every file Arc *asked for*. Stopped
        # whatever ``qbit_seeding`` says — that switch is about giving something
        # back, and this is about a torrent whose work is done — and kept,
        # because the next episode of this show is one ``filePrio`` away.
        first = torrent.completed_at is None
        torrent.completed_at = torrent.completed_at or info.completed_at or now
        if first:
            ctx.log.info(
                "a batch has every file arc asked for",
                extra={
                    "torrent_id": torrent.id,
                    "hash": torrent.info_hash,
                    "files": len(rows),
                    "wanted": len(wanted),
                },
            )
        return BatchPoll(
            synced=synced,
            finished=finished,
            stop=None if info.state in SETTLED_STATES else info.hash,
            reselect=reselect,
        )

    reason = stall_reason(info, metadata_after=metadata_after, no_bytes_after=no_bytes_after)
    if reason is None:
        return BatchPoll(synced=synced, finished=finished, reselect=reselect)

    torrent.qbit_state = QBIT_STALLED
    given_up = _give_up_on(ctx, pending, episodes, reason=reason)
    # ``handed_off`` is re-read: a file may have completed on this very poll,
    # and a pack that has just given the library a file is one whose files must
    # not be deleted with it.
    holds_files = any(row.completed_at is not None for row in rows)
    ctx.log.warning(
        "a batch is going nowhere and has been given up on",
        extra={
            "torrent_id": torrent.id,
            "hash": torrent.info_hash,
            "state": info.state,
            "progress": round(info.progress, 3),
            "time_active_s": info.time_active,
            "swarm_seeds": info.swarm_seeds,
            "swarm_peers": info.swarm_peers,
            "episodes": given_up,
            "reason": reason,
            # The one thing a reader needs to know about the bytes.
            "kept_files": holds_files,
        },
    )
    return BatchPoll(
        synced=synced,
        finished=finished,
        vanished=given_up,
        stop=info.hash if holds_files else None,
        delete=None if holds_files else info.hash,
        # Deliberately no re-selection: this pack is being deleted or left
        # stalled, and writing priorities to it is the one thing that would
        # then be either impossible or wrong.
    )


@register(POLL_QBIT)
async def poll_qbit(ctx: JobContext) -> None:
    """Sync progress and hand finished downloads to the library (FR-A5).

    **Every** torrent Arc knows about is refreshed, not only the ones behind a
    downloading episode. ``torrents/info`` returns the whole Arc category in
    one request whatever this asks for, so filtering the answer down to the
    episodes mid-flight costs nothing and *loses* something: a torrent still
    seeding behind a ``ready`` episode is what retention (M10) will decide
    about, and its last known state would otherwise be frozen at the moment
    the episode was handed to the library. The state machine is what keeps
    this from touching episodes it should not — only the two
    :data:`COMPLETABLE` states have anywhere to move to.

    **Two loops, and the same one request** (FR-A11, 2026-09-18). The first is
    the one that has always been here, now reading only the rows that name an
    episode — which is every single, and no batch, because a batch belongs to no
    single episode. The second is the batch rows, and it is a second loop rather
    than a branch inside the first because almost nothing about it is the same:
    the episodes come from ``torrent_files``, the progress that matters is each
    file's own, and "finished" is a statement about the files Arc asked for
    (:func:`_poll_batch`). Both read the one ``torrents/info`` answer.
    """
    rows = (
        await ctx.session.execute(
            select(Torrent, Episode)
            .join(Episode, Episode.id == Torrent.episode_id)
            # Explicit, though the inner join above already says it: a batch row
            # has no episode of its own (FR-A11), so this loop is byte for byte
            # the loop it was before batches existed.
            .where(Torrent.episode_id.is_not(None))
            .order_by(Torrent.id)
        )
    ).all()
    # Every batch Arc has not decided about. ``stalled`` is deliberately *not*
    # filtered out — that is the one decision whose deletion may not have landed
    # yet, and a pack still running is a pack still fetching, so it is asked for
    # again exactly as a single's is (:data:`DELETE_ON_SIGHT`).
    batches = list(
        (
            await ctx.session.scalars(
                select(Torrent)
                .where(
                    Torrent.kind == TorrentKind.BATCH,
                    or_(
                        Torrent.qbit_state.is_(None),
                        Torrent.qbit_state.not_in(sorted(DECIDED_STATES - DELETE_ON_SIGHT)),
                    ),
                )
                .order_by(Torrent.id)
            )
        ).all()
    )
    if not rows and not batches:
        ctx.log.debug("no torrents to poll")
        return

    metadata_after = timedelta(minutes=ctx.settings.stall_metadata_minutes)
    no_bytes_after = timedelta(hours=ctx.settings.stall_no_bytes_hours)

    files = await _batch_files(ctx.session, batches)
    claimed = await _claimed_episodes(ctx.session, files)

    synced = finished = vanished = 0
    seeding: list[str] = []
    stalled: list[str] = []
    stopping: list[str] = []
    #: Packs whose selection in the client has drifted from Arc's rows. Queued
    #: rather than written here: ``qbit_reselect`` is the only writer of a
    #: pack's selection, and this loop is holding a transaction.
    repair: list[int] = []
    async with QbitClient.from_settings(ctx.settings) as qbit:
        live = {info.hash: info for info in await qbit.torrents()}

        for torrent, episode in rows:
            info = live.get(torrent.info_hash.lower())
            decided = torrent.qbit_state in DECIDED_STATES
            if info is None:
                if decided:
                    # Arc is why it is gone, and the row already says so. Two
                    # things follow. The note stays (it is the only record of
                    # what became of this download), and — the important half —
                    # **the episode is not touched**: rows iterate oldest
                    # first, so a stalled attempt from this morning would
                    # otherwise drag an episode that has since been retried
                    # from ``downloading`` back to ``unavailable``, over and
                    # over, and the new download would never be handed off.
                    continue
                was = torrent.qbit_state
                torrent.qbit_state = QBIT_MISSING
                # ``downloaded`` as well as ``downloading``: an episode whose
                # torrent finished but whose file was not readable yet sits in
                # ``downloaded`` waiting for the next poll, and if the torrent
                # is deleted in between, that wait would never end.
                if episode.state in COMPLETABLE:
                    transition(episode, EpisodeState.UNAVAILABLE, reason=REMOVED_FROM_CLIENT)
                    vanished += 1
                    ctx.log.warning(
                        "a torrent Arc was downloading is gone from the client",
                        extra={"episode_id": episode.id, "hash": torrent.info_hash},
                    )
                elif was != QBIT_MISSING:
                    ctx.log.info(
                        "a torrent Arc had finished with is gone from the client",
                        extra={
                            "episode_id": episode.id,
                            "hash": torrent.info_hash,
                            "state": episode.state.value,
                        },
                    )
                continue

            torrent.progress = info.progress
            if not decided:
                # A decision is not a state qBittorrent has an opinion about:
                # the client is still happily seeding a file a person has said
                # is not this episode (:mod:`arc.services.acquisition.reject`),
                # and overwriting the note with ``stalledUP`` sixty seconds
                # later would lose the only record of why that download is not
                # to be trusted. The same for a cancelled one, whose delete has
                # been queued but has not run yet.
                torrent.qbit_state = info.state
            synced += 1
            if info.state in SEEDING_STATES and not ctx.settings.qbit_seeding:
                seeding.append(info.hash)
            if decided:
                # Still in the client, and Arc has already decided about it. A
                # ``stalled`` row here means last poll's delete did not land —
                # the client went away between the flush and the request — so
                # ask again; ``rejected`` and ``cancelled`` are somebody else's
                # to remove (:data:`DELETE_ON_SIGHT`).
                if torrent.qbit_state in DELETE_ON_SIGHT:
                    stalled.append(info.hash)
                continue
            if info.complete and episode.state in COMPLETABLE:
                await _complete(ctx, episode, torrent, info)
                finished += 1
                continue
            if episode.state not in COMPLETABLE:
                continue
            reason = stall_reason(
                info, metadata_after=metadata_after, no_bytes_after=no_bytes_after
            )
            if reason is None:
                continue
            # FR-A6 from the other end. The retry schedule handles "Nyaa has
            # nothing"; this handles "Nyaa had something and it was dead", and
            # it has to end the same way — ``unavailable`` is the state the
            # daily retry picks up and the show page explains (FR-A7), and the
            # files go with the torrent because a partial download of a release
            # Arc will never choose again is worth nothing to anybody.
            torrent.qbit_state = QBIT_STALLED
            transition(episode, EpisodeState.UNAVAILABLE, reason=reason)
            stalled.append(info.hash)
            ctx.log.warning(
                "a torrent is going nowhere and has been given up on",
                extra={
                    "episode_id": episode.id,
                    "hash": torrent.info_hash,
                    "state": info.state,
                    "progress": round(info.progress, 3),
                    "time_active_s": info.time_active,
                    "swarm_seeds": info.swarm_seeds,
                    "swarm_peers": info.swarm_peers,
                    "connected_seeds": info.num_seeds,
                    "reason": reason,
                },
            )

        for torrent in batches:
            outcome = await _poll_batch(
                ctx,
                qbit,
                torrent,
                info=live.get(torrent.info_hash.lower()),
                rows=files.get(torrent.id, ()),
                episodes=claimed,
                metadata_after=metadata_after,
                no_bytes_after=no_bytes_after,
            )
            synced += outcome.synced
            finished += outcome.finished
            vanished += outcome.vanished
            if outcome.delete is not None:
                stalled.append(outcome.delete)
            if outcome.stop is not None:
                stopping.append(outcome.stop)
            if outcome.reselect is not None:
                repair.append(outcome.reselect)

        # **The rows first, then the client.** The flush is what makes the
        # deletion safe to fail: the episode is already ``unavailable`` and the
        # row already says ``stalled``, so a client that stops answering here
        # leaves a coherent database and one torrent to tidy up — which the
        # next poll does, because a ``stalled`` row still present in the client
        # is asked to be deleted again (above). Doing it the other way round
        # would delete files and then, on a rollback, forget it had.
        #
        # A partial download of a release the ranker is now barred from
        # choosing again (``_pick``) is bytes nobody will ever use, so the
        # files go too. Every hash came out of ``qbit.torrents()`` and
        # :meth:`~arc.services.acquisition.qbit.QbitClient.delete` checks the
        # category again, so this cannot reach a torrent Arc did not add.
        await ctx.session.flush()
        for torrent_id in repair:
            # After the flush, for the reason every other enqueue in this
            # codebase is: the handler reads the rows and nothing else, so a job
            # row a worker can see before the rows it is about is a job that
            # writes yesterday's selection.
            await claims.enqueue_reselect(ctx.session, torrent_id)
        if repair:
            await ctx.session.flush()
        if stalled:
            await qbit.delete(stalled, delete_files=True)

        # Packs to stop, **and keep** (FR-A11), for either of two reasons: every
        # file Arc asked for is in, or the pack was given up on while holding a
        # file the library already has. Its own rule rather than the seeding one
        # below, and it applies even on a deployment that has chosen to seed:
        # what is being stopped is a torrent Arc is finished asking of. Neither
        # is ever deleted here — the next episode of the show may attach to the
        # first, retention owns the second's bytes per episode (FR-T1), and
        # ``batch.disposition`` is the one place a pack is removed.
        if stopping:
            await qbit.stop(stopping)
            ctx.log.info("batches stopped", extra={"count": len(stopping)})

        # Belt and braces to ``qbit_apply_policy`` (spec §9). The share-ratio
        # limit is what normally stops these, and it only applies to torrents
        # the client held when the policy was written — so anything added
        # before that, or by a client that ignored the limit, is stopped here.
        # Every hash came out of ``qbit.torrents()`` above, which is filtered
        # to Arc's category, so nothing anyone else added is touched.
        if seeding:
            await qbit.stop(seeding)
            ctx.log.info("seeding torrents stopped", extra={"count": len(seeding)})

    await ctx.session.flush()
    ctx.log.info(
        "qbittorrent polled",
        extra={
            "job_id": ctx.job.id,
            "tracked": len(rows),
            "batches": len(batches),
            "in_client": len(live),
            "synced": synced,
            "finished": finished,
            "vanished": vanished,
            "stalled": len(stalled),
            "reselected": len(repair),
            "stopped_batches": len(stopping),
            "stopped_seeding": len(seeding),
            "progress": {
                torrent.info_hash[:8]: round(torrent.progress or 0.0, 3)
                for torrent in (*(row for row, _ in rows), *batches)
            },
        },
    )


@register(QBIT_CANCEL)
async def qbit_cancel(ctx: JobContext) -> None:
    """Remove one episode's cancelled torrents from the client, with the files.

    The decision was made by the reconciler
    (:func:`~arc.services.acquisition.wants.cancel_if_unwanted`) and is already
    in the database: the episode is back to ``not_wanted`` and every torrent
    row it has says ``cancelled``. All that is left is the request to another
    process, which is exactly why it is a job — the reconciliation must not
    hold its transaction open across an HTTP call, and a client that is not
    answering must not be able to fail it.

    **Only rows marked** :data:`~arc.services.acquisition.qbit.QBIT_CANCELLED`.
    An episode can be wanted again in the seconds between the reconcile and
    this job — a user changing their mind, or a second user's list — and the
    search that follows chooses a *new* release whose row says whatever
    qBittorrent says. Deleting "this episode's torrents" would take that one
    with it; deleting the marked ones takes only what was cancelled.

    **And then the rows go.** This is the one ending that deletes a ``torrents``
    row rather than annotating it, and the reason is :func:`_pick`: a row bars
    its release from ever being chosen again, which is right for ``stalled``
    (it was tried and it failed) and for ``rejected`` (a person said that file
    was wrong), and wrong for a cancel — nothing was tried and nothing failed,
    somebody simply stopped wanting the episode. Leaving the row would mean
    that pressing Cancel and changing your mind a minute later cost you the
    best release on Nyaa for good. Nothing references the row by then: the
    episode is ``not_wanted``, the torrent is out of the client, and its files
    are gone with it.

    Nothing is caught. An unreachable client raises
    :class:`~arc.services.acquisition.qbit.QbitUnavailable` and the runner
    retries with backoff, which is the whole of the error handling this needs:
    the rows say what should happen and they do not expire — and because the
    rows are deleted only *after* the client has answered, a retry finds
    exactly the work the failed attempt left.
    """
    episode_id = int(ctx.payload["episode_id"])
    torrents = list(
        (
            await ctx.session.scalars(
                select(Torrent).where(
                    Torrent.episode_id == episode_id,
                    Torrent.qbit_state == QBIT_CANCELLED,
                )
            )
        ).all()
    )
    if not torrents:
        ctx.log.info("nothing left to cancel", extra={"episode_id": episode_id})
        return
    hashes = [torrent.info_hash for torrent in torrents]
    async with QbitClient.from_settings(ctx.settings) as qbit:
        await qbit.delete(hashes, delete_files=True)
    for torrent in torrents:
        await ctx.session.delete(torrent)
    await ctx.session.flush()
    ctx.log.info(
        "cancelled torrents removed from the client",
        extra={"job_id": ctx.job.id, "episode_id": episode_id, "hashes": hashes},
    )


@register(QBIT_RESELECT)
async def qbit_reselect(ctx: JobContext) -> None:
    """Write one batch's selection to the client and decide its fate (FR-A11).

    The **only** place a batch's priorities and run state change after it was
    added. Four paths take a file away or give one back — a want withdrawn
    (``wants.cancel_if_unwanted``), a want attached
    (:func:`~arc.services.acquisition.batch.claim_existing`), retention taking a
    file, a rejected member — and every one of them writes the
    ``torrent_files`` rows and queues this. A job for the two reasons
    ``qbit_cancel`` is one: the reconciler and the retention sweep hold
    transactions that must not span an HTTP call, and a client that is not
    answering must not be able to fail them.

    **The rows are the instruction, and the payload is only which torrent.**
    That is what makes it idempotent and what makes its per-torrent dedupe key
    right (:func:`~arc.services.acquisition.names.reselect_dedupe_key`): two
    episodes of one pack changing in the same moment — one want withdrawn while
    another attaches — is one selection to write, and a second run writes the
    same priorities the first did.

    The order inside is the add sequence's, for the same reason: **off before
    on**, so any instant in between leaves fewer files selected and never more.
    And nothing is written at all unless the client still describes the torrent
    Arc's rows describe (:func:`~arc.services.acquisition.batch.listing_mismatch`)
    — ``filePrio`` takes an index and nothing else, so a listing that has moved
    under the rows is one where 1 would fetch a file nobody asked for. That
    cannot happen without something outside Arc replacing the torrent, which is
    why it is an ``ERROR`` and a no-op rather than a repair.

    Then :func:`~arc.services.acquisition.batch.disposition`: ``KEEP`` starts it
    (or stops it, if every wanted file is already in — there is nothing to
    fetch and Arc does not seed), ``STOP`` leaves the pack in the client for the
    next episode of the show, and ``DELETE`` removes it with its files and
    deletes the row, ``torrent_files`` cascading with it.

    Nothing is caught. An unreachable client raises
    :class:`~arc.services.acquisition.qbit.QbitUnavailable`, the runner retries
    with backoff, and the rows still say what should happen — the same error
    handling ``qbit_cancel`` has, and for the same reason: the decision is in
    the database before this job runs.
    """
    torrent_id = int(ctx.payload["torrent_id"])
    # **Locked for the length of the job.** The one race that could lose bytes
    # is an attach committing ``wanted = true`` between this handler reading the
    # rows and its DELETE landing: the pack would go with its files while an
    # episode sat ``downloading`` against it. ``claim_existing`` takes the same
    # lock, so the two are ordered — whichever is second sees what the first
    # did, and neither has to guess.
    torrent = await ctx.session.get(Torrent, torrent_id, with_for_update=True)
    if torrent is None or torrent.kind is not TorrentKind.BATCH:
        # Gone (a ``DELETE`` disposition on an earlier run of this very job, or
        # the show being deleted), or not a batch at all. Neither is an error:
        # the job's whole content is "look at this pack's rows", and there are
        # none to look at.
        ctx.log.info("nothing to re-select", extra={"torrent_id": torrent_id})
        return
    if torrent.qbit_state in batch.UNATTACHABLE_STATES:
        # Arc has decided about this pack — it stalled, was cancelled, was
        # rejected, could not be read, or is not in the client any more. A
        # selection written to any of those is a request about a torrent that is
        # gone or must not run.
        ctx.log.info(
            "not re-selecting a batch arc has decided about",
            extra={"torrent_id": torrent.id, "state": torrent.qbit_state},
        )
        return

    rows = list(
        (
            await ctx.session.scalars(
                select(TorrentFile)
                .where(TorrentFile.torrent_id == torrent.id)
                .order_by(TorrentFile.file_index)
            )
        ).all()
    )
    wanted = [row for row in rows if row.wanted]
    # Read once and used after the row may have been deleted.
    info_hash = torrent.info_hash

    async with QbitClient.from_settings(ctx.settings) as qbit:
        listed = await qbit.files(info_hash)
        mismatch = batch.listing_mismatch(rows, listed)
        if mismatch is not None:
            # **And the pack is marked missing.** Nothing is written to a
            # listing Arc cannot read, but leaving the row saying "downloading"
            # would strand every episode waiting on this pack in
            # ``downloading`` for ever, each holding the one live claim that
            # stops it being fetched any other way. ``missing`` is the honest
            # word for it — this torrent is not the one Arc recorded — and it is
            # what the next poll reads to give those episodes up with the
            # ordinary sentence (FR-A6, FR-A7), or to pick the row back up if
            # the client turns out to be describing it properly again.
            torrent.qbit_state = QBIT_MISSING
            await ctx.session.flush()
            ctx.log.error(
                "a batch's file list no longer matches what arc recorded; no priority was written",
                extra={"torrent_id": torrent.id, "hash": info_hash, "reason": mismatch},
            )
            return

        # After the listing check and before the first write, so that the
        # decision is read from rows nothing has touched since the lock above.
        verdict = await batch.disposition(ctx.session, torrent)

        await qbit.file_priority(
            info_hash, [row.file_index for row in rows if not row.wanted], FILE_OFF
        )
        await qbit.file_priority(info_hash, [row.file_index for row in wanted], FILE_ON)
        for row in rows:
            row.priority = FILE_ON if row.wanted else FILE_OFF

        if verdict is batch.Disposition.DELETE:
            await qbit.delete([info_hash], delete_files=True)
            await ctx.session.delete(torrent)
        elif verdict is batch.Disposition.STOP:
            await qbit.stop([info_hash])
        elif any(row.completed_at is None for row in wanted):
            await qbit.start([info_hash])
        else:
            # Kept, and there is nothing left to fetch: every file somebody is
            # waiting for is already on the disk. Stopped rather than started,
            # because starting it would only seed (spec §9).
            await qbit.stop([info_hash])

    await ctx.session.flush()
    ctx.log.info(
        "batch selection written",
        extra={
            "job_id": ctx.job.id,
            "torrent_id": torrent_id,
            "hash": info_hash,
            "files": len(rows),
            "wanted": len(wanted),
            "episodes": sorted(row.episode_id for row in wanted if row.episode_id is not None),
            "disposition": verdict.value,
        },
    )


@register(QBIT_POLICY)
async def qbit_apply_policy(ctx: JobContext) -> None:
    """Write Arc's seeding and queue policy to the client (spec §9).

    Queued at every worker start-up and once a day. Both, because the two
    failure modes are different: a fresh container comes up with qBittorrent's
    defaults (seed forever, upload unlimited, three concurrent downloads), and
    a long-lived one can be changed by hand in the Web UI. Neither should be
    able to leave Arc seeding, and neither should be able to leave it with
    three download slots — the limits an operator raised by hand are exactly
    the ones a container restart loses, which is how they came to be written
    here.

    Nothing is caught here. qBittorrent being unreachable raises
    :class:`~arc.services.acquisition.qbit.QbitUnavailable`, the runner retries
    with backoff, and "the client is not up yet" resolves itself — which is
    exactly why this is a job rather than a line in ``arc.worker``.
    """
    async with QbitClient.from_settings(ctx.settings) as qbit:
        sent = await qbit.apply_policy(
            seeding=ctx.settings.qbit_seeding,
            upload_limit_kib=ctx.settings.qbit_upload_limit_kib,
            max_active_downloads=ctx.settings.qbit_max_active_downloads,
            max_active_torrents=ctx.settings.qbit_max_active_torrents,
        )
    ctx.log.info(
        "qbittorrent policy written",
        extra={"job_id": ctx.job.id, "seeding": ctx.settings.qbit_seeding, **sent},
    )


__all__ = [
    "ACTIVE_DL_STATES",
    "AIR_DAY_RETRY",
    "AIR_DAY_WINDOW",
    "BATCH_TAGS",
    "CLIENT_ERROR",
    "COMPUTE_WANTS",
    "ERROR_STATES",
    "FILES_GONE",
    "GIVE_UP_AFTER",
    "HELD_LOG",
    "LATER_RETRY",
    "METADATA_STATES",
    "NO_BYTES",
    "NO_METADATA",
    "NO_RELEASE",
    "NO_SEEDERS",
    "PAUSED_LOG",
    "PAUSED_RETRY",
    "POLL_QBIT",
    "QBIT_CANCEL",
    "QBIT_POLICY",
    "QBIT_RESELECT",
    "REMOVED_FROM_CLIENT",
    "SEARCH_RELEASE",
    "SETTLED_STATES",
    "STALL_METADATA_AFTER",
    "STALL_NO_BYTES_AFTER",
    "STARTED_KEY",
    "compute_wants",
    "largest_video",
    "poll_qbit",
    "qbit_apply_policy",
    "qbit_cancel",
    "qbit_reselect",
    "retry_delay",
    "search_release",
    "stall_reason",
]
