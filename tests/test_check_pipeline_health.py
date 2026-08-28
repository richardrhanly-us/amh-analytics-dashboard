"""Tests for scripts/check_pipeline_health.py -- specifically the Phase 3
compatibility rule: prefer health_status (heartbeat) when a branch has
ever reported one, fall back to legacy status parsing otherwise. Both
vocabularies can be live simultaneously during Continuous Ingestion
Phase 0's parallel-validation coexistence window.

conn.execute(...).mappings().all() is faked with plain dicts -- a dict
already satisfies both row["key"] access and **row unpacking, which is
all find_unhealthy_branches needs from a SQLAlchemy Row mapping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.check_pipeline_health import find_unhealthy_branches

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
STALE_AFTER = NOW - timedelta(minutes=60)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, stmt, params=None):
        return FakeResult(self._rows)


def _row(**overrides):
    row = {
        "organization_name": "Test Library",
        "branch_name": "Main",
        "status": None,
        "last_run": None,
        "last_attempt": None,
        "updated_at": NOW - timedelta(minutes=1),
        "health_status": None,
        "quarantined_count": None,
        "last_error": None,
    }
    row.update(overrides)
    return row


def test_healthy_heartbeat_not_flagged():
    conn = FakeConnection([_row(health_status="healthy")])
    assert find_unhealthy_branches(conn, STALE_AFTER) == []


def test_degraded_heartbeat_flagged():
    conn = FakeConnection([_row(health_status="degraded", last_error="backlog stuck")])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert any("degraded" in reason for reason in unhealthy[0]["reasons"])
    assert "backlog stuck" in unhealthy[0]["reasons"][0]


def test_auth_failure_heartbeat_flagged():
    conn = FakeConnection([_row(health_status="auth_failure")])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert any("auth_failure" in reason for reason in unhealthy[0]["reasons"])


def test_stale_no_heartbeat_flagged():
    conn = FakeConnection([_row(health_status="healthy", updated_at=NOW - timedelta(hours=2))])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert any("stale" in reason for reason in unhealthy[0]["reasons"])


def test_never_reported_flagged():
    conn = FakeConnection([_row(updated_at=None)])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert "has never reported a pipeline run" in unhealthy[0]["reasons"]


def test_legacy_failed_status_flagged_when_health_status_absent():
    conn = FakeConnection([_row(status="failed_parse_error")])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert any("failed_parse_error" in reason for reason in unhealthy[0]["reasons"])


def test_legacy_completed_status_not_flagged_when_health_status_absent():
    conn = FakeConnection([_row(status="completed")])
    assert find_unhealthy_branches(conn, STALE_AFTER) == []


def test_health_status_takes_precedence_over_stale_legacy_status():
    # A branch that has cut over to heartbeat reporting no longer has its
    # legacy `status` refreshed -- health_status healthy must win even
    # though the frozen legacy status says "failed".
    conn = FakeConnection([_row(health_status="healthy", status="failed")])
    assert find_unhealthy_branches(conn, STALE_AFTER) == []


def test_idle_branch_with_recent_heartbeat_not_flagged_as_stale():
    # Heartbeat/updated_at freshness is the staleness signal, not AMH log
    # activity -- a branch with a fresh heartbeat and nothing else wrong
    # must not be flagged, regardless of how quiet the source logs are.
    conn = FakeConnection([_row(health_status="healthy", updated_at=NOW - timedelta(seconds=30))])
    assert find_unhealthy_branches(conn, STALE_AFTER) == []


def test_multiple_branches_only_unhealthy_ones_reported():
    conn = FakeConnection([
        _row(branch_name="Main", health_status="healthy"),
        _row(branch_name="Annex", health_status="auth_failure"),
    ])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert unhealthy[0]["branch_name"] == "Annex"
