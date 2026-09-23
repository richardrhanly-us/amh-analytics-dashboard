"""v2_cutovers (alembic f2a91c7d4e83) on a REAL PostgreSQL: the migration, the append-only audit trail, the
record_v2_cutover/get_effective_v2_cutover service functions, and data_loader.load_v2_cutover's real scoping.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_ingest_v2_postgres.py. Runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server the tests may create and drop
databases on. Each test module run creates a brand-new throwaway database, migrates it to head in a subprocess, and
drops it afterward. The host must be local unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL integration tests)"
)

CUSTOMER, BRANCH = 10, 1
OTHER_CUSTOMER, OTHER_BRANCH = 20, 2


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


def _alembic(url, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    return subprocess.run(  # nosec B603
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )


class Throwaway:
    def __init__(self):
        admin = make_url(ADMIN_URL)
        _guard(admin)
        self.admin = admin
        self.name = f"sortview_v2_cutover_test_{secrets.token_hex(4)}"
        self.admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")

    def __enter__(self):
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{self.name}"'))  # nosec B608 - generated name, no user input
        self.url = self.admin.set(database=self.name)
        migrated = _alembic(self.url, "upgrade", "head")
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        return self

    def __exit__(self, *_exc):
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))  # nosec B608
        self.admin_engine.dispose()


@pytest.fixture(scope="module")
def pg_url():
    with Throwaway() as db:
        yield db.url


def _seed_tenants(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE v2_cutovers, checkin_events, reject_events, acs_item_events, ingest_key_ids, checkins, "
            "rejects, acs_events, checkins_clean, rejects_clean, agent_tokens, branches, organizations, customers "
            "RESTART IDENTITY CASCADE"
        ))
        for org_id, customer, branch, slug in ((1, CUSTOMER, BRANCH, "lib"), (2, OTHER_CUSTOMER, OTHER_BRANCH, "other")):
            conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, :n)"), {"c": customer, "n": slug})
            conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) "
                              "VALUES (:o, :s, :s, 'active', :c)"), {"o": org_id, "s": slug, "c": customer})
            conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
                              "VALUES (:b, :o, 'main', 'Main', 'active', :b)"), {"b": branch, "o": org_id})


@pytest.fixture
def engine(pg_url):
    engine = create_engine(pg_url, hide_parameters=True)
    _seed_tenants(engine)
    yield engine
    engine.dispose()


# --- the migration itself -------------------------------------------------------------------------------------------

def test_v2_cutovers_table_exists_with_the_expected_shape(engine):
    with engine.connect() as conn:
        columns = {
            row[0]: row[1]
            for row in conn.execute(text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'v2_cutovers'"
            ))
        }
    assert columns == {
        "id": "NO", "customer_id": "NO", "branch_id": "NO",
        "cutover_at": "YES", "set_by": "NO", "set_at": "NO", "note": "YES",
    }


def test_v2_cutovers_rejects_a_blank_set_by_at_the_database_level(engine):
    with engine.connect() as conn, pytest.raises(IntegrityError), conn.begin():
        conn.execute(text(
            "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by) "
            "VALUES (:c, :b, now(), '   ')"
        ), {"c": CUSTOMER, "b": BRANCH})


def test_downgrade_drops_the_table_and_upgrade_restores_it(pg_url):
    engine = create_engine(pg_url, hide_parameters=True)
    try:
        down = _alembic(pg_url, "downgrade", "0acba192bf69")
        assert down.returncode == 0, down.stderr[-2000:]
        with engine.connect() as conn:
            exists = conn.execute(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'v2_cutovers'"
            )).first()
        assert exists is None

        up = _alembic(pg_url, "upgrade", "head")
        assert up.returncode == 0, up.stderr[-2000:]
        with engine.connect() as conn:
            exists = conn.execute(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'v2_cutovers'"
            )).first()
        assert exists is not None
    finally:
        engine.dispose()


# --- record_v2_cutover / get_effective_v2_cutover -------------------------------------------------------------------

def test_record_and_get_effective_cutover_round_trip(engine):
    from src.services.ingest_v2_service import (
        get_effective_v2_cutover,
        record_v2_cutover,
    )

    with engine.connect() as conn:
        assert get_effective_v2_cutover(conn, CUSTOMER, BRANCH) is None  # never piloted

    cutover_at = None
    with engine.begin() as conn:
        from datetime import UTC, datetime
        cutover_at = datetime(2026, 10, 1, tzinfo=UTC)
        record_v2_cutover(conn, CUSTOMER, BRANCH, cutover_at, set_by="rhanly", note="pilot cutover")

    with engine.connect() as conn:
        effective = get_effective_v2_cutover(conn, CUSTOMER, BRANCH)
    assert effective == cutover_at


def test_rollback_is_a_new_row_not_an_update_and_is_distinguishable_from_never_cut_over(engine):
    from datetime import UTC, datetime

    from src.services.ingest_v2_service import (
        get_effective_v2_cutover,
        record_v2_cutover,
    )

    cutover_at = datetime(2026, 10, 1, tzinfo=UTC)
    with engine.begin() as conn:
        record_v2_cutover(conn, CUSTOMER, BRANCH, cutover_at, set_by="rhanly")

    with engine.begin() as conn:
        record_v2_cutover(conn, CUSTOMER, BRANCH, None, set_by="rhanly", note="rollback: saw identity collisions")

    with engine.connect() as conn:
        assert get_effective_v2_cutover(conn, CUSTOMER, BRANCH) is None
        row_count = conn.execute(
            text("SELECT COUNT(*) FROM v2_cutovers WHERE customer_id = :c AND branch_id = :b"),
            {"c": CUSTOMER, "b": BRANCH},
        ).scalar_one()
    assert row_count == 2  # append-only: the rollback is a NEW row, the original is never touched


def test_re_cutover_after_rollback_uses_the_newest_row(engine):
    from datetime import UTC, datetime

    from src.services.ingest_v2_service import (
        get_effective_v2_cutover,
        record_v2_cutover,
    )

    first_cutover = datetime(2026, 10, 1, tzinfo=UTC)
    second_cutover = datetime(2026, 11, 1, tzinfo=UTC)
    with engine.begin() as conn:
        record_v2_cutover(conn, CUSTOMER, BRANCH, first_cutover, set_by="rhanly")
    with engine.begin() as conn:
        record_v2_cutover(conn, CUSTOMER, BRANCH, None, set_by="rhanly", note="rollback")
    with engine.begin() as conn:
        record_v2_cutover(conn, CUSTOMER, BRANCH, second_cutover, set_by="rhanly", note="re-cutover")

    with engine.connect() as conn:
        assert get_effective_v2_cutover(conn, CUSTOMER, BRANCH) == second_cutover


def test_record_v2_cutover_refuses_an_unmapped_tenant_and_writes_nothing(engine):
    from src.services.ingest_v2_service import record_v2_cutover

    with engine.begin() as conn, pytest.raises(ValueError):
        record_v2_cutover(conn, 999999, 999999, None, set_by="rhanly")

    with engine.connect() as conn:
        row_count = conn.execute(text("SELECT COUNT(*) FROM v2_cutovers")).scalar_one()
    assert row_count == 0


def test_record_v2_cutover_refuses_a_blank_set_by(engine):
    from src.services.ingest_v2_service import record_v2_cutover

    with engine.begin() as conn, pytest.raises(ValueError):
        record_v2_cutover(conn, CUSTOMER, BRANCH, None, set_by="   ")


def test_a_cutover_on_one_tenant_is_invisible_to_another(engine):
    from datetime import UTC, datetime

    from src.services.ingest_v2_service import (
        get_effective_v2_cutover,
        record_v2_cutover,
    )

    with engine.begin() as conn:
        record_v2_cutover(conn, CUSTOMER, BRANCH, datetime(2026, 10, 1, tzinfo=UTC), set_by="rhanly")

    with engine.connect() as conn:
        assert get_effective_v2_cutover(conn, OTHER_CUSTOMER, OTHER_BRANCH) is None


# --- data_loader.load_v2_cutover against a real database --------------------------------------------------------------

def test_data_loader_load_v2_cutover_reads_the_real_row(engine, monkeypatch):
    from datetime import UTC, datetime

    import data_loader as dl
    from src.services.ingest_v2_service import record_v2_cutover

    monkeypatch.setattr(dl, "get_engine", lambda: engine)
    dl.load_v2_cutover.clear()

    cutover_at = datetime(2026, 10, 1, tzinfo=UTC)
    with engine.begin() as conn:
        record_v2_cutover(conn, CUSTOMER, BRANCH, cutover_at, set_by="rhanly")

    result = dl.load_v2_cutover(CUSTOMER, BRANCH)
    assert result is not None
    assert result.to_pydatetime().replace(microsecond=0) == cutover_at.replace(microsecond=0)

    other = dl.load_v2_cutover(OTHER_CUSTOMER, OTHER_BRANCH)
    assert other is None
    dl.load_v2_cutover.clear()
