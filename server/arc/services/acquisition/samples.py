"""The "try episode 1" sample: one episode, and no commitment (FR-A8).

Acquisition's rule is otherwise absolute — the next N unwatched episodes of a
``watching``/``planned`` show and nothing else (FR-A1) — and this is the single
exception the owner asked for: a user may ask for a show's **first episode** as
a sample, with no list change and therefore no MAL write, and decide from the
episode itself whether the show is worth adding.

It is deliberately not a new kind of object. A sample is a ``wants`` row with
``sample = true``, so the episode goes through the same states, the same
merging across users (FR-A2), the same D-day drop (FR-T2) and the same
retention grace (FR-T1) as any other want. The only thing the flag changes is
who justifies the row: the reconciler reads a sample off the row rather than
working it out from a list, and stops reviving it once it has been dropped
(:mod:`arc.services.acquisition.wants` has the argument).

Three things are refused, and each of them is the user being told something
true rather than an error:

* the show has no episodes cached yet — there is nothing to name as "episode
  1", and inventing a row for it would guess at a number the catalogue has not
  published;
* episode 1 has not aired — the acquisition rules never fetch ahead of a
  broadcast, and a sample is not a licence to;
* the show is already ``watching`` or ``planned`` **and not dormant** — the
  ordinary window covers episode 1 already, and a second row saying so would
  only be a second thing to clean up.

That last refusal is about the *window*, not about the status, which is why
FR-A9 changed it. A **dormant** watching/planned entry has no window: nothing
is being fetched for it, and answering "you are already following this show;
the next episodes are fetched automatically" would be plainly false. So a
sample is allowed on one and behaves exactly like a sample on an unlisted show
— the reconciler does not put the pair in ``wanting`` either, so the row is
read off itself in ``_sample_wants``.

A sample **never activates the entry**, and that is the point of it rather than
an oversight. Activating means "fetch this show", which is the window: N
episodes, on a list the user may not have curated. "Try episode 1" means one
episode, so it writes one want and leaves the entry dormant. The show starts
fetching properly when the user says so — by setting a status, by finishing the
sample (FR-S4 leaves an existing status alone, but the completion is a touch),
or with the Show page's "Fetch this show".

Both writers flush and leave the transaction to the caller, and both act on the
episode **through the reconciler's own helpers** —
:func:`~arc.services.acquisition.wants.start_search` and
:func:`~arc.services.acquisition.wants.release_if_unwanted` — as well as
enqueueing a reconciliation. The enqueue alone was the first version of this,
and it was wrong in the way that matters: the show page said "Not fetched" for
up to fifteen minutes after a person had pressed the button, and said it
forever while acquisition was paused. Calling the shared helpers rather than
copying their bodies is the whole point: the ``STARTABLE`` set, the legal
transitions, the search's dedupe key and "nobody wants this any more" are
decided in one place, and the sample routes are simply another caller. The
``compute_wants`` enqueue stays, because the *rest* of the world (the window,
the drops, every other episode) is still the reconciler's to settle.

Paused acquisition is unaffected by any of this: the episode truthfully reads
``wanted``, and the ``search_release`` job requeues itself without touching
Nyaa until an admin resumes.

A request also queues the show's **TMDB enrichment**
(:func:`~arc.services.tmdb.jobs.enqueue_show_enrichment`), because the episode
on its way is a 16:9 card and its still would otherwise arrive with the nightly
sweep — or whenever the viewer next happened to open Watch Now, which is where
the only other on-demand ask lives (owner, 2026-09-13). It is gated by that
module's own rules (a ``TMDB_API_KEY`` is set, the id map can reach the show,
and there is still a hole), deduplicated on one key per show, and incapable of
failing a sample: nothing here talks to TMDB, and a deployment with no key
queues nothing at all. The ``Settings`` this needs is passed in rather than
read here, the same way the router hands one to everything else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Anime, Episode, ListEntry, Want
from arc.services.acquisition.dormancy import is_dormant
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.acquisition.wants import (
    WANTING_STATUSES,
    cancel_if_unwanted,
    enqueue_cancel,
    release_if_unwanted,
    start_search,
)
from arc.services.catalog.airing import RELEASING, aired_through, is_aired
from arc.services.tmdb.jobs import enqueue_show_enrichment

log = logging.getLogger(__name__)

#: What :func:`cancel_sample` writes into ``wants.drop_reason``. A drop rather
#: than a delete, like every other way a want ends: the row is what retention
#: counts FR-T1's grace period from, and a user who cancels a sample two days
#: after the file arrived has left a file behind that somebody has to measure
#: the grace period on.
SAMPLE_CANCELLED_REASON = "sample cancelled"


def _dormant(entry: ListEntry, anime: Anime | None) -> bool:
    """Whether this entry is fetching nothing at all (FR-A9).

    The airing half comes off the show Arc already has in hand; a show with no
    cached row cannot be airing as far as anything here knows, which is the
    same answer a null status gives.
    """
    status = anime.status if anime is not None else None
    return is_dormant(entry, airing=status == RELEASING)


class SampleError(Exception):
    """Why a sample cannot be started. Carries the sentence the user reads."""


class NoEpisodes(SampleError):
    """The catalogue has not given Arc an episode list for this show yet."""


class NotAired(SampleError):
    """Episode 1 is still in the future (FR-A1: nothing unaired is fetched)."""


class AlreadyFollowing(SampleError):
    """The show is ``watching``/``planned``, so the window already covers it."""


#: The sentences behind the three refusals, in plain English because they are
#: what the show page shows: each one names the situation and, where there is
#: one, what happens instead.
NO_EPISODES_DETAIL = "this show has no episodes yet"
NOT_AIRED_DETAIL = "episode 1 has not aired yet"
ALREADY_FOLLOWING_DETAIL = (
    "you are already following this show; the next episodes are fetched automatically"
)


@dataclass(frozen=True, slots=True)
class Sample:
    """A sample want and the episode it is for.

    The pair rather than the want alone because every caller needs the episode
    *number* — it is what the response says and what the button said — and
    :func:`request_sample` has the row in hand already. Returning only the want
    made the router fetch the episode back a second time to render it.
    """

    want: Want
    episode: Episode


async def request_sample(
    session: AsyncSession, *, user_id: int, anime_id: int, now: datetime, settings: Settings
) -> Sample:
    """Want this show's first episode, without touching the user's list (FR-A8).

    A user who already has a live sample on this show gets **that** sample
    back, untouched and with nothing queued: one sample per (user, show) is the
    invariant the Cancel button depends on, and "the lowest-numbered episode"
    is not a stable answer — a catalogue that later publishes a special as
    episode 0 would otherwise turn a second press into a second live row, and
    Cancel would close only one of them.

    Otherwise: raises a :class:`SampleError` subclass for each of the three
    refusals in the module docstring, or creates the (user, episode) row — or,
    if one is already there from a list window or a drop this user has come
    back from, marks it as a sample and un-drops it, because pressing the
    button again *is* the user coming back. Flushes; the caller commits.

    The entry, if there is one, is left **dormant** (FR-A9): see the module
    docstring. One episode is what was asked for.

    The episode list is read whole and ordered by ``number``, not by id:
    episode rows arrive in whatever order the catalogue's schedule pages came
    back in, and the aired rule needs the list anyway to place its boundary.
    """
    live = await sample_for(session, user_id=user_id, anime_id=anime_id)
    if live is not None:
        episode = await session.get(Episode, live.episode_id)
        if episode is not None:
            # Ask for the pictures on this path too: a second press is often
            # somebody looking at a card that still has none, and the enqueue
            # is deduplicated, so it costs one SELECT to be sure.
            await enqueue_show_enrichment(session, anime_id, settings=settings)
            return Sample(want=live, episode=episode)

    episodes = list(
        (
            await session.scalars(
                select(Episode).where(Episode.anime_id == anime_id).order_by(Episode.number)
            )
        ).all()
    )
    if not episodes:
        raise NoEpisodes(NO_EPISODES_DETAIL)
    first = episodes[0]

    anime = await session.get(Anime, anime_id)
    anime_status = anime.status if anime is not None else None
    next_airing = anime.next_airing if anime is not None else None
    # The catalogue's own rule, read from there rather than re-derived: a show
    # page that says episode 1 aired last week and an acquisition path that
    # refuses to fetch it is the disagreement that module exists to prevent.
    boundary = aired_through(episodes, now=now, anime_status=anime_status, next_airing=next_airing)
    if not is_aired(first, now=now, anime_status=anime_status, boundary=boundary):
        raise NotAired(NOT_AIRED_DETAIL)

    entry = await session.get(ListEntry, (user_id, anime_id))
    if entry is not None and entry.status in WANTING_STATUSES and not _dormant(entry, anime):
        # The refusal is about the *window*, not about the status: a followed
        # show already has episode 1 covered, so a sample row would only be a
        # second thing to clean up. A **dormant** watching/planned entry
        # (FR-A9) has no window at all, and telling that user "the next
        # episodes are fetched automatically" would be false — so it is allowed
        # through, and behaves exactly like a sample on an unlisted show.
        raise AlreadyFollowing(ALREADY_FOLLOWING_DETAIL)

    want = await session.get(Want, (user_id, first.id))
    if want is None:
        want = Want(user_id=user_id, episode_id=first.id, sample=True)
        session.add(want)
    else:
        want.sample = True
        want.dropped_at = None
        want.drop_reason = None
    await session.flush()

    # Act on the episode now, with the reconciler's own rule, so the show page
    # tells the truth the moment it re-reads. ``retry_now`` because an
    # explicit request is exactly the case UNAVAILABLE_RETRY should not gate.
    started, _ = await start_search(session, first, now=now, retry_now=True)
    # And ask TMDB for the show's pictures, because the episode about to arrive
    # is a 16:9 card and its still would otherwise turn up with the nightly
    # sweep — or whenever the viewer next happened to open Watch Now (owner,
    # 2026-09-13). Gated and deduplicated inside the helper, a no-op without a
    # TMDB key, and never able to fail the request: nothing here calls TMDB.
    await enqueue_show_enrichment(session, anime_id, settings=settings)
    await enqueue_compute_wants(session)
    await session.flush()
    log.info(
        "sample requested",
        extra={
            "user_id": user_id,
            "anime_id": anime_id,
            "episode_id": first.id,
            "started": started,
        },
    )
    return Sample(want=want, episode=first)


async def cancel_sample(
    session: AsyncSession, *, user_id: int, anime_id: int, now: datetime
) -> bool:
    """Drop this user's live sample wants on ``anime_id``; ``False`` if none.

    Every live sample row on the show, not the first one: one press of Cancel
    is the user saying they are not interested, and a row this function left
    behind would keep an episode wanted with nothing in the UI to close it.
    :func:`request_sample` keeps there being only one in practice; this does not
    rely on that.

    The episode is dealt with here rather than at the next tick, through the
    reconciler's own two rules: one sitting in
    ``wanted``/``searching``/``unavailable`` that nobody else wants goes back to
    ``not_wanted`` (``release_if_unwanted``), and one that is *downloading* for
    nobody else is cancelled — the torrent and its partial files removed by a
    ``qbit_cancel`` job (``cancel_if_unwanted``, owner 2026-09-13). Pressing
    Cancel while the download is running used to leave it running to the end
    and transcoding for nobody; it now stops, and the release is not barred
    from being chosen again if the user changes their mind. An episode another
    user still wants is untouched either way (FR-A2). The want rows themselves
    are kept, dropped, as retention's anchor.
    """
    wants = list(
        (
            await session.scalars(
                select(Want)
                .join(Episode, Episode.id == Want.episode_id)
                .where(
                    Want.user_id == user_id,
                    Episode.anime_id == anime_id,
                    Want.sample.is_(True),
                    Want.dropped_at.is_(None),
                )
                .order_by(Episode.number)
            )
        ).all()
    )
    if not wants:
        return False

    for want in wants:
        want.dropped_at = now
        want.drop_reason = SAMPLE_CANCELLED_REASON
    await session.flush()

    # Undo each episode straight away where nothing else wants it, so the row
    # reads "Not fetched" as soon as the page re-reads rather than at the next
    # tick. Both of these are the reconciler's own rules, so an episode another
    # user wants — or one whose bytes have landed, which is retention's — is
    # left exactly as it is.
    released = cancelled = 0
    for want in wants:
        episode = await session.get(Episode, want.episode_id)
        if episode is None:
            continue
        if await release_if_unwanted(session, episode):
            released += 1
        elif await cancel_if_unwanted(session, episode):
            cancelled += 1
            await session.flush()
            await enqueue_cancel(session, episode.id)
    await enqueue_compute_wants(session)
    await session.flush()
    log.info(
        "sample cancelled",
        extra={
            "user_id": user_id,
            "anime_id": anime_id,
            "episode_ids": [want.episode_id for want in wants],
            "released": released,
            "cancelled": cancelled,
        },
    )
    return True


async def sample_for(session: AsyncSession, *, user_id: int, anime_id: int) -> Want | None:
    """This user's live sample want on this show, for the show page's response.

    The lowest-numbered one if there were somehow two, so that the page and
    :func:`request_sample` always name the same row.
    """
    return (
        await session.execute(
            select(Want)
            .join(Episode, Episode.id == Want.episode_id)
            .where(
                Want.user_id == user_id,
                Episode.anime_id == anime_id,
                Want.sample.is_(True),
                Want.dropped_at.is_(None),
            )
            .order_by(Episode.number)
            .limit(1)
        )
    ).scalar_one_or_none()


__all__ = [
    "ALREADY_FOLLOWING_DETAIL",
    "NOT_AIRED_DETAIL",
    "NO_EPISODES_DETAIL",
    "SAMPLE_CANCELLED_REASON",
    "AlreadyFollowing",
    "NoEpisodes",
    "NotAired",
    "Sample",
    "SampleError",
    "cancel_sample",
    "request_sample",
    "sample_for",
]
