"""Reports R9D1 on a REAL PostgreSQL: what width_bucket and the bound arrays actually do, and that the engine counts
by bucket row exactly as it counted by bucket column -- the authority over tests/test_report_buckets.py, whose
SQLite stand-in for width_bucket is only as good as this file shows the real one to be.

What only a real server can prove:

  * the boundaries are bound as `timestamp[]` for the legacy tables and `timestamptz[]` for the current ones;
  * width_bucket's edges: the first boundary is inside, a time ON a boundary is in the later bucket, two equal
    boundaries (the hour the clocks skip) make a bucket nothing falls in, and with the span's WHERE no bucket 0 or
    N + 1 ever reaches the application;
  * every case of tests/report_bucket_cases.py gives the same figures both ways on the real schema;
  * the old way cannot count more than 1,664 columns, and the new way counts 1,700 days -- and five years --
    exactly as an independent count of the seeded rows says.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_efficiency_report_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server. The module creates its
own throwaway database (migrated with the project's real Alembic chain) and drops it afterward. It reads as the
database's owner: row level security is tested elsewhere, and here the engine's own tenant filter is what scopes
every count. Every row is synthetic.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from report_bucket_cases import (
    BRANCH,
    CASES,
    CHICAGO,
    CUSTOMER,
    OTHER_BRANCH,
    OTHER_CUSTOMER,
    TENANT,
    Rows,
    both_ways,
    cutover_is,
    local,
    sparse_rows,
    the_engine_before_r9d1,
    utc,
)
from sqlalchemy import ARRAY, DateTime, bindparam, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from services import operational_report_service as engine
from services.operational_report_service import _SOURCES, local_range, report_window

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
KEY_ID = "6f8e2d3b-9c4a-4b7f-8d2e-3a5c7b9d1f24"

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL report bucket tests)")


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


@pytest.fixture(scope="module")
def owner():
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_report_buckets_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    database = None
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
        database = create_engine(url, hide_parameters=True)
        with database.begin() as conn:
            for customer, branch, organization in ((CUSTOMER, BRANCH, 1), (OTHER_CUSTOMER, OTHER_BRANCH, 2)):
                conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, :n)"), {"c": customer, "n": f"site-{customer}"})
                conn.execute(text("INSERT INTO organizations (id, slug, name, operational_customer_id) VALUES (:o, :s, :s, :c)"),
                             {"o": organization, "s": f"org-{organization}", "c": customer})
                conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, operational_branch_id) "
                                  "VALUES (:b, :o, 'main', 'Main', :b)"), {"b": branch, "o": organization})
        yield database
    finally:
        if database is not None:
            database.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


@pytest.fixture
def conn(owner):
    with owner.begin() as connection:
        connection.execute(text("TRUNCATE checkins, rejects, checkin_events, reject_events RESTART IDENTITY"))
    with owner.connect() as connection:
        yield connection


def _hex(*parts) -> str:
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()


def seed(conn, rows: Rows, customer=CUSTOMER, branch=BRANCH) -> None:
    scope = {"c": customer, "b": branch}
    for n, (at, destination, bin_) in enumerate(rows.v1_checkins):
        conn.execute(text("INSERT INTO checkins (customer_id, branch_id, event_time, barcode, destination, bin) "
                          "VALUES (:c, :b, :t, :barcode, :d, :bin)"),
                     {**scope, "t": at, "barcode": f"item-{n}", "d": destination, "bin": bin_})
    for n, (at, destination, bin_) in enumerate(rows.v2_checkins):
        conn.execute(text("INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, destination, bin) "
                          "VALUES (:c, :b, :k, :e, :t, :d, :bin)"),
                     {**scope, "k": KEY_ID, "e": _hex("checkin", customer, n), "t": at, "d": destination, "bin": bin_})
    for n, (at, message) in enumerate(rows.v1_rejects):
        conn.execute(text("INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message) "
                          "VALUES (:c, :b, :t, :barcode, :m)"), {**scope, "t": at, "barcode": f"item-{n}", "m": message})
    for n, (at, error_class) in enumerate(rows.v2_rejects):
        conn.execute(text("INSERT INTO reject_events (customer_id, branch_id, key_id, event_key, event_time, error_class) "
                          "VALUES (:c, :b, :k, :e, :t, :x)"),
                     {**scope, "k": KEY_ID, "e": _hex("reject", customer, n), "t": at, "x": error_class})
    conn.commit()


def _window(conn, first, last, cutover):
    with cutover_is(cutover):
        return report_window(conn, TENANT, local_range(first, last, CHICAGO))


def _source(table, group=None):
    return next(source for source in _SOURCES if (source.table, source.group_column) == (table, group))


# =====================================================================================================================
# The bound arrays, and width_bucket's edges
# =====================================================================================================================

@pytest.mark.parametrize(("aware", "expected"), [
    (False, "timestamp without time zone[]"),
    (True, "timestamp with time zone[]"),
])
def test_the_boundaries_are_bound_as_an_array_of_the_type_the_table_keeps(conn, aware, expected):
    first = utc(2026, 6, 8, 5) if aware else local(2026, 6, 8)
    statement = text("SELECT pg_typeof(:boundaries)::text").bindparams(bindparam("boundaries", type_=ARRAY(DateTime(timezone=aware))))

    assert conn.execute(statement, {"boundaries": [first, first + timedelta(days=1)]}).scalar_one() == expected


def test_width_bucket_puts_a_time_on_a_boundary_in_the_later_bucket_and_nothing_in_a_zero_width_one(conn):
    b0, b1, b2 = utc(2026, 3, 8, 6), utc(2026, 3, 8, 7), utc(2026, 3, 8, 8)
    boundaries = [b0, b1, b1, b2]       # bucket 2 is [b1, b1): the hour the clocks skip
    statement = text("SELECT width_bucket(CAST(:t AS timestamptz), :boundaries)").bindparams(
        bindparam("boundaries", type_=ARRAY(DateTime(timezone=True))))

    def bucket(instant):
        return conn.execute(statement, {"t": instant, "boundaries": boundaries}).scalar_one()

    assert bucket(b0) == 1                                  # the first boundary is inside the first bucket
    assert bucket(b0 + timedelta(minutes=30)) == 1
    assert bucket(b1 - timedelta(microseconds=1)) == 1
    assert bucket(b1) == 3                                  # on the repeated boundary: past the zero-width bucket
    assert bucket(b1 + timedelta(minutes=30)) == 3
    assert bucket(b0 - timedelta(microseconds=1)) == 0      # before every boundary ...
    assert bucket(b2) == 4                                  # ... or at the last: outside them all
    assert {bucket(b0 + timedelta(minutes=minute)) for minute in range(0, 120, 7)} <= {1, 3}


def test_the_engine_counts_each_interval_and_the_span_keeps_out_buckets_0_and_n_plus_1(conn):
    b0, b1, b2, b3 = utc(2026, 3, 8, 6), utc(2026, 3, 8, 7), utc(2026, 3, 8, 8), utc(2026, 3, 8, 9)
    seed(conn, Rows(v2_checkins=[
        (b0 - timedelta(seconds=1), "main", "1"),          # before the span
        (b0, "main", "1"), (b1 - timedelta(seconds=1), "main", "1"),
        (b1, "main", "1"), (b2, "main", "1"), (b2 + timedelta(minutes=59), "main", "1"),
        (b3, "main", "1"),                                   # the span's end: outside it
    ]))
    boundaries = (b0, b1, b1, b2, b3)

    counts = engine._execute_bucket_count(conn, TENANT, _source("checkin_events"), (b0, b3), boundaries)

    assert counts == [(2, 0, 1, 2)]
    # Every row the span holds is in exactly one bucket.
    with conn.begin_nested():
        inside = conn.execute(text("SELECT count(*) FROM checkin_events WHERE event_time >= :a AND event_time < :b"),
                              {"a": b0, "b": b3}).scalar_one()
    assert sum(counts[0]) == inside == 5


def test_the_statement_has_no_column_per_bucket_and_no_time_zone_in_it(conn):
    for source in _SOURCES:
        sql = str(engine._bucket_count_statement(source)).upper()
        assert "FILTER" not in sql and "AT TIME ZONE" not in sql and "DATE_TRUNC" not in sql
        assert sql.count("WIDTH_BUCKET(EVENT_TIME, :BOUNDARIES)") == 1


# =====================================================================================================================
# Every case, both ways, on the real schema
# =====================================================================================================================

@pytest.mark.parametrize("name", list(CASES))
def test_every_figure_is_what_the_column_per_bucket_engine_gave_on_a_real_server(conn, name):
    case = CASES[name]
    seed(conn, case.rows)
    seed(conn, case.rows, OTHER_CUSTOMER, OTHER_BRANCH)

    new, old = both_ways(conn, case.first, case.last, case.cutover)

    for figure, value in new.items():
        assert value == old[figure], figure
    assert sum(new["checkins by day"]) > 0


# =====================================================================================================================
# Long ranges
# =====================================================================================================================

def _expected_days(rows: Rows, first: date, last: date, cutover: datetime | None) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Check-ins and rejects on each day, counted from the seeded rows themselves -- not by either engine. A legacy
    row counts on its own wall-clock date, before the cutover; a current row on its instant's local date, from it."""
    days = (last - first).days + 1
    cutover_local = None if cutover is None else cutover.astimezone(CHICAGO).replace(tzinfo=None)

    def count(legacy, current):
        totals = [0] * days
        for wall in legacy:
            if cutover_local is None or wall < cutover_local:
                index = (wall.date() - first).days
                if 0 <= index < days:
                    totals[index] += 1
        for instant in current:
            if cutover is not None and instant >= cutover:
                index = (instant.astimezone(CHICAGO).date() - first).days
                if 0 <= index < days:
                    totals[index] += 1
        return tuple(totals)

    return (count([row[0] for row in rows.v1_checkins], [row[0] for row in rows.v2_checkins]),
            count([row[0] for row in rows.v1_rejects], [row[0] for row in rows.v2_rejects]))


@pytest.mark.parametrize(("first", "last", "cutover"), [
    (date(2021, 11, 24), date(2026, 7, 20), None),                                        # 1,700 days, legacy
    (date(2021, 11, 24), date(2026, 7, 20), utc(2000, 1, 1)),                            # 1,700 days, current
    (date(2021, 6, 21), date(2026, 6, 20), local(2024, 2, 29, 12).replace(tzinfo=CHICAGO)),  # 5 years, mixed, leap day
])
def test_past_1664_days_the_old_statement_cannot_be_run_and_the_new_one_counts_every_day(conn, first, last, cutover):
    rows = sparse_rows(first, last)
    seed(conn, rows)
    window = _window(conn, first, last, cutover)

    with the_engine_before_r9d1(), pytest.raises(DBAPIError, match="target lists can have at most 1664 entries"):
        engine.get_checkin_counts_by_day(conn, TENANT, window)
    conn.rollback()

    checkins, rejects = _expected_days(rows, first, last, cutover)
    assert engine.get_checkin_counts_by_day(conn, TENANT, window) == checkins
    assert engine.get_reject_counts_by_day(conn, TENANT, window) == rejects
    assert engine.get_reliability_report(conn, TENANT, window).checkin_days == checkins
    assert sum(checkins) > 0 and sum(rejects) > 0
    # Hours were always a week a statement, so the old way can still count them: and the two agree, day by day.
    new_hours = engine.get_checkin_counts_by_hour(conn, TENANT, window)
    with the_engine_before_r9d1():
        assert engine.get_checkin_counts_by_hour(conn, TENANT, window) == new_hours
    assert tuple(sum(hours) for hours in new_hours) == checkins


def test_up_to_the_old_limit_a_long_range_is_the_same_both_ways_on_a_real_server(conn):
    first, last = date(2022, 3, 1), date(2026, 7, 17)          # 1,600 days: the old statement still runs
    rows = sparse_rows(first, last)
    seed(conn, rows)

    new, old = both_ways(conn, first, last, local(2024, 6, 1).replace(tzinfo=CHICAGO))

    for figure, value in new.items():
        assert value == old[figure], figure
