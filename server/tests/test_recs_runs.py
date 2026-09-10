"""Validating picks, counting the daily budget, storing a run (FR-R3, FR-R5).

The validation tests are the important ones and they are pure: a model that
returns a show which was never in its pool, or one the user finished last week,
must not reach the page. It is the same rule as the matcher's — a guess is
never applied — and it is enforced here rather than trusted to the prompt.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, ListStatus, RecRun, User
from arc.services.auth import create_user
from arc.services.catalog import get_my_list
from arc.services.recs.base import RecsUnavailable
from arc.services.recs.pool import RELATION_FETCH_LIMIT, build_pool, candidate_of
from arc.services.recs.runs import (
    DAILY_LIMIT,
    WINDOW,
    RecsEmptyPool,
    RecsRateLimited,
    latest_run,
    remaining_today,
    run_recommendations,
    validate_picks,
)
from arc.services.recs.schema import MAX_PICKS, Pick
from tests.recs_helpers import NOW, FakeModel, anime, entry
from tests.test_recs_pool import FakeCatalog, media, save, season_row

# --- Validation (pure) ------------------------------------------------------


def pool():  # type: ignore[no-untyped-def]
    built = [candidate_of(anime(i, f"Show {i}"), "why") for i in (11, 12, 13, 14, 15, 16)]
    return [candidate for candidate in built if candidate is not None]


def picks(*ids: int) -> list[Pick]:
    return [Pick(anime_id=i, title=f"Show {i}", case=f"case {i}") for i in ids]


def test_good_picks_survive_and_keep_their_order() -> None:
    kept = validate_picks(picks(13, 11, 12), candidates=pool(), excluded=set())

    assert [pick["anime_id"] for pick in kept] == [13, 11, 12]
    assert kept[0]["case"] == "case 13"


def test_a_pick_that_was_never_in_the_pool_is_dropped() -> None:
    """The model's one genuinely dangerous failure: a real, unavailable show."""
    kept = validate_picks(picks(11, 999, 12), candidates=pool(), excluded=set())

    assert [pick["anime_id"] for pick in kept] == [11, 12]


def test_a_pick_the_user_has_already_watched_is_dropped() -> None:
    kept = validate_picks(picks(11, 12), candidates=pool(), excluded={12})

    assert [pick["anime_id"] for pick in kept] == [11]


def test_a_planned_pick_is_kept() -> None:
    """FR-R2 leaves planned in the pool, so it must survive validation too."""
    kept = validate_picks(picks(11), candidates=pool(), excluded=set())

    assert [pick["anime_id"] for pick in kept] == [11]


def test_duplicates_are_collapsed() -> None:
    kept = validate_picks(picks(11, 11, 12), candidates=pool(), excluded=set())

    assert [pick["anime_id"] for pick in kept] == [11, 12]


def test_more_than_five_picks_are_truncated() -> None:
    kept = validate_picks(picks(11, 12, 13, 14, 15, 16), candidates=pool(), excluded=set())

    assert len(kept) == MAX_PICKS


def test_the_stored_title_is_the_catalogue_s_not_the_model_s() -> None:
    invented = [Pick(anime_id=11, title="Shwo 11 (Season Two)", case="  spaced  ")]

    kept = validate_picks(invented, candidates=pool(), excluded=set())

    assert kept[0]["title"] == "Show 11"
    assert kept[0]["case"] == "spaced"


# --- The run (Postgres) -----------------------------------------------------
#
# Marked one at a time rather than with a module-level ``pytestmark``: the
# validation tests above are pure and must keep running when there is no
# database.


@pytest.fixture
async def user(db_session: AsyncSession) -> User:
    row = await create_user(db_session, "recs@arc.test", "recs-password-123")
    await db_session.flush()
    return row


async def add_runs(session: AsyncSession, user: User, count: int, *, at) -> None:  # type: ignore[no-untyped-def]
    for _ in range(count):
        session.add(RecRun(user_id=user.id, prompt=None, candidates=[], picks=[], created_at=at))
    await session.flush()


@pytest.mark.pg
async def test_a_run_stores_its_pool_its_picks_and_the_model(
    db_session: AsyncSession, user: User
) -> None:
    rows = await save(db_session, [season_row(i) for i in range(1, 5)])
    ids = [row.id for row in rows]
    model = FakeModel([(ids[0], "x", "because Frieren"), (ids[1], "y", "because Bleach")])

    run = await run_recommendations(
        db_session,
        FakeCatalog(),
        model,
        user_id=user.id,
        prompt="  something short  ",
        now=NOW,
    )

    assert run.prompt == "  something short  "  # the router trims; the service stores
    assert run.model == "claude-opus-5"
    assert [pick["anime_id"] for pick in run.picks or []] == ids[:2]
    assert len(run.candidates or []) == 4
    # The whole pool is on the run, so a pick can be explained later.
    assert {candidate["anime_id"] for candidate in run.candidates or []} == set(ids)


@pytest.mark.pg
async def test_a_run_stores_picks_and_continuations_in_one_tagged_list(
    db_session: AsyncSession, user: User
) -> None:
    """One JSONB column holds both, tagged, so neither needs a migration."""
    sequel = anime(0, "Frieren S2", anilist_id=5001)
    watched = anime(
        0,
        "Frieren",
        anilist_id=4001,
        relations=[{"anilist_id": 5001, "mal_id": None, "relation_type": "SEQUEL", "format": "TV"}],
    )
    season = season_row(1, title="Something New")
    await save(db_session, [sequel, watched, season])
    db_session.add(entry(watched.id, ListStatus.COMPLETED, score=10, user_id=user.id))
    await db_session.flush()
    model = FakeModel([(season.id, "Something New", "because Frieren")])

    run = await run_recommendations(
        db_session, FakeCatalog(), model, user_id=user.id, prompt=None, now=NOW
    )

    kinds = [item.get("kind") for item in run.picks or []]
    assert kinds == ["pick", "continuation"]
    continuation = (run.picks or [])[1]
    assert continuation["anime_id"] == sequel.id
    assert continuation["because"] == "Sequel to Frieren, which you completed"
    # …and the sequel was never offered to the model.
    assert sequel.id not in {c["anime_id"] for c in run.candidates or []}


@pytest.mark.pg
async def test_one_relation_budget_is_shared_by_the_pool_and_the_continuations(
    db_session: AsyncSession, user: User
) -> None:
    """The bound the docs promise is per *run*, not per section.

    Both halves resolve relations, and each used to start its own count and its
    own clock — so "at most ten fetches" was really twenty on a page somebody
    is waiting for.
    """
    # Ten uncached continuations and ten uncached discoveries: twenty in total,
    # every one of them resolvable, so nothing but the budget stops the fetches.
    continuation_rels = [
        {"anilist_id": 7000 + i, "mal_id": None, "relation_type": "SEQUEL", "format": "TV"}
        for i in range(10)
    ]
    other_rels = [
        {"anilist_id": 8000 + i, "mal_id": None, "relation_type": "OTHER", "format": "TV"}
        for i in range(10)
    ]
    watched = anime(0, "Watched", anilist_id=4001, relations=[*continuation_rels, *other_rels])
    await save(db_session, [watched, season_row(1)])
    db_session.add(entry(watched.id, ListStatus.COMPLETED, score=10, user_id=user.id))
    await db_session.flush()
    catalog = FakeCatalog(
        {i: media(i, f"Fetched {i}") for i in [*range(7000, 7010), *range(8000, 8010)]}
    )
    model = FakeModel([])

    await run_recommendations(db_session, catalog, model, user_id=user.id, prompt=None, now=NOW)

    # Ten, not twenty: the pool spends what it can and the continuations
    # section finds the budget already gone.
    assert catalog.calls == RELATION_FETCH_LIMIT


@pytest.mark.pg
async def test_the_pool_does_not_spend_fetches_on_continuations(
    db_session: AsyncSession, user: User
) -> None:
    """Its seeds are listed shows, so every sequel among their relations is one
    the pool would resolve and then discard."""
    sequels = [
        {"anilist_id": 7000 + i, "mal_id": None, "relation_type": "SEQUEL", "format": "TV"}
        for i in range(10)
    ]
    discovery = {"anilist_id": 9001, "mal_id": None, "relation_type": "OTHER", "format": "TV"}
    watched = anime(0, "Watched", anilist_id=4001, relations=[*sequels, discovery])
    await save(db_session, [watched])
    db_session.add(entry(watched.id, ListStatus.COMPLETED, score=10, user_id=user.id))
    await db_session.flush()
    catalog = FakeCatalog({9001: media(9001, "A Discovery")})
    model = FakeModel([])

    pool = await build_pool(
        db_session, catalog, rows=await get_my_list(db_session, user_id=user.id), now=NOW
    )

    # One fetch, for the one relation that was not a continuation.
    assert catalog.calls == 1
    assert [c.title for c in pool] == ["A Discovery"]
    assert model.calls == 0


@pytest.mark.pg
async def test_a_failed_run_stores_no_continuations_either(
    db_session: AsyncSession, user: User
) -> None:
    """They are part of a successful run; there is no half-run to attach to."""
    await save(db_session, [season_row(1)])
    model = FakeModel(error=RecsUnavailable("down"))

    with pytest.raises(RecsUnavailable):
        await run_recommendations(
            db_session, FakeCatalog(), model, user_id=user.id, prompt=None, now=NOW
        )

    assert (await db_session.execute(select(RecRun))).scalars().all() == []


@pytest.mark.pg
async def test_a_pick_outside_the_pool_never_reaches_the_row(
    db_session: AsyncSession, user: User
) -> None:
    rows = await save(db_session, [season_row(1)])
    model = FakeModel([(rows[0].id, "real", "case"), (999_999, "invented", "case")])

    run = await run_recommendations(
        db_session, FakeCatalog(), model, user_id=user.id, prompt=None, now=NOW
    )

    assert [pick["anime_id"] for pick in run.picks or []] == [rows[0].id]


@pytest.mark.pg
async def test_too_few_survivors_are_stored_rather_than_retried(
    db_session: AsyncSession, user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """A second call is another half-minute and the same chance of the same
    mistake; three of five is still a page."""
    rows = await save(db_session, [season_row(1)])
    model = FakeModel([(rows[0].id, "real", "case"), (999_999, "invented", "case")])

    run = await run_recommendations(
        db_session, FakeCatalog(), model, user_id=user.id, prompt=None, now=NOW
    )

    assert model.calls == 1
    assert len(run.picks or []) == 1


@pytest.mark.pg
async def test_a_fetched_relation_survives_a_failed_run(
    db_session: AsyncSession, user: User
) -> None:
    """The pool commits before the model is called, so its cache writes stand.

    That commit is what keeps Postgres row locks from being held across a
    twenty-second call to a third party. Keeping the fetched relation is the
    consequence, and the right way round: it is cache, and a run that failed
    after paying for a catalogue lookup should not throw it away.
    """
    watched = anime(0, "Watched", anilist_id=4001, relations=[{"anilist_id": 7777, "mal_id": None}])
    await save(db_session, [watched])
    db_session.add(entry(watched.id, ListStatus.COMPLETED, score=10, user_id=user.id))
    await db_session.flush()
    catalog = FakeCatalog({7777: media(7777, "Fetched Sequel")})
    model = FakeModel(error=RecsUnavailable("the provider is down"))

    with pytest.raises(RecsUnavailable):
        await run_recommendations(db_session, catalog, model, user_id=user.id, prompt=None, now=NOW)

    fetched = (
        (await db_session.execute(select(Anime).where(Anime.anilist_id == 7777))).scalars().first()
    )
    assert fetched is not None
    assert fetched.title_romaji == "Fetched Sequel"
    # …and no run was recorded, so the user has not spent one of their ten.
    assert await remaining_today(db_session, user_id=user.id, now=NOW) == DAILY_LIMIT


@pytest.mark.pg
async def test_a_failed_run_writes_no_rec_run_row(db_session: AsyncSession, user: User) -> None:
    """ "A row exists only on success" still holds after the early commit."""
    await save(db_session, [season_row(1)])
    model = FakeModel(error=RecsUnavailable("down"))

    with pytest.raises(RecsUnavailable):
        await run_recommendations(
            db_session, FakeCatalog(), model, user_id=user.id, prompt=None, now=NOW
        )

    assert (await db_session.execute(select(RecRun))).scalars().all() == []


@pytest.mark.pg
async def test_an_empty_pool_is_its_own_error(db_session: AsyncSession, user: User) -> None:
    model = FakeModel([])

    with pytest.raises(RecsEmptyPool):
        await run_recommendations(
            db_session, FakeCatalog(), model, user_id=user.id, prompt=None, now=NOW
        )

    assert model.calls == 0


@pytest.mark.pg
async def test_a_show_on_the_list_is_excluded_from_the_pool_the_model_sees(
    db_session: AsyncSession, user: User
) -> None:
    rows = await save(db_session, [season_row(1, title="Seen"), season_row(2, title="Unseen")])
    db_session.add(entry(rows[0].id, ListStatus.COMPLETED, user_id=user.id, score=9))
    await db_session.flush()
    model = FakeModel([(rows[1].id, "Unseen", "case")])

    run = await run_recommendations(
        db_session, FakeCatalog(), model, user_id=user.id, prompt=None, now=NOW
    )

    assert model.user is not None
    assert "Unseen" in model.user
    assert {c["anime_id"] for c in run.candidates or []} == {rows[1].id}


# --- The daily limit (FR-R5) ------------------------------------------------


@pytest.mark.pg
async def test_ten_runs_a_day_and_the_eleventh_is_refused(
    db_session: AsyncSession, user: User
) -> None:
    await save(db_session, [season_row(1)])
    await add_runs(db_session, user, DAILY_LIMIT, at=NOW - timedelta(hours=6))

    assert await remaining_today(db_session, user_id=user.id, now=NOW) == 0
    with pytest.raises(RecsRateLimited) as raised:
        await run_recommendations(
            db_session, FakeCatalog(), FakeModel([]), user_id=user.id, prompt=None, now=NOW
        )

    # The budget frees up when the oldest run leaves the window: 18 hours.
    assert raised.value.retry_after_seconds == pytest.approx(18 * 3600, abs=2)


@pytest.mark.pg
async def test_runs_older_than_the_window_do_not_count(
    db_session: AsyncSession, user: User
) -> None:
    await save(db_session, [season_row(1)])
    await add_runs(db_session, user, DAILY_LIMIT, at=NOW - WINDOW - timedelta(minutes=1))

    assert await remaining_today(db_session, user_id=user.id, now=NOW) == DAILY_LIMIT
    run = await run_recommendations(
        db_session, FakeCatalog(), FakeModel([]), user_id=user.id, prompt=None, now=NOW
    )
    assert run.id is not None


@pytest.mark.pg
async def test_the_budget_is_per_user(db_session: AsyncSession, user: User) -> None:
    other = await create_user(db_session, "other@arc.test", "other-password-123")
    await db_session.flush()
    await add_runs(db_session, user, DAILY_LIMIT, at=NOW)

    assert await remaining_today(db_session, user_id=user.id, now=NOW) == 0
    assert await remaining_today(db_session, user_id=other.id, now=NOW) == DAILY_LIMIT


@pytest.mark.pg
async def test_the_rate_limit_is_checked_before_anything_is_built(
    db_session: AsyncSession, user: User
) -> None:
    """The eleventh run must cost nothing — no pool, no catalogue, no call."""
    await save(db_session, [season_row(1)])
    await add_runs(db_session, user, DAILY_LIMIT, at=NOW)
    model = FakeModel([])
    catalog = FakeCatalog()

    with pytest.raises(RecsRateLimited):
        await run_recommendations(db_session, catalog, model, user_id=user.id, prompt=None, now=NOW)

    assert model.calls == 0
    assert catalog.calls == 0


@pytest.mark.pg
async def test_latest_run_is_the_newest_one(db_session: AsyncSession, user: User) -> None:
    await add_runs(db_session, user, 1, at=NOW - timedelta(hours=3))
    db_session.add(
        RecRun(user_id=user.id, prompt="newest", candidates=[], picks=[], created_at=NOW)
    )
    await db_session.flush()

    found = await latest_run(db_session, user_id=user.id)

    assert found is not None
    assert found.prompt == "newest"


@pytest.mark.pg
async def test_a_user_with_no_runs_has_none(db_session: AsyncSession, user: User) -> None:
    assert await latest_run(db_session, user_id=user.id) is None
    assert (await db_session.execute(select(RecRun))).scalars().all() == []
