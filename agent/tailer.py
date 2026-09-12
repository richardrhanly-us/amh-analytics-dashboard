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
    this path yet."""

    identity: SourceIdentity | None
    offset: int


@dataclass(frozen=True)
class TailResult:
    """line_offsets[i] is the seekable position at which lines[i] BEGINS
    -- same length as lines, same list always (never shorter/longer),
    added for Continuous Ingestion Phase E: deterministic source-event
    identity needs the offset of the exact physical record a line came
    from, not merely the batch's aggregate start/end offset. Like
    cursor.offset, these are f.tell()-style tokens (opaque on Windows
    text-mode files, not a true byte count) -- valid for f.seek() and for
    stable identity comparison, never for manual arithmetic."""

    lines: list[str]
    line_offsets: list[int]
    cursor: FileCursor
    existed: bool
    rotated: bool
    truncated: bool


def _read_safe_lines(path: str, start_offset: int) -> tuple[list[str], list[int], int]:
    """Reads from start_offset to the last complete line in the file.

    A trailing chunk with no newline yet is left unconsumed: the returned
    offset points just past the last line that ended in "\\n", never past
    a line that might still be mid-write. Uses f.tell()/f.seek() tokens
    directly (never manual byte arithmetic on them) since tell() is an
    opaque token on Windows text-mode files, not a true byte count.

    Also returns each kept line's own starting offset (captured via
    f.tell() immediately before reading it, the same opaque-but-seekable
    token style as everything else here) -- see TailResult.line_offsets.
    """
    lines: list[str] = []
    line_offsets: list[int] = []
    safe_offset = start_offset

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)

        while True:
            line_start = f.tell()
            line = f.readline()

            if not line:
                break  # real EOF

            if not line.endswith("\n"):
                # Partial trailing line -- the writer hasn't finished this
                # line yet. Stop here; don't consume it.
                break

            lines.append(line)
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
