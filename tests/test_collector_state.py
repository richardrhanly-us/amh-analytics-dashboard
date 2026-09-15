"""Tests for collector/state.py -- SortView Collector v1 (Phase 4a)."""

from __future__ import annotations

import json
import os

import pytest

from collector import state as state_mod

# --- atomic_write_json primitive -----------------------------------------


def test_atomic_write_leaves_no_temp_files_behind(tmp_path):
    path = tmp_path / "state.json"
    state_mod.atomic_write_json(path, {"a": 1})

    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_atomic_write_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state_mod.atomic_write_json(path, {"a": 1, "b": [1, 2, 3]})

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2, 3]}


def test_interrupted_write_leaves_previous_valid_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state_mod.atomic_write_json(path, {"a": "good"})
    original_bytes = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(os, "fsync", boom)

    with pytest.raises(OSError):
        state_mod.atomic_write_json(path, {"a": "bad"})

    assert path.read_bytes() == original_bytes
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_write_fails_cleanly_when_destination_is_held_open_on_windows(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-specific os.replace behavior")

    path = tmp_path / "state.json"
    state_mod.atomic_write_json(path, {"a": "good"})

    handle = open(path)  # noqa: SIM115 -- deliberately kept open
    try:
        with pytest.raises(PermissionError):
            state_mod.atomic_write_json(path, {"a": "bad"})
    finally:
        handle.close()

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "good"}


# --- state load/save -------------------------------------------------------


def test_load_state_with_no_file_returns_empty_state(tmp_path):
    loaded = state_mod.load_state(tmp_path / "state.json")
    assert loaded.sources == {}
    assert loaded.schema_version == state_mod.STATE_SCHEMA_VERSION


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "state.json"
    s = state_mod.with_source(
        state_mod.empty_state(), "checkins",
        state_mod.SourceState(identity=(1, 2), offset=129987),
    )
    state_mod.save_state(path, s)

    loaded = state_mod.load_state(path)
    checkins = state_mod.get_source(loaded, "checkins")
    assert checkins == state_mod.SourceState(identity=(1, 2), offset=129987)


def test_none_identity_round_trips(tmp_path):
    path = tmp_path / "state.json"
    s = state_mod.with_source(state_mod.empty_state(), "checkins", state_mod.SourceState(identity=None, offset=0))
    state_mod.save_state(path, s)

    loaded = state_mod.load_state(path)
    assert state_mod.get_source(loaded, "checkins").identity is None


def test_three_sources_persist_independently(tmp_path):
    path = tmp_path / "state.json"
    s = state_mod.empty_state()
    s = state_mod.with_source(s, "checkins", state_mod.SourceState((1, 1), 100))
    s = state_mod.with_source(s, "rejects", state_mod.SourceState((1, 2), 50))
    s = state_mod.with_source(s, "acs", state_mod.SourceState((1, 3), 200))
    state_mod.save_state(path, s)

    loaded = state_mod.load_state(path)
    assert state_mod.get_source(loaded, "checkins").offset == 100
    assert state_mod.get_source(loaded, "rejects").offset == 50
    assert state_mod.get_source(loaded, "acs").offset == 200


def test_updating_one_source_does_not_disturb_others(tmp_path):
    s = state_mod.empty_state()
    s = state_mod.with_source(s, "checkins", state_mod.SourceState((1, 1), 100))
    s = state_mod.with_source(s, "rejects", state_mod.SourceState((1, 2), 50))
    s = state_mod.with_source(s, "checkins", state_mod.SourceState((1, 1), 150))

    assert state_mod.get_source(s, "checkins").offset == 150
    assert state_mod.get_source(s, "rejects").offset == 50


def test_missing_source_entry_returns_none_not_a_default(tmp_path):
    path = tmp_path / "state.json"
    s = state_mod.with_source(state_mod.empty_state(), "checkins", state_mod.SourceState(None, 0))
    state_mod.save_state(path, s)

    loaded = state_mod.load_state(path)
    assert state_mod.get_source(loaded, "rejects") is None


# --- corruption handling ---------------------------------------------------


def test_malformed_json_raises_corrupt_state_error(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(state_mod.CorruptStateError):
        state_mod.load_state(path)


def test_wrong_schema_version_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 999, "sources": {}}), encoding="utf-8")

    with pytest.raises(state_mod.CorruptStateError):
        state_mod.load_state(path)


def test_negative_offset_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "sources": {"checkins": {"identity": None, "offset": -5}},
    }), encoding="utf-8")

    with pytest.raises(state_mod.CorruptStateError):
        state_mod.load_state(path)


def test_offset_exceeding_max_plausible_value_raises(tmp_path):
    # Regression coverage for the real-world incident that motivated this
    # bound in the first place (an opaque text-mode cookie masquerading
    # as a byte offset) -- see collector/reader.py and agent/state.py.
    path = tmp_path / "state.json"
    huge_cookie_like_value = 1461501637671185285124623296198104371161417405207
    path.write_text(json.dumps({
        "schema_version": 1,
        "sources": {"acs": {"identity": [1, 1], "offset": huge_cookie_like_value}},
    }), encoding="utf-8")

    with pytest.raises(state_mod.CorruptStateError):
        state_mod.load_state(path)


def test_negative_offset_rejected_at_construction_time():
    with pytest.raises(state_mod.CorruptStateError):
        state_mod.SourceState(identity=None, offset=-1)


def test_malformed_identity_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "sources": {"checkins": {"identity": "not-a-list", "offset": 0}},
    }), encoding="utf-8")

    with pytest.raises(state_mod.CorruptStateError):
        state_mod.load_state(path)


def test_quarantine_corrupt_file_renames_aside_and_preserves_content(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(state_mod.CorruptStateError):
        state_mod.load_state(path)

    quarantined = state_mod.quarantine_corrupt_file(path)

    assert quarantined is not None
    assert quarantined.exists()
    assert not path.exists()
    assert quarantined.read_text(encoding="utf-8") == "{not valid json"

    fresh = state_mod.load_state(path)
    assert fresh.sources == {}


def test_quarantine_with_no_file_returns_none(tmp_path):
    assert state_mod.quarantine_corrupt_file(tmp_path / "does-not-exist.json") is None


# --- status -----------------------------------------------------------------


def test_load_status_with_no_file_returns_empty_dict(tmp_path):
    assert state_mod.load_status(tmp_path / "status.json") == {}


def test_write_and_load_status_round_trip(tmp_path):
    path = tmp_path / "status.json"
    status = {"last_attempt": "2026-09-15T00:00:00.000000Z", "status": "completed", "checkins_rows": 3}
    state_mod.write_status(path, status)

    assert state_mod.load_status(path) == status


def test_load_status_with_corrupt_file_returns_empty_dict_not_raise(tmp_path):
    # Unlike state, a broken status file is a reporting artifact, not a
    # cursor -- losing it is not a correctness risk, so this deliberately
    # does NOT raise (see module docstring).
    path = tmp_path / "status.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert state_mod.load_status(path) == {}


def test_status_write_is_atomic_and_survives_interrupted_write(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    state_mod.write_status(path, {"status": "completed"})

    def boom(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(os, "fsync", boom)

    with pytest.raises(OSError):
        state_mod.write_status(path, {"status": "started"})

    assert state_mod.load_status(path) == {"status": "completed"}
