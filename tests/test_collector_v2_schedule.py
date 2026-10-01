"""Collector run / schedule diagnostics, collector side: collector/v2_schedule.py (the Windows Scheduled Task query) and the
run duration and heartbeat fields collector/v2_run.py derives from it.

The query is exercised with a fake process runner -- what PowerShell would print, as bytes -- so every outcome is
deterministic and no test starts a real process (tests/conftest.py also disables the real one for every other test). One
test at the end does run the real script through real Windows PowerShell, on Windows only, against a task name that does
not exist: it proves the script parses, runs and reports "not found" without relying on message text.

Time is injected everywhere: `QUERIED_AT` is passed as `now=`, the monotonic clock is a counter the test advances, and the
wall clock is a list of instants the test chooses -- including one that jumps backwards mid-run.
"""

from __future__ import annotations

import base64
import inspect
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from collector_v2_support import Reply, ScriptedSession
from test_collector_v2_run import Env, checkin_lines, empty_other_sources

from collector import task_settings, v2_events, v2_run, v2_schedule
from collector.v2_schedule import QUERY_FAILED, ScheduleDiagnostics

QUERIED_AT = datetime(2026, 10, 1, 19, 45, 30, tzinfo=UTC)  # freshness: allow FRESH004 -- passed as now= at every call
LAST_RUN = "2026-10-01T19:45:00Z"
NEXT_RUN = "2026-10-01T20:00:00Z"
EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
CANARY = "CANARY-SCHEDULER-EXCEPTION C:\\Users\\someone\\secret.txt"

READY, RUNNING, QUEUED, DISABLED, UNKNOWN = 3, 4, 2, 1, 0


def task(*, enabled=True, state=RUNNING, last_run=LAST_RUN, next_run=NEXT_RUN, **extra) -> bytes:
    return json.dumps({"found": True, "enabled": enabled, "state": state, "last_run": last_run, "next_run": next_run,
                       **extra}).encode("ascii")


def answering(output: bytes, returncode: int = 0):
    calls = []

    def runner(command, timeout):
        calls.append((command, timeout))
        return returncode, output

    runner.calls = calls
    return runner


def query(output: bytes = b"", *, returncode: int = 0, runner=None, platform="win32", executable=EXE, now=QUERIED_AT):
    return v2_schedule.query_schedule(now=now, runner=runner or answering(output, returncode), executable=executable,
                                      platform=platform)


def utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


# ======================================================================================================================
# nominal
# ======================================================================================================================

@pytest.mark.parametrize("state", [RUNNING, READY, QUEUED])
def test_an_enabled_task_with_a_future_next_run_is_healthy_whether_running_or_ready(state):
    # Running is the normal state during the collector's own invocation; Ready is the state between runs.
    result = query(task(state=state))

    assert result == ScheduleDiagnostics("healthy", utc(LAST_RUN), utc(NEXT_RUN))


def test_the_reported_times_are_what_windows_said_never_computed_from_the_cadence():
    # An irregular gap: 7 minutes, not the 15-minute cadence. The next run is the scheduler's value, verbatim.
    result = query(task(last_run="2026-10-01T19:40:00Z", next_run="2026-10-01T19:47:00Z"))

    assert result.next_run_at == utc("2026-10-01T19:47:00Z") and result.last_run_at == utc("2026-10-01T19:40:00Z")
    assert result.next_run_at - result.last_run_at != timedelta(minutes=task_settings.DEFAULT_CADENCE_MINUTES)
    source = inspect.getsource(v2_schedule)
    assert "CADENCE" not in source and "timedelta(minutes" not in source  # nothing here could derive a next run


def test_timestamps_are_normalized_to_aware_utc():
    result = query(task())

    for instant in (result.last_run_at, result.next_run_at):
        assert instant.tzinfo is UTC and instant.utcoffset() == timedelta(0)
    assert v2_events.format_time(result.next_run_at) == NEXT_RUN  # and round-trip to the wire spelling


def test_last_run_time_is_task_schedulers_value_however_the_run_was_started():
    # collector_last_run_at is Task Scheduler's LastRunTime for the task. Windows updates it for a trigger, for
    # Start-ScheduledTask and for "Run" in the UI alike, so a manually started task run IS the latest run. Neither the
    # code nor its documentation may narrow that to trigger-fired runs.
    manual_start = "2026-10-01T19:38:17Z"  # off the quarter-hour: plainly not a 15-minute trigger
    assert query(task(last_run=manual_start)).last_run_at == utc(manual_start)

    documentation = " ".join((v2_schedule.__doc__ or "").split())
    assert "whatever started that launch" in documentation and "Start-ScheduledTask" in documentation
    for narrowing in ("scheduled-only", "scheduled runs only", "only scheduled", "trigger-fired only"):
        assert narrowing not in documentation.lower()


# ======================================================================================================================
# not nominal: fixed codes
# ======================================================================================================================

def test_a_missing_task_is_task_missing():
    assert query(b'{"found":false}') == ScheduleDiagnostics("task_missing")


@pytest.mark.parametrize("fields", [{"enabled": False, "state": READY}, {"enabled": True, "state": DISABLED},
                                    {"enabled": False, "state": DISABLED}])
def test_a_disabled_task_is_task_disabled_and_reports_no_next_run(fields):
    result = query(task(**fields))

    assert result.status == "task_disabled" and result.next_run_at is None
    assert result.last_run_at == utc(LAST_RUN)  # when it last ran is still true, and still worth showing


@pytest.mark.parametrize("next_run", [
    None,                       # Windows has nothing scheduled
    "2026-10-01T19:45:30Z",     # exactly now: not in the future
    "2026-10-01T19:30:00Z",     # in the past
    "2026-10-03T19:45:31Z",     # further out than the server would accept: unusable for a 15-minute task
])
def test_no_usable_future_next_run_is_no_next_run(next_run):
    result = query(task(next_run=next_run))

    assert result.status == "no_next_run" and result.next_run_at is None and result.last_run_at == utc(LAST_RUN)


def test_the_never_run_sentinel_is_no_last_run_not_a_date_in_1999():
    # Get-ScheduledTaskInfo reports 1999-11-30 for a task that has never run. The server refuses any timestamp before
    # 2000, which would cost the whole heartbeat, so the sentinel must be dropped here.
    result = query(task(last_run="1999-11-30T06:00:00Z"))

    assert result == ScheduleDiagnostics("healthy", None, utc(NEXT_RUN))


def test_a_task_that_has_never_run_and_reports_no_last_run_is_still_judged_on_its_next_run():
    assert query(task(last_run=None)) == ScheduleDiagnostics("healthy", None, utc(NEXT_RUN))


# ======================================================================================================================
# query failures: one fixed code, no detail
# ======================================================================================================================

@pytest.mark.parametrize("output", [
    b"", b"   ", b"not json", b"[]", b"null", b'"healthy"', b"{}",
    b'{"found":true}',                                                    # fields missing
    task(extra_field="x"),                                               # an unexpected field
    task(enabled="true"), task(enabled=1), task(state="Running"), task(state=True), task(state=3.0),
    task(state=UNKNOWN), task(state=99),                                 # an unrecognised task state
    task(next_run="10/01/2026 3:00:00 PM"), task(next_run="2026-10-01T20:00:00"), task(next_run=1759348800),
    task(last_run="yesterday"), task(next_run="2026-10-01T20:00:00+00:00"),
    b'{"found":"false"}', b'{"found":false,"enabled":true}',
    task() + b"\nWARNING: something else printed",                        # anything but exactly one object
    "\u00e9".encode("utf-8") + task(),                                    # non-ASCII output
    b"x" * 5000,
])
def test_output_that_is_not_exactly_the_expected_object_is_query_failed(output):
    assert query(output) == QUERY_FAILED


@pytest.mark.parametrize("returncode", [1, 3, 4, -1, 255])
def test_a_non_zero_exit_is_query_failed_even_with_plausible_output(returncode):
    assert query(task(), returncode=returncode) == QUERY_FAILED


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired(cmd="powershell", timeout=30), FileNotFoundError(CANARY), PermissionError(CANARY),
    OSError(CANARY), RuntimeError(CANARY), ValueError(CANARY), MemoryError(CANARY),
])
def test_a_timeout_or_any_error_starting_the_query_is_query_failed_and_never_raises(failure):
    def runner(_command, _timeout):
        raise failure

    result = query(runner=runner)

    assert result == QUERY_FAILED
    assert "CANARY" not in repr(result)


@pytest.mark.parametrize("platform", ["linux", "darwin", "cygwin"])
def test_off_windows_is_query_failed_and_starts_nothing(platform):
    runner = answering(task())

    assert query(runner=runner, platform=platform) == QUERY_FAILED and runner.calls == []


def test_no_powershell_is_query_failed_and_starts_nothing(monkeypatch):
    monkeypatch.setattr(v2_schedule, "powershell_executable", lambda: None)
    runner = answering(task())

    assert v2_schedule.query_schedule(now=QUERIED_AT, runner=runner, platform="win32") == QUERY_FAILED
    assert runner.calls == []


def test_a_utf8_bom_before_the_object_is_tolerated():
    # A UTF-8 console code page makes PowerShell prefix one (seen for real on a code-page-65001 console).
    assert query(b"\xef\xbb\xbf" + task()).status == "healthy"
    assert query(b"\xef\xbb\xbf" + b'{"found":false}').status == "task_missing"


# ======================================================================================================================
# nothing but fixed codes and validated instants can leave the module
# ======================================================================================================================

def test_the_result_can_only_hold_an_approved_code_and_aware_instants():
    for bad in ("Healthy", "error", "disabled", "no future run scheduled", CANARY, "", None):
        with pytest.raises(ValueError):
            ScheduleDiagnostics(bad)
    with pytest.raises(ValueError):
        ScheduleDiagnostics("healthy", datetime(2026, 10, 1, 12, 0), None)  # noqa: DTZ001 - a naive instant is the point
    with pytest.raises(ValueError):
        ScheduleDiagnostics("healthy", None, NEXT_RUN)

    fields = {f.name for f in ScheduleDiagnostics.__dataclass_fields__.values()}
    assert fields == {"status", "last_run_at", "next_run_at"}
    assert set(v2_events.SCHEDULE_STATUSES) == {"healthy", "task_missing", "task_disabled", "no_next_run", "query_failed"}


def test_exception_and_process_text_never_reach_the_result_the_log_or_the_heartbeat(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    empty_other_sources(env, checkins=checkin_lines(2))

    def failing_runner(_command, _timeout):
        raise RuntimeError(CANARY)

    def schedule_query(*, now):
        return v2_schedule.query_schedule(now=now, runner=failing_runner, executable=EXE, platform="win32")

    session = ScriptedSession()
    assert env.run(session, schedule_query=schedule_query) == 0

    (heartbeat,) = session.statuses()
    assert heartbeat["collector_schedule_status"] == "query_failed"
    assert "collector_last_run_at" not in heartbeat and "collector_next_run_at" not in heartbeat
    everything = json.dumps(session.calls) + env.cfg.log_path.read_text(encoding="utf-8") + json.dumps(env.status())
    assert "CANARY" not in everything and "RuntimeError" not in everything
    assert "v2 schedule checked | schedule_status=query_failed" in env.cfg.log_path.read_text(encoding="utf-8")


def test_the_real_runner_discards_stderr_gives_no_stdin_and_uses_no_shell(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs, command=command)
        return subprocess.CompletedProcess(command, 0, stdout=task(), stderr=None)

    monkeypatch.setattr(v2_schedule.subprocess, "run", fake_run)

    assert v2_schedule.run_process(["powershell.exe", "-x"], 30.0) == (0, task())
    assert seen["stderr"] is subprocess.DEVNULL and seen["stdin"] is subprocess.DEVNULL and seen["stdout"] is subprocess.PIPE
    assert seen["timeout"] == 30.0 and seen["check"] is False and "shell" not in seen
    assert seen["command"] == ["powershell.exe", "-x"]  # a list: never a shell string


# ======================================================================================================================
# the command and the script
# ======================================================================================================================

def test_the_command_is_absolute_non_interactive_profileless_and_encoded():
    runner = answering(task())
    query(runner=runner)

    ((command, timeout),) = runner.calls
    assert command[0] == EXE and command[1:6] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                                 "-EncodedCommand"]
    assert len(command) == 7 and timeout == v2_schedule.QUERY_TIMEOUT_SECONDS
    assert base64.b64decode(command[6]).decode("utf-16-le") == v2_schedule.build_script()


def test_the_script_queries_the_canonical_task_with_the_scheduledtasks_cmdlets_only():
    script = v2_schedule.build_script()

    assert f"Get-ScheduledTask -TaskName '{task_settings.TASK_NAME}' -TaskPath '\\'" in script
    assert task_settings.TASK_NAME == "SortView Collector"
    assert "Get-ScheduledTaskInfo -InputObject $task" in script
    assert "LastRunTime" in script and "NextRunTime" in script
    # Machine-readable and locale-independent: UTC, invariant culture, integers and booleans, compact JSON.
    assert "ToUniversalTime()" in script and "InvariantCulture" in script and "ConvertTo-Json -Compress" in script
    assert "[int]$task.State" in script and "[bool]$task.Settings.Enabled" in script
    # "Not found" by the cmdlet's non-localized error id, never by message text.
    assert "FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*'" in script
    for forbidden in ("schtasks", "/fo", "LIST", ".Message", "Write-Error", "Out-File", "Invoke-Expression", "$env:"):
        assert forbidden not in script, forbidden


def test_a_quote_in_a_task_name_cannot_end_the_string_literal():
    script = v2_schedule.build_script("it's; Remove-Item C:\\ -Recurse")

    assert "-TaskName 'it''s; Remove-Item C:\\ -Recurse'" in script


def test_powershell_is_found_under_the_system_directory_not_on_the_path():
    # (The function itself is replaced for every test by tests/conftest.py, so this reads the module's source.)
    source = inspect.getsource(v2_schedule)
    function = source[source.index("def powershell_executable"):source.index("def command_line")]

    assert "GetSystemDirectoryW" in function and "WindowsPowerShell" in function
    assert "import shutil" not in source and "which(" not in source and "os.environ" not in source and "getenv(" not in source


@pytest.mark.skipif(sys.platform != "win32", reason="runs the real script through real Windows PowerShell")
def test_the_real_script_runs_and_reports_a_missing_task_by_error_id():
    executable = shutil.which("powershell")
    if executable is None:
        pytest.skip("Windows PowerShell is not available")
    command = v2_schedule.command_line(executable, "SortView Collector TEST - this task does not exist")

    returncode, output = v2_schedule.run_process(command, 120)

    assert returncode == 0
    assert v2_schedule.parse_output(output, QUERIED_AT) == ScheduleDiagnostics("task_missing")


# ======================================================================================================================
# the run: duration and the heartbeat's four fields
# ======================================================================================================================

class Monotonic:
    """A monotonic clock the test advances by hand: `perf` for run_once_v2, `advance` for whatever takes time."""

    def __init__(self, start: float = 500.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def healthy_schedule(*, now):
    return ScheduleDiagnostics("healthy", now - timedelta(seconds=30), now + timedelta(minutes=14))


def test_the_heartbeat_carries_the_four_diagnostics_as_typed_values(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    empty_other_sources(env, checkins=checkin_lines(2))
    session = ScriptedSession()

    assert env.run(session, schedule_query=healthy_schedule) == 0

    (heartbeat,) = session.statuses()
    assert heartbeat["collector_schedule_status"] == "healthy"
    assert heartbeat["collector_last_run_at"] == v2_events.format_time(env.clock - timedelta(seconds=30))
    assert heartbeat["collector_next_run_at"] == v2_events.format_time(env.clock + timedelta(minutes=14))
    duration = heartbeat["collector_run_duration_ms"]
    assert isinstance(duration, int) and not isinstance(duration, bool) and 0 <= duration <= v2_events.MAX_RUN_DURATION_MS


def test_the_duration_includes_the_scheduler_query_and_excludes_only_the_heartbeat_post(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    empty_other_sources(env, checkins=checkin_lines(3))
    mono = Monotonic()
    started_perf = mono()          # what collector/run.py's main() captured as its first act
    mono.advance(0.25)             # config loading and logger setup, before run_once_v2 is even called

    def upload(_url, _body):       # delivery takes 1.5 s
        mono.advance(1.5)  # (returning None lets the scripted session answer with its success reply)

    def schedule_query(*, now):    # starting PowerShell takes 2.6 s
        mono.advance(2.6)
        return healthy_schedule(now=now)

    def heartbeat_post(_url, _body):  # the POST takes 9 s -- and must not be in the number it carries
        mono.advance(9.0)
        return Reply(200, {"status": "success"})

    session = ScriptedSession(default=upload, status_reply=heartbeat_post)
    assert env.run(session, started_perf=started_perf, perf=mono, schedule_query=schedule_query) == 0

    (heartbeat,) = session.statuses()
    uploads = len(session.uploads())
    assert uploads >= 1
    assert heartbeat["collector_run_duration_ms"] == round((0.25 + 1.5 * uploads + 2.6) * 1000)
    assert mono() - started_perf == pytest.approx(0.25 + 1.5 * uploads + 2.6 + 9.0)  # the POST really did take its 9 s


def test_the_duration_is_measured_on_the_monotonic_clock_and_a_wall_clock_jump_cannot_change_it(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    empty_other_sources(env, checkins=checkin_lines(2))
    mono = Monotonic()
    started_perf = mono()
    # The wall clock is corrected BACKWARDS by three hours part-way through the run (an NTP step, a DST mistake).
    wall = [env.clock, env.clock - timedelta(hours=3)]
    readings = []

    def clock():
        readings.append(wall[min(len(readings), len(wall) - 1)])
        return readings[-1]

    def schedule_query(*, now):
        mono.advance(3.24)
        return ScheduleDiagnostics("query_failed")

    session = ScriptedSession()
    assert env.run(session, clock=clock, started_perf=started_perf, perf=mono, schedule_query=schedule_query) == 0

    (heartbeat,) = session.statuses()
    assert len(readings) == 2 and readings[1] < readings[0]         # the jump happened, inside the measured window
    assert heartbeat["collector_run_duration_ms"] == 3240           # exactly the monotonic elapsed time
    wall_clock_difference_ms = round((readings[1] - readings[0]).total_seconds() * 1000)
    assert wall_clock_difference_ms == -10_800_000                  # what subtracting timestamps would have produced


def test_the_run_never_derives_the_duration_from_wall_clock_timestamps():
    source = inspect.getsource(v2_run.run_once_v2) + inspect.getsource(v2_run._elapsed_ms)

    assert "perf()" in source and "time.perf_counter" in source
    assert "total_seconds" not in source and "time.time()" not in source and "datetime.now" not in source
    assert v2_run._elapsed_ms(10.0, 13.2449) == 3245 and v2_run._elapsed_ms(10.0, 10.0) == 0
    assert v2_run._elapsed_ms(10.0, 9.0) == 0                                        # never negative
    assert v2_run._elapsed_ms(0.0, 10.0 ** 9) == v2_events.MAX_RUN_DURATION_MS      # never beyond what the server accepts


def test_without_a_start_reading_the_duration_starts_at_the_run_itself(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    empty_other_sources(env, checkins=checkin_lines(1))
    mono = Monotonic()

    def schedule_query(*, now):
        mono.advance(0.5)
        return ScheduleDiagnostics("query_failed")

    session = ScriptedSession()
    env.run(session, perf=mono, schedule_query=schedule_query)

    assert session.statuses()[0]["collector_run_duration_ms"] == 500


def test_the_schedule_is_asked_with_a_fresh_reading_of_the_wall_clock_not_the_run_start(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    empty_other_sources(env, checkins=checkin_lines(1))
    later = env.clock + timedelta(minutes=7)  # a long run: the start instant is stale by the time the task is queried
    instants = iter([env.clock, later])
    asked = []

    def schedule_query(*, now):
        asked.append(now)
        return ScheduleDiagnostics("query_failed")

    env.run(ScriptedSession(), clock=lambda: next(instants), schedule_query=schedule_query)

    assert asked == [later]


def test_a_failed_run_still_reports_its_duration_and_schedule(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    empty_other_sources(env, checkins=checkin_lines(2))
    session = ScriptedSession(default=Reply(401))

    assert env.run(session, schedule_query=healthy_schedule) == 1

    (heartbeat,) = session.statuses()
    assert heartbeat["status"] == "error" and heartbeat["last_error_class"] == "auth_failure"
    assert heartbeat["collector_schedule_status"] == "healthy" and "collector_run_duration_ms" in heartbeat


def test_main_passes_its_own_monotonic_start_reading_to_the_v2_run(monkeypatch):
    seen = {}

    def fake_run_once_v2(cfg, v2, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(v2_run, "run_once_v2", fake_run_once_v2)
    monkeypatch.setattr(v2_run, "load_v2_config", lambda *_a, **_k: object())
    monkeypatch.setattr(v2_run.uploader, "build_session", lambda: object())

    assert v2_run.main_v2(object(), "config.json", logger=None, started_perf=123.5) == 0
    assert seen["started_perf"] == 123.5
    assert "started_perf=start_perf" in inspect.getsource(sys.modules["collector.run"].main)


# ======================================================================================================================
# the snapshot refuses anything that is not a code, an integer or an aware instant
# ======================================================================================================================

def _snapshot(**overrides):
    from collector.v2_status import StatusSnapshot

    return StatusSnapshot("healthy", None, 0, None, None, **overrides)


@pytest.mark.parametrize("field", ["collector_last_run_at", "collector_next_run_at"])
@pytest.mark.parametrize("value", ["2026-10-01T20:00:00Z", 1759348800, datetime(2026, 10, 1, 20, 0)])  # noqa: DTZ001
def test_the_snapshot_refuses_a_schedule_time_that_is_not_an_aware_datetime(field, value):
    from collector.v2_status import StatusError

    with pytest.raises(StatusError, match=field):
        _snapshot(**{field: value})


@pytest.mark.parametrize("value", [-1, 86_400_001, 3.24, "3240", True])
def test_the_snapshot_bounds_the_duration(value):
    from collector.v2_status import StatusError

    with pytest.raises(StatusError, match="collector_run_duration_ms"):
        _snapshot(collector_run_duration_ms=value)


@pytest.mark.parametrize("value", ["Healthy", "error", "disabled", CANARY, "", 1, True])
def test_the_snapshot_refuses_a_schedule_status_outside_the_closed_list(value):
    from collector.v2_status import StatusError

    with pytest.raises(StatusError, match="collector_schedule_status"):
        _snapshot(collector_schedule_status=value)


def test_a_snapshot_without_diagnostics_sends_none_of_the_four_fields():
    payload = _snapshot().payload("3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e")

    assert not [name for name in payload if name.startswith("collector_")]
