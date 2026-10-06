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
    * ``codecs`` — the RFC 6381 video codec string (``avc1.640028`` or
      ``hvc1.1.6.L93.B0``) for ``canPlayType``: the copy's own once it exists,
      what the server would make otherwise; null when there is nothing to make.
    """

    state: OfflineState
    progress: float | None = None
    size: int | None = None
    url: str | None = None
    codecs: str | None = None

    @classmethod
    def build(
        cls,
        episode_id: int,
        *,
        copy: OfflineCopy | None,
        job: Job | None,
        has_source: bool,
        codec: str | None,
    ) -> OfflineOut:
        """The answer for a **ready** episode (callers check that).

        ``job`` must be the episode's live (pending or running) ``offline_encode``
        or ``None`` (:func:`~arc.services.media.names.latest_offline_jobs`).
        """
        # ``job`` is the *live* encode, if there is one; a queued or preparing
        # row without one is a dead encode and reads ``failed``.
        state = offline_state(
            copy_state=copy.state if copy else None,
            has_source=has_source,
            job_alive=job is not None,
        )
        made_with = copy.codec if copy is not None and copy.codec else codec
        if state == "available" and copy is not None:
            return cls(
                state=state,
                size=copy.size,
                url=offline_url(episode_id),
                codecs=codec_string(copy.codec),
            )
        if state == "unavailable":
            return cls(state=state)
        progress: float | None = None
        if state == "preparing":
            progress = job_progress(job)
        elif state == "queued":
            progress = 0.0
        return cls(state=state, progress=progress, codecs=codec_string(made_with))


__all__ = ["OfflineOut"]
