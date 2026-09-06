"""The ``match_file`` handler: link, review, or ignore (FR-L3, FR-L4).

Against the real database with the catalogue sources faked. The matcher itself
is scored in ``test_matcher_scoring.py`` and measured over a real catalogue in
``test_matcher_acceptance.py``; what is under test *here* is the decision the
handler makes with the number it is given, and above all the one that is a
non-negotiable: nothing below the threshold is ever linked.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Anime, Episode, EpisodeState, Job, JobStatus, MediaFile, ReviewState
from arc.services.catalog import Breaker, CatalogService
from arc.services.jobs.registry import JobContext
from arc.services.library import jobs as library_jobs
from arc.services.library.matcher import MatchResult, Scored
from arc.services.library.parser import parse
from tests.anilist_mock import FRIEREN_ID, FakeAniList, frieren_fake
from tests.mal_mock import FakeMal
from tests.mal_mock import frieren_fake as mal_frieren_fake

pytestmark = pytest.mark.pg

FRIEREN_FILE = "[SubsPlease] Sousou no Frieren - 05 (1080p) [A1B2C3D4].mkv"
NCOP_FILE = "[Group] Some Show - NCOP.mkv"
BATCH_FILE = "[Judas] Sousou no Frieren - 01-28 [1080p][HEVC x265 10bit][Batch].mkv"
UNKNOWN_FILE = "[Group] Totally Unknown Show - 02 [720p].mkv"


@pytest.fixture
def library_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> CatalogService:
    """A catalogue over the AniList and MAL fakes, in place of the real one.

    ``catalog_for`` is what the handler calls; it normally builds a service
    from settings and closes it afterwards. Patching it is the only stub in
    this file — the sockets, and nothing else.
    """
    anilist: FakeAniList = frieren_fake()
    # The fake keys its search map on the exact term; the parser produces the
    # romaji title, which is what the matcher asks for.
    anilist.search["sousou no frieren"] = anilist.search["frieren"]
    mal: FakeMal = mal_frieren_fake()
    service = CatalogService(anilist.source(), mal.source(), Breaker(300.0))

    class _Holder:
        def __init__(self) -> None:
            self.service = service

        async def __aenter__(self) -> CatalogService:
            return self.service

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(library_jobs, "catalog_for", lambda settings: _Holder())
    return service


async def add_file(session: AsyncSession, tmp_path: Path, name: str) -> MediaFile:
    path = tmp_path / "manual" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0")
    media_file = MediaFile(
        path=str(path.resolve()),
        size=1,
        parsed=parse(name).as_dict(),
        review_state=ReviewState.PENDING,
    )
    session.add(media_file)
    await session.flush()
    return media_file


async def run_match(
    session: AsyncSession, settings: Settings, media_file_id: int, **extra: object
) -> None:
    job = Job(
        type=library_jobs.MATCH_FILE,
        payload={"media_file_id": media_file_id, **extra},
        status=JobStatus.RUNNING,
    )
    session.add(job)
    await session.flush()
    await library_jobs.match_file(
        JobContext(job=job, session=session, settings=settings, log=logging.getLogger("test"))
    )


def force(monkeypatch: pytest.MonkeyPatch, result: MatchResult) -> None:
    """Make the matcher return ``result``, whatever the catalogue says.

    The point of the handler tests is the *decision*, and a decision test that
    depends on the scorer producing a particular number would fail every time
    a weight moved.
    """

    async def fake_match(
        session: object, catalog: object, parsed: object, **kwargs: object
    ) -> MatchResult:
        return result

    monkeypatch.setattr(library_jobs, "match", fake_match)


class TestAutoLink:
    async def test_a_confident_match_links_and_creates_the_episode(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
    ) -> None:
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.AUTO
        assert media_file.episode_id is not None
        assert media_file.match_confidence is not None
        assert media_file.match_confidence >= library_settings.match_auto_threshold

        episode = await db_session.get(Episode, media_file.episode_id)
        assert episode is not None
        assert episode.number == 5
        assert episode.state is EpisodeState.MATCHED
        anime = await db_session.get(Anime, episode.anime_id)
        assert anime is not None
        assert anime.anilist_id == FRIEREN_ID

    async def test_the_candidates_are_stored_as_evidence(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
    ) -> None:
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        await run_match(db_session, library_settings, media_file.id)

        assert media_file.match_candidates
        best = media_file.match_candidates[0]
        assert best["episode_number"] == 5
        assert best["reasons"]

    async def test_a_second_run_changes_nothing(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
    ) -> None:
        """Handlers are idempotent (CLAUDE.md); a retry must not double up."""
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)

        await run_match(db_session, library_settings, media_file.id)
        first = (media_file.episode_id, media_file.review_state, media_file.match_confidence)
        await run_match(db_session, library_settings, media_file.id)

        assert (
            media_file.episode_id,
            media_file.review_state,
            media_file.match_confidence,
        ) == first
        episodes = list((await db_session.scalars(select(Episode))).all())
        assert len(episodes) == 1

    async def test_a_ready_episode_is_not_downgraded(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
    ) -> None:
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        await run_match(db_session, library_settings, media_file.id)
        episode = await db_session.get(Episode, media_file.episode_id or 0)
        assert episode is not None
        episode.state = EpisodeState.READY
        await db_session.flush()

        await run_match(db_session, library_settings, media_file.id)

        assert episode.state is EpisodeState.READY


class TestTheThreshold:
    """FR-L4: below the threshold, nothing is linked. Ever."""

    async def test_a_score_just_under_the_threshold_is_not_linked(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        anime = Anime(anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", episodes=28)
        db_session.add(anime)
        await db_session.flush()
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)

        assert library_settings.match_auto_threshold == 0.85
        force(
            monkeypatch,
            MatchResult(candidates=(Scored(anime.id, 5, 0.84, ("title 0.95",)),), confidence=0.84),
        )

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.PENDING
        assert media_file.episode_id is None
        assert media_file.match_confidence == 0.84
        # …and no episode row was quietly created on the way past.
        assert list((await db_session.scalars(select(Episode))).all()) == []

    async def test_a_score_exactly_at_the_threshold_is_linked(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        anime = Anime(anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", episodes=28)
        db_session.add(anime)
        await db_session.flush()
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        force(
            monkeypatch,
            MatchResult(candidates=(Scored(anime.id, 5, 0.85, exact_title=True),), confidence=0.85),
        )

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.AUTO

    async def test_the_candidates_are_kept_for_the_review_ui(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        anime = Anime(anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", episodes=28)
        db_session.add(anime)
        await db_session.flush()
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        force(
            monkeypatch,
            MatchResult(
                candidates=(Scored(anime.id, 5, 0.70, ("title 0.80",)), Scored(anime.id, 6, 0.60)),
                confidence=0.70,
            ),
        )

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.match_candidates is not None
        assert len(media_file.match_candidates) == 3  # two candidates and the reason
        assert media_file.match_candidates[0]["anime_id"] == anime.id
        assert media_file.match_candidates[-1] == {"reason": library_jobs.REASON_LOW_CONFIDENCE}

    async def test_an_ambiguous_pair_lands_in_review(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two shows that both score 0.95 are not a 0.95 match to either."""
        from arc.services.library.matcher import rank

        first = Anime(anilist_id=1001, title_romaji="Twin One", episodes=12)
        second = Anime(anilist_id=1002, title_romaji="Twin Two", episodes=12)
        db_session.add_all([first, second])
        await db_session.flush()
        media_file = await add_file(db_session, tmp_path, UNKNOWN_FILE)
        force(
            monkeypatch, rank([Scored(first.id, 2, 0.95), Scored(second.id, 2, 0.94)], minimum=0.4)
        )

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.PENDING
        assert media_file.episode_id is None

    async def test_nothing_at_all_is_a_no_candidates_item(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        media_file = await add_file(db_session, tmp_path, UNKNOWN_FILE)
        force(monkeypatch, MatchResult())

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.PENDING
        assert media_file.match_candidates == [{"reason": library_jobs.REASON_NO_CANDIDATES}]
        assert media_file.match_confidence == 0.0

    async def test_a_candidate_with_no_episode_number_is_not_linked(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        anime = Anime(anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", format="MOVIE")
        db_session.add(anime)
        await db_session.flush()
        media_file = await add_file(db_session, tmp_path, UNKNOWN_FILE)
        force(monkeypatch, MatchResult(candidates=(Scored(anime.id, None, 0.99),), confidence=0.99))

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.PENDING
        assert media_file.match_candidates is not None
        assert media_file.match_candidates[-1] == {"reason": library_jobs.REASON_NO_EPISODE}


class TestTheTitleBar:
    """The second bar the handler applies: ``MATCH_MIN_TITLE_FOR_AUTO``."""

    async def test_a_confident_score_on_a_weak_title_goes_to_review(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        anime = Anime(anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", episodes=28)
        db_session.add(anime)
        await db_session.flush()
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        assert library_settings.match_min_title_for_auto == 0.92
        force(
            monkeypatch,
            MatchResult(candidates=(Scored(anime.id, 5, 0.95, title=0.77),), confidence=0.95),
        )

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.PENDING
        assert media_file.episode_id is None
        assert media_file.match_candidates is not None
        assert media_file.match_candidates[-1] == {"reason": library_jobs.REASON_WEAK_TITLE}

    async def test_the_two_reasons_are_told_apart(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two different sentences: "not sure enough" and "not close enough"."""
        anime = Anime(anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", episodes=28)
        db_session.add(anime)
        await db_session.flush()
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        force(
            monkeypatch,
            MatchResult(candidates=(Scored(anime.id, 5, 0.60, title=1.0),), confidence=0.60),
        )

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.match_candidates is not None
        assert media_file.match_candidates[-1] == {"reason": library_jobs.REASON_LOW_CONFIDENCE}

    async def test_an_exact_title_clears_the_bar(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        anime = Anime(anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", episodes=28)
        db_session.add(anime)
        await db_session.flush()
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        force(
            monkeypatch,
            MatchResult(
                candidates=(Scored(anime.id, 5, 0.90, title=0.0, exact_title=True),),
                confidence=0.90,
            ),
        )

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.AUTO


class TestALinkedFileIsNeverUnlinked:
    """A re-match may change its mind; it may not take an episode away.

    ``match_file`` runs again for reasons that have nothing to do with the
    file — the parser changed, the catalogue row was refreshed, the job was
    retried after a failure — and the second answer is not automatically the
    better one. Clearing ``episode_id`` there took a playable episode off a
    show page and left a ``preparing`` transcode reading a file nothing
    pointed at. Reopening a link is a person's decision (FR-L6).
    """

    async def test_a_worse_second_answer_keeps_the_link(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        await run_match(db_session, library_settings, media_file.id)
        linked = media_file.episode_id
        confidence = media_file.match_confidence
        assert linked is not None
        assert media_file.review_state is ReviewState.AUTO

        anime_id = (await db_session.get(Episode, linked)).anime_id  # type: ignore[union-attr]
        force(
            monkeypatch,
            MatchResult(candidates=(Scored(anime_id, 5, 0.50, title=0.5),), confidence=0.50),
        )
        await run_match(db_session, library_settings, media_file.id)

        assert media_file.episode_id == linked
        assert media_file.review_state is ReviewState.AUTO
        assert media_file.match_confidence == confidence

    async def test_a_ready_episode_keeps_its_file(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The worst version of it: the episode is transcoded and watchable."""
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        await run_match(db_session, library_settings, media_file.id)
        episode = await db_session.get(Episode, media_file.episode_id or 0)
        assert episode is not None
        episode.state = EpisodeState.READY
        await db_session.flush()

        force(monkeypatch, MatchResult())
        await run_match(db_session, library_settings, media_file.id)

        assert media_file.episode_id == episode.id
        assert episode.state is EpisodeState.READY

    async def test_an_unlinked_file_is_still_sent_to_review(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The rule is about *unlinking*, not about the queue."""
        media_file = await add_file(db_session, tmp_path, UNKNOWN_FILE)
        force(monkeypatch, MatchResult())

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.PENDING
        assert media_file.match_candidates == [{"reason": library_jobs.REASON_NO_CANDIDATES}]


class TestKinds:
    async def test_a_creditless_file_is_ignored_without_asking(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
    ) -> None:
        media_file = await add_file(db_session, tmp_path, NCOP_FILE)

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.IGNORED
        assert media_file.episode_id is None
        assert media_file.match_candidates == [{"reason": library_jobs.REASON_NC}]

    async def test_a_batch_goes_to_review(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
    ) -> None:
        """Phase 1 links single files; a batch needs a person (FR-L6)."""
        media_file = await add_file(db_session, tmp_path, BATCH_FILE)

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is ReviewState.PENDING
        assert media_file.episode_id is None
        assert media_file.match_candidates == [{"reason": library_jobs.REASON_BATCH}]

    async def test_neither_kind_calls_the_catalogue(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ``nc`` file must not cost an AniList request per release."""
        called: list[int] = []

        def explode(settings: object) -> object:
            called.append(1)
            raise AssertionError("the catalogue must not be asked about an nc file")

        monkeypatch.setattr(library_jobs, "catalog_for", explode)
        media_file = await add_file(db_session, tmp_path, NCOP_FILE)

        await run_match(db_session, library_settings, media_file.id)

        assert called == []


class TestPeopleWin:
    @pytest.mark.parametrize("state", [ReviewState.CONFIRMED, ReviewState.IGNORED])
    async def test_a_decided_file_is_left_alone(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        state: ReviewState,
    ) -> None:
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        media_file.review_state = state
        await db_session.flush()

        await run_match(db_session, library_settings, media_file.id)

        assert media_file.review_state is state
        assert media_file.episode_id is None

    async def test_a_vanished_row_is_not_an_error(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
    ) -> None:
        """Retention (M10) can delete a file between the enqueue and the run."""
        await run_match(db_session, library_settings, 987654)


class TestPrior:
    async def test_the_expected_episode_is_used(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M6 passes ``expected`` in the payload; the handler reads it (FR-L3)."""
        anime = Anime(anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", episodes=28)
        db_session.add(anime)
        await db_session.flush()
        media_file = await add_file(db_session, tmp_path, "unreadable.release.name.mkv")

        seen: dict[str, object] = {}

        async def capture(
            session: object, service: object, parsed: object, **kwargs: object
        ) -> MatchResult:
            seen.update(kwargs)
            return MatchResult(candidates=(Scored(anime.id, 5, 0.99, prior=True),), confidence=0.99)

        monkeypatch.setattr(library_jobs, "match", capture)

        await run_match(db_session, library_settings, media_file.id, expected=[anime.id, 5])

        assert seen["expected"] == (anime.id, 5)
        assert media_file.review_state is ReviewState.AUTO

    async def test_no_prior_is_none(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        catalog: CatalogService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        async def capture(
            session: object, service: object, parsed: object, **kwargs: object
        ) -> MatchResult:
            seen.update(kwargs)
            return MatchResult()

        monkeypatch.setattr(library_jobs, "match", capture)
        media_file = await add_file(db_session, tmp_path, UNKNOWN_FILE)

        await run_match(db_session, library_settings, media_file.id)

        assert seen["expected"] is None
        assert seen["minimum"] == library_settings.match_min_candidate


class TestTheParse:
    async def test_it_reads_the_basename_not_the_stored_path(
        self, db_session: AsyncSession, library_settings: Settings, tmp_path: Path
    ) -> None:
        """So the re-parse and the one ingest stored are the same object.

        ``parse`` already reduces a path to its last component, but it keeps
        what it was given in ``raw`` — so parsing the absolute path produced a
        ``ParsedName`` that differed from the stored one in exactly the field
        a person reads first.
        """
        media_file = await add_file(db_session, tmp_path, FRIEREN_FILE)
        assert "/" in media_file.path

        parsed = library_jobs._parsed_of(media_file)

        assert parsed.raw == FRIEREN_FILE
        assert parsed == parse(FRIEREN_FILE)


class TestRegistration:
    def test_both_handlers_are_registered(self) -> None:
        from arc.services.jobs import registered_types

        assert {"library_scan", "match_file"} <= registered_types()

    def test_the_dedupe_key_is_per_file(self) -> None:
        from arc.services.library.names import match_dedupe_key

        assert match_dedupe_key(7) != match_dedupe_key(8)
