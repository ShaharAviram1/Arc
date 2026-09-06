"""The library: what is on disk, and which episode it is.

* ``parser`` — release filenames to a :class:`ParsedName` (FR-L2). Pure.
* ``matcher`` — candidates, weighted scoring and a confidence (FR-L3, FR-L4).
  The scoring half is pure; only candidate gathering touches the database.
* ``ingest`` — walking the download and manual-drop directories into
  ``media_files`` rows (FR-L1).
* ``link`` — attaching a file to an episode, shared by the matcher and the
  review API so the state transition has one definition.
* ``jobs`` — the ``library_scan`` and ``match_file`` handlers.
* ``names`` — the job type strings, importable without the handlers.

Importing this package does **not** register the job handlers, for the same
reason :mod:`arc.services.catalog` does not: the API needs the services and
has no use for the queue. The worker imports ``arc.services.library.jobs``
explicitly.
"""

from __future__ import annotations

from arc.services.library.ingest import ScanResult, ingest_file, scan
from arc.services.library.link import LinkError, UnknownAnime, ensure_episode, link
from arc.services.library.matcher import Candidate, MatchResult, Scored, match, rank, score
from arc.services.library.names import LIBRARY_SCAN, MATCH_FILE, match_dedupe_key
from arc.services.library.parser import VIDEO_EXTENSIONS, Kind, ParsedName, parse, title_key

__all__ = [
    "LIBRARY_SCAN",
    "MATCH_FILE",
    "VIDEO_EXTENSIONS",
    "Candidate",
    "Kind",
    "LinkError",
    "MatchResult",
    "ParsedName",
    "ScanResult",
    "Scored",
    "UnknownAnime",
    "ensure_episode",
    "ingest_file",
    "link",
    "match",
    "match_dedupe_key",
    "parse",
    "rank",
    "scan",
    "score",
    "title_key",
]
