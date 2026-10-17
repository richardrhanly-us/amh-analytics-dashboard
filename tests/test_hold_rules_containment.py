"""R8J: the v1 hold classifier's lists are carried in one object whose repr names no entry.

The `internal_routing` block of an organization's settings holds patron ACCOUNT names and `|DA...|` markers -- some of
which can be a person's own patron card. The v1 (pre-cutover) hold classifier, metrics.build_acs_item_summary, needs them
to decide how many holds are public. services.hold_rules hands them on as one object, V1HoldRules, whose repr and str name
no entry, and which treats a missing or malformed block as no lists.

Every name here is synthetic.
"""

from __future__ import annotations

from services.hold_rules import V1HoldRules, v1_hold_rules

CANARY_ACCOUNT = "CANARY (ST)SYNTHETIC STAFF CARD"
CANARY_DEPARTMENT = "CANARY TS-CATALOGING"
INTERNAL = {
    "branch_services_names": [f"  {CANARY_ACCOUNT.lower()} ", "", "   "],
    "collection_services_names": [CANARY_DEPARTMENT, CANARY_DEPARTMENT],
    "branch_services_da_patterns": [f"DA{CANARY_ACCOUNT}"],
    "collection_services_da_patterns": [f"da{CANARY_DEPARTMENT}", ""],
}


def test_the_rules_object_holds_the_lists_as_the_classifier_compares_them_and_its_repr_names_none():
    rules = v1_hold_rules(INTERNAL)

    assert isinstance(rules, V1HoldRules)
    assert rules.branch_services_names == frozenset({CANARY_ACCOUNT, ""})
    assert rules.collection_services_names == frozenset({CANARY_DEPARTMENT})
    assert rules.branch_services_da_patterns == (f"DA{CANARY_ACCOUNT}",)
    assert rules.collection_services_da_patterns == (f"DA{CANARY_DEPARTMENT}", "")
    # Counted as stored: a blank entry is passed on as it is, and the classifier ignores it.
    assert repr(rules) == "V1HoldRules(names=3, patterns=3)"
    assert "CANARY" not in repr(rules) and "CANARY" not in str(rules)


def test_a_missing_or_malformed_block_is_no_lists():
    empty = V1HoldRules(frozenset(), frozenset(), (), ())

    for block in (None, {}, [], "text", {"branch_services_names": "not-a-list"}, {"branch_services_names": None}):
        assert v1_hold_rules(block) == empty
