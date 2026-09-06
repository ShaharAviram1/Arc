"""From a parsed filename to an anime and an episode number (FR-L3, FR-L4).

The module is in two halves and the split is deliberate.

**The scoring is pure.** :func:`score` takes a :class:`~.parser.ParsedName` and
a :class:`Candidate` — a plain dataclass of the catalogue fields, not an ORM
row — and returns a :class:`Scored` with the weighted total and the reasons
behind it. No session, no clock, no network, so the whole ranking is testable
from a table of literals and the corpus in ``tests/fixtures/match_cases.json``
means something.

**The gathering is async and thin.** :func:`match` collects candidates from
three places and hands them to the scorer:

1. the **expected episode** — for a file Arc downloaded itself, Arc already
   knows which episode it asked for (M6). That belief is worth
   :data:`PRIOR_WEIGHT` of the distance from that candidate's own score to
   certainty — enough to carry a release whose title is written badly, and by
   construction not enough to override one that clearly says something else;
2. the **local cache** — a fuzzy sweep over ``anime`` titles and synonyms with
   rapidfuzz. Free, and it is where the answer is for every show a user has
   already added;
3. the **catalogue** — ``CatalogService.search`` for the top few hits, which
   is what finds a show nobody has added yet, and which comes with the MAL
   fallback for free (FR-C6).

Scoring weights (they sum to 1.0 before the prior):

===================  ======  ===================================================
component            weight  what it asks
===================  ======  ===================================================
title                 0.55   best similarity over romaji/english/native/synonyms
episode plausibility  0.20   does this episode number exist on this show
season agreement      0.15   does the parsed season match the candidate's
format                0.05   movie ↔ MOVIE, special ↔ OVA/ONA/SPECIAL
year                  0.05   does the parsed year match the season year
===================  ======  ===================================================

**Confidence is not the top score.** A file that scores 0.90 against two shows
is not a 0.90 match to either of them, so when the runner-up is within
:data:`AMBIGUITY_MARGIN` the confidence is capped at
:data:`AMBIGUOUS_CEILING`, which is below the auto-link threshold by
construction. That is the mechanical form of "the server must say when it is
unsure instead of guessing" (FR-L4).

**Two components have a veto, because a weighted sum has none.** Every
component above votes, so four that agree can carry one that does not, and the
two cases where that is a wrong automatic link are guarded separately:

* the *title* has to clear ``MATCH_MIN_TITLE_FOR_AUTO`` (or be an exact
  ``title_key``) before :meth:`MatchResult.auto_links` says yes — otherwise a
  season, a format, a year and an episode number that all fit carry
  ``Kaijuu 9-gou`` onto *Kaijuu 8-gou*;
* the *prior* is only applied to a candidate whose own title has cleared
  :data:`PRIOR_MIN_TITLE`, so the bonus can rescue a badly written name and
  cannot install a different show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime
from arc.services.catalog import CatalogService, SourceUnavailable, upsert_summaries
from arc.services.library.parser import ParsedName, strip_season, title_key

log = logging.getLogger(__name__)

# --- Weights ----------------------------------------------------------------

TITLE_WEIGHT = 0.55
EPISODE_WEIGHT = 0.20
SEASON_WEIGHT = 0.15
FORMAT_WEIGHT = 0.05
YEAR_WEIGHT = 0.05
BASE_WEIGHT = TITLE_WEIGHT + EPISODE_WEIGHT + SEASON_WEIGHT + FORMAT_WEIGHT + YEAR_WEIGHT

#: How much Arc's own record of what it downloaded is worth: a prior candidate
#: is moved this fraction of the way from its own score toward certainty.
#:
#: A *bonus*, not a re-weighting of the whole sum, and the difference matters.
#: Sharing a denominator with the other components would divide every rival
#: down by the same amount that lifts the prior — which makes the prior
#: unbeatable, so a mislabelled download would confidently name the episode
#: Arc happened to be fetching however plainly the filename said otherwise.
#: As a pull toward 1.0 it is bounded: a candidate must reach about 0.94 on
#: its own merits before the bonus carries it past a rival already at 0.975.
#: (FR-L3, and CLAUDE.md's "never auto-link a guess".)
PRIOR_WEIGHT = 0.60

#: The title similarity a candidate must reach on its own before
#: :data:`PRIOR_WEIGHT` is applied to it at all.
#:
#: The bonus exists to carry a release whose *title* is written badly — a
#: scene stem, an English name against a romaji row — not to carry one that
#: names a different show. Ungated, a candidate at 0.48 on the title still
#: crosses the auto-link threshold once the bonus is added, which is an
#: automatic link to a show the filename disagrees with: exactly the guess
#: CLAUDE.md forbids. Below this the prior still appears as a candidate, with
#: a reason saying why it was not believed, and the file goes to review.
PRIOR_MIN_TITLE = 0.60

#: Subtracted from an offset candidate produced by the absolute-numbering
#: rule. It is a real answer — episode 40 of a 25-episode show is episode 15
#: of the sequel — but it is an inference, and it must not beat a candidate
#: that needed no inference at all.
ABSOLUTE_PENALTY = 0.10

#: What a cour disagreement multiplies the season score by. Wide enough that
#: "Season 3" beats "Season 3 Part 2" for a file that names no part by more
#: than :data:`AMBIGUITY_MARGIN`; narrow enough that it is a preference and
#: never a veto. See :func:`part_factor`.
PART_DISAGREEMENT = 0.6

#: What "the file says season 3, the catalogue entry names no season" is
#: worth. Almost nothing: an entry that publishes no season marker *is* season
#: one. See :func:`season_agreement` for why it is not quite zero.
UNNUMBERED_AGAINST_SEASON = 0.05

#: The most a title that is not *literally* one of the show's names may score.
#: An exact key is 1.0, and the gap between the two — times
#: :data:`TITLE_WEIGHT` — is deliberately wider than
#: :data:`AMBIGUITY_MARGIN`, so "the same name" beats "very nearly the same
#: name" by enough to be decided instead of sent to a person.
NON_EXACT_CEILING = 0.88

#: How close the runner-up has to be for the result to count as ambiguous.
AMBIGUITY_MARGIN = 0.05
#: The confidence an ambiguous result is capped at. Below
#: ``MATCH_AUTO_THRESHOLD`` by construction: ambiguity always means review.
AMBIGUOUS_CEILING = 0.80

#: How many candidates the result carries, and how many the review UI shows.
MAX_CANDIDATES = 5
#: How many catalogue search hits are considered.
SEARCH_HITS = 5
#: How many cache rows the fuzzy sweep keeps before scoring.
CACHE_HITS = 8
#: Below this rapidfuzz ratio a cache row is not worth scoring at all.
CACHE_CUTOFF = 60.0

#: AniList statuses and formats the scorer names.
RELEASING = "RELEASING"
MOVIE_FORMATS = frozenset({"MOVIE"})
SPECIAL_FORMATS = frozenset({"OVA", "ONA", "SPECIAL", "TV_SHORT", "MUSIC"})
EPISODIC_FORMATS = frozenset({"TV", "TV_SHORT", "ONA", "OVA"})

#: Relation types that continue a story, for the absolute-numbering rule.
SEQUEL_RELATIONS = frozenset({"SEQUEL"})

#: The episode number a movie file is linked as. A film is one part of one
#: show, so it is episode 1 of it — the same row every other file gets, which
#: is what lets the player, the progress and the retention rules treat a movie
#: like anything else instead of growing a special case each (FR-L4).
MOVIE_EPISODE = 1
#: Episode counts a MOVIE candidate may carry and still be "one film": either
#: the catalogue says one part, or it says nothing at all.
SINGLE_MOVIE_COUNTS = frozenset({1})


@dataclass(frozen=True, slots=True)
class Candidate:
    """One title the matcher is weighing, flattened out of wherever it came from.

    Deliberately not an ``Anime`` row: the scorer must be callable from a test
    with no database, and a dataclass of exactly the scored fields is also the
    shortest possible statement of what the scorer is allowed to look at.
    """

    #: Arc's internal id. Always set by the time a candidate is scored — a
    #: search hit is upserted into ``anime`` on the way past, which is what
    #: mints it (FR-C6).
    anime_id: int
    #: The external ids, used only to resolve a relation edge (which carries
    #: external ids, never internal ones) back to another candidate.
    anilist_id: int | None = None
    mal_id: int | None = None
    #: romaji, english and native — the names the source itself publishes.
    titles: tuple[str, ...] = ()
    #: AniList's ``synonyms``: alternate spellings, other languages, fandom
    #: labels. Scored for *similarity* like any other title, but never read
    #: for a season: they are user-submitted, and one of them saying
    #: "Season 3" is not the catalogue saying so.
    synonyms: tuple[str, ...] = ()
    format: str | None = None
    episodes: int | None = None
    status: str | None = None
    season_year: int | None = None
    #: ``anime.relations`` as stored, used only by the absolute-numbering rule.
    relations: tuple[dict[str, Any], ...] = ()
    #: Where this candidate came from: ``"cache"``, ``"search"`` or
    #: ``"prior"``. Recorded in the reasons, never scored.
    origin: str = "cache"

    @property
    def all_titles(self) -> tuple[str, ...]:
        """Published titles and synonyms together, in that order."""
        return self.titles + self.synonyms

    @property
    def primary_keys(self) -> tuple[str, ...]:
        """Matching keys for the *published* titles only.

        These are the ones an exact match is allowed to be made against. A
        synonym is a fan-submitted alternate, and half the franchises in the
        catalogue list the base show's name as a synonym of every sequel — so
        letting a synonym count as an exact hit made *xxxHOLiC◆Kei* score
        exactly as well as *xxxHOLiC* on a file that says "xxxHOLiC".
        """
        return tuple(
            key
            for key in (title_key(strip_season(name).title) for name in self.titles if name)
            if key
        )

    @property
    def keys(self) -> tuple[str, ...]:
        """Every title as a matching key: season marker off, then flattened.

        The stripping is what makes the comparison fair. A filename has
        already had ``Season 2`` taken out of its title and put into
        :attr:`ParsedName.season`; leaving it in on the catalogue side would
        charge the candidate for it twice — once as a title difference, and
        again as a season the file "does not mention". ``Vinland Saga Season
        2`` and ``Vinland Saga`` reduce to one key, and which of them the file
        belongs to is decided by :func:`season_agreement` alone, which is the
        component that knows how.
        """
        keys: list[str] = []
        for name in self.all_titles:
            if not name:
                continue
            key = title_key(strip_season(name).title)
            if key and key not in keys:
                keys.append(key)
        return tuple(keys)


@dataclass(frozen=True, slots=True)
class Scored:
    """One candidate, scored, with the reasoning the review UI shows."""

    anime_id: int
    #: The episode number of *this show* the file holds. ``None`` for a movie,
    #: a batch or anything the parser found no number in.
    episode_number: int | None
    score: float
    reasons: tuple[str, ...] = ()
    #: True when the number was reached by subtracting a prequel's episode
    #: count (absolute numbering).
    absolute: bool = False
    #: The title component on its own, 0..1. Carried separately from
    #: :attr:`score` because the auto-link rule reads it separately: a
    #: weighted sum can be pushed over the threshold by everything *except*
    #: the title, and that is the one component that must not be outvoted.
    title: float = 0.0
    #: True when :attr:`ParsedName.title_key` is literally one of the
    #: candidate's published titles, once both sides are stripped and
    #: flattened. An exact name is the one thing that needs no threshold.
    exact_title: bool = False
    #: True when this is the episode Arc was downloading *and* the title
    #: agreed enough for that to be believed (:data:`PRIOR_MIN_TITLE`).
    prior: bool = False

    def title_is_close(self, minimum: float) -> bool:
        """Whether the title alone is good enough to link on (FR-L4).

        Three ways to pass, and they are three kinds of evidence rather than
        three thresholds. An **exact** ``title_key`` is the filename naming
        the show. A similarity above ``minimum`` is the filename naming it in
        a spelling close enough to be the same claim. A believed **prior** is
        Arc's own record of the episode it asked for, which is better evidence
        than any filename and has already cleared its own, lower title bar —
        without this clause the prior could never decide anything, since the
        releases it exists to rescue are exactly the ones whose titles are
        written badly (FR-L3).
        """
        return self.exact_title or self.prior or self.title >= minimum

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe mapping for ``media_files.match_candidates``."""
        return {
            "anime_id": self.anime_id,
            "episode_number": self.episode_number,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
            "absolute": self.absolute,
        }


@dataclass(frozen=True, slots=True)
class MatchResult:
    """What the matcher concluded, and how sure it is."""

    candidates: tuple[Scored, ...] = ()
    confidence: float = 0.0

    @property
    def best(self) -> Scored | None:
        return self.candidates[0] if self.candidates else None

    def auto_links(self, threshold: float, *, min_title: float = 0.0) -> bool:
        """Whether this may be linked without asking anybody (FR-L4).

        Two independent bars, and the second is not redundant. ``threshold``
        is the weighted sum, which every component votes in; ``min_title`` is
        the title component alone, which nothing else may outvote. Without it
        a candidate whose season, format, year and episode number all agree
        reaches 0.85 on a title similarity around 0.77 — *Kaijuu 9-gou* linked
        to *Kaijuu 8-gou*. See :meth:`Scored.title_is_close` for the two kinds
        of evidence that satisfy the second bar whatever ``min_title`` says.
        """
        best = self.best
        return (
            best is not None
            and best.episode_number is not None
            and self.confidence >= threshold
            and best.title_is_close(min_title)
        )

    def top(self, limit: int = MAX_CANDIDATES) -> list[dict[str, Any]]:
        return [scored.as_dict() for scored in self.candidates[:limit]]


# --- Pure scoring -----------------------------------------------------------


def title_similarity(
    key: str, candidate_keys: tuple[str, ...], *, exact_keys: tuple[str, ...] | None = None
) -> float:
    """Best similarity of ``key`` against any of a candidate's titles, 0..1.

    Two ratios, averaged, and the average is the whole point.

    The *loose* half — the larger of ``token_set_ratio`` and
    ``partial_ratio`` — is what makes "Frieren Beyond Journeys End" and
    "Frieren: Beyond Journey's End" the same title however the words are
    ordered, and what keeps a release carrying only part of a long name
    ("Frieren" for "Sousou no Frieren") in the running at all.

    On its own it is useless: it scores **100** for every short title against
    every longer one that contains it, so "Sousou no Frieren" would tie with
    "Sousou no Frieren: ●● no Mahou" and "Overlord" with "Overlord IV" —
    which is precisely the mistake that must not be made, since the two are
    different shows with different episode counts.

    The *exact* half is a whole-string ratio, which separates them: a
    containment scores high on the first and middling on the second, and an
    identity scores 100 on both.

    On top of that, an **exact** key — the same string, once both sides have
    had their season markers stripped and their punctuation flattened —
    short-circuits to 1.0, and every inexact answer is capped at
    :data:`NON_EXACT_CEILING`. That step is the difference between a library
    that resolves itself and one that does not: ratios alone put
    ``Chihayafuru 3`` two points from ``Chihayafuru``, ``Steins;Gate 0`` two
    points from ``Steins;Gate`` and ``PSYCHO-PASS 2`` two points from
    ``PSYCHO-PASS``, which is inside :data:`AMBIGUITY_MARGIN`, so every one of
    those files would go to review forever. Anything already below the ceiling
    is untouched, so a release carrying only part of a long name still scores
    what it scored.
    """
    if not key or not candidate_keys:
        return 0.0
    if key in (candidate_keys if exact_keys is None else exact_keys):
        return 1.0
    best = 0.0
    for other in candidate_keys:
        loose = max(fuzz.token_set_ratio(key, other), fuzz.partial_ratio(key, other))
        exact = fuzz.ratio(key, other)
        best = max(best, (loose + exact) / 2.0)
    return min(best / 100.0, NON_EXACT_CEILING)


def season_in_title(name: str) -> int | None:
    """The season a *catalogue* title names, if it names one.

    ``Vinland Saga Season 2`` → 2, ``Mob Psycho 100 III`` → 3, ``Sousou no
    Frieren`` → ``None``.

    Literally the parser's own rule (:func:`~.parser.strip_season`) applied to
    the other side of the comparison. One definition, so a spelling the
    filename side learns to read is one the catalogue side reads too.
    """
    return strip_season(name).season


def candidate_part(candidate: Candidate) -> int | None:
    """The cour a candidate's published titles name, if any."""
    for name in candidate.titles:
        found = strip_season(name).part
        if found is not None:
            return found
    return None


def candidate_season(candidate: Candidate) -> int | None:
    """The season the candidate's *published* titles name, if any.

    Synonyms are excluded on purpose. AniList's ``synonyms`` are
    user-submitted alternates, and English-fandom entries there routinely add
    "Season 3" to a show the catalogue itself names by its arc — which made
    *Kaguya-sama wa Kokurasetai: Ultra Romantic* look like it disagreed with a
    file that named no season at all.
    """
    for name in candidate.titles:
        found = season_in_title(name)
        if found is not None:
            return found
    return None


def episode_plausibility(parsed: ParsedName, candidate: Candidate, episode: int | None) -> float:
    """Whether ``episode`` can exist on this show, 0..1.

    Three cases and a default. A number inside a known count is a fact; a
    number on a show that is still airing without a published count is
    plausible but unproven; a number past a known count is wrong, and how
    wrong is what separates "the release is one ahead of a stale cache" from
    "this is absolute numbering across four seasons".

    A **movie** is its own case. It is episode 1 of itself, which is certain
    when the candidate is a ``MOVIE`` the catalogue lists as one part and a
    real doubt about *which* part when it lists several. Against a candidate
    that is not a movie at all this component says nothing: it is
    :func:`format_agreement` that scores that disagreement, and charging it
    twice would only make the same point louder.
    """
    if parsed.kind == "movie":
        if (candidate.format or "").upper() not in MOVIE_FORMATS:
            return 1.0
        total = candidate.episodes
        return 1.0 if total is None or total in SINGLE_MOVIE_COUNTS else 0.5
    if parsed.kind == "batch" or episode is None:
        # A batch is going to review whatever happens; it should not be
        # penalised for having no single number to place.
        return 0.5
    if episode <= 0:
        return 0.0
    total = candidate.episodes
    if total is None:
        return 0.75 if candidate.status == RELEASING else 0.5
    if episode <= total:
        return 1.0
    over = episode - total
    if over <= 1:
        # One past the count is what a just-aired finale looks like against a
        # cache that has not been refreshed yet.
        return 0.6
    return max(0.0, 0.3 - 0.02 * over)


def season_agreement(parsed: ParsedName, candidate: Candidate) -> float:
    """Whether the parsed season and the candidate's own agree, 0..1.

    The asymmetry is the point. A file that names no season is *most* likely a
    first season, so an unnumbered candidate scores full and a numbered one
    scores poorly — that is what stops ``Frieren - 05`` landing on a sequel.
    A file that names season 3 against a candidate that names **none** is the
    mirror image, and nearly as strong: an entry that publishes no season
    marker is season one, and *Boku no Hero Academia 4* is in the catalogue
    under that name. It is not scored at zero only because a handful of shows
    are numbered by arc rather than by season — a release that says ``S3``
    where the catalogue says ``Katanakaji no Sato-hen`` should still reach the
    review queue with the right show in it rather than be thrown away.
    """
    theirs = candidate_season(candidate)
    if parsed.season is None:
        base = 1.0 if theirs is None else 0.2
    elif theirs is None:
        base = 1.0 if parsed.season == 1 else UNNUMBERED_AGAINST_SEASON
    else:
        base = 1.0 if parsed.season == theirs else 0.0
    if base == 0.0:
        return base
    return base * part_factor(parsed.part, candidate_part(candidate))


def part_factor(ours: int | None, theirs: int | None) -> float:
    """How much a cour disagreement costs, as a multiplier on the season score.

    *Shingeki no Kyojin Season 3* and *Shingeki no Kyojin Season 3 Part 2* are
    two catalogue entries with the same season number and the same stripped
    title, so nothing else in the score can separate them — a file that says
    only ``S3 - 05`` would sit forever within :data:`AMBIGUITY_MARGIN` of
    both. A discount rather than a veto, because a release that names no part
    genuinely might be either, and one that names one might be matched against
    a catalogue that splits its cours differently.
    """
    if ours == theirs:
        return 1.0
    return PART_DISAGREEMENT


def format_agreement(parsed: ParsedName, candidate: Candidate) -> float:
    """Whether what the file looks like and what the show is agree, 0..1."""
    fmt = (candidate.format or "").upper()
    if not fmt:
        return 0.5
    if parsed.kind == "movie":
        return 1.0 if fmt in MOVIE_FORMATS else 0.0
    if parsed.kind == "special":
        return 1.0 if fmt in SPECIAL_FORMATS else 0.3
    if parsed.kind in {"episode", "batch"}:
        return 1.0 if fmt in EPISODIC_FORMATS else 0.2
    return 0.5


def year_agreement(parsed: ParsedName, candidate: Candidate) -> float:
    """Whether a year in the filename matches the show's season year, 0..1."""
    if parsed.year is None or candidate.season_year is None:
        return 0.5
    gap = abs(parsed.year - candidate.season_year)
    if gap == 0:
        return 1.0
    # A release year is often the year a season *finished*, one after it began.
    return 0.6 if gap == 1 else 0.0


def score(
    parsed: ParsedName,
    candidate: Candidate,
    *,
    episode: int | None = None,
    prior: bool = False,
    penalty: float = 0.0,
) -> Scored:
    """Score one candidate. Pure: same inputs, same :class:`Scored`.

    ``prior`` says this candidate is the episode Arc was downloading when the
    file appeared, and moves it :data:`PRIOR_WEIGHT` of the way toward
    certainty — but only once its own title has cleared
    :data:`PRIOR_MIN_TITLE`. ``penalty`` is subtracted afterwards; it is what
    marks an inferred answer (:func:`offset_candidates`).
    """
    exact_title = bool(parsed.title_key) and parsed.title_key in candidate.primary_keys
    title = title_similarity(parsed.title_key, candidate.keys, exact_keys=candidate.primary_keys)
    season = season_agreement(parsed, candidate)
    fmt = format_agreement(parsed, candidate)
    year = year_agreement(parsed, candidate)

    episode_number = parsed.episode if episode is None else episode
    if episode is None and parsed.episode_fraction is not None:
        # ``12.5`` is a recap that sits *between* episodes 12 and 13, and
        # :attr:`ParsedName.episode` holds the 12 only to say where it sits.
        # Offering it as episode 12 would file a summary over the episode, so
        # there is no number here to link and the file goes to a person.
        episode_number = None
    if episode_number is None and parsed.kind == "movie" and fmt == 1.0:
        # A film is episode 1 of itself. Only against a candidate the
        # catalogue actually calls a movie: giving a TV entry episode 1 would
        # turn "this is the wrong show" into a linkable answer.
        episode_number = MOVIE_EPISODE
    plausible = episode_plausibility(parsed, candidate, episode_number)

    total = (
        TITLE_WEIGHT * title
        + EPISODE_WEIGHT * plausible
        + SEASON_WEIGHT * season
        + FORMAT_WEIGHT * fmt
        + YEAR_WEIGHT * year
    ) / BASE_WEIGHT
    believed = prior and title >= PRIOR_MIN_TITLE
    if believed:
        total += PRIOR_WEIGHT * (1.0 - total)

    value = max(0.0, min(1.0, total) - penalty)

    reasons = [
        f"title {title:.2f}",
        f"episode {plausible:.2f}",
        f"season {season:.2f}",
        f"format {fmt:.2f}",
        f"year {year:.2f}",
        f"from {candidate.origin}",
    ]
    if prior:
        reasons.append(
            "expected episode for this download"
            if believed
            else "expected episode for this download, but the title disagrees"
        )
    if penalty:
        reasons.append(f"absolute numbering (-{penalty:.2f})")
    return Scored(
        anime_id=candidate.anime_id,
        episode_number=episode_number,
        score=value,
        reasons=tuple(reasons),
        absolute=bool(penalty),
        title=title,
        exact_title=exact_title,
        prior=believed,
    )


def confidence_of(candidates: list[Scored]) -> float:
    """The best score, reduced when a *different show* is nearly as good.

    The runner-up only counts if it is another anime: two episode numbers on
    the same show are the absolute-numbering rule arguing with itself, and
    that ambiguity is about the number, not about the title, so it is resolved
    by ordering rather than by sending the file to review.
    """
    if not candidates:
        return 0.0
    best = candidates[0]
    for other in candidates[1:]:
        if other.anime_id == best.anime_id:
            continue
        if best.score - other.score < AMBIGUITY_MARGIN:
            return min(best.score, AMBIGUOUS_CEILING)
        break
    return best.score


def rank(scored: list[Scored], *, minimum: float) -> MatchResult:
    """Order, deduplicate and cut a list of scored candidates.

    Deduplication is on ``(anime_id, episode_number)`` rather than on
    ``anime_id``: the same show can legitimately appear twice with two episode
    numbers, and collapsing those would hide the absolute-numbering answer
    behind the literal one.
    """
    ordered = sorted(scored, key=lambda item: (-item.score, item.anime_id))
    seen: set[tuple[int, int | None]] = set()
    unique: list[Scored] = []
    for item in ordered:
        key = (item.anime_id, item.episode_number)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    kept = [item for item in unique if item.score >= minimum][:MAX_CANDIDATES]
    return MatchResult(candidates=tuple(kept), confidence=confidence_of(kept))


# --- Candidate generation (the thin async half) -----------------------------


def _candidate_from_row(row: Anime, *, origin: str) -> Candidate:
    titles = [row.title_romaji, row.title_english, row.title_native]
    relations = tuple(item for item in (row.relations or []) if isinstance(item, dict))
    return Candidate(
        anime_id=row.id,
        anilist_id=row.anilist_id,
        mal_id=row.mal_id,
        titles=tuple(name for name in titles if name),
        synonyms=tuple(str(name) for name in (row.synonyms or []) if name),
        format=row.format,
        episodes=row.episodes,
        status=row.status,
        season_year=row.season_year,
        relations=relations,
        origin=origin,
    )


@dataclass(slots=True)
class _Pool:
    """Rows gathered for one match, deduplicated on the internal id."""

    rows: dict[int, Anime] = field(default_factory=dict)
    origins: dict[int, str] = field(default_factory=dict)

    def add(self, row: Anime, origin: str) -> None:
        # First origin wins: "prior" and "cache" both say the row was already
        # here, and that is the more informative label.
        if row.id not in self.rows:
            self.rows[row.id] = row
            self.origins[row.id] = origin

    def candidates(self) -> list[Candidate]:
        return [_candidate_from_row(row, origin=self.origins[row.id]) for row in self.rows.values()]


async def cache_candidates(
    session: AsyncSession, key: str, *, limit: int = CACHE_HITS
) -> list[Anime]:
    """Fuzzy-search the local ``anime`` cache for ``key``.

    A full sweep in Python rather than a SQL ``ILIKE``: the cache is a
    self-hosted server's worth of titles, the comparison that matters is
    rapidfuzz's and not Postgres's, and a prefix query would miss every
    release that writes the English title when the row carries the romaji.
    """
    if not key:
        return []
    rows = list(
        (
            await session.scalars(
                select(Anime).where(
                    (Anime.title_romaji.is_not(None)) | (Anime.title_english.is_not(None))
                )
            )
        ).all()
    )
    scored: list[tuple[float, int, Anime]] = []
    for row in rows:
        candidate = _candidate_from_row(row, origin="cache")
        best = 0.0
        for other in candidate.keys:
            best = max(best, fuzz.token_set_ratio(key, other), fuzz.partial_ratio(key, other))
        if best >= CACHE_CUTOFF:
            scored.append((best, row.id, row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[:limit]]


async def search_candidates(
    session: AsyncSession, catalog: CatalogService, term: str, *, limit: int = SEARCH_HITS
) -> list[Anime]:
    """Ask the catalogue, and cache what comes back.

    The upsert is not a side effect to be tidied away: it is what gives each
    hit the internal id a candidate is addressed by, and it is the same
    summary-only write the search endpoint does (``arc/api/anime.py``).
    A source that is down is not an error here — the cache sweep has usually
    already found the answer — so it is logged and skipped.
    """
    if not term:
        return []
    try:
        page = await catalog.search(term)
    except SourceUnavailable as exc:
        log.warning("catalogue search unavailable during match", extra={"error": str(exc)})
        return []
    if not page.results:
        return []
    return await upsert_summaries(session, page.results[:limit])


def offset_candidates(parsed: ParsedName, pool: list[Candidate]) -> list[Scored]:
    """Absolute-numbering answers, one per (candidate, sequel) pair.

    ``One Piece - 1122`` is unambiguous, but ``Vinland Saga - 30`` is episode
    6 of the second season, written by a group that never restarted the count.
    When the number overshoots a candidate's episode count and that candidate
    has a ``SEQUEL`` relation to another candidate *already in the pool*, the
    sequel is offered with the remainder as its episode number, minus
    :data:`ABSOLUTE_PENALTY` — it is a real answer, but an inferred one, and
    must never outrank a candidate that needed no inference.

    Only sequels in the pool are followed: a relation edge carries external
    ids, and a sequel Arc has never cached has no internal id to link an
    episode to. In practice the pool has it, because a catalogue search for
    the base title returns the sequels alongside it.

    One hop, not a chain. Two hops is a franchise, and a franchise-wide guess
    is exactly what the review queue is for.

    Returns nothing when nothing overshoots, which is the common case.
    """
    if parsed.episode is None or parsed.kind not in {"episode", "unknown"}:
        return []
    by_anilist = {c.anilist_id: c for c in pool if c.anilist_id is not None}
    by_mal = {c.mal_id: c for c in pool if c.mal_id is not None}

    found: list[Scored] = []
    for candidate in pool:
        total = candidate.episodes
        if total is None or total <= 0 or parsed.episode <= total:
            continue
        remainder = parsed.episode - total
        for relation in candidate.relations:
            if str(relation.get("relation_type") or "").upper() not in SEQUEL_RELATIONS:
                continue
            sequel = None
            if relation.get("anilist_id") is not None:
                sequel = by_anilist.get(int(relation["anilist_id"]))
            if sequel is None and relation.get("mal_id") is not None:
                sequel = by_mal.get(int(relation["mal_id"]))
            if sequel is None or sequel.anime_id == candidate.anime_id:
                continue
            # Season agreement is neutralised for the offset. A release that
            # numbers absolutely is precisely one that does not say which
            # season it is in, so scoring its silence as "this is season 1,
            # and you are season 2" would punish the candidate for the very
            # property that produced it — and the prequel, which the number
            # does not fit at all, would win every time.
            as_if = replace(parsed, season=candidate_season(sequel))
            found.append(score(as_if, sequel, episode=remainder, penalty=ABSOLUTE_PENALTY))
    return found


async def match(
    session: AsyncSession,
    catalog: CatalogService,
    parsed: ParsedName,
    *,
    expected: tuple[int, int] | None = None,
    minimum: float,
) -> MatchResult:
    """Gather candidates for ``parsed`` and rank them.

    ``expected`` is ``(anime_id, episode_number)`` — what Arc was downloading
    when this file appeared (M6). It lifts that one candidate toward certainty
    by :data:`PRIOR_WEIGHT` of the remaining distance and leaves every other
    alone, so a strong disagreement in the title still overrules it (FR-L3).

    ``minimum`` is ``MATCH_MIN_CANDIDATE``: candidates below it are not worth
    showing a human, and a result with none of them is the "no good
    candidates" review item.
    """
    pool = _Pool()

    expected_id = expected[0] if expected is not None else None
    if expected_id is not None:
        row = await session.get(Anime, expected_id)
        if row is not None:
            pool.add(row, "prior")
        else:
            log.warning(
                "expected anime for this file is not cached",
                extra={"anime_id": expected_id},
            )

    for row in await cache_candidates(session, parsed.title_key):
        pool.add(row, "cache")
    for row in await search_candidates(session, catalog, parsed.title):
        pool.add(row, "search")

    candidates = pool.candidates()
    has_prior = expected_id is not None and expected_id in pool.rows

    scored: list[Scored] = []
    for candidate in candidates:
        is_prior = has_prior and candidate.anime_id == expected_id
        episode = expected[1] if (is_prior and expected is not None) else None
        scored.append(score(parsed, candidate, episode=episode, prior=is_prior))

    scored.extend(offset_candidates(parsed, candidates))
    return rank(scored, minimum=minimum)


__all__ = [
    "ABSOLUTE_PENALTY",
    "AMBIGUITY_MARGIN",
    "AMBIGUOUS_CEILING",
    "CACHE_CUTOFF",
    "EPISODE_WEIGHT",
    "FORMAT_WEIGHT",
    "MAX_CANDIDATES",
    "MOVIE_EPISODE",
    "NON_EXACT_CEILING",
    "PART_DISAGREEMENT",
    "PRIOR_MIN_TITLE",
    "PRIOR_WEIGHT",
    "SEASON_WEIGHT",
    "TITLE_WEIGHT",
    "UNNUMBERED_AGAINST_SEASON",
    "YEAR_WEIGHT",
    "Candidate",
    "MatchResult",
    "Scored",
    "cache_candidates",
    "candidate_part",
    "candidate_season",
    "confidence_of",
    "episode_plausibility",
    "format_agreement",
    "match",
    "offset_candidates",
    "part_factor",
    "rank",
    "score",
    "search_candidates",
    "season_agreement",
    "season_in_title",
    "title_similarity",
    "year_agreement",
]
