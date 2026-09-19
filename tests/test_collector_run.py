"""Tests for collector/run.py -- SortView Collector v1 (Phase 4a).

Central properties under test: multi-batch upload state semantics (a
failure after some successful batches leaves state at the PRIOR
committed offset and safely resends everything on retry), fatal failures
exit nonzero, missing/stale source handling is non-fatal, and a scope
mismatch is surfaced clearly rather than genericized.
"""

from __future__ import annotations

import json
import logging

from collector import state as state_mod
from collector.config import CollectorConfig, SourceConfig
from collector.run import run_once


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


def _logger():
    logger = logging.getLogger("test.collector")
    logger.addHandler(logging.NullHandler())
    return logger


def _cfg(tmp_path, **overrides):
    kwargs = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://example.invalid",
        "api_token": "test-token",
        "sources": (
            SourceConfig(name="checkins", path=str(tmp_path / "Checkins.txt")),
            SourceConfig(name="rejects", path=str(tmp_path / "Rejects.txt")),
            SourceConfig(name="acs", path=str(tmp_path / "ACS Log.txt")),
        ),
        "state_path": tmp_path / "state.json",
        "status_path": tmp_path / "status.json",
        "log_path": tmp_path / "collector.log",
        "max_records_per_batch": 10,
    }
    kwargs.update(overrides)
    return CollectorConfig(**kwargs)


def _passthrough(lines):
    return [{"raw_line": line} for line in lines]


# run_once() now requires an entry for every CONFIGURED source (checkins,
# rejects, acs -- see _cfg above), regardless of whether that source
# currently has any file/data, since Phase 4a's fail-closed correction
# checks parser configuration upfront, before reading anything. Tests
# below that only care about one source's behavior still use this full
# mapping -- a parse function is only ever actually CALLED for a source
# that has new lines, so this is always safe to pass regardless of which
# files exist in a given test.
_ALL_PASSTHROUGH = {"checkins": _passthrough, "rejects": _passthrough, "acs": _passthrough}


def _write_lines(path, n, prefix="line"):
    path.write_text("".join(f"{prefix}{i}\n" for i in range(n)), encoding="utf-8")


# --- clean runs -----------------------------------------------------------


def test_first_run_with_no_source_files_completes_with_no_new_rows(tmp_path):
    cfg = _cfg(tmp_path)
    session = FakeSession()

    outcome = run_once(cfg, session=session, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome.exit_code == 0
    assert outcome.status["status"] == "completed_no_new_rows"
    assert set(outcome.status["sources_missing"]) == {"checkins", "rejects", "acs"}
    assert session.calls  # status was still POSTed


def test_successful_run_persists_new_state_and_uploads(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\nline two\n", encoding="utf-8")
    session = FakeSession(
        script=[
            _FakeResponse(
                200,
                {
                    "status": "success",
                    "checkins_inserted": 2,
                    "rejects_inserted": 0,
                    "acs_inserted": 0,
                },
            ),
            _FakeResponse(200, {"status": "success"}),
        ]
    )

    outcome = run_once(
        cfg, session=session, parse_fns=_ALL_PASSTHROUGH, logger=_logger()
    )

    assert outcome.exit_code == 0
    assert outcome.status["status"] == "completed"
    assert outcome.status["checkins_rows"] == 2
    assert outcome.status["uploaded_checkins_rows"] == 2
    assert outcome.status["uploaded_rejects_rows"] == 0
    assert outcome.status["uploaded_acs_rows"] == 0

    loaded_state = state_mod.load_state(cfg.state_path)
    checkins_state = state_mod.get_source(loaded_state, "checkins")
    assert checkins_state is not None
    # os.path.getsize(), not a hand-computed byte length -- Path.write_text
    # translates "\n" -> "\r\n" on Windows by default, so the real
    # on-disk size is not simply len(the string as UTF-8).
    assert checkins_state.offset == (tmp_path / "Checkins.txt").stat().st_size

    upload_call = next(c for c in session.calls if c[0].endswith("/upload"))
    assert upload_call[1]["checkins"] == [{"raw_line": "line one\n"}, {"raw_line": "line two\n"}]


def test_three_consecutive_runs_never_falsely_detect_rotation(tmp_path):
    # Regression test: an earlier version of run_once() passed
    # collector.state's plain-tuple identity directly into
    # collector.reader's FileIdentity-typed cursor without converting,
    # which compares unequal to a real FileIdentity on every single
    # comparison -- silently forcing a false "rotated" detection (full
    # replay) on every run after the first. Caught by this phase's own
    # test suite before shipping; guarded here explicitly so it can never
    # regress silently.
    cfg = _cfg(tmp_path)
    path = tmp_path / "Checkins.txt"

    path.write_text("line one\n", encoding="utf-8")
    outcome1 = run_once(cfg, session=FakeSession(), parse_fns=_ALL_PASSTHROUGH, logger=_logger())
    assert outcome1.status["sources_rotated"] == []

    with open(path, "a", encoding="utf-8") as f:
        f.write("line two\n")
    outcome2 = run_once(cfg, session=FakeSession(), parse_fns=_ALL_PASSTHROUGH, logger=_logger())
    assert outcome2.status["sources_rotated"] == []
    assert outcome2.status["checkins_rows"] == 1  # only the new line, not a full replay

    with open(path, "a", encoding="utf-8") as f:
        f.write("line three\n")
    outcome3 = run_once(cfg, session=FakeSession(), parse_fns=_ALL_PASSTHROUGH, logger=_logger())
    assert outcome3.status["sources_rotated"] == []
    assert outcome3.status["checkins_rows"] == 1


def test_second_run_only_uploads_new_lines(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")

    run_once(cfg, session=FakeSession(), parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
        f.write("line two\n")

    session2 = FakeSession()
    outcome2 = run_once(cfg, session=session2, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome2.status["checkins_rows"] == 1
    upload_call = next(c for c in session2.calls if c[0].endswith("/upload"))
    assert upload_call[1]["checkins"] == [{"raw_line": "line two\n"}]


# --- multi-batch upload state semantics (Phase 3 item 7) -------------------


def test_partial_batch_failure_leaves_state_untouched_and_retry_resends_everything(tmp_path):
    cfg = _cfg(tmp_path, max_records_per_batch=10)
    _write_lines(tmp_path / "Checkins.txt", 15)  # 15 records -> 2 batches (10 + 5)

    # Run A: batch 1 (10 records) succeeds, batch 2 (5 records) fails.
    session_a = FakeSession(script=[
        _FakeResponse(200, {"status": "success"}),
        _FakeResponse(500, text="server error"),
    ])

    outcome_a = run_once(cfg, session=session_a, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome_a.exit_code == 1
    assert outcome_a.status["status"] == "failed_upload"
    # State must NOT exist at all -- this was the first run, nothing was
    # ever committed, so nothing may be persisted despite batch 1 having
    # been genuinely delivered to the backend.
    assert not cfg.state_path.exists()
    upload_calls_a = [c for c in session_a.calls if c[0].endswith("/upload")]
    assert len(upload_calls_a) == 2  # both batches were attempted

    # Run B (retry -- in production, the next normal 15-minute scheduled
    # run; NOT a Task Scheduler RestartOnFailure firing, which Phase 4d
    # Section H proved does not activate for a clean nonzero exit): a
    # fresh session that succeeds this time. The file on disk is
    # unchanged (still 15 lines) -- the retry must re-read and re-send
    # ALL 15 records, including the 10 that were already delivered in
    # Run A's batch 1 (the backend's existing semantic dedup is what
    # makes that resend safe -- this test proves the CLIENT resends
    # everything, not that the backend dedupes, which is out of scope
    # here).
    session_b = FakeSession()
    outcome_b = run_once(cfg, session=session_b, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome_b.exit_code == 0
    assert outcome_b.status["status"] == "completed"
    assert outcome_b.status["checkins_rows"] == 15  # all 15, not just the 5 that "failed" before

    upload_calls_b = [c for c in session_b.calls if c[0].endswith("/upload")]
    total_resent = sum(len(c[1]["checkins"]) for c in upload_calls_b)
    assert total_resent == 15

    loaded_state = state_mod.load_state(cfg.state_path)
    assert state_mod.get_source(loaded_state, "checkins").offset == (tmp_path / "Checkins.txt").stat().st_size


def test_failure_on_one_source_leaves_ALL_sources_state_untouched(tmp_path):
    # Even a source whose own read/parse succeeded independently must not
    # have its state advanced if upload (of the combined batch) fails --
    # state is committed once, for everything, or not at all.
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("checkin line\n", encoding="utf-8")
    (tmp_path / "Rejects.txt").write_text("reject line\n", encoding="utf-8")

    session = FakeSession(script=[_FakeResponse(500, text="down")])
    outcome = run_once(
        cfg, session=session,
        parse_fns=_ALL_PASSTHROUGH,
        logger=_logger(),
    )

    assert outcome.exit_code == 1
    assert not cfg.state_path.exists()


def test_upload_failure_status_preserves_scope_mismatch_message(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")

    session = FakeSession(script=[
        _FakeResponse(403, text="Token scope does not match customer_id / branch_id")
    ])
    outcome = run_once(cfg, session=session, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome.exit_code == 1
    assert outcome.status["last_failure_category"] == "auth_failure"
    assert "Token scope does not match customer_id / branch_id" in outcome.status["last_error"]


# --- missing/stale source handling -----------------------------------------


def test_one_missing_source_does_not_block_the_others(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    # Rejects.txt and ACS Log.txt intentionally not created.

    session = FakeSession()
    outcome = run_once(cfg, session=session, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome.exit_code == 0
    assert outcome.status["checkins_rows"] == 1
    assert set(outcome.status["sources_missing"]) == {"rejects", "acs"}

    loaded_state = state_mod.load_state(cfg.state_path)
    assert state_mod.get_source(loaded_state, "checkins") is not None
    assert state_mod.get_source(loaded_state, "rejects") is None  # never touched


def test_missing_source_never_raises(tmp_path):
    cfg = _cfg(tmp_path)
    outcome = run_once(cfg, session=FakeSession(), parse_fns=_ALL_PASSTHROUGH, logger=_logger())
    assert outcome.exit_code == 0


# --- corrupt state handling -------------------------------------------------


def test_corrupt_state_file_fails_the_run_without_guessing(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_path.write_text("{not valid json", encoding="utf-8")

    outcome = run_once(cfg, session=FakeSession(), parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome.exit_code == 1
    assert outcome.status["status"] == "failed_corrupt_state"
    # The corrupt file is untouched -- recovery is an explicit operator
    # action (state.quarantine_corrupt_file), never automatic.
    assert cfg.state_path.read_text(encoding="utf-8") == "{not valid json"


# --- status is always written, even on failure ------------------------------


def test_status_is_written_atomically_even_when_upload_fails(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")

    session = FakeSession(script=[_FakeResponse(500, text="down")])
    run_once(cfg, session=session, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert cfg.status_path.exists()
    written = state_mod.load_status(cfg.status_path)
    assert written["status"] == "failed_upload"


def test_best_effort_status_post_failure_does_not_fail_an_otherwise_successful_run(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")

    # /upload succeeds, /upload-pipeline-status fails -- must not flip
    # the run's own exit code.
    session = FakeSession(script=[
        _FakeResponse(200, {"status": "success"}),  # /upload
        _FakeResponse(500, text="status endpoint down"),  # /upload-pipeline-status
    ])
    outcome = run_once(cfg, session=session, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome.exit_code == 0


# --- installation lifecycle linkage ---------------------------------------


def test_successful_run_heartbeat_carries_the_configured_installation(tmp_path):
    from collector import __version__

    cfg = _cfg(tmp_path, installation_id=41)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    session = FakeSession()

    outcome = run_once(cfg, session=session, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome.exit_code == 0
    status_calls = [payload for url, payload in session.calls if url.endswith("/upload-pipeline-status")]
    assert len(status_calls) == 1
    assert status_calls[0]["installation_id"] == 41
    assert status_calls[0]["collector_version"] == __version__
    upload_calls = [payload for url, payload in session.calls if url.endswith("/upload")]
    assert upload_calls and all("installation_id" not in p for p in upload_calls)


def test_legacy_config_run_is_unchanged_and_sends_no_installation_fields(tmp_path):
    cfg = _cfg(tmp_path)  # no installation_id: a deployed 1.0.2-style config
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    session = FakeSession()

    outcome = run_once(cfg, session=session, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome.exit_code == 0
    status_calls = [payload for url, payload in session.calls if url.endswith("/upload-pipeline-status")]
    assert len(status_calls) == 1
    assert "installation_id" not in status_calls[0]
    assert "collector_version" not in status_calls[0]


def test_rejected_installation_heartbeat_does_not_fail_an_otherwise_successful_run(tmp_path):
    # The API rejects a wrong/inactive installation_id with a 403 for the WHOLE
    # heartbeat. That must be a warning only: the data upload already
    # succeeded and its state was committed.
    cfg = _cfg(tmp_path, installation_id=41)
    (tmp_path / "Checkins.txt").write_text("line one\n", encoding="utf-8")
    session = FakeSession(script=[
        _FakeResponse(200, {"status": "success"}),  # /upload
        _FakeResponse(403, text='{"detail": "Collector installation is not authorized to report status"}'),
    ])

    outcome = run_once(cfg, session=session, parse_fns=_ALL_PASSTHROUGH, logger=_logger())

    assert outcome.exit_code == 0
    assert cfg.state_path.exists()
