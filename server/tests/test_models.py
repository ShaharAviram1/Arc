"""The models against a real Postgres: round-trips, cascades, constraints.

These run inside a transaction that is rolled back, so they leave the test
database as they found it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    MalWriteCause,
    MalWriteLog,
    MediaFile,
    Torrent,
    UpdatedBy,
    User,
    UserRole,
    Want,
    WatchProgress,
)

pytestmark = pytest.mark.pg

ANILIST_ID = 154587  # Frieren, as good a fixture as any.


async def _fixture_user(session: AsyncSession, email: str = "viewer@example.com") -> User:
    user = User(email=email, password_hash="$argon2id$fake", role=UserRole.USER)
    session.add(user)
    await session.flush()
    return user


async def _fixture_anime(session: AsyncSession, anilist_id: int = ANILIST_ID) -> Anime:
    anime = Anime(
        id=anilist_id,
        mal_id=52991,
        title_romaji="Sousou no Frieren",
        title_english="Frieren: Beyond Journey's End",
        synonyms=["Frieren at the Funeral"],
        genres=["Adventure", "Drama", "Fantasy"],
        tags=[{"name": "Elf", "rank": 90}],
        next_airing={"episode": 29, "airingAt": 1_700_000_000},
        status="FINISHED",
        episodes=28,
    )
    session.add(anime)
    await session.flush()
    return anime


async def _fixture_episode(session: AsyncSession, anime: Anime, number: int = 1) -> Episode:
    episode = Episode(
        anime_id=anime.id,
        number=number,
        title=f"Episode {number}",
        air_at=datetime(2023, 9, 29, 14, 0, tzinfo=UTC),
    )
    session.add(episode)
    await session.flush()
    return episode


async def _count(
    session: AsyncSession,
    model: type[ListEntry] | type[WatchProgress] | type[Want],
    user_id: int,
) -> int | None:
    """Rows of ``model`` still owned by ``user_id``."""
    return await session.scalar(
        select(func.count()).select_from(model).where(model.user_id == user_id)
    )


async def test_core_aggregates_round_trip(db_session: AsyncSession) -> None:
    """One of each aggregate, written and read back."""
    user = await _fixture_user(db_session)
    anime = await _fixture_anime(db_session)
    episode = await _fixture_episode(db_session, anime)

    db_session.add_all(
        [
            ListEntry(
                user_id=user.id,
                anime_id=anime.id,
                status=ListStatus.WATCHING,
                progress=3,
                score=9,
            ),
            WatchProgress(
                user_id=user.id,
                episode_id=episode.id,
                position_s=612.5,
                duration_s=1420.0,
                completed=False,
            ),
            Want(user_id=user.id, episode_id=episode.id),
        ]
    )
    await db_session.commit()

    # Defaults applied by the database, and JSONB/array columns intact.
    stored_anime = await db_session.get(Anime, ANILIST_ID)
    assert stored_anime is not None
    assert stored_anime.genres == ["Adventure", "Drama", "Fantasy"]
    assert stored_anime.synonyms == ["Frieren at the Funeral"]
    assert stored_anime.tags == [{"name": "Elf", "rank": 90}]
    assert stored_anime.next_airing == {"episode": 29, "airingAt": 1_700_000_000}

    stored_episode = await db_session.get(Episode, episode.id)
    assert stored_episode is not None
    assert stored_episode.air_at == datetime(2023, 9, 29, 14, 0, tzinfo=UTC)

    entry = await db_session.get(ListEntry, (user.id, anime.id))
    assert entry is not None
    assert entry.progress == 3
    assert entry.mal_dirty is False

    progress = await db_session.get(WatchProgress, (user.id, episode.id))
    assert progress is not None
    assert progress.position_s == pytest.approx(612.5)
    assert progress.completed is False

    want = await db_session.get(Want, (user.id, episode.id))
    assert want is not None
    assert want.dropped_at is None
    assert want.created_at is not None


async def test_enums_round_trip_as_their_values(db_session: AsyncSession) -> None:
    """Enums come back as members, and are stored as the lowercase value."""
    user = await _fixture_user(db_session, "enums@example.com")
    anime = await _fixture_anime(db_session, anilist_id=1)
    episode = await _fixture_episode(db_session, anime)
    db_session.add(
        ListEntry(
            user_id=user.id,
            anime_id=anime.id,
            status=ListStatus.ON_HOLD,
            updated_by=UpdatedBy.MAL,
        )
    )
    episode.state = EpisodeState.DOWNLOADING
    await db_session.commit()

    entry = await db_session.get(ListEntry, (user.id, anime.id))
    assert entry is not None
    assert entry.status is ListStatus.ON_HOLD
    assert entry.updated_by is UpdatedBy.MAL

    reloaded = await db_session.get(Episode, episode.id)
    assert reloaded is not None
    assert reloaded.state is EpisodeState.DOWNLOADING
    assert reloaded.state.value == "downloading"

    # …and the column really holds the spec's string, not the member name.
    raw = await db_session.execute(
        select(Episode.__table__.c.state).where(Episode.__table__.c.id == episode.id)
    )
    assert raw.scalar_one() == "downloading"

    assert (await db_session.get(User, user.id)).role is UserRole.USER  # type: ignore[union-attr]


async def test_job_defaults_and_payload(db_session: AsyncSession) -> None:
    """The shape the worker's claim loop depends on (architecture.md §2)."""
    job = Job(type="compute_wants", payload={"user_id": 7, "reason": "progress"})
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    assert job.id is not None
    assert job.status is JobStatus.PENDING
    assert job.priority == 100
    assert job.attempts == 0
    assert job.max_attempts == 3
    assert job.payload == {"user_id": 7, "reason": "progress"}
    assert job.locked_by is None
    # run_after defaults to now(), so a job is claimable immediately.
    assert job.run_after <= datetime.now(UTC) + timedelta(seconds=1)
    assert job.created_at is not None
    assert job.finished_at is None


async def test_deleting_a_user_cascades_to_their_rows(db_session: AsyncSession) -> None:
    """User-owned rows go with the user; shared library rows do not."""
    user = await _fixture_user(db_session, "cascade@example.com")
    anime = await _fixture_anime(db_session, anilist_id=2)
    episode = await _fixture_episode(db_session, anime)
    db_session.add_all(
        [
            ListEntry(user_id=user.id, anime_id=anime.id, status=ListStatus.WATCHING),
            WatchProgress(user_id=user.id, episode_id=episode.id, position_s=1.0),
            Want(user_id=user.id, episode_id=episode.id),
        ]
    )
    await db_session.commit()

    await db_session.delete(user)
    await db_session.commit()

    assert await _count(db_session, ListEntry, user.id) == 0
    assert await _count(db_session, WatchProgress, user.id) == 0
    assert await _count(db_session, Want, user.id) == 0

    # The episode and the anime are shared library state and survive.
    assert await db_session.get(Episode, episode.id) is not None
    assert await db_session.get(Anime, anime.id) is not None


async def test_episode_number_is_unique_per_anime(db_session: AsyncSession) -> None:
    anime = await _fixture_anime(db_session, anilist_id=3)
    await _fixture_episode(db_session, anime, number=5)
    await db_session.commit()

    db_session.add(Episode(anime_id=anime.id, number=5))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_email_is_unique(db_session: AsyncSession) -> None:
    await _fixture_user(db_session, "dup@example.com")
    await db_session.commit()

    db_session.add(User(email="dup@example.com", password_hash="x"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_email_is_unique_case_insensitively(db_session: AsyncSession) -> None:
    """``uq_users_email_lower``: one address is one account, whatever the case.

    A plain UNIQUE on the column would happily accept both of these rows and
    hand the same person two accounts.
    """
    await _fixture_user(db_session, "bob@x.com")
    await db_session.commit()

    db_session.add(User(email="Bob@x.com", password_hash="x"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_media_file_path_is_unique(db_session: AsyncSession) -> None:
    """One row per file on disk, so a rescan cannot duplicate the library."""
    path = "/data/downloads/Frieren - 01 [1080p].mkv"
    db_session.add(MediaFile(path=path))
    await db_session.commit()

    db_session.add(MediaFile(path=path))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_torrent_info_hash_is_unique(db_session: AsyncSession) -> None:
    """The info hash is the torrent's identity; two rows would be two views."""
    anime = await _fixture_anime(db_session, anilist_id=5)
    first = await _fixture_episode(db_session, anime, number=1)
    second = await _fixture_episode(db_session, anime, number=2)
    info_hash = "a" * 40
    db_session.add(Torrent(episode_id=first.id, info_hash=info_hash))
    await db_session.commit()

    db_session.add(Torrent(episode_id=second.id, info_hash=info_hash))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_deleting_an_anime_with_a_mal_write_log_is_refused(
    db_session: AsyncSession,
) -> None:
    """RESTRICT: the write log outlives the cached AniList row (FR-M5).

    Losing this history to a cache eviction would break the spec's promise
    that every MAL write can be explained and reverted.
    """
    user = await _fixture_user(db_session, "restrict@example.com")
    anime = await _fixture_anime(db_session, anilist_id=6)
    db_session.add(
        MalWriteLog(
            user_id=user.id,
            anime_id=anime.id,
            field="progress",
            old_value=3,
            new_value=4,
            cause=MalWriteCause.WATCH,
        )
    )
    await db_session.commit()

    await db_session.delete(anime)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_watch_progress_completed_at_round_trips(db_session: AsyncSession) -> None:
    """``completed_at`` is null until completion, then holds the moment."""
    user = await _fixture_user(db_session, "completed@example.com")
    anime = await _fixture_anime(db_session, anilist_id=7)
    episode = await _fixture_episode(db_session, anime)

    progress = WatchProgress(user_id=user.id, episode_id=episode.id, position_s=10.0)
    db_session.add(progress)
    await db_session.commit()

    stored = await db_session.get(WatchProgress, (user.id, episode.id))
    assert stored is not None
    assert stored.completed is False
    assert stored.completed_at is None

    finished_at = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    stored.completed = True
    stored.completed_at = finished_at
    await db_session.commit()

    await db_session.refresh(stored)
    assert stored.completed is True
    assert stored.completed_at == finished_at

    # …and it really came back from the column, not from the identity map.
    table = WatchProgress.__table__
    raw = await db_session.execute(
        select(table.c.completed_at).where(
            table.c.user_id == user.id, table.c.episode_id == episode.id
        )
    )
    assert raw.scalar_one() == finished_at
