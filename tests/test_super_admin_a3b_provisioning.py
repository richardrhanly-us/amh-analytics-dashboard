"""Onboarding stage A3b: Contract v2 as Super Admin's default, and what makes that safe -- the two pages, run for real.

Before A3b a rehearsal provisioned a library by accident: Enter was pressed in an early field of Provision Library, the
form submitted with its contract selector (then the LAST widget) still on the legacy default, and an organization and a
legacy installation existed before anyone had entered the Contract v2 settings. Manage Libraries could then refuse that
organization a Contract v2 installation, and had no way to repair it.

What is held here, by running the pages under `AppTest` with their services replaced (the harness of
tests/test_admin_error_containment.py):

  * both creation forms start on Contract v2, and the legacy contract can still be chosen and still provisions;
  * NOTHING is created -- no organization, no installation, no operational identity -- while any applicable
    requirement is missing: a name, a chosen plan, or (for Contract v2) the three Collector Site Settings entries;
  * the plan is chosen from the service's active plans, with none pre-selected;
  * no library's transit labels are pre-filled, and what is entered is stored by label with no synthetic key;
  * Manage Libraries shows whether an existing organization is ready for Contract v2, repairs it, and never shows a
    classification list's contents -- only how many entries each has.

That Enter cannot submit the Provision form is a property of how the form is built (AppTest has no key presses): see
tests/test_super_admin_collector_site_settings.py, which also holds the source-level pins.
"""

from __future__ import annotations

import logging

import pytest
import test_admin_error_containment as harness
from streamlit.testing.v1 import AppTest
from test_admin_error_containment import (
    MANAGE_PAGE,
    PROVISION_PAGE,
    SUPER_ADMIN_SESSION,
    _button,
    _checkbox,
    _fill_provision_form,
    _library_row,
    _patch_manage,
    _patch_provisioning,
    _rendered,
    _run,
    _run_manage,
    _selectbox,
    _text_input,
)

TIMEZONE_LABEL = "Collector time zone (IANA name, e.g. America/New_York) *"
CONTRACT_LABEL = "Ingest contract for this installation"
NEW_CONTRACT_LABEL = "Ingest contract for the new installation"
NBPL_VALUES = ("Westside", "Library Express", "New Braunfels")


def _text_area(at: AppTest, label: str):
    (match,) = [t for t in at.text_area if t.label == label]
    return match


def _errors(at: AppTest) -> list[str]:
    return [e.value for e in at.error]


def _readiness(at: AppTest) -> list[str]:
    """The one line Manage Libraries shows about Contract v2 readiness (the page has other warnings of its own)."""
    return ([w.value for w in at.warning if w.value.startswith("Not ready for a Contract v2 installation")]
            + [s.value for s in at.success if s.value.startswith("Ready for a Contract v2 installation")])


def _nothing_was_created(calls: dict) -> bool:
    return calls["create_org"] == [] and calls["create_installation"] == [] and calls["assign"] == []


def _provision_page(monkeypatch) -> tuple[AppTest, dict]:
    calls = _patch_provisioning(monkeypatch)
    return _run(PROVISION_PAGE, session=SUPER_ADMIN_SESSION), calls


def _submit(at: AppTest) -> None:
    _button(at, "Provision Library").click()
    at.run()


# =====================================================================================================================
# Provision Library
# =====================================================================================================================

def test_the_provision_form_starts_on_contract_2_with_no_plan_chosen_and_no_librarys_labels(monkeypatch):
    at, _calls = _provision_page(monkeypatch)

    assert not at.error and not at.exception
    contract = _selectbox(at, CONTRACT_LABEL)
    assert contract.value == 2 and len(contract.options) == 2  # ... and the legacy contract is the other choice
    assert contract.options[0].startswith("1 - Legacy contract") and contract.options[1].startswith("2 - Contract v2")
    plan = _selectbox(at, "Plan")
    assert plan.value is None  # no silent billing tier
    assert list(plan.options) == ["Starter (starter)", "Pro (pro)"]  # the service's active plans, and only those
    assert _text_input(at, "Home branch label").value == ""
    assert _text_area(at, "Transit destinations (one per line)").value == ""
    assert _text_input(at, TIMEZONE_LABEL).value == ""
    assert [value for value in NBPL_VALUES if value in _rendered(at)] == []
    assert "trial" not in _rendered(at).lower().replace("starts as a trial of the plan chosen here", "")


def test_a_complete_contract_2_form_provisions_with_what_was_entered_and_nothing_else(monkeypatch):
    at, calls = _provision_page(monkeypatch)
    _fill_provision_form(at)
    _text_input(at, "Home branch label").input("Central")
    _text_area(at, "Transit destinations (one per line)").input("North Branch\n\n  Bookmobile  \nnorth branch")
    _submit(at)

    assert not at.error and not at.exception
    (created,) = calls["create_org"]
    assert created["plan_code"] == "starter" and created["org_slug"] == "acme-library"
    settings = created["org_settings"]
    assert settings["collector_timezone"] == "America/New_York"
    assert settings["collector_rules"]["reviewed_by_user_id"] == harness.SUPER_ADMIN["id"]  # the signed-in Super Admin
    # Exactly the labels entered -- trimmed, blanks and repeats dropped -- each with its label's canonical key.
    assert settings["transit"] == {
        "home_branch_label": "Central",
        "destinations": [{"key": "north_branch", "label": "North Branch", "enabled": True},
                         {"key": "bookmobile", "label": "Bookmobile", "enabled": True}],
    }
    assert "dest_" not in repr(settings) and [value for value in NBPL_VALUES if value in repr(settings)] == []
    (installation,) = calls["create_installation"]
    assert installation["required_contract"] == 2 and installation["status"] == "provisioning"
    assert calls["assign"] == [{"organization_id": 10, "branch_id": 20}]


CANONICAL_KEYS = {
    "North Branch": "north_branch",
    "3rd Street Branch": "d_3rd_street_branch",
    "The Very Long Named Regional Library Annex": "the_very_long_named_regional_lib",
    "St. Mary's   Branch": "st_mary_s_branch",
    "Westside Annex": "westside",
}
COLLIDING = "The Very Long Named Regional Library Annex\nNorth Branch\nThe Very Long Named Regional Library Bookmobile"
COLLISION_MESSAGE = (
    'These transit destinations would be the same destination (they share one key): '
    '"The Very Long Named Regional Library Annex", "The Very Long Named Regional Library Bookmobile". '
    "Change one so that they differ in their first 32 letters and digits."
)


def test_every_provisioned_destination_is_stored_under_the_key_the_reports_derive_from_its_label(monkeypatch):
    from services.routing_destination import destination_key

    at, calls = _provision_page(monkeypatch)
    _fill_provision_form(at)
    _text_area(at, "Transit destinations (one per line)").input("\n".join(CANONICAL_KEYS))
    _submit(at)

    assert not at.error and not at.exception
    stored = calls["create_org"][0]["org_settings"]["transit"]["destinations"]
    assert [(entry["label"], entry["key"]) for entry in stored] == list(CANONICAL_KEYS.items())
    assert all(entry["key"] == destination_key(entry["label"]) for entry in stored)
    assert not [entry for entry in stored if entry["key"].startswith("dest_")]


def test_two_destinations_that_share_a_canonical_key_are_refused_before_anything_is_created(monkeypatch):
    at, calls = _provision_page(monkeypatch)
    _fill_provision_form(at)
    _text_area(at, "Transit destinations (one per line)").input(COLLIDING)
    _submit(at)

    assert not at.exception and _errors(at) == [COLLISION_MESSAGE]
    assert _nothing_was_created(calls)


MEANS_HOME = ('"{label}" means this library\'s own (home) branch, so it cannot be a transit destination. '
              "Remove it, or change the Home branch label.")


@pytest.mark.parametrize("home, label", [("Central", "Main"), ("Central", "local"), ("Central Library", "CENTRAL LIBRARY")],
                         ids=["Main", "local", "the home label entered on the form"])
def test_a_destination_that_means_home_is_refused_before_anything_is_created(monkeypatch, home, label):
    at, calls = _provision_page(monkeypatch)
    _fill_provision_form(at)
    _text_input(at, "Home branch label").input(home)
    _text_area(at, "Transit destinations (one per line)").input(f"North Branch\n{label}")
    _submit(at)

    assert not at.exception
    assert _errors(at) == [MEANS_HOME.format(label=label)]
    assert _nothing_was_created(calls)


def test_a_library_with_no_transit_destinations_is_stored_with_none(monkeypatch):
    at, calls = _provision_page(monkeypatch)
    _fill_provision_form(at)
    _submit(at)

    assert not at.error
    assert calls["create_org"][0]["org_settings"]["transit"] == {"home_branch_label": "", "destinations": []}


def test_submitting_the_form_as_it_first_appears_creates_nothing(monkeypatch):
    """The rehearsal's accident: an organization name, then a submit, before anything else was entered."""
    at, calls = _provision_page(monkeypatch)
    _text_input(at, "Organization name").input("Acme Library")
    _submit(at)

    assert not at.exception and _nothing_was_created(calls)
    assert at.session_state["provision_result"] is None
    errors = _errors(at)
    # Every problem at once: the plan, and the three Contract v2 entries.
    assert len(errors) == 4 and errors[0] == "A plan must be chosen."
    assert all(error.startswith("Contract v2 needs") for error in errors[1:])


def _without_the_time_zone(at):
    _text_input(at, TIMEZONE_LABEL).input("   ")


def _with_a_time_zone_that_is_not_iana(at):
    _text_input(at, TIMEZONE_LABEL).input("Central")


def _without_the_lists_review(at):
    _checkbox(at, "I have reviewed the four Internal Routing lists").uncheck()


def _without_the_destinations_confirmation(at):
    _checkbox(at, "The Home branch label and the Transit destinations").uncheck()


def _without_a_plan(at):
    _selectbox(at, "Plan").select(None)


def _without_an_organization_name(at):
    _text_input(at, "Organization name").input("  ")


def _without_a_branch_name(at):
    _text_input(at, "Primary branch name").input("")


def _without_an_installation_name(at):
    _text_input(at, "Installation name").input(" ")


@pytest.mark.parametrize("leave_out, message", [
    (_without_the_time_zone, "Contract v2 needs the Collector time zone"),
    (_with_a_time_zone_that_is_not_iana, "Collector time zone is not a known IANA time zone name."),
    (_without_the_lists_review, "Contract v2 needs the confirmation that the four Internal Routing lists were reviewed"),
    (_without_the_destinations_confirmation, "Contract v2 needs the confirmation that the Home branch label and the Transit destinations are complete"),
    (_without_a_plan, "A plan must be chosen."),
    (_without_an_organization_name, "Organization name is required."),
    (_without_a_branch_name, "Primary branch name is required."),
    (_without_an_installation_name, "Installation name is required to create an installation record."),
], ids=lambda value: value.__name__.strip("_") if callable(value) else None)
def test_each_missing_requirement_is_refused_before_any_service_is_called(monkeypatch, leave_out, message):
    at, calls = _provision_page(monkeypatch)
    _fill_provision_form(at)
    leave_out(at)
    _submit(at)

    assert not at.exception
    (error,) = _errors(at)  # the one thing missing, and only that
    assert error.startswith(message)
    assert _nothing_was_created(calls)  # no organization, no installation, no operational identity
    assert at.session_state["provision_result"] is None and not at.success


def test_the_legacy_contract_can_still_be_chosen_and_needs_none_of_the_contract_2_entries(monkeypatch):
    at, calls = _provision_page(monkeypatch)
    _text_input(at, "Organization name").input("Acme Library")
    _selectbox(at, "Plan").select("pro")
    _selectbox(at, CONTRACT_LABEL).select(1)
    _submit(at)

    assert not at.error and not at.exception
    assert [s.value for s in at.success] == ["Library provisioned."]
    (created,) = calls["create_org"]
    assert created["plan_code"] == "pro"
    # Blank Contract v2 fields store nothing: the site is not mistaken for a prepared one.
    assert "collector_timezone" not in created["org_settings"] and "collector_rules" not in created["org_settings"]
    (installation,) = calls["create_installation"]
    assert installation["required_contract"] == 1
    assert len(calls["assign"]) == 1


def test_only_a_plan_the_service_listed_can_reach_the_service(monkeypatch):
    at, calls = _provision_page(monkeypatch)

    assert not at.text_input or all("plan" not in t.label.lower() for t in at.text_input)  # nothing to type a plan into
    _fill_provision_form(at)
    # Forced past the selector, as no browser can: a value that is not one of the listed plans still creates nothing.
    try:
        _selectbox(at, "Plan").select("trial")
        _submit(at)
    except Exception:  # the selector itself may refuse a value that is not one of its options
        pass
    assert _nothing_was_created(calls)


def test_the_page_stops_with_a_fixed_message_when_the_plans_cannot_be_loaded(monkeypatch, caplog):
    import services.tenant_service as tenant

    calls = _patch_provisioning(monkeypatch)
    monkeypatch.setattr(tenant, "list_active_plans", harness._raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger="sortview.super_admin.provision"):
        at = _run(PROVISION_PAGE, session=SUPER_ADMIN_SESSION)

    assert not at.exception and harness._leaks(_rendered(at)) == []
    (error,) = _errors(at)
    assert error.startswith("The list of plans could not be loaded")
    assert not at.button and _nothing_was_created(calls)  # no form: nothing can be provisioned without a plan
    assert "Plan list could not be loaded | error_type=sqlalchemy.exc.DataError sqlstate=22P02" in caplog.text
    assert [name for name, canary in harness.CANARIES.items() if canary in caplog.text] == []


def test_the_page_stops_when_there_is_no_active_plan(monkeypatch):
    import services.tenant_service as tenant

    calls = _patch_provisioning(monkeypatch)
    monkeypatch.setattr(tenant, "list_active_plans", list)  # no active plan at all

    at = _run(PROVISION_PAGE, session=SUPER_ADMIN_SESSION)

    assert _errors(at) == ["There is no active plan to put a new library on. Contact SortView engineering."]
    assert not at.button and _nothing_was_created(calls)


# =====================================================================================================================
# Manage Libraries
# =====================================================================================================================

UNPREPARED_SITE = {
    "collector_timezone": None,
    "collector_rules": None,
    "transit_destination_labels": [],
    "internal_routing_counts": {"branch_services_names": 0, "collection_services_names": 0,
                                "branch_services_da_patterns": 0, "collection_services_da_patterns": 0},
}
SITE_TIMEZONE_LABEL = "Collector time zone (IANA name, e.g. America/New_York) *"
SITE_DESTINATIONS_LABEL = "Transit destinations (one per line)"
SITE_REVIEW_LABEL = "I confirm this organization's collector routing configuration has been reviewed"
SAVE_SITE_BUTTON = "Save Collector Site Settings"
NOT_READY = ("Not ready for a Contract v2 installation: This library has no valid Collector time zone recorded. "
             "This library's classification lists have not been recorded as reviewed.")


def _add_installation(at: AppTest, contract: int | None = None) -> None:
    if contract is not None:
        _selectbox(at, NEW_CONTRACT_LABEL).select(contract)
    _button(at, "Add Installation").click()
    at.run()


def test_add_installation_starts_on_contract_2_and_adds_one_for_a_library_that_is_ready(monkeypatch):
    calls = _patch_manage(monkeypatch)

    at = _run_manage()
    selector = _selectbox(at, NEW_CONTRACT_LABEL)
    assert selector.value == 2 and len(selector.options) == 2
    assert _readiness(at) == ["Ready for a Contract v2 installation."]
    _add_installation(at)

    assert not at.error and not at.exception
    assert [(c["organization_id"], c["branch_id"], c["required_contract"]) for c in calls["create"]] == [(10, 20, 2)]


def test_an_unprepared_library_is_shown_as_not_ready_and_is_refused_a_contract_2_installation(monkeypatch):
    calls = _patch_manage(monkeypatch, site=UNPREPARED_SITE)

    at = _run_manage()
    assert _readiness(at) == [NOT_READY]
    _add_installation(at)  # the default: Contract v2

    # The Add Installation guard is still there, behind the status line: nothing was added.
    assert _errors(at) == [
        "This library has no valid Collector time zone recorded.",
        "This library's classification lists have not been recorded as reviewed.",
        ("No installation was added. A Contract v2 installation needs both, recorded for this library first "
         "(Collector Site Settings, above)."),
    ]
    assert calls["create"] == []


def test_a_legacy_installation_can_still_be_added_to_a_library_that_is_not_ready_for_contract_2(monkeypatch):
    calls = _patch_manage(monkeypatch, site=UNPREPARED_SITE)

    at = _run_manage()
    _add_installation(at, contract=1)

    assert not at.error and not at.exception
    assert [(c["organization_id"], c["branch_id"], c["required_contract"]) for c in calls["create"]] == [(10, 20, 1)]
    assert calls["save_site"] == []  # and nothing about the site was changed to allow it


def test_an_unreadable_site_is_refused_a_contract_2_installation_but_not_a_legacy_one(monkeypatch, caplog):
    import services.tenant_service as tenant

    calls = _patch_manage(monkeypatch)
    monkeypatch.setattr(tenant, "get_collector_site_settings", harness._raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger=harness.MANAGE_LOGGER):
        at = _run_manage()
        assert _errors(at) == ["This library's Collector site settings could not be read."]
        assert SAVE_SITE_BUTTON not in [b.label for b in at.button]  # nothing to repair from an unknown state
        _add_installation(at)

    assert calls["create"] == [] and harness._leaks(_rendered(at)) == []
    assert "Collector site settings could not be read | error_type=sqlalchemy.exc.DataError sqlstate=22P02" in caplog.text
    _add_installation(at, contract=1)
    assert [c["required_contract"] for c in calls["create"]] == [1]


def _repairable(monkeypatch, **overrides):
    """Manage Libraries over a library whose stored site settings really change when the repair form saves them."""
    import services.tenant_service as tenant

    stored = {**UNPREPARED_SITE, **overrides}
    saves: list[dict] = []

    def save(organization_id, *, timezone, transit_destination_labels, reviewed_by_user_id):
        saves.append({"organization_id": organization_id, "timezone": timezone,
                      "transit_destination_labels": list(transit_destination_labels), "reviewed_by_user_id": reviewed_by_user_id})
        stored.update({
            "collector_timezone": timezone.strip(),
            "collector_rules": {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": reviewed_by_user_id},
            "transit_destination_labels": [label.strip() for label in transit_destination_labels if label.strip()],
        })

    calls = _patch_manage(monkeypatch, save_site=save)
    monkeypatch.setattr(tenant, "get_collector_site_settings", lambda organization_id: dict(stored))
    return calls, saves


def _repair(at: AppTest, *, timezone: str, destinations: str = "", reviewed: bool = True) -> None:
    _text_input(at, SITE_TIMEZONE_LABEL).input(timezone)
    _text_area(at, SITE_DESTINATIONS_LABEL).input(destinations)
    if reviewed:
        _checkbox(at, SITE_REVIEW_LABEL).check()
    _button(at, SAVE_SITE_BUTTON).click()
    at.run()


def test_the_repair_form_makes_an_unprepared_library_ready_and_a_contract_2_installation_can_then_be_added(monkeypatch):
    calls, saves = _repairable(monkeypatch)

    at = _run_manage()
    assert _readiness(at) == [NOT_READY]
    _repair(at, timezone=" America/Chicago ", destinations="North Branch\n\nBookmobile")

    assert not at.error and not at.exception
    # Saved for THIS organization, as the signed-in Super Admin -- the id from the session, not one made up here.
    assert saves == [{"organization_id": 10, "timezone": " America/Chicago ",
                      "transit_destination_labels": ["North Branch", "", "Bookmobile"],
                      "reviewed_by_user_id": harness.SUPER_ADMIN["id"]}]
    assert _readiness(at) == ["Ready for a Contract v2 installation."]
    assert _text_input(at, SITE_TIMEZONE_LABEL).value == "America/Chicago"  # what is now stored
    assert _text_area(at, SITE_DESTINATIONS_LABEL).value == "North Branch\nBookmobile"
    assert _checkbox(at, SITE_REVIEW_LABEL).value is False  # a review is attested each time, never pre-ticked

    _add_installation(at)  # the default: Contract v2
    assert not at.error
    assert [(c["organization_id"], c["required_contract"]) for c in calls["create"]] == [(10, 2)]


@pytest.mark.parametrize("timezone, reviewed, message", [
    ("", True, "Collector time zone is required, and must be a known IANA time zone name such as America/New_York. Nothing was saved."),
    ("Central", True, "Collector time zone is required, and must be a known IANA time zone name such as America/New_York. Nothing was saved."),
    ("America/Chicago", False, "Confirm that this organization's collector routing configuration has been reviewed. Nothing was saved."),
], ids=["no time zone", "a time zone that is not IANA", "not confirmed as reviewed"])
def test_the_repair_form_saves_nothing_without_a_valid_time_zone_and_the_review_confirmation(monkeypatch, timezone, reviewed, message):
    calls, saves = _repairable(monkeypatch)

    at = _run_manage()
    _repair(at, timezone=timezone, destinations="North Branch", reviewed=reviewed)

    assert _errors(at) == [message] and saves == [] and not at.exception
    assert _readiness(at) == [NOT_READY]  # still not ready
    _add_installation(at)
    assert calls["create"] == []


def test_the_repair_form_saves_nothing_when_two_destinations_share_a_canonical_key(monkeypatch):
    _calls, saves = _repairable(monkeypatch)

    at = _run_manage()
    _repair(at, timezone="America/Chicago", destinations=COLLIDING)

    assert not at.exception and _errors(at) == [COLLISION_MESSAGE, "Nothing was saved."]
    assert saves == [] and _readiness(at) == [NOT_READY]


@pytest.mark.parametrize("label", ["Main", "Local", "central"], ids=["Main", "Local", "the stored home label"])
def test_the_repair_form_saves_nothing_for_a_destination_that_means_home(monkeypatch, label):
    _calls, saves = _repairable(monkeypatch, home_branch_label="Central")

    at = _run_manage()
    _repair(at, timezone="America/Chicago", destinations=f"North Branch\n{label}")

    assert not at.exception
    assert _errors(at) == [MEANS_HOME.format(label=label), "Nothing was saved."]
    assert saves == [] and _readiness(at) == [NOT_READY]


def test_the_repair_form_shows_a_fixed_message_when_saving_fails(monkeypatch, caplog):
    _patch_manage(monkeypatch, site=UNPREPARED_SITE, save_site=harness._raise_backend_error)

    with caplog.at_level(logging.DEBUG, logger=harness.MANAGE_LOGGER):
        at = _run_manage()
        _repair(at, timezone="America/Chicago")

    message = ("The Collector site settings could not be saved; nothing was changed. Please try again. "
               "If this keeps happening, contact SortView support.")
    harness._assert_contained(at, caplog, log_message="Collector site settings save failed", expected_errors=[message])


def test_a_cancelled_library_is_shown_its_readiness_but_offered_no_repair(monkeypatch):
    calls = _patch_manage(monkeypatch, row=_library_row(organization_status="cancelled"), site=UNPREPARED_SITE)

    at = _run_manage()

    assert not at.exception and _readiness(at) == [NOT_READY]
    assert SAVE_SITE_BUTTON not in [b.label for b in at.button]
    assert [t.label for t in at.text_input if t.label == SITE_TIMEZONE_LABEL] == []
    assert calls["save_site"] == []


# --- the privacy boundary: a classification list's contents never reach this page ------------------------------------

LIST_CANARIES = ("CANARY-STAFF-NAME-ONE", "CANARY-STAFF-NAME-TWO", "CANARY-ACCOUNT-NAME", "DACANARY-PATTERN")
STORED_DOCUMENT = {
    "security": {"admin_enabled": True, "admin_password_hash": "CANARY-admin-lock-hash"},
    "internal_routing": {
        "branch_services_names": [LIST_CANARIES[0], LIST_CANARIES[1]],
        "collection_services_names": [LIST_CANARIES[2]],
        "collection_services_da_patterns": [LIST_CANARIES[3]],
    },
    "transit": {"home_branch_label": "Central", "destinations": [{"label": "North Branch", "enabled": True}]},
    "collector_timezone": "America/Denver",
}


class _DocumentEngine:
    """The organization's stored settings document, as the real get_collector_site_settings reads it."""

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _statement, _params=None):
        return self

    def mappings(self):
        return self

    def first(self):
        return {"organization_status": "active", "settings_json": STORED_DOCUMENT}


def test_manage_libraries_shows_how_many_entries_each_classification_list_has_and_never_an_entry(monkeypatch):
    import services.tenant_service as tenant

    real_read = tenant.get_collector_site_settings
    _patch_manage(monkeypatch)
    monkeypatch.setattr(tenant, "get_collector_site_settings", real_read)  # the REAL service, over a real document
    monkeypatch.setattr(tenant, "get_engine", lambda: _DocumentEngine())

    at = _run_manage()

    assert not at.exception and not at.error
    rendered = _rendered(at)
    assert ("Internal Routing lists (entries are not shown here; they are edited in the library's Admin Settings): "
            "Branch Services Names: 2; Collection Services Names: 1; Branch Services DA Patterns: 0; "
            "Collection Services DA Patterns: 1.") in rendered
    assert [canary for canary in LIST_CANARIES if canary in rendered] == [] and "CANARY" not in rendered
    assert "CANARY" not in harness._session_state_dump(at)
    # What it does show of the document: the time zone and the transit labels, which name no person.
    assert _text_input(at, SITE_TIMEZONE_LABEL).value == "America/Denver"
    assert _text_area(at, SITE_DESTINATIONS_LABEL).value == "North Branch"


def test_manage_libraries_has_no_control_that_takes_or_shows_a_classification_list(monkeypatch):
    _patch_manage(monkeypatch, site=UNPREPARED_SITE)

    at = _run_manage()

    # One text area on the whole page, and it is the transit destinations.
    assert [t.label for t in at.text_area] == [SITE_DESTINATIONS_LABEL]
    source = MANAGE_PAGE.read_text(encoding="utf-8")
    assert "get_effective_settings" not in source and "settings_json" not in source
    assert source.count("internal_routing") == source.count("internal_routing_counts")  # counts, and nothing else
