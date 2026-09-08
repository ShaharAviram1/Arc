"""The MAL job names and the one function allowed to queue a write.

Split from :mod:`arc.services.mal.jobs` for the reason every ``names`` module
in Arc is: the list service, the playback service and the API all need to
*name* a job, the worker needs to *run* it, and importing the handlers from a
request path would drag the job registry and an HTTP client into it.

Here that split does a second, larger job. :func:`enqueue_mal_push` and
:func:`arc.services.mal.writelog.record_pending` are together the **only** way
a MAL write is ever queued, which is what makes spec §4.7's FR-M7 — "there is
no code path that writes to MAL except via a user-originated event or an
explicit revert" — a property that can be *checked* rather than merely
believed: ``tests/test_mal_guard.py`` walks the source tree for calls to them
and fails if the set of callers is not exactly

* :mod:`arc.services.catalog.lists` — an explicit list edit or removal (manual)
* :mod:`arc.services.playback.progress` — a watch completion (watch)
* :mod:`arc.api.mal` — an explicit revert (revert)

Nothing else may call it, and nothing else needs to: import never queues a
write, and the "push everything dirty" button queues :data:`PUSH_ALL`, which
pushes changes that were already queued by one of the three above.

The function also carries the **is this user linked** check, so the three
callers do not each have to remember it. An unlinked user's list edits still
set ``mal_dirty``; they simply queue nothing until the account is linked, at
which point the import decides who wins (FR-M2, FR-M3).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Job, MalLink
from arc.services.jobs.queue import enqueue

#: Import one user's whole MAL list (FR-M2, FR-M3). Queued when an account is
#: linked, by the six-hourly sweep, and by ``POST /api/mal/import``.
IMPORT = "mal_import"

#: The six-hourly sweep itself: queue an :data:`IMPORT` for every linked user.
IMPORT_ALL = "mal_import_all"

#: Push one dirty (user, anime) entry to MAL (FR-M4). One job per pair.
PUSH = "mal_push"

#: Push every dirty entry of one user — the "sync now" button, and the way a
#: batch of failures is retried by hand (FR-M6).
PUSH_ALL = "mal_push_all"

#: How many times a push is attempted before it is left failed. Five with the
#: queue's exponential backoff spans a little over an hour, which covers a
#: MyAnimeList blip without hammering it (FR-M6).
PUSH_MAX_ATTEMPTS = 5

# --- Queue priorities (lower runs first; the default is 100) ----------------

#: A push is the only job in Arc that a *person* is waiting on: they changed a
#: status or finished an episode, and MyAnimeList not knowing about it is the
#: one failure they can see from outside Arc. It is also the cheapest job there
#: is — one HTTP call — so nothing loses much by letting it through first. Ten
#: rather than zero leaves room to put something in front of it later without a
#: renumbering.
PUSH_PRIORITY = 10

#: An import is the opposite: hundreds of rows, up to fifty paced catalogue
#: lookups, and nobody watching. It goes behind everything, including the
#: acquisition work it is quite likely to create.
IMPORT_PRIORITY = 200


def import_dedupe_key(user_id: int) -> str:
    """One queued import per user; a second request returns the first."""
    return f"{IMPORT}:{user_id}"


def push_dedupe_key(user_id: int, anime_id: int, *, delete: bool = False) -> str:
    """One queued push per (user, anime) — and a separate one for a removal.

    A removal gets its own key rather than sharing the pair's: if it shared,
    then "set my score, then take the show off my list" would find the score's
    job already pending, return it, and never send the ``DELETE`` at all.
    """
    verb = "delete" if delete else "push"
    return f"{PUSH}:{verb}:{user_id}:{anime_id}"


def push_all_dedupe_key(user_id: int) -> str:
    """One queued full push per user."""
    return f"{PUSH_ALL}:{user_id}"


async def is_linked(session: AsyncSession, user_id: int) -> bool:
    """Whether this user has a MAL account attached.

    True even when the link needs re-authorising: the change is still owed to
    MyAnimeList, the job will fail with a clear error, and the write log is
    where the user sees why (:mod:`arc.services.mal.writelog`). Dropping the
    work silently would lose the change instead.
    """
    return await session.get(MalLink, user_id) is not None


async def enqueue_mal_push(
    session: AsyncSession,
    *,
    user_id: int,
    anime_id: int,
    delete: bool = False,
) -> Job | None:
    """Nudge the queue to send this pair's queued writes (FR-M7).

    The job carries the pair and nothing else. What is to be written, and why,
    is in the ``pending`` ``mal_write_log`` rows the caller wrote in this same
    transaction (:func:`arc.services.mal.writelog.record_pending`) — which is
    what makes deduplication safe: two events for one show find one job, and
    the second event's rows are picked up by it regardless, because the rows
    are the state and the job is only a nudge. A cause on the *job* would be
    whichever event queued it first, and the FR-M4 guards would then be applied
    to the wrong event's fields.

    Returns ``None`` — queueing nothing — when the user has no MAL link.
    Flushed, not committed: the caller's transaction is what makes the change
    and its consequence atomic, or neither.
    """
    if not await is_linked(session, user_id):
        return None
    return await enqueue(
        session,
        PUSH,
        {"user_id": user_id, "anime_id": anime_id, "delete": delete},
        priority=PUSH_PRIORITY,
        max_attempts=PUSH_MAX_ATTEMPTS,
        dedupe_key=push_dedupe_key(user_id, anime_id, delete=delete),
    )


async def enqueue_mal_import(session: AsyncSession, *, user_id: int) -> Job:
    """Queue a full import for one user (FR-M2). Never writes to MAL."""
    return await enqueue(
        session,
        IMPORT,
        {"user_id": user_id},
        priority=IMPORT_PRIORITY,
        dedupe_key=import_dedupe_key(user_id),
    )


async def enqueue_mal_push_all(session: AsyncSession, *, user_id: int) -> Job:
    """Queue "send everything I still owe MyAnimeList" for one user.

    Not itself a write cause: it re-attempts changes that were already made in
    Arc by one of the three events above and are still marked ``mal_dirty``.
    """
    return await enqueue(
        session,
        PUSH_ALL,
        {"user_id": user_id},
        priority=PUSH_PRIORITY,
        max_attempts=PUSH_MAX_ATTEMPTS,
        dedupe_key=push_all_dedupe_key(user_id),
    )


__all__ = [
    "IMPORT",
    "IMPORT_ALL",
    "IMPORT_PRIORITY",
    "PUSH",
    "PUSH_ALL",
    "PUSH_MAX_ATTEMPTS",
    "PUSH_PRIORITY",
    "enqueue_mal_import",
    "enqueue_mal_push",
    "enqueue_mal_push_all",
    "import_dedupe_key",
    "is_linked",
    "push_all_dedupe_key",
    "push_dedupe_key",
]
