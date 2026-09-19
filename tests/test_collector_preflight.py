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

import requests

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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    report = run_preflight(cfg, session=FakeSession(), is_importable=_never_importable)

    assert report.passed is True
    assert all(r.passed for r in report.results)


def test_one_failing_check_makes_overall_report_fail(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    report = run_preflight(cfg, session=FakeSession())

    assert report.passed is False
    assert _result(report, "api_token_visible").passed is False


def test_report_serializes_to_dict_with_all_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    cfg = _cfg(tmp_path)  # Checkins.txt never created

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "source_paths_exist").passed is False
    assert "checkins" in _result(report, "source_paths_exist").detail


def test_unreadable_source_fails_readable_check(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    # Point state_path at a location that cannot be created as a
    # directory (a file already occupies that path segment).
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    cfg = _cfg(tmp_path, state_path=blocker / "sub" / "state.json")

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "state_dir_writable").passed is False


def test_writable_status_and_log_dirs_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "status_dir_writable").passed is True
    assert _result(report, "log_dir_writable").passed is True
    # Probe files must not be left behind.
    assert list(cfg.status_path.parent.iterdir()) == []


# --- token visibility ---------------------------------------------------


def test_missing_token_fails_its_check(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    report = run_preflight(cfg, session=FakeSession())

    result = _result(report, "api_token_visible")
    assert result.passed is False
    assert "SYSTEM" in result.detail  # actionable hint, not a generic message


# --- DNS failure ----------------------------------------------------------


def test_dns_failure_fails_its_check_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    def boom(host, port):
        raise OSError("Name or service not known")

    monkeypatch.setattr("collector.preflight.socket.getaddrinfo", boom)

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "dns_resolution").passed is False


# --- HTTPS / TLS failure -----------------------------------------------------


def test_connection_failure_fails_https_check(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    session = FakeSession(get_exception=requests.ConnectionError("no route to host"))
    report = run_preflight(cfg, session=session)

    assert _result(report, "https_tls_connection").passed is False


def test_tls_certificate_failure_is_reported_distinctly(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    session = FakeSession(get_exception=requests.exceptions.SSLError("certificate verify failed"))
    report = run_preflight(cfg, session=session)

    result = _result(report, "https_tls_connection")
    assert result.passed is False
    assert "certificate" in result.detail.lower() or "TLS" in result.detail


# --- auth failure / scope mismatch -------------------------------------------


def test_auth_failure_fails_both_auth_and_scope_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    session = FakeSession(post_response=_FakeResponse(401, text="Invalid agent token"))
    report = run_preflight(cfg, session=session)

    assert _result(report, "api_authentication").passed is False
    assert _result(report, "token_scope_matches").passed is False


def test_scope_mismatch_distinguishes_valid_token_from_wrong_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    session = FakeSession()
    run_preflight(cfg, session=session)

    assert len(session.post_calls) == 1
    assert session.post_calls[0][1]["status"] == "preflight_check"


# --- no direct database dependency ------------------------------------------


def test_no_database_dependency_check_passes_when_database_url_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    report = run_preflight(cfg, session=FakeSession(), is_importable=_never_importable)

    assert _result(report, "no_direct_database_dependency").passed is True


def test_no_database_dependency_check_fails_when_database_url_set(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    cfg = _cfg(tmp_path)

    report = run_preflight(cfg, session=FakeSession())

    assert _result(report, "no_direct_database_dependency").passed is False


# --- runtime imports ----------------------------------------------------


def test_runtime_imports_check_passes_in_a_healthy_install(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
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
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
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
    # api_token_visible can never actually be what fails via main()'s
    # own CLI path. This test instead exercises a genuine preflight-level
    # failure: source file never created.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    monkeypatch.setattr("collector.preflight.uploader.build_session", lambda: FakeSession())
    config_path = _write_config(tmp_path)
    # Checkins.txt deliberately never created.

    exit_code = main(["--config", str(config_path)])
    assert exit_code == 1


# --- installation_id check ------------------------------------------------


def test_preflight_probe_carries_the_installation_identity_when_configured(tmp_path, monkeypatch):
    from collector import __version__

    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    session = FakeSession()

    run_preflight(_cfg(tmp_path, installation_id=41), session=session)

    payload = session.post_calls[0][1]
    assert payload["installation_id"] == 41
    assert payload["collector_version"] == __version__
    assert payload["status"] == "preflight_check"


def test_preflight_probe_for_a_legacy_config_has_no_installation_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    session = FakeSession()

    report = run_preflight(_cfg(tmp_path), session=session)

    payload = session.post_calls[0][1]
    assert "installation_id" not in payload and "collector_version" not in payload
    result = _result(report, "installation_id_accepted")
    assert result.passed is True
    assert "legacy" in result.detail


def test_accepted_installation_id_passes_the_installation_check(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")

    report = run_preflight(_cfg(tmp_path, installation_id=41), session=FakeSession())

    result = _result(report, "installation_id_accepted")
    assert result.passed is True
    assert "41" in result.detail


def test_rejected_installation_id_fails_only_the_installation_check(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    session = FakeSession(post_response=_FakeResponse(403, text="Token scope does not match customer_id / branch_id"))

    report = run_preflight(_cfg(tmp_path, installation_id=41), session=session)

    assert _result(report, "token_scope_matches").passed is False
    result = _result(report, "installation_id_accepted")
    assert result.passed is False and "not evaluated" in result.detail


def test_network_failure_leaves_the_installation_check_not_evaluated(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    (tmp_path / "Checkins.txt").write_text("data", encoding="utf-8")
    session = FakeSession(post_response=_FakeResponse(503, text="down"))

    report = run_preflight(_cfg(tmp_path, installation_id=41), session=session)

    result = _result(report, "installation_id_accepted")
    assert result.passed is False and "could not evaluate" in result.detail
