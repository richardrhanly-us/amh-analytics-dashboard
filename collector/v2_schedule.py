"""Contract v2: Windows Scheduled Task diagnostics for the heartbeat (docs/collector-v2.md).

WHAT THIS ANSWERS. Three things the dashboard cannot know on its own -- it has no route to the AMH machine and must not
need one: when Windows Task Scheduler last launched the "SortView Collector" task, when Windows next intends to, and
whether that schedule is in a nominal state. The collector asks the scheduler itself, on the machine it runs on, and
reports the answer as two timestamps and one fixed code.

    collector_last_run_at      Get-ScheduledTaskInfo.LastRunTime of the task: the scheduler's most recent launch of it,
                               whatever started that launch (its trigger, Start-ScheduledTask, "Run" in the Task Scheduler
                               UI). During a run the task started, that is the run reporting.
    collector_next_run_at      Get-ScheduledTaskInfo.NextRunTime: the run Windows currently has scheduled. NEVER computed
                               from the cadence -- if Windows has no next run, none is reported.
    collector_schedule_status  healthy | task_missing | task_disabled | no_next_run | query_failed

NOMINAL (`healthy`) means all of: the query succeeded, the task exists, it is enabled, and Windows reports a usable
NextRunTime in the future. State=Running is normal -- it is this very invocation -- and so is Ready.

FAIL CLOSED. Anything that prevents a trustworthy answer is `query_failed`: not Windows, PowerShell not found, a timeout,
a non-zero exit, output that is not exactly the expected JSON object, a field of the wrong type, an unrecognised task
state. Nothing is guessed, and no timestamp is reported from an answer that failed validation.

NO LOCALE DEPENDENCE. `schtasks /query` prints localized labels and dates and is deliberately not used. The helper is a
fixed PowerShell script (Get-ScheduledTask / Get-ScheduledTaskInfo) that prints ONE JSON object: booleans, an integer
state, and timestamps already converted to UTC and formatted with the invariant culture. "Not found" is recognised by the
cmdlet's FullyQualifiedErrorId, which is not localized, never by message text.

NOTHING FROM THE CHILD PROCESS IS EVER LOGGED OR SENT. Its stdout is parsed and discarded; its stderr is not even
captured; an exception raised while running it is swallowed and becomes `query_failed`. What leaves this module is a
`ScheduleDiagnostics`: one of five fixed codes and two validated datetimes.

RUNS AS SYSTEM. The scheduled Collector runs as NT AUTHORITY\\SYSTEM, which can read every task. PowerShell is started by
its absolute path under the Windows system directory (asked of the OS, not taken from PATH or an environment variable),
with no profile, non-interactively, with no window, no stdin, and a bounded wait. The script is passed as
-EncodedCommand, so there is no script file to tamper with and nothing to quote; the only name in it is TASK_NAME.

COST. Starting PowerShell and loading the ScheduledTasks module takes a couple of seconds. That time is part of the
collector invocation and is deliberately INCLUDED in collector_run_duration_ms (see collector/v2_run.py).
"""

from __future__ import annotations

import base64
import ctypes
import json
import subprocess  # nosec B404 - runs Windows PowerShell by absolute path with a fixed, encoded script
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .task_settings import TASK_NAME
from .v2_events import (
    SCHEDULE_HEALTHY,
    SCHEDULE_NO_NEXT_RUN,
    SCHEDULE_QUERY_FAILED,
    SCHEDULE_STATUSES,
    SCHEDULE_TASK_DISABLED,
    SCHEDULE_TASK_MISSING,
)

QUERY_TIMEOUT_SECONDS = 30
_MAX_OUTPUT_BYTES = 4096  # the JSON object is about 150 bytes; anything much larger is not it

# The server refuses a timestamp before 2000 or more than a day ahead (ingest_v2_models.AwareTimestamp), and a refused
# field would cost the WHOLE heartbeat. Windows reports 1999-11-30 as LastRunTime for a task that has never run, so that
# sentinel is "no last run"; and a NextRunTime further out than the bound is not a usable next run for a 15-minute task.
_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
MAX_AHEAD = timedelta(days=1)

# Microsoft.PowerShell ScheduledTasks: TaskState. Integers, so nothing depends on how a name is spelled or localized.
_STATE_DISABLED, _STATE_QUEUED, _STATE_READY, _STATE_RUNNING = 1, 2, 3, 4
_NOMINAL_STATES = (_STATE_QUEUED, _STATE_READY, _STATE_RUNNING)

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_UTF8_BOM = b"\xef\xbb\xbf"

# One JSON object on stdout, exit 0; any failure other than "no such task" exits non-zero with no output we read.
# -TaskPath '\' pins the task registered at the root (where register-collector-task.ps1 puts it), not a same-named task
# in some other folder.
_SCRIPT_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
try {
    try {
        $task = Get-ScheduledTask -TaskName '__TASK_NAME__' -TaskPath '\' -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*') {
            [Console]::Out.Write('{"found":false}')
            exit 0
        }
        exit 3
    }
    $info = Get-ScheduledTaskInfo -InputObject $task -ErrorAction Stop
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    $format = "yyyy-MM-dd'T'HH:mm:ss'Z'"
    $last = $null
    if ($null -ne $info.LastRunTime) { $last = ([datetime]$info.LastRunTime).ToUniversalTime().ToString($format, $invariant) }
    $next = $null
    if ($null -ne $info.NextRunTime) { $next = ([datetime]$info.NextRunTime).ToUniversalTime().ToString($format, $invariant) }
    $result = [ordered]@{
        found = $true
        enabled = [bool]$task.Settings.Enabled
        state = [int]$task.State
        last_run = $last
        next_run = $next
    }
    [Console]::Out.Write(($result | ConvertTo-Json -Compress))
    exit 0
} catch {
    exit 4
}
"""


@dataclass(frozen=True)
class ScheduleDiagnostics:
    """What the heartbeat may carry about the schedule: a fixed code and two aware UTC instants. Nothing else."""

    status: str
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.status not in SCHEDULE_STATUSES:
            raise ValueError("unapproved field: status")
        for instant in (self.last_run_at, self.next_run_at):
            if instant is not None and (not isinstance(instant, datetime) or instant.tzinfo is None):
                raise ValueError("unapproved field: timestamp")


QUERY_FAILED = ScheduleDiagnostics(SCHEDULE_QUERY_FAILED)

# (returncode, stdout bytes) for a command line. Injectable so tests never start a real process.
ProcessRunner = Callable[[list[str], float], tuple[int, bytes]]


def build_script(task_name: str = TASK_NAME) -> str:
    # A single quote is the only character that could end the PowerShell string literal; doubling it is the language's
    # own escape. TASK_NAME is a constant with none, so this is belt and braces, not input handling.
    return _SCRIPT_TEMPLATE.replace("__TASK_NAME__", task_name.replace("'", "''"))


def powershell_executable() -> str | None:
    """Windows PowerShell's absolute path under the system directory the OS itself reports, or None off Windows / if it is
    not there. Not PATH and not an environment variable: either could point somewhere else."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    buffer = ctypes.create_unicode_buffer(260)
    length = windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        return None
    executable = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(executable) if executable.is_file() else None


def command_line(executable: str, task_name: str = TASK_NAME) -> list[str]:
    encoded = base64.b64encode(build_script(task_name).encode("utf-16-le")).decode("ascii")
    return [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]


def run_process(command: list[str], timeout: float) -> tuple[int, bytes]:
    """The real runner. stderr is discarded unread; stdout comes back as bytes for parse_output to validate."""
    completed = subprocess.run(  # nosec B603 - absolute executable path, fixed arguments, no shell
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.returncode, completed.stdout or b""


def _instant(value: object) -> datetime | None:
    """A helper timestamp -> aware UTC. Raises ValueError for anything that is not exactly the expected spelling."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp")
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=UTC)


def parse_output(output: bytes, now: datetime) -> ScheduleDiagnostics:
    """Validates the helper's stdout and turns it into a ScheduleDiagnostics. Raises ValueError (or a subclass) for anything
    that is not exactly the documented object; the caller turns that into `query_failed`."""
    if len(output) > _MAX_OUTPUT_BYTES:
        raise ValueError("output")
    output = output.removeprefix(_UTF8_BOM)  # a UTF-8 console code page makes PowerShell prefix one
    document = json.loads(output.decode("ascii").strip())
    if not isinstance(document, dict):
        raise ValueError("document")

    if document == {"found": False}:
        return ScheduleDiagnostics(SCHEDULE_TASK_MISSING)
    if set(document) != {"found", "enabled", "state", "last_run", "next_run"} or document["found"] is not True:
        raise ValueError("fields")
    enabled, state = document["enabled"], document["state"]
    if not isinstance(enabled, bool) or isinstance(state, bool) or not isinstance(state, int):
        raise ValueError("types")

    last_run = _instant(document["last_run"])
    next_run = _instant(document["next_run"])
    if last_run is not None and not _EARLIEST <= last_run <= now + MAX_AHEAD:
        last_run = None  # the never-run sentinel, or a clock so wrong the value means nothing

    if not enabled or state == _STATE_DISABLED:
        return ScheduleDiagnostics(SCHEDULE_TASK_DISABLED, last_run)
    if state not in _NOMINAL_STATES:
        raise ValueError("state")
    if next_run is None or not now < next_run <= now + MAX_AHEAD:
        return ScheduleDiagnostics(SCHEDULE_NO_NEXT_RUN, last_run)
    return ScheduleDiagnostics(SCHEDULE_HEALTHY, last_run, next_run)


def query_schedule(*, now: datetime, runner: ProcessRunner | None = None, executable: str | None = None,
                   platform: str | None = None) -> ScheduleDiagnostics:
    """Asks Windows Task Scheduler about the Collector's task. NEVER raises: every failure is `query_failed`.

    `now` is the current instant (aware), used only to decide whether the reported times are usable. `runner`,
    `executable` and `platform` exist for tests; a real run passes none of them."""
    try:
        if (platform or sys.platform) != "win32":
            return QUERY_FAILED
        executable = executable or powershell_executable()
        if executable is None:
            return QUERY_FAILED
        returncode, output = (runner or run_process)(command_line(executable), QUERY_TIMEOUT_SECONDS)
        if returncode != 0:
            return QUERY_FAILED
        return parse_output(output, now)
    except Exception:  # a timeout, an OS error, bad output: all the same fixed code, none of the detail
        return QUERY_FAILED
