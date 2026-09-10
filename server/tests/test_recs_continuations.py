"""New in the user's own franchises (§5.6).

The deterministic half of the page. No model is involved, so these tests are
about judgement encoded in code rather than in a prompt: which relations count
as "new in a franchise you follow", what the sentence under the card says, and
what must not appear — anything already on the list, anything that is a recap,
anything past the cap.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import ListStatus
from arc.services.recs.continuations import (
    MAX_CONTINUATIONS,
    Continuation,
    build_continuations,
)
from tests.recs_helpers import anime, entry
from tests.test_recs_pool import USER_ID, FakeCatalog, media, save

pytestmark = pytest.mark.pg


def relation(anilist_id: int, relation_type: str, *, format: str = "TV") -> dict[str, object]:
    return {
        "anilist_id": anilist_id,
        "mal_id": None,
        "relation_type": relation_type,
        "format": format,
    }


async def build(session: AsyncSession, rows, catalog=None, **kwargs):  # type: ignore[no-untyped-def]
    return await build_continuations(session, catalog or FakeCatalog(), rows=rows, **kwargs)


# --- Which relations count ---------------------------------------------------


@pytest.mark.parametrize(
    ("relation_type", "expected"),
    [
        ("SEQUEL", "Sequel to Frieren, which you completed"),
        ("SIDE_STORY", "Side story to Frieren, which you completed"),
        ("SPIN_OFF", "Spin-off of Frieren, which you completed"),
        ("ALTERNATIVE", "Alternative version of Frieren, which you completed"),
    ],
)
async def test_each_forward_relation_type_gets_its_own_wording(
    db_session: AsyncSession, relation_type: str, expected: str
) -> None:
    new = anime(0, "New", anilist_id=5001)
    source = anime(0, "Frieren", anilist_id=4001, relations=[relation(5001, relation_type)])
    await save(db_session, [new, source])
    rows = [(source, entry(source.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]

    found = await build(db_session, rows)

    assert [item.because for item in found] == [expected]


@pytest.mark.parametrize("relation_type", ["PREQUEL", "PARENT", "SUMMARY", "CHARACTER", "OTHER"])
async def test_relations_pointing_backwards_are_not_news(
    db_session: AsyncSession, relation_type: str
) -> None:
    """A prequel or a parent story is older than what they already watched, and
    a summary is a recap by another name."""
    old = anime(0, "Older", anilist_id=5001)
    source = anime(0, "Frieren", anilist_id=4001, relations=[relation(5001, relation_type)])
    await save(db_session, [old, source])
    rows = [(source, entry(source.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]

    assert await build(db_session, rows) == []


async def test_a_movie_counts_whatever_its_relation_type_says(
    db_session: AsyncSession,
) -> None:
    """A franchise film is usually related as OTHER or SIDE_STORY; the format
    is the reliable signal."""
    film = anime(0, "The Movie", anilist_id=5001, format="MOVIE")
    source = anime(
        0,
        "Assassination Classroom",
        anilist_id=4001,
        relations=[relation(5001, "OTHER", format="MOVIE")],
    )
    await save(db_session, [film, source])
    rows = [(source, entry(source.id, ListStatus.PLANNED, user_id=USER_ID))]

    found = await build(db_session, rows)

    assert [item.because for item in found] == [
        "Movie in the Assassination Classroom series (on your planned list)"
    ]


# --- Which shows are sources -------------------------------------------------


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (ListStatus.COMPLETED, "which you completed"),
        (ListStatus.WATCHING, "which you are watching"),
        (ListStatus.PLANNED, "(on your planned list)"),
    ],
)
async def test_the_sentence_names_how_they_know_the_source(
    db_session: AsyncSession, status: ListStatus, phrase: str
) -> None:
    new = anime(0, "New", anilist_id=5001)
    source = anime(0, "Frieren", anilist_id=4001, relations=[relation(5001, "SEQUEL")])
    await save(db_session, [new, source])
    rows = [(source, entry(source.id, status, user_id=USER_ID))]

    found = await build(db_session, rows)

    assert found[0].because.endswith(phrase)


@pytest.mark.parametrize("status", [ListStatus.DROPPED, ListStatus.ON_HOLD])
async def test_a_franchise_they_abandoned_is_not_theirs(
    db_session: AsyncSession, status: ListStatus
) -> None:
    """A sequel to something dropped is not news."""
    new = anime(0, "New", anilist_id=5001)
    source = anime(0, "Dropped", anilist_id=4001, relations=[relation(5001, "SEQUEL")])
    await save(db_session, [new, source])
    rows = [(source, entry(source.id, status, user_id=USER_ID))]

    assert await build(db_session, rows) == []


# --- What is left out --------------------------------------------------------


async def test_a_continuation_already_on_the_list_is_dropped(
    db_session: AsyncSession,
) -> None:
    """Including one they have merely planned — unlike the main pool, where a
    nudge to start a planned show is the point. Here it would be "the sequel
    you already know about"."""
    new = anime(0, "Sequel", anilist_id=5001)
    source = anime(0, "Frieren", anilist_id=4001, relations=[relation(5001, "SEQUEL")])
    await save(db_session, [new, source])
    rows = [
        (source, entry(source.id, ListStatus.COMPLETED, score=10, user_id=USER_ID)),
        (new, entry(new.id, ListStatus.PLANNED, user_id=USER_ID)),
    ]

    assert await build(db_session, rows) == []


async def test_recaps_and_specials_are_excluded(db_session: AsyncSession) -> None:
    recap = anime(0, "Frieren Recap", anilist_id=5001)
    real = anime(0, "Frieren Season 2", anilist_id=5002)
    source = anime(
        0,
        "Frieren",
        anilist_id=4001,
        relations=[relation(5001, "SUMMARY"), relation(5001, "SEQUEL"), relation(5002, "SEQUEL")],
    )
    await save(db_session, [recap, real, source])
    rows = [(source, entry(source.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]

    found = await build(db_session, rows)

    assert [item.title for item in found] == ["Frieren Season 2"]


async def test_an_ova_continuation_is_excluded_by_format(db_session: AsyncSession) -> None:
    ova = anime(0, "Bonus Episode", anilist_id=5001, format="OVA")
    source = anime(
        0, "Frieren", anilist_id=4001, relations=[relation(5001, "SIDE_STORY", format="OVA")]
    )
    await save(db_session, [ova, source])
    rows = [(source, entry(source.id, ListStatus.COMPLETED, user_id=USER_ID))]

    assert await build(db_session, rows) == []


async def test_a_continuation_appears_once_however_many_shows_point_at_it(
    db_session: AsyncSession,
) -> None:
    shared = anime(0, "Shared Sequel", anilist_id=5001)
    first = anime(0, "First", anilist_id=4001, relations=[relation(5001, "SEQUEL")])
    second = anime(0, "Second", anilist_id=4002, relations=[relation(5001, "SEQUEL")])
    await save(db_session, [shared, first, second])
    rows = [
        (first, entry(first.id, ListStatus.COMPLETED, score=10, user_id=USER_ID)),
        (second, entry(second.id, ListStatus.COMPLETED, score=8, user_id=USER_ID)),
    ]

    found = await build(db_session, rows)

    # Attributed to the show they rated highest.
    assert [(item.title, item.because) for item in found] == [
        ("Shared Sequel", "Sequel to First, which you completed")
    ]


# --- Ordering and the cap ----------------------------------------------------


async def test_the_best_rated_franchise_comes_first(db_session: AsyncSession) -> None:
    from_ten = anime(0, "From a ten", anilist_id=5001)
    from_six = anime(0, "From a six", anilist_id=5002)
    loved = anime(0, "Loved", anilist_id=4001, relations=[relation(5001, "SEQUEL")])
    liked = anime(0, "Liked", anilist_id=4002, relations=[relation(5002, "SEQUEL")])
    await save(db_session, [from_six, from_ten, liked, loved])
    rows = [
        (liked, entry(liked.id, ListStatus.COMPLETED, score=6, user_id=USER_ID)),
        (loved, entry(loved.id, ListStatus.COMPLETED, score=10, user_id=USER_ID)),
    ]

    found = await build(db_session, rows)

    assert [item.title for item in found] == ["From a ten", "From a six"]


async def test_the_newest_comes_first_within_one_franchise(
    db_session: AsyncSession,
) -> None:
    old = anime(0, "Older", anilist_id=5001, season="WINTER", season_year=2020)
    new = anime(0, "Newer", anilist_id=5002, season="FALL", season_year=2026)
    mid = anime(0, "Middle", anilist_id=5003, season="SPRING", season_year=2026)
    source = anime(
        0,
        "Source",
        anilist_id=4001,
        relations=[relation(5001, "SEQUEL"), relation(5002, "SEQUEL"), relation(5003, "SEQUEL")],
    )
    await save(db_session, [old, new, mid, source])
    rows = [(source, entry(source.id, ListStatus.COMPLETED, score=9, user_id=USER_ID))]

    found = await build(db_session, rows)

    assert [item.title for item in found] == ["Newer", "Middle", "Older"]


async def test_the_section_is_capped(db_session: AsyncSession) -> None:
    extras = [anime(0, f"Sequel {i}", anilist_id=5000 + i) for i in range(MAX_CONTINUATIONS + 4)]
    source = anime(
        0,
        "Source",
        anilist_id=4001,
        relations=[relation(5000 + i, "SEQUEL") for i in range(MAX_CONTINUATIONS + 4)],
    )
    await save(db_session, [*extras, source])
    rows = [(source, entry(source.id, ListStatus.COMPLETED, score=9, user_id=USER_ID))]

    assert len(await build(db_session, rows)) == MAX_CONTINUATIONS


async def test_an_empty_list_has_no_continuations(db_session: AsyncSession) -> None:
    assert await build(db_session, []) == []


async def test_a_relation_arc_has_never_cached_is_fetched(db_session: AsyncSession) -> None:
    source = anime(0, "Frieren", anilist_id=4001, relations=[relation(7777, "SEQUEL")])
    await save(db_session, [source])
    rows = [(source, entry(source.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]
    catalog = FakeCatalog({7777: media(7777, "Fetched Sequel")})

    found = await build(db_session, rows, catalog)

    assert [item.title for item in found] == ["Fetched Sequel"]


async def test_a_catalogue_failure_skips_the_title_rather_than_the_section(
    db_session: AsyncSession,
) -> None:
    cached = anime(0, "Cached Sequel", anilist_id=5001)
    source = anime(
        0,
        "Frieren",
        anilist_id=4001,
        relations=[relation(7777, "SEQUEL"), relation(5001, "SEQUEL")],
    )
    await save(db_session, [cached, source])
    rows = [(source, entry(source.id, ListStatus.COMPLETED, score=10, user_id=USER_ID))]

    found = await build(db_session, rows, FakeCatalog(fail=True))

    assert [item.title for item in found] == ["Cached Sequel"]


def test_the_stored_form_is_tagged() -> None:
    """``rec_runs.picks`` holds both kinds in one list, so each says which."""
    stored = Continuation(anime_id=7, title="Sequel", because="Sequel to X").as_dict()

    assert stored == {
        "kind": "continuation",
        "anime_id": 7,
        "title": "Sequel",
        "because": "Sequel to X",
    }
