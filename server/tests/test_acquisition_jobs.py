"""``search_release`` and ``poll_qbit`` end to end, with Nyaa and qBit mocked.

The handlers are exercised through :class:`JobContext` rather than through the
worker loop: the loop has its own tests, and what matters here is what one run
of a handler does to the database, to the client and to the queue.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState, Job, JobStatus, MediaFile, Torrent, Want
from arc.services.acquisition import jobs as acquisition_jobs
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition import qbit as qbit_module
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.jobs import (
    GIVE_UP_AFTER,
    NO_RELEASE,
    REMOVED_FROM_CLIENT,
    STALL_METADATA_AFTER,
    STALL_NO_BYTES_AFTER,
    STARTED_KEY,
    largest_video,
    poll_qbit,
    qbit_apply_policy,
    qbit_cancel,
    retry_delay,
    search_release,
    stall_reason,
)
from arc.services.acquisition.names import (
    QBIT_CANCEL,
    QBIT_POLICY,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
    search_dedupe_key,
)
from arc.services.acquisition.qbit import (
    QBIT_CANCELLED,
    QBIT_REJECTED,
    QBIT_STALLED,
    SEEDING_STATES,
    QbitUnavailable,
)
from arc.services.acquisition.rules import BYTES_PER_GB, PAUSED_KEY
from arc.services.acquisition.wants import cancel_if_unwanted
from arc.services.jobs.registry import JobContext
from arc.services.library.names import MATCH_FILE
from tests.acquisition_helpers import (
    NyaaStub,
    QbitStub,
    acquisition_settings,
    fake_free_space,
    force_transport,
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    read_fixture,
    set_setting,
)

pytestmark = pytest.mark.pg

FEED = read_fixture("search_frieren_07.xml")

#: The 1080p SubsPlease upload of season one's episode 7 in the fixture — the
#: release the default rules with SubsPlease preferred should land on.
SUBSPLEASE_1080 = "42d462368aed5f620f28ae99eacbbea776ed776d"


def context(
    session: AsyncSession,
    settings: Settings,
    payload: dict[str, object],
    *,
    job_type: str = SEARCH_RELEASE,
    job_id: int = 1,
) -> JobContext:
    job = Job(type=job_type, payload=dict(payload), status=JobStatus.RUNNING, attempts=1)
    job.id = job_id
    return JobContext(
        job=job,
        session=session,
        settings=settings,
        log=logging.getLogger("arc.jobs.test"),
    )


async def queued(session: AsyncSession, job_type: str) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == job_type).order_by(Job.id))
    return list(rows.all())


class Wired:
    """A show, an episode, a want and the two stubbed services."""

    def __init__(self, settings: Settings, nyaa: NyaaStub, qbit: QbitStub):
        self.settings = settings
        self.nyaa = nyaa
        self.qbit = qbit


async def wire(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
    feed: str | None = FEED,
    progress: int = 6,
    aired_through: int = 7,
) -> tuple[Wired, Episode]:
    """A watching user one episode behind, with Nyaa and qBittorrent stubbed."""
    settings = acquisition_settings(tmp_path)
    nyaa = NyaaStub({"Sousou no Frieren - 07": feed} if feed else {})
    qbit = QbitStub()

    monkeypatch.setattr(nyaa_module, "_sleep", _no_sleep)
    monkeypatch.setattr(
        nyaa_module.NyaaClient,
        "__init__",
        force_transport(nyaa_module.NyaaClient, nyaa.transport()),
    )
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, qbit.transport()),
    )

    anime = await make_anime(session, anilist_id=anilist_id)
    episodes = await make_episodes(session, anime, 12, aired_through=aired_through)
    user = await make_user(session, email)
    await make_entry(session, user, anime, progress=progress)
    episode = episodes[6]
    episode.state = EpisodeState.WANTED
    session.add(Want(user_id=user.id, episode_id=episode.id))
    await session.flush()
    return Wired(settings, nyaa, qbit), episode


async def _no_sleep(seconds: float) -> None:
    return None


# --- search_release ---------------------------------------------------------


async def test_a_search_picks_a_release_and_starts_it_downloading(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await set_setting(db_session, "preferred_groups", ["SubsPlease"])
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962001, email="search1@arc.test"
    )

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.DOWNLOADING
    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    assert torrent.info_hash == SUBSPLEASE_1080
    assert torrent.group == "SubsPlease"
    assert torrent.resolution == "1080p"
    assert torrent.seeders and torrent.seeders > 0
    assert torrent.magnet is not None and torrent.magnet.startswith("magnet:?xt=urn:btih:")


async def test_the_magnet_is_added_with_the_right_category_and_save_path(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962002, email="search2@arc.test"
    )

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    added = wired.qbit.added[0]
    assert added["category"] == "arc"
    assert added["savepath"] == f"/data/downloads/{episode.id}"
    assert added["tags"] == f"arc,episode:{episode.id}"


async def test_a_per_show_override_changes_the_pick(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962003, email="override@arc.test"
    )
    await set_setting(db_session, "preferred_groups", ["SubsPlease"])
    await set_setting(
        db_session,
        f"override:anime:{episode.anime_id}",
        {"preferred_groups": ["Erai-raws"], "resolution": "720p"},
    )

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    assert torrent.group == "Erai-raws"
    assert torrent.resolution == "720p"


async def test_the_search_is_a_no_op_for_an_episode_that_moved_on(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962004, email="moved@arc.test"
    )
    episode.state = EpisodeState.READY
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.READY
    assert wired.nyaa.queries == []
    assert wired.qbit.added == []


async def test_the_search_releases_the_episode_once_the_last_want_is_gone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing else would ever write that row: the episode must not be left."""
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962005, email="nowant@arc.test"
    )
    await db_session.execute(
        Want.__table__.delete().where(Want.episode_id == episode.id)  # type: ignore[arg-type]
    )
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.NOT_WANTED
    assert wired.nyaa.queries == []


async def test_a_search_that_started_is_released_from_searching_too(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of the same edge: the episode is already ``searching``.

    That is what a retry of a search whose want went away in the meantime
    looks like, and it is the state the old code stranded the episode in.
    """
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962008, email="stranded@arc.test"
    )
    episode.state = EpisodeState.SEARCHING
    await db_session.execute(
        Want.__table__.delete().where(Want.episode_id == episode.id)  # type: ignore[arg-type]
    )
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.NOT_WANTED
    assert wired.nyaa.queries == []


async def test_re_adding_the_show_searches_again(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """not_wanted → wanted → downloading: releasing it costs nothing later."""
    from arc.services.acquisition.wants import compute_wants

    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962009, email="readd@arc.test"
    )
    await db_session.execute(
        Want.__table__.delete().where(Want.episode_id == episode.id)  # type: ignore[arg-type]
    )
    await db_session.flush()
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    assert episode.state is EpisodeState.NOT_WANTED

    # The user puts the show back on their list; the reconciler wants it again.
    await compute_wants(db_session)
    assert episode.state is EpisodeState.WANTED

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.DOWNLOADING


async def test_an_episode_that_vanished_is_not_an_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _ = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962006, email="ghost@arc.test"
    )

    await search_release(context(db_session, wired.settings, {"episode_id": 9_999_999}))

    assert wired.nyaa.queries == []


async def test_a_release_this_episode_already_tried_is_not_tried_again(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """2026-09-13: one attempt per release, whatever became of it.

    A ``torrents`` row is only ever there because a previous attempt committed
    — ``search_release`` is one transaction — so the row means "this was tried
    and it did not produce the episode". Taking it again would re-add the same
    magnet and wait out the same six hours.
    """
    await set_setting(db_session, "preferred_groups", ["SubsPlease"])
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962007, email="retryrow@arc.test"
    )
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    first = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert first is not None and first.info_hash == SUBSPLEASE_1080
    first.qbit_state = QBIT_STALLED
    episode.state = EpisodeState.WANTED
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    rows = (
        await db_session.scalars(
            select(Torrent).where(Torrent.episode_id == episode.id).order_by(Torrent.id)
        )
    ).all()
    assert [row.info_hash for row in rows] != [SUBSPLEASE_1080], "the stalled one was skipped"
    assert len(rows) == 2, "and the next candidate down was taken"
    assert episode.state is EpisodeState.DOWNLOADING
    assert (
        len(
            (
                await db_session.scalars(
                    select(Torrent).where(Torrent.info_hash == SUBSPLEASE_1080)
                )
            ).all()
        )
        == 1
    ), "``info_hash`` is unique and nothing duplicated it"


# --- A hash another episode already holds -----------------------------------


async def other_episode(session: AsyncSession, episode: Episode, number: int) -> Episode:
    """Another episode of the same show, to hang a torrent row off."""
    found = await session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == number)
    )
    assert found is not None
    return found


async def test_a_release_another_episode_already_holds_is_skipped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``info_hash`` is unique: taking it would orphan this episode."""
    await set_setting(db_session, "preferred_groups", ["SubsPlease"])
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962015, email="reused@arc.test"
    )
    neighbour = await other_episode(db_session, episode, 8)
    db_session.add(Torrent(episode_id=neighbour.id, info_hash=SUBSPLEASE_1080))
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    assert torrent.info_hash != SUBSPLEASE_1080, "the next candidate down was taken instead"
    assert episode.state is EpisodeState.DOWNLOADING
    held = await db_session.scalar(select(Torrent).where(Torrent.info_hash == SUBSPLEASE_1080))
    assert held is not None and held.episode_id == neighbour.id, "and the first row is untouched"


async def test_every_release_taken_is_the_same_as_no_release(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962016, email="alltaken@arc.test"
    )
    neighbour = await other_episode(db_session, episode, 8)
    for item in nyaa_module.parse_feed(FEED):
        db_session.add(Torrent(episode_id=neighbour.id, info_hash=item.info_hash))
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.SEARCHING
    assert await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id)) is None
    assert len(await queued(db_session, SEARCH_RELEASE)) == 1, "the retry schedule, as with no hit"


# --- No candidate: the retry schedule (FR-A6) -------------------------------


async def test_no_candidate_requeues_the_search_half_an_hour_later_on_air_day(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962010, email="airday@arc.test", feed=None
    )
    episode.air_at = datetime.now(UTC) - timedelta(hours=2)
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.SEARCHING
    retries = [job for job in await queued(db_session, SEARCH_RELEASE)]
    assert len(retries) == 1
    delay = retries[0].run_after - datetime.now(UTC)
    assert timedelta(minutes=25) < delay <= timedelta(minutes=30)
    assert retries[0].payload["dedupe_key"] == search_dedupe_key(episode.id)


async def test_no_candidate_falls_back_to_six_hours_after_air_day(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962011, email="later@arc.test", feed=None
    )
    episode.air_at = datetime.now(UTC) - timedelta(days=4)
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    delay = (await queued(db_session, SEARCH_RELEASE))[0].run_after - datetime.now(UTC)
    assert timedelta(hours=5, minutes=55) < delay <= timedelta(hours=6)


def test_retry_delay_reads_the_air_time() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    assert (
        retry_delay(Episode(anime_id=1, number=1, air_at=now - timedelta(hours=3)), now=now)
        == acquisition_jobs.AIR_DAY_RETRY
    )
    assert (
        retry_delay(Episode(anime_id=1, number=1, air_at=now - timedelta(days=2)), now=now)
        == acquisition_jobs.LATER_RETRY
    )
    assert retry_delay(Episode(anime_id=1, number=1, air_at=None), now=now) == (
        acquisition_jobs.LATER_RETRY
    )


async def test_the_retry_carries_the_first_attempt_time_forward(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962012, email="carry@arc.test", feed=None
    )
    started = (datetime.now(UTC) - timedelta(days=3)).isoformat()

    await search_release(
        context(db_session, wired.settings, {"episode_id": episode.id, STARTED_KEY: started})
    )

    assert (await queued(db_session, SEARCH_RELEASE))[0].payload[STARTED_KEY] == started


async def test_after_fourteen_days_the_episode_is_flagged_unavailable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962013, email="giveup@arc.test", feed=None
    )
    started = (datetime.now(UTC) - GIVE_UP_AFTER - timedelta(hours=1)).isoformat()

    await search_release(
        context(db_session, wired.settings, {"episode_id": episode.id, STARTED_KEY: started})
    )

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == NO_RELEASE
    assert await queued(db_session, SEARCH_RELEASE) == []


async def test_a_revived_search_carries_the_original_start_forward(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fortnight is cumulative, not restarted by every daily revival.

    After an episode goes ``unavailable`` the daily retry queues a fresh
    ``search_release`` with nothing but an episode id. Without reading the last
    search's payload the 14-day window would begin again every morning and
    FR-A6's give-up would never happen twice.
    """
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962017, email="revive@arc.test", feed=None
    )
    started = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    finished = Job(
        type=SEARCH_RELEASE,
        payload={"episode_id": episode.id, STARTED_KEY: started},
        status=JobStatus.DONE,
        attempts=1,
    )
    db_session.add(finished)
    await db_session.flush()

    # The revival: a payload with no attempts_started_at at all.
    await search_release(
        context(db_session, wired.settings, {"episode_id": episode.id}, job_id=finished.id + 1)
    )

    retries = [job for job in await queued(db_session, SEARCH_RELEASE) if job.id != finished.id]
    assert len(retries) == 1
    assert retries[0].payload[STARTED_KEY] == started


async def test_a_revival_past_the_fortnight_gives_up_again_the_same_day(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Which is the policy: one day of quiet per attempt, while a want lasts."""
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962018, email="revive2@arc.test", feed=None
    )
    started = (datetime.now(UTC) - GIVE_UP_AFTER - timedelta(days=1)).isoformat()
    finished = Job(
        type=SEARCH_RELEASE,
        payload={"episode_id": episode.id, STARTED_KEY: started},
        status=JobStatus.DONE,
        attempts=1,
    )
    db_session.add(finished)
    await db_session.flush()

    await search_release(
        context(db_session, wired.settings, {"episode_id": episode.id}, job_id=finished.id + 1)
    )

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == NO_RELEASE
    assert [job for job in await queued(db_session, SEARCH_RELEASE) if job.id != finished.id] == []


async def test_a_search_for_another_episode_does_not_lend_its_start(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962019, email="notmine@arc.test", feed=None
    )
    neighbour = await other_episode(db_session, episode, 8)
    db_session.add(
        Job(
            type=SEARCH_RELEASE,
            payload={
                "episode_id": neighbour.id,
                STARTED_KEY: (datetime.now(UTC) - GIVE_UP_AFTER).isoformat(),
            },
            status=JobStatus.DONE,
            attempts=1,
        )
    )
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.SEARCHING, "somebody else's fortnight is not this one's"


async def test_a_retry_does_not_deduplicate_against_the_job_making_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The running job carries the same dedupe key it is about to queue under."""
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962014, email="selfdedupe@arc.test", feed=None
    )
    running = Job(
        type=SEARCH_RELEASE,
        payload={"episode_id": episode.id, "dedupe_key": search_dedupe_key(episode.id)},
        status=JobStatus.RUNNING,
        attempts=1,
    )
    db_session.add(running)
    await db_session.flush()

    ctx = context(db_session, wired.settings, running.payload, job_id=running.id)
    ctx.job.id = running.id
    await search_release(ctx)

    retries = [job for job in await queued(db_session, SEARCH_RELEASE) if job.id != running.id]
    assert len(retries) == 1


# --- The pause switch (FR-A2's burst, held) ---------------------------------


async def test_a_paused_search_requeues_itself_and_touches_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No Nyaa query, no magnet, no state change — just a job fifteen minutes on."""
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962060, email="paused1@arc.test"
    )
    await set_setting(db_session, PAUSED_KEY, True)
    before = datetime.now(UTC)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries == [], "a paused search must not ask nyaa anything"
    assert wired.qbit.calls == [], "nor add a magnet"
    assert episode.state is EpisodeState.WANTED, "the episode is left exactly as it was"
    assert await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id)) is None

    jobs = await queued(db_session, SEARCH_RELEASE)
    assert len(jobs) == 1, "the search is still owed, so it is still on the queue"
    assert jobs[0].payload["episode_id"] == episode.id
    assert jobs[0].priority == SEARCH_RELEASE_PRIORITY
    delay = jobs[0].run_after - before
    assert (
        acquisition_jobs.PAUSED_RETRY - timedelta(seconds=5)
        <= delay
        <= (acquisition_jobs.PAUSED_RETRY + timedelta(seconds=5))
    )


async def test_a_paused_search_carries_the_first_attempt_time_forward(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pause must not restart FR-A6's fortnight."""
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962061, email="paused2@arc.test"
    )
    await set_setting(db_session, PAUSED_KEY, True)
    started = (datetime.now(UTC) - timedelta(days=3)).isoformat()

    await search_release(
        context(
            db_session,
            wired.settings,
            {"episode_id": episode.id, STARTED_KEY: started},
        )
    )

    jobs = await queued(db_session, SEARCH_RELEASE)
    assert [job.payload[STARTED_KEY] for job in jobs] == [started]


async def test_a_paused_search_does_not_pile_up_behind_one_already_queued(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fifteen minutes apart, one row per episode however long the pause lasts."""
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962062, email="paused3@arc.test"
    )
    await set_setting(db_session, PAUSED_KEY, True)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}, job_id=1))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}, job_id=2))

    assert len(await queued(db_session, SEARCH_RELEASE)) == 1


async def test_a_held_search_requeues_itself_and_touches_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FR-T6: a full disk stops a search exactly the way a pause does.

    Same requeue, same fifteen minutes, same untouched episode — the only
    difference is the log line, because one of the two brakes an operator
    pressed and the other lifts itself.
    """
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962064, email="held1@arc.test"
    )
    fake_free_space(monkeypatch, acquisition_rules, 1 * BYTES_PER_GB)
    before = datetime.now(UTC)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries == [], "a held search must not ask nyaa anything"
    assert wired.qbit.calls == [], "nor add a magnet"
    assert episode.state is EpisodeState.WANTED, "the episode is left exactly as it was"
    assert await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id)) is None

    jobs = await queued(db_session, SEARCH_RELEASE)
    assert len(jobs) == 1, "the search is still owed, so it is still on the queue"
    assert jobs[0].payload["episode_id"] == episode.id
    delay = jobs[0].run_after - before
    assert (
        acquisition_jobs.PAUSED_RETRY - timedelta(seconds=5)
        <= delay
        <= (acquisition_jobs.PAUSED_RETRY + timedelta(seconds=5))
    )


async def test_a_search_runs_normally_when_the_floor_is_clear(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """So the guard cannot be on by accident on somebody else's machine."""
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962065, email="held2@arc.test"
    )
    fake_free_space(monkeypatch, acquisition_rules, 50 * BYTES_PER_GB)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries != []


async def test_polling_keeps_running_while_acquisition_is_paused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pausing means stop fetching *more*, not abandon what is already coming.

    The download was started before the pause; it finishes during it, and the
    file still reaches the library and still becomes something to watch.
    """
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962063, email="paused4@arc.test"
    )
    directory = tmp_path / "downloads" / str(episode.id)
    directory.mkdir(parents=True)
    video = directory / "[SubsPlease] Sousou no Frieren - 07 (1080p) [24356E19].mkv"
    video.write_bytes(b"x" * 4096)
    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=1.0,
        state="uploading",
        content_path=f"/data/downloads/{episode.id}",
        completion_on=int(datetime.now(UTC).timestamp()),
    )
    await set_setting(db_session, PAUSED_KEY, True)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.MATCHING
    assert await queued(db_session, MATCH_FILE) != []


# --- One Nyaa client for the whole process ----------------------------------


async def test_two_searches_go_through_one_shared_nyaa_client(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A client per job would mean a pacing gap per job, which is no gap.

    The spacing itself is asserted in ``test_nyaa.py``, where two searches can
    actually run at once; what matters here is that both jobs reach for the
    same instance, because that instance is what holds the gap and the cache.
    """
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962021, email="shared@arc.test"
    )
    neighbour = await other_episode(db_session, episode, 8)
    neighbour.state = EpisodeState.WANTED
    user_id = await db_session.scalar(select(Want.user_id).where(Want.episode_id == episode.id))
    db_session.add(Want(user_id=user_id, episode_id=neighbour.id))
    await db_session.flush()

    built: list[object] = []
    original = nyaa_module.NyaaClient.__init__

    def counting(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        built.append(self)
        original(self, *args, **kwargs)

    monkeypatch.setattr(nyaa_module.NyaaClient, "__init__", counting)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    await search_release(context(db_session, wired.settings, {"episode_id": neighbour.id}))

    assert len(built) == 1, "the second search reused the first search's client"
    assert nyaa_module.shared_client(wired.settings.nyaa_url) is built[0]


# --- qBittorrent down -------------------------------------------------------


async def test_an_unreachable_client_raises_and_leaves_the_episode_searching(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962020, email="qbitdown@arc.test"
    )
    wired.qbit.down = True

    with pytest.raises(QbitUnavailable):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.SEARCHING, "not unavailable: nothing is wrong with Nyaa"
    assert episode.unavailable_reason is None


# --- largest_video ----------------------------------------------------------


def test_the_biggest_video_in_the_directory_wins(tmp_path: Path) -> None:
    root = tmp_path / "42"
    root.mkdir()
    (root / "episode.mkv").write_bytes(b"x" * 5000)
    (root / "sample.mkv").write_bytes(b"x" * 100)
    (root / "readme.nfo").write_bytes(b"x" * 90000)

    assert largest_video(root, frozenset({"mkv", "mp4"})) == root / "episode.mkv"


def test_a_single_file_torrent_is_its_own_answer(tmp_path: Path) -> None:
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"x" * 10)

    assert largest_video(path, frozenset({"mkv"})) == path


def test_a_partial_download_is_not_picked(tmp_path: Path) -> None:
    root = tmp_path / "42"
    root.mkdir()
    (root / "episode.mkv.!qB").write_bytes(b"x" * 5000)

    assert largest_video(root, frozenset({"mkv"})) is None


def test_a_hidden_directory_is_not_walked_into(tmp_path: Path) -> None:
    """A NAS writes ``.Trash-1000``/``@eaDir`` beside the file it just saved."""
    root = tmp_path / "42"
    (root / ".Trash-1000").mkdir(parents=True)
    (root / "@eaDir").mkdir()
    (root / ".Trash-1000" / "deleted.mkv").write_bytes(b"x" * 900000)
    (root / "@eaDir" / "thumb.mkv").write_bytes(b"x" * 800000)
    (root / "episode.mkv").write_bytes(b"x" * 5000)

    assert largest_video(root, frozenset({"mkv"})) == root / "episode.mkv"


def test_a_directory_with_no_video_answers_none(tmp_path: Path) -> None:
    root = tmp_path / "42"
    root.mkdir()
    (root / "notes.txt").write_bytes(b"x")

    assert largest_video(root, frozenset({"mkv"})) is None


# --- poll_qbit --------------------------------------------------------------


async def downloading(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
) -> tuple[Wired, Episode, Torrent]:
    wired, episode = await wire(session, monkeypatch, tmp_path, anilist_id=anilist_id, email=email)
    await search_release(context(session, wired.settings, {"episode_id": episode.id}))
    torrent = await session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    return wired, episode, torrent


async def test_polling_syncs_progress_and_state(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962030, email="poll1@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.37, state="downloading")

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.progress == pytest.approx(0.37)
    assert torrent.qbit_state == "downloading"
    assert episode.state is EpisodeState.DOWNLOADING


async def test_a_finished_torrent_is_handed_to_the_library_with_the_prior(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962031, email="poll2@arc.test"
    )
    directory = tmp_path / "downloads" / str(episode.id)
    directory.mkdir(parents=True)
    video = directory / "[SubsPlease] Sousou no Frieren - 07 (1080p) [24356E19].mkv"
    video.write_bytes(b"x" * 4096)
    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=1.0,
        state="uploading",
        content_path=f"/data/downloads/{episode.id}",
        completion_on=int(datetime.now(UTC).timestamp()),
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.MATCHING
    assert torrent.completed_at is not None

    media = await db_session.scalar(select(MediaFile).where(MediaFile.path == str(video.resolve())))
    assert media is not None

    match_jobs = await queued(db_session, MATCH_FILE)
    assert len(match_jobs) == 1
    assert match_jobs[0].payload["media_file_id"] == media.id
    assert match_jobs[0].payload["expected"] == [episode.anime_id, episode.number]


async def test_the_prior_is_added_to_a_match_the_library_scan_queued_first(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two-minute scan can beat the sixty-second poll to the same file."""
    from arc.services.library import ingest

    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962032, email="poll3@arc.test"
    )
    directory = tmp_path / "downloads" / str(episode.id)
    directory.mkdir(parents=True)
    video = directory / "[SubsPlease] Sousou no Frieren - 07 (1080p) [24356E19].mkv"
    video.write_bytes(b"x" * 4096)
    scanned = await ingest.ingest_file(db_session, wired.settings, video, probe=False)
    assert scanned is not None

    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=1.0,
        state="stalledUP",
        content_path=f"/data/downloads/{episode.id}",
    )
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    match_jobs = await queued(db_session, MATCH_FILE)
    assert len(match_jobs) == 1
    assert match_jobs[0].payload["expected"] == [episode.anime_id, episode.number]


async def test_polling_twice_does_not_index_the_file_twice(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962033, email="poll4@arc.test"
    )
    directory = tmp_path / "downloads" / str(episode.id)
    directory.mkdir(parents=True)
    (directory / "ep.mkv").write_bytes(b"x" * 4096)
    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=1.0,
        state="uploading",
        content_path=f"/data/downloads/{episode.id}",
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    files = (await db_session.scalars(select(MediaFile))).all()
    assert len(files) == 1
    assert len(await queued(db_session, MATCH_FILE)) == 1


async def test_a_torrent_that_vanished_from_the_client_is_unavailable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962034, email="poll5@arc.test"
    )
    # The client is asked and answers with nothing at all.

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == REMOVED_FROM_CLIENT
    assert torrent.qbit_state == "missing"


async def test_a_torrent_that_vanished_while_downloaded_is_unavailable_too(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``downloaded`` waits for its file; a deleted torrent ends that wait."""
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962038, email="poll9@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=1.0,
        state="uploading",
        content_path=f"/data/downloads/{episode.id}",
    )
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    assert episode.state is EpisodeState.DOWNLOADED, "no file on disk yet"

    wired.qbit.torrents.clear()
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == REMOVED_FROM_CLIENT
    assert torrent.qbit_state == "missing"


async def test_polling_refreshes_every_arc_torrent_not_only_the_downloading_ones(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The listing is one request; a seeding torrent's state is worth keeping."""
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962039, email="poll10@arc.test"
    )
    episode.state = EpisodeState.MATCHED
    torrent.qbit_state = "downloading"
    torrent.progress = 0.5
    await db_session.flush()
    wired.qbit.add_torrent(torrent.info_hash, progress=1.0, state="stalledUP")

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == "stalledUP"
    assert torrent.progress == pytest.approx(1.0)
    assert episode.state is EpisodeState.MATCHED, "and nothing was done to the episode"


async def test_a_rejected_torrent_keeps_saying_so(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A person's verdict is not overwritten by what the client is doing."""
    from arc.services.acquisition.reject import QBIT_REJECTED

    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962042, email="poll12@arc.test"
    )
    episode.state = EpisodeState.UNAVAILABLE
    torrent.qbit_state = QBIT_REJECTED
    await db_session.flush()
    wired.qbit.add_torrent(torrent.info_hash, progress=1.0, state="stalledUP")

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == QBIT_REJECTED
    assert torrent.progress == pytest.approx(1.0), "the figure is still worth having"


async def test_a_torrent_gone_from_the_client_after_matching_leaves_the_episode(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962041, email="poll11@arc.test"
    )
    episode.state = EpisodeState.MATCHED
    await db_session.flush()

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == "missing"
    assert episode.state is EpisodeState.MATCHED


async def test_a_finished_torrent_with_no_file_yet_waits_for_the_next_poll(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A "complete" torrent whose file is not readable yet is not stranded."""
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962035, email="poll6@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=1.0,
        state="uploading",
        content_path=f"/data/downloads/{episode.id}",
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADED
    assert await queued(db_session, MATCH_FILE) == []

    # The file appears a moment later; the next poll picks it up from
    # ``downloaded`` rather than needing the episode to be ``downloading``.
    directory = tmp_path / "downloads" / str(episode.id)
    directory.mkdir(parents=True)
    (directory / "ep.mkv").write_bytes(b"x" * 4096)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.MATCHING
    assert len(await queued(db_session, MATCH_FILE)) == 1


async def test_polling_with_nothing_downloading_does_not_call_the_client(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _ = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962036, email="poll7@arc.test"
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert wired.qbit.calls == []


async def test_a_torrent_outside_arcs_category_is_never_touched(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962037, email="poll8@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=1.0, state="uploading", category="mine")

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.UNAVAILABLE, "invisible to Arc is the same as gone"


# --- Stalls: a torrent that is going nowhere (2026-09-13) -------------------


def info(
    state: str,
    *,
    progress: float = 0.0,
    dlspeed: int = 0,
    time_active: int | None = 0,
    num_seeds: int | None = 0,
    num_leechs: int | None = 0,
    num_complete: int | None = None,
    num_incomplete: int | None = None,
) -> qbit_module.TorrentInfo:
    """A client row. The defaults are a torrent that has just been added.

    ``num_complete``/``num_incomplete`` default to ``None`` — the tracker has
    not been scraped — because that is what the client reports for the first
    minute or two of every torrent's life and the rule has to be safe there.
    """
    return qbit_module.TorrentInfo(
        hash="a" * 40,
        name="release.mkv",
        progress=progress,
        state=state,
        dlspeed=dlspeed,
        time_active=time_active,
        num_seeds=num_seeds,
        num_leechs=num_leechs,
        num_complete=num_complete,
        num_incomplete=num_incomplete,
    )


HOUR = 3600

#: ``(what the client says, the expected reason)``. The table *is* the rule —
#: every branch of :func:`stall_reason`, and every reason it must refuse to
#: call a stall. The clock throughout is ``time_active``: how long qBittorrent
#: has been *working on* the torrent, never how old Arc's row is.
STALLS: list[tuple[qbit_module.TorrentInfo, str | None]] = [
    # Metadata: an hour of asking is a magnet nobody holds.
    (info("metaDL", time_active=61 * 60), "no metadata after 60 minutes"),
    (info("metaDL", time_active=59 * 60), None),
    (info("forcedMetaDL", time_active=3 * HOUR), "no metadata after 60 minutes"),
    # Six hours of *activity* and not one byte.
    (info("downloading", time_active=7 * HOUR), "no bytes after 6 hours"),
    (info("stalledDL", time_active=7 * HOUR), "no bytes after 6 hours"),
    (info("downloading", time_active=5 * HOUR), None),
    # Bytes have arrived, so "no bytes" does not apply...
    (info("downloading", progress=0.4, time_active=7 * HOUR), None),
    # ...and the swarm has to be *known* empty for "no seeders" to.
    (
        info(
            "stalledDL",
            progress=0.6,
            time_active=7 * HOUR,
            num_complete=0,
            num_incomplete=0,
        ),
        "no seeders after 6 hours",
    ),
    # The bug the tracker figures exist to avoid: a healthy 60 %-done torrent
    # between announces is connected to nobody and its tracker has seen twelve.
    (
        info("stalledDL", progress=0.6, time_active=7 * HOUR, num_complete=12, num_incomplete=3),
        None,
    ),
    # And an unscraped tracker says nothing at all, whatever it looks like.
    (info("stalledDL", progress=0.6, time_active=7 * HOUR), None),
    (
        info("stalledDL", progress=0.6, time_active=7 * HOUR, num_complete=-1, num_incomplete=-1),
        None,
    ),
    # A known-empty swarm on a torrent that has not started either: the swarm
    # is the more informative of the two sentences, so it is the one shown.
    (
        info("downloading", time_active=7 * HOUR, num_complete=0, num_incomplete=0),
        "no seeders after 6 hours",
    ),
    # One seeder is not an empty swarm.
    (
        info("downloading", progress=0.6, time_active=7 * HOUR, num_complete=1, num_incomplete=0),
        None,
    ),
    # **The queue.** Nine hours old, thirty seconds of work: a torrent the
    # client has only just let out of ``queuedDL``. ``stalledDL`` means "no
    # bytes this instant", which is what its first seconds look like.
    (info("stalledDL", time_active=30), None),
    (info("downloading", time_active=30), None),
    # Bytes arriving right now settles it whatever the history says.
    (info("downloading", dlspeed=900_000, time_active=9 * HOUR), None),
    # A client that does not report the clock stalls nothing.
    (info("metaDL", time_active=None), None),
    (info("downloading", time_active=None), None),
    # A person's own decision, never a stall.
    (info("stoppedDL", time_active=4 * 24 * HOUR), None),
    (info("pausedDL", time_active=4 * 24 * HOUR), None),
    # Waiting its turn behind ``max_active_downloads``.
    (info("queuedDL", time_active=2 * 24 * HOUR), None),
    # Busy, or broken in a way this rule has nothing to say about.
    (info("checkingDL", time_active=2 * 24 * HOUR), None),
    (info("moving", progress=1.0, time_active=2 * 24 * HOUR), None),
    (info("error", time_active=2 * 24 * HOUR), None),
    # Finished. Nothing left to wait for.
    (info("uploading", progress=1.0, time_active=2 * 24 * HOUR), None),
]


@pytest.mark.parametrize(("reported", "expected"), STALLS, ids=lambda value: str(value)[:56])
def test_the_stall_rule(reported: qbit_module.TorrentInfo, expected: str | None) -> None:
    assert stall_reason(reported) == expected


def test_the_thresholds_are_configurable() -> None:
    reported = info("downloading", time_active=2 * HOUR)

    assert stall_reason(reported) is None
    assert stall_reason(reported, no_bytes_after=timedelta(hours=1)) == "no bytes after 1 hour"


def test_the_defaults_are_the_settings_defaults() -> None:
    """One number, two homes: a drift here is a rule that says one thing and does another."""
    settings = Settings(env="test", _env_file=None)  # type: ignore[call-arg]

    assert timedelta(minutes=settings.stall_metadata_minutes) == STALL_METADATA_AFTER
    assert timedelta(hours=settings.stall_no_bytes_hours) == STALL_NO_BYTES_AFTER


async def stalling(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
    age: timedelta = timedelta(days=1),
) -> tuple[Wired, Episode, Torrent]:
    """A downloading episode whose torrent row was written ``age`` ago.

    The row's age is deliberately *old* in every one of these: it is not what
    the rule reads, and a test that passed because the row was young would be
    testing nothing.
    """
    wired, episode, torrent = await downloading(
        session, monkeypatch, tmp_path, anilist_id=anilist_id, email=email
    )
    torrent.added_at = datetime.now(UTC) - age
    await session.flush()
    return wired, episode, torrent


async def test_a_magnet_with_no_metadata_is_removed_and_the_episode_retried(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact production failure: three slots held by dead 2018 uploads."""
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962070, email="stall1@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.0, state="metaDL", time_active=2 * HOUR)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == "no metadata after 60 minutes"
    assert torrent.qbit_state == QBIT_STALLED
    assert wired.qbit.deleted == [{"hashes": torrent.info_hash.lower(), "deleteFiles": "true"}]
    assert wired.qbit.torrents == [], "and it is gone from the client"


async def test_a_download_with_no_bytes_after_six_hours_is_removed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962071, email="stall2@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.0, state="stalledDL", time_active=7 * HOUR)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == "no bytes after 6 hours"
    assert torrent.qbit_state == QBIT_STALLED
    assert wired.qbit.deleted[0]["deleteFiles"] == "true"


async def test_a_swarm_the_tracker_says_is_empty_is_removed_half_way_through(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962072, email="stall3@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=0.62,
        state="stalledDL",
        time_active=7 * HOUR,
        num_complete=0,
        num_incomplete=0,
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == "no seeders after 6 hours"


async def test_a_torrent_connected_to_nobody_with_a_live_tracker_is_left_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Blocker 3: ``num_seeds`` is 0 all the time on healthy torrents."""
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962081, email="stall12@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=0.6,
        state="stalledDL",
        time_active=7 * HOUR,
        num_seeds=0,
        num_leechs=0,
        num_complete=12,
        num_incomplete=4,
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert wired.qbit.deleted == [], "60 % of a file was very nearly deleted here"


async def test_an_unscraped_tracker_is_not_an_empty_swarm(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962082, email="stall13@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=0.6,
        state="stalledDL",
        time_active=7 * HOUR,
        num_complete=-1,
        num_incomplete=-1,
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert wired.qbit.deleted == []


async def test_a_torrent_just_out_of_the_queue_is_left_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Blocker 2: the row is nine hours old and the download is 30 s old."""
    wired, episode, torrent = await stalling(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962083,
        email="stall14@arc.test",
        age=timedelta(hours=9),
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.0, state="stalledDL", time_active=30)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state == "stalledDL"
    assert wired.qbit.deleted == []


async def test_the_same_torrent_seven_active_hours_later_is_removed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of blocker 2's pair: activity is what condemns it."""
    wired, episode, torrent = await stalling(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962084,
        email="stall15@arc.test",
        age=timedelta(hours=9),
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.0, state="stalledDL", time_active=7 * HOUR)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.UNAVAILABLE
    assert torrent.qbit_state == QBIT_STALLED


async def test_a_torrent_making_progress_is_left_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962073, email="stall4@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash,
        progress=0.31,
        state="downloading",
        time_active=2 * 24 * HOUR,
        num_complete=14,
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state == "downloading"
    assert wired.qbit.deleted == []


async def test_a_young_torrent_is_left_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962074, email="stall5@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.0, state="metaDL", time_active=20 * 60)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert wired.qbit.deleted == []


async def test_a_torrent_somebody_stopped_is_never_a_stall(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Production has 297 of these, stopped on purpose."""
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962075, email="stall6@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash, progress=0.0, state="stoppedDL", time_active=5 * 24 * HOUR
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state == "stoppedDL"
    assert wired.qbit.deleted == []


async def test_a_torrent_queued_behind_the_download_limit_is_never_a_stall(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With four hundred wants most of the queue is ``queuedDL`` for hours."""
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962076, email="stall7@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash, progress=0.0, state="queuedDL", time_active=2 * 24 * HOUR
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert wired.qbit.deleted == []


async def test_a_client_that_does_not_report_the_clock_stalls_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962089, email="stall16@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.0, state="metaDL")
    del wired.qbit.torrents[0]["time_active"]

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert wired.qbit.deleted == []


async def test_the_thresholds_come_from_the_environment(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962077, email="stall8@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash, progress=0.0, state="downloading", time_active=2 * HOUR
    )
    impatient = acquisition_settings(tmp_path, stall_no_bytes_hours=1)

    await poll_qbit(context(db_session, impatient, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == "no bytes after 1 hour"


async def test_a_stalled_row_keeps_saying_stalled_once_the_torrent_is_gone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Arc is the reason it is missing; "missing" would lose the reason."""
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962078, email="stall9@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.0, state="stalledDL", time_active=8 * HOUR)
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    assert torrent.qbit_state == QBIT_STALLED

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert torrent.qbit_state == QBIT_STALLED
    assert episode.unavailable_reason == "no bytes after 6 hours", "not 'removed from the client'"


async def test_a_stalled_row_does_not_drag_the_next_attempt_back(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Blocker 1. The old row outlives the stall; the new download must not.

    Rows are polled oldest first, so without the ``DECIDED`` guard the stalled
    attempt — gone from the client, by Arc's own hand — moved the episode from
    ``downloading`` back to ``unavailable`` on every poll, and the release that
    was actually working was never handed to the library.
    """
    await set_setting(db_session, "preferred_groups", ["SubsPlease"])
    wired, episode, old = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962090, email="stall17@arc.test"
    )
    wired.qbit.add_torrent(old.info_hash, progress=0.0, state="metaDL", time_active=2 * HOUR)
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    assert episode.state is EpisodeState.UNAVAILABLE

    # FR-A6's retry finds another release, which starts downloading properly.
    episode.state = EpisodeState.WANTED
    await db_session.flush()
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    fresh = await db_session.scalar(
        select(Torrent)
        .where(Torrent.episode_id == episode.id, Torrent.id != old.id)
        .order_by(Torrent.id.desc())
    )
    assert fresh is not None
    assert episode.state is EpisodeState.DOWNLOADING
    directory = tmp_path / "downloads" / str(episode.id)
    directory.mkdir(parents=True)
    (directory / "ep.mkv").write_bytes(b"x" * 4096)
    wired.qbit.add_torrent(
        fresh.info_hash,
        progress=1.0,
        state="uploading",
        content_path=f"/data/downloads/{episode.id}",
        time_active=600,
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.MATCHING, "the new download was handed off"
    assert old.qbit_state == QBIT_STALLED, "and the old row still says what became of it"
    assert len(await queued(db_session, MATCH_FILE)) == 1


async def test_a_stalled_torrent_still_in_the_client_is_deleted_again(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Should-fix 6: the rows are flushed first, so the delete may be retried.

    Simulated by marking the row ``stalled`` with the torrent still there —
    which is exactly the state a client that died between the flush and the
    ``torrents/delete`` leaves behind.
    """
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962091, email="stall18@arc.test"
    )
    torrent.qbit_state = QBIT_STALLED
    episode.state = EpisodeState.UNAVAILABLE
    await db_session.flush()
    wired.qbit.add_torrent(torrent.info_hash, progress=0.0, state="stalledDL", time_active=30)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert wired.qbit.deleted == [{"hashes": torrent.info_hash.lower(), "deleteFiles": "true"}]
    assert torrent.qbit_state == QBIT_STALLED
    assert episode.state is EpisodeState.UNAVAILABLE


async def test_a_rejected_torrent_still_in_the_client_is_never_deleted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It is holding a file somebody is looking at in review; retention owns it."""
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962092, email="stall19@arc.test"
    )
    torrent.qbit_state = QBIT_REJECTED
    episode.state = EpisodeState.UNAVAILABLE
    await db_session.flush()
    wired.qbit.add_torrent(torrent.info_hash, progress=1.0, state="stoppedUP", time_active=HOUR)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert wired.qbit.deleted == []
    assert torrent.qbit_state == QBIT_REJECTED


async def test_an_unreachable_client_leaves_a_stalling_torrent_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962079, email="stall10@arc.test"
    )
    wired.qbit.add_torrent(
        torrent.info_hash, progress=0.0, state="metaDL", time_active=3 * 24 * HOUR
    )
    wired.qbit.down = True

    with pytest.raises(QbitUnavailable):
        await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.DOWNLOADING
    assert episode.unavailable_reason is None
    assert torrent.qbit_state == "added"


async def test_a_stalled_episode_searches_again_and_avoids_the_dead_release(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole loop: stall → unavailable → FR-A6's retry → another release."""
    await set_setting(db_session, "preferred_groups", ["SubsPlease"])
    wired, episode, torrent = await stalling(
        db_session, monkeypatch, tmp_path, anilist_id=962080, email="stall11@arc.test"
    )
    dead = torrent.info_hash
    wired.qbit.add_torrent(dead, progress=0.0, state="metaDL", time_active=8 * HOUR)
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    assert episode.state is EpisodeState.UNAVAILABLE

    # What the daily retry does: ``unavailable`` → ``wanted`` → a fresh search.
    episode.state = EpisodeState.WANTED
    await db_session.flush()
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.DOWNLOADING
    chosen = wired.qbit.added[-1]["urls"]
    assert dead not in chosen, "the release that stalled is not offered again"


# --- qbit_cancel: the client half of a cancellation -------------------------


async def cancelled(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
) -> tuple[Wired, Episode, Torrent]:
    """A downloading episode the reconciler has just cancelled."""
    wired, episode, torrent = await downloading(
        session, monkeypatch, tmp_path, anilist_id=anilist_id, email=email
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.2, state="downloading")
    await session.execute(delete(Want).where(Want.episode_id == episode.id))
    assert await cancel_if_unwanted(session, episode)
    await session.flush()
    return wired, episode, torrent


async def a_user_id(session: AsyncSession) -> int:
    """Any user's id — the want this writes only has to exist, not be anybody's."""
    found = await session.scalar(select(Want.user_id).limit(1))
    if found is not None:
        return int(found)
    from arc.models import User

    return int((await session.scalars(select(User.id).limit(1))).one())


async def test_the_cancel_handler_removes_the_torrent_with_its_files(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await cancelled(
        db_session, monkeypatch, tmp_path, anilist_id=962085, email="cancel1@arc.test"
    )

    await qbit_cancel(
        context(db_session, wired.settings, {"episode_id": episode.id}, job_type=QBIT_CANCEL)
    )

    assert wired.qbit.deleted == [{"hashes": torrent.info_hash.lower(), "deleteFiles": "true"}]
    assert wired.qbit.torrents == []


async def test_the_cancel_handler_deletes_the_row_so_the_release_is_pickable_again(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Should-fix 7: a cancel must not cost the episode its best release.

    ``_pick`` bars every hash that has a ``torrents`` row, which is right for a
    release that was *tried and failed* and wrong for one nobody got round to
    wanting. So the cancel is the one ending that removes the row — and the
    proof is that changing your mind a moment later gets the same file.
    """
    await set_setting(db_session, "preferred_groups", ["SubsPlease"])
    wired, episode, torrent = await cancelled(
        db_session, monkeypatch, tmp_path, anilist_id=962093, email="cancel5@arc.test"
    )
    was = torrent.info_hash
    assert was == SUBSPLEASE_1080

    await qbit_cancel(
        context(db_session, wired.settings, {"episode_id": episode.id}, job_type=QBIT_CANCEL)
    )

    assert await db_session.scalar(select(Torrent).where(Torrent.info_hash == was)) is None
    # And the user changes their mind: the same release is chosen again.
    episode.state = EpisodeState.WANTED
    db_session.add(Want(user_id=await a_user_id(db_session), episode_id=episode.id))
    await db_session.flush()
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    again = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert again is not None and again.info_hash == was
    assert episode.state is EpisodeState.DOWNLOADING


async def test_a_stalled_row_still_bars_its_release_after_a_cancel_elsewhere(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of the rule: ``stalled`` and ``rejected`` rows stay put."""
    wired, episode, torrent = await cancelled(
        db_session, monkeypatch, tmp_path, anilist_id=962094, email="cancel6@arc.test"
    )
    torrent.qbit_state = QBIT_STALLED
    await db_session.flush()

    await qbit_cancel(
        context(db_session, wired.settings, {"episode_id": episode.id}, job_type=QBIT_CANCEL)
    )

    assert wired.qbit.deleted == [], "not this job's row"
    assert await db_session.scalar(select(Torrent).where(Torrent.id == torrent.id)) is torrent


async def test_the_cancel_handler_keeps_the_row_when_the_client_refuses(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rows are deleted only after the client has answered, so a retry works."""
    wired, episode, torrent = await cancelled(
        db_session, monkeypatch, tmp_path, anilist_id=962095, email="cancel7@arc.test"
    )
    wired.qbit.down = True

    with pytest.raises(QbitUnavailable):
        await qbit_cancel(
            context(db_session, wired.settings, {"episode_id": episode.id}, job_type=QBIT_CANCEL)
        )
    assert torrent.qbit_state == QBIT_CANCELLED

    wired.qbit.down = False
    await qbit_cancel(
        context(db_session, wired.settings, {"episode_id": episode.id}, job_type=QBIT_CANCEL)
    )

    assert wired.qbit.deleted == [{"hashes": torrent.info_hash.lower(), "deleteFiles": "true"}]
    assert await db_session.scalar(select(Torrent).where(Torrent.id == torrent.id)) is None


async def test_the_cancel_handler_leaves_a_release_chosen_since_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The episode was wanted again before the job ran; only the mark is deleted."""
    wired, episode, torrent = await cancelled(
        db_session, monkeypatch, tmp_path, anilist_id=962086, email="cancel2@arc.test"
    )
    fresh = Torrent(episode_id=episode.id, info_hash="f" * 40, qbit_state="downloading")
    db_session.add(fresh)
    await db_session.flush()
    wired.qbit.add_torrent(fresh.info_hash, progress=0.1, state="downloading")

    await qbit_cancel(
        context(db_session, wired.settings, {"episode_id": episode.id}, job_type=QBIT_CANCEL)
    )

    assert wired.qbit.deleted == [{"hashes": torrent.info_hash.lower(), "deleteFiles": "true"}]
    assert [row["hash"] for row in wired.qbit.torrents] == [fresh.info_hash]


async def test_the_cancel_handler_with_nothing_marked_asks_the_client_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Idempotence: the second run of the job, or one whose episode came back."""
    wired, episode, _ = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962087, email="cancel3@arc.test"
    )
    wired.qbit.calls.clear()

    await qbit_cancel(
        context(db_session, wired.settings, {"episode_id": episode.id}, job_type=QBIT_CANCEL)
    )

    assert wired.qbit.calls == [], "nothing to delete is not a reason to log in"


async def test_the_cancel_handler_raises_when_the_client_is_down(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """So the runner retries it; the rows say what should happen and do not expire."""
    wired, episode, torrent = await cancelled(
        db_session, monkeypatch, tmp_path, anilist_id=962088, email="cancel4@arc.test"
    )
    wired.qbit.down = True

    with pytest.raises(QbitUnavailable):
        await qbit_cancel(
            context(db_session, wired.settings, {"episode_id": episode.id}, job_type=QBIT_CANCEL)
        )

    assert torrent.qbit_state == "cancelled"
    assert episode.state is EpisodeState.NOT_WANTED


# --- Seeding policy (spec §9) -----------------------------------------------


async def test_a_completed_torrent_that_is_seeding_is_stopped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Belt and braces: the ratio limit misses torrents added before it."""
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962050, email="seed1@arc.test"
    )
    episode.state = EpisodeState.READY
    await db_session.flush()
    wired.qbit.add_torrent(torrent.info_hash, progress=1.0, state="uploading")

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert wired.qbit.stopped == [torrent.info_hash.lower()]
    assert wired.qbit.torrents[0]["state"] == "stoppedUP"


@pytest.mark.parametrize("state", sorted(SEEDING_STATES))
async def test_every_seeding_state_is_stopped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    wired, episode, torrent = await downloading(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962051 + sorted(SEEDING_STATES).index(state),
        email=f"seed-{state}@arc.test",
    )
    episode.state = EpisodeState.READY
    await db_session.flush()
    wired.qbit.add_torrent(torrent.info_hash, progress=1.0, state=state)

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert wired.qbit.stopped == [torrent.info_hash.lower()]


async def test_a_torrent_already_stopped_is_left_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962056, email="seed2@arc.test"
    )
    episode.state = EpisodeState.READY
    await db_session.flush()
    wired.qbit.add_torrent(torrent.info_hash, progress=1.0, state="stoppedUP")

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert wired.qbit.stopped == []


async def test_a_downloading_torrent_is_not_stopped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962057, email="seed3@arc.test"
    )
    wired.qbit.add_torrent(torrent.info_hash, progress=0.4, state="downloading")

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert wired.qbit.stopped == []


async def test_a_seeding_deployment_leaves_the_torrent_uploading(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``QBIT_SEEDING=true`` is the switch that turns all of this off."""
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962058, email="seed4@arc.test"
    )
    episode.state = EpisodeState.READY
    await db_session.flush()
    wired.qbit.add_torrent(torrent.info_hash, progress=1.0, state="uploading")
    seeding = acquisition_settings(tmp_path, qbit_seeding=True)

    await poll_qbit(context(db_session, seeding, {}, job_type="poll_qbit"))

    assert wired.qbit.stopped == []
    assert wired.qbit.torrents[0]["state"] == "uploading"


async def test_the_policy_handler_writes_the_preferences(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _ = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962059, email="policy1@arc.test"
    )

    await qbit_apply_policy(context(db_session, wired.settings, {}, job_type=QBIT_POLICY))

    assert wired.qbit.preferences == [
        {
            "up_limit": 512 * 1024,
            "queueing_enabled": True,
            "max_active_downloads": 8,
            "max_active_torrents": 12,
            "dont_count_slow_torrents": True,
            "slow_torrent_dl_rate_threshold": 2,
            "slow_torrent_ul_rate_threshold": 2,
            "slow_torrent_inactive_timer": 300,
            "max_ratio_enabled": True,
            "max_ratio": 0,
            "max_ratio_act": 0,
            "max_seeding_time_enabled": True,
            "max_seeding_time": 0,
        }
    ]


async def test_the_policy_handler_sends_the_configured_queue_limits(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The four keys of the 2026-09-13 decision, with the operator's figures."""
    wired, _ = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962062, email="policy4@arc.test"
    )
    queued_up = acquisition_settings(
        tmp_path, qbit_max_active_downloads=3, qbit_max_active_torrents=20
    )

    await qbit_apply_policy(context(db_session, queued_up, {}, job_type=QBIT_POLICY))

    sent = wired.qbit.preferences[0]
    assert sent["queueing_enabled"] is True
    assert sent["max_active_downloads"] == 3
    assert sent["max_active_torrents"] == 20
    assert sent["dont_count_slow_torrents"] is True
    # A torrent is only counted out after five minutes of moving nothing, so an
    # ordinary lull never costs a healthy download its slot — and the queue
    # never *removes* anything: that is the stall rule's job.
    assert sent["slow_torrent_dl_rate_threshold"] == 2
    assert sent["slow_torrent_ul_rate_threshold"] == 2
    assert sent["slow_torrent_inactive_timer"] == 300


async def test_the_policy_handler_honours_the_upload_limit_setting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _ = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962060, email="policy2@arc.test"
    )
    capped = acquisition_settings(tmp_path, qbit_upload_limit_kib=128, qbit_seeding=True)

    await qbit_apply_policy(context(db_session, capped, {}, job_type=QBIT_POLICY))

    assert wired.qbit.preferences[0]["up_limit"] == 131072
    assert "max_ratio" not in wired.qbit.preferences[0], "a seeding host keeps its own limits"


async def test_the_policy_handler_raises_when_the_client_is_down(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """So the runner retries it: "not up yet" is exactly the expected case."""
    wired, _ = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962061, email="policy3@arc.test"
    )
    wired.qbit.down = True

    with pytest.raises(QbitUnavailable):
        await qbit_apply_policy(context(db_session, wired.settings, {}, job_type=QBIT_POLICY))


# --- compute_wants as a handler ---------------------------------------------


async def test_the_compute_wants_handler_reconciles(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    anime = await make_anime(db_session, anilist_id=962040)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    user = await make_user(db_session, "handler@arc.test")
    await make_entry(db_session, user, anime, progress=3)

    await acquisition_jobs.compute_wants(
        context(db_session, settings, {}, job_type="compute_wants")
    )

    wants = (await db_session.scalars(select(Want.episode_id))).all()
    assert set(wants) == {rows[3].id, rows[4].id}
