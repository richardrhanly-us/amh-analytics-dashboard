"""Tests for agent/tailer.py -- the canonical incremental reader
(Continuous Ingestion Phase B). Covers the two proven fixes over the
deployed baseline (identity-based rotation detection, partial-line
safety) plus the normal read/missing-file paths.
"""

import os

from agent.discovery import SourceIdentity, identify
from agent.tailer import FileCursor, read_new_lines


def _write(path, text):
    path.write_text(text, encoding="utf-8")


# --- normal reads ------------------------------------------------------


def test_first_read_with_no_cursor_starts_at_zero(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "line one\nline two\n")

    result = read_new_lines(str(path), cursor=None)

    assert result.existed is True
    assert result.lines == ["line one\n", "line two\n"]
    assert result.rotated is False
    assert result.truncated is False
    assert result.cursor.offset == os.path.getsize(path)


def test_second_read_only_returns_new_lines(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "line one\n")
    first = read_new_lines(str(path), cursor=None)

    with open(path, "a", encoding="utf-8") as f:
        f.write("line two\n")

    second = read_new_lines(str(path), cursor=first.cursor)

    assert second.lines == ["line two\n"]
    assert second.rotated is False
    assert second.truncated is False


def test_read_with_no_new_data_returns_empty_lines_unchanged_cursor(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "line one\n")
    first = read_new_lines(str(path), cursor=None)

    second = read_new_lines(str(path), cursor=first.cursor)

    assert second.lines == []
    assert second.cursor == first.cursor


# --- partial-line safety (proven fix, preserved) --------------------------


def test_partial_trailing_line_not_consumed(tmp_path):
    path = tmp_path / "checkins.txt"
    complete = "line one\nline two\n"
    partial = "line three still being wri"
    _write(path, complete + partial)

    result = read_new_lines(str(path), cursor=None)

    assert result.lines == ["line one\n", "line two\n"]
    # Offset must point just past the last COMPLETE line, using the
    # real on-disk byte count -- not len(complete), since Windows
    # text-mode \n -> \r\n translation can change the byte count.
    complete_only_path = tmp_path / "complete_only.txt"
    _write(complete_only_path, complete)
    assert result.cursor.offset == os.path.getsize(complete_only_path)


def test_partial_line_completed_on_next_read_is_captured_whole(tmp_path):
    path = tmp_path / "checkins.txt"
    first_half = "partial-line-not-yet-done"
    second_half = "-now-finished"
    _write(path, "line one\n" + first_half)
    first = read_new_lines(str(path), cursor=None)
    assert first.lines == ["line one\n"]

    with open(path, "a", encoding="utf-8") as f:
        f.write(second_half + "\n")

    second = read_new_lines(str(path), cursor=first.cursor)

    # The full logical line arrives intact on the read where it's
    # finally terminated -- never split across the two reads.
    assert second.lines == [first_half + second_half + "\n"]


# --- rotation: identity-based, catches what size-only checks miss --------


def test_rotation_detected_via_identity_mismatch_regardless_of_size(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "old file with lots of content\n" * 50)
    real_identity = identify(str(path))

    # Simulate "the deployed baseline's blind spot": a rotated-in file
    # that is the SAME size or larger than the stored offset. A
    # size-only check (old_offset > file_size) would never trigger here.
    fake_old_identity = SourceIdentity(token=(real_identity.token[0], real_identity.token[1] + 999))
    stale_cursor = FileCursor(identity=fake_old_identity, offset=10)  # smaller than current size

    result = read_new_lines(str(path), cursor=stale_cursor)

    assert result.rotated is True
    assert result.truncated is False
    # Rotation means "start over" -- the whole new file is read from 0,
    # not from the stale offset into unrelated content.
    assert len(result.lines) == 50


def test_no_rotation_when_identity_unchanged(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "line one\n")
    first = read_new_lines(str(path), cursor=None)

    with open(path, "a", encoding="utf-8") as f:
        f.write("line two\n")

    second = read_new_lines(str(path), cursor=first.cursor)

    assert second.rotated is False


# --- truncation -----------------------------------------------------------


def test_truncation_detected_when_identity_matches_but_shrinks(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "line one\nline two\nline three\n")
    first = read_new_lines(str(path), cursor=None)
    real_identity = first.cursor.identity

    # Same identity, but a stored offset larger than the current size --
    # an in-place truncation, not a rotation.
    stale_cursor = FileCursor(identity=real_identity, offset=first.cursor.offset + 1000)

    result = read_new_lines(str(path), cursor=stale_cursor)

    assert result.truncated is True
    assert result.rotated is False
    assert len(result.lines) == 3  # read from 0 again


# --- missing / unreadable source -------------------------------------------


def test_missing_file_returns_existed_false_and_preserves_cursor(tmp_path):
    path = tmp_path / "does-not-exist.txt"
    prior_cursor = FileCursor(identity=SourceIdentity(token=(1, 2)), offset=42)

    result = read_new_lines(str(path), cursor=prior_cursor)

    assert result.existed is False
    assert result.lines == []
    assert result.cursor == prior_cursor


def test_missing_file_with_no_prior_cursor_returns_empty_cursor(tmp_path):
    path = tmp_path / "does-not-exist.txt"

    result = read_new_lines(str(path), cursor=None)

    assert result.existed is False
    assert result.cursor == FileCursor(identity=None, offset=0)


# --- per-line start offsets (Continuous Ingestion Phase E) -----------------


def test_line_offsets_same_length_as_lines_and_point_to_each_lines_start(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "line one\nline two\nline three\n")

    result = read_new_lines(str(path), cursor=None)

    assert len(result.line_offsets) == len(result.lines) == 3
    assert result.line_offsets == sorted(result.line_offsets)
    assert result.line_offsets[0] == 0

    # Each recorded offset must be exactly where its own line begins --
    # seeking there and reading one line must reproduce it verbatim.
    with open(path, encoding="utf-8") as f:
        for offset, expected_line in zip(result.line_offsets, result.lines):
            f.seek(offset)
            assert f.readline() == expected_line


def test_line_offsets_on_second_read_reflect_the_new_lines_own_positions(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "line one\n")
    first = read_new_lines(str(path), cursor=None)

    with open(path, "a", encoding="utf-8") as f:
        f.write("line two\n")

    second = read_new_lines(str(path), cursor=first.cursor)

    assert second.line_offsets == [first.cursor.offset]


def test_line_offsets_empty_when_no_new_lines(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "line one\n")
    first = read_new_lines(str(path), cursor=None)

    second = read_new_lines(str(path), cursor=first.cursor)

    assert second.line_offsets == []


def test_line_offsets_empty_for_missing_file(tmp_path):
    path = tmp_path / "does-not-exist.txt"

    result = read_new_lines(str(path), cursor=None)

    assert result.line_offsets == []


def test_line_offset_excludes_uncommitted_partial_trailing_line(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "line one\nline three still being wri")

    result = read_new_lines(str(path), cursor=None)

    # Only the one complete line has a recorded offset -- the partial
    # trailing line is neither in `lines` nor `line_offsets`.
    assert result.lines == ["line one\n"]
    assert result.line_offsets == [0]


def test_line_offsets_restart_from_zero_after_rotation(tmp_path):
    path = tmp_path / "checkins.txt"
    _write(path, "old file content\n" * 5)
    real_identity = identify(str(path))
    fake_old_identity = SourceIdentity(token=(real_identity.token[0], real_identity.token[1] + 999))
    stale_cursor = FileCursor(identity=fake_old_identity, offset=10)

    result = read_new_lines(str(path), cursor=stale_cursor)

    assert result.rotated is True
    assert result.line_offsets[0] == 0
    assert len(result.line_offsets) == len(result.lines) == 5


# --- offset representation: true byte counts, not opaque text cookies ----
#
# Regression coverage for the real-AMH-machine incident: a second live
# shadow-validation run on the actual Tech Logic machine captured a
# persisted ACS `cursor.offset` of 52 digits for a ~7MB file, produced by
# the PREVIOUS text-mode implementation's f.tell() cookie once ACS's raw
# SIP control-character content (genuinely invalid UTF-8 in spots, which
# is why errors="replace" was needed at all) put CPython's incremental
# decoder into non-trivial internal state. That cookie, compared against
# os.path.getsize() via `<`, produced a false but perfectly REPEATABLE
# "truncated" verdict -- not a one-off flaky read, which is why the
# earlier two-cycle discontinuity-confirmation fix could not catch it.
# These tests prove the binary-mode tailer cannot produce that class of
# value in the first place, not merely that one specific 52-digit number
# is now rejected somewhere downstream.


def _write_bytes(path, data: bytes):
    path.write_bytes(data)


def test_offset_is_a_true_byte_count_for_content_with_invalid_utf8(tmp_path):
    path = tmp_path / "ACS Log.txt"
    # \x80\x81 are not valid standalone UTF-8 bytes -- exactly the shape
    # of raw SIP/control-character content the onsite recon documented for
    # the real ACS Log.txt, and exactly what forces errors="replace" to
    # fire during decode.
    _write_bytes(path, b"line one\n" + b"line two with bad byte \x80\x81 inside\n" + b"line three\n")

    result = read_new_lines(str(path), cursor=None)

    assert len(result.lines) == 3
    assert result.cursor.offset == os.path.getsize(path)
    # Every recorded per-line offset is a real, seekable byte position
    # into the raw file -- reading raw bytes from that exact position
    # reproduces the same line's raw bytes, which is only meaningful if
    # the offset is a true byte count (an opaque text-mode cookie would
    # not support this at all).
    raw = path.read_bytes()
    for offset, expected_line in zip(result.line_offsets, result.lines):
        # expected_line has already had a trailing "\r\n" normalized to
        # "\n" (not present here) and been decoded with errors="replace";
        # re-slicing the raw bytes and decoding the same way must match.
        line_end = raw.index(b"\n", offset) + 1
        assert raw[offset:line_end].decode("utf-8", errors="replace") == expected_line


def test_offset_stays_a_true_byte_count_across_many_incremental_cycles_through_invalid_utf8(tmp_path):
    """Directly mirrors the real failure's shape: a file re-opened fresh
    on every poll cycle (as agent/runtime/collector.py does), growing
    over many cycles, repeatedly writing invalid-UTF-8 content -- the
    exact pattern that produced a 52-digit cookie under the old text-mode
    implementation. Under binary mode, cursor.offset must equal
    os.path.getsize() after every single cycle, with no growth
    possible beyond real bytes written.
    """
    path = tmp_path / "ACS Log.txt"
    _write_bytes(path, b"")

    cursor = None
    for cycle in range(30):
        with open(path, "ab") as f:
            f.write(f"normal line {cycle} ".encode() + b"\x80\x81\x82" + b" with bad bytes\n")

        result = read_new_lines(str(path), cursor=cursor)
        cursor = result.cursor

        assert result.truncated is False  # never a false truncation while genuinely growing
        assert cursor.offset == os.path.getsize(path)
        # The exact class of corruption from the incident: an offset that
        # could never be a real byte count for this file.
        assert cursor.offset < 10**6


def test_growing_file_with_invalid_utf8_never_falsely_reports_truncated(tmp_path):
    """The precise failure reproduced end-to-end: a healthy, continuously
    GROWING file containing invalid-UTF-8 content must never be reported
    as truncated, cycle after cycle -- the second shadow run's log showed
    exactly this (current_size legitimately increasing every cycle:
    7099671 -> 7100183 -> ... -- while still being compared as "less
    than" an astronomically large stale offset)."""
    path = tmp_path / "ACS Log.txt"
    _write_bytes(path, b"seed\n")
    cursor = read_new_lines(str(path), cursor=None).cursor

    sizes_seen = []
    for cycle in range(20):
        with open(path, "ab") as f:
            f.write(f"acs record {cycle} ".encode() + bytes([0x80 + (cycle % 10)]) + b" tail\n")

        result = read_new_lines(str(path), cursor=cursor)
        assert result.truncated is False
        assert result.rotated is False
        cursor = result.cursor
        sizes_seen.append(os.path.getsize(path))

    assert sizes_seen == sorted(sizes_seen)  # file only ever grew
    assert cursor.offset == os.path.getsize(path)


def test_crlf_is_normalized_to_lf_same_as_previous_text_mode_behavior(tmp_path):
    path = tmp_path / "checkins.txt"
    _write_bytes(path, b"line one\r\nline two\r\n")

    result = read_new_lines(str(path), cursor=None)

    assert result.lines == ["line one\n", "line two\n"]


def test_bare_cr_not_followed_by_lf_is_not_treated_as_a_line_terminator(tmp_path):
    """Deliberate, documented narrowing vs. full text-mode universal
    newlines (see agent/tailer.py's OFFSET REPRESENTATION docstring
    section): only b"\\n" ends a line. A stray b"\\r" with no following
    b"\\n" (plausible as raw SIP/control-character content, not an
    intended line break) must stay part of the line's content, and the
    line must not be considered complete until an actual b"\\n" arrives.
    """
    path = tmp_path / "ACS Log.txt"
    _write_bytes(path, b"before\rafter\n")

    result = read_new_lines(str(path), cursor=None)

    assert result.lines == ["before\rafter\n"]


def test_replay_of_content_never_happens_across_repeated_reads_with_binary_offsets(tmp_path):
    """A direct 'binary byte-offset path must not replay' check: reading
    repeatedly with no new data must never return already-seen lines
    again, and the cursor must never move backward."""
    path = tmp_path / "checkins.txt"
    _write_bytes(path, b"line one\n")

    first = read_new_lines(str(path), cursor=None)
    assert first.lines == ["line one\n"]

    for _ in range(5):
        again = read_new_lines(str(path), cursor=first.cursor)
        assert again.lines == []
        assert again.cursor.offset == first.cursor.offset

    with open(path, "ab") as f:
        f.write(b"line two\n")

    second = read_new_lines(str(path), cursor=first.cursor)
    assert second.lines == ["line two\n"]  # never re-includes "line one"
    assert second.cursor.offset > first.cursor.offset


def test_restart_style_reread_from_persisted_true_byte_offset_resumes_without_replay(tmp_path):
    """End-to-end restart simulation using only the tailer + a plain
    dict standing in for persisted state (agent/state.py's own restart
    tests cover the real persistence layer) -- proves a fresh call with
    the previous cursor's true byte offset picks up exactly where the
    last one left off, never re-reading already-captured content."""
    path = tmp_path / "checkins.txt"
    _write_bytes(path, b"line one\nline two\n")

    before_restart = read_new_lines(str(path), cursor=None)
    assert before_restart.lines == ["line one\n", "line two\n"]
    persisted_cursor = before_restart.cursor  # what agent/state.py would have saved

    with open(path, "ab") as f:
        f.write(b"line three\n")

    # "Restart": a brand new call, no in-memory state carried over except
    # the persisted cursor.
    after_restart = read_new_lines(str(path), cursor=persisted_cursor)

    assert after_restart.lines == ["line three\n"]
    assert after_restart.rotated is False
    assert after_restart.truncated is False
