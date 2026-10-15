"""A2 item 3: services.admin_diagnostics_service, a sorter site's legacy pipeline report for the admin diagnostics.

The real statement runs against an in-memory SQLite pipeline_status table holding the columns it reads AND the ones it
must never read -- free-text errors, one library's destinations, every naive timestamp -- each with a canary value.
SQLite has no row level security, and neither has pipeline_status in production, so the statement's own tenant filter
is what is proved here. Imported the "flat" way, the identity the API process uses.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from services import admin_diagnostics_service
from services.admin_diagnostics_service import LegacyDiagnostics, get_legacy_diagnostics
from services.tenant_resolution_service import ResolvedOperationalTenant

_SCHEMA = """
    CREATE TABLE pipeline_status (
        customer_id INTEGER, branch_id INTEGER, status TEXT, health_status TEXT,
        status_reported_at TEXT, health_status_reported_at TEXT,
        pending_outbox_count INTEGER, quarantined_count INTEGER,
        checkins_rows INTEGER, rejects_rows INTEGER, uploaded_checkins_rows INTEGER, uploaded_rejects_rows INTEGER,
        checkins_bad_datetime_rows INTEGER, rejects_bad_datetime_rows INTEGER,
        last_error TEXT, last_failure_category TEXT, destination_breakdown TEXT, transit_items INTEGER,
        problem_items INTEGER, last_run TEXT, last_attempt TEXT, updated_at TEXT, watcher_last_active_at TEXT,
        oldest_pending_event_at TEXT, last_success_at TEXT, acs_rows INTEGER)
"""

NEVER_READ = (
    "last_error", "last_failure_category", "destination_breakdown", "transit_items", "problem_items", "last_run",
    "last_attempt", "updated_at", "watcher_last_active_at", "oldest_pending_event_at", "last_success_at", "acs_rows",
)


def _tenant(customer_id=10, branch_id=1):
    return ResolvedOperationalTenant(org_slug="synthetic", branch_slug="synthetic", access_mode="full",
                                     operational_customer_id=customer_id, operational_branch_id=branch_id)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text(_SCHEMA))
    return engine


def _row(engine, customer_id, branch_id, **values):
    columns = {
        "customer_id": customer_id, "branch_id": branch_id,
        "last_error": "CANARY-raw-error C:\\path\\patron", "last_failure_category": "CANARY-category",
        "destination_breakdown": '{"Westside": 4}', "transit_items": 4401, "problem_items": 4402,
        "last_run": "2026-10-01 09:00:00", "last_attempt": "2026-10-01 09:01:00", "updated_at": "2026-10-01 09:02:00",
        "watcher_last_active_at": "2026-10-01 09:03:00", "oldest_pending_event_at": "2026-10-01 09:04:00",
        "last_success_at": "2026-10-01 09:05:00", "acs_rows": 4403,
        **values,
    }
    names = ", ".join(columns)
    params = ", ".join(f":{name}" for name in columns)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO pipeline_status ({names}) VALUES ({params})"), columns)  # nosec B608 - test-built


def _read(engine, tenant=None):
    with engine.connect() as conn:
        return get_legacy_diagnostics(conn, tenant or _tenant())


def test_the_row_is_read_with_aware_utc_stamps_and_the_last_runs_counters(engine):
    _row(engine, 10, 1, status="completed", status_reported_at="2026-10-14T10:30:00-05:00",
         health_status="healthy", health_status_reported_at="2026-10-14T15:31:00+00:00",
         pending_outbox_count=2, quarantined_count=1, checkins_rows=120, rejects_rows=3,
         uploaded_checkins_rows=118, uploaded_rejects_rows=3, checkins_bad_datetime_rows=2, rejects_bad_datetime_rows=0)

    assert _read(engine) == LegacyDiagnostics(
        run_status="completed",
        run_reported_at=datetime(2026, 10, 14, 15, 30, tzinfo=UTC),
        health_status="healthy",
        health_reported_at=datetime(2026, 10, 14, 15, 31, tzinfo=UTC),
        pending_outbox_count=2,
        quarantined_count=1,
        checkins_rows=120,
        rejects_rows=3,
        uploaded_checkins_rows=118,
        uploaded_rejects_rows=3,
        checkins_bad_datetime_rows=2,
        rejects_bad_datetime_rows=0,
    )


def test_a_site_without_a_legacy_row_has_none(engine):
    _row(engine, 11, 1, status="completed")

    assert _read(engine) is None


def test_only_the_resolved_tenants_row_is_read(engine):
    _row(engine, 10, 2, status="failed_upload")      # same customer, another branch
    _row(engine, 11, 1, status="started")            # another customer, same branch id
    _row(engine, 10, 1, status="completed")

    assert _read(engine).run_status == "completed"


def test_a_second_row_for_one_branch_is_refused_not_chosen(engine):
    _row(engine, 10, 1, status="completed")
    _row(engine, 10, 1, status="failed_upload")

    with pytest.raises(RuntimeError):
        _read(engine)


def test_a_naive_stamp_is_refused_never_guessed(engine):
    _row(engine, 10, 1, status="completed", status_reported_at="2026-10-14 10:30:00")

    with pytest.raises(ValueError):
        _read(engine)


def test_no_free_text_destination_or_naive_column_is_ever_selected_or_returned(engine):
    statement = " ".join(str(admin_diagnostics_service._LEGACY_DIAGNOSTICS_SQL).split()).lower()
    selected = statement.split(" from ")[0]
    for column in NEVER_READ:
        assert column not in selected, column

    _row(engine, 10, 1, status="completed")
    returned = dataclasses.asdict(_read(engine))
    assert not any("CANARY" in str(value) or value in (4401, 4402, 4403) for value in returned.values())
    assert not [field.name for field in dataclasses.fields(LegacyDiagnostics) if field.name.endswith("_id")]
