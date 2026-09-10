"""The acquisition handlers: wants, search, poll, policy (architecture.md §5.1).

``compute_wants`` decides *what*; ``search_release`` decides *which release*
and starts it; ``poll_qbit`` watches it finish and hands the file to the
library. Between them they cover FR-A1 through FR-A6. ``qbit_apply_policy`` is
housekeeping alongside them: it writes Arc's no-seeding, capped-upload policy
to the client (spec §9), and ``poll_qbit`` stops anything that got past it.

All three are idempotent, and each is idempotent for a different reason.
``compute_wants`` reconciles a whole table against the lists, so a second run
finds nothing to do. ``search_release`` refuses to act on an episode that is
not ``wanted`` or ``searching``, and qBittorrent answers ``Ok.`` to a magnet it
already holds. ``poll_qbit`` reads the client's live state and writes what it
says, and the ``media_files.path`` unique constraint is what keeps a
re-delivered file from being indexed twice. (``qbit_apply_policy`` writes the
same fixed values every time, which is idempotence for free.)

**The retry schedule (FR-A6) lives in the job payload, not in a column.**
A search that finds nothing requeues *itself* with a delay and carries
``attempts_started_at`` forward, so "how long have we been looking for this?"
is answered by the job that is doing the looking. The alternative — a column on
``episodes`` — would be a migration for a number only this handler reads, and
it would have to be cleared by everything else that touches the row. A search
that arrives with no such value — the daily revival of an episode that already
gave up — reads it off the last search this episode had instead, so the
fortnight is cumulative rather than restarted every morning (:func:`_started_at`).

**The pause switch stops two of the three.** ``acquisition_paused`` in
``settings`` (:func:`arc.services.acquisition.rules.is_paused`) makes
``compute_wants`` a no-op and turns ``search_release`` into "put me back on the
queue in fifteen minutes". ``poll_qbit`` keeps running: pausing means *stop
fetching more*, not *abandon the download that is already 80 % of the way in*,
and a torrent that finishes during a pause still reaches the library and still
becomes something to watch.

**qBittorrent being down is not "unavailable".** FR-A6's ``unavailable`` means
*no acceptable release exists*, which is a statement about Nyaa. A client that
cannot be reached raises, the runner retries with backoff, and the episode
stays ``searching`` — which is what the show page then says (FR-A7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Anime, Episode, EpisodeState, Job, MediaFile, Torrent, Want
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    POLL_QBIT,
    QBIT_POLICY,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
    search_dedupe_key,
)
from arc.services.acquisition.nyaa import Ranked, search_for_episode
from arc.services.acquisition.qbit import (
    SEEDING_STATES,
    QbitClient,
    QbitError,
    TorrentInfo,
    host_path,
)
from arc.services.acquisition.reject import QBIT_REJECTED
from arc.services.acquisition.rules import is_paused, load_rules
from arc.services.acquisition.states import transition
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

#: And when nobody wants the episode any more, which is not a failure at all —
#: it is why the episode leaves ``searching`` for ``not_wanted`` rather than
#: sitting there being looked for on nobody's behalf.
NO_LONGER_WANTED = "no longer wanted"

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
    """Recompute every user's acquisition window (FR-A1, FR-A2, FR-W4)."""
    result = await reconcile_wants(ctx.session)
    ctx.log.info("wants computed", extra={"job_id": ctx.job.id, **result.as_dict()})


async def _pick(ctx: JobContext, episode: Episode, ranked: list[Ranked]) -> Ranked | None:
    """The best-ranked release this episode may actually claim.

    ``torrents.info_hash`` is unique and one hash is one download in
    qBittorrent, saved under one episode's directory. So a release whose hash
    already belongs to *another* episode cannot be taken: the row would keep
    pointing at the first episode, the save path would be the first episode's,
    and this episode would end up with a torrent it does not own and a file it
    never sees. That is what a batch offered as a single episode looks like,
    and it is worth skipping rather than failing — the next candidate down is
    usually the same episode from another group.

    Same episode, same hash is not that: it is a retry after a crash, and
    :func:`_record_torrent` updates the row it already has.
    """
    for entry in ranked:
        owner = await ctx.session.scalar(
            select(Torrent.episode_id).where(Torrent.info_hash == entry.item.info_hash)
        )
        if owner is not None and owner != episode.id:
            ctx.log.info(
                "skipping a release already downloaded for another episode",
                extra={
                    "episode_id": episode.id,
                    "hash": entry.item.info_hash,
                    "held_by_episode_id": owner,
                    "title": entry.item.title,
                },
            )
            continue
        return entry
    return None


async def _record_torrent(session: AsyncSession, episode: Episode, chosen: Ranked) -> Torrent:
    """The ``torrents`` row for the chosen release, reused if it exists.

    ``info_hash`` is unique, and a hash Arc already has a row for is this
    episode's own earlier attempt — a retry after a crash, or a second run of
    this handler. The existing row is updated rather than duplicated. A hash
    belonging to a *different* episode never reaches here: :func:`_pick` has
    already passed over it.
    """
    item = chosen.item
    torrent = await session.scalar(select(Torrent).where(Torrent.info_hash == item.info_hash))
    if torrent is None:
        torrent = Torrent(episode_id=episode.id, info_hash=item.info_hash)
        session.add(torrent)
    torrent.magnet = item.magnet
    torrent.title = item.title
    torrent.group = chosen.candidate.group
    torrent.resolution = chosen.candidate.resolution
    torrent.seeders = item.seeders
    torrent.trusted = item.trusted
    await session.flush()
    return torrent


async def _schedule_retry(ctx: JobContext, episode: Episode, *, now: datetime) -> None:
    """Requeue this search, or give up and flag the episode (FR-A6)."""
    started = await _started_at(ctx, episode.id, now=now)
    if now - started >= GIVE_UP_AFTER:
        transition(episode, EpisodeState.UNAVAILABLE, reason=NO_RELEASE)
        ctx.log.warning(
            "giving up on an episode",
            extra={
                "episode_id": episode.id,
                "searching_since": started.isoformat(),
                "reason": NO_RELEASE,
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
            "retry_in_s": int(delay.total_seconds()),
            "searching_since": started.isoformat(),
            "give_up_at": (started + GIVE_UP_AFTER).isoformat(),
        },
    )


async def _requeue_paused(ctx: JobContext, episode_id: int, *, now: datetime) -> None:
    """Put this search back on the queue, unchanged, for a quarter of an hour.

    Requeued rather than failed or dropped: a pause is temporary, and the job
    row *is* the record that this episode is still owed a search. Nothing about
    the episode is touched — no state change, no ``torrents`` row, no request
    to Nyaa — so a resume finds exactly the world the pause left behind.

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
        "acquisition paused; search requeued without touching nyaa",
        extra={
            "episode_id": episode_id,
            "retry_in_s": int(PAUSED_RETRY.total_seconds()),
        },
    )


@register(SEARCH_RELEASE)
async def search_release(ctx: JobContext) -> None:
    """Find a release for one episode and start it downloading (FR-A3..A6)."""
    episode_id = int(ctx.payload["episode_id"])
    now = datetime.now(UTC)

    if await is_paused(ctx.session):
        # Before the episode is even loaded: while paused this handler must
        # read nothing it might act on and write nothing but its own retry.
        await _requeue_paused(ctx, episode_id, now=now)
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
        transition(episode, EpisodeState.NOT_WANTED, reason=NO_LONGER_WANTED)
        await ctx.session.flush()
        ctx.log.info("nobody wants this episode any more", extra={"episode_id": episode_id})
        return

    anime = await ctx.session.get(Anime, episode.anime_id)
    if anime is None:  # pragma: no cover - the foreign key forbids it
        raise RuntimeError(f"episode {episode_id} has no anime row")

    transition(episode, EpisodeState.SEARCHING, reason="looking for a release")
    await ctx.session.flush()

    rules = await load_rules(ctx.session, anime.id)
    # The process-wide client, not one of this job's own: the pacing gap and
    # the ten-minute cache are only worth anything if every concurrent search
    # goes through the same instance. It is never closed here — it outlives
    # the job (:func:`arc.services.acquisition.nyaa.shared_client`).
    nyaa = nyaa_module.shared_client(ctx.settings.nyaa_url)
    ranked = await search_for_episode(nyaa, anime, episode.number, rules)

    chosen = await _pick(ctx, episode, ranked) if ranked else None
    if chosen is None:
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
    """
    rows = (
        await ctx.session.execute(
            select(Torrent, Episode)
            .join(Episode, Episode.id == Torrent.episode_id)
            .order_by(Torrent.id)
        )
    ).all()
    if not rows:
        ctx.log.debug("no torrents to poll")
        return

    synced = finished = vanished = 0
    seeding: list[str] = []
    async with QbitClient.from_settings(ctx.settings) as qbit:
        live = {info.hash: info for info in await qbit.torrents()}

        for torrent, episode in rows:
            info = live.get(torrent.info_hash.lower())
            if info is None:
                was = torrent.qbit_state
                torrent.qbit_state = "missing"
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
                elif was != "missing":
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
            if torrent.qbit_state != QBIT_REJECTED:
                # ``rejected`` is a decision, not a state qBittorrent has an
                # opinion about: the client is still happily seeding a file a
                # person has said is not this episode
                # (:mod:`arc.services.acquisition.reject`), and overwriting the
                # note with ``stalledUP`` sixty seconds later would lose the
                # only record of why that download is not to be trusted.
                torrent.qbit_state = info.state
            synced += 1
            if info.state in SEEDING_STATES and not ctx.settings.qbit_seeding:
                seeding.append(info.hash)
            if info.complete and episode.state in COMPLETABLE:
                await _complete(ctx, episode, torrent, info)
                finished += 1

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
            "in_client": len(live),
            "synced": synced,
            "finished": finished,
            "vanished": vanished,
            "stopped_seeding": len(seeding),
            "progress": {
                torrent.info_hash[:8]: round(torrent.progress or 0.0, 3) for torrent, _ in rows
            },
        },
    )


@register(QBIT_POLICY)
async def qbit_apply_policy(ctx: JobContext) -> None:
    """Write Arc's seeding policy to the client (spec §9).

    Queued at every worker start-up and once a day. Both, because the two
    failure modes are different: a fresh container comes up with qBittorrent's
    defaults (seed forever, upload unlimited), and a long-lived one can be
    changed by hand in the Web UI. Neither should be able to leave Arc seeding.

    Nothing is caught here. qBittorrent being unreachable raises
    :class:`~arc.services.acquisition.qbit.QbitUnavailable`, the runner retries
    with backoff, and "the client is not up yet" resolves itself — which is
    exactly why this is a job rather than a line in ``arc.worker``.
    """
    async with QbitClient.from_settings(ctx.settings) as qbit:
        sent = await qbit.apply_policy(
            seeding=ctx.settings.qbit_seeding,
            upload_limit_kib=ctx.settings.qbit_upload_limit_kib,
        )
    ctx.log.info(
        "qbittorrent policy written",
        extra={"job_id": ctx.job.id, "seeding": ctx.settings.qbit_seeding, **sent},
    )


__all__ = [
    "AIR_DAY_RETRY",
    "AIR_DAY_WINDOW",
    "COMPUTE_WANTS",
    "GIVE_UP_AFTER",
    "LATER_RETRY",
    "NO_LONGER_WANTED",
    "NO_RELEASE",
    "PAUSED_RETRY",
    "POLL_QBIT",
    "QBIT_POLICY",
    "REMOVED_FROM_CLIENT",
    "SEARCH_RELEASE",
    "STARTED_KEY",
    "compute_wants",
    "largest_video",
    "poll_qbit",
    "qbit_apply_policy",
    "retry_delay",
    "search_release",
]
