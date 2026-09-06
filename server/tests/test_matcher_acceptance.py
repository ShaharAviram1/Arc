"""The matcher, measured (FR-L3, FR-L4, roadmap M5 definition of done).

98 labelled cases in ``tests/fixtures/match_cases.json`` are run against a
478-title AniList catalogue captured from the live API by
``scripts/capture_match_catalogue.py``. The catalogue is deliberately not a
list of right answers: it is what the searches actually returned, so every
case sits next to its own sequels, side stories, movies and shorts — which are
the candidates that make a match hard.

Three numbers are asserted, and they are the ones the milestone is written
against.

**Precision on auto-links must be 100 %.** Not "high": a wrong automatic link
is the failure mode the whole design exists to prevent (FR-L4, and CLAUDE.md's
non-negotiable), and one is a bug, not a percentage point.

**Recall must be at least 85 %** — that share of the cases auto-linked to the
right show and the right episode. Below that the review queue is where the
library lives and nobody empties it.

**Everything else must be reviewable**: the expected show has to be among the
top three candidates of the review item, so resolving it is one click.

An auto-link here means what it means in production: the confidence clears
``MATCH_AUTO_THRESHOLD`` *and* the title clears ``MATCH_MIN_TITLE_FOR_AUTO``
(:meth:`MatchResult.auto_links`). The second bar costs about four points of
recall and is what stops *Kaijuu 9-gou* being filed under *Kaijuu 8-gou*.

The run is done twice, because the two are genuinely different code paths:

* ``seeded`` — the whole catalogue is in the local ``anime`` cache, which is
  what a library that has been running for a week looks like. Candidates come
  from the rapidfuzz sweep, with synonyms and relations attached.
* ``cold`` — the cache is empty and every candidate comes from a catalogue
  search that returns summaries only. That is what the first file after a
  fresh install looks like, and it is the weaker of the two by construction:
  no synonyms, no relations, so no absolute numbering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime
from arc.services.catalog import Breaker, CatalogService, upsert_detail
from arc.services.library.matcher import MatchResult, match
from arc.services.library.parser import parse
from tests.anilist_mock import (
    FakeAniList,
    match_catalogue,
    match_catalogue_fake,
    match_catalogue_media,
)
from tests.mal_mock import FakeMal

pytestmark = pytest.mark.pg

CASES_FILE = Path(__file__).parent / "fixtures" / "match_cases.json"

AUTO_THRESHOLD = 0.85
MIN_CANDIDATE = 0.40
#: ``MATCH_MIN_TITLE_FOR_AUTO``'s default. Written out rather than read from
#: settings for the same reason the two above are: these are the numbers the
#: milestone is measured at, not whatever an environment happens to say.
MIN_TITLE_FOR_AUTO = 0.92

#: The milestone's floors. A wrong auto-link is a bug, so precision is 1.0 and
#: not a number to be negotiated downwards.
MIN_PRECISION = 1.0
MIN_RECALL_SEEDED = 0.85
#: The cold run has no synonyms and no relations to work with, so it is held
#: to a lower bar — but still to one, because "the first file after an install
#: goes to review" is a bad first impression. Its *reviewability* is not
#: asserted at all: with an empty cache the candidate pool is one page of
#: search results, and a show whose right answer is not on that page cannot be
#: in the top three of anything. One scan later the cache has it.
MIN_RECALL_COLD = 0.70

#: AniList ids the named tests below reach for by hand.
KAIJUU_8_GOU = 153288
#: *Made in Abyss: Dawn of the Deep Soul* — a film whose romaji title a
#: release writes out in full, so the key matches exactly.
ABYSS_MOVIE = 100643
FRIEREN = 154587

#: How many failures the report prints in full. Enough to debug a regression,
#: few enough that a cold run's dozen does not bury the numbers.
MAX_REPORTED = 5
#: How far down the review candidates the expected show may be.
TOP_N = 3


class Case(NamedTuple):
    name: str
    expected_title: str
    expected_anilist_id: int
    expected_episode: int
    note: str

    @property
    def id(self) -> str:
        return self.name


def load_cases() -> list[Case]:
    payload = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return [Case(**row) for row in payload]


CASES = load_cases()


@dataclass
class Outcome:
    """What happened to one case."""

    case: Case
    result: MatchResult
    anilist_ids: dict[int, int]  # internal id → anilist id

    @property
    def auto(self) -> bool:
        return self.result.auto_links(AUTO_THRESHOLD, min_title=MIN_TITLE_FOR_AUTO)

    @property
    def correct(self) -> bool:
        best = self.result.best
        if best is None:
            return False
        return (
            self.anilist_ids.get(best.anime_id) == self.case.expected_anilist_id
            and best.episode_number == self.case.expected_episode
        )

    @property
    def reviewable(self) -> bool:
        """Is the right show among the top candidates a person would see?"""
        return any(
            self.anilist_ids.get(scored.anime_id) == self.case.expected_anilist_id
            for scored in self.result.candidates[:TOP_N]
        )

    def describe(self) -> str:
        best = self.result.best
        got = "nothing"
        if best is not None:
            got = f"anilist={self.anilist_ids.get(best.anime_id)} ep={best.episode_number}"
        return (
            f"{self.case.name}\n"
            f"    want anilist={self.case.expected_anilist_id} "
            f"({self.case.expected_title}) ep={self.case.expected_episode}\n"
            f"    got  {got} confidence={self.result.confidence:.3f} "
            f"auto={self.auto} top{TOP_N}={self.reviewable}"
        )


def test_the_fixture_is_big_enough_and_self_consistent() -> None:
    """Every case names a real catalogue entry, by exact title.

    Exact string equality, not a search: resolving the expectation with the
    same fuzzy logic that is under test would make the fixture agree with the
    matcher by construction.
    """
    assert len(CASES) >= 50
    titles: dict[int, set[str]] = {}
    for node in match_catalogue():
        names = {value for value in (node.get("title") or {}).values() if value}
        titles[int(node["id"])] = names

    for case in CASES:
        assert case.expected_anilist_id in titles, case.name
        assert case.expected_title in titles[case.expected_anilist_id], case.name
        assert case.expected_episode >= 1, case.name
        assert case.note, case.name


@pytest.fixture
def catalog() -> CatalogService:
    """AniList over the bundled catalogue; MAL configured but never reached."""
    return CatalogService(match_catalogue_fake().source(), FakeMal().source(), Breaker(300.0))


async def seed_cache(session: AsyncSession) -> None:
    """Write the whole captured catalogue into ``anime`` as detail rows."""
    for media in match_catalogue_media():
        await upsert_detail(session, media)
    await session.flush()


async def anilist_ids(session: AsyncSession) -> dict[int, int]:
    rows = await session.execute(
        select(Anime.id, Anime.anilist_id).where(Anime.anilist_id.is_not(None))
    )
    return {internal: anilist for internal, anilist in rows.all()}


async def run_all(session: AsyncSession, service: CatalogService, *, seeded: bool) -> list[Outcome]:
    if seeded:
        await seed_cache(session)
    outcomes: list[Outcome] = []
    for case in CASES:
        result = await match(
            session, service, parse(case.name), expected=None, minimum=MIN_CANDIDATE
        )
        outcomes.append(Outcome(case, result, await anilist_ids(session)))
    return outcomes


def report(label: str, outcomes: list[Outcome]) -> dict[str, Any]:
    total = len(outcomes)
    auto = [item for item in outcomes if item.auto]
    good_auto = [item for item in auto if item.correct]
    bad_auto = [item for item in auto if not item.correct]
    review = [item for item in outcomes if not item.auto]
    unreviewable = [item for item in review if not item.reviewable]
    return {
        "label": label,
        "total": total,
        "auto": len(auto),
        "precision": len(good_auto) / len(auto) if auto else 1.0,
        "recall": len(good_auto) / total,
        "review": len(review),
        "reviewable": (len(review) - len(unreviewable)) / len(review) if review else 1.0,
        "wrong_auto": bad_auto,
        "unreviewable": unreviewable,
    }


def announce(numbers: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    with capsys.disabled():
        print(
            f"\nmatcher [{numbers['label']}]: {numbers['total']} cases | "
            f"auto-linked {numbers['auto']} | "
            f"precision {numbers['precision']:.1%} | "
            f"recall {numbers['recall']:.1%} | "
            f"review {numbers['review']} "
            f"(expected in top {TOP_N}: {numbers['reviewable']:.1%})"
        )
        for item in numbers["wrong_auto"][:MAX_REPORTED]:
            print("  WRONG AUTO-LINK\n" + item.describe())
        for item in numbers["unreviewable"][:MAX_REPORTED]:
            print("  NOT REVIEWABLE\n" + item.describe())


class TestSeeded:
    """The steady state: the catalogue is already in the local cache."""

    async def test_the_numbers(
        self, db_session: AsyncSession, catalog: CatalogService, capsys: pytest.CaptureFixture[str]
    ) -> None:
        numbers = report("seeded cache", await run_all(db_session, catalog, seeded=True))
        announce(numbers, capsys)

        assert numbers["precision"] >= MIN_PRECISION, "an auto-link named the wrong show"
        assert numbers["recall"] >= MIN_RECALL_SEEDED
        assert numbers["reviewable"] == 1.0, "a review item without the answer in its top three"


class TestCold:
    """The first file after a fresh install: nothing is cached."""

    async def test_the_numbers(
        self, db_session: AsyncSession, catalog: CatalogService, capsys: pytest.CaptureFixture[str]
    ) -> None:
        numbers = report("cold cache", await run_all(db_session, catalog, seeded=False))
        announce(numbers, capsys)

        assert numbers["precision"] >= MIN_PRECISION, "an auto-link named the wrong show"
        assert numbers["recall"] >= MIN_RECALL_COLD


class TestNegatives:
    """Cases the matcher must *not* answer confidently."""

    @pytest.mark.parametrize(
        "name",
        [
            "[Group] Totally Unknown Show - 02 [720p].mkv",
            "[Group] Qwertyuiop Asdfghjkl - 04 [1080p].mkv",
            "[Group] 33333333 - 01 [1080p].mkv",
        ],
    )
    async def test_a_show_that_does_not_exist_is_never_auto_linked(
        self, db_session: AsyncSession, catalog: CatalogService, name: str
    ) -> None:
        await seed_cache(db_session)
        result = await match(db_session, catalog, parse(name), minimum=MIN_CANDIDATE)
        assert not result.auto_links(AUTO_THRESHOLD), result.candidates

    async def test_an_episode_far_past_the_count_is_not_auto_linked(
        self, db_session: AsyncSession, catalog: CatalogService
    ) -> None:
        """Frieren has 28 episodes; there is no episode 400."""
        await seed_cache(db_session)
        result = await match(
            db_session,
            catalog,
            parse("[SubsPlease] Sousou no Frieren - 400.mkv"),
            minimum=MIN_CANDIDATE,
        )
        assert not result.auto_links(AUTO_THRESHOLD)

    async def test_a_movie_whose_name_is_not_the_movies_is_not_auto_linked(
        self, db_session: AsyncSession, catalog: CatalogService
    ) -> None:
        """A movie *is* linkable now (see :class:`TestMovies`) — this one is not.

        The release calls itself "Jujutsu Kaisen 0" and the parser reads the
        ``0`` as a number, so the title it offers is plain "Jujutsu Kaisen":
        the name of the TV show, which is not a film, and a near-miss for the
        film, which is called something else. Both bars stop it.
        """
        await seed_cache(db_session)
        result = await match(
            db_session,
            catalog,
            parse("[Judas] Jujutsu Kaisen 0 (Movie) [1080p][HEVC].mkv"),
            minimum=MIN_CANDIDATE,
        )
        assert not result.auto_links(AUTO_THRESHOLD, min_title=MIN_TITLE_FOR_AUTO)

    async def test_a_near_miss_on_the_title_is_never_auto_linked(
        self, db_session: AsyncSession, catalog: CatalogService
    ) -> None:
        """There is no *Kaijuu 9-gou*, and *Kaijuu 8-gou* is not it (FR-L4).

        Everything but the title agrees — no season either side, TV against an
        episode file, the same year, and episode 5 exists — so the weighted
        sum clears 0.85 on a title similarity of 0.88. This is the case
        ``MATCH_MIN_TITLE_FOR_AUTO`` exists for.
        """
        await seed_cache(db_session)
        result = await match(
            db_session,
            catalog,
            parse("[SubsPlease] Kaijuu 9-gou - 05 (1080p) [ABCD1234].mkv"),
            minimum=MIN_CANDIDATE,
        )

        assert result.confidence >= AUTO_THRESHOLD, "the confidence alone would have linked it"
        assert not result.auto_links(AUTO_THRESHOLD, min_title=MIN_TITLE_FOR_AUTO)
        # …and Kaijuu 8-gou is still the top candidate a person is shown.
        best = result.best
        assert best is not None
        assert (await anilist_ids(db_session))[best.anime_id] == KAIJUU_8_GOU


class TestMovies:
    """FR-L4: a film is episode 1 of itself, and links like anything else."""

    MOVIE = "[Judas] Made in Abyss - Fukaki Tamashii no Reimei (Movie) [1080p][HEVC].mkv"

    async def test_a_movie_links_to_episode_one_of_the_movie_entry(
        self, db_session: AsyncSession, catalog: CatalogService
    ) -> None:
        await seed_cache(db_session)
        result = await match(db_session, catalog, parse(self.MOVIE), minimum=MIN_CANDIDATE)

        assert result.auto_links(AUTO_THRESHOLD, min_title=MIN_TITLE_FOR_AUTO)
        best = result.best
        assert best is not None
        assert best.episode_number == 1
        assert (await anilist_ids(db_session))[best.anime_id] == ABYSS_MOVIE

    async def test_a_movie_with_only_the_tv_show_in_the_cache_goes_to_review(
        self, db_session: AsyncSession, catalog: CatalogService
    ) -> None:
        """The series of the same name is not the film (FR-L4).

        Nothing is seeded and the catalogue is asked for *Sousou no Frieren*,
        which has no film in this fixture — so the pool is the TV entries, the
        format component scores them at zero, and none of them is handed an
        episode number to link with.
        """
        result = await match(
            db_session,
            catalog,
            parse("[Erai-raws] Gekijouban Sousou no Frieren [1080p][Multiple Subtitle].mkv"),
            minimum=MIN_CANDIDATE,
        )

        assert not result.auto_links(AUTO_THRESHOLD, min_title=MIN_TITLE_FOR_AUTO)
        assert all(scored.episode_number is None for scored in result.candidates)


class TestPrior:
    """FR-L3: a file Arc downloaded starts with a strong prior."""

    async def test_the_prior_decides_a_case_that_would_go_to_review(
        self, db_session: AsyncSession, catalog: CatalogService
    ) -> None:
        """Solo Leveling season 2 — a real case from the corpus above.

        "Solo Leveling" is exactly the English title of season *one*, and the
        catalogue names season two with a subtitle, so on the filename alone
        the two land within :data:`AMBIGUITY_MARGIN` and the file goes to
        review. Told which episode it was fetching, Arc links it (FR-L3).
        """
        await seed_cache(db_session)
        season_two = await db_session.scalar(select(Anime).where(Anime.anilist_id == 176496))
        assert season_two is not None
        name = "[Yameii] Solo Leveling - S02E03 [English Dub] [1080p][WEB-DL][AAC].mkv"

        without = await match(db_session, catalog, parse(name), minimum=MIN_CANDIDATE)
        with_prior = await match(
            db_session, catalog, parse(name), expected=(season_two.id, 3), minimum=MIN_CANDIDATE
        )

        assert not without.auto_links(AUTO_THRESHOLD, min_title=MIN_TITLE_FOR_AUTO)
        assert with_prior.auto_links(AUTO_THRESHOLD, min_title=MIN_TITLE_FOR_AUTO)
        best = with_prior.best
        assert best is not None
        assert (best.anime_id, best.episode_number) == (season_two.id, 3)

    async def test_the_prior_does_not_override_a_title_that_says_otherwise(
        self, db_session: AsyncSession, catalog: CatalogService
    ) -> None:
        """0.6 buys a bad filename the benefit of the doubt, not a wrong show."""
        await seed_cache(db_session)
        frieren = await db_session.scalar(select(Anime).where(Anime.anilist_id == 154587))
        assert frieren is not None

        result = await match(
            db_session,
            catalog,
            parse("[Kametsu] Cowboy Bebop - 12 (BD 1080p Hi10P FLAC) [DEADBEEF].mkv"),
            expected=(frieren.id, 12),
            minimum=MIN_CANDIDATE,
        )

        best = result.best
        assert best is not None
        assert best.anime_id != frieren.id

    async def test_a_prior_with_no_rival_at_all_still_does_not_link(
        self, db_session: AsyncSession
    ) -> None:
        """The same case with the rival taken away, which is the real bug.

        The test above passes because Cowboy Bebop happens to be in the cache
        and outscores the lifted candidate. Take it away — an empty catalogue
        and a cache holding only the show Arc was fetching, which is what a
        new install's first download looks like — and the lifted candidate is
        alone at the top. Ungated it scored 0.87 there and Arc filed a Cowboy
        Bebop episode under Frieren with nobody asked. The gate is on the
        prior's *own* title, so it holds with or without a rival.
        """
        empty = CatalogService(FakeAniList().source(), FakeMal().source(), Breaker(300.0))
        for media in match_catalogue_media():
            if media.anilist_id == FRIEREN:
                await upsert_detail(db_session, media)
        await db_session.flush()
        frieren = await db_session.scalar(select(Anime).where(Anime.anilist_id == FRIEREN))
        assert frieren is not None

        result = await match(
            db_session,
            empty,
            parse("[Kametsu] Cowboy Bebop - 12 (BD 1080p Hi10P FLAC) [DEADBEEF].mkv"),
            expected=(frieren.id, 12),
            minimum=MIN_CANDIDATE,
        )

        lifted = next(item for item in result.candidates if item.anime_id == frieren.id)
        assert lifted.prior is False
        assert lifted.score < AUTO_THRESHOLD
        assert "expected episode for this download, but the title disagrees" in lifted.reasons
        assert not result.auto_links(AUTO_THRESHOLD, min_title=MIN_TITLE_FOR_AUTO)
