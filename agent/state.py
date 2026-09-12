"""Canonical durable state layer (Continuous Ingestion Phase C).

Replaces agent/outbox.py's SQLite `file_state` table with a single,
schema-versioned, atomically-written JSON file tracking read progress for
the three independent sources (checkins, rejects, acs). This module owns
ONLY source read-progress persistence -- it has no knowledge of HTTP,
spool files, retries, or backend responses. That's Phase D's spool.py.

The core invariant this whole phase exists to serve:

    THE CURSOR MAY LAG DURABLE CAPTURE, BUT IT MUST NEVER LEAD IT.

This module cannot single-handedly guarantee that on its own -- it also
depends on Phase D's spool writer calling save_state() only AFTER a batch
is durably on disk, never before or concurrently with capturing it. What
THIS module guarantees is its half of the invariant: a write to the state
file is atomic (never observable as a torn/partially-written file) and
self-describing (schema_version, validated on every load), so whatever
cursor a caller reads back is exactly the last value it durably
committed -- never a partially-written value, and never silently
corrupted into some other arbitrary value.

Deliberately reuses agent.tailer.FileCursor (identity + offset) rather
than inventing a parallel representation -- a SourceState's cursor is
exactly what agent.tailer.read_new_lines both consumes and produces, so
there is nothing to translate between the two layers.

SOURCE GENERATION (schema v2, Phase D correction): byte offsets alone are
only meaningful WITHIN one continuous physical stream. When Tech Logic
rotates or truncates a source file, offsets restart from (or toward)
zero, and a stale offset from the old stream can numerically collide with
or precede an offset from the new one -- which broke Phase D's
spool-ordering guarantee (a post-rotation batch could sort ahead of an
older, still-pending pre-rotation batch). `SourceState.generation` is the
fix: a small, persisted, LOGICAL integer that increments exactly when the
tailer reports a discontinuity (rotated OR truncated -- see
advance_generation), and stays flat across everything else (normal
append, process restart, machine restart). It is explicitly NOT a raw
st_dev/st_ino value and is never derived by exposing those fields outside
tailer.py/discovery.py -- generation is computed here, in state.py, from
the tailer's own rotated/truncated signals, so downstream consumers
(Phase D's spool, Phase E's event identity) only ever see a small logical
counter, never OS filesystem identity. See advance_generation's docstring
for the exact, explicit rule for every case (bootstrap, append, restart,
rotation, truncation, path change).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .discovery import SourceIdentity
from .tailer import FileCursor

SCHEMA_VERSION = 2

# The three independent sources this agent has ever watched. Phase D/E/F
# are free to iterate this; state.py itself doesn't hardcode assumptions
# about which names exist beyond treating each entry in `sources` as
# independent -- see with_source/get_source below.
SOURCE_NAMES = ("checkins", "rejects", "acs")

DEFAULT_STATE_PATH = Path("data") / "agent_state.json"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class StateError(Exception):
    """Base class for every error this module raises deliberately."""


class CorruptStateError(StateError):
    """The state file exists but cannot be safely parsed or is
    structurally invalid (bad JSON, wrong types, a negative offset,
    etc.). Never silently swallowed into an empty state by load_state --
    see its docstring for why, and quarantine_corrupt_state for the
    explicit, caller-driven recovery path.
    """


class UnsupportedSchemaVersionError(StateError):
    """The state file's schema_version is not one this code knows how to
    read. Never silently guessed at or coerced."""


@dataclass(frozen=True)
class SourceState:
    """Durable read-progress for one source file.

    path is the configured source path AT THE TIME this state was last
    saved -- callers are responsible for comparing it against the
    currently configured path and deciding what a mismatch means (see
    path_changed below); state.py does not make that call itself.

    generation is a small, persisted, LOGICAL counter -- see the module
    docstring's SOURCE GENERATION section and advance_generation below
    for exactly when it changes. It is not a raw OS identity field.
    """

    path: str
    cursor: FileCursor
    generation: int = 0
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.cursor.offset < 0:
            raise CorruptStateError(
                f"offset must be non-negative, got {self.cursor.offset!r} for path {self.path!r}"
            )
        if self.generation < 0:
            raise CorruptStateError(
                f"generation must be non-negative, got {self.generation!r} for path {self.path!r}"
            )


@dataclass(frozen=True)
class AgentState:
    schema_version: int
    sources: dict[str, SourceState] = field(default_factory=dict)


def empty_state() -> AgentState:
    """The state of an agent that has never successfully persisted
    anything -- the normal condition on a genuinely first-ever run, not
    an error. Every source is simply absent; see get_source."""
    return AgentState(schema_version=SCHEMA_VERSION, sources={})


def get_source(state: AgentState, name: str) -> SourceState | None:
    """None means "no persisted progress for this source" -- a normal,
    expected condition (first run for this source, or a source added
    after the state file was first created), never treated as
    corruption. Callers (Phase D/E bootstrap logic) decide what a
    missing entry means -- e.g. seed at current EOF vs. byte 0 -- this
    module does not invent a default cursor on their behalf.
    """
    return state.sources.get(name)


def with_source(state: AgentState, name: str, source_state: SourceState) -> AgentState:
    """Returns a NEW AgentState with `name`'s entry replaced. AgentState
    is immutable -- every update is an explicit copy, so nothing can ever
    observe a state object mid-update."""
    new_sources = dict(state.sources)
    new_sources[name] = source_state
    return AgentState(schema_version=state.schema_version, sources=new_sources)


def update_source(
    state: AgentState, name: str, *, path: str, cursor: FileCursor, generation: int
) -> AgentState:
    """Convenience wrapper around with_source for the common case: record
    a fresh cursor (typically straight from tailer.read_new_lines's
    result) for `name`, stamped with the current time.

    generation is required, not inferred here -- state.py doesn't decide
    when a generation bump is warranted on its own; the caller computes
    it explicitly via advance_generation (using the SAME
    tailer.read_new_lines result the cursor came from) and passes it in.
    This keeps the actual bump rule in exactly one place rather than
    duplicated or implied.
    """
    return with_source(
        state,
        name,
        SourceState(path=path, cursor=cursor, generation=generation, updated_at=_now_iso()),
    )


def advance_generation(prior: SourceState | None, *, rotated: bool, truncated: bool) -> int:
    """Computes the generation number for a source's NEXT persisted
    state, given the tailer's own rotated/truncated signals from the
    read that produced the new cursor. This is the one place the actual
    bump rule lives -- see the module docstring's SOURCE GENERATION
    section for the reasoning, and here for the exact case-by-case
    behavior:

      - first-ever bootstrap (prior is None): generation 0. There is no
        prior stream to be a continuation or a break from.
      - normal append (same identity, offset only grows, rotated=False,
        truncated=False): generation UNCHANGED. This is overwhelmingly
        the common case -- every ordinary poll cycle.
      - process restart / machine restart: UNCHANGED, automatically --
        this function is never even involved in a restart with no new
        read; the persisted generation is simply loaded back from disk
        by load_state exactly as it was. Restart only interacts with
        this function indirectly, via whatever the NEXT real read after
        restart reports.
      - true identity-changing rotation (tailer reports rotated=True):
        generation = prior + 1. A different physical file now exists at
        this path; its offsets start over from 0 and must not be
        compared numerically against the old file's offsets.
      - in-place truncation (tailer reports truncated=True, identity
        unchanged): generation = prior + 1, treated identically to
        rotation. Verified against agent/tailer.py's actual semantics,
        not assumed: read_new_lines resets start_offset to 0 for a
        truncation exactly the same way it does for a rotation. Without
        a generation bump here, a pre-truncation pending batch (e.g.
        offset 400000-410000) would numerically outrank a
        post-truncation batch (0-10000) that is chronologically LATER --
        reintroducing the identical starvation bug this whole correction
        exists to fix, just via truncation instead of rotation. So: yes,
        truncation counts as a new generation, exactly as expected.
      - configured path change: not special-cased directly here. In
        practice, pointing a source at a genuinely different file
        produces a different identity the very next time it's stat'd,
        which the tailer already reports as rotated=True through its
        normal identity-mismatch detection -- so a path change bumps the
        generation via the SAME rule as rotation, without needing a
        separate code path. If a future caller ever needs to force a
        generation bump without going through the tailer (e.g. an
        operator-directed reset), that would be a deliberate, explicit,
        separate action -- not something this function infers.
    """
    if prior is None:
        return 0
    if rotated or truncated:
        return prior.generation + 1
    return prior.generation


def path_changed(source_state: SourceState | None, configured_path: str) -> bool:
    """True if `configured_path` differs from what was persisted last
    time -- e.g. an operator repointed this source to a new location.
    Returns False when source_state is None (nothing persisted yet, so
    there is nothing to have "changed" from). What to actually DO about a
    changed path -- treat as a new source, refuse to start, etc. -- is a
    Phase D/E/F decision, not this module's."""
    if source_state is None:
        return False
    return source_state.path != configured_path


# ---------------------------------------------------------------------
# Serialization. SourceIdentity is never exposed to the JSON document as
# raw OS fields -- only as its own opaque `token` list, round-tripped
# through discovery.SourceIdentity so nothing outside tailer.py/state.py
# ever has to know what's inside it.
# ---------------------------------------------------------------------


def _serialize_identity(identity: SourceIdentity | None) -> list[int] | None:
    if identity is None:
        return None
    return list(identity.token)


def _deserialize_identity(raw: Any, *, context: str) -> SourceIdentity | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in raw):
        raise CorruptStateError(f"{context}: identity must be a list of integers, got {raw!r}")
    return SourceIdentity(token=tuple(raw))


def _serialize_source(source_state: SourceState) -> dict[str, Any]:
    return {
        "path": source_state.path,
        "identity": _serialize_identity(source_state.cursor.identity),
        "offset": source_state.cursor.offset,
        "generation": source_state.generation,
        "updated_at": source_state.updated_at,
    }


def _deserialize_source(name: str, raw: Any, *, default_generation: int | None = None) -> SourceState:
    context = f"source {name!r}"

    if not isinstance(raw, dict):
        raise CorruptStateError(f"{context}: entry must be an object, got {raw!r}")

    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise CorruptStateError(f"{context}: 'path' must be a non-empty string, got {path!r}")

    offset = raw.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise CorruptStateError(f"{context}: 'offset' must be an integer, got {offset!r}")
    if offset < 0:
        raise CorruptStateError(f"{context}: 'offset' must be non-negative, got {offset!r}")

    # default_generation is only ever passed for a v1->v2 migration
    # (v1 documents predate this field entirely). A v2 document must
    # have it, validated exactly like offset.
    generation: int
    if "generation" not in raw and default_generation is not None:
        generation = default_generation
    else:
        generation_raw = raw.get("generation")
        if not isinstance(generation_raw, int) or isinstance(generation_raw, bool):
            raise CorruptStateError(f"{context}: 'generation' must be an integer, got {generation_raw!r}")
        if generation_raw < 0:
            raise CorruptStateError(f"{context}: 'generation' must be non-negative, got {generation_raw!r}")
        generation = generation_raw

    identity = _deserialize_identity(raw.get("identity"), context=context)

    updated_at = raw.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        raise CorruptStateError(f"{context}: 'updated_at' must be a string or null, got {updated_at!r}")

    return SourceState(
        path=path,
        cursor=FileCursor(identity=identity, offset=offset),
        generation=generation,
        updated_at=updated_at,
    )


def to_json_dict(state: AgentState) -> dict[str, Any]:
    return {
        "schema_version": state.schema_version,
        "sources": {name: _serialize_source(s) for name, s in state.sources.items()},
    }


_MIGRATABLE_SCHEMA_VERSIONS = (1,)


def from_json_dict(raw: Any) -> AgentState:
    """The single place schema migration branches on schema_version.

    Two versions are accepted on read:

      - SCHEMA_VERSION (2): the current shape, including per-source
        'generation'. Loaded as-is.
      - 1: the pre-generation shape (Phase C). Every source in a v1
        document predates the generation concept entirely, so each is
        migrated in-memory by defaulting its generation to 0 -- the same
        value advance_generation() would produce for a source that has
        never seen a rotation or truncation. The returned AgentState
        always carries schema_version=SCHEMA_VERSION (2), never the
        original document's version, so the very next save_state() call
        persists the upgraded shape and this migration only ever runs
        once per state file.

    Any other version is a hard, explicit error -- never silently
    reinterpreted as the current version, and never a reason to fabricate
    an empty state.
    """
    if not isinstance(raw, dict):
        raise CorruptStateError(f"state document root must be an object, got {type(raw).__name__}")

    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION and schema_version not in _MIGRATABLE_SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersionError(
            f"state file has schema_version={schema_version!r}, this code only supports "
            f"{SCHEMA_VERSION} (and migrates {_MIGRATABLE_SCHEMA_VERSIONS!r})"
        )

    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, dict):
        raise CorruptStateError(f"state document 'sources' must be an object, got {sources_raw!r}")

    # A v1 document never had 'generation' at all; default it to 0 for
    # every source it contains. A v2 document must already have it on
    # each source, so default_generation stays None and a missing field
    # is treated as corruption, not silently migrated.
    default_generation = 0 if schema_version in _MIGRATABLE_SCHEMA_VERSIONS else None

    sources = {
        name: _deserialize_source(name, entry, default_generation=default_generation)
        for name, entry in sources_raw.items()
    }

    return AgentState(schema_version=SCHEMA_VERSION, sources=sources)


# ---------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------


def load_state(path: str | Path) -> AgentState:
    """Loads state from disk.

      - no file at all             -> empty_state() (normal first run)
      - valid file                 -> the parsed AgentState
      - malformed JSON / invalid
        document structure         -> raises CorruptStateError
      - unrecognized schema_version -> raises UnsupportedSchemaVersionError

    Deliberately never collapses the last two cases into a silent
    empty_state(): by the time Phase D exists, spool data may already
    have been durably captured past whatever cursor this file was
    supposed to hold. Silently forging a fresh empty state out of a
    corrupted file could make a caller believe capture legitimately never
    started, when in fact it's this file's own record of it that's
    missing -- a decision with real consequences (re-reading and
    re-uploading is fine; a caller assuming "no data exists yet" and
    acting on that assumption is not this module's call to make quietly).
    See quarantine_corrupt_state for the explicit, visible recovery path.
    """
    p = Path(path)
    if not p.exists():
        return empty_state()

    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptStateError(f"state file exists but could not be read: {exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CorruptStateError(f"state file is not valid JSON: {exc}") from exc

    return from_json_dict(raw)


def quarantine_corrupt_state(path: str | Path) -> Path | None:
    """Renames an unreadable/corrupt state file aside (never deletes it)
    so an operator or a later diagnostic pass can inspect what actually
    went wrong, and returns the quarantined path -- or None if there was
    no file to quarantine.

    This is an explicit action a CALLER takes after load_state raises,
    having decided to proceed with a fresh empty_state() -- it is never
    invoked automatically by load_state itself. The distinction matters:
    load_state raising is "I don't know what this is, I'm not guessing";
    calling this function is "a human or a higher-level policy decided
    it's safe to move on."
    """
    p = Path(path)
    if not p.exists():
        return None

    quarantine_path = p.with_name(f"{p.name}.corrupt.{_now_iso().replace(':', '-')}")
    os.replace(str(p), str(quarantine_path))
    return quarantine_path


def save_state(path: str | Path, state: AgentState) -> None:
    """Atomically writes `state` to `path`.

    Sequence: serialize to a temp file created in the SAME directory as
    the target (os.replace across filesystems is not atomic on any
    platform -- same-directory guarantees same-filesystem), write, flush,
    fsync the temp file's contents, then os.replace the temp file over
    the target.

    Windows specifics (verified empirically for this phase, not assumed):
      - os.replace maps to Windows' MoveFileExW with
        MOVEFILE_REPLACE_EXISTING, which -- unlike os.rename, which
        raises FileExistsError on Windows when the destination exists --
        atomically replaces an existing destination file. This is exactly
        why os.replace (not os.rename) is used here.
      - IMPORTANT, found empirically while building this: unlike POSIX,
        os.replace on Windows FAILS with PermissionError ([WinError 5]
        Access is denied) if anything -- even a plain read handle in the
        SAME process opened via ordinary open() -- still has the
        destination file open at the moment of replacement. Confirmed
        directly (see test_state.py) rather than assumed from
        documentation; this is not a hypothetical edge case, it
        reproduces every time. The practical consequence: nothing in this
        codebase may hold a lingering open handle to the state file.
        load_state() already satisfies this (it opens, reads fully, and
        closes before returning) -- this is a real constraint for every
        future caller and for Phase D's spool design to inherit, not just
        a note for this module.
      - There is no POSIX-style "fsync the containing directory" primitive
        on Windows (os.open(dir, os.O_RDONLY) is a POSIX pattern); NTFS's
        own metadata journal covers rename durability there instead, so
        that step is POSIX-only below, not a gap on Windows.

    A crash at any point before the final os.replace call leaves the
    previous state file completely untouched -- there is no window in
    which the live file is observed half-written.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(to_json_dict(state), indent=2)

    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        os.replace(str(tmp_path), str(p))

        if os.name != "nt":
            # Best-effort directory fsync so the rename itself is durable
            # across a crash -- POSIX only, see docstring above.
            dir_fd = os.open(str(p.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(str(tmp_path))
        raise
