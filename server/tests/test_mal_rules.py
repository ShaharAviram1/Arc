"""The sync rules, with no database and no socket (spec §4.7, §7).

These are the tests spec §7 asks for by name — "MAL sync rules (esp. 'never
write what I didn't change')" — and they are deliberately the cheapest tests in
the suite: :func:`~arc.services.mal.sync.decide_import` and
:func:`~arc.services.mal.sync.decide_push` are pure functions over two small
dataclasses, so every rule can be stated as a table row rather than as a
fixture, a job run and an assertion three layers away from the decision.

The OAuth state and the token plumbing are here too, for the same reason: they
are pure, and the security properties (a state that cannot be forged, cannot
be replayed after ten minutes, and names the user it was issued to) are worth
asserting directly rather than through a redirect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from arc.config import Settings
from arc.core.crypto import InvalidToken, decrypt, encrypt
from arc.models import ListStatus, MalWriteCause, MalWriteLog
from arc.services.mal import oauth, sync
from arc.services.mal.client import MAL_TO_STATUS, STATUS_TO_MAL, MalStatus
from arc.services.mal.sync import (
    ABSENT,
    FieldPlan,
    LocalEntry,
    QueuedWrite,
    decide_import,
    decide_push,
)
from arc.services.mal.writelog import FIELD_PROGRESS, FIELD_SCORE, FIELD_STATUS
from tests.conftest import TEST_FERNET_KEY
from tests.mal_api_mock import CLIENT_ID, OAUTH_URL, REDIRECT_URI

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=1)
LATER = NOW + timedelta(days=1)


#: The OAuth tests use the real clock rather than :data:`NOW`. A state's
#: expiry is the timestamp Fernet stamps into the token, so backdating one to
#: a fixed date would make every round-trip test assert "this expired" instead
#: of what it means to.
def issued_now() -> datetime:
    return datetime.now(UTC)


def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        env="test",
        fernet_key=TEST_FERNET_KEY,
        mal_client_id=CLIENT_ID,
        mal_oauth_url=OAUTH_URL,
        mal_redirect_uri=REDIRECT_URI,
        _env_file=None,
    )


def local(
    *,
    status: ListStatus = ListStatus.WATCHING,
    progress: int = 0,
    score: int | None = None,
    updated_at: datetime = NOW,
    dirty: bool = False,
) -> LocalEntry:
    return LocalEntry(
        status=status, progress=progress, score=score, updated_at=updated_at, mal_dirty=dirty
    )


def remote(
    *,
    status: ListStatus | None = ListStatus.WATCHING,
    progress: int = 0,
    score: int | None = None,
    updated_at: datetime | None = NOW,
) -> MalStatus:
    return MalStatus(status=status, score=score, progress=progress, updated_at=updated_at)


# --- Vocabulary -------------------------------------------------------------


def test_the_status_maps_are_inverses_and_cover_every_arc_status() -> None:
    """A status Arc can hold but cannot spell to MAL would be an unpushable row."""
    assert set(STATUS_TO_MAL) == set(ListStatus)
    assert {STATUS_TO_MAL[value]: value for value in ListStatus} == dict(MAL_TO_STATUS)
    assert STATUS_TO_MAL[ListStatus.PLANNED] == "plan_to_watch"


def test_a_zero_score_is_not_a_score() -> None:
    """MAL's 0 is "unrated"; storing it as a rating would invent one (FR-M2)."""
    parsed = MalStatus.from_payload(
        {"status": "watching", "score": 0, "num_episodes_watched": 3, "updated_at": None}
    )
    assert parsed is not None
    assert parsed.score is None
    assert parsed.progress == 3


def test_a_missing_list_status_is_no_entry_at_all() -> None:
    assert MalStatus.from_payload(None) is None
    assert MalStatus.from_payload({}) is None


# --- Import (FR-M2, FR-M3) --------------------------------------------------


def test_import_creates_when_arc_has_no_row() -> None:
    assert decide_import(None, remote()) == "create"


def test_import_overwrites_a_row_arc_has_not_touched() -> None:
    """MAL is authoritative for anything Arc did not change (FR-M2)."""
    assert decide_import(local(dirty=False, updated_at=LATER), remote(updated_at=EARLIER)) == (
        "overwrite"
    )


def test_import_keeps_a_dirty_row_arc_changed_more_recently() -> None:
    assert decide_import(local(dirty=True, updated_at=LATER), remote(updated_at=EARLIER)) == "keep"


def test_import_lets_mal_win_a_conflict_it_is_newer_in() -> None:
    assert decide_import(local(dirty=True, updated_at=EARLIER), remote(updated_at=LATER)) == (
        "conflict"
    )


def test_a_mal_entry_with_no_timestamp_never_wins() -> None:
    """An unknown timestamp must not beat a change the user definitely made."""
    assert decide_import(local(dirty=True, updated_at=EARLIER), remote(updated_at=None)) == "keep"


# --- Push (FR-M4, FR-M7) ----------------------------------------------------
#
# The push rules now take *queued rows* — one per field, each with the cause of
# the event that produced it — rather than a whole local entry and one cause
# for all three fields. That is the fix for the defect these tests could not
# see: a single job carries whatever the user did between two pushes, and the
# guards below have to be decided per field or they are decided wrongly.


def queued(field: str, value: Any, cause: MalWriteCause = MalWriteCause.MANUAL) -> QueuedWrite:
    return QueuedWrite(field=field, value=value, cause=cause)


def plans(result: list[FieldPlan]) -> list[tuple[str, Any, Any, str | None]]:
    return [(plan.field, plan.old, plan.new, plan.skipped) for plan in result]


def sent(result: list[FieldPlan]) -> list[tuple[str, Any, Any]]:
    return [(plan.field, plan.old, plan.new) for plan in result if plan.sendable]


def test_push_sends_a_queued_field_with_the_value_mal_currently_holds() -> None:
    result = decide_push(
        [queued(FIELD_STATUS, "completed")],
        remote(status=ListStatus.WATCHING, progress=12, score=8),
    )
    assert sent(result) == [(FIELD_STATUS, "watching", "completed")]


def test_a_queued_field_mal_already_agrees_with_is_not_sent() -> None:
    """A write to the value that is already there is a write nobody needs."""
    result = decide_push(
        [queued(FIELD_PROGRESS, 4), queued(FIELD_SCORE, 7)],
        remote(status=ListStatus.WATCHING, progress=4, score=7),
    )
    assert sent(result) == []
    assert {plan.skipped for plan in result} == {sync.SKIP_ALREADY}


def test_a_watch_event_raises_progress() -> None:
    result = decide_push([queued(FIELD_PROGRESS, 5, MalWriteCause.WATCH)], remote(progress=4))
    assert sent(result) == [(FIELD_PROGRESS, 4, 5)]


def test_a_watch_event_never_lowers_progress() -> None:
    """FR-M4, stated as a rule rather than as a hope — and logged, not dropped."""
    result = decide_push([queued(FIELD_PROGRESS, 2, MalWriteCause.WATCH)], remote(progress=9))
    assert sent(result) == []
    assert plans(result) == [(FIELD_PROGRESS, 9, 2, sync.SKIP_LOWERS_PROGRESS)]


@pytest.mark.parametrize("cause", [MalWriteCause.MANUAL, MalWriteCause.REVERT])
def test_an_explicit_edit_may_lower_progress(cause: MalWriteCause) -> None:
    """A user typing a smaller number is a statement; a rewatch is not."""
    result = decide_push([queued(FIELD_PROGRESS, 2, cause)], remote(progress=9))
    assert sent(result) == [(FIELD_PROGRESS, 9, 2)]


def test_a_watch_event_never_clears_a_score() -> None:
    """ "Never scored" and "score cleared" are the same null; automatic wins go to MAL."""
    result = decide_push([queued(FIELD_SCORE, None, MalWriteCause.WATCH)], remote(score=8))
    assert sent(result) == []
    assert plans(result) == [(FIELD_SCORE, 8, None, sync.SKIP_CLEARS_SCORE)]


def test_an_explicit_edit_clears_a_score() -> None:
    result = decide_push([queued(FIELD_SCORE, None)], remote(score=8))
    assert sent(result) == [(FIELD_SCORE, 8, None)]


def test_the_guards_are_decided_per_field_from_that_fields_own_cause() -> None:
    """The defect, as a rule: one push, two events, two different verdicts.

    A manual score edit and a watch advance land in one queued job (the queue
    deduplicates on the pair). The score is the user's word and goes; the
    progress is automatic and is below MyAnimeList's, so it may not.
    """
    result = decide_push(
        [
            queued(FIELD_SCORE, 9, MalWriteCause.MANUAL),
            queued(FIELD_PROGRESS, 3, MalWriteCause.WATCH),
        ],
        remote(progress=7, score=None),
    )
    assert sent(result) == [(FIELD_SCORE, None, 9)]
    assert plans(result)[1] == (FIELD_PROGRESS, 7, 3, sync.SKIP_LOWERS_PROGRESS)


def test_a_manual_score_clear_survives_a_watch_event_in_the_same_job() -> None:
    """The mirror of the above: a `watch` job must not discard a manual clear."""
    result = decide_push(
        [
            queued(FIELD_SCORE, None, MalWriteCause.MANUAL),
            queued(FIELD_PROGRESS, 9, MalWriteCause.WATCH),
        ],
        remote(progress=4, score=8),
    )
    assert sent(result) == [(FIELD_SCORE, 8, None), (FIELD_PROGRESS, 4, 9)]


def test_pushing_to_a_show_not_on_the_mal_list_writes_every_queued_field() -> None:
    result = decide_push(
        [queued(FIELD_STATUS, "planned"), queued(FIELD_SCORE, 6)],
        ABSENT,
    )
    assert sent(result) == [(FIELD_STATUS, None, "planned"), (FIELD_SCORE, None, 6)]


def test_the_plans_come_back_in_log_order() -> None:
    """Status, score, progress — however the rows were queued."""
    result = decide_push(
        [queued(FIELD_PROGRESS, 3), queued(FIELD_STATUS, "dropped"), queued(FIELD_SCORE, 2)],
        ABSENT,
    )
    assert [plan.field for plan in result] == [FIELD_STATUS, FIELD_SCORE, FIELD_PROGRESS]


def test_a_conflict_can_never_be_a_write_cause() -> None:
    """The enum's fourth value records a discarded change, never a sent one."""
    with pytest.raises(ValueError, match="conflict"):
        decide_push([queued(FIELD_STATUS, "watching", MalWriteCause.CONFLICT)], ABSENT)


# --- Coalescing -------------------------------------------------------------


def test_coalescing_keeps_the_last_value_per_field_and_names_the_rest() -> None:
    """Two edits to one field before a push are one write, not two."""
    rows = [
        MalWriteLog(id=1, field=FIELD_PROGRESS, new_value=3, cause=MalWriteCause.MANUAL),
        MalWriteLog(id=2, field=FIELD_SCORE, new_value=8, cause=MalWriteCause.MANUAL),
        MalWriteLog(id=3, field=FIELD_PROGRESS, new_value=5, cause=MalWriteCause.MANUAL),
    ]
    latest, superseded = sync.coalesce(rows)

    assert {field: row.new_value for field, row in latest.items()} == {
        FIELD_PROGRESS: 5,
        FIELD_SCORE: 8,
    }
    assert [row.id for row in superseded] == [1]


def test_coalescing_an_empty_queue_is_empty() -> None:
    assert sync.coalesce([]) == ({}, [])


# --- OAuth (FR-M1) ----------------------------------------------------------


def test_the_authorize_url_carries_a_plain_challenge_and_the_redirect() -> None:
    """MAL supports only ``plain``; sending anything else is a broken handshake."""
    config = settings()
    verifier = oauth.new_verifier()
    state = oauth.encode_state(config, user_id=7, verifier=verifier, now=issued_now())
    url = oauth.authorize_url(config, state=state, verifier=verifier)

    parts = urlsplit(url)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{OAUTH_URL}/authorize"
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["code_challenge_method"] == "plain"
    assert query["code_challenge"] == verifier
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["state"] == state


def test_the_verifier_is_the_length_and_alphabet_mal_accepts() -> None:
    verifier = oauth.new_verifier()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def test_the_state_round_trips_the_user_and_the_verifier() -> None:
    config = settings()
    verifier = oauth.new_verifier()
    state = oauth.encode_state(config, user_id=11, verifier=verifier, now=issued_now())
    decoded = oauth.decode_state(config, state)
    assert (decoded.user_id, decoded.verifier) == (11, verifier)


def test_two_states_for_one_user_in_one_moment_differ() -> None:
    """The nonce: otherwise a state would be a replayable constant."""
    config = settings()
    first = oauth.encode_state(config, user_id=1, verifier="a" * 43, now=issued_now())
    second = oauth.encode_state(config, user_id=1, verifier="a" * 43, now=issued_now())
    assert first != second


def test_a_tampered_state_is_refused() -> None:
    config = settings()
    state = oauth.encode_state(config, user_id=1, verifier="a" * 43, now=issued_now())
    with pytest.raises(oauth.InvalidState):
        oauth.decode_state(config, state[:-4] + "AAAA")


def test_a_state_from_another_deployment_is_refused() -> None:
    """A different FERNET_KEY means a state Arc did not issue."""
    other = Settings(  # type: ignore[call-arg]
        env="test",
        fernet_key="Zt8kK1kQfQ2s5w8n2Zx0aB6cD9eF1gH3iJ5kL7mN9o0=",
        mal_client_id=CLIENT_ID,
        _env_file=None,
    )
    state = oauth.encode_state(settings(), user_id=1, verifier="a" * 43, now=issued_now())
    with pytest.raises(oauth.InvalidState):
        oauth.decode_state(other, state)


def test_the_token_response_becomes_an_expiry_instant() -> None:
    tokens = oauth.MalTokens.from_payload(
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600}, now=NOW
    )
    assert tokens.expires_at == NOW + timedelta(seconds=3600)


def test_a_token_response_missing_a_token_is_an_error() -> None:
    with pytest.raises(oauth.MalOAuthError):
        oauth.MalTokens.from_payload({"access_token": "a"}, now=NOW)


# --- Encryption at rest (spec §7) -------------------------------------------


def test_encryption_round_trips_and_hides_the_plaintext() -> None:
    config = settings()
    sealed = encrypt(config, "a-secret-token")
    assert "a-secret-token" not in sealed
    assert decrypt(config, sealed) == "a-secret-token"


def test_an_expired_state_is_refused_by_its_own_timestamp() -> None:
    """The ttl is Fernet's, which is what lets the OAuth flow keep no state."""
    config = settings()
    stale = issued_now() - timedelta(seconds=oauth.STATE_TTL_SECONDS + 60)
    state = oauth.encode_state(config, user_id=1, verifier="a" * 43, now=stale)
    with pytest.raises(oauth.InvalidState):
        oauth.decode_state(config, state)


def test_a_forged_ciphertext_is_refused() -> None:
    config = settings()
    with pytest.raises(InvalidToken):
        decrypt(config, encrypt(config, "x")[:-6] + "AAAAAA")
