"""Contract v2: the raw line reader -- part of the RAW layer (docs/collector-v2.md).

This is the ONLY module that turns a Tech Logic file into text, and only collector/v2_transform.py may import it. Its output (raw lines) never
leaves the transformation layer; what leaves is typed, privacy-safe events and an integer cursor.

It reads BOUNDED chunks: at most `max_lines` COMPLETE lines from a cursor, in binary mode so offsets are true byte counts, never past a line
that might still be mid-write (Tech Logic writes while we read). Rotation is detected by file identity and truncation by a shrinking size, as in
the v1 reader (whose primitives it reuses). `read_tail` supports the dry run: complete lines from the last N bytes only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import reader
from .v2_events import Cursor


@dataclass(frozen=True)
class ChunkRead:
    lines: list[str]
    cursor: Cursor | None   # where to resume; None only if the file does not exist
    existed: bool
    rotated: bool
    truncated: bool
    more: bool              # True if the chunk stopped at the line limit and the file has more bytes


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    return text[:-2] + "\n" if text.endswith("\r\n") else text


def read_chunk(path: str, cursor: Cursor | None, max_lines: int) -> ChunkRead:
    current = reader.identify(path)
    if current is None:
        return ChunkRead([], cursor, False, False, False, False)

    identity = current.token
    rotated = truncated = False
    if cursor is None or cursor.identity is None:
        start = 0
    elif cursor.identity != identity:
        start, rotated = 0, True
    else:
        try:
            size = os.path.getsize(path)
        except OSError:
            return ChunkRead([], cursor, False, False, False, False)
        if size < cursor.offset:
            start, truncated = 0, True
        else:
            start = cursor.offset

    lines: list[str] = []
    safe = start
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            while len(lines) < max_lines:
                raw = handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    break  # EOF, or a partial trailing line: do not consume it
                lines.append(_decode(raw))
                safe = handle.tell()
        more = os.path.getsize(path) > safe
    except OSError:
        return ChunkRead([], cursor, False, False, False, False)
    return ChunkRead(lines, Cursor(identity, safe), True, rotated, truncated, more and len(lines) >= max_lines)


def read_tail(path: str, tail_bytes: int) -> list[str]:
    """Complete lines from (about) the last `tail_bytes` of the file. The first, possibly cut, line is dropped; a trailing partial line is not
    returned. Reads nothing else and changes nothing."""
    try:
        size = os.path.getsize(path)
        start = max(0, size - tail_bytes)
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    if start > 0:
        cut = data.find(b"\n")
        data = b"" if cut < 0 else data[cut + 1:]
    end = data.rfind(b"\n")
    if end < 0:
        return []
    return [_decode(line + b"\n") for line in data[:end].split(b"\n")]
