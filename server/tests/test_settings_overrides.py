"""The per-show override editor: service, routes, and what the ranker reads.

FR-A3 allows a per-show override for group and resolution; until M16 the row
could only be written by hand. These are the three things worth pinning about
the editor that writes it now (owner 2026-09-18):

1. **What is written is what the ranker reads.** The proof is
   :func:`~arc.services.acquisition.rules.load_rules` asked for the same show
   straight after the write — the reader acquisition itself calls, not a
   re-read of the row. The *pick* a rule produces is already pinned by
   ``test_acquisition_jobs.py::test_a_per_show_override_changes_the_pick`` and
   by the ranking corpus in ``test_nyaa.py``; nothing here repeats them.
2. **An override that names nothing is a deletion.** The stored ``{}`` would
   be a show the admin panel lists as not following the global rules while
   following them exactly.
3. **Only an admin, and only what an admin can see.** The routes are behind
   the router's own dependency, and the show page's ``override`` field is
   null for everybody else — the client has no admin-only page to hide it on.

The validation matrix is in ``tests/test_settings_rules.py``: it is a pure
function and needs neither a database nor a client.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from arc.db import SessionFactory
from arc.models import Job, Setting, UserRole
from arc.services.acquisition.rules import load_rules, override_key
from arc.services.settings import (
    NoSuchAnime,
    SettingsInvalid,
    delete_override,
    read_override,
    write_override,
)
from tests.acquisition_helpers import make_anime
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "member-overrides@arc.test"
USER_PASSWORD = "memberpassword"

#: An id no ``anime`` row will ever have in a truncated test database.
MISSING_ANIME_ID = 987654


def path(anime_id: int) -> str:
    return f"/api/settings/overrides/{anime_id}"


async def stored(session: AsyncSession, anime_id: int) -> Any:
    """The raw ``settings`` value for one show, or ``None`` when there is none."""
    return await session.scalar(select(Setting.value).where(Setting.key == override_key(anime_id)))


# --- The service ------------------------------------------------------------


async def test_what_is_written_is_what_the_ranker_reads(db_session: AsyncSession) -> None:
    """The point of the editor (FR-A3): one row, one reader, no second path."""
    anime = await make_anime(db_session, anilist_id=991001)
    await write_override(
        db_session, anime_id=anime.id, preferred_groups=["Erai-raws"], resolution="720p"
    )

    rules = await load_rules(db_session, anime.id)

    assert rules.preferred_groups == ("Erai-raws",)
    assert rules.preferred_resolution == "720p"
    assert rules.overridden is True
    # And the global rules are untouched: the override is about one show.
    assert (await load_rules(db_session)).overridden is False


async def test_an_override_naming_one_field_leaves_the_other_global(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=991002)
    global_rules = await load_rules(db_session)
    await write_override(db_session, anime_id=anime.id, resolution="480p")

    rules = await load_rules(db_session, anime.id)

    assert rules.preferred_resolution == "480p"
    assert rules.preferred_groups == global_rules.preferred_groups
    # The fallback is deliberately left alone: "I want 480p here" is a
    # preference, not "and never accept anything else".
    assert rules.fallback_resolution == global_rules.fallback_resolution


async def test_the_written_row_is_normalised_not_echoed(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=991003)

    override = await write_override(
        db_session,
        anime_id=anime.id,
        preferred_groups=["  SubsPlease ", "subsplease", "ASW"],
        resolution="1080p",
    )

    assert override is not None
    assert override.preferred_groups == ["SubsPlease", "ASW"]
    assert await stored(db_session, anime.id) == {
        "preferred_groups": ["SubsPlease", "ASW"],
        "resolution": "1080p",
    }


async def test_a_second_write_replaces_the_first_and_logs_the_old_value(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """ "Why is this show on 720p?" has to be answerable from the log."""
    anime = await make_anime(db_session, anilist_id=991004)
    await write_override(db_session, anime_id=anime.id, resolution="1080p")

    with caplog.at_level("INFO", logger="arc.services.settings"):
        # Only the second write's line: the first one is set-up, and whether it
        # was captured at all depends on what the rest of the suite has done to
        # this logger.
        caplog.clear()
        await write_override(
            db_session, anime_id=anime.id, preferred_groups=["ASW"], resolution="720p", admin_id=7
        )

    assert await stored(db_session, anime.id) == {
        "preferred_groups": ["ASW"],
        "resolution": "720p",
    }
    changes = [record for record in caplog.records if record.message == "per-show override changed"]
    assert len(changes) == 1
    assert changes[0].old == {"resolution": "1080p"}  # type: ignore[attr-defined]
    assert changes[0].admin_id == 7  # type: ignore[attr-defined]


async def test_writing_the_same_override_again_changes_nothing(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Idempotent, and no log line claiming a change nobody made."""
    anime = await make_anime(db_session, anilist_id=991005)
    await write_override(db_session, anime_id=anime.id, resolution="720p")

    with caplog.at_level("INFO", logger="arc.services.settings"):
        caplog.clear()
        override = await write_override(db_session, anime_id=anime.id, resolution="720p")

    assert override is not None
    assert override.resolution == "720p"
    assert [record for record in caplog.records if "override" in record.message] == []


async def test_an_override_that_names_nothing_removes_the_row(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=991006)
    await write_override(db_session, anime_id=anime.id, resolution="720p")

    assert await write_override(db_session, anime_id=anime.id) is None

    assert await stored(db_session, anime.id) is None
    assert (await load_rules(db_session, anime.id)).overridden is False


async def test_an_override_for_a_show_arc_does_not_have_is_refused(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(NoSuchAnime):
        await write_override(db_session, anime_id=MISSING_ANIME_ID, resolution="720p")

    assert await stored(db_session, MISSING_ANIME_ID) is None


async def test_a_refused_value_writes_nothing(db_session: AsyncSession) -> None:
    anime = await make_anime(db_session, anilist_id=991007)

    with pytest.raises(SettingsInvalid) as caught:
        await write_override(
            db_session, anime_id=anime.id, preferred_groups=["ASW"], resolution="4K"
        )

    assert "resolution" in caught.value.errors
    assert await stored(db_session, anime.id) is None


async def test_removing_an_override_is_idempotent(db_session: AsyncSession) -> None:
    """The second press of Remove is not an error; it is the state wanted."""
    anime = await make_anime(db_session, anilist_id=991008)
    await write_override(db_session, anime_id=anime.id, resolution="720p")

    assert await delete_override(db_session, anime_id=anime.id) is True
    assert await delete_override(db_session, anime_id=anime.id) is False
    assert await stored(db_session, anime.id) is None


async def test_an_override_outliving_its_show_can_still_be_removed(
    db_session: AsyncSession,
) -> None:
    """The one case the write refuses and the delete must not."""
    await db_session.execute(
        text("INSERT INTO settings (key, value) VALUES (:key, CAST(:value AS jsonb))"),
        {"key": override_key(MISSING_ANIME_ID), "value": '{"resolution": "720p"}'},
    )

    assert await delete_override(db_session, anime_id=MISSING_ANIME_ID) is True


async def test_reading_one_show_answers_the_row_with_its_title(
    db_session: AsyncSession,
) -> None:
    anime = await make_anime(db_session, anilist_id=991009, english="Frieren")
    assert await read_override(db_session, anime.id) is None

    await write_override(db_session, anime_id=anime.id, preferred_groups=["ASW"])

    override = await read_override(db_session, anime.id)
    assert override is not None
    assert override.anime_id == anime.id
    assert override.title == "Frieren"
    assert override.preferred_groups == ["ASW"]
    assert override.resolution is None


async def test_a_malformed_row_reads_as_no_override(db_session: AsyncSession) -> None:
    """Lenient like the rule readers: a hand-edited row must not raise."""
    anime = await make_anime(db_session, anilist_id=991010)
    await db_session.execute(
        text("INSERT INTO settings (key, value) VALUES (:key, CAST(:value AS jsonb))"),
        {"key": override_key(anime.id), "value": '"720p"'},
    )

    assert await read_override(db_session, anime.id) is None


# --- The routes -------------------------------------------------------------


async def make_show(factory: SessionFactory, *, anilist_id: int, english: str) -> int:
    async with factory() as session:
        anime = await make_anime(session, anilist_id=anilist_id, english=english)
        await session.commit()
        return anime.id


async def test_a_put_writes_the_override_and_answers_it(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await make_show(api_factory, anilist_id=992001, english="Frieren")

    response = await admin_client.put(
        path(anime_id), json={"preferred_groups": ["ASW", "asw"], "resolution": "720p"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "anime_id": anime_id,
        "title": "Frieren",
        "preferred_groups": ["ASW"],
        "resolution": "720p",
    }
    # And the rules page lists what the show page wrote.
    assert (await admin_client.get("/api/settings")).json()["overrides"] == [response.json()]


async def test_a_put_naming_neither_field_removes_the_override(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Save with an empty groups box and "Use global" is a removal."""
    anime_id = await make_show(api_factory, anilist_id=992002, english="Frieren")
    await admin_client.put(path(anime_id), json={"resolution": "720p"})

    response = await admin_client.put(
        path(anime_id), json={"preferred_groups": None, "resolution": None}
    )

    assert response.status_code == 200, response.text
    assert response.json() is None
    assert (await admin_client.get("/api/settings")).json()["overrides"] == []


async def test_a_delete_answers_204_twice(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await make_show(api_factory, anilist_id=992003, english="Frieren")
    await admin_client.put(path(anime_id), json={"resolution": "720p"})

    assert (await admin_client.delete(path(anime_id))).status_code == 204
    assert (await admin_client.delete(path(anime_id))).status_code == 204
    assert (await admin_client.get("/api/settings")).json()["overrides"] == []


async def test_writing_an_override_queues_nothing(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """A rule change takes effect on the next search, like the global ones.

    The only ``settings`` write that queues work is *clearing the pause*, and
    for a reason of its own (``test_settings_api.py``). Nothing already
    downloading is disturbed, which is what the rules editor promises.
    """
    anime_id = await make_show(api_factory, anilist_id=992004, english="Frieren")

    await admin_client.put(path(anime_id), json={"resolution": "720p"})
    await admin_client.delete(path(anime_id))

    async with api_factory() as session:
        assert list((await session.scalars(select(Job.type))).all()) == []


async def test_a_put_for_a_show_arc_does_not_have_is_a_404(admin_client: AsyncClient) -> None:
    response = await admin_client.put(path(MISSING_ANIME_ID), json={"resolution": "720p"})

    assert response.status_code == 404
    assert response.json()["detail"] == "anime not found"


async def test_a_refused_value_is_a_422_naming_the_field(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The shape the rules editor's own 422 has, so the client has one reader."""
    anime_id = await make_show(api_factory, anilist_id=992005, english="Frieren")

    response = await admin_client.put(path(anime_id), json={"resolution": "4K"})

    assert response.status_code == 422
    entry = response.json()["detail"][0]
    assert entry["loc"] == ["body", "resolution"]
    assert "2160p" in entry["msg"]
    assert entry["type"] == "value_error"
    assert (await admin_client.get("/api/settings")).json()["overrides"] == []


async def test_a_group_list_that_is_not_one_is_a_422(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Pydantic's own refusal, in the same ``detail`` shape."""
    anime_id = await make_show(api_factory, anilist_id=992006, english="Frieren")

    response = await admin_client.put(path(anime_id), json={"preferred_groups": "ASW"})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", "preferred_groups"]


async def test_a_field_nobody_declared_is_a_422(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Refused rather than silently dropped: an override has two fields."""
    anime_id = await make_show(api_factory, anilist_id=992007, english="Frieren")

    response = await admin_client.put(path(anime_id), json={"look_ahead_n": 9})

    assert response.status_code == 422


async def test_an_over_long_group_entry_is_refused(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await make_show(api_factory, anilist_id=992008, english="Frieren")

    response = await admin_client.put(path(anime_id), json={"preferred_groups": ["x" * 65]})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "preferred_groups"]


# --- Who may look, and where the show page reads it -------------------------


async def test_the_show_page_carries_the_override_for_an_admin(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """One request for the page and the editor both (M16)."""
    anime_id = await make_show(api_factory, anilist_id=992010, english="Frieren")
    assert (await admin_client.get(f"/api/anime/{anime_id}")).json()["override"] is None

    await admin_client.put(path(anime_id), json={"preferred_groups": ["ASW"]})

    body = (await admin_client.get(f"/api/anime/{anime_id}")).json()
    assert body["override"] == {
        "anime_id": anime_id,
        "title": "Frieren",
        "preferred_groups": ["ASW"],
        "resolution": None,
    }


async def test_a_non_admin_sees_no_override_on_the_show_page(
    admin_client: AsyncClient, api_app: Any, api_factory: SessionFactory
) -> None:
    anime_id = await make_show(api_factory, anilist_id=992011, english="Frieren")
    await admin_client.put(path(anime_id), json={"preferred_groups": ["ASW"]})
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD, role=UserRole.USER)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    assert body["override"] is None


async def test_a_non_admin_may_not_edit_an_override(
    api_app: Any, api_factory: SessionFactory
) -> None:
    anime_id = await make_show(api_factory, anilist_id=992012, english="Frieren")
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD, role=UserRole.USER)

    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        assert (await client.put(path(anime_id), json={"resolution": "720p"})).status_code == 403
        assert (await client.delete(path(anime_id))).status_code == 403


async def test_an_anonymous_caller_gets_401(api_client: AsyncClient) -> None:
    assert (await api_client.put(path(1), json={"resolution": "720p"})).status_code == 401
    assert (await api_client.delete(path(1))).status_code == 401
