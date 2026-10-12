"""Super Admin and Contract v2 enrollment (onboarding stage A1): what Provision Library collects for a site, what
Manage Libraries shows, and what NEITHER does yet.

A1 lands before an installer that can enroll on Contract v2. So the pages may COLLECT what a contract-2 enrollment will
need (the Collector's time zone, and the record that the classification lists were reviewed) and SHOW which contract an
installation requires -- but an installation they create is still the legacy contract, deliberately.

The pages are Streamlit scripts and are not imported: the function is extracted from the page source with `ast` and run
for real (as tests/test_super_admin_installation_config.py does), and the rest is asserted against the source.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.services import collector_rules
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
    assert "still a legacy (contract 1) installation" in source
    assert "it does not change how reports are drawn" in source


def test_manage_libraries_shows_the_required_contract_and_offers_no_way_to_choose_it():
    source = _source(MANAGE)

    assert '"required_contract": "Required Contract"' in source and source.count('"Required Contract"') == 2
    for control in ("selectbox", "number_input", "radio", "text_input", "checkbox"):
        for line in source.splitlines():
            if f"st.{control}(" in line:
                assert "contract" not in line.lower(), line


@pytest.mark.parametrize("page", [PROVISION, MANAGE], ids=["Provision Library", "Manage Libraries"])
def test_neither_page_creates_a_contract_2_installation_yet(page):
    source = _source(page)

    assert "create_collector_installation(" in source
    assert "required_contract=" not in source  # the service's default, the legacy contract, is what they get
    assert "COLLECTOR_CONTRACT_V2" not in source
