"""Durable local store for the SortView agent's continuous ingestion
pipeline (Phase 1: capture only -- nothing in this module makes a network
call).

Two tables:

  local_events
      The outbox. Every parsed event lands here immediately, keyed for
      dedup by a hash of its raw source line (see compute_dedup_key) so
      re-reading the same physical line twice -- an offset-resumption
      race, a restart mid-batch -- can never insert it twice.
      uploaded_at stays NULL until a future uploader (not built yet)
      marks a row delivered.

  file_state
      Durable byte-offset and file-identity tracking per watched file, so
      a restart resumes exactly where it left off instead of reparsing,
      and a rotated or truncated file is detected safely. File identity
      is (file_dev, file_ino) from os.stat() -- verified on Windows/NTFS
      to be stable across appends and to actually change across a
      delete-and-recreate at the same path, unlike file creation time
      (NTFS "tunneling" can silently preserve the original creation
      timestamp across a fast delete+recreate).

No module in this file imports anything from the agent's own config or
logger modules, and it has no third-party dependency beyond the stdlib --
it's usable and testable on its own, with any SQLite file path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("data") / "sortview_agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type        TEXT NOT NULL CHECK (event_type IN ('checkin', 'reject', 'acs')),
    customer_id       INTEGER NOT NULL,
    branch_id         INTEGER NOT NULL,
    event_timestamp   TEXT,
    dedup_key         TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    uploaded_at       TEXT,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   TEXT,
    last_error        TEXT,
    quarantined_at    TEXT,
    quarantine_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_local_events_dedup
    ON local_events(event_type, dedup_key);

CREATE INDEX IF NOT EXISTS idx_local_events_pending
    ON local_events(uploaded_at)
    WHERE uploaded_at IS NULL;

CREATE TABLE IF NOT EXISTS file_state (
    file_path        TEXT PRIMARY KEY,
    file_dev         INTEGER NOT NULL,
    file_ino         INTEGER NOT NULL,
    last_byte_offset INTEGER NOT NULL DEFAULT 0,
    last_modified    TEXT,
    updated_at       TEXT NOT NULL
);
"""


# Columns added after the initial Phase 1 schema. CREATE TABLE IF NOT
# EXISTS (in SCHEMA above) only helps a brand-new database -- an existing
# local_events table from an earlier phase needs these added explicitly.
# Each entry is (column_name, column_ddl_suffix); ADD COLUMN has no
# IF NOT EXISTS in SQLite, so _ensure_columns checks PRAGMA table_info
# first and only adds what's actually missing, making this safe to run
# against a fresh database (nothing to add) or a pre-existing one alike.
_EVOLVED_COLUMNS = [
    ("quarantined_at", "TEXT"),
    ("quarantine_reason", "TEXT"),
    ("last_error_category", "TEXT"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(local_events)").fetchall()}

    for column_name, column_ddl in _EVOLVED_COLUMNS:
        if column_name not in existing:
            conn.execute(f"ALTER TABLE local_events ADD COLUMN {column_name} {column_ddl}")


def connect(
    db_path: str | Path = DEFAULT_DB_PATH, *, busy_timeout_seconds: float = 30
) -> sqlite3.Connection:
    """Open (creating if needed) the outbox database with WAL mode enabled
    and the schema applied. Safe to call every time the agent starts --
    every statement in SCHEMA is idempotent (CREATE ... IF NOT EXISTS),
    and _ensure_columns only adds columns that are actually missing.

    Phase 5 / auto_vacuum: `PRAGMA auto_vacuum=INCREMENTAL` only takes
    effect when issued before any table exists in a brand-new database
    file -- confirmed empirically, including that the *order* matters
    (it must also come before `journal_mode=WAL` is set). Issuing it
    against an existing, already-populated database (every agent DB that
    has run through Phase 1-4 today) is a silent no-op: `PRAGMA
    auto_vacuum` keeps reporting 'none' afterward, and `PRAGMA
    incremental_vacuum` then does nothing. There is no way to retroactively
    enable it without a full VACUUM (a full file rebuild), which Phase 5
    deliberately does not do -- see agent/maintenance.py and the Phase 5
    report for what existing databases get instead (freelist page reuse
    + periodic WAL checkpointing).

    is_new_database is determined by the file's existence *before* this
    call creates it -- sqlite3.connect() itself creates an empty file, so
    checking afterward would always say "new".

    busy_timeout_seconds: each component (watcher/uploader/heartbeat/
    maintenance) opens its own connection via this function, so this can
    differ per component. Confirmed empirically (Phase 5 investigation):
    `PRAGMA wal_checkpoint` genuinely blocks the calling thread for up to
    this connection's own busy_timeout when another connection holds
    conflicting WAL frames open (e.g. a long read transaction) -- it does
    NOT return immediately with a "busy" status the way it might appear
    to from the result tuple alone. agent/maintenance.py deliberately
    uses a much shorter timeout than the default 30s so a checkpoint
    contending with something else blocks for at most a few seconds, not
    thirty, before giving up and simply retrying on the next cycle.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new_database = not db_path.exists()

    # isolation_level=None puts the connection in autocommit mode, so
    # transaction() below has full, explicit control over BEGIN/COMMIT/
    # ROLLBACK instead of relying on sqlite3's implicit transaction
    # handling (which opens a transaction before the first DML statement
    # and can make it non-obvious exactly what's inside one).
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=busy_timeout_seconds)
    conn.row_factory = sqlite3.Row

    if is_new_database:
        # Must come before journal_mode=WAL and before any table is
        # created -- see the docstring above.
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript(SCHEMA)
    _ensure_columns(conn)

    return conn


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_dedup_key(fields: dict[str, Any]) -> str:
    """Stable hash of the source-level fields that identify one parsed
    event, independent of any derived/computed columns (a normalized
    destination, a transit flag, and so on).

    Deliberately keyed on source fields rather than on something like
    (barcode, event_time) alone: those can legitimately collide (two
    events with an unparseable/missing timestamp would hash the same if
    event_time were part of the key), whereas the same source line
    reappearing -- an offset-resumption race, a restart mid-batch -- is
    precisely the failure mode this guards against, and it reparses to
    the same field values every time.
    """
    canonical = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Wrap a batch of event inserts plus the file_state offset update in
    one atomic transaction. If anything in the block raises, the whole
    batch rolls back -- so a crash or error partway through a batch can
    never advance the stored offset past events that were never actually
    committed to local_events."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def insert_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    customer_id: int,
    branch_id: int,
    event_timestamp: str | None,
    dedup_key: str,
    payload: dict[str, Any],
) -> bool:
    """Insert one parsed event into the outbox.

    Returns True if a new row was actually inserted, False if a row with
    the same (event_type, dedup_key) already existed and this insert was
    a no-op. Uses SELECT changes() rather than the DB-API cursor's own
    rowcount, since rowcount's behavior with `ON CONFLICT ... DO NOTHING`
    is not something to trust blindly across sqlite3 versions -- changes()
    is SQLite's own authoritative count for the connection's last write.
    """
    conn.execute(
        """
        INSERT INTO local_events (
            event_type, customer_id, branch_id, event_timestamp,
            dedup_key, payload_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (event_type, dedup_key) DO NOTHING
        """,
        (
            event_type,
            customer_id,
            branch_id,
            event_timestamp,
            dedup_key,
            json.dumps(payload),
            _now_iso(),
        ),
    )
    (changed,) = conn.execute("SELECT changes()").fetchone()
    return bool(changed)


def get_file_state(conn: sqlite3.Connection, file_path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM file_state WHERE file_path = ?",
        (file_path,),
    ).fetchone()


def save_file_state(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    file_dev: int,
    file_ino: int,
    last_byte_offset: int,
    last_modified: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO file_state (
            file_path, file_dev, file_ino, last_byte_offset,
            last_modified, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (file_path) DO UPDATE SET
            file_dev = excluded.file_dev,
            file_ino = excluded.file_ino,
            last_byte_offset = excluded.last_byte_offset,
            last_modified = excluded.last_modified,
            updated_at = excluded.updated_at
        """,
        (file_path, file_dev, file_ino, last_byte_offset, last_modified, _now_iso()),
    )


def count_pending_events(conn: sqlite3.Connection) -> int:
    """Rows that are still eligible for automatic delivery -- not yet
    uploaded and not quarantined. A quarantined row is deliberately
    parked (see quarantine_rows below); calling it "pending" would be
    misleading since nothing will attempt it again without a human
    or a future recovery mechanism.
    """
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM local_events WHERE uploaded_at IS NULL AND quarantined_at IS NULL"
    ).fetchone()
    return int(count)


def count_quarantined_events(conn: sqlite3.Connection) -> int:
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM local_events WHERE quarantined_at IS NOT NULL"
    ).fetchone()
    return int(count)


def get_oldest_pending_created_at(conn: sqlite3.Connection) -> str | None:
    """created_at (outbox entry time) of the longest-waiting pending row,
    or None if nothing is pending. Uses created_at rather than
    event_timestamp deliberately: this answers "how long has something
    been stuck in the queue", which is what a backlog-age health check
    needs -- event_timestamp is the AMH log's own (sometimes missing or
    unparseable) timestamp, not the outbox's."""
    (value,) = conn.execute(
        "SELECT MIN(created_at) FROM local_events "
        "WHERE uploaded_at IS NULL AND quarantined_at IS NULL"
    ).fetchone()
    return value


def get_last_success_at(conn: sqlite3.Connection) -> str | None:
    """Most recent uploaded_at across all rows ever delivered, or None if
    nothing has ever been delivered. Fully durable and restart-safe --
    uploaded_at is written transactionally at delivery time (see
    outbox_uploader._mark_rows_delivered), so this needs no separate
    tracking mechanism."""
    (value,) = conn.execute("SELECT MAX(uploaded_at) FROM local_events").fetchone()
    return value


def get_latest_unresolved_failure(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent delivery attempt among rows that are still pending
    (not delivered, not quarantined) -- i.e. a failure that hasn't since
    been superseded by a success. Once the rows behind a given failure are
    either delivered or quarantined, they drop out of this query entirely,
    so a recovered uploader naturally stops reporting a stale failure
    without needing any separate "clear" step: the next heartbeat simply
    finds nothing here and reports last_failure_category/last_error as
    None again.

    Only rows with a recorded last_attempt_at are considered (a
    never-attempted fresh row has neither), and only the single most
    recent attempt is returned -- the caller cares about current state,
    not history.
    """
    return conn.execute(
        "SELECT last_error_category, last_error, last_attempt_at FROM local_events "
        "WHERE uploaded_at IS NULL AND quarantined_at IS NULL AND last_attempt_at IS NOT NULL "
        "ORDER BY last_attempt_at DESC LIMIT 1"
    ).fetchone()


def get_watcher_last_active_at(conn: sqlite3.Connection) -> str | None:
    """Most recent file_state.updated_at across all watched files.
    save_file_state is called on every poll cycle the watcher makes for a
    file that exists, whether or not any new lines were found, so this is
    already a durable "the watcher loop is still ticking" signal with no
    new column or write needed -- it's a liveness signal, not an activity
    count: an idle branch with zero new AMH events still refreshes this
    every poll."""
    (value,) = conn.execute("SELECT MAX(updated_at) FROM file_state").fetchone()
    return value


# ---------------------------------------------------------------------
# Phase 5: local database maintenance (retention/pruning, WAL
# checkpointing, space diagnostics). Everything below is either a plain
# read-only PRAGMA/COUNT helper or a single bounded DELETE -- the actual
# scheduling, batching-across-cycles, and result reporting live in
# agent/maintenance.py, which is the only module that imports a logger
# and drives these in a loop. Kept here, not there, because these are
# the same kind of small, dependency-free, directly-testable primitives
# as the rest of this file.
# ---------------------------------------------------------------------


_AUTO_VACUUM_MODES = {0: "none", 1: "full", 2: "incremental"}


def get_auto_vacuum_mode(conn: sqlite3.Connection) -> str:
    """'none' | 'full' | 'incremental' | 'unknown'. See the Phase 5
    investigation note on connect() below: this reflects the mode that
    was ACTUALLY locked in when the database file was first created --
    issuing `PRAGMA auto_vacuum=INCREMENTAL` against an existing,
    already-populated database is a silent no-op in SQLite, confirmed
    empirically. Callers (agent/maintenance.py) use this to decide
    whether incremental_vacuum can do anything at all, never assume it
    from config alone."""
    (value,) = conn.execute("PRAGMA auto_vacuum").fetchone()
    return _AUTO_VACUUM_MODES.get(int(value), "unknown")


def get_page_count(conn: sqlite3.Connection) -> int:
    (value,) = conn.execute("PRAGMA page_count").fetchone()
    return int(value)


def get_freelist_count(conn: sqlite3.Connection) -> int:
    """Pages freed by DELETEs that SQLite has already reclaimed onto its
    internal freelist and will reuse for future inserts -- this happens
    regardless of auto_vacuum mode. A nonzero value here on a 'none'-mode
    database is expected and fine; it just won't shrink the file itself
    (see get_auto_vacuum_mode)."""
    (value,) = conn.execute("PRAGMA freelist_count").fetchone()
    return int(value)


def count_delivered_events(conn: sqlite3.Connection) -> int:
    """All rows ever successfully delivered (uploaded_at set), regardless
    of retention age -- the broader diagnostic count. See
    count_eligible_for_prune for the narrower, age-and-quarantine-scoped
    count that pruning actually acts on."""
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM local_events WHERE uploaded_at IS NOT NULL"
    ).fetchone()
    return int(count)


def count_eligible_for_prune(conn: sqlite3.Connection, cutoff: str) -> int:
    """Rows prune_delivered_events is actually allowed to delete: must be
    delivered (uploaded_at set), never quarantined (quarantined_at NULL
    -- defensive; Phase 2 never sets both on one row, but the predicate
    doesn't assume that), and delivered before the retention cutoff."""
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM local_events "
        "WHERE uploaded_at IS NOT NULL AND quarantined_at IS NULL AND uploaded_at < ?",
        (cutoff,),
    ).fetchone()
    return int(count)


def prune_delivered_events(conn: sqlite3.Connection, *, cutoff: str, batch_size: int) -> int:
    """Deletes up to batch_size of the oldest eligible delivered rows
    (see count_eligible_for_prune for the exact predicate) in one bounded
    transaction, oldest uploaded_at first. Returns the number of rows
    actually deleted (0 if nothing was eligible). Never touches pending
    or quarantined rows, or a delivered row newer than cutoff, by
    construction of the WHERE clause -- not by relying on caller
    discipline.

    Bounded and batched deliberately: the caller (agent/maintenance.py)
    calls this repeatedly, once per batch, up to its own per-cycle
    workload cap, so a very large eligible backlog is drained gradually
    across cycles rather than in one unbounded transaction that could
    hold locks against the watcher/uploader for an extended period.
    """
    with transaction(conn):
        conn.execute(
            """
            DELETE FROM local_events WHERE id IN (
                SELECT id FROM local_events
                WHERE uploaded_at IS NOT NULL AND quarantined_at IS NULL AND uploaded_at < ?
                ORDER BY uploaded_at ASC
                LIMIT ?
            )
            """,
            (cutoff, batch_size),
        )
        (deleted,) = conn.execute("SELECT changes()").fetchone()
    return int(deleted)


_WAL_CHECKPOINT_MODES = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")


def checkpoint_wal(conn: sqlite3.Connection, mode: str = "TRUNCATE") -> tuple[int, int, int]:
    """Returns (busy, log_frames, checkpointed_frames) from
    `PRAGMA wal_checkpoint(<mode>)`. Confirmed empirically (Phase 5
    investigation): this never *raises* on a busy reader/writer -- busy=1
    just means the checkpoint couldn't fully complete (e.g. another
    connection has an open read transaction still needing older WAL
    frames), which is a normal, expected condition to be retried on a
    later cycle, not an error.

    It DOES block the calling thread while contended, though, for up to
    this connection's own busy_timeout (see connect()'s
    busy_timeout_seconds) before giving up and returning busy=1 -- also
    confirmed empirically (a 30s-timeout connection blocked for ~32s
    under sustained contention; a 2s-timeout connection blocked for
    ~2.3s). It is not a fire-and-forget, instant call. Callers that care
    about bounding worst-case blocking time should use a connection
    opened with a correspondingly short busy_timeout_seconds --
    agent/maintenance.py does exactly this.

    Only TRUNCATE actually shrinks the -wal file's on-disk size; PASSIVE
    and RESTART checkpoint the WAL's *content* into the main file
    (frames become safely reusable) but leave the file's current size
    unchanged -- also confirmed empirically.

    mode is restricted to the four real SQLite checkpoint modes (not
    caller-controlled free text) since PRAGMA statements don't support
    bound parameters at all -- SQLite rejects `PRAGMA wal_checkpoint(?)`
    outright, confirmed empirically -- so this must be interpolated
    directly into the SQL string.
    """
    if mode not in _WAL_CHECKPOINT_MODES:
        raise ValueError(f"Unsupported WAL checkpoint mode: {mode!r}")

    row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()  # nosec B608 -- mode is restricted to the fixed _WAL_CHECKPOINT_MODES tuple above, never caller-controlled text
    busy, log_frames, checkpointed = row
    return int(busy), int(log_frames), int(checkpointed)


def run_incremental_vacuum(conn: sqlite3.Connection, max_pages: int) -> None:
    """Bounded `PRAGMA incremental_vacuum(N)`. Only meaningful when
    get_auto_vacuum_mode(conn) == "incremental" -- confirmed empirically
    to be a silent no-op otherwise (see connect()'s docstring and the
    Phase 5 investigation note). Callers are expected to check the mode
    and the freelist before calling this; this function itself doesn't
    guard against a wasted call, so it stays a plain, honest wrapper
    around one PRAGMA rather than silently deciding not to run.

    max_pages must be a real int (not caller-controlled text) since, like
    wal_checkpoint above, PRAGMA statements reject bound `?` parameters
    entirely -- confirmed empirically -- so this has to be interpolated
    directly into the SQL string.
    """
    max_pages = int(max_pages)
    conn.execute(f"PRAGMA incremental_vacuum({max_pages})")  # nosec B608 -- max_pages is coerced to int() immediately above, never caller-controlled text
