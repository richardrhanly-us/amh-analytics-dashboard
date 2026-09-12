"""Integration tests for agent/runtime/supervisor.py's AgentRunner
(Continuous Ingestion Phase F).

Exercises the whole wiring -- collector, uploader, heartbeat,
housekeeping -- as real background threads against real files on disk,
with a scripted FakeSession standing in for the network. No real HTTP,
no SQLite, no mocks of state.py/spool.py/tailer.py themselves.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

from agent import spool, state
from agent.runtime.config import RuntimeConfig, SourceConfig
from agent.runtime.supervisor import AgentRunner

CHECKIN_LINE = "Book A|{barcode}|MLFIC|FIC A|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM\n"


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {"status": "success"}
        self.text = text

    def json(self):
        return self._json_body


class FakeSession:
    """Always succeeds for /upload-pipeline-status. For /upload, replays
    a scripted sequence of responses (default: always succeed with all
    counts reported as 1)."""

    def __init__(self, upload_script=None):
        self.upload_script = list(upload_script) if upload_script is not None else None
        self.upload_calls = []
        self.status_calls = []

    def post(self, url, json, headers, timeout):
        if url.endswith("/upload-pipeline-status"):
            self.status_calls.append((url, json))
            return _FakeResponse(200, {"status": "success"})

        self.upload_calls.append((url, json))
        if self.upload_script is not None and self.upload_script:
            next_item = self.upload_script.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item
        return _FakeResponse(
            200, {"status": "success", "checkins_inserted": 1, "rejects_inserted": 1, "acs_inserted": 1}
        )


def _cfg(tmp_path, **overrides):
    kwargs = {
        "customer_id": 100,
        "branch_id": 5,
        "api_url": "https://example.invalid",
        "api_token": "test-token",
        "sources": (SourceConfig(name="checkins", path=str(tmp_path / "Checkins.txt")),),
        "state_path": tmp_path / "state" / "agent_state.json",
        "spool_root": tmp_path / "spool",
        "agent_identity_path": tmp_path / "agent_identity.json",
        "log_dir": tmp_path / "logs",
        "diagnostics_dir": tmp_path / "diagnostics",
        "collector_poll_seconds": 0.02,
        "uploader_poll_seconds": 0.02,
        "heartbeat_interval_seconds": 0.05,
        "housekeeping_interval_seconds": 0.05,
        "batch_max_events": 100,
        "batch_max_seconds": 0.05,
    }
    kwargs.update(overrides)
    return RuntimeConfig(**kwargs)


def _make_runner(tmp_path, cfg=None, upload_script=None):
    cfg = cfg or _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("", encoding="utf-8")
    runner = AgentRunner(cfg)
    fake_session = FakeSession(upload_script)
    runner.session = fake_session
    runner.uploader.session = fake_session
    runner.heartbeat.session = fake_session
    return runner, fake_session


def _wait_until(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# --- supervisor start/stop ---------------------------------------------


def test_supervisor_starts_all_four_components(tmp_path):
    runner, _session = _make_runner(tmp_path)
    runner.start()
    try:
        assert _wait_until(lambda: set(runner.health().keys()) == {"collector", "uploader", "heartbeat", "housekeeping"})
        health = runner.health()
        assert all(h.alive for h in health.values())
    finally:
        runner.stop(timeout=5.0)


def test_clean_shutdown_while_idle(tmp_path):
    runner, _session = _make_runner(tmp_path)
    runner.start()
    _wait_until(lambda: len(runner.health()) == 4)

    start = time.monotonic()
    runner.stop(timeout=5.0)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0
    assert all(not t.is_alive() for t in runner._threads)


def test_clean_shutdown_during_upload_retry_backoff(tmp_path):
    cfg = _cfg(tmp_path, uploader_backoff_base_seconds=10.0, uploader_backoff_max_seconds=60.0)
    runner, session = _make_runner(
        tmp_path, cfg=cfg, upload_script=[_FakeResponse(503, text="x")] * 20
    )
    spool.write_batch(cfg.spool_root, "checkins", [{"barcode": "1", "source_event_id": "a" * 64}], source_generation=0, start_offset=0, end_offset=10)

    runner.start()
    _wait_until(lambda: len(session.upload_calls) >= 1)  # let it enter backoff

    start = time.monotonic()
    runner.stop(timeout=5.0)
    elapsed = time.monotonic() - start

    # Even though backoff would otherwise sleep ~10s, shutdown must be prompt.
    assert elapsed < 5.0


def test_clean_shutdown_leaves_unacknowledged_spool_data_intact(tmp_path):
    cfg = _cfg(tmp_path)
    runner, session = _make_runner(tmp_path, cfg=cfg, upload_script=[Exception("boom")] * 20)
    path = spool.write_batch(
        cfg.spool_root, "checkins", [{"barcode": "1", "source_event_id": "b" * 64}], source_generation=0, start_offset=0, end_offset=10
    )

    runner.start()
    _wait_until(lambda: len(session.upload_calls) >= 1)
    runner.stop(timeout=5.0)

    assert path.exists()
    assert spool.read_batch(path) == [{"barcode": "1", "source_event_id": "b" * 64}]


def test_one_component_failure_is_surfaced_without_stopping_siblings(tmp_path):
    cfg = _cfg(tmp_path)
    runner, _session = _make_runner(tmp_path, cfg=cfg)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated heartbeat crash")

    runner.heartbeat.run_forever = boom  # type: ignore[method-assign]

    runner.start()
    try:
        assert _wait_until(lambda: not runner.health().get("heartbeat", type("x", (), {"alive": True})()).alive)
        health = runner.health()
        assert health["heartbeat"].alive is False
        assert "simulated heartbeat crash" in (health["heartbeat"].last_error or "")
        # Siblings must still be running.
        assert _wait_until(lambda: runner.health().get("collector") is not None and runner.health()["collector"].alive)
    finally:
        runner.stop(timeout=5.0)


def test_collector_thread_death_makes_heartbeat_report_degraded_not_healthy(tmp_path):
    # The exact regression scenario: a dead collector produces no new
    # pending batches, no quarantine activity, nothing that would
    # otherwise move any other health signal -- before the fix, this
    # heartbeat would have kept reporting "healthy" forever.
    cfg = _cfg(tmp_path)
    runner, session = _make_runner(tmp_path, cfg=cfg)

    def boom():
        raise RuntimeError("simulated collector crash")

    runner._collector_loop = boom  # type: ignore[method-assign]

    runner.start()
    try:
        assert _wait_until(lambda: not runner.health().get("collector", type("x", (), {"alive": True})()).alive)
        assert _wait_until(lambda: len(session.status_calls) >= 1, timeout=3.0)

        # Every heartbeat sent AFTER the collector died must report degraded.
        assert _wait_until(
            lambda: session.status_calls and session.status_calls[-1][1]["health_status"] == "degraded",
            timeout=3.0,
        )
        last_payload = session.status_calls[-1][1]
        assert "collector" in (last_payload["last_error"] or "")
    finally:
        runner.stop(timeout=5.0)


def test_uploader_thread_death_makes_heartbeat_report_degraded(tmp_path):
    cfg = _cfg(tmp_path)
    runner, session = _make_runner(tmp_path, cfg=cfg)

    def boom():
        raise RuntimeError("simulated uploader crash")

    runner._uploader_run_forever_with_status = boom  # type: ignore[method-assign]

    runner.start()
    try:
        assert _wait_until(lambda: not runner.health().get("uploader", type("x", (), {"alive": True})()).alive)
        assert _wait_until(
            lambda: session.status_calls and session.status_calls[-1][1]["health_status"] == "degraded",
            timeout=3.0,
        )
    finally:
        runner.stop(timeout=5.0)


def test_housekeeping_thread_death_marks_component_dead_but_pipeline_keeps_running(tmp_path):
    # housekeeping death still degrades the OVERALL health computation
    # (per the uniform policy adopted in heartbeat.py), but must not stop
    # collection/upload from continuing to function.
    cfg = _cfg(tmp_path)
    runner, session = _make_runner(tmp_path, cfg=cfg)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated housekeeping crash")

    runner.housekeeping.run_forever = boom  # type: ignore[method-assign]

    runner.start()
    try:
        assert _wait_until(lambda: not runner.health().get("housekeeping", type("x", (), {"alive": True})()).alive)

        with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
            f.write(CHECKIN_LINE.format(barcode="STILLWORKS1"))
        assert _wait_until(lambda: len(session.upload_calls) >= 1, timeout=5.0)
        assert session.upload_calls[0][1]["checkins"][0]["barcode"] == "STILLWORKS1"

        assert _wait_until(
            lambda: session.status_calls and session.status_calls[-1][1]["health_status"] == "degraded",
            timeout=3.0,
        )
    finally:
        runner.stop(timeout=5.0)


# --- end-to-end pipeline -------------------------------------------------


def test_end_to_end_append_parse_id_spool_upload_ack(tmp_path):
    cfg = _cfg(tmp_path)
    runner, session = _make_runner(tmp_path, cfg=cfg)

    runner.start()
    try:
        _wait_until(lambda: len(runner.health()) == 4)
        with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
            f.write(CHECKIN_LINE.format(barcode="E2E1"))

        assert _wait_until(lambda: len(session.upload_calls) >= 1, timeout=5.0)
        sent = session.upload_calls[0][1]
        assert sent["checkins"][0]["barcode"] == "E2E1"
        assert "source_event_id" in sent["checkins"][0]

        assert _wait_until(lambda: spool.list_pending_batches(cfg.spool_root, "checkins") == [], timeout=5.0)
    finally:
        runner.stop(timeout=5.0)


def test_restart_resumes_from_persisted_state_and_pending_spool(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "Checkins.txt").write_text("", encoding="utf-8")

    runner1 = AgentRunner(cfg)
    session1 = FakeSession(upload_script=[Exception("network down")] * 20)
    runner1.uploader.session = session1
    runner1.heartbeat.session = session1
    runner1.start()
    _wait_until(lambda: len(runner1.health()) == 4)

    with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="RESTART1"))

    _wait_until(lambda: spool.list_pending_batches(cfg.spool_root, "checkins") != [], timeout=5.0)
    runner1.stop(timeout=5.0)

    pending_before = spool.list_pending_batches(cfg.spool_root, "checkins")
    assert len(pending_before) == 1

    # "Restart": a fresh AgentRunner against the same durable state/spool.
    runner2 = AgentRunner(cfg)
    session2 = FakeSession()  # network is back
    runner2.uploader.session = session2
    runner2.heartbeat.session = session2
    runner2.start()
    try:
        assert _wait_until(lambda: spool.list_pending_batches(cfg.spool_root, "checkins") == [], timeout=5.0)
        assert len(session2.upload_calls) >= 1
        assert session2.upload_calls[0][1]["checkins"][0]["barcode"] == "RESTART1"
    finally:
        runner2.stop(timeout=5.0)


def test_agent_id_is_stable_across_restarts(tmp_path):
    cfg = _cfg(tmp_path)
    runner1 = AgentRunner(cfg)
    runner2 = AgentRunner(cfg)

    assert runner1.agent_id == runner2.agent_id


def test_diagnostics_snapshot_written_during_run(tmp_path):
    cfg = _cfg(tmp_path)
    runner, _session = _make_runner(tmp_path, cfg=cfg)
    runner.start()
    try:
        assert _wait_until(lambda: (cfg.diagnostics_dir / "diagnostics.json").exists(), timeout=3.0)
        doc = json.loads((cfg.diagnostics_dir / "diagnostics.json").read_text(encoding="utf-8"))
        assert doc["agent_id"] == runner.agent_id
        assert "checkins" in doc["sources"]
    finally:
        runner.stop(timeout=5.0)


# --- disk pressure integration ------------------------------------------


def test_disk_pressure_pause_prevents_cursor_advance(tmp_path):
    # ROOT CAUSE of the original flake, found by investigation (not
    # assumed): this is a genuine RUNTIME race in how the test engaged
    # disk pressure, not only a test-synchronization gap. The very first
    # collector cycle after start() also performs EOF bootstrap
    # (agent.runtime.collector.SourceCollector._bootstrap_if_needed).
    # With an absurd disk_pressure_min_free_bytes threshold active from
    # the moment the runner starts, there is a real race between "the
    # collector thread's first cycle checks is_disk_pressure_paused()"
    # and "the housekeeping thread's first cycle computes and sets it
    # True" -- both threads start at nearly the same instant. If the
    # collector's first cycle reads the flag as still False (a fraction
    # of a millisecond before housekeeping sets it), that ONE cycle
    # proceeds past the pause check and completes its bootstrap+persist
    # normally -- and under heavy system load (e.g. the full test suite
    # running many threads/processes at once) that already-in-flight
    # cycle can take an unpredictable extra stretch of wall-clock time
    # to actually finish (first-ever logger setup, parser import/logging
    # overhead, OS scheduling), landing well after every checkpoint a
    # sleep- or even a cycle-count-based synchronization scheme captures
    # relative to pause supposedly "already being active". No amount of
    # additional waiting AFTER pause is observed fixes this -- the race
    # is with the collector's VERY FIRST cycle, which may have already
    # committed to running before pause ever existed.
    #
    # THE FIX (of the test, not the runtime): never let disk pressure
    # exist before the source has already been bootstrapped once under
    # genuinely normal conditions. Start with a config where pressure
    # cannot trigger, wait for that first (real, unambiguous) bootstrap
    # to land in persisted state, and only THEN switch housekeeping's
    # config to one with an impossible free-space threshold -- so every
    # future housekeeping cycle from that point on reliably (and only
    # from then on) reports paused=True, with no startup race at all.
    # This is not "weakening" the property under test: pausing a source
    # that has never been read yet is a real scenario too, but proving
    # it deterministically would require gating on the collector's
    # in-flight-cycle state directly, which agent.runtime.supervisor
    # does not (and should not) expose just for a test. Pausing an
    # ALREADY-bootstrapped source -- proven here -- is the scenario that
    # actually matters operationally (disk pressure engaging mid-run on
    # an agent that has been running fine), and is proven with zero
    # ambiguity about pre-bootstrap ordering.
    cfg = _cfg(tmp_path)
    runner, _session = _make_runner(tmp_path, cfg=cfg)

    runner.start()
    try:
        # Bootstrap under normal (never-paused) conditions -- deterministic:
        # wait for the OBSERVABLE FACT that persisted state now has a
        # checkins entry, not for any fixed duration.
        assert _wait_until(
            lambda: state.get_source(state.load_state(cfg.state_path), "checkins") is not None, timeout=3.0
        )
        before = state.load_state(cfg.state_path)
        offset_before = state.get_source(before, "checkins").cursor.offset

        # NOW make every future real housekeeping cycle compute paused=True
        # -- swapped in only after bootstrap is already durably proven, so
        # there is no window where pause could race the first cycle.
        runner.housekeeping.cfg = replace(cfg, disk_pressure_min_free_bytes=10 ** 18)
        assert _wait_until(lambda: runner.status.is_disk_pressure_paused(), timeout=3.0)

        with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
            f.write(CHECKIN_LINE.format(barcode="PAUSED1"))

        # Deterministic synchronization for "the collector saw the new
        # bytes exist and still didn't touch them": wait for the
        # collector_cycle_count counter to advance twice past a baseline
        # captured strictly after the append -- guaranteeing at least one
        # full cycle both started after the new bytes existed AND (since
        # pause was already confirmed active and never toggles back off
        # in this test) observed pause=True throughout.
        assert runner.status.is_disk_pressure_paused()
        baseline = runner.status.get_collector_cycle_count()
        assert _wait_until(
            lambda: runner.status.get_collector_cycle_count() >= baseline + 2, timeout=5.0
        ), "collector never completed a fresh cycle after the new bytes were written"

        # Proof chain, all three parts explicit:
        assert runner.status.is_disk_pressure_paused()  # pause was (still) active throughout
        assert spool.list_pending_batches(cfg.spool_root, "checkins") == []  # no source bytes were consumed
        after = state.load_state(cfg.state_path)
        offset_after = state.get_source(after, "checkins").cursor.offset
        assert offset_after == offset_before  # persisted cursor did not advance
    finally:
        runner.stop(timeout=5.0)


def test_disk_pressure_resume_allows_collection_to_continue(tmp_path):
    cfg = _cfg(tmp_path, disk_pressure_pending_bytes_threshold=10 ** 12, disk_pressure_min_free_bytes=0)
    runner, session = _make_runner(tmp_path, cfg=cfg)

    runner.start()
    try:
        assert _wait_until(lambda: not runner.status.is_disk_pressure_paused(), timeout=3.0)

        with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
            f.write(CHECKIN_LINE.format(barcode="RESUME1"))

        assert _wait_until(lambda: len(session.upload_calls) >= 1, timeout=5.0)
        assert session.upload_calls[0][1]["checkins"][0]["barcode"] == "RESUME1"
    finally:
        runner.stop(timeout=5.0)


def test_heartbeat_reflects_pending_backlog_end_to_end(tmp_path):
    cfg = _cfg(tmp_path)
    runner, session = _make_runner(tmp_path, cfg=cfg, upload_script=[Exception("down")] * 50)
    spool.write_batch(
        cfg.spool_root, "checkins", [{"barcode": "1", "source_event_id": "c" * 64}], source_generation=0, start_offset=0, end_offset=10
    )

    runner.start()
    try:
        assert _wait_until(lambda: len(session.status_calls) >= 1, timeout=3.0)
        last_payload = session.status_calls[-1][1]
        assert last_payload["pending_outbox_count"] >= 1
    finally:
        runner.stop(timeout=5.0)


# --- shadow / capture-only validation mode (AMH live-validation prep) ------


def test_shadow_mode_makes_zero_event_upload_http_calls(tmp_path):
    cfg = _cfg(tmp_path, upload_enabled=False)
    runner, session = _make_runner(tmp_path, cfg=cfg)

    runner.start()
    try:
        with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
            f.write(CHECKIN_LINE.format(barcode="SHADOW1"))

        # Give the spool every chance to actually accumulate data (proving
        # collection itself is unaffected) and the uploader every chance
        # to (wrongly) fire if shadow mode weren't respected.
        assert _wait_until(
            lambda: spool.list_pending_batches(cfg.spool_root, "checkins") != [], timeout=5.0
        )
        time.sleep(0.3)

        assert session.upload_calls == []
    finally:
        runner.stop(timeout=5.0)


def test_shadow_mode_makes_zero_heartbeat_http_calls(tmp_path):
    cfg = _cfg(tmp_path, heartbeat_enabled=False)
    runner, session = _make_runner(tmp_path, cfg=cfg)

    runner.start()
    try:
        time.sleep(0.5)  # several heartbeat intervals at the test's fast cadence
        assert session.status_calls == []
    finally:
        runner.stop(timeout=5.0)


def test_shadow_mode_collector_still_writes_spool_and_state_normally(tmp_path):
    cfg = _cfg(tmp_path, upload_enabled=False, heartbeat_enabled=False)
    runner, _session = _make_runner(tmp_path, cfg=cfg)

    runner.start()
    try:
        with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
            f.write(CHECKIN_LINE.format(barcode="SHADOW2"))

        assert _wait_until(
            lambda: spool.list_pending_batches(cfg.spool_root, "checkins") != [], timeout=5.0
        )
        batches = spool.list_pending_batches(cfg.spool_root, "checkins")
        records = spool.read_batch(batches[0])
        assert records[0]["barcode"] == "SHADOW2"
        assert "source_event_id" in records[0]  # deterministic ID generation unaffected

        loaded = state.get_source(state.load_state(cfg.state_path), "checkins")
        assert loaded is not None  # state/cursor tracking unaffected
    finally:
        runner.stop(timeout=5.0)


def test_shadow_mode_no_error_noise_generated(tmp_path):
    # Shadow mode must never look like a failure -- no RETRYABLE_INFRA/
    # AUTH_FAILURE bookkeeping, no attempt sidecar written, no component
    # ever reported dead just because uploads/heartbeat are disabled.
    cfg = _cfg(tmp_path, upload_enabled=False, heartbeat_enabled=False)
    runner, _session = _make_runner(tmp_path, cfg=cfg)

    runner.start()
    try:
        with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
            f.write(CHECKIN_LINE.format(barcode="SHADOW3"))
        assert _wait_until(
            lambda: spool.list_pending_batches(cfg.spool_root, "checkins") != [], timeout=5.0
        )
        time.sleep(0.3)

        batch_path = spool.list_pending_batches(cfg.spool_root, "checkins")[0]
        attempts = spool.get_attempt_record(batch_path)
        assert attempts.attempts == 0  # never even tried, not "tried and failed"

        health = runner.health()
        assert all(h.alive for h in health.values())
    finally:
        runner.stop(timeout=5.0)


def test_shadow_mode_disk_pressure_still_functions(tmp_path):
    cfg = _cfg(tmp_path, upload_enabled=False, heartbeat_enabled=False, disk_pressure_min_free_bytes=10 ** 18)
    runner, _session = _make_runner(tmp_path, cfg=cfg)

    runner.start()
    try:
        assert _wait_until(lambda: runner.status.is_disk_pressure_paused(), timeout=3.0)
    finally:
        runner.stop(timeout=5.0)


def test_shadow_mode_restart_works_normally(tmp_path):
    cfg = _cfg(tmp_path, upload_enabled=False, heartbeat_enabled=False)
    (tmp_path / "Checkins.txt").write_text("", encoding="utf-8")  # EOF-bootstrap seed, not via _make_runner here

    runner1 = AgentRunner(cfg)
    session1 = FakeSession()
    runner1.uploader.session = session1
    runner1.heartbeat.session = session1
    runner1.start()
    # Wait for the EOF-bootstrap to actually land before appending -- a
    # race here (append before the first cycle ever reads the file) would
    # make bootstrap seed PAST the new line, exactly the "ignore historical
    # content" behavior NORMAL mode is supposed to have -- correct, but
    # not what this test is trying to observe.
    assert _wait_until(
        lambda: state.get_source(state.load_state(cfg.state_path), "checkins") is not None, timeout=3.0
    )
    with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
        f.write(CHECKIN_LINE.format(barcode="SHADOWRESTART1"))
    assert _wait_until(lambda: spool.list_pending_batches(cfg.spool_root, "checkins") != [], timeout=5.0)
    runner1.stop(timeout=5.0)

    pending_before = spool.list_pending_batches(cfg.spool_root, "checkins")
    assert len(pending_before) == 1

    runner2 = AgentRunner(cfg)
    session2 = FakeSession()
    runner2.uploader.session = session2
    runner2.heartbeat.session = session2
    runner2.start()
    try:
        time.sleep(0.3)
        # Still shadow mode -- the batch from before the restart is still
        # sitting there, untouched, and no upload was attempted for it.
        assert spool.list_pending_batches(cfg.spool_root, "checkins") == pending_before
        assert session2.upload_calls == []
    finally:
        runner2.stop(timeout=5.0)


def test_shadow_mode_diagnostics_clearly_report_mode_and_backlog(tmp_path):
    cfg = _cfg(tmp_path, upload_enabled=False, heartbeat_enabled=False)
    runner, _session = _make_runner(tmp_path, cfg=cfg)

    runner.start()
    try:
        with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
            f.write(CHECKIN_LINE.format(barcode="SHADOW4"))
        assert _wait_until(
            lambda: spool.list_pending_batches(cfg.spool_root, "checkins") != [], timeout=5.0
        )
        assert _wait_until(lambda: (cfg.diagnostics_dir / "diagnostics.json").exists(), timeout=3.0)

        # Wait until a diagnostics cycle runs AFTER the batch exists, so
        # the snapshot is guaranteed to reflect it.
        time.sleep(0.3)
        doc = json.loads((cfg.diagnostics_dir / "diagnostics.json").read_text(encoding="utf-8"))

        assert doc["upload_enabled"] is False
        assert doc["heartbeat_enabled"] is False
        assert "SHADOW" in doc["mode"].upper() or "CAPTURE" in doc["mode"].upper()
        assert doc["sources"]["checkins"]["pending_batch_count"] >= 1
        assert doc["sources"]["checkins"]["pending_bytes"] > 0
    finally:
        runner.stop(timeout=5.0)


def test_normal_mode_still_enables_uploads_and_heartbeat_by_default(tmp_path):
    # Guards against ever accidentally flipping the production-safe
    # default: a config that doesn't mention upload_enabled/
    # heartbeat_enabled at all must behave exactly like today's
    # production behavior -- both enabled.
    cfg = _cfg(tmp_path)
    assert cfg.upload_enabled is True
    assert cfg.heartbeat_enabled is True

    runner, session = _make_runner(tmp_path, cfg=cfg)
    runner.start()
    try:
        with open(tmp_path / "Checkins.txt", "a", encoding="utf-8") as f:
            f.write(CHECKIN_LINE.format(barcode="NORMAL1"))

        assert _wait_until(lambda: len(session.upload_calls) >= 1, timeout=5.0)
        assert _wait_until(lambda: len(session.status_calls) >= 1, timeout=3.0)
    finally:
        runner.stop(timeout=5.0)


def test_mode_description_distinguishes_production_shadow_and_custom(tmp_path):
    production = _cfg(tmp_path)
    assert "PRODUCTION" in production.mode_description

    shadow = _cfg(tmp_path, upload_enabled=False, heartbeat_enabled=False)
    assert "SHADOW" in shadow.mode_description

    custom = _cfg(tmp_path, upload_enabled=False, heartbeat_enabled=True)
    assert "CUSTOM" in custom.mode_description
