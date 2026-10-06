"""Choosing or changing the release for an episode or a trip by hand (FR-A13).

The pure halves (links, a ``.torrent``'s identity, the classification) need
no database. The rest run the service against the stubbed Nyaa and
qBittorrent of ``test_acquisition_jobs``, and a few go through HTTP for the
status codes and the rollback a refused choice relies on.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.db import SessionFactory
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    Setting,
    Torrent,
    TorrentFile,
    TorrentKind,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    Want,
)
from arc.services.acquisition import manual
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition import qbit as qbit_module
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.jobs import MANUAL_STALL, manual_stall, poll_qbit, search_release
from arc.services.acquisition.manual import (
    ManualConflict,
    ManualForbidden,
    ManualInvalid,
    ManualNotFound,
    Tried,
    candidate_id,
    choose_release,
    classify,
    episode_scope,
    list_releases,
    parse_link,
    torrent_identity,
    trip_scope,
)
from arc.services.acquisition.names import QBIT_CANCEL, QBIT_RESELECT
from arc.services.acquisition.nyaa import NyaaItem, parse_feed
from arc.services.acquisition.qbit import FILE_ON, QBIT_CANCELLED, QBIT_STALLED
from arc.services.acquisition.rules import Rules
from tests.acquisition_helpers import (
    NyaaStub,
    QbitStub,
    acquisition_settings,
    fake_free_space,
    force_transport,
    make_anime,
    make_episodes,
    make_user,
    read_fixture,
    set_setting,
)
from tests.conftest import TEST_FERNET_KEY, add_user, api_transport, login
from tests.test_acquisition_jobs import (
    BATCH_HASH,
    BATCH_TITLE,
    _batch_pool,
    _finished,
    _pack_names,
    context,
    downloading,
    wire,
)

FEED = read_fixture("search_frieren_07.xml")
SUBS_1080 = "42d462368aed5f620f28ae99eacbbea776ed776d"
SUBS_720 = "5bda2833ce8cb07ef0effcca750aab63a329ff2e"
SEASON_TWO = "66b2241832d223e5d2c3364233b43917d1924c92"
NYAA = "https://nyaa.test"


@pytest.fixture(autouse=True)
def _forget() -> Iterator[None]:
    manual.forget_searches()
    yield
    manual.forget_searches()


# --- Links (pure) ---------------------------------------------------------------


def test_a_magnet_gives_its_info_hash() -> None:
    link = parse_link(f"magnet:?xt=urn:btih:{SUBS_1080.upper()}&dn=x", nyaa_url=NYAA)
    assert link.info_hash == SUBS_1080 and link.nyaa_id is None


def test_a_base32_magnet_is_read_as_hex() -> None:
    import base64

    b32 = base64.b32encode(bytes.fromhex(SUBS_1080)).decode()
    assert parse_link(f"magnet:?xt=urn:btih:{b32}", nyaa_url=NYAA).info_hash == SUBS_1080


@pytest.mark.parametrize(
    "url",
    [
        "https://nyaa.si/view/1731672",
        "https://nyaa.si/download/1731672.torrent",
        "https://nyaa.si/view/1731672/",
        "https://nyaa.test/view/1731672",
        "  https://NYAA.si/download/1731672  ",
    ],
)
def test_nyaa_links_give_their_id(url: str) -> None:
    assert parse_link(url, nyaa_url=NYAA).nyaa_id == 1731672


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/download/1.torrent",
        "https://nyaa.si.evil.example/view/1",
        "https://sukebei.nyaa.si/view/1",
        "http://127.0.0.1/view/1",
    ],
)
def test_a_link_off_the_allowlist_is_refused(url: str) -> None:
    with pytest.raises(ManualInvalid) as caught:
        parse_link(url, nyaa_url=NYAA)
    assert caught.value.code == "host_not_allowed"
    assert "nyaa.si" in caught.value.message and "nyaa.test" in caught.value.message


@pytest.mark.parametrize(
    "raw",
    [
        "http://nyaa.si/view/1",
        "https://nyaa.si/user/someone",
        "https://nyaa.si/view/1/../../etc",
        "magnet:?dn=nothing",
        "magnet:?xt=urn:btih:nothex",
        "not a link",
        "",
    ],
)
def test_a_link_that_is_not_one_is_refused(raw: str) -> None:
    with pytest.raises(ManualInvalid) as caught:
        parse_link(raw, nyaa_url=NYAA)
    assert caught.value.code == "bad_link"


# --- A .torrent's identity (pure) -------------------------------------------------


def bencoded(name: str, *, hash_hint: bool = True) -> tuple[bytes, str]:
    """A small real v1 torrent, and its info hash.

    ``hash_hint`` adds a top-level ``hash`` key the qBittorrent stub reads to
    know which torrent it was handed (``QbitStub._blob_hash``).
    """
    raw_name = name.encode()
    info = (
        b"d6:lengthi5e4:name"
        + str(len(raw_name)).encode()
        + b":"
        + raw_name
        + b"12:piece lengthi16384e6:pieces20:"
        + b"\x01" * 20
        + b"e"
    )
    digest = hashlib.sha1(info, usedforsecurity=False).hexdigest()
    hint = b"4:hash40:" + digest.encode() if hash_hint else b""
    return b"d8:announce3:abc" + hint + b"4:info" + info + b"e", digest


def test_a_torrent_identity_is_the_sha1_of_its_info_dict() -> None:
    blob, digest = bencoded("Sousou no Frieren - 07.mkv")
    assert torrent_identity(blob) == (digest, "Sousou no Frieren - 07.mkv")


@pytest.mark.parametrize(
    "blob",
    [b"", b"<html>", b"d8:announce3:abce", b"d4:infod4:name", b"l" * 200 + b"e" * 200],
)
def test_a_blob_that_is_not_a_torrent_is_refused(blob: bytes) -> None:
    with pytest.raises(ValueError):
        torrent_identity(blob)


# --- Classification (pure) ----------------------------------------------------------


def _frieren() -> Anime:
    return Anime(
        id=1,
        anilist_id=1,
        title_romaji="Sousou no Frieren",
        title_english="Frieren: Beyond Journey's End",
        status="RELEASING",
        episodes=28,
    )


def _classify(items: list[NyaaItem], **overrides: object) -> list[manual.Choice]:
    arguments: dict[str, object] = {
        "anime": _frieren(),
        "number": 7,
        "rules": Rules(preferred_groups=("SubsPlease",)),
        "offset": None,
        "wanted_numbers": (7, 8),
        "tried": {},
        "target_ids": {70},
        "thin_floor": None,
        "packs_first": False,
    }
    arguments.update(overrides)
    return classify(items, **arguments)  # type: ignore[arg-type]


def test_the_list_ranks_what_arc_would_take_and_says_why_not_for_the_rest() -> None:
    choices = _classify(parse_feed(FEED))

    first = choices[0]
    assert first.item.info_hash == SUBS_1080 and first.acceptable and first.reason is None
    assert first.kind == "single" and first.covers == (7,)
    assert first.id == candidate_id(SUBS_1080)
    taken = [choice for choice in choices if choice.acceptable]
    kinds = [choice.kind for choice in taken]
    assert kinds == sorted(kinds, key=lambda kind: kind != "single"), "singles before packs"
    assert "batch" in kinds, "the 01-07 pack of season one is offered too"
    season_two = next(choice for choice in choices if choice.info_hash == SEASON_TWO)
    assert not season_two.acceptable and season_two.reason == "season 2, not 1"
    assert season_two.listed
    # Acceptable before not, and nothing listed twice.
    flags = [choice.acceptable for choice in choices]
    assert flags == sorted(flags, reverse=True)
    assert len({choice.id for choice in choices}) == len(choices)


def test_a_release_arc_already_tried_says_so() -> None:
    tried = {SUBS_1080: Tried(state=QBIT_STALLED, kind=TorrentKind.SINGLE, episode_id=70)}
    choices = _classify(parse_feed(FEED), tried=tried)
    stalled = next(choice for choice in choices if choice.info_hash == SUBS_1080)
    assert not stalled.acceptable and "stalled" in (stalled.reason or "")
    assert choices[0].info_hash != SUBS_1080


def test_the_release_downloading_now_is_marked_current() -> None:
    tried = {SUBS_1080: Tried(state="downloading", kind=TorrentKind.SINGLE, episode_id=70)}
    current = next(choice for choice in _classify(parse_feed(FEED), tried=tried) if choice.current)
    assert current.info_hash == SUBS_1080 and not current.acceptable


def test_a_name_with_no_episode_number_is_taken_file_by_file() -> None:
    """A whole-series disc rip with no BATCH marker must never go in as one magnet."""
    pool = parse_feed(_batch_pool(("[Judas] Sousou no Frieren [BD 1080p]", "f" * 40, 40)))
    (choice,) = _classify(pool)
    assert not choice.acceptable and choice.kind == "batch" and choice.covers is None


def test_another_show_is_not_listed() -> None:
    pool = parse_feed(
        _batch_pool(("[SubsPlease] Totally Different Show - 07 (1080p)", "e" * 40, 9))
    )
    (choice,) = _classify(pool)
    assert not choice.listed and (choice.reason or "").startswith("title ")


def test_packs_say_what_they_cover_and_a_thin_one_is_flagged_for_a_trip() -> None:
    pool = parse_feed(
        _batch_pool(
            (BATCH_TITLE, BATCH_HASH, 4),
            ("[Judas] Sousou no Frieren [BD 1080p][BATCH]", "c" * 40, 30),
            ("[SubsPlease] Sousou no Frieren - 07 (1080p)", "a" * 40, 50),
        )
    )
    choices = _classify(pool, wanted_numbers=(1, 2, 3, 30), thin_floor=10, packs_first=True)

    unnamed, single, thin = choices
    assert unnamed.kind == "batch" and unnamed.acceptable and unnamed.covers is None
    assert single.kind == "single" and single.acceptable
    assert thin.info_hash == BATCH_HASH and not thin.acceptable
    assert thin.covers == (1, 2, 3), "the wanted numbers inside 1 ~ 28"
    assert thin.reason == "a thinly seeded pack (4 seeders; trips take 10 or more)"


# --- The stall sentence ---------------------------------------------------------------


def test_a_manual_release_that_stalls_says_it_was_yours() -> None:
    picked = Torrent(info_hash="a" * 40, manual=False)
    chosen = Torrent(info_hash="b" * 40, manual=True)
    assert manual_stall(picked, "no seeders after 6 hours") == "no seeders after 6 hours"
    assert manual_stall(chosen, "no seeders after 6 hours") == MANUAL_STALL.format(
        reason="no seeders after 6 hours"
    )


# --- The service, against the stubs ---------------------------------------------------

pg = pytest.mark.pg


async def _user(session: AsyncSession, episode: Episode) -> User:
    want = await session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    user = await session.get(User, want.user_id)
    assert user is not None
    return user


async def _no_floor(session: AsyncSession) -> None:
    await set_setting(session, "min_free_gb", 0)


async def _jobs(session: AsyncSession, job_type: str) -> list[Job]:
    return list((await session.scalars(select(Job).where(Job.type == job_type))).all())


async def _mal_jobs(session: AsyncSession) -> int:
    return int(
        await session.scalar(select(func.count()).select_from(Job).where(Job.type.like("mal%")))
        or 0
    )


@pg
async def test_the_list_is_searched_once_a_minute(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963001, email="manual1@arc.test"
    )
    await _no_floor(db_session)
    user = await _user(db_session, episode)
    scope = await episode_scope(db_session, user, episode.id)

    first = await list_releases(db_session, wired.settings, scope)
    asked = len(wired.nyaa.queries)
    second = await list_releases(db_session, wired.settings, scope)

    assert asked >= 4, "title forms and the batch forms"
    assert len(wired.nyaa.queries) == asked, "the second answer is the cached one"
    assert not first.cached and second.cached
    assert [c.id for c in first.choices] == [c.id for c in second.choices]
    assert first.choices[0].acceptable
    assert first.current is None

    clock = manual._now() + manual.SEARCH_EVERY + 1
    monkeypatch.setattr(manual, "_now", lambda: clock)
    third = await list_releases(db_session, wired.settings, scope)
    # A fresh search — answered from the shared client's ten-minute cache,
    # which is the existing budget doing its job.
    assert not third.cached


@pg
async def test_choosing_a_single_replaces_the_running_one(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, old = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=963002, email="manual2@arc.test"
    )
    await _no_floor(db_session)
    user = await _user(db_session, episode)
    entry_before = await db_session.get(ListEntry, (user.id, episode.anime_id))
    assert entry_before is not None
    progress_before = entry_before.progress
    scope = await episode_scope(db_session, user, episode.id)
    listed = await list_releases(db_session, wired.settings, scope)
    current = next(choice for choice in listed.choices if choice.current)
    assert current.info_hash == old.info_hash
    assert listed.current is not None and listed.current.info_hash == old.info_hash
    other = next(
        choice
        for choice in listed.choices
        if choice.acceptable and choice.info_hash != old.info_hash
    )

    chosen = await choose_release(db_session, wired.settings, scope, candidate=other.id)

    assert chosen.kind == "single" and chosen.info_hash == other.info_hash
    assert old.qbit_state == QBIT_CANCELLED, "the old single is removed by qbit_cancel"
    assert [job.payload["episode_id"] for job in await _jobs(db_session, QBIT_CANCEL)] == [
        episode.id
    ]
    new = await db_session.scalar(select(Torrent).where(Torrent.info_hash == other.info_hash))
    assert new is not None and new.manual and new.episode_id == episode.id
    assert new.qbit_state == "added"
    assert episode.state is EpisodeState.DOWNLOADING
    assert any(other.info_hash in form.get("urls", "") for form in wired.qbit.added)
    # Nothing about the list or MyAnimeList moved (FR-A13, non-negotiable).
    assert await _mal_jobs(db_session) == 0
    await db_session.refresh(entry_before)
    assert entry_before.progress == progress_before


@pg
async def test_a_pasted_magnet_is_taken_only_when_nyaa_has_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963003, email="manual3@arc.test"
    )
    await _no_floor(db_session)
    scope = await episode_scope(db_session, await _user(db_session, episode), episode.id)

    with pytest.raises(ManualInvalid) as unknown:
        await choose_release(
            db_session, wired.settings, scope, link=f"magnet:?xt=urn:btih:{'9' * 40}"
        )
    assert unknown.value.code == "magnet_unknown"

    chosen = await choose_release(
        db_session, wired.settings, scope, link=f"magnet:?xt=urn:btih:{SUBS_720}&dn=whatever"
    )
    assert chosen.info_hash == SUBS_720 and episode.state is EpisodeState.DOWNLOADING


@pg
async def test_a_single_for_another_episode_is_refused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963004, email="manual4@arc.test"
    )
    await _no_floor(db_session)
    wired.nyaa.default = _batch_pool(("[SubsPlease] Sousou no Frieren - 09 (1080p)", "9" * 40, 40))
    scope = await episode_scope(db_session, await _user(db_session, episode), episode.id)
    listed = await list_releases(db_session, wired.settings, scope)
    nine = next(choice for choice in listed.choices if choice.info_hash == "9" * 40)
    assert not nine.acceptable and nine.covers == (9,)

    with pytest.raises(ManualInvalid) as caught:
        await choose_release(db_session, wired.settings, scope, candidate=nine.id)
    assert caught.value.code == "wrong_episode"
    assert episode.state is EpisodeState.WANTED


@pg
async def test_choosing_a_pack_selects_only_the_callers_wanted_files(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=963005, email="manual5@arc.test"
    )
    await _no_floor(db_session)
    user = await _user(db_session, episode)
    episodes = {
        row.number: row
        for row in (
            await db_session.scalars(select(Episode).where(Episode.anime_id == episode.anime_id))
        ).all()
    }
    # The caller also wants 8 (a free rider); somebody else wants 9 (not theirs).
    episodes[8].state = EpisodeState.WANTED
    db_session.add(Want(user_id=user.id, episode_id=episodes[8].id))
    other = await make_user(db_session, "manual5b@arc.test")
    episodes[9].state = EpisodeState.WANTED
    db_session.add(Want(user_id=other.id, episode_id=episodes[9].id))
    await db_session.flush()
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, 29)))
    scope = await episode_scope(db_session, user, episode.id)
    listed = await list_releases(db_session, wired.settings, scope)
    pack = next(choice for choice in listed.choices if choice.info_hash == BATCH_HASH)
    assert pack.kind == "batch" and pack.acceptable and pack.covers == (7, 8)

    chosen = await choose_release(db_session, wired.settings, scope, candidate=pack.id)

    assert chosen.kind == "batch"
    assert sorted(row.number for row in chosen.episodes) == [7, 8]
    torrent = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None and torrent.manual and torrent.kind is TorrentKind.BATCH
    rows = (
        await db_session.scalars(select(TorrentFile).where(TorrentFile.torrent_id == torrent.id))
    ).all()
    assert sorted(row.file_index + 1 for row in rows if row.wanted) == [7, 8]
    # The add sequence ran: everything off, then exactly the two on, verified.
    on = [call for call in wired.qbit.priorities if call["priority"] == FILE_ON]
    assert on == [{"hash": BATCH_HASH, "indices": [6, 7], "priority": FILE_ON}]
    assert wired.qbit.started == [BATCH_HASH]
    assert episodes[9].state is EpisodeState.WANTED, "another user's want is not the caller's"
    assert episode.state is EpisodeState.DOWNLOADING


@pg
async def test_a_pasted_link_arc_has_not_seen_is_taken_file_by_file(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963006, email="manual6@arc.test"
    )
    await _no_floor(db_session)
    blob, digest = bencoded("Sousou no Frieren")
    wired.nyaa.blobs[f"{NYAA}/download/5550001.torrent"] = blob
    wired.qbit.add_files(digest, _pack_names(6, 7, 8))
    scope = await episode_scope(db_session, await _user(db_session, episode), episode.id)

    chosen = await choose_release(
        db_session, wired.settings, scope, link="https://nyaa.si/view/5550001"
    )

    assert chosen.info_hash == digest and chosen.kind == "batch"
    assert f"{NYAA}/download/5550001.torrent" in wired.nyaa.fetched, (
        "rebuilt on the configured host"
    )
    on = [call for call in wired.qbit.priorities if call["priority"] == FILE_ON]
    assert on == [{"hash": digest, "indices": [1], "priority": FILE_ON}], "episode 7 only"


@pg
async def test_a_pasted_pack_without_the_episode_is_refused_and_removed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963007, email="manual7@arc.test"
    )
    await _no_floor(db_session)
    blob, digest = bencoded("Sousou no Frieren")
    wired.nyaa.blobs[f"{NYAA}/download/5550002.torrent"] = blob
    wired.qbit.add_files(digest, _pack_names(1, 2, 3))
    scope = await episode_scope(db_session, await _user(db_session, episode), episode.id)

    with pytest.raises(ManualInvalid) as caught:
        await choose_release(
            db_session, wired.settings, scope, link="https://nyaa.si/download/5550002.torrent"
        )

    assert caught.value.code == "pack_selects_nothing"
    assert "episode 7" in caught.value.message
    assert any(digest in form.get("hashes", "") for form in wired.qbit.deleted)
    assert wired.qbit.started == [], "not a byte moved"
    assert not [call for call in wired.qbit.priorities if call["priority"] == FILE_ON]


@pg
async def test_the_refusals(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963008, email="manual8@arc.test"
    )
    await _no_floor(db_session)
    user = await _user(db_session, episode)

    stranger = await make_user(db_session, "manual8b@arc.test")
    with pytest.raises(ManualNotFound):
        await episode_scope(db_session, stranger, episode.id)
    with pytest.raises(ManualNotFound):
        await episode_scope(db_session, user, 987654321)
    stranger.is_demo = True
    db_session.add(Want(user_id=stranger.id, episode_id=episode.id))
    await db_session.flush()
    with pytest.raises(ManualForbidden):
        await episode_scope(db_session, stranger, episode.id)

    scope = await episode_scope(db_session, user, episode.id)
    with pytest.raises(ManualInvalid) as unknown:
        await choose_release(db_session, wired.settings, scope, candidate="0" * 16)
    assert unknown.value.code == "unknown_candidate"
    with pytest.raises(ManualInvalid) as host:
        await choose_release(db_session, wired.settings, scope, link="https://evil.example/x")
    assert host.value.code == "host_not_allowed"

    # A release Arc already tried and that stalled.
    listed = await list_releases(db_session, wired.settings, scope)
    first = listed.choices[0]
    db_session.add(
        Torrent(episode_id=episode.id, info_hash=first.info_hash, qbit_state=QBIT_STALLED)
    )
    await db_session.flush()
    with pytest.raises(ManualInvalid) as tried:
        await choose_release(db_session, wired.settings, scope, candidate=first.id)
    assert tried.value.code == "already_tried"

    await set_setting(db_session, "min_free_gb", 50)
    fake_free_space(monkeypatch, acquisition_rules, 1)
    with pytest.raises(ManualConflict) as held:
        await choose_release(db_session, wired.settings, scope, candidate=first.id)
    assert held.value.code == "storage_held"


@pg
async def test_a_trip_pack_replaces_what_its_episodes_were_downloading(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, seed = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=963009, email="manual9@arc.test"
    )
    await _no_floor(db_session)
    user = await _user(db_session, seed)
    rows = (
        await db_session.scalars(select(Episode).where(Episode.anime_id == seed.anime_id))
    ).all()
    episodes = {row.number: row for row in rows}
    trip = Trip(
        user_id=user.id,
        anime_id=seed.anime_id,
        first_number=1,
        last_number=4,
        count=4,
        state=TripState.ACTIVE,
        deadline_at=datetime.now(UTC) + timedelta(days=14),
    )
    db_session.add(trip)
    await db_session.flush()
    for number in range(1, 5):
        episodes[number].state = EpisodeState.WANTED
        db_session.add(Want(user_id=user.id, episode_id=episodes[number].id, trip=True))
        db_session.add(
            TripEpisode(
                trip_id=trip.id, episode_id=episodes[number].id, state=TripEpisodeState.PENDING
            )
        )
    # Episode 2 is downloading a single of its own.
    episodes[2].state = EpisodeState.DOWNLOADING
    old = Torrent(episode_id=episodes[2].id, info_hash="2" * 40, qbit_state="downloading")
    db_session.add(old)
    await db_session.flush()
    # The pack holds 1–3 only.
    wired.qbit.add_files(BATCH_HASH, _pack_names(1, 2, 3))
    scope = await trip_scope(db_session, user, trip.id)
    assert [row.number for row in scope.targets] == [1, 2, 3, 4]
    listed = await list_releases(db_session, wired.settings, scope)
    pack = listed.choices[0]
    assert pack.kind == "batch" and pack.info_hash == BATCH_HASH, "packs first for a trip"
    assert pack.acceptable, "30 seeders clears the trip floor of 10"

    chosen = await choose_release(db_session, wired.settings, scope, candidate=pack.id)

    assert sorted(row.number for row in chosen.episodes) == [1, 2, 3]
    assert old.qbit_state == QBIT_CANCELLED
    for number in (1, 2, 3):
        assert episodes[number].state is EpisodeState.DOWNLOADING
    assert episodes[4].state is EpisodeState.WANTED, "the pack does not hold it; left alone"
    torrent = await db_session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None and torrent.manual
    assert wired.qbit.bottomed == [BATCH_HASH], "trip-only: to the back"
    assert await _mal_jobs(db_session) == 0


@pg
async def test_a_trip_belongs_to_its_owner(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    anime = await make_anime(db_session, anilist_id=963010)
    await make_episodes(db_session, anime, 3)
    owner = await make_user(db_session, "manual10@arc.test")
    other = await make_user(db_session, "manual10b@arc.test")
    trip = Trip(
        user_id=owner.id,
        anime_id=anime.id,
        first_number=1,
        last_number=3,
        count=3,
        state=TripState.FINISHED,
        deadline_at=datetime.now(UTC),
    )
    db_session.add(trip)
    await db_session.flush()
    with pytest.raises(ManualNotFound):
        await trip_scope(db_session, other, trip.id)
    with pytest.raises(ManualConflict) as ended:
        await trip_scope(db_session, owner, trip.id)
    assert ended.value.code == "trip_not_active"


@pg
async def test_a_manual_release_that_stalls_tells_the_episode(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=963011, email="manual11@arc.test"
    )
    torrent.manual = True
    await db_session.flush()
    wired.qbit.add_torrent(
        torrent.info_hash, progress=0.0, state="stalledDL", time_active=7 * 3600, num_complete=0
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.UNAVAILABLE
    assert (episode.unavailable_reason or "").startswith("the release you chose stalled: ")


@pg
async def test_a_search_does_not_replace_a_manual_choice(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FR-A6's search is a no-op for an episode already downloading its chosen release."""
    wired, episode, torrent = await downloading(
        db_session, monkeypatch, tmp_path, anilist_id=963012, email="manual12@arc.test"
    )
    torrent.manual = True
    await db_session.flush()
    asked = len(wired.nyaa.queries)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert len(wired.nyaa.queries) == asked
    assert episode.state is EpisodeState.DOWNLOADING and torrent.qbit_state != QBIT_CANCELLED


# --- Through HTTP --------------------------------------------------------------------

EMAIL = "chooser@arc.test"
PASSWORD = "chooserpassword"


def _stub(monkeypatch: pytest.MonkeyPatch, nyaa: NyaaStub, qbit: QbitStub) -> None:
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


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
async def chooser(
    api_app: FastAPI, api_factory: SessionFactory, tmp_path: Path
) -> AsyncIterator[AsyncClient]:
    # Nyaa and qBittorrent at the stubs' addresses, everything else as the app had it.
    api_app.state.settings = acquisition_settings(
        tmp_path,
        database_url=api_app.state.settings.database_url,
        fernet_key=TEST_FERNET_KEY,
    )
    await add_user(api_factory, EMAIL, PASSWORD)
    async with api_factory() as session:
        row = await session.get(Setting, "min_free_gb")
        assert row is not None
        row.value = 0
        await session.commit()
    async with api_transport(api_app) as client:
        yield await login(client, EMAIL, PASSWORD)


async def _wanted_episode(factory: SessionFactory, *, anilist_id: int, email: str = EMAIL) -> int:
    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        anime = await make_anime(session, anilist_id=anilist_id)
        episodes = await make_episodes(session, anime, 12, aired_through=7)
        episode = episodes[6]
        episode.state = EpisodeState.WANTED
        session.add(Want(user_id=user.id, episode_id=episode.id))
        await session.commit()
        return episode.id


@pg
async def test_http_refusals(
    chooser: AsyncClient, api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    episode_id = await _wanted_episode(api_factory, anilist_id=963101)

    assert (await api_client.get(f"/api/episodes/{episode_id}/releases")).status_code == 401
    missing = await chooser.get("/api/episodes/987654/releases")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "not_wanted"
    bad = await chooser.post(
        f"/api/episodes/{episode_id}/release", json={"link": "https://evil.example/view/1"}
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "host_not_allowed"
    assert "nyaa.si" in bad.json()["detail"]["message"]
    neither = await chooser.post(f"/api/episodes/{episode_id}/release", json={})
    assert neither.status_code == 422
    unknown = await chooser.post(
        f"/api/episodes/{episode_id}/release", json={"candidate_id": "deadbeef"}
    )
    assert unknown.status_code == 422 and unknown.json()["detail"]["code"] == "unknown_candidate"
    no_trip = await chooser.get("/api/trips/987654/releases")
    assert no_trip.status_code == 404


@pg
async def test_http_demo_is_refused(api_app: FastAPI, api_factory: SessionFactory) -> None:
    user = await add_user(api_factory, "demo-chooser@arc.test", "demopassword1")
    async with api_factory() as session:
        row = await session.get(User, user.id)
        assert row is not None
        row.is_demo = True
        await session.commit()
    episode_id = await _wanted_episode(
        api_factory, anilist_id=963102, email="demo-chooser@arc.test"
    )
    async with api_transport(api_app) as client:
        await login(client, "demo-chooser@arc.test", "demopassword1")
        listed = await client.get(f"/api/episodes/{episode_id}/releases")
        chose = await client.post(
            f"/api/episodes/{episode_id}/release", json={"link": "https://nyaa.si/view/1"}
        )
    assert listed.status_code == 403 and listed.json()["detail"]["code"] == "demo_account"
    assert chose.status_code == 403


@pg
async def test_http_a_refused_choice_leaves_the_old_download_alone(
    chooser: AsyncClient,
    api_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nyaa = NyaaStub({"Sousou no Frieren - 07": FEED})
    qbit = QbitStub()
    _stub(monkeypatch, nyaa, qbit)
    episode_id = await _wanted_episode(api_factory, anilist_id=963103)
    async with api_factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        episode.state = EpisodeState.SEARCHING
        await session.flush()
        episode.state = EpisodeState.DOWNLOADING
        session.add(Torrent(episode_id=episode_id, info_hash="7" * 40, qbit_state="downloading"))
        await session.commit()
    blob, digest = bencoded("Sousou no Frieren")
    nyaa.blobs["https://nyaa.test/download/5550003.torrent"] = blob
    qbit.add_files(digest, _pack_names(1, 2))

    listed = await chooser.get(f"/api/episodes/{episode_id}/releases")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["scope"] == "episode" and body["number"] == 7
    assert body["candidates"] and body["candidates"][0]["acceptable"] is True
    assert set(body["candidates"][0]) >= {
        "id",
        "title",
        "group",
        "resolution",
        "size",
        "seeders",
        "leechers",
        "kind",
        "trusted",
        "covers",
        "acceptable",
        "reason",
    }
    assert body["current"]["kind"] == "single"

    refused = await chooser.post(
        f"/api/episodes/{episode_id}/release", json={"link": "https://nyaa.si/view/5550003"}
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "pack_selects_nothing"
    async with api_factory() as session:
        episode = await session.get(Episode, episode_id)
        old = await session.scalar(select(Torrent).where(Torrent.info_hash == "7" * 40))
        assert episode is not None and old is not None
        assert episode.state is EpisodeState.DOWNLOADING, "rolled back"
        assert old.qbit_state == "downloading"
        assert not (await session.scalars(select(Job).where(Job.type == QBIT_CANCEL))).all()
        assert not (await session.scalars(select(Job).where(Job.type == QBIT_RESELECT))).all()
    assert any(digest in form.get("hashes", "") for form in qbit.deleted)

    chosen = await chooser.post(
        f"/api/episodes/{episode_id}/release",
        json={"candidate_id": body["candidates"][0]["id"]},
    )
    assert chosen.status_code == 202, chosen.text
    assert chosen.json()["episodes"] == [
        {"episode_id": episode_id, "number": 7, "state": "downloading"}
    ]
    async with api_factory() as session:
        old = await session.scalar(select(Torrent).where(Torrent.info_hash == "7" * 40))
        assert old is not None and old.qbit_state == QBIT_CANCELLED
        anime_id = (await session.get(Episode, episode_id)).anime_id  # type: ignore[union-attr]
    show = await chooser.get(f"/api/anime/{anime_id}")
    row = next(row for row in show.json()["episodes"] if row["id"] == episode_id)
    assert row["wanted_by_me"] is True
    assert row["release"]["manual"] is True


# --- Review fixes (2026-10-06) ---------------------------------------------------


@pytest.mark.parametrize(
    "blob",
    [
        b"d1:a1:b-6:e",  # a negative length used to loop for ever
        b"d1:a_1:be",
        b"d1:a 1:be",
        b"d1:a+1:be",
        b"d4:infod4:name-1:e",
        b"d4:infodi5e",  # no progress possible: a dict key that is not a string
        b"d4:infol" + b"i1e" * (manual.MAX_BENCODE_VALUES + 5) + b"ee",
        b"d4:info" + b"l" * 70 + b"e" * 70 + b"e",
    ],
)
def test_the_bencode_reader_refuses_and_terminates(blob: bytes) -> None:
    import time as clock

    started = clock.monotonic()
    with pytest.raises(ValueError):
        torrent_identity(blob)
    assert clock.monotonic() - started < 2


@pytest.mark.parametrize(
    "url", ["https://nyaa.si/view/١٢٣", "https://nyaa.si/download/１２.torrent"]
)
def test_non_ascii_digits_are_not_an_id(url: str) -> None:
    with pytest.raises(ManualInvalid) as caught:
        parse_link(url, nyaa_url=NYAA)
    assert caught.value.code == "bad_link"


def _scope_stub(user_id: int, scope_id: int) -> manual.Scope:
    return manual.Scope(
        kind="episode",
        id=scope_id,
        user=User(id=user_id),
        anime=Anime(id=1),
        targets=[],
        wanted_numbers=(),
        number=1,
    )


async def test_one_live_search_per_scope_is_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    calls: list[int] = []

    async def slow(_session: object, _settings: object, scope: manual.Scope) -> manual.Searched:
        calls.append(scope.id)
        await asyncio.sleep(0.01)
        return manual.Searched(at=manual._now(), choices=[])

    monkeypatch.setattr(manual, "_search_live", slow)
    scope = _scope_stub(41, 1)
    first, second = await asyncio.gather(
        manual._search(None, None, scope),  # type: ignore[arg-type]
        manual._search(None, None, scope),  # type: ignore[arg-type]
    )
    assert calls == [1], "the second request waited for the first"
    assert {first[1], second[1]} == {False, True}


async def test_live_searches_are_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    gate = asyncio.Event()

    async def held(_session: object, _settings: object, scope: manual.Scope) -> manual.Searched:
        await gate.wait()
        return manual.Searched(at=manual._now(), choices=[])

    monkeypatch.setattr(manual, "_search_live", held)
    running = asyncio.ensure_future(manual._search(None, None, _scope_stub(42, 1)))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    with pytest.raises(manual.ManualTooMany) as mine:
        await manual._search(None, None, _scope_stub(42, 2))  # type: ignore[arg-type]
    assert mine.value.code == "search_running" and mine.value.status == 429
    other = asyncio.ensure_future(manual._search(None, None, _scope_stub(43, 1)))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    with pytest.raises(manual.ManualTooMany) as busy:
        await manual._search(None, None, _scope_stub(44, 1))  # type: ignore[arg-type]
    assert busy.value.code == "searches_busy"
    gate.set()
    await asyncio.gather(running, other)

    gate.set()
    for scope_id in (2, 3):
        await manual._search(None, None, _scope_stub(42, scope_id))  # type: ignore[arg-type]
    with pytest.raises(manual.ManualTooMany) as minute:
        await manual._search(None, None, _scope_stub(42, 4))  # type: ignore[arg-type]
    assert minute.value.code == "too_many_searches"


def _numbered_feed(title: str, info_hash: str, nyaa_id: int, seeders: int = 30) -> str:
    return _batch_pool((title, info_hash, seeders)).replace(
        f"https://nyaa.test/download/{info_hash}.torrent",
        f"https://nyaa.test/download/{nyaa_id}.torrent",
    )


@pg
@pytest.mark.parametrize("route", ["candidate", "magnet", "link"])
async def test_another_show_cannot_be_chosen_by_any_route(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963020, email="manual20@arc.test", feed=None
    )
    await _no_floor(db_session)
    other = "e" * 40
    wired.nyaa.default = _numbered_feed(
        "[SubsPlease] Totally Different Show - 07 (1080p)", other, 7770001
    )
    scope = await episode_scope(db_session, await _user(db_session, episode), episode.id)
    listed = await list_releases(db_session, wired.settings, scope)
    assert all(choice.info_hash != other for choice in listed.choices), "not listed"
    request = {
        "candidate": {"candidate": candidate_id(other)},
        "magnet": {"link": f"magnet:?xt=urn:btih:{other}"},
        "link": {"link": "https://nyaa.si/view/7770001"},
    }[route]

    with pytest.raises(ManualInvalid) as caught:
        await choose_release(db_session, wired.settings, scope, **request)

    assert caught.value.code == "wrong_show"
    assert wired.qbit.added == []


@pg
async def test_a_pasted_torrent_of_another_show_is_refused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963021, email="manual21@arc.test"
    )
    await _no_floor(db_session)
    blob, digest = bencoded("Totally Different Show")
    wired.nyaa.blobs[f"{NYAA}/download/5550021.torrent"] = blob
    wired.qbit.add_files(digest, ["Show/- 06 [1080p].mkv", "Show/- 07 [1080p].mkv"])
    scope = await episode_scope(db_session, await _user(db_session, episode), episode.id)

    with pytest.raises(ManualInvalid) as caught:
        await choose_release(db_session, wired.settings, scope, link="https://nyaa.si/view/5550021")

    assert caught.value.code == "wrong_show"
    assert wired.qbit.added == [] and wired.qbit.started == []


@pg
async def test_a_pack_torrent_is_fetched_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963022, email="manual22@arc.test"
    )
    await _no_floor(db_session)
    blob, digest = bencoded("Sousou no Frieren")
    url = f"{NYAA}/download/5550022.torrent"
    wired.nyaa.blobs[url] = blob
    wired.qbit.add_files(digest, _pack_names(7))
    scope = await episode_scope(db_session, await _user(db_session, episode), episode.id)

    await choose_release(db_session, wired.settings, scope, link="https://nyaa.si/view/5550022")

    assert wired.nyaa.fetched.count(url) == 1


@pg
async def test_a_stale_search_does_not_overwrite_a_manual_choice(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The search's row says ``wanted``; the database says ``downloading`` (manual)."""
    from sqlalchemy import update

    wired, episode = await wire(
        db_session, monkeypatch, tmp_path, anilist_id=963023, email="manual23@arc.test"
    )
    assert episode.state is EpisodeState.WANTED
    await db_session.execute(
        update(Episode)
        .where(Episode.id == episode.id)
        .values(state=EpisodeState.DOWNLOADING)
        .execution_options(synchronize_session=False)
    )
    assert episode.state is EpisodeState.WANTED, "the identity map is stale"

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.nyaa.queries == [], "the search saw downloading and stopped"
    assert episode.state is EpisodeState.DOWNLOADING


@pg
async def test_http_a_failed_commit_removes_the_torrent_it_added(
    chooser: AsyncClient,
    api_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nyaa = NyaaStub({"Sousou no Frieren - 07": FEED})
    qbit = QbitStub()
    _stub(monkeypatch, nyaa, qbit)
    episode_id = await _wanted_episode(api_factory, anilist_id=963104)
    listed = await chooser.get(f"/api/episodes/{episode_id}/releases")
    assert listed.status_code == 200, listed.text
    single = next(c for c in listed.json()["candidates"] if c["kind"] == "single")
    assert single["id"] == candidate_id(SUBS_1080)
    # The client lists what it was given, so the delete's category check passes.
    qbit.add_torrent(SUBS_1080, state="metaDL")

    async def broken(self: AsyncSession) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(AsyncSession, "commit", broken)
    with pytest.raises(RuntimeError):
        await chooser.post(
            f"/api/episodes/{episode_id}/release", json={"candidate_id": single["id"]}
        )
    monkeypatch.undo()

    assert len(qbit.added) == 1 and SUBS_1080 in qbit.added[0]["urls"]
    assert any(SUBS_1080 in form.get("hashes", "") for form in qbit.deleted)
