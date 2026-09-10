"""The library job type strings, importable without the handlers.

The same split as :mod:`arc.services.catalog.names`, for the same reason: the
API and the ingest service need to *name* a job, the worker needs to *run* it,
and importing the handler module from a request path would drag the whole job
registry — and the catalogue client it builds — into the API.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Job, JobStatus
from arc.services.jobs.queue import enqueue

#: Walk the download and manual-drop directories (FR-L1). Scheduled, and run
#: once at worker start. Deduplicated on the type itself — two scans queued at
#: once would do the same work twice — by the scheduler (``arc/worker.py``)
#: and by ``POST /api/jobs`` (``TYPE_DEDUPED`` in ``arc/api/jobs.py``), so
#: pressing the button while the timer's scan is pending queues nothing.
LIBRARY_SCAN = "library_scan"

#: Match one ``media_files`` row against the catalogue (FR-L3).
MATCH_FILE = "match_file"

#: Queue priority for the scan (lower runs first; the default is 100). Behind
#: the default: it walks two directory trees on a two-minute timer and finds
#: nothing almost every time, and the files it *does* find are handed on as
#: :data:`MATCH_FILE` jobs — which keep the default, because by then something
#: is on the disk and an episode is one match away from being playable.
LIBRARY_SCAN_PRIORITY = 200


#: Ask a model which show an unsure file is (FR-L5). Only ever queued for a
#: file that is already in the review queue, and only when
#: ``LLM_MATCH_SUGGESTIONS`` is on and a provider is configured.
LLM_SUGGEST_MATCH = "llm_suggest_match"

#: Queue priority for a suggestion (lower runs first; the default is 100).
#: Behind everything: a suggestion is an aid to a person who is not looking at
#: the queue yet, while every job ahead of it is somebody waiting for an
#: episode. It also costs a third-party request with a daily quota, which is
#: another reason not to let it overtake work that costs nothing.
LLM_SUGGEST_PRIORITY = 150


def match_dedupe_key(media_file_id: int) -> str:
    """Dedupe key for a ``match_file`` job.

    Per file, so a rescan that races the first scan's job does not queue a
    second match for the same row, while two different files still match
    concurrently.
    """
    return f"{MATCH_FILE}:{media_file_id}"


def suggest_dedupe_key(media_file_id: int) -> str:
    """One queued suggestion per file, however many things ask for it.

    Per file rather than per file-and-force: the point of the key is that a
    model is asked about a given file once at a time, and "ask again" while a
    request for that same file is already in flight is answered by the one in
    flight.
    """
    return f"{LLM_SUGGEST_MATCH}:{media_file_id}"


async def enqueue_suggestion(
    session: AsyncSession, media_file_id: int, *, force: bool = False
) -> Job:
    """Queue a match suggestion for one file, deduplicated on the file.

    Flushed, not committed, like every other enqueue — the caller's
    transaction is what makes the review decision and its consequence atomic.

    ``force`` asks the handler to replace a suggestion the file already has;
    without it the handler skips a file that has one, so a re-match does not
    spend a request re-answering a question that is already answered.

    **A forced ask upgrades a job that is already queued.** The dedupe key is
    the file, not the file and the flag, so an automatic ask queued by
    ``_review`` would otherwise swallow a person's "ask again": the existing
    row would be returned, run without ``force``, see the suggestion already
    on the file and skip. So a pending row has ``force`` written into its
    payload instead — the same thing ``search_release`` does when a queued
    search learns a better ``expected``. A *running* row is left alone: it has
    already read its payload, and it is producing a fresh answer anyway.

    Lives here rather than beside the handler for the same reason
    :func:`arc.services.media.names.enqueue_transcode` does: ``POST
    /api/review/{id}/suggest`` needs to *name* the job, and importing the
    handler module from a request path would drag the job registry and both
    model SDKs into the API.
    """
    payload: dict[str, Any] = {"media_file_id": media_file_id}
    if force:
        payload["force"] = True
    job = await enqueue(
        session,
        LLM_SUGGEST_MATCH,
        payload,
        priority=LLM_SUGGEST_PRIORITY,
        dedupe_key=suggest_dedupe_key(media_file_id),
    )
    if force and job.status is JobStatus.PENDING and not job.payload.get("force"):
        # Replaced rather than mutated in place: ``payload`` is JSONB, and
        # SQLAlchemy only notices a new object.
        job.payload = {**job.payload, "force": True}
    return job


__all__ = [
    "LIBRARY_SCAN",
    "LIBRARY_SCAN_PRIORITY",
    "LLM_SUGGEST_MATCH",
    "LLM_SUGGEST_PRIORITY",
    "MATCH_FILE",
    "enqueue_suggestion",
    "match_dedupe_key",
    "suggest_dedupe_key",
]
