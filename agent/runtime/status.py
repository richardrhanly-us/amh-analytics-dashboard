"""Thread-safe, in-memory-only shared status board (Continuous Ingestion
Phase F).

Bridges the Collector/Uploader loops (which already return rich,
per-cycle report objects -- CollectorCycleReport, UploadCycleResult) to
the Heartbeat loop, which needs a handful of "when did X last happen"
facts that live nowhere durable (spool/state track WHAT is pending, not
WHEN something last succeeded). The Supervisor's loop wrappers are the
only things that write to this -- SourceCollector/Uploader themselves
have no knowledge of it, keeping those modules' own tests free of any
status-object plumbing.

Deliberately never persisted: losing this on restart just means a few
heartbeat fields read as None/empty until the next cycle repopulates
them -- never a correctness problem, since nothing safety-critical is
tracked here (compare with agent/state.py, where losing data WOULD be a
correctness problem).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class ComponentHealth:
    name: str
    alive: bool = True
    last_error: str | None = None
    last_error_at: str | None = None
    started_at: str | None = None


@dataclass
class _StatusFields:
    last_success_at: str | None = None
    last_spool_write_at: str | None = None
    last_collector_active_at: str | None = None
    last_upload_failure_category: str | None = None
    last_upload_error: str | None = None
    last_collector_error: str | None = None
    source_missing: dict[str, bool] = field(default_factory=dict)
    source_generations: dict[str, int] = field(default_factory=dict)
    source_offsets: dict[str, int] = field(default_factory=dict)
    components: dict[str, ComponentHealth] = field(default_factory=dict)
    disk_pressure_paused: bool = False
    disk_pressure_reason: str | None = None
    collector_cycle_count: int = 0


class RuntimeStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fields = _StatusFields()

    # --- writers (called only from Supervisor loop wrappers) ---------------

    def record_upload_success(self) -> None:
        with self._lock:
            self._fields.last_success_at = _now_iso()
            self._fields.last_upload_failure_category = None
            self._fields.last_upload_error = None

    def record_upload_failure(self, category: str | None, error: str | None) -> None:
        with self._lock:
            self._fields.last_upload_failure_category = category
            self._fields.last_upload_error = error

    def record_spool_write(self) -> None:
        with self._lock:
            self._fields.last_spool_write_at = _now_iso()

    def record_collector_active(self) -> None:
        with self._lock:
            self._fields.last_collector_active_at = _now_iso()

    def record_collector_error(self, error: str) -> None:
        with self._lock:
            self._fields.last_collector_error = error

    def set_source_missing(self, name: str, missing: bool) -> None:
        with self._lock:
            self._fields.source_missing[name] = missing

    def set_source_position(self, name: str, *, generation: int, offset: int) -> None:
        with self._lock:
            self._fields.source_generations[name] = generation
            self._fields.source_offsets[name] = offset

    def record_component_started(self, name: str) -> None:
        with self._lock:
            self._fields.components[name] = ComponentHealth(name=name, alive=True, started_at=_now_iso())

    def record_component_failed(self, name: str, error: str) -> None:
        with self._lock:
            self._fields.components[name] = ComponentHealth(
                name=name, alive=False, last_error=str(error), last_error_at=_now_iso()
            )

    def set_disk_pressure(self, paused: bool, reason: str | None = None) -> None:
        with self._lock:
            self._fields.disk_pressure_paused = paused
            self._fields.disk_pressure_reason = reason

    def is_disk_pressure_paused(self) -> bool:
        with self._lock:
            return self._fields.disk_pressure_paused

    def record_collector_cycle_completed(self) -> int:
        """Called once per completed collector loop iteration --
        regardless of whether that iteration actually read anything or
        was skipped entirely due to disk pressure. Exists so tests can
        prove a property like "at least one full collector cycle ran
        AFTER a given point in time" deterministically (by waiting for
        this counter to advance past a captured baseline) instead of
        guessing at a sleep duration that merely makes a race unlikely.
        Returns the new count."""
        with self._lock:
            self._fields.collector_cycle_count += 1
            return self._fields.collector_cycle_count

    def get_collector_cycle_count(self) -> int:
        with self._lock:
            return self._fields.collector_cycle_count

    def record_component_heartbeat(self, name: str) -> None:
        """Called periodically by a still-running component loop so a
        crashed-but-not-yet-detected thread can eventually be told apart
        from a genuinely alive one -- not currently used for anything
        beyond recording alive=True; see get_component_health."""
        with self._lock:
            existing = self._fields.components.get(name)
            if existing is None or not existing.alive:
                self._fields.components[name] = ComponentHealth(name=name, alive=True, started_at=_now_iso())

    # --- readers -------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            f = self._fields
            return {
                "last_success_at": f.last_success_at,
                "last_spool_write_at": f.last_spool_write_at,
                "last_collector_active_at": f.last_collector_active_at,
                "last_upload_failure_category": f.last_upload_failure_category,
                "last_upload_error": f.last_upload_error,
                "last_collector_error": f.last_collector_error,
                "source_missing": dict(f.source_missing),
                "source_generations": dict(f.source_generations),
                "source_offsets": dict(f.source_offsets),
                "components": {k: v for k, v in f.components.items()},
                "disk_pressure_paused": f.disk_pressure_paused,
                "disk_pressure_reason": f.disk_pressure_reason,
                "collector_cycle_count": f.collector_cycle_count,
            }

    def component_health(self) -> dict[str, ComponentHealth]:
        with self._lock:
            return dict(self._fields.components)
