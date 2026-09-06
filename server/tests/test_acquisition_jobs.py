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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState, Job, JobStatus, MediaFile, Torrent, Want
from arc.services.acquisition import jobs as acquisition_jobs
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition import qbit as qbit_module
from arc.services.acquisition.jobs import (
    GIVE_UP_AFTER,
    NO_RELEASE,
    REMOVED_FROM_CLIENT,
    STARTED_KEY,
    largest_video,
    poll_qbit,
    retry_delay,
    search_release,
)
from arc.services.acquisition.names import SEARCH_RELEASE, search_dedupe_key
from arc.services.acquisition.qbit import QbitUnavailable
from arc.services.jobs.registry import JobContext
from arc.services.library.names import MATCH_FILE
from tests.acquisition_helpers import (
    NyaaStub,
    QbitStub,
    acquisition_settings,
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


async def test_reusing_an_existing_torrent_row_rather_than_duplicating_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``info_hash`` is unique; a retry after a crash must not violate it."""
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962007, email="retryrow@arc.test"
    )
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    episode.state = EpisodeState.WANTED
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    rows = (
        await db_session.scalars(select(Torrent).where(Torrent.info_hash == SUBSPLEASE_1080))
    ).all()
    assert len(rows) == 1


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
