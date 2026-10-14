"""What a collector's report about its own scheduled task means, judged at a given instant.

    judge_schedule(schedule_status, next_run_at, now) -> ScheduleJudgement(judgement, reason)

The collector reads its own Windows Scheduled Task and reports, in every heartbeat, one fixed schedule code
(collector/v2_schedule.py) and Task Scheduler's NextRunTime. What each code says is services.pipeline_state's rule
(schedule_problem); this module adds the one thing that needs a clock.

A REPORT IS JUDGED AT AN INSTANT. "healthy" in the latest heartbeat only describes the moment it was sent. If the
collector then stops running -- the task is deleted or disabled, the machine is off -- no newer heartbeat arrives to
say so, and the stored next-run time simply slides into the past. So a healthy schedule whose next run is overdue by
more than the grace below is an error, and so is a healthy one with no next run at all. The latest row IS the newest
heartbeat for the key, so "overdue and not replaced" needs no second read.

`now` IS GIVEN, NEVER READ. Both instants are compared as instants, whatever zone they are expressed in.

The four judgements:

    ok              the schedule was reported healthy and its next run is not overdue
    error           demonstrable: the collector reported a structural fault, or its next run is missing or overdue
    indeterminate   the collector is evidently running, but could not read its own schedule (or sent a code that is
                    not known), so the schedule is unconfirmed
    unknown         nothing was reported: an older collector that predates the diagnostics

An EMPTY value (None, "", or any other falsy value) is "not reported", as the dashboard has always read it -- which is
the one place this differs from pipeline_state.schedule_problem, where any present value other than "healthy" is an
unconfirmed schedule.

Only the dashboard (services.pipeline_context_service) judges the schedule this way today; the customer API's
pipeline state does not judge age at all. What each judgement and reason is CALLED on screen belongs to whoever shows
it.

Framework-neutral: standard library and services.pipeline_state only. No Streamlit, no FastAPI, no database, no
environment, no clock, no logging.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from services.pipeline_state import schedule_problem

# THE GRACE IS TWO CADENCES. The task runs every 15 minutes with MultipleInstancesPolicy=IgnoreNew: if a run is still
# going when the next trigger fires, that trigger is legitimately skipped, and the next heartbeat can be a full cadence
# later than the stored next-run time. One cadence of grace would therefore raise a false alarm on a single long run;
# two tolerates one skipped trigger and still reports a stopped collector within half an hour. The cadence is the
# collector's own constant (collector/task_settings.py DEFAULT_CADENCE_MINUTES); nothing under src/ imports the
# collector package, so it is restated here and tests/test_collector_schedule.py pins the two together.
COLLECTOR_CADENCE_MINUTES = 15
SCHEDULE_OVERDUE_GRACE_CADENCES = 2
SCHEDULE_OVERDUE_GRACE = timedelta(minutes=COLLECTOR_CADENCE_MINUTES * SCHEDULE_OVERDUE_GRACE_CADENCES)

SCHEDULE_OK, SCHEDULE_ERROR, SCHEDULE_INDETERMINATE, SCHEDULE_UNKNOWN = "ok", "error", "indeterminate", "unknown"

# Why a judgement is not `ok`. The three structural ones are the collector's own codes.
TASK_MISSING = "task_missing"
TASK_DISABLED = "task_disabled"
NO_NEXT_RUN = "no_next_run"  # reported by the collector, or a "healthy" report with no next run in it
OVERDUE = "overdue"
UNREADABLE = "unreadable"  # "query_failed", or a code that is not known

_HEALTHY = "healthy"


@dataclass(frozen=True, slots=True)
class ScheduleJudgement:
    """One of the four judgements, and a reason code when it is `error` or `indeterminate` (None otherwise)."""

    judgement: str
    reason: str | None


def judge_schedule(schedule_status: object, next_run_at: datetime | None, now: datetime) -> ScheduleJudgement:
    """The judgement of a reported schedule at `now`.

    `next_run_at` is the reported next run as an aware datetime, or None when there is none (or it could not be read).
    It is used only for a "healthy" report: a structural fault is an error at once, with no grace, whatever it says.
    """
    if not schedule_status:
        return ScheduleJudgement(SCHEDULE_UNKNOWN, None)
    if schedule_status == _HEALTHY:
        if next_run_at is None:
            return ScheduleJudgement(SCHEDULE_ERROR, NO_NEXT_RUN)
        if now - next_run_at > SCHEDULE_OVERDUE_GRACE:
            return ScheduleJudgement(SCHEDULE_ERROR, OVERDUE)
        return ScheduleJudgement(SCHEDULE_OK, None)
    if schedule_problem(schedule_status) == "failed":
        # Only the collector's three structural codes are `failed` to schedule_problem, and each is its own reason.
        return ScheduleJudgement(SCHEDULE_ERROR, str(schedule_status))
    return ScheduleJudgement(SCHEDULE_INDETERMINATE, UNREADABLE)
