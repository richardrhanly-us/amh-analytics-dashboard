"""Local, privacy-safe per-run audit logging (Phase 1).

Durably records, once per collector invocation, ONLY the operational
metadata an operator needs to answer "when did the last run happen, how
long did it take, did it succeed, how far did each source's offset move,
how many records were discovered/uploaded, and -- if it failed -- which
stage and what kind of failure": never patron/item content, never raw
log/exception text, never a filesystem source path, never a credential.
See this module's PRIVACY BOUNDARY section below for the exact contract.

CANONICAL PATH: `C:\\ProgramData\\SortViewCollector\\logs\\runs.jsonl`,
next to the existing `collector.log` -- NOT `C:\\ProgramData\\SortView\\`,
which is the separate, frozen continuous-agent package's own root (see
collector/__init__.py's own docstring on why this package has zero
dependency on that one). collector/config.py's `run_audit_path` defaults
to exactly this: `<log_path's own directory>\\runs.jsonl`, so an
already-deployed config with no such key gets this automatically, with no
config edit required.

PRE-CONFIG FALLBACK. A run can fail before a CollectorConfig even exists
(a malformed config file, a missing API token, an unreadable config path)
-- collector/run.py's main() has no `cfg.run_audit_path` to write to in
that case. DEFAULT_FALLBACK_AUDIT_PATH is a second, independently known
path for exactly that situation, so a config-load failure is never
"invisible" to the audit trail. It intentionally resolves to the SAME
real directory a normal install already writes to
(`C:\\ProgramData\\SortViewCollector\\logs\\`), so on a real deployed
machine the fallback and the normal path typically converge on the exact
same file; it is a plain module attribute (not computed inside a function)
specifically so tests can monkeypatch it to a throwaway path and never
touch a real machine's ProgramData tree -- see tests/conftest.py's
autouse `_collector_run_audit_fallback_path` fixture, which does exactly
that for every test in this suite.

DURABILITY. Appends are a single JSON line, `flush()` + `os.fsync()`,
matching the same "never leave a torn or missing write" discipline
collector/state.py's atomic_write_json already established for
state.json/status.json -- reimplemented independently here (append, not
replace, is the right primitive for a growing history file; state.py's
own primitive is a full-file replace and isn't reusable as-is). A torn
trailing line (process killed mid-write) is tolerated by construction: a
reader that can't json.loads a line simply discards that one line, never
the whole file (see _iter_valid_records).

CONCURRENCY. Task Scheduler's MultipleInstancesPolicy=IgnoreNew (see
collector/task_settings.py) already prevents two SCHEDULED runs from
overlapping, but says nothing about an operator manually launching the
collector by hand while a scheduled run is mid-flight. Rather than trust
"a short append is probably atomic enough," every append and every prune
takes a real, short-lived OS-level file lock (standard library only, no
third-party dependency) on a small sidecar `.lock` file next to the
JSONL file itself -- never on the JSONL file's own handle, so ordinary
buffered reads of runs.jsonl are never blocked by it. `msvcrt.locking` on
Windows (the real, only shipped production target -- see
collector/__init__.py); `fcntl.flock` on POSIX, so this module's own
test suite exercises genuine inter-process/inter-thread mutual exclusion
on Linux CI too, not a no-op stand-in -- see _file_lock's own docstring
for the exact shape both share.

RETENTION. 30 days, enforced as part of every append (not a separate
scheduled job or subsystem): after appending, the file is re-read, any
line older than the retention window OR that fails to parse is dropped,
and -- ONLY if something was actually dropped -- the file is rewritten
via temp-file + fsync + os.replace (the same atomic-rewrite discipline as
collector/state.py::atomic_write_json, applied to a list of lines instead
of one JSON document). On an ordinary run (nothing yet expired) this is a
cheap read-and-compare with no rewrite at all. A single runs.jsonl file
was kept over daily-rotated files deliberately: at one record per ~15
minutes, 30 days is at most ~2880 lines (some tens of KB) -- reading and
rewriting that whole file is trivial cost, so a second axis of complexity
(which day's file to read for "the last run," cleaning up empty old daily
files, redoing the lock scope per-file) would add real complexity for no
practical benefit at this volume. Revisit only if the record shape or
cadence changes enough to make that cost non-trivial.

FAILURE ISOLATION -- the one constraint everything else here is
subordinate to: audit logging must NEVER change the collector's own exit
code or outcome. append_run_record catches and swallows every exception
itself (OSError, a lock timeout, a permissions failure, disk full) and
returns a bool the caller MAY log but must never act on otherwise.

PRIVACY BOUNDARY -- fields that are explicitly NEVER written here, even
though some would be easy to add: barcode, title, any patron identifier,
raw source lines, raw ACS messages, destination values or breakdowns,
reject/error-message breakdowns, raw HTTP response bodies, str(exc) or
any traceback, API tokens, Authorization headers, credentials,
DATABASE_URL, or any filesystem source path. Only aggregate counts,
offsets (plain integers), source NAMES (e.g. "checkins", never a path),
HTTP status codes, and a small fixed failure-code enum are ever written.
See tests/test_collector_run_audit.py's privacy-canary test, which
fails the build if any of the above ever appears in a built record.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

# Deliberately not a try/except ImportError. mypy special-cases a literal
# `sys.platform == "win32"` comparison: on whichever platform mypy itself
# runs on, the OTHER branch is treated as unreachable and is not
# type-checked at all, so typeshed's platform-gated attributes
# (msvcrt.locking/LK_NBLCK/LK_UNLCK only exist in the stub under win32;
# fcntl only exists under POSIX) never produce a "module has no
# attribute" error on either platform. _acquire/_release below repeat
# this same `if sys.platform == "win32":` check inline around each
# platform's calls (not split into separate single-platform functions --
# mypy's narrowing does not follow a call from one function into
# another's body) -- see the module docstring's CONCURRENCY section for
# why both branches are real locking, not a fallback pair.
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# See module docstring's CANONICAL PATH / PRE-CONFIG FALLBACK sections. A
# plain module attribute, not a function's return value, so tests can
# monkeypatch it directly (see tests/conftest.py).
DEFAULT_FALLBACK_AUDIT_PATH = Path(r"C:\ProgramData\SortViewCollector\logs\runs.jsonl")

RETENTION_DAYS = 30

_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_INTERVAL_SECONDS = 0.05
_LOCK_REGION_SIZE = 1  # msvcrt.locking locks a byte range; one byte is enough for a whole-file advisory lock


class ErrorCode(str, Enum):
    """A small, fixed, privacy-safe failure taxonomy -- never a raw
    exception message or response body. The three upload-related members
    deliberately share their exact string values with
    collector.uploader.FailureCategory (retryable_infra/auth_failure/
    permanent_rejection) -- that enum is already the safe, existing
    classification for an upload failure; this is not a second,
    competing taxonomy, just the same one reused for this local record."""

    CONFIG_INVALID = "config_invalid"
    PARSER_NOT_CONFIGURED = "parser_not_configured"
    STATE_CORRUPT = "state_corrupt"
    RETRYABLE_INFRA = "retryable_infra"
    AUTH_FAILURE = "auth_failure"
    PERMANENT_REJECTION = "permanent_rejection"
    UNHANDLED_EXCEPTION = "unhandled_exception"


@dataclass(frozen=True)
class SourceAudit:
    """The four Phase 1 per-source fields -- nothing else. offset_before/
    offset_after are the same true byte offsets collector/state.py
    already persists; new_records/uploaded are the same counts already
    written into status.json (checkins_rows/uploaded_checkins_rows etc.)."""

    offset_before: int | None
    offset_after: int | None
    new_records: int | None
    uploaded: int | None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "offset_before": self.offset_before,
            "offset_after": self.offset_after,
            "new_records": self.new_records,
            "uploaded": self.uploaded,
        }


def new_run_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    """Same ISO-8601-with-Z shape as collector/run.py's own _now_iso --
    reimplemented locally rather than imported, matching this package's
    established per-module independence (see collector/state.py, reader.py,
    uploader.py, run.py, each of which already defines its own copy)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_started_at(value: Any) -> datetime | None:
    """None (not a raised error) for anything unparseable -- this is the
    single place both a torn trailing line AND a structurally-valid-but-
    nonsensical record collapse to "drop it during retention", never a
    crash. Accepts exactly the shape now_iso() produces; anything else is
    treated the same as unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def build_record(
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    result: str,
    collector_version: str,
    sources: dict[str, SourceAudit] | None = None,
    sources_missing: list[str] | None = None,
    sources_rotated: list[str] | None = None,
    sources_truncated: list[str] | None = None,
    upload_status: int | None = None,
    pipeline_status: int | None = None,
    failure_stage: str | None = None,
    error_code: ErrorCode | str | None = None,
) -> dict[str, Any]:
    """Assembles the exact Phase 1 record shape -- a pure function, no
    I/O. Every field here is either a plain scalar the caller already
    computed (a count, an offset, an HTTP status code, a fixed error
    code) or a short list of source NAMES -- never source content. See
    module docstring's PRIVACY BOUNDARY."""
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "result": result,
        "collector_version": collector_version,
        "sources": {name: audit.to_dict() for name, audit in (sources or {}).items()},
        "sources_missing": list(sources_missing or []),
        "sources_rotated": list(sources_rotated or []),
        "sources_truncated": list(sources_truncated or []),
        "http": {
            "upload_status": upload_status,
            "pipeline_status": pipeline_status,
        },
        "failure_stage": failure_stage,
        "error_code": error_code.value if isinstance(error_code, ErrorCode) else error_code,
    }


# ---------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------


def _acquire(fd: int, deadline: float) -> None:
    """Blocks (via bounded non-blocking retry) until the advisory lock on
    `fd` is held, or raises OSError once `deadline` (a time.monotonic()
    value) passes. The msvcrt/fcntl calls are inlined here, each inside
    its own literal `if sys.platform == "win32":` check, rather than
    split into separate helper functions -- mypy's platform-unreachability
    narrowing for a `sys.platform` comparison only applies to the code
    lexically inside that same if/else, not to a function called from
    within it, so keeping both branches in one function body (instead of
    two single-platform functions) is what actually keeps this file
    mypy-clean on both Windows and Linux without a `type: ignore`."""
    while True:
        try:
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_REGION_SIZE)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_LOCK_POLL_INTERVAL_SECONDS)


def _release(fd: int) -> None:
    """Best-effort unlock -- see _acquire's docstring for why both
    platforms' calls are inlined in one function rather than split."""
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_REGION_SIZE)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def _file_lock(lock_path: Path, *, timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """A short-lived, real OS-level advisory lock on `lock_path` (a small
    sidecar file, never runs.jsonl itself) -- genuine inter-process (and
    inter-thread) mutual exclusion on BOTH platforms this collector runs
    or is tested on: `msvcrt.locking` on Windows (the real, only shipped
    production target -- see collector/__init__.py), `fcntl.flock` on
    POSIX (so Linux CI/dev exercises real locking semantics too, not a
    no-op stand-in). Verified on each platform by this module's own
    two-thread contention test. Both use the identical bounded-retry
    shape: a non-blocking acquire attempt, retried every
    _LOCK_POLL_INTERVAL_SECONDS until `timeout` elapses, then the
    underlying OSError is re-raised -- callers (append_run_record) catch
    this like any other audit failure and never let it affect the
    collector's own exit code.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + timeout
    try:
        _acquire(fd, deadline)
        try:
            yield
        finally:
            _release(fd)
    finally:
        os.close(fd)


def _lock_path_for(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _needs_leading_separator(path: Path) -> bool:
    """True only when the file exists, is non-empty, and its last byte is
    NOT a newline -- i.e. a genuinely torn trailing line from an earlier
    crash. False (no separator needed) for a missing file, an empty file,
    or -- the normal case after any prior successful append -- a file
    that already ends cleanly with '\\n'."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size == 0:
        return False
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        last_byte = f.read(1)
    return last_byte != b"\n"


# ---------------------------------------------------------------------
# Append + retention (both run under the same lock)
# ---------------------------------------------------------------------


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Reads back the run-history file, oldest first. A torn trailing
    line, or any other line that fails to parse, is silently skipped --
    never raised, never logged with its own (potentially-truncated,
    unpredictable) content. Not used by append_run_record itself
    (_prune_locked reads and validates lines directly, in one pass, for
    its own reasons) -- this is the read-side counterpart for a caller
    (a future support-info view, or a test) that wants the parsed
    records back."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _prune_locked(path: Path, *, retention_days: int, now: datetime) -> None:
    """Must be called while already holding _file_lock(path). Rewrites
    the file ONLY if something is actually being dropped (an expired or
    unparseable line) -- an ordinary append where nothing has expired
    yet costs nothing beyond one read-and-compare pass. Uses the same
    temp-file + fsync + os.replace discipline as
    collector/state.py::atomic_write_json."""
    try:
        raw_lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return

    cutoff = now - timedelta(days=retention_days)
    kept_lines: list[str] = []
    dropped = 0

    for line in raw_lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            dropped += 1  # torn/corrupt line -- dropped, never resurfaced
            continue
        if not isinstance(record, dict):
            dropped += 1
            continue
        started_at = _parse_started_at(record.get("started_at"))
        if started_at is None or started_at < cutoff:
            dropped += 1
            continue
        kept_lines.append(json.dumps(record, separators=(",", ":")))

    if dropped <= 0:
        return

    _atomic_rewrite_lines(path, kept_lines)


def _atomic_rewrite_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(str(tmp_path))
        raise


def append_run_record(
    path: Path,
    record: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
    retention_days: int = RETENTION_DAYS,
) -> bool:
    """Appends one record, then prunes anything past retention -- both
    under the same lock. Returns True on success, False on any failure.
    NEVER raises: every exception is caught here so a logging/disk/
    permissions problem can never change the collector's own exit code
    or outcome (see module docstring's FAILURE ISOLATION)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(_lock_path_for(path)):
            line = json.dumps(record, separators=(",", ":"))
            # If an earlier process was killed mid-write, the file can end
            # with a torn line that has no trailing newline yet --
            # appending directly after it in "a" mode would silently MERGE
            # this brand-new, otherwise-perfectly-valid record onto the
            # end of that garbage line, corrupting both. Checked BEFORE
            # opening in append mode, since tell() right after opening
            # only reports "non-empty", not "properly newline-terminated".
            needs_separator = _needs_leading_separator(path)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                if needs_separator:
                    f.write("\n")
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())

            _prune_locked(path, retention_days=retention_days, now=datetime.now(UTC))
        return True
    except Exception as exc:  # deliberately broad -- see module docstring's FAILURE ISOLATION
        if logger is not None:
            # Aggregate/category only -- never str(exc)'s own message,
            # which could in principle echo back a path or other detail
            # this module exists to keep out of a durable file. A bare
            # exception TYPE name is safe (e.g. "PermissionError").
            logger.warning("Run-audit logging failed (collector run itself is unaffected): %s", type(exc).__name__)
        return False
