"""Tests for agent/runtime/config.py (Continuous Ingestion Phase F)."""

from __future__ import annotations

import json

import pytest

from agent.runtime.config import (
    BootstrapMode,
    ConfigError,
    SourceConfig,
    load_runtime_config,
)


def _write_config(tmp_path, **overrides):
    doc = {
        "customer_id": 100,
        "branch_id": 5,
        "api_url": "https://example.invalid/",
        "sources": [{"name": "checkins", "path": str(tmp_path / "Checkins.txt")}],
        "state_path": str(tmp_path / "state" / "agent_state.json"),
        "spool_root": str(tmp_path / "spool"),
        "agent_identity_path": str(tmp_path / "agent_identity.json"),
        "log_dir": str(tmp_path / "logs"),
        "diagnostics_dir": str(tmp_path / "diagnostics"),
    }
    doc.update(overrides)
    path = tmp_path / "runtime_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_loads_valid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path)

    cfg = load_runtime_config(path)

    assert cfg.customer_id == 100
    assert cfg.api_url == "https://example.invalid"  # trailing slash stripped
    assert cfg.api_token == "test-token"
    assert cfg.sources[0].name == "checkins"
    assert cfg.batch_max_events == 100  # default


def test_missing_api_token_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    path = _write_config(tmp_path)

    with pytest.raises(ConfigError, match="API token"):
        load_runtime_config(path)


def test_missing_config_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")

    with pytest.raises(ConfigError, match="not found"):
        load_runtime_config(tmp_path / "does-not-exist.json")


def test_missing_required_key_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    del doc["spool_root"]
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(ConfigError, match="spool_root"):
        load_runtime_config(path)


def test_empty_sources_list_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, sources=[])

    with pytest.raises(ConfigError, match="sources"):
        load_runtime_config(path)


def test_numeric_override_from_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, batch_max_events=50)

    cfg = load_runtime_config(path)

    assert cfg.batch_max_events == 50


def test_env_var_overrides_json_value(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    monkeypatch.setenv("SORTVIEW_RUNTIME_BATCH_MAX_EVENTS", "7")
    path = _write_config(tmp_path, batch_max_events=50)

    cfg = load_runtime_config(path)

    assert cfg.batch_max_events == 7


def test_offset_bootstrap_requires_offset_value():
    with pytest.raises(ConfigError, match="bootstrap_offset"):
        SourceConfig(name="checkins", path="x", bootstrap_mode=BootstrapMode.OFFSET, bootstrap_offset=None)


def test_negative_bootstrap_offset_rejected():
    with pytest.raises(ConfigError):
        SourceConfig(name="checkins", path="x", bootstrap_mode=BootstrapMode.OFFSET, bootstrap_offset=-1)


def test_source_lookup_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path)
    cfg = load_runtime_config(path)

    assert cfg.source("checkins").name == "checkins"
    with pytest.raises(ConfigError, match="no source"):
        cfg.source("does-not-exist")


# --- shadow / capture-only validation mode ----------------------------------


def test_upload_and_heartbeat_enabled_by_default(tmp_path, monkeypatch):
    # Production-safe default: a config that never mentions these keys at
    # all must behave exactly like today's production behavior.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path)

    cfg = load_runtime_config(path)

    assert cfg.upload_enabled is True
    assert cfg.heartbeat_enabled is True


def test_shadow_mode_flags_read_from_json_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, upload_enabled=False, heartbeat_enabled=False)

    cfg = load_runtime_config(path)

    assert cfg.upload_enabled is False
    assert cfg.heartbeat_enabled is False


def test_shadow_mode_flag_env_override_accepts_common_spellings(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    monkeypatch.setenv("SORTVIEW_RUNTIME_UPLOAD_ENABLED", "false")
    path = _write_config(tmp_path)

    cfg = load_runtime_config(path)

    assert cfg.upload_enabled is False


def test_shadow_mode_flag_env_override_true_beats_json_false(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    monkeypatch.setenv("SORTVIEW_RUNTIME_HEARTBEAT_ENABLED", "true")
    path = _write_config(tmp_path, heartbeat_enabled=False)

    cfg = load_runtime_config(path)

    assert cfg.heartbeat_enabled is True


def test_invalid_boolean_string_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, upload_enabled="not-a-boolean")

    with pytest.raises(ConfigError, match="boolean"):
        load_runtime_config(path)


def test_mode_description_production(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path)
    cfg = load_runtime_config(path)

    assert "PRODUCTION" in cfg.mode_description
    assert "SHADOW" not in cfg.mode_description


def test_mode_description_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, upload_enabled=False, heartbeat_enabled=False)
    cfg = load_runtime_config(path)

    assert "SHADOW" in cfg.mode_description
    assert "PRODUCTION" not in cfg.mode_description


def test_mode_description_custom_mixed_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, upload_enabled=False, heartbeat_enabled=True)
    cfg = load_runtime_config(path)

    assert "CUSTOM" in cfg.mode_description
