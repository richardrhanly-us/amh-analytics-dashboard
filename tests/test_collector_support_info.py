"""Tests for collector/support_info.py -- SortView Collector v1 (Phase
4c). Read-only, no network calls -- see module docstring.
"""

from __future__ import annotations

import json
import sys

from collector_token_support import store_api_token

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
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path)

    info = gather_support_info(str(config_path))

    assert info.config_loaded is True
    assert info.collector_version == __version__
    assert info.task_name == TASK_NAME
    assert info.python_executable == sys.executable
    assert info.last_status is None


def test_gather_support_info_reports_existing_status(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path)
    status_path = tmp_path / "data" / "status.json"
    write_status(status_path, {"status": "completed", "last_run": "2026-09-15T00:00:00.000000Z"})

    info = gather_support_info(str(config_path))

    assert info.last_status is not None
    assert info.last_status["status"] == "completed"


def test_gather_support_info_with_invalid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "valid-environment-token-0001")  # valid, and irrelevant: no api_token.dpapi
    config_path = _write_config(tmp_path)

    info = gather_support_info(str(config_path))

    assert info.config_loaded is False
    assert info.config_error is not None
    assert info.last_status is None


def test_to_dict_round_trips_through_json(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path)

    info = gather_support_info(str(config_path))
    doc = info.to_dict()

    assert json.loads(json.dumps(doc)) == doc


# --- CLI main() ------------------------------------------------------------


def test_main_returns_0_for_valid_config(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path)

    assert main(["--config", str(config_path)]) == 0


def test_main_returns_2_for_invalid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "valid-environment-token-0001")  # valid, and irrelevant: no api_token.dpapi
    config_path = _write_config(tmp_path)

    assert main(["--config", str(config_path)]) == 2


def test_main_writes_json_output(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path)
    output_path = tmp_path / "support-info.json"

    main(["--config", str(config_path), "--output", str(output_path)])

    doc = json.loads(output_path.read_text(encoding="utf-8"))
    assert doc["config_loaded"] is True
    assert doc["task_name"] == TASK_NAME


# --- API token: the SOURCE only, never the token --------------------------------------------------------------------------

SECRET = "CANARY-support-info-token-000000000001"


ENV_SECRET = "CANARY-support-info-env-token-00000001"


def test_support_info_with_only_an_environment_token_does_not_load_and_never_shows_it(tmp_path, monkeypatch, capsys):
    # No api_token.dpapi: a valid environment token is not a token source, and there is no "migration fallback" line.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_SECRET)
    output_path = tmp_path / "support-info.json"

    assert main(["--config", str(_write_config(tmp_path)), "--output", str(output_path)]) == 2

    out = capsys.readouterr().out
    written = output_path.read_text(encoding="utf-8")
    document = json.loads(written)
    assert document["config_loaded"] is False and document["api_token_source"] is None
    assert "Config did NOT load: Missing API token" in out and "API token source:" not in out
    for text in (out, written):
        assert ENV_SECRET not in text and "environment" not in text and "migration" not in text


def test_support_info_shows_a_dpapi_source_and_never_the_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_SECRET)  # present, and ignored
    store_api_token(monkeypatch, SECRET)
    output_path = tmp_path / "support-info.json"

    info = gather_support_info(str(_write_config(tmp_path)))
    assert main(["--config", str(_write_config(tmp_path)), "--output", str(output_path)]) == 0

    out = capsys.readouterr().out
    written = output_path.read_text(encoding="utf-8")
    assert info.api_token_source == "dpapi" and "API token source:    dpapi\n" in out  # the resolved source, printed as-is
    assert json.loads(written)["api_token_source"] == "dpapi"
    assert SECRET not in json.dumps(info.to_dict()) and SECRET not in repr(info)
    for text in (out, written):
        for secret in (SECRET, ENV_SECRET):
            assert secret not in text
        assert "migration" not in text and "fallback" not in text


def test_support_info_prints_whatever_source_was_resolved_with_no_special_case(capsys):
    from pathlib import Path

    from collector import support_info

    info = support_info.SupportInfo(
        collector_version="0", install_root="r", config_path="c", task_name="t", python_executable="p", python_version="3",
        config_loaded=True, config_error=None, last_status=None, api_token_source="some-source",
    )
    support_info._print_support_info(info)

    assert "API token source:    some-source\n" in capsys.readouterr().out
    source = Path(support_info.__file__).read_text(encoding="utf-8")
    assert "TOKEN_SOURCE_ENVIRONMENT" not in source and "migration fallback" not in source
