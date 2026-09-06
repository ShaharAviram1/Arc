"""The matcher's pure half (FR-L3, FR-L4).

Everything here is a function of literals: a :class:`ParsedName`, a
:class:`Candidate`, and the number that comes out. No database, no catalogue,
no clock — which is the whole reason the scoring was written as pure functions
in the first place (architecture.md §5.2).

The acceptance run over a real catalogue is ``test_matcher_acceptance.py``;
this file is about *why* each component moves the score.
"""

from __future__ import annotations

import pytest

from arc.services.library.matcher import (
    ABSOLUTE_PENALTY,
    AMBIGUITY_MARGIN,
    AMBIGUOUS_CEILING,
    MOVIE_EPISODE,
    NON_EXACT_CEILING,
    PRIOR_MIN_TITLE,
    PRIOR_WEIGHT,
    TITLE_WEIGHT,
    Candidate,
    MatchResult,
    Scored,
    confidence_of,
    episode_plausibility,
    format_agreement,
    offset_candidates,
    rank,
    score,
    season_agreement,
    season_in_title,
    title_similarity,
    year_agreement,
)
from arc.services.library.parser import parse

AUTO = 0.85
MINIMUM = 0.40
#: ``MATCH_MIN_TITLE_FOR_AUTO``'s default, as a literal: these tests are about
#: the rule, and reading the setting here would make them agree with whatever
#: it happens to say.
MIN_TITLE = 0.92

#: A filename whose title is unreadable — 0.48 against Frieren — and one that
#: is merely written badly, at about 0.79. The gap between them is the whole
#: of :data:`PRIOR_MIN_TITLE`.
UNREADABLE = "[G] mysterious.release.name - 05 [1080p].mkv"
SHORTENED = "[G] Frieren - 05 [1080p].mkv"


def frieren(**overrides: object) -> Candidate:
    """The common candidate: a finished 28-episode TV show."""
    base = {
        "anime_id": 1,
        "anilist_id": 154587,
        "titles": ("Sousou no Frieren", "Frieren: Beyond Journey's End", "葬送のフリーレン"),
        "format": "TV",
        "episodes": 28,
        "status": "FINISHED",
        "season_year": 2023,
    }
    base.update(overrides)
    return Candidate(**base)  # type: ignore[arg-type]


class TestTitleSimilarity:
    def test_an_exact_key_is_one(self) -> None:
        assert title_similarity("sousou no frieren", ("sousou no frieren",)) == 1.0

    def test_the_best_of_several_titles_wins(self) -> None:
        """The English title is a near miss; the romaji is not even close."""
        candidate = frieren()
        near = title_similarity("frieren beyond journeys end", candidate.keys)
        far = title_similarity("cowboy bebop", candidate.keys)
        assert near == NON_EXACT_CEILING > far

    def test_an_exact_key_outscores_every_near_miss(self) -> None:
        """The step, not the ratio, is what separates a show from a relative.

        The gap has to be worth more than :data:`AMBIGUITY_MARGIN` once
        :data:`TITLE_WEIGHT` is applied, or "Chihayafuru 3" and "Chihayafuru"
        are indistinguishable and every such file goes to review forever.
        """
        exact = title_similarity("sousou no frieren", ("sousou no frieren",))
        near = title_similarity("sousou no frieren", ("sousou no frieren no mahou",))
        assert exact == 1.0
        assert near <= NON_EXACT_CEILING
        assert (exact - near) * TITLE_WEIGHT > AMBIGUITY_MARGIN

    def test_a_synonym_is_never_an_exact_hit(self) -> None:
        """Sequels in the catalogue routinely list the base show as a synonym.

        Letting that count as "the same title" made *xxxHOLiC◆Kei* score
        exactly as well as *xxxHOLiC* on a file that said only "xxxHOLiC".
        """
        sequel = frieren(titles=("xxxHOLiC Kei",), synonyms=("xxxHOLiC",))
        assert "xxxholic" in sequel.keys
        assert "xxxholic" not in sequel.primary_keys
        assert (
            title_similarity("xxxholic", sequel.keys, exact_keys=sequel.primary_keys)
            <= NON_EXACT_CEILING
        )

    def test_a_stripped_season_makes_a_sequel_an_exact_hit_on_the_title(self) -> None:
        """And the season component is what then tells them apart.

        ``Vinland Saga Season 2`` reduces to the same key as ``Vinland Saga``
        on purpose: charging the candidate for the season *and* scoring the
        season is charging it twice.
        """
        sequel = frieren(titles=("Vinland Saga Season 2",))
        assert sequel.primary_keys == ("vinland saga",)
        assert season_agreement(parse("[G] Vinland Saga - 05 [1080p].mkv"), sequel) < 0.3

    def test_a_partial_title_still_scores(self) -> None:
        """A group that writes only "Frieren" must stay in the running."""
        assert title_similarity("frieren", frieren().keys) > 0.55

    def test_a_different_show_scores_low(self) -> None:
        assert title_similarity("cowboy bebop", frieren().keys) < 0.4

    def test_a_short_title_does_not_win_by_containment(self) -> None:
        """``partial_ratio`` alone would score these equal; the mix does not.

        This is the bug the whole-string half of the formula exists for: with
        ``partial_ratio`` only, "overlord" scores 1.0 against "Overlord IV",
        and a first season would beat its own fourth every time.
        """
        exact = title_similarity("overlord", ("Overlord",))
        contained = title_similarity("overlord", ("Overlord IV",))
        assert exact > contained

    def test_nothing_to_compare_is_zero(self) -> None:
        assert title_similarity("", ("frieren",)) == 0.0
        assert title_similarity("frieren", ()) == 0.0


class TestSeasonInTitle:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Vinland Saga Season 2", 2),
            ("Mob Psycho 100 III", 3),
            ("Boku no Hero Academia 7th Season", 7),
            ("Shingeki no Kyojin S3", 3),
            ("Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season Part 2", 2),
            ("Sousou no Frieren", None),
            ("Steins;Gate 0", None),
            ("86: Eighty Six", None),
        ],
    )
    def test_reads_a_catalogue_title(self, name: str, expected: int | None) -> None:
        assert season_in_title(name) == expected


class TestEpisodePlausibility:
    def test_inside_a_known_count_is_certain(self) -> None:
        assert episode_plausibility(parse("Frieren - 05.mkv"), frieren(), 5) == 1.0

    def test_the_last_episode_is_certain(self) -> None:
        assert episode_plausibility(parse("Frieren - 28.mkv"), frieren(), 28) == 1.0

    def test_one_past_the_count_is_a_stale_cache(self) -> None:
        """A finale that aired before the last refresh is not a wrong match."""
        value = episode_plausibility(parse("Frieren - 29.mkv"), frieren(), 29)
        assert 0.5 < value < 1.0

    def test_far_past_the_count_collapses(self) -> None:
        assert episode_plausibility(parse("Frieren - 90.mkv"), frieren(), 90) < 0.15

    def test_an_unknown_count_while_airing_beats_an_unknown_count_while_not(self) -> None:
        airing = frieren(episodes=None, status="RELEASING")
        finished = frieren(episodes=None, status="FINISHED")
        parsed = parse("Frieren - 41.mkv")
        assert episode_plausibility(parsed, airing, 41) > episode_plausibility(parsed, finished, 41)

    def test_a_movie_has_nothing_to_place(self) -> None:
        parsed = parse("[G] Some Movie (Movie) [1080p].mkv")
        assert episode_plausibility(parsed, frieren(format="MOVIE", episodes=1), None) == 1.0

    def test_episode_zero_is_impossible(self) -> None:
        assert episode_plausibility(parse("Frieren - 05.mkv"), frieren(), 0) == 0.0


class TestSeasonAgreement:
    def test_no_season_either_side_agrees(self) -> None:
        assert season_agreement(parse("Frieren - 05.mkv"), frieren()) == 1.0

    def test_no_season_against_a_sequel_disagrees(self) -> None:
        """This is what stops ``Frieren - 05`` landing on season 2."""
        sequel = frieren(titles=("Sousou no Frieren Season 2",))
        assert season_agreement(parse("Frieren - 05.mkv"), sequel) < 0.3

    def test_matching_numbers_agree(self) -> None:
        parsed = parse("[G] Vinland Saga S2 - 05 [1080p].mkv")
        assert season_agreement(parsed, frieren(titles=("Vinland Saga Season 2",))) == 1.0

    def test_conflicting_numbers_disagree_completely(self) -> None:
        parsed = parse("[G] Vinland Saga S2 - 05 [1080p].mkv")
        assert season_agreement(parsed, frieren(titles=("Vinland Saga Season 3",))) == 0.0

    def test_season_one_against_an_unnumbered_title_agrees(self) -> None:
        parsed = parse("[G] Frieren S1 - 05 [1080p].mkv")
        assert season_agreement(parsed, frieren()) == 1.0

    def test_a_later_season_against_an_unnumbered_title_is_a_shrug(self) -> None:
        """The catalogue often carries a subtitle where a release carries a number."""
        parsed = parse("[G] Kimetsu no Yaiba S3 - 05 [1080p].mkv")
        value = season_agreement(
            parsed, frieren(titles=("Kimetsu no Yaiba: Katanakaji no Sato-hen",))
        )
        assert 0.0 < value < 1.0


class TestFormatAndYear:
    @pytest.mark.parametrize(
        ("name", "fmt", "expected"),
        [
            ("[G] Some Movie (Movie) [1080p].mkv", "MOVIE", 1.0),
            ("[G] Some Movie (Movie) [1080p].mkv", "TV", 0.0),
            ("[G] Show - OVA 02 [1080p].mkv", "OVA", 1.0),
            ("[G] Show - 02 [1080p].mkv", "TV", 1.0),
            ("[G] Show - 02 [1080p].mkv", "MOVIE", 0.2),
        ],
    )
    def test_format(self, name: str, fmt: str, expected: float) -> None:
        assert format_agreement(parse(name), frieren(format=fmt)) == expected

    def test_an_unknown_format_is_a_shrug(self) -> None:
        assert format_agreement(parse("[G] Show - 02 [1080p].mkv"), frieren(format=None)) == 0.5

    def test_year(self) -> None:
        movie = "[G] Kimi no Na wa. (2016) [Movie][1080p].mkv"
        assert year_agreement(parse(movie), frieren(season_year=2016)) == 1.0
        assert year_agreement(parse(movie), frieren(season_year=2017)) == 0.6
        assert year_agreement(parse(movie), frieren(season_year=2001)) == 0.0

    def test_a_missing_year_is_a_shrug(self) -> None:
        assert year_agreement(parse("Frieren - 05.mkv"), frieren()) == 0.5


class TestScore:
    def test_a_perfect_match_is_near_one(self) -> None:
        parsed = parse("[SubsPlease] Sousou no Frieren - 05 (1080p) [ABCD1234].mkv")
        assert score(parsed, frieren()).score > 0.95

    def test_a_wrong_show_is_far_below_the_threshold(self) -> None:
        parsed = parse("[SubsPlease] Cowboy Bebop - 05 (1080p) [ABCD1234].mkv")
        assert score(parsed, frieren()).score < AUTO

    def test_the_reasons_name_every_component(self) -> None:
        reasons = " ".join(score(parse("Frieren - 05.mkv"), frieren()).reasons)
        for component in ("title", "episode", "season", "format", "year", "from"):
            assert component in reasons

    def test_a_prior_lifts_a_weak_match(self) -> None:
        """FR-L3: a file Arc downloaded starts with a strong prior."""
        parsed = parse(SHORTENED)
        without = score(parsed, frieren())
        with_prior = score(parsed, frieren(), prior=True)
        assert with_prior.score > without.score
        assert "expected episode for this download" in with_prior.reasons

    def test_a_prior_moves_a_candidate_toward_certainty(self) -> None:
        parsed = parse(SHORTENED)
        plain = score(parsed, frieren()).score
        lifted = score(parsed, frieren(), prior=True).score
        assert lifted == pytest.approx(plain + PRIOR_WEIGHT * (1.0 - plain))

    def test_a_prior_never_beats_a_title_that_says_otherwise(self) -> None:
        """The failure the bonus form exists to prevent (FR-L3, FR-L4).

        Arc downloaded something for Frieren episode 5 and a Cowboy Bebop file
        appeared. The prior must not make Frieren the answer — and a prior
        that shared the weighted sum's denominator *would*, because it would
        divide every rival down by the same 0.6 that lifts it.
        """
        parsed = parse("[SubsPlease] Cowboy Bebop - 05 (1080p) [ABCD1234].mkv")
        bebop = Candidate(anime_id=2, titles=("Cowboy Bebop",), format="TV", episodes=26)
        assert score(parsed, bebop).score > score(parsed, frieren(), prior=True).score
        assert score(parsed, frieren(), prior=True).score < AUTO

    def test_the_prior_supplies_the_episode_number(self) -> None:
        parsed = parse("[G] Frieren [1080p].mkv")
        assert score(parsed, frieren(), episode=7, prior=True).episode_number == 7

    def test_a_penalty_is_subtracted_and_recorded(self) -> None:
        parsed = parse("[G] Sousou no Frieren - 05 [1080p].mkv")
        plain = score(parsed, frieren())
        penalised = score(parsed, frieren(), penalty=ABSOLUTE_PENALTY)
        assert penalised.score == pytest.approx(plain.score - ABSOLUTE_PENALTY)
        assert penalised.absolute is True

    def test_the_score_is_bounded(self) -> None:
        parsed = parse("[SubsPlease] Sousou no Frieren - 05 (1080p) [ABCD1234].mkv")
        assert 0.0 <= score(parsed, frieren(), prior=True).score <= 1.0


class TestThePriorNeedsATitle:
    """FR-L3 stops where FR-L4 begins: the bonus rescues a name, not a show.

    :data:`PRIOR_MIN_TITLE` is the line. Below it the prior is still *shown* —
    it is genuinely what Arc was fetching, and a person resolving the item
    wants to know that — but it is not believed, and the file goes to review.
    """

    def test_a_title_that_says_nothing_gets_no_bonus_and_goes_to_review(self) -> None:
        """0.48 on the title: the filename is not evidence for this show.

        Ungated this scored 0.87 and linked itself — an automatic link to a
        show the filename does not name, which is the guess CLAUDE.md forbids.
        """
        parsed = parse(UNREADABLE)
        plain = score(parsed, frieren())
        lifted = score(parsed, frieren(), prior=True)

        assert plain.title < PRIOR_MIN_TITLE
        assert lifted.score == pytest.approx(plain.score)
        assert lifted.score < AUTO
        assert not rank([lifted], minimum=MINIMUM).auto_links(AUTO, min_title=MIN_TITLE)

    def test_the_reason_says_why_it_was_not_believed(self) -> None:
        lifted = score(parse(UNREADABLE), frieren(), prior=True)
        assert "expected episode for this download, but the title disagrees" in lifted.reasons
        assert lifted.prior is False

    def test_a_title_above_the_line_is_believed_and_links(self) -> None:
        """0.79 on the title: written badly, but it is naming this show."""
        parsed = parse(SHORTENED)
        lifted = score(parsed, frieren(), prior=True)

        assert lifted.title >= PRIOR_MIN_TITLE
        assert lifted.prior is True
        assert lifted.score >= AUTO
        assert rank([lifted], minimum=MINIMUM).auto_links(AUTO, min_title=MIN_TITLE)

    def test_the_prior_carries_a_title_the_similarity_bar_would_stop(self) -> None:
        """The two bars are for two kinds of evidence, and the prior is better.

        A release Arc asked for itself is a stronger claim than the string in
        its filename, so a believed prior clears :data:`MIN_TITLE` on its own —
        otherwise the bonus could never decide anything, since the releases it
        exists for are precisely the ones with unreadable names (FR-L3).
        """
        lifted = score(parse(SHORTENED), frieren(), prior=True)
        assert lifted.title < MIN_TITLE
        assert not lifted.exact_title
        assert lifted.title_is_close(MIN_TITLE)


class TestTheTitleBar:
    """FR-L4: four components that agree may not outvote the one that does not.

    *Kaijuu 9-gou* against a cached *Kaijuu 8-gou* is the case: same season
    (none), same format, same year, and episode 5 exists on both — so the
    weighted sum reaches 0.91 on a title similarity of 0.88.
    """

    def kaijuu(self) -> Candidate:
        return Candidate(
            anime_id=8,
            anilist_id=153288,
            titles=("Kaijuu 8-gou", "Kaiju No. 8"),
            format="TV",
            episodes=12,
            status="FINISHED",
            season_year=2024,
        )

    def test_a_near_miss_clears_the_threshold_and_is_still_not_linked(self) -> None:
        scored = score(
            parse("[SubsPlease] Kaijuu 9-gou - 05 (1080p) [ABCD1234].mkv"), self.kaijuu()
        )
        result = rank([scored], minimum=MINIMUM)

        assert result.confidence >= AUTO, "the sum alone would have linked it"
        assert scored.title < MIN_TITLE
        assert not result.auto_links(AUTO, min_title=MIN_TITLE)

    def test_a_romanisation_variant_still_links_itself(self) -> None:
        """The bar is on the *key*, and a key ignores punctuation and case.

        "Kaiju No. 8" and "Kaiju No 8" are one name written two ways, so the
        similarity is 1.0 and the file links without anybody being asked —
        which is the whole point of having a key rather than a string.
        """
        scored = score(parse("[Yameii] Kaiju No 8 - S01E05 [1080p][WEB-DL].mkv"), self.kaijuu())
        result = rank([scored], minimum=MINIMUM)

        assert scored.exact_title
        assert result.auto_links(AUTO, min_title=MIN_TITLE)

    def test_an_exact_key_clears_any_bar(self) -> None:
        scored = score(parse("[G] Sousou no Frieren - 05 [1080p].mkv"), frieren())
        assert scored.exact_title
        assert scored.title_is_close(1.0)

    def test_the_bar_is_off_by_default(self) -> None:
        """``auto_links`` without ``min_title`` is the confidence rule alone."""
        scored = score(
            parse("[SubsPlease] Kaijuu 9-gou - 05 (1080p) [ABCD1234].mkv"), self.kaijuu()
        )
        assert rank([scored], minimum=MINIMUM).auto_links(AUTO)


class TestMovies:
    """A film is episode 1 of itself (FR-L4).

    Before this it was episode *nothing*: the parser takes the number off a
    movie release because a "01" in one is a part number, so every movie
    reached :meth:`MatchResult.auto_links` with ``episode_number is None`` and
    every movie went to review, however plainly it named itself.
    """

    NAME = "[Erai-raws] Gekijouban Sousou no Frieren [1080p][Multiple Subtitle].mkv"

    def movie(self, **overrides: object) -> Candidate:
        base: dict[str, object] = {
            "anime_id": 2,
            "titles": ("Gekijouban Sousou no Frieren",),
            "format": "MOVIE",
            "episodes": 1,
            "status": "FINISHED",
        }
        base.update(overrides)
        return Candidate(**base)  # type: ignore[arg-type]

    def test_a_movie_against_a_movie_entry_links_as_episode_one(self) -> None:
        scored = score(parse(self.NAME), self.movie())
        result = rank([scored], minimum=MINIMUM)

        assert scored.episode_number == MOVIE_EPISODE
        assert result.auto_links(AUTO, min_title=MIN_TITLE)

    def test_a_catalogue_that_publishes_no_count_is_still_one_film(self) -> None:
        assert score(parse(self.NAME), self.movie(episodes=None)).episode_number == MOVIE_EPISODE

    def test_a_movie_against_the_tv_entry_only_goes_to_review(self) -> None:
        """The TV show of the same name is not the film, and has no episode 1
        that this file could be. The format component says so and nothing
        hands it a number to link with."""
        tv = self.movie(anime_id=3, titles=("Sousou no Frieren",), format="TV", episodes=28)
        scored = score(parse(self.NAME), tv)
        result = rank([scored], minimum=MINIMUM)

        assert scored.episode_number is None
        assert not result.auto_links(AUTO, min_title=MIN_TITLE)

    def test_a_multi_part_movie_entry_is_a_real_doubt(self) -> None:
        """Which of three parts is *this* file? Nobody here knows."""
        one = score(parse(self.NAME), self.movie())
        three = score(parse(self.NAME), self.movie(episodes=3))
        assert three.score < one.score

    def test_the_episode_plausibility_of_a_film(self) -> None:
        parsed = parse(self.NAME)
        assert episode_plausibility(parsed, self.movie(), 1) == 1.0
        assert episode_plausibility(parsed, self.movie(episodes=None), 1) == 1.0
        assert episode_plausibility(parsed, self.movie(episodes=3), 1) == 0.5


class TestRecaps:
    """A fractional episode has no episode to link to (FR-L4)."""

    def test_a_recap_is_never_offered_as_the_episode_it_follows(self) -> None:
        """``12.5`` sits between 12 and 13; it is neither of them.

        The parser stores the 12 to say *where* the recap sits, and an exact
        title against a show whose episode 12 exists scores 0.94 — so without
        this the summary would be filed over the episode automatically.
        """
        parsed = parse("[Doki] Sousou no Frieren - 12.5 (1280x720 Hi10P AAC).mkv")
        assert (parsed.episode, parsed.episode_fraction) == (12, 0.5)

        scored = score(parsed, frieren())
        result = rank([scored], minimum=MINIMUM)

        assert scored.episode_number is None
        assert not result.auto_links(AUTO, min_title=MIN_TITLE)

    def test_an_ordinary_episode_is_untouched(self) -> None:
        assert (
            score(parse("[G] Sousou no Frieren - 12 [1080p].mkv"), frieren()).episode_number == 12
        )


class TestConfidence:
    def test_a_clear_winner_keeps_its_score(self) -> None:
        candidates = [Scored(1, 5, 0.94), Scored(2, 5, 0.60)]
        assert confidence_of(candidates) == 0.94

    def test_a_close_runner_up_caps_the_confidence(self) -> None:
        candidates = [Scored(1, 5, 0.94), Scored(2, 5, 0.94 - AMBIGUITY_MARGIN / 2)]
        assert confidence_of(candidates) == AMBIGUOUS_CEILING

    def test_the_cap_is_below_the_auto_threshold(self) -> None:
        """Ambiguity always means review — by construction, not by tuning."""
        assert AMBIGUOUS_CEILING < AUTO

    def test_a_close_runner_up_never_raises_the_confidence(self) -> None:
        candidates = [Scored(1, 5, 0.55), Scored(2, 5, 0.54)]
        assert confidence_of(candidates) == 0.55

    def test_the_same_show_twice_is_not_ambiguous(self) -> None:
        """Two episode numbers on one show is an ordering question, not a doubt."""
        candidates = [Scored(1, 5, 0.94), Scored(1, 33, 0.93), Scored(2, 5, 0.10)]
        assert confidence_of(candidates) == 0.94

    def test_nothing_is_zero(self) -> None:
        assert confidence_of([]) == 0.0


class TestRank:
    def test_orders_by_score_and_cuts_at_the_minimum(self) -> None:
        result = rank([Scored(1, 5, 0.30), Scored(2, 5, 0.90), Scored(3, 5, 0.60)], minimum=MINIMUM)
        assert [item.anime_id for item in result.candidates] == [2, 3]

    def test_deduplicates_on_show_and_episode(self) -> None:
        result = rank([Scored(1, 5, 0.90), Scored(1, 5, 0.80), Scored(1, 9, 0.70)], minimum=MINIMUM)
        assert [(item.anime_id, item.episode_number) for item in result.candidates] == [
            (1, 5),
            (1, 9),
        ]

    def test_an_empty_result_never_auto_links(self) -> None:
        result = rank([Scored(1, 5, 0.10)], minimum=MINIMUM)
        assert result.candidates == ()
        assert result.best is None
        assert result.auto_links(AUTO) is False

    def test_a_candidate_without_an_episode_never_auto_links(self) -> None:
        """A movie scores well and still has nothing to link to (FR-L4)."""
        result = rank([Scored(1, None, 0.99)], minimum=MINIMUM)
        assert result.confidence == 0.99
        assert result.auto_links(AUTO) is False

    def test_top_is_json_safe(self) -> None:
        import json

        result = rank([Scored(1, 5, 0.9012345, ("title 1.00",))], minimum=MINIMUM)
        payload = result.top()
        assert json.loads(json.dumps(payload)) == payload
        assert payload[0]["score"] == 0.9012

    def test_an_empty_result_object(self) -> None:
        assert MatchResult().best is None
        assert MatchResult().auto_links(0.0) is False


class TestOffsetCandidates:
    """Absolute numbering across a sequel chain."""

    def relation(self, anilist_id: int, kind: str = "SEQUEL") -> dict[str, object]:
        return {"anilist_id": anilist_id, "mal_id": None, "relation_type": kind}

    def pool(self, relation_type: str = "SEQUEL") -> list[Candidate]:
        first = Candidate(
            anime_id=1,
            anilist_id=100,
            titles=("Vinland Saga",),
            format="TV",
            episodes=24,
            status="FINISHED",
            relations=(self.relation(200, relation_type),),
        )
        second = Candidate(
            anime_id=2,
            anilist_id=200,
            titles=("Vinland Saga Season 2",),
            format="TV",
            episodes=24,
            status="FINISHED",
        )
        return [first, second]

    def test_an_overshooting_number_lands_on_the_sequel(self) -> None:
        parsed = parse("[G] Vinland Saga - 30 [1080p].mkv")
        offsets = offset_candidates(parsed, self.pool())
        assert [(item.anime_id, item.episode_number) for item in offsets] == [(2, 6)]
        assert offsets[0].absolute is True

    def test_a_number_inside_the_count_produces_nothing(self) -> None:
        assert offset_candidates(parse("[G] Vinland Saga - 12 [1080p].mkv"), self.pool()) == []

    def test_only_sequels_are_followed(self) -> None:
        parsed = parse("[G] Vinland Saga - 30 [1080p].mkv")
        assert offset_candidates(parsed, self.pool("SIDE_STORY")) == []

    def test_a_sequel_that_is_not_in_the_pool_is_not_invented(self) -> None:
        first = self.pool()[0]
        assert offset_candidates(parse("[G] Vinland Saga - 30 [1080p].mkv"), [first]) == []

    def test_the_offset_is_penalised_below_an_uninferred_hit(self) -> None:
        """An inference never scores as well as a number that simply fits."""
        pool = self.pool()
        offsets = offset_candidates(parse("[G] Vinland Saga - 30 [1080p].mkv"), pool)
        clean = score(parse("[G] Vinland Saga - 12 [1080p].mkv"), pool[0])
        assert max(item.score for item in offsets) < clean.score

    def test_an_absolute_number_beats_the_impossible_literal_reading(self) -> None:
        """Episode 30 of a 24-episode show is episode 6 of its sequel.

        Both readings are offered — the literal one is what a stale episode
        count looks like — but the offset wins, because a number that fits is
        worth more than a number that does not even after the inference is
        charged for.
        """
        parsed = parse("[G] Vinland Saga - 30 [1080p].mkv")
        pool = self.pool()
        scored = [score(parsed, candidate) for candidate in pool]
        scored.extend(offset_candidates(parsed, pool))

        result = rank(scored, minimum=MINIMUM)
        best = result.best

        assert best is not None
        assert (best.anime_id, best.episode_number, best.absolute) == (2, 6, True)
        assert (1, 30) in {(item.anime_id, item.episode_number) for item in result.candidates}

    def test_a_batch_is_never_offset(self) -> None:
        assert offset_candidates(parse("[G] Vinland Saga - 01-30 [1080p].mkv"), self.pool()) == []
