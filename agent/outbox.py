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
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(local_events)").fetchall()}

    for column_name, column_ddl in _EVOLVED_COLUMNS:
        if column_name not in existing:
            conn.execute(f"ALTER TABLE local_events ADD COLUMN {column_name} {column_ddl}")


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the outbox database with WAL mode enabled
    and the schema applied. Safe to call every time the agent starts --
    every statement in SCHEMA is idempotent (CREATE ... IF NOT EXISTS),
    and _ensure_columns only adds columns that are actually missing."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None puts the connection in autocommit mode, so
    # transaction() below has full, explicit control over BEGIN/COMMIT/
    # ROLLBACK instead of relying on sqlite3's implicit transaction
    # handling (which opens a transaction before the first DML statement
    # and can make it non-obvious exactly what's inside one).
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row

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
