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

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from arc.models import Anime
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition.nyaa import (
    Candidate,
    NyaaItem,
    absolute_offset,
    acceptable,
    anime_season,
    anime_titles,
    is_single,
    queries,
    rank,
)
from arc.services.acquisition.rules import Rules
from arc.services.library.parser import parse

CORPUS = Path(__file__).parent / "fixtures" / "query_corpus.txt"

#: Where the corpus stands. A floor, not a snapshot: new cases are welcome and
#: the ones already here are the evidence behind a query form.
MIN_CASES = 20

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
        "prequel_episodes",
        "number",
        "prefer",
    }
)

#: The AniList id the block's synthetic prequel is filed under.
PREQUEL_ID = 1
REQUIRED_FIELDS = ("case", "romaji", "format", "number")

#: And the ones it may repeat. ``batch_accept`` / ``batch_reject`` are the
#: batch half of ``accept`` / ``reject`` (FR-A11, 2026-09-18): the same release
#: names put through ``acceptable(..., batches=True)``, which is the only way
#: past the batch rejection and is asked for by nothing but a finished show
#: with no single at all. Two fields rather than a flag on the existing ones
#: because a line's *default* reading has to stay the one every block written
#: before today meant — ``reject`` means "never, whatever the caller asked".
LIST_FIELDS = frozenset(
    {"synonym", "query", "absent", "accept", "reject", "batch_accept", "batch_reject"}
)

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
    #: Batches ``acceptable(..., batches=True)`` must accept as a candidate,
    #: and ones it must still refuse (FR-A11).
    batch_accepted: tuple[Release, ...] = ()
    batch_rejected: tuple[Release, ...] = ()
    prefer: str | None = None
    #: What :func:`absolute_offset` answered for this entry, computed from the
    #: block's own ``prequel_episodes`` through the real walk rather than
    #: declared: the corpus asserts the *rule*, not a number typed beside it.
    offset: int | None = None

    @property
    def id(self) -> str:
        return f"L{self.line}:{self.name}"


@dataclass
class _Block:
    line: int
    single: dict[str, str] = field(default_factory=dict)
    lists: dict[str, list[str]] = field(default_factory=lambda: {key: [] for key in LIST_FIELDS})


def _relations(prequel: str | None, prequel_episodes: int | None) -> list[dict[str, object]] | None:
    """``anime.relations``, carrying a ``PREQUEL`` when the block says so.

    ``prequel_episodes`` implies one: a block that declares how long the
    previous season ran is a block whose entry has a previous season.
    """
    if prequel_episodes is None and (prequel or "no").strip().lower() not in {"yes", "true"}:
        return None
    return [{"anilist_id": PREQUEL_ID, "relation_type": "PREQUEL", "format": "TV"}]


def _offset(anime: Anime, prequel_episodes: int | None) -> int | None:
    """:func:`absolute_offset` over a one-hop chain the block described.

    The prequel row is synthesised here rather than cached anywhere, which is
    the whole reason the corpus can stay offline: ``absolute_offset`` takes its
    resolver, so "Arc has this row and it ran 24 episodes" is two lines.
    """
    if prequel_episodes is None:
        return None
    prequel = Anime(
        anilist_id=PREQUEL_ID,
        title_romaji="the previous season",
        format="TV",
        status="FINISHED",
        episodes=prequel_episodes,
    )

    async def resolve(anilist_id: int | None, _mal_id: int | None) -> Anime | None:
        return prequel if anilist_id == PREQUEL_ID else None

    return asyncio.run(absolute_offset(anime, resolve))


def _count(raw: str | None) -> int | None:
    if raw is None or raw.strip() in {"", "null", "none"}:
        return None
    return int(raw)


def _case(block: _Block) -> Case:
    for required in REQUIRED_FIELDS:
        assert required in block.single, f"{CORPUS.name}:{block.line} has no {required}"
    single = block.single
    prequel_episodes = _count(single.get("prequel_episodes"))
    anime = Anime(
        anilist_id=block.line,
        title_romaji=single["romaji"],
        title_english=single.get("english"),
        title_native=single.get("native"),
        synonyms=block.lists["synonym"] or None,
        format=single["format"],
        episodes=_count(single.get("episodes")),
        season_year=_count(single.get("year")),
        relations=_relations(single.get("prequel"), prequel_episodes),
    )
    return Case(
        offset=_offset(anime, prequel_episodes),
        line=block.line,
        name=single["case"],
        anime=anime,
        number=int(single["number"]),
        query_forms=tuple(block.lists["query"]),
        absent_forms=tuple(block.lists["absent"]),
        accepted=tuple(Release.parse(line) for line in block.lists["accept"]),
        rejected=tuple(Release.parse(line) for line in block.lists["reject"]),
        batch_accepted=tuple(Release.parse(line) for line in block.lists["batch_accept"]),
        batch_rejected=tuple(Release.parse(line) for line in block.lists["batch_reject"]),
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
        assert (
            case.query_forms
            or case.absent_forms
            or case.accepted
            or case.rejected
            or case.batch_accepted
            or case.batch_rejected
        ), case.id


def _accept(case: Case, release: Release, *, batches: bool = False) -> Candidate | None:
    return acceptable(
        release.item,
        titles=anime_titles(case.anime),
        number=case.number,
        season=anime_season(case.anime),
        single=is_single(case.anime),
        year=case.anime.season_year,
        offset=case.offset,
        batches=batches,
    )


def test_every_batch_the_corpus_rejects_is_still_rejected_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard on the batch work of 2026-09-18 (FR-A11).

    ``acceptable`` grew a ``batches`` argument, and the whole of its safety is
    that it defaults to ``False``: no caller written before FR-A11 may start
    seeing a batch. So every ``reject`` line in the file that really is a batch
    — the *Dagashi Kashi* season pack, both *One-Room TA* packs, the two
    *Frieren* ranges, the film batch, the absolute *Jujutsu Kaisen* pack — is
    asserted here to be rejected by the default call, and to be rejected *as a
    batch* rather than by some other rule that happens to catch it.

    :func:`test_case` already re-asserts each of them per block. This says the
    same thing in one place and about the *reason*, so that a change which
    smuggled batches past the default would fail with the sentence naming it.
    """
    batch_rejections = [
        (case, release)
        for case in CASES
        for release in case.rejected
        if parse(release.title).is_batch
    ]

    assert len(batch_rejections) >= 8, "the corpus has lost its batch rejections"
    for case, release in batch_rejections:
        caplog.clear()
        with caplog.at_level("DEBUG", logger=nyaa_module.__name__):
            assert _accept(case, release) is None, f"{case.id}: {release.title!r} is a batch"
        reasons = [getattr(record, "reason", "") for record in caplog.records]
        assert any(reason.startswith("batch release") for reason in reasons), (
            f"{case.id}: {release.title!r} was rejected by {reasons}, not as a batch"
        )


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_case(case: Case) -> None:
    built = queries(case.anime, case.number, offset=case.offset)

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
    for release in case.batch_accepted:
        batch = _accept(case, release, batches=True)
        assert batch is not None, f"{case.id}: {release.title!r} should be a batch candidate"
        assert batch.is_batch, f"{case.id}: {release.title!r} is not a batch at all"
        assert _accept(case, release) is None, (
            f"{case.id}: {release.title!r} must still be rejected by the default"
        )
    for release in case.batch_rejected:
        assert _accept(case, release, batches=True) is None, (
            f"{case.id}: {release.title!r} should have been rejected even as a batch"
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
    batch_accepted = sum(len(case.batch_accepted) for case in CASES)
    batch_rejected = sum(len(case.batch_rejected) for case in CASES)

    with capsys.disabled():
        print(
            f"\nquery corpus: {len(CASES)} cases | {forms} forms required,"
            f" {absent} forbidden | {accepted} releases accepted, {rejected} rejected"
            f" | batches: {batch_accepted} accepted, {batch_rejected} rejected"
        )

    assert forms >= len(CASES), "every case should require at least one query form"
    assert accepted + rejected >= len(CASES)
