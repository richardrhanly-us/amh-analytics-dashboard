"""services.settings_merge: the one rule for a site's effective settings -- its organization's settings document with
its branch's merged over it -- and the one way a stored settings_json becomes a document. Pure: no database, no
framework, no environment.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Mapping
from typing import Any

import pytest

from src.services import settings_merge
from src.services.settings_merge import merge_settings, settings_document

ORGANIZATION = {
    "library_name": "Example Library",
    "transit": {"home": "Main", "destinations": ["A"], "nested": {"a": 1, "b": 2}},
    "internal_routing": {"branch_services_names": ["ORG NAME"], "collection_services_names": ["KEPT"]},
    "security": {"admin_enabled": True},
    "collector_timezone": "America/Chicago",
}
BRANCH = {
    "transit": {"destinations": ["B"], "nested": {"b": 3, "c": 4}},
    "internal_routing": {"branch_services_names": ["BRANCH NAME"]},
    "security": "disabled",
    "branch_name": "East",
}


def test_a_branch_value_replaces_the_organizations_at_the_same_key():
    assert merge_settings({"library_name": "Org", "a": 1}, {"library_name": "Branch"}) == {"library_name": "Branch", "a": 1}


def test_nested_objects_merge_key_by_key():
    organization = {"transit": {"home": "Main", "destinations": ["A"], "nested": {"a": 1, "b": 2}}}
    branch = {"transit": {"destinations": ["B"], "nested": {"b": 3, "c": 4}}}

    assert merge_settings(organization, branch) == {
        "transit": {"home": "Main", "destinations": ["B"], "nested": {"a": 1, "b": 3, "c": 4}}
    }


def test_scalars_and_lists_replace_and_are_never_merged():
    cases = [
        # (organization, branch, effective)
        ({"x": [1, 2]}, {"x": [3]}, {"x": [3]}),
        ({"x": {"a": 1}}, {"x": "off"}, {"x": "off"}),
        ({"x": "on"}, {"x": {"a": 1}}, {"x": {"a": 1}}),
        ({"x": {"a": 1}}, {"x": None}, {"x": None}),
        ({"x": {"a": 1}}, {"x": []}, {"x": []}),
        ({"x": 1}, {"x": False}, {"x": False}),
    ]
    for organization, branch, effective in cases:
        assert merge_settings(organization, branch) == effective, (organization, branch)


def test_a_missing_or_empty_document_is_an_empty_document():
    cases = [
        (None, None, {}),
        ({}, {}, {}),
        (None, {"b": 2}, {"b": 2}),
        ({"a": 1}, None, {"a": 1}),
        ({}, {"b": 2}, {"b": 2}),
        ({"a": 1}, {}, {"a": 1}),
    ]
    for organization, branch, effective in cases:
        assert merge_settings(organization, branch) == effective, (organization, branch)


@pytest.mark.parametrize("not_a_document", [None, "", "text", [], [1], 7, True])
def test_a_value_that_is_not_an_object_is_an_empty_document(not_a_document):
    assert merge_settings(not_a_document, not_a_document) == {}
    assert merge_settings({"a": 1}, not_a_document) == {"a": 1}
    assert merge_settings(not_a_document, {"b": 2}) == {"b": 2}


def test_neither_document_is_changed_and_the_result_is_a_new_document():
    organization, branch = copy.deepcopy(ORGANIZATION), copy.deepcopy(BRANCH)

    effective = merge_settings(organization, branch)
    effective["transit"]["home"] = "Changed"
    effective["added"] = True

    assert organization == ORGANIZATION and branch == BRANCH
    assert effective is not organization and effective is not branch
    assert effective["transit"] is not organization["transit"]


# The two copies merge_settings replaced, verbatim as they were at 5fd6d4b: services.tenant_service._deep_merge_settings
# and services.collector_rules.effective_settings (with its _document).
def _deep_merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_settings(result[key], value)
        else:
            result[key] = value

    return result


def _document(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _collector_effective_settings(organization_settings: object, branch_settings: object) -> dict[str, Any]:
    result = _document(organization_settings)
    for key, value in _document(branch_settings).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _collector_effective_settings(result[key], value)
        else:
            result[key] = value
    return result


def test_it_is_exactly_what_the_two_copies_it_replaced_produce():
    from src.services.collector_rules import effective_settings

    cases = [
        (ORGANIZATION, BRANCH),
        (ORGANIZATION, {}),
        ({}, BRANCH),
        ({}, {}),
        ({"internal_routing": {"branch_services_names": ["A"]}}, {"internal_routing": {"collection_services_names": ["B"]}}),
        ({"internal_routing": "not an object"}, {"internal_routing": {"branch_services_names": ["B"]}}),
        ({"a": {"b": {"c": {"d": 1, "e": 2}}}}, {"a": {"b": {"c": {"e": 3}, "f": [4]}}}),
    ]
    for organization, branch in cases:
        expected = _deep_merge_settings(copy.deepcopy(organization), copy.deepcopy(branch))
        assert _collector_effective_settings(organization, branch) == expected, (organization, branch)
        assert merge_settings(organization, branch) == expected, (organization, branch)
        assert effective_settings(organization, branch) == expected, (organization, branch)


def test_a_stored_settings_json_becomes_a_document():
    cases = [
        ({"a": {"b": 1}}, {"a": {"b": 1}}),
        ('{"a": {"b": 1}}', {"a": {"b": 1}}),
        (b'{"a": 1}', {"a": 1}),
        (None, {}),
        ("[1, 2]", {}),
        ("7", {}),
        ([("a", 1)], {}),
    ]
    for stored, document in cases:
        assert settings_document(stored) == document, stored

    stored = {"a": 1}
    assert settings_document(stored) is not stored


def test_unreadable_stored_text_is_refused_not_guessed():
    with pytest.raises(ValueError):
        settings_document("{not json")


def test_the_module_is_pure():
    source = inspect.getsource(settings_merge)

    for forbidden in ("streamlit", "fastapi", "sqlalchemy", "database", "get_engine", "os.getenv", "os.environ", "logging"):
        assert forbidden not in source.split('"""', 2)[2], forbidden
