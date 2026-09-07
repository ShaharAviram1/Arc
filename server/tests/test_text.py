"""Shortening a failure without losing either end (:mod:`arc.core.text`).

Pure, so these are plain synchronous tests. What they pin down is the one
property both callers depend on: whatever the limit, the result still starts
with the sentence that names the failure.
"""

from __future__ import annotations

import pytest

from arc.core.text import ELISION, keep_head_and_tail, trim_middle

HEAD = "ffmpeg exited 1"
LAST = "Error opening output file index.m3u8."


def a_tail(lines: int = 200) -> str:
    return "\n".join(f"[libx264 @ 0x1] noise line {index}" for index in range(lines)) + f"\n{LAST}"


def test_something_short_is_left_exactly_as_it_is() -> None:
    assert keep_head_and_tail(HEAD, "one line", limit=500) == f"{HEAD}\none line"
    assert keep_head_and_tail(HEAD, "", limit=500) == HEAD
    # No elision when nothing was elided: an ellipsis on a two-line failure
    # would say something was dropped when nothing was.
    assert ELISION not in keep_head_and_tail(HEAD, "one line", limit=500)


def test_the_head_survives_and_the_middle_goes() -> None:
    trimmed = keep_head_and_tail(HEAD, a_tail(), limit=500)

    assert len(trimmed) == 500
    assert trimmed.startswith(HEAD)
    assert trimmed.endswith(LAST)
    assert "noise line 0" not in trimmed
    assert ELISION in trimmed


@pytest.mark.parametrize("limit", [1, 8, 16, 20, 60, 200, 4000])
def test_the_limit_is_never_exceeded_and_the_head_leads(limit: int) -> None:
    trimmed = keep_head_and_tail(HEAD, a_tail(), limit=limit)

    assert len(trimmed) <= limit
    assert HEAD.startswith(trimmed[: min(limit, len(HEAD))])


def test_a_head_too_long_for_the_limit_keeps_the_head_and_drops_the_tail() -> None:
    """At that point there is no room for two things, and the sentence wins."""
    long_head = "x" * 40

    assert keep_head_and_tail(long_head, a_tail(), limit=10) == "x" * 10


def test_trim_middle_splits_a_joined_string_on_its_first_line() -> None:
    joined = f"{HEAD}\n{a_tail()}"

    trimmed = trim_middle(joined, limit=500)

    assert trimmed.startswith(HEAD)
    assert trimmed.endswith(LAST)
    assert len(trimmed) == 500


def test_trimming_an_already_trimmed_string_is_safe() -> None:
    """The stored payload has been through this once; the API does it again."""
    once = keep_head_and_tail(HEAD, a_tail(), limit=4000)

    twice = trim_middle(once, limit=500)

    assert twice.startswith(HEAD)
    assert twice.endswith(LAST)
    assert len(twice) <= 500
