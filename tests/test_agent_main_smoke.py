"""Smoke tests for agent/main.py -- the one canonical CLI entry point for
the continuous agent (`python -m agent.main --config ...`), per the
cleanup/convergence phase's "there should be ONE supported canonical
runtime command" requirement.

Covers argument parsing and config-error handling -- the thin layer
agent.main.main() adds on top of AgentRunner. The AgentRunner lifecycle
itself (start/stop, real threads, real signal-triggered shutdown
semantics via stop_event) is already exhaustively covered directly in
tests/test_runtime_supervisor.py; a full OS-level SIGTERM-delivery test
through this CLI wrapper was deliberately not added here -- real signal
delivery semantics are platform-specific enough (and Windows Service
Recovery's actual behavior is explicitly one of this project's pending
live-AMH-machine validation items, see agent/README.md) that faking it
reliably inside this test environment added fragility without adding
real confidence beyond what test_runtime_supervisor.py already proves.
"""

from __future__ import annotations

import json
import threading

import pytest

from agent import main as agent_main
from agent.runtime.supervisor import AgentRunner


def _write_config(tmp_path):
    doc = {
        "customer_id": 100,
        "branch_id": 5,
        "api_url": "https://example.invalid",
        "sources": [{"name": "checkins", "path": str(tmp_path / "Checkins.txt")}],
        "state_path": str(tmp_path / "state" / "agent_state.json"),
        "spool_root": str(tmp_path / "spool"),
        "agent_identity_path": str(tmp_path / "agent_identity.json"),
        "log_dir": str(tmp_path / "logs"),
        "diagnostics_dir": str(tmp_path / "diagnostics"),
        "collector_poll_seconds": 0.05,
        "uploader_poll_seconds": 0.05,
        "heartbeat_interval_seconds": 0.5,
        "housekeeping_interval_seconds": 0.5,
    }
    (tmp_path / "Checkins.txt").write_text("", encoding="utf-8")
    config_path = tmp_path / "runtime_config.json"
    config_path.write_text(json.dumps(doc), encoding="utf-8")
    return config_path


def test_missing_config_argument_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        agent_main.main([])

    assert exc_info.value.code != 0


def test_invalid_config_path_returns_exit_code_2(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")

    exit_code = agent_main.main(["--config", str(tmp_path / "does-not-exist.json")])

    assert exit_code == 2


def test_missing_api_token_returns_exit_code_2(monkeypatch, tmp_path):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    config_path = _write_config(tmp_path)

    exit_code = agent_main.main(["--config", str(config_path)])

    assert exit_code == 2


def test_worker_crash_causes_main_to_exit_nonzero(monkeypatch, tmp_path, capsys):
    """The end-to-end exit-code path: agent_main.main() constructs and
    starts a real AgentRunner internally (not something this test can
    reach directly), so the crashing loop is injected via a CLASS-level
    monkeypatch on AgentRunner itself -- every instance main() creates,
    including the one inside this call, picks it up.

    Run on a background thread with a bounded join, specifically so a
    regression in the fail-fast wiring (stop_event never getting set)
    fails this test with a clear assertion instead of hanging the whole
    suite on agent_main.main()'s otherwise-untimed
    runner.stop_event.wait() call. Python only allows signal.signal() to
    be called from the main thread, so it's stubbed out here -- this test
    is about the crash -> exit-code path, not signal delivery (already
    covered structurally by agent_main.main()'s own code, which this
    doesn't change).
    """
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    monkeypatch.setattr("signal.signal", lambda *args, **kwargs: None)

    def boom(self):
        raise RuntimeError("simulated collector crash")

    monkeypatch.setattr(AgentRunner, "_collector_loop", boom)

    result: dict[str, int] = {}

    def run():
        result["exit_code"] = agent_main.main(
            ["--config", str(config_path), "--shutdown-timeout", "5"]
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=15.0)

    assert not thread.is_alive(), (
        "agent_main.main() did not return -- the worker crash likely failed to "
        "set stop_event (fail-fast regression)"
    )
    assert result.get("exit_code") == 1

    stderr = capsys.readouterr().err
    assert "collector" in stderr
    assert "simulated collector crash" in stderr
