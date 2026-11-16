"""UI2 v6: scripts/set_sorter_bin_layout.py's writes and services.bin_layout_service's read, on a REAL PostgreSQL.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_v2_cutover_postgres.py. Runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server the tests may create and drop
databases on. The module creates a brand-new throwaway database, migrates it to head in a subprocess, and drops it
afterward. The host must be local unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts import set_sorter_bin_layout as script
from services import bin_layout_service
from services.bin_layout import BinLayout

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL integration tests)"
)

SEVEN_BINS = {"end": "0", "columns": [["6", "5"], ["4", "3"], ["2", "1"]]}
OTHER_KEYS = {
    "efficiency": {"labor_rate": "21.50", "one_time_cost": "250000.00"},
    "internal_routing": {"rules": [{"match": "x"}]},
}


@pytest.fixture(scope="module")
def pg_url():
    admin = make_url(ADMIN_URL)
    if (admin.host or "") not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(f"refusing to run against non-local PostgreSQL host {admin.host!r}")
    name = f"sortview_bin_layout_test_{secrets.token_hex(4)}"
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
def db(pg_url, monkeypatch):
    engine = create_engine(pg_url, hide_parameters=True)
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE collector_installations, organization_settings, branch_settings, branches, organizations, customers "
            "RESTART IDENTITY CASCADE"
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
    monkeypatch.setattr(bin_layout_service, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def _url(db) -> str:
    return db.url.render_as_string(hide_password=False)


def _run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    return script.main(list(argv), out=out), out.getvalue()


def _set(db, layout=SEVEN_BINS, org="acme", sorter="main", name="Main Library") -> tuple[int, str]:
    return _run("set", "--org-slug", org, "--sorter-slug", sorter, "--confirm-branch-name", name,
                "--layout", json.dumps(layout), "--execute", _url(db))


def _clear(db, org="acme", sorter="main", name="Main Library") -> tuple[int, str]:
    return _run("clear", "--org-slug", org, "--sorter-slug", sorter, "--confirm-branch-name", name, "--execute", _url(db))


def _documents(db) -> dict[int, dict]:
    with db.connect() as conn:
        return dict(conn.execute(text("SELECT branch_id, settings_json FROM branch_settings")).all())


def test_set_stores_the_layout_and_keeps_every_other_setting_exactly(db):
    code, output = _set(db)

    assert code == script.EXIT_OK, output
    assert "Layout stored before: null" in output
    assert _documents(db) == {1: {**OTHER_KEYS, "bin_layout": SEVEN_BINS}}
    assert bin_layout_service.read_bin_layouts("acme") == {
        "main": BinLayout(end="0", columns=(("6", "5"), ("4", "3"), ("2", "1"))),
    }


def test_set_is_repeatable_and_prints_what_it_replaced(db):
    assert _set(db)[0] == script.EXIT_OK
    code, output = _set(db)

    assert code == script.EXIT_OK
    assert f"Layout stored before: {json.dumps(SEVEN_BINS)}" in output
    assert _documents(db) == {1: {**OTHER_KEYS, "bin_layout": SEVEN_BINS}}


def test_set_on_a_sorter_with_no_settings_document_creates_one_holding_only_the_layout(db):
    assert _set(db, sorter="east", name="East Branch")[0] == script.EXIT_OK

    assert _documents(db)[2] == {"bin_layout": SEVEN_BINS}
    assert _documents(db)[1] == OTHER_KEYS


def test_clear_removes_only_the_layout(db):
    _set(db)

    code, output = _clear(db)

    assert code == script.EXIT_OK
    assert f"Layout stored before: {json.dumps(SEVEN_BINS)}" in output
    assert _documents(db) == {1: OTHER_KEYS}
    assert bin_layout_service.read_bin_layouts("acme") == {}


@pytest.mark.parametrize(("org", "sorter", "name"), [
    ("acme", "main", "Main"),            # not the branch name exactly
    ("acme", "main", "Beta Main"),       # another organization's site's name
    ("beta", "main", "Main Library"),    # the right name, the wrong organization
    ("acme", "westside", "Main Library"),
    ("nbpl", "main", "Main Library"),
])
def test_a_site_that_is_not_exactly_confirmed_is_refused_and_nothing_is_written(db, org, sorter, name):
    before = _documents(db)

    code, output = _set(db, org=org, sorter=sorter, name=name)

    assert code == script.EXIT_REFUSED
    assert "Nothing was written." in output
    assert _documents(db) == before


def test_review_lists_the_stored_layout_and_changes_nothing(db):
    _set(db)
    before = _documents(db)

    code, output = _run("review", "--org-slug", "acme", "--database-url", _url(db))

    assert code == script.EXIT_OK
    assert f"bin layout: {json.dumps(SEVEN_BINS)}" in output
    assert _documents(db) == before
