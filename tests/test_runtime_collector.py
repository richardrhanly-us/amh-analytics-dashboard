"""Tests for agent/runtime/collector.py -- the canonical continuous
collector (Continuous Ingestion Phase F).

Central property under test throughout: THE CURSOR MAY LAG DURABLE
CAPTURE, BUT MUST NEVER LEAD DURABLE CAPTURE. A crash between spool
publication and cursor persistence must be safe to reread from; a crash
before spool publication must leave the cursor completely unchanged.
"""

from __future__ import annotations

import os

from agent import discovery, spool, state
from agent.discovery import SourceIdentity
from agent.runtime.collector import SourceCollector
from agent.runtime.config import BootstrapMode, SourceConfig

CHECKIN_LINE = "Book A|{barcode}|MLFIC|FIC A|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM\n"


def _make_collector(tmp_path, *, name="checkins", path=None, bootstrap_mode=BootstrapMode.NORMAL,
                     bootstrap_offset=None, batch_max_events=100, batch_max_seconds=2.0, time_fn=None,
                     agent_id="agent-1"):
    source_cfg = SourceConfig(
        name=name, path=str(path), bootstrap_mode=bootstrap_mode, bootstrap_offset=bootstrap_offset
    )
    kwargs = {
        "agent_id": agent_id,
        "customer_id": 100,
        "branch_id": 5,
        "state_path": tmp_path / "state" / "agent_state.json",
        "spool_root": tmp_path / "spool",
        "batch_max_events": batch_max_events,
        "batch_max_seconds": batch_max_seconds,
    }
    if time_fn is not None:
        kwargs["time_fn"] = time_fn
    return SourceCollector(source_cfg, **kwargs)


def _pending_records(tmp_path, source="checkins"):
    batches = spool.list_pending_batches(tmp_path / "spool", source)
    records = []
    for b in batches:
        records.extend(spool.read_batch(b))
    return records


_SENTINEL_ROTATED_IDENTITY = SourceIdentity(token=(-1, -1))


def _simulate_rotation(monkeypatch, path):
    """Deterministically simulates Tech Logic rotating `path`: writes new
    content at the same path (as a real rotation would leave behind) and
    forces discovery.identify(path) to report a different SourceIdentity
    for it, regardless of what the OS actually does to the underlying
    inode/file-id for a delete+recreate at this path.

    This exists because the OS does NOT guarantee a delete+recreate at
    the same path gets a new inode/file-id -- POSIX only guarantees
    identity uniqueness among currently-existing files, not across a
    delete+recreate, and some filesystems (observed on Linux CI's ext4
    /tmp) can immediately reuse the just-freed inode for the new file.
    Relying on `path.unlink(); path.write_text(...)` to reliably trigger
    rotated=True is therefore a non-portable test assumption, not a
    guaranteed OS contract -- this simulates the condition the collector
    actually branches on (a changed SourceIdentity) directly, via the
    same abstraction agent/tailer.py itself uses, instead of via
    incidental OS allocator timing. See
    test_rotation_real_os_delete_recreate_matches_discovery_identify for
    a companion test against real OS behavior.
    """
    real_identify = discovery.identify
    path_str = str(path)

    def fake_identify(p):
        if p == path_str:
            return _SENTINEL_ROTATED_IDENTITY
        return real_identify(p)

    monkeypatch.setattr(discovery, "identify", fake_identify)


# --- bootstrap ---------------------------------------------------------


def test_normal_bootstrap_seeds_at_eof_and_ignores_historical_content(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text(CHECKIN_LINE.format(barcode="OLD1") * 5, encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, bootstrap_mode=BootstrapMode.NORMAL)
    report = collector.poll_once()

    assert report.events_read == 0
    assert _pending_records(tmp_path) == []

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="NEW1"))

    collector.poll_once()
    collector.force_flush()

    records = _pending_records(tmp_path)
    assert len(records) == 1
    assert records[0]["barcode"] == "NEW1"


def test_replay_bootstrap_reads_historical_content_from_byte_zero(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text(CHECKIN_LINE.format(barcode="HIST1") + CHECKIN_LINE.format(barcode="HIST2"), encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, bootstrap_mode=BootstrapMode.REPLAY)
    collector.poll_once()
    collector.force_flush()

    records = _pending_records(tmp_path)
    assert [r["barcode"] for r in records] == ["HIST1", "HIST2"]


def test_offset_bootstrap_starts_at_explicit_offset(tmp_path):
    path = tmp_path / "Checkins.txt"
    first_line = CHECKIN_LINE.format(barcode="SKIP1")
    path.write_text(first_line + CHECKIN_LINE.format(barcode="KEEP1"), encoding="utf-8")
    offset_after_first_line = len(first_line.encode("utf-8"))

    collector = _make_collector(
        tmp_path, path=path, bootstrap_mode=BootstrapMode.OFFSET, bootstrap_offset=offset_after_first_line
    )
    collector.poll_once()
    collector.force_flush()

    records = _pending_records(tmp_path)
    assert [r["barcode"] for r in records] == ["KEEP1"]


def test_normal_bootstrap_persists_state_immediately_with_no_spool_activity(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text(CHECKIN_LINE.format(barcode="OLD1"), encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, bootstrap_mode=BootstrapMode.NORMAL)
    collector.poll_once()

    loaded = state.load_state(tmp_path / "state" / "agent_state.json")
    checkins_state = state.get_source(loaded, "checkins")
    assert checkins_state is not None
    assert checkins_state.cursor.offset == os.path.getsize(path)


def test_bootstrap_does_not_happen_twice_across_restarts(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text(CHECKIN_LINE.format(barcode="OLD1"), encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, bootstrap_mode=BootstrapMode.NORMAL)
    collector.poll_once()

    # "Restart": a fresh collector instance reads the same persisted
    # state, must NOT bootstrap again (which would re-seed at a NEW EOF
    # and silently skip anything written in between).
    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="NEW1"))

    collector2 = _make_collector(tmp_path, path=path, bootstrap_mode=BootstrapMode.NORMAL)
    collector2.poll_once()
    collector2.force_flush()

    records = _pending_records(tmp_path)
    assert [r["barcode"] for r in records] == ["NEW1"]


# --- batching: count and timer thresholds -----------------------------------


def test_flush_triggers_at_event_count_threshold(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=3, batch_max_seconds=1000.0)
    collector.poll_once()  # bootstrap at EOF (empty file)

    with open(path, "a", encoding="utf-8") as f:
        f.writelines(CHECKIN_LINE.format(barcode=f"B{i}") for i in range(3))

    report = collector.poll_once()

    assert report.events_flushed == 3
    assert report.spool_batches_written == 1
    assert len(_pending_records(tmp_path)) == 3


def test_flush_does_not_trigger_before_threshold(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=5, batch_max_seconds=1000.0)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="B0"))

    report = collector.poll_once()

    assert report.events_flushed == 0
    assert _pending_records(tmp_path) == []


def test_flush_triggers_at_timer_threshold(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    fake_now = [1000.0]
    collector = _make_collector(
        tmp_path, path=path, batch_max_events=100, batch_max_seconds=2.0, time_fn=lambda: fake_now[0]
    )
    collector.poll_once()  # bootstrap

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="B0"))

    report1 = collector.poll_once()  # buffers 1 event, starts timer at 1000.0
    assert report1.events_flushed == 0

    fake_now[0] = 1002.5  # 2.5s later -- past the 2.0s threshold
    report2 = collector.poll_once()  # no new lines, but timer check still runs

    assert report2.events_flushed == 1
    assert len(_pending_records(tmp_path)) == 1


def test_burst_larger_than_threshold_is_chunked_into_multiple_batches(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=10, batch_max_seconds=1000.0)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.writelines(CHECKIN_LINE.format(barcode=f"B{i}") for i in range(25))

    report = collector.poll_once()

    assert report.events_read == 25
    assert report.events_flushed == 25
    assert report.spool_batches_written == 3  # 10 + 10 + 5
    assert len(_pending_records(tmp_path)) == 25


def test_no_empty_spool_batch_ever_written(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=3, batch_max_seconds=0.01)
    collector.poll_once()
    collector.poll_once()  # no new data, timer may fire but buffer is empty

    assert spool.list_pending_batches(tmp_path / "spool", "checkins") == []


# --- multi-poll buffer duplication safety -----------------------------------
#
# Verifies the distinction documented in collector.py's module docstring:
# the LIVE READ CURSOR (self._read_cursor) advances on every successful
# read regardless of buffering/flush state, so a poll cycle with nothing
# new never re-reads (and never re-adds to the buffer) bytes already
# sitting there waiting for a flush. This must hold on ordinary healthy
# runs, not just after a crash -- backend idempotency is a safety net,
# never a license to manufacture avoidable duplicates.


def test_one_event_survives_repeated_polls_before_timer_flush_without_duplication(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()  # bootstrap at EOF=0

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="SOLO1"))

    collector.poll_once()  # reads + buffers the 1 new event
    assert len(collector._buffer) == 1

    for _ in range(5):
        report = collector.poll_once()  # repeated polls, nothing new on disk
        assert report.events_read == 0
        assert len(collector._buffer) == 1  # never grows from re-reading

    collector.force_flush()
    records = _pending_records(tmp_path)
    assert len(records) == 1
    assert records[0]["barcode"] == "SOLO1"
    assert len({r["source_event_id"] for r in records}) == 1


def test_several_events_survive_repeated_polls_before_timer_flush_without_duplication(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.writelines(CHECKIN_LINE.format(barcode=f"MULTI{i}") for i in range(4))

    collector.poll_once()  # buffers all 4 in one read
    assert len(collector._buffer) == 4

    for _ in range(8):
        report = collector.poll_once()
        assert report.events_read == 0
        assert len(collector._buffer) == 4

    collector.force_flush()
    records = _pending_records(tmp_path)
    assert sorted(r["barcode"] for r in records) == [f"MULTI{i}" for i in range(4)]
    assert len({r["source_event_id"] for r in records}) == 4  # every id unique, nothing duplicated


def test_new_events_arriving_while_older_buffer_pending_do_not_duplicate_old_ones(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="OLD1"))
    collector.poll_once()
    assert len(collector._buffer) == 1

    collector.poll_once()  # repeated poll, nothing new yet
    assert len(collector._buffer) == 1

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="NEW1"))
    collector.poll_once()
    assert len(collector._buffer) == 2  # OLD1 not duplicated, NEW1 added once

    collector.force_flush()
    records = _pending_records(tmp_path)
    assert sorted(r["barcode"] for r in records) == ["NEW1", "OLD1"]
    assert len({r["source_event_id"] for r in records}) == 2


def test_persisted_cursor_lags_live_read_cursor_while_buffer_pending(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="LAG1"))
    collector.poll_once()

    # Live read cursor has already advanced past the buffered record...
    assert collector.current_offset == os.path.getsize(path)

    # ...but the PERSISTED cursor must still reflect nothing new (still 0,
    # from the EOF-bootstrap seed) -- it only advances once the buffer is
    # actually flushed to the spool.
    persisted = state.get_source(state.load_state(tmp_path / "state" / "agent_state.json"), "checkins")
    assert persisted.cursor.offset == 0

    collector.force_flush()

    persisted_after = state.get_source(state.load_state(tmp_path / "state" / "agent_state.json"), "checkins")
    assert persisted_after.cursor.offset == os.path.getsize(path)


# --- crash-boundary safety --------------------------------------------------


def test_crash_after_spool_publish_before_state_save_is_safe_to_reread(tmp_path, monkeypatch):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=1, batch_max_seconds=1000.0)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="CRASH1"))

    # Simulate a crash: spool write succeeds, but persisting state fails.
    original_save_state = state.save_state
    def boom(*args, **kwargs):
        raise OSError("simulated crash after spool write")
    monkeypatch.setattr(state, "save_state", boom)

    try:
        collector.poll_once()
    except OSError:
        pass

    monkeypatch.setattr(state, "save_state", original_save_state)

    # Durable capture happened -- the event IS in the spool.
    records = _pending_records(tmp_path)
    assert len(records) == 1
    assert records[0]["barcode"] == "CRASH1"

    # But the persisted cursor was NOT advanced (crash happened before
    # save_state completed) -- a fresh collector "restarting" rereads the
    # same bytes and reproduces the IDENTICAL event (same source_event_id).
    fresh_collector = _make_collector(tmp_path, path=path, batch_max_events=1, batch_max_seconds=1000.0)
    fresh_collector.poll_once()

    records_after_restart = _pending_records(tmp_path)
    assert len(records_after_restart) == 2  # the original spool file + a duplicate reread
    ids = {r["source_event_id"] for r in records_after_restart}
    assert len(ids) == 1  # both are the SAME deterministic event id -- safe duplicate


def test_crash_before_spool_publish_leaves_cursor_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=1, batch_max_seconds=1000.0)
    collector.poll_once()

    state_path = tmp_path / "state" / "agent_state.json"
    before = state.load_state(state_path)
    cursor_before = state.get_source(before, "checkins").cursor

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="NEVER1"))

    def boom(*args, **kwargs):
        raise OSError("simulated crash before spool write")
    monkeypatch.setattr(spool, "write_batch", boom)

    try:
        collector.poll_once()
    except OSError:
        pass

    assert spool.list_pending_batches(tmp_path / "spool", "checkins") == []

    after = state.load_state(state_path)
    cursor_after = state.get_source(after, "checkins").cursor
    assert cursor_after == cursor_before


def test_crash_with_unflushed_low_volume_buffer_leaves_nothing_spooled(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()  # bootstrap at EOF=0, persisted immediately

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="UNFLUSHED1"))
    collector.poll_once()  # buffered, below both thresholds -- never flushed

    # "Crash": the collector object (and its in-memory buffer) is simply
    # discarded, exactly as a killed process would lose it. Nothing was
    # ever spooled, and nothing was ever persisted beyond the original
    # EOF seed.
    del collector

    assert spool.list_pending_batches(tmp_path / "spool", "checkins") == []
    persisted = state.get_source(state.load_state(tmp_path / "state" / "agent_state.json"), "checkins")
    assert persisted.cursor.offset == 0


def test_restart_after_crash_with_unflushed_buffer_rereads_exactly_once(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="UNFLUSHED2"))
    collector.poll_once()  # buffered, never flushed
    del collector  # "crash" -- buffer lost, nothing was ever durable

    # "Restart": a fresh collector reloads the same persisted state
    # (still at the pre-read EOF-seed position) and re-reads the source
    # from scratch.
    fresh = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    fresh.poll_once()
    fresh.force_flush()

    records = _pending_records(tmp_path)
    assert len(records) == 1  # re-read exactly once -- not duplicated, not lost
    assert records[0]["barcode"] == "UNFLUSHED2"


# --- rotation / truncation increment generation -----------------------------


def test_rotation_flushes_old_generation_buffer_then_bumps_generation(tmp_path, monkeypatch):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="PRE1"))
    collector.poll_once()  # buffered, not yet flushed (below threshold)

    # Rotate: new content at the same path, with identity forced to
    # change deterministically -- see _simulate_rotation for why this
    # doesn't rely on the OS actually allocating a new inode.
    path.write_text(CHECKIN_LINE.format(barcode="POST1"), encoding="utf-8")
    _simulate_rotation(monkeypatch, path)

    report = collector.poll_once()
    collector.force_flush()  # flush the new-generation buffer too, for inspection

    assert report.rotated is True
    # The pre-rotation buffered event was flushed as its OWN batch
    # (generation 0) before the new generation's data is processed.
    batches = spool.list_pending_batches(tmp_path / "spool", "checkins")
    metas = [spool.parse_batch_filename(b) for b in batches]
    generations = sorted(m.generation for m in metas if m is not None)
    assert generations == [0, 1]

    loaded = state.get_source(state.load_state(tmp_path / "state" / "agent_state.json"), "checkins")
    assert loaded.generation == 1


def test_truncation_bumps_generation_same_as_rotation(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text(CHECKIN_LINE.format(barcode="A") * 5, encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, bootstrap_mode=BootstrapMode.REPLAY, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()
    collector.force_flush()

    # Truncate in place (same identity, smaller size).
    path.write_text(CHECKIN_LINE.format(barcode="B"), encoding="utf-8")

    report = collector.poll_once()
    collector.force_flush()

    assert report.truncated is True
    loaded = state.get_source(state.load_state(tmp_path / "state" / "agent_state.json"), "checkins")
    assert loaded.generation == 1


def test_rotation_with_unflushed_buffer_present_flushes_old_generation_exactly_once(tmp_path, monkeypatch):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="PRE_ROTATE"))
    collector.poll_once()  # buffered under generation 0, NOT flushed yet
    assert len(collector._buffer) == 1

    # A couple of ordinary polls first, proving the buffered record isn't
    # duplicated even before the rotation happens.
    collector.poll_once()
    collector.poll_once()
    assert len(collector._buffer) == 1

    # See _simulate_rotation: forces a changed SourceIdentity
    # deterministically rather than relying on the OS reallocating a new
    # inode for a same-path delete+recreate.
    path.write_text(CHECKIN_LINE.format(barcode="POST_ROTATE"), encoding="utf-8")
    _simulate_rotation(monkeypatch, path)

    collector.poll_once()
    collector.force_flush()

    records = _pending_records(tmp_path)
    barcodes = sorted(r["barcode"] for r in records)
    assert barcodes == ["POST_ROTATE", "PRE_ROTATE"]  # each appears exactly once
    assert len({r["source_event_id"] for r in records}) == 2

    batches = spool.list_pending_batches(tmp_path / "spool", "checkins")
    generations = sorted(spool.parse_batch_filename(b).generation for b in batches)
    assert generations == [0, 1]


def test_rotation_real_os_delete_recreate_matches_discovery_identify(tmp_path):
    """Integration-style companion to the synthetic-identity rotation
    tests above: exercises a REAL path.unlink() + path.write_text() at
    the OS level (no monkeypatching) and asserts the collector's
    `rotated` flag always agrees with whatever discovery.identify()
    itself actually observed for this delete+recreate on this OS/
    filesystem -- rather than assuming any particular inode-allocation
    behavior.

    This intentionally does NOT assert `rotated is True` unconditionally:
    a Linux CI investigation found that some filesystems (observed on
    ubuntu-latest's ext4 /tmp) can immediately reuse the just-freed inode
    for the recreated file, so a real delete+recreate does not always
    change SourceIdentity. That is a known, narrow limitation of a
    pure-identity rotation strategy (see agent/discovery.py's module
    docstring) -- not something this test should paper over by forcing
    an OS-dependent outcome. What this test guards against is a wiring
    regression: the collector must never disagree with discovery.identify
    about whether identity actually changed.
    """
    path = tmp_path / "Checkins.txt"
    path.write_text(CHECKIN_LINE.format(barcode="PRE1"), encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()
    collector.force_flush()

    identity_before = discovery.identify(str(path))
    path.unlink()
    path.write_text(CHECKIN_LINE.format(barcode="POST1"), encoding="utf-8")
    identity_after = discovery.identify(str(path))
    identity_changed = identity_before != identity_after

    report = collector.poll_once()

    assert report.rotated is identity_changed

    if identity_changed:
        collector.force_flush()
        records = _pending_records(tmp_path)
        assert any(r["barcode"] == "POST1" for r in records)


def test_truncation_with_unflushed_buffer_present_preserves_generation_and_cursor(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path, batch_max_events=100, batch_max_seconds=1000.0)
    collector.poll_once()

    # Several lines, so the live read cursor sits well past any short
    # replacement content below -- an unambiguous truncation, not just a
    # same-size-or-larger rewrite that a naive size check could miss.
    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="PRE_TRUNC") * 5)
    collector.poll_once()  # buffered under generation 0, not flushed
    collector.poll_once()  # repeated poll -- must not duplicate
    assert len(collector._buffer) == 5

    path.write_text(CHECKIN_LINE.format(barcode="POST_TRUNC"), encoding="utf-8")

    report = collector.poll_once()
    collector.force_flush()

    assert report.truncated is True
    records = _pending_records(tmp_path)
    barcodes = sorted(r["barcode"] for r in records)
    assert barcodes == ["POST_TRUNC"] + ["PRE_TRUNC"] * 5
    assert len({r["source_event_id"] for r in records}) == 6  # every id unique, nothing duplicated

    loaded = state.get_source(state.load_state(tmp_path / "state" / "agent_state.json"), "checkins")
    assert loaded.generation == 1
    assert loaded.cursor.offset == os.path.getsize(path)


# --- malformed lines still advance the cursor -------------------------------


def test_all_malformed_lines_advance_cursor_without_spooling(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    collector = _make_collector(tmp_path, path=path)
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.write("not|enough|fields\n")

    report = collector.poll_once()

    assert report.events_read == 0
    assert report.state_persisted is True
    assert spool.list_pending_batches(tmp_path / "spool", "checkins") == []

    loaded = state.get_source(state.load_state(tmp_path / "state" / "agent_state.json"), "checkins")
    assert loaded.cursor.offset == os.path.getsize(path)


# --- clock rollback has no correctness effect -------------------------------


def test_clock_rollback_does_not_break_or_prematurely_trigger_timer_flush(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text("", encoding="utf-8")

    fake_now = [5000.0]
    collector = _make_collector(
        tmp_path, path=path, batch_max_events=100, batch_max_seconds=2.0, time_fn=lambda: fake_now[0]
    )
    collector.poll_once()

    with open(path, "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="B0"))
    collector.poll_once()  # buffers 1 event, timer starts at 5000.0

    # Wall clock jumps BACKWARD (e.g. system suspend/resume oddity -- in
    # normal production this can't happen from an NTP correction, since
    # the default time_fn is time.monotonic(), immune to wall-clock
    # adjustments; this test injects an adversarial time_fn to prove the
    # collector itself doesn't assume forward-only time). Must not crash,
    # and must not spuriously trigger a flush (elapsed looks deeply
    # negative, well under the threshold).
    fake_now[0] = 100.0
    report = collector.poll_once()
    assert report.events_flushed == 0
    assert spool.list_pending_batches(tmp_path / "spool", "checkins") == []

    # No data is lost while "stuck" behind a rolled-back clock -- the
    # event is still safely sitting in the buffer.
    assert len(collector._buffer) == 1

    # Once the clock genuinely progresses past batch_max_seconds relative
    # to when buffering actually started (5000.0), the flush fires --
    # rollback delays, but never loses, a timer-based flush.
    fake_now[0] = 5002.5
    report2 = collector.poll_once()
    assert report2.events_flushed == 1


def test_clock_rollback_does_not_affect_deterministic_event_ids(tmp_path):
    path = tmp_path / "Checkins.txt"
    path.write_text(CHECKIN_LINE.format(barcode="CLOCK1"), encoding="utf-8")

    # Two entirely independent collectors, both replaying the SAME
    # physical bytes from scratch, under wildly different (and even
    # backward-moving) clock readings -- must produce the identical
    # deterministic source_event_id regardless.
    collector_a = _make_collector(
        tmp_path / "a", path=path, bootstrap_mode=BootstrapMode.REPLAY, time_fn=lambda: 999999.0
    )
    collector_b = _make_collector(
        tmp_path / "b", path=path, bootstrap_mode=BootstrapMode.REPLAY, time_fn=lambda: 0.0
    )
    collector_a.poll_once()
    collector_a.force_flush()
    collector_b.poll_once()
    collector_b.force_flush()

    records_a = []
    for b in spool.list_pending_batches(tmp_path / "a" / "spool", "checkins"):
        records_a.extend(spool.read_batch(b))
    records_b = []
    for b in spool.list_pending_batches(tmp_path / "b" / "spool", "checkins"):
        records_b.extend(spool.read_batch(b))

    assert records_a[0]["source_event_id"] == records_b[0]["source_event_id"]


# --- multi-source independence ----------------------------------------------


def test_rejects_and_acs_sources_use_their_own_parsers(tmp_path):
    rejects_path = tmp_path / "Rejects.txt"
    rejects_path.write_text("", encoding="utf-8")
    acs_path = tmp_path / "ACS.txt"
    acs_path.write_text("", encoding="utf-8")

    rejects_collector = _make_collector(tmp_path, name="rejects", path=rejects_path)
    acs_collector = _make_collector(tmp_path, name="acs", path=acs_path)
    rejects_collector.poll_once()
    acs_collector.poll_once()

    with open(rejects_path, "a", encoding="utf-8") as f:
        f.write("111|Item Not Found|1/31/2026|8:00:00 AM\n")
    with open(acs_path, "a", encoding="utf-8") as f:
        f.write("\x011/31/2026\x028:00:00 AM\x02CK|AB12345\x01\n")

    rejects_collector.poll_once()
    rejects_collector.force_flush()
    acs_collector.poll_once()
    acs_collector.force_flush()

    reject_records = _pending_records(tmp_path, source="rejects")
    acs_records = _pending_records(tmp_path, source="acs")
    assert len(reject_records) == 1
    assert reject_records[0]["barcode"] == "111"
    assert len(acs_records) == 1
    assert acs_records[0]["barcode"] == "12345"
