#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         dashboard_refresh_service.py
#
#  Description: Resolves the dashboard's live auto-refresh interval,
#               operating-hours gate, and live-data cache-key identity
#               (Continuous Ingestion Phase 4 / Collector-cadence-aligned
#               refresh). Kept separate from app.py so all of it is
#               unit-testable without importing the Streamlit entry point
#               itself.
#
#***************************************************************

from datetime import datetime

# Aligned with the production scheduled Collector's own cadence (every 15
# minutes) -- polling Live Today's pipeline status every few seconds or
# even every 10s, as this used to default to, was never going to observe a
# new successful run any sooner, it just burned DB reads. 180s (3 minutes)
# still notices a new run promptly without hammering the database between
# Collector runs.
DEFAULT_REFRESH_SECONDS = 180

# Hard floor so a misconfigured SORTVIEW_DASHBOARD_REFRESH_SECONDS can
# never regress production back to sub-minute (or even sub-3-minute)
# polling -- see resolve_refresh_interval_seconds.
MIN_REFRESH_SECONDS = 180

ENV_VAR_NAME = "SORTVIEW_DASHBOARD_REFRESH_SECONDS"

# Sentinel run-identity used by resolve_live_data_cache_key when
# pipeline_status is missing/empty or its last_run column is NULL, i.e. no
# scheduled Collector run has ever completed successfully for this tenant
# yet. Distinct from any real last_run timestamp string.
NO_SUCCESSFUL_RUN_KEY = "__no_successful_run__"


#***************************************************************
#
#  Function:     is_operating_hours
#
#  Description: Determines whether the current Central Time value
#               falls within the dashboard's active operating window.
#               Auto-refresh is only enabled during these hours to
#               reduce unnecessary refreshes outside normal use.
#
#  Parameters:  now_ct - Current datetime in the application timezone.
#
#  Returns:     bool - True if the time is between 6:00 AM and
#               8:59 PM Central Time; otherwise False.
#
#***************************************************************

def is_operating_hours(now_ct: datetime) -> bool:
    return 6 <= now_ct.hour < 21


#***************************************************************
#
#  Function:     resolve_refresh_interval_seconds
#
#  Description: Parses the configured dashboard refresh interval from a
#               raw environment-variable string. Falls back to
#               DEFAULT_REFRESH_SECONDS -- never raises -- for a missing,
#               non-integer, zero, or negative value, and clamps any
#               valid-but-too-frequent value up to MIN_REFRESH_SECONDS so
#               this can never resolve to sub-3-minute automatic polling,
#               however it is configured.
#
#  Parameters:  raw_value - The raw SORTVIEW_DASHBOARD_REFRESH_SECONDS
#                           environment value (e.g. from os.getenv), or
#                           None if unset.
#
#  Returns:     tuple[int, str | None] - The resolved integer interval in
#                      seconds (always >= MIN_REFRESH_SECONDS), and a
#                      warning message to log (None if the value was
#                      accepted as-is or unset).
#
#***************************************************************

def resolve_refresh_interval_seconds(raw_value: str | None) -> tuple[int, str | None]:
    if raw_value is None or raw_value.strip() == "":
        return DEFAULT_REFRESH_SECONDS, None

    try:
        parsed = int(raw_value.strip())
    except ValueError:
        return (
            DEFAULT_REFRESH_SECONDS,
            (
                f"{ENV_VAR_NAME}={raw_value!r} is not a valid integer -- "
                f"falling back to {DEFAULT_REFRESH_SECONDS}s."
            ),
        )

    if parsed <= 0:
        return (
            DEFAULT_REFRESH_SECONDS,
            (
                f"{ENV_VAR_NAME}={raw_value!r} must be a positive number of seconds -- "
                f"falling back to {DEFAULT_REFRESH_SECONDS}s."
            ),
        )

    if parsed < MIN_REFRESH_SECONDS:
        return (
            MIN_REFRESH_SECONDS,
            (
                f"{ENV_VAR_NAME}={raw_value!r} is below the minimum automatic Live "
                f"Today poll interval of {MIN_REFRESH_SECONDS}s (the production "
                f"scheduled Collector runs every 15 minutes, so polling more often "
                f"than every {MIN_REFRESH_SECONDS}s cannot observe a new run any "
                f"sooner) -- using {MIN_REFRESH_SECONDS}s instead."
            ),
        )

    return parsed, None


#***************************************************************
#
#  Function:     resolve_live_data_cache_key
#
#  Description: Builds the cache-key value passed as `refresh_count` to
#               load_checkins_df/load_rejects_df/load_acs_df on every
#               Live Today fragment run (automatic poll or manual
#               Refresh now). Deliberately built from only two inputs --
#               the scheduled Collector's own successful-run identity
#               (pipeline_status["last_run"], which only advances on a
#               successful scheduled run -- see collector/run.py's
#               run_once) and a manual-refresh counter -- so an automatic
#               poll that observes no new successful run reuses the exact
#               same key as last time (a cache HIT, no DB read), while a
#               new successful run or a manual Refresh now click produces
#               a new key (a cache MISS, a real read).
#
#               Deliberately does NOT take pipeline_status["updated_at"]
#               or last_attempt: updated_at is bumped by every
#               pipeline_status write including continuous-agent
#               heartbeat fields (roughly every 60s) and last_attempt
#               advances on failed scheduled attempts too, so either one
#               would force a reload of live event data that has not
#               actually changed. See docs/collector-v2.md and
#               main.py's _PIPELINE_STATUS_HEARTBEAT_FIELDS for the
#               writer split this deliberately ignores.
#
#  Parameters:  last_run - pipeline_status["last_run"] as loaded (an ISO
#                          timestamp string), or None/falsy if no
#                          scheduled Collector run has ever completed
#                          successfully for this tenant yet.
#               manual_refresh_count - The tenant-scoped manual Refresh
#                          now counter from st.session_state; incrementing
#                          it always produces a new key regardless of
#                          last_run.
#
#  Returns:     str - Deterministic cache-key value: identical input
#                     pairs always produce the identical string.
#
#***************************************************************

def resolve_live_data_cache_key(last_run: str | None, manual_refresh_count: int) -> str:
    run_identity = last_run if last_run else NO_SUCCESSFUL_RUN_KEY
    return f"{run_identity}::{manual_refresh_count}"


#***************************************************************
#
#  Function:     resolve_run_every_seconds
#
#  Description: Resolves the effective `run_every` value passed to Live
#               Today's auto-refreshing st.fragment. Centralizes the two
#               independent reasons auto-refresh can be off -- outside
#               operating hours, or the user has explicitly paused it
#               (WCAG 2.2.2 Pause, Stop, Hide) -- so app.py never has to
#               combine them itself. A user-set pause always wins: it is
#               honored the same whether or not the dashboard is
#               currently inside operating hours.
#
#  Parameters:  is_operating_hours_now - Whether the current time falls
#                           within the dashboard's active operating
#                           window (see is_operating_hours).
#               is_paused - Whether the user has paused live updates for
#                           this session (see st.session_state).
#               interval_seconds - The configured refresh cadence in
#                           seconds (see resolve_refresh_interval_seconds).
#
#  Returns:     int | None - The interval to pass as st.fragment's
#                      run_every, or None to disable auto-refresh.
#
#***************************************************************

def resolve_run_every_seconds(
    *, is_operating_hours_now: bool, is_paused: bool, interval_seconds: int
) -> int | None:
    if is_paused or not is_operating_hours_now:
        return None
    return interval_seconds
