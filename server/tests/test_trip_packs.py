"""Trip packs (M19 T5; FR-A12 with FR-A4/FR-A11's amendment, owner 2026-10-05).

A trip on a **finished** show with at least ``TRIP_BATCH_MIN`` trip episodes
still wanted asks the batch forms first and takes one pack whose file list
holds enough of them, instead of a dozen singles. Everything else — an airing
show, a non-trip search, the kill switch off, too few trip episodes — searches
exactly as before. Nyaa and qBittorrent are mocked as in
``test_acquisition_jobs``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    Torrent,
    TorrentFile,
    TorrentKind,
    Want,
)
from arc.services.acquisition import jobs as acquisition_jobs
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition import qbit as qbit_module
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.jobs import (
    TRIP_PACK_LOCK_KEY,
    poll_qbit,
    qbit_reselect,
    qbit_top,
    search_release,
    trip_pack_need,
)
from arc.services.acquisition.names import QBIT_RESELECT, QBIT_TOP
from arc.services.acquisition.nyaa import (
    MAX_REQUESTS,
    NyaaClient,
    PackPreference,
    Ranked,
    batch_queries,
    group_queries,
    queries,
    search_for_episode,
)
from arc.services.acquisition.qbit import FILE_ON, QBIT_UNREADABLE
from arc.services.acquisition.rules import BYTES_PER_GB, Rules
from arc.services.acquisition.wants import compute_wants
from arc.services.trips.create import create_trip
from arc.services.trips.names import TRIP_BATCH_MIN
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
    no_sleep,
    set_setting,
)
from tests.test_acquisition_jobs import (
    BATCH_HASH,
    BATCH_TITLE,
    ONE_PACK,
    SECOND_HASH,
    SECOND_TITLE,
    Wired,
    _batch_pool,
    _finished,
    _pack_names,
    context,
)
from tests.test_nyaa import (
    KIMETSU_AIRING,
    KIMETSU_BATCH,
    KIMETSU_QUERIES,
    KIMETSU_S1,
    MUSHOKU_S3_FINISHED,
    feed_of_seeded,
)


@pytest.fixture(autouse=True)
def _forget_packs() -> Iterator[None]:
    """The pack memo is process-wide; every test starts without one."""
    acquisition_jobs._PACK_MEMO.clear()
    yield
    acquisition_jobs._PACK_MEMO.clear()


# --- The rule ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("wanted", "attachable", "need"),
    [
        (TRIP_BATCH_MIN - 1, TRIP_BATCH_MIN - 1, None),
        (TRIP_BATCH_MIN, TRIP_BATCH_MIN, TRIP_BATCH_MIN),
        (12, 12, TRIP_BATCH_MIN),
        (12, 2, 2),
        (12, 1, None),
        (12, 0, None),
    ],
)
def test_the_pack_need(wanted: int, attachable: int, need: int | None) -> None:
    """``min(TRIP_BATCH_MIN, attachable)`` once enough are wanted; one file is a single."""
    assert TRIP_BATCH_MIN == 4
    assert trip_pack_need(wanted, attachable) == need


# --- The search seam (no database) --------------------------------------------


class Taker:
    """A ``PackTaker`` that records what it was offered and answers ``result``."""

    def __init__(self, result: bool) -> None:
        self.result = result
        self.offered: list[list[str]] = []

    async def __call__(self, packs: list[Ranked]) -> bool:
        self.offered.append([entry.item.title for entry in packs])
        return self.result


async def test_a_preferred_pack_is_asked_for_first_and_ends_the_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub(default=feed_of_seeded((KIMETSU_BATCH, 30)))
    taker = Taker(True)

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(
            client,
            KIMETSU_S1,
            10,
            Rules(),
            prefer=PackPreference(take=taker, numbers=tuple(range(1, 13)), need=4),
        )

    # The pack forms first, then the title forms once (owner incident
    # 2026-10-06: a pack is measured against the best single before it is
    # offered), and nothing after the take.
    packs = batch_queries(KIMETSU_S1)
    assert stub.queries[: len(packs)] == packs
    assert stub.queries[len(packs) :] == queries(KIMETSU_S1, 10)
    assert taker.offered == [[KIMETSU_BATCH]]
    assert found.pack_taken
    assert found.requests == found.forms == len(stub.queries)
    assert found.ranked == [] and found.batches == []


async def test_a_pack_that_names_too_few_is_never_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name gate: 2 of 12 against a need of 4. A pack naming no range is offered."""
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    narrow = "[Erai-raws] Kimetsu no Yaiba - 09 ~ 10 [1080p][BATCH]"
    unnamed = "[Judas] Kimetsu no Yaiba [BD 1080p][BATCH]"
    stub = NyaaStub(
        {form: feed_of_seeded((narrow, 90), (unnamed, 5)) for form in batch_queries(KIMETSU_S1)}
    )
    taker = Taker(False)

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(
            client,
            KIMETSU_S1,
            10,
            Rules(),
            prefer=PackPreference(take=taker, numbers=tuple(range(1, 13)), need=4),
        )

    assert taker.offered == [[unnamed]]
    assert not found.pack_taken


async def test_a_declined_pack_falls_through_inside_one_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three pack forms first, then the ordinary search, twenty requests in all.

    The pack forms are not asked a second time at the end, and the narrowing
    no longer reserves room for them, so the budget is spent exactly once.
    """
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub(default=feed_of_seeded(("[Nobody] Something Else [1080p]", 9)))
    rules = Rules(preferred_groups=("Judas", "Anime Time", "Yameii"))
    taker = Taker(False)

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(
            client,
            MUSHOKU_S3_FINISHED,
            11,
            rules,
            prefer=PackPreference(take=taker, numbers=tuple(range(1, 13)), need=4),
        )

    packs = batch_queries(MUSHOKU_S3_FINISHED)
    titles = queries(MUSHOKU_S3_FINISHED, 11)
    assert stub.queries[:3] == packs
    assert stub.queries[3 : 3 + len(titles)] == titles
    assert len(stub.queries) == len(set(stub.queries)), "not one query twice"
    assert found.requests == len(stub.queries) == MAX_REQUESTS
    assert len(stub.queries) - len(packs) - len(titles) == MAX_REQUESTS - 3 - 10
    assert len(group_queries(MUSHOKU_S3_FINISHED, 11, rules)) > MAX_REQUESTS - 13
    assert taker.offered == [], "nothing to offer, nothing taken"
    assert found.forms == len(titles) and not found.pack_taken


async def test_an_airing_show_ignores_the_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub(default=feed_of_seeded((KIMETSU_BATCH, 300)))
    taker = Taker(True)

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(
            client,
            KIMETSU_AIRING,
            10,
            Rules(),
            prefer=PackPreference(take=taker, numbers=tuple(range(1, 13)), need=4),
        )

    assert stub.queries == KIMETSU_QUERIES
    assert taker.offered == [] and not found.pack_taken and found.batches == []


# --- search_release end to end -------------------------------------------------

pg = pytest.mark.pg


async def _trip(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
    numbers: range = range(1, 13),
    pool: str = ONE_PACK,
    status: str = "FINISHED",
    trip: bool = True,
) -> tuple[Wired, dict[int, Episode], Anime]:
    """A show whose ``numbers`` are wanted only for a trip, every other want gone."""
    wired, seed = await _finished(
        session, monkeypatch, tmp_path, anilist_id=anilist_id, email=email, pool=pool, status=status
    )
    want = await session.scalar(select(Want).where(Want.episode_id == seed.id))
    assert want is not None
    user_id = want.user_id
    await session.execute(delete(Want).where(Want.episode_id == seed.id))
    seed.state = EpisodeState.NOT_WANTED
    rows = (await session.scalars(select(Episode).where(Episode.anime_id == seed.anime_id))).all()
    episodes = {row.number: row for row in rows}
    for number in numbers:
        episodes[number].state = EpisodeState.WANTED
        session.add(Want(user_id=user_id, episode_id=episodes[number].id, trip=trip))
    await session.flush()
    anime = await session.get(Anime, seed.anime_id)
    assert anime is not None
    return wired, episodes, anime


def _preferred(anime: Anime, number: int) -> list[str]:
    """What a trip search asks when a pack is worth weighing: pack forms, then title forms.

    The title forms come before the offer since the owner incident of
    2026-10-06, so the pack can be measured against the best single.
    """
    return [*batch_queries(anime), *queries(anime, number)]


async def _search(session: AsyncSession, wired: Wired, episode: Episode, job_id: int = 1) -> None:
    await search_release(
        context(session, wired.settings, {"episode_id": episode.id}, job_id=job_id)
    )


async def _files(session: AsyncSession, torrent_id: int) -> list[TorrentFile]:
    rows = await session.scalars(
        select(TorrentFile)
        .where(TorrentFile.torrent_id == torrent_id)
        .order_by(TorrentFile.file_index)
    )
    return list(rows.all())


@pg
async def test_a_twelve_episode_trip_takes_one_pack_and_the_rest_attach_for_free(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episodes, anime = await _trip(
        db_session, monkeypatch, tmp_path, anilist_id=962500, email="pack1@arc.test"
    )
    # One sibling has a search of its own in flight: it is not taken along,
    # and its search attaches to the pack later through ``claim_existing``.
    episodes[5].state = EpisodeState.SEARCHING
    await db_session.flush()
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))

    await _search(db_session, wired, episodes[1])

    assert wired.nyaa.queries == _preferred(anime, 1), "pack forms, then title forms once"
    assert episodes[1].last_search_forms == len(_preferred(anime, 1)), "truthfully"
    torrents = (await db_session.scalars(select(Torrent))).all()
    assert len(torrents) == 1
    pack = torrents[0]
    assert pack.kind is TorrentKind.BATCH and pack.info_hash == BATCH_HASH
    assert wired.qbit.bottomed == [BATCH_HASH], "a trip-only pack goes to the back"
    wanted = [row for row in await _files(db_session, pack.id) if row.wanted]
    assert sorted(row.file_index + 1 for row in wanted) == [n for n in range(1, 13) if n != 5]
    for number in range(1, 13):
        if number != 5:
            assert episodes[number].state is EpisodeState.DOWNLOADING

    # Every sibling's own search costs Nyaa nothing at all.
    wired.nyaa.queries.clear()
    wired.nyaa.fetched.clear()
    for job_id, number in enumerate(range(2, 13), start=2):
        await _search(db_session, wired, episodes[number], job_id=job_id)
    assert wired.nyaa.queries == [] and wired.nyaa.fetched == []
    assert episodes[5].state is EpisodeState.DOWNLOADING
    rows = await _files(db_session, pack.id)
    assert sum(1 for row in rows if row.wanted) == 12
    assert {row.episode_id for row in rows if row.wanted} == {
        episodes[number].id for number in range(1, 13)
    }
    assert len(wired.qbit.uploaded) == 1, "still one pack"


@pg
async def test_a_pack_naming_too_few_is_skipped_and_singles_are_taken(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    single = "[SubsPlease] Sousou no Frieren - 01 (1080p) [AB12CD34].mkv"
    wired, episodes, anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962501,
        email="pack2@arc.test",
        pool=_batch_pool((single, "a" * 40, 80)),
    )
    narrow = "[Erai-raws] Sousou no Frieren - 01 ~ 02 [1080p][BATCH]"
    for form in batch_queries(anime):
        wired.nyaa.answers[form] = _batch_pool((narrow, "e" * 40, 200))

    await _search(db_session, wired, episodes[1])

    assert wired.nyaa.queries[:3] == batch_queries(anime)
    assert wired.nyaa.fetched == [], "a pack named 01 ~ 02 is not even fetched"
    assert len(wired.nyaa.queries) <= MAX_REQUESTS
    assert len(wired.nyaa.queries) == len(set(wired.nyaa.queries))
    torrent = await db_session.scalar(select(Torrent))
    assert torrent is not None and torrent.kind is TorrentKind.SINGLE
    assert torrent.info_hash == "a" * 40
    assert episodes[1].state is EpisodeState.DOWNLOADING
    assert episodes[2].state is EpisodeState.WANTED, "nothing taken along"


@pg
async def test_a_pack_whose_files_hold_too_few_is_removed_without_a_tombstone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The plan gate: a pack naming no range is read, holds 2 of 12, and goes."""
    single = "[SubsPlease] Sousou no Frieren - 01 (1080p) [AB12CD34].mkv"
    wired, episodes, anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962502,
        email="pack3@arc.test",
        pool=_batch_pool((single, "a" * 40, 80)),
    )
    for form in batch_queries(anime):
        wired.nyaa.answers[form] = _batch_pool((SECOND_TITLE, SECOND_HASH, 50))
    wired.qbit.add_files(SECOND_HASH, _pack_names(1, 2, group="Judas"))

    await _search(db_session, wired, episodes[1])

    assert wired.qbit.deleted == [{"hashes": SECOND_HASH, "deleteFiles": "true"}]
    assert wired.qbit.started == [] or SECOND_HASH not in wired.qbit.started
    assert (
        await db_session.scalar(select(Torrent).where(Torrent.info_hash == SECOND_HASH))
    ) is None, "not unreadable, merely not worth preferring"
    torrent = await db_session.scalar(select(Torrent))
    assert torrent is not None and torrent.info_hash == "a" * 40
    assert episodes[1].state is EpisodeState.DOWNLOADING


@pg
@pytest.mark.parametrize(
    ("count", "preferred"), [(TRIP_BATCH_MIN - 1, False), (TRIP_BATCH_MIN, True)]
)
async def test_the_trip_batch_min_boundary(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    count: int,
    preferred: bool,
) -> None:
    wired, episodes, anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962510 + count,
        email=f"pack-min{count}@arc.test",
        numbers=range(1, count + 1),
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))

    await _search(db_session, wired, episodes[1])

    if preferred:
        assert wired.nyaa.queries == _preferred(anime, 1)
    else:
        # The ordinary order: every title form first, the pack only as the
        # fallback once no single exists — which this pool also ends in.
        assert wired.nyaa.queries[0] == queries(anime, 1)[0]
        assert wired.nyaa.queries[-3:] == batch_queries(anime)
    assert all(episodes[n].state is EpisodeState.DOWNLOADING for n in range(1, count + 1))


@pg
async def test_a_named_pack_covering_exactly_the_minimum_is_taken(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    four = "[Erai-raws] Sousou no Frieren - 01 ~ 04 [1080p][BATCH]"
    wired, episodes, anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962520,
        email="pack-exact@arc.test",
        pool=_batch_pool((four, "f" * 40, 40)),
    )
    wired.qbit.add_files("f" * 40, _pack_names(1, 2, 3, 4))

    await _search(db_session, wired, episodes[1])

    assert wired.nyaa.queries == _preferred(anime, 1)
    assert [episodes[n].state for n in range(1, 6)] == [
        EpisodeState.DOWNLOADING,
        EpisodeState.DOWNLOADING,
        EpisodeState.DOWNLOADING,
        EpisodeState.DOWNLOADING,
        EpisodeState.WANTED,
    ]


@pg
async def test_the_kill_switch_off_searches_as_before(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episodes, anime = await _trip(
        db_session, monkeypatch, tmp_path, anilist_id=962530, email="pack-off@arc.test"
    )
    await set_setting(db_session, acquisition_rules.BATCH_FALLBACK_KEY, False)

    await _search(db_session, wired, episodes[1])

    assert wired.nyaa.queries[0] == queries(anime, 1)[0]
    assert wired.qbit.uploaded == [] and wired.nyaa.fetched == []
    assert episodes[1].state is EpisodeState.SEARCHING


@pg
async def test_an_airing_show_searches_as_before(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episodes, anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962531,
        email="pack-airing@arc.test",
        status="RELEASING",
    )

    await _search(db_session, wired, episodes[1])

    assert wired.nyaa.queries == queries(anime, 1), "title forms only"
    assert wired.qbit.uploaded == []


@pg
async def test_a_non_trip_search_is_unchanged_and_its_pack_keeps_its_place(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episodes, anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962532,
        email="pack-normal@arc.test",
        trip=False,
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))

    await _search(db_session, wired, episodes[1])

    assert wired.nyaa.queries[0] == queries(anime, 1)[0]
    assert wired.nyaa.queries[-3:] == batch_queries(anime), "the pack only as the fallback"
    assert episodes[1].state is EpisodeState.DOWNLOADING
    assert wired.qbit.bottomed == [], "a streaming pack is not sent to the back"


# --- The fix loop (review of T5) -------------------------------------------------


def _singles(*numbers: int) -> str:
    """A feed holding one live single per number, each with its own hash."""
    return _batch_pool(
        *(
            (
                f"[SubsPlease] Sousou no Frieren - {number:02d} (1080p) [AB12CD{number:02d}].mkv",
                f"{number:02d}" * 20,
                80,
            )
            for number in numbers
        )
    )


@pg
async def test_a_declined_pack_the_fallback_takes_is_fetched_and_added_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S1 (the reviewer's probe): the gate holds the pack and the fallback takes it."""
    wired, episodes, anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962600,
        email="probe@arc.test",
        pool=_batch_pool(),
    )
    for form in batch_queries(anime):
        wired.nyaa.answers[form] = _batch_pool((SECOND_TITLE, SECOND_HASH, 50))
    wired.qbit.add_files(SECOND_HASH, _pack_names(1, 2, group="Judas"))

    await _search(db_session, wired, episodes[1])

    assert len(wired.nyaa.fetched) == 1, "one .torrent fetch"
    assert len(wired.qbit.uploaded) == 1, "one add"
    assert wired.qbit.deleted == [], "never deleted and re-added"
    assert wired.qbit.started == [SECOND_HASH]
    pack = await db_session.scalar(select(Torrent).where(Torrent.info_hash == SECOND_HASH))
    assert pack is not None and pack.qbit_state != QBIT_UNREADABLE
    assert episodes[1].state is EpisodeState.DOWNLOADING
    assert episodes[2].state is EpisodeState.DOWNLOADING


@pg
async def test_a_no_range_pack_without_this_episode_leaves_no_tombstone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S2: its name never claimed episode 1, and it is right for episodes 2–12."""
    wired, episodes, anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962601,
        email="norange@arc.test",
        pool=_singles(*range(1, 13)),
    )
    for form in batch_queries(anime):
        wired.nyaa.answers[form] = _batch_pool((SECOND_TITLE, SECOND_HASH, 50))
    wired.qbit.add_files(SECOND_HASH, _pack_names(*range(2, 13), group="Judas"))

    await _search(db_session, wired, episodes[1])

    assert (
        await db_session.scalar(select(Torrent).where(Torrent.info_hash == SECOND_HASH))
    ) is None, "no tombstone barring the pack for the show"
    assert episodes[1].state is EpisodeState.DOWNLOADING, "the single for episode 1"

    # Episode 2's search takes the very pack.
    await _search(db_session, wired, episodes[2], job_id=2)
    pack = await db_session.scalar(select(Torrent).where(Torrent.info_hash == SECOND_HASH))
    assert pack is not None and pack.kind is TorrentKind.BATCH
    assert all(episodes[n].state is EpisodeState.DOWNLOADING for n in range(2, 13))


@pg
async def test_a_pack_several_siblings_decline_is_added_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S2: what a pack holds is remembered for the show's other trip searches."""
    wired, episodes, anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962602,
        email="memo@arc.test",
        pool=_singles(*range(1, 13)),
    )
    for form in batch_queries(anime):
        wired.nyaa.answers[form] = _batch_pool((SECOND_TITLE, SECOND_HASH, 50))
    wired.qbit.add_files(SECOND_HASH, _pack_names(1, 2, group="Judas"))

    for job_id, number in enumerate(range(1, 6), start=1):
        await _search(db_session, wired, episodes[number], job_id=job_id)

    assert len(wired.qbit.uploaded) == 1, "read once, remembered after"
    assert len(wired.nyaa.fetched) == 1
    assert all(episodes[n].state is EpisodeState.DOWNLOADING for n in range(1, 6))
    singles = (
        await db_session.scalars(select(Torrent).where(Torrent.kind == TorrentKind.SINGLE))
    ).all()
    assert len(singles) == 5


@pg
async def test_a_free_rider_another_search_took_meanwhile_is_not_claimed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S3: the targets are re-read under lock before the selection is written."""
    wired, episodes, anime = await _trip(
        db_session, monkeypatch, tmp_path, anilist_id=962603, email="stale@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))
    original = qbit_module.QbitClient.files
    stale = episodes[3].id

    async def files(self: qbit_module.QbitClient, info_hash: str) -> list[qbit_module.FileInfo]:
        # Another search's ``searching`` lands while this one reads the pack.
        await db_session.execute(
            update(Episode)
            .where(Episode.id == stale)
            .values(state=EpisodeState.SEARCHING)
            .execution_options(synchronize_session=False)
        )
        return await original(self, info_hash)

    monkeypatch.setattr(qbit_module.QbitClient, "files", files)

    await _search(db_session, wired, episodes[1])

    on = [call["indices"] for call in wired.qbit.priorities if call["priority"] == FILE_ON]
    assert on == [[n - 1 for n in range(1, 13) if n != 3]]
    assert episodes[3].state is EpisodeState.SEARCHING
    row = await db_session.scalar(select(TorrentFile).where(TorrentFile.episode_id == stale))
    assert row is not None and not row.wanted


@pg
async def test_a_trip_search_holds_the_shows_pack_lock(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S3: ``pg_advisory_xact_lock(trip_pack, anime)`` until the job commits."""
    wired, episodes, anime = await _trip(
        db_session, monkeypatch, tmp_path, anilist_id=962604, email="lock@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))

    await _search(db_session, wired, episodes[1])

    held = await db_session.scalar(
        text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
            " AND pid = pg_backend_pid() AND objsubid = 2"
            " AND objid = CAST(:anime AS oid) AND CAST(classid AS bigint) = :key"
        ).bindparams(anime=anime.id, key=TRIP_PACK_LOCK_KEY % (1 << 32))
    )
    assert held == 1


@pg
async def test_extras_and_episodes_before_the_trip_stay_off(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episodes, _ = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962605,
        email="extras@arc.test",
        numbers=range(5, 13),
    )
    wired.qbit.add_files(
        BATCH_HASH,
        [
            "Sousou no Frieren/[Erai-raws] Sousou no Frieren - NCOP [1080p].mkv",
            "Sousou no Frieren/[Erai-raws] Sousou no Frieren - NCED1 [1080p].mkv",
            "Sousou no Frieren/sample.mkv",
            *_pack_names(*range(1, 13)),
        ],
    )

    await _search(db_session, wired, episodes[5])

    on = [call["indices"] for call in wired.qbit.priorities if call["priority"] == FILE_ON]
    assert on == [[3 + n - 1 for n in range(5, 13)]], "episodes 5–12 and nothing else"
    assert all(episodes[n].state is EpisodeState.NOT_WANTED for n in range(1, 5))


@pg
async def test_a_pack_also_taken_for_a_window_episode_keeps_its_place(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episodes, _ = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962606,
        email="mixed@arc.test",
        numbers=range(1, 12),
    )
    want = await db_session.scalar(select(Want).where(Want.episode_id == episodes[1].id))
    assert want is not None
    episodes[12].state = EpisodeState.WANTED
    db_session.add(Want(user_id=want.user_id, episode_id=episodes[12].id, trip=False))
    await db_session.flush()
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))

    await _search(db_session, wired, episodes[1])

    assert wired.qbit.started == [BATCH_HASH]
    assert episodes[12].state is EpisodeState.DOWNLOADING
    assert wired.qbit.bottomed == [], "somebody streams from it"


@pg
async def test_a_trip_pack_a_window_episode_attaches_to_goes_back_to_the_top(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S4, packs: ``qbit_reselect`` undoes the ``bottomPrio``."""
    wired, episodes, _ = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962607,
        email="untrip-pack@arc.test",
        numbers=range(1, 12),
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))
    await _search(db_session, wired, episodes[1])
    assert wired.qbit.bottomed == [BATCH_HASH]
    pack = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert pack is not None
    # The trip-only pack re-selected keeps its place.
    await qbit_reselect(
        context(db_session, wired.settings, {"torrent_id": pack.id}, job_type=QBIT_RESELECT)
    )
    assert wired.qbit.topped == []

    # Somebody's window reaches episode 12, which the pack holds.
    want = await db_session.scalar(select(Want).where(Want.episode_id == episodes[1].id))
    assert want is not None
    episodes[12].state = EpisodeState.WANTED
    db_session.add(Want(user_id=want.user_id, episode_id=episodes[12].id, trip=False))
    await db_session.flush()
    await _search(db_session, wired, episodes[12], job_id=2)
    assert episodes[12].state is EpisodeState.DOWNLOADING
    await qbit_reselect(
        context(db_session, wired.settings, {"torrent_id": pack.id}, job_type=QBIT_RESELECT)
    )
    assert wired.qbit.topped == [BATCH_HASH]


@pytest.fixture
def trip_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    return acquisition_settings(tmp_path)


@pg
async def test_a_single_no_longer_trip_only_goes_back_to_the_top(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, trip_settings: Settings
) -> None:
    """S4, singles: the reconciler sees the flip and queues ``qbit_top``."""
    qbit = QbitStub()
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, qbit.transport()),
    )
    anime = await make_anime(db_session, anilist_id=962608, status="FINISHED", episodes=12)
    rows = await make_episodes(db_session, anime, 12, aired_through=12)
    traveller = await make_user(db_session, "untrip-a@arc.test")
    viewer = await make_user(db_session, "untrip-b@arc.test")
    await create_trip(
        db_session,
        trip_settings,
        user=traveller,
        anime_id=anime.id,
        count=3,
        now=datetime.now(UTC),
    )
    await compute_wants(db_session)
    rows[2].state = EpisodeState.DOWNLOADING
    db_session.add(Torrent(episode_id=rows[2].id, info_hash="9" * 40, qbit_state="downloading"))
    await db_session.flush()
    assert (await db_session.scalars(select(Job).where(Job.type == QBIT_TOP))).all() == []

    await make_entry(db_session, viewer, anime, progress=2)
    result = await compute_wants(db_session)

    assert result.untripped == (rows[2].id,)
    queued = (await db_session.scalars(select(Job).where(Job.type == QBIT_TOP))).all()
    assert [job.payload["episode_id"] for job in queued] == [rows[2].id]
    again = await compute_wants(db_session)
    assert again.untripped == (), "a flip is reported once"

    await qbit_top(
        context(db_session, trip_settings, {"episode_id": rows[2].id}, job_type=QBIT_TOP)
    )
    assert qbit.topped == ["9" * 40]


@pg
async def test_qbit_top_leaves_a_trip_only_episode_where_it_is(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episodes, _ = await _trip(
        db_session, monkeypatch, tmp_path, anilist_id=962609, email="top-noop@arc.test"
    )
    episodes[1].state = EpisodeState.DOWNLOADING
    db_session.add(Torrent(episode_id=episodes[1].id, info_hash="8" * 40))
    await db_session.flush()

    await qbit_top(
        context(db_session, wired.settings, {"episode_id": episodes[1].id}, job_type=QBIT_TOP)
    )

    assert wired.qbit.topped == []


# --- Well-seeded packs only (owner incident 2026-10-06) -----------------------


@pytest.mark.parametrize(
    ("pack", "single", "min_seeders", "preferred"),
    [
        (4, 80, 10, False),  # production: a pack listed with four seeders
        (9, None, 10, False),  # below the floor even with no single at all
        (10, None, 10, True),
        (40, 50, 10, True),
        (40, 119, 10, True),
        (40, 120, 10, False),  # the single has 3x the pack's seeders
        (12, 36, 10, False),
        (12, 35, 10, True),
        (2, 1, 1, True),
    ],
)
def test_pack_worth_preferring(
    pack: int, single: int | None, min_seeders: int, preferred: bool
) -> None:
    assert nyaa_module.SINGLE_SEEDER_RATIO == 3
    assert nyaa_module.pack_worth_preferring(pack, single, min_seeders=min_seeders) is preferred


FRIEREN_01 = "[SubsPlease] Sousou no Frieren - 01 (1080p) [AB12CD34].mkv"
FRIEREN_FINISHED = Anime(
    anilist_id=154587,
    title_romaji="Sousou no Frieren",
    episodes=28,
    status="FINISHED",
    format="TV",
)


async def _weigh(
    monkeypatch: pytest.MonkeyPatch, *, pack: int, single: int | None, min_seeders: int = 10
) -> tuple[Taker, nyaa_module.Search]:
    """One trip search of episode 1 over a pool holding a pack and maybe a single."""
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    releases = [(BATCH_TITLE_28, BATCH_HASH, pack)]
    if single is not None:
        releases.append((FRIEREN_01, "a" * 40, single))
    stub = NyaaStub(default=_batch_pool(*releases))
    taker = Taker(True)
    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(
            client,
            FRIEREN_FINISHED,
            1,
            Rules(),
            prefer=PackPreference(
                take=taker, numbers=tuple(range(1, 13)), need=4, min_seeders=min_seeders
            ),
        )
    return taker, found


BATCH_TITLE_28 = "[Erai-raws] Sousou no Frieren - 01 ~ 28 [1080p][BATCH]"


async def test_a_thinly_seeded_pack_is_not_offered_and_singles_are_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker, found = await _weigh(monkeypatch, pack=4, single=80)
    assert taker.offered == []
    assert not found.pack_taken
    assert [entry.item.title for entry in found.ranked] == [FRIEREN_01]


async def test_a_well_seeded_pack_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    taker, found = await _weigh(monkeypatch, pack=40, single=50)
    assert taker.offered == [[BATCH_TITLE_28]]
    assert found.pack_taken


async def test_a_pack_three_times_outseeded_by_the_best_single_is_not_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker, found = await _weigh(monkeypatch, pack=12, single=36)
    assert taker.offered == []
    assert [entry.item.title for entry in found.ranked] == [FRIEREN_01]


async def test_the_title_forms_are_not_asked_twice_when_the_pack_is_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub(
        default=_batch_pool((BATCH_TITLE_28, BATCH_HASH, 40), (FRIEREN_01, "a" * 40, 200))
    )
    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(
            client,
            FRIEREN_FINISHED,
            1,
            Rules(),
            prefer=PackPreference(
                take=Taker(True), numbers=tuple(range(1, 13)), need=4, min_seeders=10
            ),
        )
    assert len(stub.queries) == len(set(stub.queries))
    assert found.forms == len(queries(FRIEREN_FINISHED, 1))
    assert found.ranked and found.ranked[0].item.title == FRIEREN_01


def _pack_and_single(pack: int, single: int | None) -> str:
    releases = [(BATCH_TITLE, BATCH_HASH, pack)]
    if single is not None:
        releases.append((FRIEREN_01, "a" * 40, single))
    return _batch_pool(*releases)


@pg
@pytest.mark.parametrize(
    ("pack", "single", "takes_pack"),
    [(4, 80, False), (40, 50, True), (12, 36, False)],
)
async def test_a_trip_takes_a_pack_only_when_it_is_well_seeded(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pack: int,
    single: int,
    takes_pack: bool,
) -> None:
    wired, episodes, _anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962600 + pack,
        email=f"seeded{pack}@arc.test",
        pool=_pack_and_single(pack, single),
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))

    await _search(db_session, wired, episodes[1])

    torrents = (await db_session.scalars(select(Torrent))).all()
    assert len(torrents) == 1
    if takes_pack:
        assert torrents[0].kind is TorrentKind.BATCH
        assert all(episodes[n].state is EpisodeState.DOWNLOADING for n in range(1, 13))
    else:
        assert torrents[0].kind is TorrentKind.SINGLE and torrents[0].info_hash == "a" * 40
        assert wired.qbit.uploaded == [], "the pack was never even added"
        assert episodes[1].state is EpisodeState.DOWNLOADING
        assert episodes[2].state is EpisodeState.WANTED


@pg
async def test_the_seeder_floor_is_the_admin_setting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lowered to 3, the production pack of 4 seeders would be preferred again."""
    wired, episodes, _anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962650,
        email="seeded-setting@arc.test",
        pool=_pack_and_single(4, None),
    )
    await set_setting(db_session, "trip_pack_min_seeders", 3)
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))

    await _search(db_session, wired, episodes[1])

    torrent = await db_session.scalar(select(Torrent))
    assert torrent is not None and torrent.kind is TorrentKind.BATCH


@pg
async def test_a_stalled_trip_pack_is_deleted_and_its_episodes_search_singles(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The production incident, end to end, with the pack at 0 % after the stall clock."""
    wired, episodes, _anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962660,
        email="stalled-trip@arc.test",
        pool=_pack_and_single(30, 5),
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))
    await _search(db_session, wired, episodes[1])
    pack = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert pack is not None and pack.kind is TorrentKind.BATCH
    assert all(episodes[n].state is EpisodeState.DOWNLOADING for n in range(1, 13))

    entry = next(t for t in wired.qbit.torrents if t["hash"] == BATCH_HASH)
    entry |= {
        "state": "stalledDL",
        "progress": 0.0,
        "dlspeed": 0,
        "time_active": 7 * 3600,
        "num_complete": 4,
        "num_incomplete": 0,
    }
    await search_jobs_cleared(db_session)
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert pack.qbit_state == qbit_module.QBIT_STALLED, "the tombstone that bars the hash"
    assert {"hashes": BATCH_HASH, "deleteFiles": "true"} in wired.qbit.deleted
    assert not any(row.wanted for row in await _files(db_session, pack.id)), "claims released"
    for number in range(1, 13):
        assert episodes[number].state is EpisodeState.WANTED, number
    jobs = (await db_session.scalars(select(Job).where(Job.type == "search_release"))).all()
    assert {int(job.payload["episode_id"]) for job in jobs} == {
        episodes[n].id for n in range(1, 13)
    }
    assert {job.priority for job in jobs} == {160}

    # The retry: the pack is neither attached to nor added again; a single is taken.
    wired.qbit.uploaded.clear()
    episodes[1].state = EpisodeState.WANTED
    await _search(db_session, wired, episodes[1], job_id=99)

    assert wired.qbit.uploaded == [], "the stalled pack is not re-taken"
    single = await db_session.scalar(select(Torrent).where(Torrent.info_hash == "a" * 40))
    assert single is not None and single.kind is TorrentKind.SINGLE
    assert episodes[1].state is EpisodeState.DOWNLOADING
    assert pack.qbit_state == qbit_module.QBIT_STALLED


@pg
async def test_a_stalled_pack_for_a_normal_want_still_waits_out_the_retry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only trip-only episodes are put straight back; the rest keep FR-A6's ending."""
    wired, episodes, _anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962661,
        email="stalled-normal@arc.test",
        pool=_pack_and_single(30, 5),
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))
    await _search(db_session, wired, episodes[1])
    await db_session.execute(update(Want).values(trip=False))
    entry = next(t for t in wired.qbit.torrents if t["hash"] == BATCH_HASH)
    entry |= {"state": "stalledDL", "progress": 0.0, "dlspeed": 0, "time_active": 7 * 3600}

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert all(episodes[n].state is EpisodeState.UNAVAILABLE for n in range(1, 13))


async def search_jobs_cleared(session: AsyncSession) -> None:
    """Drop the searches the trip itself queued, so the stall's own are what is left."""
    await session.execute(delete(Job).where(Job.type == "search_release"))
    await session.flush()


# --- No thin pack for a trip by any route (owner incident 2026-10-06) ---------


async def _trip_with_pack(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, anilist_id: int
) -> tuple[Wired, dict[int, Episode], Torrent]:
    """A trip that took a pack, then was cancelled and asked for again.

    The cancel is what production would leave: every claim given back, the pack
    kept stopped by ``disposition`` (the show is still on the list), and the
    episodes wanted once more by a new trip.
    """
    wired, episodes, _anime = await _trip(
        session,
        monkeypatch,
        tmp_path,
        anilist_id=anilist_id,
        email=f"rerequest{anilist_id}@arc.test",
        pool=_pack_and_single(30, 5),
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))
    await _search(session, wired, episodes[1])
    pack = await session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert pack is not None and pack.kind is TorrentKind.BATCH
    await session.execute(
        update(TorrentFile).where(TorrentFile.torrent_id == pack.id).values(wanted=False)
    )
    for number in range(1, 13):
        episodes[number].state = EpisodeState.WANTED
    await session.flush()
    wired.qbit.uploaded.clear()
    return wired, episodes, pack


@pg
@pytest.mark.parametrize(
    ("seeders", "state"),
    [(4, "stalledDL"), (30, "stoppedDL"), (30, "pausedDL"), (None, "stalledDL")],
)
async def test_a_trip_asked_again_does_not_reattach_a_thin_or_stopped_pack(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seeders: int | None,
    state: str,
) -> None:
    """Production row 58: 4 seeders, stopped at 0 % after the trip was cancelled."""
    wired, episodes, pack = await _trip_with_pack(
        db_session, monkeypatch, tmp_path, anilist_id=962700 + (seeders or 0) + len(state)
    )
    pack.seeders = seeders
    pack.qbit_state = state
    await db_session.flush()

    await _search(db_session, wired, episodes[1], job_id=50)

    assert not any(row.wanted for row in await _files(db_session, pack.id)), "not re-attached"
    assert wired.qbit.uploaded == []
    single = await db_session.scalar(select(Torrent).where(Torrent.info_hash == "a" * 40))
    assert single is not None and single.kind is TorrentKind.SINGLE
    assert episodes[1].state is EpisodeState.DOWNLOADING


@pg
async def test_a_trip_still_reattaches_a_well_seeded_running_pack(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episodes, pack = await _trip_with_pack(
        db_session, monkeypatch, tmp_path, anilist_id=962760
    )
    pack.qbit_state = "downloading"
    await db_session.flush()
    wired.nyaa.queries.clear()

    await _search(db_session, wired, episodes[1], job_id=50)

    assert wired.nyaa.queries == [], "attached for free"
    assert episodes[1].state is EpisodeState.DOWNLOADING
    assert any(row.wanted for row in await _files(db_session, pack.id))


@pg
async def test_claim_existing_keeps_its_old_rule_for_a_non_trip_search(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from arc.services.acquisition import batch as batch_module

    _wired, episodes, pack = await _trip_with_pack(
        db_session, monkeypatch, tmp_path, anilist_id=962761
    )
    pack.seeders = 4
    pack.qbit_state = "stoppedDL"
    await db_session.flush()
    episodes[1].state = EpisodeState.SEARCHING

    assert await batch_module.claim_existing(db_session, episodes[1], trip_min_seeders=10) is None
    attached = await batch_module.claim_existing(db_session, episodes[1])
    assert attached is not None and attached.torrent_id == pack.id


@pg
async def test_the_fallback_refuses_a_thin_pack_for_a_trip(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No acceptable single and only a 4-seeder pack: FR-A6's retry, not the pack."""
    from datetime import timedelta

    wired, episodes, _anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962770,
        email="thin-fallback@arc.test",
        pool=_pack_and_single(4, None),
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))
    monkeypatch.setattr(acquisition_jobs, "GIVE_UP_AFTER", timedelta(0))

    await _search(db_session, wired, episodes[1])

    assert wired.qbit.uploaded == [] and wired.nyaa.fetched == []
    assert (await db_session.scalars(select(Torrent))).all() == []
    assert episodes[1].state is EpisodeState.UNAVAILABLE
    assert episodes[1].unavailable_reason == "only a thinly seeded pack"


@pg
async def test_the_fallback_retries_quietly_before_the_fortnight(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episodes, _anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962771,
        email="thin-retry@arc.test",
        pool=_pack_and_single(4, None),
    )
    await search_jobs_cleared(db_session)

    await _search(db_session, wired, episodes[1])

    assert wired.qbit.uploaded == []
    retry = (await db_session.scalars(select(Job).where(Job.type == "search_release"))).all()
    assert [int(job.payload["episode_id"]) for job in retry] == [episodes[1].id]


@pg
async def test_a_non_trip_search_still_takes_a_thin_pack_as_fallback(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FR-A11 for streaming is unchanged: no single, so the only pack is taken."""
    wired, episodes, _anime = await _trip(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=962772,
        email="thin-normal@arc.test",
        pool=_pack_and_single(4, None),
        trip=False,
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 13)))

    await _search(db_session, wired, episodes[1])

    pack = await db_session.scalar(select(Torrent))
    assert pack is not None and pack.kind is TorrentKind.BATCH
