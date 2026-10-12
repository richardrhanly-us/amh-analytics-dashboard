"""Reports R10E: SortView Value settings on a REAL PostgreSQL -- the migrated schema, real JSONB, real constraints.

The requests go through the real production routes (TestClient(main.app)) and nothing between a request and the rows is
replaced except the session lookup: the real access and entitlement services, the real
services.sortview_value_service and every one of its SQL statements run against the database.

What only a real server can prove:
  * a write sets or removes the `sortview_value` key of the stored document and leaves every other key exactly as it
    was -- the Efficiency block beside it included;
  * a settings row that does not exist is created once, and never twice;
  * a write that races a change to ANOTHER key of the same document loses neither;
  * every statement reaches a row only through the acting user's own organization: another organization's owner, a
    member who is not an owner or admin, a removed membership, and a suspended or cancelled organization are all
    refused by the SQL itself, not only by the route in front of it;
  * the labor rate falls back on the ORGANIZATION's stored Efficiency default, and on nothing a sorter site has;
  * a stored Efficiency block that cannot be read does not stop the SortView Value settings being read.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_efficiency_settings_postgres.py: runs only when
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
    sortview_value_service,
)
from services.efficiency_settings import OrganizationEfficiencySettings
from services.sortview_value import (
    SortViewValueError,
    SortViewValueSettings,
    resolve_labor_rate,
    validate_sortview_value_settings,
)

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL SortView Value settings tests)"
)

ORIGIN = "https://app.example.invalid"
COOKIE = "__Host-sortview_api_session"
PATH = "/api/organizations/{org}/settings/sortview-value"

# Database ids. Distinctive, so none can be in a response by accident; never sent in a request.
ACME, BETA, PAUSED, CLOSED = 7111, 7212, 7313, 7414
OWNER, ADMIN, VIEWER, BETA_OWNER, FORMER_ADMIN = 9111, 9112, 9113, 9211, 9114
ACME_MAIN = 5111

CANARY_HASH = "CANARY-admin-lock-hash-44d7"
ACME_EFFICIENCY = {"labor_rate": "17.56", "manual_items_per_hour": "45.0"}
ACME_DOCUMENT = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": CANARY_HASH},
    "transit": {"home_branch_label": "Main", "destinations": [{"key": "b1", "label": "Westside", "enabled": True}]},
    "efficiency": ACME_EFFICIENCY,
    "account_settings": {"notes": "a note with \"quotes\" and unicode: é"},
}
# A sorter site's own Efficiency override. It is never a SortView Value rate.
MAIN_DOCUMENT = {"branch_name": "Main", "efficiency": {"labor_rate": "99.00", "one_time_cost": "118003.92"}}

REPORTING = {
    "tasks_per_month": "12.0",
    "baseline_minutes_per_task": "90.0",
    "current_minutes_per_task": "15.0",
    "basis": "estimate",
    "as_of": "2026-09-01",
}
TROUBLESHOOTING = {
    "incidents_per_month": "4.0",
    "staff_per_incident": "2.0",
    "baseline_minutes": "45.0",
    "current_minutes": "20.0",
    "basis": "measured",
    "as_of": "2026-09-15",
}
BLOCK = {"labor_rate": "32.50", "subscription_annual_cost": "4800.00", "reporting": REPORTING, "troubleshooting": TROUBLESHOOTING}
ORGANIZATION_NOT_FOUND = {"code": "organization_not_found", "message": "Organization not found."}

# A date every stored `as_of` above is on or before: what a block is validated against here.
AS_OF = date(2026, 10, 6)


def _settings(block: dict) -> SortViewValueSettings:
    return validate_sortview_value_settings(block, today=AS_OF)


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
    name = f"sortview_value_test_{secrets.token_hex(4)}"
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
    for org_id, slug, status in ((ACME, "acme", "active"), (BETA, "beta", "trial"), (PAUSED, "paused", "suspended"), (CLOSED, "closed", "cancelled")):
        conn.execute(
            text("INSERT INTO organizations (id, slug, name, status) VALUES (:id, :slug, :name, :status)"),
            {"id": org_id, "slug": slug, "name": f"{slug.title()} Library", "status": status},
        )
    for user_id in (OWNER, ADMIN, VIEWER, BETA_OWNER, FORMER_ADMIN):
        conn.execute(text("INSERT INTO app_users (id, email) VALUES (:id, :email)"), {"id": user_id, "email": f"user{user_id}@example.invalid"})
    for org_id, user_id, role in (
        (ACME, OWNER, "owner"), (ACME, ADMIN, "admin"), (ACME, VIEWER, "viewer"), (ACME, FORMER_ADMIN, "admin"), (BETA, BETA_OWNER, "owner"),
        # OWNER also owns the suspended and the cancelled organization.
        (PAUSED, OWNER, "owner"), (CLOSED, OWNER, "owner"),
    ):
        conn.execute(text("INSERT INTO memberships (organization_id, user_id, role) VALUES (:org, :user, :role)"), {"org": org_id, "user": user_id, "role": role})
    # FORMER_ADMIN was an admin of Acme and was removed: the role left on the row is history.
    conn.execute(text("UPDATE memberships SET removed_at = NOW() WHERE organization_id = :org AND user_id = :user"), {"org": ACME, "user": FORMER_ADMIN})
    # Acme has a sorter site with an Efficiency override of its own.
    conn.execute(
        text("INSERT INTO branches (id, organization_id, slug, name, status) VALUES (:id, :org, 'main', 'Main', 'active')"),
        {"id": ACME_MAIN, "org": ACME},
    )
    conn.execute(
        text("INSERT INTO collector_installations (organization_id, branch_id, name, status) VALUES (:org, :branch, 'AMH', 'active')"),
        {"org": ACME, "branch": ACME_MAIN},
    )
    conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:org, CAST(:doc AS JSONB))"), {"org": ACME, "doc": json.dumps(ACME_DOCUMENT)})
    conn.execute(text("INSERT INTO branch_settings (branch_id, settings_json) VALUES (:branch, CAST(:doc AS JSONB))"), {"branch": ACME_MAIN, "doc": json.dumps(MAIN_DOCUMENT)})


@pytest.fixture
def db(engine, monkeypatch):
    with engine.begin() as conn:
        _seed(conn)
    # Every service a request here can reach reads the same throwaway database -- the Efficiency settings' own
    # among them, for the one test that asks its route.
    for module in (access_service, entitlement_service, sortview_value_service, efficiency_settings_service):
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


def _put(api, org, body, user=OWNER):
    return api.put(PATH.format(org=org), json=body, headers={"Cookie": f"{COOKIE}={user}", "Origin": ORIGIN})


def _get(api, org, user=OWNER):
    return api.get(PATH.format(org=org), headers={"Cookie": f"{COOKIE}={user}"})


def _organizations(engine) -> dict[int, dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT organization_id AS owner, settings_json FROM organization_settings ORDER BY id")).all()
    assert len({row.owner for row in rows}) == len(rows), "a settings row was duplicated"
    return {row.owner: row.settings_json for row in rows}


def _branches(engine) -> dict[int, dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT branch_id AS owner, settings_json FROM branch_settings ORDER BY id")).all()
    return {row.owner: row.settings_json for row in rows}


def _set_key(engine, org_id: int, key: str, value: object) -> None:
    """Puts `value` under one key of a stored organization document, as another writer would."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE organization_settings SET settings_json = jsonb_set(settings_json, CAST(:path AS TEXT[]), CAST(:value AS JSONB), true) WHERE organization_id = :org"),
            {"org": org_id, "path": "{" + key + "}", "value": json.dumps(value)},
        )


# =====================================================================================================================
# One key of one document
# =====================================================================================================================

def test_reading_an_organization_with_no_block_gives_nothing_set_and_the_efficiency_default_to_fall_back_on(api, db):
    body = _get(api, "acme").json()["sortview_value"]

    assert body["subscription_annual_cost"] is None
    assert all(value is None for value in body["reporting"].values())
    assert all(value is None for value in body["troubleshooting"].values())
    assert body["labor_rate"] == {"sortview_value": None, "organization_efficiency": "17.56", "effective": "17.56", "source": "organization_efficiency"}
    # Reading wrote nothing.
    assert _organizations(db) == {ACME: ACME_DOCUMENT}


def test_a_put_sets_the_sortview_value_key_and_leaves_every_other_key_exactly_as_it_was(api, db):
    response = _put(api, "acme", BLOCK)

    assert response.status_code == 200
    assert _organizations(db)[ACME] == {**ACME_DOCUMENT, "sortview_value": BLOCK}
    assert _get(api, "acme").json() == response.json()
    assert response.json()["sortview_value"]["labor_rate"] == {
        "sortview_value": "32.50",
        "organization_efficiency": "17.56",
        "effective": "32.50",
        "source": "sortview_value",
    }
    assert CANARY_HASH not in response.text
    # No sorter site's document was touched.
    assert _branches(db) == {ACME_MAIN: MAIN_DOCUMENT}


def test_the_stored_values_are_json_text_never_json_numbers(api, db):
    _put(api, "acme", {"labor_rate": "32.5", "subscription_annual_cost": "0", "reporting": {"tasks_per_month": "12"}})

    with db.connect() as conn:
        types = conn.execute(text(
            "SELECT jsonb_typeof(settings_json #> '{sortview_value,labor_rate}'), "
            "jsonb_typeof(settings_json #> '{sortview_value,subscription_annual_cost}'), "
            "jsonb_typeof(settings_json #> '{sortview_value,reporting,tasks_per_month}'), "
            "jsonb_typeof(settings_json #> '{sortview_value,reporting}'), "
            "settings_json #>> '{sortview_value,subscription_annual_cost}' "
            "FROM organization_settings WHERE organization_id = :org"
        ), {"org": ACME}).one()

    assert tuple(types) == ("string", "string", "string", "object", "0.00")
    assert _organizations(db)[ACME]["sortview_value"] == {"labor_rate": "32.50", "subscription_annual_cost": "0.00", "reporting": {"tasks_per_month": "12.0"}}


def test_an_organization_with_no_settings_row_gets_one_holding_the_block_alone_and_only_one(api, db):
    assert BETA not in _organizations(db)

    first = _put(api, "beta", BLOCK, BETA_OWNER)
    second = _put(api, "beta", {"labor_rate": "40.00"}, BETA_OWNER)

    assert (first.status_code, second.status_code) == (200, 200)
    stored = _organizations(db)  # asserts there is no duplicate row
    assert stored[BETA] == {"sortview_value": {"labor_rate": "40.00"}}
    assert stored[ACME] == ACME_DOCUMENT


def test_reading_an_organization_with_no_settings_row_creates_none(api, db):
    response = _get(api, "beta", BETA_OWNER)

    assert response.status_code == 200
    assert response.json()["sortview_value"]["labor_rate"] == {"sortview_value": None, "organization_efficiency": None, "effective": None, "source": None}
    assert BETA not in _organizations(db)


def test_replacing_the_block_clears_what_is_left_out_and_keeps_the_rest_of_the_document(api, db):
    _put(api, "acme", BLOCK)

    _put(api, "acme", {"reporting": {"tasks_per_month": "3"}})

    assert _organizations(db)[ACME] == {**ACME_DOCUMENT, "sortview_value": {"reporting": {"tasks_per_month": "3.0"}}}


def test_a_put_of_nothing_removes_the_key_and_only_the_key_and_creates_no_row(api, db):
    _put(api, "acme", BLOCK)

    cleared = _put(api, "acme", {})
    never_had_one = _put(api, "beta", {}, BETA_OWNER)

    assert (cleared.status_code, never_had_one.status_code) == (200, 200)
    assert _organizations(db) == {ACME: ACME_DOCUMENT}
    assert "sortview_value" not in _organizations(db)[ACME]


def test_a_write_bumps_the_rows_updated_at(api, db):
    with db.begin() as conn:
        conn.execute(text("UPDATE organization_settings SET updated_at = '2020-01-01T00:00:00Z' WHERE organization_id = :org"), {"org": ACME})

    _put(api, "acme", BLOCK)
    with db.connect() as conn:
        after_set = conn.execute(text("SELECT updated_at FROM organization_settings WHERE organization_id = :org"), {"org": ACME}).scalar_one()
    with db.begin() as conn:
        conn.execute(text("UPDATE organization_settings SET updated_at = '2020-01-01T00:00:00Z' WHERE organization_id = :org"), {"org": ACME})
    _put(api, "acme", {})
    with db.connect() as conn:
        after_clear = conn.execute(text("SELECT updated_at FROM organization_settings WHERE organization_id = :org"), {"org": ACME}).scalar_one()

    assert after_set.year > 2020 and after_clear.year > 2020


# =====================================================================================================================
# The labor rate
# =====================================================================================================================

def test_the_rate_falls_back_on_the_organizations_efficiency_default_and_never_on_a_sorters_override(api, db):
    # Acme's one sorter site has a labor rate of 99.00 of its own. It is not the organization's.
    stored = sortview_value_service.read_sortview_value("acme", user_id=OWNER)

    assert stored is not None
    assert stored.organization_efficiency == OrganizationEfficiencySettings(labor_rate=stored.organization_efficiency.labor_rate, manual_items_per_hour=stored.organization_efficiency.manual_items_per_hour)
    assert str(stored.organization_efficiency.labor_rate) == "17.56"
    resolved = resolve_labor_rate(stored.settings, stored.organization_efficiency)
    assert resolved is not None and (str(resolved.value), resolved.source) == ("17.56", "organization_efficiency")
    assert "99.00" not in _get(api, "acme").text
    assert "118003" not in _get(api, "acme").text


def test_with_no_efficiency_default_and_no_rate_of_its_own_there_is_none(api, db):
    _set_key(db, ACME, "efficiency", {"manual_items_per_hour": "45.0"})

    body = _put(api, "acme", {"subscription_annual_cost": "4800"}).json()["sortview_value"]

    assert body["labor_rate"] == {"sortview_value": None, "organization_efficiency": None, "effective": None, "source": None}
    # The sorter's 99.00 is still not a rate.
    assert "99.00" not in json.dumps(body)


def test_changing_the_efficiency_default_changes_the_fallback_and_not_the_blocks_own_rate(api, db):
    _put(api, "acme", {"subscription_annual_cost": "4800"})
    _set_key(db, ACME, "efficiency", {"labor_rate": "21.00"})
    assert _get(api, "acme").json()["sortview_value"]["labor_rate"]["effective"] == "21.00"

    _put(api, "acme", {"labor_rate": "32.50"})
    _set_key(db, ACME, "efficiency", {"labor_rate": "25.00"})
    assert _get(api, "acme").json()["sortview_value"]["labor_rate"] == {
        "sortview_value": "32.50",
        "organization_efficiency": "25.00",
        "effective": "32.50",
        "source": "sortview_value",
    }


@pytest.mark.parametrize(
    "efficiency",
    [
        {"labor_rate": 17.56},
        {"labor_rate": "not a rate"},
        # A JSON string, which the database hands back as text: malformed, whatever the text says.
        "not an object",
        '{"labor_rate": "17.56", "manual_items_per_hour": "45.0"}',
        ["labor_rate"],
        7,
    ],
)
def test_a_stored_efficiency_block_that_cannot_be_read_does_not_stop_the_value_settings(api, db, efficiency):
    _put(api, "acme", {"subscription_annual_cost": "4800", "reporting": REPORTING})
    _set_key(db, ACME, "efficiency", efficiency)

    response = _get(api, "acme")
    stored = sortview_value_service.read_sortview_value("acme", user_id=OWNER)

    assert response.status_code == 200
    body = response.json()["sortview_value"]
    assert body["subscription_annual_cost"] == "4800.00" and body["reporting"] == REPORTING
    assert body["labor_rate"] == {"sortview_value": None, "organization_efficiency": None, "effective": None, "source": None}
    assert stored is not None and stored.efficiency_unreadable is True
    # And it can still be written to, without the unreadable block being touched.
    assert _put(api, "acme", BLOCK).status_code == 200
    assert _organizations(db)[ACME]["efficiency"] == efficiency


# =====================================================================================================================
# Whose settings
# =====================================================================================================================

def test_one_organizations_owner_cannot_read_or_write_anothers_through_the_routes(api, db):
    _put(api, "acme", BLOCK)
    before = _organizations(db)

    for user, org in ((BETA_OWNER, "acme"), (OWNER, "beta"), (OWNER, "nowhere")):
        read, written = _get(api, org, user), _put(api, org, {"labor_rate": "99.00"}, user)
        assert (read.status_code, read.json()) == (404, ORGANIZATION_NOT_FOUND)
        assert (written.status_code, written.json()) == (404, ORGANIZATION_NOT_FOUND)
        assert "32.50" not in read.text

    assert _organizations(db) == before


def test_the_sql_itself_refuses_anyone_who_is_not_an_active_owner_or_admin_of_that_organization(db):
    """Past the routes: the service called directly, as if a route had forgotten to check."""
    service = sortview_value_service
    service.replace_sortview_value("acme", _settings(BLOCK), user_id=OWNER)
    before = _organizations(db)

    # Another organization's owner, a viewer of this one, an admin whose membership was removed, and nobody at all.
    for user_id in (BETA_OWNER, VIEWER, FORMER_ADMIN, 424242):
        assert service.read_sortview_value("acme", user_id=user_id) is None
        assert service.replace_sortview_value("acme", _settings({"labor_rate": "99.00"}), user_id=user_id) is None
        assert service.replace_sortview_value("acme", _settings({}), user_id=user_id) is None

    assert _organizations(db) == before
    # While the organization's own owner and admin are answered.
    for user_id in (OWNER, ADMIN):
        stored = service.read_sortview_value("acme", user_id=user_id)
        assert stored is not None and stored.settings == _settings(BLOCK)


def test_the_write_statements_themselves_match_no_row_for_the_wrong_user_or_organization(db):
    """Each write statement on its own, without the scope check the service runs first."""
    service = sortview_value_service
    service.replace_sortview_value("acme", _settings(BLOCK), user_id=OWNER)
    block = json.dumps({"labor_rate": "99.00"})
    before = _organizations(db)

    with db.begin() as conn:
        for user_id, org_slug in (
            (BETA_OWNER, "acme"), (VIEWER, "acme"), (FORMER_ADMIN, "acme"), (OWNER, "beta"), (OWNER, "paused"), (OWNER, "closed"), (OWNER, "nowhere"),
        ):
            scope = {"org_slug": org_slug, "user_id": user_id}
            assert conn.execute(service._SET_SQL, {**scope, "block": block}).rowcount == 0
            assert conn.execute(service._CLEAR_SQL, scope).rowcount == 0

    assert _organizations(db) == before


def test_a_viewer_and_a_removed_admin_are_refused_by_the_route_and_nothing_is_read_or_written(api, db):
    _put(api, "acme", BLOCK)
    before = _organizations(db)

    viewer_read, viewer_write = _get(api, "acme", VIEWER), _put(api, "acme", {"labor_rate": "99.00"}, VIEWER)
    assert (viewer_read.status_code, viewer_write.status_code) == (403, 403)
    assert "32.50" not in viewer_read.text
    # A removed member is not a member: the organization is not found for them at all.
    former_read, former_write = _get(api, "acme", FORMER_ADMIN), _put(api, "acme", {"labor_rate": "99.00"}, FORMER_ADMIN)
    assert (former_read.status_code, former_write.status_code) == (404, 404)

    assert _organizations(db) == before


def test_a_suspended_organization_can_be_read_and_not_written_by_the_route_or_by_the_sql(api, db):
    with db.begin() as conn:
        conn.execute(
            text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:org, CAST(:doc AS JSONB))"),
            {"org": PAUSED, "doc": json.dumps({"sortview_value": BLOCK})},
        )
    before = _organizations(db)

    read, written = _get(api, "paused"), _put(api, "paused", {"labor_rate": "99.00"})
    assert read.status_code == 200 and read.json()["sortview_value"]["labor_rate"]["effective"] == "32.50"
    assert (written.status_code, written.json()["code"]) == (403, "organization_read_only")
    # And past the route.
    stored = sortview_value_service.read_sortview_value("paused", user_id=OWNER)
    assert stored is not None and stored.settings == _settings(BLOCK)
    assert sortview_value_service.replace_sortview_value("paused", _settings({"labor_rate": "99.00"}), user_id=OWNER) is None
    assert sortview_value_service.replace_sortview_value("paused", _settings({}), user_id=OWNER) is None

    assert _organizations(db) == before


def test_a_cancelled_organization_is_not_found_even_for_its_owner(api, db):
    assert (_get(api, "closed").status_code, _get(api, "closed").json()) == (404, ORGANIZATION_NOT_FOUND)
    assert _put(api, "closed", BLOCK).json() == ORGANIZATION_NOT_FOUND
    assert sortview_value_service.read_sortview_value("closed", user_id=OWNER) is None
    assert sortview_value_service.replace_sortview_value("closed", _settings(BLOCK), user_id=OWNER) is None
    assert CLOSED not in _organizations(db)


def test_no_database_id_reaches_any_answer(api, db):
    for response in (_put(api, "acme", BLOCK), _get(api, "acme"), _put(api, "acme", {"labor_rate": "bad"}), _get(api, "beta", OWNER), _get(api, "acme", VIEWER)):
        for identifier in (ACME, BETA, OWNER, ADMIN, VIEWER, ACME_MAIN):
            assert str(identifier) not in response.text


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
        # The SortView Value write is waiting on the row the other writer holds: it has not gone ahead on a stale copy.
        assert thread.is_alive()
        transaction.commit()
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        other.close()
    assert failures == []


def test_a_write_and_a_concurrent_change_to_the_efficiency_block_both_survive(db):
    _concurrently(
        db,
        "UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{efficiency,labor_rate}', '\"21.00\"') WHERE organization_id = :org",
        {"org": ACME},
        lambda: sortview_value_service.replace_sortview_value("acme", _settings(BLOCK), user_id=OWNER),
    )

    stored = _organizations(db)[ACME]
    assert stored["sortview_value"] == BLOCK
    assert stored["efficiency"] == {**ACME_EFFICIENCY, "labor_rate": "21.00"}
    assert stored["security"] == ACME_DOCUMENT["security"]


def test_a_write_and_a_concurrent_change_to_another_key_both_survive(db):
    _concurrently(
        db,
        "UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{transit,home_branch_label}', '\"Renamed\"') WHERE organization_id = :org",
        {"org": ACME},
        lambda: sortview_value_service.replace_sortview_value("acme", _settings(BLOCK), user_id=OWNER),
    )

    stored = _organizations(db)[ACME]
    assert stored["sortview_value"] == BLOCK
    assert stored["transit"]["home_branch_label"] == "Renamed"
    assert stored["efficiency"] == ACME_EFFICIENCY


def test_clearing_the_block_and_a_concurrent_change_to_another_key_both_survive(db):
    sortview_value_service.replace_sortview_value("acme", _settings(BLOCK), user_id=OWNER)

    _concurrently(
        db,
        "UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{library_name}', '\"Acme Public Library\"') WHERE organization_id = :org",
        {"org": ACME},
        lambda: sortview_value_service.replace_sortview_value("acme", _settings({}), user_id=OWNER),
    )

    assert _organizations(db)[ACME] == {**ACME_DOCUMENT, "library_name": "Acme Public Library"}


def test_simultaneous_first_writes_for_one_organization_make_one_row_holding_one_writers_whole_block(db):
    """Beta has no settings row. Eight writers each try to create it at once, each with a block of its own."""
    numbers = range(20, 28)
    failures: list[BaseException] = []
    answers: list[object] = []

    def block_of(number: int) -> dict:
        # Two fields that say which writer this is: a rate to two places, a count to one -- each as its field keeps it.
        return {"labor_rate": f"{number}.00", "reporting": {"tasks_per_month": f"{number}.0"}}

    # Every block is valid before any thread starts: what is being tested is the SQL, not the settings model.
    wanted = {number: _settings(block_of(number)) for number in numbers}
    assert BETA not in _organizations(db)

    def write(number: int):
        try:
            answers.append(sortview_value_service.replace_sortview_value("beta", wanted[number], user_id=BETA_OWNER))
        except BaseException as error:  # reported to the test below
            failures.append(error)

    threads = [threading.Thread(target=write, args=(number,)) for number in numbers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert failures == []
    # Every writer reached the database and was answered with a stored block.
    assert len(answers) == len(numbers) and all(answer is not None for answer in answers)
    with db.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*) FROM organization_settings WHERE organization_id = :org"), {"org": BETA}).scalar_one()
        kind = conn.execute(text("SELECT jsonb_typeof(settings_json) FROM organization_settings WHERE organization_id = :org"), {"org": BETA}).scalar_one()
    assert rows == 1
    assert kind == "object"
    stored = _organizations(db)  # asserts no organization has a duplicate row
    assert set(stored[BETA]) == {"sortview_value"}
    # The block is one writer's, whole: never one writer's rate beside another's count.
    assert stored[BETA]["sortview_value"] in [block_of(number) for number in numbers]
    assert sortview_value_service.read_sortview_value("beta", user_id=BETA_OWNER).settings in wanted.values()

    # And the last write of the key wins, whole: one more, after the rest, is exactly what is then stored.
    sortview_value_service.replace_sortview_value("beta", _settings({"subscription_annual_cost": "50.00"}), user_id=BETA_OWNER)
    assert _organizations(db)[BETA] == {"sortview_value": {"subscription_annual_cost": "50.00"}}


# =====================================================================================================================
# What is stored but malformed
# =====================================================================================================================

def test_a_malformed_stored_block_is_a_contained_error_and_is_repaired_by_a_put(api, db):
    _set_key(db, ACME, "sortview_value", {"labor_rate": 32.5, "reporting": "not an object"})

    response = _get(api, "acme")
    assert (response.status_code, response.json()["code"]) == (500, "sortview_value_settings_invalid")
    with pytest.raises(SortViewValueError):
        sortview_value_service.read_sortview_value("acme", user_id=OWNER)

    assert _put(api, "acme", BLOCK).status_code == 200
    assert _organizations(db)[ACME] == {**ACME_DOCUMENT, "sortview_value": BLOCK}
    assert _get(api, "acme").status_code == 200


@pytest.mark.parametrize(
    "stored",
    [
        "not an object",
        # A JSON string whose text is itself a valid block. It is still a string: it is not decoded a second time.
        json.dumps(BLOCK),
        "32.50",
        "",
    ],
)
def test_a_stored_value_key_that_is_a_json_string_is_the_contained_error_and_is_repaired_or_cleared(api, db, stored):
    _set_key(db, ACME, "sortview_value", stored)
    with db.connect() as conn:
        kind = conn.execute(text("SELECT jsonb_typeof(settings_json -> 'sortview_value') FROM organization_settings WHERE organization_id = :org"), {"org": ACME}).scalar_one()
    assert kind == "string"

    response = _get(api, "acme")

    # The established answer for stored settings that cannot be read -- not a decoding failure, and not the generic error.
    assert response.status_code == 500
    assert response.json() == {"code": "sortview_value_settings_invalid", "message": "The stored SortView Value settings could not be read."}
    assert "JSONDecodeError" not in response.text and "32.50" not in response.text
    with pytest.raises(SortViewValueError) as error:
        sortview_value_service.read_sortview_value("acme", user_id=OWNER)
    assert [(problem.field, problem.code) for problem in error.value.problems] == [("sortview_value", "not_an_object")]

    # A PUT repairs it: the string is replaced by the block, and nothing else of the document moves.
    assert _put(api, "acme", BLOCK).status_code == 200
    assert _organizations(db)[ACME] == {**ACME_DOCUMENT, "sortview_value": BLOCK}
    assert _get(api, "acme").status_code == 200

    # And put back, a PUT of nothing removes it.
    _set_key(db, ACME, "sortview_value", stored)
    cleared = _put(api, "acme", {})
    assert cleared.status_code == 200
    assert _organizations(db)[ACME] == ACME_DOCUMENT
    assert _get(api, "acme").status_code == 200


def test_a_malformed_stored_block_can_be_cleared_and_the_rest_of_the_document_is_as_it_was(api, db):
    _set_key(db, ACME, "sortview_value", "not an object")

    assert _put(api, "acme", {}).status_code == 200
    assert _organizations(db)[ACME] == ACME_DOCUMENT


def test_a_malformed_value_block_does_not_touch_the_efficiency_settings(api, db):
    _set_key(db, ACME, "sortview_value", {"labor_rate": "bad"})

    response = api.get("/api/organizations/acme/settings/efficiency", headers={"Cookie": f"{COOKIE}={OWNER}"})

    assert response.status_code == 200
    assert response.json() == {"efficiency": ACME_EFFICIENCY}
