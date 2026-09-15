"""Tests for collector/config.py -- SortView Collector v1 (Phase 4a)."""

from __future__ import annotations

import json

import pytest

from collector.config import ConfigError, load_config


def _write_config(tmp_path, **overrides):
    doc = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://sortview-app-2p336.ondigitalocean.app",
        "sources": [
            {"name": "checkins", "path": "C:\\TLCFinalDlls\\Checkins.txt"},
            {"name": "rejects", "path": "C:\\TLCFinalDlls\\Rejects.txt"},
            {"name": "acs", "path": "C:\\TLCFinalDlls\\ACS Log.txt"},
        ],
        "state_path": str(tmp_path / "data" / "state.json"),
        "status_path": str(tmp_path / "data" / "status.json"),
        "log_path": str(tmp_path / "logs" / "collector.log"),
    }
    doc.update(overrides)
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_loads_valid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path)

    cfg = load_config(path)

    assert cfg.customer_id == 1
    assert cfg.branch_id == 1
    assert cfg.api_url == "https://sortview-app-2p336.ondigitalocean.app"
    assert cfg.api_token == "test-token"
    assert [s.name for s in cfg.sources] == ["checkins", "rejects", "acs"]
    assert cfg.source("acs").path == "C:\\TLCFinalDlls\\ACS Log.txt"


def test_api_url_trailing_slash_is_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, api_url="https://example.invalid/")

    cfg = load_config(path)
    assert cfg.api_url == "https://example.invalid"


def test_missing_config_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.json")


def test_missing_required_key_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    doc = {"customer_id": 1}
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_api_token_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    path = _write_config(tmp_path)

    with pytest.raises(ConfigError):
        load_config(path)


def test_api_token_never_read_from_config_file(tmp_path, monkeypatch):
    # Even if a token-shaped field is present in the file, it must be
    # ignored -- the token comes from the environment only.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "real-token")
    path = _write_config(tmp_path, api_token="token-from-file-must-be-ignored")

    cfg = load_config(path)
    assert cfg.api_token == "real-token"


def test_empty_sources_list_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, sources=[])

    with pytest.raises(ConfigError):
        load_config(path)


def test_source_missing_name_or_path_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, sources=[{"name": "checkins"}])

    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_source_names_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, sources=[
        {"name": "checkins", "path": "a.txt"},
        {"name": "checkins", "path": "b.txt"},
    ])

    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_source_lookup_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    cfg = load_config(_write_config(tmp_path))

    with pytest.raises(ConfigError):
        cfg.source("does-not-exist")


def test_numeric_overrides_from_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, max_records_per_batch=250, http_read_timeout=120.0)

    cfg = load_config(path)
    assert cfg.max_records_per_batch == 250
    assert cfg.http_read_timeout == 120.0


def test_numeric_defaults_when_not_specified(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    cfg = load_config(_write_config(tmp_path))

    assert cfg.max_records_per_batch == 1000
    assert cfg.http_connect_timeout == 10.0
    assert cfg.http_read_timeout == 60.0
