"""Retention: what may be deleted, and what deleting it does (FR-T1, FR-T3).

Two halves, like the code. The first runs
:func:`arc.services.retention.sweep.candidates` against a frozen clock and
reads as the rule itself: who watched what, when, and whether the grace period
has run out. The second actually deletes, with qBittorrent stubbed, and checks
that the directories, the rows, the torrent and the episode's state all end up
where FR-T3 says they should.

The clock is frozen rather than waited on (:mod:`tests.retention_helpers`):
every timestamp is written relative to :data:`~tests.retention_helpers.NOW`,
and ``candidates(now=NOW)`` is what makes "eight days ago" mean eight days.
"""

from __future__ import annotations

import logging
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    EpisodeState,
    Job,
    JobStatus,
    ListStatus,
    MediaFile,
    Rendition,
    Torrent,
    Want,
)
from arc.services.acquisition import qbit as qbit_module
from arc.services.acquisition.names import SEARCH_RELEASE
from arc.services.acquisition.rules import PAUSED_KEY
from arc.services.acquisition.wants import (
    REASON_NOT_WANTING,
    STALE_DROP_REASON,
    compute_wants,
)
from arc.services.jobs.registry import JobContext
from arc.services.media.names import output_dir_for
from arc.services.retention.delete import delete_episode_files
from arc.services.retention.jobs import delete_files, retention_sweep
from arc.services.retention.names import DELETE_EPISODE_FILES, RETENTION_SWEEP
from arc.services.retention.rules import grace_days, unwatched_days
from arc.services.retention.sweep import (
    REASON_NO_FILES,
    RETAINED_STATES,
    candidates,
    retained_bytes,
    safe_path,
    targets_for_episode,
)
from tests.acquisition_helpers import (
    QbitStub,
    acquisition_settings,
    force_transport,
    make_entry,
    make_user,
    set_setting,
)
from tests.retention_helpers import (
    DAY,
    NOW,
    add_completion,
    add_want,
    days_ago,
    make_retained_episode,
    write_rendition_dir,
    write_source_file,
)

pytestmark = pytest.mark.pg

HASH = "b" * 40


def context(
    session: AsyncSession,
    settings: Settings,
    payload: dict[str, object] | None = None,
    *,
    job_type: str = RETENTION_SWEEP,
) -> JobContext:
    job = Job(type=job_type, payload=dict(payload or {}), status=JobStatus.RUNNING, attempts=1)
    job.id = 1
    return JobContext(
        job=job,
        session=session,
        settings=settings,
        log=logging.getLogger("arc.jobs.test"),
    )


async def rows_for(session: AsyncSession, model: type, episode_id: int) -> int:
    """How many rows of ``model`` still point at this episode."""
    found = await session.scalars(select(model).where(model.episode_id == episode_id))
    return len(list(found.all()))


def qbit_stub(monkeypatch: pytest.MonkeyPatch, *, holding: str | None = HASH) -> QbitStub:
    """A qBittorrent stub every client built from settings will talk to."""
    stub = QbitStub()
    if holding is not None:
        stub.add_torrent(holding, name="Retention Test - 07", progress=1.0, state="stalledUP")
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, stub.transport()),
    )
    return stub


# --- The rule (FR-T1) -------------------------------------------------------


async def test_the_settings_defaults_are_the_spec_defaults(db_session: AsyncSession) -> None:
    """G = 7 and D = 21 out of the box (FR-T1, FR-T2, FR-T5)."""
    assert await grace_days(db_session) == 7
    assert await unwatched_days(db_session) == 21


async def test_an_episode_one_user_has_not_finished_stays(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """The grace period runs from the *last* completion, not the first."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(db_session, settings, anilist_id=970001)
    alice = await make_user(db_session, "alice-t1@arc.test")
    bob = await make_user(db_session, "bob-t1@arc.test")
    await add_completion(db_session, alice, episode, at=days_ago(8))
    await add_completion(db_session, bob, episode, at=days_ago(2))

    assert await candidates(db_session, settings, now=NOW) == []


async def test_an_episode_everyone_finished_a_week_ago_goes(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(db_session, settings, anilist_id=970002)
    alice = await make_user(db_session, "alice-t2@arc.test")
    bob = await make_user(db_session, "bob-t2@arc.test")
    await add_completion(db_session, alice, episode, at=days_ago(8))
    await add_completion(db_session, bob, episode, at=days_ago(9))

    found = await candidates(db_session, settings, now=NOW)

    assert [target.episode_id for target in found] == [episode.id]
    assert "watched" in found[0].reason


async def test_a_dropped_want_and_a_completion_both_count(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """One user gave up, the other finished; both were eight days ago."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(db_session, settings, anilist_id=970003)
    alice = await make_user(db_session, "alice-t3@arc.test")
    bob = await make_user(db_session, "bob-t3@arc.test")
    await add_want(
        db_session, alice, episode, dropped_at=days_ago(8), drop_reason="unwatched for D days"
    )
    await add_completion(db_session, bob, episode, at=days_ago(8))

    found = await candidates(db_session, settings, now=NOW)

    assert [target.episode_id for target in found] == [episode.id]


async def test_a_want_dropped_this_morning_holds_the_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(db_session, settings, anilist_id=970004)
    alice = await make_user(db_session, "alice-t4@arc.test")
    bob = await make_user(db_session, "bob-t4@arc.test")
    await add_want(db_session, alice, episode, dropped_at=days_ago(0.5))
    await add_completion(db_session, bob, episode, at=days_ago(9))

    assert await candidates(db_session, settings, now=NOW) == []


@pytest.mark.parametrize(("age", "expected"), [(8, True), (6, False)])
async def test_an_episode_nobody_ever_wanted_is_judged_on_its_age(
    db_session: AsyncSession, tmp_path: Path, age: int, expected: bool
) -> None:
    """A manual drop: no wants, no watchers, so the file's own age decides."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970005 + age, ready_at=days_ago(age)
    )

    found = await candidates(db_session, settings, now=NOW)

    assert bool(found) is expected
    if expected:
        assert found[0].episode_id == episode.id
        assert "nobody" in found[0].reason


async def test_an_episode_somebody_still_wants_is_never_a_candidate(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970020, ready_at=days_ago(60)
    )
    alice = await make_user(db_session, "alice-t5@arc.test")
    bob = await make_user(db_session, "bob-t5@arc.test")
    await add_completion(db_session, alice, episode, at=days_ago(30))
    await add_want(db_session, bob, episode)  # live

    assert await candidates(db_session, settings, now=NOW) == []


@pytest.mark.parametrize(
    "state",
    [
        EpisodeState.PREPARING,
        EpisodeState.DOWNLOADING,
        EpisodeState.SEARCHING,
        EpisodeState.MATCHING,
    ],
)
async def test_an_episode_with_work_in_flight_is_never_a_candidate(
    db_session: AsyncSession, tmp_path: Path, state: EpisodeState
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=970030 + list(EpisodeState).index(state),
        state=state,
        ready_at=days_ago(60),
    )
    alice = await make_user(db_session, f"inflight-{state.value}@arc.test")
    await add_completion(db_session, alice, episode, at=days_ago(30))

    assert await candidates(db_session, settings, now=NOW) == []
    assert state not in RETAINED_STATES


@pytest.mark.parametrize(
    "state", [EpisodeState.DOWNLOADED, EpisodeState.MATCHED, EpisodeState.FAILED]
)
async def test_an_episode_that_never_reached_ready_is_still_swept(
    db_session: AsyncSession, tmp_path: Path, state: EpisodeState
) -> None:
    """FR-T1's "source + rendition": a download that stalled has bytes too."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=970040 + list(EpisodeState).index(state),
        state=state,
        ready_at=days_ago(20),
        with_rendition=False,
    )

    found = await candidates(db_session, settings, now=NOW)

    assert [target.episode_id for target in found] == [episode.id]


async def test_a_longer_grace_period_holds_everything_back(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """G is admin-editable (FR-T5), and the sweep reads it every run."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970050, ready_at=days_ago(8)
    )
    assert [target.episode_id for target in await candidates(db_session, settings, now=NOW)] == [
        episode.id
    ]

    await set_setting(db_session, "grace_days_g", 30)

    assert await candidates(db_session, settings, now=NOW) == []


async def test_the_preview_reports_the_bytes_and_the_paths(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970060, ready_at=days_ago(30), info_hash=HASH
    )

    (target,) = await candidates(db_session, settings, now=NOW)

    assert target.targets.rendition_dir == output_dir_for(settings, episode.id).resolve()
    assert target.targets.source_dir == (settings.downloads_dir / str(episode.id)).resolve()
    assert target.targets.torrent_hashes == (HASH,)
    assert target.bytes > 2048, "the source file plus the rendition's segments"


# --- Deleting (FR-T3) -------------------------------------------------------


async def test_deleting_removes_the_files_the_rows_and_the_torrent(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    stub = qbit_stub(monkeypatch)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970070, ready_at=days_ago(30), info_hash=HASH
    )
    rendition_dir = output_dir_for(settings, episode.id)
    source_dir = settings.downloads_dir / str(episode.id)

    await retention_sweep(context(db_session, settings))

    assert not rendition_dir.exists()
    assert not source_dir.exists()
    assert await rows_for(db_session, Rendition, episode.id) == 0
    assert await rows_for(db_session, MediaFile, episode.id) == 0
    assert await rows_for(db_session, Torrent, episode.id) == 0
    assert episode.state is EpisodeState.NOT_WANTED
    assert stub.deleted == [{"hashes": HASH, "deleteFiles": "true"}]


async def test_a_second_sweep_finds_nothing_left_to_do(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch)
    await make_retained_episode(
        db_session, settings, anilist_id=970080, ready_at=days_ago(30), info_hash=HASH
    )
    await retention_sweep(context(db_session, settings))

    assert await candidates(db_session, settings, now=NOW) == []
    await retention_sweep(context(db_session, settings))  # and it does not raise


async def test_a_missing_directory_is_not_an_error(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Somebody removed the rendition by hand; the row must still go."""
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970090, ready_at=days_ago(30)
    )
    shutil.rmtree(output_dir_for(settings, episode.id))

    await retention_sweep(context(db_session, settings))

    assert episode.state is EpisodeState.NOT_WANTED
    assert await rows_for(db_session, Rendition, episode.id) == 0


async def test_a_torrent_the_client_has_forgotten_is_tolerated(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client only deletes hashes in Arc's own category; the rest are skipped."""
    settings = acquisition_settings(tmp_path)
    stub = qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970100, ready_at=days_ago(30), info_hash=HASH
    )

    await retention_sweep(context(db_session, settings))

    assert stub.deleted == [], "nothing was sent for a hash the client does not hold"
    assert episode.state is EpisodeState.NOT_WANTED
    assert await rows_for(db_session, Torrent, episode.id) == 0


async def test_a_dry_run_deletes_nothing_at_all(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path, retention_dry_run=True)
    stub = qbit_stub(monkeypatch)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970110, ready_at=days_ago(30), info_hash=HASH
    )

    await retention_sweep(context(db_session, settings))

    assert output_dir_for(settings, episode.id).exists()
    assert (settings.downloads_dir / str(episode.id)).exists()
    assert stub.deleted == []
    assert episode.state is EpisodeState.READY
    assert await rows_for(db_session, Rendition, episode.id) == 1
    assert len(await candidates(db_session, settings, now=NOW)) == 1


async def test_a_path_outside_the_data_directories_is_refused(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edited ``renditions.dir`` must not turn into an ``rmtree``."""
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("mine", encoding="utf-8")

    episode = await make_retained_episode(
        db_session, settings, anilist_id=970120, ready_at=days_ago(30), with_rendition=False
    )
    db_session.add(
        Rendition(
            episode_id=episode.id,
            dir=str(outside),
            playlist_path=str(outside / "index.m3u8"),
            ready_at=days_ago(30),
        )
    )
    await db_session.flush()

    assert safe_path(outside, settings) is None
    (target,) = await candidates(db_session, settings, now=NOW)
    assert target.targets.rendition_dir is None

    await retention_sweep(context(db_session, settings))

    assert (outside / "keep.txt").exists(), "a directory outside DATA_DIR is never touched"
    assert episode.state is EpisodeState.NOT_WANTED


async def test_a_symlinked_episode_directory_is_refused(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Following a link is how a delete inside DATA_DIR removes something else."""
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=970130,
        ready_at=days_ago(30),
        with_rendition=False,
        with_source=False,
    )
    real = tmp_path / "somebody_elses_library"
    real.mkdir()
    (real / "precious.mkv").write_bytes(b"0" * 16)
    link = settings.downloads_dir / str(episode.id)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real, target_is_directory=True)
    write_rendition_dir(settings, episode.id)
    db_session.add(
        Rendition(
            episode_id=episode.id,
            dir=str(output_dir_for(settings, episode.id)),
            playlist_path="index.m3u8",
            ready_at=days_ago(30),
        )
    )
    await db_session.flush()

    (target,) = await candidates(db_session, settings, now=NOW)
    assert target.targets.source_dir is None, "the symlink is not a directory Arc may delete"

    await retention_sweep(context(db_session, settings))

    assert (real / "precious.mkv").exists()
    assert link.is_symlink(), "the link itself is left alone too"
    assert not output_dir_for(settings, episode.id).exists(), "the rendition still went"


async def test_a_manual_drop_has_its_own_file_deleted(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-T3: a file with no episode directory of its own goes individually."""
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970140, ready_at=days_ago(30), with_source=False
    )
    settings.manual_dir.mkdir(parents=True, exist_ok=True)
    dropped = settings.manual_dir / "Retention Test - 07.mkv"
    dropped.write_bytes(b"0" * 4096)
    db_session.add(
        MediaFile(
            episode_id=episode.id,
            path=str(dropped.resolve()),
            size=4096,
            created_at=days_ago(30),
        )
    )
    await db_session.flush()

    await retention_sweep(context(db_session, settings))

    assert not dropped.exists()
    assert await rows_for(db_session, MediaFile, episode.id) == 0


# --- The manual button (FR-T4) ----------------------------------------------


async def test_the_manual_job_deletes_inside_the_grace_period(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970150, ready_at=days_ago(0.5), info_hash=HASH
    )
    assert await candidates(db_session, settings, now=NOW) == [], "far inside the grace period"

    await delete_files(
        context(db_session, settings, {"episode_id": episode.id}, job_type=DELETE_EPISODE_FILES)
    )

    assert not output_dir_for(settings, episode.id).exists()
    assert episode.state is EpisodeState.NOT_WANTED


async def test_the_manual_job_refuses_an_episode_that_is_being_prepared(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=970160,
        state=EpisodeState.PREPARING,
        ready_at=days_ago(30),
    )

    await delete_files(
        context(db_session, settings, {"episode_id": episode.id}, job_type=DELETE_EPISODE_FILES)
    )

    assert output_dir_for(settings, episode.id).exists()
    assert episode.state is EpisodeState.PREPARING


async def test_targets_for_an_episode_with_nothing_on_disk_are_empty(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=970170,
        with_rendition=False,
        with_source=False,
    )

    targets = await targets_for_episode(db_session, settings, episode.id)

    assert targets.empty


# --- Re-acquisition (FR-T3) -------------------------------------------------


async def test_a_deleted_episode_is_acquired_again_when_somebody_wants_it(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-T3's second half: a rewind or a new user re-fetches the episode."""
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch)
    await set_setting(db_session, PAUSED_KEY, False)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970180, number=1, ready_at=days_ago(30), info_hash=HASH
    )
    await retention_sweep(context(db_session, settings))
    assert episode.state is EpisodeState.NOT_WANTED

    newcomer = await make_user(db_session, "newcomer@arc.test")
    anime = await db_session.get(Anime, episode.anime_id)
    assert anime is not None
    await make_entry(db_session, newcomer, anime, progress=0)

    result = await compute_wants(db_session, now=NOW)

    assert episode.state is EpisodeState.WANTED
    assert result.added == 1
    searches = (await db_session.scalars(select(Job).where(Job.type == SEARCH_RELEASE))).all()
    assert [job.payload["episode_id"] for job in searches] == [episode.id]


# --- Disk usage (FR-T4) -----------------------------------------------------


async def test_retained_bytes_counts_sources_and_renditions(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970190, ready_at=days_ago(1)
    )
    source = settings.downloads_dir / str(episode.id) / "episode.mkv"
    rendition = output_dir_for(settings, episode.id)

    total = await retained_bytes(db_session, settings)

    expected = source.stat().st_size + sum(
        path.stat().st_size for path in rendition.iterdir() if path.is_file()
    )
    assert total == expected


async def test_retained_bytes_measures_a_rendition_where_its_row_says_it_is(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A rendition written before ``DATA_DIR`` moved is still on the disk.

    ``renditions.dir`` and :func:`output_dir_for` disagree for anything from an
    older layout, and this number is what an admin reads before deciding what
    to delete: reporting the old directory as zero bytes would understate the
    disk by exactly the files that are hardest to find by hand.
    """
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=970195,
        ready_at=days_ago(1),
        with_rendition=False,
        with_source=False,
    )
    elsewhere = settings.renditions_dir / "old-layout" / str(episode.id)
    elsewhere.mkdir(parents=True)
    (elsewhere / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (elsewhere / "seg_00000.m4s").write_bytes(b"0" * 4096)
    db_session.add(
        Rendition(
            episode_id=episode.id,
            dir=str(elsewhere),
            playlist_path=str(elsewhere / "index.m3u8"),
            ready_at=days_ago(1),
        )
    )
    await db_session.flush()

    total = await retained_bytes(db_session, settings)

    assert total == sum(path.stat().st_size for path in elsewhere.iterdir())
    assert not output_dir_for(settings, episode.id).exists(), "the id-derived path is empty"


async def test_retained_bytes_ignores_an_episode_with_no_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    await make_retained_episode(
        db_session,
        settings,
        anilist_id=970200,
        state=EpisodeState.NOT_WANTED,
        with_rendition=False,
        with_source=False,
    )

    assert await retained_bytes(db_session, settings) == 0


# --- Wiring -----------------------------------------------------------------


async def test_a_want_dropped_as_stale_starts_the_grace_period(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """FR-T2 into FR-T1: the drop is what the G days are then counted from."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970210, ready_at=days_ago(40)
    )
    user = await make_user(db_session, "stale-then-swept@arc.test")
    await add_want(
        db_session,
        user,
        episode,
        dropped_at=days_ago(7) + timedelta(hours=1),
        drop_reason="unwatched for D days",
    )

    assert await candidates(db_session, settings, now=NOW) == [], "an hour short of G"

    found = await candidates(db_session, settings, now=NOW + 2 * DAY)

    assert [target.episode_id for target in found] == [episode.id]
    assert "dropped" in found[0].reason


async def test_a_show_taken_off_the_list_starts_the_grace_period(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """FR-W4 into FR-T1: dropping a show is a moment, and G runs from it.

    The bug this pins down: the want used to be *deleted* when the show stopped
    being watched, which left the sweep with no anchor at all. It fell back to
    the age of the files — so an episode somebody dropped this morning was
    deleted tonight because its bytes happened to be a month old, and the log
    said "nobody wants it and nobody ever did" about a show that had been on
    the user's list until breakfast.
    """
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970250, ready_at=days_ago(40)
    )
    anime = await db_session.get(Anime, episode.anime_id)
    assert anime is not None
    user = await make_user(db_session, "gaveup@arc.test")
    entry = await make_entry(db_session, user, anime, progress=episode.number - 1)
    await compute_wants(db_session, now=NOW)
    assert await candidates(db_session, settings, now=NOW) == [], "somebody wants it"

    entry.status = ListStatus.DROPPED
    await db_session.flush()
    await compute_wants(db_session, now=NOW)

    assert await candidates(db_session, settings, now=NOW + 6 * DAY) == [], "inside G"
    found = await candidates(db_session, settings, now=NOW + 8 * DAY)
    assert [target.episode_id for target in found] == [episode.id]
    assert "dropped" in found[0].reason, found[0].reason


async def test_a_sweep_carries_on_when_qbittorrent_is_down(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreachable torrent must not hold up every deletion behind it.

    The episode with the torrent is left exactly as it was — the client call is
    the first thing the deleter makes, before a byte is removed — and is a
    candidate again on the hour.
    """
    settings = acquisition_settings(tmp_path)
    stub = qbit_stub(monkeypatch)
    with_torrent = await make_retained_episode(
        db_session, settings, anilist_id=970260, ready_at=days_ago(30), info_hash=HASH
    )
    without = await make_retained_episode(
        db_session, settings, anilist_id=970261, ready_at=days_ago(30)
    )
    stub.down = True

    await retention_sweep(context(db_session, settings))

    assert output_dir_for(settings, with_torrent.id).exists()
    assert with_torrent.state is EpisodeState.READY
    assert await rows_for(db_session, Torrent, with_torrent.id) == 1
    assert not output_dir_for(settings, without.id).exists(), "the rest of the sweep ran"
    assert without.state is EpisodeState.NOT_WANTED
    assert [target.episode_id for target in await candidates(db_session, settings, now=NOW)] == [
        with_torrent.id
    ], "and it comes round again next hour"


async def test_a_ready_episode_with_no_files_is_reset(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row claiming to be playable over an empty directory (FR-T3).

    No grace period: there are no bytes to be careful about, only a play button
    that would 404. The sweep deletes nothing and puts the episode back where
    acquisition can fetch it again.
    """
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=970270,
        ready_at=days_ago(1),
        with_rendition=False,
        with_source=False,
    )

    (target,) = await candidates(db_session, settings, now=NOW)
    assert target.reason == REASON_NO_FILES
    assert target.bytes == 0 and target.anchor is None

    await retention_sweep(context(db_session, settings))

    assert episode.state is EpisodeState.NOT_WANTED
    assert await candidates(db_session, settings, now=NOW) == []


async def test_a_ready_episode_with_no_files_is_left_alone_while_wanted(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Nothing with a live want on it is ever a candidate, this included."""
    settings = acquisition_settings(tmp_path)
    episode = await make_retained_episode(
        db_session,
        settings,
        anilist_id=970275,
        ready_at=days_ago(1),
        with_rendition=False,
        with_source=False,
    )
    waiting = await make_user(db_session, "waiting-nofiles@arc.test")
    await add_want(db_session, waiting, episode)

    assert await candidates(db_session, settings, now=NOW) == []


async def test_the_deleter_reports_what_it_removed(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970220, ready_at=days_ago(30), info_hash=HASH
    )
    (target,) = await candidates(db_session, settings, now=NOW)

    removed = await delete_episode_files(db_session, settings, episode, target.targets)

    assert removed.acted and removed.state_changed
    assert removed.media_files == 1
    assert removed.torrents == 1
    assert removed.hashes == (HASH,)
    assert removed.freed_bytes == target.bytes
    assert removed.rendition_dir and removed.source_dir


async def test_a_stale_want_survives_the_sweep_that_deleted_its_episode(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The FR-T2 drop is the record, and it must outlive the files.

    Clearing it with the rest of the rows would put the episode straight back
    in the window — the show is still watching, the user's progress has not
    moved — and Arc would fetch it again, drop it again D days later and delete
    it again G days after that, for ever.
    """
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970230, ready_at=days_ago(40)
    )
    user = await make_user(db_session, "leftover@arc.test")
    await add_want(
        db_session, user, episode, dropped_at=days_ago(30), drop_reason=STALE_DROP_REASON
    )

    await retention_sweep(context(db_session, settings))
    await compute_wants(db_session, now=NOW)

    assert episode.state is EpisodeState.NOT_WANTED
    want = await db_session.get(Want, (user.id, episode.id))
    assert want is not None and want.dropped_at is not None


async def test_the_sweep_clears_the_want_row_it_was_counting_from(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A want dropped with the show is a tombstone for files that are now gone.

    Unlike the stale drop above it has nothing left to say: the show is not
    watching, so nothing is going to re-create the row, and leaving it would
    only make the next reconciliation refuse a re-acquisition that ought to
    happen the moment somebody wants the episode again (FR-T3).
    """
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970235, ready_at=days_ago(40)
    )
    user = await make_user(db_session, "shelved@arc.test")
    await add_want(
        db_session, user, episode, dropped_at=days_ago(30), drop_reason=REASON_NOT_WANTING
    )

    await retention_sweep(context(db_session, settings))

    assert episode.state is EpisodeState.NOT_WANTED
    assert await db_session.get(Want, (user.id, episode.id)) is None


async def test_a_live_want_is_never_deleted_by_a_manual_deletion(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-T4 deletes the files of an episode somebody wants; the want stands.

    That is what makes "or re-fetch" work without a second button: the episode
    goes back to ``not_wanted`` and the next ``compute_wants`` acquires it
    again for the user who is still waiting.
    """
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970236, ready_at=days_ago(1)
    )
    user = await make_user(db_session, "stillwants@arc.test")
    await add_want(db_session, user, episode)

    await delete_files(
        context(db_session, settings, {"episode_id": episode.id}, job_type=DELETE_EPISODE_FILES)
    )

    want = await db_session.get(Want, (user.id, episode.id))
    assert want is not None and want.dropped_at is None
    assert episode.state is EpisodeState.NOT_WANTED


def test_write_source_file_lands_under_the_downloads_root(tmp_path: Path) -> None:
    """A guard on the helper the file tests depend on."""
    settings = acquisition_settings(tmp_path)
    path = write_source_file(settings, 42)

    assert path.resolve().is_relative_to(settings.downloads_dir)


async def test_a_want_that_appears_mid_sweep_saves_the_episode(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The candidate list is planned once and acted on one episode at a time."""
    settings = acquisition_settings(tmp_path)
    qbit_stub(monkeypatch, holding=None)
    episode = await make_retained_episode(
        db_session, settings, anilist_id=970240, ready_at=days_ago(30)
    )
    latecomer = await make_user(db_session, "latecomer@arc.test")
    (target,) = await candidates(db_session, settings, now=NOW)
    assert target.episode_id == episode.id

    # …and only now does somebody want it again.
    await add_want(db_session, latecomer, episode)
    await retention_sweep(context(db_session, settings))

    assert output_dir_for(settings, episode.id).exists()
    assert episode.state is EpisodeState.READY
