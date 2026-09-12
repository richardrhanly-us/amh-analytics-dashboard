"""Source file identity abstraction (Continuous Ingestion Phase B).

Correction from the Phase A review: (st_dev, st_ino) must not leak into
every layer of the new architecture. Its rotation-detection behavior was
proven safe in this repo's Phase 1 investigation (verified on Windows/NTFS:
stable across appends, changes across delete+recreate, unlike creation
time which NTFS "tunneling" can silently preserve) -- but that
investigation happened on one dev machine, not the real AMH machine, and
Phase A's own audit lists "real filesystem identity semantics" as an item
that still needs validation there.

So: tailer.py is the only module allowed to know identity is currently
(st_dev, st_ino). Everything downstream -- state.py, spool.py, event-id
logic -- only ever sees a SourceIdentity, and only ever compares two of
them for equality. If real-machine validation later shows a different
mechanism is needed, only identify()/SourceIdentity change; no other
module's code or tests should need to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceIdentity:
    """Opaque, comparable identity for a source file at a point in time.

    Two SourceIdentity values compare equal iff the tailer considers them
    the same underlying file -- callers must never inspect `token`'s
    contents or assume anything about its shape beyond that it supports
    equality. `token` is a tuple so this stays trivially hashable/
    serializable without needing custom __eq__/__hash__ logic.
    """

    token: tuple[int, ...]


def identify(path: str) -> SourceIdentity | None:
    """Returns the current identity of the file at `path`, or None if it
    does not exist right now (a missing source file is a normal,
    non-fatal condition -- TLC may not have created it yet, or it's
    between rotation and recreation).

    Currently backed by (st_dev, st_ino) -- see the module docstring for
    why this is the one place in the codebase allowed to know that.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        # A transient stat failure (e.g. the file is mid-rotation on some
        # filesystems) is treated the same as "not currently identifiable"
        # rather than propagating -- the tailer already handles None
        # identity as "can't read right now, try again next cycle."
        return None

    return SourceIdentity(token=(st.st_dev, st.st_ino))
