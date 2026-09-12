"""Canonical housekeeping (Continuous Ingestion Phase F).

Only lightweight responsibilities remain -- deliberately NOT a port of
agent/maintenance.py's SQLite concepts (WAL checkpointing, incremental
vacuum, delivered-row pruning). None of that applies here: spool.py has
no WAL, no vacuum, and already deletes a batch file the moment it's
acknowledged (see spool.acknowledge) -- there is no "delivered event
retention" concept to prune, because delivered events simply don't exist
as files anymore.

What this module actually does, once per housekeeping_interval_seconds:

  1. DISK-PRESSURE CHECK (see check_disk_pressure) -- the real safety
     mechanism Phase D correction #4 deferred implementing until a real
     runtime existed. Never deletes pending data to satisfy a size limit;
     instead flips a shared pause flag (agent.runtime.status.RuntimeStatus)
     that the Supervisor's collector loop checks BEFORE reading any new
     source data, so the cursor structurally cannot advance past
     unspooled content while paused.
  2. STALE TEMP-FILE CLEANUP -- agent.state.save_state / agent.spool's
     atomic write already clean up their own .tmp files on a failed
     write; this exists only for the rare case a process was killed hard
     enough to skip even that cleanup (e.g. SIGKILL, power loss). Only
     ever removes a temp file whose own mtime is older than
     stale_temp_file_age_seconds (default 1 hour) -- never a file that
     could still be mid-write by a process that's merely slow.
  3. DIAGNOSTICS SNAPSHOT -- writes the richer, local-only operational
     picture (source generations/offsets/paths, spool stats per source,
     component health, disk pressure state) to a JSON file under
     RuntimeConfig.diagnostics_dir. This is where the detail that does
     NOT go into the backend heartbeat (see agent/runtime/heartbeat.py's
     docstring for why) actually lives -- for local troubleshooting on
     the AMH machine, not a network call.

DISK-PRESSURE UNRESOLVED RISK (documented, not hidden): if collection
stays paused long enough that Tech Logic itself rotates or deletes the
unread source file before capacity recovers, the unread content is gone
at the OS level -- no amount of local safety logic here can recover
bytes Tech Logic no longer has. Pausing prevents this agent from making
the problem WORSE (it never advances a cursor past data it never
captured), but does not eliminate the underlying operational risk if a
disk-pressure incident runs long enough. Recovery in that case is a
Tech Logic / AMH-side data availability question, not something this
module can solve.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import spool
from .config import RuntimeConfig
from .status import RuntimeStatus


@dataclass(frozen=True)
class DiskPressureReport:
    paused: bool
    reason: str | None
    total_pending_bytes: int
    free_bytes: int | None


def check_disk_pressure(cfg: RuntimeConfig, status: RuntimeStatus) -> DiskPressureReport:
    total_pending_bytes = 0
    for source_cfg in cfg.sources:
        stats = spool.get_spool_stats(cfg.spool_root, source_cfg.name)
        total_pending_bytes += stats.pending_bytes

    free_bytes: int | None = None
    try:
        usage = shutil.disk_usage(Path(cfg.spool_root).anchor or ".")
        free_bytes = usage.free
    except OSError:
        free_bytes = None

    reasons = []
    if total_pending_bytes >= cfg.disk_pressure_pending_bytes_threshold:
        reasons.append(
            f"pending spool bytes {total_pending_bytes} >= threshold {cfg.disk_pressure_pending_bytes_threshold}"
        )
    if free_bytes is not None and free_bytes <= cfg.disk_pressure_min_free_bytes:
        reasons.append(f"free disk bytes {free_bytes} <= threshold {cfg.disk_pressure_min_free_bytes}")

    paused = bool(reasons)
    reason = "; ".join(reasons) if reasons else None
    status.set_disk_pressure(paused, reason)

    return DiskPressureReport(paused, reason, total_pending_bytes, free_bytes)


def clean_stale_temp_files(cfg: RuntimeConfig, *, stale_age_seconds: float = 3600.0) -> int:
    """Removes .tmp files older than stale_age_seconds under spool_root
    and state_path's directory -- the leftovers of a write that was
    interrupted hard enough to skip its own cleanup (see module
    docstring). Returns the count removed. Never touches a file younger
    than the threshold, so a genuinely in-progress write is never at risk."""
    removed = 0
    now = time.time()
    search_dirs = [Path(cfg.spool_root), Path(cfg.state_path).parent]

    for base_dir in search_dirs:
        if not base_dir.exists():
            continue
        for tmp_path in base_dir.rglob("*.tmp"):
            try:
                age = now - tmp_path.stat().st_mtime
                if age >= stale_age_seconds:
                    tmp_path.unlink()
                    removed += 1
            except OSError:
                continue

    return removed


def write_diagnostics_snapshot(cfg: RuntimeConfig, status: RuntimeStatus, *, agent_id: str, agent_version: str, started_at: str) -> Path:
    snap = status.snapshot()

    per_source = {}
    for source_cfg in cfg.sources:
        stats = spool.get_spool_stats(cfg.spool_root, source_cfg.name)
        per_source[source_cfg.name] = {
            "path": source_cfg.path,
            "generation": snap["source_generations"].get(source_cfg.name),
            "offset": snap["source_offsets"].get(source_cfg.name),
            "missing": snap["source_missing"].get(source_cfg.name, False),
            "pending_batch_count": stats.pending_batch_count,
            "pending_bytes": stats.pending_bytes,
            "quarantined_batch_count": stats.quarantined_batch_count,
            "quarantined_bytes": stats.quarantined_bytes,
        }

    document: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_version": agent_version,
        "customer_id": cfg.customer_id,
        "branch_id": cfg.branch_id,
        "started_at": started_at,
        # Same loud, unambiguous mode signal as the startup log banner
        # (see AgentRunner.start()) -- so a person reading only
        # diagnostics.json (no logs at hand) still can't miss which mode
        # produced this data.
        "mode": cfg.mode_description,
        "upload_enabled": cfg.upload_enabled,
        "heartbeat_enabled": cfg.heartbeat_enabled,
        "sources": per_source,
        "components": {name: vars(h) for name, h in snap["components"].items()},
        "disk_pressure_paused": snap["disk_pressure_paused"],
        "disk_pressure_reason": snap["disk_pressure_reason"],
        "last_success_at": snap["last_success_at"],
        "last_collector_active_at": snap["last_collector_active_at"],
    }

    diagnostics_dir = Path(cfg.diagnostics_dir)
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    dest = diagnostics_dir / "diagnostics.json"

    fd, tmp_name = None, None
    import tempfile

    fd, tmp_name = tempfile.mkstemp(dir=str(diagnostics_dir), prefix=".diagnostics.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(document, indent=2, default=str))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(dest))
    except BaseException:
        try:
            os.remove(str(tmp_path))
        except OSError:
            pass
        raise

    return dest


class Housekeeping:
    def __init__(
        self,
        cfg: RuntimeConfig,
        status: RuntimeStatus,
        logger: Any,
        *,
        agent_id: str,
        agent_version: str,
        started_at: str,
    ) -> None:
        self.cfg = cfg
        self.status = status
        self.logger = logger
        self.agent_id = agent_id
        self.agent_version = agent_version
        self.started_at = started_at

    def run_cycle(self) -> DiskPressureReport:
        report = check_disk_pressure(self.cfg, self.status)
        if report.paused:
            self.logger.warning("Disk pressure: collection paused | reason=%s", report.reason)
        else:
            self.logger.info(
                "Disk pressure check ok | pending_bytes=%s free_bytes=%s",
                report.total_pending_bytes, report.free_bytes,
            )

        removed = clean_stale_temp_files(self.cfg)
        if removed:
            self.logger.info("Removed %s stale temp file(s)", removed)

        write_diagnostics_snapshot(
            self.cfg, self.status, agent_id=self.agent_id, agent_version=self.agent_version,
            started_at=self.started_at,
        )

        return report

    def run_forever(self, *, stop_event, max_iterations: int | None = None) -> None:
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            if stop_event.is_set():
                return
            self.run_cycle()
            iterations += 1
            if (max_iterations is None or iterations < max_iterations) and stop_event.wait(
                self.cfg.housekeeping_interval_seconds
            ):
                return
