#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         filters_service.py
#
#  Description: Provides date filter controls for the SortView
#               dashboard. This file determines the active reporting
#               date range based on the selected dashboard view, the
#               available data range, and the current local date.
#
#***************************************************************

import pandas as pd
import streamlit as st

#***************************************************************
#
#  Function:     resolve_date_filters
#
#  Description: Determines the start and end dates used by dashboard
#               reporting views. For Overview, Reports, and Transits,
#               this function displays sidebar date filter controls
#               and supports single-day, rolling-range, month-based,
#               all-time, and custom date selections.
#
#  Parameters:  selected_view - Currently selected dashboard section.
#               min_date - Earliest available date in the dataset.
#               max_date - Latest available date in the dataset.
#               local_today - Current local date.
#
#  Returns:     tuple - Selected start date and end date.
#
#***************************************************************

DATE_RANGE_MODE_OPTIONS = [
    "Single Day", "Last 7 Days", "Last 30 Days", "Month to Date", "Full Month", "All Time", "Custom",
]

# Dashboard performance pass: "All Time" was the previous default, which
# means every first visit to Overview/Reports/Transits paid the cost of
# filtering/transforming the entire history table in pandas (a
# significant, measured cost -- see the dashboard-date-range performance
# report) before the user had asked for anything beyond a normal
# reporting window. "Last 30 Days" is a much lighter default; "All Time"
# remains fully available as an explicit choice, just no longer the
# unconditional first thing every session pays for.
DEFAULT_DATE_RANGE_MODE = "Last 30 Days"

# The widget's own key -- shared across Overview/Reports/Transits, since
# resolve_date_filters renders this same radio from the same call site
# regardless of which of the three is active (see app.py). This alone is
# NOT enough to survive a detour through Live Today, though: Streamlit
# clears a widget's session_state entry for any script run where that
# widget isn't instantiated at all, and Live Today never renders this
# radio. Confirmed with an AppTest-based check during development --
# switching Overview -> Live Today -> Reports reset an explicit "All
# Time" choice back to the default despite the key, because the key's
# entry had been cleared out from under it during the Live Today run.
DATE_RANGE_MODE_STATE_KEY = "dashboard_date_range_mode"

# A second, plain (non-widget) session_state slot that is never tied to
# whether this widget rendered on a given run, so it survives a detour
# through Live Today intact. It's used to re-seed the widget's own key
# every time the widget is about to render, and is kept in sync with the
# widget's latest value right after. This is what actually delivers "an
# explicit user choice is respected until they change it again" -- the
# widget key alone only covers navigating directly between Overview/
# Reports/Transits, not a round trip through a view that never renders
# the widget at all.
DATE_RANGE_MODE_PERSISTED_KEY = "dashboard_date_range_mode_persisted"


def resolve_date_filters(selected_view, min_date, max_date, local_today):
    # Default to the full available range, capped at the current local date.
    start_date = min_date
    end_date = min(max_date, local_today)

    # Only reporting-style views need sidebar date filters.
    if selected_view in ["Overview", "Reports", "Transits"]:
        st.sidebar.header("Filters")

        # Prevent filters from selecting dates beyond the current local day.
        max_allowed_date = min(max_date, local_today)

        # Re-seed the widget's key from the durable, non-widget slot every
        # time this radio is about to render -- covers both "never chosen
        # anything yet this session" (durable slot also absent -> use the
        # default) and "chose something, then took a detour through a
        # view that doesn't render this widget" (durable slot has the
        # real value; the widget's own key was cleared in between).
        if DATE_RANGE_MODE_STATE_KEY not in st.session_state:
            st.session_state[DATE_RANGE_MODE_STATE_KEY] = st.session_state.get(
                DATE_RANGE_MODE_PERSISTED_KEY, DEFAULT_DATE_RANGE_MODE
            )

        # No index/value is passed here -- the widget's value is driven
        # entirely by session_state, seeded just above.
        range_mode = st.sidebar.radio(
            "Date Range",
            DATE_RANGE_MODE_OPTIONS,
            key=DATE_RANGE_MODE_STATE_KEY,
        )

        # Mirror the current choice into the durable slot immediately, so
        # it's available to re-seed the widget the next time it renders.
        st.session_state[DATE_RANGE_MODE_PERSISTED_KEY] = range_mode

        # Filter to one selected day.
        if range_mode == "Single Day":
            selected_day = st.sidebar.date_input(
                "Choose Day",
                value=max_allowed_date,
                min_value=min_date,
                max_value=max_allowed_date
            )
            start_date = selected_day
            end_date = selected_day

        # Filter to the most recent seven-day window.
        elif range_mode == "Last 7 Days":
            end_date = max_allowed_date
            start_date = max(min_date, end_date - pd.Timedelta(days=6))

        # Filter to the most recent thirty-day window.
        elif range_mode == "Last 30 Days":
            end_date = max_allowed_date
            start_date = max(min_date, end_date - pd.Timedelta(days=29))

        # Filter from the first day of the current month through the latest allowed date.
        elif range_mode == "Month to Date":
            end_date = max_allowed_date
            start_date = max(min_date, end_date.replace(day=1))

        # Filter to a completed calendar month.
        elif range_mode == "Full Month":
            first_day_current_month = local_today.replace(day=1)
            last_day_previous_month = first_day_current_month - pd.Timedelta(days=1)

            month_starts = pd.date_range(
                start=min_date.replace(day=1),
                end=last_day_previous_month.replace(day=1),
                freq="MS"
            )

            month_options = []
            month_map = {}

            # Build a list of completed months that fit inside the available data range.
            for month_start in month_starts:
                month_start_date = month_start.date()
                next_month_start = (month_start + pd.offsets.MonthBegin(1)).date()
                month_end_date = next_month_start - pd.Timedelta(days=1)

                if (
                    month_start_date >= min_date
                    and month_end_date <= max_allowed_date
                    and month_end_date < first_day_current_month
                ):
                    label = month_start.strftime("%B %Y")
                    month_options.append(label)
                    month_map[label] = (month_start_date, month_end_date)

            month_options = list(reversed(month_options))

            # Use the selected completed month when available.
            if month_options:
                selected_month_label = st.sidebar.selectbox(
                    "Choose Full Month",
                    month_options,
                    index=0
                )
                start_date, end_date = month_map[selected_month_label]
            else:
                st.sidebar.warning("No completed full months are available in the current dataset.")
                start_date = min_date
                end_date = max_allowed_date

        # Filter to all available data.
        elif range_mode == "All Time":
            start_date = min_date
            end_date = max_allowed_date

        # Let the user choose a custom date range.
        elif range_mode == "Custom":
            custom_range = st.sidebar.date_input(
                "Custom Range",
                value=(max(min_date, max_allowed_date - pd.Timedelta(days=6)), max_allowed_date),
                min_value=min_date,
                max_value=max_allowed_date
            )

            # Streamlit may return a tuple/list for ranges or a single date for one selection.
            if isinstance(custom_range, (list, tuple)):
                if len(custom_range) == 2:
                    start_date, end_date = custom_range
                elif len(custom_range) == 1:
                    start_date = custom_range[0]
                    end_date = custom_range[0]
                else:
                    start_date = max(min_date, max_allowed_date - pd.Timedelta(days=6))
                    end_date = max_allowed_date
            else:
                start_date = custom_range
                end_date = custom_range

            # If the selected range is reversed, correct the order.
            if start_date > end_date:
                start_date, end_date = end_date, start_date

        # Show the active date range in the sidebar.
        st.sidebar.caption(
            f"Showing: {start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}"
        )

    return start_date, end_date
