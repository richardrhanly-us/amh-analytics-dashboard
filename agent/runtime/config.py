"""Canonical runtime configuration (Continuous Ingestion Phase F).

One config surface for the new runtime -- deliberately NOT the legacy
agent/config.py (which loads agent_config.json and is tightly coupled to
the deployed scheduled pipeline's shape: raw_checkins_file,
processed_checkins_file, status_file, ...). Sharing that loader would
pull the new runtime into the legacy config file's schema and its eager
os.getenv("SORTVIEW_API_TOKEN")-at-import-time side effect
(agent/uploader.py does this too, which is why nothing in this package
imports agent.uploader either). Two independently-evolvable config
surfaces is the point -- the legacy pipeline must keep working completely
unchanged; see the Phase A/B reports for why.

No hardcoded production path. The intended production layout is:

    C:\\ProgramData\\SortView\\
      config\\agent_runtime_config.json
      state\\agent_state.json
      spool\\
      logs\\
      diagnostics\\

but that is a DEPLOYMENT convention, not something baked into defaults
here -- every path below is an explicit, required field in the config
file, so unit tests never have to fight a Windows-only default path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Base class for every error this module raises deliberately."""


class BootstrapMode(str, Enum):
    """See agent/runtime/collector.py's module docstring for exactly what
    each mode does. NORMAL is the only safe production default -- REPLAY
    and OFFSET must be requested explicitly, per source, never implied."""

    NORMAL = "normal"
    REPLAY = "replay"
    OFFSET = "offset"


@dataclass(frozen=True)
class SourceConfig:
    name: str
    path: str
    bootstrap_mode: BootstrapMode = BootstrapMode.NORMAL
    bootstrap_offset: int | None = None

    def __post_init__(self) -> None:
        if self.bootstrap_mode is BootstrapMode.OFFSET and self.bootstrap_offset is None:
            raise ConfigError(f"source {self.name!r}: bootstrap_mode 'offset' requires bootstrap_offset")
        if self.bootstrap_offset is not None and self.bootstrap_offset < 0:
            raise ConfigError(f"source {self.name!r}: bootstrap_offset must be non-negative")


@dataclass(frozen=True)
class RuntimeConfig:
    customer_id: int
    branch_id: int
    api_url: str
    api_token: str

    sources: tuple[SourceConfig, ...]

    state_path: Path
    spool_root: Path
    agent_identity_path: Path
    log_dir: Path
    diagnostics_dir: Path

    batch_max_events: int = 100
    batch_max_seconds: float = 2.0

    collector_poll_seconds: float = 1.0

    uploader_poll_seconds: float = 3.0
    uploader_backoff_base_seconds: float = 2.0
    uploader_backoff_max_seconds: float = 300.0
    uploader_backoff_multiplier: float = 2.0
    uploader_max_isolation_requests: int = 20
    uploader_max_upload_body_bytes: int = 2 * 1024 * 1024

    heartbeat_interval_seconds: float = 60.0
    heartbeat_backoff_base_seconds: float = 5.0
    heartbeat_backoff_max_seconds: float = 300.0

    housekeeping_interval_seconds: float = 300.0

    # Disk-pressure thresholds -- see agent/runtime/housekeeping.py.
    disk_pressure_pending_bytes_threshold: int = 500 * 1024 * 1024
    disk_pressure_min_free_bytes: int = 1024 * 1024 * 1024

    http_connect_timeout: float = 10.0
    http_upload_read_timeout: float = 300.0
    http_status_read_timeout: float = 60.0
    http_transport_retry_total: int = 2
    http_transport_backoff_factor: float = 0.5

    # Simple semver. 0.1.0 is the first canonical build considered a real
    # NBPL production-cutover candidate (real-AMH-machine shadow
    # validation passed on schema v3 -- see agent/README.md and
    # docs/amh-production-cutover-runbook.md) -- staying below 1.0.0
    # deliberately, since production UPLOADING itself has not yet been
    # validated end-to-end (see docs/amh-production-cutover-runbook.md's
    # Proven vs. Unproven section). Bump to 1.0.0 once a full controlled
    # production cutover completes successfully and the canonical agent
    # is the sole production ingestion path for at least one branch.
    agent_version: str = "0.1.0"

    # SHADOW / CAPTURE-ONLY VALIDATION MODE (Continuous Ingestion
    # preparation phase). Both default to True -- NORMAL PRODUCTION MODE
    # is the safe default for any config that doesn't mention these keys
    # at all; a config must explicitly opt INTO shadow mode, never fall
    # into it by omission. See agent/runtime/supervisor.py's module
    # docstring and agent/README.md for the full behavior contract.
    #
    # upload_enabled=False: the uploader thread keeps running on its
    # normal schedule (so start/stop/restart behavior is identical to
    # production) but never calls agent.runtime.http_client.post_json for
    # /upload -- collection, source_event_id generation, state, and spool
    # all continue completely unaffected. Deliberately NOT implemented by
    # pointing api_url at a broken endpoint or api_token at an invalid
    # value -- that would generate real retry/backoff/error noise (and
    # exercise AUTH_FAILURE/RETRYABLE_INFRA bookkeeping) for a condition
    # that isn't actually a failure, which is exactly the noise this flag
    # exists to avoid.
    # heartbeat_enabled=False: same idea for /upload-pipeline-status --
    # the heartbeat thread keeps computing its health snapshot (useful
    # for local logs/diagnostics) on its normal interval, but never POSTs
    # it.
    upload_enabled: bool = True
    heartbeat_enabled: bool = True

    def source(self, name: str) -> SourceConfig:
        for source_cfg in self.sources:
            if source_cfg.name == name:
                return source_cfg
        raise ConfigError(f"no source configured named {name!r}")

    @property
    def mode_description(self) -> str:
        """One human-readable line unambiguously stating which mode is
        active -- used in the startup log banner and diagnostics.json so
        a person looking at either immediately knows what's running.
        Never silently blends into the surrounding log output."""
        if self.upload_enabled and self.heartbeat_enabled:
            return "NORMAL PRODUCTION MODE (uploads and heartbeat both enabled)"
        if not self.upload_enabled and not self.heartbeat_enabled:
            return "SHADOW / CAPTURE-ONLY VALIDATION MODE (no production event uploads, no production heartbeat writes)"
        return (
            f"CUSTOM MODE (upload_enabled={self.upload_enabled}, "
            f"heartbeat_enabled={self.heartbeat_enabled}) -- not standard production or shadow mode, verify intentional"
        )


_REQUIRED_TOP_LEVEL_KEYS = [
    "customer_id", "branch_id", "api_url", "sources",
    "state_path", "spool_root", "agent_identity_path", "log_dir", "diagnostics_dir",
]

_OPTIONAL_NUMERIC_KEYS = [
    "batch_max_events", "batch_max_seconds", "collector_poll_seconds",
    "uploader_poll_seconds", "uploader_backoff_base_seconds", "uploader_backoff_max_seconds",
    "uploader_backoff_multiplier", "uploader_max_isolation_requests", "uploader_max_upload_body_bytes",
    "heartbeat_interval_seconds", "heartbeat_backoff_base_seconds", "heartbeat_backoff_max_seconds",
    "housekeeping_interval_seconds",
    "disk_pressure_pending_bytes_threshold", "disk_pressure_min_free_bytes",
    "http_connect_timeout", "http_upload_read_timeout", "http_status_read_timeout",
    "http_transport_retry_total", "http_transport_backoff_factor",
]

# Kept separate from _OPTIONAL_NUMERIC_KEYS deliberately: bool(<non-empty
# string>) is ALWAYS True in Python regardless of content (bool("false")
# is True), so these need their own parser -- see _parse_bool -- rather
# than the numeric loop's generic field_type(value) coercion.
_OPTIONAL_BOOL_KEYS = ["upload_enabled", "heartbeat_enabled"]


def _env_override(key: str) -> str | None:
    return os.environ.get(f"SORTVIEW_RUNTIME_{key.upper()}")


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    raise ConfigError(f"cannot parse boolean value from {value!r}")


def _parse_source(raw: dict[str, Any]) -> SourceConfig:
    if "name" not in raw or "path" not in raw:
        raise ConfigError(f"source entry missing 'name' or 'path': {raw!r}")
    mode = BootstrapMode(raw.get("bootstrap_mode", BootstrapMode.NORMAL.value))
    return SourceConfig(
        name=raw["name"],
        path=raw["path"],
        bootstrap_mode=mode,
        bootstrap_offset=raw.get("bootstrap_offset"),
    )


def load_runtime_config(config_path: str | Path) -> RuntimeConfig:
    """Loads and validates the canonical runtime config from a JSON file.

    api_token is NEVER read from the JSON file itself (a credential has
    no business sitting in a config file next to source paths) -- it
    comes from the SORTVIEW_API_TOKEN environment variable, same name the
    legacy agent already uses, so both paths document the token the same
    way to an operator even though they're read by different code.

    Any of the optional numeric/behavioral settings can be overridden by
    an environment variable named SORTVIEW_RUNTIME_<KEY_UPPERCASE>,
    checked AFTER the JSON file's own value (env wins) -- useful for
    tests and for a deployment that wants one setting different without
    editing the file.
    """
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"runtime config file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"runtime config file is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("runtime config document root must be an object")

    missing = [key for key in _REQUIRED_TOP_LEVEL_KEYS if key not in raw]
    if missing:
        raise ConfigError(f"runtime config missing required key(s): {', '.join(missing)}")

    api_token = os.environ.get("SORTVIEW_API_TOKEN")
    if not api_token:
        raise ConfigError("Missing API token. Set SORTVIEW_API_TOKEN as an environment variable.")

    sources_raw = raw["sources"]
    if not isinstance(sources_raw, list) or not sources_raw:
        raise ConfigError("runtime config 'sources' must be a non-empty list")
    sources = tuple(_parse_source(s) for s in sources_raw)

    kwargs: dict[str, Any] = {
        "customer_id": int(raw["customer_id"]),
        "branch_id": int(raw["branch_id"]),
        "api_url": str(raw["api_url"]).rstrip("/"),
        "api_token": api_token,
        "sources": sources,
        "state_path": Path(raw["state_path"]),
        "spool_root": Path(raw["spool_root"]),
        "agent_identity_path": Path(raw["agent_identity_path"]),
        "log_dir": Path(raw["log_dir"]),
        "diagnostics_dir": Path(raw["diagnostics_dir"]),
    }
    if "agent_version" in raw:
        kwargs["agent_version"] = str(raw["agent_version"])

    defaults = RuntimeConfig(**kwargs)
    for key in _OPTIONAL_NUMERIC_KEYS:
        value: Any = raw.get(key, getattr(defaults, key))
        env_value = _env_override(key)
        if env_value is not None:
            value = env_value
        field_type = type(getattr(defaults, key))
        kwargs[key] = field_type(value)

    for key in _OPTIONAL_BOOL_KEYS:
        value = raw.get(key, getattr(defaults, key))
        env_value = _env_override(key)
        if env_value is not None:
            value = env_value
        kwargs[key] = _parse_bool(value)

    return RuntimeConfig(**kwargs)
