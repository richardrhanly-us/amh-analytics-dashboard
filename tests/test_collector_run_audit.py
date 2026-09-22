"""Tests for collector/run_audit.py (Phase 1 local run-audit logging) and
its wiring into collector/run.py::main().

Two groups: direct unit tests of collector/run_audit.py's own primitives
(record shape, append durability, locking, retention, torn-line
tolerance, the privacy canary), and end-to-end tests that drive
collector.run.main() itself and inspect the resulting runs.jsonl -- the
same style as tests/test_collector_main.py's FakeSession/_write_config
helpers, reused here rather than reinvented.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from collector import run as run_mod
from collector import run_audit

# ---------------------------------------------------------------------
# Shared helpers (mirrors tests/test_collector_main.py's own conventions)
# ---------------------------------------------------------------------


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
    monkeypatch.setattr(
        run_mod.parsers,
        "build_production_parse_fns",
        lambda **_kwargs: {
            "checkins": lambda lines: [{"raw_line": line} for line in lines],
            "rejects": lambda lines: [{"raw_line": line} for line in lines],
            "acs": lambda lines: [{"raw_line": line} for line in lines],
        },
    )


def _write_config(tmp_path, **extra):
    doc = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://example.invalid",
        "sources": [{"name": "checkins", "path": str(tmp_path / "Checkins.txt")}],
        "state_path": str(tmp_path / "state.json"),
        "status_path": str(tmp_path / "status.json"),
        "log_path": str(tmp_path / "collector.log"),
    }
    doc.update(extra)
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _drop_collector_log_handlers():
    logger = logging.getLogger("sortview.collector")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def _read_records(path):
    return list(run_audit.iter_records(path))


@pytest.fixture(autouse=True)
def _isolate_collector_logger():
    _drop_collector_log_handlers()
    yield
    _drop_collector_log_handlers()


# ---------------------------------------------------------------------
# Unit tests: collector/run_audit.py's own primitives
# ---------------------------------------------------------------------


def test_build_record_has_exactly_the_phase_1_shape():
    record = run_audit.build_record(
        run_id="r1",
        started_at="2026-01-01T00:00:00.000000Z",
        finished_at="2026-01-01T00:00:05.000000Z",
        duration_ms=5000,
        result="completed",
        collector_version="1.0.4",
        sources={
            "checkins": run_audit.SourceAudit(offset_before=100, offset_after=200, new_records=3, uploaded=3),
        },
        sources_missing=["rejects"],
        sources_rotated=[],
        sources_truncated=[],
        upload_status=200,
        pipeline_status=200,
        failure_stage=None,
        error_code=None,
    )
    assert set(record.keys()) == {
        "run_id", "started_at", "finished_at", "duration_ms", "result", "collector_version",
        "sources", "sources_missing", "sources_rotated", "sources_truncated", "http",
        "failure_stage", "error_code",
    }
    assert record["sources"]["checkins"] == {
        "offset_before": 100, "offset_after": 200, "new_records": 3, "uploaded": 3,
    }
    assert record["http"] == {"upload_status": 200, "pipeline_status": 200}
    assert record["sources_missing"] == ["rejects"]


def test_build_record_accepts_error_code_enum_or_plain_string():
    via_enum = run_audit.build_record(
        run_id="r", started_at="x", finished_at="y", duration_ms=1, result="failed_upload",
        collector_version="1.0.4", error_code=run_audit.ErrorCode.AUTH_FAILURE,
    )
    via_string = run_audit.build_record(
        run_id="r", started_at="x", finished_at="y", duration_ms=1, result="failed_upload",
        collector_version="1.0.4", error_code="auth_failure",
    )
    assert via_enum["error_code"] == "auth_failure" == via_string["error_code"]


def test_append_run_record_writes_one_compact_json_line(tmp_path):
    path = tmp_path / "runs.jsonl"
    record = run_audit.build_record(
        run_id="r1", started_at=run_audit.now_iso(), finished_at=run_audit.now_iso(),
        duration_ms=10, result="completed", collector_version="1.0.4",
    )
    ok = run_audit.append_run_record(path, record)
    assert ok is True
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert json.loads(lines[0])["run_id"] == "r1"


def test_multiple_appends_produce_multiple_distinct_records(tmp_path):
    path = tmp_path / "runs.jsonl"
    for i in range(3):
        record = run_audit.build_record(
            run_id=f"r{i}", started_at=run_audit.now_iso(), finished_at=run_audit.now_iso(),
            duration_ms=1, result="completed", collector_version="1.0.4",
        )
        assert run_audit.append_run_record(path, record) is True

    records = _read_records(path)
    assert [r["run_id"] for r in records] == ["r0", "r1", "r2"]


def test_torn_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "runs.jsonl"
    good = run_audit.build_record(
        run_id="good", started_at=run_audit.now_iso(), finished_at=run_audit.now_iso(),
        duration_ms=1, result="completed", collector_version="1.0.4",
    )
    path.write_text(json.dumps(good) + "\n" + '{"run_id": "torn", "started_at": "2026-01-0', encoding="utf-8")

    # Reading tolerates the torn line...
    records = _read_records(path)
    assert [r["run_id"] for r in records] == ["good"]

    # ...and appending (which prunes) does too -- no exception, and the
    # torn line is dropped rather than corrupting the whole file.
    new_record = run_audit.build_record(
        run_id="new", started_at=run_audit.now_iso(), finished_at=run_audit.now_iso(),
        duration_ms=1, result="completed", collector_version="1.0.4",
    )
    assert run_audit.append_run_record(path, new_record) is True
    records_after = _read_records(path)
    assert [r["run_id"] for r in records_after] == ["good", "new"]


def test_retention_drops_records_older_than_30_days(tmp_path):
    path = tmp_path / "runs.jsonl"
    now = datetime.now(UTC)
    old_started_at = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recent_started_at = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    old_record = run_audit.build_record(
        run_id="old", started_at=old_started_at, finished_at=old_started_at,
        duration_ms=1, result="completed", collector_version="1.0.4",
    )
    recent_record = run_audit.build_record(
        run_id="recent", started_at=recent_started_at, finished_at=recent_started_at,
        duration_ms=1, result="completed", collector_version="1.0.4",
    )
    path.write_text(json.dumps(old_record) + "\n" + json.dumps(recent_record) + "\n", encoding="utf-8")

    new_record = run_audit.build_record(
        run_id="new", started_at=run_audit.now_iso(), finished_at=run_audit.now_iso(),
        duration_ms=1, result="completed", collector_version="1.0.4",
    )
    assert run_audit.append_run_record(path, new_record) is True

    remaining = {r["run_id"] for r in _read_records(path)}
    assert remaining == {"recent", "new"}
    assert "old" not in remaining


def test_retention_skips_rewrite_when_nothing_has_expired(tmp_path, monkeypatch):
    path = tmp_path / "runs.jsonl"
    record = run_audit.build_record(
        run_id="r1", started_at=run_audit.now_iso(), finished_at=run_audit.now_iso(),
        duration_ms=1, result="completed", collector_version="1.0.4",
    )
    assert run_audit.append_run_record(path, record) is True

    def _boom(*_a, **_kw):
        raise AssertionError("rewrite should not have been attempted -- nothing expired")

    monkeypatch.setattr(run_audit, "_atomic_rewrite_lines", _boom)
    record2 = run_audit.build_record(
        run_id="r2", started_at=run_audit.now_iso(), finished_at=run_audit.now_iso(),
        duration_ms=1, result="completed", collector_version="1.0.4",
    )
    # Would raise via _boom if a rewrite were (incorrectly) attempted.
    assert run_audit.append_run_record(path, record2) is True


def test_append_failure_is_swallowed_and_returns_false(tmp_path, monkeypatch, caplog):
    path = tmp_path / "runs.jsonl"

    def _boom(*_a, **_kw):
        raise OSError("disk is full")

    monkeypatch.setattr("builtins.open", _boom)
    record = run_audit.build_record(
        run_id="r", started_at=run_audit.now_iso(), finished_at=run_audit.now_iso(),
        duration_ms=1, result="completed", collector_version="1.0.4",
    )
    ok = run_audit.append_run_record(path, record, logger=logging.getLogger("test.audit"))
    assert ok is False


def test_append_failure_message_never_contains_the_raw_exception_text(tmp_path, monkeypatch, caplog):
    path = tmp_path / "runs.jsonl"
    secret = "barcode=99999999999999 patron_id=SECRET-PATRON-42"

    def _boom(*_a, **_kw):
        raise OSError(secret)

    monkeypatch.setattr("builtins.open", _boom)
    logger = logging.getLogger("test.audit.secret")
    with caplog.at_level(logging.WARNING, logger="test.audit.secret"):
        record = run_audit.build_record(
            run_id="r", started_at=run_audit.now_iso(), finished_at=run_audit.now_iso(),
            duration_ms=1, result="completed", collector_version="1.0.4",
        )
        run_audit.append_run_record(path, record, logger=logger)

    logged_text = "\n".join(caplog.messages)
    assert secret not in logged_text
    assert "OSError" in logged_text  # the exception TYPE name is fine, just never its message


# --- locking -----------------------------------------------------------
#
# _file_lock uses msvcrt.locking on win32 and fcntl.flock on POSIX (see
# collector/run_audit.py's own CONCURRENCY section) -- real inter-process/
# inter-thread mutual exclusion on both, not a real-lock-on-Windows-only,
# no-op-elsewhere pair. These tests are written generically (no
# `sys.platform` branching of their own) specifically so the SAME test
# file exercises and verifies real locking semantics whichever platform
# actually runs it -- this project's Windows dev/production runs, and
# Linux CI, both get a real, meaningful assertion, not a skipped one.


def test_file_lock_round_trips_immediately_when_uncontended(tmp_path):
    """The most basic platform-agnostic proof the active primitive
    (msvcrt on win32, fcntl on POSIX) actually acquires and releases: no
    contention at all, so this must return promptly, and a second,
    sequential acquire must also succeed (proving release genuinely freed
    it rather than leaving it held)."""
    lock_path = tmp_path / "runs.jsonl.lock"
    with run_audit._file_lock(lock_path):
        pass
    with run_audit._file_lock(lock_path):
        pass


def test_file_lock_serializes_two_threads(tmp_path):
    lock_path = tmp_path / "runs.jsonl.lock"
    events: list[str] = []

    def worker(name, hold_seconds):
        with run_audit._file_lock(lock_path):
            events.append(f"{name}-start")
            time.sleep(hold_seconds)
            events.append(f"{name}-end")

    t1 = threading.Thread(target=worker, args=("A", 0.15))
    t2 = threading.Thread(target=worker, args=("B", 0.0))
    t1.start()
    time.sleep(0.03)  # give A a head start so it reliably acquires first
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert events[0] == "A-start"
    assert events[1] == "A-end"  # A's own end must precede B's start -- no interleaving
    assert events[2] == "B-start"
    assert events[3] == "B-end"


def test_file_lock_times_out_rather_than_hanging_forever(tmp_path):
    lock_path = tmp_path / "runs.jsonl.lock"
    held = threading.Event()
    release = threading.Event()

    def holder():
        with run_audit._file_lock(lock_path):
            held.set()
            release.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(timeout=5)
    try:
        with pytest.raises(OSError), run_audit._file_lock(lock_path, timeout=0.2):
            pass  # pragma: no cover
    finally:
        release.set()
        t.join(timeout=5)


# ---------------------------------------------------------------------
# End-to-end via collector.run.main()
# ---------------------------------------------------------------------


def test_successful_run_record(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    session = FakeSession(script=[_FakeResponse(200, {"status": "success", "checkins_inserted": 1})])
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: session)
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 0

    records = _read_records(tmp_path / "runs.jsonl")
    assert len(records) == 1
    record = records[0]
    assert record["result"] == "completed"
    assert record["collector_version"] == run_mod.__version__
    assert record["failure_stage"] is None
    assert record["error_code"] is None
    assert record["http"]["upload_status"] == 200
    assert record["http"]["pipeline_status"] == 200
    assert record["sources"]["checkins"]["new_records"] == 1
    assert record["sources"]["checkins"]["uploaded"] == 1
    assert record["sources"]["checkins"]["offset_before"] is None  # first-ever run
    assert record["sources"]["checkins"]["offset_after"] is not None


def test_no_new_rows_run_record(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 0

    records = _read_records(tmp_path / "runs.jsonl")
    assert records[0]["result"] == "completed_no_new_rows"
    assert records[0]["http"]["upload_status"] is None  # no batches were attempted
    assert records[0]["http"]["pipeline_status"] == 200  # the heartbeat still fires


def test_upload_failure_record(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    _configure_a_passthrough_parser_for_testing(monkeypatch)
    monkeypatch.setattr(
        run_mod.uploader, "build_session",
        lambda: FakeSession(script=[_FakeResponse(401, text="Token scope does not match customer_id / branch_id")]),
    )

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 1

    records = _read_records(tmp_path / "runs.jsonl")
    record = records[0]
    assert record["result"] == "failed_upload"
    assert record["failure_stage"] == "upload"
    assert record["error_code"] == "auth_failure"
    assert record["http"]["upload_status"] == 401
    # No raw response text anywhere in the record.
    assert "Token scope" not in json.dumps(record)


def test_corrupt_state_failure_record(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "state.json").write_text("{not valid json", encoding="utf-8")
    session = FakeSession()
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: session)
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 1

    records = _read_records(tmp_path / "runs.jsonl")
    record = records[0]
    assert record["result"] == "failed_corrupt_state"
    assert record["failure_stage"] == "state"
    assert record["error_code"] == "state_corrupt"
    assert record["sources"] == {}  # the per-source loop never ran
    # run_once returns before ever calling uploader.post_status for a
    # corrupt state file (it fails before any network call) -- both HTTP
    # fields must be null, never an invented/approximated value, since
    # neither call genuinely happened this run.
    assert record["http"] == {"upload_status": None, "pipeline_status": None}
    assert len(session.calls) == 0  # confirms no HTTP call was made at all this run


def test_config_failure_before_cfg_exists_writes_to_the_fallback_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")

    exit_code = run_mod.main(["--config", str(tmp_path / "does-not-exist.json")])
    assert exit_code == 2

    # The autouse fixture in tests/conftest.py redirects
    # run_audit.DEFAULT_FALLBACK_AUDIT_PATH into this test's own tmp_path
    # (see that fixture's docstring) -- read it back from there, exactly
    # as collector/run.py's ConfigError branch would have written it.
    records = _read_records(run_audit.DEFAULT_FALLBACK_AUDIT_PATH)
    assert len(records) == 1
    record = records[0]
    assert record["result"] == "failed_config"
    assert record["failure_stage"] == "config"
    assert record["error_code"] == "config_invalid"
    assert record["sources"] == {}


def test_parser_not_configured_failure_record(monkeypatch, tmp_path, capsys):
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
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 2

    # cfg DID exist for this failure (unlike the config-load case above),
    # so the record lands at cfg's own resolved path, next to collector.log
    # -- never the pre-config fallback.
    records = _read_records(tmp_path / "runs.jsonl")
    record = records[0]
    assert record["result"] == "failed_parser_not_configured"
    assert record["failure_stage"] == "parser_wiring"
    assert record["error_code"] == "parser_not_configured"
    assert "mystery_source" not in json.dumps(record)  # the source name never lands in the audit record either


def test_unhandled_exception_record(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())

    def boom(*args, **kwargs):
        raise RuntimeError("simulated unexpected bug with a secret token=abc123")

    monkeypatch.setattr(run_mod, "run_once", boom)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 1

    records = _read_records(tmp_path / "runs.jsonl")
    record = records[0]
    assert record["result"] == "failed_unhandled"
    assert record["failure_stage"] == "unhandled"
    assert record["error_code"] == "unhandled_exception"
    assert "token=abc123" not in json.dumps(record)
    assert "simulated unexpected bug" not in json.dumps(record)


def test_offsets_before_and_after_advance_across_two_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    _configure_a_passthrough_parser_for_testing(monkeypatch)
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())

    checkins_path = tmp_path / "Checkins.txt"
    # Binary writes -- collector/reader.py's offsets are true physical
    # byte counts (see its own module docstring), and a text-mode write
    # on Windows silently translates "\n" to "\r\n", which would make any
    # byte-offset expectation below wrong by one byte per line.
    checkins_path.write_bytes(b"line one\n")
    assert run_mod.main(["--config", str(config_path)]) == 0
    size_after_run1 = checkins_path.stat().st_size

    with open(checkins_path, "ab") as f:
        f.write(b"line two\n")
    assert run_mod.main(["--config", str(config_path)]) == 0
    size_after_run2 = checkins_path.stat().st_size

    records = _read_records(tmp_path / "runs.jsonl")
    assert len(records) == 2
    run1, run2 = records
    assert run1["sources"]["checkins"]["offset_before"] is None
    assert run1["sources"]["checkins"]["offset_after"] == size_after_run1
    # Run 2 must resume from exactly where run 1 left off.
    assert run2["sources"]["checkins"]["offset_before"] == run1["sources"]["checkins"]["offset_after"]
    assert run2["sources"]["checkins"]["offset_after"] == size_after_run2
    assert run2["sources"]["checkins"]["new_records"] == 1


def test_new_records_and_uploaded_counters_reflect_backend_dedup(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\nline two\n", encoding="utf-8")
    _configure_a_passthrough_parser_for_testing(monkeypatch)
    # The backend reports fewer inserted than sent (e.g. one was already a
    # duplicate) -- new_records and uploaded must be allowed to differ.
    monkeypatch.setattr(
        run_mod.uploader, "build_session",
        lambda: FakeSession(script=[_FakeResponse(200, {"status": "success", "checkins_inserted": 1})]),
    )

    assert run_mod.main(["--config", str(config_path)]) == 0

    record = _read_records(tmp_path / "runs.jsonl")[0]
    assert record["sources"]["checkins"]["new_records"] == 2
    assert record["sources"]["checkins"]["uploaded"] == 1


def test_duration_ms_uses_monotonic_clock_not_wall_clock(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    # Freeze started_at and finished_at to the exact SAME wall-clock
    # string -- wall-clock subtraction would give exactly 0ms. duration_ms
    # must instead come from time.perf_counter(), which reports a real,
    # controlled 250ms elapsed here regardless of what the wall clock says.
    # A genuinely recent (not stale-past) frozen value is used deliberately
    # -- a stale one would make the record look >30 days old and get
    # pruned by the very same append_run_record call that just wrote it.
    frozen_now = run_audit.now_iso()
    monkeypatch.setattr(run_mod.run_audit, "now_iso", lambda: frozen_now)
    perf_values = [100.0, 100.25]

    def _fake_perf_counter():
        return perf_values.pop(0) if perf_values else 100.25  # clamp defensively rather than raise on an extra call

    monkeypatch.setattr(run_mod.time, "perf_counter", _fake_perf_counter)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 0

    record = _read_records(tmp_path / "runs.jsonl")[0]
    assert record["started_at"] == record["finished_at"] == frozen_now
    assert record["duration_ms"] == 250


def test_multiple_invocations_append_multiple_records(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    for _ in range(3):
        assert run_mod.main(["--config", str(config_path)]) == 0

    records = _read_records(tmp_path / "runs.jsonl")
    assert len(records) == 3
    assert len({r["run_id"] for r in records}) == 3  # each run gets its own unique id


def test_audit_write_failure_does_not_change_collector_exit_or_result(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    def _boom(*_a, **_kw):
        raise OSError("simulated disk failure writing the audit record")

    monkeypatch.setattr(run_mod.run_audit, "append_run_record", _boom)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 0  # unaffected by the audit-write failure
    assert (tmp_path / "state.json").exists()  # the run itself still completed normally


def test_audit_prune_failure_does_not_change_collector_exit_or_result(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: FakeSession())
    _configure_a_passthrough_parser_for_testing(monkeypatch)

    def _boom(*_a, **_kw):
        raise OSError("simulated failure during retention pruning")

    monkeypatch.setattr(run_audit, "_prune_locked", _boom)

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 0
    assert (tmp_path / "state.json").exists()
    # The append itself still happened before pruning blew up (pruning
    # runs after the write, inside the same locked block) -- but even if
    # it hadn't, the assertion above (exit code + state) is what actually
    # matters: a local audit-logging failure must never surface here.


# --- privacy canary ------------------------------------------------------


def test_privacy_canary_forbidden_content_never_appears_in_a_run_record(monkeypatch, tmp_path):
    """End-to-end across all three sources -- checkins, rejects, AND acs
    (the only source with a patron_id field) -- flowing through the REAL
    production parsers (not the passthrough stub), uploaded with a real
    token in the request, so the resulting audit record is checked
    against every forbidden category at once: patron identifier, barcode,
    title, destination, reject error message, source line content/path,
    and credentials. Only aggregate counts, offsets, source NAMES, and
    safe codes may appear."""
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "CANARY-SECRET-TOKEN-abc123")
    doc = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://example.invalid",
        "sources": [
            {"name": "checkins", "path": str(tmp_path / "Checkins.txt")},
            {"name": "rejects", "path": str(tmp_path / "Rejects.txt")},
            {"name": "acs", "path": str(tmp_path / "ACS Log.txt")},
        ],
        "state_path": str(tmp_path / "state.json"),
        "status_path": str(tmp_path / "status.json"),
        "log_path": str(tmp_path / "collector.log"),
    }
    config_path = tmp_path / "collector_config.json"
    config_path.write_text(json.dumps(doc), encoding="utf-8")

    checkins_line = "Sunny days /|33472004192508|MLEPB|E KERBEL NATURE|000|1|False||4|N|N|N|8/31/2026|4:18:39 PM"
    (tmp_path / "Checkins.txt").write_text(checkins_line + "\n", encoding="utf-8")
    rejects_line = "44409912345678|Item Not Found -- CANARY-REJECT-MESSAGE-MARKER|8/31/2026|4:19:00 PM"
    (tmp_path / "Rejects.txt").write_text(rejects_line + "\n", encoding="utf-8")
    # Real ACS control-character/tag shape (see tests/test_parser_canonical.py):
    # AB=barcode, AJ=title, AA=patron_id, CT=destination.
    acs_line = "\x018/31/2026\x024:20:00 PM\x02CK|AB99988877766|AJCANARY-ACS-TITLE|AACANARY-PATRON-ID-999|CTWestside\x01"
    (tmp_path / "ACS Log.txt").write_bytes(acs_line.encode("utf-8") + b"\n")

    session = FakeSession()
    monkeypatch.setattr(run_mod.uploader, "build_session", lambda: session)
    # Deliberately NOT calling _configure_a_passthrough_parser_for_testing --
    # this exercises the real field values a naive implementation might be
    # tempted to copy from the parsed record into the audit record.

    exit_code = run_mod.main(["--config", str(config_path)])
    assert exit_code == 0
    # Sanity: the real parsers really did run and really did produce
    # upload payloads containing this content (proving the canary would
    # actually catch a leak, not just checking an empty/unexercised path).
    upload_payload = next(p for url, p in session.calls if url.endswith("/upload"))
    assert upload_payload["checkins"][0]["barcode"] == "33472004192508"
    assert upload_payload["acs"][0]["patron_id"] == "CANARY-PATRON-ID-999"

    raw_file_text = (tmp_path / "runs.jsonl").read_text(encoding="utf-8")
    forbidden = [
        "33472004192508",              # checkins barcode
        "E KERBEL NATURE",              # checkins title
        "Sunny days",                   # checkins title fragment
        "44409912345678",               # rejects barcode
        "CANARY-REJECT-MESSAGE-MARKER", # reject error message
        "99988877766",                  # acs barcode
        "CANARY-ACS-TITLE",             # acs title
        "CANARY-PATRON-ID-999",         # acs patron identifier
        "Westside",                     # acs/checkins destination
        "CANARY-SECRET-TOKEN-abc123",   # API token
        "Bearer",                       # Authorization header shape
        str(tmp_path / "Checkins.txt"), # filesystem source path
        "Checkins.txt", "Rejects.txt", "ACS Log.txt",  # bare filenames -- only source NAMES are allowed
    ]
    for value in forbidden:
        assert value not in raw_file_text, f"forbidden value leaked into runs.jsonl: {value!r}"

    record = _read_records(tmp_path / "runs.jsonl")[0]
    for source_name in ("checkins", "rejects", "acs"):
        assert record["sources"][source_name]["new_records"] == 1
        assert set(record["sources"][source_name].keys()) == {"offset_before", "offset_after", "new_records", "uploaded"}


def test_build_record_has_no_field_capable_of_carrying_forbidden_content():
    """build_record's own signature is the real guarantee: SourceAudit has
    exactly 4 fixed int|None fields (see SourceAudit.to_dict), and every
    other parameter is a plain scalar or a short list of source NAMES --
    there is no parameter that accepts a barcode, title, raw line, or
    token in the first place. This asserts that structural guarantee
    directly (field names), rather than searching output text for
    specific strings, which the end-to-end canary test above already does."""
    assert {f.name for f in dataclasses.fields(run_audit.SourceAudit)} == {
        "offset_before", "offset_after", "new_records", "uploaded",
    }
    record = run_audit.build_record(
        run_id="r", started_at="s", finished_at="f", duration_ms=1, result="completed",
        collector_version="1.0.4",
        sources={"checkins": run_audit.SourceAudit(offset_before=1, offset_after=2, new_records=3, uploaded=3)},
    )
    assert set(record["sources"]["checkins"].keys()) == {"offset_before", "offset_after", "new_records", "uploaded"}
    assert set(record.keys()) == {
        "run_id", "started_at", "finished_at", "duration_ms", "result", "collector_version",
        "sources", "sources_missing", "sources_rotated", "sources_truncated", "http",
        "failure_stage", "error_code",
    }
