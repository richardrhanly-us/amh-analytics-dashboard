"""Tests for collector/reader.py -- SortView Collector v1 (Phase 4a).

Covers: true byte offsets, partial-line safety, identity+size rotation/
truncation detection, and missing/stale source handling. Does not (and
cannot) prove real Windows/Tech Logic file behavior -- see the module's
own VALIDATION STATUS docstring section; that remains a required, separate
onsite step before production use.
"""

from __future__ import annotations

import os

from collector.reader import FileIdentity, SourceCursor, identify, read_new_lines


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
