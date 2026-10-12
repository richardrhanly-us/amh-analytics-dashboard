"""Preflight validation (Phase 4c).

Fourteen independent checks, each reported separately (name, pass/fail,
detail message), aggregated into one report that fails overall if ANY
check fails. Every check is read-only or self-cleaning (writes and
immediately removes its own probe file) -- running preflight never
leaves anything behind and never mutates production source files, state,
or status.

ONE DELIBERATE SERVER-SIDE EFFECT: the authentication/scope check is a real
POST /upload-pipeline-status (labeled status "preflight_check"), and when the
config carries an installation_id it also carries it. An accepted probe is a
confirmed installation contact like any scheduled-run heartbeat: the server
may move that installation provisioning -> active, stamps installed_at if it
is still unset (so installed_at is the first confirmed contact -- possibly this
preflight, before the Scheduled Task has ever run) and sets last_seen_at.

WHY THIS MATTERS UNDER SYSTEM SPECIFICALLY: running this INTERACTIVELY
(as whichever admin account happens to run the installer) proves nothing
about whether the actual Scheduled Task -- which runs as SYSTEM, a
completely different security principal with its own file permissions,
its own (largely empty) per-user proxy configuration, and its own
environment -- can do the same things. See
collector/deploy/run-preflight-as-system.ps1 for the mechanism that runs
THIS SAME module under the real SYSTEM identity via a temporary,
self-cleaning Scheduled Task. Nothing in this module assumes or hides
that distinction -- it only ever reports what the CURRENT process
actually observed, which is exactly why running it twice (once
interactively, once as SYSTEM) can produce two different, both-honest
reports.

PROXY/TLS HONESTY (Phase 3 correction, carried forward here): a passing
HTTPS/TLS check here proves THIS invocation, under THIS process identity,
successfully connected -- it never claims proxy or TLS behavior is
equivalent between an interactive account and SYSTEM. If municipal
network policy requires an explicit proxy, that must be configured for
whichever identity actually needs it (see docs) -- this module does not
attempt to auto-detect or auto-configure a proxy on the operator's
behalf.

NO DIRECT DATABASE CONNECTIVITY (check 13): this deliberately does NOT
attempt to reach a database host/port -- doing so would be pointless (the
collector should never need to) and could look like unwanted network
probing. Instead it verifies, introspectively, that the CURRENT
interpreter's own dependency closure has no database driver available at
all and no DATABASE_URL configured -- directly closing the loop on a real
finding from this project's own history (the live AMH agent venv was
found to have psycopg2-binary/SQLAlchemy installed with no code that
should need them).

CONTRACT V2 (`contract_mode: "v2"`): every check above still runs, unchanged,
and six more are appended (run_v2_checks). An absent `contract_mode` is v1 and
gets none of them -- no v2 file is opened and no v2 endpoint is called. Any
other value fails `config_loads` (exit 2). The v2 checks call the SAME
primitives a scheduled v2 run does, so preflight fails where the run would:

    v2_config_valid         v2_config.load_v2_config (key_id, timezone, limits)
    v2_sources_recognized   the run's own source-name rule (`unknown_source`)
    v2_data_dirs_writable   the folders of the v2 state/status/cache/quarantine
    v2_secret_usable        the secret exists, its ACL is verified, THIS process
                            can decrypt it, and it is bound to the config's key_id
    v2_rules_valid          v2_rules.load_rules
    v2_ingest_authorized    one POST /v2/status

A failing v2 check's detail starts with a FIXED CODE (`secret_missing`,
`rules_invalid`, `ingest_key_not_authorized`, `not_evaluated`, ...): a caller
relies on `name`, `passed` and that code, never on the prose after it. Nothing
here prints the key_id, the secret, a rule or a response body, and preflight
never creates or replaces a secret.

TWO SERVER PROBES. The POST /upload-pipeline-status probe stays exactly as it
is -- it is what activates the installation and stamps installed_at /
last_seen_at, which /v2/status never touches. POST /v2/status follows ONLY when
that probe passed and every local v2 check passed, and proves what nothing
else can: v2 ingest is enabled and the configured key is active for the
token's tenant.

A SECOND SERVER-SIDE EFFECT, v2 only: /v2/status stores a full heartbeat
snapshot on the ingest key, which is what the dashboard shows for a branch on
Contract v2. Preflight re-sends the health, error class, last success and
quarantine count of the local status_v2.json when a run has written one (run
and schedule diagnostics are omitted: they are not this invocation's to
report). On a fresh install there is nothing to re-send, so it sends the
smallest valid snapshot, `healthy`. THAT HEARTBEAT MEANS "v2 preflight
passed", NOT "a production run has happened".
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import requests

from . import uploader
from .config import TOKEN_SOURCE_DPAPI, CollectorConfig, ConfigError, load_config

if TYPE_CHECKING:  # the v2 modules are imported only inside the v2 checks: the v1 path never loads them
    from .v2_config import V2Config
    from .v2_keys import SecretStore
    from .v2_status import StatusSnapshot


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    results: list[CheckResult]
    generated_at: str

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "passed": self.passed,
            "checks": [{"name": r.name, "passed": r.passed, "detail": r.detail} for r in self.results],
        }


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _check_source_exists(cfg: CollectorConfig) -> CheckResult:
    missing = [s.name for s in cfg.sources if not Path(s.path).exists()]
    if missing:
        return CheckResult("source_paths_exist", False, f"missing: {', '.join(missing)}")
    return CheckResult("source_paths_exist", True, f"{len(cfg.sources)} source path(s) exist")


def _check_source_readable(cfg: CollectorConfig) -> CheckResult:
    unreadable = []
    for s in cfg.sources:
        if not Path(s.path).exists():
            continue  # already reported by _check_source_exists
        try:
            with open(s.path, "rb") as f:
                f.read(1)
        except OSError as exc:
            unreadable.append(f"{s.name} ({exc})")
    if unreadable:
        return CheckResult("source_paths_readable", False, f"unreadable: {'; '.join(unreadable)}")
    return CheckResult("source_paths_readable", True, "all existing source paths are readable")


def _check_dir_writable(name: str, target_dir: Path) -> CheckResult:
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target_dir), prefix=".preflight-probe-", suffix=".tmp")
        os.close(fd)
        os.remove(tmp_name)
    except OSError as exc:
        return CheckResult(name, False, f"cannot write to {target_dir}: {exc}")
    return CheckResult(name, True, f"{target_dir} is writable")


def _check_token_source(cfg: CollectorConfig) -> CheckResult:
    """Reports WHERE load_config's single token resolution found the token -- never the token, its length or any hash.
    Preflight reads the token exactly as the runtime does (cfg.api_token), with no second read of its own. api_token.dpapi
    is the only valid source; a missing or unusable file normally fails earlier, in load_config (config_loads)."""
    if not cfg.api_token or not cfg.api_token.strip():
        return CheckResult("api_token_source", False, "no API token was resolved for THIS process identity")
    if cfg.api_token_source == TOKEN_SOURCE_DPAPI:
        return CheckResult("api_token_source", True, "token source: dpapi (api_token.dpapi, ACL verified)")
    return CheckResult("api_token_source", False, f"unrecognized token source {cfg.api_token_source!r}")


def _check_dns(cfg: CollectorConfig) -> CheckResult:
    parsed = urlsplit(cfg.api_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return CheckResult("dns_resolution", False, f"could not parse a hostname from api_url {cfg.api_url!r}")
    try:
        socket.getaddrinfo(host, port)
    except OSError as exc:
        return CheckResult("dns_resolution", False, f"could not resolve {host}: {exc}")
    return CheckResult("dns_resolution", True, f"resolved {host}")


def _check_https_and_tls(cfg: CollectorConfig, session) -> CheckResult:
    try:
        response = session.get(cfg.api_url + "/", timeout=(cfg.http_connect_timeout, cfg.http_read_timeout))
    except requests.exceptions.SSLError as exc:
        return CheckResult(
            "https_tls_connection", False,
            f"TLS certificate validation failed -- if this network uses a TLS-inspecting proxy, its "
            f"root CA may need to be installed in the Windows trust store for this process identity: {exc}",
        )
    except requests.RequestException as exc:
        return CheckResult("https_tls_connection", False, f"could not connect: {exc}")
    return CheckResult("https_tls_connection", True, f"connected, HTTP {response.status_code}")


def _check_auth_and_scope(cfg: CollectorConfig, session) -> tuple[CheckResult, CheckResult, CheckResult]:
    """One real call (the same POST /upload-pipeline-status the production
    collector already makes every run) serves all three checks -- there is
    no side-effect-free authenticated endpoint to probe instead, and this
    is the exact call path production actually uses, which is arguably more
    meaningful than a synthetic check would be. The status payload is
    clearly labeled as a preflight probe.

    Because it is the production call, a config that carries an
    installation_id also sends it (with the running version): a passing
    preflight therefore also proves the installation_id is accepted by the
    server. An accepted probe is the first confirmed contact with the
    installation, exactly like a scheduled-run heartbeat: it may move a
    `provisioning` installation to `active`, stamps `installed_at` if unset
    (first confirmed contact -- not specifically the first scheduled run) and
    sets `last_seen_at` (most recent confirmed contact). That is deliberate:
    it catches a mistyped installation_id at install time rather than letting
    the Collector upload fine forever while never linking to its
    installation record."""
    probe_status = {
        "status": "preflight_check",
        "last_attempt": _now_iso(),
        "checkins_rows": 0, "rejects_rows": 0, "acs_rows": 0,
    }
    outcome = uploader.post_status(session, cfg, probe_status)

    if outcome.success:
        if cfg.installation_id is None:
            installation_result = CheckResult(
                "installation_id_accepted", True,
                "config has no installation_id (legacy config) -- installation lifecycle reporting is disabled",
            )
        else:
            installation_result = CheckResult(
                "installation_id_accepted", True, f"installation_id={cfg.installation_id} accepted by the server"
            )
        return (
            CheckResult("api_authentication", True, "authenticated successfully"),
            CheckResult("token_scope_matches", True, f"token is scoped to customer_id={cfg.customer_id}, branch_id={cfg.branch_id}"),
            installation_result,
        )

    error_text = outcome.error or "unknown error"
    if outcome.category == uploader.FailureCategory.AUTH_FAILURE:
        if "installation" in error_text.lower():
            # The server checks the token and its scope BEFORE the
            # installation, so reaching this rejection means both passed.
            return (
                CheckResult("api_authentication", True, "token itself is valid"),
                CheckResult("token_scope_matches", True, f"token is scoped to customer_id={cfg.customer_id}, branch_id={cfg.branch_id}"),
                CheckResult(
                    "installation_id_accepted", False,
                    f"installation_id={cfg.installation_id} was rejected -- confirm it is the Installation ID "
                    f"shown in Super Admin for this Collector, and that the installation is not inactive/retired: {error_text}",
                ),
            )
        if "scope" in error_text.lower():
            return (
                CheckResult("api_authentication", True, "token itself is valid"),
                CheckResult("token_scope_matches", False, error_text),
                CheckResult("installation_id_accepted", False, "not evaluated -- token scope check failed first"),
            )
        return (
            CheckResult("api_authentication", False, error_text),
            CheckResult("token_scope_matches", False, "not evaluated -- authentication failed first"),
            CheckResult("installation_id_accepted", False, "not evaluated -- authentication failed first"),
        )

    # Non-auth failure (network/5xx/etc.) -- can't evaluate auth or scope
    # at all; report all as not evaluated rather than guessing.
    return (
        CheckResult("api_authentication", False, f"could not evaluate -- {error_text}"),
        CheckResult("token_scope_matches", False, f"could not evaluate -- {error_text}"),
        CheckResult("installation_id_accepted", False, f"could not evaluate -- {error_text}"),
    )


def _default_is_importable(module_name: str) -> bool:
    try:
        __import__(module_name)
    except ImportError:
        return False
    return True


def _check_no_direct_database_dependency(*, is_importable=_default_is_importable) -> CheckResult:
    """is_importable is injectable specifically so this check's PASS/FAIL
    logic is testable independent of whatever happens to be installed in
    the ambient interpreter running the tests (this repo's own shared dev
    venv has psycopg2-binary/SQLAlchemy installed for the BACKEND -- that
    is expected and correct for repo development, and must not make this
    check's own unit tests depend on a clean, collector-only venv to pass).
    The real default (_default_is_importable) is what actually runs via
    run_preflight()/main() against the real installed collector venv."""
    problems = []
    for module_name in ("psycopg2", "sqlalchemy"):
        if not is_importable(module_name):
            continue
        problems.append(f"{module_name} is importable in this interpreter (unexpected dependency bloat)")
    if os.environ.get("DATABASE_URL"):
        problems.append("DATABASE_URL is set for this process (the collector must never need it)")

    if problems:
        return CheckResult("no_direct_database_dependency", False, "; ".join(problems))
    return CheckResult(
        "no_direct_database_dependency", True,
        "no database driver importable and DATABASE_URL is not set -- HTTPS-only, as required",
    )


def _check_runtime_imports() -> CheckResult:
    try:
        from . import config as _config_mod  # noqa: F401
        from . import reader as _reader_mod  # noqa: F401
        from . import run as _run_mod  # noqa: F401
        from . import state as _state_mod  # noqa: F401
        from . import uploader as _uploader_mod  # noqa: F401
    except ImportError as exc:
        return CheckResult("collector_runtime_imports", False, f"import failed: {exc}")
    return CheckResult(
        "collector_runtime_imports", True,
        f"all collector modules import cleanly (python={sys.executable}, version={sys.version.split()[0]})",
    )


def run_preflight(cfg: CollectorConfig, *, session=None, is_importable=_default_is_importable) -> PreflightReport:
    session = session or uploader.build_session()
    results: list[CheckResult] = [
        _check_source_exists(cfg),
        _check_source_readable(cfg),
        _check_dir_writable("state_dir_writable", cfg.state_path.parent),
        _check_dir_writable("status_dir_writable", cfg.status_path.parent),
        _check_dir_writable("log_dir_writable", cfg.log_path.parent),
        _check_token_source(cfg),
        _check_dns(cfg),
        _check_https_and_tls(cfg, session),
    ]

    auth_result, scope_result, installation_result = _check_auth_and_scope(cfg, session)
    results.append(auth_result)
    results.append(scope_result)
    results.append(installation_result)

    results.append(_check_no_direct_database_dependency(is_importable=is_importable))
    results.append(_check_runtime_imports())

    return PreflightReport(results=results, generated_at=_now_iso())


# --- Contract v2 (see the module docstring's CONTRACT V2 section) -----------

V2_CHECK_NAMES = (
    "v2_config_valid", "v2_sources_recognized", "v2_data_dirs_writable", "v2_secret_usable", "v2_rules_valid",
    "v2_ingest_authorized",
)
_LEGACY_PROBE_CHECKS = ("api_authentication", "token_scope_matches", "installation_id_accepted")
_STATUS_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # v2_events.format_time: how status_v2.json spells an instant


def _check_v2_sources(cfg: CollectorConfig) -> CheckResult:
    from .v2_transform import SOURCE_ORDER

    unknown = [s.name for s in cfg.sources if s.name not in SOURCE_ORDER]
    if unknown:
        return CheckResult("v2_sources_recognized", False, f"unknown_source: {', '.join(unknown)}")
    return CheckResult("v2_sources_recognized", True, f"{len(cfg.sources)} source name(s) recognized by Contract v2")


def _check_v2_data_dirs(v2: V2Config) -> CheckResult:
    folders = sorted({p.parent for p in (v2.state_path, v2.status_path, v2.patron_cache_path, v2.quarantine_path)})
    for folder in folders:
        result = _check_dir_writable("v2_data_dirs_writable", folder)
        if not result.passed:
            return CheckResult("v2_data_dirs_writable", False, f"not_writable: {result.detail}")
    return CheckResult("v2_data_dirs_writable", True, f"{len(folders)} v2 data folder(s) writable")


def _check_v2_secret(v2: V2Config, store: SecretStore | None) -> CheckResult:
    """The same three steps, in the same order, as v2_run.build_runtime. The secret itself is never bound to a name
    here: it is loaded to prove it can be, and dropped."""
    from .v2_keys import DpapiSecretStore, SecretStoreError, require_protected
    from .v2_safe_errors import CollectorV2Error, describe

    store = store or DpapiSecretStore(v2.secret_path)
    try:
        if not store.exists():
            raise SecretStoreError("secret_missing")
        require_protected(store.acl_state())
        store.load(v2.key_id)
    except CollectorV2Error as exc:
        return CheckResult("v2_secret_usable", False, exc.code)
    except Exception as exc:  # described by type and location only, never by message
        return CheckResult("v2_secret_usable", False, f"secret_check_failed: {describe(exc)}")
    return CheckResult(
        "v2_secret_usable", True, "secret present, bound to the configured key_id, and its ACL is verified protected"
    )


def _check_v2_rules(v2: V2Config) -> CheckResult:
    """v2_rules.load_rules itself -- the run's one reader, parser and validator -- with a throwaway key, as the dry run
    does: whether a rules file is valid does not depend on the key, so this check needs no secret."""
    import secrets

    from .v2_identity import derive_subkeys
    from .v2_rules import load_rules
    from .v2_safe_errors import CollectorV2Error, describe

    try:
        load_rules(v2.rules_path, derive_subkeys(secrets.token_bytes(32)))
    except CollectorV2Error as exc:
        return CheckResult("v2_rules_valid", False, exc.code)
    except Exception as exc:  # described by type and location only, never by message
        return CheckResult("v2_rules_valid", False, f"rules_check_failed: {describe(exc)}")
    return CheckResult("v2_rules_valid", True, "the local classification rules load and validate")


def _v2_probe_snapshot(v2: V2Config) -> StatusSnapshot:
    """What the /v2/status probe sends. The last run's own health, error class, last success, quarantine count and
    start time when status_v2.json holds a usable one -- so an established install's dashboard state is re-sent, not
    replaced -- and otherwise the smallest valid snapshot, `healthy`. Run and schedule diagnostics are never sent."""
    from . import state
    from .v2_events import HEALTH_STATUSES, LAST_ERROR_CLASSES
    from .v2_status import MAX_COUNTER, StatusSnapshot, parse_prior

    prior = state.load_status(v2.status_path)
    health = prior.get("health_status")
    if not isinstance(health, str) or health not in HEALTH_STATUSES:
        return StatusSnapshot(status="healthy", last_error_class=None, quarantined_count=0, last_success_at=None,
                              watcher_last_active_at=None)

    error_class = prior.get("last_error_class")
    quarantined = prior.get("quarantined_count")
    last_attempt = prior.get("last_attempt")
    try:
        active_at = (datetime.strptime(last_attempt, _STATUS_TIME_FORMAT).replace(tzinfo=UTC)
                     if isinstance(last_attempt, str) else None)
    except ValueError:
        active_at = None
    return StatusSnapshot(
        status=health,
        last_error_class=error_class if isinstance(error_class, str) and error_class in LAST_ERROR_CLASSES else None,
        quarantined_count=(quarantined if isinstance(quarantined, int) and not isinstance(quarantined, bool)
                           and 0 <= quarantined <= MAX_COUNTER else 0),
        last_success_at=parse_prior(prior)[1],
        watcher_last_active_at=active_at,
    )


def _check_v2_ingest(cfg: CollectorConfig, v2: V2Config, session) -> CheckResult:
    """One POST /v2/status through the run's own client, which classifies by status code alone and keeps no body. It is
    only called after the /upload-pipeline-status probe proved the token, its scope, the tenant and the installation,
    so a 403 here can only be the ingest key."""
    from . import v2_uploader

    name = "v2_ingest_authorized"
    outcome = v2_uploader.post_status(session, cfg, v2, _v2_probe_snapshot(v2))
    code = outcome.status_code
    if outcome.ok:
        return CheckResult(name, True, "authorized: v2 ingest is enabled and the configured ingest key is active for this tenant")
    if code == 401:
        return CheckResult(name, False, "authentication_failed: the server rejected the API token (HTTP 401)")
    if code == 403:
        return CheckResult(
            name, False,
            "ingest_key_not_authorized: the configured v2.key_id is not an active ingest key of this tenant (HTTP 403)",
        )
    if code in (404, 405):
        return CheckResult(
            name, False,
            f"v2_unavailable: the server has no v2 ingest route -- Contract v2 is not enabled there, or api_url is wrong (HTTP {code})",
        )
    if code == 422:
        return CheckResult(name, False, "status_request_invalid: the server refused the v2 status request (HTTP 422)")
    if code is None or code == 200 or code == 429 or 500 <= code <= 599:
        seen = "no response" if code is None else f"HTTP {code}"
        return CheckResult(name, False, f"server_unavailable: could not evaluate -- {seen}")
    return CheckResult(name, False, f"unexpected_response: could not evaluate -- HTTP {code}")


def run_v2_checks(cfg: CollectorConfig, config_path: str | Path, *, session, legacy_probe_passed: bool,
                  store: SecretStore | None = None) -> list[CheckResult]:
    """The six Contract v2 checks, always all six and always in V2_CHECK_NAMES order. `legacy_probe_passed` is whether
    the /upload-pipeline-status probe's three checks all passed; `store` is injectable for tests only."""
    from .v2_config import load_v2_config

    def not_evaluated(names: tuple[str, ...], why: str) -> list[CheckResult]:
        return [CheckResult(name, False, f"not_evaluated: {why}") for name in names]

    try:
        v2 = load_v2_config(config_path, require=True)
    except ConfigError as exc:  # names the setting, never its value
        return [CheckResult("v2_config_valid", False, f"config_invalid: {exc}"),
                *not_evaluated(V2_CHECK_NAMES[1:], "the v2 configuration is invalid")]
    if v2 is None:  # unreachable with require=True; keeps the type honest without an assert
        return not_evaluated(V2_CHECK_NAMES, "the v2 configuration is missing")

    results = [
        CheckResult("v2_config_valid", True, "the v2 section loads (key_id, timezone and limits are valid)"),
        _check_v2_sources(cfg),
        _check_v2_data_dirs(v2),
        _check_v2_secret(v2, store),
        _check_v2_rules(v2),
    ]
    # The heartbeat this probe stores says "v2 preflight passed": it is not sent for an install that did not.
    if not legacy_probe_passed:
        results += not_evaluated(V2_CHECK_NAMES[-1:], "the /upload-pipeline-status probe did not pass")
    elif not all(r.passed for r in results):
        results += not_evaluated(V2_CHECK_NAMES[-1:], "a local v2 check failed")
    else:
        results.append(_check_v2_ingest(cfg, v2, session))
    return results


def _print_report(report: PreflightReport) -> None:
    print(f"SortView Collector preflight -- {report.generated_at}")
    print("=" * 78)
    for r in report.results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.name}: {r.detail}")
    print("=" * 78)
    print("OVERALL: PASS" if report.passed else "OVERALL: FAIL")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SortView Collector -- preflight validation")
    parser.add_argument("--config", required=True, help="Path to the collector config JSON file")
    parser.add_argument("--output", help="Optional path to also write the report as JSON")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        # After load_config, so a v1 config's own errors are reported exactly as before. Absent means "v1".
        from .v2_config import read_contract_mode

        mode = read_contract_mode(args.config)
    except ConfigError as exc:
        # 2 = config problem, matching collector/run.py's own exit-code
        # convention -- distinct from 1 (checks ran, something failed).
        result = CheckResult("config_loads", False, str(exc))
        report = PreflightReport(results=[result], generated_at=_now_iso())
        _print_report(report)
        if args.output:
            Path(args.output).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        return 2

    config_loads_result = CheckResult("config_loads", True, f"loaded {args.config}")
    if mode == "v2":
        session = uploader.build_session()  # one session for both server probes
        report = run_preflight(cfg, session=session)
        legacy_probe_passed = all(r.passed for r in report.results if r.name in _LEGACY_PROBE_CHECKS)
        v2_results = run_v2_checks(cfg, args.config, session=session, legacy_probe_passed=legacy_probe_passed)
        report = PreflightReport(results=[*report.results, *v2_results], generated_at=report.generated_at)
    else:
        report = run_preflight(cfg)
    report = PreflightReport(results=[config_loads_result, *report.results], generated_at=report.generated_at)
    _print_report(report)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
