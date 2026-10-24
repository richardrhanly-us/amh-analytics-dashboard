"""Tests for collector/reader.py -- SortView Collector v1 (Phase 4a).

Covers: true byte offsets, partial-line safety, identity+size rotation/
truncation detection, and missing/stale source handling. Does not (and
cannot) prove real Windows/Tech Logic file behavior -- see the module's
own VALIDATION STATUS docstring section; that remains a required, separate
onsite step before production use.
"""

from __future__ import annotations

import os

import pytest

from collector import reader, v2_reader
from collector.reader import (
    FileIdentity,
    SourceCursor,
    identify,
    read_new_lines,
    same_file,
)
from collector.v2_events import Cursor


def _write_bytes(path, data: bytes):
    path.write_bytes(data)


# --- true byte offsets --------------------------------------------------


def test_offset_is_true_byte_count_for_plain_content(tmp_path):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\nline two\n")

    result = read_new_lines(str(path), cursor=None)

    assert result.lines == ["line one\n", "line two\n"]
    assert result.cursor.offset == os.path.getsize(path)


def test_offset_is_true_byte_count_for_content_with_invalid_utf8(tmp_path):
    # Same shape as raw SIP/control-character content in ACS Log.txt --
    # exactly the class of content that corrupted a text-mode tell()
    # cookie in this project's own production history.
    path = tmp_path / "ACS Log.txt"
    _write_bytes(path, b"line one\n" + b"line two with bad byte \x80\x81 inside\n" + b"line three\n")

    result = read_new_lines(str(path), cursor=None)

    assert len(result.lines) == 3
    assert result.cursor.offset == os.path.getsize(path)
    assert result.cursor.offset < 10**6  # sane, not an opaque multi-digit cookie


def test_second_read_only_returns_new_lines_and_advances_offset(tmp_path):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\n")
    first = read_new_lines(str(path), cursor=None)

    with open(path, "ab") as f:
        f.write(b"line two\n")

    second = read_new_lines(str(path), cursor=first.cursor)

    assert second.lines == ["line two\n"]
    assert second.cursor.offset > first.cursor.offset


def test_repeated_read_with_no_new_data_returns_empty_and_unchanged_cursor(tmp_path):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\n")
    first = read_new_lines(str(path), cursor=None)

    second = read_new_lines(str(path), cursor=first.cursor)

    assert second.lines == []
    assert second.cursor == first.cursor


# --- partial-line safety -------------------------------------------------


def test_partial_trailing_line_not_consumed(tmp_path):
    path = tmp_path / "Checkins.txt"
    complete = b"line one\nline two\n"
    partial = b"line three still being wri"
    _write_bytes(path, complete + partial)

    result = read_new_lines(str(path), cursor=None)

    assert result.lines == ["line one\n", "line two\n"]
    assert result.cursor.offset == len(complete)


def test_partial_line_completed_on_next_read_is_captured_whole(tmp_path):
    path = tmp_path / "Checkins.txt"
    first_half = b"partial-line-not-yet-done"
    second_half = b"-now-finished"
    _write_bytes(path, b"line one\n" + first_half)
    first = read_new_lines(str(path), cursor=None)
    assert first.lines == ["line one\n"]

    with open(path, "ab") as f:
        f.write(second_half + b"\n")

    second = read_new_lines(str(path), cursor=first.cursor)

    assert second.lines == [(first_half + second_half).decode() + "\n"]


def test_crlf_normalized_to_lf(tmp_path):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\r\nline two\r\n")

    result = read_new_lines(str(path), cursor=None)

    assert result.lines == ["line one\n", "line two\n"]


# --- identity-based rotation detection ------------------------------------


def test_identify_stable_across_appends(tmp_path):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\n")
    before = identify(str(path))

    with open(path, "ab") as f:
        f.write(b"line two\n")

    after = identify(str(path))
    assert before == after


def test_identify_returns_none_for_missing_file(tmp_path):
    assert identify(str(tmp_path / "does-not-exist.txt")) is None


def test_rotation_detected_via_identity_mismatch_regardless_of_size(tmp_path):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"new file, smaller than the stale offset\n")
    real_identity = identify(str(path))

    # A different (fake) identity than what's actually on disk, with a
    # stale offset SMALLER than the current file -- a size-only check
    # would never catch this; identity must.
    fake_old_identity = FileIdentity(token=(real_identity.token[0], real_identity.token[1] + 999))
    stale_cursor = SourceCursor(identity=fake_old_identity, offset=5)

    result = read_new_lines(str(path), cursor=stale_cursor)

    assert result.rotated is True
    assert result.truncated is False
    assert result.lines == ["new file, smaller than the stale offset\n"]


def test_no_rotation_when_identity_unchanged(tmp_path):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\n")
    first = read_new_lines(str(path), cursor=None)

    with open(path, "ab") as f:
        f.write(b"line two\n")

    second = read_new_lines(str(path), cursor=first.cursor)
    assert second.rotated is False


def test_truncation_detected_when_identity_matches_but_shrinks(tmp_path):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\nline two\nline three\n")
    first = read_new_lines(str(path), cursor=None)

    # os.truncate operates on the SAME inode in place (unlike rewriting
    # via a fresh open(), which is not guaranteed to preserve identity on
    # every platform) -- deterministic: identity must be unchanged, size
    # must have shrunk.
    os.truncate(path, 9)  # "line one\n" is 9 bytes
    assert identify(str(path)) == first.cursor.identity

    result = read_new_lines(str(path), cursor=first.cursor)

    assert result.truncated is True
    assert result.rotated is False
    assert result.lines == ["line one\n"]  # read from 0 again


def test_generation_reset_replays_whole_new_file_from_zero(tmp_path):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"old content\n" * 5)
    real_identity = identify(str(path))
    fake_old_identity = FileIdentity(token=(real_identity.token[0], real_identity.token[1] + 999))
    stale_cursor = SourceCursor(identity=fake_old_identity, offset=10)

    result = read_new_lines(str(path), cursor=stale_cursor)

    assert len(result.lines) == 5
    assert result.cursor.offset == os.path.getsize(path)


# --- missing/stale source handling -----------------------------------------


def test_missing_file_returns_existed_false_and_preserves_cursor(tmp_path):
    path = tmp_path / "does-not-exist.txt"
    prior_cursor = SourceCursor(identity=FileIdentity(token=(1, 2)), offset=42)

    result = read_new_lines(str(path), cursor=prior_cursor)

    assert result.existed is False
    assert result.lines == []
    assert result.cursor == prior_cursor


def test_missing_file_with_no_prior_cursor_returns_empty_cursor(tmp_path):
    path = tmp_path / "does-not-exist.txt"

    result = read_new_lines(str(path), cursor=None)

    assert result.existed is False
    assert result.cursor == SourceCursor(identity=None, offset=0)


def test_missing_file_never_raises(tmp_path):
    # Explicit non-fatal contract -- a scheduled run must not crash just
    # because Tech Logic hasn't created a file yet.
    path = tmp_path / "not-here-yet.txt"
    result = read_new_lines(str(path), cursor=None)
    assert result.existed is False


# --- Windows file identity across the Python 3.11 -> 3.12+ runtime upgrade -----
#
# Measured on one NTFS volume: Python 3.9/3.11 report os.stat's st_dev as the
# 32-bit volume serial, Python 3.12/3.14 as the full 64-bit serial, whose low 32
# bits are the old value; st_ino (the file index) is identical in all of them.

LEGACY_SERIAL = 0xF43F455C  # st_dev under a Python 3.11-based runtime
FULL_SERIAL = 0xECF43F79F43F455C  # st_dev of the same volume under Python 3.12+
FILE_INDEX = 0x400000001EF542  # st_ino under both


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(reader, "_is_windows", lambda: True)


@pytest.fixture
def off_windows(monkeypatch):
    monkeypatch.setattr(reader, "_is_windows", lambda: False)


def _upgraded_runtime(monkeypatch, index=FILE_INDEX):
    """identify() as the Python 3.12+ runtime reports every existing file."""
    monkeypatch.setattr(reader, "identify", lambda path: FileIdentity((FULL_SERIAL, index)) if os.path.exists(path) else None)


@pytest.mark.parametrize("windows", [True, False])
def test_same_file_accepts_exact_equality_on_every_platform(monkeypatch, windows):
    monkeypatch.setattr(reader, "_is_windows", lambda: windows)
    assert same_file((FULL_SERIAL, FILE_INDEX), (FULL_SERIAL, FILE_INDEX))
    assert same_file((LEGACY_SERIAL, FILE_INDEX), (LEGACY_SERIAL, FILE_INDEX))


def test_same_file_accepts_the_legacy_32_bit_serial_of_the_same_file_on_windows(on_windows):
    assert same_file((LEGACY_SERIAL, FILE_INDEX), (FULL_SERIAL, FILE_INDEX))


def test_same_file_never_accepts_the_legacy_form_off_windows(off_windows):
    assert not same_file((LEGACY_SERIAL, FILE_INDEX), (FULL_SERIAL, FILE_INDEX))


@pytest.mark.parametrize("saved, current", [
    ((LEGACY_SERIAL, FILE_INDEX + 1), (FULL_SERIAL, FILE_INDEX)),           # another file index: a replaced file
    ((LEGACY_SERIAL ^ 1, FILE_INDEX), (FULL_SERIAL, FILE_INDEX)),           # low 32 bits differ: another volume
    ((FULL_SERIAL, FILE_INDEX), (FULL_SERIAL ^ (1 << 40), FILE_INDEX)),     # two different 64-bit serials
    ((FULL_SERIAL, FILE_INDEX), (LEGACY_SERIAL, FILE_INDEX)),               # the reverse direction
    ((LEGACY_SERIAL, FILE_INDEX), (LEGACY_SERIAL ^ 1, FILE_INDEX)),         # two different 32-bit serials
    ((LEGACY_SERIAL, FILE_INDEX), (LEGACY_SERIAL, FILE_INDEX + 1)),         # same 32-bit serial, another file
    ((True, FILE_INDEX), ((5 << 32) | 1, FILE_INDEX)),                     # a boolean is not a serial
    ((-1, FILE_INDEX), (FULL_SERIAL, FILE_INDEX)),                         # negative
    ((str(LEGACY_SERIAL), FILE_INDEX), (FULL_SERIAL, FILE_INDEX)),          # a string
    ((LEGACY_SERIAL, FILE_INDEX, 0), (FULL_SERIAL, FILE_INDEX)),           # three elements
    ([LEGACY_SERIAL, FILE_INDEX], (FULL_SERIAL, FILE_INDEX)),               # not a token tuple
    ((LEGACY_SERIAL, float(FILE_INDEX)), (FULL_SERIAL, FILE_INDEX)),       # a float file index
    (None, (FULL_SERIAL, FILE_INDEX)),
    ((LEGACY_SERIAL, FILE_INDEX), None),
])
def test_same_file_refuses_every_other_combination_on_windows(on_windows, saved, current):
    assert not same_file(saved, current)


# Contract v1: reader.read_new_lines


def test_a_legacy_cursor_keeps_its_offset_after_the_runtime_upgrade(tmp_path, monkeypatch, on_windows):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\nline two\n")
    cursor = SourceCursor(identity=FileIdentity((LEGACY_SERIAL, FILE_INDEX)), offset=9)  # saved by the 3.11 runtime after "line one"
    _upgraded_runtime(monkeypatch)

    result = read_new_lines(str(path), cursor=cursor)

    assert (result.rotated, result.truncated) == (False, False)
    assert result.lines == ["line two\n"]  # only what is new, exactly once
    assert result.cursor == SourceCursor(identity=FileIdentity((FULL_SERIAL, FILE_INDEX)), offset=os.path.getsize(path))


def test_a_legacy_cursor_beyond_the_end_of_the_file_is_still_truncation(tmp_path, monkeypatch, on_windows):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\n")
    _upgraded_runtime(monkeypatch)

    result = read_new_lines(str(path), cursor=SourceCursor(identity=FileIdentity((LEGACY_SERIAL, FILE_INDEX)), offset=30))

    assert (result.rotated, result.truncated) == (False, True)
    assert result.lines == ["line one\n"]  # read from 0 again


def test_a_legacy_cursor_for_another_file_index_is_still_rotation(tmp_path, monkeypatch, on_windows):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"new one\nnew two\n")
    _upgraded_runtime(monkeypatch)

    result = read_new_lines(str(path), cursor=SourceCursor(identity=FileIdentity((LEGACY_SERIAL, FILE_INDEX + 1)), offset=8))

    assert (result.rotated, result.truncated) == (True, False)
    assert result.lines == ["new one\n", "new two\n"]


def test_a_legacy_cursor_off_windows_is_rotation_as_before(tmp_path, monkeypatch, off_windows):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\nline two\n")
    _upgraded_runtime(monkeypatch)

    result = read_new_lines(str(path), cursor=SourceCursor(identity=FileIdentity((LEGACY_SERIAL, FILE_INDEX)), offset=9))

    assert result.rotated is True and len(result.lines) == 2


# Contract v2: v2_reader.read_chunk


def test_v2_a_legacy_cursor_keeps_its_offset_after_the_runtime_upgrade(tmp_path, monkeypatch, on_windows):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\nline two\n")
    _upgraded_runtime(monkeypatch)

    chunk = v2_reader.read_chunk(str(path), Cursor((LEGACY_SERIAL, FILE_INDEX), 9), max_lines=100)

    assert (chunk.rotated, chunk.truncated) == (False, False)
    assert chunk.lines == ["line two\n"]
    assert chunk.cursor == Cursor((FULL_SERIAL, FILE_INDEX), os.path.getsize(path))


def test_v2_a_legacy_cursor_with_nothing_new_moves_to_the_current_identity_at_the_same_offset(tmp_path, monkeypatch, on_windows):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\n")
    _upgraded_runtime(monkeypatch)

    chunk = v2_reader.read_chunk(str(path), Cursor((LEGACY_SERIAL, FILE_INDEX), 9), max_lines=100)

    assert chunk.lines == [] and not chunk.rotated and chunk.cursor == Cursor((FULL_SERIAL, FILE_INDEX), 9)


def test_v2_a_legacy_cursor_beyond_the_end_of_the_file_is_still_truncation(tmp_path, monkeypatch, on_windows):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\n")
    _upgraded_runtime(monkeypatch)

    chunk = v2_reader.read_chunk(str(path), Cursor((LEGACY_SERIAL, FILE_INDEX), 30), max_lines=100)

    assert (chunk.rotated, chunk.truncated) == (False, True) and chunk.lines == ["line one\n"]


def test_v2_a_legacy_cursor_for_another_file_index_is_still_rotation(tmp_path, monkeypatch, on_windows):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"new one\nnew two\n")
    _upgraded_runtime(monkeypatch)

    chunk = v2_reader.read_chunk(str(path), Cursor((LEGACY_SERIAL, FILE_INDEX + 1), 8), max_lines=100)

    assert (chunk.rotated, chunk.truncated) == (True, False) and len(chunk.lines) == 2


def test_v2_a_legacy_cursor_off_windows_is_rotation_as_before(tmp_path, monkeypatch, off_windows):
    path = tmp_path / "Checkins.txt"
    _write_bytes(path, b"line one\nline two\n")
    _upgraded_runtime(monkeypatch)

    chunk = v2_reader.read_chunk(str(path), Cursor((LEGACY_SERIAL, FILE_INDEX), 9), max_lines=100)

    assert chunk.rotated is True and len(chunk.lines) == 2
