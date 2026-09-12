"""Tests for agent/runtime/housekeeping.py (Continuous Ingestion Phase F)."""

from __future__ import annotations

import json
import logging
import time

from agent import spool
from agent.runtime.config import RuntimeConfig, SourceConfig
from agent.runtime.housekeeping import (
    check_disk_pressure,
    clean_stale_temp_files,
    write_diagnostics_snapshot,
)
from agent.runtime.status import RuntimeStatus


def _cfg(tmp_path, **overrides):
    kwargs = {
        "customer_id": 100,
        "branch_id": 5,
        "api_url": "https://example.invalid",
        "api_token": "test-token",
        "sources": (SourceConfig(name="checkins", path=str(tmp_path / "Checkins.txt")),),
        "state_path": tmp_path / "state" / "agent_state.json",
        "spool_root": tmp_path / "spool",
        "agent_identity_path": tmp_path / "agent_identity.json",
        "log_dir": tmp_path / "logs",
        "diagnostics_dir": tmp_path / "diagnostics",
        "disk_pressure_pending_bytes_threshold": 10 ** 12,
        "disk_pressure_min_free_bytes": 0,
    }
    kwargs.update(overrides)
    return RuntimeConfig(**kwargs)


def _logger():
    logger = logging.getLogger("test-housekeeping")
    logger.addHandler(logging.NullHandler())
    return logger


def test_no_pressure_when_below_both_thresholds(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()

    report = check_disk_pressure(cfg, status)

    assert report.paused is False
    assert status.is_disk_pressure_paused() is False


def test_pressure_when_pending_bytes_threshold_exceeded(tmp_path):
    cfg = _cfg(tmp_path, disk_pressure_pending_bytes_threshold=1)
    status = RuntimeStatus()
    spool.write_batch(cfg.spool_root, "checkins", [{"a": 1}], source_generation=0, start_offset=0, end_offset=10)

    report = check_disk_pressure(cfg, status)

    assert report.paused is True
    assert "pending spool bytes" in report.reason
    assert status.is_disk_pressure_paused() is True


def test_pressure_when_free_disk_threshold_exceeded(tmp_path):
    cfg = _cfg(tmp_path, disk_pressure_min_free_bytes=10 ** 18)  # unreasonably huge -- always "below"
    status = RuntimeStatus()

    report = check_disk_pressure(cfg, status)

    assert report.paused is True
    assert "free disk bytes" in report.reason


def test_pressure_resumes_once_pending_data_is_delivered(tmp_path):
    cfg = _cfg(tmp_path, disk_pressure_pending_bytes_threshold=1)
    status = RuntimeStatus()
    path = spool.write_batch(cfg.spool_root, "checkins", [{"a": 1}], source_generation=0, start_offset=0, end_offset=10)

    report1 = check_disk_pressure(cfg, status)
    assert report1.paused is True

    spool.acknowledge(path)
    report2 = check_disk_pressure(cfg, status)

    assert report2.paused is False
    assert status.is_disk_pressure_paused() is False


def test_pending_data_is_never_deleted_by_disk_pressure_check(tmp_path):
    cfg = _cfg(tmp_path, disk_pressure_pending_bytes_threshold=1)
    status = RuntimeStatus()
    spool.write_batch(cfg.spool_root, "checkins", [{"a": 1}], source_generation=0, start_offset=0, end_offset=10)

    check_disk_pressure(cfg, status)

    assert len(spool.list_pending_batches(cfg.spool_root, "checkins")) == 1


# --- stale temp file cleanup -------------------------------------------


def test_stale_temp_file_is_removed(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.spool_root).mkdir(parents=True)
    stale = cfg.spool_root / "orphan.tmp"
    stale.write_text("leftover", encoding="utf-8")
    old_time = time.time() - 7200
    import os
    os.utime(stale, (old_time, old_time))

    removed = clean_stale_temp_files(cfg, stale_age_seconds=3600)

    assert removed == 1
    assert not stale.exists()


def test_fresh_temp_file_is_not_removed(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.spool_root).mkdir(parents=True)
    fresh = cfg.spool_root / "in-progress.tmp"
    fresh.write_text("still writing", encoding="utf-8")

    removed = clean_stale_temp_files(cfg, stale_age_seconds=3600)

    assert removed == 0
    assert fresh.exists()


# --- diagnostics snapshot ------------------------------------------------


def test_diagnostics_snapshot_is_written_as_valid_json(tmp_path):
    cfg = _cfg(tmp_path)
    status = RuntimeStatus()
    status.set_source_position("checkins", generation=2, offset=1000)

    dest = write_diagnostics_snapshot(
        cfg, status, agent_id="agent-1", agent_version="1.0.0", started_at="2026-01-01T00:00:00.000000Z"
    )

    assert dest.exists()
    doc = json.loads(dest.read_text(encoding="utf-8"))
    assert doc["agent_id"] == "agent-1"
    assert doc["sources"]["checkins"]["generation"] == 2
    assert doc["sources"]["checkins"]["offset"] == 1000
