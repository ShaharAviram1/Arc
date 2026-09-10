"""New in the franchises the user already follows (§5.6, FR-R2).

A second, smaller section beside the model's picks, and **no model is called
for it**. That is the point: "season two of the show you finished last week is
out" needs no argument, and asking a language model to write one produces four
sentences of padding around a fact the user could have been told directly.
Everything here is a database read plus, at most, a few catalogue lookups.

It exists because the main pool now *excludes* these. A sequel scores well on
every signal the pool has — same genres, related to a show they loved — and
would crowd out the discoveries the page is actually for, while being the one
recommendation that does not need discovering. Splitting them lets each half
do its job: the model argues about strangers, this lists what is new at home.

The rules, in the order they matter:

* Sources are shows the user is **watching, has completed, or has planned** —
  the three states that mean "this franchise is mine". Dropped and on-hold are
  excluded: a sequel to something abandoned is not news.
* Relations counted are ``SEQUEL``, ``SIDE_STORY``, ``SPIN_OFF`` and
  ``ALTERNATIVE``, plus **anything in MOVIE format**, which is how a franchise
  film is usually related. ``PREQUEL``, ``PARENT`` and ``SUMMARY`` are
  deliberately absent: those point backwards or sideways at something older,
  and are excluded from the main pool without being worth surfacing here.
* Anything already on the user's list is dropped — including ``planned``,
  unlike the main pool. The main pool keeps planned shows because a nudge to
  start one is useful; here it would be "the sequel you already know about".
* Recaps and specials go, by the same title patterns the pool uses.
* Ordered by the source show's score, then by air date, newest first: the
  sequel to the show they gave a 10 comes before the spin-off of the one they
  merely finished.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, ListEntry, ListStatus
from arc.services.catalog import CatalogService, preferred_title
from arc.services.recs.pool import (
    ALLOWED_FORMATS,
    RelationBudget,
    is_recap,
    relation_key,
    resolve_relations,
)

log = logging.getLogger(__name__)

#: How many are shown. Eight is enough for a section that sits under the picks
#: without becoming the page.
MAX_CONTINUATIONS = 8

#: Relation types worth surfacing. Forward-looking only: a ``PREQUEL`` or a
#: ``PARENT`` is something older than what they already watched, and a
#: ``SUMMARY`` is a recap by another name.
CONTINUATION_TYPES = frozenset({"SEQUEL", "SIDE_STORY", "SPIN_OFF", "ALTERNATIVE"})

#: The list states that make a show "one of theirs".
SOURCE_STATUSES = (ListStatus.WATCHING, ListStatus.COMPLETED, ListStatus.PLANNED)

#: How the source show is described in the ``because`` line, per list state.
_PHRASE = {
    ListStatus.COMPLETED: "which you completed",
    ListStatus.WATCHING: "which you are watching",
    ListStatus.PLANNED: "on your planned list",
}


@dataclass(frozen=True, slots=True)
class Continuation:
    """One new entry in a franchise the user follows."""

    anime_id: int
    title: str
    #: The whole explanation, written here rather than by a model: "Sequel to
    #: Frieren, which you completed".
    because: str

    def as_dict(self) -> dict[str, Any]:
        """The form stored in ``rec_runs.picks`` (tagged ``kind``)."""
        return {
            "kind": "continuation",
            "anime_id": self.anime_id,
            "title": self.title,
            "because": self.because,
        }


def _relation_kind(relation: dict[str, Any]) -> str | None:
    """What this relation is called in the ``because`` line, or ``None``.

    ``None`` means "not a continuation": either a relation type that points
    backwards, or a format nobody would be told about.
    """
    kind = str(relation.get("relation_type") or "").upper()
    fmt = str(relation.get("format") or "").upper()
    if fmt == "MOVIE":
        return "Movie in the {title} series"
    if kind == "SEQUEL":
        return "Sequel to {title}"
    if kind == "SIDE_STORY":
        return "Side story to {title}"
    if kind == "SPIN_OFF":
        return "Spin-off of {title}"
    if kind == "ALTERNATIVE":
        return "Alternative version of {title}"
    return None


def _because(template: str, *, source_title: str, status: ListStatus) -> str:
    """ "Sequel to X, which you completed" / "Movie in the X series (on your
    planned list)".

    Planned reads as a parenthetical because the other two are relative
    clauses and "which is on your planned list" is a mouthful for a card.
    """
    head = template.format(title=source_title)
    phrase = _PHRASE[status]
    if status is ListStatus.PLANNED:
        return f"{head} ({phrase})"
    return f"{head}, {phrase}"


def _sources(rows: Sequence[tuple[Anime, ListEntry]]) -> list[tuple[Anime, ListEntry]]:
    """The listed shows worth following, best first.

    Score descending so a franchise the user rated highly contributes before
    one they merely finished; ``anime.id`` breaks ties so the section is stable
    between two runs.
    """
    mine = [(a, e) for a, e in rows if e.status in SOURCE_STATUSES]
    return sorted(mine, key=lambda pair: (-(pair[1].score or 0), pair[0].id))


#: Anime seasons in calendar order, for the air-date sort below.
_SEASON_ORDER = {"WINTER": 0, "SPRING": 1, "SUMMER": 2, "FALL": 3}


def _aired_key(anime: Anime) -> tuple[int, int]:
    """How recent a title is, for ordering. Unknown sorts oldest.

    ``anime`` has no start-date column — the catalogue stores a season and a
    year, which is the granularity both sources agree on — so "air date" here
    means the season it started in. That is precise enough for a section whose
    job is "newest first" among a handful of franchise entries.
    """
    year = anime.season_year or 0
    return (year, _SEASON_ORDER.get((anime.season or "").upper(), 0))


async def build_continuations(
    session: AsyncSession,
    catalog: CatalogService,
    *,
    rows: Sequence[tuple[Anime, ListEntry]],
    limit: int = MAX_CONTINUATIONS,
    budget: RelationBudget | None = None,
) -> list[Continuation]:
    """Up to ``limit`` new entries in the user's own franchises.

    Shares :func:`arc.services.recs.pool.resolve_relations` with the pool, and
    — when the caller passes the run's ``budget`` — the *same* allowance of ten
    fetches and five seconds, spent between them rather than twice over. The
    pool is served first, so this section may find the budget already gone; a
    title that cannot be resolved is skipped rather than failing the run.
    """
    sources = _sources(rows)
    if not sources:
        return []

    listed = {anime.id for anime, _ in rows}
    # ``(relation, the source's score, the finished sentence)``. Built in
    # source order so the resolver sees the best franchises first and spends
    # its ten fetches on them.
    wanted: list[tuple[dict[str, Any], int, str]] = []
    for anime, entry in sources:
        source_title = preferred_title(anime)
        for relation in anime.relations or ():
            if not isinstance(relation, dict):
                continue
            template = _relation_kind(relation)
            if template is None:
                continue
            wanted.append(
                (
                    relation,
                    entry.score or 0,
                    _because(template, source_title=source_title, status=entry.status),
                )
            )

    if not wanted:
        return []

    resolved = await resolve_relations(
        session, catalog, [relation for relation, _, _ in wanted], budget=budget
    )

    found: list[tuple[int, tuple[int, int], Continuation]] = []
    seen: set[int] = set()
    for relation, score, because in wanted:
        row = resolved.get(relation_key(relation))
        if row is None or row.id in listed or row.id in seen:
            continue
        if not row.format or row.format.upper() not in ALLOWED_FORMATS:
            continue
        title = preferred_title(row)
        if is_recap(title):
            continue
        seen.add(row.id)
        found.append(
            (score, _aired_key(row), Continuation(anime_id=row.id, title=title, because=because))
        )

    # The source's score first — a sequel to a 10 outranks a spin-off of a 6 —
    # then the newest, then the id so two runs agree.
    found.sort(key=lambda item: (-item[0], tuple(-part for part in item[1]), item[2].anime_id))
    log.info(
        "continuations built",
        extra={"count": min(len(found), limit), "sources": len(sources)},
    )
    return [item[2] for item in found[:limit]]


__all__ = [
    "CONTINUATION_TYPES",
    "MAX_CONTINUATIONS",
    "SOURCE_STATUSES",
    "Continuation",
    "build_continuations",
]
