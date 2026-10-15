"""A2 item 4: services.internal_routing -- what an Internal Routing replacement must be, and how a stored block is read.
Pure: no database, no framework."""

from __future__ import annotations

import pytest

from services import collector_rules, hold_rules
from services.internal_routing import (
    LISTS,
    MAX_ENTRIES,
    MAX_ENTRY_LENGTH,
    InternalRoutingError,
    InternalRoutingProblem,
    entry_counts,
    stored_internal_routing,
    validate_internal_routing,
)

CANARY = "CANARY-ROUTING-7Q4Z"
EMPTY = {name: [] for name in LISTS}


def _problems(block) -> tuple[InternalRoutingProblem, ...]:
    with pytest.raises(InternalRoutingError) as refused:
        validate_internal_routing(block)
    assert CANARY not in str(refused.value) and CANARY not in repr(refused.value.problems)
    return refused.value.problems


def test_the_four_lists_are_the_classifiers_own():
    assert LISTS == hold_rules.V1_HOLD_LISTS == collector_rules.NAME_LISTS
    assert MAX_ENTRIES == collector_rules.MAX_RULE_ENTRIES


def test_entries_are_trimmed_blanks_dropped_case_kept_and_later_case_insensitive_repeats_dropped_in_order():
    lists = validate_internal_routing({
        **EMPTY,
        "branch_services_names": ["  Zeta ", "", "alpha", "ZETA", "   ", "Alpha", "beta"],
        "collection_services_da_patterns": ["DaOne", "daone"],
    })

    assert lists["branch_services_names"] == ["Zeta", "alpha", "beta"]
    assert lists["collection_services_da_patterns"] == ["DaOne"]
    assert lists["collection_services_names"] == []


def test_empty_lists_are_allowed():
    assert validate_internal_routing(EMPTY) == EMPTY


def test_exactly_the_limits_are_allowed():
    lists = validate_internal_routing({**EMPTY, "branch_services_names": [f"N{i}" for i in range(MAX_ENTRIES)],
                                       "collection_services_names": ["x" * MAX_ENTRY_LENGTH]})
    assert len(lists["branch_services_names"]) == MAX_ENTRIES


@pytest.mark.parametrize(("entries", "problem"), [
    (["x"] * (MAX_ENTRIES + 1), ("branch_services_names", "too_many")),
    (["ok", CANARY + "y" * MAX_ENTRY_LENGTH], ("branch_services_names.1", "too_long")),
    ([CANARY + "\n2"], ("branch_services_names.0", "control_character")),
    ([CANARY + "\t"], ("branch_services_names.0", None)),  # a trailing tab is trimmed whitespace, not refused
    ([CANARY + "\x00"], ("branch_services_names.0", "control_character")),
    ([f"|{CANARY}|"], ("branch_services_names.0", "separator_character")),
    ([None], ("branch_services_names.0", "not_text")),
    ([7], ("branch_services_names.0", "not_text")),
    ([["nested"]], ("branch_services_names.0", "not_text")),
])
def test_each_refused_entry_is_named_by_field_and_index_with_a_code_and_never_by_value(entries, problem):
    block = {**EMPTY, "branch_services_names": entries}
    if problem[1] is None:
        assert validate_internal_routing(block)["branch_services_names"] == [CANARY]
        return
    assert _problems(block) == (InternalRoutingProblem(*problem),)


def test_every_problem_in_every_list_is_reported_together():
    problems = _problems({
        "branch_services_names": "not a list",
        "collection_services_names": [CANARY + "|", 3],
        "branch_services_da_patterns": None,
        "collection_services_da_patterns": [],
        "extra": [],
    })

    assert set(problems) == {
        InternalRoutingProblem("extra", "unknown_field"),
        InternalRoutingProblem("branch_services_names", "not_a_list"),
        InternalRoutingProblem("collection_services_names.0", "separator_character"),
        InternalRoutingProblem("collection_services_names.1", "not_text"),
        InternalRoutingProblem("branch_services_da_patterns", "required"),
    }


def test_a_block_that_is_not_an_object_is_refused():
    assert _problems(["not", "an", "object"]) == (InternalRoutingProblem("internal_routing", "not_an_object"),)


def test_a_stored_block_is_read_leniently_so_it_can_be_seen_and_corrected():
    stored = stored_internal_routing({
        "branch_services_names": [" Kept ", "", "a|b", 12, "kept"],
        "collection_services_names": "not a list",
        "nested": {"ignored": True},
    })

    assert stored == {"branch_services_names": ["Kept", "a|b", "12", "kept"], "collection_services_names": [],
                      "branch_services_da_patterns": [], "collection_services_da_patterns": []}
    assert stored_internal_routing(None) == EMPTY


def test_counts_are_one_number_per_list():
    assert entry_counts({**EMPTY, "branch_services_names": ["a", "b"]}) == {
        "branch_services_names": 2, "collection_services_names": 0,
        "branch_services_da_patterns": 0, "collection_services_da_patterns": 0,
    }


def test_what_a_replacement_stores_is_what_both_classifiers_read_unchanged():
    lists = validate_internal_routing({**EMPTY, "branch_services_names": ["Outreach", "Programs"],
                                       "collection_services_da_patterns": ["DaOne"]})

    v1 = hold_rules.v1_hold_rules(lists)
    v2 = collector_rules.build_classification_rules({"internal_routing": lists})

    assert v1.branch_services_names == frozenset({"OUTREACH", "PROGRAMS"})
    assert v1.collection_services_da_patterns == ("DAONE",)
    assert v2["branch_services_names"] == ["OUTREACH", "PROGRAMS"]
    assert v2["collection_services_da_patterns"] == ["DAONE"]
