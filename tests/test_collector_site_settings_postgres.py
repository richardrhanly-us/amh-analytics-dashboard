"""A3b: an organization's Collector site settings on a REAL PostgreSQL -- the migrated schema and the real JSONB
statement.

services.tenant_service.set_collector_site_settings / get_collector_site_settings are called directly, with nothing
replaced but the engine.

What only a real server can prove:

  * a first write creates exactly one settings row, holding the two enrollment keys and the `transit` block alone;
  * a later write sets those and leaves EVERY other key of the document exactly as it was -- the admin lock, the
    classification lists, anything nested -- and replaces the enabled destinations whole;
  * the statement itself refuses a cancelled organization, and writes nothing for one that does not exist;
  * what was written is what a Contract v2 enrollment's own checks accept.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_routing_settings_postgres.py: runs only when
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
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from services import collector_rules, tenant_service

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL collector site settings tests)"
)

ACME, NEW, CLOSED, MISSING = 1, 2, 3, 99
REVIEWED_AT = datetime(2026, 10, 11, 15, 4, 5, tzinfo=UTC)  # passed in; nothing here reads a clock
MARKER = {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": 7}

# What else Acme's document holds. None of it is the Collector site settings'.
OTHER_KEYS = {
    "library_name": "Acme Library",
    "security": {"admin_enabled": True, "admin_password_hash": "CANARY-admin-lock-hash"},
    "efficiency": {"labor_rate": "17.56"},
    "internal_routing": {"branch_services_names": ["CANARY-staff-name"], "collection_services_names": [], "nested": {"a": [1, 2, {"b": None}]}},
}
OLD_TRANSIT = {
    "home_branch_label": "Central",
    "destinations": [
        {"key": "dest_1", "label": "North Branch", "enabled": True},
        {"key": "old", "label": "Old Depot", "enabled": False},
    ],
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
    name = f"sortview_collector_site_test_{secrets.token_hex(4)}"
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
        conn.execute(text("TRUNCATE organization_settings, organizations RESTART IDENTITY CASCADE"))
        for org_id, slug, status in ((ACME, "acme", "active"), (NEW, "new", "trial"), (CLOSED, "closed", "cancelled")):
            conn.execute(
                text("INSERT INTO organizations (id, slug, name, status) VALUES (:id, :slug, :name, :status)"),
                {"id": org_id, "slug": slug, "name": f"{slug.title()} Library", "status": status},
            )
        for org_id in (ACME, CLOSED):
            conn.execute(
                text("INSERT INTO organization_settings (organization_id, settings_json) VALUES (:org, CAST(:doc AS JSONB))"),
                {"org": org_id, "doc": json.dumps({**OTHER_KEYS, "transit": OLD_TRANSIT})},
            )
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)
    return engine


def _documents(engine) -> dict[int, dict]:
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(text("SELECT organization_id, settings_json FROM organization_settings ORDER BY 1"))}


def _save(organization_id: int, labels=("Riverside", "Bookmobile")) -> dict:
    return tenant_service.set_collector_site_settings(
        organization_id, timezone="America/New_York", transit_destination_labels=list(labels),
        reviewed_by_user_id=7, reviewed_at=REVIEWED_AT,
    )


def test_a_first_write_creates_one_row_holding_the_enrollment_keys_and_the_transit_block_alone(db):
    stored = _save(NEW)

    assert stored == {"collector_timezone": "America/New_York", "collector_rules": MARKER}
    assert _documents(db)[NEW] == {
        "collector_timezone": "America/New_York",
        "collector_rules": MARKER,
        "transit": {"destinations": [{"key": "riverside", "label": "Riverside", "enabled": True},
                                     {"key": "bookmobile", "label": "Bookmobile", "enabled": True}]},
    }
    assert set(_documents(db)) == {ACME, NEW, CLOSED}  # exactly one new row


def test_a_later_write_leaves_every_other_key_exactly_as_it_was(db):
    _save(ACME)

    document = _documents(db)[ACME]
    assert {key: document[key] for key in OTHER_KEYS} == OTHER_KEYS  # the admin lock and the lists, nested values and all
    assert document["collector_timezone"] == "America/New_York" and document["collector_rules"] == MARKER
    # The enabled destinations are replaced whole (the synthetic "dest_1" entry is gone); the home label and the
    # disabled destination are kept.
    assert document["transit"] == {
        "home_branch_label": "Central",
        "destinations": [
            {"key": "riverside", "label": "Riverside", "enabled": True},
            {"key": "bookmobile", "label": "Bookmobile", "enabled": True},
            {"key": "old", "label": "Old Depot", "enabled": False},
        ],
    }
    assert set(document) == set(OTHER_KEYS) | {"transit", "collector_timezone", "collector_rules"}


def test_a_second_write_replaces_the_first_and_still_touches_nothing_else(db):
    _save(ACME)
    _save(ACME, labels=())

    document = _documents(db)[ACME]
    assert {key: document[key] for key in OTHER_KEYS} == OTHER_KEYS
    assert document["transit"]["destinations"] == [{"key": "old", "label": "Old Depot", "enabled": False}]


def test_a_cancelled_organization_is_refused_and_nothing_is_written(db):
    before = _documents(db)

    with pytest.raises(RuntimeError, match="cancelled"):
        _save(CLOSED)

    assert _documents(db) == before


def test_the_statement_itself_writes_nothing_for_a_cancelled_or_missing_organization(db):
    """What the read a moment earlier cannot guarantee, the statement does: run on its own, it touches no row."""
    before = _documents(db)
    parameters = {"document": "{}", "site": "{}", "transit": "{}"}

    with db.begin() as conn:
        for organization_id in (CLOSED, MISSING):
            written = conn.execute(text(tenant_service._SET_COLLECTOR_SITE_SQL), {**parameters, "organization_id": organization_id})
            assert written.first() is None

    assert _documents(db) == before


def test_an_organization_that_does_not_exist_is_refused(db):
    with pytest.raises(RuntimeError, match="not found"):
        _save(MISSING)

    assert MISSING not in _documents(db)


def test_what_was_written_is_what_a_contract_2_enrollment_reads_and_accepts(db):
    assert tenant_service.get_collector_site_settings(NEW)["collector_timezone"] is None  # not ready before

    _save(NEW, labels=("North Branch",))
    site = tenant_service.get_collector_site_settings(NEW)

    assert site["transit_destination_labels"] == ["North Branch"]
    assert collector_rules.collector_timezone(site) == "America/New_York"
    collector_rules.require_rules_reviewed(site)
    # ... and the rules a Collector would be handed key that destination by its own label.
    rules = collector_rules.build_classification_rules(_documents(db)[NEW])
    assert rules["destinations"] == [{"slug": "north_branch", "contains": "NORTH BRANCH"}]


def test_reading_returns_counts_of_the_classification_lists_and_never_an_entry(db):
    site = tenant_service.get_collector_site_settings(ACME)

    assert site["internal_routing_counts"]["branch_services_names"] == 1
    assert "CANARY" not in repr(site)
