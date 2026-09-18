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
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    MediaFile,
    Torrent,
    TorrentFile,
    TorrentKind,
    Want,
)
from arc.services.acquisition import batch as batch_module
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
    qbit_reselect,
    retry_delay,
    search_release,
    stall_reason,
)
from arc.services.acquisition.names import (
    QBIT_CANCEL,
    QBIT_POLICY,
    QBIT_RESELECT,
    QBIT_RESELECT_PRIORITY,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
    reselect_dedupe_key,
    search_dedupe_key,
)
from arc.services.acquisition.qbit import (
    DECIDED_STATES,
    QBIT_CANCELLED,
    QBIT_REJECTED,
    QBIT_STALLED,
    QBIT_UNREADABLE,
    SEEDING_STATES,
    QbitError,
    QbitUnavailable,
)
from arc.services.acquisition.reject import (
    WRONG_FILE,
    batch_member_of,
    episode_id_of,
    reject_download,
)
from arc.services.acquisition.rules import BYTES_PER_GB, PAUSED_KEY
from arc.services.acquisition.wants import cancel_if_unwanted
from arc.services.jobs.queue import DEDUPE_FIELD
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
    torrent_blob,
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


# --- Absolute numbering on sequels (FR-A4, 2026-09-17) ----------------------


def _feed(title: str, *, info_hash: str, seeders: int = 120) -> str:
    """One Nyaa RSS item, enough for the filter and the ranker."""
    return (
        '<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa" version="2.0"><channel><item>'
        f"<title>{title}</title>"
        "<link>https://nyaa.test/download/1.torrent</link>"
        f"<nyaa:infoHash>{info_hash}</nyaa:infoHash>"
        f"<nyaa:seeders>{seeders}</nyaa:seeders>"
        "<nyaa:leechers>1</nyaa:leechers><nyaa:downloads>9</nyaa:downloads>"
        "<nyaa:size>1.3 GiB</nyaa:size><nyaa:trusted>Yes</nyaa:trusted>"
        "<nyaa:remake>No</nyaa:remake><nyaa:categoryId>1_2</nyaa:categoryId>"
        "</item></channel></rss>"
    )


async def _as_sequel(session: AsyncSession, episode: Episode, *, prequel_episodes: int) -> Anime:
    """Turn this episode's show into a second season with a cached prequel."""
    anime = await session.get(Anime, episode.anime_id)
    assert anime is not None
    prequel = Anime(
        anilist_id=(anime.anilist_id or 0) + 500_000,
        title_romaji="Sousou no Frieren",
        format="TV",
        status="FINISHED",
        episodes=prequel_episodes,
    )
    session.add(prequel)
    await session.flush()
    anime.title_romaji = "Sousou no Frieren 2nd Season"
    anime.title_english = None
    anime.format = "TV"
    anime.episodes = 12
    anime.relations = [
        {
            "anilist_id": prequel.anilist_id,
            "mal_id": None,
            "relation_type": "PREQUEL",
            "format": "TV",
        }
    ]
    await session.flush()
    return anime


async def test_the_offset_is_read_off_the_cached_prequel_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relation blob carries ids and a title; the count is on the other row."""
    _wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962060, email="offset@arc.test", feed=None
    )
    anime = await _as_sequel(db_session, episode, prequel_episodes=28)

    assert await acquisition_jobs._prequel_offset(db_session, anime) == 28


async def test_a_prequel_arc_has_not_cached_declines_the_offset(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rule declines rather than guessing at a length it cannot read."""
    _wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962061, email="nooffset@arc.test", feed=None
    )
    anime = await _as_sequel(db_session, episode, prequel_episodes=28)
    anime.relations = [
        {"anilist_id": 7_654_321, "mal_id": None, "relation_type": "PREQUEL", "format": "TV"}
    ]
    await db_session.flush()

    assert await acquisition_jobs._prequel_offset(db_session, anime) is None


async def test_a_prequel_still_airing_declines_the_offset(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An announced episode count is the number likeliest to be wrong."""
    _wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962064, email="airing@arc.test", feed=None
    )
    anime = await _as_sequel(db_session, episode, prequel_episodes=28)
    prequel = await db_session.scalar(
        select(Anime).where(Anime.anilist_id == (anime.anilist_id or 0) + 500_000)
    )
    assert prequel is not None
    prequel.status = "RELEASING"
    await db_session.flush()

    assert await acquisition_jobs._prequel_offset(db_session, anime) is None


async def test_a_malformed_relation_blob_declines_rather_than_failing(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``relations`` is JSONB from somebody else: a bad id is not a failed search.

    The whole search still runs, asks its ordinary forms and stamps the episode
    — it simply asks no absolute form.
    """
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962065, email="badblob@arc.test", feed=None
    )
    anime = await _as_sequel(db_session, episode, prequel_episodes=28)
    anime.relations = [
        {"anilist_id": "n/a", "mal_id": None, "relation_type": "PREQUEL", "format": "TV"},
        "not even a blob",
    ]
    await db_session.flush()

    assert await acquisition_jobs._prequel_offset(db_session, anime) is None

    with caplog.at_level(logging.INFO):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries
    assert not any("- 35" in query for query in wired.nyaa.queries)
    assert episode.last_search_forms == len(wired.nyaa.queries)


async def test_a_search_asks_for_the_absolute_number_and_takes_that_release(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point, end to end: `- 35` is episode 7 of a season that follows 28.

    The only feed that answers is the absolute form's, so nothing but the new
    rule can produce a download here — and the torrent row it writes belongs to
    the episode numbered 7, not to one numbered 35.
    """
    absolute = "[SubsPlease] Sousou no Frieren - 35 (1080p) [AB12CD34].mkv"
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962062, email="absolute@arc.test", feed=None
    )
    await _as_sequel(db_session, episode, prequel_episodes=28)
    wired.nyaa.answers = {"Sousou no Frieren - 35": _feed(absolute, info_hash="c" * 40)}

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert "Sousou no Frieren - 35" in wired.nyaa.queries
    assert episode.state is EpisodeState.DOWNLOADING
    assert episode.number == 7
    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    assert torrent.title == absolute


async def test_a_season_marked_release_is_preferred_over_the_absolute_one(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit beats inferred even when the inferred one has the seeders."""
    absolute = "[SubsPlease] Sousou no Frieren - 35 (1080p) [AB12CD34].mkv"
    marked = "[Erai-raws] Sousou no Frieren S2 - 07 [1080p][Multiple Subtitle].mkv"
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962063, email="explicit@arc.test", feed=None
    )
    await _as_sequel(db_session, episode, prequel_episodes=28)
    wired.nyaa.answers = {
        "Sousou no Frieren - 35": _feed(absolute, info_hash="c" * 40, seeders=4000),
        "Sousou no Frieren S2 - 07": _feed(marked, info_hash="d" * 40, seeders=3),
    }

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    assert torrent.title == marked


# --- The search diagnostic on the episode row (FR-A7, 2026-09-14) -----------


async def test_a_search_stamps_what_it_asked_and_saw_on_the_episode(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Seven forms for this entry; the one stubbed feed answers twenty items."""
    before = datetime.now(UTC)
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962030, email="stamp@arc.test"
    )

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.last_search_forms == len(wired.nyaa.queries) == 7
    assert episode.last_search_results == 20, "the merged pool, before the filter"
    assert episode.last_search_at is not None and episode.last_search_at >= before


async def test_a_search_that_finds_nothing_stamps_the_zero(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The case the row exists for: "6 forms, 0 results" is a query problem."""
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962031, email="stamp0@arc.test", feed=None
    )

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.SEARCHING
    assert episode.last_search_forms == 7
    assert episode.last_search_results == 0
    assert episode.last_search_at is not None


async def test_a_paused_search_stamps_nothing_because_it_asked_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962032, email="stampoff@arc.test"
    )
    await set_setting(db_session, PAUSED_KEY, True)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries == []
    assert episode.last_search_at is None
    assert episode.last_search_forms is None
    assert episode.last_search_results is None


# --- Narrowing a finished show's search (FR-A4, 2026-09-18) -----------------


#: The first of the nine group-narrowed forms this show earns: romaji dash
#: form, first group. Written out rather than derived, because the *order* is
#: the claim being made.
NARROWED_FIRST = "Sousou no Frieren - 07 HorribleSubs"


async def test_a_finished_shows_search_asks_by_group_and_takes_what_it_finds(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole fix, end to end: nothing under the titles, the episode under a group.

    Every title form answers the empty feed — which is what *Kimetsu no Yaiba*
    episode 10 amounted to after the filter had correctly rejected all 91
    franchise results — and the narrowed form is the one that answers. The log
    line carries both counts, since "7 forms, 16 requests" is the sentence that
    says a search went narrower than it used to.
    """
    single = "[HorribleSubs] Sousou no Frieren - 07 [1080p].mkv"
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962070, email="narrow@arc.test", feed=None
    )
    anime = await db_session.get(Anime, episode.anime_id)
    assert anime is not None
    anime.status = "FINISHED"
    await db_session.flush()
    wired.nyaa.answers = {NARROWED_FIRST: _feed(single, info_hash="e" * 40, seeders=6)}

    with caplog.at_level(logging.INFO):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert NARROWED_FIRST in wired.nyaa.queries
    assert episode.state is EpisodeState.DOWNLOADING
    torrent = await db_session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
    assert torrent is not None
    assert torrent.title == single
    record = next(row for row in caplog.records if row.getMessage() == "nyaa search finished")
    assert episode.last_search_forms is not None
    assert record.__dict__["forms"] == episode.last_search_forms
    assert record.__dict__["requests"] > record.__dict__["forms"]


async def test_an_airing_shows_search_asks_its_title_forms_and_stops(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``RELEASING`` is ``make_anime``'s default status, and the one that narrows not.

    The same release sitting behind the same narrowed form: an airing show does
    not ask, because its own week's upload is inside the newest 75 results and
    Nyaa's patience is the budget.
    """
    single = "[HorribleSubs] Sousou no Frieren - 07 [1080p].mkv"
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=962071, email="noneed@arc.test", feed=None
    )
    wired.nyaa.answers = {NARROWED_FIRST: _feed(single, info_hash="e" * 40, seeders=6)}

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert NARROWED_FIRST not in wired.nyaa.queries
    assert not any("HorribleSubs" in query for query in wired.nyaa.queries)
    assert episode.state is EpisodeState.SEARCHING
    assert episode.last_search_results == 0


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
    # Busy, and busy is not broken.
    (info("checkingDL", time_active=2 * 24 * HOUR), None),
    (info("moving", progress=1.0, time_active=2 * 24 * HOUR), None),
    # ...but the client having stopped is (2026-09-18). No threshold: there is
    # nothing to wait for. ``missingFiles`` is how retention unlinking one
    # episode's file out of a *running* pack shows up.
    (info("error", time_active=2 * 24 * HOUR), "the torrent client reported an error"),
    (info("error", time_active=30), "the torrent client reported an error"),
    (info("missingFiles", progress=0.5, time_active=30), "the downloaded files are missing"),
    (info("missingFiles", time_active=None), "the downloaded files are missing"),
    # Except when it has finished: nothing is waiting on those bytes.
    (info("missingFiles", progress=1.0, time_active=2 * 24 * HOUR), None),
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


# --- Batch support with selective download (FR-A4, FR-A11, 2026-09-18) ------
#
# The production case: *Kimetsu no Yaiba* episode 10 came back under thirteen
# forms as 91 merged results with no season-one single among them and several
# complete-season packs. FR-A4's "never fetch whole seasons" is a statement
# about *bytes*, so a finished show with no acceptable single may take one of
# those packs and download only the wanted episode's file. Everything below is
# about the one thing that makes it safe: there is no instant at which an
# unwanted file is selected and the torrent is running.
#
# The show here is `wire`'s Frieren rather than Kimetsu, because `wire` already
# builds the user, the entry and the want; what is borrowed from the real case
# is its shape — a `FINISHED` entry whose feed holds packs and nothing else.

#: The pack that covers episode 7 (and 8, and everything else), and the hash
#: it is offered under. The hash is written into the feed's ``<link>`` so
#: ``NyaaStub`` can answer the ``.torrent`` behind it with a blob carrying the
#: same identity — which is what the real client reads out of the bencode.
BATCH_TITLE = "[Erai-raws] Sousou no Frieren - 01 ~ 28 [1080p][BATCH]"
BATCH_HASH = "b" * 40
#: A second pack at the same resolution and a tenth of the seeders, so it ranks
#: behind the first by FR-A3's third rule and "try the next candidate" has one
#: to try. It names **no** range, which is the other shape a pack comes in:
#: coverage its file list settles rather than its name.
SECOND_TITLE = "[Judas] Sousou no Frieren [BD 1080p][BATCH]"
SECOND_HASH = "c" * 40
#: And the single that is not: nobody is holding it, which is what
#: ``MIN_SEEDERS`` rejects and therefore what leaves ``Search.ranked`` empty.
DEAD_SINGLE = "[HorribleSubs] Sousou no Frieren - 07 [1080p].mkv"


def _pack_names(*numbers: int, group: str = "Erai-raws") -> list[str]:
    """One file per episode, inside the directory a pack puts them in."""
    return [
        f"Sousou no Frieren/[{group}] Sousou no Frieren - {number:02d} [1080p].mkv"
        for number in numbers
    ]


def _batch_pool(*releases: tuple[str, str, int]) -> str:
    """A Nyaa feed of ``(title, info hash, seeders)``, hash in the link."""
    items = "".join(
        "<item>"
        f"<title>{title}</title>"
        f"<link>https://nyaa.test/download/{info_hash}.torrent</link>"
        f"<nyaa:infoHash>{info_hash}</nyaa:infoHash>"
        f"<nyaa:seeders>{seeders}</nyaa:seeders>"
        "<nyaa:leechers>2</nyaa:leechers><nyaa:downloads>9</nyaa:downloads>"
        "<nyaa:size>14.8 GiB</nyaa:size><nyaa:trusted>No</nyaa:trusted>"
        "<nyaa:remake>No</nyaa:remake><nyaa:categoryId>1_2</nyaa:categoryId>"
        "</item>"
        for title, info_hash, seeders in releases
    )
    return (
        '<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa" version="2.0">'
        f"<channel>{items}</channel></rss>"
    )


#: The pool every test below starts from: a dead single and one pack.
ONE_PACK = _batch_pool((DEAD_SINGLE, "d" * 40, 0), (BATCH_TITLE, BATCH_HASH, 30))
#: And the same with a second pack behind it, for the refusal paths.
TWO_PACKS = _batch_pool(
    (DEAD_SINGLE, "d" * 40, 0),
    (BATCH_TITLE, BATCH_HASH, 30),
    (SECOND_TITLE, SECOND_HASH, 5),
)


async def _finished(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
    pool: str = ONE_PACK,
    status: str = "FINISHED",
    fmt: str = "TV",
) -> tuple[Wired, Episode]:
    """``wire``, with the show finished and every query answering ``pool``.

    ``default`` rather than a keyed answer: the point of the fixture is that
    *no* form finds a single, which is what the thirteen forms of the real case
    amounted to once the filter had correctly rejected all 91 results.
    """
    wired, episode = await wire(
        session, monkeypatch, tmp_path, anilist_id=anilist_id, email=email, feed=None
    )
    anime = await session.get(Anime, episode.anime_id)
    assert anime is not None
    anime.status = status
    anime.format = fmt
    await session.flush()
    wired.nyaa.default = pool
    return wired, episode


async def _torrent_files(session: AsyncSession, torrent_id: int) -> list[TorrentFile]:
    rows = await session.scalars(
        select(TorrentFile)
        .where(TorrentFile.torrent_id == torrent_id)
        .order_by(TorrentFile.file_index)
    )
    return list(rows.all())


async def test_a_finished_show_with_no_single_takes_a_batch_and_one_file(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole feature in one run, asserted in the order it happens.

    Twenty-eight files in the torrent, one of them selected, and the log line
    says both figures — which is the sentence that proves FR-A4's spirit is
    intact rather than merely claimed.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962100, email="batch1@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))

    with caplog.at_level(logging.INFO):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    # 1. the ``.torrent`` itself, not the magnet.
    assert wired.nyaa.fetched == [f"https://nyaa.test/download/{BATCH_HASH}.torrent"]
    # 2. added as a file, stopped, under its own directory.
    assert len(wired.qbit.uploaded) == 1
    fields = wired.qbit.uploaded[0]["fields"]
    assert fields["savepath"] == f"/data/downloads/batch/{BATCH_HASH}"
    assert fields["stopped"] == "true" and fields["paused"] == "true"
    assert fields["category"] == "arc" and fields["tags"] == acquisition_jobs.BATCH_TAGS
    # 4/5. every file off **before** the wanted one on, which is the order the
    # byte guarantee is made of.
    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, list(range(28))),
        (1, [6]),
    ]
    # 7. and the start is the last call of the sequence.
    sequence = [call for call in wired.qbit.calls if call.endswith(("/filePrio", "/start"))]
    assert sequence[-1].endswith("/start")
    assert wired.qbit.started == [BATCH_HASH]

    torrent = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None
    assert torrent.kind is TorrentKind.BATCH
    assert torrent.episode_id is None, "a batch belongs to no single episode"
    assert torrent.save_path == f"/data/downloads/batch/{BATCH_HASH}"
    assert torrent.title == BATCH_TITLE and torrent.group == "Erai-raws"
    assert torrent.wanted_bytes is not None and torrent.total_size is not None
    assert torrent.wanted_bytes < torrent.total_size

    rows = await _torrent_files(db_session, torrent.id)
    assert len(rows) == 28
    wanted = [row for row in rows if row.wanted]
    assert [row.file_index for row in wanted] == [6]
    assert wanted[0].episode_id == episode.id
    assert wanted[0].priority == 1
    assert all(row.priority == 0 for row in rows if not row.wanted)
    assert episode.state is EpisodeState.DOWNLOADING

    record = next(row for row in caplog.records if row.getMessage() == "batch chosen")
    assert record.__dict__["files"] == 28 and record.__dict__["wanted"] == 1
    assert record.__dict__["wanted_bytes"] < record.__dict__["total_size"]
    assert " of " in record.__dict__["selected"]


async def test_the_not_wanted_files_are_recorded_with_their_episodes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rows nobody asked for are what makes the *second* want free."""
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962101, email="batch2@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    torrent = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None
    rows = await _torrent_files(db_session, torrent.id)
    assert all(row.episode_id is not None for row in rows[:12])
    assert sum(1 for row in rows if row.wanted) == 1


async def test_extras_inside_the_pack_are_never_selected(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NCOP, NCED, the ``.nfo`` and the sample stay at priority 0.

    Excluded by the kind the parser reads rather than by a rule of their own,
    and asserted through the *client* rather than through the plan: what
    matters is the request that was actually made.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962102, email="batch3@arc.test"
    )
    wired.qbit.add_files(
        BATCH_HASH,
        [
            "Sousou no Frieren/[Erai-raws] Sousou no Frieren - NCOP [1080p].mkv",
            "Sousou no Frieren/[Erai-raws] Sousou no Frieren - NCED1 [1080p].mkv",
            "Sousou no Frieren/Sousou no Frieren.nfo",
            "Sousou no Frieren/sample.mkv",
            *_pack_names(7),
        ],
    )

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, [0, 1, 2, 3, 4]),
        (1, [4]),
    ]
    assert episode.state is EpisodeState.DOWNLOADING


async def test_a_batch_whose_wanted_file_cannot_be_identified_is_deleted(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The plan's D5: a pack Arc cannot read is not downloaded at all.

    It was added stopped, so nothing has been fetched — and it goes with its
    files, the episode keeps FR-A6's ordinary retry, and the row it leaves
    behind is the tombstone that stops the same pack being fetched again in six
    hours (:data:`~arc.services.acquisition.qbit.QBIT_UNREADABLE`).
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962103, email="batch4@arc.test"
    )
    # The pack holds the show, and not this episode.
    wired.qbit.add_files(BATCH_HASH, _pack_names(1, 2, 3))

    with caplog.at_level(logging.WARNING):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.deleted == [{"hashes": BATCH_HASH, "deleteFiles": "true"}]
    assert wired.qbit.started == [], "nothing may be started that was not read"
    assert wired.qbit.torrents == [], "and it is gone from the client"
    tombstone = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert tombstone is not None
    assert tombstone.qbit_state == QBIT_UNREADABLE
    assert tombstone.kind is TorrentKind.BATCH and tombstone.episode_id is None
    assert await _torrent_files(db_session, tombstone.id) == []
    assert episode.state is EpisodeState.SEARCHING
    assert len(await queued(db_session, SEARCH_RELEASE)) == 1
    refused = next(
        row
        for row in caplog.records
        if row.getMessage() == "a batch was refused and deleted before anything was fetched"
    )
    assert refused.__dict__["reason"] == "no file in the batch is episode 7"


async def test_two_files_claiming_the_episode_refuse_the_batch(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A v1 beside a v2 is a guess, and a guess is not made."""
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962104, email="batch5@arc.test"
    )
    wired.qbit.add_files(
        BATCH_HASH,
        [
            "Sousou no Frieren/[Erai-raws] Sousou no Frieren - 07 [1080p].mkv",
            "Sousou no Frieren/[Erai-raws] Sousou no Frieren - 07v2 [1080p].mkv",
        ],
    )

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.priorities == [], "a refused plan writes no selection at all"
    assert wired.qbit.deleted != []
    assert episode.state is EpisodeState.SEARCHING


async def test_a_tampered_read_back_deletes_the_batch_and_tries_the_next(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The byte-safety gate, with a client that accepted the write and ignored it.

    The first pack reports a file selected that Arc turned off; it is deleted
    while still stopped and the second pack — which behaves — is taken. This is
    the one check whose whole value is that it never fires in practice.
    """
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962105,
        email="batch6@arc.test",
        pool=TWO_PACKS,
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))
    wired.qbit.ignores_prio[BATCH_HASH] = [0]
    wired.qbit.add_files(SECOND_HASH, _pack_names(*range(1, 13)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.deleted == [{"hashes": BATCH_HASH, "deleteFiles": "true"}]
    assert wired.qbit.started == [SECOND_HASH]
    torrent = await db_session.scalar(select(Torrent))
    assert torrent is not None and torrent.info_hash == SECOND_HASH
    assert episode.state is EpisodeState.DOWNLOADING
    # **No tombstone.** A read-back the client disagreed with is a statement
    # about the client this minute, not about the pack, so the release is not
    # barred for ever over it.
    assert (await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))) is None


async def test_every_batch_refused_ends_unavailable_with_its_own_sentence(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FR-A6's fortnight, FR-A7's reason: "there were packs and I could not read them".

    A different thing to be told than "no acceptable release found", and the
    only one of the two a user could act on.
    """
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962106,
        email="batch7@arc.test",
        pool=TWO_PACKS,
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(1, 2))
    wired.qbit.add_files(SECOND_HASH, _pack_names(3, 4))
    started = (datetime.now(UTC) - GIVE_UP_AFTER - timedelta(hours=1)).isoformat()

    await search_release(
        context(
            db_session,
            wired.settings,
            {"episode_id": episode.id, STARTED_KEY: started},
        )
    )

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == batch_module.UNREADABLE_BATCH
    assert episode.unavailable_reason != NO_RELEASE
    assert len(wired.qbit.deleted) == 2, "both packs were added, read and removed"
    # Both left a tombstone, so tomorrow's revival does not fetch either again.
    states = (await db_session.scalars(select(Torrent.qbit_state))).all()
    assert sorted(states) == [QBIT_UNREADABLE, QBIT_UNREADABLE]


async def test_two_episodes_wanted_at_once_share_one_batch(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One torrent, one add, two files, two episodes downloading.

    The payoff taken at the only moment it is free: the selection is being
    written anyway, so the second episode costs one more index in one request
    Arc was making regardless.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962107, email="batch8@arc.test"
    )
    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 8)
    )
    assert neighbour is not None
    neighbour.state = EpisodeState.WANTED
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    db_session.add(Want(user_id=want.user_id, episode_id=neighbour.id))
    await db_session.flush()
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert len(wired.qbit.uploaded) == 1, "one pack, added once"
    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, list(range(28))),
        (1, [6, 7]),
    ]
    assert episode.state is EpisodeState.DOWNLOADING
    assert neighbour.state is EpisodeState.DOWNLOADING
    torrent = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None
    wanted = [row for row in await _torrent_files(db_session, torrent.id) if row.wanted]
    assert {row.episode_id for row in wanted} == {episode.id, neighbour.id}
    assert torrent.wanted_bytes == sum(row.size for row in wanted)


async def test_a_later_episode_attaches_to_the_batch_without_asking_nyaa(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``claim_existing``, which is the whole reason episode 11 is free.

    Not one Nyaa request and not one byte of overhead: the file is already in a
    torrent Arc holds, so the search turns it on and queues the re-selection.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962108, email="batch9@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    torrent = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None
    wired.nyaa.queries.clear()
    wired.nyaa.fetched.clear()
    wired.qbit.calls.clear()

    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 11)
    )
    assert neighbour is not None
    neighbour.state = EpisodeState.WANTED
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    db_session.add(Want(user_id=want.user_id, episode_id=neighbour.id))
    await db_session.flush()

    await search_release(
        context(db_session, wired.settings, {"episode_id": neighbour.id}, job_id=2)
    )

    assert wired.nyaa.queries == [], "an attached episode asks nyaa nothing at all"
    assert wired.nyaa.fetched == []
    assert wired.qbit.calls == [], "and the client is not touched here either"
    assert neighbour.state is EpisodeState.DOWNLOADING
    assert len(wired.qbit.uploaded) == 1, "still one pack in the client"

    row = await db_session.scalar(select(TorrentFile).where(TorrentFile.episode_id == neighbour.id))
    assert row is not None and row.wanted
    jobs = await queued(db_session, QBIT_RESELECT)
    assert [job.payload["torrent_id"] for job in jobs] == [torrent.id]
    assert jobs[0].priority == QBIT_RESELECT_PRIORITY
    assert jobs[0].payload["dedupe_key"] == reselect_dedupe_key(torrent.id)


async def test_a_decided_batch_is_not_attached_to(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pack that stalled is not a pack to serve another episode from."""
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962109, email="batch10@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    torrent = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None
    torrent.qbit_state = QBIT_STALLED
    await db_session.flush()

    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 11)
    )
    assert neighbour is not None
    neighbour.state = EpisodeState.WANTED
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    db_session.add(Want(user_id=want.user_id, episode_id=neighbour.id))
    await db_session.flush()
    wired.nyaa.queries.clear()

    await search_release(
        context(db_session, wired.settings, {"episode_id": neighbour.id}, job_id=2)
    )

    assert wired.nyaa.queries != [], "it searched rather than attaching"
    assert await queued(db_session, QBIT_RESELECT) == []
    row = await db_session.scalar(select(TorrentFile).where(TorrentFile.episode_id == neighbour.id))
    assert row is not None and not row.wanted


async def test_a_batch_arc_has_already_recorded_is_not_added_twice(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_pick``'s rule, applied to the batch branch: a hash with a row was tried.

    The next candidate down is taken instead, which is what keeps a pack that
    stalled from being fetched again every six hours.
    """
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962110,
        email="batch11@arc.test",
        pool=TWO_PACKS,
    )
    db_session.add(
        Torrent(
            kind=TorrentKind.BATCH,
            episode_id=None,
            info_hash=BATCH_HASH,
            qbit_state=QBIT_STALLED,
        )
    )
    await db_session.flush()
    wired.qbit.add_files(SECOND_HASH, _pack_names(*range(1, 13)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.fetched == [f"https://nyaa.test/download/{SECOND_HASH}.torrent"]
    assert wired.qbit.started == [SECOND_HASH]
    assert episode.state is EpisodeState.DOWNLOADING


async def test_reserving_a_batch_another_search_took_first_raises(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The concurrent-pick race, from the losing side.

    ``torrents.info_hash`` is unique, so the loser of two searches ranking the
    same pack fails — deliberately and by name here rather than on the flush —
    the job retries, and the retry's ``claim_existing`` attaches to the row the
    winner wrote instead of adding the pack a second time. The two tests after
    this one are the *timing* of that failure and that retry.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962111, email="batch12@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    winner = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert winner is not None

    with pytest.raises(batch_module.BatchTaken, match="was recorded as torrent"):
        await batch_module.reserve_batch(
            db_session,
            ranked=await _a_batch_candidate(db_session, wired, episode),
            save_path="/data/downloads/batch/x",
            info_hash=BATCH_HASH,
        )


async def test_the_loser_of_a_race_reserves_before_it_touches_the_selection(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**The order the whole race turns on.**

    Two searches for two episodes of one finished show run on two worker slots.
    Neither has committed, so both get past "a hash with a row was already
    tried" — and the window in which the other one commits is real and wide: it
    is a paced Nyaa request long, which is where this test puts it. If the
    selection were written before the row, the loser would run ``filePrio 0``
    over **every** index of a torrent the winner had already selected files in
    and started, wiping the winner's selection and leaving an episode
    downloading nothing, and would only discover it had lost at the end.

    So: the winner's row appears while the loser is fetching the ``.torrent``,
    and the loser must raise having touched the client's *selection* not at all.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962122, email="batch23@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    # The winner got there first and left the client selecting file 6 only.
    for row in wired.qbit.file_lists[BATCH_HASH]:
        row["priority"] = 1 if row["index"] == 6 else 0
    winners_selection = [dict(row) for row in wired.qbit.file_lists[BATCH_HASH]]

    fetch = nyaa_module.NyaaClient.torrent_file

    async def racing(self: nyaa_module.NyaaClient, url: str) -> bytes:
        """The other worker commits its row while this one is fetching."""
        blob = await fetch(self, url)
        db_session.add(
            Torrent(
                kind=TorrentKind.BATCH,
                episode_id=None,
                info_hash=BATCH_HASH,
                qbit_state="added",
                save_path=f"/data/downloads/batch/{BATCH_HASH}",
            )
        )
        await db_session.flush()
        return blob

    monkeypatch.setattr(nyaa_module.NyaaClient, "torrent_file", racing)

    with pytest.raises(batch_module.BatchTaken):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.priorities == [], "the loser must not write a selection"
    assert wired.qbit.stopped == [], "nor stop the winner's torrent"
    assert wired.qbit.started == [], "nor start anything"
    assert wired.qbit.file_lists[BATCH_HASH] == winners_selection
    # And nothing is selected that has no ``wanted`` row behind it: the loser
    # wrote no rows, and the winner's row (this test's stand-in) has none yet.
    torrent = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None
    assert await _torrent_files(db_session, torrent.id) == []


async def _a_batch_candidate(
    session: AsyncSession, wired: Wired, episode: Episode
) -> nyaa_module.Ranked:
    """One real batch candidate for this show, for the unit-level calls."""
    anime = await session.get(Anime, episode.anime_id)
    assert anime is not None
    found = await nyaa_module.search_for_episode(
        nyaa_module.shared_client(wired.settings.nyaa_url),
        anime,
        8,
        await acquisition_rules.load_rules(session, anime.id),
    )
    return found.batches[0]


async def _client_files(wired: Wired, info_hash: str) -> list[qbit_module.FileInfo]:
    """What ``torrents/files`` says, through the real client."""
    async with qbit_module.QbitClient.from_settings(wired.settings) as qbit:
        return await qbit.files(info_hash)


async def test_the_retry_after_the_race_attaches_instead_of_adding_again(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half: the loser's retry finds the winner's row and attaches."""
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962112, email="batch13@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    loser = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 9)
    )
    assert loser is not None
    loser.state = EpisodeState.SEARCHING
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    db_session.add(Want(user_id=want.user_id, episode_id=loser.id))
    await db_session.flush()
    wired.nyaa.queries.clear()

    await search_release(context(db_session, wired.settings, {"episode_id": loser.id}, job_id=3))

    assert wired.nyaa.queries == []
    assert loser.state is EpisodeState.DOWNLOADING
    assert len(wired.qbit.uploaded) == 1


async def test_the_batch_fallback_can_be_turned_off(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The kill switch (FR-D2): off, and the search is what it was before M16.

    The packs are still *found* — that costs nothing, it is the pool the forms
    already merged — and not one of them is fetched, added or recorded.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962113, email="batch14@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await set_setting(db_session, acquisition_rules.BATCH_FALLBACK_KEY, False)

    with caplog.at_level(logging.INFO):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.fetched == [], "no .torrent is fetched"
    assert wired.qbit.uploaded == [] and wired.qbit.calls == []
    assert await db_session.scalar(select(Torrent)) is None
    assert episode.state is EpisodeState.SEARCHING
    assert len(await queued(db_session, SEARCH_RELEASE)) == 1
    assert any(
        row.getMessage() == "batch fallback is off; the batches on offer were not considered"
        for row in caplog.records
    )


async def test_a_malformed_fallback_row_reads_as_the_default(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The readers are lenient: one hand-edited row must not decide this."""
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962114, email="batch15@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await set_setting(db_session, acquisition_rules.BATCH_FALLBACK_KEY, "yes please")

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.DOWNLOADING


async def test_a_paused_run_touches_no_batch(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Paused means paused, and a pack is the greediest thing to fetch."""
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962115, email="batch16@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await set_setting(db_session, PAUSED_KEY, True)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries == [] and wired.nyaa.fetched == []
    assert wired.qbit.calls == []
    assert episode.state is EpisodeState.WANTED
    assert await db_session.scalar(select(Torrent)) is None


async def test_a_storage_held_run_touches_no_batch(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FR-T6, and the figure that matters here is the pack's, not the file's."""
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962116, email="batch17@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    fake_free_space(monkeypatch, acquisition_rules, 1 * BYTES_PER_GB)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries == [] and wired.nyaa.fetched == []
    assert wired.qbit.calls == []
    assert episode.state is EpisodeState.WANTED


async def test_an_airing_show_is_offered_no_batch(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same pool, the same want, and a ``RELEASING`` entry: nothing happens.

    Its own week's release is inside the newest 75 results, and a pack of
    twenty-eight is twenty-seven files nobody asked for.
    """
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962117,
        email="batch18@arc.test",
        status="RELEASING",
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries != [], "it did search"
    assert wired.nyaa.fetched == [], "and fetched no .torrent"
    assert wired.qbit.uploaded == []
    assert not any(call.endswith("/torrents/add") for call in wired.qbit.calls)
    assert episode.state is EpisodeState.SEARCHING


async def test_a_film_is_offered_no_batch(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``MOVIE`` entry is one release: there is no episode for a plan to find.

    ``nyaa.is_single`` is what gates it, which is why the entry's format is the
    only thing this test changes — a franchise's three films in one torrent is
    exactly the download FR-A4 forbids.
    """
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962118,
        email="batch19@arc.test",
        fmt="MOVIE",
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.fetched == []
    assert wired.qbit.uploaded == []
    assert not any(call.endswith("/torrents/add") for call in wired.qbit.calls)


async def test_a_single_however_thin_beats_every_pack(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``not ranked`` is the gate, not "fewer than three": the episode wins.

    One seeder, no preferred group — it is still the episode, and the batch
    branch is never reached.
    """
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962119,
        email="batch20@arc.test",
        pool=_batch_pool(
            (DEAD_SINGLE, "e" * 40, 1),
            (BATCH_TITLE, BATCH_HASH, 900),
        ),
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.fetched == []
    assert wired.qbit.uploaded == []
    torrent = await db_session.scalar(select(Torrent))
    assert torrent is not None
    assert torrent.kind is TorrentKind.SINGLE and torrent.info_hash == "e" * 40


async def test_a_pack_with_too_many_files_is_refused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A collection somebody uploaded, not a season (``MAX_TORRENT_FILES``)."""
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962120, email="batch21@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, qbit_module.MAX_TORRENT_FILES + 2)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.priorities == [], "nothing is written to a pack that big"
    assert wired.qbit.deleted != []
    assert episode.state is EpisodeState.SEARCHING


async def test_a_torrent_nyaa_will_not_hand_over_skips_to_the_next_pack(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One candidate Arc cannot read is not a failure for the episode."""
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962121,
        email="batch22@arc.test",
        pool=TWO_PACKS,
    )
    # An HTML interstitial rather than a bencoded dict: exactly what a
    # rate-limited Nyaa answers, and what ``torrent_file`` refuses.
    wired.nyaa.blobs[f"https://nyaa.test/download/{BATCH_HASH}.torrent"] = b"<html>no</html>"
    wired.qbit.add_files(SECOND_HASH, _pack_names(*range(1, 13)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.uploaded != [] and len(wired.qbit.uploaded) == 1
    assert wired.qbit.started == [SECOND_HASH]
    assert episode.state is EpisodeState.DOWNLOADING


async def test_the_pack_is_stopped_again_before_its_selection_is_written(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Belt and braces to ``stopped=true``, and it is not theoretical.

    ``add_file`` reports success for a torrent the client already holds **in
    any run state** — a crash after the start with the row rolled back, an
    operator's own add of the same pack — and a build that honours neither
    ``stopped`` nor ``paused`` would download the whole season at full speed
    for the two round trips it takes to write the selection. One idempotent
    call closes all three, and it has to come before the first ``filePrio``.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962123, email="batch24@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.stopped == [BATCH_HASH]
    order = [call for call in wired.qbit.calls if call.endswith(("/stop", "/filePrio", "/start"))]
    assert order[0].endswith("/stop") and order[-1].endswith("/start")
    assert episode.state is EpisodeState.DOWNLOADING


async def test_a_pack_whose_file_list_cannot_be_read_is_never_started(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A row with no usable name used to be dropped from the listing.

    That is the one way this could be quietly unsafe: the index behind it would
    never be named in the ``filePrio 0`` that turns every file off — and a
    freshly added torrent has every file selected — while being absent from
    both listings the read-back compares, so it would download unseen. The
    client now refuses to describe such a pack at all, and Arc does not start
    what it cannot enumerate.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962124, email="batch25@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    # A file the client lists and cannot name. Nothing else about the pack is
    # wrong: episode 7 is right there at index 6.
    wired.qbit.file_lists[BATCH_HASH][3] = {"index": 3, "size": 1, "priority": 1, "progress": 0.0}

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.priorities == [], "not one priority is written to it"
    assert wired.qbit.started == []
    assert wired.qbit.deleted == [{"hashes": BATCH_HASH, "deleteFiles": "true"}]
    tombstone = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert tombstone is not None and tombstone.qbit_state == QBIT_UNREADABLE
    assert episode.state is EpisodeState.SEARCHING


async def test_a_blob_that_is_not_the_advertised_torrent_is_refused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Everything after the add is keyed on a hash that came out of a feed.

    So the client has to confirm the identity and not merely the success: a
    ``.torrent`` that turns out to be some other torrent is a refused
    candidate, remembered so the same feed item is not fetched again in six
    hours, and the next pack down is taken.
    """
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962125,
        email="batch26@arc.test",
        pool=TWO_PACKS,
    )
    # The bytes behind the first item are a different torrent altogether.
    wired.nyaa.blobs[f"https://nyaa.test/download/{BATCH_HASH}.torrent"] = torrent_blob("f" * 40)
    wired.qbit.add_files(SECOND_HASH, _pack_names(*range(1, 13)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    tombstone = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert tombstone is not None and tombstone.qbit_state == QBIT_UNREADABLE
    assert wired.qbit.started == [SECOND_HASH]
    assert episode.state is EpisodeState.DOWNLOADING


async def test_the_client_confirming_another_hash_is_an_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same rule at the client, where it is one line and easy to lose."""
    wired, _ = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962126, email="batch27@arc.test"
    )

    async with qbit_module.QbitClient.from_settings(wired.settings) as qbit:
        with pytest.raises(QbitError, match="did not accept the torrent file"):
            await qbit.add_file(
                torrent_blob("a" * 40),
                save_path="/data/downloads/batch/x",
                info_hash="b" * 40,
                tags="arc,batch",
            )


async def test_a_free_rider_the_pack_does_not_hold_is_dropped_not_refused(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pack is still the answer for the episode the search is *for*.

    Episode 8 asked to ride along and this pack stops at 7. Refusing over that
    would leave episode 7 unfetched too — and episode 8 has a search of its own
    (FR-A6), which will find its own pack or attach to a later one.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962127, email="batch28@arc.test"
    )
    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 8)
    )
    assert neighbour is not None
    neighbour.state = EpisodeState.WANTED
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    db_session.add(Want(user_id=want.user_id, episode_id=neighbour.id))
    await db_session.flush()
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 8)))

    with caplog.at_level(logging.INFO):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert episode.state is EpisodeState.DOWNLOADING
    assert neighbour.state is EpisodeState.WANTED, "it keeps its own search"
    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, list(range(7))),
        (1, [6]),
    ]
    chosen = next(row for row in caplog.records if row.getMessage() == "batch chosen")
    assert chosen.__dict__["episodes"] == [7] and chosen.__dict__["not_held"] == [8]


async def test_an_episode_already_searching_is_not_taken_along(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It has a ``search_release`` of its own, quite possibly on the other slot.

    That job may be about to add a *single* for it, and an episode with a single
    and a batch claim at once is two downloads of one episode and a row nobody
    will ever clear. An episode that is merely ``wanted`` has no such job in
    flight, and it attaches for free when its own search runs.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962128, email="batch29@arc.test"
    )
    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 8)
    )
    assert neighbour is not None
    neighbour.state = EpisodeState.SEARCHING
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    db_session.add(Want(user_id=want.user_id, episode_id=neighbour.id))
    await db_session.flush()
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, list(range(28))),
        (1, [6]),
    ]
    assert neighbour.state is EpisodeState.SEARCHING
    row = await db_session.scalar(select(TorrentFile).where(TorrentFile.episode_id == neighbour.id))
    assert row is not None and not row.wanted, "its file is recorded and not claimed"


async def test_an_unreadable_pack_is_not_fetched_again_tomorrow(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The point of the tombstone, and of the sentence the second run gives.

    Without the row this pack is fetched from Nyaa, added, read and deleted
    again every six hours, for every episode of the show, for a fortnight. And
    the second run's reason must not claim it read anything: every candidate was
    *skipped*, so the ordinary no-release sentence is the honest one.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962129, email="batch30@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(1, 2, 3))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    assert await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    wired.nyaa.fetched.clear()
    wired.qbit.uploaded.clear()
    started = (datetime.now(UTC) - GIVE_UP_AFTER - timedelta(hours=1)).isoformat()

    await search_release(
        context(
            db_session,
            wired.settings,
            {"episode_id": episode.id, STARTED_KEY: started},
            job_id=2,
        )
    )

    assert wired.nyaa.fetched == [], "the .torrent is not fetched a second time"
    assert wired.qbit.uploaded == []
    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == NO_RELEASE, "nothing was read this time"


async def test_a_batch_the_client_has_lost_is_not_attached_to(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``missing`` is not a decision, and it is the same answer to the question.

    A torrent that is gone from the client is not going to complete another
    episode's file, so an episode attached to it would sit ``downloading`` for
    ever waiting for bytes nobody is sending.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962130, email="batch31@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    torrent = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None
    torrent.qbit_state = qbit_module.QBIT_MISSING
    await db_session.flush()

    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 11)
    )
    assert neighbour is not None
    neighbour.state = EpisodeState.WANTED
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    db_session.add(Want(user_id=want.user_id, episode_id=neighbour.id))
    await db_session.flush()
    wired.nyaa.queries.clear()

    await search_release(
        context(db_session, wired.settings, {"episode_id": neighbour.id}, job_id=2)
    )

    assert wired.nyaa.queries != [], "it searched rather than attaching"
    assert await queued(db_session, QBIT_RESELECT) == []


async def test_the_kill_switch_stops_new_files_being_enabled_too(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An admin who turns it off means "fetch no more of this" (FR-D2).

    Enabling another file in a pack Arc already holds is still fetching more, so
    the attach is gated by the same switch as the pick. What keeps running is
    what is already claimed: the episode the pack was taken for is left exactly
    as it was.
    """
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=962131, email="batch32@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))
    assert episode.state is EpisodeState.DOWNLOADING

    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 11)
    )
    assert neighbour is not None
    neighbour.state = EpisodeState.WANTED
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    db_session.add(Want(user_id=want.user_id, episode_id=neighbour.id))
    await set_setting(db_session, acquisition_rules.BATCH_FALLBACK_KEY, False)

    await search_release(
        context(db_session, wired.settings, {"episode_id": neighbour.id}, job_id=2)
    )

    assert neighbour.state is EpisodeState.SEARCHING, "it looked for a single instead"
    assert await queued(db_session, QBIT_RESELECT) == []
    row = await db_session.scalar(select(TorrentFile).where(TorrentFile.episode_id == neighbour.id))
    assert row is not None and not row.wanted
    assert episode.state is EpisodeState.DOWNLOADING, "what is already claimed keeps going"


# --- The batch poll and the re-selection (FR-A11, 2026-09-18) ---------------
#
# Where the batch pick above ends — a pack in the client with one file selected
# — this begins. Two ideas are being tested and nothing else: **an episode is
# complete when its own file is**, and a pack whose work is done is *stopped and
# kept* rather than deleted, because the next episode of the show is one
# `filePrio` away from being served for nothing.


async def _in_flight(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
    also: tuple[int, ...] = (),
) -> tuple[Wired, Episode, Torrent]:
    """A pack of 28 files taken for episode 7, plus any episode in ``also``.

    The client's calls are cleared afterwards, so every assertion below is
    about what the *poll* did rather than about what the pick did.
    """
    wired, episode = await _finished(
        session, monkeypatch, tmp_path, anilist_id=anilist_id, email=email
    )
    want = await session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    for number in also:
        neighbour = await session.scalar(
            select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == number)
        )
        assert neighbour is not None
        neighbour.state = EpisodeState.WANTED
        session.add(Want(user_id=want.user_id, episode_id=neighbour.id))
    await session.flush()

    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    await search_release(context(session, wired.settings, {"episode_id": episode.id}))
    torrent = await session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None
    wired.qbit.calls.clear()
    wired.qbit.priorities.clear()
    wired.qbit.started.clear()
    wired.qbit.stopped.clear()
    return wired, episode, torrent


def _member(tmp_path: Path, number: int, *, size: int = 4096) -> Path:
    """Write one of the pack's files where the worker will look for it."""
    path = tmp_path / "downloads" / "batch" / BATCH_HASH / _pack_names(number)[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _arrived(wired: Wired, *indices: int) -> None:
    """Tell the client those file indices are 100 % downloaded."""
    for row in wired.qbit.file_lists[BATCH_HASH]:
        if row["index"] in indices:
            row["progress"] = 1.0


def _files_calls(wired: Wired) -> int:
    return sum(1 for call in wired.qbit.calls if call.endswith("/torrents/files"))


async def _poll(session: AsyncSession, wired: Wired) -> None:
    await poll_qbit(context(session, wired.settings, {}, job_type="poll_qbit"))


async def test_a_finished_batch_file_hands_off_its_own_path(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**Its** file, not the biggest one — which inside a pack is another episode.

    The directory holds episode 1 at twice the size, so a hand-off that used
    ``largest_video`` would index the wrong episode with episode 7's prior. That
    is the one mistake this loop exists to not make.
    """
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962200, email="poll-batch1@arc.test"
    )
    _member(tmp_path, 1, size=8192)
    video = _member(tmp_path, 7)
    _arrived(wired, 6)

    await _poll(db_session, wired)

    assert episode.state is EpisodeState.MATCHING
    media = await db_session.scalar(select(MediaFile).where(MediaFile.path == str(video.resolve())))
    assert media is not None, "the file the row names, not the biggest in the directory"
    match_jobs = await queued(db_session, MATCH_FILE)
    assert len(match_jobs) == 1
    assert match_jobs[0].payload["media_file_id"] == media.id
    assert match_jobs[0].payload["expected"] == [episode.anime_id, episode.number]

    rows = await _torrent_files(db_session, torrent.id)
    done = next(row for row in rows if row.file_index == 6)
    assert done.completed_at is not None and done.progress == pytest.approx(1.0)
    assert all(row.completed_at is None for row in rows if row.file_index != 6)

    # Every file Arc asked for is in, so the pack is stopped — and kept.
    assert torrent.completed_at is not None
    assert wired.qbit.stopped == [BATCH_HASH]
    assert wired.qbit.deleted == [], "a finished pack is never deleted by the poll"
    assert wired.qbit.torrents != [], "and it is still in the client"
    kept = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert kept is not None


async def test_a_second_file_completes_its_own_episode_only(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two wants, one pack, two polls: each episode moves when its file lands."""
    wired, episode, torrent = await _in_flight(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962201,
        email="poll-batch2@arc.test",
        also=(8,),
    )
    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 8)
    )
    assert neighbour is not None
    _member(tmp_path, 7)
    _member(tmp_path, 8)
    _arrived(wired, 6)

    await _poll(db_session, wired)

    assert episode.state is EpisodeState.MATCHING
    assert neighbour.state is EpisodeState.DOWNLOADING, "its own file is not in yet"
    assert torrent.completed_at is None
    assert wired.qbit.stopped == [], "the pack is still fetching episode 8"

    _arrived(wired, 7)
    await _poll(db_session, wired)

    assert neighbour.state is EpisodeState.MATCHING
    assert torrent.completed_at is not None
    assert wired.qbit.stopped == [BATCH_HASH]
    assert len(await queued(db_session, MATCH_FILE)) == 2


async def test_an_in_flight_batch_asks_for_its_file_list_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _episode, _torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962202, email="poll-batch3@arc.test"
    )

    await _poll(db_session, wired)

    assert _files_calls(wired) == 1, "one extra request per in-flight pack, and one only"


async def test_a_settled_batch_asks_for_no_file_list_at_all(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pack sitting there waiting to serve the next episode costs nothing.

    Every wanted file complete **and** the client reporting it stopped is the
    whole condition, and it is what keeps a shelf of finished packs from turning
    the sixty-second poll into one request per pack per minute.
    """
    wired, episode, _torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962203, email="poll-batch4@arc.test"
    )
    _member(tmp_path, 7)
    _arrived(wired, 6)
    await _poll(db_session, wired)
    assert episode.state is EpisodeState.MATCHING
    wired.qbit.calls.clear()

    await _poll(db_session, wired)

    assert _files_calls(wired) == 0
    assert [call for call in wired.qbit.calls if call.endswith("/torrents/info")] == [
        "/api/v2/torrents/info"
    ], "the one listing the poll was making anyway"


def _dead_swarm(wired: Wired) -> None:
    """Make the client report the pack's swarm as empty after seven hours."""
    wired.qbit.torrents[0] |= {
        "state": "stalledDL",
        "progress": 0.5,
        "time_active": 7 * HOUR,
        "num_complete": 0,
        "num_incomplete": 0,
    }


async def test_a_stalled_batch_that_has_fetched_nothing_goes_with_its_files(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordinary case, and the single path's ending exactly: nothing is lost.

    The pack has handed the library nothing, so the partial bytes of a release
    the ranker is now barred from choosing again are worth nothing to anybody.
    """
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962207, email="poll-batch8@arc.test"
    )
    _dead_swarm(wired)

    await _poll(db_session, wired)

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == "no seeders after 6 hours"
    assert torrent.qbit_state == QBIT_STALLED
    assert wired.qbit.deleted == [{"hashes": BATCH_HASH, "deleteFiles": "true"}]
    assert not next(
        row for row in await _torrent_files(db_session, torrent.id) if row.file_index == 6
    ).wanted


async def test_a_stalled_batch_keeps_the_file_the_library_already_has(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pack is not a single: one episode can be in while another's swarm dies.

    Episode 7's file is handed off; episode 8's swarm is empty. Episode 8 takes
    FR-A6's ordinary retry with the stall sentence on it (FR-A7) and gives its
    claim back, and the pack is **stopped** rather than deleted with its files —
    ``deleteFiles=true`` would take episode 7's file out from under the library
    and leave a ``media_files`` row pointing at nothing. Those bytes are
    retention's to measure and delete per episode (FR-T1).
    """
    wired, episode, torrent = await _in_flight(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962204,
        email="poll-batch5@arc.test",
        also=(8,),
    )
    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 8)
    )
    assert neighbour is not None
    video = _member(tmp_path, 7)
    _arrived(wired, 6)
    await _poll(db_session, wired)
    assert episode.state is EpisodeState.MATCHING
    wired.qbit.stopped.clear()

    _dead_swarm(wired)
    await _poll(db_session, wired)

    assert neighbour.state is EpisodeState.UNAVAILABLE
    assert neighbour.unavailable_reason == "no seeders after 6 hours"
    assert episode.state is EpisodeState.MATCHING, "what arrived is left alone"
    assert video.exists(), "and so is the file the library is holding"
    assert torrent.qbit_state == QBIT_STALLED
    assert wired.qbit.deleted == [], "never with its files"
    assert wired.qbit.stopped == [BATCH_HASH]
    assert wired.qbit.torrents != [], "still in the client, holding episode 7"

    rows = await _torrent_files(db_session, torrent.id)
    kept = next(row for row in rows if row.file_index == 6)
    given_up = next(row for row in rows if row.file_index == 7)
    assert kept.wanted, "a file that arrived keeps its claim"
    assert not given_up.wanted, "and one that never will gives it back"

    # And the poll after that does not change its mind: a ``stalled`` row stays
    # in the query so an undelivered request is retried, and the retry is the
    # same request — stop, never delete.
    wired.qbit.deleted.clear()
    await _poll(db_session, wired)
    assert wired.qbit.deleted == []
    assert video.exists()


async def test_a_batch_gone_from_the_client_gives_up_the_same_way(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``missing``: the same ending one step further on, and nothing to delete."""
    wired, episode, torrent = await _in_flight(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962205,
        email="poll-batch6@arc.test",
        also=(8,),
    )
    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 8)
    )
    assert neighbour is not None
    _member(tmp_path, 7)
    _arrived(wired, 6)
    await _poll(db_session, wired)

    wired.qbit.torrents.clear()
    await _poll(db_session, wired)

    assert neighbour.state is EpisodeState.UNAVAILABLE
    assert neighbour.unavailable_reason == REMOVED_FROM_CLIENT
    assert episode.state is EpisodeState.MATCHING
    assert torrent.qbit_state == qbit_module.QBIT_MISSING
    assert wired.qbit.deleted == [], "there is nothing there to delete"
    rows = await _torrent_files(db_session, torrent.id)
    assert not next(row for row in rows if row.file_index == 7).wanted

    # And a second poll says nothing further: the episodes have moved on.
    await _poll(db_session, wired)
    assert neighbour.state is EpisodeState.UNAVAILABLE


async def test_a_selection_that_drifted_is_written_again_by_the_next_poll(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**The rows are the instruction, and the poll is the reconciler.**

    Supersedes "a file switched off in the Web UI is left off" (2026-09-18):
    the same warning is logged, and the pack is then handed back to
    ``qbit_reselect``, because the case that cannot be told apart from an
    operator's hand is a re-selection Arc itself lost — ``enqueue``
    deduplicates against *running* jobs, so a want that changes while one is
    mid-flight is dropped and the running job writes the old answer. Nothing is
    written to the client from inside the poll; the job is.
    """
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962206, email="poll-batch7@arc.test"
    )
    for row in wired.qbit.file_lists[BATCH_HASH]:
        if row["index"] == 6:
            row["priority"] = 0

    with caplog.at_level(logging.WARNING):
        await _poll(db_session, wired)

    assert any(
        record.getMessage() == "a batch's selection in the client disagrees with arc's rows"
        for record in caplog.records
    )
    assert wired.qbit.priorities == [], "the poll writes rows and queues a job, nothing else"
    claim = next(row for row in await _torrent_files(db_session, torrent.id) if row.file_index == 6)
    assert claim.wanted, "the row still says arc asked for it"
    assert claim.completed_at is None
    assert episode.state is EpisodeState.DOWNLOADING
    assert [job.payload["torrent_id"] for job in await queued(db_session, QBIT_RESELECT)] == [
        torrent.id
    ]

    # And the job puts it back exactly as the rows say.
    await _reselect(db_session, wired, torrent.id)
    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, [index for index in range(28) if index != 6]),
        (1, [6]),
    ]


async def test_a_selection_arc_dropped_while_a_reselect_was_running_is_repaired(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The bug the reconciler exists for, staged exactly as it happens.

    A ``qbit_reselect`` for this pack is **running**, so the dedupe key is taken
    and the cancel's own enqueue returns that row instead of making one — the
    decision is in the database and nothing is left in the queue to carry it
    out. The running job then finishes, having written priority 1 for the row
    that was just un-wanted, and nothing in the queue disagrees. The first poll
    after that is what notices and queues a fresh one; a poll *during* the run
    is deduplicated like everything else and simply says so in the log, which is
    why this is self-healing within a tick rather than instantly.
    """
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962207, email="poll-batch8@arc.test"
    )
    running = Job(
        type=QBIT_RESELECT,
        payload={"torrent_id": torrent.id, DEDUPE_FIELD: reselect_dedupe_key(torrent.id)},
        status=JobStatus.RUNNING,
        attempts=1,
    )
    db_session.add(running)
    await db_session.flush()
    await _drop_wants(db_session, episode.id)
    assert await cancel_if_unwanted(db_session, episode) is True
    # The cancel's enqueue found the running job and added nothing, so the
    # client still has file 6 selected and nothing is queued to change it.
    assert [job.id for job in await queued(db_session, QBIT_RESELECT)] == [running.id]
    assert wired.qbit.file_lists[BATCH_HASH][6]["priority"] == 1

    # The running job ends, still holding the answer from before the cancel.
    running.status = JobStatus.DONE
    await db_session.flush()

    await _poll(db_session, wired)

    fresh = [job for job in await queued(db_session, QBIT_RESELECT) if job.id != running.id]
    assert [job.payload["torrent_id"] for job in fresh] == [torrent.id]

    await _reselect(db_session, wired, torrent.id)
    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, list(range(28)))
    ], "every file off: nobody wants anything in this pack any more"


# --- qbit_reselect ----------------------------------------------------------


async def _reselect(session: AsyncSession, wired: Wired, torrent_id: int) -> None:
    await qbit_reselect(
        context(session, wired.settings, {"torrent_id": torrent_id}, job_type=QBIT_RESELECT)
    )


async def _flip(session: AsyncSession, torrent_id: int, *, on: int | None, off: int) -> None:
    """Un-want file ``off`` and want file ``on``, as the four callers do."""
    for row in await _torrent_files(session, torrent_id):
        if row.file_index == off:
            row.wanted = False
        elif on is not None and row.file_index == on:
            row.wanted = True
    await session.flush()


async def test_the_reselect_writes_the_priorities_the_rows_say(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Off before on, every index named, and the rows are the only instruction."""
    wired, _episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962210, email="reselect1@arc.test"
    )
    await _flip(db_session, torrent.id, on=7, off=6)

    await _reselect(db_session, wired, torrent.id)

    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, [index for index in range(28) if index != 7]),
        (1, [7]),
    ]
    assert wired.qbit.started == [BATCH_HASH], "something is wanted and not in yet: KEEP"
    assert wired.qbit.stopped == [] and wired.qbit.deleted == []
    rows = await _torrent_files(db_session, torrent.id)
    assert next(row for row in rows if row.file_index == 7).priority == 1
    assert next(row for row in rows if row.file_index == 6).priority == 0


async def test_the_reselect_stops_a_pack_the_show_may_still_want(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """STOP: nothing is wanted now, and the show is still on somebody's list."""
    wired, _episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962211, email="reselect2@arc.test"
    )
    await _flip(db_session, torrent.id, on=None, off=6)

    await _reselect(db_session, wired, torrent.id)

    assert wired.qbit.stopped == [BATCH_HASH]
    assert wired.qbit.started == [] and wired.qbit.deleted == []
    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, list(range(28)))
    ], "an empty selection makes no second request"
    assert (
        await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    ) is not None, "kept, so the next episode attaches for nothing"


async def test_the_reselect_deletes_a_pack_nothing_can_want_again(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DELETE: with its files, and the row goes because there is nothing wrong with it."""
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962212, email="reselect3@arc.test"
    )
    entry = await db_session.scalar(select(ListEntry).where(ListEntry.anime_id == episode.anime_id))
    assert entry is not None
    entry.status = ListStatus.COMPLETED
    await _flip(db_session, torrent.id, on=None, off=6)

    await _reselect(db_session, wired, torrent.id)

    assert wired.qbit.deleted == [{"hashes": BATCH_HASH, "deleteFiles": "true"}]
    assert (await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))) is None
    assert await _torrent_files(db_session, torrent.id) == [], "the rows cascade with it"

    # Idempotent: the second run has no row to read and asks the client nothing.
    wired.qbit.calls.clear()
    await _reselect(db_session, wired, torrent.id)
    assert wired.qbit.calls == []


async def test_a_file_list_that_disagrees_with_the_rows_changes_nothing(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``filePrio`` takes an index and nothing else, so a moved listing is a no-op."""
    wired, _episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962213, email="reselect4@arc.test"
    )
    await _flip(db_session, torrent.id, on=7, off=6)
    wired.qbit.file_lists[BATCH_HASH].pop()

    with caplog.at_level(logging.ERROR):
        await _reselect(db_session, wired, torrent.id)

    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert wired.qbit.priorities == []
    assert wired.qbit.started == [] and wired.qbit.stopped == [] and wired.qbit.deleted == []
    assert torrent.qbit_state == qbit_module.QBIT_MISSING, "not the pack arc recorded"


async def test_a_pack_whose_listing_moved_does_not_strand_its_episodes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The other half of the mismatch: somebody has to tell the episodes.

    Writing nothing is right — ``filePrio`` takes an index and the indices have
    moved — but a row left saying ``downloading`` would leave every episode
    waiting on this pack in ``downloading`` for ever, each holding the one live
    claim that stops it being fetched any other way. ``missing`` is what the
    poll reads to end that with the ordinary sentence (FR-A6, FR-A7).
    """
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962218, email="reselect9@arc.test"
    )
    wired.qbit.file_lists[BATCH_HASH].pop()
    await _reselect(db_session, wired, torrent.id)
    assert torrent.qbit_state == qbit_module.QBIT_MISSING

    # The next poll, by which time the pack really is gone from the client.
    wired.qbit.torrents.clear()
    await _poll(db_session, wired)

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == REMOVED_FROM_CLIENT
    claim = next(row for row in await _torrent_files(db_session, torrent.id) if row.file_index == 6)
    assert not claim.wanted, "and the claim is given back, so the next pack is not refused"


async def test_a_reselect_for_a_torrent_that_went_away_is_a_no_op(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962214, email="reselect5@arc.test"
    )

    await _reselect(db_session, wired, torrent.id + 9_000)

    assert wired.qbit.calls == [], "not even a login"


async def test_a_reselect_of_a_decided_batch_touches_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pack that stalled is not a pack to write a selection to."""
    wired, _episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962215, email="reselect6@arc.test"
    )
    torrent.qbit_state = QBIT_STALLED
    await db_session.flush()

    await _reselect(db_session, wired, torrent.id)

    assert wired.qbit.calls == []


async def test_the_reselect_is_idempotent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rows are the instruction, so the second run writes the same thing."""
    wired, _episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962216, email="reselect7@arc.test"
    )
    await _flip(db_session, torrent.id, on=7, off=6)

    await _reselect(db_session, wired, torrent.id)
    first = [(call["priority"], call["indices"]) for call in wired.qbit.priorities]
    await _reselect(db_session, wired, torrent.id)

    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == first + first
    assert wired.qbit.started == [BATCH_HASH, BATCH_HASH]
    rows = await _torrent_files(db_session, torrent.id)
    assert [row.file_index for row in rows if row.wanted] == [7]


async def test_the_reselect_stops_a_pack_whose_files_are_all_in(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """KEEP with nothing left to fetch is a stop: starting it would only seed."""
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962217, email="reselect8@arc.test"
    )
    _member(tmp_path, 7)
    _arrived(wired, 6)
    await _poll(db_session, wired)
    assert episode.state is EpisodeState.MATCHING
    wired.qbit.stopped.clear()

    await _reselect(db_session, wired, torrent.id)

    assert wired.qbit.started == []
    assert wired.qbit.stopped == [BATCH_HASH]
    assert wired.qbit.deleted == []


# --- Cancel and reject on a shared pack (FR-A11, T6) ------------------------
#
# One idea, three callers. A batch-backed episode must never take the path a
# single takes: ``qbit_cancel`` deletes a hash **with its files** and a pack's
# files belong to several episodes, so a cancel that marked the torrent
# ``cancelled`` would delete somebody else's half-finished download. Everything
# below asserts the same two halves — **only that episode's row changes**, and
# **the torrent is left in the client** — for a want withdrawn and for a file a
# person rejected in review.


async def _wants_of(session: AsyncSession, torrent_id: int) -> list[int]:
    """The file indices this pack is currently fetching."""
    rows = await _torrent_files(session, torrent_id)
    return [row.file_index for row in rows if row.wanted]


async def _drop_wants(session: AsyncSession, episode_id: int) -> None:
    await session.execute(delete(Want).where(Want.episode_id == episode_id))
    await session.flush()


async def test_cancelling_one_episode_of_a_pack_un_wants_only_its_file(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole of T6 in one run: one row off, the pack untouched.

    Two episodes are riding on this pack. Episode 7's last want goes away, and
    what must happen is that *its* file stops being fetched and nothing else
    changes — not the torrent's state, not episode 8's file, and above all not
    the torrent's presence in the client.
    """
    wired, episode, torrent = await _in_flight(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962230,
        email="batchcancel1@arc.test",
        also=(8,),
    )
    assert await _wants_of(db_session, torrent.id) == [6, 7]
    await _drop_wants(db_session, episode.id)

    assert await cancel_if_unwanted(db_session, episode) is True

    assert episode.state is EpisodeState.NOT_WANTED
    assert await _wants_of(db_session, torrent.id) == [7], "episode 8 is still being fetched"
    assert torrent.qbit_state not in {QBIT_CANCELLED, *DECIDED_STATES}
    assert torrent.qbit_state == "added"
    assert wired.qbit.deleted == [], "a shared pack is never deleted by a cancel"
    assert wired.qbit.torrents != [], "and it is still in the client"

    # The row keeps its episode, which is what makes changing your mind free.
    row = next(row for row in await _torrent_files(db_session, torrent.id) if row.file_index == 6)
    assert row.episode_id == episode.id and row.wanted is False

    jobs = await queued(db_session, QBIT_RESELECT)
    assert [job.payload["torrent_id"] for job in jobs] == [torrent.id]
    assert jobs[0].priority == QBIT_RESELECT_PRIORITY
    assert jobs[0].payload[DEDUPE_FIELD] == reselect_dedupe_key(torrent.id)
    assert await queued(db_session, QBIT_CANCEL) == [], "the reconciler queues that, not this"


async def test_two_episodes_of_one_pack_cancelled_at_once_queue_one_reselect(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dedupe key is per **torrent**: one selection to write, one job."""
    wired, episode, torrent = await _in_flight(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962231,
        email="batchcancel2@arc.test",
        also=(8,),
    )
    neighbour = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 8)
    )
    assert neighbour is not None
    await _drop_wants(db_session, episode.id)
    await _drop_wants(db_session, neighbour.id)

    assert await cancel_if_unwanted(db_session, episode) is True
    assert await cancel_if_unwanted(db_session, neighbour) is True

    assert await _wants_of(db_session, torrent.id) == []
    assert len(await queued(db_session, QBIT_RESELECT)) == 1
    assert wired.qbit.deleted == []


async def test_the_single_cancel_path_is_untouched_by_the_batch_branch(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A single still marks its row ``cancelled`` and queues no re-selection."""
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=962232, email="batchcancel3@arc.test"
    )
    await _drop_wants(db_session, episode.id)

    assert await cancel_if_unwanted(db_session, episode) is True

    assert torrent.qbit_state == QBIT_CANCELLED
    assert episode.state is EpisodeState.NOT_WANTED
    assert await queued(db_session, QBIT_RESELECT) == []
    assert wired.qbit is not None


async def test_the_cancel_handler_never_selects_a_batch_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``qbit_cancel`` is unchanged and cannot reach a pack (the plan's §2).

    It selects by ``episode_id`` **and** by ``qbit_state == cancelled``, and a
    batch row matches neither — its episode is null and the branch above never
    writes that state. So the job queued for a batch-backed episode by any
    caller that queues one anyway finds nothing, and the pack is left alone.
    """
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962233, email="batchcancel4@arc.test"
    )
    await _drop_wants(db_session, episode.id)
    assert await cancel_if_unwanted(db_session, episode) is True

    await qbit_cancel(
        context(db_session, wired.settings, {"episode_id": episode.id}, job_type=QBIT_CANCEL)
    )

    assert wired.qbit.deleted == []
    assert wired.qbit.torrents != []
    kept = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert kept is not None and kept.id == torrent.id


async def test_attaching_to_a_pack_forgets_what_it_knew_about_the_old_copy(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Turning a file back on is asking for it again, so the row starts over.

    The row carries ``completed_at`` and ``progress`` from the last time this
    file arrived, and by the time anybody re-wants the episode those bytes may
    be gone — retention took them, or somebody deleted them by hand. Left
    standing, the stamp is read by three different things as "already here":
    ``qbit_reselect`` stops the pack instead of starting it, the poll treats it
    as settled and never asks for a file list again, and the hand-off looks for
    a path that is not there once a minute for ever — with the episode stuck in
    ``downloaded`` holding the one live claim that stops it being fetched any
    other way.
    """
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962238, email="batchclaim1@arc.test"
    )
    media = await _delivered(db_session, wired, tmp_path, episode)
    claim = next(row for row in await _torrent_files(db_session, torrent.id) if row.file_index == 6)
    assert claim.completed_at is not None and claim.progress == pytest.approx(1.0)

    # The bytes go, by a hand that is not retention's — so nothing has tidied
    # the row up — and the episode comes back round to being wanted.
    Path(media.path).unlink()
    claim.wanted = False
    episode.state = EpisodeState.WANTED
    await db_session.flush()
    wired.qbit.started.clear()
    wired.qbit.stopped.clear()

    attached = await batch_module.claim_existing(db_session, episode)

    assert attached is not None
    assert claim.completed_at is None and claim.progress is None
    assert episode.state is EpisodeState.DOWNLOADING

    await _reselect(db_session, wired, torrent.id)

    assert wired.qbit.started == [BATCH_HASH], "there is something to fetch, so: start"
    assert wired.qbit.stopped == []


async def test_a_cancelled_episode_is_served_by_the_same_pack_with_no_search(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Changing your mind is free, which is why ``episode_id`` is kept.

    ``claim_existing`` runs before Nyaa is asked anything at all, so the second
    search for this episode is zero feed requests and zero new bytes.
    """
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962234, email="batchcancel5@arc.test"
    )
    user_id = await a_user_id(db_session)
    await _drop_wants(db_session, episode.id)
    assert await cancel_if_unwanted(db_session, episode) is True
    db_session.add(Want(user_id=user_id, episode_id=episode.id))
    episode.state = EpisodeState.WANTED
    await db_session.flush()
    wired.nyaa.queries.clear()
    wired.nyaa.fetched.clear()
    wired.qbit.uploaded.clear()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries == [], "served from the pack arc already has"
    assert wired.nyaa.fetched == []
    assert wired.qbit.uploaded == [], "and no second copy of a 14 GB pack"
    assert episode.state is EpisodeState.DOWNLOADING
    assert await _wants_of(db_session, torrent.id) == [6]


# --- reject_download on a batch member --------------------------------------


async def _delivered(
    session: AsyncSession,
    wired: Wired,
    tmp_path: Path,
    episode: Episode,
) -> MediaFile:
    """Let episode 7's file land and come back with the row the poll indexed."""
    video = _member(tmp_path, 7)
    _arrived(wired, 6)
    await _poll(session, wired)
    assert episode.state is EpisodeState.MATCHING
    media = await session.scalar(select(MediaFile).where(MediaFile.path == str(video.resolve())))
    assert media is not None
    return media


def test_the_batch_layout_yields_no_episode_id_from_its_path(tmp_path: Path) -> None:
    """The fail-closed half of the design, asserted rather than assumed.

    ``downloads/batch/<hash>/`` is named for the *pack* precisely so that
    ``int(relative.parts[0])`` raises and every id-from-path inference answers
    ``None``: a pack holding twenty-eight episodes under a directory named for
    one of them is the one shape that could attribute another episode's file to
    the wrong episode.
    """
    downloads = tmp_path / "downloads"
    inside = downloads / "batch" / BATCH_HASH / "Sousou no Frieren" / "07.mkv"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")

    assert episode_id_of(str(inside), downloads_dir=downloads) is None
    assert batch_member_of(str(inside), downloads_dir=downloads) == (
        BATCH_HASH,
        "Sousou no Frieren/07.mkv",
    )
    # And the single layout is still read exactly as it was.
    single = downloads / "42" / "episode.mkv"
    single.parent.mkdir(parents=True)
    single.write_bytes(b"x")
    assert episode_id_of(str(single), downloads_dir=downloads) == 42
    assert batch_member_of(str(single), downloads_dir=downloads) is None


async def test_rejecting_a_batch_member_un_wants_that_row_only(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The episode ends exactly where a single's would, and the pack survives.

    ``unavailable`` with :data:`WRONG_FILE` is the single path's own ending. What
    must *not* happen is the torrent being marked ``rejected``: that would strand
    the files of every other episode in the pack, including the one still
    downloading here.
    """
    wired, episode, torrent = await _in_flight(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962235,
        email="batchreject1@arc.test",
        also=(8,),
    )
    media = await _delivered(db_session, wired, tmp_path, episode)

    moved = await reject_download(db_session, media, downloads_dir=wired.settings.downloads_dir)

    assert moved is not None and moved.id == episode.id
    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == WRONG_FILE
    assert torrent.qbit_state != QBIT_REJECTED, "a shared pack is never rejected"
    assert wired.qbit.deleted == []
    assert await _wants_of(db_session, torrent.id) == [7], "episode 8 is untouched"

    rejected = next(
        row for row in await _torrent_files(db_session, torrent.id) if row.file_index == 6
    )
    # Cleared, unlike cancel and retention: the file has been looked at and it
    # is not this episode, so the retry must not be served it again.
    assert rejected.episode_id is None and rejected.wanted is False
    assert [job.payload["torrent_id"] for job in await queued(db_session, QBIT_RESELECT)] == [
        torrent.id
    ]


async def test_a_rejected_batch_member_is_not_offered_back_to_its_episode(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The point of clearing the row: FR-A6's retry must not loop on one file."""
    wired, episode, torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962236, email="batchreject2@arc.test"
    )
    media = await _delivered(db_session, wired, tmp_path, episode)
    await reject_download(db_session, media, downloads_dir=wired.settings.downloads_dir)

    assert await batch_module.claim_existing(db_session, episode) is None
    assert await _wants_of(db_session, torrent.id) == []


async def test_a_batch_file_nothing_downloaded_is_not_rejected_into_an_episode(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path under ``batch/`` with no row behind it says nothing about anything."""
    wired, episode, _torrent = await _in_flight(
        db_session, monkeypatch, tmp_path, anilist_id=962237, email="batchreject3@arc.test"
    )
    stray = tmp_path / "downloads" / "batch" / ("a" * 40) / "someone else.mkv"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"x")
    media = MediaFile(path=str(stray.resolve()), size=1)
    db_session.add(media)
    await db_session.flush()

    assert (
        await reject_download(db_session, media, downloads_dir=wired.settings.downloads_dir) is None
    )
    assert episode.state is EpisodeState.DOWNLOADING
