"""Super Admin and Contract v2 enrollment (onboarding stage A1): what Provision Library collects for a site, what
Manage Libraries shows, and what NEITHER does yet.

A1 landed before an installer that could enroll on Contract v2, so the pages only COLLECTED what a contract-2 enrollment
needs (the Collector's time zone, and the record that the classification lists were reviewed) and SHOWED which contract
an installation requires.

A3a ships that installer (Collector 1.0.14) and adds an explicit CONTRACT SELECTOR to both places an installation is
created -- Provision Library, and Manage Libraries' "Add installation". The selected value is passed explicitly to
create_collector_installation, a Contract v2 choice is refused for a site that has not been prepared, and an existing
installation's contract can never be changed.

A3b makes CONTRACT V2 THE SELECTOR'S DEFAULT on both pages (the legacy contract stays selectable, and the service's own
default stays 1), and makes that safe: the selector is the first field of a Provision form that Enter cannot submit,
no library's transit labels are pre-filled, and no synthetic destination key is written. What the pages DO when they
run is in tests/test_super_admin_a3b_provisioning.py.

The pages are Streamlit scripts and are not imported: the function is extracted from the page source with `ast` and run
for real (as tests/test_super_admin_installation_config.py does), and the rest is asserted against the source.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.services import collector_rules
from src.services import tenant_service as tenant_contracts
from src.services.collector_rules import CollectorSettingsError

ROOT = Path(__file__).resolve().parent.parent
PROVISION = ROOT / "super_admin" / "pages" / "Provision_Library.py"
MANAGE = ROOT / "super_admin" / "pages" / "Manage_Libraries.py"
REVIEWED_INSTANT = datetime(2026, 10, 11, 15, 4, 5, tzinfo=UTC)  # passed in; nothing here reads a clock


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _site_settings():
    tree = ast.parse(_source(PROVISION))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "collector_site_settings")
    namespace: dict[str, Any] = {
        "Any": Any, "datetime": datetime,
        "TIMEZONE_KEY": collector_rules.TIMEZONE_KEY, "RULES_REVIEW_KEY": collector_rules.RULES_REVIEW_KEY,
        "collector_timezone": collector_rules.collector_timezone, "rules_review_marker": collector_rules.rules_review_marker,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(PROVISION), "exec"), namespace)  # nosec B102 - repo source
    return namespace["collector_site_settings"]


def test_nothing_entered_stores_nothing_so_an_unprepared_site_is_never_mistaken_for_a_ready_one():
    settings = _site_settings()("", False, reviewer_user_id=7, now=REVIEWED_INSTANT)

    assert settings == {}
    # And a site provisioned that way is not ready for a contract-2 enrollment, on either count.
    with pytest.raises(CollectorSettingsError):
        collector_rules.collector_timezone(settings)
    with pytest.raises(CollectorSettingsError):
        collector_rules.require_rules_reviewed(settings)


def test_a_time_zone_and_a_ticked_review_store_exactly_what_enrollment_reads():
    settings = _site_settings()("  America/New_York ", True, reviewer_user_id=7, now=REVIEWED_INSTANT)

    assert settings == {
        "collector_timezone": "America/New_York",
        "collector_rules": {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": 7},
    }
    assert collector_rules.collector_timezone(settings) == "America/New_York"
    collector_rules.require_rules_reviewed(settings)
    # The review record names who and when, and no account.
    assert set(settings["collector_rules"]) == {"reviewed_at", "reviewed_by_user_id"}


def test_each_is_stored_only_when_it_was_given():
    build = _site_settings()

    assert set(build("America/Denver", False, reviewer_user_id=7, now=REVIEWED_INSTANT)) == {"collector_timezone"}
    assert set(build("   ", True, reviewer_user_id=7, now=REVIEWED_INSTANT)) == {"collector_rules"}


@pytest.mark.parametrize("entered", ["Central", "CST", "America/Not_A_City", "UTC-6"])
def test_a_time_zone_that_is_not_iana_is_refused_before_anything_is_created(entered):
    with pytest.raises(CollectorSettingsError):
        _site_settings()(entered, True, reviewer_user_id=7, now=REVIEWED_INSTANT)

    # The page validates before it creates the organization, and says so in fixed words.
    source = _source(PROVISION)
    assert source.index("collector_settings = collector_site_settings(") < source.index("result = create_organization_with_primary_branch(")
    assert "Collector time zone is not a known IANA time zone name." in source


def test_the_page_merges_them_into_the_organizations_settings_and_defaults_neither():
    source = _source(PROVISION)

    assert "org_settings.update(collector_settings)" in source
    # No default zone, and the confirmation starts unticked.
    assert 'st.text_input(\n        "Collector time zone (IANA name, e.g. America/New_York) *",\n        help=' in source.replace("\r\n", "\n")
    assert "value=False,\n    )\n" in source.replace("\r\n", "\n")
    assert "America/Chicago" not in source
    # Neither key is among the settings a new organization gets without anyone entering them.
    defaults = source[source.index("DEFAULT_ORG_SETTINGS = {"):source.index("DEFAULT_BRANCH_SETTINGS")]
    assert "collector_timezone" not in defaults and "collector_rules" not in defaults
    assert "reviewer_user_id=int(auth_user[\"id\"])" in source and "now=datetime.now(UTC)" in source


def test_the_page_says_what_the_fields_are_for_and_what_they_do_not_change():
    source = " ".join(_source(PROVISION).split())

    assert "Collector Site Settings (Contract v2)" in source
    assert "REQUIRED for a Contract v2 installation (the default, chosen at the top of this form)" in source
    assert "for a legacy (contract 1) \" \"installation leaving them blank changes nothing." in source
    assert "still the default" not in source
    assert "it does not change how reports are drawn" in source


def test_the_three_contract_2_entries_are_marked_required_and_the_form_says_what_the_mark_means():
    source = _source(PROVISION)
    section = source[source.index('st.subheader("Collector Site Settings (Contract v2)")'):source.index('st.subheader("Agent Config")')]

    labels = [" ".join(ast.literal_eval(node.args[0]).split()) for node in ast.walk(ast.parse(section.replace("\n    ", "\n")))
              if isinstance(node, ast.Call) and ast.unparse(node.func) in ("st.text_input", "st.checkbox")]
    assert len(labels) == 3 and all(label.endswith(" *") for label in labels), labels
    assert "Fields marked * are required for a Contract v2 installation, and are checked before anything is created." in " ".join(source.split()).replace('" "', "")


def test_manage_libraries_shows_the_required_contract_and_never_edits_an_existing_installations():
    source = _source(MANAGE)

    assert '"required_contract": "Required Contract"' in source and source.count('"Required Contract"') == 2
    # The ONE contract control on the page is the selector in the "Add installation" form. Nothing in the form that
    # edits an existing installation mentions a contract, and the update call is never given one.
    controls = [line for line in source.splitlines()
                if re.search(r"st\.(selectbox|number_input|radio|text_input|checkbox)\(", line) and "contract" in line.lower()]
    assert [line.strip() for line in controls] == ["new_contract = st.selectbox("]
    assert source.count('"Ingest contract for the new installation"') == 1
    add_form = source[source.index('with st.form("add_installation_form"):'):]
    assert '"Ingest contract for the new installation"' in add_form
    update_call = source[source.index("update_collector_installation("):]
    update_call = update_call[:update_call.index(")")]
    assert "contract" not in update_call.lower()
    assert "required_contract" not in _function_source(ROOT / "src" / "services" / "tenant_service.py", "update_collector_installation").split('"""')[2]


# --- the contract selector (A3a) -------------------------------------------------------------------------------------

PAGES = pytest.mark.parametrize("page", [PROVISION, MANAGE], ids=["Provision Library", "Manage Libraries"])


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(_source(path))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_source(path), node) or ""


def _page_namespace(path: Path, *names: str) -> dict[str, Any]:
    """The page's own contract constants and the named functions, executed for real (the page itself is a Streamlit
    script and is never imported)."""
    tree = ast.parse(_source(path))
    wanted = [
        node for node in tree.body
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith(("COLLECTOR_CONTRACT_OPTIONS",
            "DEFAULT_COLLECTOR_CONTRACT", "CONTRACT_V2_MINIMUM")) for t in node.targets))
        or (isinstance(node, ast.FunctionDef) and node.name in names)
    ]
    namespace: dict[str, Any] = {
        "COLLECTOR_CONTRACT_LEGACY": tenant_contracts.COLLECTOR_CONTRACT_LEGACY, "COLLECTOR_CONTRACT_V2": tenant_contracts.COLLECTOR_CONTRACT_V2,
        "CollectorSettingsError": CollectorSettingsError, "collector_timezone": collector_rules.collector_timezone,
        "require_rules_reviewed": collector_rules.require_rules_reviewed,
    }
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(path), "exec"), namespace)  # nosec B102 - repo source
    return namespace


@PAGES
def test_both_creation_paths_have_a_contract_selector_whose_default_is_contract_2_and_still_offers_the_legacy_one(page):
    namespace = _page_namespace(page, "collector_contract_label")

    assert namespace["COLLECTOR_CONTRACT_OPTIONS"] == (1, 2)  # the legacy contract is still a choice
    assert namespace["DEFAULT_COLLECTOR_CONTRACT"] == 2  # A3b: the default IS Contract v2
    source = _source(page)
    assert source.count("st.selectbox(") >= 1
    selector = source[source.index('"Ingest contract for'):]
    selector = selector[:selector.index("format_func=collector_contract_label")]
    assert "options=list(COLLECTOR_CONTRACT_OPTIONS)" in selector
    assert "index=COLLECTOR_CONTRACT_OPTIONS.index(DEFAULT_COLLECTOR_CONTRACT)" in selector  # the default is the constant, not a literal
    assert "DEFAULT_COLLECTOR_CONTRACT = COLLECTOR_CONTRACT_V2" in source
    assert "DEFAULT_COLLECTOR_CONTRACT = COLLECTOR_CONTRACT_LEGACY" not in source


def test_the_provision_form_cannot_be_submitted_by_enter_and_its_contract_selector_is_its_first_field():
    """Enter in a text field used to submit the whole form -- with the selector, then the LAST widget, still on its
    default. Only the button submits now, and the selector comes before every field whose requiredness it decides."""
    tree = ast.parse(_source(PROVISION))
    (form,) = [node for node in ast.walk(tree) if isinstance(node, ast.With)
               and ast.unparse(node.items[0].context_expr).startswith("st.form(")]
    call = form.items[0].context_expr

    assert ast.literal_eval(call.args[0]) == "provision_library_form"
    assert {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords} == {"enter_to_submit": False}
    widgets = [node for node in ast.walk(form) if isinstance(node, ast.Call) and ast.unparse(node.func).startswith("st.")
               and ast.unparse(node.func) not in ("st.form", "st.caption", "st.columns", "st.subheader")]
    first = min(widgets, key=lambda node: (node.lineno, node.col_offset))
    assert ast.unparse(first.func) == "st.selectbox" and ast.literal_eval(first.args[0]) == "Ingest contract for this installation"
    # ... and it is inside the form, once: there is no second contract control anywhere on the page.
    assert _source(PROVISION).count('"Ingest contract for this installation"') == 1


def test_the_plan_is_chosen_from_the_active_plans_with_none_pre_selected_and_is_never_typed():
    source = _source(PROVISION)
    tree = ast.parse(source)
    (plan,) = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == "st.selectbox"
               and ast.literal_eval(node.args[0]) == "Plan"]
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in plan.keywords}

    assert keywords["options"] == "list(plan_names)" and keywords["index"] == "None"  # the service's plans; no default
    assert "active_plans = list_active_plans()" in source
    assert "plan_names = {str(plan['code']): str(plan['name']) for plan in active_plans}" in ast.unparse(tree)
    assert 'text_input("Plan' not in source and "'trial'" not in source and '"trial"' not in source
    # Refused before anything is created, and what reaches the service is the chosen code itself.
    assert source.index('problems.append("A plan must be chosen.")') < source.index("result = create_organization_with_primary_branch(")
    assert "plan_code=plan_code," in source


@PAGES
def test_the_selected_contract_is_passed_explicitly_to_the_service_and_its_default_is_not_relied_on(page):
    calls = [node for node in ast.walk(ast.parse(_source(page)))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "create_collector_installation"]

    assert len(calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert keywords["required_contract"] in ("installation_contract", "new_contract")  # the selector's own value
    assert keywords["status"] == "'provisioning'"


def test_the_service_default_and_validation_are_unchanged():
    import inspect

    from src.services import tenant_service

    assert inspect.signature(tenant_service.create_collector_installation).parameters["required_contract"].default == 1
    assert tenant_service.COLLECTOR_CONTRACTS == (1, 2)


@PAGES
def test_the_contract_2_choice_is_labelled_with_the_collector_release_it_needs(page):
    namespace = _page_namespace(page, "collector_contract_label")
    label = namespace["collector_contract_label"]

    assert label(1) == "1 - Legacy contract"  # no longer called the default
    assert label(2) == "2 - Contract v2 (requires SortView Collector 1.0.14 or later)"  # freshness: allow FRESH002 -- the first release that can enroll on Contract v2: a fixed minimum the page states, not the current version
    assert namespace["CONTRACT_V2_MINIMUM_COLLECTOR_VERSION"] == (1, 0, 14)
    # The current release is never copied into the page: this minimum is a fixed fact, held as a tuple.
    assert "CONTRACT_V2_MINIMUM_COLLECTOR_VERSION = (1, 0, 14)" in _source(page)
    assert "{CONTRACT_V2_MINIMUM_COLLECTOR} release bundle or later" in _source(page)


def test_provisioning_on_contract_2_requires_the_time_zone_both_confirmations_and_says_which_is_missing():
    problems = _page_namespace(PROVISION, "contract_two_provisioning_problems")["contract_two_provisioning_problems"]

    assert problems("America/New_York", True, True) == []
    assert len(problems("", False, False)) == 3
    assert len(problems("   ", True, True)) == 1 and "time zone" in problems("   ", True, True)[0]
    assert len(problems("America/New_York", False, True)) == 1 and "Internal Routing lists" in problems("America/New_York", False, True)[0]
    (labels,) = problems("America/New_York", True, False)
    assert "Home branch label" in labels and "Transit destinations" in labels and "complete" in labels


def test_every_check_runs_before_anything_is_created_and_the_contract_2_ones_only_for_contract_2():
    source = _source(PROVISION).replace("\r\n", "\n")
    submit = source[source.index("if submitted:"):]
    check = submit.index("if create_installation and installation_contract == COLLECTOR_CONTRACT_V2:")
    stop = submit.index("    if problems:\n        for problem in problems:\n            st.error(problem)\n        st.stop()\n")
    first_write = min(submit.index(call) for call in (
        "create_organization_with_primary_branch(", "create_collector_installation(", "run_operational_stage("))

    # Every problem is collected, shown and stopped on before the first of the three service calls.
    assert "problems.extend(contract_two_provisioning_problems(" in submit[check:stop]
    for required in ('"Organization name is required."', '"Primary branch name is required."', '"A plan must be chosen."',
                     '"Installation name is required to create an installation record."'):
        assert submit.index(required) < stop, required
    assert check < stop < submit.index("collector_settings = collector_site_settings(") < first_write
    # A legacy installation is provisioned exactly as before: none of the three is required for it.
    assert source.count("contract_two_provisioning_problems(") == 2  # its definition and that one guarded call


NBPL_VALUES = ("Westside", "Library Express", "library_express", "westside", "New Braunfels", '"Main"', "'Main'")


def test_provisioning_pre_fills_no_librarys_transit_labels_and_writes_no_synthetic_destination_key():
    """The form used to pre-fill one library's home label and destinations, and to store whatever was entered under
    "dest_1" / "dest_2". For Contract v2 the destinations become the site's classification rules, so nothing is
    pre-filled, and the administrator still confirms the list is complete -- an unticked box, required."""
    source = _source(PROVISION)

    assert [value for value in NBPL_VALUES if value in source] == []
    assert "dest_1" not in source and "dest_2" not in source and re.search(r"dest_\{|f\"dest|'key':|\"key\":", source) is None
    assert 'home_branch_label = st.text_input("Home branch label")' in source  # no value=
    assert 'transit_destinations_text = st.text_area("Transit destinations (one per line)", height=100)' in source
    assert '"destinations": transit_destination_entries(lines_to_list(transit_destinations_text)),' in source
    defaults = _page_namespace_value(PROVISION, "DEFAULT_ORG_SETTINGS")
    assert defaults["transit"] == {"home_branch_label": "", "destinations": []}
    box = source[source.index("collector_routing_labels_confirmed = st.checkbox("):]
    box = box[:box.index("st.subheader(")]
    assert "value=False" in box and "pre-filled" not in box and "complete for this library" in " ".join(box.split())
    assert "collector_routing_labels_confirmed" in source[source.index("problems.extend(contract_two_provisioning_problems("):][:200]


def _page_namespace_value(path: Path, name: str) -> Any:
    tree = ast.parse(_source(path))
    (node,) = [n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    return ast.literal_eval(node.value)


# One canonical key for a destination, wherever it is saved: services.routing_destination.destination_key(label).
CANONICAL_KEYS = [
    ("North Branch", "north_branch"),
    ("3rd Street Branch", "d_3rd_street_branch"),  # a slug starts with a letter
    ("The Very Long Named Regional Library Annex", "the_very_long_named_regional_lib"),  # ... and is at most 32 long
    ("St. Mary's   Branch", "st_mary_s_branch"),  # punctuation and repeated spaces
    ("Westside Annex", "westside"),  # the collector's built-in rule for that text
]


@pytest.mark.parametrize("label, key", CANONICAL_KEYS, ids=[label for label, _key in CANONICAL_KEYS])
def test_a_saved_destinations_key_is_the_one_key_the_collector_and_the_reports_both_use(label, key):
    """The regression: stored as {"key": "dest_1", "label": "North Branch"}, a destination went to the Collector as the
    slug `dest_1` while the reports looked for `north_branch` -- so its check-ins were counted as "other". And a key
    that is not a valid collector slug (one starting with a digit, or longer than 32) could never be met at all."""
    from collector import v2_rules
    from services.ingest_v2_models import DESTINATION_PATTERN
    from services.routing_destination import build_routing_config, destination_key

    (entry,) = tenant_contracts.transit_destination_entries([f"  {label} "])
    settings = {"transit": {"home_branch_label": "Central", "destinations": [entry]}}
    (rule,) = collector_rules.build_classification_rules(settings)["destinations"]
    (routed,) = build_routing_config(settings, site_name="Central").transit

    # stored key == destination_key(label) == the Collector's rule slug == the reports' routing key
    assert entry == {"key": key, "label": label, "enabled": True}
    assert entry["key"] == destination_key(label) == rule["slug"] == routed.key == key
    assert rule["contains"] == label.upper()
    # ... and it is a destination slug the contract allows, so a Collector CAN store a check-in under it.
    assert re.fullmatch(DESTINATION_PATTERN, key) and len(key) <= 32
    assert not key.startswith("dest_")
    # The stored key passes through the rules builder -- the server's copy and the Collector's own -- unchanged,
    # and a check-in stored under it is keyed back to it.
    assert collector_rules._slugify(key) == v2_rules._slugify(key) == key
    assert destination_key(key) == key
    # The Collector's own seeder gives the same rule for the same stored entry.
    assert v2_rules.seed_from_settings(settings)["destinations"] == [{"slug": key, "contains": label.upper()}]


def test_entries_are_trimmed_and_a_blank_or_repeated_label_is_left_out():
    entries = tenant_contracts.transit_destination_entries(["  East Side Annex  ", "", None, "east side annex", "Riverside"])

    assert entries == [{"key": "east_side_annex", "label": "East Side Annex", "enabled": True},
                       {"key": "riverside", "label": "Riverside", "enabled": True}]
    assert tenant_contracts.transit_destination_entries([]) == []


@pytest.mark.parametrize("labels, groups", [
    # Two long labels that are the same in their first 32 letters and digits.
    (["The Very Long Named Regional Library Annex", "North Branch", "The Very Long Named Regional Library Bookmobile"],
     [["The Very Long Named Regional Library Annex", "The Very Long Named Regional Library Bookmobile"]]),
    (["North Branch", "North-Branch", "NORTH  BRANCH!"], [["North Branch", "North-Branch", "NORTH  BRANCH!"]]),
    (["Westside", "Westside Annex", "3rd Street", "3rd  street."], [["Westside", "Westside Annex"], ["3rd Street", "3rd  street."]]),
])
def test_different_labels_that_share_one_canonical_key_are_reported_as_a_collision(labels, groups):
    assert tenant_contracts.transit_destination_key_collisions(labels) == groups
    messages = tenant_contracts.transit_destination_collision_messages(labels)
    assert len(messages) == len(groups)
    for message, group in zip(messages, groups, strict=True):
        assert all(f'"{label}"' in message for label in group) and "share one key" in message


@pytest.mark.parametrize("labels", [
    [],
    ["North Branch", "South Branch", "3rd Street Branch", "The Very Long Named Regional Library Annex"],
    ["North Branch", "north branch", "  NORTH BRANCH  "],  # the same label again is one label, not a collision
])
def test_labels_that_are_each_a_destination_of_their_own_are_no_collision(labels):
    assert tenant_contracts.transit_destination_key_collisions(labels) == []
    assert tenant_contracts.transit_destination_collision_messages(labels) == []


def test_the_synthetic_key_was_the_defect_this_guards_against():
    from services.routing_destination import build_routing_config

    old = {"transit": {"destinations": [{"key": "dest_1", "label": "North Branch", "enabled": True}]}}

    assert [d["slug"] for d in collector_rules.build_classification_rules(old)["destinations"]] == ["dest_1"]
    assert [d.key for d in build_routing_config(old, site_name="Central").transit] == ["north_branch"]  # ... which never met


def test_adding_a_contract_2_installation_needs_the_sites_stored_time_zone_and_review_record():
    problems = _page_namespace(MANAGE, "contract_two_site_problems")["contract_two_site_problems"]
    marker = collector_rules.rules_review_marker(reviewed_at=REVIEWED_INSTANT, reviewed_by_user_id=7)
    ready = {"collector_timezone": "America/New_York", "collector_rules": marker}

    assert problems(ready) == []
    assert len(problems({})) == 2 and len(problems(None)) == 2
    assert problems({"collector_rules": marker}) == ["This library has no valid Collector time zone recorded."]
    assert problems({"collector_timezone": "America/New_York"}) == ["This library's classification lists have not been recorded as reviewed."]
    (invalid_zone,) = problems({"collector_timezone": "Mars/Olympus_Mons", "collector_rules": marker})
    assert "Mars" not in invalid_zone  # a fixed sentence: no settings value is repeated
    # The same two checks a Contract v2 enrollment makes, so "ready here" is "ready there".
    collector_rules.collector_timezone(ready)
    collector_rules.require_rules_reviewed(ready)


def test_adding_an_installation_checks_readiness_only_for_contract_2_and_adds_nothing_when_it_fails():
    source = _source(MANAGE)
    form = source[source.index('with st.form("add_installation_form"):'):]

    assert 'if add_installation and new_name.strip() and new_contract == COLLECTOR_CONTRACT_V2:' in form
    # Read from the ORGANIZATION's own settings, where the enrollment reads them -- not the branch-merged ones.
    assert "contract_two_site_problems(get_collector_site_settings(organization_id))" in form
    assert "get_effective_settings" not in source
    refused = form.index("elif add_installation and contract_problems:")
    created = form.index("create_collector_installation(")
    assert refused < created and "No installation was added." in form[refused:created]
    assert "elif add_installation:" in form[refused:created]  # creation is the LAST branch: reached only with no problem


def test_manage_libraries_explains_that_a_branch_has_one_contract_2_collector():
    source = " ".join(_source(MANAGE).split())

    assert "A branch has ONE Contract v2 \" \"Collector" in source
    assert "will be refused at install time" in source and "Replacing a machine is not supported from this page yet" in source


@PAGES
def test_neither_page_has_any_way_to_change_an_existing_installations_contract(page):
    source = _source(page)

    assert "UPDATE collector_installations" not in source
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "update_collector_installation":
            assert "required_contract" not in {keyword.arg for keyword in node.keywords}
