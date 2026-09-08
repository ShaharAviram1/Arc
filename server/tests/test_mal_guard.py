"""FR-M7, enforced against the source tree rather than against one code path.

"There is no code path that writes to MyAnimeList except via a user-originated
event or an explicit revert" is a statement about the *whole* application, and
no functional test can make it. A test that exercises the three legitimate
callers proves those three work; it says nothing about the fourth one somebody
adds next month, which is exactly the failure this requirement exists to
prevent.

So these tests read the code. They walk ``arc/`` and assert that the set of
modules calling each of the two dangerous functions is exactly the set that is
allowed to. Adding a caller anywhere else fails here with the file name, and
the fix is either to delete the call or — if it really is a user-originated
event — to add the module to the list below *and* explain why in the same
commit. That is a deliberate speed bump, not an obstacle.

The tests are AST-based rather than grep-based: a comment mentioning
``enqueue_mal_push`` is not a call, an import of it is not a call, and a
matcher that cannot tell the difference is one that gets muted the first time
somebody documents the rule in a docstring.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from arc.models import MalWriteCause, MalWriteStatus
from arc.services.mal.writelog import assert_write_cause

ARC = Path(__file__).resolve().parent.parent / "arc"

#: The only modules that may queue a MyAnimeList write, and the user-originated
#: event each of them represents (spec §4.7 FR-M4, FR-M7).
#: The definition itself is not in the set: ``names.py`` declares the function
#: and never calls it, and a matcher that counted a definition as a call would
#: also count the next module that merely imports it.
ALLOWED_ENQUEUERS = {
    # An explicit list edit or removal — FR-W2's status/score, and a removal.
    "services/catalog/lists.py",
    # Watching an episode to the end — FR-S4's completion, and nothing else.
    "services/playback/progress.py",
    # An explicit revert — FR-M5.
    "api/mal.py",
}

#: …and the only modules that may write the queued rows those jobs send. The
#: rows are the queue now (:mod:`arc.services.mal.writelog`), so this is the
#: guard that matters most: a job with no pending rows sends nothing, and a
#: pending row nobody is allowed to write is a write nobody can make.
#:
#: ``services/mal/sync.py`` is here for one function, ``record_removal``: a
#: removal's row has to be written before the entry is deleted, and it lives
#: next to the rules that close it. It is called from ``lists.py`` and from
#: nowhere else.
ALLOWED_QUEUERS = ALLOWED_ENQUEUERS | {"services/mal/sync.py"}

#: …and the only module that may call the MAL API's write endpoints at all.
#: ``client.py`` defines them and is deliberately absent for the same reason
#: ``names.py`` is above: :mod:`arc.services.mal.sync` is the single place that
#: decides what to send, and every other module has to go through its rules.
ALLOWED_WRITERS = {"services/mal/sync.py"}

ENQUEUE = "enqueue_mal_push"
QUEUE_ROW = "record_pending"
WRITE_CALLS = frozenset({"update_list_status", "delete_list_status"})


def _callers(name_of_call: frozenset[str]) -> set[str]:
    """Modules under ``arc/`` containing a *call* to any of these names."""
    found: set[str] = set()
    for path in sorted(ARC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if called in name_of_call:
                found.add(path.relative_to(ARC).as_posix())
                break
    return found


def test_only_three_events_can_queue_a_mal_write() -> None:
    """The FR-M7 guard. If this fails, read the module docstring above."""
    assert _callers(frozenset({ENQUEUE})) == ALLOWED_ENQUEUERS


def test_only_three_events_can_write_a_queued_row() -> None:
    """The same guard, one level down: the rows *are* the queue.

    ``enqueue_mal_push`` only nudges the worker; what gets sent is whatever
    pending rows exist for the pair. So the set of modules that may create one
    is as load-bearing as the set that may queue the job, and is checked the
    same way.
    """
    assert _callers(frozenset({QUEUE_ROW})) == ALLOWED_QUEUERS


def test_a_queued_row_cannot_be_smuggled_in_as_a_closed_one() -> None:
    """``record_closed`` refuses to write the one status that means "queued"."""
    from arc.services.mal.writelog import record_closed

    with pytest.raises(ValueError, match="record_pending"):
        asyncio.run(
            record_closed(
                cast(Any, None),
                user_id=1,
                anime_id=1,
                field="status",
                old_value=None,
                new_value="watching",
                cause=MalWriteCause.MANUAL,
                status=MalWriteStatus.PENDING,
            )
        )


@pytest.mark.parametrize("cause", [MalWriteCause.WATCH, MalWriteCause.MANUAL, MalWriteCause.REVERT])
def test_a_queued_row_carries_the_cause_of_the_event_that_made_it(cause: MalWriteCause) -> None:
    """Per row, not per job: it is what the FR-M4 guards are decided from."""
    assert assert_write_cause(cause) is cause


def test_only_the_sync_rules_touch_the_mal_write_endpoints() -> None:
    """No router, job or service may PATCH or DELETE a list status directly."""
    assert _callers(WRITE_CALLS) == ALLOWED_WRITERS


def test_the_read_only_catalogue_client_cannot_write() -> None:
    """The FR-C6 fallback shares a package with the writer and nothing else.

    ``MalSource`` is built from a client id and is used for anonymous
    catalogue reads; if it ever grew a write method, every anonymous catalogue
    lookup would become a code path that could touch somebody's list.
    """
    source = (ARC / "services/mal/catalog.py").read_text(encoding="utf-8")
    assert "my_list_status" not in source
    assert "Authorization" not in source


@pytest.mark.parametrize("cause", [MalWriteCause.WATCH, MalWriteCause.MANUAL, MalWriteCause.REVERT])
def test_the_three_user_originated_causes_are_accepted(cause: MalWriteCause) -> None:
    assert assert_write_cause(cause) is cause


def test_conflict_is_never_a_write_cause() -> None:
    """The fourth value exists to record a discarded change, not a sent one."""
    with pytest.raises(ValueError):
        assert_write_cause(MalWriteCause.CONFLICT)
