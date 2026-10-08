"""R8G: an organization's routing settings on a REAL PostgreSQL -- the migrated schema and the real JSONB statements.

services.routing_settings_service is called directly, with nothing replaced, and the reports' own reader
(services.routing_config_service) reads what it wrote.

What only a real server can prove:

  * a first write creates exactly one settings row, holding the `transit` key alone;
  * a replacement sets that one key and leaves every other key of the document exactly as it was -- and replaces the
    block whole, with nothing of the old destinations merged in;
  * a change to ANOTHER key made at the same moment is not lost;
  * the statements themselves refuse anyone who is not an active owner or admin of that organization, and refuse to
    write to a suspended or cancelled one;
  * what is selected is the `transit` key, never the document;
  * the organization's block is what this reads and writes, whatever a branch's own settings say -- and the reports,
    which do merge a branch's block over it, see a replacement at once.

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
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from services import routing_config_service, routing_settings_service
from services.routing_settings import RoutingDestinationSetting, RoutingSettings
from services.routing_settings_service import (
    read_organization_routing,
    replace_organization_routing,
)
from services.tenant_resolution_service import ResolvedOperationalTenant

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL routing settings tests)"
)

ACME, BETA, PAUSED, CLOSED = 1, 2, 3, 4
ACME_CUSTOMER, ACME_MAIN, ACME_EAST = 8101, 11, 12
OWNER, ADMIN, MANAGER, VIEWER, REMOVED, BETA_OWNER = 101, 102, 103, 104, 105, 201

# What else Acme's document holds. None of it is routing's.
OTHER_KEYS = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": "CANARY-admin-lock-hash"},
    "efficiency": {"labor_rate": "17.56", "manual_items_per_hour": "45.0"},
    "internal_routing": {"branch_services_names": ["CANARY-staff-name"], "collection_services_names": [], "nested": {"a": [1, 2, {"b": None}]}},
}
OLD_TRANSIT = {
    "home_branch_label": "Main",
    "destinations": [
        {"key": "branch_1", "label": "Westside", "enabled": True},
        {"key": "lx", "label": "Library Express", "enabled": True},
        {"key": "old", "label": "Old Depot", "enabled": False},
    ],
}


def _settings(home, *destinations) -> RoutingSettings:
    return RoutingSettings(home, tuple(RoutingDestinationSetting(label, enabled) for label, enabled in destinations))


NEW = _settings("Central", ("North Annex", True), ("Westside", False))


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
    name = f"sortview_routing_test_{secrets.token_hex(4)}"
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


@pytest.fixture
def db(engine, monkeypatch):
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE organization_settings, branch_settings, memberships, branches, app_users, organizations, customers "
            "RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, 'acme')"), {"c": ACME_CUSTOMER})
        for org_id, slug, status, customer in ((ACME, "acme", "active", ACME_CUSTOMER), (BETA, "beta", "trial", None),
                                               (PAUSED, "paused", "suspended", None), (CLOSED, "closed", "cancelled", None)):
            conn.execute(
                text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES (:id, :slug, :name, :status, :c)"),
                {"id": org_id, "slug": slug, "name": f"{slug.title()} Library", "status": status, "c": customer},
            )
        for user_id in (OWNER, ADMIN, MANAGER, VIEWER, REMOVED, BETA_OWNER):
            conn.execute(text("INSERT INTO app_users (id, email) VALUES (:id, :email)"), {"id": user_id, "email": f"user{user_id}@example.invalid"})
        for org_id, user_id, role, removed in (
            (ACME, OWNER, "owner", False), (ACME, ADMIN, "admin", False), (ACME, MANAGER, "manager", False), (ACME, VIEWER, "viewer", False),
            # An owner who was removed: the row, and the role on it, are still there.
            (ACME, REMOVED, "owner", True),
            (BETA, BETA_OWNER, "owner", False),
            # OWNER also owns the suspended and the cancelled organization.
            (PAUSED, OWNER, "owner", False), (CLOSED, OWNER, "owner", False),
        ):
            conn.execute(
                text("INSERT INTO memberships (organization_id, user_id, role, removed_at) VALUES (:org, :user, :role, CASE WHEN :removed THEN NOW() END)"),
                {"org": org_id, "user": user_id, "role": role, "removed": removed},
            )
        for branch_id, slug in ((ACME_MAIN, "main"), (ACME_EAST, "east")):
            conn.execute(
                text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) VALUES (:b, :o, :s, :n, 'active', :b)"),
                {"b": branch_id, "o": ACME, "s": slug, "n": f"{slug.title()} Branch"},
            )
        conn.execute(
            text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:org, CAST(:doc AS JSONB))"),
            {"org": ACME, "doc": json.dumps({**OTHER_KEYS, "transit": OLD_TRANSIT})},
        )
        conn.execute(
            text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:org, CAST(:doc AS JSONB))"),
            {"org": PAUSED, "doc": json.dumps({"transit": OLD_TRANSIT})},
        )
    monkeypatch.setattr(routing_settings_service, "get_engine", lambda: engine)
    return engine


def _documents(engine) -> dict[int, dict]:
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(text("SELECT organization_id, settings_json FROM organization_settings ORDER BY 1"))}


def _everything(engine) -> tuple:
    with engine.connect() as conn:
        return (
            [tuple(r) for r in conn.execute(text("SELECT organization_id, settings_json::text FROM organization_settings ORDER BY 1"))],
            [tuple(r) for r in conn.execute(text("SELECT branch_id, settings_json::text FROM branch_settings ORDER BY 1"))],
        )


def _report_routing(engine, branch_id=ACME_MAIN):
    tenant = ResolvedOperationalTenant(org_slug="acme", branch_slug="main", access_mode="full",
                                       operational_customer_id=ACME_CUSTOMER, operational_branch_id=branch_id)
    with engine.connect() as conn:
        return routing_config_service.get_routing_config(conn, tenant)


# =====================================================================================================================
# One key of one document
# =====================================================================================================================

def test_a_replacement_sets_the_transit_key_alone_and_every_other_key_is_exactly_as_it_was(db):
    before = _documents(db)

    assert replace_organization_routing("acme", NEW, user_id=OWNER) == NEW

    after = _documents(db)
    assert after[ACME]["transit"] == {
        "home_branch_label": "Central",
        "destinations": [
            {"key": "north_annex", "label": "North Annex", "enabled": True},
            {"key": "westside", "label": "Westside", "enabled": False},
        ],
    }
    assert {k: v for k, v in after[ACME].items() if k != "transit"} == OTHER_KEYS == {k: v for k, v in before[ACME].items() if k != "transit"}
    assert after[PAUSED] == before[PAUSED] and set(after) == {ACME, PAUSED}  # no other organization's row, and no new one


def test_a_replacement_replaces_the_block_whole_and_merges_in_nothing_of_the_old_one(db):
    assert replace_organization_routing("acme", _settings("", ("Only One", True)), user_id=ADMIN) == _settings("", ("Only One", True))

    stored = _documents(db)[ACME]["transit"]
    assert stored == {"home_branch_label": "", "destinations": [{"key": "only_one", "label": "Only One", "enabled": True}]}
    assert "Old Depot" not in json.dumps(stored) and "Library Express" not in json.dumps(stored)

    # ... and "no destinations at all" is stored as exactly that, not as an absent key.
    assert replace_organization_routing("acme", _settings("Main"), user_id=ADMIN) == _settings("Main")
    assert _documents(db)[ACME]["transit"] == {"home_branch_label": "Main", "destinations": []}
    assert read_organization_routing("acme", user_id=VIEWER) is None and read_organization_routing("acme", user_id=OWNER) == _settings("Main")


def test_a_first_write_creates_exactly_one_row_holding_the_key_alone_and_a_second_updates_it(db):
    assert BETA not in _documents(db)
    assert read_organization_routing("beta", user_id=BETA_OWNER) == _settings("")   # nothing stored is no settings, not "not found"

    assert replace_organization_routing("beta", NEW, user_id=BETA_OWNER) == NEW
    assert replace_organization_routing("beta", _settings("Main", ("Westside", True)), user_id=BETA_OWNER) == _settings("Main", ("Westside", True))

    with db.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text("SELECT settings_json FROM organization_settings WHERE organization_id = :o"), {"o": BETA})]
    assert rows == [({"transit": {"home_branch_label": "Main", "destinations": [{"key": "westside", "label": "Westside", "enabled": True}]}},)]


def test_a_write_bumps_the_rows_updated_at(db):
    with db.begin() as conn:
        conn.execute(text("UPDATE organization_settings SET updated_at = NOW() - INTERVAL '1 day' WHERE organization_id = :o"), {"o": ACME})

    replace_organization_routing("acme", NEW, user_id=OWNER)

    with db.connect() as conn:
        assert conn.execute(text("SELECT updated_at > NOW() - INTERVAL '1 minute' FROM organization_settings WHERE organization_id = :o"), {"o": ACME}).scalar()


def test_only_the_transit_key_is_selected_never_the_document_it_is_in(db):
    selected: list = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().startswith("SELECT"):
            selected.append(statement)

    event.listen(db, "after_cursor_execute", record)
    try:
        assert read_organization_routing("acme", user_id=OWNER) is not None
        replace_organization_routing("acme", NEW, user_id=OWNER)
    finally:
        event.remove(db, "after_cursor_execute", record)

    assert selected and all("settings_json -> 'transit' AS routing" in statement for statement in selected)
    # And what comes back through the service holds nothing else of the document.
    assert "CANARY" not in repr(read_organization_routing("acme", user_id=OWNER))


def test_what_the_dashboards_form_stored_is_read_and_never_refused(db):
    with db.begin() as conn:
        conn.execute(
            text("UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{transit}', CAST(:t AS JSONB)) WHERE organization_id = :o"),
            {"o": ACME, "t": json.dumps({"home_branch_label": " Main ", "destinations": [
                {"key": "branch_1", "label": "Westside", "enabled": True}, {"key": "branch_2", "label": "Westside"},
                {"key": "main", "label": "Main", "enabled": True}, {"key": "branch_4", "label": "  ", "enabled": True}, "not-an-object",
            ]})},
        )

    assert read_organization_routing("acme", user_id=ADMIN) == _settings("Main", ("Westside", True), ("Westside", True), ("Main", True))

    for malformed in ("null", '"text"', "[1, 2]", "7"):
        with db.begin() as conn:
            conn.execute(
                text("UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{transit}', CAST(:t AS JSONB)) WHERE organization_id = :o"),
                {"o": ACME, "t": malformed},
            )
        assert read_organization_routing("acme", user_id=ADMIN) == _settings("")
        # ... and it can still be put right.
        assert replace_organization_routing("acme", NEW, user_id=ADMIN) == NEW


# =====================================================================================================================
# Whose settings: decided by the statements themselves
# =====================================================================================================================

def test_the_sql_itself_refuses_anyone_who_is_not_an_active_owner_or_admin_of_that_organization(db):
    """Past the routes: the service called directly, as if a route had forgotten to check."""
    before = _everything(db)

    # A manager, a viewer, an owner whose membership was removed, another organization's owner, and nobody.
    for user_id in (MANAGER, VIEWER, REMOVED, BETA_OWNER, 424242):
        assert read_organization_routing("acme", user_id=user_id) is None
        assert replace_organization_routing("acme", NEW, user_id=user_id) is None
    assert read_organization_routing("nowhere", user_id=OWNER) is None
    assert replace_organization_routing("nowhere", NEW, user_id=OWNER) is None
    assert replace_organization_routing("beta", NEW, user_id=OWNER) is None    # an owner, of somewhere else

    assert _everything(db) == before
    for user_id in (OWNER, ADMIN):
        assert read_organization_routing("acme", user_id=user_id) is not None


def test_the_write_statement_alone_matches_no_row_for_the_wrong_user_or_organization(db):
    """The write statement on its own, without the scope check the service runs first."""
    before = _everything(db)
    block = json.dumps({"home_branch_label": "CANARY", "destinations": []})

    with db.begin() as conn:
        for user_id, org_slug in ((MANAGER, "acme"), (VIEWER, "acme"), (REMOVED, "acme"), (BETA_OWNER, "acme"), (OWNER, "beta"),
                                  (OWNER, "paused"), (OWNER, "closed"), (OWNER, "nowhere"), (424242, "acme")):
            written = conn.execute(routing_settings_service._SET_ORGANIZATION_SQL, {"org_slug": org_slug, "user_id": user_id, "block": block})
            assert written.rowcount == 0, (user_id, org_slug)

    assert _everything(db) == before


def test_a_suspended_organizations_routing_can_be_read_and_not_written_and_a_cancelled_ones_is_not_found(db):
    before = _everything(db)

    assert read_organization_routing("paused", user_id=OWNER) == _settings(
        "Main", ("Westside", True), ("Library Express", True), ("Old Depot", False))
    assert replace_organization_routing("paused", NEW, user_id=OWNER) is None
    assert read_organization_routing("closed", user_id=OWNER) is None
    assert replace_organization_routing("closed", NEW, user_id=OWNER) is None

    assert _everything(db) == before


# =====================================================================================================================
# Two writers, one document
# =====================================================================================================================

def test_a_routing_write_and_a_concurrent_change_to_another_key_both_survive(db):
    """Another writer holds a change to the SAME row -- to the `efficiency` key -- uncommitted. The routing write
    waits for it, then sets its one key on the document as it then is."""
    failures: list[BaseException] = []

    def write():
        try:
            replace_organization_routing("acme", NEW, user_id=OWNER)
        except BaseException as error:  # reported to the test below
            failures.append(error)

    other = db.connect()
    transaction = other.begin()
    try:
        other.execute(
            text("UPDATE organization_settings SET settings_json = jsonb_set(settings_json, '{efficiency}', CAST(:b AS JSONB)) WHERE organization_id = :o"),
            {"o": ACME, "b": json.dumps({"labor_rate": "99.00"})},
        )
        thread = threading.Thread(target=write)
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive()    # waiting on the row: it has not gone ahead on a stale copy of the document
        transaction.commit()
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        other.close()

    assert failures == []
    document = _documents(db)[ACME]
    assert document["efficiency"] == {"labor_rate": "99.00"}                       # the other writer's change
    assert document["transit"]["home_branch_label"] == "Central"                   # and this one's
    assert {k: v for k, v in document.items() if k not in ("transit", "efficiency")} == {k: v for k, v in OTHER_KEYS.items() if k != "efficiency"}


def test_two_first_writes_for_one_organization_make_one_row(db):
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def write(label: str):
        try:
            barrier.wait(timeout=10)
            replace_organization_routing("beta", _settings("Main", (label, True)), user_id=BETA_OWNER)
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=write, args=(label,)) for label in ("North", "South")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert failures == []
    with db.connect() as conn:
        rows = [r[0] for r in conn.execute(text("SELECT settings_json FROM organization_settings WHERE organization_id = :o"), {"o": BETA})]
    assert len(rows) == 1 and set(rows[0]) == {"transit"}
    assert [d["label"] for d in rows[0]["transit"]["destinations"]] in (["North"], ["South"])


# =====================================================================================================================
# The organization's own block, and what the reports make of it
# =====================================================================================================================

def test_the_reports_see_a_replacement_at_once_by_the_same_keys(db):
    assert [(d.key, d.label) for d in _report_routing(db).transit] == [("westside", "Westside"), ("library_express", "Library Express")]

    replace_organization_routing("acme", _settings("Central", ("North Annex", True), ("Westside", False), ("Library Express", True)), user_id=OWNER)

    routing = _report_routing(db)
    assert routing.home_label == "Central" and routing.home_keys == {"main", "central"}
    # The enabled destinations, in the order written; a disabled one is no destination to a report.
    assert [(d.key, d.label) for d in routing.transit] == [("north_annex", "North Annex"), ("library_express", "Library Express")]


def test_a_branchs_own_block_is_the_reports_business_and_is_neither_returned_nor_touched_here(db):
    # R8G edits the organization's block. A sorter site's branch settings can hold a `transit` of their own, which the
    # reports merge over it; this service does not read, merge or write it.
    branch_block = {"home_branch_label": "East Wing", "destinations": [{"key": "dock", "label": "Dock", "enabled": True}]}
    with db.begin() as conn:
        conn.execute(text("INSERT INTO branch_settings (branch_id, settings_json) VALUES (:b, CAST(:doc AS JSONB))"),
                     {"b": ACME_EAST, "doc": json.dumps({"branch_name": "East", "transit": branch_block})})

    # What an administrator reads and replaces is the organization's own ...
    assert read_organization_routing("acme", user_id=OWNER) == _settings("Main", ("Westside", True), ("Library Express", True), ("Old Depot", False))
    assert replace_organization_routing("acme", NEW, user_id=OWNER) == NEW
    assert read_organization_routing("acme", user_id=OWNER) == NEW

    # ... the branch's block is exactly as it was ...
    with db.connect() as conn:
        assert conn.execute(text("SELECT settings_json FROM branch_settings WHERE branch_id = :b"), {"b": ACME_EAST}).scalar() == {
            "branch_name": "East", "transit": branch_block}
    # ... and the reports still resolve each site for themselves: the site with a block of its own, by that block.
    assert [d.label for d in _report_routing(db, ACME_MAIN).transit] == ["North Annex"]
    east = _report_routing(db, ACME_EAST)
    assert east.home_label == "East Wing" and [d.label for d in east.transit] == ["Dock"]
