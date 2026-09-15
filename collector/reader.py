"""Binary-safe incremental line reader (Phase 4a).

Reimplements, as fresh and independent code, the two proven concepts from
agent/tailer.py's and agent/discovery.py's OFFSET REPRESENTATION /
FAILURE ISOLATION work -- deliberately NOT imported from there (see
collector/__init__.py's module docstring for why this package has zero
dependency on the continuous-agent architecture):

  1. TRUE BYTE OFFSETS. Reads the source file in BINARY mode. cursor
     offsets are always real, physical byte counts -- never a text-mode
     "opaque cookie" that can, for content containing invalid UTF-8 (the
     exact failure class already proven, in this project's own history,
     to corrupt an ACS Log.txt offset into a 52-digit non-byte-count
     value), silently stop being comparable to os.path.getsize().

  2. PARTIAL-LINE SAFETY. A "complete" line is any run of bytes up to and
     including one b"\\n" byte, decided BEFORE any decoding is attempted.
     A trailing chunk with no b"\\n" yet (Tech Logic mid-write) is left
     completely unconsumed -- the returned offset never advances past it.

IDENTITY-BASED ROTATION DETECTION, alongside the pre-existing size check:
an (st_dev, st_ino)-equivalent pair (os.stat's own fields, unmodified) is
compared against whatever was last persisted for this source. This closes
the documented blind spot in the size-only check this project already
knows about (a same-size-or-larger replacement file passes a size-only
check undetected) -- see agent/tailer.py's own module docstring for the
original motivating case.

VALIDATION STATUS -- explicit, not glossed over (Phase 3 correction):
this mechanism is NOT yet proven against real Tech Logic
truncation/replacement/rotation patterns on the actual AMH machine. Unit
tests here prove the LOGIC is internally correct; they do not and cannot
prove real Windows/Tech Logic file behavior matches what this module
assumes. A dedicated onsite validation step is required before this is
relied on in production -- see the Phase 3 gap audit and
docs/collector-v1 (once written in a later phase). No claim is made here
about what happens if that validation ever finds a mismatch; that is
exactly what the validation step exists to discover, not something to
predict in advance.

MISSING/STALE SOURCE HANDLING: a source file that does not exist (Tech
Logic hasn't created it yet, or it's between rotation and recreation) is
a normal, non-fatal condition -- read_new_lines returns existed=False
with the cursor unchanged, the same behavior already proven in both the
legacy pipeline and the continuous agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class FileIdentity:
    """Opaque, comparable identity for a source file at a point in time.
    Backed by (st_dev, st_ino) -- see this module's docstring. Callers
    must never inspect `token`'s contents, only compare two instances for
    equality."""

    token: tuple[int, int]


def identify(path: str) -> FileIdentity | None:
    """Current identity of the file at `path`, or None if it does not
    exist right now -- a missing source file is normal, not an error."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return FileIdentity(token=(st.st_dev, st.st_ino))


@dataclass(frozen=True)
class SourceCursor:
    """Durable position in one source file. identity is None only when no
    source has ever been successfully identified at this path yet.
    offset is always a true, physical byte count (see module docstring)."""

    identity: FileIdentity | None
    offset: int


@dataclass(frozen=True)
class ReadResult:
    lines: list[str]
    cursor: SourceCursor
    existed: bool
    rotated: bool
    truncated: bool


def _decode_line(raw_line: bytes) -> str:
    """Decodes one already byte-boundary-confirmed complete line.
    errors="replace" handles genuinely invalid UTF-8 (e.g. raw SIP
    control-character content in ACS Log.txt) without raising and without
    affecting this line's own byte-offset accounting, since the offset
    was already fixed before decoding ran. A trailing "\\r\\n" is
    normalized to "\\n" to match ordinary Windows-authored line endings;
    a bare "\\r" with no following "\\n" is left as ordinary content, not
    treated as its own line terminator."""
    text = raw_line.decode("utf-8", errors="replace")
    if text.endswith("\r\n"):
        text = text[:-2] + "\n"
    return text


def _read_complete_lines(path: str, start_offset: int) -> tuple[list[str], int]:
    """Reads from start_offset to the last complete line. Returns
    (lines, end_offset) where end_offset points just past the last line
    that ended in b"\\n" -- never past a line that might still be
    mid-write."""
    lines: list[str] = []
    safe_offset = start_offset

    with open(path, "rb") as f:
        f.seek(start_offset)
        while True:
            raw_line = f.readline()
            if not raw_line:
                break  # real EOF
            if not raw_line.endswith(b"\n"):
                break  # partial trailing line -- do not consume
            lines.append(_decode_line(raw_line))
            safe_offset = f.tell()

    return lines, safe_offset


def read_new_lines(path: str, cursor: SourceCursor | None) -> ReadResult:
    """Reads whatever new, complete lines are available at `path` since
    `cursor`. Never raises on a missing/unreadable file."""
    current_identity = identify(path)

    if current_identity is None:
        return ReadResult(
            lines=[],
            cursor=cursor if cursor is not None else SourceCursor(identity=None, offset=0),
            existed=False,
            rotated=False,
            truncated=False,
        )

    rotated = False
    truncated = False

    if cursor is None or cursor.identity is None:
        start_offset = 0
    elif cursor.identity != current_identity:
        start_offset = 0
        rotated = True
    else:
        try:
            current_size = os.path.getsize(path)
        except OSError:
            return ReadResult(
                lines=[], cursor=cursor, existed=False, rotated=False, truncated=False
            )

        if current_size < cursor.offset:
            start_offset = 0
            truncated = True
        else:
            start_offset = cursor.offset

    try:
        lines, end_offset = _read_complete_lines(path, start_offset)
    except OSError:
        # File existed a moment ago (identify() succeeded) but became
        # unreadable before we could open it -- treat as "try again next
        # scheduled run," not fatal to this one.
        return ReadResult(
            lines=[],
            cursor=cursor or SourceCursor(current_identity, 0),
            existed=False,
            rotated=False,
            truncated=False,
        )

    return ReadResult(
        lines=lines,
        cursor=SourceCursor(identity=current_identity, offset=end_offset),
        existed=True,
        rotated=rotated,
        truncated=truncated,
    )
