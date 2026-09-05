"""Handlers that belong to the queue itself rather than to a feature.

The real job types listed in architecture.md §5 (``compute_wants``,
``search_release``, ``poll_qbit``, ``ingest_file``, ``match_file``,
``llm_suggest_match``, ``transcode``, ``mal_push``, ``mal_import``,
``retention_sweep``) register themselves from their own service packages as
those milestones land. What lives here is only what the queue needs to prove
itself end to end.
"""

from __future__ import annotations

from arc.services.jobs.registry import JobContext, register


@register("noop")
async def noop(ctx: JobContext) -> None:
    """Do nothing, visibly.

    The M1 definition of done: enqueue one of these from the API and watch
    the worker run it.
    """
    ctx.log.info(
        "noop job",
        extra={"job_id": ctx.job.id, "payload": ctx.payload},
    )


@register("fail_once")
async def fail_once(ctx: JobContext) -> None:
    """Fail on the first attempt, succeed on any later one.

    Exists so that retry-with-backoff can be exercised for real — in tests
    and by hand against a running worker — without a feature to break.
    ``attempts`` is incremented by the claim, so it is 1 during the first run.
    """
    if ctx.job.attempts < 2:
        raise RuntimeError(f"fail_once: deliberate failure on attempt {ctx.job.attempts}")
    ctx.log.info(
        "fail_once job succeeded on retry",
        extra={"job_id": ctx.job.id, "attempts": ctx.job.attempts},
    )


__all__ = ["fail_once", "noop"]
