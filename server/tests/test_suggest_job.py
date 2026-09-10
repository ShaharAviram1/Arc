"""The ``llm_suggest_match`` handler (FR-L5, §5.2).

Against the real database with the model chain faked — the same arrangement
``test_match_job.py`` uses for the catalogue, and for the same reason: what is
under test is the decision the handler makes with the answer it is given, not
the provider.

The load-bearing test in this file is
:func:`TestNeverLinks.test_a_confident_suggestion_still_does_not_link`. FR-L5
and CLAUDE.md both say the suggestion is shown and never applied, and the only
way that stops being true is somebody adding a "well, if it is confident…"
branch to the handler. This is the test that fails when they do.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    ReviewState,
)
from arc.services.jobs.registry import JobContext
from arc.services.library import jobs as library_jobs
from arc.services.library.names import (
    LLM_SUGGEST_MATCH,
    LLM_SUGGEST_PRIORITY,
    enqueue_suggestion,
)
from arc.services.library.parser import parse
from arc.services.recs.base import JsonResult, RecsFailed, RecsRefused, RecsUnavailable

pytestmark = pytest.mark.pg

FRIEREN_FILE = "[SubsPlease] Sousou no Frieren - 03 (1080p) [A1B2C3D4].mkv"


@pytest.fixture
def suggest_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Suggestions on, with a Gemini key so the chain counts as configured."""
    return settings.model_copy(
        update={
            "data_dir": tmp_path,
            "llm_match_suggestions": True,
            "gemini_api_key": "AIza-a-real-looking-key",
        }
    )


class FakeChain:
    """A :class:`~arc.services.recs.base.JsonModel` that answers from a script.

    Records what it was asked, which is how the prompt's contents are checked
    from the handler's side rather than only in ``test_suggest.py``.
    """

    def __init__(self, answer: Any = None, *, error: Exception | None = None) -> None:
        self.answer = answer if answer is not None else {}
        self.error = error
        self.calls = 0
        self.system = ""
        self.user = ""
        self.schema: dict[str, Any] = {}
        self.name = ""
        self.closed = False

    async def complete(
        self, *, system: str, user: str, schema: dict[str, Any], name: str = "recs_picks"
    ) -> JsonResult:
        self.calls += 1
        self.system, self.user, self.schema, self.name = system, user, schema, name
        if self.error is not None:
            raise self.error
        return JsonResult(
            data=self.answer,
            model="gemini-3.5-flash",
            usage={"input_tokens": 500, "output_tokens": 40},
            provider="gemini",
        )

    async def aclose(self) -> None:
        self.closed = True


def use(monkeypatch: pytest.MonkeyPatch, chain: FakeChain | None) -> FakeChain | None:
    """Put ``chain`` in place of the process's real one."""
    monkeypatch.setattr(library_jobs, "shared_model", lambda settings: chain)
    return chain


async def add_anime(session: AsyncSession, anime_id: int, title: str, **values: Any) -> Anime:
    row = Anime(
        id=anime_id,
        anilist_id=anime_id,
        summary_source="anilist",
        detail_source="anilist",
        title_romaji=title,
        format="TV",
        episodes=values.pop("episodes", 28),
        **values,
    )
    session.add(row)
    await session.flush()
    return row


async def add_file(
    session: AsyncSession,
    tmp_path: Path,
    *,
    name: str = FRIEREN_FILE,
    directory: str = "manual",
    review_state: ReviewState = ReviewState.PENDING,
    candidates: list[dict[str, Any]] | None = None,
    suggestion: dict[str, Any] | None = None,
    episode_id: int | None = None,
) -> MediaFile:
    path = tmp_path / directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0")
    media_file = MediaFile(
        path=str(path.resolve()),
        size=1,
        parsed=parse(name).as_dict(),
        review_state=review_state,
        match_candidates=candidates,
        llm_suggestion=suggestion,
        episode_id=episode_id,
    )
    session.add(media_file)
    await session.flush()
    return media_file


async def run_suggest(
    session: AsyncSession, settings: Settings, media_file_id: int, **extra: Any
) -> None:
    job = Job(
        type=LLM_SUGGEST_MATCH,
        payload={"media_file_id": media_file_id, **extra},
        status=JobStatus.RUNNING,
    )
    session.add(job)
    await session.flush()
    await library_jobs.llm_suggest_match(
        JobContext(job=job, session=session, settings=settings, log=logging.getLogger("test"))
    )


def candidates(*ids: int) -> list[dict[str, Any]]:
    """A stored ``match_candidates`` blob, reason sentence and all."""
    return [
        {"anime_id": anime_id, "episode_number": 3, "score": 0.8, "reasons": ["title 0.8"]}
        for anime_id in ids
    ] + [{"reason": "below the auto-link threshold"}]


ANSWER = {
    "anime_id": 11,
    "episode_number": 3,
    "reason": "The filename names this title exactly.",
    "confidence": "high",
}


# --- Enqueueing from the match (FR-L5) --------------------------------------


async def queued_suggestions(session: AsyncSession) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == LLM_SUGGEST_MATCH))
    return list(rows.all())


class TestEnqueue:
    """Only from review, and only when the feature can actually answer."""

    async def test_a_file_sent_to_review_queues_a_suggestion(
        self, db_session: AsyncSession, suggest_settings: Settings, tmp_path: Path
    ) -> None:
        media_file = await add_file(db_session, tmp_path, name="[Group] Nothing Known - 02.mkv")

        await _review(db_session, suggest_settings, media_file)

        jobs = await queued_suggestions(db_session)
        assert [job.payload["media_file_id"] for job in jobs] == [media_file.id]
        assert jobs[0].priority == LLM_SUGGEST_PRIORITY
        # Not forced: an automatic ask must not overwrite an answer already there.
        assert "force" not in jobs[0].payload

    async def test_nothing_is_queued_when_the_flag_is_off(
        self, db_session: AsyncSession, suggest_settings: Settings, tmp_path: Path
    ) -> None:
        settings = suggest_settings.model_copy(update={"llm_match_suggestions": False})
        media_file = await add_file(db_session, tmp_path)

        await _review(db_session, settings, media_file)

        assert await queued_suggestions(db_session) == []

    async def test_nothing_is_queued_when_no_provider_is_configured(
        self, db_session: AsyncSession, suggest_settings: Settings, tmp_path: Path
    ) -> None:
        """The flag on its own is an intention, not a working feature."""
        settings = suggest_settings.model_copy(update={"gemini_api_key": None})
        media_file = await add_file(db_session, tmp_path)

        await _review(db_session, settings, media_file)

        assert await queued_suggestions(db_session) == []

    async def test_an_auto_linked_file_queues_nothing(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The enqueue hangs off ``_review``, which a confident match never reaches."""
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path)
        from arc.services.library.link import link

        await link(
            db_session,
            media_file,
            anime_id=11,
            episode_number=3,
            review_state=ReviewState.AUTO,
        )

        assert await queued_suggestions(db_session) == []

    async def test_a_second_review_of_the_same_file_does_not_double_up(
        self, db_session: AsyncSession, suggest_settings: Settings, tmp_path: Path
    ) -> None:
        media_file = await add_file(db_session, tmp_path)

        await _review(db_session, suggest_settings, media_file)
        await _review(db_session, suggest_settings, media_file)

        assert len(await queued_suggestions(db_session)) == 1

    async def test_a_file_with_no_shortlist_asks_nothing(
        self, db_session: AsyncSession, suggest_settings: Settings, tmp_path: Path
    ) -> None:
        """A batch, or "no good candidates": there is nothing to choose between.

        The job would reach the model check, find an empty shortlist and store
        an error — a queue slot and a review item's column spent saying what
        the candidate list already says.
        """
        media_file = await add_file(db_session, tmp_path)

        await _review(db_session, suggest_settings, media_file, shortlist=False)

        assert media_file.review_state is ReviewState.PENDING
        assert await queued_suggestions(db_session) == []

    async def test_a_forced_ask_upgrades_a_job_already_queued(
        self, db_session: AsyncSession, suggest_settings: Settings, tmp_path: Path
    ) -> None:
        """Otherwise the automatic ask swallows the person's "ask again".

        The dedupe key is the file, so the API's forced enqueue returns the row
        ``_review`` queued — which, left alone, would run without ``force``,
        see the suggestion already on the file, and skip.
        """
        media_file = await add_file(db_session, tmp_path)
        await _review(db_session, suggest_settings, media_file)
        queued = (await queued_suggestions(db_session))[0]
        assert "force" not in queued.payload

        forced = await enqueue_suggestion(db_session, media_file.id, force=True)

        assert forced.id == queued.id
        assert forced.payload["force"] is True
        assert len(await queued_suggestions(db_session)) == 1

    async def test_a_running_job_is_left_alone(
        self, db_session: AsyncSession, suggest_settings: Settings, tmp_path: Path
    ) -> None:
        """It has already read its payload, and is producing a fresh answer."""
        media_file = await add_file(db_session, tmp_path)
        await _review(db_session, suggest_settings, media_file)
        queued = (await queued_suggestions(db_session))[0]
        queued.status = JobStatus.RUNNING
        await db_session.flush()

        forced = await enqueue_suggestion(db_session, media_file.id, force=True)

        assert forced.id == queued.id
        assert "force" not in forced.payload


async def _review(
    session: AsyncSession,
    settings: Settings,
    media_file: MediaFile,
    *,
    shortlist: bool = True,
) -> None:
    """Drive the handler's review path directly, without a whole match.

    ``shortlist`` is the difference between "below the threshold, and here are
    three shows it could be" and "a batch file" — the second reaches the queue
    with nothing to choose between.
    """
    from arc.services.library.matcher import MatchResult, Scored

    result = (
        MatchResult(candidates=(Scored(anime_id=11, episode_number=3, score=0.7),), confidence=0.7)
        if shortlist
        else MatchResult()
    )
    job = Job(type="match_file", payload={}, status=JobStatus.RUNNING)
    session.add(job)
    await session.flush()
    await library_jobs._review(
        JobContext(job=job, session=session, settings=settings, log=logging.getLogger("test")),
        media_file,
        result,
        library_jobs.REASON_LOW_CONFIDENCE,
    )


# --- The handler -------------------------------------------------------------


class TestHappyPath:
    async def test_the_answer_is_stored_with_its_provenance(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        use(monkeypatch, FakeChain(ANSWER))

        await run_suggest(db_session, suggest_settings, media_file.id)

        stored = media_file.llm_suggestion
        assert stored is not None
        assert stored["anime_id"] == 11
        assert stored["episode_number"] == 3
        assert stored["confidence"] == "high"
        assert stored["model"] == "gemini-3.5-flash"
        assert stored["provider"] == "gemini"
        assert stored["created_at"]
        assert "error" not in stored

    async def test_the_prompt_carries_the_candidates_and_the_schema(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        await add_anime(db_session, 12, "Sousou no Frieren 2nd Season")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11, 12))
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert "anime_id=11" in chain.user
        assert "anime_id=12" in chain.user
        assert "Sousou no Frieren 2nd Season" in chain.user
        assert chain.name == "match_suggestion"
        assert chain.schema["properties"].keys() == {
            "anime_id",
            "episode_number",
            "reason",
            "confidence",
        }

    async def test_a_downloaded_file_carries_the_expected_episode_prior(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Derived from the save path, so it survives an ask days later."""
        await add_anime(db_session, 11, "Sousou no Frieren")
        episode = Episode(anime_id=11, number=3, state=EpisodeState.MATCHING)
        db_session.add(episode)
        await db_session.flush()
        media_file = await add_file(
            db_session,
            tmp_path,
            directory=f"downloads/{episode.id}",
            candidates=candidates(11),
        )
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert "Arc downloaded this file itself" in chain.user
        assert "episode 3" in chain.user

    async def test_a_manual_drop_has_no_prior(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert "Arc downloaded" not in chain.user

    async def test_an_answer_naming_a_show_that_was_not_offered_stores_null(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        use(monkeypatch, FakeChain({**ANSWER, "anime_id": 999}))

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert media_file.llm_suggestion is not None
        assert media_file.llm_suggestion["anime_id"] is None


class TestNeverLinks:
    """FR-L5's non-negotiable, from three directions."""

    async def test_a_confident_suggestion_still_does_not_link(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        use(monkeypatch, FakeChain(ANSWER))

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert media_file.episode_id is None
        assert media_file.review_state is ReviewState.PENDING
        assert (await db_session.scalars(select(Episode))).all() == []

    async def test_the_confidence_and_the_candidates_are_untouched(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The suggestion is written beside the match, never over it."""
        await add_anime(db_session, 11, "Sousou no Frieren")
        stored = candidates(11)
        media_file = await add_file(db_session, tmp_path, candidates=stored)
        media_file.match_confidence = 0.62
        use(monkeypatch, FakeChain(ANSWER))

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert media_file.match_confidence == 0.62
        assert media_file.match_candidates == stored

    async def test_no_transcode_is_queued(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        use(monkeypatch, FakeChain(ANSWER))

        await run_suggest(db_session, suggest_settings, media_file.id)

        queued = await db_session.scalars(select(Job).where(Job.type == "transcode"))
        assert queued.all() == []


class TestIdempotence:
    async def test_a_file_that_went_away_is_a_no_op(
        self, db_session: AsyncSession, suggest_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, 987654)

        assert chain.calls == 0

    @pytest.mark.parametrize(
        "state", [ReviewState.AUTO, ReviewState.CONFIRMED, ReviewState.IGNORED]
    )
    async def test_a_file_no_longer_pending_is_skipped(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state: ReviewState,
    ) -> None:
        """Somebody resolved it while the job sat in the queue."""
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(
            db_session, tmp_path, review_state=state, candidates=candidates(11)
        )
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert chain.calls == 0
        assert media_file.llm_suggestion is None

    async def test_an_existing_suggestion_is_not_re_asked(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        already = {"anime_id": 11, "reason": "already answered", "confidence": "low"}
        media_file = await add_file(
            db_session, tmp_path, candidates=candidates(11), suggestion=already
        )
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert chain.calls == 0
        assert media_file.llm_suggestion == already

    async def test_force_replaces_it(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(
            db_session,
            tmp_path,
            candidates=candidates(11),
            suggestion={"error": "not configured"},
        )
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, media_file.id, force=True)

        assert chain.calls == 1
        assert media_file.llm_suggestion is not None
        assert media_file.llm_suggestion["anime_id"] == 11


class TestFailures:
    async def test_an_unavailable_chain_raises_so_the_queue_retries(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The one outcome another attempt could fix."""
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        use(monkeypatch, FakeChain(error=RecsUnavailable("every model is on cooldown")))

        with pytest.raises(RecsUnavailable):
            await run_suggest(db_session, suggest_settings, media_file.id)

        assert media_file.llm_suggestion is None

    async def test_a_refusal_is_stored_rather_than_retried(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        use(monkeypatch, FakeChain(error=RecsRefused("declined", stop_details="policy")))

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert media_file.llm_suggestion is not None
        assert media_file.llm_suggestion["error"] == library_jobs.SUGGEST_REFUSED
        assert media_file.episode_id is None

    async def test_an_unusable_answer_is_stored_rather_than_retried(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        use(monkeypatch, FakeChain(error=RecsFailed("truncated")))

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert media_file.llm_suggestion is not None
        assert "truncated" in media_file.llm_suggestion["error"]

    async def test_an_answer_that_is_not_an_object_is_stored_as_an_error(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        chain = FakeChain()
        chain.answer = ["not", "an", "object"]
        use(monkeypatch, chain)

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert media_file.llm_suggestion is not None
        assert media_file.llm_suggestion["error"]
        assert media_file.llm_suggestion["model"] == "gemini-3.5-flash"

    async def test_a_file_with_no_candidates_is_never_asked_about(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the reason sentence: there is nothing to choose between."""
        media_file = await add_file(
            db_session, tmp_path, candidates=[{"reason": "no good candidates"}]
        )
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert chain.calls == 0
        assert media_file.llm_suggestion is not None
        assert media_file.llm_suggestion["error"] == library_jobs.SUGGEST_NO_CANDIDATES

    async def test_an_unconfigured_chain_records_why(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale job, or the operator turning the feature off mid-queue."""
        settings = suggest_settings.model_copy(update={"gemini_api_key": None})
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, settings, media_file.id)

        assert chain.calls == 0
        assert media_file.llm_suggestion is not None
        assert media_file.llm_suggestion["error"] == library_jobs.SUGGEST_NOT_CONFIGURED

    async def test_a_candidate_whose_row_has_gone_is_dropped(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11, 4242))
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, media_file.id)

        assert "anime_id=4242" not in chain.user
        assert "anime_id=11" in chain.user


class TestAFailedReAskKeepsAGoodAnswer:
    """``force`` must be safe to press (FR-L6: resolve an item in a minute).

    A re-ask that trades a usable suggestion for "the model declined" would
    make the button a gamble, so a failure that lands on a row which already
    has an answer goes into ``last_error`` beside it.
    """

    async def seed_answered(
        self, session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> MediaFile:
        await add_anime(session, 11, "Sousou no Frieren")
        media_file = await add_file(session, tmp_path, candidates=candidates(11))
        use(monkeypatch, FakeChain(ANSWER))
        await run_suggest(session, self.settings, media_file.id)
        return media_file

    settings: Settings

    @pytest.fixture(autouse=True)
    def _settings(self, suggest_settings: Settings) -> None:
        self.settings = suggest_settings

    @pytest.mark.parametrize(
        "error",
        [
            RecsRefused("declined", stop_details="policy"),
            RecsFailed("truncated"),
        ],
        ids=["refusal", "unusable"],
    )
    async def test_the_answer_survives_and_the_failure_is_filed_beside_it(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        media_file = await self.seed_answered(db_session, tmp_path, monkeypatch)
        use(monkeypatch, FakeChain(error=error))

        await run_suggest(db_session, suggest_settings, media_file.id, force=True)

        stored = media_file.llm_suggestion
        assert stored is not None
        assert stored["anime_id"] == 11
        assert stored["confidence"] == "high"
        assert "error" not in stored
        assert stored["last_error"]["error"]
        assert stored["last_error"]["created_at"]

    async def test_an_empty_shortlist_does_not_wipe_it_either(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The candidates can be rewritten by a re-match under the suggestion."""
        media_file = await self.seed_answered(db_session, tmp_path, monkeypatch)
        media_file.match_candidates = [{"reason": "no good candidates"}]
        use(monkeypatch, FakeChain(ANSWER))

        await run_suggest(db_session, suggest_settings, media_file.id, force=True)

        stored = media_file.llm_suggestion
        assert stored is not None
        assert stored["anime_id"] == 11
        assert stored["last_error"]["error"] == library_jobs.SUGGEST_NO_CANDIDATES

    async def test_a_row_with_nothing_worth_keeping_is_replaced(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The other direction: a failure over an old failure is just a failure."""
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(
            db_session,
            tmp_path,
            candidates=candidates(11),
            suggestion={"error": "not configured", "model": None, "created_at": "2026-09-01"},
        )
        use(monkeypatch, FakeChain(error=RecsRefused("declined")))

        await run_suggest(db_session, suggest_settings, media_file.id, force=True)

        stored = media_file.llm_suggestion
        assert stored is not None
        assert stored["error"] == library_jobs.SUGGEST_REFUSED
        assert "last_error" not in stored

    async def test_an_unasked_row_records_a_plain_error(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        media_file = await add_file(db_session, tmp_path, candidates=candidates(11))
        use(monkeypatch, FakeChain(error=RecsRefused("declined")))

        await run_suggest(db_session, suggest_settings, media_file.id)

        stored = media_file.llm_suggestion
        assert stored is not None
        assert stored["error"] == library_jobs.SUGGEST_REFUSED
        assert "last_error" not in stored

    async def test_a_later_success_drops_the_note(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``last_error`` is about an attempt, not about the file."""
        media_file = await self.seed_answered(db_session, tmp_path, monkeypatch)
        use(monkeypatch, FakeChain(error=RecsRefused("declined")))
        await run_suggest(db_session, suggest_settings, media_file.id, force=True)
        use(monkeypatch, FakeChain({**ANSWER, "confidence": "medium"}))

        await run_suggest(db_session, suggest_settings, media_file.id, force=True)

        stored = media_file.llm_suggestion
        assert stored is not None
        assert stored["confidence"] == "medium"
        assert "last_error" not in stored


class TestTheChainIsPerProcess:
    """Cooldowns must outlive one job, or a burst re-spends a spent model."""

    async def test_every_job_is_handed_the_same_chain(
        self,
        db_session: AsyncSession,
        suggest_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_anime(db_session, 11, "Sousou no Frieren")
        first = await add_file(db_session, tmp_path, name="a.mkv", candidates=candidates(11))
        second = await add_file(db_session, tmp_path, name="b.mkv", candidates=candidates(11))
        chain = use(monkeypatch, FakeChain(ANSWER))
        assert chain is not None

        await run_suggest(db_session, suggest_settings, first.id)
        await run_suggest(db_session, suggest_settings, second.id)

        assert chain.calls == 2
        # Never closed by a handler: the worker owns it and closes it at
        # shutdown, so a second job still has a live client.
        assert chain.closed is False

    def test_the_factory_caches_one_and_hands_it_back(self, suggest_settings: Settings) -> None:
        from arc.services.recs import factory

        first = factory.shared_model(suggest_settings)
        second = factory.shared_model(suggest_settings)

        assert first is not None
        assert first is second
        assert factory.reset_shared_model() is first
        # …and after a reset it is built again rather than remembered as None.
        assert factory.shared_model(suggest_settings) is not first

    def test_an_unconfigured_chain_is_not_rebuilt_every_call(
        self, suggest_settings: Settings
    ) -> None:
        """``None`` is an answer, so it must not read as "not built yet"."""
        from arc.services.recs import factory

        settings = suggest_settings.model_copy(update={"gemini_api_key": None})

        assert factory.shared_model(settings) is None
        assert factory._shared_built is True


def test_the_stored_shape_is_json_serialisable() -> None:
    """It goes into JSONB, so nothing exotic may reach it."""
    from arc.services.library.suggest import Suggestion

    blob = {
        **Suggestion(anime_id=11, episode_number=3, reason="r", confidence="high").as_dict(),
        "model": "gemini-3.5-flash",
        "provider": "gemini",
        "created_at": "2026-09-10T12:00:00+00:00",
    }

    assert json.loads(json.dumps(blob)) == blob
