"""Windows Scheduled Task settings (Phase 4c).

Pure Python, cross-platform-testable -- generates the actual Windows Task
Scheduler XML format (the same shape `schtasks /query /tn ... /xml`
produces, and that `schtasks /create /xml <file> /tn "..."` consumes to
register a task) as a plain string. Never shells out to schtasks,
PowerShell, or any Windows-only API -- this module has no OS dependency
at all, specifically so its settings (cadence, overlap policy, restart
policy, execution limit, run principal) are unit-testable in this
project's own CI (which runs on ubuntu-latest).

This is deliberately the SINGLE source of truth for what the production
task's settings actually are: collector/deploy/register-collector-task.ps1
calls `python -m collector.task_settings` to generate the XML file it
registers, rather than duplicating these values as separate
PowerShell-native cmdlet parameters that could silently drift from what
this module's own tests verify.

Values match the approved Phase 4c defaults, chosen to match (where nothing
argues otherwise) the exact policy already proven live in production by
the legacy pipeline's own Scheduled Task (sortview-scheduler):
  - 15-minute cadence
  - MultipleInstancesPolicy = IgnoreNew (same as live production)
  - RestartOnFailure: 3 attempts, 5 minutes apart (same as live production)
  - ExecutionTimeLimit: 1 hour (same as live production)
  - Principal: SYSTEM (DIFFERENT from live production, which runs
    interactively as "Operator" with LogonType=InteractiveToken -- see
    the Phase 3 gap audit for why that's a real reboot-survival risk this
    project deliberately does not carry forward for the new task)

This module NEVER registers, starts, stops, or queries a real task --
see collector/deploy/*.ps1 for the scripts that actually do, which this
module only supplies XML content to.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Deliberately distinct from BOTH the legacy task name ("sortview-scheduler")
# and the (frozen, unused) continuous agent's task name ("SortView Canonical
# Agent") -- never confusable with either at a glance in Task Scheduler's UI.
TASK_NAME = "SortView Collector"

DEFAULT_CADENCE_MINUTES = 15
DEFAULT_RESTART_COUNT = 3
DEFAULT_RESTART_INTERVAL_MINUTES = 5
DEFAULT_EXECUTION_TIME_LIMIT_HOURS = 1
DEFAULT_PRINCIPAL = "SYSTEM"


@dataclass(frozen=True)
class TaskDefinition:
    """Everything needed to generate one task's XML. python_exe,
    config_path, and working_dir must be real, absolute Windows paths at
    registration time -- this module does not validate that (no
    filesystem access at all), only that they're non-empty strings."""

    python_exe: str
    config_path: str
    working_dir: str
    principal: str = DEFAULT_PRINCIPAL
    cadence_minutes: int = DEFAULT_CADENCE_MINUTES
    restart_count: int = DEFAULT_RESTART_COUNT
    restart_interval_minutes: int = DEFAULT_RESTART_INTERVAL_MINUTES
    execution_time_limit_hours: int = DEFAULT_EXECUTION_TIME_LIMIT_HOURS
    # ISO 8601, e.g. "2026-09-15T00:00:00". None means build_task_xml()
    # fills in "now" at generation time -- kept as an explicit, optional
    # override (not baked into this dataclass's own default, since
    # dataclass field defaults are evaluated once at class-definition
    # time, not per instance) so tests can pass a fixed value for
    # deterministic assertions.
    start_boundary: str | None = None

    def __post_init__(self) -> None:
        if not self.python_exe.strip():
            raise ValueError("python_exe must not be empty")
        if not self.config_path.strip():
            raise ValueError("config_path must not be empty")
        if not self.working_dir.strip():
            raise ValueError("working_dir must not be empty")
        if self.cadence_minutes <= 0:
            raise ValueError(f"cadence_minutes must be positive, got {self.cadence_minutes!r}")
        if self.restart_count < 0:
            raise ValueError(f"restart_count must be non-negative, got {self.restart_count!r}")
        if self.restart_interval_minutes <= 0:
            raise ValueError(
                f"restart_interval_minutes must be positive, got {self.restart_interval_minutes!r}"
            )
        if self.execution_time_limit_hours <= 0:
            raise ValueError(
                f"execution_time_limit_hours must be positive, got {self.execution_time_limit_hours!r}"
            )


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_task_xml(definition: TaskDefinition) -> str:
    """Returns the Task Scheduler XML document for `definition`, as a
    string ready to write to a file and register via
    `schtasks /create /xml <file> /tn "..."`.

    Key settings and why:
      - MultipleInstancesPolicy=IgnoreNew: if a run is still in progress
        when the next trigger fires, the new one is skipped rather than
        running concurrently -- matches the overlap policy already
        proven safe in live production for the legacy task.
      - RestartOnFailure: the mechanism that makes collector/run.py's
        fatal-exit-nonzero contract meaningful -- a failed run (exit
        code 1 or 2) gets retried automatically before falling back to
        the next normal 15-minute cycle.
      - ExecutionTimeLimit: a bound on a single run, matching the
        proven-live legacy value. Unlike the (frozen, unused)
        continuous agent's task, this is NOT set to unlimited -- the
        collector is a short-lived one-shot script, not a long-running
        process, so a finite limit is the correct safety bound here,
        not a gotcha to work around.
      - Repetition with no <Duration>: repeats every cadence_minutes
        indefinitely (standard Task Scheduler semantics for "run
        forever on this interval"), not a fixed number of times.
      - StartWhenAvailable=true (in Settings) + no BootTrigger: this is
        the standard, minimal Windows pattern for reboot survival -- if
        the machine was off/rebooting when a repetition was due, Task
        Scheduler runs it as soon as the system is available again, on
        its own, once the trigger's persisted definition is re-evaluated
        after boot. A separate explicit BootTrigger was deliberately NOT
        added on top of this: it would risk firing once at boot AND
        again from the TimeTrigger's own catch-up almost immediately
        after, for no benefit StartWhenAvailable doesn't already provide
        -- unrequested complexity, not a reboot-survival requirement.
      - Principal: SYSTEM by default -- no password to manage or
        expire, survives reboot with no interactive login required, per
        the approved Phase 3 recommendation. See this module's own
        docstring for why this deliberately differs from the currently
        observed live production configuration.
    """
    cadence_iso = f"PT{definition.cadence_minutes}M"
    restart_interval_iso = f"PT{definition.restart_interval_minutes}M"
    execution_limit_iso = f"PT{definition.execution_time_limit_hours}H"
    # Naive/local time is INTENTIONAL, not an oversight: Task Scheduler's
    # StartBoundary (with no timezone suffix) is interpreted as the
    # machine's own local wall-clock time, which is exactly what's wanted
    # for a locally-recurring trigger -- converting to UTC here would be
    # the actual bug.
    start_boundary = definition.start_boundary or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")  # noqa: DTZ005

    python_exe = _xml_escape(definition.python_exe)
    working_dir = _xml_escape(definition.working_dir)
    principal = _xml_escape(definition.principal)
    arguments = _xml_escape(f'-m collector.run --config "{definition.config_path}"')

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>SortView Collector -- one-shot scheduled ingestion run. Registered by collector/deploy/register-collector-task.ps1. Does not touch C:\\SortViewAgent or its Scheduled Task.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>{cadence_iso}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start_boundary}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{principal}</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>{execution_limit_iso}</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>{restart_interval_iso}</Interval>
      <Count>{definition.restart_count}</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def main(argv: list[str] | None = None) -> int:
    """CLI so collector/deploy/register-collector-task.ps1 (and
    unregister/temporary-preflight variants) can generate the XML this
    module is the single source of truth for, then hand it to
    `schtasks /create /xml <file> /tn "..."` -- rather than duplicating
    these settings as separate PowerShell-native cmdlet parameters that
    could silently drift from what this module's own tests verify."""
    parser = argparse.ArgumentParser(description="Generate SortView Collector Task Scheduler XML")
    parser.add_argument("--python-exe", required=True)
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--working-dir", required=True)
    parser.add_argument("--output", required=True, help="Path to write the generated XML file")
    parser.add_argument("--principal", default=DEFAULT_PRINCIPAL)
    parser.add_argument("--cadence-minutes", type=int, default=DEFAULT_CADENCE_MINUTES)
    parser.add_argument("--restart-count", type=int, default=DEFAULT_RESTART_COUNT)
    parser.add_argument("--restart-interval-minutes", type=int, default=DEFAULT_RESTART_INTERVAL_MINUTES)
    parser.add_argument("--execution-time-limit-hours", type=int, default=DEFAULT_EXECUTION_TIME_LIMIT_HOURS)
    args = parser.parse_args(argv)

    definition = TaskDefinition(
        python_exe=args.python_exe,
        config_path=args.config_path,
        working_dir=args.working_dir,
        principal=args.principal,
        cadence_minutes=args.cadence_minutes,
        restart_count=args.restart_count,
        restart_interval_minutes=args.restart_interval_minutes,
        execution_time_limit_hours=args.execution_time_limit_hours,
    )
    xml_text = build_task_xml(definition)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # UTF-16, matching the XML declaration's own encoding attribute --
    # schtasks /create /xml is picky about this matching the file's
    # actual bytes.
    output_path.write_text(xml_text, encoding="utf-16")
    print(f"Wrote task XML to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
