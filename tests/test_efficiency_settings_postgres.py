"""Reports R6B: Efficiency settings on a REAL PostgreSQL -- the migrated schema, real JSONB, real constraints.

The requests go through the real production routes (TestClient(main.app)) and nothing between a request and the rows is
replaced except the session lookup: the real access and entitlement services, the real
services.efficiency_settings_service and every one of its SQL statements run against the database.

What only a real server can prove:
  * a write sets or removes the `efficiency` key of the stored document and leaves every other key exactly as it was;
  * a settings row that does not exist is created once, and never twice;
  * a write that races a change to ANOTHER key of the same document loses neither;
  * every statement reaches a row only through the acting user's own organization: the same branch slug in two
    organizations, a member who is not an owner or admin, and a suspended or cancelled organization are all refused
    by the SQL itself, not only by the route in front of it;
  * a sorter site is found by the inventory's rule and needs no operational data scope.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_rls_phase1_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server. The module creates its
own throwaway database (migrated with the project's real Alembic chain) and drops it afterward. Production is never
touched.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import main
from services import (
    access_service,
    efficiency_settings_service,
    entitlement_service,
    session_service,
    sorter_inventory_service,
)
from services.efficiency_settings import (
    EfficiencySettingsError,
    OrganizationEfficiencySettings,
    SorterEfficiencySettings,
    validate_organization_efficiency_settings,
    validate_sorter_efficiency_settings,
)

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL efficiency settings tests)"
)

ORIGIN = "https://app.example.invalid"
COOKIE = "__Host-sortview_api_session"
ORG = "/api/organizations/{org}/settings/efficiency"
SORTER = "/api/organizations/{org}/branches/{branch}/settings/efficiency"

# Database ids. Distinctive, so none can be in a response by accident; never sent in a request.
ACME, BETA, PAUSED, CLOSED = 7101, 7202, 7303, 7404
OWNER, ADMIN, VIEWER, BETA_OWNER = 9101, 9102, 9103, 9201
ACME_MAIN, ACME_EAST, ACME_ANNEX, ACME_OLD, ACME_RETIRED, BETA_MAIN, PAUSED_MAIN = 5101, 5102, 5103, 5104, 5105, 5201, 5301

CANARY_HASH = "CANARY-admin-lock-hash-91c2"
ACME_DOCUMENT = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": CANARY_HASH},
    "transit": {"home_branch_label": "Main", "destinations": [{"key": "b1", "label": "Westside", "enabled": True}]},
    "account_settings": {"notes": "a note with \"quotes\" and unicode: é"},
}
MAIN_DOCUMENT = {"branch_name": "Main", "transit": {"home_branch_label": "Main Branch"}}

ORG_RATES = {"labor_rate": "17.56", "manual_items_per_hour": "45.0"}
SORTER_BLOCK = {
    "labor_rate": "20.00",
    "manual_items_per_hour": "50.0",
    "one_time_cost": "118003.92",
    "recurring_annual_cost": "8400.00",
    "in_service_date": "2020-11-20",
}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}
SORTER_NOT_FOUND = {"code": "sorter_not_found", "message": "Sorter not found."}


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


@pytest.fixture(scope="module")
def engine():
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_efficiency_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    database = create_engine(url, hide_parameters=True)
    try:
        migrated = subprocess.run(  # nosec B603
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        yield database
    finally:
        database.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


def _seed(conn) -> None:
    conn.execute(text(
        "TRUNCATE organization_settings, branch_settings, collector_installations, memberships, branches, app_users, "
        "organizations RESTART IDENTITY CASCADE"
    ))
    # No organization or branch here has an operational id: nothing about Efficiency settings needs one.
    for org_id, slug, status in ((ACME, "acme", "active"), (BETA, "beta", "trial"), (PAUSED, "paused", "suspended"), (CLOSED, "closed", "cancelled")):
        conn.execute(
            text("INSERT INTO organizations (id, slug, name, status) VALUES (:id, :slug, :name, :status)"),
            {"id": org_id, "slug": slug, "name": f"{slug.title()} Library", "status": status},
        )
    for user_id in (OWNER, ADMIN, VIEWER, BETA_OWNER):
        conn.execute(text("INSERT INTO app_users (id, email) VALUES (:id, :email)"), {"id": user_id, "email": f"user{user_id}@example.invalid"})
    for org_id, user_id, role in (
        (ACME, OWNER, "owner"), (ACME, ADMIN, "admin"), (ACME, VIEWER, "viewer"), (BETA, BETA_OWNER, "owner"),
        # OWNER also owns the suspended and the cancelled organization.
        (PAUSED, OWNER, "owner"), (CLOSED, OWNER, "owner"),
    ):
        conn.execute(text("INSERT INTO memberships (organization_id, user_id, role) VALUES (:org, :user, :role)"), {"org": org_id, "user": user_id, "role": role})
    # Both organizations have a branch called "main".
    for branch_id, org_id, slug, status in (
        (ACME_MAIN, ACME, "main", "active"), (ACME_EAST, ACME, "east", "active"), (ACME_ANNEX, ACME, "annex", "active"),
        (ACME_OLD, ACME, "old", "inactive"), (ACME_RETIRED, ACME, "retired-site", "active"), (BETA_MAIN, BETA, "main", "active"),
        (PAUSED_MAIN, PAUSED, "main", "active"),
    ):
        conn.execute(
            text("INSERT INTO branches (id, organization_id, slug, name, status) VALUES (:id, :org, :slug, :name, :status)"),
            {"id": branch_id, "org": org_id, "slug": slug, "name": slug.title(), "status": status},
        )
    # Annex hosts no sorter. "old" hosts one at a branch that is no longer active; "retired-site" only a retired one.
    for org_id, branch_id, status in (
        (ACME, ACME_MAIN, "active"), (ACME, ACME_MAIN, "inactive"), (ACME, ACME_EAST, "provisioning"), (ACME, ACME_OLD, "active"),
        (ACME, ACME_RETIRED, "retired"), (BETA, BETA_MAIN, "active"), (PAUSED, PAUSED_MAIN, "active"),
    ):
        conn.execute(
            text("INSERT INTO collector_installations (organization_id, branch_id, name, status) VALUES (:org, :branch, 'AMH', :status)"),
            {"org": org_id, "branch": branch_id, "status": status},
        )
    conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:org, CAST(:doc AS JSONB))"), {"org": ACME, "doc": json.dumps(ACME_DOCUMENT)})
    conn.execute(text("INSERT INTO branch_settings (branch_id, settings_json) VALUES (:branch, CAST(:doc AS JSONB))"), {"branch": ACME_MAIN, "doc": json.dumps(MAIN_DOCUMENT)})


@pytest.fixture
def db(engine, monkeypatch):
    with engine.begin() as conn:
        _seed(conn)
    for module in (access_service, entitlement_service, efficiency_settings_service, sorter_inventory_service):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(
        session_service,
        "validate_session",
        lambda raw_token: {"id": int(raw_token), "email": f"user{raw_token}@example.invalid", "full_name": "Test User"} if raw_token.isdigit() else None,
    )
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", ORIGIN)
    return engine


@pytest.fixture
def api(db):
    main.limiter.reset()
    yield TestClient(main.app)
    main.limiter.reset()


def _headers(user: int) -> dict[str, str]:
    return {"Cookie": f"{COOKIE}={user}", "Origin": ORIGIN}


def _put(api, path, body, user=OWNER):
    return api.put(path, json=body, headers=_headers(user))


def _get(api, path, user=OWNER):
    return api.get(path, headers={"Cookie": f"{COOKIE}={user}"})


def _documents(engine, table: str, key: str) -> dict[int, dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT {key} AS owner, settings_json FROM {table} ORDER BY id")).all()  # nosec B608 - test constants
    assert len({row.owner for row in rows}) == len(rows), "a settings row was duplicated"
    return {row.owner: row.settings_json for row in rows}


def _organizations(engine) -> dict[int, dict]:
    return _documents(engine, "organization_settings", "organization_id")


def _branches(engine) -> dict[int, dict]:
    return _documents(engine, "branch_settings", "branch_id")


def _org_settings(body: dict) -> OrganizationEfficiencySettings:
    return validate_organization_efficiency_settings(body)


def _sorter_settings(body: dict) -> SorterEfficiencySettings:
    return validate_sorter_efficiency_settings(body, today=date(2026, 10, 6))


# =====================================================================================================================
# Organization
# =====================================================================================================================

def test_an_organization_put_sets_the_efficiency_key_and_leaves_every_other_key_exactly_as_it_was(api, db):
    response = _put(api, ORG.format(org="acme"), {"labor_rate": "17.56", "manual_items_per_hour": "45"})

    assert response.status_code == 200, response.text
    assert response.json() == {"efficiency": ORG_RATES}
    assert _organizations(db) == {ACME: {**ACME_DOCUMENT, "efficiency": ORG_RATES}}
    assert _get(api, ORG.format(org="acme")).json() == {"efficiency": ORG_RATES}
    assert CANARY_HASH not in response.text
    # Nothing else was written anywhere.
    assert _branches(db) == {ACME_MAIN: MAIN_DOCUMENT}


def test_the_stored_values_are_json_text_never_json_numbers(api, db):
    _put(api, ORG.format(org="acme"), ORG_RATES)
    _put(api, SORTER.format(org="acme", branch="main"), SORTER_BLOCK)

    with db.connect() as conn:
        types = conn.execute(text("""
            SELECT 'org.' || key AS field, jsonb_typeof(value) AS type
            FROM organization_settings, jsonb_each(settings_json -> 'efficiency')
            UNION ALL
            SELECT 'sorter.' || key, jsonb_typeof(value)
            FROM branch_settings, jsonb_each(settings_json -> 'efficiency')
        """)).all()

    assert len(types) == 7
    assert {row.type for row in types} == {"string"}


def test_an_organization_with_no_settings_row_gets_one_holding_the_block_alone_and_only_one(api, db):
    path = ORG.format(org="beta")
    assert _get(api, path, BETA_OWNER).json() == {"efficiency": {"labor_rate": None, "manual_items_per_hour": None}}
    assert BETA not in _organizations(db)

    first = _put(api, path, {"labor_rate": "25.00"}, BETA_OWNER)
    second = _put(api, path, ORG_RATES, BETA_OWNER)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == {"efficiency": {"labor_rate": "25.00", "manual_items_per_hour": None}}
    assert _organizations(db) == {ACME: ACME_DOCUMENT, BETA: {"efficiency": ORG_RATES}}


def test_replacing_the_block_clears_a_field_that_is_left_out_and_keeps_the_rest_of_the_document(api, db):
    path = ORG.format(org="acme")
    _put(api, path, ORG_RATES)

    assert _put(api, path, {"manual_items_per_hour": "47.1"}).json() == {"efficiency": {"labor_rate": None, "manual_items_per_hour": "47.1"}}
    assert _organizations(db)[ACME] == {**ACME_DOCUMENT, "efficiency": {"manual_items_per_hour": "47.1"}}


def test_a_put_of_nothing_removes_the_key_and_only_the_key_and_creates_no_row(api, db):
    _put(api, ORG.format(org="acme"), ORG_RATES)

    assert _put(api, ORG.format(org="acme"), {}).json() == {"efficiency": {"labor_rate": None, "manual_items_per_hour": None}}
    assert _put(api, ORG.format(org="beta"), {}, BETA_OWNER).status_code == 200

    assert _organizations(db) == {ACME: ACME_DOCUMENT}
    # And it can be set again afterwards.
    assert _put(api, ORG.format(org="acme"), ORG_RATES).json() == {"efficiency": ORG_RATES}


def test_a_write_bumps_the_rows_updated_at(api, db):
    with db.begin() as conn:
        conn.execute(text("UPDATE organization_settings SET updated_at = '2020-01-01T00:00:00Z' WHERE organization_id = :org"), {"org": ACME})

    _put(api, ORG.format(org="acme"), ORG_RATES)

    with db.connect() as conn:
        assert conn.execute(text("SELECT updated_at > '2026-01-01' FROM organization_settings WHERE organization_id = :org"), {"org": ACME}).scalar()


# =====================================================================================================================
# Sorter
# =====================================================================================================================

def test_a_sorter_put_sets_the_key_in_its_host_branchs_document_and_touches_nothing_else(api, db):
    _put(api, ORG.format(org="acme"), ORG_RATES)

    response = _put(api, SORTER.format(org="acme", branch="main"), SORTER_BLOCK)

    assert response.status_code == 200, response.text
    assert response.json()["efficiency"]["labor_rate"] == {"organization": "17.56", "sorter": "20.00", "effective": "20.00", "source": "sorter"}
    assert _branches(db) == {ACME_MAIN: {**MAIN_DOCUMENT, "efficiency": SORTER_BLOCK}}
    assert _organizations(db) == {ACME: {**ACME_DOCUMENT, "efficiency": ORG_RATES}}


def test_a_sorter_with_no_settings_row_and_no_operational_scope_can_still_be_configured(api, db):
    path = SORTER.format(org="acme", branch="east")
    _put(api, ORG.format(org="acme"), ORG_RATES)
    with db.connect() as conn:
        unmapped = conn.execute(
            text("SELECT o.operational_customer_id, b.operational_branch_id FROM branches b JOIN organizations o ON o.id = b.organization_id WHERE b.id = :id"),
            {"id": ACME_EAST},
        ).one()
    assert tuple(unmapped) == (None, None)

    before = _get(api, path)
    first = _put(api, path, {"one_time_cost": "0.00"})
    second = _put(api, path, {"one_time_cost": "0.00", "recurring_annual_cost": "8400"})

    assert before.json()["efficiency"]["labor_rate"] == {"organization": "17.56", "sorter": None, "effective": "17.56", "source": "organization"}
    assert before.json()["efficiency"]["one_time_cost"] is None
    assert first.json()["efficiency"]["one_time_cost"] == "0.00"
    assert second.json()["efficiency"]["recurring_annual_cost"] == "8400.00"
    assert _branches(db) == {ACME_MAIN: MAIN_DOCUMENT, ACME_EAST: {"efficiency": {"one_time_cost": "0.00", "recurring_annual_cost": "8400.00"}}}


def test_clearing_a_sorters_block_removes_the_key_and_keeps_the_rest(api, db):
    path = SORTER.format(org="acme", branch="main")
    _put(api, path, SORTER_BLOCK)

    cleared = _put(api, path, {})

    assert cleared.json()["efficiency"]["one_time_cost"] is None
    assert _branches(db) == {ACME_MAIN: MAIN_DOCUMENT}
    assert _put(api, SORTER.format(org="acme", branch="east"), {}).status_code == 200
    assert ACME_EAST not in _branches(db)


def test_a_sorter_site_is_found_by_the_inventorys_rule(api, db):
    listed = {site.slug for site in sorter_inventory_service.list_sorter_sites("acme")}
    assert listed == {"main", "east"}

    for branch in ("main", "east"):
        assert _get(api, SORTER.format(org="acme", branch=branch)).status_code == 200
    # No sorter there; a branch that is no longer active; only a retired installation; no such branch.
    for branch in ("annex", "old", "retired-site", "nowhere"):
        path = SORTER.format(org="acme", branch=branch)
        assert (_get(api, path).status_code, _get(api, path).json()) == (404, SORTER_NOT_FOUND)
        assert _put(api, path, SORTER_BLOCK).json() == SORTER_NOT_FOUND
    assert _branches(db) == {ACME_MAIN: MAIN_DOCUMENT}


def test_a_cost_in_the_organizations_stored_block_is_never_a_sorters(api, db):
    with db.begin() as conn:
        conn.execute(
            text("UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{efficiency}', CAST(:block AS JSONB)) WHERE organization_id = :org"),
            {"org": ACME, "block": json.dumps({**ORG_RATES, "one_time_cost": "100000.00", "in_service_date": "2019-01-01"})},
        )

    efficiency = _get(api, SORTER.format(org="acme", branch="east")).json()["efficiency"]

    assert (efficiency["one_time_cost"], efficiency["in_service_date"]) == (None, None)
    assert efficiency["labor_rate"]["effective"] == "17.56"


# =====================================================================================================================
# Whose row a statement can reach
# =====================================================================================================================

def test_the_same_branch_slug_in_two_organizations_is_two_sorters_and_each_owner_reaches_only_their_own(api, db):
    _put(api, SORTER.format(org="acme", branch="main"), SORTER_BLOCK, OWNER)
    _put(api, SORTER.format(org="beta", branch="main"), {"one_time_cost": "5.00"}, BETA_OWNER)

    assert _branches(db) == {ACME_MAIN: {**MAIN_DOCUMENT, "efficiency": SORTER_BLOCK}, BETA_MAIN: {"efficiency": {"one_time_cost": "5.00"}}}
    assert _get(api, SORTER.format(org="beta", branch="main"), BETA_OWNER).json()["efficiency"]["one_time_cost"] == "5.00"
    assert _get(api, SORTER.format(org="acme", branch="main"), OWNER).json()["efficiency"]["one_time_cost"] == "118003.92"


def test_one_organizations_owner_cannot_read_or_write_anothers_through_the_routes(api, db):
    _put(api, ORG.format(org="beta"), ORG_RATES, BETA_OWNER)
    before = (_organizations(db), _branches(db))

    for path in (ORG.format(org="beta"), SORTER.format(org="beta", branch="main")):
        read, written = _get(api, path, OWNER), _put(api, path, {"labor_rate": "99.00"}, OWNER)
        assert (read.status_code, read.json()) == (404, ORGANIZATION_NOT_FOUND)
        assert (written.status_code, written.json()) == (404, ORGANIZATION_NOT_FOUND)
        assert "17.56" not in read.text

    assert (_organizations(db), _branches(db)) == before


def test_the_sql_itself_refuses_anyone_who_is_not_an_owner_or_admin_of_that_organization(db):
    """Past the routes: the service called directly, as if a route had forgotten to check."""
    service = efficiency_settings_service
    with db.begin() as conn:
        conn.execute(
            text("UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{efficiency}', CAST(:block AS JSONB)) WHERE organization_id = :org"),
            {"org": ACME, "block": json.dumps(ORG_RATES)},
        )
    before = (_organizations(db), _branches(db))
    wanted_org, wanted_sorter = _org_settings({"labor_rate": "99.00"}), _sorter_settings({"one_time_cost": "99.00"})

    # Another organization's owner, a viewer of this one, and a user id that does not exist.
    for user_id in (BETA_OWNER, VIEWER, 424242):
        assert service.read_organization_efficiency("acme", user_id=user_id) is None
        assert service.read_sorter_efficiency("acme", "main", user_id=user_id) is None
        assert service.replace_organization_efficiency("acme", wanted_org, user_id=user_id) is None
        assert service.replace_organization_efficiency("acme", _org_settings({}), user_id=user_id) is None
        assert service.replace_sorter_efficiency("acme", "main", wanted_sorter, user_id=user_id) is None
        assert service.replace_sorter_efficiency("acme", "main", _sorter_settings({}), user_id=user_id) is None
        assert service.replace_sorter_efficiency("acme", "east", wanted_sorter, user_id=user_id) is None

    assert (_organizations(db), _branches(db)) == before
    # While the organization's own owner and admin are answered.
    for user_id in (OWNER, ADMIN):
        assert service.read_organization_efficiency("acme", user_id=user_id) == _org_settings(ORG_RATES)


def test_the_write_statements_themselves_match_no_row_for_the_wrong_user_or_organization(db):
    """Each write statement on its own, without the scope check the service runs first."""
    service = efficiency_settings_service
    block = json.dumps({"labor_rate": "99.00"})
    before = (_organizations(db), _branches(db))

    with db.begin() as conn:
        for user_id, org_slug in ((BETA_OWNER, "acme"), (VIEWER, "acme"), (OWNER, "beta"), (OWNER, "paused"), (OWNER, "closed"), (OWNER, "nowhere")):
            scope = {"org_slug": org_slug, "user_id": user_id}
            assert conn.execute(service._SET_ORGANIZATION_SQL, {**scope, "block": block}).rowcount == 0
            assert conn.execute(service._CLEAR_ORGANIZATION_SQL, scope).rowcount == 0
            assert conn.execute(service._SET_SORTER_SQL, {**scope, "branch_slug": "main", "block": block}).rowcount == 0
            assert conn.execute(service._CLEAR_SORTER_SQL, {**scope, "branch_slug": "main"}).rowcount == 0
        for branch_slug in ("annex", "old", "retired-site", "nowhere"):
            scope = {"org_slug": "acme", "user_id": OWNER, "branch_slug": branch_slug}
            assert conn.execute(service._SET_SORTER_SQL, {**scope, "block": block}).rowcount == 0
            assert conn.execute(service._CLEAR_SORTER_SQL, scope).rowcount == 0

    assert (_organizations(db), _branches(db)) == before


def test_a_viewer_is_refused_by_the_route_and_nothing_is_read_or_written(api, db):
    before = (_organizations(db), _branches(db))

    for path in (ORG.format(org="acme"), SORTER.format(org="acme", branch="main")):
        assert _get(api, path, VIEWER).status_code == 403
        assert _put(api, path, {"labor_rate": "99.00"}, VIEWER).status_code == 403
    assert _put(api, ORG.format(org="acme"), ORG_RATES, ADMIN).status_code == 200

    assert _branches(db) == before[1]
    assert _organizations(db)[ACME]["efficiency"] == ORG_RATES


def test_a_suspended_organization_can_be_read_and_not_written_by_the_route_or_by_the_sql(api, db):
    with db.begin() as conn:
        conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:org, CAST(:doc AS JSONB))"), {"org": PAUSED, "doc": json.dumps({"efficiency": ORG_RATES})})
    before = (_organizations(db), _branches(db))

    assert _get(api, ORG.format(org="paused")).json() == {"efficiency": ORG_RATES}
    assert _get(api, SORTER.format(org="paused", branch="main")).json()["efficiency"]["labor_rate"]["effective"] == "17.56"
    for path in (ORG.format(org="paused"), SORTER.format(org="paused", branch="main")):
        refused = _put(api, path, {"labor_rate": "99.00"})
        assert (refused.status_code, refused.json()["code"]) == (403, "organization_read_only")
    assert efficiency_settings_service.replace_organization_efficiency("paused", _org_settings({"labor_rate": "99.00"}), user_id=OWNER) is None
    assert efficiency_settings_service.replace_sorter_efficiency("paused", "main", _sorter_settings({"one_time_cost": "1.00"}), user_id=OWNER) is None

    assert (_organizations(db), _branches(db)) == before


def test_a_cancelled_organization_is_not_found_even_for_its_owner(api, db):
    assert _get(api, ORG.format(org="closed")).json() == ORGANIZATION_NOT_FOUND
    assert _put(api, ORG.format(org="closed"), ORG_RATES).json() == ORGANIZATION_NOT_FOUND
    assert efficiency_settings_service.read_organization_efficiency("closed", user_id=OWNER) is None
    assert CLOSED not in _organizations(db)


def test_no_database_id_reaches_any_answer(api, db):
    _put(api, ORG.format(org="acme"), ORG_RATES)

    for response in (
        _get(api, ORG.format(org="acme")), _get(api, SORTER.format(org="acme", branch="main")), _put(api, SORTER.format(org="acme", branch="main"), SORTER_BLOCK),
    ):
        for number in (ACME, OWNER, ACME_MAIN):
            assert str(number) not in response.text.replace("118003.92", "")


# =====================================================================================================================
# Two writers, one document
# =====================================================================================================================

def _concurrently(db, other_writer_sql: str, parameters: dict, write) -> None:
    """Holds another writer's change to the same row uncommitted, starts `write`, then commits the other one."""
    failures: list[BaseException] = []

    def run():
        try:
            write()
        except BaseException as error:  # reported to the test below
            failures.append(error)

    other = db.connect()
    transaction = other.begin()
    try:
        other.execute(text(other_writer_sql), parameters)
        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.5)
        # The efficiency write is waiting on the row the other writer holds: it has not gone ahead on a stale copy.
        assert thread.is_alive()
        transaction.commit()
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        other.close()
    assert failures == []


def test_an_organization_write_and_a_concurrent_change_to_another_key_both_survive(db):
    _concurrently(
        db,
        "UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{transit,home_branch_label}', '\"Renamed\"') WHERE organization_id = :org",
        {"org": ACME},
        lambda: efficiency_settings_service.replace_organization_efficiency("acme", _org_settings(ORG_RATES), user_id=OWNER),
    )

    stored = _organizations(db)[ACME]
    assert stored["efficiency"] == ORG_RATES
    assert stored["transit"]["home_branch_label"] == "Renamed"
    assert stored["security"] == ACME_DOCUMENT["security"]


def test_a_sorter_write_and_a_concurrent_change_to_another_key_both_survive(db):
    _concurrently(
        db,
        "UPDATE branch_settings SET settings_json = jsonb_set(settings_json, '{branch_name}', '\"Central\"') WHERE branch_id = :branch",
        {"branch": ACME_MAIN},
        lambda: efficiency_settings_service.replace_sorter_efficiency("acme", "main", _sorter_settings(SORTER_BLOCK), user_id=OWNER),
    )

    assert _branches(db)[ACME_MAIN] == {**MAIN_DOCUMENT, "branch_name": "Central", "efficiency": SORTER_BLOCK}


def test_clearing_the_block_and_a_concurrent_change_to_another_key_both_survive(db):
    efficiency_settings_service.replace_organization_efficiency("acme", _org_settings(ORG_RATES), user_id=OWNER)

    _concurrently(
        db,
        "UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{library_name}', '\"Acme Public Library\"') WHERE organization_id = :org",
        {"org": ACME},
        lambda: efficiency_settings_service.replace_organization_efficiency("acme", _org_settings({}), user_id=OWNER),
    )

    assert _organizations(db)[ACME] == {**ACME_DOCUMENT, "library_name": "Acme Public Library"}


def test_two_first_writes_for_one_organization_make_one_row(db):
    failures: list[BaseException] = []

    def write(rate: str):
        try:
            efficiency_settings_service.replace_organization_efficiency("beta", _org_settings({"labor_rate": rate}), user_id=BETA_OWNER)
        except BaseException as error:  # reported to the test below
            failures.append(error)

    threads = [threading.Thread(target=write, args=(f"{rate}.00",)) for rate in range(20, 28)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert failures == []
    stored = _organizations(db)  # asserts there is no duplicate row
    assert set(stored[BETA]) == {"efficiency"}
    assert stored[BETA]["efficiency"]["labor_rate"] in {f"{rate}.00" for rate in range(20, 28)}


# =====================================================================================================================
# What is stored but malformed
# =====================================================================================================================

def test_a_malformed_stored_block_is_a_contained_error_and_is_repaired_by_a_put(api, db):
    with db.begin() as conn:
        conn.execute(
            text("UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{efficiency}', '{\"labor_rate\": 17.56}') WHERE organization_id = :org"),
            {"org": ACME},
        )
    before = _branches(db)

    for response in (_get(api, ORG.format(org="acme")), _get(api, SORTER.format(org="acme", branch="main")), _put(api, SORTER.format(org="acme", branch="main"), SORTER_BLOCK)):
        assert (response.status_code, response.json()["code"]) == (500, "efficiency_settings_invalid")
    # The sorter was not written to: the transaction that found the organization's block unreadable wrote nothing.
    assert _branches(db) == before
    with pytest.raises(EfficiencySettingsError):
        efficiency_settings_service.read_organization_efficiency("acme", user_id=OWNER)

    assert _put(api, ORG.format(org="acme"), ORG_RATES).json() == {"efficiency": ORG_RATES}
    assert _put(api, SORTER.format(org="acme", branch="main"), SORTER_BLOCK).status_code == 200
    assert _organizations(db)[ACME] == {**ACME_DOCUMENT, "efficiency": ORG_RATES}
