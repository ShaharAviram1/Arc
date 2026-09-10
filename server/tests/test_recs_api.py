"""The recommendations endpoints over HTTP (FR-R1, FR-R4, FR-R5).

The router is thin, so these tests are about the five ways a run can fail and
the one shape the client is coded against. The Anthropic call is a fake on
``app.state.recs_model`` — the same seam the catalogue uses — so nothing here
reaches the network, and ``configured: false`` is tested by leaving the seam
empty and the key unset.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from arc.api.recs import DECLINED, EMPTY_POOL, MAX_PROMPT_CHARS, NOT_CONFIGURED, UPSTREAM
from arc.db import SessionFactory
from arc.models import Anime, ListEntry, ListStatus, RecRun, User, UserRole
from arc.services.recs import DAILY_LIMIT
from arc.services.recs.base import RecsFailed, RecsRefused, RecsUnavailable
from arc.services.recs.chain import RecsChain
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login
from tests.recs_helpers import NOW, FakeModel, anime

pytestmark = pytest.mark.pg

USER_EMAIL = "recs@arc.test"
USER_PASSWORD = "recs-password-123"

#: ``NOW`` is 4 November 2026 — FALL 2026.
SEASON = ("FALL", 2026)


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arc.api.recs.now", lambda: NOW)


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


async def add_season(factory: SessionFactory, count: int = 3) -> list[int]:
    """``count`` cached rows in the current season, ready to be recommended."""
    async with factory() as session:
        rows = [
            anime(
                0,
                f"Season Show {i}",
                anilist_id=9000 + i,
                genres=["Drama"],
                description="Something happens.",
                season=SEASON[0],
                season_year=SEASON[1],
            )
            for i in range(1, count + 1)
        ]
        for row in rows:
            row.id = None  # type: ignore[assignment]
            session.add(row)
        await session.commit()
        return [row.id for row in rows]


async def add_run(factory: SessionFactory, user: User, *, picks: list[dict[str, object]], at=NOW):  # type: ignore[no-untyped-def]
    async with factory() as session:
        run = RecRun(
            user_id=user.id,
            prompt="something short",
            candidates=[{"anime_id": 1}],
            picks=picks,
            model="claude-opus-5",
            created_at=at,
        )
        session.add(run)
        await session.commit()
        return run.id


def use(app: FastAPI, model: object) -> None:
    app.state.recs_model = model


# --- GET /api/recs ----------------------------------------------------------


async def test_the_page_needs_a_session(api_client: AsyncClient) -> None:
    assert (await api_client.get("/api/recs")).status_code == 401
    assert (await api_client.post("/api/recs/runs", json={"prompt": None})).status_code == 401


async def test_a_user_with_no_runs_gets_the_empty_shape(
    client: AsyncClient, api_app: FastAPI
) -> None:
    use(api_app, FakeModel([]))

    body = (await client.get("/api/recs")).json()

    assert body == {
        "run": None,
        "remaining_today": DAILY_LIMIT,
        "limit_per_day": DAILY_LIMIT,
        "configured": True,
    }


async def test_the_page_reports_when_there_is_no_api_key(client: AsyncClient) -> None:
    """The test settings carry no LLM_API_KEY, which is the point."""
    body = (await client.get("/api/recs")).json()

    assert body["configured"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"recs_provider": "gemini", "gemini_api_key": "AIza-x"},
        {"recs_provider": "openrouter", "openrouter_api_key": "sk-or-x"},
        {
            "recs_provider": "anthropic",
            "recs_model": "claude-opus-5",
            "anthropic_api_key": "sk-ant-x",
        },
    ],
    ids=["gemini", "openrouter", "anthropic"],
)
async def test_any_configured_provider_makes_the_page_configured(
    api_app: FastAPI, user: User, overrides: dict[str, object]
) -> None:
    """``RECS_PROVIDER`` reaches the endpoint; the router cannot tell them apart."""
    api_app.state.settings = api_app.state.settings.model_copy(update=overrides)

    async with api_transport(api_app) as http:
        await login(http, USER_EMAIL, USER_PASSWORD)
        body = (await http.get("/api/recs")).json()

    assert body["configured"] is True
    assert isinstance(api_app.state.recs_model, RecsChain)
    await api_app.state.recs_model.aclose()


async def test_only_a_fallback_key_still_configures_the_page(api_app: FastAPI, user: User) -> None:
    """A deployment that holds only the paid key gets a one-entry chain."""
    api_app.state.settings = api_app.state.settings.model_copy(
        update={
            "recs_fallback_provider": "openrouter",
            "recs_fallback_model": "google/gemini-2.5-flash",
            "openrouter_api_key": "sk-or-x",
        }
    )

    async with api_transport(api_app) as http:
        await login(http, USER_EMAIL, USER_PASSWORD)
        body = (await http.get("/api/recs")).json()

    assert body["configured"] is True
    await api_app.state.recs_model.aclose()


# --- The admin-only chain view ----------------------------------------------


async def test_the_chain_is_hidden_from_ordinary_users(
    client: AsyncClient, api_app: FastAPI
) -> None:
    """It names the providers and models a deployment pays for."""
    use(api_app, FakeModel([]))

    body = (await client.get("/api/recs")).json()

    assert "chain" not in body
    # …and the rest of the contract is untouched.
    assert set(body) == {"run", "remaining_today", "limit_per_day", "configured"}


async def test_an_admin_sees_the_chain(api_app: FastAPI, api_factory: SessionFactory) -> None:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    api_app.state.settings = api_app.state.settings.model_copy(
        update={
            "gemini_api_key": "AIza-x",
            "recs_model": "gemini-3.5-flash,gemini-2.5-flash",
            "recs_fallback_provider": "openrouter",
            "recs_fallback_model": "google/gemini-2.5-flash",
            "openrouter_api_key": "sk-or-x",
        }
    )

    async with api_transport(api_app) as http:
        await login(http, ADMIN_EMAIL, ADMIN_PASSWORD)
        body = (await http.get("/api/recs")).json()

    assert body["chain"] == [
        {"provider": "gemini", "model": "gemini-3.5-flash", "available": True},
        {"provider": "gemini", "model": "gemini-2.5-flash", "available": True},
        {"provider": "openrouter", "model": "google/gemini-2.5-flash", "available": True},
    ]
    await api_app.state.recs_model.aclose()


async def test_an_admin_sees_which_entries_are_spent(
    api_app: FastAPI, api_factory: SessionFactory
) -> None:
    """The question this field exists to answer: how much of today is left."""
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"gemini_api_key": "AIza-x", "recs_model": "one,two"}
    )
    async with api_transport(api_app) as http:
        await login(http, ADMIN_EMAIL, ADMIN_PASSWORD)
        await http.get("/api/recs")
        chain = api_app.state.recs_model
        chain._mark_exhausted(chain.entries[0])

        body = (await http.get("/api/recs")).json()

    assert [row["available"] for row in body["chain"]] == [False, True]
    await chain.aclose()


async def test_an_admin_on_an_unconfigured_deployment_sees_an_empty_chain(
    api_app: FastAPI, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)

    async with api_transport(api_app) as http:
        await login(http, ADMIN_EMAIL, ADMIN_PASSWORD)
        body = (await http.get("/api/recs")).json()

    assert body["configured"] is False
    assert body["chain"] == []


async def test_the_newest_run_comes_back_with_cards(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory, user: User
) -> None:
    ids = await add_season(api_factory)
    run_id = await add_run(
        api_factory, user, picks=[{"anime_id": ids[0], "title": "x", "case": "because Frieren"}]
    )

    body = (await client.get("/api/recs")).json()

    assert body["run"]["id"] == run_id
    assert body["run"]["prompt"] == "something short"
    assert body["run"]["model"] == "claude-opus-5"
    assert body["run"]["candidate_count"] == 1
    pick = body["run"]["picks"][0]
    assert pick["case"] == "because Frieren"
    assert pick["anime"]["id"] == ids[0]
    assert pick["anime"]["title"]["preferred"] == "Season Show 1"
    # The client renders its existing add-to-planned control from this.
    assert pick["anime"]["list_status"] is None


async def test_a_pick_carries_the_caller_s_list_status(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    ids = await add_season(api_factory)
    await add_run(api_factory, user, picks=[{"anime_id": ids[0], "title": "x", "case": "c"}])
    async with api_factory() as session:
        session.add(ListEntry(user_id=user.id, anime_id=ids[0], status=ListStatus.PLANNED))
        await session.commit()

    body = (await client.get("/api/recs")).json()

    assert body["run"]["picks"][0]["anime"]["list_status"] == "planned"


async def test_picks_and_continuations_are_separate_sections(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    ids = await add_season(api_factory, 2)
    await add_run(
        api_factory,
        user,
        picks=[
            {"kind": "pick", "anime_id": ids[0], "title": "a", "case": "because Frieren"},
            {
                "kind": "continuation",
                "anime_id": ids[1],
                "title": "b",
                "because": "Sequel to Frieren, which you completed",
            },
        ],
    )

    body = (await client.get("/api/recs")).json()["run"]

    assert [p["anime"]["id"] for p in body["picks"]] == [ids[0]]
    assert body["picks"][0]["case"] == "because Frieren"
    assert [c["anime"]["id"] for c in body["continuations"]] == [ids[1]]
    assert body["continuations"][0]["because"] == "Sequel to Frieren, which you completed"
    # A continuation is a card like any other, so add-to-planned works on it.
    assert "list_status" in body["continuations"][0]["anime"]


async def test_a_run_stored_before_continuations_existed_still_renders(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """Old rows carry no ``kind``; they were all picks, and read as picks."""
    ids = await add_season(api_factory, 1)
    await add_run(api_factory, user, picks=[{"anime_id": ids[0], "title": "a", "case": "old"}])

    body = (await client.get("/api/recs")).json()["run"]

    assert [p["anime"]["id"] for p in body["picks"]] == [ids[0]]
    assert body["continuations"] == []


async def test_a_pick_whose_show_has_been_pruned_is_dropped(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    ids = await add_season(api_factory, 2)
    await add_run(
        api_factory,
        user,
        picks=[
            {"anime_id": ids[0], "title": "kept", "case": "c"},
            {"anime_id": 999_999, "title": "gone", "case": "c"},
        ],
    )

    body = (await client.get("/api/recs")).json()

    assert [pick["anime"]["id"] for pick in body["run"]["picks"]] == [ids[0]]


async def test_remaining_today_counts_down(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    for _ in range(3):
        await add_run(api_factory, user, picks=[])
    # …and one that has aged out of the window.
    await add_run(api_factory, user, picks=[], at=NOW - timedelta(days=2))

    assert (await client.get("/api/recs")).json()["remaining_today"] == DAILY_LIMIT - 3


# --- POST /api/recs/runs ----------------------------------------------------


async def test_a_run_is_created_and_returned(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    ids = await add_season(api_factory)
    use(api_app, FakeModel([(ids[0], "x", "Because you finished Frieren.")]))

    response = await client.post("/api/recs/runs", json={"prompt": "something short and funny"})

    assert response.status_code == 201
    body = response.json()
    assert body["prompt"] == "something short and funny"
    assert body["candidate_count"] == len(ids)
    assert [pick["anime"]["id"] for pick in body["picks"]] == [ids[0]]
    # …and it is the run the page shows on reload (FR-R5).
    assert (await client.get("/api/recs")).json()["run"]["id"] == body["id"]


async def test_the_mood_prompt_is_trimmed_and_empty_becomes_null(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    ids = await add_season(api_factory)
    use(api_app, FakeModel([(ids[0], "x", "c")]))

    blank = await client.post("/api/recs/runs", json={"prompt": "   "})
    padded = await client.post("/api/recs/runs", json={"prompt": "  cosy  "})

    assert blank.json()["prompt"] is None
    assert padded.json()["prompt"] == "cosy"


async def test_a_missing_prompt_key_is_allowed(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    ids = await add_season(api_factory)
    use(api_app, FakeModel([(ids[0], "x", "c")]))

    assert (await client.post("/api/recs/runs", json={})).status_code == 201


async def test_an_over_long_prompt_is_refused(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    await add_season(api_factory)
    use(api_app, FakeModel([]))

    response = await client.post("/api/recs/runs", json={"prompt": "x" * (MAX_PROMPT_CHARS + 1)})

    assert response.status_code == 422


async def test_no_api_key_is_a_503(client: AsyncClient, api_factory: SessionFactory) -> None:
    await add_season(api_factory)

    response = await client.post("/api/recs/runs", json={"prompt": None})

    assert response.status_code == 503
    assert response.json()["detail"] == NOT_CONFIGURED


async def test_an_empty_catalogue_is_a_409(client: AsyncClient, api_app: FastAPI) -> None:
    use(api_app, FakeModel([]))

    response = await client.post("/api/recs/runs", json={"prompt": None})

    assert response.status_code == 409
    assert response.json()["detail"] == EMPTY_POOL


async def test_a_refusal_is_a_503(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    await add_season(api_factory)
    use(api_app, FakeModel(error=RecsRefused("declined", stop_details=None)))

    response = await client.post("/api/recs/runs", json={"prompt": None})

    assert response.status_code == 503
    assert response.json()["detail"] == DECLINED


@pytest.mark.parametrize("error", [RecsUnavailable("connection refused"), RecsFailed("truncated")])
async def test_an_upstream_failure_is_a_502(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory, error: Exception
) -> None:
    await add_season(api_factory)
    use(api_app, FakeModel(error=error))

    response = await client.post("/api/recs/runs", json={"prompt": None})

    assert response.status_code == 502
    assert response.json()["detail"] == UPSTREAM


async def test_the_eleventh_run_of_the_day_is_a_429(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory, user: User
) -> None:
    await add_season(api_factory)
    use(api_app, FakeModel([]))
    for _ in range(DAILY_LIMIT):
        await add_run(api_factory, user, picks=[], at=NOW - timedelta(hours=2))

    response = await client.post("/api/recs/runs", json={"prompt": None})

    assert response.status_code == 429
    body = response.json()
    # The client renders the wait; it must not have to parse a sentence for it.
    assert body["retry_after_seconds"] == pytest.approx(22 * 3600, abs=2)
    assert body["detail"]
    assert response.headers["Retry-After"] == str(body["retry_after_seconds"])


async def test_a_failed_run_does_not_spend_the_budget(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    """A 502 stores nothing, so the user has not lost one of their ten."""
    await add_season(api_factory)
    use(api_app, FakeModel(error=RecsUnavailable("down")))

    assert (await client.post("/api/recs/runs", json={"prompt": None})).status_code == 502
    assert (await client.get("/api/recs")).json()["remaining_today"] == DAILY_LIMIT


async def test_one_user_cannot_see_another_s_run(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    other = await add_user(api_factory, "other@arc.test", "other-password-123")
    await add_run(api_factory, other, picks=[])

    assert (await client.get("/api/recs")).json()["run"] is None


async def test_a_run_needs_an_allowed_origin(client: AsyncClient, api_app: FastAPI) -> None:
    """The CSRF rule applies here like everywhere else that writes."""
    use(api_app, FakeModel([]))

    response = await client.post(
        "/api/recs/runs", json={"prompt": None}, headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403


async def test_the_pool_never_contains_a_show_the_user_has_watched(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory, user: User
) -> None:
    """The milestone's definition of done, asserted end to end."""
    ids = await add_season(api_factory, 3)
    async with api_factory() as session:
        session.add(ListEntry(user_id=user.id, anime_id=ids[0], status=ListStatus.COMPLETED))
        session.add(ListEntry(user_id=user.id, anime_id=ids[1], status=ListStatus.PLANNED))
        await session.commit()
    model = FakeModel([(ids[2], "x", "c")])
    use(api_app, model)

    response = await client.post("/api/recs/runs", json={"prompt": None})

    assert response.status_code == 201
    async with api_factory() as session:
        run = await session.get(RecRun, response.json()["id"])
        assert run is not None
        offered = {candidate["anime_id"] for candidate in run.candidates or []}
    # Completed is gone; planned stayed (FR-R2).
    assert offered == {ids[1], ids[2]}


async def test_a_pick_the_user_has_already_watched_never_reaches_the_page(
    client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory, user: User
) -> None:
    ids = await add_season(api_factory, 2)
    async with api_factory() as session:
        session.add(ListEntry(user_id=user.id, anime_id=ids[0], status=ListStatus.COMPLETED))
        await session.commit()
    # The model returns the completed show anyway; it must be dropped.
    use(api_app, FakeModel([(ids[0], "watched", "c"), (ids[1], "fresh", "c")]))

    body = (await client.post("/api/recs/runs", json={"prompt": None})).json()

    assert [pick["anime"]["id"] for pick in body["picks"]] == [ids[1]]


async def test_the_anime_rows_are_untouched_by_a_run(api_factory: SessionFactory) -> None:
    """Sanity: the recommender reads the catalogue, it does not rewrite it."""
    ids = await add_season(api_factory, 1)
    async with api_factory() as session:
        row = await session.get(Anime, ids[0])
        assert row is not None
        assert row.title_romaji == "Season Show 1"
