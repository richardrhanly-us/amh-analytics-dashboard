"""Config loading (Phase 4a).

One JSON config file (no secret in it) + the API token from
<root>\\secrets\\api_token.dpapi (collector/api_token_store.py; see
resolve_api_token for the 1.0.11-only SORTVIEW_API_TOKEN migration
fallback) -- the same no-secret-in-config split already proven in both the legacy pipeline
(agent/config.py) and the continuous agent (agent/runtime/config.py),
reimplemented here as fresh, independent code (see collector/__init__.py).

`sources` is a small named array, not three hardcoded fields (the one
deliberate structural change from the legacy config shape) -- this makes
source PATHS configurable per install. It does NOT make the collector
format/vendor-generic: agent/parser/* (unchanged, out of scope for this
phase) still only understands Tech Logic's specific file formats. A
second AMH vendor would need a new parser, not just a config edit -- see
the Phase 2 architecture review for why this distinction matters and must
not be overclaimed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, api_token_store

# TEMPORARY migration fallback (Block 2A). The API token now lives in <root>\secrets\api_token.dpapi
# (collector/api_token_store.py). Only while __version__ is BELOW this sunset release may a MISSING token file fall back to
# the SORTVIEW_API_TOKEN environment variable -- so a machine can be updated first and its token moved afterwards. Reaching
# the sunset release switches the fallback off by itself, and that release cannot be built until this constant and its
# branch are deleted: its value then IS the release version, which scripts/check_release_readiness.py (READY002) refuses to
# see copied anywhere but collector.__version__. tests/test_collector_config.py enforces the behavior too.
# bandit B105 false-positives on the three names below (they contain "TOKEN"): a release number and two source LABELS,
# never a credential -- same inline-nosec convention as collector/build_release.py.
ENV_TOKEN_FALLBACK_REMOVED_IN = "1.0.12"  # nosec B105
TOKEN_SOURCE_DPAPI = "dpapi"  # nosec B105
TOKEN_SOURCE_ENVIRONMENT = "environment"  # nosec B105


class ConfigError(Exception):
    """Base class for every error this module raises deliberately."""


@dataclass(frozen=True)
class SourceConfig:
    name: str
    path: str


@dataclass(frozen=True)
class CollectorConfig:
    customer_id: int
    branch_id: int
    api_url: str
    api_token: str = field(repr=False)
    sources: tuple[SourceConfig, ...]

    state_path: Path
    status_path: Path
    log_path: Path

    max_records_per_batch: int = 1000
    http_connect_timeout: float = 10.0
    http_read_timeout: float = 60.0

    # collector_installations.id of THIS deployed Collector. Required for
    # newly generated commercial configs (the installers write it and
    # finish-install validates it), but OPTIONAL HERE so an already-deployed
    # 1.0.2 config that predates it keeps loading and running: with None,
    # the heartbeat simply omits installation linkage (see
    # uploader.post_status) and the backend never touches
    # collector_installations for it. Never inferred from branch/hostname.
    installation_id: int | None = None

    # Local-only, privacy-safe run-history JSONL file (collector/run_audit.py).
    # Optional so every already-deployed config -- which predates this field
    # entirely -- keeps loading and running unmodified. load_config() below
    # always resolves this to a concrete Path (log_path's own directory,
    # `runs.jsonl`, unless the config explicitly overrides it); None here is
    # only the dataclass-level fallback for a CollectorConfig built directly
    # (e.g. in a test) rather than through load_config.
    run_audit_path: Path | None = None

    # Where api_token came from: "dpapi" (api_token.dpapi) or "environment" (the 1.0.11-only migration fallback). Never
    # the token itself. The default only applies to a CollectorConfig built directly (e.g. in a test).
    api_token_source: str = TOKEN_SOURCE_ENVIRONMENT

    def source(self, name: str) -> SourceConfig:
        for source_cfg in self.sources:
            if source_cfg.name == name:
                return source_cfg
        raise ConfigError(f"no source configured named {name!r}")


_REQUIRED_TOP_LEVEL_KEYS = [
    "customer_id", "branch_id", "api_url", "sources",
    "state_path", "status_path", "log_path",
]

_OPTIONAL_NUMERIC_KEYS = ["max_records_per_batch", "http_connect_timeout", "http_read_timeout"]


def _parse_source(raw: dict[str, Any]) -> SourceConfig:
    if "name" not in raw or "path" not in raw:
        raise ConfigError(f"source entry missing 'name' or 'path': {raw!r}")
    if not str(raw["name"]).strip():
        raise ConfigError(f"source entry has an empty 'name': {raw!r}")
    if not str(raw["path"]).strip():
        raise ConfigError(f"source entry has an empty 'path': {raw!r}")
    return SourceConfig(name=str(raw["name"]), path=str(raw["path"]))


def _parse_installation_id(raw: dict[str, Any]) -> int | None:
    """Absent (or JSON null) -> None: a legacy config. Anything present must
    be a genuine positive JSON integer -- bool is rejected explicitly (it is
    an int subclass), and a string/float is rejected rather than coerced, so
    a mistyped value fails loudly at config load instead of silently
    heartbeating as some other installation."""
    value = raw.get("installation_id")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"config 'installation_id' must be a positive integer, got {value!r}")
    return value


def _read_raw(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config document root must be an object")

    missing = [key for key in _REQUIRED_TOP_LEVEL_KEYS if key not in raw]
    if missing:
        raise ConfigError(f"config missing required key(s): {', '.join(missing)}")
    return raw


@dataclass(frozen=True)
class TokenSettings:
    """Everything needed to find and bind the API token file -- and nothing secret."""

    customer_id: int
    branch_id: int
    token_path: Path


def _token_settings(raw: dict[str, Any]) -> TokenSettings:
    override = raw.get("api_token_path")
    return TokenSettings(
        customer_id=int(raw["customer_id"]),
        branch_id=int(raw["branch_id"]),
        token_path=Path(str(override)) if override else api_token_store.default_path(raw["state_path"]),
    )


def load_token_settings(config_path: str | Path) -> TokenSettings:
    """The tenant and token-file path from a config, WITHOUT resolving the token -- for provisioning it (`api-token set`),
    which must work before any token exists."""
    return _token_settings(_read_raw(config_path))


def _release_tuple(version: str) -> tuple[int, ...]:
    """'2.3.4' -> (2, 3, 4). Only the numeric X.Y.Z core is compared; a suffix (-rc1, +build) is ignored."""
    core = version.split("-", 1)[0].split("+", 1)[0]
    return tuple(int(part) for part in core.split("."))


def resolve_api_token(settings: TokenSettings) -> tuple[str, str]:
    """The ONE place the Collector's API token is resolved. Returns (token, source).

    * api_token.dpapi present -> it must load (ACL verified, decrypts, well-formed, bound to this customer/branch) or this
      FAILS CLOSED; a present file NEVER falls back to the environment.
    * api_token.dpapi absent  -> SORTVIEW_API_TOKEN, but only below the ENV_TOKEN_FALLBACK_REMOVED_IN sunset release.
      "Absent" means only `token_missing` (nothing at the path at all): a path that cannot even be checked -- e.g. access
      denied to a non-elevated process -- is an error, never a reason to use the environment.
    * neither                 -> fails closed.
    """
    try:
        token = api_token_store.DpapiTokenStore(settings.token_path).load(settings.customer_id, settings.branch_id)
    except api_token_store.ApiTokenStoreError as exc:
        if exc.code != "token_missing":
            raise ConfigError(
                f"API token file {settings.token_path} could not be used ({exc.code}); "
                "the environment variable is NOT used while this file exists"
            ) from None
    else:
        return token, TOKEN_SOURCE_DPAPI

    if _release_tuple(__version__) < _release_tuple(ENV_TOKEN_FALLBACK_REMOVED_IN):
        env_token = os.environ.get("SORTVIEW_API_TOKEN")
        if env_token and env_token.strip():
            return env_token, TOKEN_SOURCE_ENVIRONMENT
        raise ConfigError(
            f"Missing API token: no {settings.token_path} and no SORTVIEW_API_TOKEN environment variable. "
            "Store it with `SortViewCollector.exe api-token set --config <path>`."
        )
    raise ConfigError(
        f"Missing API token: no {settings.token_path}. Store it with `SortViewCollector.exe api-token set --config <path>`."
    )


def load_config(config_path: str | Path) -> CollectorConfig:
    """Loads and validates the collector config from a JSON file.

    api_token is NEVER read from the JSON file -- it comes from resolve_api_token (the DPAPI-protected api_token.dpapi, or
    during the 1.0.11 migration only, the SORTVIEW_API_TOKEN environment variable), so a copy of the config file (support
    request, backup, version control) never carries a credential.
    """
    raw = _read_raw(config_path)
    api_token, api_token_source = resolve_api_token(_token_settings(raw))

    sources_raw = raw["sources"]
    if not isinstance(sources_raw, list) or not sources_raw:
        raise ConfigError("config 'sources' must be a non-empty list")
    sources = tuple(_parse_source(s) for s in sources_raw)

    names = [s.name for s in sources]
    if len(names) != len(set(names)):
        raise ConfigError(f"config 'sources' has duplicate names: {names}")

    kwargs: dict[str, Any] = {
        "customer_id": int(raw["customer_id"]),
        "branch_id": int(raw["branch_id"]),
        "api_url": str(raw["api_url"]).rstrip("/"),
        "api_token": api_token,
        "api_token_source": api_token_source,
        "sources": sources,
        "state_path": Path(raw["state_path"]),
        "status_path": Path(raw["status_path"]),
        "log_path": Path(raw["log_path"]),
        "installation_id": _parse_installation_id(raw),
        # Default: same directory as log_path, so an existing deployed
        # config (which has never heard of this key) gets local run-audit
        # logging automatically, right next to collector.log, with zero
        # config changes required.
        "run_audit_path": (
            Path(str(raw["run_audit_path"])) if raw.get("run_audit_path") else Path(raw["log_path"]).parent / "runs.jsonl"
        ),
    }

    defaults = CollectorConfig(**kwargs)
    for key in _OPTIONAL_NUMERIC_KEYS:
        value = raw.get(key, getattr(defaults, key))
        field_type = type(getattr(defaults, key))
        kwargs[key] = field_type(value)

    return CollectorConfig(**kwargs)
