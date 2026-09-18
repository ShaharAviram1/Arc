"""SQLAlchemy models, one module per aggregate (architecture.md §3, §4).

Importing this package is what makes ``Base.metadata`` complete: Alembic's
``env.py`` and anything that creates tables must import ``arc.models``, never
``arc.db`` alone.
"""

from __future__ import annotations

from arc.db import Base
from arc.models.acquisition import (
    EPISODE_FILE_INDEX,
    KIND_EPISODE_PREDICATE,
    WANTED_CLAIM_INDEX,
    Torrent,
    TorrentFile,
    Want,
)
from arc.models.anime import Anime, Episode
from arc.models.enums import (
    EpisodeState,
    JobStatus,
    ListStatus,
    MalWriteCause,
    MalWriteStatus,
    ReviewState,
    TorrentKind,
    UpdatedBy,
    UserRole,
)
from arc.models.job import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_PRIORITY,
    TRANSCODE_EPISODE_INDEX,
    Job,
)
from arc.models.mal import MalLink, MalWriteLog
from arc.models.media import MediaFile, Rendition
from arc.models.offline import (
    OFFLINE_SEARCH_INDEX,
    OFFLINE_SEASON_INDEX,
    OfflineAnime,
    OfflineId,
    OfflineImport,
)
from arc.models.recs import RecRun
from arc.models.settings import DEFAULT_SETTINGS, Setting
from arc.models.tracking import IN_PROGRESS_INDEX, ListEntry, WatchProgress
from arc.models.user import Invite, Session, User

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_PRIORITY",
    "DEFAULT_SETTINGS",
    "EPISODE_FILE_INDEX",
    "IN_PROGRESS_INDEX",
    "KIND_EPISODE_PREDICATE",
    "OFFLINE_SEARCH_INDEX",
    "OFFLINE_SEASON_INDEX",
    "TRANSCODE_EPISODE_INDEX",
    "WANTED_CLAIM_INDEX",
    "Anime",
    "Base",
    "Episode",
    "EpisodeState",
    "Invite",
    "Job",
    "JobStatus",
    "ListEntry",
    "ListStatus",
    "MalLink",
    "MalWriteCause",
    "MalWriteLog",
    "MalWriteStatus",
    "MediaFile",
    "OfflineAnime",
    "OfflineId",
    "OfflineImport",
    "RecRun",
    "Rendition",
    "ReviewState",
    "Session",
    "Setting",
    "Torrent",
    "TorrentFile",
    "TorrentKind",
    "UpdatedBy",
    "User",
    "UserRole",
    "Want",
    "WatchProgress",
]
