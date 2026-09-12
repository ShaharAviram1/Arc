"""The offline catalogue: two public datasets, imported weekly (M15.5).

AniList suspended its third-party API for three days in September 2026 and
search, matching and the season page all went with it. This package is the
answer to that: a copy of manami's ``anime-offline-database`` (every anime
there is, with every alternative title) and Fribb's ``anime-lists`` (the map
between AniList, MAL, Kitsu, AniDB, TMDB, TVDB and IMDb ids), downloaded once a
week into two tables that cannot be unreachable (FR-C6, architecture.md §5.0a).

* ``names`` — the job type, importable without registering a handler
* ``download`` — streaming both files to disk, hashing as they go
* ``parse`` — pure functions from text to rows; where the leniency lives
* ``importer`` — replace-on-import in one transaction, skipped on an unchanged
  checksum
* ``jobs`` — the weekly ``import_offline_catalogue`` handler

Importing this package registers nothing, for the same reason
:mod:`arc.services.catalog` does not: the API reads these tables and has no use
for the handler. The worker imports ``jobs`` explicitly.

This milestone's first part is the import alone. Search, filename matching,
season seeding and the TMDB enrichment that the id map exists for are separate
tasks, and nothing outside this package reads either table yet.
"""

from __future__ import annotations

from arc.services.catalog.offline.download import Downloaded, download, header_version, open_lines
from arc.services.catalog.offline.importer import (
    ImportResult,
    current_import,
    import_fribb,
    import_manami,
)
from arc.services.catalog.offline.names import FRIBB, IMPORT_OFFLINE, MANAMI, OFFLINE_PRIORITY
from arc.services.catalog.offline.parse import (
    ManamiHeader,
    OfflineAnimeRow,
    OfflineIdRow,
    build_search_text,
    ids_from_sources,
    parse_fribb,
    parse_manami,
)

__all__ = [
    "FRIBB",
    "IMPORT_OFFLINE",
    "MANAMI",
    "OFFLINE_PRIORITY",
    "Downloaded",
    "ImportResult",
    "ManamiHeader",
    "OfflineAnimeRow",
    "OfflineIdRow",
    "build_search_text",
    "current_import",
    "download",
    "header_version",
    "ids_from_sources",
    "import_fribb",
    "import_manami",
    "open_lines",
    "parse_fribb",
    "parse_manami",
]
