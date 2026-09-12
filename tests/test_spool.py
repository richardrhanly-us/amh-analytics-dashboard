"""Tests for agent/spool.py -- the durable NDJSON spool layer
(Continuous Ingestion Phase D) that replaces agent/outbox.py's SQLite
local_events table.

Central properties under test:
  - a batch is never observable in a partial/torn state
  - write_batch's return contract makes "durable" and "installed"
    synonymous -- there is no path to a caller believing a batch is safe
    before it actually is
  - nothing here ever touches a cursor -- that's Phase E/F's job
  - quarantine only ever moves content aside, never deletes it
  - retryable-infra and auth failures NEVER quarantine valid data,
    regardless of how many attempts accumulate (Phase D correction #1)
  - ordering survives restart, clock rollback, and timestamp collision
    because it's keyed on source offsets, not wall-clock time
    (Phase D correction #2)
"""

from __future__ import annotations

import json
import os

import pytest

from agent import spool

SOURCE = "checkins"


def _spool_root(tmp_path):
    return tmp_path / "spool"


def _write(root, records, start=0, end=None, generation=0):
    if end is None:
        end = start + 100
    return spool.write_batch(
        root, SOURCE, records, source_generation=generation, start_offset=start, end_offset=end
    )


# --- successful write -------------------------------------------------


def test_write_batch_returns_a_path_that_exists_and_is_readable(tmp_path):
    root = _spool_root(tmp_path)
    records = [{"barcode": "111"}, {"barcode": "222"}]

    path = _write(root, records, start=0, end=100)

    assert path.exists()
    assert spool.read_batch(path) == records


def test_write_batch_lands_under_pending_source_subdirectory(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    assert path.parent == root / "pending" / SOURCE
    assert path.suffix == ".ndjson"


def test_write_batch_rejects_empty_records():
    with pytest.raises(ValueError):
        spool.write_batch("irrelevant", SOURCE, [], source_generation=0, start_offset=0, end_offset=100)


def test_write_batch_rejects_non_positive_offset_range():
    with pytest.raises(ValueError):
        spool.write_batch(
            "irrelevant", SOURCE, [{"a": 1}], source_generation=0, start_offset=100, end_offset=100
        )
    with pytest.raises(ValueError):
        spool.write_batch(
            "irrelevant", SOURCE, [{"a": 1}], source_generation=0, start_offset=200, end_offset=100
        )


def test_write_batch_rejects_negative_generation():
    with pytest.raises(ValueError):
        spool.write_batch(
            "irrelevant", SOURCE, [{"a": 1}], source_generation=-1, start_offset=0, end_offset=100
        )


def test_write_batch_serializes_one_json_object_per_line(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}, {"b": 2}])

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": 2}]


def test_batch_metadata_exposes_offset_range(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}], start=500, end=900, generation=0)

    metadata = spool.parse_batch_filename(path)

    assert metadata.generation == 0
    assert metadata.start_offset == 500
    assert metadata.end_offset == 900
    assert metadata.created_at is not None


def test_batch_metadata_exposes_generation(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}], start=0, end=10_000, generation=13)

    metadata = spool.parse_batch_filename(path)

    assert metadata.generation == 13
    assert metadata.start_offset == 0
    assert metadata.end_offset == 10_000


def test_parse_batch_filename_returns_none_for_unrecognized_name(tmp_path):
    assert spool.parse_batch_filename(tmp_path / "not-a-batch.ndjson") is None
    assert spool.parse_batch_filename(tmp_path / "batch-notanumber-100-1-1-1-abc.ndjson") is None


# --- interrupted write leaves nothing partial visible ---------------------


def test_interrupted_write_leaves_no_final_or_partial_batch_visible(tmp_path, monkeypatch):
    root = _spool_root(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(os, "fsync", boom)

    with pytest.raises(OSError):
        _write(root, [{"a": 1}])

    pending_dir = root / "pending" / SOURCE
    assert spool.list_pending_batches(root, SOURCE) == []
    if pending_dir.exists():
        assert list(pending_dir.iterdir()) == []


def test_interrupted_write_does_not_disturb_previously_written_batches(tmp_path, monkeypatch):
    root = _spool_root(tmp_path)
    good_path = _write(root, [{"a": 1}], start=0, end=100)
    original_bytes = good_path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        _write(root, [{"b": 2}], start=100, end=200)

    assert good_path.read_bytes() == original_bytes
    assert spool.list_pending_batches(root, SOURCE) == [good_path]


# --- restart discovers already-durable batches -----------------------------


def test_restart_style_reload_discovers_existing_pending_batches(tmp_path):
    root = _spool_root(tmp_path)
    path_a = _write(root, [{"a": 1}], start=0, end=100)
    path_b = _write(root, [{"b": 2}], start=100, end=200)

    rediscovered = spool.list_pending_batches(root, SOURCE)

    assert rediscovered == sorted([path_a, path_b])


# --- deterministic oldest-first enumeration, keyed on source offset -------


def test_pending_batches_enumerate_oldest_first_by_offset(tmp_path):
    root = _spool_root(tmp_path)
    first = _write(root, [{"seq": 1}], start=0, end=100)
    second = _write(root, [{"seq": 2}], start=100, end=200)
    third = _write(root, [{"seq": 3}], start=200, end=300)

    assert spool.list_pending_batches(root, SOURCE) == [first, second, third]


def test_enumeration_is_stable_across_repeated_calls(tmp_path):
    root = _spool_root(tmp_path)
    _write(root, [{"seq": 1}], start=0, end=100)
    _write(root, [{"seq": 2}], start=100, end=200)

    assert spool.list_pending_batches(root, SOURCE) == spool.list_pending_batches(root, SOURCE)


def test_enumeration_empty_when_no_pending_directory_exists(tmp_path):
    root = _spool_root(tmp_path)
    assert spool.list_pending_batches(root, SOURCE) == []


def test_ordering_survives_identical_timestamps(tmp_path, monkeypatch):
    """A prior version of this filename scheme used wall-clock time as
    the primary sort key and broke under this exact scenario: two calls
    to time.time_ns() returned the identical value in a real test run on
    this machine. Offset-first ordering is immune to that -- confirmed
    here by freezing the clock entirely and relying only on offsets to
    keep 10 rapid writes in correct order."""
    monkeypatch.setattr(spool.time, "time_ns", lambda: 1_700_000_000_000_000_000)

    root = _spool_root(tmp_path)
    paths = [_write(root, [{"seq": i}], start=i * 100, end=(i + 1) * 100) for i in range(10)]

    assert spool.list_pending_batches(root, SOURCE) == paths


def test_ordering_survives_clock_moving_backward(tmp_path, monkeypatch):
    """Simulates an NTP sync or manual clock correction mid-run: the
    wall clock reported by time.time_ns() jumps backward between two
    writes. Offset-first ordering must not be disturbed by this at all,
    since offsets never come from the wall clock."""
    root = _spool_root(tmp_path)

    monkeypatch.setattr(spool.time, "time_ns", lambda: 2_000_000_000_000_000_000)
    first = _write(root, [{"seq": 1}], start=0, end=100)

    # Clock jumps backward by a huge margin -- would sort BEFORE `first`
    # under a timestamp-first scheme, but must not under offset-first.
    monkeypatch.setattr(spool.time, "time_ns", lambda: 1_000_000_000_000_000_000)
    second = _write(root, [{"seq": 2}], start=100, end=200)

    assert spool.list_pending_batches(root, SOURCE) == [first, second]


def test_ordering_survives_process_restart_style_sequence_reset(tmp_path, monkeypatch):
    """Simulates a restart: the in-process sequence counter and a frozen
    clock are both reset back to their starting values, exactly as a
    fresh process would begin. A batch written "before restart" must
    still sort before one written "after," because its offset is lower
    -- unaffected by the counter/clock reset."""
    root = _spool_root(tmp_path)

    monkeypatch.setattr(spool.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    before_restart = _write(root, [{"seq": 1}], start=0, end=100)

    # "Restart": reset the sequence counter and reuse the same frozen
    # timestamp value a fresh process would also plausibly observe.
    import itertools

    monkeypatch.setattr(spool, "_sequence_counter", itertools.count())
    after_restart = _write(root, [{"seq": 2}], start=100, end=200)

    assert spool.list_pending_batches(root, SOURCE) == [before_restart, after_restart]


# --- generation-aware ordering (logical source generation correction) -----


def test_pending_batches_enumerate_oldest_first_within_one_generation(tmp_path):
    root = _spool_root(tmp_path)
    first = _write(root, [{"seq": 1}], start=0, end=100, generation=5)
    second = _write(root, [{"seq": 2}], start=100, end=200, generation=5)
    third = _write(root, [{"seq": 3}], start=200, end=300, generation=5)

    assert spool.list_pending_batches(root, SOURCE) == [first, second, third]


def test_higher_generation_always_sorts_after_lower_generation_regardless_of_offset(tmp_path):
    """The exact starvation scenario this correction exists to fix: a
    still-pending pre-rotation batch (generation 12, high offsets) must
    sort BEFORE a post-rotation batch (generation 13, offsets reset near
    0), even though 8_000_000 > 0 -- offset alone would get this
    backwards."""
    root = _spool_root(tmp_path)

    pre_rotation = _write(root, [{"seq": "old"}], start=8_000_000, end=8_010_000, generation=12)
    post_rotation = _write(root, [{"seq": "new"}], start=0, end=10_000, generation=13)

    assert spool.list_pending_batches(root, SOURCE) == [pre_rotation, post_rotation]


def test_many_generations_of_offset_resets_still_sort_correctly(tmp_path):
    """Simulates several rotations in a row, each resetting the offset
    back toward 0 -- generation must still dominate every time."""
    root = _spool_root(tmp_path)

    gen0 = _write(root, [{"g": 0}], start=900_000, end=1_000_000, generation=0)
    gen1 = _write(root, [{"g": 1}], start=0, end=5_000, generation=1)
    gen2 = _write(root, [{"g": 2}], start=500_000, end=600_000, generation=2)
    gen3 = _write(root, [{"g": 3}], start=0, end=1_000, generation=3)

    assert spool.list_pending_batches(root, SOURCE) == [gen0, gen1, gen2, gen3]


def test_ordering_across_rotation_survives_identical_timestamps(tmp_path, monkeypatch):
    """Combines the timestamp-collision regression with a rotation: even
    with the wall clock frozen (so the old timestamp-first scheme would
    have been ambiguous or wrong), generation-then-offset still orders a
    post-rotation batch after a pre-rotation one correctly."""
    monkeypatch.setattr(spool.time, "time_ns", lambda: 1_700_000_000_000_000_000)

    root = _spool_root(tmp_path)
    pre_rotation = _write(root, [{"seq": "old"}], start=8_000_000, end=8_010_000, generation=12)
    post_rotation = _write(root, [{"seq": "new"}], start=0, end=10_000, generation=13)

    assert spool.list_pending_batches(root, SOURCE) == [pre_rotation, post_rotation]


def test_ordering_across_rotation_survives_clock_moving_backward(tmp_path, monkeypatch):
    """The wall clock jumping backward between the pre- and post-rotation
    writes must not disturb generation-then-offset ordering."""
    root = _spool_root(tmp_path)

    monkeypatch.setattr(spool.time, "time_ns", lambda: 2_000_000_000_000_000_000)
    pre_rotation = _write(root, [{"seq": "old"}], start=8_000_000, end=8_010_000, generation=12)

    monkeypatch.setattr(spool.time, "time_ns", lambda: 1_000_000_000_000_000_000)
    post_rotation = _write(root, [{"seq": "new"}], start=0, end=10_000, generation=13)

    assert spool.list_pending_batches(root, SOURCE) == [pre_rotation, post_rotation]


def test_ordering_across_rotation_survives_process_restart_style_sequence_reset(tmp_path, monkeypatch):
    """A process restart between the pre- and post-rotation writes resets
    the in-process sequence counter, but the persisted generation (which
    would be reloaded from agent/state.py's state file in real operation)
    is what actually governs ordering here -- unaffected by the reset."""
    import itertools

    root = _spool_root(tmp_path)

    monkeypatch.setattr(spool.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    pre_rotation = _write(root, [{"seq": "old"}], start=8_000_000, end=8_010_000, generation=12)

    # "Restart": reset the sequence counter, reuse the same frozen
    # timestamp a fresh process could also plausibly observe.
    monkeypatch.setattr(spool, "_sequence_counter", itertools.count())
    post_rotation = _write(root, [{"seq": "new"}], start=0, end=10_000, generation=13)

    assert spool.list_pending_batches(root, SOURCE) == [pre_rotation, post_rotation]


def test_ordering_survives_machine_restart_style_cold_rediscovery(tmp_path):
    """Simulates a full machine restart: batches written by an earlier
    process (pre-rotation) sit untouched on disk, a new process starts
    completely fresh (new sequence counter via re-import semantics, no
    in-memory knowledge of anything), writes the post-rotation batch, and
    a cold `list_pending_batches` call must still enumerate correctly --
    proving nothing about correct ordering here depends on in-memory
    state surviving the restart, only on-disk filenames do."""
    root = _spool_root(tmp_path)
    pre_rotation = _write(root, [{"seq": "old"}], start=8_000_000, end=8_010_000, generation=12)

    # Nothing in-memory carries over "after the restart" except what's on
    # disk -- nothing further is stubbed or reset, this call is exactly
    # what a freshly-started process would do after reading generation=13
    # back from agent/state.py's persisted state file.
    post_rotation = _write(root, [{"seq": "new"}], start=0, end=10_000, generation=13)

    rediscovered = spool.list_pending_batches(root, SOURCE)
    assert rediscovered == [pre_rotation, post_rotation]


def test_offset_reset_after_rotation_does_not_starve_older_pending_batch(tmp_path):
    """End-to-end proof of the fix: write several post-rotation batches
    with small offsets while the pre-rotation batch is still pending
    (e.g. stuck retrying) -- it must never be starved out of first
    position by an unbounded stream of newer, lower-offset batches."""
    root = _spool_root(tmp_path)

    pre_rotation = _write(root, [{"seq": "old"}], start=8_000_000, end=8_010_000, generation=12)
    post_rotation_batches = [
        _write(root, [{"seq": f"new-{i}"}], start=i * 1000, end=(i + 1) * 1000, generation=13)
        for i in range(20)
    ]

    ordering = spool.list_pending_batches(root, SOURCE)
    assert ordering[0] == pre_rotation
    assert ordering[1:] == post_rotation_batches


# --- acknowledgment ---------------------------------------------------


def test_acknowledge_removes_only_the_acknowledged_batch(tmp_path):
    root = _spool_root(tmp_path)
    path_a = _write(root, [{"a": 1}], start=0, end=100)
    path_b = _write(root, [{"b": 2}], start=100, end=200)

    spool.acknowledge(path_a)

    remaining = spool.list_pending_batches(root, SOURCE)
    assert remaining == [path_b]
    assert not path_a.exists()


def test_acknowledge_is_idempotent(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    spool.acknowledge(path)
    spool.acknowledge(path)  # must not raise

    assert spool.list_pending_batches(root, SOURCE) == []


def test_acknowledge_also_removes_attempts_sidecar(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])
    spool.record_attempt_failure(path, "boom", spool.FailureCategory.RETRYABLE_INFRA)
    assert spool._attempts_sidecar_path(path).exists()

    spool.acknowledge(path)

    assert not spool._attempts_sidecar_path(path).exists()


# --- failure classification (Phase D correction #1) ------------------------


def test_retryable_infra_failure_never_quarantines_even_after_many_attempts(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])
    original_bytes = path.read_bytes()

    for i in range(50):
        record = spool.record_attempt_failure(
            path, f"connection refused #{i}", spool.FailureCategory.RETRYABLE_INFRA
        )
        assert record.attempts == i + 1

    # Still pending, content untouched, no matter how many attempts.
    assert path.exists()
    assert path.read_bytes() == original_bytes
    assert spool.list_pending_batches(root, SOURCE) == [path]
    assert (root / "quarantine" / SOURCE).exists() is False


def test_auth_failure_never_quarantines_even_after_many_attempts(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    for i in range(50):
        spool.record_attempt_failure(path, f"401 unauthorized #{i}", spool.FailureCategory.AUTH_FAILURE)

    assert path.exists()
    assert spool.list_pending_batches(root, SOURCE) == [path]
    assert (root / "quarantine" / SOURCE).exists() is False


def test_permanent_rejection_category_alone_does_not_auto_quarantine(tmp_path):
    """record_attempt_failure never quarantines on its own, regardless of
    category -- even PERMANENT_REJECTION requires a separate, explicit
    quarantine_batch call once a caller (not this module) has isolated
    the batch to its smallest practical unit. This module does not make
    that isolation decision."""
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    spool.record_attempt_failure(path, "400 bad request", spool.FailureCategory.PERMANENT_REJECTION)

    assert path.exists()
    assert spool.list_pending_batches(root, SOURCE) == [path]


def test_attempt_record_reflects_category_and_error(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    record = spool.record_attempt_failure(path, "boom", spool.FailureCategory.AUTH_FAILURE)

    assert record.attempts == 1
    assert record.category == "auth_failure"
    assert record.last_error == "boom"
    assert record.last_attempt_at is not None


def test_failed_attempt_increments_counter(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    first = spool.record_attempt_failure(path, "err1", spool.FailureCategory.RETRYABLE_INFRA)
    second = spool.record_attempt_failure(path, "err2", spool.FailureCategory.RETRYABLE_INFRA)

    assert first.attempts == 1
    assert second.attempts == 2


def test_retry_counter_persists_across_restart_style_reload(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])
    spool.record_attempt_failure(path, "err1", spool.FailureCategory.RETRYABLE_INFRA)
    spool.record_attempt_failure(path, "err2", spool.FailureCategory.AUTH_FAILURE)

    # "Restart": read the sidecar back fresh, exactly as a new process
    # would after finding this batch still pending.
    reloaded = spool._load_attempts(path)

    assert reloaded.attempts == 2
    assert reloaded.category == "auth_failure"
    assert reloaded.last_error == "err2"


# --- explicit quarantine (separate from failure-count tracking) -----------


def test_explicit_quarantine_after_caller_determines_permanent_rejection(tmp_path):
    """The correct flow for a permanent, isolated rejection: the caller
    (future Phase E/F logic, simulated here) records the attempt for
    diagnostics, then explicitly decides to quarantine -- two separate
    calls, never automatic."""
    root = _spool_root(tmp_path)
    records = [{"barcode": "111"}]
    path = _write(root, records)

    spool.record_attempt_failure(path, "400 bad request", spool.FailureCategory.PERMANENT_REJECTION)
    dest = spool.quarantine_batch(root, SOURCE, path, reason="isolated poison event, 400 on retry")

    assert not path.exists()
    assert dest.exists()
    assert spool.read_batch(dest) == records
    # The attempts sidecar's accurate count moves along with the batch.
    moved_sidecar = spool._attempts_sidecar_path(dest)
    assert json.loads(moved_sidecar.read_text(encoding="utf-8"))["attempts"] == 1


def test_quarantine_writes_a_reason_sidecar(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    dest = spool.quarantine_batch(root, SOURCE, path, reason="fatal error text")

    reason_path = spool._reason_sidecar_path(dest)
    assert reason_path.exists()
    reason = json.loads(reason_path.read_text(encoding="utf-8"))
    assert "fatal error text" in reason["reason"]
    assert "quarantined_at" in reason


# --- corrupt/malformed batch quarantine ------------------------------------


def test_malformed_json_line_raises_corrupt_batch_error(tmp_path):
    root = _spool_root(tmp_path)
    path = root / "pending" / SOURCE / "batch-1-2-3-4-deadbeef.ndjson"
    path.parent.mkdir(parents=True)
    path.write_text('{"a": 1}\nnot valid json\n', encoding="utf-8")

    with pytest.raises(spool.CorruptBatchError):
        spool.read_batch(path)


def test_non_object_line_raises_corrupt_batch_error(tmp_path):
    root = _spool_root(tmp_path)
    path = root / "pending" / SOURCE / "batch-1-2-3-4-deadbeef.ndjson"
    path.parent.mkdir(parents=True)
    path.write_text('{"a": 1}\n"just a string"\n', encoding="utf-8")

    with pytest.raises(spool.CorruptBatchError):
        spool.read_batch(path)


def test_blank_lines_are_tolerated_not_treated_as_corrupt(tmp_path):
    root = _spool_root(tmp_path)
    path = root / "pending" / SOURCE / "batch-1-2-3-4-deadbeef.ndjson"
    path.parent.mkdir(parents=True)
    path.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")

    assert spool.read_batch(path) == [{"a": 1}, {"b": 2}]


def test_read_batch_or_quarantine_quarantines_corrupt_batch_and_returns_none(tmp_path):
    root = _spool_root(tmp_path)
    path = root / "pending" / SOURCE / "batch-1-2-3-4-deadbeef.ndjson"
    path.parent.mkdir(parents=True)
    path.write_text("not json at all", encoding="utf-8")

    result = spool.read_batch_or_quarantine(root, SOURCE, path)

    assert result is None
    assert not path.exists()
    quarantined = list((root / "quarantine" / SOURCE).iterdir())
    assert any(p.suffix == ".ndjson" for p in quarantined)


def test_read_batch_or_quarantine_returns_records_for_a_good_batch(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    result = spool.read_batch_or_quarantine(root, SOURCE, path)

    assert result == [{"a": 1}]
    assert path.exists()  # untouched -- only corrupt batches get quarantined


# --- quarantine never silently deletes evidence ----------------------------


def test_quarantine_never_deletes_the_batch_content(tmp_path):
    root = _spool_root(tmp_path)
    path = root / "pending" / SOURCE / "batch-1-2-3-4-deadbeef.ndjson"
    path.parent.mkdir(parents=True)
    raw_content = '{"a": 1}\ngarbage\n'
    path.write_text(raw_content, encoding="utf-8")

    dest = spool.quarantine_batch(root, SOURCE, path, reason="test")

    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == raw_content
    assert not path.exists()


def test_quarantine_disambiguates_a_naming_collision_instead_of_overwriting(tmp_path):
    root = _spool_root(tmp_path)
    quarantine_dir = root / "quarantine" / SOURCE
    quarantine_dir.mkdir(parents=True)
    existing = quarantine_dir / "batch-1-2-3-4-deadbeef.ndjson"
    existing.write_text("original evidence", encoding="utf-8")

    incoming = root / "pending" / SOURCE / "batch-1-2-3-4-deadbeef.ndjson"
    incoming.parent.mkdir(parents=True)
    incoming.write_text("new evidence", encoding="utf-8")

    dest = spool.quarantine_batch(root, SOURCE, incoming, reason="collision test")

    assert existing.read_text(encoding="utf-8") == "original evidence"
    assert dest != existing
    assert dest.read_text(encoding="utf-8") == "new evidence"


# --- independent handling of multiple pending batches ----------------------


def test_multiple_batches_are_independently_acknowledged_or_retried(tmp_path):
    root = _spool_root(tmp_path)
    path_a = _write(root, [{"a": 1}], start=0, end=100)
    path_b = _write(root, [{"b": 2}], start=100, end=200)
    path_c = _write(root, [{"c": 3}], start=200, end=300)

    spool.acknowledge(path_a)
    spool.record_attempt_failure(path_b, "transient", spool.FailureCategory.RETRYABLE_INFRA)
    # path_c untouched

    remaining = spool.list_pending_batches(root, SOURCE)
    assert remaining == [path_b, path_c]
    assert spool._load_attempts(path_b).attempts == 1
    assert spool._load_attempts(path_c).attempts == 0


def test_sources_are_independent_of_each_other(tmp_path):
    root = _spool_root(tmp_path)
    checkins_path = spool.write_batch(
        root, "checkins", [{"a": 1}], source_generation=0, start_offset=0, end_offset=100
    )
    rejects_path = spool.write_batch(
        root, "rejects", [{"b": 2}], source_generation=0, start_offset=0, end_offset=100
    )

    spool.acknowledge(checkins_path)

    assert spool.list_pending_batches(root, "checkins") == []
    assert spool.list_pending_batches(root, "rejects") == [rejects_path]


# --- Windows-safe short-lived handles ---------------------------------------


def test_read_then_acknowledge_does_not_hold_a_handle_open(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    records = spool.read_batch(path)
    spool.acknowledge(path)  # must not raise

    assert records == [{"a": 1}]
    assert not path.exists()


def test_read_then_quarantine_does_not_hold_a_handle_open(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    spool.read_batch(path)
    dest = spool.quarantine_batch(root, SOURCE, path, reason="test")  # must not raise

    assert dest.exists()


def test_write_batch_handle_is_closed_before_batch_is_readable(tmp_path):
    root = _spool_root(tmp_path)
    path = _write(root, [{"a": 1}])

    with open(path, encoding="utf-8") as f:
        content = f.read()

    assert json.loads(content.strip()) == {"a": 1}


# --- crash-boundary behavior supporting "cursor may lag, never lead" ------


def test_write_batch_never_returns_a_path_before_content_is_on_disk(tmp_path):
    root = _spool_root(tmp_path)

    path = _write(root, [{"a": 1}])

    assert path.exists()
    assert spool.read_batch(path) == [{"a": 1}]


def test_spool_module_never_imports_state_or_tailer_module():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(spool))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any("state" in name for name in imported)
    assert not any("tailer" in name for name in imported)


def test_a_failed_write_batch_call_can_be_safely_retried(tmp_path, monkeypatch):
    """If the caller's cursor-advance logic never ran (because
    write_batch raised), retrying the exact same logical write must be
    safe -- proving a crash between "read source" and "durable spool
    write" just means re-reading, never data loss or corruption."""
    root = _spool_root(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        _write(root, [{"a": 1}], start=0, end=100)

    monkeypatch.undo()

    path = _write(root, [{"a": 1}], start=0, end=100)
    assert spool.read_batch(path) == [{"a": 1}]


# --- quarantine_records (Phase F: poison-event isolation primitive) -------


def test_quarantine_records_writes_directly_to_quarantine_without_a_pending_file(tmp_path):
    root = _spool_root(tmp_path)
    records = [{"barcode": "POISON1"}]

    dest = spool.quarantine_records(root, SOURCE, records, reason="isolated poison event")

    assert dest.exists()
    assert dest.parent == root / "quarantine" / SOURCE
    assert spool.read_batch(dest) == records
    assert spool.list_pending_batches(root, SOURCE) == []  # never touched pending/


def test_quarantine_records_writes_a_reason_sidecar(tmp_path):
    root = _spool_root(tmp_path)

    dest = spool.quarantine_records(root, SOURCE, [{"a": 1}], reason="400 bad request on isolated record")

    reason_path = spool._reason_sidecar_path(dest)
    assert reason_path.exists()
    reason = json.loads(reason_path.read_text(encoding="utf-8"))
    assert "400 bad request" in reason["reason"]
    assert "quarantined_at" in reason


def test_quarantine_records_rejects_empty_list():
    with pytest.raises(ValueError):
        spool.quarantine_records("irrelevant", SOURCE, [], reason="nothing to quarantine")


def test_quarantine_records_multiple_records_in_one_call(tmp_path):
    root = _spool_root(tmp_path)
    records = [{"barcode": "P1"}, {"barcode": "P2"}]

    dest = spool.quarantine_records(root, SOURCE, records, reason="isolated pair")

    assert spool.read_batch(dest) == records


def test_quarantine_records_counted_in_spool_stats(tmp_path):
    root = _spool_root(tmp_path)
    spool.quarantine_records(root, SOURCE, [{"a": 1}], reason="test")

    stats = spool.get_spool_stats(root, SOURCE)
    assert stats.quarantined_batch_count == 1
    assert stats.quarantined_bytes > 0


# --- diagnostics -------------------------------------------------------


def test_spool_stats_reflect_pending_and_quarantined_state(tmp_path):
    root = _spool_root(tmp_path)
    _write(root, [{"a": 1}], start=0, end=100)
    bad_path = _write(root, [{"b": 2}], start=100, end=200)
    spool.quarantine_batch(root, SOURCE, bad_path, reason="fatal")

    stats = spool.get_spool_stats(root, SOURCE)

    assert stats.pending_batch_count == 1
    assert stats.pending_bytes > 0
    assert stats.oldest_pending_created_at is not None
    assert stats.quarantined_batch_count == 1
    assert stats.quarantined_bytes > 0


def test_spool_stats_empty_when_nothing_exists(tmp_path):
    root = _spool_root(tmp_path)
    stats = spool.get_spool_stats(root, SOURCE)

    assert stats.pending_batch_count == 0
    assert stats.pending_bytes == 0
    assert stats.oldest_pending_created_at is None
    assert stats.quarantined_batch_count == 0
    assert stats.quarantined_bytes == 0
