"""The candidate pool (FR-R2, §5.6).

Everything the model is allowed to say lives in this list, so the tests are
about what is *not* in it as much as what is: nothing the user has watched, is
watching, dropped or put on hold; nothing without a title; no music videos; no
duplicates; and never more than forty.

Postgres-backed, because two of the three sources are queries this code cannot
be honest about against a mock — the season pair lookup and the genre array
overlap are both Postgres features.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, ListEntry, ListStatus
from arc.services.catalog import CatalogMedia, MediaTitle, SourceUnavailable
from arc.services.recs.pool import (
    POOL_CAP,
    RELATION_FETCH_LIMIT,
    SEASON_SHARE,
    SYNOPSIS_CHARS,
    TAG_LIMIT,
    build_pool,
    candidate_of,
    top_genres,
)
from tests.recs_helpers import NOW, anime, entry

pytestmark = pytest.mark.pg

#: ``NOW`` is 4 November 2026 — FALL 2026, so next season is WINTER 2027.
THIS_SEASON = ("FALL", 2026)
NEXT_SEASON = ("WINTER", 2027)

USER_ID = 1


class FakeCatalog:
    """A catalogue that answers relation lookups from a script.

    ``by_anilist_id`` returns whatever is keyed under the id; anything missing
    raises, which is the case the pool has to survive without failing the run.
    """

    def __init__(self, media: dict[int, CatalogMedia] | None = None, *, fail: bool = False) -> None:
        self.media = media or {}
        self.fail = fail
        self.calls = 0

    async def by_anilist_id(self, anilist_id: int) -> CatalogMedia | None:
        self.calls += 1
        if self.fail:
            raise SourceUnavailable("anilist", "down")
        return self.media.get(anilist_id)

    async def by_mal_id(self, mal_id: int) -> CatalogMedia | None:
        self.calls += 1
        if self.fail:
            raise SourceUnavailable("mal", "down")
        return self.media.get(mal_id)


def media(anilist_id: int, title: str, **kwargs: Any) -> CatalogMedia:
    return CatalogMedia(
        source="anilist",
        anilist_id=anilist_id,
        title=MediaTitle(romaji=title),
        format=kwargs.pop("format", "TV"),
        episodes=kwargs.pop("episodes", 12),
        **kwargs,
    )


async def save(session: AsyncSession, rows: Sequence[Anime]) -> list[Anime]:
    """Insert rows without pinning their ids, so ``id`` order is insert order."""
    for row in rows:
        row.id = None  # type: ignore[assignment]
        session.add(row)
    await session.flush()
    return list(rows)


def season_row(index: int, *, season: tuple[str, int] = THIS_SEASON, **kwargs: Any) -> Anime:
    return anime(
        index,
        kwargs.pop("title", f"Season Show {index}"),
        anilist_id=1000 + index,
        season=season[0],
        season_year=season[1],
        **kwargs,
    )


async def pool_for(
    session: AsyncSession,
    catalog: Any,
    rows: Sequence[tuple[Anime, ListEntry]] = (),
    *,
    now: datetime = NOW,
) -> list[Any]:
    return await build_pool(session, catalog, rows=rows, now=now)


# --- Turning a row into a candidate -----------------------------------------


def test_a_music_video_is_never_a_candidate() -> None:
    assert candidate_of(anime(1, "OP", format="MUSIC"), "why") is None


def test_a_row_with_no_title_is_never_a_candidate() -> None:
    row = anime(1, "x")
    row.title_romaji = None
    assert candidate_of(row, "why") is None


def test_tags_are_capped_and_the_synopsis_is_flattened_and_truncated() -> None:
    row = anime(
        1,
        "Frieren",
        tags=[f"tag{i}" for i in range(20)],
        description="A\n\nlong  story. " + ("x" * 900),
    )

    candidate = candidate_of(row, "why")

    assert candidate is not None
    assert len(candidate.tags) == TAG_LIMIT
    assert candidate.synopsis is not None
    assert "\n" not in candidate.synopsis
    assert candidate.synopsis.startswith("A long story.")
    assert len(candidate.synopsis) <= SYNOPSIS_CHARS + 1  # the ellipsis


@pytest.mark.parametrize("fmt", ["TV", "TV_SHORT", "ONA", "MOVIE"])
def test_the_formats_people_actually_watch_are_candidates(fmt: str) -> None:
    assert candidate_of(anime(1, "Show", format=fmt), "why") is not None


@pytest.mark.parametrize("fmt", ["MUSIC", "OVA", "SPECIAL", "MANGA", "NOVEL", None])
def test_every_other_format_is_not(fmt: str | None) -> None:
    """An allow-list: the tail of AniList's vocabulary is not television."""
    assert candidate_of(anime(1, "Show", format=fmt), "why") is None


@pytest.mark.parametrize(
    "title",
    [
        "Frieren Recap",
        "Gintama: The Semi-Final Special",
        "Shirobako Theater",
        "Aria the Theatre",
        "Mini Toji",
        "Picture Drama",
        "Haikyuu Omake",
        "Nichijou no Daze",
        "Bleach PV",
    ],
)
def test_recaps_and_specials_are_not_candidates(title: str) -> None:
    """AniList files these beside the show, so without this the page offers
    "Frieren Recap" to somebody who just watched Frieren."""
    assert candidate_of(anime(1, title), "why") is None


@pytest.mark.parametrize(
    "title",
    ["The Administrator", "Terminir", "Especially Yours", "Dazed and Confused", "Spvcial"],
)
def test_a_word_that_merely_contains_a_pattern_is_kept(title: str) -> None:
    """The reason the match is on word boundaries: "mini" inside
    *Administrator* is not a recap, and a substring test would drop it."""
    assert candidate_of(anime(1, title), "why") is not None


# --- Taste ------------------------------------------------------------------


def test_top_genres_are_weighted_by_score_and_ignore_planned_and_dropped() -> None:
    rows = [
        (anime(1, "A", genres=["Drama", "Mystery"]), entry(1, ListStatus.COMPLETED, score=10)),
        (anime(2, "B", genres=["Drama"]), entry(2, ListStatus.COMPLETED, score=9)),
        (anime(3, "C", genres=["Comedy"]), entry(3, ListStatus.WATCHING)),
        # Neither of these may vote.
        (anime(4, "D", genres=["Ecchi", "Ecchi"]), entry(4, ListStatus.PLANNED, score=10)),
        (anime(5, "E", genres=["Horror"]), entry(5, ListStatus.DROPPED, score=10)),
    ]

    assert top_genres(rows) == ("Drama", "Mystery", "Comedy")


def test_top_genres_of_an_empty_list_is_empty() -> None:
    assert top_genres([]) == ()


# --- Source a: the season ---------------------------------------------------


async def test_the_pool_is_this_season_and_the_next(db_session: AsyncSession) -> None:
    await save(
        db_session,
        [
            season_row(1, title="Now"),
            season_row(2, title="Next", season=NEXT_SEASON),
            season_row(3, title="Last", season=("SUMMER", 2026)),
        ],
    )

    pool = await pool_for(db_session, FakeCatalog())

    assert {candidate.title for candidate in pool} == {"Now", "Next"}
    assert [candidate.why for candidate in pool] == [
        "airing this season (Fall 2026)",
        "starts next season (Winter 2027)",
    ]


@pytest.mark.parametrize(
    "status",
    [ListStatus.WATCHING, ListStatus.COMPLETED, ListStatus.ON_HOLD, ListStatus.DROPPED],
)
async def test_anything_already_decided_about_is_excluded(
    db_session: AsyncSession, status: ListStatus
) -> None:
    rows = await save(db_session, [season_row(1, title="Seen"), season_row(2, title="Unseen")])
    listed = [(rows[0], entry(rows[0].id, status, user_id=USER_ID))]

    pool = await pool_for(db_session, FakeCatalog(), listed)

    assert [candidate.title for candidate in pool] == ["Unseen"]


async def test_planned_is_the_exception_and_stays_in(db_session: AsyncSession) -> None:
    """FR-R2 keeps ``planned`` precisely because it is the best nudge there is."""
    rows = await save(db_session, [season_row(1, title="Planned")])
    listed = [(rows[0], entry(rows[0].id, ListStatus.PLANNED, user_id=USER_ID))]

    pool = await pool_for(db_session, FakeCatalog(), listed)

    assert [candidate.title for candidate in pool] == ["Planned"]


async def test_a_music_row_never_reaches_the_pool(db_session: AsyncSession) -> None:
    await save(db_session, [season_row(1, title="OP", format="MUSIC"), season_row(2)])

    pool = await pool_for(db_session, FakeCatalog())

    assert [candidate.title for candidate in pool] == ["Season Show 2"]


async def test_the_season_is_ordered_by_popularity(db_session: AsyncSession) -> None:
    """A season is more titles than the pool takes, so something must choose,
    and "what everyone else is watching" is the only honest signal a source
    with no personal input has."""
    rows = [
        season_row(1, title="Unknown"),
        season_row(2, title="Huge"),
        season_row(3, title="Small"),
    ]
    rows[1].popularity = 500_000
    rows[2].popularity = 900
    await save(db_session, rows)

    pool = await pool_for(db_session, FakeCatalog())

    # Nulls last: a row written before the column existed sorts to the bottom
    # rather than the top.
    assert [c.title for c in pool] == ["Huge", "Small", "Unknown"]


async def test_genre_matches_are_ordered_by_score(db_session: AsyncSession) -> None:
    """Score rather than popularity, and it is the one place they differ: this
    source has already matched on taste, so the question left is "is it good"."""
    good = anime(0, "Good", anilist_id=6001, genres=["Drama", "Mystery"])
    poor = anime(0, "Poor", anilist_id=6002, genres=["Drama", "Mystery"])
    unrated = anime(0, "Unrated", anilist_id=6003, genres=["Drama", "Mystery"])
    good.average_score = 88
    poor.average_score = 51
    watched = anime(0, "Watched", anilist_id=4001, genres=["Drama", "Mystery"])
    await save(db_session, [unrated, poor, good, watched])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=9, user_id=USER_ID))]

    pool = await pool_for(db_session, FakeCatalog(), listed)

    assert [c.title for c in pool] == ["Good", "Poor", "Unrated"]


async def test_the_pool_is_capped_at_forty(db_session: AsyncSession) -> None:
    await save(db_session, [season_row(i) for i in range(1, 80)])

    pool = await pool_for(db_session, FakeCatalog())

    assert len(pool) == POOL_CAP
    assert len({candidate.anime_id for candidate in pool}) == POOL_CAP


# --- Source b: relations ----------------------------------------------------


async def test_a_sequel_of_a_listed_show_is_not_in_the_main_pool(
    db_session: AsyncSession,
) -> None:
    """It belongs to the continuations section, which can name what it follows.

    A sequel scores well on every signal the pool has and needs none of them:
    "season two of the thing you finished" is a fact, not an argument, and
    leaving it here would crowd out the discoveries the page is for.
    """
    sequel = anime(0, "Frieren S2", anilist_id=5001, season="SUMMER", season_year=2020)
    watched = anime(
        0,
        "Frieren",
        anilist_id=4001,
        relations=[{"anilist_id": 5001, "mal_id": None, "relation_type": "SEQUEL"}],
    )
    await save(db_session, [sequel, watched, season_row(9)])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]

    pool = await pool_for(db_session, FakeCatalog(), listed)

    assert "Frieren S2" not in {c.title for c in pool}
    assert [c.title for c in pool] == ["Season Show 9"]


@pytest.mark.parametrize(
    "relation_type",
    ["SEQUEL", "PREQUEL", "SIDE_STORY", "SPIN_OFF", "ALTERNATIVE", "PARENT", "SUMMARY"],
)
async def test_every_continuation_relation_type_is_excluded(
    db_session: AsyncSession, relation_type: str
) -> None:
    related = anime(0, "Related", anilist_id=5001, genres=["Drama", "Mystery"])
    watched = anime(
        0,
        "Watched",
        anilist_id=4001,
        genres=["Drama", "Mystery"],
        relations=[{"anilist_id": 5001, "mal_id": None, "relation_type": relation_type}],
    )
    await save(db_session, [related, watched])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=9, user_id=USER_ID))]

    assert await pool_for(db_session, FakeCatalog(), listed) == []


async def test_an_unrelated_relation_type_still_reaches_the_pool(
    db_session: AsyncSession,
) -> None:
    """``CHARACTER``/``OTHER`` are not continuations, so they are fair game."""
    related = anime(0, "Shared Universe", anilist_id=5001)
    watched = anime(
        0,
        "Watched",
        anilist_id=4001,
        relations=[{"anilist_id": 5001, "mal_id": None, "relation_type": "OTHER"}],
    )
    await save(db_session, [related, watched])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]

    pool = await pool_for(db_session, FakeCatalog(), listed)

    assert [c.title for c in pool] == ["Shared Universe"]
    assert pool[0].why == "related to Watched, which you completed"


async def test_a_low_scored_show_is_not_a_relation_seed_when_a_high_one_exists(
    db_session: AsyncSession,
) -> None:
    good_sequel = anime(0, "Good Sequel", anilist_id=5001)
    bad_sequel = anime(0, "Bad Sequel", anilist_id=5002)
    loved = anime(0, "Loved", anilist_id=4001, relations=[{"anilist_id": 5001, "mal_id": None}])
    tolerated = anime(0, "Meh", anilist_id=4002, relations=[{"anilist_id": 5002, "mal_id": None}])
    await save(db_session, [good_sequel, bad_sequel, loved, tolerated])
    listed = [
        (loved, entry(loved.id, ListStatus.COMPLETED, score=9, user_id=USER_ID)),
        (tolerated, entry(tolerated.id, ListStatus.COMPLETED, score=4, user_id=USER_ID)),
    ]

    titles = {c.title for c in await pool_for(db_session, FakeCatalog(), listed)}

    assert "Good Sequel" in titles
    assert "Bad Sequel" not in titles


async def test_an_unscored_list_still_produces_relation_seeds(db_session: AsyncSession) -> None:
    """Most people score nothing; a pool that needed scores would be empty."""
    sequel = anime(0, "Sequel", anilist_id=5001)
    watched = anime(0, "Watched", anilist_id=4001, relations=[{"anilist_id": 5001, "mal_id": None}])
    await save(db_session, [sequel, watched])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, user_id=USER_ID))]

    assert "Sequel" in {c.title for c in await pool_for(db_session, FakeCatalog(), listed)}


async def test_a_relation_arc_has_never_cached_is_fetched_and_upserted(
    db_session: AsyncSession,
) -> None:
    watched = anime(0, "Watched", anilist_id=4001, relations=[{"anilist_id": 7777, "mal_id": None}])
    await save(db_session, [watched])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]
    catalog = FakeCatalog({7777: media(7777, "Fetched Sequel")})

    pool = await pool_for(db_session, catalog, listed)

    assert "Fetched Sequel" in {c.title for c in pool}
    # …and it is now a real row, so the next run needs no fetch.
    cached = (
        (await db_session.execute(select(Anime).where(Anime.anilist_id == 7777))).scalars().one()
    )
    assert cached.title_romaji == "Fetched Sequel"


async def test_a_catalogue_failure_skips_the_relation_rather_than_the_run(
    db_session: AsyncSession,
) -> None:
    watched = anime(0, "Watched", anilist_id=4001, relations=[{"anilist_id": 7777, "mal_id": None}])
    await save(db_session, [watched, season_row(9)])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]
    catalog = FakeCatalog(fail=True)

    pool = await pool_for(db_session, catalog, listed)

    assert catalog.calls == 1
    assert [c.title for c in pool] == ["Season Show 9"]


async def test_the_number_of_catalogue_fetches_is_capped(db_session: AsyncSession) -> None:
    """Twenty uncached relations must not be twenty round trips on a page load.

    Each one is a catalogue call a user is waiting for, so the optional half of
    this source is bounded twice — by this count and by a wall-clock deadline.
    """
    relations = [{"anilist_id": 7000 + i, "mal_id": None} for i in range(20)]
    watched = anime(0, "Watched", anilist_id=4001, relations=relations)
    await save(db_session, [watched])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]
    # Every one of them resolves, so nothing but the cap stops the loop.
    catalog = FakeCatalog({7000 + i: media(7000 + i, f"Sequel {i}") for i in range(20)})

    pool = await pool_for(db_session, catalog, listed)

    assert catalog.calls == RELATION_FETCH_LIMIT
    assert len(pool) == RELATION_FETCH_LIMIT


async def test_a_relation_the_user_has_already_watched_is_excluded(
    db_session: AsyncSession,
) -> None:
    sequel = anime(0, "Sequel", anilist_id=5001)
    watched = anime(0, "Watched", anilist_id=4001, relations=[{"anilist_id": 5001, "mal_id": None}])
    await save(db_session, [sequel, watched])
    listed = [
        (watched, entry(watched.id, ListStatus.COMPLETED, score=10, user_id=USER_ID)),
        (sequel, entry(sequel.id, ListStatus.DROPPED, user_id=USER_ID)),
    ]

    assert await pool_for(db_session, FakeCatalog(), listed) == []


# --- Source c: genres -------------------------------------------------------


async def test_two_shared_genres_qualify_and_one_does_not(db_session: AsyncSession) -> None:
    two = anime(0, "Two", anilist_id=6001, genres=["Drama", "Mystery"])
    one = anime(0, "One", anilist_id=6002, genres=["Drama", "Sports"])
    watched = anime(0, "Watched", anilist_id=4001, genres=["Drama", "Mystery", "Supernatural"])
    await save(db_session, [two, one, watched])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=9, user_id=USER_ID))]

    pool = await pool_for(db_session, FakeCatalog(), listed)

    titles = {c.title for c in pool}
    assert "Two" in titles
    assert "One" not in titles
    assert next(c for c in pool if c.title == "Two").why == "shares your genres: Drama, Mystery"


async def test_a_show_is_never_in_the_pool_twice(db_session: AsyncSession) -> None:
    """The same row qualifies as both a seasonal title and a genre match."""
    both = anime(
        0,
        "Both",
        anilist_id=6001,
        genres=["Drama", "Mystery"],
        season=THIS_SEASON[0],
        season_year=THIS_SEASON[1],
    )
    watched = anime(0, "Watched", anilist_id=4001, genres=["Drama", "Mystery"])
    await save(db_session, [both, watched])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=9, user_id=USER_ID))]

    pool = await pool_for(db_session, FakeCatalog(), listed)

    assert [c.title for c in pool] == ["Both"]
    assert pool[0].why.startswith("airing this season")


async def test_the_season_does_not_crowd_out_the_personal_sources(
    db_session: AsyncSession,
) -> None:
    """A whole season is bigger than the pool; shares are what keep room."""
    await save(db_session, [season_row(i) for i in range(1, 60)])
    sequel = anime(0, "Sequel", anilist_id=5001)
    watched = anime(0, "Watched", anilist_id=4001, relations=[{"anilist_id": 5001, "mal_id": None}])
    await save(db_session, [sequel, watched])
    listed = [(watched, entry(watched.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]

    pool = await pool_for(db_session, FakeCatalog(), listed)

    assert len(pool) == POOL_CAP
    assert "Sequel" in {c.title for c in pool}
    seasonal = [c for c in pool if c.why.startswith(("airing this season", "starts next season"))]
    # The season keeps its share, then takes back what the others did not use.
    assert len(seasonal) >= SEASON_SHARE


async def test_an_empty_catalogue_gives_an_empty_pool(db_session: AsyncSession) -> None:
    assert await pool_for(db_session, FakeCatalog()) == []
