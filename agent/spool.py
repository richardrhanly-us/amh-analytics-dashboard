"""Canonical durable spool layer (Continuous Ingestion Phase D).

Replaces agent/outbox.py's SQLite `local_events` table (the "unsent event
durability" responsibility from the Phase A SQLite mapping) with plain
NDJSON batch files on disk. This module owns ONLY durable batch capture,
enumeration, acknowledgment, and retry bookkeeping -- it has no knowledge
of HTTP, parsers, or cursors. It never imports agent.state or
agent.tailer, and it never advances anything resembling a cursor itself:
per the Phase C correction, ordering (spool-durable-BEFORE-cursor-advance)
is Phase E/F orchestration's job, not this module's. Enforced structurally
here by write_batch's return contract -- see its docstring.

Layout, one subtree per independent source (Phase A correction #5: source
-specific spool batches, not mixed-source batches):

    <spool_root>/
      pending/
        checkins/
          batch-<generation>-<start_offset>-<end_offset>-<ns>-<seq>-<rand>.ndjson
          batch-<generation>-<start_offset>-<end_offset>-<ns>-<seq>-<rand>.attempts.json
        rejects/
        acs/
      quarantine/
        checkins/
          batch-....ndjson              moved here verbatim, never deleted
          batch-....reason.json         why, and when
        rejects/
        acs/

ORDERING (Phase D correction #2, refined by the generation correction
below): filenames sort primarily on a LOGICAL source generation, then on
the source's own start/end byte offsets -- never on wall-clock time. This
was a deliberate change from an earlier version of this module that used
time.time_ns() first. Generation-then-offset was chosen because both
halves are immune to the failure modes that broke the timestamp-first
scheme:

  - process restart: a fresh process has no in-memory counter to reset --
    the next batch it writes necessarily starts at the offset (and
    carries the generation) the prior process (or agent/state.py) left
    off at, which is always >= every previously-written batch's
    (generation, end_offset) for that same source. No coordination file
    needed for this to hold.
  - system clock moving backward (NTP sync, manual correction, timezone
    change): neither offsets nor generation come from the wall clock, so
    neither can be disturbed by it at all.
  - source-file ROTATION OR TRUNCATION: offsets alone reset to a small
    value here (Phase B's tailer detects this via SourceIdentity, or a
    size shrink under the same identity, and reports offset 0 for the
    new/truncated stream), which previously let a batch written just
    after a rotation sort AHEAD of an older, still-pending batch written
    just before it -- a real starvation bug, not merely a rare fairness
    nicety, since a persistently-failing older batch could be starved
    indefinitely by an unbounded stream of newer post-rotation batches.
    FIXED by generation: agent/state.py's `SourceState.generation` is a
    small, persisted, LOGICAL counter (see its module docstring) that
    increments exactly when the tailer reports a discontinuity (rotated
    OR truncated) and is threaded into every batch this module writes for
    that source. Sort key is (generation, start_offset, end_offset, ...):
    within one generation, offsets alone order correctly exactly as
    before; across generations, the generation always wins, so a
    still-pending pre-rotation batch (e.g. generation 12, offset
    8,000,000-8,010,000) always sorts BEFORE any post-rotation batch
    (generation 13, offset 0-10,000) regardless of how their raw offsets
    compare. write_batch requires source_generation explicitly -- this
    module does not compute it; the caller reads it from
    agent.state.SourceState.generation (via agent.state.advance_generation
    the same call that produced the cursor being spooled).

A wall-clock timestamp and a strictly-increasing per-process sequence
number remain in the filename as tie-breakers (for two batches with
identical generation and offsets, which cannot happen for the same
source, or for human/log readability), not as the primary sort key.

DURABILITY (Phase D correction #3 -- do not overclaim): the write path
below (temp file -> flush -> fsync -> close -> os.replace, plus a
best-effort directory fsync on POSIX) gives atomic publication and is
safe against a normal process crash or interruption -- a reader can never
observe a torn/partial file, and previously-completed batches are never
disturbed by a failed later write. It is NOT a proven guarantee against
sudden power loss: directory fsync is only best-effort on POSIX, has no
equivalent invoked here on Windows, and this has not been validated
against real power-cut testing on the actual target hardware. If an
extreme crash/power event leaves genuine uncertainty about whether a
just-created batch survived, the system's actual safety net is the
invariant this whole phase serves: THE CURSOR MAY LAG DURABLE CAPTURE,
BUT IT MUST NEVER LEAD IT. An unadvanced cursor after such an event
causes safe re-reading, never silent data loss -- durability at the
single-write level is defense in depth, not the only thing standing
between this design and losing data.

FAILURE CLASSIFICATION (Phase D correction #1): record_attempt_failure
never quarantines a batch based on attempt count. A retryable
infrastructure failure (network/timeout/5xx/429) or an auth failure
(401/403) must NOT cause valid, undelivered event data to be quarantined
merely because delivery has failed repeatedly -- the batch stays pending
indefinitely; sustained failure is a health/monitoring signal (Phase G),
not a data-loss trigger. Quarantine remains available as its own,
separate, explicit action (quarantine_batch) for exactly two cases this
module actually knows about: malformed/unreadable spool content
(read_batch_or_quarantine), and a caller that has independently
determined -- via bad-event isolation logic that does not exist yet, see
Phase E/F -- that a specific, already-isolated batch reproduces a
permanent, deterministic backend rejection (400/413-shaped) on its own.
This module does not implement that isolation; it only continues to
provide the primitive (quarantine_batch) for whoever does.

DISK PRESSURE (Phase D correction #4 -- recorded, not implemented): this
phase does not enforce any disk-usage cap. get_spool_stats already
exposes what a later housekeeping/heartbeat check would need
(pending_bytes) to act on. The invariant for that later phase, recorded
here rather than silently decided: pending spool data must never be
deleted merely to satisfy a disk cap. The intended eventual behavior
is to report degraded/critical health and pause additional capture
before the host disk is exhausted, never advance a cursor for data that
was never durably spooled, and resume once capacity is restored. How a
prolonged capture pause should interact with Tech Logic's own log
rotation/retention on the real machine is an explicitly open question --
not decided, and not to be silently assumed, in this phase.

Windows constraint (found in Phase C, load-bearing here too): os.replace
fails with PermissionError if anything still has the destination file
open, even a same-process read handle. Every operation below that
renames or deletes a file (write_batch's install, quarantine_batch,
acknowledge) does so only after any read of that file has already fully
completed and closed its handle -- never while one is open.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

# Strictly increasing per-process tie-breaker for _new_batch_filename --
# only relevant when two batches for the same source somehow share the
# exact same (start_offset, end_offset) pair, which normal operation
# never produces (each batch covers a distinct, forward-moving byte
# range) -- kept as defense in depth, not the primary ordering mechanism.
_sequence_counter = itertools.count()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SpoolError(Exception):
    """Base class for every error this module raises deliberately."""


class CorruptBatchError(SpoolError):
    """A batch file exists but its content is not safely readable (I/O
    error, invalid JSON, or a line that isn't a JSON object). Raised by
    read_batch, never silently swallowed -- see read_batch_or_quarantine
    for the explicit, caller-visible recovery path."""


class FailureCategory(str, Enum):
    """Mirrors agent/outbox_uploader.py's proven classification from the
    experimental uploader -- preserved as a concept, not preserved as
    SQLite/recursive-splitting machinery. Only RETRYABLE_INFRA and
    AUTH_FAILURE are ever passed to record_attempt_failure in this phase
    (neither ever leads to quarantine here). PERMANENT_REJECTION is
    included so a future Phase E/F uploader has a name for the category
    it will use once it isolates a poison event down to its own batch and
    calls quarantine_batch directly -- this module does not act on it
    itself."""

    RETRYABLE_INFRA = "retryable_infra"
    AUTH_FAILURE = "auth_failure"
    PERMANENT_REJECTION = "permanent_rejection"


@dataclass(frozen=True)
class AttemptRecord:
    attempts: int
    category: str | None
    last_error: str | None
    last_attempt_at: str | None


@dataclass(frozen=True)
class BatchMetadata:
    """First-class source-position metadata for one batch, parsed from
    its filename. Exposed for Phase E idempotency (a deterministic event
    identity can be derived from source + generation + offset range) and
    diagnostics -- not just an internal sorting detail."""

    generation: int
    start_offset: int
    end_offset: int
    created_at: str | None


@dataclass(frozen=True)
class SpoolStats:
    pending_batch_count: int
    pending_bytes: int
    oldest_pending_created_at: str | None
    quarantined_batch_count: int
    quarantined_bytes: int


# ---------------------------------------------------------------------
# Paths / naming
# ---------------------------------------------------------------------


def _pending_dir(spool_root: str | Path, source_name: str) -> Path:
    return Path(spool_root) / "pending" / source_name


def _quarantine_dir(spool_root: str | Path, source_name: str) -> Path:
    return Path(spool_root) / "quarantine" / source_name


# Fixed widths so lexicographic string sort agrees with numeric sort.
# 20 digits comfortably exceeds any realistic file offset; 6 digits
# comfortably exceeds any realistic number of rotations/truncations a
# source could see across the agent's operational lifetime.
_OFFSET_WIDTH = 20
_GENERATION_WIDTH = 6


def _new_batch_filename(generation: int, start_offset: int, end_offset: int) -> str:
    ns = time.time_ns()
    seq = next(_sequence_counter)
    return (
        f"batch-{generation:0{_GENERATION_WIDTH}d}"
        f"-{start_offset:0{_OFFSET_WIDTH}d}-{end_offset:0{_OFFSET_WIDTH}d}"
        f"-{ns}-{seq:010d}-{uuid.uuid4().hex[:6]}.ndjson"
    )


def parse_batch_filename(path: str | Path) -> BatchMetadata | None:
    """Parses generation, start/end offsets, and a best-effort creation
    timestamp back out of a batch's own filename. Returns None for
    anything that doesn't match the expected shape rather than raising --
    this is a read-only diagnostic/metadata accessor, never load-bearing
    for correctness (the file's existence and content are what matter;
    this is about exposing what's already encoded in its name)."""
    name = Path(path).name
    if not name.endswith(".ndjson"):
        return None

    parts = name[: -len(".ndjson")].split("-")
    if len(parts) < 5 or parts[0] != "batch":
        return None

    try:
        generation = int(parts[1])
        start_offset = int(parts[2])
        end_offset = int(parts[3])
        ns = int(parts[4])
        created_at = datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, OSError, OverflowError):
        return None

    return BatchMetadata(generation=generation, start_offset=start_offset, end_offset=end_offset, created_at=created_at)


def _attempts_sidecar_path(batch_path: Path) -> Path:
    return batch_path.with_suffix(".attempts.json")


def _reason_sidecar_path(batch_path: Path) -> Path:
    return batch_path.with_suffix(".reason.json")


# ---------------------------------------------------------------------
# Atomic write primitive (same proven shape as agent/state.py's
# save_state -- duplicated here rather than imported from state.py to
# keep this module's only dependency on Phase C limited to nothing at
# all, per this phase's scope).
# ---------------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """temp file in the same directory -> write -> flush -> fsync ->
    close -> os.replace. The handle is fully closed (the `with` block has
    exited) before os.replace runs -- required on Windows, where
    replacing a file that's still open anywhere fails with
    PermissionError (Phase C finding).

    See the module docstring's DURABILITY section for exactly what this
    does and does not guarantee -- atomic publication and safety against
    a normal process crash, yes; a proven guarantee against sudden power
    loss, no.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        os.replace(str(tmp_path), str(path))

        if os.name != "nt":
            # Best-effort only -- see module docstring. There is no
            # equivalent call on Windows; NTFS's own metadata journal is
            # what covers rename durability there instead.
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(str(tmp_path))
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


# ---------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------


def write_batch(
    spool_root: str | Path,
    source_name: str,
    records: list[dict[str, Any]],
    *,
    source_generation: int,
    start_offset: int,
    end_offset: int,
) -> Path:
    """Durably writes `records` (one JSON object per line, NDJSON) as a
    new pending batch for `source_name`, tagged with the exact source
    generation and byte-offset range it was read from, and returns the
    final installed path.

    This is the entire contract that makes "impossible to treat a batch
    as durable before it's installed" true: there is no partial-success
    return value and no path handed back before the atomic install
    completes. Either this returns a Path the caller can trust is fully,
    durably on disk under pending/<source_name>/, or it raises and
    nothing new is visible there at all. Callers (Phase E/F
    orchestration) are expected to advance their cursor/state ONLY after
    this call returns successfully, never before and never concurrently
    -- this function itself has no involvement with, or knowledge of, any
    cursor.

    source_generation must be the value agent.state.SourceState.generation
    would hold for this read (i.e. whatever agent.state.advance_generation
    computed for the SAME tailer read that produced start_offset/
    end_offset) -- this module does not compute or infer it, it only
    threads it into the batch's identity and filename so ordering stays
    correct across rotation/truncation (see module docstring's ORDERING
    section). start_offset/end_offset must describe real forward progress
    (end_offset > start_offset) within that generation. Refuses to write a
    zero-record batch -- there is nothing durable to protect in an empty
    batch.
    """
    if not records:
        raise ValueError("write_batch requires at least one record; refusing to write an empty batch")
    if source_generation < 0:
        raise ValueError(f"source_generation must be non-negative, got {source_generation}")
    if end_offset <= start_offset:
        raise ValueError(f"end_offset ({end_offset}) must be greater than start_offset ({start_offset})")
    # A real-machine bug (see agent/tailer.py and agent/state.py's OFFSET
    # REPRESENTATION sections) once produced a 52-digit "offset" that
    # wasn't a real byte count at all -- a value that size would silently
    # overflow _OFFSET_WIDTH's fixed-width zero-padding below and corrupt
    # this module's generation-then-offset lexicographic sort ordering
    # (see the module docstring's ORDERING section) without raising
    # anywhere. Reject it here, at the point it would be encoded into a
    # filename, rather than only upstream in agent/state.py -- this
    # module has its own callers/tests independent of state.py.
    _max_offset = 10**_OFFSET_WIDTH - 1
    if start_offset > _max_offset or end_offset > _max_offset:
        raise ValueError(
            f"start_offset/end_offset ({start_offset}, {end_offset}) exceed the maximum value "
            f"representable in the fixed-width batch filename ({_max_offset}) -- not a real byte offset"
        )

    final_path = _pending_dir(spool_root, source_name) / _new_batch_filename(
        source_generation, start_offset, end_offset
    )
    payload = "\n".join(json.dumps(record, default=str) for record in records) + "\n"
    _atomic_write_text(final_path, payload)

    return final_path


# ---------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------


def list_pending_batches(spool_root: str | Path, source_name: str) -> list[Path]:
    """Oldest-first by (generation, offset) (see module docstring's
    ORDERING section for why, rather than wall-clock time), deterministic
    -- a plain sorted
    directory listing, since filenames already sort correctly by
    construction. Safe to call at any time, including immediately after a
    restart: pending batches are just files that already exist, there is
    no separate index to rebuild or that could disagree with what's
    actually on disk."""
    pending_dir = _pending_dir(spool_root, source_name)
    if not pending_dir.exists():
        return []

    return sorted(p for p in pending_dir.iterdir() if p.is_file() and p.suffix == ".ndjson")


# ---------------------------------------------------------------------
# Reading / corruption handling
# ---------------------------------------------------------------------


def read_batch(path: str | Path) -> list[dict[str, Any]]:
    """Reads and parses one batch file. Opens, reads fully, and closes
    before returning -- never holds the handle open, so a caller is free
    to immediately acknowledge (delete) or quarantine (rename) the same
    path right after this returns without racing an open handle.

    Raises CorruptBatchError -- never silently returns a partial or
    best-effort result -- if the file can't be read, contains a line that
    isn't valid JSON, or a line that parses but isn't a JSON object.
    Blank lines are tolerated and skipped (a defensive allowance for a
    stray trailing newline, not a sign of corruption).
    """
    p = Path(path)

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptBatchError(f"{p.name}: could not be read: {exc}") from exc

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorruptBatchError(f"{p.name}: line {line_number} is not valid JSON: {exc}") from exc

        if not isinstance(record, dict):
            raise CorruptBatchError(
                f"{p.name}: line {line_number} is not a JSON object (got {type(record).__name__})"
            )

        records.append(record)

    return records


def read_batch_or_quarantine(
    spool_root: str | Path, source_name: str, path: str | Path
) -> list[dict[str, Any]] | None:
    """Convenience wrapper: returns the parsed records, or -- if and only
    if the batch is genuinely corrupt -- quarantines it (never deletes)
    and returns None. This is the one function that turns "malformed
    spool file" into an automatic, but never silent, quarantine: the
    batch's content survives intact in quarantine/, with a reason sidecar
    explaining exactly why, for later inspection.

    This is unrelated to delivery failure classification below -- a
    batch that fails to upload for any reason (retryable, auth, or
    permanent) is never touched by this function; it only ever fires for
    content that cannot be safely parsed at all.
    """
    try:
        return read_batch(path)
    except CorruptBatchError as exc:
        quarantine_batch(spool_root, source_name, path, reason=str(exc))
        return None


# ---------------------------------------------------------------------
# Acknowledgment
# ---------------------------------------------------------------------


def acknowledge(path: str | Path) -> None:
    """Deletes a batch (and its attempts sidecar, if any) after a
    successful, confirmed upload. Idempotent -- acknowledging an
    already-gone batch (e.g. a duplicate/retried ack) is a safe no-op,
    never an error, since the durable outcome ("this batch is gone
    because it was delivered") is identical either way."""
    p = Path(path)
    with contextlib.suppress(FileNotFoundError):
        os.remove(str(p))
    with contextlib.suppress(FileNotFoundError):
        os.remove(str(_attempts_sidecar_path(p)))


# ---------------------------------------------------------------------
# Retry tracking (Phase D correction #1: NEVER quarantines by itself)
# ---------------------------------------------------------------------


def _load_attempts(path: Path) -> AttemptRecord:
    sidecar = _attempts_sidecar_path(path)
    if not sidecar.exists():
        return AttemptRecord(attempts=0, category=None, last_error=None, last_attempt_at=None)

    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        return AttemptRecord(
            attempts=int(raw.get("attempts", 0)),
            category=raw.get("category"),
            last_error=raw.get("last_error"),
            last_attempt_at=raw.get("last_attempt_at"),
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        # A corrupt attempts sidecar must never block retrying the batch
        # itself, and must never be treated as a reason to quarantine the
        # batch -- the batch's own content is still perfectly intact and
        # durable, which is what actually matters. Worst case here is the
        # attempt counter under-counts by resetting to 0, which has no
        # quarantine consequence at all now (see record_attempt_failure).
        return AttemptRecord(attempts=0, category=None, last_error=None, last_attempt_at=None)


def _save_attempts(path: Path, record: AttemptRecord) -> None:
    payload = json.dumps(
        {
            "attempts": record.attempts,
            "category": record.category,
            "last_error": record.last_error,
            "last_attempt_at": record.last_attempt_at,
        },
        indent=2,
    )
    _atomic_write_text(_attempts_sidecar_path(path), payload)


def _sanitize_error(error: str) -> str:
    text = str(error)
    max_length = 500
    if len(text) <= max_length:
        return text
    return text[:max_length] + "...<truncated>"


def get_attempt_record(path: str | Path) -> AttemptRecord:
    """Public read accessor for a batch's attempt sidecar -- returns a
    zeroed AttemptRecord (attempts=0, category=None, ...) if no attempt
    has ever been recorded, exactly like a fresh file. Added for Phase F's
    heartbeat, which needs to summarize failure state across all pending
    batches without reaching into this module's private _load_attempts."""
    return _load_attempts(Path(path))


def record_attempt_failure(path: str | Path, error: str, category: FailureCategory) -> AttemptRecord:
    """Durably records one failed delivery attempt for `path` -- purely
    diagnostic and backoff-supporting bookkeeping. The batch file is left
    completely untouched in pending/ regardless of category or how many
    attempts have accumulated.

    THIS FUNCTION NEVER QUARANTINES. That is the Phase D correction: an
    earlier version of this module quarantined a batch once a fixed
    attempt count was reached, with no regard for *why* delivery kept
    failing -- which would have silently discarded perfectly valid,
    still-undelivered event data during nothing worse than a network
    outage or a temporarily wrong token. Sustained RETRYABLE_INFRA or
    AUTH_FAILURE is a health/monitoring concern (surfaced by a later
    phase's heartbeat reading get_spool_stats/this sidecar's
    last_attempt_at), never a reason for this module to make the batch
    disappear on its own. Backoff scheduling itself (bounded exponential
    + jitter) is the future uploader loop's responsibility, not this
    module's -- what's recorded here (attempts, category, timestamps) is
    exactly what such a loop needs to compute it.

    Persists across restart trivially: the sidecar is just another file
    on disk, read back by _load_attempts the same way regardless of
    whether the process restarted in between attempts.
    """
    prior = _load_attempts(Path(path))
    updated = AttemptRecord(
        attempts=prior.attempts + 1,
        category=category.value,
        last_error=_sanitize_error(error),
        last_attempt_at=_now_iso(),
    )
    _save_attempts(Path(path), updated)
    return updated


def quarantine_batch(spool_root: str | Path, source_name: str, path: str | Path, reason: str) -> Path:
    """Moves a batch into quarantine/<source_name>/, verbatim, alongside
    a reason sidecar explaining why and when. Never deletes -- os.replace
    is a move, not a copy-then-delete, and if a same-named file somehow
    already exists in quarantine (astronomically unlikely given the
    filename scheme, but never assumed away), the incoming file is given
    a disambiguated name instead of silently overwriting whatever's
    already there.

    Only ever called for two reasons in this codebase today:
    read_batch_or_quarantine (content is genuinely unreadable/malformed),
    or a future caller that has independently proven -- via isolation
    logic this module does not implement -- that this exact, already-
    smallest-practical-unit batch reproduces a permanent, deterministic
    backend rejection on its own. Never called merely because delivery
    attempts have accumulated; see record_attempt_failure.
    """
    p = Path(path)
    quarantine_dir = _quarantine_dir(spool_root, source_name)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    dest = quarantine_dir / p.name
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}-dup-{uuid.uuid4().hex[:8]}{dest.suffix}")

    os.replace(str(p), str(dest))

    _atomic_write_text(
        _reason_sidecar_path(dest),
        json.dumps({"reason": reason, "quarantined_at": _now_iso()}, indent=2),
    )

    sidecar = _attempts_sidecar_path(p)
    if sidecar.exists():
        with contextlib.suppress(OSError):
            os.replace(str(sidecar), str(quarantine_dir / sidecar.name))

    return dest


def quarantine_records(
    spool_root: str | Path, source_name: str, records: list[dict[str, Any]], reason: str
) -> Path:
    """Writes `records` directly into quarantine/<source_name>/ as a new
    NDJSON file, alongside a reason sidecar -- WITHOUT those records ever
    having existed as their own pending/ batch file first.

    This is the primitive Phase F's poison-event isolation needs:
    isolation determines that some SUBSET of an existing pending batch's
    records is the poison cause, while the rest of that batch's records
    are delivered separately as sub-uploads. There is no clean way to
    "partially quarantine" the original file (it may contain healthy
    siblings), so the poison subset is written straight to quarantine as
    its own new evidence file, and the caller acknowledges (deletes) the
    original pending file only once every one of its records has been
    definitively resolved -- delivered or quarantined here. See
    agent/runtime/uploader.py for the caller.

    Deliberately does not carry real source-offset metadata in its
    filename (unlike write_batch/quarantine_batch) -- a quarantined
    record is terminal, never re-enumerated by list_pending_batches, and
    ordering/generation no longer apply to it. Refuses an empty list, for
    the same reason write_batch refuses an empty batch: there is nothing
    to quarantine.
    """
    if not records:
        raise ValueError("quarantine_records requires at least one record; refusing an empty quarantine")

    quarantine_dir = _quarantine_dir(spool_root, source_name)
    ns = time.time_ns()
    seq = next(_sequence_counter)
    dest = quarantine_dir / f"isolated-{ns}-{seq:010d}-{uuid.uuid4().hex[:6]}.ndjson"

    payload = "\n".join(json.dumps(record, default=str) for record in records) + "\n"
    _atomic_write_text(dest, payload)

    _atomic_write_text(
        _reason_sidecar_path(dest),
        json.dumps({"reason": reason, "quarantined_at": _now_iso()}, indent=2),
    )

    return dest


# ---------------------------------------------------------------------
# Diagnostics / heartbeat visibility
# ---------------------------------------------------------------------


def get_spool_stats(spool_root: str | Path, source_name: str) -> SpoolStats:
    """Read-only. Everything a future heartbeat needs to answer "how many
    spool batches are pending" and "how many bytes are pending" without
    any database -- just directory listings and file sizes. Also exactly
    what a later disk-pressure policy (Phase D correction #4, not
    implemented here) would need to act on -- pending_bytes is already
    exposed for that purpose."""
    pending = list_pending_batches(spool_root, source_name)
    pending_bytes = sum(p.stat().st_size for p in pending)
    oldest_metadata = parse_batch_filename(pending[0]) if pending else None

    quarantine_dir = _quarantine_dir(spool_root, source_name)
    quarantined = (
        [p for p in quarantine_dir.iterdir() if p.is_file() and p.suffix == ".ndjson"]
        if quarantine_dir.exists()
        else []
    )
    quarantined_bytes = sum(p.stat().st_size for p in quarantined)

    return SpoolStats(
        pending_batch_count=len(pending),
        pending_bytes=pending_bytes,
        oldest_pending_created_at=oldest_metadata.created_at if oldest_metadata else None,
        quarantined_batch_count=len(quarantined),
        quarantined_bytes=quarantined_bytes,
    )
