"""Contract v2: the safe quarantine (docs/collector-v2.md).

An event the server permanently refuses (a 409 `event_conflict`, or a 422 for that one event) must not be retried forever, and must not
block the cursor. It is quarantined: recorded here, skipped from then on, and never sent again.

WHAT IS STORED, and all of it: the `event_key` (a keyed HMAC of v2-safe fields -- opaque), the event kind, the normalized UTC event time,
a FIXED reason code and the whole-day date it was quarantined. There is no field for anything else, so there is nowhere for a raw line, a
barcode, a patron identifier or a message to go: the entry type only accepts those five, each validated against its format.

`quarantined_count` (the heartbeat's number) is simply how many entries are CURRENTLY retained. Entries expire after `retention_days` and the
store is capped; the oldest go first.

A patron-card guard drop is NOT a quarantine entry: it only increments an aggregate counter (nothing about the scan is kept).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import state
from .v2_events import HMAC_HEX_PATTERN, KINDS

REASONS = ("event_conflict", "invalid_event")
SCHEMA_VERSION = 1
_HEX = re.compile(HMAC_HEX_PATTERN)
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class QuarantineEntryError(ValueError):
    """An entry field is not in its safe format. The message never contains the value."""


@dataclass(frozen=True)
class QuarantineEntry:
    event_key: str
    kind: str
    event_time: str  # normalized UTC, YYYY-MM-DDTHH:MM:SSZ
    reason: str
    quarantined_on: str  # YYYY-MM-DD

    def __post_init__(self) -> None:
        if not (isinstance(self.event_key, str) and _HEX.fullmatch(self.event_key)):
            raise QuarantineEntryError("unsafe or malformed field: event_key")
        if self.kind not in KINDS:
            raise QuarantineEntryError("unsafe or malformed field: kind")
        if not (isinstance(self.event_time, str) and _TIME.fullmatch(self.event_time)):
            raise QuarantineEntryError("unsafe or malformed field: event_time")
        if self.reason not in REASONS:
            raise QuarantineEntryError("unsafe or malformed field: reason")
        if not (isinstance(self.quarantined_on, str) and _DATE.fullmatch(self.quarantined_on)):
            raise QuarantineEntryError("unsafe or malformed field: quarantined_on")

    def as_dict(self) -> dict[str, str]:
        return {"event_key": self.event_key, "kind": self.kind, "event_time": self.event_time, "reason": self.reason,
                "quarantined_on": self.quarantined_on}


class Quarantine:
    def __init__(self, path: str | Path, *, max_entries: int = 10_000, retention_days: int = 90, today: date | None = None,
                 read_only: bool = False):
        self.path = Path(path)
        self.max_entries, self.retention_days = max_entries, retention_days
        self.today = today or datetime.now(UTC).date()
        self.read_only = read_only
        self.entries: list[QuarantineEntry] = []
        self.reset_from_corrupt_file = False
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("schema")
            self.entries = [QuarantineEntry(**item) for item in document["entries"]]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # An unreadable file is set aside (never trusted, never printed) and the quarantine starts empty: at worst a conflict is
            # re-discovered and quarantined again. A read-only view just starts empty.
            self.entries = []
            self.reset_from_corrupt_file = True
            if not self.read_only:
                try:
                    os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
                except OSError:
                    pass
        self._prune()

    def _prune(self) -> None:
        cutoff = (self.today - timedelta(days=self.retention_days)).isoformat()
        kept = [entry for entry in self.entries if entry.quarantined_on >= cutoff]
        if len(kept) > self.max_entries:
            kept = kept[len(kept) - self.max_entries:]  # oldest first: drop from the front
        self.entries = kept

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def count(self) -> int:
        """quarantined_count: the entries currently retained."""
        return len(self.entries)

    def contains(self, kind: str, event_key: str) -> bool:
        return any(entry.kind == kind and entry.event_key == event_key for entry in self.entries)

    def keys_for(self, kind: str) -> set[str]:
        return {entry.event_key for entry in self.entries if entry.kind == kind}

    def add(self, new_entries: list[QuarantineEntry]) -> None:
        if self.read_only:
            return
        known = {(entry.kind, entry.event_key) for entry in self.entries}
        for entry in new_entries:
            if (entry.kind, entry.event_key) not in known:
                self.entries.append(entry)
                known.add((entry.kind, entry.event_key))
        self._prune()
        self.save()

    def save(self) -> None:
        if self.read_only:
            return
        state.atomic_write_json(self.path, {"schema_version": SCHEMA_VERSION, "entries": [e.as_dict() for e in self.entries]})

    def prune_and_save(self) -> None:
        self._prune()
        self.save()
