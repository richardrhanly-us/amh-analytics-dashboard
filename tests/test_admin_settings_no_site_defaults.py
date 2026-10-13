"""Onboarding stage A3b: an organization whose routing settings are missing is never given another library's.

The Admin Settings page used to fill a missing `transit` block with one library's own values -- its home label and its
two destinations -- and to render "Branch 1" / "Branch 2" placeholder rows where an organization had none. Either was
then SAVED as that organization's routing configuration the first time anyone pressed Save, for any reason; and a
Contract v2 enrollment builds the site's classification rules from exactly that block.

What is held here, by running the page under `AppTest` (the harness of tests/test_admin_error_containment.py) and by
calling the dashboard's settings loader directly:

  * with nothing stored, the page shows blanks, and saving it stores an empty routing block;
  * an empty destination row is never saved, and a destination that IS entered gets its own label's key;
  * what an organization does have stored is shown and saved back unchanged;
  * the dashboard's home label falls back to the branch's own name, not to a fixed one.

A library's values in tests, fixtures and docs are not what this is about: only what the running application supplies
when settings are absent.
"""

from __future__ import annotations

import json

import pytest
import test_admin_error_containment as harness
from streamlit.testing.v1 import AppTest
from test_admin_error_containment import (
    ADMIN_SESSION,
    SETTINGS_PAGE,
    SETTINGS_SERVICE,
    _button,
    _rendered,
    _run,
    _text_input,
)

# One library's own values, and the page's old placeholder rows.
SITE_VALUES = ("Westside", "Library Express", "library_express", "westside", "New Braunfels")
PLACEHOLDERS = ("Branch 1", "Branch 2", "branch_1", "branch_2")


def _page(monkeypatch, settings: dict | None = None) -> tuple[AppTest, harness._Engine]:
    def effective(org_slug, branch_slug=None):
        return {**harness._effective(org_slug, branch_slug), "settings": dict(settings or {})}

    engine = harness._Engine()
    harness._patch_settings_page(monkeypatch, effective=effective, engine=engine)
    return _run(SETTINGS_PAGE, session=ADMIN_SESSION), engine


def _saved_organization_settings(engine: harness._Engine) -> dict:
    (params,) = [p for sql, p in engine.executed if sql.startswith("INSERT INTO organization_settings")]
    return json.loads(params["settings_json"])


def _save(at: AppTest) -> None:
    _button(at, "Save Settings").click()
    at.run()


def test_an_organization_with_no_routing_settings_is_shown_blanks_and_no_other_librarys_values(monkeypatch):
    at, _engine = _page(monkeypatch)

    assert not at.error and not at.exception
    assert _text_input(at, "Home branch label").value == ""
    # Two rows are offered, and both are empty.
    assert [(_text_input(at, f"Key {n}").value, _text_input(at, f"Label {n}").value) for n in (1, 2)] == [("", ""), ("", "")]
    rendered = _rendered(at)  # every field, and the "Current DB Preview"
    assert [value for value in SITE_VALUES + PLACEHOLDERS if value in rendered] == []
    assert '"home_branch_label": "Main"' not in rendered


def test_saving_with_nothing_entered_stores_an_empty_routing_block_not_a_default_one(monkeypatch):
    """Saving for any other reason -- here, a new library name -- used to store the page's default destinations."""
    at, engine = _page(monkeypatch)
    _text_input(at, "Library name").input("Acme Public Library")
    _save(at)

    assert not at.error and not at.exception
    saved = _saved_organization_settings(engine)
    assert saved["library_name"] == "Acme Public Library"
    assert saved["transit"] == {"home_branch_label": "", "destinations": []}
    assert [value for value in SITE_VALUES + PLACEHOLDERS if value in json.dumps(saved)] == []


def test_a_destination_that_is_entered_is_saved_under_its_own_labels_key_and_an_empty_row_is_not(monkeypatch):
    at, engine = _page(monkeypatch)
    _text_input(at, "Home branch label").input("Central")
    _text_input(at, "Label 1").input("  North Branch ")  # row 2 is left empty
    _save(at)

    assert not at.error and not at.exception
    assert _saved_organization_settings(engine)["transit"] == {
        "home_branch_label": "Central",
        "destinations": [{"key": "north_branch", "label": "North Branch", "enabled": True}],
    }


@pytest.mark.parametrize("label, key", [
    ("North Branch", "north_branch"),
    ("3rd Street Branch", "d_3rd_street_branch"),
    ("The Very Long Named Regional Library Annex", "the_very_long_named_regional_lib"),
    ("St. Mary's   Branch", "st_mary_s_branch"),
    ("Westside Annex", "westside"),
])
def test_a_blank_key_is_saved_as_the_key_the_reports_derive_from_the_label(monkeypatch, label, key):
    from services.routing_destination import destination_key

    at, engine = _page(monkeypatch)
    _text_input(at, "Label 1").input(label)  # Key 1 is left blank
    _save(at)

    assert not at.error and not at.exception
    (saved,) = _saved_organization_settings(engine)["transit"]["destinations"]
    assert saved == {"key": key, "label": label, "enabled": True}
    assert saved["key"] == destination_key(label)


def test_two_enabled_destinations_that_share_a_canonical_key_are_refused_and_nothing_is_saved(monkeypatch):
    at, engine = _page(monkeypatch)
    _text_input(at, "Label 1").input("The Very Long Named Regional Library Annex")
    _text_input(at, "Label 2").input("The Very Long Named Regional Library Bookmobile")
    _save(at)

    assert not at.exception
    assert [e.value for e in at.error] == [
        ('These transit destinations would be the same destination (they share one key): '
         '"The Very Long Named Regional Library Annex", "The Very Long Named Regional Library Bookmobile". '
         "Change one so that they differ in their first 32 letters and digits."),
        "Settings were not saved.",
    ]
    assert not [sql for sql, _params in engine.executed if sql.startswith(("INSERT INTO", "UPDATE"))]


def _nothing_was_written(engine) -> bool:
    return not [sql for sql, _params in engine.executed if sql.startswith(("INSERT INTO", "UPDATE"))]


@pytest.mark.parametrize("typed, label, key", [
    ("north_annex", "North Annex", "north_annex"),
    ("  North Annex ", "North Annex", "north_annex"),  # tidied as a typed key always was: trimmed, lower case, _ for a space
    ("d_3rd_street_branch", "3rd Street Branch", "d_3rd_street_branch"),
    ("the_very_long_named_regional_lib", "The Very Long Named Regional Library Annex", "the_very_long_named_regional_lib"),
    ("st_mary_s_branch", "St. Mary's   Branch", "st_mary_s_branch"),
    ("westside", "Westside Annex", "westside"),
])
def test_a_typed_key_that_is_its_labels_canonical_key_is_accepted(monkeypatch, typed, label, key):
    from services.routing_destination import destination_key

    at, engine = _page(monkeypatch)
    _text_input(at, "Key 1").input(typed)
    _text_input(at, "Label 1").input(label)
    _save(at)

    assert not at.error and not at.exception
    assert _saved_organization_settings(engine)["transit"]["destinations"] == [{"key": key, "label": label, "enabled": True}]
    assert key == destination_key(label)


@pytest.mark.parametrize("typed, label, canonical", [
    ("Annex", "North Annex", "north_annex"),
    ("dest_1", "North Branch", "north_branch"),          # the old synthetic key
    ("branch_1", "North Branch", "north_branch"),        # the page's old placeholder key
    ("3rd_street_branch", "3rd Street Branch", "d_3rd_street_branch"),  # not a slug the contract allows
    ("westside_annex", "Westside Annex", "westside"),
])
def test_a_typed_key_that_is_not_its_labels_canonical_key_is_refused_and_nothing_is_saved(monkeypatch, typed, label, canonical):
    at, engine = _page(monkeypatch)
    _text_input(at, "Key 1").input(typed)
    _text_input(at, "Label 1").input(label)
    _save(at)

    assert not at.exception
    assert [e.value for e in at.error] == [
        (f'Transit destination 1: the key "{typed.lower()}" is not the key for the label "{label}", which is '
         f'"{canonical}". Clear the Key field to use it, or enter exactly that key.'),
        "Settings were not saved.",
    ]
    assert _nothing_was_written(engine)  # not saved under the typed key, and not silently rewritten either
    assert _text_input(at, "Key 1").value == typed  # still what the administrator typed, to correct


def test_a_stored_key_that_is_not_canonical_stops_the_save_until_it_is_cleared(monkeypatch):
    """An organization provisioned before A3b has {"key": "dest_1", ...} stored. The page shows it, refuses to save it
    again, and saves the canonical key once the Key field is cleared."""
    at, engine = _page(monkeypatch, {"transit": {"home_branch_label": "Central", "destinations": [
        {"key": "dest_1", "label": "North Branch", "enabled": True}]}})

    assert _text_input(at, "Key 1").value == "dest_1"
    _save(at)
    assert [e.value for e in at.error][-1] == "Settings were not saved." and _nothing_was_written(engine)

    _text_input(at, "Key 1").input("")
    _save(at)
    assert not at.error
    assert _saved_organization_settings(engine)["transit"]["destinations"] == [
        {"key": "north_branch", "label": "North Branch", "enabled": True}]


MEANS_HOME = ('"{label}" means this library\'s own (home) branch, so it cannot be a transit destination. '
              "Remove it, or change the Home branch label.")


@pytest.mark.parametrize("home, label", [
    ("Central", "Main"),      # the collector contract's home
    ("Central", "LOCAL"),
    ("Central", "1"),
    ("Central", "central"),   # the home label entered on this same form
    ("", "Main"),
])
def test_a_destination_that_means_home_is_refused_and_nothing_is_saved(monkeypatch, home, label):
    at, engine = _page(monkeypatch)
    _text_input(at, "Home branch label").input(home)
    _text_input(at, "Label 1").input(label)
    _text_input(at, "Label 2").input("North Branch")
    _save(at)

    assert not at.exception
    assert [e.value for e in at.error] == [MEANS_HOME.format(label=label), "Settings were not saved."]
    assert _nothing_was_written(engine)


def test_a_disabled_destination_is_not_held_to_the_home_or_collision_rules(monkeypatch):
    at, engine = _page(monkeypatch, {"transit": {"home_branch_label": "Central", "destinations": [
        {"key": "north_branch", "label": "North Branch", "enabled": True},
        {"key": "north_branch", "label": "North-Branch", "enabled": False}]}})
    _save(at)

    assert not at.error  # a disabled row is not a destination the Collector or the reports use
    assert len(_saved_organization_settings(engine)["transit"]["destinations"]) == 2


STORED_TRANSIT = {
    "home_branch_label": "Main",
    "destinations": [
        {"key": "westside", "label": "Westside", "enabled": True},
        {"key": "library_express", "label": "Library Express", "enabled": False},
        {"label": "Bookmobile", "enabled": True},  # stored with no key, as provisioning now stores a destination
    ],
}


def test_what_an_organization_has_stored_is_shown_and_saved_back_as_it_is(monkeypatch):
    at, engine = _page(monkeypatch, {"transit": STORED_TRANSIT})

    assert _text_input(at, "Home branch label").value == "Main"
    assert [_text_input(at, f"Label {n}").value for n in (1, 2, 3)] == ["Westside", "Library Express", "Bookmobile"]
    assert _text_input(at, "Key 3").value == ""
    assert not [t for t in at.text_input if t.label == "Label 4"]  # its own three rows, and no extra one
    _save(at)

    assert _saved_organization_settings(engine)["transit"] == {
        "home_branch_label": "Main",
        "destinations": [
            {"key": "westside", "label": "Westside", "enabled": True},
            {"key": "library_express", "label": "Library Express", "enabled": False},
            {"key": "bookmobile", "label": "Bookmobile", "enabled": True},  # the label's own key, not an invented one
        ],
    }


def test_the_pages_defaults_and_the_settings_loader_name_no_library_and_no_placeholder():
    page = SETTINGS_PAGE.read_text(encoding="utf-8")
    defaults = page[page.index("DEFAULT_SETTINGS = {"):page.index("def deep_merge_defaults")]

    assert [value for value in SITE_VALUES + ('"Main"',) if value in defaults] == []
    assert '"home_branch_label": "",' in defaults and '"destinations": [],' in defaults
    assert 'f"Branch {i+1}"' not in page and 'f"branch_{i+1}"' not in page
    assert 'transit_settings.get("home_branch_label", "")' in page
    service = SETTINGS_SERVICE.read_text(encoding="utf-8")
    assert [value for value in SITE_VALUES if value in service] == []
    assert 'get("home_branch_label", "Main")' not in service


# --- the dashboard's settings loader ----------------------------------------------------------------------------------

@pytest.fixture
def settings_service():
    from services import settings_service as module

    module.load_runtime_settings.clear()
    yield module
    module.load_runtime_settings.clear()


def _effective(settings: dict):
    def effective(org_slug, branch_slug=None):
        return {
            "organization": {"id": 1, "name": "Acme Library", "slug": org_slug},
            "branch": {"id": 2, "name": "Central Branch", "slug": branch_slug or "central", "is_primary": True},
            "subscription": None,
            "settings": settings,
            "entitlements": {},
        }
    return effective


@pytest.mark.parametrize("settings", [
    {},
    {"transit": {}},
    {"transit": {"home_branch_label": "", "destinations": []}},  # what provisioning and Admin Settings now store
    {"transit": {"home_branch_label": "   "}},
], ids=["no transit block", "an empty block", "a blank label and no destinations", "a label of spaces"])
def test_a_library_with_no_routing_settings_gets_its_own_branch_name_and_no_destinations(settings_service, monkeypatch, tmp_path, settings):
    monkeypatch.setattr(settings_service, "get_effective_settings", _effective(settings))

    result = settings_service.load_runtime_settings(tmp_path / "unused.json", org_slug="acme", branch_slug="central")

    assert result["source"] == "database"
    assert result["TRANSIT_HOME_LABEL"] == "Central Branch"  # the site itself; never a fixed "Main"
    assert result["TRANSIT_DESTINATIONS"] == [] and result["TRANSIT_LABELS"] == []
    assert [value for value in SITE_VALUES if value in repr(result)] == []


def test_a_configured_home_label_and_destinations_are_used_as_they_are(settings_service, monkeypatch, tmp_path):
    settings = {"transit": {"home_branch_label": "Main", "destinations": [{"label": "North Branch", "enabled": True}]}}
    monkeypatch.setattr(settings_service, "get_effective_settings", _effective(settings))

    result = settings_service.load_runtime_settings(tmp_path / "unused.json", org_slug="acme", branch_slug="central")

    assert result["TRANSIT_HOME_LABEL"] == "Main" and result["TRANSIT_LABELS"] == ["North Branch"]


def test_a_settings_file_with_no_library_or_routing_block_names_no_library(settings_service, tmp_path):
    settings_file = tmp_path / "branch_settings.json"
    settings_file.write_text("{}", encoding="utf-8")

    result = settings_service.load_app_settings_from_file(settings_file)

    assert result["LIBRARY_NAME"] == "Library" and result["TRANSIT_HOME_LABEL"] == result["BRANCH_NAME"] == "Main Branch"
    assert result["TRANSIT_LABELS"] == [] and [value for value in SITE_VALUES if value in repr(result)] == []
