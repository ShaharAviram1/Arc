"""Pack match review (M21 T7; spec FR-A4, FR-A11; architecture.md §5.2 step 4).

A season pack whose files cannot tell Arc which one is the wanted episode is
kept stopped with nothing selected for a person's pick, never guessed. The
search, the pick, the dismiss, the landing and the reconciliation run against
the real database with Nyaa and qBittorrent mocked (``tests/acquisition_helpers``);
the routes run against the real app.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.db import SessionFactory
from arc.models import (
    PACK_PICK_DISMISSED,
    PACK_PICK_PICKED,
    PACK_PICK_REASON,
    Anime,
    Episode,
    EpisodeState,
    MediaFile,
    PackPick,
    ReviewState,
    Torrent,
    TorrentFile,
    TorrentKind,
    User,
    Want,
)
from arc.services.acquisition import batch as batch_module
from arc.services.acquisition import jobs as acquisition_jobs
from arc.services.acquisition import pack_picks
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition import wants as wants_module
from arc.services.acquisition.demand import ACQUISITION_PAUSED, STORAGE_HELD
from arc.services.acquisition.jobs import (
    compute_wants,
    poll_qbit,
    qbit_forget_pack,
    qbit_reselect,
    search_release,
)
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    QBIT_FORGET_PACK,
    QBIT_RESELECT,
)
from arc.services.acquisition.qbit import (
    MAX_TORRENT_FILES,
    QBIT_AWAITING_PICK,
    QBIT_CANCELLED,
    QBIT_UNREADABLE,
    FileInfo,
    QbitUnavailable,
)
from arc.services.acquisition.rules import BYTES_PER_GB, PAUSED_KEY
from arc.services.acquisition.states import transition
from arc.services.media.names import REMUX, REMUX_LIVE
from tests.acquisition_helpers import fake_free_space, make_anime, make_episodes, set_setting
from tests.conftest import ADMIN_EMAIL, add_user, api_transport, login
from tests.test_acquisition_jobs import (
    BATCH_HASH,
    BATCH_TITLE,
    SECOND_HASH,
    TWO_PACKS,
    Wired,
    _finished,
    _pack_names,
    context,
    queued,
)

pytestmark = pytest.mark.pg

#: Two files claim episode 7: a v1 beside a v2. A guess is not made.
AMBIGUOUS = [
    "Sousou no Frieren/[Erai-raws] Sousou no Frieren - 06 [1080p].mkv",
    "Sousou no Frieren/[Erai-raws] Sousou no Frieren - 07 [1080p].mkv",
    "Sousou no Frieren/[Erai-raws] Sousou no Frieren - 07v2 [1080p].mkv",
    "Sousou no Frieren/Sousou no Frieren.nfo",
]


# --- Pure rules -------------------------------------------------------------


def test_pick_eligibility_a_permanent_identification_refusal_qualifies() -> None:
    for reason in (
        "no file in the batch is episode 7",
        "2 files claim episode 7: a.mkv, b.mkv",
    ):
        assert pack_picks.identification_refusal(reason)
        assert pack_picks.pick_eligible(reason, permanent=True)


def test_pick_eligibility_too_many_files_never_qualifies() -> None:
    reason = f"{MAX_TORRENT_FILES + 1} files, over {MAX_TORRENT_FILES}"
    assert not pack_picks.identification_refusal(reason)
    assert not pack_picks.pick_eligible(reason, permanent=True)
    for other in (None, "", "the client listed no files", "file 3 was asked for and is not"):
        assert not pack_picks.pick_eligible(other, permanent=True)


def test_pick_eligibility_a_transient_refusal_never_qualifies() -> None:
    """A refusal that may change tomorrow is not a pack to hold for a person."""
    reason = "no file in the batch is episode 7"
    assert not pack_picks.pick_eligible(reason, permanent=False)


def test_gone_from_the_client_needs_two_misses_an_hour_apart() -> None:
    now = datetime.now(UTC)
    hash_ = "ab" * 20
    other = {"cd" * 20}
    gone_from = pack_picks.gone_from
    Sighting = pack_picks.Sighting

    # An unknown listing (unreachable, or empty) changes nothing either way.
    assert gone_from(hash_, absent_since=None, now=now, live=None) == Sighting(False, None)
    assert gone_from(hash_, absent_since=None, now=now, live=set()) == Sighting(False, None)
    earlier = now - timedelta(hours=2)
    assert gone_from(hash_, absent_since=earlier, now=now, live=None) == Sighting(False, earlier)
    # Listed: kept, and an earlier miss is forgotten.
    assert gone_from(hash_, absent_since=None, now=now, live={hash_}) == Sighting(False, None)
    assert gone_from(hash_, absent_since=earlier, now=now, live={hash_}) == Sighting(False, None)
    # The first miss only stamps the moment.
    assert gone_from(hash_, absent_since=None, now=now, live=other) == Sighting(False, now)
    # A second miss under an hour later keeps waiting; an hour or more is gone.
    recent = now - timedelta(minutes=59)
    assert gone_from(hash_, absent_since=recent, now=now, live=other) == Sighting(False, recent)
    hour = now - pack_picks.GONE_AFTER
    assert gone_from(hash_, absent_since=hour, now=now, live=other) == Sighting(True, hour)
    # Case does not matter: the listing is lower-case, a stored hash may not be.
    assert gone_from(hash_.upper(), absent_since=None, now=now, live={hash_}) == Sighting(
        False, None
    )


def test_pick_candidates_are_the_video_files_with_the_parser_reading() -> None:
    anime = Anime(id=1, title_romaji="Sousou no Frieren", title_english=None, synonyms=None)
    files = [
        FileInfo(index=index, name=name, size=100 + index, priority=1, progress=0.0)
        for index, name in enumerate(AMBIGUOUS)
    ]
    candidates = batch_module.pick_candidates(anime, files)
    assert [candidate["file_index"] for candidate in candidates] == [0, 1, 2]
    assert [candidate["parsed_number"] for candidate in candidates] == [6, 7, 7]
    assert candidates[1] == {
        "file_index": 1,
        "path": AMBIGUOUS[1],
        "size": 101,
        "parsed_number": 7,
    }


def test_a_pick_goes_stale_after_d_days_and_never_before_a_day() -> None:
    now = datetime.now(UTC)
    assert pack_picks.stale(now - timedelta(days=21), now=now, period=timedelta(days=21))
    assert not pack_picks.stale(now - timedelta(days=20), now=now, period=timedelta(days=21))
    assert not pack_picks.stale(now - timedelta(hours=2), now=now, period=timedelta(0))
    assert pack_picks.stale(now - timedelta(days=1), now=now, period=timedelta(0))


# --- The refusal path, the pick, the dismiss (service + jobs) ---------------


async def _refused(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    anilist_id: int,
    email: str,
    names: list[str] | None = None,
) -> tuple[Wired, Episode, PackPick]:
    """A finished show whose only pack cannot say which file is episode 7."""
    wired, episode = await _finished(
        session, monkeypatch, tmp_path, anilist_id=anilist_id, email=email
    )
    wired.qbit.add_files(BATCH_HASH, names if names is not None else AMBIGUOUS)
    await search_release(context(session, wired.settings, {"episode_id": episode.id}))
    pick = await pack_picks.pending_for(session, episode.id)
    assert pick is not None
    wired.qbit.calls.clear()
    wired.qbit.priorities.clear()
    wired.qbit.started.clear()
    wired.qbit.stopped.clear()
    return wired, episode, pick


async def _pack(session: AsyncSession) -> Torrent:
    torrent = await session.scalar(select(Torrent).where(Torrent.info_hash == BATCH_HASH))
    assert torrent is not None
    return torrent


async def _rows(session: AsyncSession, torrent_id: int) -> list[TorrentFile]:
    rows = await session.scalars(
        select(TorrentFile)
        .where(TorrentFile.torrent_id == torrent_id)
        .order_by(TorrentFile.file_index)
    )
    return list(rows.all())


async def test_refusal_keeps_the_pack_stopped_and_opens_a_pending_pick(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=963700, email="pick1@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, AMBIGUOUS)

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    # Nothing fetched, nothing deleted: every file off, never started.
    assert wired.qbit.deleted == [], "no delete_files for the pack a person will read"
    assert wired.qbit.started == []
    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, [0, 1, 2, 3])
    ]
    torrent = await _pack(db_session)
    assert torrent.kind is TorrentKind.BATCH
    assert torrent.qbit_state == QBIT_AWAITING_PICK
    rows = await _rows(db_session, torrent.id)
    assert [row.file_index for row in rows] == [0, 1, 2, 3], "one row per file"
    assert not any(row.wanted for row in rows)
    episode_six = await db_session.scalar(
        select(Episode.id).where(Episode.anime_id == episode.anime_id, Episode.number == 6)
    )
    assert rows[0].episode_id == episode_six, "the parser's unambiguous reading"
    assert rows[1].episode_id is None and rows[2].episode_id is None, "ambiguity maps to none"

    pick = await pack_picks.pending_for(db_session, episode.id)
    assert pick is not None
    assert pick.torrent_id == torrent.id
    assert pick.reason.startswith("2 files claim episode 7")
    assert [candidate["file_index"] for candidate in pick.candidates] == [0, 1, 2]
    assert [candidate["parsed_number"] for candidate in pick.candidates] == [6, 7, 7]
    assert all("/data" not in candidate["path"] for candidate in pick.candidates)

    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == PACK_PICK_REASON


async def test_refusal_for_too_many_files_opens_no_pick(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await _finished(
        db_session, monkeypatch, tmp_path, anilist_id=963701, email="pick2@arc.test"
    )
    wired.qbit.add_files(BATCH_HASH, _pack_names(*range(1, MAX_TORRENT_FILES + 2)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert await pack_picks.pending_for(db_session, episode.id) is None
    assert (await _pack(db_session)).qbit_state == QBIT_UNREADABLE
    assert wired.qbit.deleted == [{"hashes": BATCH_HASH, "deleteFiles": "true"}]


async def test_a_revival_with_a_pick_pending_rests_on_it_and_opens_no_second(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963702, email="pick3@arc.test"
    )
    transition(episode, EpisodeState.WANTED, reason="revived")
    await db_session.flush()

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}, job_id=2))

    picks = (await db_session.scalars(select(PackPick))).all()
    assert [row.id for row in picks] == [pick.id]
    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == PACK_PICK_REASON
    assert wired.qbit.uploaded[1:] == [], "the held pack is not fetched again"


async def test_pick_claims_the_file_reselects_and_starts_the_pack(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963703, email="pick4@arc.test"
    )
    user = await db_session.scalar(select(User).where(User.email == "pick4@arc.test"))
    assert user is not None

    await pack_picks.pick(db_session, pick.id, file_index=2, user_id=user.id, settings=None)

    torrent = await _pack(db_session)
    assert torrent.qbit_state not in (QBIT_AWAITING_PICK, QBIT_CANCELLED)
    rows = await _rows(db_session, torrent.id)
    claimed = [row for row in rows if row.wanted]
    assert [(row.file_index, row.episode_id) for row in claimed] == [(2, episode.id)]
    assert episode.state is EpisodeState.DOWNLOADING
    assert pick.resolution == PACK_PICK_PICKED
    assert pick.picked_file_index == 2
    assert pick.resolved_by == user.id and pick.resolved_at is not None
    assert len(await queued(db_session, QBIT_RESELECT)) == 1

    await qbit_reselect(
        context(db_session, wired.settings, {"torrent_id": torrent.id}, job_type=QBIT_RESELECT)
    )

    assert [(call["priority"], call["indices"]) for call in wired.qbit.priorities] == [
        (0, [0, 1, 3]),
        (1, [2]),
    ]
    assert wired.qbit.started == [BATCH_HASH]
    assert wired.qbit.deleted == []


async def test_dismiss_tombstones_the_pack_and_deletes_it_with_its_files(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963704, email="pick5@arc.test"
    )

    await pack_picks.dismiss(db_session, pick.id, user_id=None)

    torrent = await _pack(db_session)
    assert torrent.qbit_state == QBIT_UNREADABLE
    assert await _rows(db_session, torrent.id) == [], "a tombstone has no file rows"
    assert pick.resolution == PACK_PICK_DISMISSED and pick.resolved_at is not None
    assert episode.state is EpisodeState.UNAVAILABLE
    assert episode.unavailable_reason == batch_module.UNREADABLE_BATCH
    jobs = await queued(db_session, QBIT_FORGET_PACK)
    assert [job.payload["torrent_id"] for job in jobs] == [torrent.id]

    await qbit_forget_pack(
        context(db_session, wired.settings, {"torrent_id": torrent.id}, job_type=QBIT_FORGET_PACK)
    )

    assert wired.qbit.deleted == [{"hashes": BATCH_HASH, "deleteFiles": "true"}]
    assert wired.qbit.torrents == []


async def test_forget_never_deletes_a_pack_that_is_not_a_tombstone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _episode, _pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963705, email="pick6@arc.test"
    )
    torrent = await _pack(db_session)

    await qbit_forget_pack(
        context(db_session, wired.settings, {"torrent_id": torrent.id}, job_type=QBIT_FORGET_PACK)
    )

    assert wired.qbit.deleted == []


async def test_pick_on_a_withdrawn_want_is_refused_and_changes_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963706, email="pick7@arc.test"
    )
    await db_session.execute(
        update(Want).where(Want.episode_id == episode.id).values(dropped_at=datetime.now(UTC))
    )

    with pytest.raises(pack_picks.PickConflict) as caught:
        await pack_picks.pick(db_session, pick.id, file_index=1, user_id=None, settings=None)

    assert caught.value.code == pack_picks.NOT_WANTED
    assert pick.resolved_at is None
    assert (await _pack(db_session)).qbit_state == QBIT_AWAITING_PICK
    assert not any(row.wanted for row in await _rows(db_session, pick.torrent_id))
    assert episode.state is EpisodeState.UNAVAILABLE


async def test_a_second_pick_on_a_resolved_pick_is_refused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wired, _episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963707, email="pick8@arc.test"
    )
    await pack_picks.pick(db_session, pick.id, file_index=1, user_id=None, settings=None)

    with pytest.raises(pack_picks.PickConflict) as caught:
        await pack_picks.pick(db_session, pick.id, file_index=2, user_id=None, settings=None)
    assert caught.value.code == pack_picks.NOT_PENDING
    with pytest.raises(pack_picks.PickConflict):
        await pack_picks.dismiss(db_session, pick.id, user_id=None)
    assert pick.picked_file_index == 1


async def test_a_picked_file_lands_linked_as_confirmed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963708, email="pick9@arc.test"
    )
    await pack_picks.pick(db_session, pick.id, file_index=2, user_id=None, settings=None)
    torrent = await _pack(db_session)
    await qbit_reselect(
        context(db_session, wired.settings, {"torrent_id": torrent.id}, job_type=QBIT_RESELECT)
    )
    video = tmp_path / "downloads" / "batch" / BATCH_HASH / AMBIGUOUS[2]
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x" * 4096)
    for row in wired.qbit.file_lists[BATCH_HASH]:
        if row["index"] == 2:
            row["progress"] = 1.0

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    media = await db_session.scalar(select(MediaFile).where(MediaFile.path == str(video.resolve())))
    assert media is not None
    assert media.episode_id == episode.id
    assert media.review_state is ReviewState.CONFIRMED
    assert episode.state is EpisodeState.MATCHED


async def test_pending_out_awaiting_pick_follows_the_pick(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The flag ``/play`` sends is the pending row itself (checked over HTTP below)."""
    _wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963709, email="pick10@arc.test"
    )
    assert await pack_picks.awaiting_ids(db_session, [episode.id]) == {episode.id}
    await pack_picks.dismiss(db_session, pick.id, user_id=None)
    assert await pack_picks.awaiting_ids(db_session, [episode.id]) == set()


# --- Reconciliation -----------------------------------------------------------


async def _compute(session: AsyncSession, wired: Wired) -> None:
    await compute_wants(context(session, wired.settings, {}, job_type=COMPUTE_WANTS))


async def test_a_stale_pick_is_dismissed_after_d_days(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963710, email="pick11@arc.test"
    )
    await set_setting(db_session, "unwatched_days_d", 5)

    await _compute(db_session, wired)
    assert pick.resolved_at is None, "younger than D"

    pick.created_at = datetime.now(UTC) - timedelta(days=5, minutes=1)
    await db_session.flush()
    await _compute(db_session, wired)

    assert pick.resolution == PACK_PICK_DISMISSED
    assert (await _pack(db_session)).qbit_state == QBIT_UNREADABLE
    assert episode.unavailable_reason == batch_module.UNREADABLE_BATCH
    assert len(await queued(db_session, QBIT_FORGET_PACK)) == 1

    # Idempotent: a second pass finds nothing to do.
    await _compute(db_session, wired)
    assert len(await queued(db_session, QBIT_FORGET_PACK)) == 1


async def test_a_pack_gone_from_the_client_is_reconciled(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963711, email="pick12@arc.test"
    )
    await _compute(db_session, wired)
    assert pick.resolved_at is None, "still in the client"
    assert pick.absent_since is None

    # The client lists something else: the first miss is only noted.
    listed = wired.qbit.torrents[0]
    other = {**listed, "hash": "e" * 40}
    wired.qbit.torrents = [other]
    before = datetime.now(UTC)
    await _compute(db_session, wired)
    assert pick.resolved_at is None, "absent once: kept"
    assert pick.absent_since is not None and pick.absent_since >= before
    first_miss = pick.absent_since

    # Missing again minutes later: still kept, and the first miss stands.
    await _compute(db_session, wired)
    assert pick.resolved_at is None, "absent twice, but not an hour apart"
    assert pick.absent_since == first_miss

    # Missing again an hour after the first miss: gone.
    pick.absent_since = datetime.now(UTC) - pack_picks.GONE_AFTER - timedelta(minutes=1)
    await db_session.flush()
    await _compute(db_session, wired)

    assert pick.resolution == PACK_PICK_DISMISSED
    assert (await _pack(db_session)).qbit_state == QBIT_UNREADABLE
    assert episode.unavailable_reason == batch_module.UNREADABLE_BATCH


async def test_a_pack_listed_again_between_two_misses_resets_the_clock(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963723, email="gone2@arc.test"
    )
    listed = wired.qbit.torrents[0]
    other = {**listed, "hash": "e" * 40}

    # Missed once, long enough ago that a second miss would dismiss it ...
    wired.qbit.torrents = [other]
    await _compute(db_session, wired)
    assert pick.absent_since is not None
    pick.absent_since = datetime.now(UTC) - timedelta(hours=3)
    await db_session.flush()

    # ... but the client lists it again in between: the miss is forgotten.
    wired.qbit.torrents = [listed, other]
    await _compute(db_session, wired)
    assert pick.resolved_at is None
    assert pick.absent_since is None

    # So the next miss is a first miss again, not a second.
    wired.qbit.torrents = [other]
    await _compute(db_session, wired)
    assert pick.resolved_at is None, "a fresh first miss"
    assert pick.absent_since is not None
    assert (await _pack(db_session)).qbit_state == QBIT_AWAITING_PICK


async def test_an_unknown_listing_neither_confirms_nor_clears_a_miss(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963724, email="gone3@arc.test"
    )
    stamped = datetime.now(UTC) - timedelta(hours=3)
    pick.absent_since = stamped
    await db_session.flush()

    wired.qbit.torrents = []  # a client that has just restarted lists nothing
    await _compute(db_session, wired)

    assert pick.resolved_at is None
    assert pick.absent_since == stamped


async def test_a_pick_the_listing_was_not_asked_about_is_not_judged_missing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pick opened after the listing was read is unknown this pass, not missing."""
    wired, _episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963725, email="gone4@arc.test"
    )
    pick.absent_since = datetime.now(UTC) - timedelta(hours=3)
    await db_session.flush()
    stamped = pick.absent_since

    found = await pack_picks.reconcile(
        db_session,
        now=datetime.now(UTC),
        period=timedelta(days=21),
        live_hashes={"e" * 40},
        listed_for=[],
    )

    assert found == pack_picks.Reconciled()
    assert pick.resolved_at is None
    assert pick.absent_since == stamped


async def test_an_empty_client_listing_dismisses_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963714, email="pick15@arc.test"
    )
    pick.created_at = datetime.now(UTC) - timedelta(hours=2)
    await db_session.flush()
    wired.qbit.torrents = []

    await _compute(db_session, wired)

    assert pick.resolved_at is None
    assert (await _pack(db_session)).qbit_state == QBIT_AWAITING_PICK


async def test_an_unreachable_client_leaves_a_young_pick_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963712, email="pick13@arc.test"
    )
    wired.qbit.torrents = []
    wired.qbit.password = "wrong"
    wired.qbit.logged_in = False

    await _compute(db_session, wired)

    assert pick.resolved_at is None


async def test_the_batch_poll_still_skips_a_pack_awaiting_a_pick(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _episode, _pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963713, email="pick14@arc.test"
    )

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    assert (await _pack(db_session)).qbit_state == QBIT_AWAITING_PICK
    assert not any(call.endswith("/torrents/files") for call in wired.qbit.calls)
    assert wired.qbit.started == []

    wired.qbit.torrents = []
    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))
    assert (await _pack(db_session)).qbit_state == QBIT_AWAITING_PICK, "not called missing"


# --- The routes ---------------------------------------------------------------

USER_EMAIL = "pack-viewer@arc.test"
USER_PASSWORD = "viewer-password"


@pytest.fixture
async def user_client(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    async with api_transport(api_app) as client:
        yield await login(client, USER_EMAIL, USER_PASSWORD)


async def _seed(factory: SessionFactory, *, email: str = ADMIN_EMAIL) -> tuple[int, int, int]:
    """A pending pick on episode 7, wanted by ``email``: (pick, episode, torrent)."""
    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        anime = await make_anime(session, anilist_id=963790, status="FINISHED", episodes=12)
        episodes = await make_episodes(session, anime, 12)
        episode = episodes[6]
        episode.state = EpisodeState.UNAVAILABLE
        episode.unavailable_reason = PACK_PICK_REASON
        session.add(Want(user_id=user.id, episode_id=episode.id))
        torrent = Torrent(
            kind=TorrentKind.BATCH,
            episode_id=None,
            info_hash=BATCH_HASH,
            magnet=f"magnet:?xt=urn:btih:{BATCH_HASH}",
            title=BATCH_TITLE,
            save_path=f"/data/downloads/batch/{BATCH_HASH}",
            qbit_state=QBIT_AWAITING_PICK,
        )
        session.add(torrent)
        await session.flush()
        for index, name in enumerate(AMBIGUOUS):
            session.add(
                TorrentFile(
                    torrent_id=torrent.id,
                    file_index=index,
                    path=name,
                    size=1000,
                    episode_id=episodes[5].id if index == 0 else None,
                    wanted=False,
                    priority=0,
                )
            )
        pick = PackPick(
            torrent_id=torrent.id,
            episode_id=episode.id,
            reason="2 files claim episode 7: …",
            candidates=[
                {"file_index": 0, "path": AMBIGUOUS[0], "size": 1000, "parsed_number": 6},
                {"file_index": 1, "path": AMBIGUOUS[1], "size": 1000, "parsed_number": 7},
                {"file_index": 2, "path": AMBIGUOUS[2], "size": 1000, "parsed_number": 7},
            ],
        )
        session.add(pick)
        await session.commit()
        return pick.id, episode.id, torrent.id


async def _reload_pick(factory: SessionFactory, pick_id: int) -> PackPick:
    async with factory() as session:
        row = await session.get(PackPick, pick_id)
        assert row is not None
        return row


async def test_get_packs_lists_pending_picks_with_candidates(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    pick_id, episode_id, _ = await _seed(api_factory)

    response = await admin_client.get("/api/review/packs")

    assert response.status_code == 200
    [row] = response.json()
    assert row["id"] == pick_id
    assert row["episode"]["id"] == episode_id and row["episode"]["number"] == 7
    assert row["anime"]["title"]["romaji"] == "Sousou no Frieren"
    assert row["reason"].startswith("2 files claim episode 7")
    assert row["title"] == BATCH_TITLE
    assert [c["parsed_number"] for c in row["candidates"]] == [6, 7, 7]
    assert all(not c["path"].startswith("/") for c in row["candidates"])

    summary = (await admin_client.get("/api/review/summary")).json()
    assert summary == {"pending": 0, "packs": 1}


async def test_pick_route_claims_the_file_and_a_second_pick_is_409(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    pick_id, episode_id, torrent_id = await _seed(api_factory)

    response = await admin_client.post(f"/api/review/packs/{pick_id}/pick", json={"file_index": 2})
    assert response.status_code == 204, response.text

    async with api_factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None and episode.state is EpisodeState.DOWNLOADING
        claimed = (
            await session.scalars(
                select(TorrentFile).where(
                    TorrentFile.torrent_id == torrent_id, TorrentFile.wanted.is_(True)
                )
            )
        ).all()
        assert [(row.file_index, row.episode_id) for row in claimed] == [(2, episode_id)]
    assert (await _reload_pick(api_factory, pick_id)).resolution == PACK_PICK_PICKED
    assert (await admin_client.get("/api/review/packs")).json() == []

    again = await admin_client.post(f"/api/review/packs/{pick_id}/pick", json={"file_index": 1})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == pack_picks.NOT_PENDING


async def test_pick_route_on_a_withdrawn_want_is_409_and_changes_nothing(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    pick_id, episode_id, torrent_id = await _seed(api_factory)
    async with api_factory() as session:
        await session.execute(
            update(Want).where(Want.episode_id == episode_id).values(dropped_at=datetime.now(UTC))
        )
        await session.commit()

    response = await admin_client.post(f"/api/review/packs/{pick_id}/pick", json={"file_index": 2})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == pack_picks.NOT_WANTED
    assert (await _reload_pick(api_factory, pick_id)).resolved_at is None
    async with api_factory() as session:
        torrent = await session.get(Torrent, torrent_id)
        assert torrent is not None and torrent.qbit_state == QBIT_AWAITING_PICK
        episode = await session.get(Episode, episode_id)
        assert episode is not None and episode.state is EpisodeState.UNAVAILABLE


async def test_pick_route_refuses_a_file_that_is_not_a_candidate(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    pick_id, _, _ = await _seed(api_factory)
    response = await admin_client.post(f"/api/review/packs/{pick_id}/pick", json={"file_index": 3})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == pack_picks.NOT_A_CANDIDATE


async def test_dismiss_route_tombstones_the_pack(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    pick_id, episode_id, torrent_id = await _seed(api_factory)

    response = await admin_client.post(f"/api/review/packs/{pick_id}/dismiss")

    assert response.status_code == 204, response.text
    assert (await _reload_pick(api_factory, pick_id)).resolution == PACK_PICK_DISMISSED
    async with api_factory() as session:
        torrent = await session.get(Torrent, torrent_id)
        assert torrent is not None and torrent.qbit_state == QBIT_UNREADABLE
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        assert episode.unavailable_reason == batch_module.UNREADABLE_BATCH
    again = await admin_client.post(f"/api/review/packs/{pick_id}/dismiss")
    assert again.status_code == 409


async def test_unknown_pick_is_404(admin_client: AsyncClient) -> None:
    assert (await admin_client.post("/api/review/packs/999999/dismiss")).status_code == 404


async def test_pack_routes_are_admin_only(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    pick_id, _, _ = await _seed(api_factory, email=USER_EMAIL)

    assert (await user_client.get("/api/review/packs")).status_code == 403
    pick = await user_client.post(f"/api/review/packs/{pick_id}/pick", json={"file_index": 1})
    assert pick.status_code == 403
    assert (await user_client.post(f"/api/review/packs/{pick_id}/dismiss")).status_code == 403
    assert (await user_client.get("/api/review/summary")).json()["packs"] == 0
    assert (await _reload_pick(api_factory, pick_id)).resolved_at is None


async def test_pack_routes_require_an_origin(
    api_app: FastAPI, admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    pick_id, _, _ = await _seed(api_factory)
    async with api_transport(api_app, origin=None) as bare:
        bare.cookies = admin_client.cookies
        pick = await bare.post(f"/api/review/packs/{pick_id}/pick", json={"file_index": 1})
        dismiss = await bare.post(f"/api/review/packs/{pick_id}/dismiss")
    assert pick.status_code == 403 and dismiss.status_code == 403
    assert pick.json() == {"detail": "origin not allowed"}
    assert (await _reload_pick(api_factory, pick_id)).resolved_at is None


async def test_play_says_awaiting_pick_while_the_pick_is_pending(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    pick_id, episode_id, _ = await _seed(api_factory)

    pending = await admin_client.get(f"/api/episodes/{episode_id}/play")
    assert pending.status_code == 200, pending.text
    assert pending.json()["pending"]["awaiting_pick"] is True
    assert pending.json()["episode"]["awaiting_pick"] is True

    assert (await admin_client.post(f"/api/review/packs/{pick_id}/dismiss")).status_code == 204
    after = (await admin_client.get(f"/api/episodes/{episode_id}/play")).json()
    assert after["pending"]["awaiting_pick"] is False
    assert after["episode"]["awaiting_pick"] is False


# --- Fix round 1 ---------------------------------------------------------------


async def _nothing_moved(session: AsyncSession, episode: Episode, pick: PackPick) -> None:
    assert pick.resolved_at is None
    assert (await _pack(session)).qbit_state == QBIT_AWAITING_PICK
    assert not any(row.wanted for row in await _rows(session, pick.torrent_id))
    assert episode.state is EpisodeState.UNAVAILABLE
    assert await queued(session, QBIT_RESELECT) == []


async def test_a_pick_while_paused_is_refused_and_writes_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963720, email="fix1@arc.test"
    )
    await set_setting(db_session, PAUSED_KEY, True)

    with pytest.raises(pack_picks.PickConflict) as caught:
        await pack_picks.pick(
            db_session, pick.id, file_index=1, user_id=None, settings=wired.settings
        )

    assert caught.value.code == ACQUISITION_PAUSED and caught.value.status == 409
    await _nothing_moved(db_session, episode, pick)


async def test_a_pick_while_storage_is_held_is_refused_and_writes_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963721, email="fix2@arc.test"
    )
    fake_free_space(monkeypatch, acquisition_rules, 1 * BYTES_PER_GB)

    with pytest.raises(pack_picks.PickConflict) as caught:
        await pack_picks.pick(
            db_session, pick.id, file_index=1, user_id=None, settings=wired.settings
        )

    assert caught.value.code == STORAGE_HELD
    await _nothing_moved(db_session, episode, pick)


async def test_the_pick_reconciliation_lists_the_client_before_the_want_lock(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The listing (an HTTP call) is read before ``lock_wants`` is taken, and
    the pick rows are locked only after it: the routes' order, the want lock
    then a pick row, and the global lock never held across the client call."""
    wired, _episode, _pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963722, email="fix3@arc.test"
    )
    order: list[str] = []
    real_lock = acquisition_jobs.lock_wants
    real_hashes = pack_picks.awaiting_hashes
    real_reconcile = pack_picks.reconcile
    real_torrents = acquisition_jobs.QbitClient.torrents

    async def lock(session: AsyncSession) -> None:
        order.append("lock_wants")
        await real_lock(session)

    async def hashes(session: AsyncSession) -> list[str]:
        order.append("pick hashes")
        return await real_hashes(session)

    async def torrents(self: object, *args: object, **kwargs: object) -> object:
        order.append("listing")
        return await real_torrents(self, *args, **kwargs)  # type: ignore[arg-type]

    async def reconcile(session: AsyncSession, **kwargs: object) -> pack_picks.Reconciled:
        order.append("pick rows")
        return await real_reconcile(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(acquisition_jobs, "lock_wants", lock)
    monkeypatch.setattr(pack_picks, "awaiting_hashes", hashes)
    monkeypatch.setattr(acquisition_jobs.QbitClient, "torrents", torrents)
    monkeypatch.setattr(pack_picks, "reconcile", reconcile)

    await _compute(db_session, wired)

    assert order[:4] == ["pick hashes", "listing", "lock_wants", "pick rows"], order


async def test_a_picked_pack_offers_no_other_reading_to_a_later_want(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the person's answer stays: "- 08" is not claimable as episode 8."""
    _wired, episode, pick = await _refused(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=963723,
        email="fix4@arc.test",
        names=_pack_names(8, 9),
    )
    rows = await _rows(db_session, pick.torrent_id)
    assert rows[0].episode_id is not None, "the parser read '- 08' as episode 8"
    assert [c["parsed_number"] for c in pick.candidates] == [8, 9]

    await pack_picks.pick(db_session, pick.id, file_index=1, user_id=None, settings=None)

    rows = await _rows(db_session, pick.torrent_id)
    assert [(row.file_index, row.episode_id) for row in rows] == [(0, None), (1, episode.id)]
    assert [c["parsed_number"] for c in pick.candidates] == [8, 9], "kept for display"
    eighth = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 8)
    )
    assert eighth is not None
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    eighth.state = EpisodeState.SEARCHING
    db_session.add(Want(user_id=want.user_id, episode_id=eighth.id))
    await db_session.flush()

    assert await batch_module.claim_existing(db_session, eighth) is None


async def test_a_search_that_fails_after_holding_a_pack_takes_it_out_of_the_client(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=963724,
        email="fix5@arc.test",
        pool=TWO_PACKS,
    )
    wired.qbit.add_files(BATCH_HASH, AMBIGUOUS)
    wired.qbit.add_files(SECOND_HASH, _pack_names(*range(1, 13)))
    real = acquisition_jobs._add_stopped
    calls = 0

    async def flaky(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise QbitUnavailable("the client went away mid-search")
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(acquisition_jobs, "_add_stopped", flaky)

    with pytest.raises(QbitUnavailable):
        await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert {"hashes": BATCH_HASH, "deleteFiles": "true"} in wired.qbit.deleted
    assert all(t["hash"].lower() != BATCH_HASH for t in wired.qbit.torrents)
    assert (await db_session.scalars(select(PackPick))).all() == []


async def test_a_held_pack_is_tombstoned_when_a_later_pack_is_started(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode = await _finished(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=963725,
        email="fix6@arc.test",
        pool=TWO_PACKS,
    )
    wired.qbit.add_files(BATCH_HASH, AMBIGUOUS)
    wired.qbit.add_files(SECOND_HASH, _pack_names(*range(1, 13)))

    await search_release(context(db_session, wired.settings, {"episode_id": episode.id}))

    assert wired.qbit.started == [SECOND_HASH]
    assert wired.qbit.deleted == [{"hashes": BATCH_HASH, "deleteFiles": "true"}]
    assert (await _pack(db_session)).qbit_state == QBIT_UNREADABLE
    assert (await db_session.scalars(select(PackPick))).all() == []
    assert episode.state is EpisodeState.DOWNLOADING


async def _forgets(session: AsyncSession) -> int:
    return len(await queued(session, QBIT_FORGET_PACK))


async def test_a_pick_nobody_wants_any_more_is_dismissed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963726, email="fix7@arc.test"
    )
    await db_session.execute(
        update(Want).where(Want.episode_id == episode.id).values(dropped_at=datetime.now(UTC))
    )

    await _compute(db_session, wired)
    assert pick.resolution == PACK_PICK_DISMISSED
    assert await _forgets(db_session) == 1
    await _compute(db_session, wired)
    assert await _forgets(db_session) == 1, "idempotent"


async def test_a_pick_whose_episode_came_as_a_single_is_dismissed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963727, email="fix8@arc.test"
    )
    transition(episode, EpisodeState.WANTED, reason="revived")
    transition(episode, EpisodeState.SEARCHING, reason="revived")
    await _compute(db_session, wired)
    assert pick.resolved_at is None, "a search in flight is not a reason"

    db_session.add(
        Torrent(
            kind=TorrentKind.SINGLE,
            episode_id=episode.id,
            info_hash="f" * 40,
            magnet="magnet:?xt=urn:btih:" + "f" * 40,
            title="[SubsPlease] Sousou no Frieren - 07 (1080p)",
            qbit_state="downloading",
        )
    )
    transition(episode, EpisodeState.DOWNLOADING, reason="a single turned up")
    await db_session.flush()

    await _compute(db_session, wired)

    assert pick.resolution == PACK_PICK_DISMISSED
    assert (await _pack(db_session)).qbit_state == QBIT_UNREADABLE
    assert episode.state is EpisodeState.DOWNLOADING


async def test_a_pick_whose_episode_is_claimed_from_another_pack_is_dismissed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963728, email="fix9@arc.test"
    )
    other = Torrent(
        kind=TorrentKind.BATCH,
        episode_id=None,
        info_hash="9" * 40,
        magnet="magnet:?xt=urn:btih:" + "9" * 40,
        title="[Other] Sousou no Frieren [BATCH]",
        qbit_state="downloading",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        TorrentFile(
            torrent_id=other.id,
            file_index=0,
            path="x - 07.mkv",
            size=1,
            episode_id=episode.id,
            wanted=True,
        )
    )
    transition(episode, EpisodeState.WANTED, reason="revived")
    transition(episode, EpisodeState.SEARCHING, reason="revived")
    transition(episode, EpisodeState.DOWNLOADING, reason="attached")
    await db_session.flush()

    await _compute(db_session, wired)

    assert pick.resolution == PACK_PICK_DISMISSED


async def test_while_paused_a_stale_pick_is_not_dismissed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, _episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963729, email="fix10@arc.test"
    )
    pick.created_at = datetime.now(UTC) - timedelta(days=60)
    await set_setting(db_session, PAUSED_KEY, True)

    await _compute(db_session, wired)
    assert pick.resolved_at is None

    await set_setting(db_session, PAUSED_KEY, False)
    await _compute(db_session, wired)
    assert pick.resolution == PACK_PICK_DISMISSED


async def test_a_held_pack_does_not_make_its_show_thin(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The slow-swarm rule reads Arc's releases, not a pack nobody is fetching."""
    _wired, episode, _pick = await _refused(
        db_session,
        monkeypatch,
        tmp_path,
        anilist_id=963730,
        email="fix11@arc.test",
        names=_pack_names(5, 6, 8),
    )
    assert any(row.episode_id for row in await _rows(db_session, _pick.torrent_id))

    thin = await wants_module._thin_shows(db_session, [episode.anime_id], below=1000)

    assert episode.anime_id not in thin


async def test_two_picks_on_one_pack_the_second_finds_it_live(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963731, email="fix12@arc.test"
    )
    eighth = await db_session.scalar(
        select(Episode).where(Episode.anime_id == episode.anime_id, Episode.number == 8)
    )
    assert eighth is not None
    want = await db_session.scalar(select(Want).where(Want.episode_id == episode.id))
    assert want is not None
    eighth.state = EpisodeState.UNAVAILABLE
    db_session.add(Want(user_id=want.user_id, episode_id=eighth.id))
    second = PackPick(
        torrent_id=pick.torrent_id,
        episode_id=eighth.id,
        reason="no file in the batch is episode 8",
        candidates=pick.candidates,
    )
    db_session.add(second)
    await db_session.flush()

    await pack_picks.pick(db_session, pick.id, file_index=1, user_id=None, settings=None)
    with pytest.raises(pack_picks.PickConflict) as caught:
        await pack_picks.pick(db_session, second.id, file_index=2, user_id=None, settings=None)
    assert caught.value.code == pack_picks.NOT_WAITING

    await pack_picks.dismiss(db_session, second.id, user_id=None)

    assert second.resolution == PACK_PICK_DISMISSED
    assert (await _pack(db_session)).qbit_state == pack_picks.LIVE_AFTER_PICK
    assert [row.file_index for row in await _rows(db_session, pick.torrent_id) if row.wanted] == [1]
    assert await _forgets(db_session) == 0


async def test_a_pick_on_an_episode_that_moved_on_is_refused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963732, email="fix13@arc.test"
    )
    transition(episode, EpisodeState.WANTED, reason="revived")
    transition(episode, EpisodeState.SEARCHING, reason="revived")
    transition(episode, EpisodeState.DOWNLOADING, reason="a single turned up")
    await db_session.flush()

    with pytest.raises(pack_picks.PickConflict) as caught:
        await pack_picks.pick(db_session, pick.id, file_index=1, user_id=None, settings=None)

    assert caught.value.code == pack_picks.MOVED_ON
    assert pick.resolved_at is None


async def test_a_landed_pick_queues_the_remux_and_selects_nothing_else(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wired, episode, pick = await _refused(
        db_session, monkeypatch, tmp_path, anilist_id=963733, email="fix14@arc.test"
    )
    await pack_picks.pick(db_session, pick.id, file_index=2, user_id=None, settings=None)
    torrent = await _pack(db_session)
    await qbit_reselect(
        context(db_session, wired.settings, {"torrent_id": torrent.id}, job_type=QBIT_RESELECT)
    )
    video = tmp_path / "downloads" / "batch" / BATCH_HASH / AMBIGUOUS[2]
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x" * 4096)
    for row in wired.qbit.file_lists[BATCH_HASH]:
        if row["index"] == 2:
            row["progress"] = 1.0

    await poll_qbit(context(db_session, wired.settings, {}, job_type="poll_qbit"))

    assert episode.state is EpisodeState.MATCHED
    selected = [row["index"] for row in wired.qbit.file_lists[BATCH_HASH] if row["priority"] > 0]
    assert selected == [2]
    assert [row.file_index for row in await _rows(db_session, torrent.id) if row.wanted] == [2]
    assert len(await queued(db_session, REMUX)) == 1
    assert await queued(db_session, REMUX_LIVE) == []


async def test_pick_route_refuses_while_paused(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    pick_id, _, torrent_id = await _seed(api_factory)
    async with api_factory() as session:
        await set_setting(session, PAUSED_KEY, True)
        await session.commit()

    response = await admin_client.post(f"/api/review/packs/{pick_id}/pick", json={"file_index": 1})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ACQUISITION_PAUSED
    assert (await _reload_pick(api_factory, pick_id)).resolved_at is None
    async with api_factory() as session:
        torrent = await session.get(Torrent, torrent_id)
        assert torrent is not None and torrent.qbit_state == QBIT_AWAITING_PICK


async def test_the_show_row_and_play_agree_on_awaiting_pick(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """One rule: a pending pick and the episode ``unavailable`` or ``wanted``."""
    _pick_id, episode_id, _ = await _seed(api_factory)
    async with api_factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        anime_id = episode.anime_id

    async def both() -> tuple[bool, bool]:
        show = (await admin_client.get(f"/api/anime/{anime_id}")).json()
        row = next(e for e in show["episodes"] if e["id"] == episode_id)
        play = (await admin_client.get(f"/api/episodes/{episode_id}/play")).json()
        return row["awaiting_pick"], play["pending"]["awaiting_pick"]

    async def set_state(state: EpisodeState) -> None:
        async with api_factory() as session:
            await session.execute(
                update(Episode).where(Episode.id == episode_id).values(state=state)
            )
            await session.commit()

    assert await both() == (True, True)
    await set_state(EpisodeState.WANTED)  # the daily revival's window
    assert await both() == (True, True)
    await set_state(EpisodeState.DOWNLOADING)  # a single turned up
    assert await both() == (False, False)
