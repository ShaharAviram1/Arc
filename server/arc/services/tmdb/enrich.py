"""What TMDB is allowed to write, and onto which season (M15.5, FR-C6).

The whole of the enrichment rule is cache rule 3
(:mod:`arc.services.catalog.cache`) read one source further down: **a weaker
source fills columns that are null and touches nothing else.** TMDB is the
weakest source Arc has — it is reached by id through a weekly snapshot of
somebody else's cross-id map, and it knows nothing about anime as anime — so it
never overwrites anything. In particular:

* ``banner_url`` and ``cover_large_url`` are filled only where they are null.
  Both are AniList's columns when AniList has answered, and a backdrop
  replacing published key art is exactly the mistake rule 3 exists to stop.
  ``cover_url`` is not touched at all: MAL's 230 px cover lives there, TMDB has
  no equivalent, and the two are not interchangeable.
* ``credits`` is filled where there is nothing and *completed* where a weaker
  source left only the studio row. An AniList detail fetch owns the column
  outright: if it wrote credits, TMDB does not touch them, even a one-row list.
  Whoever filled the column is read off ``detail_source``, the same way
  :func:`arc.services.catalog.cache._apply` reads it.
* An episode ``title`` or ``still_url`` is only ever written where there is
  none at all — the rule ``_fill_episode_art`` already applies to AniList's
  ``streamingEpisodes``, restated here because this path writes the rows one
  by one rather than through an ON CONFLICT insert.
* No episode row is ever **created**. Episodes are Arc's (rule 4): the
  catalogue decides how many a show has, and a still is not evidence of an
  episode.

**Which season.** Anime seasons are TMDB seasons of one series far more often
than they are separate series, so an enrichment that ignored the season would
put season 1's stills on season 3's episodes. The id map usually says
(``offline_ids.tmdb_season``); when it does not, :func:`resolve_season` works
it out from the series payload, and when it cannot, the show still gets its
backdrop and poster and no stills at all. Guessing wrong is worse than not
guessing: a wrong still is a picture of the wrong episode on the row somebody
is about to click.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, Episode
from arc.services.catalog.credits import STUDIO_ROLE, credits_from
from arc.services.catalog.source import EpisodeArt
from arc.services.tmdb.client import (
    BACKDROP_SIZE,
    POSTER_SIZE,
    STILL_SIZE,
    image_url,
)

log = logging.getLogger(__name__)

#: The source name TMDB writes under. Not in
#: :data:`arc.services.catalog.source.SOURCE_NAMES`, and deliberately so: that
#: tuple is the order :class:`CatalogService` *asks* its sources in, and TMDB
#: is never asked — it has no titles, no search and no schedule. It is recorded
#: here only so a log line can say where a backdrop came from.
SOURCE = "tmdb"

#: TMDB's crew ``job`` strings, mapped onto the six credits the show page
#: renders (:data:`arc.services.catalog.credits.CREDIT_ORDER`). Its own table
#: rather than AniList's (:func:`arc.services.anilist.extras.credit_role`),
#: because the two sources spell the same jobs differently: TMDB says
#: "Original Music Composer" and "Comic Book" where AniList says "Music" and
#: "Original Story". Both are matched whole for the same reason — "Music
#: Director" is not the director and not the composer.
#:
#: ``Series Director`` is the anime director; plain ``Director`` is what TMDB
#: calls an *episode* director, of which a long show has twenty. Both are
#: mapped, and the episode-count ranking in :func:`crew_credits` is what keeps
#: the right one: the series director is credited on every episode and the
#: others on two.
CREW_JOBS: dict[str, str] = {
    "Series Director": "Director",
    "Director": "Director",
    "Series Composition": "Series Composition",
    "Writer": "Series Composition",
    "Screenplay": "Series Composition",
    "Character Designer": "Character Design",
    "Character Design": "Character Design",
    "Original Music Composer": "Music",
    "Music": "Music",
    "Comic Book": "Original Creator",
    "Novel": "Original Creator",
    "Original Story": "Original Creator",
    "Creator": "Original Creator",
}

#: How many names one credit row may collect. Two, because a character design
#: really is shared and picking one of the pair would be a silent editorial
#: decision — and because TMDB's aggregate crew lists twenty episode directors
#: under ``Director`` and the design has room for neither twenty nor the
#: argument about which three to show.
MAX_PER_ROLE = 2

#: TMDB's season 0 is the specials, which are never an anime's cour.
SPECIALS_SEASON = 0


@dataclass(frozen=True, slots=True)
class TmdbPayloads:
    """The raw responses one enrichment works from.

    ``season`` is ``None`` for a film and for a series whose season could not
    be resolved; ``credits`` is ``None`` when the credits call failed, which is
    not a reason to throw away the art that did arrive.
    """

    #: ``GET /tv/{id}`` or ``GET /movie/{id}``.
    show: dict[str, Any] | None = None
    #: ``GET /tv/{id}/season/{n}``.
    season: dict[str, Any] | None = None
    #: ``GET /tv/{id}/aggregate_credits`` or ``GET /movie/{id}/credits``.
    credits: dict[str, Any] | None = None
    #: Which season ``season`` is, for the log line.
    season_number: int | None = None


@dataclass(frozen=True, slots=True)
class Enrichment:
    """Exactly the writes an enrichment will make. Nothing implicit.

    Every field is "what to write", never "what TMDB said": a backdrop TMDB has
    for a row that already carries one is simply absent here. That is what
    makes the rule testable without a database — :func:`plan_enrichment` is
    where rule 3 lives, and :func:`apply_enrichment` only writes.
    """

    banner_url: str | None = None
    cover_large_url: str | None = None
    credits: list[dict[str, Any]] | None = None
    #: One entry per episode row that gains a title, a still, or both. The
    #: fields inside are already filtered: an entry carries a title only when
    #: the row has none.
    episodes: tuple[EpisodeArt, ...] = field(default=())

    @property
    def empty(self) -> bool:
        """Whether this enrichment would write nothing at all."""
        return (
            self.banner_url is None
            and self.cover_large_url is None
            and self.credits is None
            and not self.episodes
        )

    def columns(self) -> list[str]:
        """The ``anime`` columns this would fill, for a log line."""
        return [
            name
            for name, value in (
                ("banner_url", self.banner_url),
                ("cover_large_url", self.cover_large_url),
                ("credits", self.credits),
            )
            if value is not None
        ]


def _text(value: Any) -> str | None:
    """A trimmed string, or ``None`` for anything empty or not a string."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _year(value: Any) -> int | None:
    """The year of a TMDB ``air_date`` (``"2023-09-29"``), if it has one."""
    text = _text(value)
    if text is None or len(text) < 4 or not text[:4].isdigit():
        return None
    return int(text[:4])


def _seasons(show: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The series' real seasons: specials dropped, malformed entries dropped."""
    return [
        season
        for season in ((show or {}).get("seasons") or [])
        if isinstance(season, dict)
        and isinstance(season.get("season_number"), int)
        and season["season_number"] != SPECIALS_SEASON
    ]


def resolve_season(
    anime: Anime, show: dict[str, Any] | None, *, mapped: int | None = None
) -> int | None:
    """Which TMDB season of ``show`` this Arc row is, or ``None``.

    ``mapped`` is ``offline_ids.tmdb_season`` — Fribb's own answer, and the one
    that is trusted whenever the series actually has that season. It is right
    far more often than anything derived here, because it was written by
    somebody looking at both entries.

    With no mapping the season is worked out from the payload, and both halves
    of the test have to agree:

    1. the season's ``air_date`` year is ``anime.season_year``, which is what
       makes a cour a cour — a Fall 2023 show is the season that started in
       2023, not the one that started in 2021;
    2. among those, the one whose ``episode_count`` is closest to
       ``anime.episodes``, which separates a 12-episode cour from the
       26-episode one that premiered the same year. Ties go to the lower
       season number.

    When no season's year matches there is one last case worth taking: a series
    with exactly one season, whose episode count agrees with Arc's (or where
    Arc has no count to disagree with). That is the ordinary single-cour show
    whose TMDB entry simply has no ``air_date``, and it is safe because there
    is no other season for a still to land on by mistake.

    Anything else returns ``None`` and the show gets art without stills.
    """
    seasons = _seasons(show)
    if not seasons:
        return None

    if mapped is not None and any(season["season_number"] == mapped for season in seasons):
        return int(mapped)

    if anime.season_year is not None:
        dated = [season for season in seasons if _year(season.get("air_date")) == anime.season_year]
        if dated:
            if anime.episodes is None:
                return int(min(season["season_number"] for season in dated))
            best = min(
                dated,
                key=lambda season: (
                    abs(int(season.get("episode_count") or 0) - int(anime.episodes or 0)),
                    int(season["season_number"]),
                ),
            )
            return int(best["season_number"])

    if len(seasons) == 1:
        only = seasons[0]
        count = only.get("episode_count")
        if anime.episodes is None or not isinstance(count, int) or count == anime.episodes:
            return int(only["season_number"])
    return None


def _crew_jobs(member: dict[str, Any]) -> list[tuple[str, int]]:
    """``(job, episode count)`` for one crew member, in both TMDB shapes.

    ``aggregate_credits`` gives a ``jobs`` list with an ``episode_count`` each;
    a film's ``credits`` gives one flat ``job`` and no count. A film has one
    episode's worth of everything, so the missing count reads as 1.
    """
    out: list[tuple[str, int]] = []
    for entry in member.get("jobs") or []:
        if not isinstance(entry, dict):
            continue
        job = _text(entry.get("job"))
        if job is not None:
            count = entry.get("episode_count")
            out.append((job, int(count) if isinstance(count, int) else 1))
    flat = _text(member.get("job"))
    if flat is not None:
        out.append((flat, 1))
    return out


def crew_credits(credits: dict[str, Any] | None) -> list[tuple[str, str]]:
    """A TMDB crew as ``(credit, name)`` pairs, best-credited first.

    Within a role the candidates are ranked by how many episodes they are
    credited on, highest first, and the top :data:`MAX_PER_ROLE` are kept. That
    ranking is the whole reason the aggregate endpoint is the one Arc calls:
    Frieren's crew lists twenty-two people under ``Director`` on one or two
    episodes each and the series director on all thirty-eight, and without the
    count there is no way to tell them apart.

    A job that is not in :data:`CREW_JOBS` is dropped rather than shown: an
    unmapped credit is not a row the design has a slot for. Ordering *between*
    roles is :func:`arc.services.catalog.credits.credits_from`'s job.
    """
    ranked: dict[str, list[tuple[int, int, str]]] = {}
    for position, member in enumerate((credits or {}).get("crew") or []):
        if not isinstance(member, dict):
            continue
        name = _text(member.get("name")) or _text(member.get("original_name"))
        if name is None:
            continue
        for job, episodes in _crew_jobs(member):
            role = CREW_JOBS.get(job)
            if role is None:
                continue
            # Negated so a plain ascending sort is "most episodes first, then
            # TMDB's own order"; the position keeps it deterministic.
            ranked.setdefault(role, []).append((-episodes, position, name))

    out: list[tuple[str, str]] = []
    for role, candidates in ranked.items():
        seen: set[str] = set()
        for _, _, name in sorted(candidates):
            if name in seen:
                continue
            seen.add(name)
            out.append((role, name))
            if len(seen) >= MAX_PER_ROLE:
                break
    return out


def _has_person_credit(credits: Any) -> bool:
    """Whether a stored ``anime.credits`` names anybody other than the studio.

    A MAL-filled row carries exactly one entry, the studio
    (:func:`arc.services.catalog.credits.credits_from` with no staff), and that
    is a hole rather than an answer — it is the case TMDB is here to complete.
    """
    if not isinstance(credits, list):
        return False
    return any(
        isinstance(row, dict) and row.get("role") != STUDIO_ROLE and row.get("name")
        for row in credits
    )


def _may_write_credits(anime: Anime) -> bool:
    """Whether TMDB is allowed to write this row's credits (rule 3).

    AniList owns the column once it has filled it — a one-row AniList answer is
    still AniList's answer, and an entry with no staff listed is a fact about
    the entry. Everything else (MAL, the offline catalogue, a row no detail
    fetch has reached) may be completed, and only while it holds no person.
    """
    if anime.detail_source == "anilist" and anime.credits is not None:
        return False
    return not _has_person_credit(anime.credits)


def episode_art(
    season: dict[str, Any] | None, episodes: Sequence[Episode]
) -> tuple[EpisodeArt, ...]:
    """Titles and stills for the episode rows that have none.

    Matched by number: TMDB's ``episode_number`` against ``episodes.number``.
    An episode TMDB lists that Arc has no row for is skipped — this path never
    creates a row (rule 4) — and a field the row already carries is left out of
    the entry entirely, so the plan says only what it would change.
    """
    by_number = {row.number: row for row in episodes}
    out: list[EpisodeArt] = []
    for entry in (season or {}).get("episodes") or []:
        if not isinstance(entry, dict):
            continue
        number = entry.get("episode_number")
        if not isinstance(number, int):
            continue
        row = by_number.get(number)
        if row is None:
            continue
        title = _text(entry.get("name")) if row.title is None else None
        still = image_url(entry.get("still_path"), STILL_SIZE) if row.still_url is None else None
        if title is None and still is None:
            continue
        out.append(EpisodeArt(number=number, title=title, still_url=still))
    return tuple(out)


def plan_enrichment(
    anime: Anime, episodes: Sequence[Episode], payloads: TmdbPayloads, *, art_only: bool = False
) -> Enrichment:
    """Exactly what TMDB may write onto this row. Pure; touches no session.

    Every decision in this function is "is there a hole here": see the module
    docstring for the rule and for why each column obeys it the way it does.

    ``art_only`` plans the backdrop and the poster and nothing else. It is the
    mode the season sweep runs in (``jobs.py``), where only the show payload
    was fetched — so the credits and the stills would come out empty anyway,
    and saying so here is what makes "one request" a property of the plan
    rather than an accident of what the caller happened to pass.
    """
    show = payloads.show or {}

    banner = (
        image_url(show.get("backdrop_path"), BACKDROP_SIZE) if anime.banner_url is None else None
    )
    poster = (
        image_url(show.get("poster_path"), POSTER_SIZE) if anime.cover_large_url is None else None
    )

    if art_only:
        return Enrichment(banner_url=banner, cover_large_url=poster)

    credits: list[dict[str, Any]] | None = None
    if _may_write_credits(anime):
        staff = crew_credits(payloads.credits)
        if staff:
            # The studio is Arc's own (AniList or MAL): TMDB's
            # ``production_companies`` lists the committee and the distributor
            # as readily as the studio, and a wrong name in the one row a
            # viewer recognises is worse than the row TMDB cannot improve.
            credits = credits_from(anime.studio, staff)

    return Enrichment(
        banner_url=banner,
        cover_large_url=poster,
        credits=credits,
        episodes=episode_art(payloads.season, episodes),
    )


async def apply_enrichment(session: AsyncSession, anime: Anime, plan: Enrichment) -> int:
    """Write ``plan`` and return how many episode rows it touched.

    A plain assignment per column and per row: :func:`plan_enrichment` has
    already decided that each of these is a hole, and re-deciding it in SQL
    would be rule 3 written twice. The episode rows are re-read here rather
    than taken from the caller so that the write lands on objects this session
    owns, whatever the caller was holding.

    Flushes, does not commit — the job runner owns the transaction.
    """
    if plan.banner_url is not None:
        anime.banner_url = plan.banner_url
    if plan.cover_large_url is not None:
        anime.cover_large_url = plan.cover_large_url
    if plan.credits is not None:
        anime.credits = plan.credits

    touched = 0
    if plan.episodes:
        wanted = {art.number: art for art in plan.episodes}
        rows = await session.scalars(
            select(Episode)
            .where(Episode.anime_id == anime.id, Episode.number.in_(sorted(wanted)))
            .execution_options(populate_existing=True)
        )
        for row in rows.all():
            art = wanted[row.number]
            # Guarded again at the write: the plan was made from a read that
            # may be a few hundred milliseconds old, and "only where there is
            # none" has to hold against the row as it is now.
            if art.title is not None and row.title is None:
                row.title = art.title
                touched += 1
            if art.still_url is not None and row.still_url is None:
                row.still_url = art.still_url
                touched += 1

    await session.flush()
    return touched


def summarise(plan: Enrichment) -> dict[str, Any]:
    """A log-ready description of an enrichment: what, not how much."""
    return {"columns": plan.columns(), "episodes": len(plan.episodes)}


__all__ = [
    "CREW_JOBS",
    "MAX_PER_ROLE",
    "SOURCE",
    "SPECIALS_SEASON",
    "Enrichment",
    "TmdbPayloads",
    "apply_enrichment",
    "crew_credits",
    "episode_art",
    "plan_enrichment",
    "resolve_season",
    "summarise",
]
