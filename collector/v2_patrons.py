"""Contract v2: the LOCAL patron cache and ruleset registry (docs/collector-v2.md).

Message-64 records tell the collector which account a patron identifier belongs to (a name and a patron type). The cloud must never see
any of it, so the collector keeps just enough to classify a hold and to recognize a patron card, and keeps it in this SQLite file:

    patrons(pk BLOB, name_k BLOB, type_ill INT, name_ill INT, first_seen DATE, last_seen DATE)

  pk         HMAC(K_patron, patron identifier)      -- never the identifier
  name_k     HMAC(K_name, upper-cased patron name)  -- never the name; lets rule lists be re-evaluated after a rules change
  type_ill   the patron TYPE was "ILL"              -- a classification flag
  name_ill   the patron NAME contains the ILL/INTERLIBRARY keywords (the dashboard's test) -- a classification flag
  first_seen / last_seen                            -- coarse (whole-day) dates

No cleartext identifier or name is ever written. `tests` read the raw bytes of the file and of every page to prove it.

LIFECYCLE. A 180-day SLIDING TTL: a row lives while it keeps being seen (a message-64 record, an ACS message quoting the identifier, or the
guard matching a scanned card). Older rows are purged at the start of every run; a row cap evicts the least recently seen if it is exceeded.

THE GUARD. `contains(pk)` answers "is this string a patron identifier this machine has seen?" -- the only way a barcode is ever treated as
a patron card. It is NEVER decided from the string's length or pattern.

WRITES ARE STAGED. Changes go to an in-memory overlay first and reach the file only on `commit()`, which the run calls after a chunk is
delivered. `read_only=True` (the dry run) never writes: it reads the file if it exists and keeps its overlay in memory.

The ruleset registry maps a keyed rules fingerprint to an OPAQUE RANDOM UUID: the ruleset_id a hold reports has no relationship to the rules.

THE HOLD LEDGER (`holds`). A patron's message-64 record can arrive AFTER a hold it classifies, and the dashboard applies the latest profile to
every hold in its window. To reproduce that without sending patron information, the collector keeps, for the LATEST hold of each item that has a
patron: the item_key (the same keyed HMAC the cloud already has), the event time, the patron HMAC, the destination slug and the flags it sent
(plus the part of them that came from the hold's own message, and a correction counter). When a later profile changes a patron's classification,
the collector re-sends those holds with corrected flags (docs/collector-v2.md). Nothing in the ledger is cleartext; it links an item pseudonym to a
patron pseudonym LOCALLY only, is kept for `hold_days` (default 90) and capped at `hold_max_rows`, and is never uploaded.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Self

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patrons (
    pk BLOB PRIMARY KEY, name_k BLOB, type_ill INTEGER NOT NULL DEFAULT 0, name_ill INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS patrons_last_seen_idx ON patrons (last_seen);
CREATE TABLE IF NOT EXISTS rulesets (
    fingerprint BLOB PRIMARY KEY, ruleset_id TEXT NOT NULL, created TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS holds (
    item_key TEXT PRIMARY KEY, event_time TEXT NOT NULL, patron_pk BLOB NOT NULL, destination TEXT NOT NULL,
    s_ill INTEGER NOT NULL, s_branch INTEGER NOT NULL, s_coll INTEGER NOT NULL,
    f_ill INTEGER NOT NULL, f_branch INTEGER NOT NULL, f_coll INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0, event_day TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS holds_patron_idx ON holds (patron_pk);
CREATE INDEX IF NOT EXISTS holds_day_idx ON holds (event_day);
"""
_MAX_RULESETS = 50


@dataclass(frozen=True)
class HoldRow:
    """The latest hold of one item that has a patron, as the ledger keeps it. All keyed or safe values; see the module docstring."""

    item_key: str
    event_time: str                          # YYYY-MM-DDTHH:MM:SSZ, exactly as sent
    patron_pk: bytes                         # HMAC(K_patron, patron identifier): local only
    destination: str
    static: tuple[bool, bool, bool]          # ill / branch / collection contributions from the hold's OWN message
    flags: tuple[bool, bool, bool]           # what was last sent
    revision: int = 0


@dataclass(frozen=True)
class PatronInfo:
    """What the classifier may know about a patron: two flags and a keyed name. Nothing readable."""

    name_k: bytes | None
    type_ill: bool
    name_ill: bool


class PatronCache:
    def __init__(self, path: str | Path, *, ttl_days: int = 180, max_rows: int = 500_000, hold_days: int = 90,
                 hold_max_rows: int = 250_000, today: date | None = None, read_only: bool = False):
        self.path = Path(path)
        self.ttl_days, self.max_rows = ttl_days, max_rows
        self.hold_days, self.hold_max_rows = hold_days, hold_max_rows
        self._hold_overlay: dict[str, HoldRow | None] = {}
        self.today = today or datetime.now(UTC).date()
        self.read_only = read_only
        self._overlay: dict[bytes, dict] = {}
        self._new_rulesets: dict[bytes, str] = {}
        if read_only:
            # Never creates, migrates or writes the file: an existing cache is opened read-only, a missing one is an empty in-memory table.
            if self.path.is_file():
                self._db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            else:
                self._db = sqlite3.connect(":memory:")
                self._db.executescript(_SCHEMA)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path))
            self._db.execute("PRAGMA secure_delete = ON")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    @classmethod
    def in_memory(cls, *, today: date | None = None) -> PatronCache:
        """A cache that lives only in memory and never touches a file (the dry run)."""
        return cls(Path("<in-memory>"), today=today, read_only=True)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- reading --------------------------------------------------------------------------------------------------------------

    def lookup(self, pk: bytes) -> PatronInfo | None:
        staged = self._overlay.get(pk)
        row = self._db.execute("SELECT name_k, type_ill, name_ill FROM patrons WHERE pk = ?", (pk,)).fetchone()
        stored = PatronInfo(row[0], bool(row[1]), bool(row[2])) if row else None
        if staged is None:
            return stored
        if staged["profile"]:
            return PatronInfo(staged["name_k"], staged["type_ill"], staged["name_ill"])
        return stored or PatronInfo(None, False, False)

    def contains(self, pk: bytes) -> bool:
        """The patron-card guard's question. True only for an identifier this machine has actually seen."""
        return pk in self._overlay or self._db.execute("SELECT 1 FROM patrons WHERE pk = ?", (pk,)).fetchone() is not None

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM patrons").fetchone()[0])

    # --- staging and committing ---------------------------------------------------------------------------------------------

    def stage(self, pk: bytes, *, name_k: bytes | None = None, type_ill: bool = False, name_ill: bool = False,
              profile: bool = False) -> None:
        """Records a sighting. `profile=True` (a message-64 record) also sets the name key and flags; otherwise only the last-seen
        date moves and an existing profile is kept."""
        if not profile and pk in self._overlay:
            return  # a plain sighting never replaces a profile (or an earlier sighting) staged for the same chunk
        self._overlay[pk] = {"name_k": name_k, "type_ill": type_ill, "name_ill": name_ill, "profile": profile}

    def commit(self) -> None:
        if self.read_only:
            self._overlay.clear()
            self._hold_overlay.clear()
            return
        self._commit_holds()
        if not self._overlay and not self._new_rulesets:
            return
        now = self.today.isoformat()
        with self._db:
            for pk, item in self._overlay.items():
                if item["profile"]:
                    self._db.execute(
                        "INSERT INTO patrons (pk, name_k, type_ill, name_ill, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(pk) DO UPDATE SET name_k = excluded.name_k, type_ill = excluded.type_ill, "
                        "name_ill = excluded.name_ill, last_seen = excluded.last_seen",
                        (pk, item["name_k"], int(item["type_ill"]), int(item["name_ill"]), now, now))
                else:
                    self._db.execute(
                        "INSERT INTO patrons (pk, name_k, type_ill, name_ill, first_seen, last_seen) VALUES (?, NULL, 0, 0, ?, ?) "
                        "ON CONFLICT(pk) DO UPDATE SET last_seen = excluded.last_seen", (pk, now, now))
            for fingerprint, ruleset_id in self._new_rulesets.items():
                self._db.execute("INSERT OR IGNORE INTO rulesets (fingerprint, ruleset_id, created) VALUES (?, ?, ?)",
                                 (fingerprint, ruleset_id, now))
        self._overlay.clear()
        self._new_rulesets.clear()

    def discard(self) -> None:
        self._overlay.clear()
        self._hold_overlay.clear()

    # --- lifecycle -----------------------------------------------------------------------------------------------------------

    def purge(self) -> tuple[int, int]:
        """Deletes rows not seen for `ttl_days`, then the least recently seen rows beyond `max_rows`. Returns (expired, evicted).
        A read-only cache never deletes."""
        if self.read_only:
            return 0, 0
        cutoff = (self.today - timedelta(days=self.ttl_days)).isoformat()
        with self._db:
            expired = self._db.execute("DELETE FROM patrons WHERE last_seen < ?", (cutoff,)).rowcount
            excess = self.count() - self.max_rows
            evicted = 0
            if excess > 0:
                evicted = self._db.execute(
                    "DELETE FROM patrons WHERE pk IN (SELECT pk FROM patrons ORDER BY last_seen ASC LIMIT ?)", (excess,)).rowcount
            self._db.execute(
                "DELETE FROM rulesets WHERE fingerprint NOT IN (SELECT fingerprint FROM rulesets ORDER BY created DESC LIMIT ?)",
                (_MAX_RULESETS,))
        return int(expired), int(evicted)

    # --- ruleset registry ------------------------------------------------------------------------------------------------------

    def ruleset_id_for(self, fingerprint: bytes) -> str:
        """The opaque random UUID reported for a rules fingerprint: the stored one, or a NEW random one. A read-only cache returns a
        fresh random UUID without storing it."""
        if fingerprint in self._new_rulesets:
            return self._new_rulesets[fingerprint]
        row = self._db.execute("SELECT ruleset_id FROM rulesets WHERE fingerprint = ?", (fingerprint,)).fetchone()
        if row:
            return str(row[0])
        ruleset_id = str(uuid.uuid4())
        self._new_rulesets[fingerprint] = ruleset_id
        if not self.read_only:
            with self._db:
                self._db.execute("INSERT OR IGNORE INTO rulesets (fingerprint, ruleset_id, created) VALUES (?, ?, ?)",
                                 (fingerprint, ruleset_id, self.today.isoformat()))
            self._new_rulesets.pop(fingerprint, None)
        return ruleset_id

    # --- the hold ledger ----------------------------------------------------------------------------------------------------

    _HOLD_COLUMNS = "item_key, event_time, patron_pk, destination, s_ill, s_branch, s_coll, f_ill, f_branch, f_coll, revision"

    @staticmethod
    def _hold_from(row: tuple) -> HoldRow:
        return HoldRow(row[0], row[1], bytes(row[2]), row[3], (bool(row[4]), bool(row[5]), bool(row[6])),
                       (bool(row[7]), bool(row[8]), bool(row[9])), int(row[10]))

    def _hold_query(self, sql: str, params: tuple = ()) -> list[tuple]:
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            if self.read_only:  # a cache file written before the ledger existed, opened read-only: there is simply nothing in it
                return []
            raise

    def hold_get(self, item_key: str) -> HoldRow | None:
        if item_key in self._hold_overlay:
            return self._hold_overlay[item_key]
        rows = self._hold_query(f"SELECT {self._HOLD_COLUMNS} FROM holds WHERE item_key = ?", (item_key,))  # nosec B608 - constant columns
        return self._hold_from(rows[0]) if rows else None

    def hold_put(self, row: HoldRow) -> None:
        self._hold_overlay[row.item_key] = row

    def hold_delete(self, item_key: str) -> None:
        self._hold_overlay[item_key] = None

    def holds_for_patron(self, patron_pk: bytes) -> list[HoldRow]:
        """Every ledger hold of this patron (staged changes included), oldest first."""
        found = {r.item_key: r for r in (self._hold_from(t) for t in self._hold_query(
            f"SELECT {self._HOLD_COLUMNS} FROM holds WHERE patron_pk = ?", (patron_pk,)))}  # nosec B608 - constant columns
        for item_key, row in self._hold_overlay.items():
            if row is None or row.patron_pk != patron_pk:
                found.pop(item_key, None)
            else:
                found[item_key] = row
        return sorted(found.values(), key=lambda r: (r.event_time, r.item_key))

    def hold_count(self) -> int:
        rows = self._hold_query("SELECT COUNT(*) FROM holds")
        return int(rows[0][0]) if rows else 0

    def _commit_holds(self) -> None:
        if not self._hold_overlay:
            return
        with self._db:
            for item_key, row in self._hold_overlay.items():
                if row is None:
                    self._db.execute("DELETE FROM holds WHERE item_key = ?", (item_key,))
                    continue
                self._db.execute(
                    f"INSERT OR REPLACE INTO holds ({self._HOLD_COLUMNS}, event_day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",  # nosec B608
                    (row.item_key, row.event_time, row.patron_pk, row.destination, *(int(x) for x in row.static),
                     *(int(x) for x in row.flags), row.revision, row.event_time[:10]))
        self._hold_overlay.clear()

    def purge_holds(self) -> int:
        """Deletes ledger holds older than `hold_days` (by their EVENT day), then the oldest beyond `hold_max_rows`. Returns how many went."""
        if self.read_only:
            return 0
        cutoff = (self.today - timedelta(days=self.hold_days)).isoformat()
        with self._db:
            removed = self._db.execute("DELETE FROM holds WHERE event_day < ?", (cutoff,)).rowcount
            excess = self.hold_count() - self.hold_max_rows
            if excess > 0:
                removed += self._db.execute(
                    "DELETE FROM holds WHERE item_key IN (SELECT item_key FROM holds ORDER BY event_day ASC, item_key ASC LIMIT ?)",
                    (excess,)).rowcount
        return int(removed)
