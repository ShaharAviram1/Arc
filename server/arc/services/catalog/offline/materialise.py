"""Turning an ``offline_anime`` row into an ``anime`` row (M15.5, FR-C1).

A card links to ``/anime/{id}``, and that id is Arc's own: the offline
catalogue has no ids anything outside it can address, and its surrogate key is
replaced whole every week. So an offline hit that is going to be shown to
somebody has to become an ``anime`` row first, exactly as a live search hit
does (``arc/api/anime.py``).

The conversion is deliberately flat-footed. **No heuristics about titles**:
manami publishes one ``title`` and a list of synonyms, with no statement about
which language any of them is in, so the title goes to ``title_romaji`` and
``title_english`` is left null for a live source to fill. Guessing which
synonym is the English one is how a show ends up labelled in Ukrainian.

Two things this module does *not* do, and both are rule 3
(:mod:`arc.services.catalog.cache`):

* it writes as the **weakest** source (:data:`~…source.OFFLINE`), so every
  column it fills is filled only where AniList and MAL have left a null, and
  the first live payload for the same show overwrites all of it;
* it never sets ``refreshed_at`` or ``detail_source``. The offline record is a
  *summary* — one title, a cover, an episode count, no synopsis, no genres, no
  relations, no schedule — so a row materialised here is still a row that has
  never been fetched, and opening it still triggers a full fetch. Marking it
  refreshed would mean a show found during an outage never got its real
  details once the outage ended.

``synonyms`` and ``studio`` are the exception to that second point, and are
written by :func:`_fill_detail_nulls` rather than by the upsert: they are
*detail* columns that the offline record genuinely knows (the synonym list is
the reason the dataset is worth importing at all — thirty spellings where
AniList gives three), and filling a null in one is what lets the local search
and the filename matcher find the show by a name nobody has cached yet. They
are filled only where there is nothing, and they still leave the row unrefreshed.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, OfflineAnime
from arc.services.catalog.cache import upsert_summaries
from arc.services.catalog.source import OFFLINE, CatalogMedia, MediaTitle

#: manami's ``status`` vocabulary in AniList's, which is what ``anime.status``
#: holds and what the client renders. ``UNKNOWN`` maps to null rather than to a
#: fourth string: "nobody knows" is what a null column already means.
STATUS = {
    "FINISHED": "FINISHED",
    "ONGOING": "RELEASING",
    "UPCOMING": "NOT_YET_RELEASED",
}

#: The season string manami uses for "no season", which is a null here for the
#: same reason as ``UNKNOWN`` above — and because the schedule groups by
#: ``season``/``season_year`` and would otherwise grow an ``UNDEFINED`` season.
UNDEFINED_SEASON = "UNDEFINED"

#: The type manami gives an entry it cannot classify. ``anime.format`` is
#: rendered on a card, so it is left null rather than shown as "UNKNOWN".
UNKNOWN_TYPE = "UNKNOWN"

#: manami's score is 0–10; ``anime.average_score`` is 0–100.
SCORE_SCALE = 10


def _studio(row: OfflineAnime) -> str | None:
    """The first studio, capitalised when the dataset has lowercased it.

    manami normalises every studio name to lower case (``"madhouse inc."``),
    and ``anime.studio`` is rendered as-is on the show page. Title-casing is
    applied **only** to a name that is entirely lower case, so a name that
    carries its own capitals (``"ufotable"`` does not; ``"STUDIO4°C"`` would)
    is passed through untouched rather than re-cased into something wrong.
    """
    studios = [str(name).strip() for name in (row.studios or []) if str(name).strip()]
    if not studios:
        return None
    name = studios[0]
    return name.title() if name == name.lower() else name


def media_from(row: OfflineAnime) -> CatalogMedia:
    """One ``offline_anime`` row as the summary payload the cache speaks.

    ``episodes = 0`` becomes null: the dataset uses it for an entry whose count
    nobody has announced yet, and a literal zero would read as "this show has
    no episodes" — which, on a new row, is 0 episode rows and 0 wants.
    """
    return CatalogMedia(
        source=OFFLINE,
        title=MediaTitle(romaji=row.title),
        anilist_id=row.anilist_id,
        mal_id=row.mal_id,
        format=None if row.type == UNKNOWN_TYPE else row.type,
        episodes=row.episodes or None,
        status=STATUS.get(row.status or ""),
        season=None if row.season == UNDEFINED_SEASON else row.season,
        season_year=row.season_year,
        cover_url=row.picture,
        average_score=None if row.score is None else round(row.score * SCORE_SCALE),
        synonyms=[str(name) for name in (row.synonyms or []) if name],
        studio=_studio(row),
        full=False,
    )


async def _fill_detail_nulls(
    session: AsyncSession, rows: Sequence[Anime], media: Sequence[CatalogMedia]
) -> None:
    """Write ``synonyms`` and ``studio`` where the row has none (see the module docstring).

    Keyed by external id rather than by position: the upsert collapses
    duplicates and drops payloads carrying no id at all, so its answer is not
    one row per payload.
    """
    by_anilist = {item.anilist_id: item for item in media if item.anilist_id is not None}
    by_mal = {item.mal_id: item for item in media if item.mal_id is not None}
    for row in rows:
        found = by_anilist.get(row.anilist_id) if row.anilist_id is not None else None
        if found is None and row.mal_id is not None:
            found = by_mal.get(row.mal_id)
        if found is None:
            continue
        if row.synonyms is None and found.synonyms:
            row.synonyms = list(found.synonyms)
        if row.studio is None and found.studio is not None:
            row.studio = found.studio
    await session.flush()


async def upsert_offline_summaries(
    session: AsyncSession, rows: Sequence[OfflineAnime]
) -> list[Anime]:
    """Materialise offline rows as ``anime`` rows and return them, in order.

    Rows carrying neither an AniList nor a MAL id are dropped by the upsert —
    manami has a few thousand of them, and a row no source can ever be asked
    about is a card that leads nowhere.
    """
    media = [media_from(row) for row in rows]
    anime = await upsert_summaries(session, media)
    await _fill_detail_nulls(session, anime, media)
    return anime


__all__ = ["SCORE_SCALE", "STATUS", "media_from", "upsert_offline_summaries"]
