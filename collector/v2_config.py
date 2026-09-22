"""Contract v2: configuration (docs/collector-v2.md).

`contract_mode` selects the wire contract. It defaults to "v1" when absent, so every existing config keeps running exactly as before;
"v2" must be chosen explicitly. The v2 settings live in their own `"v2"` section of the same JSON file (no secret in it: the HMAC secret
is in a DPAPI blob, the API token is still an environment variable):

    "contract_mode": "v2",
    "v2": {
      "key_id":   "<server-issued UUIDv4>",           required (non-secret)
      "timezone": "America/Chicago",                   required IANA name: Tech Logic timestamps are naive local times
      ... optional paths and limits (below); every path has a default next to the v1 files

v2 keeps its OWN state, status, patron cache and quarantine files, so running v2 (or its dry run) never touches the v1 cursor.

Error messages name the setting, never its value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import ConfigError
from .v2_events import UnsafeEventError, validate_key_id

MODES = ("v1", "v2")
MAX_EVENTS_PER_REQUEST_CEILING = 1000  # the server's total-events cap


@dataclass(frozen=True)
class V2Config:
    key_id: str
    timezone: str
    secret_path: Path
    rules_path: Path
    patron_cache_path: Path
    quarantine_path: Path
    state_path: Path
    status_path: Path
    chunk_lines: int = 2000
    max_events_per_request: int = 1000
    max_chunks_per_run: int = 100
    request_interval_seconds: float = 2.2  # keeps a busy run under the server's 30 requests/minute limit
    patron_ttl_days: int = 180
    patron_cache_max_rows: int = 500_000
    hold_ledger_days: int = 90                 # how long a held item can still be corrected by a later patron profile
    hold_ledger_max_rows: int = 250_000
    quarantine_max_entries: int = 10_000
    dry_run_tail_bytes: int = 2 * 1024 * 1024
    error_after_consecutive_failures: int = 3

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _read_document(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw: Any = None
    unreadable = False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # JSONDecodeError holds the whole document; it must not survive as an exception context
        unreadable = True
    if unreadable:
        raise ConfigError("config file is not valid JSON")
    if not isinstance(raw, dict):
        raise ConfigError("config document root must be an object")
    return raw


def read_contract_mode(config_path: str | Path) -> str:
    """"v1" (the default when `contract_mode` is absent) or "v2". Needs no token and no other setting."""
    mode = _read_document(config_path).get("contract_mode", "v1")
    if mode not in MODES:
        raise ConfigError("config 'contract_mode' must be \"v1\" or \"v2\"")
    return str(mode)


def _int(section: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = section.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"config 'v2.{name}' must be an integer between {low} and {high}")
    return value


def _path(section: dict[str, Any], name: str, default: Path) -> Path:
    value = section.get(name)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"config 'v2.{name}' must be a non-empty path")
    return Path(value)


def load_v2_config(config_path: str | Path, *, require: bool = False) -> V2Config | None:
    """The v2 settings, or None when there is no `v2` section and `require` is False. With `require`, a missing section is an error."""
    raw = _read_document(config_path)
    section = raw.get("v2")
    if section is None:
        if require:
            raise ConfigError("config has no 'v2' section")
        return None
    if not isinstance(section, dict):
        raise ConfigError("config 'v2' must be an object")

    key_id = ""
    try:
        key_id = validate_key_id(section.get("key_id"))
    except UnsafeEventError:
        key_id = ""
    if not key_id:
        raise ConfigError("config 'v2.key_id' must be the server-issued key_id (a lower-case UUID)")

    timezone = section.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise ConfigError("config 'v2.timezone' is required: an IANA time zone name, for example America/Chicago")
    known = True
    try:
        ZoneInfo(timezone)
    except Exception:  # unknown key, malformed key, or no tz database: all the same to the operator
        known = False
    if not known:
        raise ConfigError("config 'v2.timezone' is not a known IANA time zone name")

    for required in ("state_path",):
        if required not in raw:
            raise ConfigError(f"config missing required key: {required}")
    root = Path(str(raw["state_path"])).parent.parent  # e.g. C:\ProgramData\SortViewCollector
    data = root / "data" / "v2"

    max_events = _int(section, "max_events_per_request", 1000, 1, MAX_EVENTS_PER_REQUEST_CEILING)
    interval = section.get("request_interval_seconds", 2.2)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 <= interval <= 60:
        raise ConfigError("config 'v2.request_interval_seconds' must be a number between 0 and 60")

    return V2Config(
        key_id=key_id,
        timezone=timezone,
        secret_path=_path(section, "secret_path", root / "secrets" / "v2_key.dpapi"),
        rules_path=_path(section, "rules_path", root / "config" / "classification_rules.json"),
        patron_cache_path=_path(section, "patron_cache_path", data / "patron_cache.db"),
        quarantine_path=_path(section, "quarantine_path", data / "quarantine_v2.json"),
        state_path=_path(section, "state_path", data / "state_v2.json"),
        status_path=_path(section, "status_path", data / "status_v2.json"),
        chunk_lines=_int(section, "chunk_lines", 2000, 1, 50_000),
        max_events_per_request=max_events,
        max_chunks_per_run=_int(section, "max_chunks_per_run", 100, 1, 10_000),
        request_interval_seconds=float(interval),
        patron_ttl_days=_int(section, "patron_ttl_days", 180, 1, 3650),
        patron_cache_max_rows=_int(section, "patron_cache_max_rows", 500_000, 1000, 10_000_000),
        hold_ledger_days=_int(section, "hold_ledger_days", 90, 1, 3650),
        hold_ledger_max_rows=_int(section, "hold_ledger_max_rows", 250_000, 1000, 10_000_000),
        quarantine_max_entries=_int(section, "quarantine_max_entries", 10_000, 10, 1_000_000),
        dry_run_tail_bytes=_int(section, "dry_run_tail_bytes", 2 * 1024 * 1024, 1024, 256 * 1024 * 1024),
        error_after_consecutive_failures=_int(section, "error_after_consecutive_failures", 3, 1, 1000),
    )


# --- the dry run's own, smaller configuration ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DryRunSettings:
    """Everything `run --v2-dry-run` needs, and NOTHING it must not have: where the Tech Logic files are, the time zone, the local rules and
    the tail size. No API token, no API URL, no key_id, no state/status/log/secret path: the dry run reads and classifies local data only."""

    sources: tuple[tuple[str, str], ...]     # (name, path)
    timezone: str
    rules_path: Path
    dry_run_tail_bytes: int = 2 * 1024 * 1024

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_dry_run_settings(config_path: str | Path) -> DryRunSettings:
    """Reads only the settings the dry run needs from the collector config. It never reads the environment (so no token) and never builds the
    v1 configuration, so a config that lacks the API url, ids or state paths still works. Error messages name settings, never values."""
    raw = _read_document(config_path)
    section = raw.get("v2")
    if not isinstance(section, dict):
        raise ConfigError("config has no 'v2' section (the dry run needs at least 'v2.timezone')")

    timezone = section.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise ConfigError("config 'v2.timezone' is required: an IANA time zone name, for example America/Chicago")
    known = True
    try:
        ZoneInfo(timezone)
    except Exception:  # unknown key, malformed key, or no tz database: all the same to the operator
        known = False
    if not known:
        raise ConfigError("config 'v2.timezone' is not a known IANA time zone name")

    entries = raw.get("sources")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("config 'sources' must be a non-empty list")
    sources: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("name", "")).strip() or not str(entry.get("path", "")).strip():
            raise ConfigError("every entry of config 'sources' needs a non-empty 'name' and 'path'")
        sources.append((str(entry["name"]), str(entry["path"])))
    if len({name for name, _path in sources}) != len(sources):
        raise ConfigError("config 'sources' has duplicate names")

    if section.get("rules_path") is not None:
        rules_path = _path(section, "rules_path", Path("."))
    elif "state_path" in raw:
        rules_path = Path(str(raw["state_path"])).parent.parent / "config" / "classification_rules.json"
    else:
        raise ConfigError("config 'v2.rules_path' is required (the local classification rules)")
    return DryRunSettings(tuple(sources), timezone, rules_path,
                          _int(section, "dry_run_tail_bytes", 2 * 1024 * 1024, 1024, 256 * 1024 * 1024))
