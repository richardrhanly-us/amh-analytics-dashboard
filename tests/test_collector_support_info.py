"""Tests for collector/support_info.py -- SortView Collector v1 (Phase
4c). Read-only, no network calls -- see module docstring.
"""

from __future__ import annotations

import json
import sys

from collector import __version__
from collector.state import write_status
from collector.support_info import gather_support_info, main
from collector.task_settings import TASK_NAME


def _write_config(tmp_path, **overrides):
    doc = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://example.invalid",
        "sources": [{"name": "checkins", "path": str(tmp_path / "Checkins.txt")}],
        "state_path": str(tmp_path / "data" / "state.json"),
        "status_path": str(tmp_path / "data" / "status.json"),
        "log_path": str(tmp_path / "logs" / "collector.log"),
    }
    doc.update(overrides)
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_gather_support_info_with_valid_config_no_status_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)

    info = gather_support_info(str(config_path))

    assert info.config_loaded is True
    assert info.collector_version == __version__
    assert info.task_name == TASK_NAME
    assert info.python_executable == sys.executable
    assert info.last_status is None


def test_gather_support_info_reports_existing_status(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    status_path = tmp_path / "data" / "status.json"
    write_status(status_path, {"status": "completed", "last_run": "2026-09-15T00:00:00.000000Z"})

    info = gather_support_info(str(config_path))

    assert info.last_status is not None
    assert info.last_status["status"] == "completed"


def test_gather_support_info_with_invalid_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    config_path = _write_config(tmp_path)

    info = gather_support_info(str(config_path))

    assert info.config_loaded is False
    assert info.config_error is not None
    assert info.last_status is None


def test_to_dict_round_trips_through_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)

    info = gather_support_info(str(config_path))
    doc = info.to_dict()

    assert json.loads(json.dumps(doc)) == doc


# --- CLI main() ------------------------------------------------------------


def test_main_returns_0_for_valid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)

    assert main(["--config", str(config_path)]) == 0


def test_main_returns_2_for_invalid_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    config_path = _write_config(tmp_path)

    assert main(["--config", str(config_path)]) == 2


def test_main_writes_json_output(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    output_path = tmp_path / "support-info.json"

    main(["--config", str(config_path), "--output", str(output_path)])

    doc = json.loads(output_path.read_text(encoding="utf-8"))
    assert doc["config_loaded"] is True
    assert doc["task_name"] == TASK_NAME
