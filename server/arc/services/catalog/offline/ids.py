"""The offline cross-id map: AniList ↔ MAL, without asking anybody (FR-C6).

One show is one ``anime`` row whichever source found it, and the id that makes
that true has to come from somewhere. Until M15.5 it came from AniList: a row
created by a MAL search carried no ``anilist_id`` until ``catalog_reconcile``
asked AniList for one — which is exactly the thing that does not work during
the outage the fallback exists for.

These two tables already hold the answer. Fribb's ``anime-lists``
(``offline_ids``) is a purpose-built id map, and manami's entries
(``offline_anime``) carry the ids parsed out of their ``sources`` URLs. So the
map is consulted first and the network second, and a MAL-found show lands on
the row AniList made last week with no request at all.

Two rules, and both are about not merging two shows by accident.

**Ambiguity is a miss.** Neither file is unique-keyed — they are somebody
else's, and a duplicate in one is a duplicate row rather than a failed import
(``arc/models/offline.py``). When the rows disagree about which AniList id a
MAL id maps to, this returns ``None``: attaching one of two answers would point
a user's list entries and episode rows at the wrong show, and "no id yet" is a
state Arc already handles.

**Fribb wins where both speak.** It is the file whose whole purpose is the
mapping; manami's ids are a by-product of its ``sources`` list and are the
fallback for the entries Fribb has never heard of.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from arc.models import OfflineAnime, OfflineId

#: ``(lookup column, answer column)`` per table, Fribb first. The order is the
#: precedence rule: manami is only asked about ids Fribb could not place.
_MAL_TO_ANILIST: tuple[tuple[InstrumentedAttribute[int | None], ...], ...] = (
    (OfflineId.mal_id, OfflineId.anilist_id),
    (OfflineAnime.mal_id, OfflineAnime.anilist_id),
)
_ANILIST_TO_MAL: tuple[tuple[InstrumentedAttribute[int | None], ...], ...] = (
    (OfflineId.anilist_id, OfflineId.mal_id),
    (OfflineAnime.anilist_id, OfflineAnime.mal_id),
)


async def lookup_ids(
    session: AsyncSession, *, anilist_id: int | None = None, mal_id: int | None = None
) -> OfflineId | None:
    """The ``offline_ids`` row for whichever id the caller has.

    The AniList id is tried first when both are given — it is the more
    specific of the two in practice, because Fribb lists entries with a MAL id
    and no AniList one far more often than the reverse.

    Returns ``None`` when the map has no row, and also when it has more than
    one: see the module docstring on ambiguity. This is the row the TMDB
    enrichment reads (M15.5 bullet 4), which is why it hands back the whole
    record rather than one id.
    """
    for column, value in ((OfflineId.anilist_id, anilist_id), (OfflineId.mal_id, mal_id)):
        if value is None:
            continue
        statement = select(OfflineId).where(column == value).limit(2)
        rows = list((await session.scalars(statement)).all())
        if len(rows) == 1:
            return rows[0]
        if rows:
            # Two entries claiming one id: no answer beats the first one the
            # planner happened to return.
            return None
    return None


async def _map_ids(
    session: AsyncSession,
    known: Iterable[int],
    tables: tuple[tuple[InstrumentedAttribute[int | None], ...], ...],
) -> dict[int, int]:
    """``{known id → mapped id}``, from each table in ``tables`` in turn.

    One query per table for the whole batch rather than one per id: this runs
    on the search path, where the batch is a page of results.

    A value two rows disagree about is dropped rather than guessed, and a later
    table only ever fills in an id an earlier one had nothing to say about.
    """
    pending = [value for value in dict.fromkeys(known) if value is not None]
    if not pending:
        return {}

    found: dict[int, int] = {}
    for lookup, answer in tables:
        if not pending:
            break
        rows = await session.execute(
            select(lookup, answer).where(lookup.in_(pending), answer.is_not(None))
        )
        pairs: dict[int, int | None] = {}
        for key, value in rows.all():
            if key in pairs and pairs[key] != value:
                pairs[key] = None  # two answers; see the module docstring
                continue
            pairs.setdefault(int(key), int(value))
        for key, value in pairs.items():
            if value is not None:
                found[key] = value
        pending = [value for value in pending if value not in found]
    return found


async def anilist_ids_for(session: AsyncSession, mal_ids: Iterable[int]) -> dict[int, int]:
    """``{mal_id → anilist_id}`` for every MAL id the map can place."""
    return await _map_ids(session, mal_ids, _MAL_TO_ANILIST)


async def mal_ids_for(session: AsyncSession, anilist_ids: Iterable[int]) -> dict[int, int]:
    """``{anilist_id → mal_id}`` for every AniList id the map can place."""
    return await _map_ids(session, anilist_ids, _ANILIST_TO_MAL)


async def anilist_for_mal(session: AsyncSession, mal_id: int) -> int | None:
    """The AniList id of the show MAL calls ``mal_id``, if the map knows it."""
    return (await anilist_ids_for(session, [mal_id])).get(mal_id)


async def mal_for_anilist(session: AsyncSession, anilist_id: int) -> int | None:
    """The MAL id of the show AniList calls ``anilist_id``, if the map knows it."""
    return (await mal_ids_for(session, [anilist_id])).get(anilist_id)


async def fill_missing_ids(
    session: AsyncSession, pairs: Sequence[tuple[int | None, int | None]]
) -> list[tuple[int | None, int | None]]:
    """``(anilist_id, mal_id)`` for each payload, with the gaps filled in.

    The shape the cache needs (:func:`arc.services.catalog.cache._upsert`): a
    payload carrying one id is looked up in the map so the row lookup that
    follows finds the row the *other* source created. A payload that already
    has both, or neither, costs nothing — the queries only run when something
    is actually missing, which on the ordinary AniList path is never, since its
    search results carry ``idMal``.

    Never overwrites an id a payload already has. A source's own statement
    about itself outranks a third party's file, and a disagreement between them
    is a thing to leave alone rather than to act on.
    """
    missing_mal = [anilist for anilist, mal in pairs if anilist is not None and mal is None]
    missing_anilist = [mal for anilist, mal in pairs if mal is not None and anilist is None]
    if not missing_mal and not missing_anilist:
        return list(pairs)

    by_anilist = await mal_ids_for(session, missing_mal) if missing_mal else {}
    by_mal = await anilist_ids_for(session, missing_anilist) if missing_anilist else {}

    filled: list[tuple[int | None, int | None]] = []
    for anilist, mal in pairs:
        if anilist is not None and mal is None:
            filled.append((anilist, by_anilist.get(anilist)))
        elif mal is not None and anilist is None:
            filled.append((by_mal.get(mal), mal))
        else:
            filled.append((anilist, mal))
    return filled


__all__ = [
    "anilist_for_mal",
    "anilist_ids_for",
    "fill_missing_ids",
    "lookup_ids",
    "mal_for_anilist",
    "mal_ids_for",
]
