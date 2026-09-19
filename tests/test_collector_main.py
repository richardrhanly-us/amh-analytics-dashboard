"""Tests for collector/run.py::main() -- the CLI entry point's exit-code
contract (Phase 4a). A cleanly-detected failure and an unanticipated
crash must both produce the same nonzero exit, and a normal/failed-but-
handled run must never be confused with each other -- this is what lets
the next normal 15-minute scheduled run recognize uncommitted work and
retry it (see collector/run.py's module docstring, RECOVERY MODEL).
Task Scheduler's RestartOnFailure setting is NOT this mechanism -- Phase
4d Section H live-tested it on LIB-L26 and found it does not activate
for a clean nonzero exit.
"""

from __future__ import annotations

import json

import pytest

from collector import run as run_mod


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {"status": "success"}
        self.text = text or json.dumps(self._json_body)

    def json(self):
        return self._json_body


class FakeSession:
    def __init__(self, script=None):
        self.script = list(script) if script is not None else None
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        if self.script is not None and self.script:
            next_item = self.script.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item
        return _FakeResponse(200, {"status": "success"})


def _configure_a_passthrough_parser_for_testing(monkeypatch):
    """Substitutes a trivial passthrough parser for whichever source(s)
    a test's config names, so tests about orchestration (upload failure,
    successful run, state persistence) don't need a real 14-field
    checkin line just to produce a non-empty record. Deliberately
    monkeypatches collector.parsers.build_production_parse_fns itself
    (the actual production seam, post parser-parity wiring), not
    anything inside collector/parsers.py -- these tests are about
    main()'s orchestration, not about parsing correctness, which
    tests/test_collector_parsers.py already covers directly."""
    monkeypatch.setattr(
        run_mod.parsers,
        "build_production_parse_fns",
        lambda **_kwargs: {
            "checkins": lambda lines: [{"raw_line": line} for line in lines],
            "rejects": lambda lines: [{"raw_line": line} for line in lines],
            "acs": lambda lines: [{"raw_line": line} for line in lines],
        },
    )


def _write_config(tmp_path):
    doc = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://example.invalid",
        "sources": [{"name": "checkins", "path": str(tmp_path / "Checkins.txt")}],
        "state_path": str(tmp_path / "state.json"),
        "status_path": str(tmp_path / "status.json"),
        "log_path": str(tmp_path / "collector.log"),
    }
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_missing_config_argument_exits_nonzero():
    with pytest.raises(SystemExit) as exc_info:
        run_mod.main([])
    assert exc_info.value.code != 0


def test_invalid_config_path_returns_exit_code_2(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    exit_code = run_mod.main(["--config", str(tmp_path / "does-not-exist.json")])
    assert exit_code == 2


def test_missing_api_token_returns_exit_code_2(monkeypatch, tmp_path):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    config_path = _write_config(tmp_path)
    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 2


def test_clean_run_with_no_source_data_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 0


def test_upload_failure_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    monkeypatch.setattr(
        run_mod.uploader, "build_session",
        lambda: FakeSession(script=[_FakeResponse(500, text="down")]),
    )

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 1
    assert not (tmp_path / "state.json").exists()  # state correctly not advanced


# --- production parser wiring (parser-parity phase) ------------------------
#
# Prior to this phase, main()'s production path used a deliberately empty
# _PRODUCTION_PARSE_FNS constant, and an ordinary CLI invocation for ANY
# configured source (including checkins/rejects/acs) failed closed with
# exit code 2. That constant no longer exists -- main() now calls
# collector.parsers.build_production_parse_fns(), which provides real
# adapters for exactly checkins/rejects/acs (see
# tests/test_collector_parsers.py for adapter-boundary coverage). The two
# tests below replace the old ones: one proves the wiring is genuinely
# live end to end through main() itself (not just at the parsers.py
# level), the other proves fail-closed still protects against a source
# name outside the three that are actually wired.


def test_ordinary_cli_invocation_parses_and_uploads_real_checkins_data(monkeypatch, tmp_path):
    """THE end-to-end proof that production parser wiring is genuinely
    live: an ordinary `python -m collector.run --config ...` invocation
    -- no monkeypatching of collector.parsers at all -- must parse a real
    Tech Logic checkins line via the real, unchanged
    agent/parser/checkins.py and upload it in the exact backend row
    shape, through main()'s own default path."""
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    real_line = "Sunny days /|33472004192508|MLEPB|E KERBEL NATURE|000|1|False||4|N|N|N|8/31/2026|4:18:39 PM"
    (tmp_path / "Checkins.txt").write_text(real_line + "\n", encoding="utf-8")
    fake_session = FakeSession()
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: fake_session)
    # Deliberately NOT calling _configure_a_passthrough_parser_for_testing
    # here -- this test exercises the real, untouched production seam.

    exit_code = run_mod.main(["--config", str(config_path)])

    assert exit_code == 0
    upload_calls = [call for call in fake_session.calls if call[0].endswith("/upload")]
    assert len(upload_calls) == 1
    _url, payload = upload_calls[0]
    assert len(payload["checkins"]) == 1
    uploaded = payload["checkins"][0]
    assert uploaded["barcode"] == "33472004192508"
    assert uploaded["destination"] == "Main"
    assert uploaded["customer_id"] == 1
    assert uploaded["branch_id"] == 1
    assert "raw_line" not in uploaded  # confirms the REAL adapter ran, not a passthrough stub


def test_unknown_source_name_still_fails_closed(monkeypatch, tmp_path, capsys):
    """Fail-closed remains the structural default for any source name
    outside the three collector.parsers.build_production_parse_fns()
    actually provides -- e.g. a config typo, or a not-yet-supported
    fourth source. No monkeypatching of collector.parsers here either;
    this exercises the real production seam's edge case."""
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    doc = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://example.invalid",
        "sources": [{"name": "mystery_source", "path": str(tmp_path / "Mystery.txt")}],
        "state_path": str(tmp_path / "state.json"),
        "status_path": str(tmp_path / "status.json"),
        "log_path": str(tmp_path / "collector.log"),
    }
    config_path = tmp_path / "collector_config.json"
    config_path.write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / "Mystery.txt").write_text("line one\n", encoding="utf-8")
    fake_session = FakeSession()
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: fake_session)

    exit_code = run_mod.main(["--config", str(config_path)])

    assert exit_code == 2
    stderr = capsys.readouterr().err
    assert "parser" in stderr.lower()
    assert "mystery_source" in stderr

    assert fake_session.calls == []
    assert not (tmp_path / "state.json").exists()


def test_unanticipated_exception_still_exits_nonzero(monkeypatch, tmp_path, capsys):
    """The belt-and-braces path: a bug that escapes run_once's own
    handling entirely must still produce a nonzero exit -- this is the
    same fail-fast discipline already proven for the continuous agent's
    main() (see tests/test_agent_main_smoke.py), applied here."""
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())

    def boom(*args, **kwargs):
        raise RuntimeError("simulated unexpected bug")

    monkeypatch.setattr(run_mod, "run_once", boom)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 1


def test_successful_run_exits_zero_and_persists_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    exit_code = run_mod.main(["--config", str(config_path)])

    assert exit_code == 0
    assert (tmp_path / "state.json").exists()


# --- legacy (no installation_id) configs ----------------------------------


def _write_config_with(tmp_path, **extra):
    path = _write_config(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(extra)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _drop_collector_log_handlers():
    # collector.run._build_logger caches handlers on a shared named logger,
    # which would otherwise keep writing to (and holding open) an earlier
    # test's tmp_path log file.
    import logging

    logger = logging.getLogger("sortview.collector")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_legacy_config_without_installation_id_runs_and_logs_a_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    _drop_collector_log_handlers()
    config_path = _write_config(tmp_path)  # no installation_id
    session = FakeSession()
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: session)
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    try:
        exit_code = run_mod.main(["--config", str(config_path)])
    finally:
        _drop_collector_log_handlers()

    assert exit_code == 0
    assert "no installation_id" in (tmp_path / "collector.log").read_text(encoding="utf-8")
    status_calls = [p for url, p in session.calls if url.endswith("/upload-pipeline-status")]
    assert status_calls and "installation_id" not in status_calls[0]


def test_config_with_installation_id_runs_without_the_legacy_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    _drop_collector_log_handlers()
    config_path = _write_config_with(tmp_path, installation_id=41)
    session = FakeSession()
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: session)
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    try:
        exit_code = run_mod.main(["--config", str(config_path)])
    finally:
        _drop_collector_log_handlers()

    assert exit_code == 0
    assert "no installation_id" not in (tmp_path / "collector.log").read_text(encoding="utf-8")
    status_calls = [p for url, p in session.calls if url.endswith("/upload-pipeline-status")]
    assert status_calls and status_calls[0]["installation_id"] == 41


def test_invalid_installation_id_in_config_exits_with_config_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config_with(tmp_path, installation_id="not-a-number")

    assert run_mod.main(["--config", str(config_path)]) == 2
