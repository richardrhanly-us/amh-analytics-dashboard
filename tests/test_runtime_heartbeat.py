"""Tests for agent/runtime/heartbeat.py (Continuous Ingestion Phase F)."""

from __future__ import annotations

import logging

from agent import spool
from agent.runtime.config import RuntimeConfig, SourceConfig
from agent.runtime.heartbeat import Heartbeat, compute_health_snapshot
from agent.runtime.status import RuntimeStatus


def _cfg(tmp_path):
    return RuntimeConfig(
        customer_id=100,
        branch_id=5,
        api_url="https://example.invalid",
        api_token="test-token",
        sources=(
            SourceConfig(name="checkins", path=str(tmp_path / "Checkins.txt")),
            SourceConfig(name="rejects", path=str(tmp_path / "Rejects.txt")),
            SourceConfig(name="acs", path=str(tmp_path / "ACS.txt")),
        ),
        state_path=tmp_path / "state" / "agent_state.json",
        spool_root=tmp_path / "spool",
        agent_identity_path=tmp_path / "agent_identity.json",
        log_dir=tmp_path / "logs",
        diagnostics_dir=tmp_path / "diagnostics",
    )


def _logger():
    logger = logging.getLogger("test-heartbeat")
    logger.addHandler(logging.NullHandler())
    return logger


def test_healthy_when_nothing_pending_and_no_failures(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "healthy"
    assert snapshot.pending_outbox_count == 0
    assert snapshot.quarantined_count == 0


def test_reflects_pending_backlog_across_all_sources(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    spool.write_batch(cfg.spool_root, "checkins", [{"a": 1}], source_generation=0, start_offset=0, end_offset=10)
    spool.write_batch(cfg.spool_root, "rejects", [{"a": 1}], source_generation=0, start_offset=0, end_offset=10)

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.pending_outbox_count == 2


def test_reflects_quarantined_count(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    spool.quarantine_records(cfg.spool_root, "checkins", [{"a": 1}], reason="test")

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.quarantined_count == 1
    assert snapshot.health_status == "degraded"


def test_reflects_auth_failure_state(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.record_upload_failure("auth_failure", "401 invalid token")

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "auth_failure"
    assert snapshot.last_failure_category == "auth_failure"
    assert snapshot.last_error == "401 invalid token"


def test_auth_failure_takes_precedence_over_quarantine(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    spool.quarantine_records(cfg.spool_root, "checkins", [{"a": 1}], reason="test")
    status.record_upload_failure("auth_failure", "bad token")

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "auth_failure"


def test_source_missing_marks_degraded(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.set_source_missing("checkins", True)

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "degraded"


def test_upload_success_clears_failure_state(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.record_upload_failure("retryable_infra", "connection refused")
    status.record_upload_success()

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "healthy"
    assert snapshot.last_failure_category is None
    assert snapshot.last_error is None
    assert snapshot.last_success_at is not None


def test_dead_collector_component_marks_health_degraded(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.record_component_started("collector")
    status.record_component_failed("collector", "simulated collector crash")

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "degraded"
    assert "collector" in snapshot.last_error


def test_dead_uploader_component_marks_health_degraded(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.record_component_started("uploader")
    status.record_component_failed("uploader", "simulated uploader crash")

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "degraded"
    assert "uploader" in snapshot.last_error


def test_dead_heartbeat_component_marks_health_degraded(tmp_path):
    # Self-referential caveat documented in the module docstring: this
    # proves the COMPUTATION correctly reflects a dead heartbeat if
    # something else (e.g. a diagnostics reader) calls it -- it does not
    # contradict the fact that a truly dead heartbeat can never actually
    # SEND this snapshot to the backend itself.
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.record_component_started("heartbeat")
    status.record_component_failed("heartbeat", "simulated heartbeat crash")

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "degraded"


def test_dead_housekeeping_component_marks_health_degraded(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.record_component_started("housekeeping")
    status.record_component_failed("housekeeping", "simulated housekeeping crash")

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "degraded"


def test_healthy_running_component_does_not_mark_degraded(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.record_component_started("collector")  # alive, never failed

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "healthy"


def test_dead_component_does_not_override_auth_failure_precedence(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.record_component_failed("collector", "crash")
    status.record_upload_failure("auth_failure", "401 invalid token")

    snapshot = compute_health_snapshot(cfg, status)

    assert snapshot.health_status == "auth_failure"


def test_last_error_never_contains_raw_token(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.record_upload_failure(
        "auth_failure",
        "authentication/authorization failure 401: invalid credentials",
    )

    snapshot = compute_health_snapshot(cfg, status)

    assert "test-token" not in (snapshot.last_error or "")


def test_send_once_posts_every_field_even_when_none(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, json, headers, timeout):
            self.calls.append((url, json))

            class R:
                status_code = 200

                def json(self):
                    return {"status": "success"}

            return R()

    session = FakeSession()
    heartbeat = Heartbeat(cfg, session, status, _logger())

    sent = heartbeat.send_once()

    assert sent is True
    payload = session.calls[0][1]
    for field_name in (
        "health_status", "pending_outbox_count", "quarantined_count",
        "oldest_pending_event_at", "last_success_at", "last_failure_category",
        "last_error", "watcher_last_active_at",
    ):
        assert field_name in payload
