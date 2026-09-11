"""Deciding what to transcode, from an ffprobe payload alone (FR-P2).

Everything here is **pure**. It takes the JSON ffprobe printed, the two
language preferences out of ``settings``, and the two paths the job already
knows, and returns a :class:`TranscodePlan`: which video stream, which audio
stream, which subtitle track, and the exact ``ffmpeg`` argument list that
follows from them. No filesystem, no subprocess, no database — so the whole of
"which track would Arc pick?" is answerable in a unit test over a captured
probe, which is the only way the track rules can be trusted.

Three rules, and each one exists because of a real file.

* **The video is the first stream that is not a cover.** A release with an
  embedded poster carries it as a video stream with ``disposition
  .attached_pic``; mapping ``0:v:0`` would encode the poster.
* **The audio is the first stream in the wanted language, else the first
  audio.** A dual-audio release lists English first as often as not, and the
  configured default is Japanese (FR-P2).
* **The subtitle is chosen, not defaulted.** A ``MultiSub`` release has
  sixteen text tracks in fourteen languages, and among the ones in the right
  language there is often a "Signs & Songs" track that renders nothing but
  the odd sign. Picking the wrong one produces an episode that looks fine
  until somebody speaks. The preference order is spelled out in
  :func:`subtitle_score`, most significant first.

**Bitmap subtitles are not subtitles here.** ``hdmv_pgs_subtitle`` and
``dvd_subtitle`` can be burned in — ``overlay`` handles them — but they carry
no styling, break on scaling and are a separate code path for a case Arc's
sources (Crunchyroll WEB-DLs, fansub MKVs) do not produce. They are recorded
as a note and the episode is transcoded without subtitles, which is exactly
what FR-P2 says to do when there is no usable text track.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

#: Subtitle codecs libass or the ``subtitles`` filter can render from a file.
TEXT_SUBTITLE_CODECS: Final[frozenset[str]] = frozenset(
    {"ass", "ssa", "subrip", "srt", "webvtt", "vtt", "mov_text", "text"}
)

#: The ones that go through libass with styling intact.
ASS_CODECS: Final[frozenset[str]] = frozenset({"ass", "ssa"})

#: Picture-based subtitle codecs. Recognised only so the note can name them.
BITMAP_SUBTITLE_CODECS: Final[frozenset[str]] = frozenset(
    {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
)

#: Words in a track title that mean "this one has the dialogue in it".
FULL_TITLE_WORDS: Final[tuple[str, ...]] = ("full", "dialogue", "dialog", "subtitles")

#: And the ones that mean "this one is for a dub viewer": signs and song
#: lyrics only. A track whose title says both ("Signs & Full") is read as full.
SIGNS_TITLE_WORDS: Final[tuple[str, ...]] = ("signs", "songs", "s&s", "forced")

#: ISO 639-1 → every tag Arc accepts for it. ffprobe reports 639-2/B for
#: Matroska ("jpn", "ger", "fre"), the settings table holds 639-1 ("ja"), and
#: an MP4 can carry either plus a region ("en-US", stripped before lookup).
#: Only the languages Arc's sources actually ship; anything unlisted still
#: matches itself, so an exotic tag is compared literally rather than ignored.
LANGUAGE_ALIASES: Final[Mapping[str, frozenset[str]]] = {
    "en": frozenset({"en", "eng", "english"}),
    "ja": frozenset({"ja", "jpn", "jap", "japanese"}),
    "es": frozenset({"es", "spa", "esp", "spanish"}),
    "pt": frozenset({"pt", "por", "portuguese"}),
    "fr": frozenset({"fr", "fre", "fra", "french"}),
    "de": frozenset({"de", "ger", "deu", "german"}),
    "it": frozenset({"it", "ita", "italian"}),
    "ru": frozenset({"ru", "rus", "russian"}),
    "ar": frozenset({"ar", "ara", "arabic"}),
    "zh": frozenset({"zh", "chi", "zho", "chinese"}),
    "ko": frozenset({"ko", "kor", "korean"}),
    "pl": frozenset({"pl", "pol", "polish"}),
    "id": frozenset({"id", "ind", "indonesian"}),
    "th": frozenset({"th", "tha", "thai"}),
    "vi": frozenset({"vi", "vie", "vietnamese"}),
    "ms": frozenset({"ms", "may", "msa", "malay"}),
    "nl": frozenset({"nl", "dut", "nld", "dutch"}),
    "tr": frozenset({"tr", "tur", "turkish"}),
    "he": frozenset({"he", "heb", "hebrew"}),
    "hi": frozenset({"hi", "hin", "hindi"}),
}

#: File names inside the job's work directory. Relative, and deliberately
#: boring: ffmpeg's filter graph parser gives ``:``, ``'``, ``[`` and ``\`` a
#: meaning of their own, and a release called
#: ``[Erai-raws] … [1080p CR WEB-DL][MultiSub][1E63CFD7].mkv`` contains three
#: of them. The subtitle track is extracted to one of these names and the
#: filter is pointed at *that*, so nothing from the source name ever reaches
#: the filter graph. ffmpeg runs with the rendition directory as its working
#: directory, which is what keeps these relative.
WORK_DIR: Final[str] = "_work"
ASS_FILE: Final[str] = f"{WORK_DIR}/sub.ass"
SRT_FILE: Final[str] = f"{WORK_DIR}/sub.srt"
FONTS_DIR: Final[str] = f"{WORK_DIR}/fonts"

#: The three names the HLS muxer writes, relative to the rendition directory.
PLAYLIST_NAME: Final[str] = "index.m3u8"
INIT_NAME: Final[str] = "init.mp4"
SEGMENT_PATTERN: Final[str] = "seg_%05d.m4s"
SEGMENT_GLOB: Final[str] = "seg_*.m4s"

#: Notes a plan can carry. Short sentences: they are logged, stored on the job
#: payload and (for the first two) explain a rendition with no subtitles.
NOTE_NO_SUBTITLES = "the source has no subtitle track; nothing was burned in"
NOTE_BITMAP_ONLY = "the only subtitle tracks are bitmap ({codecs}); nothing was burned in"
NOTE_NO_SUB_LANGUAGE = "no {lang} subtitle track; used {chosen} instead"
NOTE_NO_AUDIO_LANGUAGE = "no {lang} audio track; used {chosen} instead"
NOTE_NO_AUDIO = "the source has no audio track"


class PlanError(ValueError):
    """The source cannot be transcoded at all — no video stream."""


def normalise_language(tag: str | None) -> str | None:
    """A language tag reduced to something comparable, or ``None``.

    ``"en-US"`` and ``"ENG "`` both come back lowercased and without the
    region. ``"und"`` — Matroska's "undetermined", which is what an untagged
    track carries — is ``None``, so it never matches a preference and is only
    ever picked as a fallback.
    """
    if not tag:
        return None
    cleaned = tag.strip().lower().replace("_", "-").split("-")[0]
    if not cleaned or cleaned in {"und", "unk", "none", "mis", "zxx"}:
        return None
    return cleaned


def canonical_language(tag: str | None) -> str | None:
    """A language tag as the two-letter code Arc stores and compares against.

    ``"jpn"`` and ``"ja-JP"`` are both ``"ja"``. The ``renditions`` row, the
    API and the ``settings`` table all speak ISO 639-1, so the conversion
    happens once, here, rather than at each of the three. A tag Arc has no
    alias for is kept as it is — better a rendition that says ``swa`` than one
    that says nothing.
    """
    normalised = normalise_language(tag)
    if normalised is None:
        return None
    if normalised in LANGUAGE_ALIASES:
        return normalised
    for canonical, aliases in LANGUAGE_ALIASES.items():
        if normalised in aliases:
            return canonical
    return normalised


def language_matches(tag: str | None, wanted: str) -> bool:
    """Whether a stream's language tag is the ``wanted`` language.

    ``wanted`` is what the settings table holds (``"en"``, ``"ja"``); ``tag``
    is whatever the container says. Both are normalised and then compared
    through :data:`LANGUAGE_ALIASES`, so ``"ja"`` matches ``"jpn"`` and
    ``"de"`` matches ``"ger"`` and ``"deu"``.
    """
    stream = normalise_language(tag)
    want = normalise_language(wanted)
    if stream is None or want is None:
        return False
    if stream == want:
        return True
    aliases = LANGUAGE_ALIASES.get(want)
    if aliases is not None and stream in aliases:
        return True
    # The preference may itself be written the long way ("jpn" in the
    # settings table). Look it up from the other side too.
    for canonical, group in LANGUAGE_ALIASES.items():
        if want in group:
            return stream == canonical or stream in group
    return False


@dataclass(frozen=True, slots=True)
class Stream:
    """One stream of the source, as the plan refers to it.

    ``index`` is ffprobe's absolute stream index and is what the log and the
    API report; ``type_index`` is the position *within its own kind*, which is
    what ``-map 0:s:2`` means. Confusing the two is how a job ends up burning
    in the Portuguese track, so both are carried explicitly and neither is
    recomputed anywhere else.
    """

    index: int
    type_index: int
    codec: str | None = None
    language: str | None = None
    title: str | None = None
    forced: bool = False
    default: bool = False
    width: int | None = None
    height: int | None = None

    @property
    def is_ass(self) -> bool:
        return (self.codec or "").lower() in ASS_CODECS


@dataclass(frozen=True, slots=True)
class TranscodePlan:
    """What one transcode is going to do.

    Immutable and printable: the handler logs it verbatim before it starts, so
    "which track did it pick?" is answered by the log of the run rather than
    by re-deriving it afterwards from a file that may since have been deleted.
    """

    source: Path
    output_dir: Path
    video: Stream
    audio: Stream | None = None
    subtitle: Stream | None = None
    #: Source duration in seconds, from ffprobe's ``format`` block. The
    #: denominator of every progress fraction; ``None`` means progress cannot
    #: be reported and the job shows a stage without a percentage.
    duration: float | None = None
    #: Attachment streams, in order, with the file name each should be
    #: written under. Empty for a container with no fonts (an MP4, an SRT-only
    #: MKV), which is the ordinary case outside fansub releases.
    attachments: tuple[tuple[int, str], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def subtitle_lang(self) -> str | None:
        """The language recorded on the rendition row; ``None`` for no subs."""
        return canonical_language(self.subtitle.language) if self.subtitle else None

    @property
    def audio_lang(self) -> str | None:
        return canonical_language(self.audio.language) if self.audio else None

    @property
    def width(self) -> int | None:
        return self.video.width

    @property
    def height(self) -> int | None:
        return self.video.height

    @property
    def subtitle_file(self) -> str | None:
        """Where :mod:`arc.services.media.transcode` extracts the track to.

        ``.ass`` when the track is ASS or SSA (libass renders it with its
        styling), ``.srt`` otherwise — a text format ffmpeg can always convert
        to and the ``subtitles`` filter can always read.
        """
        if self.subtitle is None:
            return None
        return ASS_FILE if self.subtitle.is_ass else SRT_FILE

    def as_dict(self) -> dict[str, Any]:
        """The plan as one log line's worth of fields."""
        return {
            "source": str(self.source),
            "output_dir": str(self.output_dir),
            "video_stream": self.video.index,
            "audio_stream": self.audio.index if self.audio else None,
            "audio_lang": self.audio_lang,
            "subtitle_stream": self.subtitle.index if self.subtitle else None,
            "subtitle_lang": self.subtitle_lang,
            "subtitle_codec": self.subtitle.codec if self.subtitle else None,
            "subtitle_title": self.subtitle.title if self.subtitle else None,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "attachments": len(self.attachments),
            "notes": list(self.notes),
        }


def _tags(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    tags = raw.get("tags")
    return tags if isinstance(tags, Mapping) else {}


def _disposition(raw: Mapping[str, Any], key: str) -> bool:
    disposition = raw.get("disposition")
    if not isinstance(disposition, Mapping):
        return False
    return bool(disposition.get(key))


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _stream(raw: Mapping[str, Any], type_index: int) -> Stream:
    tags = _tags(raw)
    codec = raw.get("codec_name")
    return Stream(
        index=_int(raw.get("index")) or 0,
        type_index=type_index,
        codec=str(codec) if codec is not None else None,
        language=tags.get("language"),
        title=tags.get("title"),
        forced=_disposition(raw, "forced"),
        default=_disposition(raw, "default"),
        width=_int(raw.get("width")),
        height=_int(raw.get("height")),
    )


def _by_type(payload: Mapping[str, Any]) -> dict[str, list[tuple[Mapping[str, Any], Stream]]]:
    """Group the probe's streams by codec type, numbering each kind from 0."""
    grouped: dict[str, list[tuple[Mapping[str, Any], Stream]]] = {}
    raw_streams = payload.get("streams")
    if not isinstance(raw_streams, Sequence):
        return grouped
    for raw in raw_streams:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("codec_type") or "")
        bucket = grouped.setdefault(kind, [])
        bucket.append((raw, _stream(raw, len(bucket))))
    return grouped


def _title_words(stream: Stream) -> tuple[bool, bool]:
    """``(looks like a full track, looks like a signs-only track)``."""
    title = (stream.title or "").lower()
    full = any(word in title for word in FULL_TITLE_WORDS)
    signs = any(word in title for word in SIGNS_TITLE_WORDS)
    return full, signs and not full


def subtitle_score(stream: Stream, *, sub_lang: str) -> tuple[int, ...]:
    """How much Arc wants to burn ``stream`` in. Bigger is better.

    A tuple compared left to right, so the order below *is* the preference
    order and a test can assert each level independently:

    1. the track is in the configured language;
    2. it is not a signs-and-songs track;
    3. its title says it carries the dialogue;
    4. it is not a forced track;
    5. it is ASS/SSA rather than SRT (styling, positioning, typesetting);
    6. it is the container's default track.

    The index is not in the tuple: :func:`_pick_subtitle` breaks ties on it, so
    "the first one that is otherwise equally good" stays the answer.
    """
    full, signs = _title_words(stream)
    return (
        int(language_matches(stream.language, sub_lang)),
        int(not signs),
        int(full),
        int(not stream.forced),
        int(stream.is_ass),
        int(stream.default),
    )


def _pick_subtitle(streams: Sequence[Stream], *, sub_lang: str) -> tuple[Stream | None, list[str]]:
    """The track to burn in, and any note about how that went."""
    notes: list[str] = []
    text = [s for s in streams if (s.codec or "").lower() in TEXT_SUBTITLE_CODECS]
    if not text:
        bitmap = sorted({(s.codec or "?").lower() for s in streams})
        if bitmap:
            notes.append(NOTE_BITMAP_ONLY.format(codecs=", ".join(bitmap)))
        else:
            notes.append(NOTE_NO_SUBTITLES)
        return None, notes

    chosen = max(text, key=lambda s: (subtitle_score(s, sub_lang=sub_lang), -s.index))
    if not language_matches(chosen.language, sub_lang):
        notes.append(
            NOTE_NO_SUB_LANGUAGE.format(
                lang=sub_lang, chosen=canonical_language(chosen.language) or "an untagged track"
            )
        )
    return chosen, notes


def _pick_audio(streams: Sequence[Stream], *, audio_lang: str) -> tuple[Stream | None, list[str]]:
    if not streams:
        return None, [NOTE_NO_AUDIO]
    for stream in streams:
        if language_matches(stream.language, audio_lang):
            return stream, []
    chosen = streams[0]
    return chosen, [
        NOTE_NO_AUDIO_LANGUAGE.format(
            lang=audio_lang, chosen=canonical_language(chosen.language) or "an untagged track"
        )
    ]


def _duration(payload: Mapping[str, Any]) -> float | None:
    container = payload.get("format")
    if not isinstance(container, Mapping):
        return None
    try:
        duration = float(container["duration"])
    except KeyError, TypeError, ValueError:
        return None
    return duration if duration > 0 else None


def attachment_name(raw_name: Any, type_index: int) -> str:
    """A safe file name for an attachment, from the one the container gives.

    The name is written by whoever muxed the file, so it is input: a font
    called ``../../../etc/passwd`` must land in the fonts directory as an
    unremarkable file and nowhere else. Everything but letters, digits, dot,
    dash and underscore is dropped, and leading dots go with it.

    What survives is then **prefixed with the attachment's own index**, which
    is what makes the name unique. Two different fonts in one container can
    perfectly well both be called ``font.ttf`` — and sanitising collapses more
    than that, since ``a b;rm -rf /.ttf`` and ``abrmrf.ttf`` clean to the same
    string — and ffmpeg dumps them in one command, so the second would silently
    overwrite the first and libass would render half the typesetting in the
    wrong face. The prefix also gives the fallback for a name that sanitised to
    nothing something to be: ``007_font``.
    """
    name = str(raw_name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(char for char in name if char.isalnum() or char in "._-").lstrip(".")
    return f"{type_index:03d}_{cleaned or 'font'}"


def build_plan(
    payload: Mapping[str, Any] | None,
    *,
    source: Path,
    output_dir: Path,
    sub_lang: str = "en",
    audio_lang: str = "ja",
) -> TranscodePlan:
    """Turn an ffprobe payload into a :class:`TranscodePlan`.

    Raises :class:`PlanError` when there is nothing to encode — no probe at
    all, or a file whose only video stream is a cover image. Both are real
    failures of the job rather than degraded plans: an episode with no picture
    is not an episode.
    """
    if not payload:
        raise PlanError(f"ffprobe returned nothing for {source}")

    grouped = _by_type(payload)
    videos = [
        stream for raw, stream in grouped.get("video", ()) if not _disposition(raw, "attached_pic")
    ]
    if not videos:
        raise PlanError(f"no playable video stream in {source}")

    audios = [stream for _, stream in grouped.get("audio", ())]
    subtitles = [stream for _, stream in grouped.get("subtitle", ())]

    audio, audio_notes = _pick_audio(audios, audio_lang=audio_lang)
    subtitle, subtitle_notes = _pick_subtitle(subtitles, sub_lang=sub_lang)
    attachments = tuple(
        (stream.type_index, attachment_name(_tags(raw).get("filename"), stream.type_index))
        for raw, stream in grouped.get("attachment", ())
    )

    return TranscodePlan(
        source=source,
        output_dir=output_dir,
        video=videos[0],
        audio=audio,
        subtitle=subtitle,
        duration=_duration(payload),
        attachments=attachments,
        notes=tuple(audio_notes + subtitle_notes),
    )


@dataclass(frozen=True, slots=True)
class EncodeOptions:
    """The knobs ``arc/config.py`` holds, in the shape the args builder wants.

    The defaults are the defaults in :class:`~arc.config.Settings`, so a test
    that builds a bare ``EncodeOptions()`` encodes what production encodes.
    """

    video_encoder: str = "libx264"
    preset: str = "fast"
    crf: int = 19
    #: x264's content tuning, or ``None``/``""`` for none at all. ``-tune``
    #: is only passed when it is set: an empty string on the command line is
    #: not "no tuning", it is an unknown tune and ffmpeg exits on it.
    tune: str | None = "animation"
    segment_seconds: int = 6
    audio_bitrate: str = "160k"
    #: Optional VBV ceiling in kbit/s. ``bufsize`` defaults to twice the
    #: maxrate when only the maxrate is set; neither is passed when the
    #: maxrate is not, because a bufsize alone means nothing to x264.
    maxrate_kbps: int | None = None
    bufsize_kbps: int | None = None
    #: Set once the subtitle track has actually been extracted. ``None`` means
    #: "no burn-in": either the source had no text track, or the extraction
    #: failed and the job chose a subtitle-less encode over no episode at all.
    subtitle_file: str | None = None
    fonts_dir: str | None = field(default=FONTS_DIR)


def subtitle_filter(subtitle_file: str, *, fonts_dir: str | None = FONTS_DIR) -> str:
    """The ``-vf`` value that burns ``subtitle_file`` in.

    Both paths are **relative**, and that is the whole point of this function:
    ffmpeg runs with the rendition directory as its working directory, so the
    filter graph never contains a character that came from the release name.
    ``ass=`` for an ASS file (libass, with the source's own styling and the
    extracted fonts), ``subtitles=`` for anything else.
    """
    name = "ass" if subtitle_file.endswith(".ass") else "subtitles"
    parts = [f"{name}={subtitle_file}"]
    if fonts_dir:
        parts.append(f"fontsdir={fonts_dir}")
    return ":".join(parts)


def encode_args(plan: TranscodePlan, options: EncodeOptions | None = None) -> list[str]:
    """The ffmpeg arguments for the encode, without the binary.

    A list, never a string, and never handed to a shell: the source path is
    the one piece of this that Arc did not write, and it routinely contains
    brackets, spaces and apostrophes.

    Keyframes are forced onto the segment boundary with ``-force_key_frames``
    rather than left to ``-g``: the HLS muxer can only cut where there is an
    IDR frame, and without this a 6-second target produces segments of 4 and
    11 seconds, which hls.js stalls on when seeking.

    Nothing here scales the picture, and that is on purpose: Arc serves one
    rendition at the source's own resolution, so the only filter in the graph
    is the subtitle burn-in and swscale is never asked to resample. It is also
    why there is no ``-sws_flags``: the flag would describe a resize that does
    not happen (architecture.md §5.3a).
    """
    options = options or EncodeOptions()
    segment = max(int(options.segment_seconds), 1)

    args = [
        "-nostdin",
        "-hide_banner",
        "-y",
        "-i",
        str(plan.source),
        "-map",
        f"0:v:{plan.video.type_index}",
    ]
    if plan.audio is not None:
        args += ["-map", f"0:a:{plan.audio.type_index}"]
    if options.subtitle_file:
        args += ["-vf", subtitle_filter(options.subtitle_file, fonts_dir=options.fonts_dir)]

    args += [
        "-c:v",
        options.video_encoder,
        "-preset",
        options.preset,
        "-crf",
        str(options.crf),
    ]
    if options.tune:
        args += ["-tune", options.tune]
    if options.maxrate_kbps:
        bufsize = options.bufsize_kbps or options.maxrate_kbps * 2
        args += ["-maxrate", f"{options.maxrate_kbps}k", "-bufsize", f"{bufsize}k"]
    args += [
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{segment})",
    ]
    if plan.audio is not None:
        args += ["-c:a", "aac", "-b:a", options.audio_bitrate, "-ac", "2"]
    else:
        args += ["-an"]

    args += [
        # No subtitle or data stream reaches the output: the subtitles are in
        # the picture now, and a text track hls.js would have to be told to
        # ignore is a bug waiting for a browser to disagree about.
        "-sn",
        "-dn",
        "-map_chapters",
        "-1",
        "-f",
        "hls",
        "-hls_time",
        str(segment),
        "-hls_playlist_type",
        "vod",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_type",
        "fmp4",
        "-hls_fmp4_init_filename",
        INIT_NAME,
        "-hls_segment_filename",
        SEGMENT_PATTERN,
        # Machine-readable progress on stdout, and nothing else on it; the
        # human-readable status line would otherwise be a single 40 KB
        # carriage-returned line on stderr.
        "-progress",
        "pipe:1",
        "-nostats",
        PLAYLIST_NAME,
    ]
    return args


def subtitle_extract_args(plan: TranscodePlan) -> list[str] | None:
    """Arguments that write the chosen subtitle track to its own file.

    Extracted first and burned in second, rather than pointing the filter at
    the source with ``si=``, for the reason in :data:`WORK_DIR`: it takes the
    release name out of the filter graph entirely. ASS is copied (styling and
    all); anything else is converted to SRT, which the ``subtitles`` filter
    reads and which ``mov_text`` and WebVTT both convert to cleanly.
    """
    if plan.subtitle is None or plan.subtitle_file is None:
        return None
    codec = "copy" if plan.subtitle.is_ass else "srt"
    return [
        "-nostdin",
        "-hide_banner",
        "-y",
        "-i",
        str(plan.source),
        "-map",
        f"0:s:{plan.subtitle.type_index}",
        "-c:s",
        codec,
        plan.subtitle_file,
    ]


def font_extract_args(plan: TranscodePlan, *, fonts_dir: str = FONTS_DIR) -> list[str] | None:
    """Arguments that dump every attachment into ``fonts_dir``.

    Each attachment is named explicitly rather than dumped with
    ``-dump_attachment:t ""``: that form writes whatever file name the
    container carries, into the working directory, which is a path traversal
    with a font on the end of it (:func:`attachment_name`).

    ``-t 0`` on a null output is what makes this exit cleanly. Attachment
    dumping is an input-side side effect, so ffmpeg with no output at all does
    the work and then exits non-zero complaining that no output was specified.
    """
    if not plan.attachments:
        return None
    args = ["-nostdin", "-hide_banner", "-y"]
    for type_index, name in plan.attachments:
        args += [f"-dump_attachment:t:{type_index}", f"{fonts_dir}/{name}"]
    args += ["-i", str(plan.source), "-t", "0", "-f", "null", "-"]
    return args


__all__ = [
    "ASS_CODECS",
    "ASS_FILE",
    "BITMAP_SUBTITLE_CODECS",
    "FONTS_DIR",
    "INIT_NAME",
    "LANGUAGE_ALIASES",
    "NOTE_BITMAP_ONLY",
    "NOTE_NO_AUDIO",
    "NOTE_NO_AUDIO_LANGUAGE",
    "NOTE_NO_SUBTITLES",
    "NOTE_NO_SUB_LANGUAGE",
    "PLAYLIST_NAME",
    "SEGMENT_GLOB",
    "SEGMENT_PATTERN",
    "SRT_FILE",
    "TEXT_SUBTITLE_CODECS",
    "WORK_DIR",
    "EncodeOptions",
    "PlanError",
    "Stream",
    "TranscodePlan",
    "attachment_name",
    "build_plan",
    "canonical_language",
    "encode_args",
    "font_extract_args",
    "language_matches",
    "normalise_language",
    "subtitle_extract_args",
    "subtitle_filter",
    "subtitle_score",
]
