"""The library job type strings, importable without the handlers.

The same split as :mod:`arc.services.catalog.names`, for the same reason: the
API and the ingest service need to *name* a job, the worker needs to *run* it,
and importing the handler module from a request path would drag the whole job
registry — and the catalogue client it builds — into the API.
"""

from __future__ import annotations

#: Walk the download and manual-drop directories (FR-L1). Scheduled, and run
#: once at worker start. Deduplicated on the type itself — two scans queued at
#: once would do the same work twice — by the scheduler (``arc/worker.py``)
#: and by ``POST /api/jobs`` (``TYPE_DEDUPED`` in ``arc/api/jobs.py``), so
#: pressing the button while the timer's scan is pending queues nothing.
LIBRARY_SCAN = "library_scan"

#: Match one ``media_files`` row against the catalogue (FR-L3).
MATCH_FILE = "match_file"


def match_dedupe_key(media_file_id: int) -> str:
    """Dedupe key for a ``match_file`` job.

    Per file, so a rescan that races the first scan's job does not queue a
    second match for the same row, while two different files still match
    concurrently.
    """
    return f"{MATCH_FILE}:{media_file_id}"


__all__ = ["LIBRARY_SCAN", "MATCH_FILE", "match_dedupe_key"]
