"""SQLAlchemy models, one module per aggregate (architecture.md §3, §4).

Importing this package is what makes ``Base.metadata`` complete: Alembic's
``env.py`` and anything that creates tables must import ``arc.models``, never
``arc.db`` alone.
"""

from __future__ import annotations

from arc.db import Base
from arc.models.acquisition import Torrent, Want
from arc.models.anime import Anime, Episode
from arc.models.enums import (
    EpisodeState,
    JobStatus,
    ListStatus,
    MalWriteCause,
    MalWriteStatus,
    ReviewState,
    UpdatedBy,
    UserRole,
)
from arc.models.job import DEFAULT_MAX_ATTEMPTS, DEFAULT_PRIORITY, Job
from arc.models.mal import MalLink, MalWriteLog
from arc.models.media import MediaFile, Rendition
from arc.models.recs import RecRun
from arc.models.settings import DEFAULT_SETTINGS, Setting
from arc.models.tracking import ListEntry, WatchProgress
from arc.models.user import Invite, Session, User

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_PRIORITY",
    "DEFAULT_SETTINGS",
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
    "RecRun",
    "Rendition",
    "ReviewState",
    "Session",
    "Setting",
    "Torrent",
    "UpdatedBy",
    "User",
    "UserRole",
    "Want",
    "WatchProgress",
]
