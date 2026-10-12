"""Tests for collector/preflight.py -- SortView Collector v1 (Phase 4c).

Central property under test: preflight only ever reports what THIS
process observed -- see module docstring's SYSTEM-context caveat. These
tests exercise the check LOGIC; they cannot and do not claim to prove
real SYSTEM-context or real network/TLS behavior -- that remains a
required onsite validation step (see
collector/deploy/run-preflight-as-system.ps1 and the Phase 4c report).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import requests
from collector_token_support import break_api_token, store_api_token

from collector import preflight as preflight_module
from collector.config import CollectorConfig, SourceConfig
from collector.preflight import main, run_preflight


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {"status": "success"}
        self.text = text or json.dumps(self._json_body)

    def json(self):
        return self._json_body


class FakeSession:
    """Supports both .get() (HTTPS/TLS check) and .post() (auth/scope
    check via collector.uploader.post_status). Scripted responses are
    keyed by HTTP method so tests can control each independently."""

    def __init__(self, get_response=None, post_response=None, get_exception=None):
        self.get_response = get_response or _FakeResponse(200)
        self.post_response = post_response or _FakeResponse(200, {"status": "success"})
        self.get_exception = get_exception
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    def get(self, url, timeout=None):
        self.get_calls.append(url)
        if self.get_exception is not None:
            raise self.get_exception
        return self.get_response

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append((url, json))
        return self.post_response


def _never_importable(name: str) -> bool:
    """Injected into run_preflight's is_importable param for tests that
    want the no_direct_database_dependency check to PASS -- deliberately
    decoupled from whatever happens to actually be installed in the
    ambient venv running these tests (this repo's own dev venv has
    psycopg2-binary/SQLAlchemy installed for the unrelated backend)."""
    return False


def _cfg(tmp_path, **overrides):
    kwargs = {
        "customer_id": 1,
        "branch_id": 1,
        # localhost, not example.invalid (an RFC 2606 reserved TLD that
        # is DESIGNED to never resolve) -- always resolvable, even fully
        # offline, so the DNS check can genuinely pass in these
        # happy-path tests without depending on real internet access.
        "api_url": "https://localhost",
        "api_token": "test-token",
        "sources": (
            SourceConfig(name="checkins", path=str(tmp_path / "Checkins.txt")),
        ),
        "state_path": tmp_path / "data" / "state.json",
        "status_path": tmp_path / "data" / "status.json",
        "log_path": tmp_path / "logs" / "collector.log",
    }
    kwargs.update(overrides)
    return CollectorConfig(**kwargs)


def _result(report, name):
    return next(r for r in report.results if r.name == name)


# --- aggregation --------------------------------------------------------


def test_all_checks_pass_report_is_overall_pass(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    report = run_preflight(cfg, session=FakeSession(), is_importable=_never_importable)

    assert report.passed is True
    assert all(r.passed for r in report.results)


def test_one_failing_check_makes_overall_report_fail(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, api_token="   ")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    report = run_preflight(cfg, session=FakeSession())

    assert report.passed is False
    assert _result(report, "api_token_source").passed is False


def test_report_serializes_to_dict_with_all_checks(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    report = run_preflight(cfg, session=FakeSession(), is_importable=_never_importable)
    doc = report.to_dict()

    assert doc["passed"] is True
    assert len(doc["checks"]) == len(report.results)
    assert {"name", "passed", "detail"} <= doc["checks"][0].keys()


# --- missing / unreadable source --------------------------------------------


def test_missing_source_fails_source_exists_check(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)  # Checkins.txt never created

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "source_paths_exist").passed is False
    assert "checkins" in _result(report, "source_paths_exist").detail


def test_unreadable_source_fails_readable_check(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    # A directory at the configured path exists but can never be opened
    # as a file -- a portable, reliable way to simulate "exists but not
    # readable" without platform-specific permission manipulation.
    (tmp_path / "Checkins.txt").mkdir()

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "source_paths_exist").passed is True
    assert _result(report, "source_paths_readable").passed is False


# --- unwritable state/status/log directories --------------------------------


def test_unwritable_state_dir_fails_its_check(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    # Point state_path at a location that cannot be created as a
    # directory (a file already occupies that path segment).
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    cfg = _cfg(tmp_path, state_path=blocker / "sub" / "state.json")

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "state_dir_writable").passed is False


def test_writable_status_and_log_dirs_pass(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "status_dir_writable").passed is True
    assert _result(report, "log_dir_writable").passed is True
    # Probe files must not be left behind.
    assert list(cfg.status_path.parent.iterdir()) == []


# --- token visibility ---------------------------------------------------


def test_a_blank_resolved_token_fails_the_token_source_check(tmp_path):
    cfg = _cfg(tmp_path, api_token="")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    report = run_preflight(cfg, session=FakeSession())

    result = _result(report, "api_token_source")
    assert result.passed is False
    assert "THIS process identity" in result.detail  # actionable hint, not a generic message


def test_the_token_source_check_reports_where_the_token_came_from_and_nothing_about_it(tmp_path):
    token = "CANARY-PREFLIGHT-TOKEN-0000000001"
    cfg = _cfg(tmp_path, api_token=token, api_token_source="dpapi")

    result = _result(run_preflight(cfg, session=FakeSession()), "api_token_source")

    assert result.passed is True
    assert result.detail.startswith("token source: dpapi")
    assert token not in result.detail and str(len(token)) not in result.detail and "length" not in result.detail


def test_a_config_built_without_naming_a_source_is_dpapi():
    # The dataclass default is the only source there is -- never a second, weaker one.
    assert CollectorConfig.__dataclass_fields__["api_token_source"].default == "dpapi"


@pytest.mark.parametrize("source", ["environment", "clipboard", "", "DPAPI"])
def test_dpapi_is_the_only_token_source_that_passes(tmp_path, source):
    # "environment" was a PASS in the one migration release; it is now just another unrecognized source.
    result = _result(run_preflight(_cfg(tmp_path, api_token_source=source), session=FakeSession()), "api_token_source")

    assert result.passed is False
    assert "unrecognized token source" in result.detail and "migration" not in result.detail


def test_preflight_reads_the_token_only_through_load_config_never_the_environment_itself(tmp_path, monkeypatch):
    # Preflight must see exactly what the runtime sees: the token load_config resolved. The environment variable is
    # not a token source at all, so the module never even names it.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "a-different-environment-token-0001")
    session = FakeSession()
    cfg = _cfg(tmp_path, api_token="resolved-by-load-config-0001", api_token_source="dpapi")

    assert _result(run_preflight(cfg, session=session), "api_token_source").passed is True
    assert _result(run_preflight(cfg, session=session), "api_authentication").passed is True
    source = Path(preflight_module.__file__).read_text(encoding="utf-8")
    assert "SORTVIEW_API_TOKEN" not in source and not re.search(r"migration fallback", source, re.IGNORECASE)


def test_preflight_main_with_a_dpapi_token_authenticates_with_it_and_says_so(tmp_path, monkeypatch, capsys):
    # The whole CLI path: load_config resolves the token from api_token.dpapi (store stubbed -- no real DPAPI here),
    # and the auth probe carries exactly that token -- never the environment's, even when one is set.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "valid-environment-token-0001")
    store_api_token(monkeypatch, "from-dpapi-token-000000001")
    seen_headers: list[dict] = []

    class Recording(FakeSession):
        def post(self, url, json=None, headers=None, timeout=None):
            seen_headers.append(headers)
            return self.post_response

    monkeypatch.setattr("collector.preflight.uploader.build_session", lambda: Recording())
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    main(["--config", str(config_path)])
    out = capsys.readouterr().out

    assert "[PASS] api_token_source: token source: dpapi" in out
    assert seen_headers and seen_headers[0]["Authorization"] == "Bearer from-dpapi-token-000000001"
    assert "from-dpapi-token" not in out and "valid-environment-token" not in out


def test_preflight_main_does_not_fall_back_to_the_environment_when_the_token_file_is_damaged(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "valid-environment-token-0001")
    break_api_token(monkeypatch, "token_unreadable")
    session = FakeSession()
    monkeypatch.setattr("collector.preflight.uploader.build_session", lambda: session)

    assert main(["--config", str(_write_config(tmp_path))]) == 2
    out = capsys.readouterr().out
    assert "[FAIL] config_loads" in out and "token_unreadable" in out
    assert session.post_calls == []  # no auth probe was ever sent with the environment token


def test_preflight_main_fails_when_there_is_no_token_file_even_with_a_valid_environment_token(tmp_path, monkeypatch, capsys):
    # The updaters rely on exactly this: a new runtime on a machine with no api_token.dpapi fails verification.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "valid-environment-token-0001")
    session = FakeSession()
    monkeypatch.setattr("collector.preflight.uploader.build_session", lambda: session)

    assert main(["--config", str(_write_config(tmp_path))]) == 2  # nothing under tmp_path/secrets
    out = capsys.readouterr().out
    assert "[FAIL] config_loads" in out and "Missing API token" in out and "api_token.dpapi" in out
    assert "valid-environment-token" not in out and session.post_calls == []


# --- DNS failure ----------------------------------------------------------


def test_dns_failure_fails_its_check_only(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    def boom(host, port):
        raise OSError("Name or service not known")

    monkeypatch.setattr("collector.preflight.socket.getaddrinfo", boom)

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "dns_resolution").passed is False


# --- HTTPS / TLS failure -----------------------------------------------------


def test_connection_failure_fails_https_check(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    session = FakeSession(get_exception=requests.ConnectionError("no route to host"))
    report = run_preflight(cfg, session=session)

    assert _result(report, "https_tls_connection").passed is False


def test_tls_certificate_failure_is_reported_distinctly(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    session = FakeSession(get_exception=requests.exceptions.SSLError("certificate verify failed"))
    report = run_preflight(cfg, session=session)

    result = _result(report, "https_tls_connection")
    assert result.passed is False
    assert "certificate" in result.detail.lower() or "TLS" in result.detail


# --- auth failure / scope mismatch -------------------------------------------


def test_auth_failure_fails_both_auth_and_scope_checks(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    session = FakeSession(post_response=_FakeResponse(401, text="Invalid agent token"))
    report = run_preflight(cfg, session=session)

    assert _result(report, "api_authentication").passed is False
    assert _result(report, "token_scope_matches").passed is False


def test_scope_mismatch_distinguishes_valid_token_from_wrong_scope(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    session = FakeSession(
        post_response=_FakeResponse(403, text="Token scope does not match customer_id / branch_id")
    )
    report = run_preflight(cfg, session=session)

    # The token itself authenticated -- it's specifically the SCOPE that's
    # wrong. This distinction is exactly what lets an operator tell "wrong
    # token" apart from "right token, wrong customer_id/branch_id config".
    assert _result(report, "api_authentication").passed is True
    assert _result(report, "token_scope_matches").passed is False
    assert "customer_id" in _result(report, "token_scope_matches").detail


def test_auth_check_probe_payload_is_clearly_labeled(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    session = FakeSession()
    run_preflight(cfg, session=session)

    assert len(session.post_calls) == 1
    assert session.post_calls[0][1]["status"] == "preflight_check"


# --- no direct database dependency ------------------------------------------


def test_no_database_dependency_check_passes_when_database_url_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    report = run_preflight(cfg, session=FakeSession(), is_importable=_never_importable)

    assert _result(report, "no_direct_database_dependency").passed is True


def test_no_database_dependency_check_fails_when_database_url_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "no_direct_database_dependency").passed is False


# --- runtime imports ----------------------------------------------------


def test_runtime_imports_check_passes_in_a_healthy_install(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "collector_runtime_imports").passed is True


# --- CLI main() ----------------------------------------------------------


def _write_config(tmp_path, **overrides):
    doc = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://example.invalid",
        "sources": [{"name": "checkins", "path": str(tmp_path / "Checkins.txt")}],
        "state_path": str(tmp_path / "data" / "state.json"),
        "status_path": str(tmp_path / "data" / "status.json"),
        "log_path": str(tmp_path / "logs" / "collector.log"),
    }
    doc.update(overrides)
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_main_returns_2_on_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "valid-environment-token-0001")  # valid, and irrelevant: no api_token.dpapi
    config_path = _write_config(tmp_path)

    exit_code = main(["--config", str(config_path)])
    assert exit_code == 2  # matches collector/run.py's config-error convention


def test_main_writes_json_output_when_requested(tmp_path, monkeypatch):
    # This test verifies --output's JSON structure specifically, not
    # that every check passes -- the real ambient venv running these
    # tests is this repo's shared dev venv, which legitimately has
    # psycopg2-binary/SQLAlchemy installed for the unrelated backend, so
    # no_direct_database_dependency genuinely (and correctly) fails here.
    # main() has no test-only is_importable injection point (it's a
    # production CLI, not meant to grow test-only parameters) -- see
    # test_all_checks_pass_report_is_overall_pass for the isolated,
    # fully-controlled version of "does everything pass" via
    # run_preflight() directly.
    store_api_token(monkeypatch, "test-token")
    monkeypatch.setattr("collector.preflight.uploader.build_session", lambda: FakeSession())
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    output_path = tmp_path / "preflight-result.json"

    main(["--config", str(config_path), "--output", str(output_path)])

    assert output_path.exists()
    doc = json.loads(output_path.read_text(encoding="utf-8"))
    assert "passed" in doc
    assert any(c["name"] == "config_loads" and c["passed"] for c in doc["checks"])
    assert any(c["name"] == "source_paths_exist" and c["passed"] for c in doc["checks"])


def test_main_returns_nonzero_when_a_check_fails(tmp_path, monkeypatch):
    # A missing token trips collector.config.load_config itself (exit 2,
    # tested separately in test_main_returns_2_on_config_error) --
    # config loading REQUIRES the token to already be set, so
    # api_token_source can never actually be what fails via main()'s
    # own CLI path. This test instead exercises a genuine preflight-level
    # failure: source file never created.
    store_api_token(monkeypatch, "test-token")
    monkeypatch.setattr("collector.preflight.uploader.build_session", lambda: FakeSession())
    config_path = _write_config(tmp_path)
    # Checkins.txt deliberately never created.

    exit_code = main(["--config", str(config_path)])
    assert exit_code == 1


# --- installation_id check ------------------------------------------------


def test_preflight_probe_carries_the_installation_identity_when_configured(tmp_path, monkeypatch):
    from collector import __version__

    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    session = FakeSession()

    run_preflight(_cfg(tmp_path, installation_id=41), session=session)

    payload = session.post_calls[0][1]
    assert payload["installation_id"] == 41
    assert payload["collector_version"] == __version__
    assert payload["status"] == "preflight_check"


def test_preflight_probe_for_a_legacy_config_has_no_installation_fields(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    session = FakeSession()

    report = run_preflight(_cfg(tmp_path), session=session)

    payload = session.post_calls[0][1]
    assert "installation_id" not in payload and "collector_version" not in payload
    result = _result(report, "installation_id_accepted")
    assert result.passed is True
    assert "legacy" in result.detail


def test_accepted_installation_id_passes_the_installation_check(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    report = run_preflight(_cfg(tmp_path, installation_id=41), session=FakeSession())

    result = _result(report, "installation_id_accepted")
    assert result.passed is True
    assert "41" in result.detail


def test_rejected_installation_id_fails_only_the_installation_check(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    session = FakeSession(post_response=_FakeResponse(
        403, text='{"detail": "Collector installation is not authorized to report status"}'
    ))

    report = run_preflight(_cfg(tmp_path, installation_id=41), session=session)

    # The server checks token and scope BEFORE the installation, so both passed.
    assert _result(report, "api_authentication").passed is True
    assert _result(report, "token_scope_matches").passed is True
    result = _result(report, "installation_id_accepted")
    assert result.passed is False
    assert "Installation ID" in result.detail and "Super Admin" in result.detail
    assert report.passed is False


def test_scope_mismatch_leaves_the_installation_check_not_evaluated(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    session = FakeSession(post_response=_FakeResponse(403, text="Token scope does not match customer_id / branch_id"))

    report = run_preflight(_cfg(tmp_path, installation_id=41), session=session)

    assert _result(report, "token_scope_matches").passed is False
    result = _result(report, "installation_id_accepted")
    assert result.passed is False and "not evaluated" in result.detail


def test_network_failure_leaves_the_installation_check_not_evaluated(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    session = FakeSession(post_response=_FakeResponse(503, text="down"))

    report = run_preflight(_cfg(tmp_path, installation_id=41), session=session)

    result = _result(report, "installation_id_accepted")
    assert result.passed is False and "could not evaluate" in result.detail


# =============================================================================
# Contract v2 (`contract_mode: "v2"`): six more checks, appended to the ones
# above. Everything above is v1 and must keep passing untouched.
# =============================================================================

import base64
from datetime import UTC, datetime, timedelta

from collector_v2_support import (
    KEY_ID,
    MASTER,
    NAME_COLL,
    NAME_PROG,
    OTHER_KEY_ID,
    PATTERN_COLL,
    TOKEN,
    FakeStore,
    load_v2,
    write_rules,
)
from collector_v2_support import write_config as write_v2_config

from collector import state as collector_state
from collector import v2_keys, v2_status
from collector.config import load_config
from collector.preflight import V2_CHECK_NAMES, run_v2_checks
from collector.v2_keys import SecretStoreError
from src.services.ingest_v2_models import StatusV2Request

V1_CHECK_NAMES = [
    "config_loads", "source_paths_exist", "source_paths_readable", "state_dir_writable", "status_dir_writable",
    "log_dir_writable", "api_token_source", "dns_resolution", "https_tls_connection", "api_authentication",
    "token_scope_matches", "installation_id_accepted", "no_direct_database_dependency", "collector_runtime_imports",
]
V2_SOURCES = ("acs", "checkins", "rejects")
SECRET_FORMS = (MASTER.hex(), base64.b64encode(MASTER).decode("ascii"))
RESPONSE_CANARY = "CANARY-SERVER-RESPONSE-BODY"


class DualSession:
    """Answers the HTTPS check and BOTH status probes, each scripted on its own, and records every POST."""

    def __init__(self, legacy=None, v2=None):
        self.legacy = legacy or _FakeResponse(200, {"status": "success"})
        self.v2 = v2 or _FakeResponse(200, {"status": "success", "contract_version": 2})
        self.post_calls: list[tuple[str, dict]] = []
        self.headers: list[dict] = []

    def get(self, url, timeout=None):
        return _FakeResponse(200)

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append((url, json))
        self.headers.append(headers)
        item = self.v2 if url.endswith("/v2/status") else self.legacy
        if isinstance(item, BaseException):
            raise item
        return item

    def paths(self):
        return [url.rsplit("testserver", 1)[-1] for url, _body in self.post_calls]

    def v2_bodies(self):
        return [body for url, body in self.post_calls if url.endswith("/v2/status")]


class V2Install:
    """One collector root with a v2 config, valid rules and the three Tech Logic files. The secret store is a fake:
    the real DPAPI store has its own Windows-only tests (tests/test_collector_v2_keys.py)."""

    def __init__(self, tmp_path, monkeypatch, *, contract_mode="v2", rules=True, **config_kwargs):
        store_api_token(monkeypatch, TOKEN)
        self.root = tmp_path / "root"
        self.config_path = write_v2_config(self.root, contract_mode=contract_mode, **config_kwargs)
        for name in V2_SOURCES:
            path = self.root / "tech" / f"{name}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"one\ntwo\n")
        self.cfg = load_config(self.config_path)
        self.v2 = load_v2(self.config_path)
        if rules:
            write_rules(self.v2)
        self.store = FakeStore()
        self.session = DualSession()

    def edit(self, change):
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        change(document)
        self.config_path.write_text(json.dumps(document), encoding="utf-8")
        self.cfg = load_config(self.config_path)

    def checks(self, *, legacy_probe_passed=True):
        results = run_v2_checks(self.cfg, self.config_path, session=self.session, legacy_probe_passed=legacy_probe_passed,
                                store=self.store)
        assert tuple(r.name for r in results) == V2_CHECK_NAMES  # always all six, always in this order
        return {r.name: r for r in results}

    def main(self, monkeypatch, tmp_path):
        """The real CLI, with the network and the DPAPI store replaced. Returns (exit code, the --output document)."""
        monkeypatch.setattr("collector.preflight.uploader.build_session", lambda: self.session)
        monkeypatch.setattr(v2_keys, "DpapiSecretStore", lambda _path: self.store)
        monkeypatch.setattr(preflight_module.socket, "getaddrinfo", lambda *_args: [])  # the test host does not resolve
        output = tmp_path / "preflight.json"
        code = main(["--config", str(self.config_path), "--output", str(output)])
        return code, json.loads(output.read_text(encoding="utf-8"))

    def files_written(self):
        """Every file under the root that is not an input (Tech Logic files, the config, the rules)."""
        skipped = {"tech", "config"}
        return {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and not skipped & set(path.relative_to(self.root).parts) and path.name != "config.json"
        }


def _by_name(document):
    return {check["name"]: check for check in document["checks"]}


# --- contract selection -----------------------------------------------------


def test_v2_a_complete_valid_setup_passes_all_six_checks(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)

    checks = install.checks()

    assert all(check.passed for check in checks.values()), checks
    assert install.session.paths() == ["/v2/status"]  # one probe, no upload
    assert install.store.loads == 1


def test_v2_main_runs_every_v1_check_then_the_six_v2_checks_and_both_probes_in_order(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)

    _code, document = install.main(monkeypatch, tmp_path)

    assert [check["name"] for check in document["checks"]] == [*V1_CHECK_NAMES, *V2_CHECK_NAMES]
    assert all(_by_name(document)[name]["passed"] for name in V2_CHECK_NAMES)
    # The legacy probe (installation activation) is still sent, first and unchanged; /v2/status follows.
    assert install.session.paths() == ["/upload-pipeline-status", "/v2/status"]
    assert install.session.post_calls[0][1]["status"] == "preflight_check"


def test_v2_main_exits_zero_only_when_every_check_passes(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # main() has no is_importable parameter: this repo's dev venv has the backend's database drivers installed.
    monkeypatch.setattr(preflight_module.run_preflight, "__kwdefaults__", {"session": None, "is_importable": _never_importable})

    code, document = install.main(monkeypatch, tmp_path)
    assert (code, document["passed"]) == (0, True), document

    install.session = DualSession(v2=_FakeResponse(403, {"detail": "Ingest key is not authorized"}))
    code, document = install.main(monkeypatch, tmp_path)
    assert (code, document["passed"]) == (1, False)
    assert [c["name"] for c in document["checks"] if not c["passed"]] == ["v2_ingest_authorized"]


@pytest.mark.parametrize("contract_mode", [None, "v1"])
def test_absent_or_v1_contract_mode_runs_no_v2_check_and_never_calls_v2_status(tmp_path, monkeypatch, contract_mode):
    install = V2Install(tmp_path, monkeypatch, contract_mode=contract_mode, rules=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a v1 preflight must not touch Contract v2")

    monkeypatch.setattr(preflight_module, "run_v2_checks", forbidden)
    monkeypatch.setattr(v2_keys.DpapiSecretStore, "load", forbidden)

    _code, document = install.main(monkeypatch, tmp_path)

    assert [check["name"] for check in document["checks"]] == V1_CHECK_NAMES
    assert install.session.paths() == ["/upload-pipeline-status"]
    assert install.files_written() == set()  # nothing under the root


def test_an_unsupported_contract_mode_fails_config_loads_with_exit_2_and_probes_nothing(tmp_path, monkeypatch, capsys):
    install = V2Install(tmp_path, monkeypatch, contract_mode="v3")

    code, document = install.main(monkeypatch, tmp_path)

    assert code == 2
    assert [(c["name"], c["passed"]) for c in document["checks"]] == [("config_loads", False)]
    assert "contract_mode" in document["checks"][0]["detail"]
    assert install.session.post_calls == []
    assert "OVERALL: FAIL" in capsys.readouterr().out


# --- local checks -----------------------------------------------------------


def _raising_store(code):
    store = FakeStore()

    def load(_expected_key_id):
        raise SecretStoreError(code)

    store.load = load
    return store


@pytest.mark.parametrize(
    ("store", "code"),
    [
        (FakeStore(present=False), "secret_missing"),
        (FakeStore(bound_key_id=OTHER_KEY_ID), "secret_key_mismatch"),
        (FakeStore(acl="exposed"), "secret_exposed"),
        (FakeStore(acl="unknown"), "secret_acl_unverified"),
        (_raising_store("secret_unreadable"), "secret_unreadable"),
        (_raising_store("secret_store_unavailable"), "secret_store_unavailable"),
    ],
    ids=["missing", "key-mismatch", "acl-exposed", "acl-unverified", "unreadable", "store-unavailable"],
)
def test_v2_each_secret_failure_is_its_fixed_code_and_stops_the_server_probe(tmp_path, monkeypatch, store, code):
    install = V2Install(tmp_path, monkeypatch)
    install.store = store

    checks = install.checks()

    assert (checks["v2_secret_usable"].passed, checks["v2_secret_usable"].detail) == (False, code)
    assert checks["v2_ingest_authorized"].passed is False
    assert checks["v2_ingest_authorized"].detail.startswith("not_evaluated:")
    assert install.session.post_calls == []  # no "preflight passed" heartbeat for an install that did not
    assert checks["v2_rules_valid"].passed is True  # independent: one run reports every local problem


def test_v2_the_acl_is_verified_before_the_secret_is_loaded(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    install.store = FakeStore(acl="exposed")

    install.checks()

    assert install.store.loads == 0


def test_v2_the_secret_check_never_creates_a_secret(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    install.store = FakeStore(present=False)
    install.store.create = lambda _key_id: pytest.fail("preflight must never create a secret")

    _code, document = install.main(monkeypatch, tmp_path)

    assert _by_name(document)["v2_secret_usable"]["detail"] == "secret_missing"
    assert not install.v2.secret_path.exists()


def test_v2_an_unexpected_secret_error_is_described_by_type_never_by_message(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)

    def load(_expected_key_id):
        raise RuntimeError(f"boom {SECRET_FORMS[0]} {KEY_ID}")

    install.store.load = load

    detail = install.checks()["v2_secret_usable"].detail

    assert detail.startswith("secret_check_failed: builtins.RuntimeError")
    assert SECRET_FORMS[0] not in detail and KEY_ID not in detail and "boom" not in detail


def test_v2_missing_rules_is_rules_missing(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch, rules=False)

    checks = install.checks()

    assert (checks["v2_rules_valid"].passed, checks["v2_rules_valid"].detail) == (False, "rules_missing")
    assert install.session.post_calls == []


@pytest.mark.parametrize(
    "text",
    [
        "{ not json " + NAME_PROG,
        json.dumps({"schema_version": 99, "branch_services_names": [NAME_PROG]}),
        json.dumps({"schema_version": 1, "destinations": [{"slug": "Not A Slug", "contains": PATTERN_COLL}]}),
        json.dumps({"schema_version": 1, "branch_services_names": NAME_COLL}),
    ],
    ids=["not-json", "wrong-schema", "bad-destination", "names-not-a-list"],
)
def test_v2_invalid_rules_is_rules_invalid_and_never_echoes_the_file(tmp_path, monkeypatch, text):
    install = V2Install(tmp_path, monkeypatch, rules=False)
    install.v2.rules_path.parent.mkdir(parents=True, exist_ok=True)
    install.v2.rules_path.write_text(text, encoding="utf-8")

    checks = install.checks()

    assert (checks["v2_rules_valid"].passed, checks["v2_rules_valid"].detail) == (False, "rules_invalid")
    assert install.session.post_calls == []


def test_v2_the_rules_check_uses_the_runtime_loader_and_needs_no_secret(tmp_path, monkeypatch):
    from collector import v2_rules

    install = V2Install(tmp_path, monkeypatch)
    install.store = FakeStore(present=False)
    seen = []
    real_load = v2_rules.load_rules
    monkeypatch.setattr(v2_rules, "load_rules", lambda path, keys: (seen.append(path), real_load(path, keys))[1])

    assert install.checks()["v2_rules_valid"].passed is True
    assert seen == [install.v2.rules_path]


@pytest.mark.parametrize(
    ("damage", "setting"),
    [
        (lambda d: d["v2"].__setitem__("timezone", "Mars/Olympus_Mons"), "v2.timezone"),
        (lambda d: d["v2"].pop("timezone"), "v2.timezone"),
        (lambda d: d["v2"].__setitem__("key_id", "NOT-A-KEY-ID"), "v2.key_id"),
        (lambda d: d["v2"].__setitem__("max_events_per_request", 5000), "v2.max_events_per_request"),
        (lambda d: d.pop("v2"), "'v2' section"),
    ],
    ids=["unknown-timezone", "no-timezone", "bad-key-id", "bad-limit", "no-section"],
)
def test_v2_an_invalid_v2_section_fails_v2_config_valid_and_leaves_the_rest_not_evaluated(tmp_path, monkeypatch, damage, setting):
    install = V2Install(tmp_path, monkeypatch)
    install.edit(damage)

    checks = install.checks()

    detail = checks["v2_config_valid"].detail
    assert checks["v2_config_valid"].passed is False
    assert detail.startswith("config_invalid:") and setting in detail
    assert "Mars" not in detail and "NOT-A-KEY-ID" not in detail and "5000" not in detail  # the setting, never its value
    for name in V2_CHECK_NAMES[1:]:
        assert (checks[name].passed, checks[name].detail.split(":")[0]) == (False, "not_evaluated")
    assert install.session.post_calls == [] and install.store.loads == 0


def test_v2_an_invalid_v2_section_is_a_failed_check_exit_1_not_a_config_load_failure(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    install.edit(lambda d: d["v2"].__setitem__("timezone", "Mars/Olympus_Mons"))

    code, document = install.main(monkeypatch, tmp_path)

    assert code == 1
    assert _by_name(document)["config_loads"]["passed"] is True
    assert _by_name(document)["v2_config_valid"]["passed"] is False
    assert install.session.paths() == ["/upload-pipeline-status"]  # the legacy probe still ran; /v2/status did not


def test_v2_an_unknown_source_name_fails_v2_sources_recognized(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    (install.root / "tech" / "holds.txt").write_bytes(b"")
    install.edit(lambda d: d["sources"].append({"name": "holds", "path": str(install.root / "tech" / "holds.txt")}))

    checks = install.checks()

    assert (checks["v2_sources_recognized"].passed, checks["v2_sources_recognized"].detail) == (False, "unknown_source: holds")
    assert install.session.post_calls == []


def test_v2_a_subset_of_the_three_sources_is_recognized(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch, sources=False)  # the support config with ONE source: acs

    assert install.checks()["v2_sources_recognized"].passed is True


def test_v2_a_missing_or_unreadable_source_still_fails_the_existing_source_checks(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    (install.root / "tech" / "rejects.txt").unlink()
    real_open = open

    def denied(path, *args, **kwargs):
        if str(path).endswith("checkins.txt"):
            raise PermissionError(13, "Access is denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)

    code, document = install.main(monkeypatch, tmp_path)

    checks = _by_name(document)
    assert code == 1
    assert (checks["source_paths_exist"]["passed"], checks["source_paths_exist"]["detail"]) == (False, "missing: rejects")
    assert checks["source_paths_readable"]["passed"] is False and "checkins" in checks["source_paths_readable"]["detail"]


def test_v2_an_unwritable_v2_data_folder_fails_v2_data_dirs_writable(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    (install.root / "data").write_bytes(b"a file where the data folder should be")  # so data\v2 cannot be created

    checks = install.checks()

    assert checks["v2_data_dirs_writable"].passed is False
    assert checks["v2_data_dirs_writable"].detail.startswith("not_writable:")
    assert install.session.post_calls == []


def test_v2_data_dirs_covers_the_state_status_cache_and_quarantine_folders(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    install = V2Install(tmp_path, monkeypatch, v2_extra={"quarantine_path": str(elsewhere / "q" / "quarantine_v2.json"),
                                                         "patron_cache_path": str(elsewhere / "c" / "patron_cache.db")})
    probed = []
    real_check = preflight_module._check_dir_writable
    monkeypatch.setattr(preflight_module, "_check_dir_writable", lambda name, folder: (probed.append(folder), real_check(name, folder))[1])

    assert install.checks()["v2_data_dirs_writable"].passed is True
    assert set(probed) == {install.root / "data" / "v2", elsewhere / "q", elsewhere / "c"}


def test_v2_preflight_leaves_nothing_behind_no_state_cache_quarantine_status_or_secret(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)

    install.main(monkeypatch, tmp_path)

    assert install.files_written() == set()
    assert not install.v2.state_path.exists() and not install.v2.patron_cache_path.exists()
    assert not install.v2.quarantine_path.exists() and not install.v2.status_path.exists()


# --- the two server probes --------------------------------------------------


@pytest.mark.parametrize(
    "legacy",
    [
        _FakeResponse(401, text="Invalid agent token"),
        _FakeResponse(403, text="Token scope does not match customer_id / branch_id"),
        _FakeResponse(403, text="Collector installation is not authorized to report status"),
        _FakeResponse(503, text="unavailable"),
        requests.ConnectionError("connection refused"),
    ],
    ids=["401", "scope", "installation", "503", "network"],
)
def test_v2_a_failed_legacy_probe_skips_the_v2_probe(tmp_path, monkeypatch, legacy):
    install = V2Install(tmp_path, monkeypatch)
    install.edit(lambda d: d.__setitem__("installation_id", 7))
    install.session = DualSession(legacy=legacy)

    code, document = install.main(monkeypatch, tmp_path)

    check = _by_name(document)["v2_ingest_authorized"]
    assert code == 1
    assert (check["passed"], check["detail"]) == (False, "not_evaluated: the /upload-pipeline-status probe did not pass")
    assert install.session.paths() == ["/upload-pipeline-status"]


def test_v2_the_legacy_probe_still_carries_the_installation_identity(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    install.edit(lambda d: d.__setitem__("installation_id", 7))

    install.main(monkeypatch, tmp_path)

    legacy_body = install.session.post_calls[0][1]
    assert (legacy_body["installation_id"], legacy_body["status"]) == (7, "preflight_check")
    assert "installation_id" not in install.session.v2_bodies()[0]  # /v2/status has no such field


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (_FakeResponse(401, {"detail": RESPONSE_CANARY}), "authentication_failed"),
        (_FakeResponse(403, {"detail": RESPONSE_CANARY}), "ingest_key_not_authorized"),
        (_FakeResponse(404, {"detail": RESPONSE_CANARY}), "v2_unavailable"),
        (_FakeResponse(405, {"detail": RESPONSE_CANARY}), "v2_unavailable"),
        (_FakeResponse(422, {"detail": [{"loc": ["body", "status"], "msg": RESPONSE_CANARY}]}), "status_request_invalid"),
        (_FakeResponse(429, {"detail": RESPONSE_CANARY}), "server_unavailable"),
        (_FakeResponse(500, {"detail": RESPONSE_CANARY}), "server_unavailable"),
        (_FakeResponse(503, {"detail": RESPONSE_CANARY}), "server_unavailable"),
        (_FakeResponse(200, {"status": RESPONSE_CANARY}), "server_unavailable"),
        (requests.ConnectionError(RESPONSE_CANARY), "server_unavailable"),
        (requests.Timeout(RESPONSE_CANARY), "server_unavailable"),
        (_FakeResponse(418, {"detail": RESPONSE_CANARY}), "unexpected_response"),
    ],
    ids=["401", "403", "404", "405", "422", "429", "500", "503", "200-not-success", "network", "timeout", "418"],
)
def test_v2_status_failures_are_classified_by_status_code_and_never_show_the_response(tmp_path, monkeypatch, reply, code):
    install = V2Install(tmp_path, monkeypatch)
    install.session = DualSession(v2=reply)

    check = install.checks()["v2_ingest_authorized"]

    assert check.passed is False
    assert check.detail.split(":")[0] == code
    assert RESPONSE_CANARY not in check.detail
    assert len(install.session.v2_bodies()) == 1  # asked once: preflight does not retry on its own


def test_v2_status_200_passes_and_sends_the_token_as_a_bearer_header_only(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)

    check = install.checks()["v2_ingest_authorized"]

    assert (check.passed, check.detail.split(":")[0]) == (True, "authorized")
    assert install.session.headers[0]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(install.session.v2_bodies())


# --- what the /v2/status probe sends ----------------------------------------


def test_v2_a_fresh_install_sends_the_smallest_valid_healthy_snapshot(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)

    install.checks()

    body = install.session.v2_bodies()[0]
    assert body == {"contract_version": 2, "key_id": KEY_ID, "status": "healthy", "pending_outbox_count": 0,
                    "quarantined_count": 0}
    StatusV2Request.model_validate(body)  # the server's own model accepts it


def test_v2_an_established_install_re_sends_its_last_run_status_without_run_or_schedule_diagnostics(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    last_run = datetime.now(UTC).replace(microsecond=0)
    last_success = last_run - timedelta(minutes=15)
    collector_state.write_status(install.v2.status_path, v2_status.local_status_document(
        now=last_run, ok=False, health="degraded", last_error_class="retryable_infra", consecutive_failures=1,
        last_success_at=last_success, counters={"lines_read": 12, "events_inserted": 3}, quarantined_count=4))
    before = install.v2.status_path.read_bytes()

    install.checks()

    body = install.session.v2_bodies()[0]
    assert body == {
        "contract_version": 2, "key_id": KEY_ID, "status": "degraded", "last_error_class": "retryable_infra",
        "pending_outbox_count": 0, "quarantined_count": 4, "last_success_at": last_success.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "watcher_last_active_at": last_run.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    assert not [key for key in body if key.startswith("collector_")]
    StatusV2Request.model_validate(body)
    assert install.v2.status_path.read_bytes() == before  # read, never rewritten


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{ not json",
        b"[]",
        json.dumps({"health_status": "fantastic", "quarantined_count": 3}).encode(),
        json.dumps({"quarantined_count": 3, "last_error_class": "other"}).encode(),
    ],
    ids=["empty", "not-json", "not-an-object", "unknown-health", "no-health"],
)
def test_v2_an_unusable_status_file_falls_back_to_the_fresh_snapshot(tmp_path, monkeypatch, content):
    install = V2Install(tmp_path, monkeypatch)
    install.v2.status_path.parent.mkdir(parents=True, exist_ok=True)
    install.v2.status_path.write_bytes(content)

    assert install.checks()["v2_ingest_authorized"].passed is True

    assert install.session.v2_bodies()[0] == {"contract_version": 2, "key_id": KEY_ID, "status": "healthy",
                                              "pending_outbox_count": 0, "quarantined_count": 0}


def test_v2_unusable_fields_of_a_status_file_are_dropped_not_sent(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    install.v2.status_path.parent.mkdir(parents=True, exist_ok=True)
    install.v2.status_path.write_text(json.dumps({
        "health_status": "error", "last_error_class": "CANARY free text", "quarantined_count": -5,
        "last_success_at": "yesterday", "last_attempt": 12345, "counters": {"lines_read": 9},
    }), encoding="utf-8")

    install.checks()

    assert install.session.v2_bodies()[0] == {"contract_version": 2, "key_id": KEY_ID, "status": "error",
                                              "pending_outbox_count": 0, "quarantined_count": 0}


# --- privacy ----------------------------------------------------------------


def test_v2_nothing_preflight_prints_or_writes_holds_the_key_id_token_secret_or_a_rule(tmp_path, monkeypatch, capsys):
    install = V2Install(tmp_path, monkeypatch)

    def sweep():
        _code, document = install.main(monkeypatch, tmp_path)
        captured = capsys.readouterr()
        text = captured.out + captured.err + json.dumps(document)
        for forbidden in (KEY_ID, TOKEN, *SECRET_FORMS, NAME_PROG, NAME_COLL, PATTERN_COLL, RESPONSE_CANARY):
            assert forbidden not in text, forbidden
        return document

    assert all(_by_name(sweep())[name]["passed"] for name in V2_CHECK_NAMES)

    # ... and not when things fail either: a wrong key, a rejected probe and broken rules.
    install.session = DualSession(v2=_FakeResponse(403, {"detail": RESPONSE_CANARY}))
    assert _by_name(sweep())["v2_ingest_authorized"]["passed"] is False
    install.store = FakeStore(bound_key_id=OTHER_KEY_ID)
    install.v2.rules_path.write_text("{ broken " + NAME_PROG, encoding="utf-8")
    failed = _by_name(sweep())
    assert (failed["v2_secret_usable"]["detail"], failed["v2_rules_valid"]["detail"]) == ("secret_key_mismatch", "rules_invalid")


def test_v2_every_failing_v2_detail_starts_with_a_fixed_code(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch, rules=False)
    install.store = FakeStore(present=False)
    (install.root / "data").write_bytes(b"x")
    (install.root / "tech" / "holds.txt").write_bytes(b"")
    install.edit(lambda d: d["sources"].append({"name": "holds", "path": str(install.root / "tech" / "holds.txt")}))

    checks = install.checks()

    codes = {name: re.match(r"^[a-z0-9_]+", check.detail).group(0) for name, check in checks.items() if not check.passed}
    assert codes == {
        "v2_sources_recognized": "unknown_source", "v2_data_dirs_writable": "not_writable",
        "v2_secret_usable": "secret_missing", "v2_rules_valid": "rules_missing", "v2_ingest_authorized": "not_evaluated",
    }


def test_the_v1_preflight_path_loads_no_heavy_v2_module(tmp_path):
    """A fresh interpreter running a v1 preflight loads only v2_config (to read the mode) and what it imports."""
    import subprocess
    import sys

    root = Path(preflight_module.__file__).resolve().parent.parent
    config = write_v2_config(tmp_path / "root", contract_mode=None)
    script = (
        "import sys, json, requests\n"
        "from collector import api_token_store, preflight, uploader\n"
        "api_token_store.DpapiTokenStore.load = lambda self, c, b: 'token'\n"
        "class S:\n"
        "    def get(self, *a, **k): raise requests.ConnectionError('offline')\n"
        "    def post(self, *a, **k): raise requests.ConnectionError('offline')\n"
        "uploader.build_session = lambda: S()\n"
        "assert preflight.main(['--config', sys.argv[1]]) == 1\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('collector.v2_'))))\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(config)], cwd=root, capture_output=True, text=True, check=False,
                            env={**__import__("os").environ, "DATABASE_URL": "sqlite:///x", "PYTHONPATH": str(root)})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == ["collector.v2_config", "collector.v2_events"]


# --- Windows only: the secret check against the REAL DPAPI store ------------

windows_only = pytest.mark.skipif(__import__("sys").platform != "win32", reason="DPAPI and NTFS ACLs exist only on Windows")


class _TrustedAclStore(v2_keys.DpapiSecretStore):
    """The real DPAPI store with only its ACL verdict fixed (as in tests/test_collector_v2_keys.py): a test tmp folder
    inherits the user's ACL, and locking it to Administrators + SYSTEM would lock a non-elevated test out of it."""

    def acl_state(self):
        return "protected"


@windows_only
def test_v2_the_real_store_in_a_folder_the_user_can_read_fails_the_secret_check_as_exposed(tmp_path, monkeypatch):
    """No stubbing at all: real DPAPI blob, real icacls verdict, through the store preflight builds for itself."""
    install = V2Install(tmp_path, monkeypatch)
    _TrustedAclStore(install.v2.secret_path).create(KEY_ID)
    install.store = None  # run_v2_checks builds DpapiSecretStore(v2.secret_path)
    before = install.v2.secret_path.read_bytes()

    checks = install.checks()

    assert (checks["v2_secret_usable"].passed, checks["v2_secret_usable"].detail) == (False, "secret_exposed")
    assert install.session.post_calls == []
    assert install.v2.secret_path.read_bytes() == before  # never replaced


@windows_only
def test_v2_the_real_store_decrypts_in_this_process_and_enforces_the_key_binding(tmp_path, monkeypatch):
    install = V2Install(tmp_path, monkeypatch)
    install.store = _TrustedAclStore(install.v2.secret_path)
    install.store.create(KEY_ID)

    assert install.checks()["v2_secret_usable"].passed is True

    install.edit(lambda d: d["v2"].__setitem__("key_id", OTHER_KEY_ID))  # the config now names another key
    assert install.checks()["v2_secret_usable"].detail == "secret_key_mismatch"

    install.v2.secret_path.write_bytes(b"not a dpapi blob")
    assert install.checks()["v2_secret_usable"].detail == "secret_unreadable"

    install.v2.secret_path.unlink()
    assert install.checks()["v2_secret_usable"].detail == "secret_missing"
