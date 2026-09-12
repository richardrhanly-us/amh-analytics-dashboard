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
