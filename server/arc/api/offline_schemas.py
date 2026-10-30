"""What a client is told about an episode's small offline copy (FR-P6).

One shape, :class:`OfflineOut`, sent in three places: as ``EpisodeOut.offline``
on every episode row (show page, home shelves, the player), and as the body of
``POST`` and ``GET /api/episodes/{id}/offline``. Its own module because both
:mod:`arc.api.anime_schemas` and :mod:`arc.api.episode_extras` need it, and the
second is imported by the first.
"""

from __future__ import annotations

from pydantic import BaseModel

from arc.api.media_stream import offline_url
from arc.models import Job, OfflineCopy
from arc.services.media.copies import OfflineState, codec_string, job_progress, offline_state


class OfflineOut(BaseModel):
    """One episode's small copy, as the device decides what to download.

    * ``state`` — ``none`` (ask for one), ``queued``/``preparing`` (the server
      is making it; poll or listen for ``offline_copy``), ``available`` (fetch
      ``url``), ``failed`` (ask again), ``unavailable`` (no copy and no source
      left: take the full-size ``download_url`` instead).
    * ``progress`` — 0..1 while ``queued`` or ``preparing``, else null.
    * ``size`` — bytes, once ``available``.
    * ``url`` — ``/media/{id}/offline.mp4``, once ``available``; built beside
      the route that answers it, so the client never assembles a media path.
    * ``codecs`` — the RFC 6381 video codec string (``avc1.640028``,
      ``hvc1.1.6.L120.B0``, … — by codec and height,
      :func:`~arc.services.media.copies.codec_string`) for ``canPlayType``:
      the copy's own once it exists, what the server would make otherwise;
      null when there is nothing to make.
    * ``sha256`` — the copy's SHA-256 (lower-case hex) once ``available``,
      when the server has one (copies made before 2026-10-30 have none): the
      device checks the bytes it downloaded against it before it trusts them.
    """

    state: OfflineState
    progress: float | None = None
    size: int | None = None
    url: str | None = None
    codecs: str | None = None
    sha256: str | None = None

    @classmethod
    def build(
        cls,
        episode_id: int,
        *,
        copy: OfflineCopy | None,
        job: Job | None,
        has_source: bool,
        codec: str | None,
        height: int | None = None,
    ) -> OfflineOut:
        """The answer for a **ready** episode (callers check that).

        ``job`` must be the episode's live (pending or running) ``offline_encode``
        or ``None`` (:func:`~arc.services.media.names.latest_offline_jobs`).
        ``codec`` and ``height`` are the host's ``OFFLINE_CODEC`` and
        ``OFFLINE_HEIGHT``, for a copy not made yet.
        """
        # ``job`` is the *live* encode, if there is one; a queued or preparing
        # row without one is a dead encode and reads ``failed``.
        state = offline_state(
            copy_state=copy.state if copy else None,
            has_source=has_source,
            job_alive=job is not None,
        )
        made = copy is not None and bool(copy.codec)
        made_with = copy.codec if copy is not None and made else codec
        made_at = copy.height if copy is not None and made else height
        if state == "available" and copy is not None:
            return cls(
                state=state,
                size=copy.size,
                url=offline_url(episode_id),
                codecs=codec_string(copy.codec, copy.height),
                sha256=copy.sha256,
            )
        if state == "unavailable":
            return cls(state=state)
        progress: float | None = None
        if state == "preparing":
            progress = job_progress(job)
        elif state == "queued":
            progress = 0.0
        return cls(state=state, progress=progress, codecs=codec_string(made_with, made_at))


__all__ = ["OfflineOut"]
