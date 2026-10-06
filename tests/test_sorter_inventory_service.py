"""F5.6: an organization's sorting machines, from its registered Collector
installations.

    sorter_sites(installations)      the pure rule: which installations count, and one site per host branch
    list_sorter_sites(org_slug)      the read, for one organization

The read runs its REAL SQL against an in-memory SQLite database holding
organizations, branches and collector_installations, with rows written by the
REAL services.tenant_service.create_collector_installation and
update_collector_installation where SQLite can run them.

Imported the "flat" way (services.sorter_inventory_service), the identity the
API process uses.
"""

from __future__ import annotations

import dataclasses
import inspect
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import main
from services import sorter_inventory_service, tenant_service
from services.sorter_inventory_service import (
    REPORTING_STATUSES,
    VISIBLE_STATUSES,
    SorterSite,
    list_sorter_sites,
    sorter_sites,
)


def _row(name, status, branch_slug="main", branch_name="Main Library") -> dict:
    return {"name": name, "status": status, "branch_slug": branch_slug, "branch_name": branch_name}


# =====================================================================================================================
# sorter_sites: the pure rule
# =====================================================================================================================

def test_one_installation_is_one_sorter_site_addressed_by_its_host_branch():
    assert sorter_sites([_row("Main Library AMH", "active")]) == [
        SorterSite(slug="main", name="Main Library AMH", host_branch_slug="main", host_branch_name="Main Library",
                   status="active", collector_count=1),
    ]


def test_no_installations_is_no_sorters():
    assert sorter_sites([]) == []


@pytest.mark.parametrize(("status", "collectors"), [("active", 1), ("provisioning", 1), ("inactive", 0)])
def test_every_visible_status_is_listed_with_its_own_status(status, collectors):
    (site,) = sorter_sites([_row("Sorter", status)])

    assert (site.status, site.collector_count) == (status, collectors)


@pytest.mark.parametrize("status", ["retired", "RETIRED", "deleted", "", None, "Active"])
def test_a_retired_or_unknown_status_is_never_a_sorter(status):
    assert sorter_sites([_row("Sorter", status)]) == []


def test_sites_at_different_branches_are_separate_and_keep_the_order_given():
    sites = sorter_sites([
        _row("Central Library AMH", "active", "central", "Central Library"),
        _row("East Branch AMH", "active", "east", "East Branch"),
        _row("North Kiosk Sorter", "provisioning", "north", "North"),
    ])

    assert [(site.slug, site.name, site.host_branch_name) for site in sites] == [
        ("central", "Central Library AMH", "Central Library"),
        ("east", "East Branch AMH", "East Branch"),
        ("north", "North Kiosk Sorter", "North"),
    ]
    assert len({site.slug for site in sites}) == 3


def test_two_active_installations_at_one_branch_are_one_site_marked_as_two_collectors():
    """Their events cannot be told apart, so they are not offered as two dashboards."""
    sites = sorter_sites([_row("AMH 1", "active"), _row("AMH 2", "active")])

    assert sites == [SorterSite(slug="main", name="AMH 1", host_branch_slug="main", host_branch_name="Main Library",
                                status="active", collector_count=2)]


def test_a_replaced_machine_left_inactive_does_not_name_the_site_or_count_as_a_collector():
    sites = sorter_sites([_row("Old sorter PC", "inactive"), _row("New sorter PC", "active")])

    assert [(site.name, site.status, site.collector_count) for site in sites] == [("New sorter PC", "active", 1)]


def test_the_lead_installation_is_the_most_alive_then_the_earliest_registered():
    assert sorter_sites([_row("c", "inactive"), _row("b", "provisioning"), _row("a", "active")])[0].name == "a"
    assert sorter_sites([_row("b", "provisioning"), _row("c", "inactive")])[0].name == "b"
    assert sorter_sites([_row("first", "active"), _row("second", "active")])[0].name == "first"
    assert sorter_sites([_row("only", "inactive"), _row("gone", "retired")])[0].name == "only"


def test_a_retired_installation_beside_a_live_one_is_not_counted():
    (site,) = sorter_sites([_row("Old", "retired"), _row("New", "active"), _row("Spare", "provisioning")])

    assert (site.name, site.collector_count) == ("New", 2)


def test_a_site_whose_only_installations_are_inactive_has_no_reporting_collector():
    (site,) = sorter_sites([_row("Stored sorter", "inactive"), _row("Another", "inactive")])

    assert (site.status, site.collector_count) == ("inactive", 0)


def test_a_blank_name_falls_back_to_the_host_branch_name_and_a_name_is_trimmed():
    assert sorter_sites([_row("   ", "active")])[0].name == "Main Library"
    assert sorter_sites([_row(None, "active")])[0].name == "Main Library"
    assert sorter_sites([_row("  Main Library AMH ", "active")])[0].name == "Main Library AMH"


def test_the_site_slug_survives_renaming_the_machine():
    before = sorter_sites([_row("Main Library AMH", "active")])[0]
    after = sorter_sites([_row("Main Library - Tech Logic UltraSort #2", "active")])[0]

    assert before.slug == after.slug == "main"


def test_the_status_sets_are_the_ones_the_rest_of_the_system_uses():
    assert set(VISIBLE_STATUSES) == set(tenant_service.COLLECTOR_INSTALLATION_STATUSES) - {"retired"}
    assert set(REPORTING_STATUSES) == set(main.INSTALLATION_HEARTBEAT_STATUSES)
    assert set(REPORTING_STATUSES) <= set(VISIBLE_STATUSES)


def test_a_site_has_exactly_six_fields_and_none_is_an_identifier_or_a_secret():
    assert [field.name for field in dataclasses.fields(SorterSite)] == [
        "slug", "name", "host_branch_slug", "host_branch_name", "status", "collector_count",
    ]


# =====================================================================================================================
# list_sorter_sites: the read
# =====================================================================================================================

_SCHEMA = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, name TEXT, status TEXT, operational_customer_id INTEGER)",
    (
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, name TEXT, "
        "is_primary BOOLEAN, status TEXT, operational_branch_id INTEGER)"
    ),
    (
        "CREATE TABLE collector_installations (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, "
        "name TEXT, hostname TEXT, collector_version TEXT, status TEXT, installed_at TEXT, last_seen_at TEXT, "
        "created_at TEXT, updated_at TEXT)"
    ),
    (
        "INSERT INTO organizations (id, slug, name, status) VALUES (1, 'acme', 'Acme', 'active'), "
        "(2, 'beta', 'Beta', 'active'), (3, 'paused', 'Paused', 'suspended')"
    ),
    (
        "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) VALUES "
        "(11, 1, 'main', 'Main Library', 1, 'active', 11), "
        "(12, 1, 'westside', 'Westside', 0, 'active', 12), "         # a branch, and a routing destination: no sorter
        "(13, 1, 'east', 'East Branch', 0, 'active', 13), "
        "(14, 1, 'closed', 'Closed Branch', 0, 'inactive', 14), "
        "(21, 2, 'main', 'Beta Main', 1, 'active', 21), "            # the same slug, in another organization
        "(31, 3, 'main', 'Paused Main', 1, 'active', 31)"
    ),
)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in _SCHEMA:
            conn.execute(text(statement))
    monkeypatch.setattr(sorter_inventory_service, "get_engine", lambda: engine)
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def _install(db, organization_id, branch_id, name, status="active", hostname="CANARY-HOST-01", version="9.9.9-canary"):
    """Registers an installation. Created by the real admin service; a status
    other than the one it was created with is then written directly, as an
    administrator's later edit would leave it."""
    created = tenant_service.create_collector_installation(
        organization_id, branch_id, name, hostname=hostname, collector_version=version, status="provisioning",
    )
    with db.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = :s WHERE id = :i"), {"s": status, "i": created["id"]})
    return created["id"]


def test_a_registered_installation_is_the_organizations_sorter(db):
    _install(db, 1, 11, "Main Library AMH")

    assert list_sorter_sites("acme") == [
        SorterSite(slug="main", name="Main Library AMH", host_branch_slug="main", host_branch_name="Main Library",
                   status="active", collector_count=1),
    ]


def test_an_organization_with_branches_but_no_installation_has_no_sorters(db):
    assert list_sorter_sites("acme") == []


def test_a_branch_with_no_installation_is_not_a_sorter(db):
    _install(db, 1, 11, "Main Library AMH")

    slugs = [site.slug for site in list_sorter_sites("acme")]

    # Westside and East are branches of the organization; neither has a machine.
    assert slugs == ["main"]


def test_a_routing_destination_is_not_a_sorter_however_it_is_configured(db):
    """Settings are not even read: the inventory has no way to learn of a destination."""
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE organization_settings (organization_id INTEGER, settings_json TEXT)"))
        conn.execute(
            text("INSERT INTO organization_settings VALUES (1, :settings)"),
            {"settings": json.dumps({"transit": {"destinations": [{"label": "Westside"}, {"label": "Outreach"}]}})},
        )
    _install(db, 1, 11, "Main Library AMH")

    assert [site.name for site in list_sorter_sites("acme")] == ["Main Library AMH"]
    assert "settings" not in str(sorter_inventory_service._INSTALLATIONS_SQL)


def test_one_sorter_at_each_of_several_branches_lists_the_primary_branch_first_then_by_branch_name(db):
    _install(db, 1, 12, "Westside AMH")
    _install(db, 1, 13, "East Branch AMH")
    _install(db, 1, 11, "Main Library AMH")

    assert [(site.slug, site.name, site.host_branch_name) for site in list_sorter_sites("acme")] == [
        ("main", "Main Library AMH", "Main Library"),
        ("east", "East Branch AMH", "East Branch"),
        ("westside", "Westside AMH", "Westside"),
    ]


def test_two_installations_at_one_branch_are_one_site_and_the_earlier_registered_names_it(db):
    _install(db, 1, 11, "AMH 1")
    _install(db, 1, 11, "AMH 2")

    assert list_sorter_sites("acme") == [
        SorterSite(slug="main", name="AMH 1", host_branch_slug="main", host_branch_name="Main Library",
                   status="active", collector_count=2),
    ]


@pytest.mark.parametrize(("status", "listed"), [("active", True), ("provisioning", True), ("inactive", True), ("retired", False)])
def test_each_installation_status(db, status, listed):
    _install(db, 1, 11, "Main Library AMH", status=status)

    sites = list_sorter_sites("acme")

    assert [site.status for site in sites] == ([status] if listed else [])


def test_a_retired_machine_and_its_replacement_are_one_live_sorter(db):
    _install(db, 1, 11, "Old sorter", status="retired")
    _install(db, 1, 11, "New sorter")

    assert [(site.name, site.collector_count) for site in list_sorter_sites("acme")] == [("New sorter", 1)]


def test_an_installation_at_an_inactive_branch_is_not_listed(db):
    _install(db, 1, 14, "Closed Branch AMH")
    _install(db, 1, 11, "Main Library AMH")

    assert [site.slug for site in list_sorter_sites("acme")] == ["main"]


def test_another_organizations_installations_are_never_listed(db):
    _install(db, 1, 11, "Acme Main AMH")
    _install(db, 2, 21, "Beta Main AMH")

    assert [site.name for site in list_sorter_sites("acme")] == ["Acme Main AMH"]
    assert [site.name for site in list_sorter_sites("beta")] == ["Beta Main AMH"]
    # The two share a branch slug; each organization's site is its own.
    assert list_sorter_sites("acme")[0].host_branch_name == "Main Library"
    assert list_sorter_sites("beta")[0].host_branch_name == "Beta Main"


def test_an_installation_row_pointing_at_another_organizations_branch_matches_nothing(db):
    with db.begin() as conn:
        conn.execute(text(
            "INSERT INTO collector_installations (organization_id, branch_id, name, status) VALUES (1, 21, 'Crossed', 'active')"
        ))

    assert list_sorter_sites("acme") == []
    assert list_sorter_sites("beta") == []


def test_the_real_admin_service_refuses_to_register_a_machine_at_another_organizations_branch(db):
    with pytest.raises(RuntimeError):
        tenant_service.create_collector_installation(1, 21, "Crossed")


def test_a_suspended_organizations_sorters_are_still_listed(db):
    _install(db, 3, 31, "Paused Main AMH")

    assert [site.name for site in list_sorter_sites("paused")] == ["Paused Main AMH"]


@pytest.mark.parametrize("slug", ["no-such-org", "", "ACME", "acme ", "acme' OR '1'='1"])
def test_an_organization_that_does_not_exist_has_no_sorters(db, slug):
    _install(db, 1, 11, "Main Library AMH")

    assert list_sorter_sites(slug) == []


def test_nothing_about_the_machine_itself_is_read(db):
    _install(db, 1, 11, "Main Library AMH", hostname="CANARY-HOST-01", version="9.9.9-canary")

    (site,) = list_sorter_sites("acme")

    assert "CANARY" not in repr(site) and "9.9.9" not in repr(site)
    sql = " ".join(str(sorter_inventory_service._INSTALLATIONS_SQL).split())
    selected = sql.split(" FROM ")[0]
    assert selected == "SELECT ci.name AS name, ci.status AS status, b.slug AS branch_slug, b.name AS branch_name"
    for forbidden in ("hostname", "collector_version", "token", "enrollment", "last_seen_at", "ci.id AS", "customer"):
        assert forbidden not in selected


def test_the_read_is_one_statement_bound_to_the_organization_slug_only(db):
    sql = " ".join(str(sorter_inventory_service._INSTALLATIONS_SQL).split())

    assert "WHERE o.slug = :org_slug AND b.status = 'active' AND ci.status IN ('active', 'provisioning', 'inactive')" in sql
    assert "JOIN branches b ON b.id = ci.branch_id AND b.organization_id = o.id" in sql
    assert sql.count(":") == 1
    for forbidden in ("checkins", "checkin_events", "ingest_key_ids", "agent_tokens", "INSERT", "UPDATE", "DELETE"):
        assert forbidden not in sql


def test_the_module_needs_neither_streamlit_nor_pandas_and_changes_nothing():
    source = inspect.getsource(sorter_inventory_service)
    imports = [line.strip() for line in source.splitlines() if line.startswith(("import ", "from "))]

    assert imports == [
        "from __future__ import annotations",
        "from collections.abc import Iterable, Mapping",
        "from dataclasses import dataclass",
        "from typing import Any",
        "from sqlalchemy import text",
        "from database import get_engine",
    ]
    assert ".begin(" not in source and "commit" not in source
