"""R8J: the v1 hold classifier's lists are kept out of the dashboard's generic settings.

The `internal_routing` block of an organization's settings holds patron ACCOUNT names and `|DA...|` markers -- some of
which can be a person's own patron card. The dashboard needs them in exactly one place: the v1 (pre-cutover) hold
classifier, metrics.build_acs_item_summary, which decides how many holds are public. So:

  * services.settings_service hands them on as one object, V1_HOLD_RULES, whose repr names no entry -- and nowhere else:
    not as INTERNAL_ROUTING, not as four top-level lists, not inside the settings document or the tenant it returns;
  * src/app.py reads that one object and gives its four lists to the two hold summaries, and to nothing else;
  * the Admin Settings page's "Current DB Preview" shows how many entries each list has, never an entry.

Every name here is synthetic.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.services import settings_service
from src.services.settings_service import (
    V1HoldRules,
    public_internal_routing_view,
    v1_hold_rules,
    without_internal_routing,
)

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "src" / "app.py"

CANARY_ACCOUNT = "CANARY (ST)SYNTHETIC STAFF CARD"
CANARY_DEPARTMENT = "CANARY TS-CATALOGING"
INTERNAL = {
    "branch_services_names": [f"  {CANARY_ACCOUNT.lower()} ", "", "   "],
    "collection_services_names": [CANARY_DEPARTMENT, CANARY_DEPARTMENT],
    "branch_services_da_patterns": [f"DA{CANARY_ACCOUNT}"],
    "collection_services_da_patterns": [f"da{CANARY_DEPARTMENT}", ""],
}
SETTINGS = {
    "library_name": "Synthetic Library",
    "transit": {"home_branch_label": "Main", "destinations": [{"key": "annex", "label": "Annex", "enabled": True}]},
    "internal_routing": INTERNAL,
    "security": {"admin_enabled": True, "admin_password_hash": "CANARY-HASH"},
}


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    settings_service.load_runtime_settings.clear()
    yield
    settings_service.load_runtime_settings.clear()


def _effective(org_slug, branch_slug=None):
    return {
        "organization": {"id": 1, "name": "Synthetic Library", "slug": org_slug},
        "branch": {"id": 2, "name": "Main", "slug": branch_slug or "main", "is_primary": True},
        "subscription": None,
        "settings": json.loads(json.dumps(SETTINGS)),
        "entitlements": {},
    }


def _everything_but_the_rules(app_settings: dict) -> str:
    return repr({key: value for key, value in app_settings.items() if key != "V1_HOLD_RULES"})


@pytest.fixture
def from_database(monkeypatch):
    monkeypatch.setattr(settings_service, "get_effective_settings", _effective)
    return settings_service.load_app_settings_from_db("synthetic", "main")


@pytest.fixture
def from_file(tmp_path):
    path = tmp_path / "branch_settings.json"
    path.write_text(json.dumps({"library": {"library_name": "Synthetic Library"}, **SETTINGS}), encoding="utf-8")
    return settings_service.load_app_settings_from_file(path)


# =====================================================================================================================
# The settings the dashboard is given
# =====================================================================================================================

@pytest.mark.parametrize("source", ["from_database", "from_file"])
def test_no_list_name_or_pattern_is_in_the_dashboards_settings_but_the_one_rules_object(request, source):
    app_settings = request.getfixturevalue(source)

    for gone in ("INTERNAL_ROUTING", "BRANCH_SERVICES_NAMES", "COLLECTION_SERVICES_NAMES", "BRANCH_SERVICES_DA_PATTERNS",
                 "COLLECTION_SERVICES_DA_PATTERNS"):
        assert gone not in app_settings, gone
    assert "internal_routing" not in app_settings["branch_settings"]
    if "tenant" in app_settings:
        assert "internal_routing" not in app_settings["tenant"]["settings"]
    text = _everything_but_the_rules(app_settings)
    assert "CANARY" not in text and "internal_routing" not in text
    # What the dashboard still needs of the same document is all there.
    assert app_settings["TRANSIT_LABELS"] == ["Annex"]
    assert app_settings["branch_settings"]["transit"] == SETTINGS["transit"]


@pytest.mark.parametrize("source", ["from_database", "from_file"])
def test_the_rules_object_holds_the_lists_as_the_classifier_compares_them_and_its_repr_names_none(request, source):
    rules = request.getfixturevalue(source)["V1_HOLD_RULES"]

    assert isinstance(rules, V1HoldRules)
    assert rules.branch_services_names == frozenset({CANARY_ACCOUNT, ""})
    assert rules.collection_services_names == frozenset({CANARY_DEPARTMENT})
    assert rules.branch_services_da_patterns == (f"DA{CANARY_ACCOUNT}",)
    assert rules.collection_services_da_patterns == (f"DA{CANARY_DEPARTMENT}", "")
    # Counted as stored: a blank entry is passed on as it is, and the classifier ignores it.
    assert repr(rules) == "V1HoldRules(names=3, patterns=3)"
    assert "CANARY" not in repr(rules) and "CANARY" not in str(rules)


def test_the_rules_object_survives_the_settings_cache_and_the_cached_settings_hold_no_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_service, "get_effective_settings", _effective)

    cached = settings_service.load_runtime_settings(tmp_path / "unused.json", org_slug="synthetic", branch_slug="main")
    again = settings_service.load_runtime_settings(tmp_path / "unused.json", org_slug="synthetic", branch_slug="main")

    assert again["V1_HOLD_RULES"] == cached["V1_HOLD_RULES"] == pickle.loads(pickle.dumps(cached["V1_HOLD_RULES"]))
    assert "CANARY" not in _everything_but_the_rules(again)


def test_a_missing_or_malformed_block_is_no_lists():
    empty = V1HoldRules(frozenset(), frozenset(), (), ())

    for block in (None, {}, [], "text", {"branch_services_names": "not-a-list"}, {"branch_services_names": None}):
        assert v1_hold_rules(block) == empty
    assert without_internal_routing({"internal_routing": INTERNAL, "transit": {}}) == {"transit": {}}
    assert without_internal_routing(None) == {}


# =====================================================================================================================
# What may be shown of the block outside its own form
# =====================================================================================================================

def test_the_public_view_is_how_many_entries_each_list_has_and_never_an_entry():
    view = public_internal_routing_view(INTERNAL)

    assert view == {
        "branch_services_names": "1 entry (not shown)",
        "collection_services_names": "2 entries (not shown)",
        "branch_services_da_patterns": "1 entry (not shown)",
        "collection_services_da_patterns": "1 entry (not shown)",
    }
    assert "CANARY" not in repr(view)
    assert public_internal_routing_view(None) == {key: "0 entries (not shown)" for key in view}


def test_the_admin_pages_db_preview_shows_counts_and_no_entry(monkeypatch):
    from test_admin_error_containment import (
        ADMIN_SESSION,
        SETTINGS_PAGE,
        _patch_settings_page,
        _run,
    )

    def unlocked(org_slug, branch_slug=None):
        # No admin password set, so the page opens straight onto its form and its preview.
        effective = _effective(org_slug, branch_slug)
        del effective["settings"]["security"]
        return effective

    _patch_settings_page(monkeypatch, effective=unlocked)

    at = _run(SETTINGS_PAGE, session=ADMIN_SESSION)

    assert not at.exception, [e.value for e in at.exception]
    previews = [element.value for element in at.json]
    assert len(previews) == 1
    preview = json.loads(previews[0]) if isinstance(previews[0], str) else previews[0]
    assert preview["internal_routing"] == public_internal_routing_view(INTERNAL)
    assert "CANARY" not in json.dumps(preview)
    assert preview["security"] == {"admin_enabled": True, "admin_password_set": False}
    # The form itself still shows the entries to the people allowed to edit them.
    assert any(CANARY_DEPARTMENT in (area.value or "") for area in at.text_area)


# =====================================================================================================================
# The dashboard's own use of them
# =====================================================================================================================

def test_the_dashboard_reads_the_one_object_and_hands_its_lists_only_to_the_two_hold_summaries():
    source = APP.read_text(encoding="utf-8")

    assert source.count('app_settings["V1_HOLD_RULES"]') == 1
    for gone in ("INTERNAL_ROUTING", "BRANCH_SERVICES_NAMES", "COLLECTION_SERVICES_NAMES", "_DA_PATTERNS = ", "internal_routing"):
        assert gone not in source, gone
    # Each of the four lists is passed exactly twice: to the live hold summary and to the history one.
    for field in ("branch_services_names", "collection_services_names", "branch_services_da_patterns", "collection_services_da_patterns"):
        assert source.count(f"V1_HOLD_RULES.{field}") == 2, field
    assert source.count("V1_HOLD_RULES") == 1 + 4 * 2 + 1  # read once, eight uses, one mention in its comment


def test_the_dashboard_still_starts(monkeypatch):
    # The app module imports and reaches its sign-in gate with the changed settings shape.
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()

    assert not at.exception, [e.value for e in at.exception]
