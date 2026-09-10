"""The admin rules editor over HTTP: ``GET``/``PUT /api/settings`` (FR-D2).

The validation matrix lives in ``tests/test_settings_rules.py`` — this file is
about the wiring: the JSON shape the client renders, that a partial write is
partial, that the values reach the rule readers acquisition and retention
actually use, and that nobody but an admin gets near any of it.

``settings`` is not in ``CLEANUP_TABLES`` (conftest) — truncating it would
leave a database with no seeded rules at all — so it is *restored* rather than
emptied, by ``_reseed_settings`` in the shared teardown. That is why these
tests can assert on the whole mapping without a fixture of their own.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from arc.db import SessionFactory
from arc.models import DEFAULT_SETTINGS, Job, UserRole
from arc.services.acquisition.names import COMPUTE_WANTS
from arc.services.acquisition.rules import load_rules, look_ahead_n, override_key
from arc.services.retention.rules import grace_days, unwatched_days
from arc.services.settings import FALLBACK_EQUALS_PREFERRED, UNKNOWN_KEY
from tests.acquisition_helpers import make_anime
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "member-settings@arc.test"
USER_PASSWORD = "memberpassword"


async def put(client: AsyncClient, body: dict[str, Any]) -> Any:
    response = await client.put("/api/settings", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --- GET --------------------------------------------------------------------


async def test_the_page_carries_the_values_the_defaults_and_the_overrides(
    admin_client: AsyncClient,
) -> None:
    body = (await admin_client.get("/api/settings")).json()

    assert sorted(body) == ["defaults", "overrides", "values"]
    assert body["values"] == dict(DEFAULT_SETTINGS)
    assert body["defaults"] == dict(DEFAULT_SETTINGS)
    assert body["overrides"] == []


async def test_the_defaults_never_move_when_the_values_do(admin_client: AsyncClient) -> None:
    """The client offers "reset to default" from this field, not from a copy."""
    body = await put(admin_client, {"look_ahead_n": 5})

    assert body["values"]["look_ahead_n"] == 5
    assert body["defaults"]["look_ahead_n"] == DEFAULT_SETTINGS["look_ahead_n"]


async def test_a_per_show_override_is_listed_with_its_title(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=990001, english="Frieren")
        await session.commit()
        anime_id = anime.id
    async with api_factory() as session:
        await session.execute(
            text("INSERT INTO settings (key, value) VALUES (:key, CAST(:value AS jsonb))"),
            {
                "key": override_key(anime_id),
                "value": '{"preferred_groups": ["ASW"], "resolution": "720p"}',
            },
        )
        await session.commit()

    body = (await admin_client.get("/api/settings")).json()

    assert body["overrides"] == [
        {
            "anime_id": anime_id,
            "title": "Frieren",
            "preferred_groups": ["ASW"],
            "resolution": "720p",
        }
    ]
    # Read-only here: an override is not one of the editable keys (M16).
    assert override_key(anime_id) not in body["values"]


async def test_a_malformed_override_row_is_skipped_rather_than_shown(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        await session.execute(
            text("INSERT INTO settings (key, value) VALUES (:key, CAST(:value AS jsonb))"),
            {"key": "override:anime:not-a-number", "value": '{"resolution": "720p"}'},
        )
        await session.commit()

    assert (await admin_client.get("/api/settings")).json()["overrides"] == []


# --- PUT --------------------------------------------------------------------


async def test_a_partial_put_leaves_everything_else_alone(admin_client: AsyncClient) -> None:
    body = await put(admin_client, {"grace_days_g": 3})

    assert body["values"]["grace_days_g"] == 3
    assert body["values"]["unwatched_days_d"] == DEFAULT_SETTINGS["unwatched_days_d"]
    assert body["values"]["preferred_resolution"] == DEFAULT_SETTINGS["preferred_resolution"]


async def test_a_put_answers_the_same_shape_as_a_get(admin_client: AsyncClient) -> None:
    """One response type, so the client has one renderer and no refetch."""
    written = await put(admin_client, {"look_ahead_n": 4})
    read = (await admin_client.get("/api/settings")).json()

    assert written == read


async def test_the_values_written_are_the_ones_the_rule_readers_use(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The DoD: editable without touching env or the database."""
    await put(
        admin_client,
        {
            "preferred_groups": ["  Erai-raws ", "erai-raws", "SubsPlease"],
            "preferred_resolution": "2160p",
            "fallback_resolution": "1080p",
            "look_ahead_n": 6,
            "grace_days_g": 2,
            "unwatched_days_d": 30,
            "sub_lang": "PT-BR",
        },
    )

    async with api_factory() as session:
        rules = await load_rules(session)
        assert rules.preferred_groups == ("Erai-raws", "SubsPlease")
        assert rules.preferred_resolution == "2160p"
        assert rules.fallback_resolution == "1080p"
        assert await look_ahead_n(session) == 6
        assert await grace_days(session) == 2
        assert await unwatched_days(session) == 30

    assert (await admin_client.get("/api/settings")).json()["values"]["sub_lang"] == "pt-br"


async def test_pausing_through_the_settings_api_is_the_same_switch(
    admin_client: AsyncClient,
) -> None:
    """FR-D2 and the pause button write one key; the status route agrees."""
    await put(admin_client, {"acquisition_paused": True})

    assert (await admin_client.get("/api/acquisition/status")).json()["paused"] is True

    await put(admin_client, {"acquisition_paused": False})
    assert (await admin_client.get("/api/acquisition/status")).json()["paused"] is False


async def compute_wants_jobs(factory: SessionFactory) -> list[int]:
    """The ids of every queued ``compute_wants``, oldest first."""
    async with factory() as session:
        rows = await session.scalars(
            select(Job.id).where(Job.type == COMPUTE_WANTS).order_by(Job.id)
        )
        return list(rows.all())


async def test_unpausing_from_the_rules_editor_queues_the_recompute(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Parity with ``POST /api/acquisition/resume``.

    Without it the button starts fetching again at once and the rules editor
    leaves Arc idle until the fifteen-minute tick — one switch that means two
    different things depending on the screen it was flipped from.
    """
    await put(admin_client, {"acquisition_paused": True})
    assert await compute_wants_jobs(api_factory) == [], "pausing queues nothing"

    await put(admin_client, {"acquisition_paused": False})

    assert len(await compute_wants_jobs(api_factory)) == 1


async def test_a_put_that_does_not_change_the_pause_queues_nothing(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """It is already false; writing false again is not a resume."""
    await put(admin_client, {"acquisition_paused": False, "look_ahead_n": 3})

    assert await compute_wants_jobs(api_factory) == []


async def test_the_resume_route_and_the_editor_queue_the_same_one_job(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Both go through the same deduped enqueue, so pressing both is one job."""
    await put(admin_client, {"acquisition_paused": True})
    await put(admin_client, {"acquisition_paused": False})
    await admin_client.post("/api/acquisition/pause")
    await admin_client.post("/api/acquisition/resume")

    assert len(await compute_wants_jobs(api_factory)) == 1


async def test_a_stored_collision_does_not_block_an_unrelated_edit(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """A hand-edited pair is left alone rather than failing every later PUT."""
    async with api_factory() as session:
        await session.execute(
            text("UPDATE settings SET value = '\"1080p\"'::jsonb WHERE key = :key"),
            {"key": "fallback_resolution"},
        )
        await session.commit()

    body = await put(admin_client, {"look_ahead_n": 3})

    assert body["values"]["look_ahead_n"] == 3
    assert body["values"]["fallback_resolution"] == "1080p"


async def test_an_empty_body_changes_nothing(admin_client: AsyncClient) -> None:
    assert (await put(admin_client, {}))["values"] == dict(DEFAULT_SETTINGS)


# --- Refusals ---------------------------------------------------------------


def _fields(payload: Any) -> list[str]:
    """The keys a 422 blamed, out of FastAPI's ``detail`` shape."""
    return sorted(entry["loc"][-1] for entry in payload["detail"])


async def test_an_unknown_key_is_a_422_and_writes_nothing(admin_client: AsyncClient) -> None:
    response = await admin_client.put("/api/settings", json={"look_ahead_n": 9, "hosting": "aws"})

    assert response.status_code == 422
    assert _fields(response.json()) == ["hosting"]
    assert response.json()["detail"][0]["msg"] == UNKNOWN_KEY
    # The valid half of the same body must not have landed.
    body = (await admin_client.get("/api/settings")).json()
    assert body["values"]["look_ahead_n"] == DEFAULT_SETTINGS["look_ahead_n"]


async def test_a_bad_value_names_the_field_it_came_from(admin_client: AsyncClient) -> None:
    response = await admin_client.put("/api/settings", json={"look_ahead_n": 500})

    assert response.status_code == 422
    assert _fields(response.json()) == ["look_ahead_n"]


async def test_a_fallback_equal_to_the_stored_preferred_is_refused(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.put(
        "/api/settings", json={"fallback_resolution": DEFAULT_SETTINGS["preferred_resolution"]}
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == FALLBACK_EQUALS_PREFERRED


async def test_a_body_that_is_not_an_object_is_a_422(admin_client: AsyncClient) -> None:
    assert (await admin_client.put("/api/settings", json=[1, 2])).status_code == 422


# --- Who may look -----------------------------------------------------------


async def test_a_non_admin_may_neither_read_nor_write_the_rules(
    api_app: Any, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD, role=UserRole.USER)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        assert (await client.get("/api/settings")).status_code == 403
        assert (await client.put("/api/settings", json={"look_ahead_n": 1})).status_code == 403


async def test_an_anonymous_caller_gets_401(api_client: AsyncClient) -> None:
    assert (await api_client.get("/api/settings")).status_code == 401
    assert (await api_client.put("/api/settings", json={"look_ahead_n": 1})).status_code == 401
