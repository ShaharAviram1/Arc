"""Parser behaviour the corpus does not pin (FR-L2).

``tests/fixtures/release_names.txt`` asserts title_key, episode, season, kind,
group and version over 247 names. This file covers the rest — the technical
fields, the batch span, the fractional-episode and Japanese-marker rules,
``title_key`` itself, and the handful of rules whose *reason* matters more
than any one filename does.
"""

from __future__ import annotations

import pytest

from arc.services.library.parser import ParsedName, parse, title_key


class TestTitleKey:
    def test_punctuation_becomes_a_space(self) -> None:
        assert title_key("Re:Zero kara Hajimeru") == title_key("Re Zero kara Hajimeru")
        assert title_key("Kaguya-sama") == "kaguya sama"
        assert title_key("Steins;Gate 0") == "steins gate 0"

    def test_case_and_whitespace_are_flattened(self) -> None:
        assert title_key("  BOCCHI  the   Rock!  ") == "bocchi the rock"

    def test_kana_survives(self) -> None:
        assert title_key("葬送のフリーレン") == "葬送のフリーレン"

    def test_empty_is_empty(self) -> None:
        assert title_key("") == ""
        assert title_key("---") == ""


class TestTechnicalFields:
    @pytest.mark.parametrize(
        ("name", "field", "expected"),
        [
            ("[G] Show - 01 [1080p].mkv", "resolution", "1080p"),
            ("[G] Show - 01 (1920x1080 Blu-ray FLAC).mkv", "resolution", "1080p"),
            ("[G] Show - 01 [4K][HEVC].mkv", "resolution", "2160p"),
            ("[G] Show - 01 [BD 1080p].mkv", "source", "BD"),
            ("Show.Name.S01E01.1080p.WEB-DL.x264-G.mkv", "source", "WEB"),
            ("[G] Show - 01 [DVDRip].mkv", "source", "DVD"),
            ("[G] Show - 01 [1080p][HEVC x265 10bit].mkv", "codec", "H265"),
            ("Show.Name.S01E01.1080p.WEB.H264-G.mkv", "codec", "H264"),
            ("[G] Show - 01 [1080p][AV1].mkv", "codec", "AV1"),
            ("[G] Show - 01 [1080p].mp4", "extension", "mp4"),
            ("[G] Show - 01 [1080p].mkv", "extension", "mkv"),
            ("[Anime Time] Kimi no Na wa. (2016) [Movie][BD 1080p].mkv", "year", 2016),
        ],
    )
    def test_field(self, name: str, field: str, expected: object) -> None:
        assert getattr(parse(name), field) == expected

    def test_an_unknown_extension_is_none(self) -> None:
        """Only the configured video extensions are recorded as one."""
        assert parse("[G] Show - 01 [1080p].nfo").extension is None


class TestBatches:
    def test_a_range_becomes_a_span(self) -> None:
        parsed = parse("[Judas] Fate Zero - 01-25 [1080p].mkv")
        assert (parsed.kind, parsed.episode, parsed.episode_end) == ("batch", 1, 25)
        assert parsed.episode_span == tuple(range(1, 26))
        assert parsed.is_batch

    def test_a_tilde_range_counts(self) -> None:
        assert parse("[Erai-raws] Bocchi the Rock! - 01~12 [1080p].mkv").episode_end == 12

    def test_a_hyphenated_title_number_is_not_a_batch(self) -> None:
        """``Ranma 1-2`` is a name; anitopy reads it as episodes 1 and 2."""
        parsed = parse("[G] Ranma 1-2 (2024) - 03 [1080p].mkv")
        assert parsed.kind == "episode"
        assert parsed.episode == 3
        assert parsed.episode_end is None

    def test_a_single_file_has_no_span_end(self) -> None:
        parsed = parse("[SubsPlease] Sousou no Frieren - 05 (1080p) [ABCD1234].mkv")
        assert parsed.episode_end is None
        assert parsed.episode_span == (5,)

    @pytest.mark.parametrize(
        ("name", "span"),
        [
            # The Dagashi Kashi shape: a title that ends in a number, a spaced
            # tilde range, and anitopy reporting only the "01". This is the
            # production failure of 2026-09-13 — 6.3 GB and twelve transcodes.
            ("[Erai-raws] Dagashi Kashi 2 - 01 ~ 12 [1080p][Multiple Subtitle]", (1, 12)),
            ("[Erai-raws] Bocchi the Rock! - 01~12 [1080p].mkv", (1, 12)),
            ("[Judas] Fate Zero - 01-25 [1080p].mkv", (1, 25)),
            ("[Group] Show - 01 - 12 [1080p].mkv", (1, 12)),
            ("[Group] Show - E01-E12 [1080p].mkv", (1, 12)),
            ("[Group] Show S01E01-E12 [1080p].mkv", (1, 12)),
            ("[Group] One Piece - 001-024 [480p].mkv", (1, 24)),
            # The wave dash and the fullwidth tilde a Japanese raw writes.
            ("[Group] Show - 01 〜 12 [1080p].mkv", (1, 12)),
            ("[Group] Show - 01〜12 [1080p].mkv", (1, 12)),
            ("[Group] Show - 01～12 [1080p].mkv", (1, 12)),
            # A two-episode pack. Padded, so it is a range and not a name.
            ("[RH] Fukigen na Mononokean - 01-02 (The Morose Mononokean) [1080p]", (1, 2)),
        ],
    )
    def test_a_range_at_the_episode_position_is_a_batch(
        self, name: str, span: tuple[int, int]
    ) -> None:
        parsed = parse(name)
        assert parsed.kind == "batch"
        assert (parsed.episode, parsed.episode_end) == span

    @pytest.mark.parametrize(
        "name",
        [
            "[Group] Show - 01〜12 [1080p].mkv",
            "[Group] Show - 01～12 [1080p].mkv",
        ],
    )
    def test_a_range_anitopy_cannot_read_is_taken_off_the_title(self, name: str) -> None:
        """anitopy knows ``~`` and not ``〜``, so it hands back ``Show - 01〜12``.

        A title that keeps the run in it is a title the matcher can never
        match, and a batch is exactly the file a person has to look at.
        """
        assert parse(name).title_key == "show"

    @pytest.mark.parametrize(
        ("name", "title_key_"),
        [
            ("[Group] 86 - 01〜11 [1080p].mkv", "86"),
            ("[Group] 07-Ghost - 01〜25 [1080p].mkv", "07 ghost"),
        ],
    )
    def test_only_the_files_own_run_comes_off_the_title(self, name: str, title_key_: str) -> None:
        """A number in the *name* of a show is not the run the file holds.

        The pattern is built from the two numbers the file was found to hold,
        so *86* keeps its title and *07-Ghost* keeps its hyphen.
        """
        parsed = parse(name)

        assert parsed.kind == "batch"
        assert parsed.title_key == title_key_

    def test_a_pair_of_years_is_not_a_range(self) -> None:
        """``1998-1999`` is when *Cowboy Bebop* aired, not what the file holds.

        Both ends have to be plausible years: ``0001-2000`` of One Piece is a
        run somebody really does upload.
        """
        parsed = parse("[Kametsu] Cowboy Bebop 1998-1999 [BD 1080p].mkv")

        assert parsed.kind != "batch"
        assert parsed.episode_end is None
        assert parse("[Judas] One Piece - 0001-1000 [1080p].mkv").episode_end == 1000

    def test_a_number_that_opens_an_episode_title_is_not_a_range(self) -> None:
        """anitopy reports ``- 01 - 100 Poems`` as the pair (1, 100).

        It is episode 1 of a show whose episode title starts with a number, and
        a range has to have nothing but a tag behind it to count as one.
        """
        parsed = parse("[Commie] Chihayafuru - 01 - 100 Poems [ABCD1234].mkv")

        assert (parsed.kind, parsed.episode, parsed.episode_end) == ("episode", 1, None)

    @pytest.mark.parametrize(
        "name",
        [
            "[Erai-raws] Shingeki no Kyojin Season 3 Part 2 - 01 ~ 10 [1080p][BATCH]",
            "[Erai-raws] Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season Part 2 - 01 ~ 12"
            " [1080p][Multiple Subtitles][Unofficial Batch]",
            "[Group] Show - 12 [Batch][1080p].mkv",
            "[Group] Show Season Pack [1080p].mkv",
            "Show.Name.Season.Pack.1080p.BluRay.x264-GROUP.mkv",
            "[Group] Show Season_Pack [1080p].mkv",
        ],
    )
    def test_a_batch_marker_is_a_batch_whatever_the_numbers_say(self, name: str) -> None:
        """``[BATCH]`` used to be read only when the name gave no number at all.

        Which is never true of the case it was for: a batch names the first
        episode of the run it holds, and Arc downloaded the run.
        """
        assert parse(name).kind == "batch"

    def test_complete_on_one_numbered_file_is_a_last_episode_not_a_season(self) -> None:
        """``COMPLETE`` is what a group shouts on the last episode of a run.

        The same claim ``END`` makes, which is why :data:`_END_RE` carries the
        word too — so unlike ``BATCH`` it is read only when the name names no
        single episode.
        """
        parsed = parse("[Group] Show - 12 [COMPLETE][1080p].mkv")

        assert (parsed.kind, parsed.episode, parsed.episode_end) == ("episode", 12, None)

    @pytest.mark.parametrize(
        "name",
        [
            "[Group] Show [Complete][1080p].mkv",
            "[Group] Show - 01-12 [Complete][1080p].mkv",
            "[Kametsu] Cowboy Bebop Complete Series (BD 1080p Hi10P FLAC).mkv",
            "[Kametsu] Gurren Lagann S1 Complete (BD 1080p Hi10P FLAC).mkv",
        ],
    )
    def test_complete_with_no_single_episode_is_still_a_batch(self, name: str) -> None:
        assert parse(name).kind == "batch"

    @pytest.mark.parametrize(
        ("name", "episode"),
        [
            # A version marker is not a range, and neither is a resolution, a
            # bit depth, a language pair or a date.
            ("[Group] Show - 01v2 [1080p].mkv", 1),
            ("[Group] Show - 12v2 [1920x1080][10-bit][FLAC].mkv", 12),
            ("[Group] Show - 05 [1080p][x264-AAC][10-bit].mkv", 5),
            ("[Erai-raws] Show - 05 [1080p][Multiple Subtitle] [ENG][POR-BR][SPA-LA]", 5),
            ("Show.Name.S01E05.2024-05-10.1080p.WEB-DL.x264-GROUP.mkv", 5),
            # A title that ends in a padded number, counting down or from zero.
            ("[Group] Mob Psycho 100 - 07 [1080p].mkv", 7),
            ("[Group] Mobile Suit Gundam 00 - 05 [1080p].mkv", 5),
            ("[Group] 86 - 09 [1080p].mkv", 9),
            ("[Group] 07-Ghost - 05 [1080p].mkv", 5),
            # An episode number the release wrote with no range anywhere.
            ("[Erai-raws] Dagashi Kashi 2 - 10 [1080p][Multiple Subtitle].mkv", 10),
        ],
    )
    def test_a_single_episode_stays_a_single_episode(self, name: str, episode: int) -> None:
        parsed = parse(name)
        assert (parsed.kind, parsed.episode, parsed.episode_end) == ("episode", episode, None)

    def test_the_files_inside_a_batch_are_ordinary_episodes(self) -> None:
        """What ingest actually sees. A batch directory holds episode files.

        The directory is a batch and never becomes a ``media_files`` row; the
        files in it are what Arc scans, and each of them is one episode. Only
        the last path component is read, so the batch's own name in the path
        above must not make an episode of it a batch.
        """
        directory = "[Erai-raws] Dagashi Kashi 2 - 01 ~ 12 [1080p][Multiple Subtitle]"
        for number in (1, 10, 12):
            name = f"{directory}/[Erai-raws] Dagashi Kashi 2 - {number:02d} [1080p].mkv"
            parsed = parse(name)
            assert (parsed.kind, parsed.episode, parsed.episode_end) == ("episode", number, None)


class TestKinds:
    @pytest.mark.parametrize(
        "name",
        [
            "[G] Some Show - NCOP.mkv",
            "[G] Some Show - NCED2 [1080p].mkv",
            "[G] Some Show - PV [1080p].mkv",
            "[G] Some Show - Trailer 2 [1080p].mkv",
            "[G] Some Show [Creditless Opening][1080p].mkv",
        ],
    )
    def test_creditless_files_are_nc_with_no_episode(self, name: str) -> None:
        """An OP's ``2`` is an OP index, not episode 2 (FR-L4)."""
        parsed = parse(name)
        assert parsed.kind == "nc"
        assert parsed.episode is None

    def test_a_movie_has_no_episode(self) -> None:
        assert parse("[G] Suzume no Tojimari (2022) [Movie][1080p].mkv").episode is None

    def test_an_ova_keeps_its_number(self) -> None:
        parsed = parse("[G] Shingeki no Kyojin - OVA 02 [1080p].mkv")
        assert (parsed.kind, parsed.episode) == ("special", 2)

    def test_a_nameless_file_is_unknown(self) -> None:
        assert parse("[G] Sousou no Frieren [1080p].mkv").kind == "unknown"


class TestSeasons:
    @pytest.mark.parametrize(
        "name",
        [
            "[G] Show Name 2nd Season - 04 [1080p].mkv",
            "[G] Show Name Season 2 - 04 [1080p].mkv",
            "[G] Show Name S2 - 04 [1080p].mkv",
            "[G] Show Name II - 04 [1080p].mkv",
            "[G] Show Name Part 2 - 04 [1080p].mkv",
        ],
    )
    def test_five_spellings_agree(self, name: str) -> None:
        parsed = parse(name)
        assert (parsed.title_key, parsed.season, parsed.episode) == ("show name", 2, 4)

    def test_a_marker_before_a_subtitle_is_found(self) -> None:
        parsed = parse("[G] Mushoku Tensei II - Isekai Ittara Honki Dasu - 03 [1080p].mkv")
        assert parsed.season == 2
        assert parsed.title_key == "mushoku tensei isekai ittara honki dasu"

    @pytest.mark.parametrize(
        ("name", "expected_key"),
        [
            ("[Commie] Chihayafuru 3 - 24 [BD 720p].mkv", "chihayafuru 3"),
            ("[Commie] Steins;Gate 0 - 11 [BD 1080p].mkv", "steins gate 0"),
            ("86 - 09.mkv", "86"),
            ("[G] Mob Psycho 100 - 07 [1080p].mkv", "mob psycho 100"),
            ("[G] Ghost in the Shell SAC_2045 - 06 [1080p].mkv", "ghost in the shell sac 2045"),
        ],
    )
    def test_a_bare_number_is_part_of_the_name(self, name: str, expected_key: str) -> None:
        parsed = parse(name)
        assert parsed.title_key == expected_key
        assert parsed.season is None

    def test_a_season_range_names_no_season(self) -> None:
        parsed = parse("[Judas] Overlord - S1-S4 - 01-52 [1080p].mkv")
        assert parsed.season is None
        assert parsed.title_key == "overlord"


class TestFractionalEpisodes:
    """``12.5`` is a recap between 12 and 13, not episode 125 and not 12.

    The number is universal and means one thing everywhere it appears. Read
    naively it became **125** — ``re.sub(r"[^0-9]", "", "12.5")`` — which is an
    episode no show has, so every such file went to review with nothing usable
    in it.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "Title - 12.5.mkv",
            "[Doki] Ore no Imouto ga Konnani Kawaii Wake ga Nai - 12.5 (1280x720 Hi10P AAC).mkv",
            "[G] Some Show - EP12.5 [1080p].mkv",
        ],
    )
    def test_the_number_splits_into_an_episode_and_a_fraction(self, name: str) -> None:
        parsed = parse(name)
        assert parsed.episode == 12
        assert parsed.episode_fraction == 0.5

    def test_it_is_a_special_not_an_episode(self) -> None:
        """Linking it as episode 12 would put a summary where the episode goes."""
        assert parse("Title - 12.5.mkv").kind == "special"

    def test_an_ordinary_number_has_no_fraction(self) -> None:
        assert parse("[G] Some Show - 12 [1080p].mkv").episode_fraction is None

    def test_a_resolution_is_not_a_fractional_episode(self) -> None:
        parsed = parse("[G] Some Show - 03 (1920x1080 Blu-ray FLAC).mkv")
        assert (parsed.episode, parsed.episode_fraction) == (3, None)

    def test_the_fraction_survives_as_dict(self) -> None:
        import json

        payload = parse("Title - 12.5.mkv").as_dict()
        assert json.loads(json.dumps(payload))["episode_fraction"] == 0.5


class TestJapaneseEpisodeMarkers:
    """``第3話`` is "episode 3", and a raw group writes nothing else.

    Both halves matter. Unread, the file has no episode number at all; unstripped,
    the marker stays in the title and every episode of a show becomes a
    different title, none of them the one the catalogue carries.
    """

    @pytest.mark.parametrize(
        ("name", "expected_key"),
        [
            ("[Zero-Raws] Sousou no Frieren 第3話 (MX 1280x720 x264 AAC).mp4", "sousou no frieren"),
            ("[Zero-Raws] 葬送のフリーレン 第03話 (MX 1280x720 x264 AAC).mp4", "葬送のフリーレン"),
            ("[Group] Shingeki no Kyojin 第 3 話 [1080p].mkv", "shingeki no kyojin"),
        ],
    )
    def test_the_number_is_read_and_the_marker_is_stripped(
        self, name: str, expected_key: str
    ) -> None:
        parsed = parse(name)
        assert parsed.episode == 3
        assert parsed.kind == "episode"
        assert parsed.title_key == expected_key

    def test_a_leading_zero_is_not_a_different_episode(self) -> None:
        first = parse("[G] Show 第3話.mkv")
        second = parse("[G] Show 第03話.mkv")
        assert first.episode == second.episode == 3

    def test_a_number_the_release_also_writes_in_arabic_wins(self) -> None:
        """anitopy's own answer is not overridden; the marker is a fallback."""
        parsed = parse("[G] Show - 07 第7話 [1080p].mkv")
        assert parsed.episode == 7


class TestRobustness:
    def test_a_path_is_reduced_to_its_basename(self) -> None:
        long = "/data/downloads/[G] Show/[G] Show - 03 [1080p].mkv"
        assert parse(long).title_key == "show"
        assert parse(long).raw == long

    @pytest.mark.parametrize("name", ["", ".", "....", "   ", "[]", "[][][]", "x" * 400])
    def test_nonsense_does_not_raise(self, name: str) -> None:
        parsed = parse(name)
        assert isinstance(parsed, ParsedName)
        assert parsed.raw == name

    def test_as_dict_is_json_safe(self) -> None:
        import json

        payload = parse("[SubsPlease] Sousou no Frieren - 05 (1080p) [ABCD1234].mkv").as_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["title_key"] == "sousou no frieren"
