"""Block 4a: the customer read side's first operational query.

get_latest_ingest_status(conn, tenant) reads the latest Contract v2 heartbeat
for one tenant from ingest_key_ids, on a connection the caller supplies.

These tests run the REAL SQL against an in-memory SQLite table with the
columns the query touches (plus the identifier columns it must never return).
SQLite has no row level security, so everything proved here about tenant
isolation is proved by the statement's own explicit customer_id / branch_id
filter -- which is exactly the half of the "scoped twice" design this module
is responsible for. RLS itself is covered in tests/test_rls_phase1_postgres.py.

SQLite hands a timestamp back as the text it was stored as, where PostgreSQL
returns a datetime; the service passes the value through untouched either way,
so the tests compare against the stored text.

Imported the "flat" way (services.operational_read_service), the identity the
API process uses.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from services import operational_read_service
from services.operational_read_service import (
    IngestStatus,
    get_latest_ingest_status,
)
from services.tenant_resolution_service import ResolvedOperationalTenant

_DDL = """
    CREATE TABLE ingest_key_ids (
        id INTEGER PRIMARY KEY,
        key_id TEXT,
        customer_id INTEGER,
        branch_id INTEGER,
        algorithm TEXT,
        status TEXT,
        created_at TEXT,
        retired_at TEXT,
        last_heartbeat_at TEXT,
        health_status TEXT,
        last_error_class TEXT,
        pending_outbox_count INTEGER,
        quarantined_count INTEGER,
        oldest_pending_event_at TEXT,
        last_success_at TEXT,
        watcher_last_active_at TEXT,
        collector_last_run_at TEXT,
        collector_next_run_at TEXT,
        collector_run_duration_ms INTEGER,
        collector_schedule_status TEXT
    )
"""

CUSTOMER_A, BRANCH_A = 8101, 11
CUSTOMER_B, BRANCH_B = 8202, 21
TENANT_A = ResolvedOperationalTenant(
    org_slug="acme", branch_slug="main", access_mode="full",
    operational_customer_id=CUSTOMER_A, operational_branch_id=BRANCH_A,
)

# Distinctive values for every column the service must never return.
KEY_ID = "3db44444-931c-43cc-af3c-b1001443e761"
ALGORITHM = "hmac-sha256-v1"

SAFE_FIELDS = [
    "health_status",
    "last_error_class",
    "pending_outbox_count",
    "quarantined_count",
    "oldest_pending_event_at",
    "last_success_at",
    "watcher_last_active_at",
    "last_heartbeat_at",
    "collector_last_run_at",
    "collector_next_run_at",
    "collector_run_duration_ms",
    "collector_schedule_status",
]


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(_DDL))
    yield engine
    engine.dispose()


def _insert(engine, *, customer_id=CUSTOMER_A, branch_id=BRANCH_A, status="active", last_heartbeat_at=None,
            health_status="healthy", key_id=KEY_ID, **columns) -> None:
    values = {
        "key_id": key_id, "customer_id": customer_id, "branch_id": branch_id, "algorithm": ALGORITHM,
        "status": status, "last_heartbeat_at": last_heartbeat_at, "health_status": health_status, **columns,
    }
    names = ", ".join(values)
    binds = ", ".join(f":{name}" for name in values)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO ingest_key_ids ({names}) VALUES ({binds})"), values)  # nosec B608


def _read(engine, tenant=TENANT_A) -> IngestStatus | None:
    with engine.connect() as conn:
        return get_latest_ingest_status(conn, tenant)


# =====================================================================================================================
# What it returns
# =====================================================================================================================

def test_it_returns_the_active_keys_heartbeat_snapshot(engine):
    _insert(
        engine,
        last_heartbeat_at="2026-10-04 15:00:00+00:00",
        health_status="degraded",
        last_error_class="retryable_infra",
        pending_outbox_count=12,
        quarantined_count=1,
        oldest_pending_event_at="2026-10-04 14:10:00+00:00",
        last_success_at="2026-10-04 14:45:00+00:00",
        watcher_last_active_at="2026-10-04 14:59:00+00:00",
        collector_last_run_at="2026-10-04 14:58:00+00:00",
        collector_next_run_at="2026-10-04 15:13:00+00:00",
        collector_run_duration_ms=4321,
        collector_schedule_status="ok",
    )

    assert _read(engine) == IngestStatus(
        health_status="degraded",
        last_error_class="retryable_infra",
        pending_outbox_count=12,
        quarantined_count=1,
        oldest_pending_event_at="2026-10-04 14:10:00+00:00",
        last_success_at="2026-10-04 14:45:00+00:00",
        watcher_last_active_at="2026-10-04 14:59:00+00:00",
        last_heartbeat_at="2026-10-04 15:00:00+00:00",
        collector_last_run_at="2026-10-04 14:58:00+00:00",
        collector_next_run_at="2026-10-04 15:13:00+00:00",
        collector_run_duration_ms=4321,
        collector_schedule_status="ok",
    )


def test_a_key_that_has_never_reported_is_returned_with_empty_fields(engine):
    _insert(engine, health_status=None)

    assert _read(engine) == IngestStatus(**dict.fromkeys(SAFE_FIELDS))


def test_no_active_key_for_the_tenant_is_none(engine):
    assert _read(engine) is None

    _insert(engine, customer_id=CUSTOMER_B, branch_id=BRANCH_B, last_heartbeat_at="2026-10-04 15:00:00+00:00")

    assert _read(engine) is None


# =====================================================================================================================
# Which row it picks
# =====================================================================================================================

def test_only_an_active_key_is_eligible(engine):
    _insert(engine, status="retired", last_heartbeat_at="2026-10-04 23:00:00+00:00", health_status="error")

    assert _read(engine) is None

    _insert(engine, status="active", last_heartbeat_at="2026-10-01 08:00:00+00:00", health_status="healthy",
            key_id="6b0c9b37-aba4-4c93-b09b-c562977ff157")

    # The retired key reported more recently, and is still not the answer.
    assert _read(engine).health_status == "healthy"


@pytest.mark.parametrize("status", ["ACTIVE", "Active", "", None, "pending"])
def test_a_status_that_is_not_exactly_active_is_not_eligible(engine, status):
    _insert(engine, status=status, last_heartbeat_at="2026-10-04 15:00:00+00:00")

    assert _read(engine) is None


def test_among_several_active_keys_the_latest_heartbeat_wins(engine):
    _insert(engine, last_heartbeat_at="2026-10-04 10:00:00+00:00", health_status="error", key_id="k-older")
    _insert(engine, last_heartbeat_at="2026-10-04 15:00:00+00:00", health_status="healthy", key_id="k-newest")
    _insert(engine, last_heartbeat_at="2026-10-04 12:00:00+00:00", health_status="degraded", key_id="k-middle")

    assert _read(engine).last_heartbeat_at == "2026-10-04 15:00:00+00:00"
    assert _read(engine).health_status == "healthy"


def test_a_key_with_no_heartbeat_sorts_after_one_that_has_reported(engine):
    # Inserted first, so it would win any ordering that put NULL first or left the order to chance.
    _insert(engine, last_heartbeat_at=None, health_status=None, key_id="k-never-reported")
    _insert(engine, last_heartbeat_at="2026-10-01 08:00:00+00:00", health_status="degraded", key_id="k-reported")

    assert _read(engine).health_status == "degraded"


def test_exactly_one_row_is_returned_however_many_match(engine):
    for hour in range(5):
        _insert(engine, last_heartbeat_at=f"2026-10-04 1{hour}:00:00+00:00", key_id=f"k-{hour}")

    assert isinstance(_read(engine), IngestStatus)
    assert _read(engine).last_heartbeat_at == "2026-10-04 14:00:00+00:00"


# =====================================================================================================================
# The explicit tenant filter (SQLite has no RLS: only the statement's own WHERE separates these rows)
# =====================================================================================================================

def test_another_customers_key_is_never_returned(engine):
    _insert(engine, customer_id=CUSTOMER_B, branch_id=BRANCH_A, last_heartbeat_at="2026-10-04 23:00:00+00:00",
            health_status="error")
    _insert(engine, customer_id=CUSTOMER_A, branch_id=BRANCH_A, last_heartbeat_at="2026-10-01 08:00:00+00:00",
            health_status="healthy", key_id="k-mine")

    assert _read(engine).health_status == "healthy"


def test_another_branchs_key_is_never_returned(engine):
    _insert(engine, customer_id=CUSTOMER_A, branch_id=BRANCH_B, last_heartbeat_at="2026-10-04 23:00:00+00:00",
            health_status="error")

    assert _read(engine) is None

    _insert(engine, customer_id=CUSTOMER_A, branch_id=BRANCH_A, last_heartbeat_at="2026-10-01 08:00:00+00:00",
            health_status="healthy", key_id="k-mine")

    assert _read(engine).health_status == "healthy"


def test_each_tenant_reads_its_own_row_from_the_same_table(engine):
    tenant_b = ResolvedOperationalTenant(
        org_slug="beta", branch_slug="main", access_mode="read_only",
        operational_customer_id=CUSTOMER_B, operational_branch_id=BRANCH_B,
    )
    _insert(engine, customer_id=CUSTOMER_A, branch_id=BRANCH_A, health_status="healthy", key_id="k-a")
    _insert(engine, customer_id=CUSTOMER_B, branch_id=BRANCH_B, health_status="error", key_id="k-b")

    assert _read(engine, TENANT_A).health_status == "healthy"
    assert _read(engine, tenant_b).health_status == "error"  # a read_only tenant reads like any other


def test_the_tenant_ids_are_bound_from_the_resolved_tenant():
    class RecordingConnection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, parameters=None):
            self.calls.append((statement, parameters))
            return self

        def mappings(self):
            return self

        def first(self):
            return None

    conn = RecordingConnection()

    assert get_latest_ingest_status(conn, TENANT_A) is None
    ((statement, parameters),) = conn.calls  # exactly one statement, on the connection that was passed in
    assert parameters == {"customer_id": CUSTOMER_A, "branch_id": BRANCH_A}
    assert statement is operational_read_service._LATEST_INGEST_STATUS_SQL


# =====================================================================================================================
# The statement itself
# =====================================================================================================================

def _sql() -> str:
    return " ".join(str(operational_read_service._LATEST_INGEST_STATUS_SQL).split())


def test_the_statement_reads_only_ingest_key_ids_with_the_approved_shape():
    sql = _sql()

    assert "FROM ingest_key_ids WHERE" in sql
    assert sql.count("FROM") == 1 and "JOIN" not in sql
    assert "customer_id = :customer_id AND branch_id = :branch_id AND status = 'active'" in sql
    # Pinned as text because SQLite's default NULL ordering is not PostgreSQL's:
    # the behaviour test above shows the effect, this shows the clause that causes it.
    assert sql.endswith("ORDER BY last_heartbeat_at DESC NULLS LAST LIMIT 1")


def test_the_statement_selects_exactly_the_safe_columns():
    sql = _sql()
    select_list = [column.strip() for column in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]

    assert select_list == SAFE_FIELDS
    assert "*" not in sql
    # The table's own name contains "key_id", so look at everything but that name.
    everything_but_the_table_name = sql.replace("ingest_key_ids", "")
    for forbidden in ("key_id", "algorithm", "retired_at", "created_at"):
        assert forbidden not in everything_but_the_table_name, forbidden
    for forbidden in ("id", "customer_id", "branch_id", "status"):
        assert forbidden not in select_list, forbidden


# =====================================================================================================================
# The result object
# =====================================================================================================================

def test_the_result_has_exactly_the_approved_safe_fields():
    assert [f.name for f in dataclasses.fields(IngestStatus)] == SAFE_FIELDS


def test_the_result_carries_no_identifier(engine):
    _insert(engine, last_heartbeat_at="2026-10-04 15:00:00+00:00")

    result = _read(engine)

    rendered = f"{result!r} {dataclasses.asdict(result)}"
    for forbidden in (KEY_ID, ALGORITHM, str(CUSTOMER_A), "key_id", "customer_id", "branch_id", "algorithm"):
        assert forbidden not in rendered, forbidden
    assert not any(value == BRANCH_A for value in dataclasses.asdict(result).values())


def test_the_result_is_immutable(engine):
    _insert(engine)

    result = _read(engine)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.health_status = "healthy"
    with pytest.raises((AttributeError, TypeError)):
        result.key_id = KEY_ID  # slots: no attribute can be added either


# =====================================================================================================================
# Errors
# =====================================================================================================================

def test_a_failing_query_propagates_instead_of_returning_none(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE ingest_key_ids"))

    with pytest.raises(Exception, match="ingest_key_ids"):
        _read(engine)


def test_the_callers_own_database_error_passes_through_unchanged():
    class SyntheticDatabaseError(Exception):
        pass

    class BrokenConnection:
        def execute(self, *_args, **_kwargs):
            raise SyntheticDatabaseError("synthetic database failure")

    with pytest.raises(SyntheticDatabaseError, match="synthetic database failure"):
        get_latest_ingest_status(BrokenConnection(), TENANT_A)


# =====================================================================================================================
# Module boundaries
# =====================================================================================================================

def test_the_function_takes_a_connection_and_a_resolved_tenant_and_nothing_else():
    parameters = inspect.signature(get_latest_ingest_status).parameters

    assert list(parameters) == ["conn", "tenant"]
    assert all(p.default is inspect.Parameter.empty for p in parameters.values())  # the connection is required


def test_the_module_creates_no_engine_and_opens_no_connection():
    source = inspect.getsource(operational_read_service)

    assert not hasattr(operational_read_service, "get_engine")
    assert not hasattr(operational_read_service, "create_engine")
    for forbidden in ("get_engine", "create_engine", "import database", "from database", ".connect(", ".begin(",
                      "tenant_connection", "set_config", ".commit("):
        assert forbidden not in source, forbidden


def test_the_module_is_framework_neutral_and_uncached():
    imports = [line.strip() for line in inspect.getsource(operational_read_service).splitlines()
               if line.startswith(("import ", "from "))]

    assert imports == [
        "from __future__ import annotations",
        "from dataclasses import dataclass",
        "from datetime import datetime",
        "from sqlalchemy import text",
        "from sqlalchemy.engine import Connection",
        "from services.tenant_resolution_service import ResolvedOperationalTenant",
    ]
    assert not hasattr(get_latest_ingest_status, "clear")
    assert not hasattr(get_latest_ingest_status, "cache_clear")
    assert not hasattr(get_latest_ingest_status, "__wrapped__")


def test_nothing_is_cached_between_calls(engine):
    _insert(engine, health_status="healthy", key_id="k-first")
    assert _read(engine).health_status == "healthy"

    with engine.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET health_status = 'error'"))
    assert _read(engine).health_status == "error"

    with engine.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET status = 'retired'"))
    assert _read(engine) is None
