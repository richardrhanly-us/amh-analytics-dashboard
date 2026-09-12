"""Tests for agent/event_identity.py -- deterministic source-event
identity (Continuous Ingestion Phase E).
"""

from __future__ import annotations

import hashlib

import pytest

from agent import event_identity


def _compute(**overrides):
    params = {
        "agent_id": "agent-abc-123",
        "source": "checkins",
        "generation": 12,
        "start_offset": 8_001_234,
    }
    params.update(overrides)
    return event_identity.compute_source_event_id(**params)


# --- 1. same input -> same id, repeatedly -----------------------------------


def test_same_inputs_produce_the_same_id_every_time():
    first = _compute()
    second = _compute()
    third = _compute()

    assert first == second == third


def test_id_is_a_64_char_lowercase_hex_digest():
    result = _compute()

    assert len(result) == 64
    assert result == result.lower()
    int(result, 16)  # raises ValueError if not valid hex


# --- 2-5. each component changing the id -------------------------------


def test_different_offset_produces_different_id():
    assert _compute(start_offset=1) != _compute(start_offset=2)


def test_different_generation_produces_different_id():
    assert _compute(generation=1) != _compute(generation=2)


def test_different_logical_source_produces_different_id():
    assert _compute(source="checkins") != _compute(source="rejects") != _compute(source="acs")


def test_different_agent_id_produces_different_id():
    assert _compute(agent_id="agent-one") != _compute(agent_id="agent-two")


def test_all_components_independently_change_the_id():
    # Sanity check that no two distinct (agent, source, generation,
    # offset) tuples collide across a small combinatorial sweep.
    seen = set()
    for agent_id in ("agent-a", "agent-b"):
        for source in ("checkins", "rejects", "acs"):
            for generation in (0, 1):
                for start_offset in (0, 100):
                    event_id = event_identity.compute_source_event_id(
                        agent_id=agent_id, source=source, generation=generation, start_offset=start_offset
                    )
                    assert event_id not in seen
                    seen.add(event_id)


# --- 6-7. clock/process independence ----------------------------------------


def test_module_never_imports_time_or_process_state():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(event_identity))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any(name in ("time", "datetime", "os", "uuid") for name in imported)


def test_repeated_calls_across_simulated_restart_are_identical():
    # A "process restart" changes nothing about this module's inputs or
    # outputs -- it's a pure function with no persisted/in-memory state
    # of its own. Re-invoking it fresh (as a new process would) must
    # reproduce the exact same id.
    before = _compute()
    after = _compute()

    assert before == after


# --- canonical string / hash construction -----------------------------------


def test_canonical_identity_string_matches_documented_format():
    result = event_identity.canonical_identity_string(
        agent_id="agent-id", source="checkins", generation=12, start_offset=8001234
    )

    assert result == "agent-id|checkins|12|8001234"


def test_compute_source_event_id_is_sha256_of_canonical_string():
    canonical = event_identity.canonical_identity_string(
        agent_id="agent-id", source="checkins", generation=12, start_offset=8001234
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    result = event_identity.compute_source_event_id(
        agent_id="agent-id", source="checkins", generation=12, start_offset=8001234
    )

    assert result == expected


# --- malformed component handling -------------------------------------------


def test_empty_agent_id_is_rejected():
    with pytest.raises(event_identity.EventIdentityError):
        _compute(agent_id="")


def test_empty_source_is_rejected():
    with pytest.raises(event_identity.EventIdentityError):
        _compute(source="")


def test_negative_generation_is_rejected():
    with pytest.raises(event_identity.EventIdentityError):
        _compute(generation=-1)


def test_negative_offset_is_rejected():
    with pytest.raises(event_identity.EventIdentityError):
        _compute(start_offset=-1)


def test_agent_id_containing_separator_is_rejected():
    # A pipe inside a component could otherwise make two different
    # logical identities hash to the same canonical string, e.g.
    # ("a|b", "c", ...) vs ("a", "b|c", ...).
    with pytest.raises(event_identity.EventIdentityError):
        _compute(agent_id="agent|with|pipes")


def test_source_containing_separator_is_rejected():
    with pytest.raises(event_identity.EventIdentityError):
        _compute(source="check|ins")


# --- attach_source_event_id --------------------------------------------------


def test_attach_source_event_id_adds_field_without_mutating_input():
    record = {"barcode": "12345"}

    result = event_identity.attach_source_event_id(
        record, agent_id="agent-abc", source="checkins", generation=0, start_offset=0
    )

    assert "source_event_id" not in record  # original untouched
    assert result["barcode"] == "12345"
    assert result["source_event_id"] == event_identity.compute_source_event_id(
        agent_id="agent-abc", source="checkins", generation=0, start_offset=0
    )


def test_attach_source_event_id_is_deterministic_across_calls():
    record = {"barcode": "12345"}

    first = event_identity.attach_source_event_id(
        record, agent_id="agent-abc", source="checkins", generation=3, start_offset=500
    )
    second = event_identity.attach_source_event_id(
        record, agent_id="agent-abc", source="checkins", generation=3, start_offset=500
    )

    assert first["source_event_id"] == second["source_event_id"]
