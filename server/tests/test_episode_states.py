"""The episode state machine (spec §6, arc/services/acquisition/states.py).

The table in :data:`TRANSITIONS` is the specification, so these tests read it
rather than restating it: *every* legal edge is exercised and *every* pair not
in the table is asserted to raise. That way adding a state to the enum without
deciding where it fits fails here instead of somewhere downstream.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Episode, EpisodeState, MediaFile, ReviewState
from arc.services.acquisition.states import (
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    advance_to_matched,
    can_transition,
    transition,
)
from arc.services.library.link import link
from tests.acquisition_helpers import make_anime


def episode(state: EpisodeState, *, id: int = 1) -> Episode:
    row = Episode(anime_id=7, number=3, state=state)
    row.id = id
    row.state_changed_at = datetime.now(UTC) - timedelta(hours=1)
    return row


LEGAL = [(source, target) for source, targets in TRANSITIONS.items() for target in targets]
ILLEGAL = [
    (source, target)
    for source, target in itertools.product(EpisodeState, EpisodeState)
    if source is not target and target not in TRANSITIONS.get(source, frozenset())
]


@pytest.mark.parametrize(("source", "target"), LEGAL, ids=lambda s: getattr(s, "value", s))
def test_every_legal_transition_moves_the_episode(
    source: EpisodeState, target: EpisodeState
) -> None:
    row = episode(source)
    before = row.state_changed_at

    assert transition(row, target, reason="because") is True

    assert row.state is target
    assert before is not None and row.state_changed_at is not None
    assert row.state_changed_at > before


@pytest.mark.parametrize(("source", "target"), ILLEGAL, ids=lambda s: getattr(s, "value", s))
def test_every_illegal_transition_raises(source: EpisodeState, target: EpisodeState) -> None:
    row = episode(source)

    with pytest.raises(IllegalTransition) as caught:
        transition(row, target)

    assert row.state is source, "a refused transition must not half-apply"
    assert source.value in str(caught.value)
    assert target.value in str(caught.value)


def test_the_table_covers_every_state() -> None:
    """A state with no entry would raise on every transition out of it."""
    assert set(TRANSITIONS) == set(EpisodeState)


def test_re_requesting_the_current_state_is_a_no_op() -> None:
    row = episode(EpisodeState.DOWNLOADING)
    before = row.state_changed_at

    assert transition(row, EpisodeState.DOWNLOADING) is False
    assert row.state_changed_at == before


def test_can_transition_agrees_with_the_table() -> None:
    assert can_transition(EpisodeState.WANTED, EpisodeState.SEARCHING)
    assert can_transition(EpisodeState.WANTED, EpisodeState.WANTED), "a no-op is always allowed"
    assert not can_transition(EpisodeState.WANTED, EpisodeState.READY)


def test_unavailable_stores_its_reason_and_clears_it_on_the_way_out() -> None:
    row = episode(EpisodeState.SEARCHING)

    transition(row, EpisodeState.UNAVAILABLE, reason="no acceptable release found")
    assert row.unavailable_reason == "no acceptable release found"

    transition(row, EpisodeState.WANTED, reason="somebody wants it again")
    assert row.unavailable_reason is None


def test_a_reason_is_not_stored_for_states_that_are_not_unavailable() -> None:
    row = episode(EpisodeState.WANTED)

    transition(row, EpisodeState.SEARCHING, reason="looking")

    assert row.unavailable_reason is None


def test_transitions_are_logged_with_both_ends(caplog: pytest.LogCaptureFixture) -> None:
    row = episode(EpisodeState.NOT_WANTED, id=44)

    with caplog.at_level("INFO", logger="arc.services.acquisition.states"):
        transition(row, EpisodeState.WANTED, reason="a user wants this episode")

    record = next(r for r in caplog.records if r.message == "episode state changed")
    assert record.episode_id == 44
    assert record.__dict__["from"] == "not_wanted"
    assert record.to == "wanted"
    assert record.reason == "a user wants this episode"


# --- advance_to_matched, the linker's entry point ---------------------------


@pytest.mark.parametrize(
    "state",
    [
        EpisodeState.NOT_WANTED,
        EpisodeState.WANTED,
        EpisodeState.SEARCHING,
        EpisodeState.DOWNLOADING,
        EpisodeState.DOWNLOADED,
        EpisodeState.MATCHING,
        EpisodeState.UNAVAILABLE,
        EpisodeState.FAILED,
    ],
    ids=lambda s: s.value,
)
def test_a_link_pulls_an_unfinished_episode_to_matched(state: EpisodeState) -> None:
    row = episode(state)

    assert advance_to_matched(row) is True
    assert row.state is EpisodeState.MATCHED


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES), ids=lambda s: s.value)
def test_a_link_never_downgrades_a_playable_episode(state: EpisodeState) -> None:
    """FR-P5: a re-matched source file must not restart a finished episode."""
    row = episode(state)

    assert advance_to_matched(row) is False
    assert row.state is state


def test_a_link_on_an_already_matched_episode_is_a_no_op() -> None:
    row = episode(EpisodeState.MATCHED)

    assert advance_to_matched(row) is False
    assert row.state is EpisodeState.MATCHED


@pytest.mark.pg
async def test_link_leaves_a_ready_episode_ready(db_session: AsyncSession) -> None:
    """The same rule, through the real :func:`arc.services.library.link.link`."""
    anime = await make_anime(db_session, anilist_id=961111)
    episode_row = Episode(
        anime_id=anime.id,
        number=4,
        state=EpisodeState.READY,
        state_changed_at=datetime.now(UTC),
    )
    db_session.add(episode_row)
    media = MediaFile(path="/tmp/arc-test-ready.mkv", parsed={}, review_state=ReviewState.PENDING)
    db_session.add(media)
    await db_session.flush()

    await link(
        db_session,
        media,
        anime_id=anime.id,
        episode_number=4,
        review_state=ReviewState.AUTO,
        confidence=0.99,
    )

    assert episode_row.state is EpisodeState.READY
    assert media.episode_id == episode_row.id
