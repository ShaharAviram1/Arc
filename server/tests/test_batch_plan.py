"""The pure half of the batch pick: which file is which episode (FR-A11).

:func:`~arc.services.acquisition.batch.plan_files` is where the byte guarantee
is decided, and it is a pure function over a list the client handed over — so
the table of file names, kinds and numbers in here *is* the rule. Nothing in
this module touches a database, a torrent client or Nyaa.

The case throughout is the production one: *Kimetsu no Yaiba* (2019,
``FINISHED``) episode 10, whose search came back with 91 results under thirteen
forms and no season-one single among them, and whose only seeded releases are
complete-season packs.
"""

from __future__ import annotations

import pytest

from arc.models import Anime
from arc.services.acquisition.batch import (
    FilePlan,
    plan_files,
    plan_offset,
    size_label,
    targets,
    verify_selection,
)
from arc.services.acquisition.nyaa import Candidate, NyaaItem, Ranked, acceptable
from arc.services.acquisition.qbit import FILE_OFF, FILE_ON, FileInfo

KIMETSU = Anime(
    anilist_id=101922,
    title_romaji="Kimetsu no Yaiba",
    title_english="Demon Slayer: Kimetsu no Yaiba",
    status="FINISHED",
    format="TV",
    episodes=26,
    season_year=2019,
)
KIMETSU.id = 5

#: A second season whose group never restarted the count: SubsPlease numbered
#: *Jujutsu Kaisen* season two 25–47 (FR-A4, 2026-09-17), which is the one case
#: a file's own number is not the entry's episode number.
JUJUTSU_S2 = Anime(
    anilist_id=145064,
    title_romaji="Jujutsu Kaisen 2nd Season",
    title_english="Jujutsu Kaisen Season 2",
    status="FINISHED",
    format="TV",
    episodes=23,
)
JUJUTSU_S2.id = 9

GB = 1024**3


def seeded(title: str, seeders: int) -> NyaaItem:
    """One feed item, as ``test_nyaa`` builds them: the title is the question."""
    return NyaaItem(title=title, link="", info_hash="0" * 40, seeders=seeders)


def file(index: int, name: str, *, size: int = 10, priority: int = FILE_ON) -> FileInfo:
    """One row of ``torrents/files`` as a freshly added torrent reports it.

    ``priority`` defaults to on, because that is what the client says about a
    torrent it has not been told anything about yet — the state the add
    sequence's "every file off first" exists to replace.
    """
    return FileInfo(index=index, name=name, size=size, priority=priority, progress=0.0)


def pack(*numbers: int, group: str = "Erai-raws") -> list[FileInfo]:
    """A *Kimetsu* pack holding the given episodes, one file each."""
    return [
        file(
            index,
            f"Kimetsu no Yaiba/[{group}] Kimetsu no Yaiba - {number:02d} [1080p].mkv",
            size=GB,
        )
        for index, number in enumerate(numbers)
    ]


def batch(title: str, *, number: int = 10) -> Ranked:
    """One batch candidate through the real filter, wrapped for :func:`targets`.

    Built rather than invented: ``covers`` and ``offset`` are what
    :func:`targets` reads, and both are the filter's answers about a real
    release name.
    """
    candidate = acceptable(
        seeded(title, 20),
        titles=("Kimetsu no Yaiba", "Demon Slayer: Kimetsu no Yaiba"),
        number=number,
        season=None,
        batches=True,
    )
    assert isinstance(candidate, Candidate), title
    return Ranked(candidate=candidate, group_rank=0, resolution_rank=0, seeders=20, trusted=False)


# --- The ordinary case ------------------------------------------------------


def test_the_wanted_episode_is_found_and_nothing_else_is_selected() -> None:
    """The whole claim of the feature, as one assertion.

    Twenty-six episodes in the torrent and one of them selected: the pack is
    what gets added and one file is what gets fetched.
    """
    files = pack(*range(1, 27))

    plan = plan_files(KIMETSU, (10,), files, required=10)

    assert plan.refused_reason is None
    assert plan.wanted == (10,)
    assert plan.wanted_indices == (9,)
    assert plan.indices == tuple(range(26))
    assert plan.episodes[10].name.endswith("- 10 [1080p].mkv")


def test_the_selected_bytes_are_a_fraction_of_the_payload() -> None:
    """``wanted_bytes`` is the only size figure any rule may read (FR-A11)."""
    plan = plan_files(KIMETSU, (10, 11), pack(*range(1, 27)), required=10)

    assert plan.wanted_bytes == 2 * GB
    assert plan.total_size == 26 * GB
    assert plan.wanted_bytes < plan.total_size
    assert size_label(plan.wanted_bytes) == "2.0 GB"
    assert size_label(plan.total_size) == "26.0 GB"


def test_every_file_is_planned_for_a_row_even_the_ones_nobody_asked_for() -> None:
    """All of them get a ``torrent_files`` row; only two are wanted.

    The not-wanted rows are the point of the table: they are what a later want
    attaches to without a search (``claim_existing``).
    """
    plan = plan_files(KIMETSU, (10,), pack(*range(1, 27)), required=10)

    assert len(plan.files) == 26
    assert sorted(plan.episodes) == list(range(1, 27))
    assert plan.wanted == (10,)


def test_a_second_wanted_episode_in_the_same_pack_is_selected_too() -> None:
    plan = plan_files(KIMETSU, (10, 11), pack(*range(1, 27)), required=10)

    assert plan.wanted == (10, 11)
    assert plan.wanted_indices == (9, 10)


# --- What maps to nothing ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Kimetsu no Yaiba/[Erai-raws] Kimetsu no Yaiba - NCOP [1080p].mkv",
        "Kimetsu no Yaiba/[Erai-raws] Kimetsu no Yaiba - NCED1 [1080p].mkv",
        "Kimetsu no Yaiba/Kimetsu no Yaiba - 10.nfo",
        "Kimetsu no Yaiba/sample.mkv",
        "Kimetsu no Yaiba/fonts/Roboto.ttf",
        "Kimetsu no Yaiba/[Erai-raws] Kimetsu no Yaiba - 01 ~ 26 [1080p].mkv",
        "Kimetsu no Yaiba/[Erai-raws] Kimetsu no Yaiba - Movie [1080p].mkv",
    ],
)
def test_extras_and_non_video_files_map_to_no_episode(name: str) -> None:
    """Creditless openings, ``.nfo``s, samples, fonts, blobs and films.

    Excluded by the *kind* the parser reads and by the extension, without a
    rule of their own — which is why a new kind of extra needs no new code
    here, and why a file Arc cannot read fails closed at
    :data:`~arc.services.acquisition.qbit.FILE_OFF`.
    """
    files = [*pack(10), file(1, name, size=GB)]

    plan = plan_files(KIMETSU, (10,), files, required=10)

    assert plan.refused_reason is None
    assert plan.wanted_indices == (0,)
    assert 1 not in {info.index for info in plan.wanted_files}


def test_a_file_of_another_show_inside_the_pack_is_dropped() -> None:
    """The bonus OVA case: the title is checked when the file names one."""
    files = [*pack(10), file(1, "[Erai-raws] Gintama - 10 [1080p].mkv", size=GB)]

    plan = plan_files(KIMETSU, (10,), files, required=10)

    assert plan.wanted_indices == (0,)
    assert plan.episodes[10].index == 0


def test_a_file_naming_another_season_is_dropped() -> None:
    files = [file(0, "[Judas] Kimetsu no Yaiba S2 - 10 [BD 1080p].mkv", size=GB)]

    plan = plan_files(KIMETSU, (10,), files, required=10)

    assert plan.refused_reason == "no file in the batch is episode 10"


def test_a_file_that_names_no_title_is_still_taken_at_its_number() -> None:
    """The release name carried the identity; a member only carries a number.

    A pack's own name cleared the title floor before Arc fetched its
    ``.torrent``, so ``- 10 [1080p].mkv`` inside it is episode 10 of that show
    and nothing else.
    """
    files = [file(0, "- 10 [1080p].mkv", size=GB)]

    plan = plan_files(KIMETSU, (10,), files, required=10)

    assert plan.refused_reason is None
    assert plan.wanted_indices == (0,)


# --- Refusals ---------------------------------------------------------------


def test_two_files_claiming_one_episode_refuse_the_whole_batch() -> None:
    """A v1 beside a v2 is not resolved by version, size or position.

    "Never guess which file" is the non-negotiable this path is written around,
    and the honest ending is that the pack is not added at all.
    """
    files = [
        file(0, "[Erai-raws] Kimetsu no Yaiba - 10 [1080p].mkv", size=GB),
        file(1, "[Erai-raws] Kimetsu no Yaiba - 10v2 [1080p].mkv", size=GB),
    ]

    plan = plan_files(KIMETSU, (10,), files, required=10)

    assert plan.refused_reason is not None
    assert plan.refused_reason.startswith("2 files claim episode 10")
    assert "10v2" in plan.refused_reason
    # A refused plan selects nothing, whatever it could read.
    assert plan.wanted == () and plan.wanted_indices == () and plan.wanted_bytes == 0


def test_a_pack_that_does_not_hold_the_episode_at_all_is_refused() -> None:
    plan = plan_files(KIMETSU, (10,), pack(1, 2, 3), required=10)

    assert plan.refused_reason == "no file in the batch is episode 10"


def test_an_empty_torrent_is_refused_rather_than_planned() -> None:
    plan = plan_files(KIMETSU, (10,), [], required=10)

    assert plan.refused_reason == "no file in the batch is episode 10"
    assert plan.total_size == 0


def test_a_free_rider_the_pack_does_not_hold_is_dropped_not_refused() -> None:
    """The pack is still the answer for the episode Arc came for.

    Episode 27 asked to ride along and this pack has 26 files; refusing over
    that would leave episode 10 unfetched as well, and episode 27 has a search
    of its own (FR-A6) which will find its own pack or attach to this one.
    """
    plan = plan_files(KIMETSU, (10, 27), pack(*range(1, 27)), required=10)

    assert plan.refused_reason is None
    assert plan.wanted == (10,)
    assert plan.dropped == (27,)


def test_an_ambiguous_free_rider_is_dropped_too() -> None:
    """Two files claiming a free rider is still not a reason to refuse.

    It *is* a reason not to fetch either of them: the episode is dropped and
    nothing about it is guessed at.
    """
    files = [
        *pack(10),
        file(1, "[Erai-raws] Kimetsu no Yaiba - 11 [1080p].mkv", size=GB),
        file(2, "[Erai-raws] Kimetsu no Yaiba - 11v2 [1080p].mkv", size=GB),
    ]

    plan = plan_files(KIMETSU, (10, 11), files, required=10)

    assert plan.refused_reason is None
    assert plan.wanted == (10,) and plan.dropped == (11,)
    assert 11 not in plan.episodes


def test_the_searched_episode_is_wanted_even_if_nobody_asked_for_it() -> None:
    """``required`` is always in ``wanted``, whatever ``wanted_numbers`` says."""
    plan = plan_files(KIMETSU, (), pack(*range(1, 27)), required=10)

    assert plan.wanted == (10,)


# --- The absolute reading ---------------------------------------------------


def test_an_absolutely_numbered_file_is_read_with_the_offset() -> None:
    """``Jujutsu Kaisen - 25`` is episode 1 of a second season that follows 24."""
    files = [
        file(0, "[SubsPlease] Jujutsu Kaisen - 25 (1080p) [AAAAAAAA].mkv", size=GB),
        file(1, "[SubsPlease] Jujutsu Kaisen - 26 (1080p) [BBBBBBBB].mkv", size=GB),
    ]

    plan = plan_files(JUJUTSU_S2, (1,), files, required=1, offset=24)

    assert plan.refused_reason is None
    assert plan.wanted_indices == (0,)
    assert plan.episodes == {1: files[0], 2: files[1]}


def test_without_an_offset_the_same_file_is_episode_twenty_five() -> None:
    """No offset means "read the numbers as written"."""
    files = [file(0, "[SubsPlease] Jujutsu Kaisen - 25 (1080p) [AAAAAAAA].mkv", size=GB)]

    plan = plan_files(JUJUTSU_S2, (1,), files, required=1)

    assert plan.refused_reason == "no file in the batch is episode 1"


def test_a_file_that_names_its_season_is_never_read_absolutely() -> None:
    """A group that wrote ``S2`` has answered the question the arithmetic asks."""
    files = [file(0, "[SubsPlease] Jujutsu Kaisen S2 - 01 (1080p) [AAAAAAAA].mkv", size=GB)]

    plan = plan_files(JUJUTSU_S2, (1,), files, required=1, offset=24)

    assert plan.refused_reason is None
    assert plan.wanted_indices == (0,)


def test_a_number_at_or_below_the_prequel_total_maps_to_nothing() -> None:
    """The reading is only ever offered *above* the prequel's own total.

    ``- 24`` names no season and is not above 24, so the absolute reading does
    not apply — and the ordinary reading makes it season **one**'s episode 24,
    which is not this entry's. It maps to nothing, which is the right answer: a
    complete-franchise pack carries both seasons and the first one's files are
    not this row's.
    """
    files = [file(0, "[SubsPlease] Jujutsu Kaisen - 24 (1080p) [AAAAAAAA].mkv", size=GB)]

    plan = plan_files(JUJUTSU_S2, (24,), files, required=24, offset=24)

    assert plan.episodes == {}
    assert plan.refused_reason == "no file in the batch is episode 24"


# --- Which numbering a pack's files are read in -----------------------------


def test_a_pack_accepted_absolutely_carries_that_reading_into_its_files() -> None:
    candidate = acceptable(
        seeded("[Erai-raws] Jujutsu Kaisen - 25 ~ 47 [BATCH]", 20),
        titles=("Jujutsu Kaisen 2nd Season",),
        number=1,
        season=2,
        batches=True,
        offset=24,
    )
    assert isinstance(candidate, Candidate)
    chosen = Ranked(candidate=candidate, group_rank=0, resolution_rank=0, seeders=20, trusted=False)

    assert plan_offset(chosen, 24) == 24


def test_a_pack_that_named_its_own_range_is_read_per_season() -> None:
    """The one mistake this must not make.

    A group that numbered a season-two pack 1–26 means 1–26, and shifting it by
    the entry's offset would read its episode 25 as episode 1.
    """
    chosen = batch("[Erai-raws] Kimetsu no Yaiba - 01 ~ 26 [BATCH]")

    assert chosen.candidate.offset == 0 and chosen.candidate.covers != ()
    assert plan_offset(chosen, 24) is None


def test_a_pack_that_named_no_range_is_read_with_the_entrys_offset() -> None:
    """A complete-series pack of an absolutely numbered sequel is the case.

    It claimed no range, so there is nothing to read its files against but the
    entry — and a pack with no range is exactly the shape a whole-franchise
    Blu-ray comes in.
    """
    chosen = batch("[Judas] Kimetsu no Yaiba [BD 1080p][BATCH]")

    assert chosen.candidate.covers == ()
    assert plan_offset(chosen, 24) == 24
    assert plan_offset(chosen, None) is None


# --- Which episodes a batch is taken for ------------------------------------


def test_a_named_range_is_asked_only_for_the_wants_it_covers() -> None:
    """A pack of 1–12 is not refused for failing to hold episode 20."""
    chosen = batch("[Erai-raws] Kimetsu no Yaiba - 01 ~ 12 [BATCH]")

    assert targets(chosen, 10, (10, 11, 20)) == (10, 11)


def test_a_pack_that_names_no_range_is_asked_for_every_want() -> None:
    """Its coverage is unknowable from its name and settled by its file list."""
    chosen = batch("[Judas] Kimetsu no Yaiba [BD 1080p][BATCH]")

    assert chosen.candidate.covers == ()
    assert targets(chosen, 10, (10, 11, 20)) == (10, 11, 20)


def test_the_searched_episode_is_always_a_target() -> None:
    chosen = batch("[Erai-raws] Kimetsu no Yaiba - 01 ~ 12 [BATCH]")

    assert targets(chosen, 10, ()) == (10,)


def test_an_absolute_pack_counts_the_entrys_own_numbers() -> None:
    """A ``25 ~ 47`` pack of a second season that follows 24 holds its 1–23."""
    candidate = acceptable(
        seeded("[Erai-raws] Jujutsu Kaisen - 25 ~ 47 [BATCH]", 20),
        titles=("Jujutsu Kaisen 2nd Season",),
        number=1,
        season=2,
        batches=True,
        offset=24,
    )
    assert isinstance(candidate, Candidate)
    chosen = Ranked(candidate=candidate, group_rank=0, resolution_rank=0, seeders=20, trusted=False)

    assert chosen.candidate.offset == 24
    assert targets(chosen, 1, (1, 2, 30)) == (1, 2)


# --- The read-back gate -----------------------------------------------------


def test_a_selection_that_reads_back_as_written_agrees() -> None:
    before = pack(10, 11, 12)
    after = [
        file(0, before[0].name, size=GB, priority=FILE_ON),
        file(1, before[1].name, size=GB, priority=FILE_OFF),
        file(2, before[2].name, size=GB, priority=FILE_OFF),
    ]

    assert verify_selection(before, after, (0,)) is None


def test_an_extra_selected_file_fails_the_read_back() -> None:
    """The one that matters: bytes nobody asked for."""
    before = pack(10, 11)
    after = [
        file(0, before[0].name, size=GB, priority=FILE_ON),
        file(1, before[1].name, size=GB, priority=FILE_ON),
    ]

    assert verify_selection(before, after, (0,)) == "file 1 is selected and was not asked for"


def test_a_wanted_file_left_off_fails_the_read_back() -> None:
    """Starting a torrent whose selection did not land would fetch nothing."""
    before = pack(10)
    after = [file(0, before[0].name, size=GB, priority=FILE_OFF)]

    assert verify_selection(before, after, (0,)) == "file 0 was asked for and is not selected"


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ([], "the client listed 1 files and then 0"),
        (
            [file(0, "something-else.mkv", size=GB)],
            "file 0 changed name or size between the two listings",
        ),
        (
            [file(9, "[Erai-raws] Kimetsu no Yaiba - 10 [1080p].mkv", size=GB)],
            "file 9 was not in the first listing",
        ),
    ],
)
def test_a_listing_that_no_longer_describes_the_same_torrent_fails(
    after: list[FileInfo], expected: str
) -> None:
    """The plan was read off the first listing; a client that now says
    something else has invalidated the mapping it was built from."""
    before = [file(0, "[Erai-raws] Kimetsu no Yaiba - 10 [1080p].mkv", size=GB)]

    assert verify_selection(before, after, (0,)) == expected


def test_a_plan_is_the_only_source_of_its_own_numbers() -> None:
    """The derived figures are properties, so they cannot disagree with ``files``."""
    plan = FilePlan(anime_id=1, files=(file(0, "a.mkv", size=7),), episodes={}, wanted=())

    assert plan.total_size == 7
    assert plan.wanted_bytes == 0
    assert plan.indices == (0,)
    assert plan.wanted_indices == ()
