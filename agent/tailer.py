"""Canonical incremental log tailer (Continuous Ingestion Phase B).

Merges the two real reliability fixes proven in the SQLite-era research
(agent/watcher.py, Phases 1-2) into one storage-independent primitive:

  1. Identity-based rotation detection. The currently deployed production
     pipeline (agent/SortViewAgent - What is currently sitting on the AMH
     computer/parse_checkins.py etc.) only checks `start_offset > file_size`
     to detect a reset -- if Tech Logic deletes and recreates a log file
     and the new file happens to be the same size or larger than the old
     stored offset, that check silently misses the rotation entirely and
     reads from the wrong position in an unrelated file's content. This
     tailer instead checks file identity (see discovery.py) on every read,
     which catches a rotation regardless of the new file's size.

  2. Partial-line safety. The deployed pipeline's `f.readlines()` reads
     all the way to EOF unconditionally, including whatever Tech Logic has
     written so far of a line it hasn't finished writing yet -- and then
     advances the stored offset past those bytes regardless. If the parser
     drops that partial line as a "short row" (the common case), the rest
     of it is never captured once TLC finishes writing, and parsing
     resumes mid-line on the next read. This tailer stops at the last
     complete ("\\n"-terminated) line and never advances past a line that
     might still be mid-write.

OFFSET REPRESENTATION -- BINARY MODE, TRUE BYTE OFFSETS (real-AMH-machine
correction, added after a second 2026-09-14 shadow validation run found a
live production bug this repo's own dev-machine testing never exposed):
this module reads the source file in BINARY mode and every offset it
produces or accepts (FileCursor.offset, TailResult.line_offsets) is a
real, physical byte count from the start of the file -- safe for direct
numeric comparison, subtraction, ordering, and JSON serialization.

This replaces an earlier TEXT-mode implementation
(`open(path, "r", encoding="utf-8", errors="replace")`, `f.tell()`/
`f.seek()`) that was already known and documented to return an "opaque"
value on Windows, not guaranteed to equal a true byte count -- but was
believed safe in practice because an isolated spot-check
(`f.seek(0, 2); f.tell() == os.path.getsize()`) matched on both Checkins
and ACS. That spot-check exercised only a single seek-to-EOF-on-a-freshly-
opened-handle call, which never puts CPython's incremental UTF-8 decoder
into a non-trivial internal state. Live incremental reading of the real
ACS Log.txt -- which the onsite recon already documented as containing
raw SIP control-character bytes, i.e. NOT always valid UTF-8, which is
exactly why `errors="replace"` was needed at all -- does exercise that
state, and `TextIOWrapper.tell()`'s cookie is a bit-packed integer
encoding (start position, decoder flags, pending-bytes-to-feed) whenever
the decoder isn't in a clean/trivial state at the queried position, not
merely a byte count in that case. That is precisely how the second shadow
run captured a persisted ACS `cursor.offset` of
`1461501637671185285124623296198104371161417405207` (52 digits, for a
~7MB file) even though checkins/rejects (plain pipe-delimited text,
presumably always valid UTF-8) never exhibited it. Comparing that opaque
value against `os.path.getsize()` via `<` in the old truncation check was
therefore comparing two values from different domains -- it happened to
look like "file size dropped below the recorded offset" (current_size
~7.1MB is obviously less than a 52-digit number), producing a false
"truncated" verdict that reproduced identically on every subsequent poll
(the value is static once persisted, the file could never plausibly grow
past it), which is exactly why the earlier two-cycle confirmation fix
(agent/runtime/collector.py) could not catch this specific failure mode:
confirmation defends against a FLAKY signal, and this was a
deterministically wrong CALCULATION, not a flaky one.

Binary mode removes this entire class of bug structurally rather than
patching around this one incident: `io.BufferedReader.tell()`/`seek()`
operate purely on physical byte position, with no decoding or newline
translation involved at all, so there is no decoder state for a cookie to
encode in the first place -- on Windows or any other platform, for any
source content, however malformed.

Partial-line safety is unchanged in spirit, just decided at the byte
level: a "complete" line is now any run of bytes up to and including a
single b"\\n" (0x0A) byte, decided BEFORE any decoding happens (the raw
bytes are inspected for a trailing b"\\n" first; only a line already
known to be complete is ever decoded) -- see _read_safe_lines. 0x0A can
never appear as a continuation or lead byte of a valid multi-byte UTF-8
sequence, so splitting raw bytes on it can never sever a multi-byte
character; decoding each already-delimited line independently is always
safe.

Windows CRLF: a trailing b"\\r\\n" is normalized to "\\n" after decoding
(matching what Python's text-mode universal-newline translation already
did for this common case, so parser-facing line content is unchanged for
ordinary Windows-authored files). A bare b"\\r" NOT immediately followed
by b"\\n" is deliberately NOT treated as its own line terminator here,
unlike full text-mode universal-newline translation -- this is an
intentional, narrower contract (b"\\n" is the only authoritative
terminator) rather than an oversight: real Tech Logic content can contain
stray control bytes (see the SIP control-character content documented in
the onsite recon) that must never be mistaken for an intended line break.

Deliberately has no SQLite/database dependency, and no knowledge of any
particular parser -- it hands back raw lines. What to do with a rotation
or truncation event (e.g. EOF-bootstrap vs. explicit replay on first
contact with a file) is a durable-state decision, not a tailer decision --
see agent/state.py (Phase C) for that. A cursor of None here just means
"no prior position is known," and is read starting at byte 0 -- the same
behavior agent/watcher.py already had; Phase C/D decide whether "no prior
position" should instead mean "seed at current EOF" for a normal
production install.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import discovery
from .discovery import SourceIdentity


@dataclass(frozen=True)
class FileCursor:
    """Durable, storage-agnostic position in one source file. identity is
    None only when no source has ever been successfully identified at
    this path yet. offset is always a true, physical byte count -- see
    the module docstring's OFFSET REPRESENTATION section."""

    identity: SourceIdentity | None
    offset: int


@dataclass(frozen=True)
class TailResult:
    """line_offsets[i] is the true byte position at which lines[i]
    BEGINS -- same length as lines, same list always (never shorter/
    longer), added for Continuous Ingestion Phase E: deterministic
    source-event identity needs the offset of the exact physical record a
    line came from, not merely the batch's aggregate start/end offset.
    Like cursor.offset, these are real byte counts (see the module
    docstring's OFFSET REPRESENTATION section) -- safe for f.seek(),
    stable identity comparison, AND direct numeric arithmetic/ordering."""

    lines: list[str]
    line_offsets: list[int]
    cursor: FileCursor
    existed: bool
    rotated: bool
    truncated: bool


def _decode_line(raw_line: bytes) -> str:
    """Decodes one already byte-boundary-confirmed complete line (see
    _read_safe_lines -- this is only ever called on bytes already known
    to end in b"\\n"). errors="replace" mirrors the previous text-mode
    behavior for genuinely invalid UTF-8 (e.g. raw SIP control-character
    bytes) -- malformed bytes become U+FFFD rather than raising, but
    unlike the previous text-mode implementation, that substitution can
    no longer corrupt this line's OWN byte-offset accounting, because the
    byte offset was already fixed before decoding ever ran.

    A trailing "\\r\\n" is normalized to "\\n" -- see the module
    docstring's Windows CRLF section for why this (and only this) legacy
    text-mode behavior is preserved.
    """
    text = raw_line.decode("utf-8", errors="replace")
    if text.endswith("\r\n"):
        text = text[:-2] + "\n"
    return text


def _read_safe_lines(path: str, start_offset: int) -> tuple[list[str], list[int], int]:
    """Reads from start_offset to the last complete line in the file.

    A trailing chunk with no newline yet is left unconsumed: the returned
    offset points just past the last line that ended in b"\\n", never past
    a line that might still be mid-write. Opens the file in BINARY mode
    and uses f.tell()/f.seek() as true byte offsets throughout -- see the
    module docstring's OFFSET REPRESENTATION section for why this
    replaced an earlier text-mode implementation.

    Also returns each kept line's own true starting byte offset (captured
    via f.tell() immediately before reading it) -- see
    TailResult.line_offsets.
    """
    lines: list[str] = []
    line_offsets: list[int] = []
    safe_offset = start_offset

    with open(path, "rb") as f:
        f.seek(start_offset)

        while True:
            line_start = f.tell()
            raw_line = f.readline()

            if not raw_line:
                break  # real EOF

            if not raw_line.endswith(b"\n"):
                # Partial trailing line -- the writer hasn't finished this
                # line yet. Stop here; don't consume it. Decided on the
                # RAW bytes, before any decoding is attempted.
                break

            lines.append(_decode_line(raw_line))
            line_offsets.append(line_start)
            safe_offset = f.tell()

    return lines, line_offsets, safe_offset


def read_new_lines(path: str, cursor: FileCursor | None) -> TailResult:
    """Reads whatever new, complete lines are safely available at `path`
    since `cursor`, handling rotation and truncation the same way proven
    safe in Phase 1, but through the SourceIdentity abstraction instead of
    raw stat fields.

    Never raises on a missing/unreadable file -- that's a normal condition
    (TLC hasn't written it yet, or it's momentarily locked/rotating), not
    fatal. Returns existed=False with the cursor unchanged in that case,
    so the caller can retry on the next cycle without losing position.
    """
    current_identity = discovery.identify(path)

    if current_identity is None:
        return TailResult(
            lines=[],
            line_offsets=[],
            cursor=cursor if cursor is not None else FileCursor(identity=None, offset=0),
            existed=False,
            rotated=False,
            truncated=False,
        )

    rotated = False
    truncated = False

    if cursor is None or cursor.identity is None:
        start_offset = 0
    elif cursor.identity != current_identity:
        # A different file now lives at this path -- Tech Logic rotated it.
        # Detected regardless of the new file's size, unlike the deployed
        # baseline's size-only check.
        start_offset = 0
        rotated = True
    else:
        try:
            current_size = os.path.getsize(path)
        except OSError:
            return TailResult(
                lines=[], line_offsets=[], cursor=cursor, existed=False, rotated=False, truncated=False
            )

        if current_size < cursor.offset:
            # Same file identity, but shrunk -- truncated in place.
            # Valid comparison: both sides are true byte counts (see the
            # module docstring's OFFSET REPRESENTATION section) -- this
            # was NOT always true before the binary-mode fix, when
            # cursor.offset could be an opaque text-mode cookie in a
            # different, larger numeric domain than os.path.getsize().
            start_offset = 0
            truncated = True
        else:
            start_offset = cursor.offset

    try:
        lines, line_offsets, end_offset = _read_safe_lines(path, start_offset)
    except OSError:
        # File existed a moment ago (identify() succeeded) but became
        # unreadable before we could open it -- e.g. a narrow race with
        # rotation. Treat as "try again next cycle," not fatal.
        return TailResult(
            lines=[],
            line_offsets=[],
            cursor=cursor or FileCursor(current_identity, 0),
            existed=False,
            rotated=False,
            truncated=False,
        )

    return TailResult(
        lines=lines,
        line_offsets=line_offsets,
        cursor=FileCursor(identity=current_identity, offset=end_offset),
        existed=True,
        rotated=rotated,
        truncated=truncated,
    )
