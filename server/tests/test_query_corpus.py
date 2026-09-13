"""The Nyaa query corpus (FR-A4, architecture.md §6, §10).

``tests/fixtures/query_corpus.txt`` is a block per real case: a catalogue entry
production had trouble with, the episode it was looking for, the query forms
Arc must build, and real nyaa.si release names it must accept and reject. Its
header documents the format; this module is only the reader and the
assertions.

**It runs offline.** :func:`~arc.services.acquisition.nyaa.queries`,
:func:`~arc.services.acquisition.nyaa.acceptable` and
:func:`~arc.services.acquisition.nyaa.rank` are pure functions of an ``anime``
row and a release name, which is what makes a corpus of this shape possible:
no network, no database, no fixtures to capture. The one thing it cannot pin is
what Nyaa *answers* — the ``search_*.xml`` fixtures in ``tests/fixtures/nyaa``
do that — so a query form here is a claim about what release groups write, and
the file is where those claims are written down and dated.

:func:`test_case` asserts each block individually, so a regression names the
show it broke. :func:`test_corpus_summary` prints the aggregate the way
``test_parser_corpus`` does and holds a floor on the number of cases, so that
the cases added for a bug cannot be quietly deleted along with the fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from arc.models import Anime
from arc.services.acquisition.nyaa import (
    Candidate,
    NyaaItem,
    acceptable,
    anime_season,
    anime_titles,
    is_single,
    queries,
    rank,
)
from arc.services.acquisition.rules import Rules

CORPUS = Path(__file__).parent / "fixtures" / "query_corpus.txt"

#: Where the corpus stands. A floor, not a snapshot: new cases are welcome and
#: the ones already here are the evidence behind a query form.
MIN_CASES = 17

#: Fields a block may carry once, and the ones it must.
SINGLE_FIELDS = frozenset(
    {
        "case",
        "romaji",
        "english",
        "native",
        "format",
        "episodes",
        "year",
        "prequel",
        "number",
        "prefer",
    }
)
REQUIRED_FIELDS = ("case", "romaji", "format", "number")

#: And the ones it may repeat.
LIST_FIELDS = frozenset({"synonym", "query", "absent", "accept", "reject"})

#: Seeders an ``accept`` line gets when it does not say. Any constant will do:
#: the ranking assertion is about the releases that *do* say.
DEFAULT_SEEDERS = 100

_SEEDERS_RE = re.compile(r"\s*@seeders=(\d+)\s*$")


@dataclass(frozen=True, slots=True)
class Release:
    """One release name from the corpus, with the seeders it declared."""

    title: str
    seeders: int = DEFAULT_SEEDERS

    @classmethod
    def parse(cls, line: str) -> Release:
        matched = _SEEDERS_RE.search(line)
        if matched is None:
            return cls(line.strip())
        return cls(line[: matched.start()].strip(), int(matched.group(1)))

    @property
    def item(self) -> NyaaItem:
        """A feed item that is nothing but this name and its seeders."""
        return NyaaItem(
            title=self.title,
            link="",
            info_hash=f"{abs(hash(self.title)):040x}"[:40],
            seeders=self.seeders,
        )


@dataclass(frozen=True, slots=True)
class Case:
    """One block of the corpus."""

    line: int
    name: str
    anime: Anime
    number: int
    query_forms: tuple[str, ...] = ()
    absent_forms: tuple[str, ...] = ()
    accepted: tuple[Release, ...] = ()
    rejected: tuple[Release, ...] = ()
    prefer: str | None = None

    @property
    def id(self) -> str:
        return f"L{self.line}:{self.name}"


@dataclass
class _Block:
    line: int
    single: dict[str, str] = field(default_factory=dict)
    lists: dict[str, list[str]] = field(default_factory=lambda: {key: [] for key in LIST_FIELDS})


def _relations(prequel: str | None) -> list[dict[str, object]] | None:
    """``anime.relations``, carrying a ``PREQUEL`` when the block says so."""
    if (prequel or "no").strip().lower() not in {"yes", "true"}:
        return None
    return [{"anilist_id": 1, "relation_type": "PREQUEL", "format": "TV"}]


def _count(raw: str | None) -> int | None:
    if raw is None or raw.strip() in {"", "null", "none"}:
        return None
    return int(raw)


def _case(block: _Block) -> Case:
    for required in REQUIRED_FIELDS:
        assert required in block.single, f"{CORPUS.name}:{block.line} has no {required}"
    single = block.single
    anime = Anime(
        anilist_id=block.line,
        title_romaji=single["romaji"],
        title_english=single.get("english"),
        title_native=single.get("native"),
        synonyms=block.lists["synonym"] or None,
        format=single["format"],
        episodes=_count(single.get("episodes")),
        season_year=_count(single.get("year")),
        relations=_relations(single.get("prequel")),
    )
    return Case(
        line=block.line,
        name=single["case"],
        anime=anime,
        number=int(single["number"]),
        query_forms=tuple(block.lists["query"]),
        absent_forms=tuple(block.lists["absent"]),
        accepted=tuple(Release.parse(line) for line in block.lists["accept"]),
        rejected=tuple(Release.parse(line) for line in block.lists["reject"]),
        prefer=single.get("prefer"),
    )


def load_corpus() -> list[Case]:
    """Every block in the fixture, comments and blank lines dropped."""
    cases: list[Case] = []
    block: _Block | None = None
    for number, raw in enumerate(CORPUS.read_text(encoding="utf-8").splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        if not raw.strip():
            if block is not None:
                cases.append(_case(block))
                block = None
            continue
        key, colon, value = raw.partition(":")
        key = key.strip().lower()
        assert colon and (key in SINGLE_FIELDS or key in LIST_FIELDS), (
            f"{CORPUS.name}:{number} is not a known field: {raw!r}"
        )
        if block is None:
            block = _Block(line=number)
        if key in LIST_FIELDS:
            block.lists[key].append(value.strip())
        else:
            assert key not in block.single, f"{CORPUS.name}:{number} repeats {key}"
            block.single[key] = value.strip()
    if block is not None:
        cases.append(_case(block))
    return cases


CASES = load_corpus()


def test_corpus_is_big_enough() -> None:
    """No fewer cases than the day the file was written."""
    assert len(CASES) >= MIN_CASES


def test_every_case_has_a_unique_name() -> None:
    names = [case.name for case in CASES]
    assert len(set(names)) == len(names)


def test_every_case_asserts_something() -> None:
    """A block with no expectations would pass for ever and mean nothing."""
    for case in CASES:
        assert case.query_forms or case.absent_forms or case.accepted or case.rejected, case.id


def _accept(case: Case, release: Release) -> Candidate | None:
    return acceptable(
        release.item,
        titles=anime_titles(case.anime),
        number=case.number,
        season=anime_season(case.anime),
        single=is_single(case.anime),
        year=case.anime.season_year,
    )


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_case(case: Case) -> None:
    built = queries(case.anime, case.number)

    for form in case.query_forms:
        assert form in built, f"{case.id}: {form!r} is not among {built}"
    for form in case.absent_forms:
        assert form not in built, f"{case.id}: {form!r} must not be asked for"

    candidates: list[Candidate] = []
    for release in case.accepted:
        candidate = _accept(case, release)
        assert candidate is not None, f"{case.id}: {release.title!r} should be acceptable"
        candidates.append(candidate)
    for release in case.rejected:
        assert _accept(case, release) is None, (
            f"{case.id}: {release.title!r} should have been rejected"
        )

    if case.prefer is not None:
        ranked = rank(candidates, Rules())
        assert ranked[0].item.title == case.prefer, f"{case.id}: ranked {ranked[0].item.title!r}"


def test_corpus_summary(capsys: pytest.CaptureFixture[str]) -> None:
    """The aggregate, printed the way the parser corpus prints its precision."""
    forms = sum(len(case.query_forms) for case in CASES)
    absent = sum(len(case.absent_forms) for case in CASES)
    accepted = sum(len(case.accepted) for case in CASES)
    rejected = sum(len(case.rejected) for case in CASES)

    with capsys.disabled():
        print(
            f"\nquery corpus: {len(CASES)} cases | {forms} forms required,"
            f" {absent} forbidden | {accepted} releases accepted, {rejected} rejected"
        )

    assert forms >= len(CASES), "every case should require at least one query form"
    assert accepted + rejected >= len(CASES)
