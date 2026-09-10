"""One recommendation run, end to end (FR-R2 … FR-R5, §5.6).

Rate limit, history, pool, call, validate, persist — in that order, and the
order matters: the rate limit is checked before anything is built, so a user
who has spent their ten runs pays nothing for the eleventh.

Two decisions are worth stating plainly.

**A pick is a claim, not a result.** :func:`validate_picks` drops anything the
model returned that is not in the pool it was given, and anything the user has
already watched. The model has both facts in its prompt; that it usually
respects them is not a reason to trust it when it does not. A run that comes
back with two survivors is stored with two — the model is not asked again,
because a second call on the same prompt is another 30 seconds and another
chance at the same mistake, and three-of-five is a page worth showing.

**Ten runs a day, counted from the table.** FR-R5's limit needs no new column
and no in-memory state: ``rec_runs`` already carries ``(user_id, created_at)``
and is already indexed on it. The wait is computed from the oldest run inside
the window, which is the moment the budget actually frees up.

The count is checked and then acted on without a lock, so a user firing two
requests in the same instant can land an eleventh run. That overshoot is
accepted: the limit exists to bound cost and abuse, one extra run costs a
fraction of a cent, and the alternatives — a row lock held across a
twenty-second model call, or an advisory lock — are each worse than the
problem. It is a budget, not an invariant.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, ListEntry, ListStatus, RecRun
from arc.services.catalog import CatalogService, get_my_list
from arc.services.recs.base import RecsError, RecsModel
from arc.services.recs.continuations import build_continuations
from arc.services.recs.history import summarise
from arc.services.recs.pool import (
    EXCLUDED_STATUSES,
    Candidate,
    RelationBudget,
    build_pool,
)
from arc.services.recs.prompt import SYSTEM_PROMPT, build_user_message
from arc.services.recs.schema import MAX_PICKS, MIN_PICKS, PICKS_SCHEMA, Pick

log = logging.getLogger(__name__)

#: Runs per user per day (FR-R5), and the window they are counted over.
DAILY_LIMIT = 10

#: The two kinds of entry in ``rec_runs.picks``. The column is one JSONB list
#: holding both, tagged, rather than two columns — it needs no migration, and
#: the two are rendered as one page. A row written before the tag existed has
#: no ``kind`` and reads as a pick, which is what it was.
PICK_KIND = "pick"
CONTINUATION_KIND = "continuation"


def is_pick(entry: dict[str, Any]) -> bool:
    """Whether a stored entry is a model pick rather than a continuation."""
    return str(entry.get("kind", PICK_KIND)) == PICK_KIND


#: The width of ``rec_runs.model``. Named here because this is where the value
#: is written, and a mismatch would only show up as a failed insert.
MODEL_COLUMN_CHARS = 64
WINDOW = timedelta(hours=24)


class RecsRateLimited(RecsError):
    """The caller has used their ten runs for the day (→ 429)."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"daily recommendation limit reached; try again in {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class RecsEmptyPool(RecsError):
    """There is nothing left to recommend from (→ 409).

    Either the catalogue cache is empty (a brand-new install before the first
    season sweep) or the user has already put every candidate on their list.
    Both are a message to the user, not an error to log.
    """


# --- Rate limit (FR-R5) -----------------------------------------------------


async def _window_rows(
    session: AsyncSession, *, user_id: int, now: datetime
) -> tuple[int, datetime | None]:
    """``(runs in the last 24 h, when the oldest of them was created)``."""
    since = now - WINDOW
    row = (
        await session.execute(
            select(func.count(RecRun.id), func.min(RecRun.created_at)).where(
                RecRun.user_id == user_id, RecRun.created_at >= since
            )
        )
    ).one()
    return int(row[0] or 0), row[1]


async def remaining_today(session: AsyncSession, *, user_id: int, now: datetime) -> int:
    """How many runs the user has left (FR-R5). Never negative."""
    used, _ = await _window_rows(session, user_id=user_id, now=now)
    return max(0, DAILY_LIMIT - used)


async def _check_rate_limit(session: AsyncSession, *, user_id: int, now: datetime) -> None:
    used, oldest = await _window_rows(session, user_id=user_id, now=now)
    if used < DAILY_LIMIT:
        return
    # The budget frees up when the oldest run in the window leaves it. A null
    # ``oldest`` cannot happen while ``used`` is non-zero, but a whole window
    # is the safe answer if it ever does.
    wait = (oldest + WINDOW - now).total_seconds() if oldest is not None else WINDOW.total_seconds()
    raise RecsRateLimited(max(1, math.ceil(wait)))


# --- Validation -------------------------------------------------------------


def validate_picks(
    picks: Sequence[Pick],
    *,
    candidates: Sequence[Candidate],
    excluded: set[int],
) -> list[dict[str, Any]]:
    """The picks worth storing, in the order the model gave them.

    Four filters, all of them things a model does occasionally: an id that was
    never in the pool, a show the user has already watched (belt and braces —
    the pool excluded it, so this only fires if the model invented the id), the
    same show twice, and more than :data:`MAX_PICKS` of them.

    The stored title is the **candidate's**, not the model's: they agree in
    every good answer, and where they do not, the catalogue is right.
    """
    by_id = {candidate.anime_id: candidate for candidate in candidates}
    kept: list[dict[str, Any]] = []
    seen: set[int] = set()

    for pick in picks:
        if len(kept) >= MAX_PICKS:
            break
        candidate = by_id.get(pick.anime_id)
        if candidate is None:
            log.warning("pick dropped: not in the pool", extra={"anime_id": pick.anime_id})
            continue
        if pick.anime_id in excluded:
            log.warning("pick dropped: already on the list", extra={"anime_id": pick.anime_id})
            continue
        if pick.anime_id in seen:
            continue
        seen.add(pick.anime_id)
        kept.append(
            {
                "kind": PICK_KIND,
                "anime_id": pick.anime_id,
                "title": candidate.title,
                "case": pick.case.strip(),
            }
        )

    return kept


def _excluded_ids(rows: Sequence[tuple[Anime, ListEntry]]) -> set[int]:
    return {anime.id for anime, entry in rows if entry.status in EXCLUDED_STATUSES}


# --- The run ----------------------------------------------------------------


async def run_recommendations(
    session: AsyncSession,
    catalog: CatalogService,
    model: RecsModel,
    *,
    user_id: int,
    prompt: str | None,
    now: datetime,
) -> RecRun:
    """Build a pool, ask the model, keep what survives, store the run.

    Returns the persisted (but not committed) :class:`~arc.models.RecRun`;
    committing is the caller's, so a route can decide the transaction.
    """
    await _check_rate_limit(session, user_id=user_id, now=now)

    rows = await get_my_list(session, user_id=user_id)
    history = summarise(rows)

    # One budget for the whole run, spent between the two things that resolve
    # relations. Separately they would each take ten fetches and five seconds,
    # which is twice what a page a user is waiting for can afford
    # (:class:`~arc.services.recs.pool.RelationBudget`). The pool goes first:
    # forty candidates the model chooses from are worth more than the ninth
    # sequel in a section capped at eight.
    budget = RelationBudget()
    candidates = await build_pool(session, catalog, rows=rows, now=now, budget=budget)
    if not candidates:
        raise RecsEmptyPool("no candidates")

    # Deterministic and unargued, so it never goes near the model: "season two
    # of the show you finished" needs no case written for it (§5.6). Built here
    # rather than after the answer so that everything touching the database and
    # the catalogue happens before the commit, leaving the model call last and
    # alone.
    continuations = await build_continuations(session, catalog, rows=rows, budget=budget)

    # Commit before the model call, and this is the important line in the
    # function. Building the pool and the continuations can insert ``anime``
    # rows (a relation the catalogue had to fetch), which means an open
    # transaction holding row
    # locks — and the next statement waits on a third party for seconds.
    # Holding Postgres locks across that would let one recommendation stall
    # every writer that touches those rows. ``expire_on_commit=False`` (set on
    # the session factory) keeps everything loaded above usable afterwards.
    #
    # The cost is that a fetched relation survives a failed run. That is the
    # right way round: it is cache, the next run reuses it, and a run that
    # fails after paying for four catalogue lookups should not throw them away.
    # "A ``rec_runs`` row exists only on success" still holds — the row is not
    # written until after the picks are validated, below.
    await session.commit()

    result = await model.recommend(
        system=SYSTEM_PROMPT,
        user=build_user_message(prompt=prompt, history=history, candidates=candidates),
        schema=PICKS_SCHEMA,
    )

    picks = validate_picks(result.picks.picks, candidates=candidates, excluded=_excluded_ids(rows))
    if len(picks) < MIN_PICKS:
        # Not retried on purpose: another call is another half-minute on a page
        # somebody is watching, and what survives is still a page.
        log.warning(
            "recommendation run kept fewer picks than asked for",
            extra={
                "user_id": user_id,
                "kept": len(picks),
                "returned": len(result.picks.picks),
                "candidates": len(candidates),
            },
        )

    # Continuations were built above, before the commit; they are stored only
    # now, because a run that the model never answered has no row at all.
    run = RecRun(
        user_id=user_id,
        prompt=prompt,
        candidates=[candidate.as_dict() for candidate in candidates],
        picks=picks + [item.as_dict() for item in continuations],
        # ``rec_runs.model`` is String(64). Every real id is far shorter, but
        # the value comes from a third party and a long one would fail the
        # insert *after* the model has already been paid for.
        model=(result.model or "")[:MODEL_COLUMN_CHARS] or None,
        created_at=now,
    )
    session.add(run)
    await session.flush()
    # The provider is not a column — ``rec_runs`` predates the chain and this
    # needs no migration — so it goes in the log, where "which provider served
    # today's runs" is the question an operator actually asks.
    log.info(
        "recommendation run stored",
        extra={
            "user_id": user_id,
            "run_id": run.id,
            "provider": result.provider,
            "model": run.model,
            "picks": len(picks),
            "continuations": len(continuations),
            "candidates": len(candidates),
        },
    )
    return run


async def latest_run(session: AsyncSession, *, user_id: int) -> RecRun | None:
    """The user's newest run, or ``None`` (FR-R5: the page is instant on reload)."""
    return (
        (
            await session.execute(
                select(RecRun)
                .where(RecRun.user_id == user_id)
                .order_by(RecRun.created_at.desc(), RecRun.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def anime_for_picks(
    session: AsyncSession, run: RecRun | None
) -> tuple[dict[int, Anime], dict[int, ListStatus]]:
    """The anime rows a run's entries name, and the caller's status for each.

    Returned as two maps because a pick whose row has since been pruned is
    dropped when the run is serialised, and the router needs to know which.
    """
    if run is None or not run.picks:
        return {}, {}
    ids = [int(pick["anime_id"]) for pick in run.picks if pick.get("anime_id") is not None]
    if not ids:
        return {}, {}
    rows = (await session.execute(select(Anime).where(Anime.id.in_(ids)))).scalars().all()
    statuses = (
        await session.execute(
            select(ListEntry.anime_id, ListEntry.status).where(
                ListEntry.user_id == run.user_id, ListEntry.anime_id.in_(ids)
            )
        )
    ).all()
    return {row.id: row for row in rows}, {anime_id: status for anime_id, status in statuses}


__all__ = [
    "CONTINUATION_KIND",
    "DAILY_LIMIT",
    "MAX_PICKS",
    "MIN_PICKS",
    "WINDOW",
    "RecsEmptyPool",
    "RecsRateLimited",
    "PICK_KIND",
    "anime_for_picks",
    "is_pick",
    "latest_run",
    "remaining_today",
    "run_recommendations",
    "validate_picks",
]
