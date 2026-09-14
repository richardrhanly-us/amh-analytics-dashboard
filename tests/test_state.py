"""Tests for agent/state.py -- the durable, schema-versioned, atomically
written state layer (Continuous Ingestion Phase C) that replaces
agent/outbox.py's SQLite file_state table.

Central property under test throughout: THE CURSOR MAY LAG DURABLE
CAPTURE, BUT IT MUST NEVER LEAD IT. For this module specifically, that
means a read must never observe a torn/partially-written file, and must
never silently manufacture a plausible-looking cursor out of a corrupted
one.
"""

from __future__ import annotations

import json
import os

import pytest

from agent import state
from agent.discovery import SourceIdentity
from agent.tailer import FileCursor

# --- empty / initial state ------------------------------------------------


def test_empty_state_has_no_sources():
    s = state.empty_state()

    assert s.schema_version == state.SCHEMA_VERSION
    assert state.get_source(s, "checkins") is None


def test_load_state_with_no_file_returns_empty_state(tmp_path):
    path = tmp_path / "agent_state.json"

    loaded = state.load_state(path)

    assert loaded.schema_version == state.SCHEMA_VERSION
    assert loaded.sources == {}


# --- save/load round trip -------------------------------------------------


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "agent_state.json"
    s = state.update_source(
        state.empty_state(),
        "checkins",
        path="C:\\TLCFinalDlls\\Checkins.txt",
        cursor=FileCursor(identity=SourceIdentity(token=(1, 2)), offset=129987), generation=0,)

    state.save_state(path, s)
    loaded = state.load_state(path)

    checkins = state.get_source(loaded, "checkins")
    assert checkins is not None
    assert checkins.path == "C:\\TLCFinalDlls\\Checkins.txt"
    assert checkins.cursor == FileCursor(identity=SourceIdentity(token=(1, 2)), offset=129987)
    assert checkins.updated_at is not None


def test_round_trip_preserves_cursor_with_no_identity_yet(tmp_path):
    path = tmp_path / "agent_state.json"
    s = state.update_source(
        state.empty_state(),
        "checkins",
        path="C:\\TLCFinalDlls\\Checkins.txt",
        cursor=FileCursor(identity=None, offset=0), generation=0,)

    state.save_state(path, s)
    loaded = state.load_state(path)

    assert state.get_source(loaded, "checkins").cursor.identity is None


# --- three independent sources --------------------------------------------


def test_three_sources_persist_independently(tmp_path):
    path = tmp_path / "agent_state.json"
    s = state.empty_state()
    s = state.update_source(s, "checkins", path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 100), generation=0)
    s = state.update_source(s, "rejects", path="Rejects.txt", cursor=FileCursor(SourceIdentity((1, 2)), 50), generation=0)
    s = state.update_source(s, "acs", path="ACS Log.txt", cursor=FileCursor(SourceIdentity((1, 3)), 200), generation=0)

    state.save_state(path, s)
    loaded = state.load_state(path)

    assert state.get_source(loaded, "checkins").cursor.offset == 100
    assert state.get_source(loaded, "rejects").cursor.offset == 50
    assert state.get_source(loaded, "acs").cursor.offset == 200


def test_updating_one_source_does_not_disturb_the_others(tmp_path):
    s = state.empty_state()
    s = state.update_source(s, "checkins", path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 100), generation=0)
    s = state.update_source(s, "rejects", path="Rejects.txt", cursor=FileCursor(SourceIdentity((1, 2)), 50), generation=0)

    s = state.update_source(s, "checkins", path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 150), generation=0)

    assert state.get_source(s, "checkins").cursor.offset == 150
    assert state.get_source(s, "rejects").cursor.offset == 50  # untouched


def test_missing_source_entry_returns_none_not_a_default(tmp_path):
    path = tmp_path / "agent_state.json"
    s = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(None, 0), generation=0)
    state.save_state(path, s)

    loaded = state.load_state(path)

    # "rejects" was never persisted -- must be None, not a fabricated
    # zero-offset entry, so the caller can tell "never seen" apart from
    # "seen and at offset 0."
    assert state.get_source(loaded, "rejects") is None


# --- atomic replacement / durability --------------------------------------


def test_save_leaves_no_temp_files_behind(tmp_path):
    path = tmp_path / "agent_state.json"
    state.save_state(path, state.empty_state())

    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_repeated_saves_overwrite_cleanly(tmp_path):
    path = tmp_path / "agent_state.json"
    for offset in (10, 20, 30):
        s = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(None, offset), generation=0)
        state.save_state(path, s)

    loaded = state.load_state(path)
    assert state.get_source(loaded, "checkins").cursor.offset == 30

    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_interrupted_write_leaves_previous_valid_state_intact(tmp_path, monkeypatch):
    path = tmp_path / "agent_state.json"
    good = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(None, 111), generation=0)
    state.save_state(path, good)
    original_bytes = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(os, "fsync", boom)

    bad = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(None, 999), generation=0)
    with pytest.raises(OSError):
        state.save_state(path, bad)

    # The live file must be byte-for-byte what it was before the failed
    # save attempted anything -- os.replace is never reached.
    assert path.read_bytes() == original_bytes
    # And no orphaned temp file left behind.
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_save_fails_cleanly_when_destination_is_held_open_on_windows(tmp_path):
    """Empirical finding while building this phase: unlike POSIX,
    os.replace on Windows raises PermissionError if the destination has
    any open handle, even a same-process read handle opened with plain
    open(). This proves the failure is clean (raises, doesn't corrupt,
    doesn't leave a temp file) rather than a silent hang or partial
    write -- see the Phase C report for why this matters for Phase D."""
    if os.name != "nt":
        pytest.skip("Windows-specific os.replace behavior")

    path = tmp_path / "agent_state.json"
    good = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(None, 111), generation=0)
    state.save_state(path, good)

    handle = open(path)  # noqa: SIM115 -- deliberately kept open to reproduce the finding
    try:
        bad = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(None, 999), generation=0)
        with pytest.raises(PermissionError):
            state.save_state(path, bad)
    finally:
        handle.close()

    # Original content survives; no orphaned temp file.
    loaded = state.load_state(path)
    assert state.get_source(loaded, "checkins").cursor.offset == 111
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


# --- corruption handling ---------------------------------------------------


def test_malformed_json_raises_corrupt_state_error(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_non_object_root_raises_corrupt_state_error(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_missing_sources_key_raises_corrupt_state_error(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({"schema_version": state.SCHEMA_VERSION}), encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_unsupported_schema_version_raises(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({"schema_version": 999, "sources": {}}), encoding="utf-8")

    with pytest.raises(state.UnsupportedSchemaVersionError):
        state.load_state(path)


def test_negative_offset_in_file_raises_corrupt_state_error(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({
        "schema_version": state.SCHEMA_VERSION,
        "sources": {"checkins": {"path": "Checkins.txt", "identity": None, "offset": -5, "generation": 0, "updated_at": None}},
    }), encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_non_integer_offset_in_file_raises_corrupt_state_error(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({
        "schema_version": state.SCHEMA_VERSION,
        "sources": {"checkins": {"path": "Checkins.txt", "identity": None, "offset": "129987", "generation": 0, "updated_at": None}},
    }), encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_offset_exceeding_max_plausible_byte_count_raises_corrupt_state_error(tmp_path):
    # Regression test for the real-AMH-machine incident (see agent/tailer.py
    # and agent/state.py's OFFSET REPRESENTATION sections): a persisted
    # 52-digit "offset" -- the actual value a pre-fix opaque text-mode
    # cookie produced for a ~7MB ACS file -- must never be silently
    # accepted as a real byte count again, whatever schema version wrote
    # it or however it ended up in the file.
    path = tmp_path / "agent_state.json"
    huge_cookie_like_value = 1461501637671185285124623296198104371161417405207
    path.write_text(json.dumps({
        "schema_version": state.SCHEMA_VERSION,
        "sources": {
            "acs": {
                "path": "ACS Log.txt", "identity": [1, 1], "offset": huge_cookie_like_value,
                "generation": 0, "updated_at": None,
            }
        },
    }), encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_offset_at_max_plausible_boundary_is_accepted(tmp_path):
    path = tmp_path / "agent_state.json"
    s = state.update_source(
        state.empty_state(), "checkins", path="Checkins.txt",
        cursor=FileCursor(None, state._MAX_PLAUSIBLE_OFFSET), generation=0,
    )
    state.save_state(path, s)

    loaded = state.load_state(path)
    assert state.get_source(loaded, "checkins").cursor.offset == state._MAX_PLAUSIBLE_OFFSET


def test_offset_one_past_max_plausible_boundary_is_rejected_at_construction_time():
    with pytest.raises(state.CorruptStateError):
        state.SourceState(
            path="Checkins.txt", cursor=FileCursor(None, state._MAX_PLAUSIBLE_OFFSET + 1)
        )


def test_negative_offset_rejected_at_construction_time():
    with pytest.raises(state.CorruptStateError):
        state.SourceState(path="Checkins.txt", cursor=FileCursor(None, -1))


def test_malformed_identity_raises_corrupt_state_error(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({
        "schema_version": state.SCHEMA_VERSION,
        "sources": {"checkins": {"path": "Checkins.txt", "identity": "not-a-list", "offset": 0, "generation": 0, "updated_at": None}},
    }), encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_quarantine_corrupt_state_renames_aside_and_preserves_content(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)

    quarantined = state.quarantine_corrupt_state(path)

    assert quarantined is not None
    assert quarantined.exists()
    assert not path.exists()
    assert quarantined.read_text(encoding="utf-8") == "{not valid json"

    # And the caller can now deliberately proceed with a fresh state.
    fresh = state.load_state(path)
    assert fresh.sources == {}


def test_quarantine_with_no_file_returns_none(tmp_path):
    path = tmp_path / "does-not-exist.json"
    assert state.quarantine_corrupt_state(path) is None


# --- identity serialization -------------------------------------------------


def test_identity_round_trips_through_json_exactly(tmp_path):
    path = tmp_path / "agent_state.json"
    identity = SourceIdentity(token=(123456789, 42))
    s = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(identity, 10), generation=0)

    state.save_state(path, s)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["sources"]["checkins"]["identity"] == [123456789, 42]

    loaded = state.load_state(path)
    assert state.get_source(loaded, "checkins").cursor.identity == identity


def test_none_identity_round_trips_as_json_null(tmp_path):
    path = tmp_path / "agent_state.json"
    s = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(None, 0), generation=0)

    state.save_state(path, s)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["sources"]["checkins"]["identity"] is None


def test_deserialized_identity_compares_equal_to_a_fresh_identify_result(tmp_path):
    # Guards against a subtle tuple-vs-list bug: JSON has no tuple type,
    # so deserialization must convert back to a real tuple or equality
    # against a live discovery.identify() result would silently always
    # be False.
    path = tmp_path / "agent_state.json"
    real_file = tmp_path / "checkins.txt"
    real_file.write_text("data", encoding="utf-8")

    from agent.discovery import identify

    real_identity = identify(str(real_file))
    s = state.update_source(state.empty_state(), "checkins", path=str(real_file), cursor=FileCursor(real_identity, 4), generation=0)
    state.save_state(path, s)

    loaded = state.load_state(path)
    assert state.get_source(loaded, "checkins").cursor.identity == real_identity


# --- path / identity change, truncation-adjacent behavior -----------------


def test_path_changed_true_when_configured_path_differs(tmp_path):
    s = state.update_source(state.empty_state(), "checkins", path="C:\\Old\\Checkins.txt", cursor=FileCursor(None, 0), generation=0)
    checkins = state.get_source(s, "checkins")

    assert state.path_changed(checkins, "C:\\New\\Checkins.txt") is True
    assert state.path_changed(checkins, "C:\\Old\\Checkins.txt") is False


def test_path_changed_false_when_no_prior_source_state():
    assert state.path_changed(None, "C:\\Anything.txt") is False


def test_state_stores_whatever_identity_it_is_given_after_a_rotation(tmp_path):
    # state.py doesn't decide what a rotation means -- it just persists
    # the new cursor tailer.py hands it. This proves that update after an
    # identity change is a plain, unremarkable overwrite -- generation is
    # computed by the caller via advance_generation, exactly as a real
    # tailer-driven caller would, and must bump on a true rotation.
    path = tmp_path / "agent_state.json"
    s = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 5000), generation=0)
    state.save_state(path, s)

    # Simulate tailer.py detecting a rotation and resetting to a new
    # identity at offset 0.
    prior = state.get_source(state.load_state(path), "checkins")
    next_generation = state.advance_generation(prior, rotated=True, truncated=False)
    s2 = state.update_source(state.load_state(path), "checkins", path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 2)), 0), generation=next_generation)
    state.save_state(path, s2)

    loaded = state.load_state(path)
    checkins = state.get_source(loaded, "checkins")
    assert checkins.cursor.identity == SourceIdentity((1, 2))
    assert checkins.cursor.offset == 0
    assert checkins.generation == 1


def test_state_stores_whatever_offset_it_is_given_after_a_truncation(tmp_path):
    # Same principle for truncation: same identity, offset reset to 0 by
    # the tailer -- state.py just records it faithfully. Truncation also
    # bumps generation, exactly like rotation -- see advance_generation.
    path = tmp_path / "agent_state.json"
    identity = SourceIdentity((1, 1))
    s = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(identity, 5000), generation=0)
    state.save_state(path, s)

    prior = state.get_source(state.load_state(path), "checkins")
    next_generation = state.advance_generation(prior, rotated=False, truncated=True)
    s2 = state.update_source(state.load_state(path), "checkins", path="Checkins.txt", cursor=FileCursor(identity, 0), generation=next_generation)
    state.save_state(path, s2)

    loaded = state.load_state(path)
    checkins = state.get_source(loaded, "checkins")
    assert checkins.cursor.identity == identity
    assert checkins.cursor.offset == 0
    assert checkins.generation == 1


# --- repeated updates / process-restart style reload -----------------------


def test_many_repeated_updates_then_reload(tmp_path):
    path = tmp_path / "agent_state.json"
    s = state.empty_state()

    for offset in range(0, 1000, 37):
        s = state.update_source(s, "checkins", path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), offset), generation=0)
        state.save_state(path, s)

    loaded = state.load_state(path)
    assert state.get_source(loaded, "checkins").cursor.offset == max(range(0, 1000, 37))


def test_process_restart_style_reload_is_indistinguishable_from_original(tmp_path):
    path = tmp_path / "agent_state.json"
    s = state.empty_state()
    s = state.update_source(s, "checkins", path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 100), generation=0)
    s = state.update_source(s, "rejects", path="Rejects.txt", cursor=FileCursor(SourceIdentity((1, 2)), 50), generation=0)
    s = state.update_source(s, "acs", path="ACS Log.txt", cursor=FileCursor(SourceIdentity((1, 3)), 200), generation=0)
    state.save_state(path, s)

    # "Restart the process": nothing but the file on disk survives.
    reloaded = state.load_state(path)

    assert reloaded.schema_version == s.schema_version
    assert set(reloaded.sources) == set(s.sources)
    for name in state.SOURCE_NAMES:
        assert reloaded.sources[name].cursor == s.sources[name].cursor
        assert reloaded.sources[name].path == s.sources[name].path


# --- advance_generation (Phase D correction: logical source generation) ----


def test_advance_generation_is_zero_on_first_ever_bootstrap():
    # prior is None: there is no earlier stream to be a continuation of.
    assert state.advance_generation(None, rotated=False, truncated=False) == 0
    # Even a (nonsensical) rotated/truncated signal with no prior is still 0
    # -- there is nothing to have rotated FROM.
    assert state.advance_generation(None, rotated=True, truncated=False) == 0
    assert state.advance_generation(None, rotated=False, truncated=True) == 0


def test_advance_generation_unchanged_on_normal_append():
    prior = state.SourceState(path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 100), generation=7)

    assert state.advance_generation(prior, rotated=False, truncated=False) == 7


def test_advance_generation_bumps_on_rotation():
    prior = state.SourceState(path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 8_010_000), generation=12)

    assert state.advance_generation(prior, rotated=True, truncated=False) == 13


def test_advance_generation_bumps_on_truncation():
    # Truncation resets offsets toward 0 exactly like rotation does, so it
    # must bump generation too -- otherwise a pre-truncation pending batch
    # would still numerically outrank a chronologically later one.
    prior = state.SourceState(path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 400_000), generation=3)

    assert state.advance_generation(prior, rotated=False, truncated=True) == 4


def test_advance_generation_bumps_once_even_if_both_flags_set():
    prior = state.SourceState(path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 5000), generation=1)

    assert state.advance_generation(prior, rotated=True, truncated=True) == 2


def test_advance_generation_unchanged_across_a_process_restart_style_reload(tmp_path):
    # A restart with no new tailer read never calls advance_generation at
    # all -- the persisted generation simply survives the round trip.
    path = tmp_path / "agent_state.json"
    s = state.update_source(state.empty_state(), "checkins", path="Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 100), generation=5)
    state.save_state(path, s)

    reloaded = state.get_source(state.load_state(path), "checkins")

    assert reloaded.generation == 5


def test_advance_generation_treats_a_path_change_via_rotation_detection_as_a_bump():
    # state.py doesn't special-case a configured path change -- it relies
    # on the tailer already reporting rotated=True the first time the new
    # path is stat'd (a different identity). advance_generation just
    # applies the same rule it always does.
    prior = state.SourceState(path="C:\\Old\\Checkins.txt", cursor=FileCursor(SourceIdentity((1, 1)), 9000), generation=2)

    assert state.advance_generation(prior, rotated=True, truncated=False) == 3


# --- schema version support (v3: true byte-offset representation) ---------
#
# v1->v2 auto-migration existed and was safe (purely additive: a missing
# `generation` field defaulted to 0). v2->v3 is NOT auto-migrated -- see
# agent/state.py's OFFSET REPRESENTATION docstring section: a v2 document's
# `offset` values are opaque text-mode cookies, not true byte counts, and
# are not safely reinterpretable as the byte offsets v3 requires. Both v1
# and v2 documents now hit the same "unsupported version" path as any
# other unrecognized version.


def test_v1_document_no_longer_loads(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "sources": {
            "checkins": {"path": "Checkins.txt", "identity": [1, 1], "offset": 12345, "updated_at": "2026-01-01T00:00:00.000000Z"},
        },
    }), encoding="utf-8")

    with pytest.raises(state.UnsupportedSchemaVersionError):
        state.load_state(path)


def test_v2_document_no_longer_loads(tmp_path):
    # Even a structurally well-formed v2 document (generation present,
    # offset non-negative and small) must not load -- the offset's MEANING
    # changed, not just its schema shape, so no v2 document can be assumed
    # safe to reinterpret regardless of its actual field values.
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({
        "schema_version": 2,
        "sources": {
            "checkins": {"path": "Checkins.txt", "identity": None, "offset": 500, "generation": 0, "updated_at": None},
        },
    }), encoding="utf-8")

    with pytest.raises(state.UnsupportedSchemaVersionError):
        state.load_state(path)


def test_v1_and_v2_are_not_in_migratable_versions():
    assert 1 not in state._MIGRATABLE_SCHEMA_VERSIONS
    assert 2 not in state._MIGRATABLE_SCHEMA_VERSIONS


def test_current_schema_version_document_requires_generation_field_on_each_source(tmp_path):
    # A document at the current schema version is never migrated -- a
    # missing generation field on it is corruption, not something to
    # default.
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({
        "schema_version": state.SCHEMA_VERSION,
        "sources": {
            "checkins": {"path": "Checkins.txt", "identity": None, "offset": 0, "updated_at": None},
        },
    }), encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_negative_generation_in_file_raises_corrupt_state_error(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({
        "schema_version": state.SCHEMA_VERSION,
        "sources": {
            "checkins": {"path": "Checkins.txt", "identity": None, "offset": 0, "generation": -1, "updated_at": None},
        },
    }), encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_non_integer_generation_in_file_raises_corrupt_state_error(tmp_path):
    path = tmp_path / "agent_state.json"
    path.write_text(json.dumps({
        "schema_version": state.SCHEMA_VERSION,
        "sources": {
            "checkins": {"path": "Checkins.txt", "identity": None, "offset": 0, "generation": "12", "updated_at": None},
        },
    }), encoding="utf-8")

    with pytest.raises(state.CorruptStateError):
        state.load_state(path)


def test_negative_generation_rejected_at_construction_time():
    with pytest.raises(state.CorruptStateError):
        state.SourceState(path="Checkins.txt", cursor=FileCursor(None, 0), generation=-1)
