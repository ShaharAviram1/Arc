"""The release-name corpus (spec §7, architecture.md §10, FR-L2).

Two things happen here and they are deliberately separate.

:func:`test_corpus_case` asserts **every** line of
``tests/fixtures/release_names.txt`` individually, so a regression names the
filename it broke rather than a percentage.

:func:`test_corpus_precision` asserts the *aggregate* the M5 definition of done
is written against: ≥ 97 % on episode+kind, ≥ 93 % on title_key. It is not
redundant with the per-case test — it is the number to quote, and it is what
would still hold the line if somebody ever marked a case ``xfail``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from arc.services.library.parser import ParsedName, parse

CORPUS = Path(__file__).parent / "fixtures" / "release_names.txt"

#: The floors from the milestone. Not "the numbers we currently get": if the
#: parser improves these stay put, and if it regresses past them the build
#: fails whatever the per-case test says.
MIN_EPISODE_KIND_PRECISION = 0.97
MIN_TITLE_PRECISION = 0.93

#: The fields the corpus pins. Everything else a ``ParsedName`` carries —
#: resolution, source, codec, year, extension — is exercised by
#: ``test_parser.py``; pinning them here would make every line four times as
#: wide for cases that are all the same three answers.
FIELDS = ("title_key", "episode", "season", "kind", "group", "version")


class Case(NamedTuple):
    """One corpus line."""

    line: int
    name: str
    expected: dict[str, Any]

    @property
    def id(self) -> str:
        return f"L{self.line}:{self.name}"


def load_corpus() -> list[Case]:
    """Every case in the fixture, comments and blank lines dropped."""
    cases: list[Case] = []
    for number, raw in enumerate(CORPUS.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        name, tab, payload = raw.partition("\t")
        assert tab, f"{CORPUS.name}:{number} is not name<TAB>json"
        cases.append(Case(number, name, json.loads(payload)))
    return cases


CASES = load_corpus()


#: architecture.md §10 asks for at least 200 real names; the corpus stands
#: well above that, and the floor is set at where it stands so that the cases
#: added for a bug cannot be quietly deleted along with the fix.
MIN_CASES = 235


def test_corpus_is_big_enough() -> None:
    """At least the 200 architecture.md §10 asks for, and no fewer than today."""
    assert len(CASES) >= MIN_CASES


def test_corpus_pins_every_field() -> None:
    """Every line must state every field, so a missing key cannot pass."""
    for case in CASES:
        assert set(case.expected) == set(FIELDS), case.id


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_corpus_case(case: Case) -> None:
    parsed = parse(case.name)
    actual = {field: getattr(parsed, field) for field in FIELDS}
    assert actual == case.expected


def _precision(cases: list[Case], check: Any) -> tuple[int, int]:
    hits = sum(1 for case in cases if check(parse(case.name), case.expected))
    return hits, len(cases)


def test_corpus_precision(capsys: pytest.CaptureFixture[str]) -> None:
    """The aggregate the milestone is measured against, printed and asserted."""

    def episode_and_kind(parsed: ParsedName, expected: dict[str, Any]) -> bool:
        return parsed.episode == expected["episode"] and parsed.kind == expected["kind"]

    def title(parsed: ParsedName, expected: dict[str, Any]) -> bool:
        return parsed.title_key == expected["title_key"]

    ek_hits, total = _precision(CASES, episode_and_kind)
    title_hits, _ = _precision(CASES, title)
    ek = ek_hits / total
    tk = title_hits / total

    with capsys.disabled():
        print(
            f"\ncorpus: {total} names | episode+kind {ek_hits}/{total} = {ek:.3%}"
            f" | title_key {title_hits}/{total} = {tk:.3%}"
        )

    assert ek >= MIN_EPISODE_KIND_PRECISION, f"episode+kind precision {ek:.3%}"
    assert tk >= MIN_TITLE_PRECISION, f"title_key precision {tk:.3%}"


def test_parse_is_deterministic() -> None:
    """Same string in, same dataclass out — the promise the corpus rests on."""
    for case in CASES[:25]:
        assert parse(case.name) == parse(case.name)
