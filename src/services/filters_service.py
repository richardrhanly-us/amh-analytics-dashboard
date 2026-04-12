import pandas as pd
import streamlit as st


def resolve_date_filters(selected_view, min_date, max_date, local_today):
    start_date = min_date
    end_date = min(max_date, local_today)

    if selected_view in ["Overview", "Reports", "Transits"]:
        st.sidebar.header("Filters")

        max_allowed_date = min(max_date, local_today)

        range_mode = st.sidebar.radio(
            "Date Range",
            ["Single Day", "Last 7 Days", "Last 30 Days", "Month to Date", "Full Month", "All Time", "Custom"],
            index=5
        )

        if range_mode == "Single Day":
            selected_day = st.sidebar.date_input(
                "Choose Day",
                value=max_allowed_date,
                min_value=min_date,
                max_value=max_allowed_date
            )
            start_date = selected_day
            end_date = selected_day

        elif range_mode == "Last 7 Days":
            end_date = max_allowed_date
            start_date = max(min_date, end_date - pd.Timedelta(days=6))

        elif range_mode == "Last 30 Days":
            end_date = max_allowed_date
            start_date = max(min_date, end_date - pd.Timedelta(days=29))

        elif range_mode == "Month to Date":
            end_date = max_allowed_date
            start_date = max(min_date, end_date.replace(day=1))

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

        elif range_mode == "All Time":
            start_date = min_date
            end_date = max_allowed_date

        elif range_mode == "Custom":
            custom_range = st.sidebar.date_input(
                "Custom Range",
                value=(max(min_date, max_allowed_date - pd.Timedelta(days=6)), max_allowed_date),
                min_value=min_date,
                max_value=max_allowed_date
            )

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

            if start_date > end_date:
                start_date, end_date = end_date, start_date

        st.sidebar.caption(
            f"Showing: {start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}"
        )

    return start_date, end_date
