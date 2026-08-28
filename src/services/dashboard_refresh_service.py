#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         dashboard_refresh_service.py
#
#  Description: Resolves the dashboard's live auto-refresh interval and
#               operating-hours gate (Continuous Ingestion Phase 4).
#               Kept separate from app.py so both are unit-testable
#               without importing the Streamlit entry point itself.
#
#***************************************************************

from datetime import datetime

DEFAULT_REFRESH_SECONDS = 10

ENV_VAR_NAME = "SORTVIEW_DASHBOARD_REFRESH_SECONDS"


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
#               DEFAULT_REFRESH_SECONDS -- never raises, and never
#               returns a value that would produce a zero or negative
#               refresh interval -- for a missing, non-integer, zero,
#               or negative value.
#
#  Parameters:  raw_value - The raw SORTVIEW_DASHBOARD_REFRESH_SECONDS
#                           environment value (e.g. from os.getenv), or
#                           None if unset.
#
#  Returns:     tuple[int, str | None] - The resolved positive integer
#                      interval in seconds, and a warning message to log
#                      (None if the value was accepted as-is or unset).
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

    return parsed, None
