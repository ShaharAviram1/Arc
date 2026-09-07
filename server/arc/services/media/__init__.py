"""Media inspection and preparation.

* ``probe`` — ffprobe over a source file, optional by design (architecture.md
  §5.2).
* ``plan`` — pure track selection and ffmpeg argument building (FR-P2).
* ``transcode`` — running ffmpeg: fonts, subtitles, HLS, progress (FR-P1).
* ``names`` — the ``transcode`` job type, its dedupe key and its priority,
  importable without pulling the handler in (FR-P3).
* ``jobs`` — the handler itself and the startup sweep. Importing it registers
  the handler, so only the worker does.
"""

from __future__ import annotations

from arc.services.media.names import (
    TRANSCODE,
    enqueue_transcode,
    latest_transcode_jobs,
    transcode_dedupe_key,
    transcode_priority,
)
from arc.services.media.plan import (
    EncodeOptions,
    PlanError,
    TranscodePlan,
    build_plan,
    encode_args,
)
from arc.services.media.probe import ffprobe_json, ffprobe_path, probe_summary, summarise
from arc.services.media.transcode import TranscodeError, TranscodeResult, transcode

__all__ = [
    "TRANSCODE",
    "EncodeOptions",
    "PlanError",
    "TranscodeError",
    "TranscodePlan",
    "TranscodeResult",
    "build_plan",
    "encode_args",
    "enqueue_transcode",
    "ffprobe_json",
    "ffprobe_path",
    "latest_transcode_jobs",
    "probe_summary",
    "summarise",
    "transcode",
    "transcode_dedupe_key",
    "transcode_priority",
]
