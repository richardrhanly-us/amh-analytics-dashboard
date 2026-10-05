"""Block 9b on a REAL PostgreSQL: the server-stamped report instants of pipeline_status.

tests/test_pipeline_status_report_stamps.py shows which column each kind of report moves, on SQLite. What only a real
server can show is here: that CURRENT_TIMESTAMP into a TIMESTAMPTZ column is a true instant whatever time zone the
writing session has (the reason these two columns exist, and exactly what `updated_at` is not); that a second report
moves its stamp strictly forward; that both stamps of one statement are the same instant; and that a request the
server refuses leaves the row untouched because its transaction never commits.

The real endpoint is driven through TestClient(main.app), against a database migrated to head by the project's real
Alembic chain, with main.engine pointed at it. Only the token lookup's hash expression is swapped for the built-in
equivalent (pgcrypto is not on every throwaway server), as the other PostgreSQL modules do.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_ingest_v2_postgres.py. They run only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server the tests may create and drop
databases on, e.g.

    SORTVIEW_TEST_POSTGRES_URL=postgresql://postgres:@127.0.0.1:5432/postgres

The host must be local (localhost / 127.0.0.1 / ::1) unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also set, so a
production URL left in an environment variable cannot be used.

Every value is SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import main

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)"
)

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
_BUILTIN_SHA256_EXPR = "encode(sha256(convert_to(:token, 'UTF8')), 'hex')"  # pgcrypto is not on every throwaway server

CUSTOMER_A, BRANCH_A, TOKEN_A = 10, 1, "CANARY-STAMPS-TOKEN-A-9101"
CUSTOMER_B, BRANCH_B, TOKEN_B = 11, 2, "CANARY-STAMPS-TOKEN-B-9102"
CLIENT_SUPPLIED = "1988-08-08T08:08:08Z"

RUN_REPORT = {"status": "completed", "last_attempt": "2026-10-05T18:45:00Z", "last_run": "2026-10-05T18:45:03Z",
              "checkins_rows": 4, "rejects_rows": 0, "acs_rows": 2, "uploaded_checkins_rows": 4}
HEARTBEAT = {"health_status": "healthy", "pending_outbox_count": 0, "quarantined_count": 0,
             "last_success_at": "2026-10-05T18:44:00Z", "watcher_last_active_at": "2026-10-05T18:45:00Z"}
# Exactly the probe collector/preflight.py::_check_auth_and_scope posts at install and update time.
PREFLIGHT_PROBE = {"status": "preflight_check", "last_attempt": "2026-10-05T18:45:00.000000Z",
                   "checkins_rows": 0, "rejects_rows": 0, "acs_rows": 0}

client = TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


# --- a throwaway, fully migrated database -----------------------------------------------------------------------------

def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


@pytest.fixture(scope="module")
def pg_url():
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_stamps_api_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    test_url = admin.set(database=name)
    env = {**os.environ, "DATABASE_URL": test_url.render_as_string(hide_password=False)}
    migrated = subprocess.run(  # nosec B603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    try:
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        yield test_url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


def _seed(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE agent_tokens, collector_installations, pipeline_status, branches, organizations, "
                          "customers RESTART IDENTITY CASCADE"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Lib A'), (11, 'Lib B')"))
        conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
                          "(1, 'lib-a', 'Lib A', 'active', 10), (2, 'lib-b', 'Lib B', 'active', 11)"))
        conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) VALUES "
                          "(1, 1, 'main', 'Main', 'active', 1), (2, 2, 'main', 'Main', 'active', 2)"))
        conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, name, status) VALUES "
                          "(101, 1, 1, 'Sorter', 'active'), (104, 1, 1, 'Old sorter', 'inactive'), "
                          "(201, 2, 2, 'Other sorter', 'active')"))
        for token, customer, branch in ((TOKEN_A, CUSTOMER_A, BRANCH_A), (TOKEN_B, CUSTOMER_B, BRANCH_B)):
            conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active) "
                              "VALUES (:h, :c, :b, 't', TRUE)"),
                         {"h": hashlib.sha256(token.encode()).hexdigest(), "c": customer, "b": branch})


def _point_the_api_at(engine, monkeypatch) -> None:
    monkeypatch.setattr(main, "engine", engine)
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, _BUILTIN_SHA256_EXPR))


@pytest.fixture
def pg(pg_url, monkeypatch):
    engine = create_engine(pg_url, hide_parameters=True)
    _seed(engine)
    _point_the_api_at(engine, monkeypatch)
    yield engine
    engine.dispose()


def post(body: dict, *, token=TOKEN_A, customer_id=CUSTOMER_A, branch_id=BRANCH_A):
    return client.post("/upload-pipeline-status", json={"customer_id": customer_id, "branch_id": branch_id, **body},
                       headers={"Authorization": f"Bearer {token}"})


def accepted(body: dict, **kwargs) -> None:
    response = post(body, **kwargs)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "success", "message": "Pipeline status uploaded"}


def row(engine, customer_id=CUSTOMER_A, branch_id=BRANCH_A) -> dict | None:
    with engine.connect() as conn:
        found = conn.execute(text("SELECT * FROM pipeline_status WHERE customer_id = :c AND branch_id = :b"),
                             {"c": customer_id, "b": branch_id}).mappings().first()
    return dict(found) if found else None


def stamps(engine, customer_id=CUSTOMER_A, branch_id=BRANCH_A) -> tuple[datetime | None, datetime | None]:
    found = row(engine, customer_id, branch_id)
    return (found["status_reported_at"], found["health_status_reported_at"])


def server_now(engine) -> datetime:
    with engine.connect() as conn:
        return conn.execute(text("SELECT clock_timestamp()")).scalar()


def pause(engine) -> None:
    """Lets the server's clock move on by more than any clock's granularity, so "strictly later" is meaningful."""
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_sleep(0.05)"))


def _is_recent_instant(value, before: datetime, after: datetime) -> bool:
    """An aware datetime that lies within the request: between two readings of the SERVER's own clock."""
    return (isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
            and before - timedelta(seconds=1) <= value <= after + timedelta(seconds=1))


# =====================================================================================================================
# Each signal stamps its own column
# =====================================================================================================================

def test_a_status_only_report_stamps_status_reported_at_with_an_instant_and_leaves_the_health_stamp_null(pg):
    before = server_now(pg)

    accepted(RUN_REPORT)

    status_at, health_at = stamps(pg)
    assert _is_recent_instant(status_at, before, server_now(pg))
    assert health_at is None


def test_a_health_status_only_report_stamps_health_status_reported_at_and_leaves_the_status_stamp_null(pg):
    before = server_now(pg)

    accepted(HEARTBEAT)

    status_at, health_at = stamps(pg)
    assert _is_recent_instant(health_at, before, server_now(pg))
    assert status_at is None


def test_a_report_carrying_both_signals_stamps_both_with_the_same_instant(pg):
    before = server_now(pg)

    accepted({**RUN_REPORT, **HEARTBEAT})

    status_at, health_at = stamps(pg)
    assert _is_recent_instant(status_at, before, server_now(pg))
    assert status_at == health_at           # one statement, one transaction, one reading of the clock


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"checkins_rows": 9, "last_attempt": "2026-10-05T18:45:00Z", "last_run": "2026-10-05T18:45:03Z"},
        {"pending_outbox_count": 3, "quarantined_count": 1, "watcher_last_active_at": "2026-10-05T18:45:00Z"},
        {"destination_breakdown": {"Main": 4}},
        {"installation_id": 101, "collector_version": "0.0.1-synthetic"},
    ],
    ids=["nothing but the tenant", "run fields without status", "heartbeat fields without health_status",
         "destination breakdown", "installation linkage only"],
)
def test_a_report_carrying_neither_signal_stamps_neither(pg, body):
    accepted(RUN_REPORT)
    accepted(HEARTBEAT)
    stamps_before, updated_before = stamps(pg), row(pg)["updated_at"]
    pause(pg)

    accepted(body)

    assert stamps(pg) == stamps_before               # both exactly as they were, to the microsecond
    assert row(pg)["updated_at"] > updated_before    # while updated_at moved, as it always has


def test_a_first_ever_report_carrying_neither_signal_creates_the_row_with_both_stamps_null(pg):
    accepted({"checkins_rows": 2})

    assert stamps(pg) == (None, None)
    assert row(pg)["updated_at"] is not None


# =====================================================================================================================
# A second report moves only its own stamp, and moves it forward
# =====================================================================================================================

def test_a_second_status_report_advances_status_reported_at_and_leaves_the_health_stamp_unchanged(pg):
    accepted(HEARTBEAT)
    accepted(RUN_REPORT)
    first_status_at, health_before = stamps(pg)
    pause(pg)

    accepted({**RUN_REPORT, "status": "completed_no_new_rows"})

    status_at, health_at = stamps(pg)
    assert status_at > first_status_at
    assert health_at == health_before and health_at is not None
    assert row(pg)["status"] == "completed_no_new_rows" and row(pg)["health_status"] == "healthy"


def test_a_second_health_report_advances_health_status_reported_at_and_leaves_the_status_stamp_unchanged(pg):
    accepted(RUN_REPORT)
    accepted(HEARTBEAT)
    status_before, first_health_at = stamps(pg)
    pause(pg)

    accepted({**HEARTBEAT, "health_status": "degraded"})

    status_at, health_at = stamps(pg)
    assert health_at > first_health_at
    assert status_at == status_before and status_at is not None
    assert row(pg)["health_status"] == "degraded" and row(pg)["status"] == "completed"


def test_the_later_stamp_says_which_signal_was_reported_last(pg):
    # What the two columns are for: the same pair of stored values, and the stamps alone tell the two orders apart.
    accepted(RUN_REPORT)
    pause(pg)
    accepted(HEARTBEAT)
    status_at, health_at = stamps(pg)
    assert health_at > status_at            # the heartbeat was last

    pause(pg)
    accepted(RUN_REPORT)
    status_at, health_at = stamps(pg)
    assert status_at > health_at            # now the run report was
    assert (row(pg)["status"], row(pg)["health_status"]) == ("completed", "healthy")   # the values never changed


# =====================================================================================================================
# The preflight probe
# =====================================================================================================================

def test_a_preflight_probe_stamps_status_reported_at_and_never_creates_a_health_stamp(pg):
    before = server_now(pg)

    accepted({**PREFLIGHT_PROBE, "installation_id": 101, "collector_version": "0.0.1-synthetic"})

    status_at, health_at = stamps(pg)
    assert _is_recent_instant(status_at, before, server_now(pg))
    assert health_at is None
    assert row(pg)["status"] == "preflight_check"


def test_a_preflight_probe_advances_the_status_stamp_of_a_branch_that_already_reports_and_not_its_health_stamp(pg):
    accepted(RUN_REPORT)
    accepted(HEARTBEAT)
    first_status_at, health_before = stamps(pg)
    pause(pg)

    accepted(PREFLIGHT_PROBE)

    status_at, health_at = stamps(pg)
    assert status_at > first_status_at and health_at == health_before


# =====================================================================================================================
# The server's clock: an instant, whatever the session's time zone, and never the client's value
# =====================================================================================================================

def test_the_two_columns_are_timestamptz_and_updated_at_is_still_a_naive_timestamp(pg):
    with pg.connect() as conn:
        types = dict(conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'pipeline_status' AND column_name IN ('status_reported_at', 'health_status_reported_at', "
            "'updated_at', 'last_run', 'last_attempt')"
        )).all())

    assert types == {
        "status_reported_at": "timestamp with time zone", "health_status_reported_at": "timestamp with time zone",
        "updated_at": "timestamp without time zone", "last_run": "timestamp without time zone",
        "last_attempt": "timestamp without time zone",
    }


@pytest.mark.parametrize("session_time_zone", ["UTC", "America/Chicago", "Asia/Tokyo", "Asia/Kolkata"])
def test_the_stamp_is_the_same_instant_whatever_time_zone_the_writing_session_has(pg_url, monkeypatch, session_time_zone):
    writer = create_engine(pg_url, connect_args={"options": f"-c timezone={session_time_zone}"}, hide_parameters=True)
    reader = create_engine(pg_url, connect_args={"options": "-c timezone=UTC"}, hide_parameters=True)
    try:
        _seed(reader)
        _point_the_api_at(writer, monkeypatch)
        with writer.connect() as conn:
            assert conn.execute(text("SHOW timezone")).scalar() == session_time_zone

        before = server_now(reader)
        accepted({**RUN_REPORT, **HEARTBEAT})
        after = server_now(reader)

        with reader.connect() as conn:
            status_epoch, health_epoch, updated_naive = conn.execute(text(
                "SELECT EXTRACT(EPOCH FROM status_reported_at), EXTRACT(EPOCH FROM health_status_reported_at), updated_at "
                "FROM pipeline_status WHERE customer_id = :c AND branch_id = :b"), {"c": CUSTOMER_A, "b": BRANCH_A}).one()

        # The instant lies inside the request, measured on the server's own clock -- in every session time zone.
        assert before.timestamp() - 1 <= float(status_epoch) <= after.timestamp() + 1
        assert float(health_epoch) == float(status_epoch)
        # updated_at, by contrast, is the writing session's WALL CLOCK: read as UTC it is off by that zone's offset.
        # That is unchanged behaviour, and it is exactly why it could not serve as the report instant.
        offset = before.astimezone(ZoneInfo(session_time_zone)).utcoffset()
        as_if_utc = updated_naive.replace(tzinfo=before.tzinfo)
        assert abs((as_if_utc - before) - offset) < timedelta(seconds=5)
    finally:
        writer.dispose()
        reader.dispose()


def test_a_client_cannot_supply_either_stamp(pg):
    before = server_now(pg)

    accepted({**RUN_REPORT, **HEARTBEAT, "status_reported_at": CLIENT_SUPPLIED,
              "health_status_reported_at": CLIENT_SUPPLIED})

    status_at, health_at = stamps(pg)
    assert _is_recent_instant(status_at, before, server_now(pg)) and health_at == status_at
    assert status_at.year != 1988


def test_supplying_a_stamp_without_its_signal_stamps_nothing(pg):
    accepted({"checkins_rows": 1, "status_reported_at": CLIENT_SUPPLIED, "health_status_reported_at": CLIENT_SUPPLIED})

    assert stamps(pg) == (None, None)


def test_none_of_the_clients_own_timestamps_becomes_a_stamp(pg):
    before = server_now(pg)

    accepted({**RUN_REPORT, **HEARTBEAT, "last_attempt": "1988-01-01T00:00:00Z", "last_run": "1988-01-01T00:00:01Z",
              "last_success_at": "1988-01-01T00:00:02Z", "watcher_last_active_at": "1988-01-01T00:00:03Z"})

    found = row(pg)
    assert found["last_run"].year == 1988 and found["last_attempt"].year == 1988   # stored as sent, as before
    assert _is_recent_instant(found["status_reported_at"], before, server_now(pg))
    assert _is_recent_instant(found["health_status_reported_at"], before, server_now(pg))


def test_a_signal_sent_as_an_explicit_null_was_still_reported(pg):
    accepted(RUN_REPORT)
    first_status_at, _ = stamps(pg)
    pause(pg)

    accepted({"status": None})

    status_at, health_at = stamps(pg)
    assert status_at > first_status_at and health_at is None
    assert row(pg)["status"] is None


# =====================================================================================================================
# What is unchanged
# =====================================================================================================================

def test_updated_at_still_advances_on_every_accepted_report(pg):
    accepted({})
    seen = [row(pg)["updated_at"]]
    for body in ({"checkins_rows": 1}, RUN_REPORT, HEARTBEAT, {**RUN_REPORT, **HEARTBEAT}, PREFLIGHT_PROBE):
        pause(pg)
        accepted(body)
        seen.append(row(pg)["updated_at"])

    assert seen == sorted(seen) and len(set(seen)) == len(seen)     # strictly increasing, whatever was carried
    assert all(value.tzinfo is None for value in seen)              # and still a naive timestamp


def test_the_other_writers_columns_are_still_left_alone_by_a_partial_report(pg):
    accepted(RUN_REPORT)
    run_columns = {key: row(pg)[key] for key in ("status", "last_run", "last_attempt", "checkins_rows", "acs_rows")}

    accepted(HEARTBEAT)

    assert {key: row(pg)[key] for key in run_columns} == run_columns
    assert row(pg)["health_status"] == "healthy" and row(pg)["pending_outbox_count"] == 0


def test_one_tenants_report_never_stamps_another_tenants_row(pg):
    accepted({**RUN_REPORT, **HEARTBEAT}, token=TOKEN_B, customer_id=CUSTOMER_B, branch_id=BRANCH_B)
    other_before = row(pg, CUSTOMER_B, BRANCH_B)
    pause(pg)

    accepted({**RUN_REPORT, **HEARTBEAT})

    assert row(pg, CUSTOMER_B, BRANCH_B) == other_before         # not one column of the other tenant's row moved
    assert all(value is not None for value in stamps(pg))
    assert stamps(pg)[0] > other_before["status_reported_at"]


# =====================================================================================================================
# A refused or failed request stamps nothing: its transaction never commits
# =====================================================================================================================

BOTH = {**RUN_REPORT, **HEARTBEAT}
REFUSED = {
    "no authorization header": (lambda: client.post("/upload-pipeline-status",
                                                    json={"customer_id": CUSTOMER_A, "branch_id": BRANCH_A, **BOTH}), 401),
    "unknown token": (lambda: post(BOTH, token="no-such-token"), 401),
    "another tenant's token": (lambda: post(BOTH, token=TOKEN_B), 403),
    "an installation that is not active": (lambda: post({**BOTH, "installation_id": 104}), 403),
    "another tenant's installation": (lambda: post({**BOTH, "installation_id": 201}), 403),
    "an unknown installation": (lambda: post({**BOTH, "installation_id": 999}), 403),
    "an invalid health_status": (lambda: post({**RUN_REPORT, "health_status": "excellent"}), 422),
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_a_refused_report_leaves_an_existing_row_and_both_stamps_exactly_as_they_were(pg, name):
    accepted(BOTH)
    before = row(pg)
    pause(pg)
    send, expected_status = REFUSED[name]

    assert send().status_code == expected_status

    assert row(pg) == before        # both stamps, updated_at and every value: byte for byte


@pytest.mark.parametrize("name", list(REFUSED))
def test_a_refused_report_creates_no_row(pg, name):
    send, expected_status = REFUSED[name]

    assert send().status_code == expected_status
    assert row(pg) is None


def test_a_statement_that_fails_after_the_stamps_were_written_rolls_them_back(pg, monkeypatch):
    accepted(BOTH)
    before = row(pg)
    pause(pg)
    real = main._build_pipeline_status_upsert

    def stamps_then_fails(data):
        # The real statement -- stamps included -- followed, in the same transaction, by one that cannot succeed.
        sql, params = real(data)
        return sql + "; SELECT 1 / 0", params

    monkeypatch.setattr(main, "_build_pipeline_status_upsert", stamps_then_fails)

    response = post({**BOTH, "installation_id": 101})

    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    assert row(pg) == before        # the upsert ran and stamped, the transaction failed, nothing of it remains
