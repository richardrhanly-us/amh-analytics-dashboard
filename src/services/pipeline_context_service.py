from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from formatting import format_relative_time

# WCAG 2.1 AA (>=4.5:1, normal-size text) foreground/background pairs for
# each pipeline-status family, one pair per Streamlit theme. The prior
# values (kept here only as a record, never used) failed AA in 3 of 4
# states -- as low as 3.07:1 for the degraded/running amber in light
# mode. Every state still pairs its color with an explicit text label
# (pipeline_status_label) -- color is reinforcement, never the only
# signal. Dark-mode backgrounds are flat opaque colors, not the prior
# alpha-blended rgba() tints: an rgba() background's true rendered
# contrast depends on whatever is composited underneath it in the DOM,
# which cannot be verified or asserted by a test -- a flat color can.
# Exact ratios are computed and locked in by
# tests/test_pipeline_status_contrast.py; change the two together.
_STATUS_COLOR_PAIRS = {
    # family: {theme: (foreground, background)}
    "healthy": {
        "light": ("#047857", "#ecfdf5"),  # 5.21:1
        "dark": ("#34d399", "#052e1f"),  # 7.71:1
    },
    "degraded": {
        "light": ("#92400e", "#fffbeb"),  # 6.84:1
        "dark": ("#fbbf24", "#3a2405"),  # 8.77:1
    },
    "failed": {
        "light": ("#b91c1c", "#fef2f2"),  # 5.91:1
        "dark": ("#f87171", "#450a0a"),  # 5.84:1
    },
    "unknown": {
        "light": ("#6b7280", "#f9fafb"),  # 4.63:1 -- already passed; left unchanged
        "dark": ("#94a3b8", "#1e293b"),  # 5.71:1
    },
}


def _status_colors(family: str, theme_base: str) -> tuple[str, str]:
    """(foreground, background) for `family` ("healthy"/"degraded"/
    "failed"/"unknown") under the given theme_base ("light"/"dark" --
    anything else is treated as light, matching every other theme_base
    check in this codebase)."""
    theme_key = "dark" if theme_base == "dark" else "light"
    return _STATUS_COLOR_PAIRS[family][theme_key]


def _parse_status_datetime(value, local_tz):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(local_tz)
        return dt.astimezone(local_tz)
    except Exception:
        return None


def build_pipeline_context(pipeline_status, df_live_raw, now_ct, local_tz, theme_base):
    checkins_updated = None
    if len(df_live_raw) > 0 and "datetime" in df_live_raw.columns:
        latest_dt = df_live_raw["datetime"].max()
        if pd.notna(latest_dt):
            if getattr(latest_dt, "tzinfo", None) is None:
                checkins_updated = latest_dt.tz_localize(local_tz)
            else:
                checkins_updated = latest_dt.tz_convert(local_tz)

    pipeline_status_label = "Unknown"
    pipeline_status_color, pipeline_status_bg = _status_colors("unknown", theme_base)

    status_updated_dt = None
    last_run = None
    last_attempt = None
    watcher_last_active = None

    checkins_rows = 0
    rejects_rows = 0
    transit_items = 0
    problem_items = 0
    uploaded_checkins_rows = 0
    uploaded_rejects_rows = 0
    checkins_bad_datetime_rows = 0
    rejects_bad_datetime_rows = 0
    destination_breakdown = {}

    if pipeline_status:
        status_updated_raw = pipeline_status.get("updated_at")
        last_run_raw = pipeline_status.get("last_run")
        last_attempt_raw = pipeline_status.get("last_attempt")
        watcher_last_active_raw = pipeline_status.get("watcher_last_active_at")

        checkins_rows = pipeline_status.get("checkins_rows", 0)
        rejects_rows = pipeline_status.get("rejects_rows", 0)
        transit_items = pipeline_status.get("transit_items", 0)
        problem_items = pipeline_status.get("problem_items", 0)
        uploaded_checkins_rows = pipeline_status.get("uploaded_checkins_rows", 0)
        uploaded_rejects_rows = pipeline_status.get("uploaded_rejects_rows", 0)
        checkins_bad_datetime_rows = pipeline_status.get("checkins_bad_datetime_rows", 0)
        rejects_bad_datetime_rows = pipeline_status.get("rejects_bad_datetime_rows", 0)
        destination_breakdown = pipeline_status.get("destination_breakdown", {}) or {}

        status_updated_dt = _parse_status_datetime(status_updated_raw, local_tz)
        last_run = _parse_status_datetime(last_run_raw, local_tz)
        last_attempt = _parse_status_datetime(last_attempt_raw, local_tz)
        watcher_last_active = _parse_status_datetime(watcher_last_active_raw, local_tz)

    app_refreshed_str = now_ct.strftime("%b %d, %Y %I:%M %p")

    pipeline_status_written_str = (
        status_updated_dt.strftime("%b %d, %Y %I:%M %p")
        if status_updated_dt else "N/A"
    )
    pipeline_last_run_str = (
        last_run.strftime("%b %d, %Y %I:%M %p")
        if last_run else "N/A"
    )
    pipeline_last_attempt_str = (
        last_attempt.strftime("%b %d, %Y %I:%M %p")
        if last_attempt else "N/A"
    )

    pipeline_status_written_ago = format_relative_time(status_updated_dt, now_ct)
    pipeline_last_run_ago = format_relative_time(last_run, now_ct)
    pipeline_last_attempt_ago = format_relative_time(last_attempt, now_ct)

    latest_checkin_str = (
        checkins_updated.strftime("%b %d, %Y %I:%M %p")
        if checkins_updated else "N/A"
    )
    latest_checkin_ago = format_relative_time(checkins_updated, now_ct)

    # pipeline_status may contain both scheduled Collector run status and
    # continuous-agent heartbeat status. During coexistence or migration,
    # stale values from an inactive writer can remain in the shared row.
    # Prefer heartbeat health only when its watcher timestamp is newer than
    # the latest Collector attempt; otherwise the Collector run status wins.

    health_status = pipeline_status.get("health_status") if pipeline_status else None
    pipeline_run_status = pipeline_status.get("status", "unknown") if pipeline_status else "unknown"

    collector_last_active = max(
        (dt for dt in (last_attempt, last_run) if dt is not None),
        default=None,
    )

    heartbeat_is_current = (
        health_status is not None
        and watcher_last_active is not None
        and (
            collector_last_active is None
            or watcher_last_active > collector_last_active
        )
    )

    if heartbeat_is_current:
        status_code_text = str(health_status)

        if health_status == "healthy":
            pipeline_status_label = "Pipeline Healthy"
            pipeline_status_color, pipeline_status_bg = _status_colors("healthy", theme_base)
            pipeline_result_text = "Continuous agent reporting healthy"
        elif health_status == "degraded":
            pipeline_status_label = "Pipeline Degraded"
            pipeline_status_color, pipeline_status_bg = _status_colors("degraded", theme_base)
            pipeline_result_text = (
                "Continuous agent reporting degraded -- backlog, quarantine, "
                "or unresolved delivery failure"
            )
        elif health_status == "auth_failure":
            pipeline_status_label = "Pipeline Auth Failure"
            pipeline_status_color, pipeline_status_bg = _status_colors("failed", theme_base)
            pipeline_result_text = "Continuous agent cannot authenticate -- check the agent token"

        pipeline_expanded = health_status != "healthy"

    else:
        status_code_text = str(pipeline_run_status)

        if pipeline_run_status == "completed":
            pipeline_status_label = "Pipeline Healthy"
            pipeline_status_color, pipeline_status_bg = _status_colors("healthy", theme_base)
            pipeline_result_text = (
                f"Uploaded {uploaded_checkins_rows:,} new checkins and "
                f"{uploaded_rejects_rows:,} new rejects this run"
            )
        elif pipeline_run_status == "completed_no_new_rows":
            pipeline_status_label = "Pipeline Healthy"
            pipeline_status_color, pipeline_status_bg = _status_colors("healthy", theme_base)
            pipeline_result_text = "Run completed, but no new rows were uploaded"
        elif pipeline_run_status == "skipped_no_source_changes":
            pipeline_status_label = "Pipeline Healthy"
            pipeline_status_color, pipeline_status_bg = _status_colors("healthy", theme_base)
            pipeline_result_text = "No new source changes detected this run"
        elif str(pipeline_run_status).startswith("failed"):
            pipeline_status_label = "Pipeline Failed"
            pipeline_status_color, pipeline_status_bg = _status_colors("failed", theme_base)
            pipeline_result_text = "Latest run failed"
        elif pipeline_run_status == "started":
            pipeline_status_label = "Pipeline Running"
            pipeline_status_color, pipeline_status_bg = _status_colors("degraded", theme_base)
            pipeline_result_text = "Run in progress"
        else:
            pipeline_status_label = "Pipeline Status Unknown"
            pipeline_status_color, pipeline_status_bg = _status_colors("unknown", theme_base)
            pipeline_result_text = "Unknown"

        pipeline_expanded = pipeline_run_status not in [
            "completed",
            "completed_no_new_rows",
            "skipped_no_source_changes",
        ]

    if isinstance(destination_breakdown, dict) and destination_breakdown:
        destination_breakdown_text = ", ".join(
            [f"{k}: {int(v):,}" for k, v in destination_breakdown.items()]
        )
    else:
        destination_breakdown_text = "N/A"

    return {
        "pipeline_status_label": pipeline_status_label,
        "pipeline_status_color": pipeline_status_color,
        "pipeline_status_bg": pipeline_status_bg,
        "pipeline_expanded": pipeline_expanded,
        "app_refreshed_str": app_refreshed_str,
        "latest_checkin_str": latest_checkin_str,
        "latest_checkin_ago": latest_checkin_ago,
        "pipeline_status_written_str": pipeline_status_written_str,
        "pipeline_status_written_ago": pipeline_status_written_ago,
        "pipeline_last_attempt_str": pipeline_last_attempt_str,
        "pipeline_last_attempt_ago": pipeline_last_attempt_ago,
        "pipeline_last_run_str": pipeline_last_run_str,
        "pipeline_last_run_ago": pipeline_last_run_ago,
        "pipeline_result_text": pipeline_result_text,
        "status_code_text": status_code_text,
        "checkins_rows": checkins_rows,
        "rejects_rows": rejects_rows,
        "uploaded_checkins_rows": uploaded_checkins_rows,
        "uploaded_rejects_rows": uploaded_rejects_rows,
        "checkins_bad_datetime_rows": checkins_bad_datetime_rows,
        "rejects_bad_datetime_rows": rejects_bad_datetime_rows,
        "transit_items": transit_items,
        "problem_items": problem_items,
        "destination_breakdown_text": destination_breakdown_text,
        # Collector run/schedule diagnostics exist only in the Contract v2 heartbeat. None = no such lines at all, so
        # a v1 branch's panel renders exactly as it did before they existed.
        "collector_diagnostics_lines": None,
    }


# --- Contract v2 collector run / schedule diagnostics -----------------------------------------------------------------
#
# The collector reads its own Windows Scheduled Task and reports, in every heartbeat: Task Scheduler's LastRunTime and
# NextRunTime, how long the invocation took, and one fixed schedule code (collector/v2_schedule.py). This section turns
# those four stored values into the panel's four lines and one judgement about the schedule.
#
# THE DASHBOARD ALSO REASONS ABOUT TIME. "healthy" in the latest heartbeat only describes the moment it was sent. If the
# collector then stops running -- the task is deleted or disabled, the machine is off -- no newer heartbeat arrives to
# say so, and the stored next-run time simply slides into the past. So a healthy schedule whose next run is overdue by
# more than the grace below is shown as an error. The latest row IS the newest heartbeat for the key, so "overdue and
# not replaced" needs no second query.
#
# THE GRACE IS TWO CADENCES. The task runs every 15 minutes with MultipleInstancesPolicy=IgnoreNew: if a run is still
# going when the next trigger fires, that trigger is legitimately skipped, and the next heartbeat can be a full cadence
# later than the stored next-run time. One cadence of grace would therefore raise a false alarm on a single long run;
# two tolerates one skipped trigger and still reports a stopped collector within half an hour. The cadence is the
# collector's own constant (collector/task_settings.py DEFAULT_CADENCE_MINUTES); nothing under src/ imports the
# collector package, so it is restated here and tests/test_v2_collector_diagnostics.py pins the two together.
COLLECTOR_CADENCE_MINUTES = 15
SCHEDULE_OVERDUE_GRACE_CADENCES = 2
SCHEDULE_OVERDUE_GRACE = timedelta(minutes=COLLECTOR_CADENCE_MINUTES * SCHEDULE_OVERDUE_GRACE_CADENCES)

# What the schedule line says for each code the collector can report (ingest_v2_models.SCHEDULE_STATUSES). Fixed text,
# stated in words: colour reinforces the panel's state but is never the only signal.
_SCHEDULE_REASON_TEXT = {
    "task_missing": "scheduled task not found",
    "task_disabled": "scheduled task is disabled",
    "no_next_run": "no future run scheduled",
}
_SCHEDULE_QUERY_FAILED_TEXT = "schedule could not be checked"
_SCHEDULE_NOT_REPORTED_TEXT = "Unknown (not reported by this collector version)"

# The four judgements. ERROR is demonstrable (the collector said so, or the next run is overdue); INDETERMINATE is "the
# collector is evidently running, but could not read its own schedule"; UNKNOWN is an older collector that reports none
# of this and must change nothing.
SCHEDULE_OK, SCHEDULE_ERROR, SCHEDULE_INDETERMINATE, SCHEDULE_UNKNOWN = "ok", "error", "indeterminate", "unknown"


def _format_time_until(dt_value, now_value):
    """"in 12 min" for a future instant; the ordinary "... ago" wording once it has passed."""
    if dt_value is None:
        return "N/A"
    seconds = (dt_value - now_value).total_seconds()
    if seconds <= 0:
        return format_relative_time(dt_value, now_value)
    minutes = int(seconds // 60)
    if minutes < 1:
        return "in under a minute"
    if minutes < 60:
        return f"in {minutes} min"
    hours = minutes // 60
    return "in 1 hr" if hours == 1 else f"in {hours} hrs"


def _format_duration(duration_ms):
    """Milliseconds -> "3.24 seconds". Anything that is not a non-negative whole number reads as N/A."""
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        return "N/A"
    return f"{duration_ms / 1000:.2f} seconds"


def build_collector_diagnostics(v2_ingest_status, now_ct, local_tz):
    """(judgement, reason text or None, the four panel lines) for a Contract v2 heartbeat row.

    Every field is optional: a heartbeat from a collector that predates the diagnostics has all four absent/None, which
    renders as N/A / Unknown and yields SCHEDULE_UNKNOWN -- never an exception, and never a schedule error."""
    last_run = _parse_status_datetime(v2_ingest_status.get("collector_last_run_at"), local_tz)
    next_run = _parse_status_datetime(v2_ingest_status.get("collector_next_run_at"), local_tz)
    status = v2_ingest_status.get("collector_schedule_status")

    def _display(dt):
        return dt.strftime("%b %d, %Y %I:%M %p") if dt else "N/A"

    if not status:
        judgement, reason = SCHEDULE_UNKNOWN, None
        schedule_text = _SCHEDULE_NOT_REPORTED_TEXT
    elif status == "healthy":
        if next_run is None:  # "healthy" without a next run cannot be trusted as healthy
            judgement, reason = SCHEDULE_ERROR, _SCHEDULE_REASON_TEXT["no_next_run"]
        elif now_ct - next_run > SCHEDULE_OVERDUE_GRACE:
            judgement, reason = SCHEDULE_ERROR, "scheduled run overdue"
        else:
            judgement, reason = SCHEDULE_OK, None
        schedule_text = "Healthy" if judgement == SCHEDULE_OK else f"Error — {reason}"
    elif status in _SCHEDULE_REASON_TEXT:
        judgement, reason = SCHEDULE_ERROR, _SCHEDULE_REASON_TEXT[status]
        schedule_text = f"Error — {reason}"
    else:  # query_failed, or a code this dashboard does not know: the schedule's real state is not known
        judgement, reason = SCHEDULE_INDETERMINATE, _SCHEDULE_QUERY_FAILED_TEXT
        schedule_text = f"Error — {reason}"

    last_run_line = (
        f"Last Collector Run: {_display(last_run)} ({format_relative_time(last_run, now_ct)})" if last_run
        else "Last Collector Run: N/A"
    )
    next_run_line = (
        f"Next Scheduled Run: {_display(next_run)} ({_format_time_until(next_run, now_ct)})" if next_run
        else "Next Scheduled Run: N/A"
    )
    lines = [
        last_run_line,
        next_run_line,
        f"Latest Run Duration: {_format_duration(v2_ingest_status.get('collector_run_duration_ms'))}",
        f"Collector Schedule: {schedule_text}",
    ]
    return judgement, reason, lines


#***************************************************************
#
#  Function:     build_v2_aware_pipeline_context
#
#  Description: Government-readiness audit, Part 5: the minimum
#               coexistence-aware health behavior needed for a v2 pilot.
#               A branch with no v2 heartbeat ever recorded (the
#               overwhelming majority today, including every branch that
#               has never been cut over) delegates ENTIRELY to
#               build_pipeline_context, unchanged -- so an untouched v1
#               historical branch's status stays exactly as understandable
#               as it is today. Only once a branch's v2 key has actually
#               reported at least one heartbeat does this function prefer
#               that v2 status over v1's pipeline_status, so a branch that
#               has moved to v2 does not read as "stale" merely because
#               its v1 Collector/agent stopped writing pipeline_status.
#
#               This does not redesign monitoring generally -- it reuses
#               the same status colors, labels and shape as
#               build_pipeline_context, and touches nothing about how
#               v1-only branches are shown.
#
#               Once v2 is active, no field of the frozen v1
#               pipeline_status row is shown -- each panel field is either
#               v2-sourced or explicitly N/A (None for counters):
#                 Latest Status Row Written  <- last_heartbeat_at (server
#                                               stamp of the heartbeat write)
#                 Last Pipeline Attempt      <- watcher_last_active_at
#                                               (collector/v2_run.py stamps it
#                                               on EVERY run, success or not)
#                 Last Successful Upload Run <- last_success_at (advances
#                                               only on a successful run)
#                 Run Summary counters and   <- N/A: the v2 heartbeat
#                 Destination Breakdown         (StatusV2Request) carries no
#                                               per-run counters at all
#                 Last Collector Run,        <- collector_last_run_at,
#                 Next Scheduled Run,           collector_next_run_at,
#                 Latest Run Duration,          collector_run_duration_ms,
#                 Collector Schedule            collector_schedule_status
#                                               (build_collector_diagnostics;
#                                               v2 only -- a v1 branch has no
#                                               such lines)
#               App Last Refreshed and Latest Checkin in DB are not v1
#               status fields (now_ct and the live dataframe) and are kept.
#
#  Parameters:  pipeline_status - v1 pipeline_status row (see
#                                 build_pipeline_context).
#               v2_ingest_status - The tenant's latest v2 heartbeat
#                                 (data_loader.load_v2_ingest_status), or
#                                 None if it has never reported.
#               df_live_raw - Live checkin dataframe (see
#                            build_pipeline_context).
#               now_ct - Current local time.
#               local_tz - Local timezone.
#               theme_base - "light" or "dark".
#
#  Returns:     dict - Same key shape as build_pipeline_context.
#
#***************************************************************

_V2_HEALTH_STATUS_FAMILY = {
    "healthy": "healthy",
    "degraded": "degraded",
    "error": "failed",
}


def build_v2_aware_pipeline_context(pipeline_status, v2_ingest_status, df_live_raw, now_ct, local_tz, theme_base):
    if not v2_ingest_status:
        return build_pipeline_context(pipeline_status, df_live_raw, now_ct, local_tz, theme_base)

    base = build_pipeline_context(pipeline_status, df_live_raw, now_ct, local_tz, theme_base)

    health_status = v2_ingest_status.get("health_status")
    family = _V2_HEALTH_STATUS_FAMILY.get(health_status, "unknown")
    pipeline_status_color, pipeline_status_bg = _status_colors(family, theme_base)

    if family == "healthy":
        pipeline_status_label = "Pipeline Healthy"
        pipeline_result_text = "Contract v2 collector reporting healthy"
    elif family == "degraded":
        pipeline_status_label = "Pipeline Degraded"
        pending = v2_ingest_status.get("pending_outbox_count")
        quarantined = v2_ingest_status.get("quarantined_count")
        pipeline_result_text = (
            "Contract v2 collector reporting degraded -- "
            f"pending {pending if pending is not None else 'unknown'}, "
            f"quarantined {quarantined if quarantined is not None else 'unknown'}"
        )
    elif family == "failed":
        last_error_class = v2_ingest_status.get("last_error_class")
        pipeline_status_label = "Pipeline Auth Failure" if last_error_class == "auth_failure" else "Pipeline Failed"
        pipeline_result_text = (
            "Contract v2 collector cannot authenticate -- check the ingest key"
            if last_error_class == "auth_failure"
            else f"Contract v2 collector reporting an error (last_error_class={last_error_class or 'unknown'})"
        )
    else:
        pipeline_status_label = "Pipeline Status Unknown"
        pipeline_result_text = "No Contract v2 status has been recorded yet"

    # The collector's schedule can only make the headline WORSE, and never displaces a more serious one:
    #   * an ingestion error (auth failure included) keeps its own label and text -- the schedule lines still appear
    #     beneath it;
    #   * a demonstrable schedule error (task missing / disabled / no future run / overdue) means a pipeline that
    #     reported healthy or degraded cannot honestly be shown as either: "Pipeline Schedule Error";
    #   * a schedule that could not be checked leaves a healthy pipeline "Pipeline Degraded" -- heartbeats are arriving,
    #     so the collector is running, but its schedule is not confirmed; an already-degraded one stays as it is;
    #   * an older collector that reports no schedule at all changes nothing.
    schedule_judgement, schedule_reason, collector_diagnostics_lines = build_collector_diagnostics(
        v2_ingest_status, now_ct, local_tz
    )
    if family != "failed":
        if schedule_judgement == SCHEDULE_ERROR:
            reported = "healthy" if family == "healthy" else ("degraded" if family == "degraded" else "no status")
            family = "failed"
            pipeline_status_color, pipeline_status_bg = _status_colors(family, theme_base)
            pipeline_status_label = "Pipeline Schedule Error"
            pipeline_result_text = (
                f"Collector schedule error -- {schedule_reason} (ingestion last reported {reported})"
            )
        elif schedule_judgement == SCHEDULE_INDETERMINATE and family == "healthy":
            family = "degraded"
            pipeline_status_color, pipeline_status_bg = _status_colors(family, theme_base)
            pipeline_status_label = "Pipeline Degraded"
            pipeline_result_text = (
                "Contract v2 collector reporting healthy, but its schedule could not be checked"
            )

    last_heartbeat = _parse_status_datetime(v2_ingest_status.get("last_heartbeat_at"), local_tz)
    last_attempt = _parse_status_datetime(v2_ingest_status.get("watcher_last_active_at"), local_tz)
    last_success = _parse_status_datetime(v2_ingest_status.get("last_success_at"), local_tz)

    def _display(dt):
        return dt.strftime("%b %d, %Y %I:%M %p") if dt else "N/A"

    return {
        "pipeline_status_label": pipeline_status_label,
        "pipeline_status_color": pipeline_status_color,
        "pipeline_status_bg": pipeline_status_bg,
        "pipeline_expanded": family != "healthy",
        "app_refreshed_str": base["app_refreshed_str"],
        "latest_checkin_str": base["latest_checkin_str"],
        "latest_checkin_ago": base["latest_checkin_ago"],
        "pipeline_status_written_str": _display(last_heartbeat),
        "pipeline_status_written_ago": format_relative_time(last_heartbeat, now_ct),
        "pipeline_last_attempt_str": _display(last_attempt),
        "pipeline_last_attempt_ago": format_relative_time(last_attempt, now_ct),
        "pipeline_last_run_str": _display(last_success),
        "pipeline_last_run_ago": format_relative_time(last_success, now_ct),
        "pipeline_result_text": pipeline_result_text,
        "status_code_text": str(health_status or "unknown"),
        "checkins_rows": None,
        "rejects_rows": None,
        "uploaded_checkins_rows": None,
        "uploaded_rejects_rows": None,
        "checkins_bad_datetime_rows": None,
        "rejects_bad_datetime_rows": None,
        "transit_items": None,
        "problem_items": None,
        "destination_breakdown_text": "N/A (not reported by the Contract v2 collector)",
        "collector_diagnostics_lines": collector_diagnostics_lines,
    }
