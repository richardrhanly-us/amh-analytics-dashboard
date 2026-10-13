"""Super Admin and Contract v2 enrollment (onboarding stage A1): what Provision Library collects for a site, what
Manage Libraries shows, and what NEITHER does yet.

A1 landed before an installer that could enroll on Contract v2, so the pages only COLLECTED what a contract-2 enrollment
needs (the Collector's time zone, and the record that the classification lists were reviewed) and SHOWED which contract
an installation requires.

A3a ships that installer (Collector 1.0.14) and adds an explicit CONTRACT SELECTOR to both places an installation is
created -- Provision Library, and Manage Libraries' "Add installation". ITS DEFAULT IS STILL THE LEGACY CONTRACT: nothing
is contract 2 unless an administrator chooses it. The selected value is passed explicitly to
create_collector_installation, a Contract v2 choice is refused for a site that has not been prepared, and an existing
installation's contract can never be changed.

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
    assert 'st.text_input(\n        "Collector time zone (IANA name, e.g. America/New_York)",\n        help=' in source.replace("\r\n", "\n")
    assert "value=False,\n    )\n" in source.replace("\r\n", "\n")
    assert "America/Chicago" not in source
    # Neither key is among the settings a new organization gets without anyone entering them.
    defaults = source[source.index("DEFAULT_ORG_SETTINGS = {"):source.index("DEFAULT_BRANCH_SETTINGS")]
    assert "collector_timezone" not in defaults and "collector_rules" not in defaults
    assert "reviewer_user_id=int(auth_user[\"id\"])" in source and "now=datetime.now(UTC)" in source


def test_the_page_says_what_the_fields_are_for_and_what_they_do_not_change():
    source = " ".join(_source(PROVISION).split())

    assert "Collector Site Settings (Contract v2)" in source
    assert "For a legacy (contract 1) \" \"installation -- still the default below -- leaving these blank changes nothing." in source
    assert "it does not change how reports are drawn" in source


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
def test_both_creation_paths_have_a_contract_selector_whose_default_is_still_the_legacy_contract(page):
    namespace = _page_namespace(page, "collector_contract_label")

    assert namespace["COLLECTOR_CONTRACT_OPTIONS"] == (1, 2)
    assert namespace["DEFAULT_COLLECTOR_CONTRACT"] == 1  # A3a: the default is NOT flipped
    source = _source(page)
    assert source.count("st.selectbox(") >= 1
    selector = source[source.index('"Ingest contract for'):]
    selector = selector[:selector.index("format_func=collector_contract_label")]
    assert "options=list(COLLECTOR_CONTRACT_OPTIONS)" in selector
    assert "index=COLLECTOR_CONTRACT_OPTIONS.index(DEFAULT_COLLECTOR_CONTRACT)" in selector  # the default is the constant, not a literal
    assert "DEFAULT_COLLECTOR_CONTRACT = COLLECTOR_CONTRACT_LEGACY" in source
    assert "DEFAULT_COLLECTOR_CONTRACT = COLLECTOR_CONTRACT_V2" not in source


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

    assert label(1) == "1 - Legacy contract (the default)"
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
    assert "Home branch label" in labels and "Transit destinations" in labels and "examples" in labels


def test_the_contract_2_check_runs_before_anything_is_created_and_only_for_contract_2():
    source = _source(PROVISION)
    check = source.index("if create_installation and installation_contract == COLLECTOR_CONTRACT_V2:")
    block = source[check:source.index("collector_settings = collector_site_settings(")]

    assert "contract_two_provisioning_problems(" in block and "st.error(problem)" in block and "st.stop()" in block
    assert check < source.index("create_organization_with_primary_branch(\n")  # nothing exists yet when it stops
    assert check < source.index("installation = create_collector_installation(")
    # A legacy installation is provisioned exactly as before: none of the three is required for it.
    assert source.count("contract_two_provisioning_problems(") == 2  # its definition and that one guarded call


def test_the_pre_filled_transit_labels_cannot_silently_become_a_contract_2_sites_rules():
    """The form still pre-fills example labels (changing those defaults belongs to the stage that makes contract 2 the
    default). For contract 2 they would become the site's classification rules, so the administrator must confirm
    they are the library's own -- an unticked box, required, with the reason stated."""
    source = _source(PROVISION)

    assert 'value="Westside"' in source and 'value="Library Express"' in source  # unchanged in A3a
    box = source[source.index("collector_routing_labels_confirmed = st.checkbox("):]
    box = box[:box.index("st.subheader(")]
    assert "value=False" in box and "pre-filled with examples" in " ".join(box.split())
    assert "collector_routing_labels_confirmed" in source[source.index("contract_two_provisioning_problems(\n            collector_timezone_input"):][:200]


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
    assert 'get_effective_settings(str(selected_row["organization_slug"]))["settings"]' in form
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
