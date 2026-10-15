"""A2 item 4: a library's collector configuration on a REAL PostgreSQL -- the migrated schema and the real statements.

services.tenant_service.replace_internal_routing / set_collector_enrollment_settings are called directly, with nothing
replaced but the engine. What only a real server can prove:

  * each write survives a re-read, changes only its own keys, and leaves every other key and every other library's
    document exactly as it was; a first write creates the row; a cancelled library is refused with nothing written;
  * the audit row is written with the change, names the library as target_org_slug and holds counts only;
  * the v1 hold rules (services.hold_report_service.legacy_hold_rules) read the new lists on their next read;
  * the next Contract v2 enrollment's own preparation (collector_enrollment_service._prepare_v2_enrollment) builds its
    rules and time zone from the new values;
  * a library whose Collector is ALREADY enrolled on Contract v2 has its key, its cutover, its installation and its
    enrollment codes untouched -- nothing is sent to it, and it is not enrolled again.

OPT-IN AND SAFE BY CONSTRUCTION -- the convention of tests/test_collector_site_settings_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server. The module creates its
own throwaway database (migrated with the project's real Alembic chain) and drops it afterward.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from services import collector_enrollment_service, hold_report_service, tenant_service
from services.collector_enrollment_service import EnrollmentRefused, InstallationScope
from services.tenant_resolution_service import ResolvedOperationalTenant

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL collector configuration tests)"
)

ACME, INSTALLED, CLOSED, FRESH = 1, 2, 3, 4
ACTOR = {"id": 7, "email": "admin@example.invalid"}
CANARY = "CANARY-ROUTING-7Q4Z"
INSTALLED_KEY = "4f1c2b3a-9d8e-4c7b-a6f5-0e1d2c3b4a59"
REVIEWED_AT = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

OLD_ROUTING = {
    "branch_services_names": [CANARY, "Branch Programs"],
    "collection_services_names": ["Cataloging"],
    "branch_services_da_patterns": [],
    "collection_services_da_patterns": ["DAOLD"],
}
NEW_ROUTING = {
    "branch_services_names": ["Outreach"],
    "collection_services_names": ["Cataloging", "Mending"],
    "branch_services_da_patterns": ["DaNew"],
    "collection_services_da_patterns": [],
}
# Everything else a library's document holds. None of it is this item's.
OTHER_KEYS = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": "CANARY-admin-lock-hash"},
    "efficiency": {"labor_rate": "17.56"},
    "transit": {"home_branch_label": "Central",
                "destinations": [{"key": "north_branch", "label": "North Branch", "enabled": True}]},
    "nested": {"a": [1, 2, {"b": None}]},
}
INSTALLED_DOCUMENT = {
    **OTHER_KEYS,
    "collector_timezone": "America/Denver",
    "collector_rules": {"reviewed_at": "2026-09-01T00:00:00+00:00", "reviewed_by_user_id": 3},
    "internal_routing": OLD_ROUTING,
}


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
    name = f"sortview_collector_config_test_{secrets.token_hex(4)}"
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
            "TRUNCATE auth_audit_log, v2_cutovers, ingest_key_ids, collector_enrollment_codes, agent_tokens, "
            "collector_installations, organization_settings, branch_settings, branches, organizations, customers "
            "RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Acme'), (11, 'Installed')"))
        conn.execute(text(
            "INSERT INTO organizations (id, slug, name, status, operational_customer_id, created_at) VALUES "
            "(1, 'acme', 'Acme Library', 'active', 10, '2026-01-01T00:00:00+00:00'), "
            "(2, 'installed', 'Installed Library', 'active', 11, '2026-01-01T00:00:00+00:00'), "
            "(3, 'closed', 'Closed Library', 'cancelled', NULL, '2026-01-01T00:00:00+00:00'), "
            "(4, 'fresh', 'Fresh Library', 'trial', NULL, '2026-01-01T00:00:00+00:00')"
        ))
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) VALUES "
            "(1, 1, 'main', 'Main', 'active', 1), (2, 2, 'main', 'Main', 'active', 2)"
        ))
        conn.execute(text(
            "INSERT INTO organization_settings (organization_id, settings_json) VALUES "
            "(1, CAST(:acme AS JSONB)), (2, CAST(:installed AS JSONB)), (3, CAST(:closed AS JSONB))"
        ), {"acme": json.dumps({**OTHER_KEYS, "internal_routing": OLD_ROUTING}),
            "installed": json.dumps(INSTALLED_DOCUMENT),
            "closed": json.dumps({**OTHER_KEYS, "internal_routing": OLD_ROUTING})})
        # The installed library: its Collector enrolled on Contract v2 -- an active key, its cutover, its installation.
        conn.execute(text(
            "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) VALUES "
            "(101, 1, 1, 'Acme Sorter', 'provisioning', 2), (201, 2, 2, 'Installed Sorter', 'active', 2)"
        ))
        conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:k, 11, 2)"),
                     {"k": INSTALLED_KEY})
        conn.execute(text(
            "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, note) "
            "VALUES (11, 2, '2026-01-01T00:00:00+00:00', 'initial', 'enrolled')"
        ))
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)
    return engine


def _documents(engine) -> dict[int, dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT organization_id, settings_json FROM organization_settings ORDER BY 1"))
        return {row[0]: row[1] for row in rows}


def _audit(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT user_id, email, event_type, is_success, message, metadata FROM auth_audit_log ORDER BY id"
        )).mappings().all()
        return [dict(row) for row in rows]


def _installed_state(engine) -> dict[str, list]:
    with engine.connect() as conn:
        return {
            table: [tuple(row) for row in conn.execute(text(f"SELECT * FROM {table} ORDER BY 1"))]  # nosec B608
            for table in ("ingest_key_ids", "v2_cutovers", "collector_installations", "collector_enrollment_codes",
                          "agent_tokens", "branch_settings")
        }


def _replace(organization_id=ACME, lists=NEW_ROUTING, slug="acme"):
    return tenant_service.replace_internal_routing(organization_id, lists, target_org_slug=slug, actor=ACTOR)


def _set(organization_id=ACME, zone="America/New_York", slug="acme"):
    return tenant_service.set_collector_enrollment_settings(
        organization_id, timezone=zone, target_org_slug=slug, actor=ACTOR, reviewed_at=REVIEWED_AT,
    )


# =====================================================================================================================
# persistence and isolation
# =====================================================================================================================

def test_replacing_the_lists_survives_a_re_read_and_changes_nothing_else_anywhere(db):
    before = _documents(db)

    assert _replace() == NEW_ROUTING

    after = _documents(db)
    assert after[ACME] == {**OTHER_KEYS, "internal_routing": NEW_ROUTING}
    assert {k: v for k, v in after.items() if k != ACME} == {k: v for k, v in before.items() if k != ACME}
    assert tenant_service.get_internal_routing(ACME) == NEW_ROUTING
    assert tenant_service.get_internal_routing(INSTALLED) == OLD_ROUTING


def test_setting_the_collector_settings_survives_a_re_read_and_changes_only_its_two_keys(db):
    before = _documents(db)

    _set()

    after = _documents(db)
    assert after[ACME] == {**before[ACME], "collector_timezone": "America/New_York",
                           "collector_rules": {"reviewed_at": "2026-10-15T12:00:00+00:00", "reviewed_by_user_id": 7}}
    assert after[ACME]["transit"] == OTHER_KEYS["transit"] and after[ACME]["internal_routing"] == OLD_ROUTING
    assert {k: v for k, v in after.items() if k != ACME} == {k: v for k, v in before.items() if k != ACME}
    site = tenant_service.get_collector_site_settings(ACME)
    assert site["collector_timezone"] == "America/New_York"
    assert site["internal_routing_counts"] == {"branch_services_names": 2, "collection_services_names": 1,
                                               "branch_services_da_patterns": 0, "collection_services_da_patterns": 1}


def test_a_first_write_creates_the_row_with_only_its_own_keys(db):
    _replace(FRESH, slug="fresh")
    assert _documents(db)[FRESH] == {"internal_routing": NEW_ROUTING}

    _set(FRESH, slug="fresh")
    assert set(_documents(db)[FRESH]) == {"internal_routing", "collector_timezone", "collector_rules"}


def test_a_cancelled_library_is_refused_with_nothing_written_and_nothing_audited(db):
    before = _documents(db)

    with pytest.raises(tenant_service.OrganizationCancelledError):
        _replace(CLOSED, slug="closed")
    with pytest.raises(tenant_service.OrganizationCancelledError):
        _set(CLOSED, slug="closed")

    assert _documents(db) == before
    assert _audit(db) == []


def test_an_unknown_library_is_refused_with_nothing_written(db):
    before = _documents(db)

    with pytest.raises(RuntimeError):
        _replace(99, slug="nobody")

    assert _documents(db) == before and _audit(db) == []


# =====================================================================================================================
# the audit record
# =====================================================================================================================

def test_each_write_is_audited_with_counts_only_under_target_org_slug(db):
    _replace()
    _set()

    routing, settings = _audit(db)
    assert routing == {
        "user_id": 7, "email": "admin@example.invalid", "event_type": "admin_internal_routing_replaced",
        "is_success": True, "message": "Internal Routing replaced.",
        "metadata": {
            "target_org_slug": "acme",
            "previous_counts": {"branch_services_names": 2, "collection_services_names": 1,
                                "branch_services_da_patterns": 0, "collection_services_da_patterns": 1},
            "counts": {"branch_services_names": 1, "collection_services_names": 2,
                       "branch_services_da_patterns": 1, "collection_services_da_patterns": 0},
            "actor_user_id": 7,
        },
    }
    assert settings["event_type"] == "admin_collector_settings_updated"
    assert settings["metadata"] == {"target_org_slug": "acme", "previous_collector_timezone": None,
                                    "collector_timezone": "America/New_York", "rules_reviewed": True, "actor_user_id": 7}
    text_of_rows = json.dumps(_audit(db), default=str)
    for value in (CANARY, "Branch Programs", "Outreach", "Mending", "DaNew", "DAOLD", "Cataloging"):
        assert value not in text_of_rows
    # Never attributed to the library as an event that happened IN it: its own members' activity views read org_slug.
    assert all("org_slug" not in row["metadata"] for row in _audit(db))


# =====================================================================================================================
# what reads the saved values
# =====================================================================================================================

def test_the_v1_hold_rules_read_the_new_lists_on_their_next_read(db):
    tenant = ResolvedOperationalTenant(org_slug="acme", branch_slug="main", access_mode="full",
                                       operational_customer_id=10, operational_branch_id=1)
    with db.connect() as conn:
        before = hold_report_service.legacy_hold_rules(conn, tenant)
    assert CANARY.upper() in before.branch_services_names

    _replace()

    with db.connect() as conn:
        after = hold_report_service.legacy_hold_rules(conn, tenant)
    assert after.branch_services_names == frozenset({"OUTREACH"})
    assert after.collection_services_names == frozenset({"CATALOGING", "MENDING"})
    assert after.branch_services_da_patterns == ("DANEW",)
    assert after.collection_services_da_patterns == ()


def test_the_next_contract_v2_enrollment_is_prepared_from_the_new_rules_and_time_zone(db):
    scope = InstallationScope(installation_id=101, installation_name="Acme Sorter", organization_id=ACME, branch_id=1,
                              customer_id=10, operational_branch_id=1, required_contract=2)
    with db.begin() as conn, pytest.raises(EnrollmentRefused) as refused:
        collector_enrollment_service._prepare_v2_enrollment(conn, scope)
    assert refused.value.reason == "timezone_not_configured"  # not ready until the admin records it

    _replace()
    _set()

    with db.begin() as conn:
        site = collector_enrollment_service._prepare_v2_enrollment(conn, scope)
    assert site.timezone == "America/New_York"
    assert site.rules["branch_services_names"] == ["OUTREACH"]
    assert site.rules["collection_services_names"] == ["CATALOGING", "MENDING"]
    assert site.rules["branch_services_da_patterns"] == ["DANEW"]
    assert site.rules["collection_services_da_patterns"] == []
    assert site.rules["destinations"] == [{"slug": "north_branch", "contains": "NORTH BRANCH"}]


def test_an_already_enrolled_collector_is_not_touched_and_is_not_enrolled_again(db):
    before = _installed_state(db)

    _replace(INSTALLED, slug="installed")
    _set(INSTALLED, zone="America/Chicago", slug="installed")

    assert _installed_state(db) == before
    assert _documents(db)[INSTALLED]["internal_routing"] == NEW_ROUTING  # SortView's configuration did change
    scope = InstallationScope(installation_id=201, installation_name="Installed Sorter", organization_id=INSTALLED,
                              branch_id=2, customer_id=11, operational_branch_id=2, required_contract=2)
    with db.begin() as conn, pytest.raises(EnrollmentRefused) as refused:
        collector_enrollment_service._prepare_v2_enrollment(conn, scope)
    assert refused.value.reason == "active_ingest_key_exists"
