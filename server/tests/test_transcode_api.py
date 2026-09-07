"""The playback-preparation half of the API (FR-P4, FR-P5).

Two things: the fields the show page renders a "preparing 43 %" badge, a
failure sentence and a ready episode from, and the admin button that queues a
transcode. Asserted on the JSON rather than on the schema objects — the shape
is the contract the client is generated from.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from arc.api.anime_schemas import MAX_FAILURE_CHARS
from arc.core.text import ELISION
from arc.db import SessionFactory
from arc.models import EpisodeState, Job, JobStatus, Rendition, UserRole
from arc.services.media.names import TRANSCODE, transcode_dedupe_key
from arc.services.media.plan import NOTE_BITMAP_ONLY
from tests.acquisition_helpers import make_anime, make_episodes
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "viewer@arc.test"
USER_PASSWORD = "viewerpassword"
ADMIN_EMAIL = "boss@arc.test"
ADMIN_PASSWORD = "bosspassword123"


async def a_show(factory: SessionFactory, *, anilist_id: int) -> tuple[int, list[int]]:
    """A show with four episodes, one per preparation state."""
    async with factory() as session:
        anime = await make_anime(session, anilist_id=anilist_id)
        episodes = await make_episodes(session, anime, 4, aired_through=4)
        ready, preparing, failed, matched = episodes
        ready.state = EpisodeState.READY
        preparing.state = EpisodeState.PREPARING
        failed.state = EpisodeState.FAILED
        matched.state = EpisodeState.MATCHED

        session.add(
            Rendition(
                episode_id=ready.id,
                dir=f"/data/renditions/{ready.id}",
                playlist_path=f"/data/renditions/{ready.id}/index.m3u8",
                duration=1418.5,
                width=1920,
                height=1080,
                subtitle_lang="en",
                audio_lang="ja",
            )
        )
        session.add(
            Job(
                type=TRANSCODE,
                payload={"episode_id": preparing.id, "progress": 0.43, "stage": "encode"},
                status=JobStatus.RUNNING,
            )
        )
        session.add(
            Job(
                type=TRANSCODE,
                payload={
                    "episode_id": failed.id,
                    "progress": 0.1,
                    "stage": "encode",
                    "error_tail": "ffmpeg exited 1\n[libx264] no such file or directory",
                },
                status=JobStatus.FAILED,
                attempts=3,
                max_attempts=3,
            )
        )
        await session.commit()
        return anime.id, [episode.id for episode in episodes]


async def episodes_of(api_app, factory: SessionFactory, anime_id: int) -> dict[int, dict]:
    await add_user(factory, USER_EMAIL, USER_PASSWORD)
    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        body = (await client.get(f"/api/anime/{anime_id}")).json()
    return {row["id"]: row for row in body["episodes"]}


# --- EpisodeOut -------------------------------------------------------------


async def test_the_show_page_reports_preparation_state_per_episode(
    api_app, api_factory: SessionFactory
) -> None:
    anime_id, ids = await a_show(api_factory, anilist_id=971001)
    rows = await episodes_of(api_app, api_factory, anime_id)
    ready, preparing, failed, matched = (rows[episode_id] for episode_id in ids)

    assert ready["state"] == "ready"
    assert ready["rendition"] == {
        "duration": pytest.approx(1418.5),
        "width": 1920,
        "height": 1080,
        "subtitle_lang": "en",
        "audio_lang": "ja",
        # No transcode job for this episode, so nothing to say — and an empty
        # list rather than null, so the client never writes the fallback.
        "notes": [],
    }
    assert ready["prepare_progress"] is None
    assert ready["failure_reason"] is None

    assert preparing["prepare_progress"] == pytest.approx(0.43)
    assert preparing["failure_reason"] is None
    assert preparing["rendition"] is None

    assert failed["failure_reason"].endswith("no such file or directory")
    assert failed["prepare_progress"] is None

    assert matched["prepare_progress"] is None
    assert matched["failure_reason"] is None
    assert matched["rendition"] is None


async def test_a_ready_episode_carries_the_notes_its_transcode_recorded(
    api_app, api_factory: SessionFactory
) -> None:
    """FR-P2's "flag it": *why* this rendition has no subtitles, in a sentence."""
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=971005)
        episodes = await make_episodes(session, anime, 1)
        episodes[0].state = EpisodeState.READY
        session.add(
            Rendition(
                episode_id=episodes[0].id,
                dir=f"/data/renditions/{episodes[0].id}",
                playlist_path=f"/data/renditions/{episodes[0].id}/index.m3u8",
                subtitle_lang=None,
            )
        )
        session.add(
            Job(
                type=TRANSCODE,
                payload={
                    "episode_id": episodes[0].id,
                    "stage": "done",
                    "progress": 1.0,
                    "notes": [NOTE_BITMAP_ONLY.format(codecs="hdmv_pgs_subtitle"), 7],
                },
                status=JobStatus.DONE,
            )
        )
        await session.commit()
        anime_id, episode_id = anime.id, episodes[0].id

    rows = await episodes_of(api_app, api_factory, anime_id)
    notes = rows[episode_id]["rendition"]["notes"]

    # The string survives; the hand-edited 7 is dropped rather than rendered.
    assert notes == [NOTE_BITMAP_ONLY.format(codecs="hdmv_pgs_subtitle")]


async def test_a_preparing_episode_with_no_job_yet_reports_zero_not_null(
    api_app, api_factory: SessionFactory
) -> None:
    """A queued transcode is 0 %, not "unknown": the client shows a bar either way."""
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=971002)
        episodes = await make_episodes(session, anime, 1)
        episodes[0].state = EpisodeState.PREPARING
        await session.commit()
        anime_id, episode_id = anime.id, episodes[0].id

    rows = await episodes_of(api_app, api_factory, anime_id)
    assert rows[episode_id]["prepare_progress"] == 0.0


async def test_a_failure_with_no_payload_falls_back_to_the_job_error(
    api_app, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=971003)
        episodes = await make_episodes(session, anime, 1)
        episodes[0].state = EpisodeState.FAILED
        session.add(
            Job(
                type=TRANSCODE,
                payload={"episode_id": episodes[0].id},
                status=JobStatus.FAILED,
                last_error="TranscodeError('ffmpeg is not installed')",
            )
        )
        await session.commit()
        anime_id, episode_id = anime.id, episodes[0].id

    rows = await episodes_of(api_app, api_factory, anime_id)
    assert "not installed" in rows[episode_id]["failure_reason"]


async def test_a_long_failure_still_begins_with_the_line_that_names_it(
    api_app, api_factory: SessionFactory
) -> None:
    """FR-P4's sentence is the *first* line, and it has to survive the trim.

    ffmpeg says what it could not do on its last line and why on its first, and
    the four thousand characters of stream layout in between are what a person
    scrolls past. Trimming to the last 500 characters — which is what this used
    to do — produced a failure that began half way through a line of x264
    diagnostics with no mention of ffmpeg having exited at all.
    """
    head = "ffmpeg exited 1"
    noise = "\n".join(f"[libx264 @ 0x1] stream mapping line {index}" for index in range(200))
    last = "Error opening output file index.m3u8."
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=971006)
        episodes = await make_episodes(session, anime, 1)
        episodes[0].state = EpisodeState.FAILED
        session.add(
            Job(
                type=TRANSCODE,
                payload={"episode_id": episodes[0].id, "error_tail": f"{head}\n{noise}\n{last}"},
                status=JobStatus.FAILED,
            )
        )
        await session.commit()
        anime_id, episode_id = anime.id, episodes[0].id

    reason = (await episodes_of(api_app, api_factory, anime_id))[episode_id]["failure_reason"]

    assert len(reason) <= MAX_FAILURE_CHARS
    assert reason.startswith(head)
    assert reason.endswith(last)
    # The middle is what went, and it says so rather than just stopping.
    assert ELISION.strip() in reason
    assert "stream mapping line 0" not in reason


async def test_only_the_latest_transcode_job_is_read(api_app, api_factory: SessionFactory) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=971004)
        episodes = await make_episodes(session, anime, 1)
        episodes[0].state = EpisodeState.PREPARING
        for progress in (0.1, 0.9):
            session.add(
                Job(
                    type=TRANSCODE,
                    payload={"episode_id": episodes[0].id, "progress": progress},
                    status=JobStatus.DONE,
                )
            )
            await session.flush()
        await session.commit()
        anime_id, episode_id = anime.id, episodes[0].id

    rows = await episodes_of(api_app, api_factory, anime_id)
    assert rows[episode_id]["prepare_progress"] == pytest.approx(0.9)


# --- The admin endpoint -----------------------------------------------------


async def test_a_signed_in_user_may_not_queue_a_transcode(
    api_app, api_factory: SessionFactory
) -> None:
    _, ids = await a_show(api_factory, anilist_id=971010)
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    async with api_transport(api_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        response = await client.post(f"/api/episodes/{ids[3]}/transcode")
    assert response.status_code == 403


async def test_an_anonymous_caller_is_refused(api_app, api_factory: SessionFactory) -> None:
    _, ids = await a_show(api_factory, anilist_id=971011)
    async with api_transport(api_app) as client:
        response = await client.post(f"/api/episodes/{ids[3]}/transcode")
    assert response.status_code == 401


async def test_an_admin_queues_a_transcode_for_a_matched_episode(
    api_app, api_factory: SessionFactory
) -> None:
    _, ids = await a_show(api_factory, anilist_id=971012)
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(api_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        response = await client.post(f"/api/episodes/{ids[3]}/transcode")
        again = await client.post(f"/api/episodes/{ids[3]}/transcode")

    assert response.status_code == 202
    body = response.json()
    assert body["type"] == TRANSCODE
    assert body["payload"]["episode_id"] == ids[3]
    assert body["payload"]["dedupe_key"] == transcode_dedupe_key(ids[3])
    # Pressing it twice queues one job, and says so with the same row.
    assert again.status_code == 202
    assert again.json()["id"] == body["id"]

    async with api_factory() as session:
        rows = await session.scalars(select(Job).where(Job.type == TRANSCODE))
        assert len([job for job in rows.all() if job.payload["episode_id"] == ids[3]]) == 1


async def test_a_failed_episode_can_be_retried_without_force(
    api_app, api_factory: SessionFactory
) -> None:
    _, ids = await a_show(api_factory, anilist_id=971013)
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(api_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        response = await client.post(f"/api/episodes/{ids[2]}/transcode")
    assert response.status_code == 202
    assert "force" not in response.json()["payload"]


async def test_a_ready_episode_needs_force(api_app, api_factory: SessionFactory) -> None:
    _, ids = await a_show(api_factory, anilist_id=971014)
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(api_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        refused = await client.post(f"/api/episodes/{ids[0]}/transcode")
        forced = await client.post(f"/api/episodes/{ids[0]}/transcode?force=true")

    assert refused.status_code == 409
    assert "force=true" in refused.json()["detail"]
    assert forced.status_code == 202
    assert forced.json()["payload"]["force"] is True


async def test_an_episode_with_nothing_to_prepare_is_a_conflict(
    api_app, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=971015)
        episodes = await make_episodes(session, anime, 1)
        episodes[0].state = EpisodeState.WANTED
        await session.commit()
        episode_id = episodes[0].id

    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(api_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        response = await client.post(f"/api/episodes/{episode_id}/transcode")
    assert response.status_code == 409
    assert "wanted" in response.json()["detail"]


async def test_an_unknown_episode_is_a_404(api_app, api_factory: SessionFactory) -> None:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(api_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        response = await client.post("/api/episodes/999999/transcode")
    assert response.status_code == 404


async def test_the_transcode_route_needs_an_allowed_origin(
    api_app, api_factory: SessionFactory
) -> None:
    _, ids = await a_show(api_factory, anilist_id=971016)
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(api_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        async with api_transport(api_app, origin="https://evil.example") as attacker:
            attacker.cookies = client.cookies
            response = await attacker.post(f"/api/episodes/{ids[3]}/transcode")
    assert response.status_code == 403
