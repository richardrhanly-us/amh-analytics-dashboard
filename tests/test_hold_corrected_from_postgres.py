"""Hold-count accuracy, decision D5: scripts/set_hold_corrected_from.py on a REAL PostgreSQL, and the report's read of
the marker it writes (services.hold_report_service.hold_corrected_from).

OPT-IN AND SAFE BY CONSTRUCTION -- the convention of tests/test_bin_layout_postgres.py. Runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server. The module creates a brand-new
throwaway database, migrates it to head in a subprocess, and drops it afterward. The host must be local unless
SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts import set_hold_corrected_from as script
from services.hold_report_service import hold_corrected_from
from services.tenant_resolution_service import ResolvedOperationalTenant

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL integration tests)"
)

OTHER_KEYS = {
    "efficiency": {"labor_rate": "21.50", "one_time_cost": "250000.00"},
    "internal_routing": {"branch_services_names": ["SYNTHETIC ACCOUNT"]},
    "bin_layout": {"end": "0", "columns": [["2", "1"]]},
}
NOW = datetime(2026, 11, 18, 18, 0, tzinfo=UTC)  # freshness: allow FRESH004 -- passed as now= to script.main at every call (noon in Chicago)
TOMORROW = "2026-11-19"
OPERATOR = "operator@example.org"
ACME_MAIN = ResolvedOperationalTenant(org_slug="acme", branch_slug="main", access_mode="full", operational_customer_id=10,
                                      operational_branch_id=1)


@pytest.fixture(scope="module")
def pg_url():
    admin = make_url(ADMIN_URL)
    if (admin.host or "") not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(f"refusing to run against non-local PostgreSQL host {admin.host!r}")
    name = f"sortview_hold_marker_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    try:
        migrated = subprocess.run(  # nosec B603
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, capture_output=True, text=True, check=False,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
        )
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        yield url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


@pytest.fixture
def db(pg_url):
    engine = create_engine(pg_url, hide_parameters=True)
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE auth_audit_log, collector_installations, organization_settings, branch_settings, branches, "
            "organizations, customers RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Acme'), (11, 'Beta')"))
        conn.execute(text(
            "INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
            "(1, 'acme', 'Acme Library', 'active', 10), (2, 'beta', 'Beta Library', 'active', 11)"
        ))
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) VALUES "
            "(1, 1, 'main', 'Main Library', TRUE, 'active', 1), (2, 1, 'east', 'East Branch', FALSE, 'active', 2), "
            "(3, 2, 'main', 'Beta Main', TRUE, 'active', 3)"
        ))
        conn.execute(text(
            "INSERT INTO collector_installations (id, organization_id, branch_id, name, status) VALUES "
            "(101, 1, 1, 'Main AMH', 'active'), (102, 1, 2, 'East AMH', 'active'), (201, 2, 3, 'Beta AMH', 'active')"
        ))
        conn.execute(text("INSERT INTO branch_settings (branch_id, settings_json) VALUES (1, CAST(:d AS JSONB))"),
                     {"d": json.dumps(OTHER_KEYS)})
    yield engine
    engine.dispose()


def _url(db) -> str:
    return db.url.render_as_string(hide_password=False)


def _run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    return script.main(list(argv), out=out, now=NOW), out.getvalue()


def _site(org="acme", sorter="main", name="Main Library") -> list[str]:
    return ["--org-slug", org, "--sorter-slug", sorter, "--confirm-branch-name", name, "--operator-email", OPERATOR]


def _set(db, day=TOMORROW, **site) -> tuple[int, str]:
    return _run("set", *_site(**site), "--corrected-from", day, "--note", "verified 1.0.16", "--execute", _url(db))


def _clear(db, **site) -> tuple[int, str]:
    return _run("clear", *_site(**site), "--note", "rollback to 1.0.15", "--execute", _url(db))


def _documents(db) -> dict[int, dict]:
    with db.connect() as conn:
        return dict(conn.execute(text("SELECT branch_id, settings_json FROM branch_settings")).all())


def _audit(db) -> list[tuple]:
    with db.connect() as conn:
        return [tuple(row) for row in conn.execute(text(
            "SELECT user_id, email, event_type, is_success, metadata FROM auth_audit_log ORDER BY id")).all()]


def _marker(db, tenant=ACME_MAIN) -> date | None:
    with db.connect() as conn:
        return hold_corrected_from(conn, tenant)


def test_set_stores_only_the_marker_keeps_every_other_key_and_writes_one_audit_row(db):
    code, output = _set(db)

    assert code == script.EXIT_OK, output
    assert "Marker stored before: null" in output
    assert _documents(db) == {1: {**OTHER_KEYS, "hold_classification": {"corrected_from": TOMORROW}}}
    assert _audit(db) == [(None, OPERATOR, script.EVENT_SET, True, {
        "target_org_slug": "acme", "target_sorter_slug": "main", "branch_id": 1, "previous": None,
        "new": {"corrected_from": TOMORROW}, "note": "verified 1.0.16"})]
    assert _marker(db) == date(2026, 11, 19)


def test_clear_removes_only_the_marker_audits_the_previous_value_and_every_v2_day_is_uncorrected_again(db):
    _set(db)

    code, output = _clear(db)

    assert code == script.EXIT_OK, output
    assert f'Marker stored before: {json.dumps({"corrected_from": TOMORROW})}' in output
    assert _documents(db) == {1: OTHER_KEYS}
    assert [row[2] for row in _audit(db)] == [script.EVENT_SET, script.EVENT_CLEARED]
    assert _audit(db)[1][4] == {"target_org_slug": "acme", "target_sorter_slug": "main", "branch_id": 1,
                                "previous": {"corrected_from": TOMORROW}, "new": None, "note": "rollback to 1.0.15"}
    assert _marker(db) is None


def test_a_new_marker_after_a_rollback_and_a_new_upgrade_is_set_and_audited_again(db):
    _set(db)
    _clear(db)

    assert _set(db, day="2026-11-25")[0] == script.EXIT_OK
    assert _marker(db) == date(2026, 11, 25)
    assert [row[4]["previous"] for row in _audit(db)] == [None, {"corrected_from": TOMORROW}, None]


def test_set_on_a_site_with_no_settings_document_creates_one_holding_only_the_marker(db):
    assert _set(db, sorter="east", name="East Branch")[0] == script.EXIT_OK

    assert _documents(db)[2] == {"hold_classification": {"corrected_from": TOMORROW}}
    assert _documents(db)[1] == OTHER_KEYS
    assert _marker(db, ResolvedOperationalTenant("acme", "east", "full", 10, 2)) == date(2026, 11, 19)
    assert _marker(db) is None                                     # per branch: the other site is untouched


@pytest.mark.parametrize("day", ["2026-11-18", "2026-11-17", "2026-09-28"])
def test_today_or_an_earlier_day_is_refused_and_nothing_is_written_or_audited(db, day):
    before = _documents(db)

    code, output = _set(db, day=day)

    assert code == script.EXIT_REFUSED and "Nothing was written." in output and "not after today (2026-11-18" in output
    assert _documents(db) == before and _audit(db) == []


@pytest.mark.parametrize(("org", "sorter", "name"), [
    ("acme", "main", "Main"), ("acme", "main", "Beta Main"), ("beta", "main", "Main Library"), ("acme", "westside", "Main Library"),
])
def test_a_site_that_is_not_exactly_confirmed_is_refused_and_nothing_is_written_or_audited(db, org, sorter, name):
    before = _documents(db)

    code, output = _set(db, org=org, sorter=sorter, name=name)

    assert code == script.EXIT_REFUSED and "Nothing was written." in output
    assert _documents(db) == before and _audit(db) == []


def test_clearing_when_there_is_no_marker_is_refused_and_not_audited(db):
    code, output = _clear(db)

    assert code == script.EXIT_REFUSED and "no marker to clear" in output
    assert _audit(db) == []


@pytest.mark.parametrize("argv", [
    ["set", *_site()[:-2], "--operator-email", "not-an-email", "--corrected-from", TOMORROW],
    ["set", *_site(), "--corrected-from", "19/11/2026"],
    ["set", *_site(), "--corrected-from", "2026-11-31"],
])
def test_bad_arguments_are_refused_before_any_database_is_touched(db, argv):
    code, output = _run(*argv, "--execute", _url(db))

    assert code == script.EXIT_REFUSED and "Nothing was written." in output
    assert _documents(db) == {1: OTHER_KEYS} and _audit(db) == []


def test_without_execute_it_is_a_dry_run(db):
    code, output = _run("set", *_site(), "--corrected-from", TOMORROW)

    assert code == script.EXIT_OK and output.startswith("DRY RUN") and "audited" in output
    assert _documents(db) == {1: OTHER_KEYS} and _audit(db) == []


def test_review_lists_each_sites_marker_and_changes_nothing(db):
    _set(db)
    with db.begin() as conn:
        conn.execute(text("INSERT INTO branch_settings (branch_id, settings_json) VALUES "
                          "(3, '{\"hold_classification\": {\"corrected_from\": \"soon\"}}'::jsonb)"))
    before, audit = _documents(db), _audit(db)

    code, output = _run("review", "--database-url", _url(db))

    assert code == script.EXIT_OK
    assert f"hold classification: corrected_from {TOMORROW}" in output
    assert "hold classification: none (every v2 day uncorrected)" in output                  # east
    assert "hold classification: INVALID -- read as none" in output                            # beta: unreadable
    assert _documents(db) == before and _audit(db) == audit


def test_an_unreadable_stored_marker_is_read_by_the_report_as_no_marker(db):
    with db.begin() as conn:
        conn.execute(text("UPDATE branch_settings SET settings_json = settings_json || "
                          "'{\"hold_classification\": {\"corrected_from\": \"2026-11-19\", \"extra\": 1}}'::jsonb "
                          "WHERE branch_id = 1"))

    assert _marker(db) is None


def test_the_organizations_settings_never_supply_a_branchs_marker(db):
    with db.begin() as conn:
        conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES "
                          "(1, '{\"hold_classification\": {\"corrected_from\": \"2026-11-19\"}}'::jsonb)"))

    assert _marker(db) is None
