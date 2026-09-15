"""Atomic state and status persistence (Phase 4a).

Reimplements, as fresh and independent code, the proven atomic-write
pattern from agent/state.py::save_state -- deliberately NOT imported from
there (see collector/__init__.py). Directly motivated by two real,
distinct findings from this project's own history and the live-AMH
verification:

  - A reported `PermissionError: [WinError 5] Access is denied` while
    replacing agent_state.json -- the exact failure shape a plain
    `open(path, "w")` write (which is what BOTH the legacy pipeline's
    save_pipeline_state/write_status_file and an earlier iteration of the
    continuous agent's own state writer had to guard against) is
    vulnerable to on Windows if anything -- even a same-process read
    handle -- still has the destination open at the moment of
    replacement.
  - Disk exhaustion is only PARTLY covered by this module: an atomic
    write here fails LOUDLY (raises, leaves the previous valid file
    completely untouched) rather than silently corrupting -- that is what
    this module actually guarantees. It does NOT guarantee anything about
    logs, temp files, or the clean CSV output written elsewhere in the
    pipeline; disk exhaustion can still affect those independently. This
    module's contract is scoped to the two files it owns: state.json and
    status.json.

Used for BOTH state (cursor/offset/identity per source) and status (the
human/backend-facing run report) -- both are equally exposed to the same
class of Windows write-replacement risk, so both get the same atomic
treatment, not just the one that happened to be reported.

VALIDATION STATUS -- explicit, not overclaimed: this module's
interruption-safety and Windows same-process-open-handle behavior are
UNIT-TESTED (tests/test_collector_state.py reproduces both a crash
mid-write and a held-open destination handle, on the development
machine, and confirms the previous valid file survives untouched
either way). That is NOT the same claim as "proven safe against real
Tech Logic concurrent file access on the actual AMH machine" -- the
original WinError 5 report happened under real, uncontrolled production
I/O this module's tests cannot reproduce by construction. Onsite
validation on the real AMH machine remains required before this is
relied on in production, the same standing this project already applies
to collector/reader.py's identity-based rotation detection.
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

STATE_SCHEMA_VERSION = 1

# No real Tech Logic file on any realistic deployment will ever be
# anywhere close to this many bytes (10**18 is an exabyte) -- a sanity
# backstop against a corrupted/non-byte-count offset value ever being
# accepted, the same class of protection this project's continuous-agent
# work already added after a real incident (see agent/state.py).
_MAX_PLAUSIBLE_OFFSET = 10**18


class StateError(Exception):
    """Base class for every error this module raises deliberately."""


class CorruptStateError(StateError):
    """The state or status file exists but cannot be safely parsed or is
    structurally invalid. Never silently reinterpreted as empty -- a
    caller must explicitly decide recovery (e.g. quarantine the file and
    proceed fresh), the same principle already proven in agent/state.py."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------
# Atomic write primitive -- shared by state and status.
# ---------------------------------------------------------------------


def atomic_write_json(path: str | Path, data: dict[str, Any]) -> None:
    """Atomically writes `data` as JSON to `path`: temp file in the SAME
    directory (same filesystem, required for os.replace to be atomic) ->
    write -> flush -> fsync -> close -> os.replace.

    A crash or a disk-full condition at any point before the final
    os.replace leaves the previous file completely untouched -- there is
    no window where a reader could observe a torn/partially-written file.
    Raises (OSError, typically) rather than ever leaving a corrupt file
    behind.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(str(tmp_path), str(p))
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(str(tmp_path))
        raise


def _read_json(path: str | Path) -> Any:
    p = Path(path)
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptStateError(f"{p}: exists but could not be read: {exc}") from exc

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CorruptStateError(f"{p}: not valid JSON: {exc}") from exc


def quarantine_corrupt_file(path: str | Path) -> Path | None:
    """Renames an unreadable/corrupt file aside (never deletes) so an
    operator or a later diagnostic pass can inspect what went wrong.
    Returns the quarantined path, or None if there was no file to
    quarantine. An explicit action a CALLER takes after load_state/
    load_status raises -- never invoked automatically."""
    p = Path(path)
    if not p.exists():
        return None
    quarantine_path = p.with_name(f"{p.name}.corrupt.{_now_iso().replace(':', '-')}")
    os.replace(str(p), str(quarantine_path))
    return quarantine_path


# ---------------------------------------------------------------------
# Cursor / offset state
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class SourceState:
    """identity is None only when this source has never been
    successfully identified yet. offset is always a true, physical byte
    count -- see collector/reader.py."""

    identity: tuple[int, int] | None
    offset: int

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise CorruptStateError(f"offset must be non-negative, got {self.offset!r}")
        if self.offset > _MAX_PLAUSIBLE_OFFSET:
            raise CorruptStateError(
                f"offset {self.offset!r} exceeds the maximum plausible byte offset "
                f"({_MAX_PLAUSIBLE_OFFSET!r}) -- not a real byte count"
            )


@dataclass(frozen=True)
class CollectorState:
    schema_version: int = STATE_SCHEMA_VERSION
    sources: dict[str, SourceState] = field(default_factory=dict)


def empty_state() -> CollectorState:
    """The state of a collector that has never successfully persisted
    anything -- the normal condition on a genuinely first-ever run."""
    return CollectorState()


def get_source(state: CollectorState, name: str) -> SourceState | None:
    return state.sources.get(name)


def with_source(state: CollectorState, name: str, source_state: SourceState) -> CollectorState:
    """Returns a NEW CollectorState with `name`'s entry replaced --
    immutable, so nothing can ever observe a partial update."""
    new_sources = dict(state.sources)
    new_sources[name] = source_state
    return CollectorState(schema_version=state.schema_version, sources=new_sources)


def _serialize_state(state: CollectorState) -> dict[str, Any]:
    return {
        "schema_version": state.schema_version,
        "sources": {
            name: {
                "identity": list(s.identity) if s.identity is not None else None,
                "offset": s.offset,
            }
            for name, s in state.sources.items()
        },
    }


def _deserialize_state(raw: Any) -> CollectorState:
    if not isinstance(raw, dict):
        raise CorruptStateError(f"state document root must be an object, got {type(raw).__name__}")

    schema_version = raw.get("schema_version")
    if schema_version != STATE_SCHEMA_VERSION:
        raise CorruptStateError(
            f"state file has schema_version={schema_version!r}, this code only supports "
            f"{STATE_SCHEMA_VERSION!r} -- never silently reinterpreted"
        )

    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, dict):
        raise CorruptStateError(f"state document 'sources' must be an object, got {sources_raw!r}")

    sources: dict[str, SourceState] = {}
    for name, entry in sources_raw.items():
        if not isinstance(entry, dict):
            raise CorruptStateError(f"source {name!r}: entry must be an object, got {entry!r}")

        offset = entry.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise CorruptStateError(f"source {name!r}: 'offset' must be an integer, got {offset!r}")

        identity_raw = entry.get("identity")
        if identity_raw is None:
            identity = None
        elif (
            isinstance(identity_raw, list)
            and len(identity_raw) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in identity_raw)
        ):
            identity = (identity_raw[0], identity_raw[1])
        else:
            raise CorruptStateError(
                f"source {name!r}: 'identity' must be a 2-element integer list or null, got {identity_raw!r}"
            )

        sources[name] = SourceState(identity=identity, offset=offset)

    return CollectorState(schema_version=STATE_SCHEMA_VERSION, sources=sources)


def load_state(path: str | Path) -> CollectorState:
    """- no file at all            -> empty_state() (normal first run)
    - valid file                  -> the parsed CollectorState
    - malformed/invalid           -> raises CorruptStateError

    Never silently collapses a corrupt file into empty_state() -- that
    could make a caller believe collection legitimately never started,
    when it's actually just this file's own record of it that's broken.
    See quarantine_corrupt_file for the explicit recovery path.
    """
    p = Path(path)
    if not p.exists():
        return empty_state()
    return _deserialize_state(_read_json(p))


def save_state(path: str | Path, state: CollectorState) -> None:
    atomic_write_json(path, _serialize_state(state))


# ---------------------------------------------------------------------
# Run status
# ---------------------------------------------------------------------


def load_status(path: str | Path) -> dict[str, Any]:
    """Returns {} if the file doesn't exist or can't be parsed -- unlike
    state, a missing/corrupt PRIOR status is not itself a correctness
    risk (it's a reporting artifact, not a cursor); losing it just means
    the next status write starts from a blank slate rather than carrying
    forward stale fields. This mirrors the legacy pipeline's own proven,
    reasonable behavior for this specific file."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = _read_json(p)
    except CorruptStateError:
        return {}
    return raw if isinstance(raw, dict) else {}


def write_status(path: str | Path, status: dict[str, Any]) -> None:
    atomic_write_json(path, status)
