"""Tests for collector/run.py::main() -- the CLI entry point's exit-code
contract (Phase 4a). This is the exact signal Task Scheduler's
RestartOnFailure policy (confirmed live: 3 attempts, 5 minutes apart)
depends on -- a cleanly-detected failure and an unanticipated crash must
both produce the same nonzero exit, and a normal/failed-but-handled run
must never be confused with each other.
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


def _configure_a_parser_for_testing(monkeypatch):
    """Simulates "the production parser HAS been wired in" for tests that
    want to exercise behavior OTHER than the fail-closed check itself
    (upload failure, successful run, etc.). Deliberately explicit and
    separate from run_mod._PRODUCTION_PARSE_FNS, which tests must never
    populate directly -- see test_ordinary_cli_invocation_fails_closed_
    without_production_parser below, which is the one test that must run
    against the REAL, untouched default."""
    monkeypatch.setattr(
        run_mod, "_PRODUCTION_PARSE_FNS", {"checkins": lambda lines: [{"raw_line": line} for line in lines]}
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
    _configure_a_parser_for_testing(monkeypatch)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 0


def test_upload_failure_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    _configure_a_parser_for_testing(monkeypatch)

    monkeypatch.setattr(
        run_mod.uploader, "build_session",
        lambda: FakeSession(script=[_FakeResponse(500, text="down")]),
    )

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 1
    assert not (tmp_path / "state.json").exists()  # state correctly not advanced


# --- fail closed while the production parser is not wired -----------------


def test_ordinary_cli_invocation_fails_closed_without_production_parser(monkeypatch, tmp_path, capsys):
    """THE required regression test: an ordinary `python -m collector.run
    --config ...` invocation -- no monkeypatching of the parser, real
    source data present, otherwise fully valid config/token/network mock
    -- must NEVER succeed while run_mod._PRODUCTION_PARSE_FNS is empty. It
    must exit with a distinct, actionable, nonzero code, make NO upload
    call, and persist NO state. This is the exact failure mode an earlier
    version of this module was vulnerable to (silently defaulting to a
    raw-line passthrough parser and uploading {"raw_line": ...} records
    as if they were real Tech Logic data)."""
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\nline two\n", encoding="utf-8")
    fake_session = FakeSession()
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: fake_session)
    # Deliberately NOT calling _configure_a_parser_for_testing here -- this
    # test exercises the real, untouched default.

    exit_code = run_mod.main(["--config", str(config_path)])

    assert exit_code == 2  # same family as a bad --config / missing token
    stderr = capsys.readouterr().err
    assert "parser" in stderr.lower()
    assert "checkins" in stderr  # names exactly which source is unconfigured

    # No data was ever uploaded, and no cursor was ever advanced --
    # fail closed means nothing happens, not "happens with fake data".
    assert fake_session.calls == []
    assert not (tmp_path / "state.json").exists()


def test_production_parse_fns_constant_is_empty_by_default():
    # A direct assertion on the constant itself -- if anything ever
    # populates it as a "quick fix" without going through the
    # parser-parity phase, this test (and the one above) will catch it.
    assert run_mod._PRODUCTION_PARSE_FNS == {}


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
    _configure_a_parser_for_testing(monkeypatch)

    exit_code = run_mod.main(["--config", str(config_path)])

    assert exit_code == 0
    assert (tmp_path / "state.json").exists()
