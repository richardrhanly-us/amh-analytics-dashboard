"""The customer API's pipeline states: one closed set of four, and how a value
a collector reported becomes one of them.

    ok         the last report said things are working
    degraded   the last report said things are working with a problem
    failed     the last report said things are not working
    unknown    there is no report, or nothing here can say what it means

A state describes WHAT WAS LAST REPORTED. It says nothing about how long ago
that was: a branch whose collector stopped a week ago still has whatever
state its last report carried. Judging age is not this module's job, and
nothing here takes or reads a time.

A branch's collection status is stored in one of two places, and each has its
own vocabulary:

    pipeline_status.status            the result of a scheduled run        (legacy)
    pipeline_status.health_status     a continuous agent's heartbeat       (legacy)
    ingest_key_ids.health_status      the collector's heartbeat            (current)
    ingest_key_ids.collector_schedule_status
                                      what the collector found when it
                                      looked at its own scheduled task     (current)

There is one function per stored signal. WHICH signal speaks for a branch --
legacy or current, and for a legacy row the run status or the heartbeat -- is
decided elsewhere, from the cutover record and the server's own report
stamps; this module compares no timestamps and selects no source.

FAIL CLOSED. Every function that reads a stored value is total: any value at
all gets an answer and nothing is raised. A value is matched EXACTLY as it is
stored -- no trimming, no lower-casing -- because every writer stores fixed
lower-case tokens, and a value that is not exactly one of them is not known
to mean what the nearest token means. Whatever is not recognised is `unknown`
(or, for the schedule, a problem): it is never `ok`.

Framework-neutral: standard library only. No Streamlit, no FastAPI, no
SQLAlchemy, no database, no environment, no clock, no logging.
"""

from __future__ import annotations

from typing import Literal

# The public states. The set is closed: nothing outside it is ever returned.
PIPELINE_STATES: tuple[str, ...] = ("ok", "degraded", "failed", "unknown")
PipelineState = Literal["ok", "degraded", "failed", "unknown"]

# Least to most severe, for worse_state only. `unknown` is the least severe
# so that a reported fault always outranks it -- which is NOT the same as
# `ok` outranking it: nothing in this module ever turns `unknown` into `ok`.
_SEVERITY: dict[PipelineState, int] = {"unknown": 0, "ok": 1, "degraded": 2, "failed": 3}


# =====================================================================================================================
# Legacy: pipeline_status
# =====================================================================================================================

# The run statuses that report a finished run with nothing wrong: a run that
# uploaded rows, one that found none to upload, and one that found its source
# files unchanged.
_OK_RUN_STATUSES = frozenset({"completed", "completed_no_new_rows", "skipped_no_source_changes"})

# Every failure a collector reports is spelled failed_<what> ("failed_upload",
# "failed_upload_none", ...), so the prefix is the rule: a failure this module
# has never seen spelled out is still a failure.
_FAILED_RUN_STATUS_PREFIX = "failed"


def state_for_run_status(status: object) -> PipelineState:
    """The state a legacy pipeline_status.status value reports.

    `ok` for a finished run, `failed` for anything beginning "failed", and
    `unknown` for everything else -- which includes two values that are
    written on purpose: "started" (a run began; its outcome has not been
    reported) and "preflight_check" (an install-time probe, not a run). A
    missing, empty or blank value, any other text and any value that is not
    a string are `unknown` too.
    """
    if not isinstance(status, str):
        return "unknown"
    if status in _OK_RUN_STATUSES:
        return "ok"
    if status.startswith(_FAILED_RUN_STATUS_PREFIX):
        return "failed"
    return "unknown"


def state_for_legacy_health(health_status: object) -> PipelineState:
    """The state a legacy pipeline_status.health_status value reports:
    healthy / degraded / auth_failure. Anything else, a missing value
    included, is `unknown`."""
    if health_status == "healthy":
        return "ok"
    if health_status == "degraded":
        return "degraded"
    if health_status == "auth_failure":
        return "failed"
    return "unknown"


# =====================================================================================================================
# Current: ingest_key_ids
# =====================================================================================================================

def state_for_current_health(health_status: object) -> PipelineState:
    """The state an ingest_key_ids.health_status value reports: healthy /
    degraded / error. Anything else is `unknown` -- including a missing
    value, which is what a key holds until its collector first reports."""
    if health_status == "healthy":
        return "ok"
    if health_status == "degraded":
        return "degraded"
    if health_status == "error":
        return "failed"
    return "unknown"


# What the collector reported about its own scheduled task, where that is a
# fault. The task being missing or disabled, or having no next run, means the
# collector will not run again: `failed`. Not having been able to read the
# schedule means the collector is running but its schedule is unconfirmed:
# `degraded`.
_SCHEDULE_PROBLEMS: dict[str, PipelineState] = {
    "task_missing": "failed",
    "task_disabled": "failed",
    "no_next_run": "failed",
    "query_failed": "degraded",
}
_SCHEDULE_OK = "healthy"


def schedule_problem(schedule_status: object) -> PipelineState | None:
    """The fault an ingest_key_ids.collector_schedule_status value reports,
    as a state, or None when it reports none.

    None is for "healthy" and for a missing value (a collector too old to
    report its schedule): neither says anything is wrong, and neither says
    anything is right about the pipeline either -- so None never improves a
    state, it only leaves one as it is.

    A value that is present but is not one of the known codes is `degraded`,
    exactly as "query_failed" is: the collector reported something about its
    schedule that cannot be read as healthy, so the schedule is unconfirmed.
    It is never treated as "no problem".
    """
    if schedule_status is None or schedule_status == _SCHEDULE_OK:
        return None
    if isinstance(schedule_status, str) and schedule_status in _SCHEDULE_PROBLEMS:
        return _SCHEDULE_PROBLEMS[schedule_status]
    return "degraded"


def state_for_current_report(health_status: object, schedule_status: object) -> PipelineState:
    """The state of a current heartbeat: its health, made worse -- never
    better -- by a schedule fault reported with it.

    With no schedule fault the health's own state stands, `unknown` included:
    a healthy schedule does not make an unreported health `ok`. With one, the
    result is the worse of the two, so a fault is shown even when the health
    is `ok` or `unknown`, and never hides a health that is already worse.
    """
    health = state_for_current_health(health_status)
    problem = schedule_problem(schedule_status)
    if problem is None:
        return health
    return worse_state(health, problem)


# =====================================================================================================================
# Comparing states
# =====================================================================================================================

def worse_state(first: PipelineState, second: PipelineState) -> PipelineState:
    """The more severe of two states: unknown < ok < degraded < failed.

    For combining a state with a FAULT reported alongside it, so that the
    fault shows. It is not a way to merge two signals in general: by this
    ordering the worse of `unknown` and `ok` is `ok`, so a caller must never
    pass "nothing wrong" as a state -- state_for_current_report does not call
    this at all when there is no fault to combine.
    """
    return first if _SEVERITY[first] >= _SEVERITY[second] else second
