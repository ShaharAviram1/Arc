"""Validating the admin-editable rules (FR-D2, FR-T5).

:func:`arc.services.settings.validate` is a pure function over two mappings
precisely so that the whole matrix can be asserted here without a database or
an HTTP client; ``tests/test_settings_api.py`` covers the wiring on top of it.

The bar these tests hold the writer to is higher than the readers': the rule
readers accept a bad row and fall back with a warning, because they must never
stop acquisition or turn a grace period into zero. The *write* path is the one
place a mistake can still be reported to the person making it.

The per-show override validator (:func:`~arc.services.settings.validate_override`,
M16) is here for the same reason and held to the same bar: it is the same two
validators the global keys use, so the file that pins the global matrix is the
file that pins the difference — which is only ever *what counts as naming a
field*.

:func:`arc.services.acquisition.rules.storage_hold` is here for the same
reason — it is the other pure function over these values (FR-T6), so its whole
matrix is a table rather than a filesystem.
"""

from __future__ import annotations

from typing import Any

import pytest

from arc.models import DEFAULT_SETTINGS
from arc.services.acquisition.rules import (
    BYTES_PER_GB as GB,
)
from arc.services.acquisition.rules import (
    MAX_LOOK_AHEAD,
    MAX_MIN_FREE_GB,
    MAX_SLOT_CAP,
    storage_hold,
)
from arc.services.settings import (
    FALLBACK_EQUALS_PREFERRED,
    MAX_DAYS,
    MAX_GROUP_LENGTH,
    MAX_GROUPS,
    UNKNOWN_KEY,
    SettingsInvalid,
    validate,
    validate_override,
)


def refused(patch: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, str]:
    """``validate`` must have refused ``patch``; returns the field errors."""
    with pytest.raises(SettingsInvalid) as caught:
        validate(patch, current)
    return caught.value.errors


# --- The shape of the answer ------------------------------------------------


def test_only_the_keys_given_come_back() -> None:
    assert validate({"look_ahead_n": 4}) == {"look_ahead_n": 4}


def test_an_empty_patch_is_valid_and_writes_nothing() -> None:
    assert validate({}) == {}


def test_every_default_key_is_editable() -> None:
    """The DoD: "every admin-configurable value is editable without env or DB"."""
    assert validate(dict(DEFAULT_SETTINGS) | {"fallback_resolution": "720p"}).keys() == (
        DEFAULT_SETTINGS.keys()
    )


def test_an_unknown_key_is_refused_rather_than_stored() -> None:
    assert refused({"look_ahead_n": 2, "max_transcodes": 8}) == {"max_transcodes": UNKNOWN_KEY}


def test_every_fault_is_reported_at_once() -> None:
    """One round trip, one list of what is wrong: a form is filled in at once."""
    errors = refused({"look_ahead_n": 99, "sub_lang": "!!", "nope": 1})
    assert sorted(errors) == ["look_ahead_n", "nope", "sub_lang"]


# --- preferred_groups -------------------------------------------------------


def test_groups_are_trimmed_and_kept_in_order() -> None:
    assert validate({"preferred_groups": ["  SubsPlease ", "Erai-raws"]}) == {
        "preferred_groups": ["SubsPlease", "Erai-raws"]
    }


def test_groups_are_de_duplicated_case_insensitively_first_spelling_wins() -> None:
    """``group_rank`` folds case, so both spellings are one group."""
    assert validate({"preferred_groups": ["SubsPlease", "subsplease", "ASW"]}) == {
        "preferred_groups": ["SubsPlease", "ASW"]
    }


def test_an_empty_list_is_a_valid_preference() -> None:
    """An empty list means "no preference" and has to stay expressible (FR-A3)."""
    assert validate({"preferred_groups": []}) == {"preferred_groups": []}


@pytest.mark.parametrize("value", ["SubsPlease", {"a": 1}, [1, 2], ["ok", None], None])
def test_groups_must_be_a_list_of_strings(value: Any) -> None:
    assert "preferred_groups" in refused({"preferred_groups": value})


def test_an_empty_group_is_refused_rather_than_silently_dropped() -> None:
    assert "empty" in refused({"preferred_groups": ["SubsPlease", "   "]})["preferred_groups"]


def test_an_over_long_group_is_refused() -> None:
    assert "preferred_groups" in refused({"preferred_groups": ["x" * (MAX_GROUP_LENGTH + 1)]})
    assert validate({"preferred_groups": ["x" * MAX_GROUP_LENGTH]})


def test_too_many_groups_are_refused() -> None:
    assert validate({"preferred_groups": [f"g{n}" for n in range(MAX_GROUPS)]})
    assert "preferred_groups" in refused(
        {"preferred_groups": [f"g{n}" for n in range(MAX_GROUPS + 1)]}
    )


# --- The two resolutions ----------------------------------------------------


@pytest.mark.parametrize("value", ["2160p", "1080p", "720p", "480p"])
def test_the_known_resolutions_are_accepted(value: str) -> None:
    assert validate({"preferred_resolution": value}, {"fallback_resolution": "unset"}) == {
        "preferred_resolution": value
    }


@pytest.mark.parametrize("value", ["1080", "1080P", "FHD", "4k", 1080, None])
def test_an_unknown_resolution_is_refused(value: Any) -> None:
    """The parser reports ``1080p``; anything else would rank nothing."""
    assert "preferred_resolution" in refused({"preferred_resolution": value})


def test_the_fallback_may_not_equal_the_preferred() -> None:
    errors = refused({"preferred_resolution": "1080p", "fallback_resolution": "1080p"})
    assert errors == {
        "preferred_resolution": FALLBACK_EQUALS_PREFERRED,
        "fallback_resolution": FALLBACK_EQUALS_PREFERRED,
    }


def test_a_partial_patch_is_checked_against_what_is_already_stored() -> None:
    """Only the fallback is sent, and it collides with the stored preferred."""
    current = {"preferred_resolution": "1080p", "fallback_resolution": "720p"}
    assert refused({"fallback_resolution": "1080p"}, current) == {
        "fallback_resolution": FALLBACK_EQUALS_PREFERRED
    }


def test_swapping_both_in_one_patch_is_allowed() -> None:
    current = {"preferred_resolution": "1080p", "fallback_resolution": "720p"}
    assert validate({"preferred_resolution": "720p", "fallback_resolution": "1080p"}, current) == {
        "preferred_resolution": "720p",
        "fallback_resolution": "1080p",
    }


def test_a_stored_collision_does_not_block_an_unrelated_edit() -> None:
    """A hand-edited pair must not fail every later edit under two field names
    the admin never sent, with no way out except changing something else."""
    current = {"preferred_resolution": "1080p", "fallback_resolution": "1080p"}

    assert validate({"look_ahead_n": 3}, current) == {"look_ahead_n": 3}


def test_a_stored_collision_is_still_enforced_when_a_resolution_is_edited() -> None:
    """Leaving it alone is not the same as forgetting the rule."""
    current = {"preferred_resolution": "1080p", "fallback_resolution": "1080p"}

    assert refused({"preferred_resolution": "1080p"}, current) == {
        "preferred_resolution": FALLBACK_EQUALS_PREFERRED
    }
    assert validate({"fallback_resolution": "720p"}, current) == {"fallback_resolution": "720p"}


def test_the_collision_check_is_skipped_when_a_resolution_is_already_wrong() -> None:
    """One message per field, and the useful one — not "and they clash"."""
    errors = refused({"preferred_resolution": "HD", "fallback_resolution": "HD"})
    assert FALLBACK_EQUALS_PREFERRED not in errors.values()


# --- The three windows ------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, MAX_LOOK_AHEAD])
def test_n_is_accepted_up_to_the_ceiling(value: int) -> None:
    assert validate({"look_ahead_n": value}) == {"look_ahead_n": value}


@pytest.mark.parametrize("value", [-1, MAX_LOOK_AHEAD + 1, 200, "2", 2.5, None, True])
def test_n_outside_the_ceiling_is_refused(value: Any) -> None:
    """FR-A1 exists to forbid whole-season fetches; 200 is one."""
    assert "look_ahead_n" in refused({"look_ahead_n": value})


@pytest.mark.parametrize("key", ["grace_days_g", "unwatched_days_d"])
@pytest.mark.parametrize("value", [0, 7, MAX_DAYS])
def test_the_retention_windows_take_a_day_count(key: str, value: int) -> None:
    assert validate({key: value}) == {key: value}


@pytest.mark.parametrize("key", ["grace_days_g", "unwatched_days_d"])
@pytest.mark.parametrize("value", [-1, MAX_DAYS + 1, "7", True, None])
def test_a_bad_retention_window_is_refused(key: str, value: Any) -> None:
    assert key in refused({key: value})


# --- Languages --------------------------------------------------------------


@pytest.mark.parametrize("key", ["sub_lang", "audio_lang"])
@pytest.mark.parametrize(("sent", "stored"), [("en", "en"), ("pt-br", "pt-br"), (" EN ", "en")])
def test_a_language_tag_is_normalised(key: str, sent: str, stored: str) -> None:
    assert validate({key: sent}) == {key: stored}


@pytest.mark.parametrize("key", ["sub_lang", "audio_lang"])
@pytest.mark.parametrize("value", ["e", "englishhh", "en_US", "en-", "-en", "e1", 5, None, ""])
def test_a_bad_language_tag_is_refused(key: str, value: Any) -> None:
    assert key in refused({key: value})


# --- The kill switch --------------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_the_pause_switch_takes_a_boolean(value: bool) -> None:
    assert validate({"acquisition_paused": value}) == {"acquisition_paused": value}


@pytest.mark.parametrize("value", ["true", 1, 0, None, "yes"])
def test_the_pause_switch_refuses_anything_else(value: Any) -> None:
    """A truthy string here would silently mean "paused" for ever."""
    assert "acquisition_paused" in refused({"acquisition_paused": value})


# --- The storage floor (FR-T6) ----------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 10, MAX_MIN_FREE_GB])
def test_the_storage_floor_takes_whole_gb(value: int) -> None:
    """0 is legal and means "no reserve"; the ceiling is a typo guard."""
    assert validate({"min_free_gb": value}) == {"min_free_gb": value}


@pytest.mark.parametrize("value", [-1, MAX_MIN_FREE_GB + 1, "10", 10.5, None, True])
def test_a_bad_storage_floor_is_refused(value: Any) -> None:
    assert "min_free_gb" in refused({"min_free_gb": value})


# --- The slot cap K (FR-A10) ------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 5, MAX_SLOT_CAP])
def test_the_slot_cap_takes_a_whole_number(value: int) -> None:
    """0 is legal and means *unlimited* here, unlike N's 0 (FR-A10)."""
    assert validate({"slot_cap_k": value}) == {"slot_cap_k": value}


@pytest.mark.parametrize("value", [-1, MAX_SLOT_CAP + 1, "5", 5.5, None, True])
def test_a_bad_slot_cap_is_refused(value: Any) -> None:
    assert "slot_cap_k" in refused({"slot_cap_k": value})


# --- storage_hold(), pure ---------------------------------------------------


@pytest.mark.parametrize(
    ("free", "floor", "held"),
    [
        # Under the floor: held.
        (1 * GB, 10 * GB, True),
        (0, 10 * GB, True),
        # Exactly at it is not: the floor is what must be left free, and
        # leaving exactly that much has left it.
        (10 * GB, 10 * GB, False),
        (11 * GB, 10 * GB, False),
        # A floor of zero never holds, whatever the disk says — that is what
        # "no reserve" means, and it keeps "the guard is off" and "the disk is
        # full" from sharing an answer.
        (0, 0, False),
        (1, 0, False),
        # A negative floor cannot be written but can be hand-edited; it reads
        # as off rather than as "hold for ever".
        (0, -1, False),
    ],
)
def test_the_storage_hold_rule(free: int, floor: int, held: bool) -> None:
    """FR-T6's whole arithmetic, as a table."""
    assert storage_hold(free, floor) is held


# --- Per-show overrides, pure (FR-A3, M16) ----------------------------------


def refused_override(preferred_groups: Any = None, resolution: Any = None) -> dict[str, str]:
    """``validate_override`` must have refused; returns the field errors."""
    with pytest.raises(SettingsInvalid) as caught:
        validate_override(preferred_groups, resolution)
    return caught.value.errors


def test_an_override_may_name_either_field_or_both() -> None:
    assert validate_override(["SubsPlease"], None) == {"preferred_groups": ["SubsPlease"]}
    assert validate_override(None, "720p") == {"resolution": "720p"}
    assert validate_override(["ASW"], "480p") == {
        "preferred_groups": ["ASW"],
        "resolution": "480p",
    }


def test_an_override_is_held_to_the_same_group_rules_as_the_global_list() -> None:
    """Same validator, so the ranker reads one kind of list (FR-A3)."""
    assert validate_override(["  SubsPlease ", "subsplease", "ASW"]) == {
        "preferred_groups": ["SubsPlease", "ASW"]
    }
    assert "preferred_groups" in refused_override(["x" * (MAX_GROUP_LENGTH + 1)])
    assert "preferred_groups" in refused_override([f"g{n}" for n in range(MAX_GROUPS + 1)])
    assert "empty" in refused_override(["SubsPlease", "  "])["preferred_groups"]


@pytest.mark.parametrize("value", ["SubsPlease", 7, [1], {"a": "b"}])
def test_override_groups_must_be_a_list_of_strings(value: Any) -> None:
    assert "preferred_groups" in refused_override(value)


@pytest.mark.parametrize("value", ["1080", "FHD", "720P", 1080, True, []])
def test_an_override_resolution_outside_the_closed_set_is_refused(value: Any) -> None:
    assert "resolution" in refused_override(None, value)


@pytest.mark.parametrize(
    ("groups", "resolution"),
    [
        (None, None),
        ([], None),
        (None, ""),
        ([], "   "),
    ],
)
def test_an_override_that_names_nothing_is_no_override(groups: Any, resolution: Any) -> None:
    """The caller deletes the row rather than storing ``{}`` (M16).

    An empty groups field and a resolution of "Use global" are how the show
    page says "back to the global rules", and that is the same state as never
    having had an override — one fewer thing for the ranker to read.
    """
    assert validate_override(groups, resolution) == {}


def test_both_fields_are_blamed_at_once() -> None:
    """One 422 naming both, so the form marks both rather than one at a time."""
    assert sorted(refused_override(["x" * 999], "4K")) == ["preferred_groups", "resolution"]
