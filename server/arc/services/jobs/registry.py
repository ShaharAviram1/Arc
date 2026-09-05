"""Job type → handler registry.

A job row carries a ``type`` string and a small JSONB payload; this module is
what turns that string into something to run. Handlers are registered by
decorating an async function::

    @register("search_release")
    async def search_release(ctx: JobContext) -> None:
        ...

Every handler must be **idempotent and safe to retry** (CLAUDE.md): the same
row may be run twice after a worker crash, and the runner retries failures
with backoff.

A handler is given a :class:`JobContext`, not loose arguments, so that adding
something the handlers need later (a metrics sink, a clock) does not mean
editing every signature.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Job


class UnknownJobType(LookupError):
    """Raised when a job row names a type nothing has registered.

    This is not a transient failure: the row is failed immediately rather
    than retried, because no amount of waiting will produce a handler.
    """


@dataclass(slots=True)
class JobContext:
    """Everything a handler is given.

    ``session`` is the handler's own session. The runner commits it when the
    handler returns and rolls it back when it raises, so a handler that fails
    half-way leaves nothing behind; the job's own status update is written
    separately, in a short transaction of its own.
    """

    #: The claimed row, **detached**: the claim session is closed by the time a
    #: handler runs, and it is created with ``expire_on_commit=False``, so the
    #: attributes stay readable but belong to no session. Read them freely;
    #: never ``session.refresh()`` or ``session.merge()`` this object (it would
    #: attach the row to the handler's session and write it back on commit),
    #: and treat writes to it as local — the runner owns the job row.
    job: Job
    session: AsyncSession
    settings: Settings
    log: logging.Logger

    @property
    def payload(self) -> dict[str, Any]:
        """Shorthand for ``ctx.job.payload``."""
        return self.job.payload


type JobHandler = Callable[[JobContext], Awaitable[None]]

_HANDLERS: dict[str, JobHandler] = {}


def register(job_type: str) -> Callable[[JobHandler], JobHandler]:
    """Decorator registering ``job_type`` against the decorated coroutine.

    Registering the same type twice is a programming error (two handlers
    silently shadowing each other is the kind of bug that only shows up in
    production), so it raises rather than overwriting. Re-registering the
    *same* function is allowed: module reimport under pytest is harmless.
    """
    if not job_type:
        raise ValueError("job type must not be empty")

    def decorator(handler: JobHandler) -> JobHandler:
        existing = _HANDLERS.get(job_type)
        if existing is not None and existing is not handler:
            raise ValueError(f"job type {job_type!r} is already registered to {existing!r}")
        _HANDLERS[job_type] = handler
        return handler

    return decorator


def get_handler(job_type: str) -> JobHandler:
    """Return the handler for ``job_type`` or raise :class:`UnknownJobType`."""
    handler = _HANDLERS.get(job_type)
    if handler is None:
        known = ", ".join(sorted(_HANDLERS)) or "(none)"
        raise UnknownJobType(
            f"no handler registered for job type {job_type!r}; known types: {known}"
        )
    return handler


def registered_types() -> frozenset[str]:
    """Every registered job type. Used by the admin queue view and tests."""
    return frozenset(_HANDLERS)


__all__ = [
    "JobContext",
    "JobHandler",
    "UnknownJobType",
    "get_handler",
    "register",
    "registered_types",
]
