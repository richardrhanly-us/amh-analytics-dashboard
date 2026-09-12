"""Tests for agent/identity.py -- durable, locally-generated agent
installation identity (Continuous Ingestion Phase E).
"""

from __future__ import annotations

import json

import pytest

from agent import identity


def test_first_call_creates_and_returns_a_new_agent_id(tmp_path):
    path = tmp_path / "agent_identity.json"

    agent_id = identity.load_or_create_agent_id(path)

    assert isinstance(agent_id, str)
    assert agent_id
    assert path.exists()


def test_repeated_calls_return_the_same_id(tmp_path):
    path = tmp_path / "agent_identity.json"

    first = identity.load_or_create_agent_id(path)
    second = identity.load_or_create_agent_id(path)

    assert first == second


def test_process_restart_style_reload_returns_the_same_id(tmp_path):
    # "Restart": nothing in-memory survives except what's on disk.
    path = tmp_path / "agent_identity.json"
    original = identity.load_or_create_agent_id(path)

    reloaded = identity.load_or_create_agent_id(path)

    assert reloaded == original


def test_different_paths_get_different_ids(tmp_path):
    id_a = identity.load_or_create_agent_id(tmp_path / "a.json")
    id_b = identity.load_or_create_agent_id(tmp_path / "b.json")

    assert id_a != id_b


def test_reinstall_style_deletion_mints_a_new_id(tmp_path):
    # Simulates the exact scenario agent/event_identity.py's design
    # depends on: a lost/recreated identity file (reinstall, state-loss)
    # must produce a genuinely different agent_id, not reuse the old one.
    path = tmp_path / "agent_identity.json"
    original = identity.load_or_create_agent_id(path)

    path.unlink()

    reinstalled = identity.load_or_create_agent_id(path)

    assert reinstalled != original


def test_atomic_write_leaves_no_temp_files_behind(tmp_path):
    path = tmp_path / "agent_identity.json"
    identity.load_or_create_agent_id(path)

    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_persisted_file_contains_agent_id_key(tmp_path):
    path = tmp_path / "agent_identity.json"
    agent_id = identity.load_or_create_agent_id(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["agent_id"] == agent_id


# --- corruption handling ---------------------------------------------------


def test_malformed_json_raises_corrupt_agent_identity_error(tmp_path):
    path = tmp_path / "agent_identity.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(identity.CorruptAgentIdentityError):
        identity.load_or_create_agent_id(path)


def test_non_object_root_raises_corrupt_agent_identity_error(tmp_path):
    path = tmp_path / "agent_identity.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(identity.CorruptAgentIdentityError):
        identity.load_or_create_agent_id(path)


def test_missing_agent_id_key_raises_corrupt_agent_identity_error(tmp_path):
    path = tmp_path / "agent_identity.json"
    path.write_text(json.dumps({"not_agent_id": "whatever"}), encoding="utf-8")

    with pytest.raises(identity.CorruptAgentIdentityError):
        identity.load_or_create_agent_id(path)


def test_empty_string_agent_id_raises_corrupt_agent_identity_error(tmp_path):
    path = tmp_path / "agent_identity.json"
    path.write_text(json.dumps({"agent_id": ""}), encoding="utf-8")

    with pytest.raises(identity.CorruptAgentIdentityError):
        identity.load_or_create_agent_id(path)


def test_non_string_agent_id_raises_corrupt_agent_identity_error(tmp_path):
    path = tmp_path / "agent_identity.json"
    path.write_text(json.dumps({"agent_id": 12345}), encoding="utf-8")

    with pytest.raises(identity.CorruptAgentIdentityError):
        identity.load_or_create_agent_id(path)


def test_corrupt_file_never_silently_regenerates_a_replacement_id(tmp_path):
    # A silent replacement here would quietly change every future event
    # ID's namespace with no operator visibility -- must raise instead.
    path = tmp_path / "agent_identity.json"
    path.write_text("garbage", encoding="utf-8")

    with pytest.raises(identity.CorruptAgentIdentityError):
        identity.load_or_create_agent_id(path)

    # The corrupt file itself is left untouched -- no silent overwrite.
    assert path.read_text(encoding="utf-8") == "garbage"
