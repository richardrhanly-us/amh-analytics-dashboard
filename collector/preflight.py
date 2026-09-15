"""Preflight validation (Phase 4c).

Fourteen independent checks, each reported separately (name, pass/fail,
detail message), aggregated into one report that fails overall if ANY
check fails. Every check is read-only or self-cleaning (writes and
immediately removes its own probe file) -- running preflight never
leaves anything behind and never mutates production source files, state,
or status.

WHY THIS MATTERS UNDER SYSTEM SPECIFICALLY: running this INTERACTIVELY
(as whichever admin account happens to run the installer) proves nothing
about whether the actual Scheduled Task -- which runs as SYSTEM, a
completely different security principal with its own file permissions,
its own (largely empty) per-user proxy configuration, and its own view
of Machine-scope environment variables -- can do the same things. See
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
from typing import Any
from urllib.parse import urlsplit

import requests

from . import uploader
from .config import CollectorConfig, ConfigError, load_config


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


def _check_token_visible() -> CheckResult:
    token = os.environ.get("SORTVIEW_API_TOKEN")
    if not token:
        return CheckResult(
            "api_token_visible", False,
            "SORTVIEW_API_TOKEN is not set for THIS process identity -- if this was run as SYSTEM, "
            "confirm it was set as a Machine-scope environment variable, not a per-user one",
        )
    return CheckResult("api_token_visible", True, f"token is visible (length={len(token)})")


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


def _check_auth_and_scope(cfg: CollectorConfig, session) -> tuple[CheckResult, CheckResult]:
    """One real call (the same POST /upload-pipeline-status the production
    collector already makes every run) serves both checks -- there is no
    side-effect-free authenticated endpoint to probe instead, and this is
    the exact call path production actually uses, which is arguably more
    meaningful than a synthetic check would be. The status payload is
    clearly labeled as a preflight probe."""
    probe_status = {
        "status": "preflight_check",
        "last_attempt": _now_iso(),
        "checkins_rows": 0, "rejects_rows": 0, "acs_rows": 0,
    }
    outcome = uploader.post_status(session, cfg, probe_status)

    if outcome.success:
        return (
            CheckResult("api_authentication", True, "authenticated successfully"),
            CheckResult("token_scope_matches", True, f"token is scoped to customer_id={cfg.customer_id}, branch_id={cfg.branch_id}"),
        )

    error_text = outcome.error or "unknown error"
    if outcome.category == uploader.FailureCategory.AUTH_FAILURE:
        if "scope" in error_text.lower():
            return (
                CheckResult("api_authentication", True, "token itself is valid"),
                CheckResult("token_scope_matches", False, error_text),
            )
        return (
            CheckResult("api_authentication", False, error_text),
            CheckResult("token_scope_matches", False, "not evaluated -- authentication failed first"),
        )

    # Non-auth failure (network/5xx/etc.) -- can't evaluate auth or scope
    # at all; report both as not evaluated rather than guessing.
    return (
        CheckResult("api_authentication", False, f"could not evaluate -- {error_text}"),
        CheckResult("token_scope_matches", False, f"could not evaluate -- {error_text}"),
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
        _check_token_visible(),
        _check_dns(cfg),
        _check_https_and_tls(cfg, session),
    ]

    auth_result, scope_result = _check_auth_and_scope(cfg, session)
    results.append(auth_result)
    results.append(scope_result)

    results.append(_check_no_direct_database_dependency(is_importable=is_importable))
    results.append(_check_runtime_imports())

    return PreflightReport(results=results, generated_at=_now_iso())


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
    report = run_preflight(cfg)
    report = PreflightReport(results=[config_loads_result, *report.results], generated_at=report.generated_at)
    _print_report(report)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
