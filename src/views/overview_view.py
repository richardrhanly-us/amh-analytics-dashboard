import streamlit as st
import pandas as pd

from metrics import build_acs_item_summary, build_roi_payload
from ui_components import (
    render_kpi_card,
    format_hour,
    format_ill_branch_subtitle,
)


def render_overview(
    df,
    rejects_df,
    acs_history_raw,
    start_date,
    end_date,
    date_range_text,
    TRANSIT_LABELS,
    TRANSIT_HOME_LABEL,
    BRANCH_SERVICES_NAMES,
    COLLECTION_SERVICES_NAMES,
    BRANCH_SERVICES_DA_PATTERNS,
    COLLECTION_SERVICES_DA_PATTERNS,
    attention_title,
    attention_text,
    attention_color,
    overview_transit_counts_map,
    overview_transit_pct_map,
):
    st.subheader("Summary")
    st.caption("Get a historical summary by choosing a date range.")

    # ---------------------------------
    # Internal Workflow Summary
    # ---------------------------------
    overview_acs_df = acs_history_raw.copy()

    if len(overview_acs_df) > 0 and "datetime" in overview_acs_df.columns:
        overview_acs_df["datetime"] = pd.to_datetime(overview_acs_df["datetime"], errors="coerce")
        overview_acs_df = overview_acs_df.dropna(subset=["datetime"]).copy()
        overview_acs_df = overview_acs_df[
            (overview_acs_df["datetime"].dt.date >= start_date)
            & (overview_acs_df["datetime"].dt.date <= end_date)
        ].copy()

    overview_acs_summary = build_acs_item_summary(
        overview_acs_df,
        transit_labels=TRANSIT_LABELS,
        branch_services_names=BRANCH_SERVICES_NAMES,
        collection_services_names=COLLECTION_SERVICES_NAMES,
        branch_services_da_patterns=BRANCH_SERVICES_DA_PATTERNS,
        collection_services_da_patterns=COLLECTION_SERVICES_DA_PATTERNS,
    )

    overview_holds = overview_acs_summary["holds_total"]
    overview_ill = overview_acs_summary["ill_total"]
    overview_ill_main = overview_acs_summary["ill_main"]
    overview_ill_by_branch = overview_acs_summary["ill_by_branch"]

    st.markdown("### Internal Workflow")
    internal_overview_col1, internal_overview_col2, internal_overview_col3, internal_overview_col4 = st.columns(4)

    with internal_overview_col1:
        render_kpi_card(
            "Holds",
            f"{overview_holds:,}",
            f"{date_range_text}",
            "#6b7280",
            value_font_size="2.0rem",
            border_color="#34d399",
        )

    with internal_overview_col2:
        render_kpi_card(
            "ILL",
            f"{overview_ill:,}",
            format_ill_branch_subtitle(
                overview_ill_main,
                overview_ill_by_branch,
                TRANSIT_HOME_LABEL,
                TRANSIT_LABELS,
            ),
            "#6b7280",
            value_font_size="2.0rem",
            border_color="#34d399",
        )

    with internal_overview_col3:
        render_kpi_card(
            "Programming",
            f"{overview_acs_summary['programming_total']:,}",
            f"{date_range_text}",
            "#6b7280",
            value_font_size="2.0rem",
            border_color="#34d399",
        )

    with internal_overview_col4:
        render_kpi_card(
            "Collection Services",
            f"{overview_acs_summary['collection_services_total']:,}",
            f"{date_range_text}",
            "#6b7280",
            value_font_size="1.9rem",
            border_color="#34d399",
        )

    with st.expander("ILL Debug (Overview)", expanded=False):
        ill_items_df = overview_acs_summary["items_df"]

        if len(ill_items_df) > 0:
            ill_debug_df = ill_items_df[
                (ill_items_df["is_hold"]) & (ill_items_df["is_ill"])
            ].copy()

            st.write("ILL item count:", len(ill_debug_df))

            st.dataframe(
                ill_debug_df.sort_values("datetime", ascending=False),
                use_container_width=True,
            )
        else:
            st.info("No ACS items available for this range.")

    if len(rejects_df) > 0:
        top_issue = rejects_df["error_simple"].value_counts().idxmax()
        top_issue_count = rejects_df["error_simple"].value_counts().max()
        top_issue_pct = (top_issue_count / len(rejects_df)) * 100

        issue_explanations = {
            "Item Not Found": "Barcode not recognized by ILS / missing item record",
            "ILS / ACS Failure": "Communication issue between AMH and ILS/ACS",
            "RFID Collision": "Multiple tags detected in bin",
            "Call Number / Config Error": "Item routing configuration mismatch",
            "Routing Error": "Destination not resolved correctly",
            "Other": "Uncategorized system failure",
        }

        issue_detail = issue_explanations.get(top_issue, "Operational issue requiring review")

        top_issue_subtitle = (
            f"<span style='color:#059669'>{top_issue_count:,} rejects ({top_issue_pct:.1f}% of failures)</span><br>"
            f"<span style='color:#6b7280'>{issue_detail}</span>"
        )
    else:
        top_issue = "N/A"
        top_issue_subtitle = "No rejects in selected range"

    st.markdown(
        f"""
        <div style="
            border-left: 4px solid {attention_color};
            background-color: #f9fafb;
            padding: 14px 16px;
            border-radius: 8px;
            margin-top: 8px;
            margin-bottom: 18px;
        ">
            <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                {attention_title}
            </div>
            <div style="color: #4b5563; line-height: 1.5;">
                {attention_text}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    daily_counts = df["datetime"].dt.date.value_counts().sort_index()
    avg_daily_checkins = daily_counts.mean() if len(daily_counts) > 0 else 0

    overview_volume_mode = st.radio(
        "Volume display",
        ["Average per Day", "Total"],
        horizontal=True,
        key="overview_volume_mode",
    )

    days_in_range = df["datetime"].dt.date.nunique() if len(df) > 0 else 0
    reject_count = len(rejects_df)
    reject_pct = (reject_count / len(df) * 100) if len(df) > 0 else 0
    avg_daily_rejects = (reject_count / days_in_range) if days_in_range > 0 else 0

    peak_avg_hour_value = "N/A"
    peak_avg_hour_subtitle = "No activity in selected range"
    peak_total_hour_value = "N/A"
    peak_total_hour_subtitle = "No activity in selected range"

    if len(df) > 0:
        hourly_source = df.copy()
        hourly_source["date_only"] = hourly_source["datetime"].dt.date
        hourly_source["hour_only"] = hourly_source["datetime"].dt.hour

        hourly_daily = (
            hourly_source.groupby(["date_only", "hour_only"])
            .size()
            .reset_index(name="checkins")
        )

        hourly_avg = (
            hourly_daily.groupby("hour_only")["checkins"]
            .mean()
            .reset_index(name="avg_checkins")
        )

        if len(hourly_avg) > 0:
            peak_avg_row = hourly_avg.loc[hourly_avg["avg_checkins"].idxmax()]
            peak_avg_hour_value = format_hour(int(peak_avg_row["hour_only"]))
            peak_avg_hour_subtitle = f"{peak_avg_row['avg_checkins']:,.1f} avg checkins/day"

        hourly_total = (
            df["datetime"].dt.hour.value_counts()
            .sort_index()
            .reset_index()
        )
        hourly_total.columns = ["hour_only", "total_checkins"]

        if len(hourly_total) > 0:
            peak_total_row = hourly_total.loc[hourly_total["total_checkins"].idxmax()]
            peak_total_hour_value = format_hour(int(peak_total_row["hour_only"]))
            peak_total_hour_subtitle = f"{int(peak_total_row['total_checkins']):,} total checkins"

    fail_peak_avg_value = "N/A"
    fail_peak_avg_subtitle = "No failures in selected range"
    fail_peak_total_value = "N/A"
    fail_peak_total_subtitle = "No failures in selected range"

    if len(rejects_df) > 0:
        reject_hour_source = rejects_df.copy()
        reject_hour_source["date_only"] = reject_hour_source["datetime"].dt.date
        reject_hour_source["hour_only"] = reject_hour_source["datetime"].dt.hour

        fail_hour_daily = (
            reject_hour_source.groupby(["date_only", "hour_only"])
            .size()
            .reset_index(name="failures")
        )

        fail_hour_avg = (
            fail_hour_daily.groupby("hour_only")["failures"]
            .mean()
            .reset_index(name="avg_failures")
        )

        if len(fail_hour_avg) > 0:
            peak_avg_fail_row = fail_hour_avg.loc[fail_hour_avg["avg_failures"].idxmax()]
            fail_peak_avg_value = format_hour(int(peak_avg_fail_row["hour_only"]))
            fail_peak_avg_subtitle = f"{peak_avg_fail_row['avg_failures']:,.1f} avg failures/day"

        fail_hour_total = (
            rejects_df["datetime"].dt.hour.value_counts()
            .sort_index()
            .reset_index()
        )
        fail_hour_total.columns = ["hour_only", "total_failures"]

        if len(fail_hour_total) > 0:
            peak_total_fail_row = fail_hour_total.loc[fail_hour_total["total_failures"].idxmax()]
            fail_peak_total_value = format_hour(int(peak_total_fail_row["hour_only"]))
            fail_peak_total_subtitle = f"{int(peak_total_fail_row['total_failures']):,} total rejects"

    peak_day_avg_value = "N/A"
    peak_day_avg_subtitle = "No activity in selected range"
    peak_day_total_value = "N/A"
    peak_day_total_subtitle = "No activity in selected range"

    if len(df) > 0:
        daily_volume = df["datetime"].dt.date.value_counts().sort_index()

        if len(daily_volume) > 0:
            peak_day_date = daily_volume.idxmax()
            peak_day_count = int(daily_volume.max())
            peak_day_name = pd.to_datetime(peak_day_date).strftime("%A")

            peak_day_total_value = peak_day_name
            peak_day_total_subtitle = (
                f"{peak_day_count:,} checkins on "
                f"{pd.to_datetime(peak_day_date).strftime('%b %d, %Y')}"
            )

        weekday_source = df.copy()
        weekday_source["date_only"] = weekday_source["datetime"].dt.date
        weekday_source["day_of_week"] = weekday_source["datetime"].dt.day_name()

        weekday_daily = (
            weekday_source.groupby(["date_only", "day_of_week"])
            .size()
            .reset_index(name="daily_checkins")
        )

        weekday_avg = (
            weekday_daily.groupby("day_of_week")["daily_checkins"]
            .mean()
            .reindex([
                "Monday", "Tuesday", "Wednesday",
                "Thursday", "Friday", "Saturday", "Sunday",
            ])
            .fillna(0)
        )

        if len(weekday_avg) > 0:
            peak_day_avg_name = weekday_avg.idxmax()
            peak_day_avg_value = peak_day_avg_name
            peak_day_avg_subtitle = f"{weekday_avg.max():,.1f} avg checkins/day"

    overview_avg_hours_saved = 0.0
    overview_total_hours_saved = 0.0
    overview_avg_labor_value = 0.0
    overview_total_labor_value = 0.0

    MANUAL_RATE_OVERVIEW = 45

    overview_roi_payload = build_roi_payload(
        df,
        start_date,
        end_date,
        hourly_cost=18.0,
        upfront_cost=200000.0,
        monthly_cost=0.0,
        yearly_cost=8000.0,
    )

    HOURLY_COST_OVERVIEW = (
        overview_roi_payload["hourly_cost"]
        if overview_roi_payload and "hourly_cost" in overview_roi_payload
        else 18.0
    )

    if len(df) > 0:
        labor_df = df.copy()
        labor_df["date"] = labor_df["datetime"].dt.date
        labor_df["hour"] = labor_df["datetime"].dt.hour

        labor_daily_hourly = (
            labor_df.groupby(["date", "hour"])
            .size()
            .reset_index(name="checkins")
        )

        labor_avg_hourly = (
            labor_daily_hourly.groupby("hour")["checkins"]
            .mean()
            .reset_index(name="avg_items_per_hour")
        )

        if len(labor_avg_hourly) > 0:
            labor_peak_row = labor_avg_hourly.loc[labor_avg_hourly["avg_items_per_hour"].idxmax()]
            labor_threshold = labor_peak_row["avg_items_per_hour"] * 0.75
            labor_peak_hours = labor_avg_hourly[
                labor_avg_hourly["avg_items_per_hour"] >= labor_threshold
            ].copy()

            amh_rate_overview = (
                labor_peak_hours["avg_items_per_hour"].mean()
                if len(labor_peak_hours) > 0
                else labor_peak_row["avg_items_per_hour"]
            )
        else:
            amh_rate_overview = 130.0

        labor_daily_counts = df["datetime"].dt.date.value_counts().sort_index()
        labor_staff_df = labor_daily_counts.reset_index()
        labor_staff_df.columns = ["date", "checkins"]

        labor_staff_df["manual_hours"] = labor_staff_df["checkins"] / MANUAL_RATE_OVERVIEW
        labor_staff_df["amh_hours"] = labor_staff_df["checkins"] / amh_rate_overview
        labor_staff_df["hours_saved"] = (
            labor_staff_df["manual_hours"] - labor_staff_df["amh_hours"]
        ).clip(lower=0)

        overview_avg_hours_saved = labor_staff_df["hours_saved"].mean() if len(labor_staff_df) > 0 else 0.0
        overview_total_hours_saved = labor_staff_df["hours_saved"].sum() if len(labor_staff_df) > 0 else 0.0

        overview_avg_labor_value = overview_avg_hours_saved * HOURLY_COST_OVERVIEW
        overview_total_labor_value = overview_total_hours_saved * HOURLY_COST_OVERVIEW

    row1_col1, row1_col2, row1_col3 = st.columns(3)
    row2_col1, row2_col2, row2_col3 = st.columns(3)
    row3_col1, row3_col2, row3_col3 = st.columns(3)
    row4_col1, row4_col2, row4_col3 = st.columns(3)

    with row1_col1:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Daily Checkins",
                f"{avg_daily_checkins:,.1f}",
                f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d')}",
                "#6b7280",
            )
        else:
            render_kpi_card(
                "Total Checkins",
                f"{len(df):,}",
                f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d')}",
                "#6b7280",
            )

    enabled_overview_labels = TRANSIT_LABELS[:2]

    if len(enabled_overview_labels) > 0:
        transit_label_1 = enabled_overview_labels[0]
        avg_daily_transit_1 = (overview_transit_counts_map.get(transit_label_1, 0) / days_in_range) if days_in_range > 0 else 0

        with row1_col2:
            if overview_volume_mode == "Average per Day":
                render_kpi_card(
                    f"Avg {transit_label_1} Transits",
                    f"{avg_daily_transit_1:,.1f}",
                    "Per day",
                    "#6b7280",
                )
            else:
                render_kpi_card(
                    f"Total {transit_label_1} Transits",
                    f"{overview_transit_counts_map.get(transit_label_1, 0):,}",
                    f"{overview_transit_pct_map.get(transit_label_1, 0):.2f}% of total items",
                    "#6b7280",
                )
    else:
        with row1_col2:
            render_kpi_card(
                "Transit Destination 1",
                "N/A",
                "No enabled transit destinations",
                "#6b7280",
            )

    if len(enabled_overview_labels) > 1:
        transit_label_2 = enabled_overview_labels[1]
        avg_daily_transit_2 = (overview_transit_counts_map.get(transit_label_2, 0) / days_in_range) if days_in_range > 0 else 0

        with row1_col3:
            if overview_volume_mode == "Average per Day":
                render_kpi_card(
                    f"Avg {transit_label_2} Transits",
                    f"{avg_daily_transit_2:,.1f}",
                    "Per day",
                    "#6b7280",
                )
            else:
                render_kpi_card(
                    f"Total {transit_label_2} Transits",
                    f"{overview_transit_counts_map.get(transit_label_2, 0):,}",
                    f"{overview_transit_pct_map.get(transit_label_2, 0):.2f}% of total items",
                    "#6b7280",
                )
    else:
        with row1_col3:
            render_kpi_card(
                "Transit Destination 2",
                "N/A",
                "No second enabled transit destination",
                "#6b7280",
            )

    with row2_col1:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Daily Rejects",
                f"{avg_daily_rejects:,.1f}",
                "Per day",
                "#6b7280",
            )
        else:
            render_kpi_card(
                "Reject Count",
                f"{reject_count:,}",
                "Total failed checkins",
                "#6b7280",
            )

    with row2_col2:
        render_kpi_card(
            "Reject %",
            f"{reject_pct:.2f}%",
            date_range_text,
            "#6b7280",
            value_font_size="1.55rem",
        )

    with row2_col3:
        render_kpi_card(
            "Top Issue",
            top_issue,
            top_issue_subtitle,
            "#059669",
            value_font_size="1.15rem",
            value_wrap=True,
        )

    with row3_col1:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Peak Avg Hour",
                peak_avg_hour_value,
                peak_avg_hour_subtitle,
                "#6b7280",
            )
        else:
            render_kpi_card(
                "Peak Total Hour",
                peak_total_hour_value,
                peak_total_hour_subtitle,
                "#6b7280",
            )

    with row3_col2:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Fail Peak Hr",
                fail_peak_avg_value,
                fail_peak_avg_subtitle,
                "#6b7280",
            )
        else:
            render_kpi_card(
                "Peak Failure Hour",
                fail_peak_total_value,
                fail_peak_total_subtitle,
                "#6b7280",
            )

    with row3_col3:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Peak Day of Week",
                peak_day_avg_value,
                peak_day_avg_subtitle,
                "#6b7280",
                value_font_size="1.4rem",
                value_wrap=True,
            )
        else:
            render_kpi_card(
                "Peak Day",
                peak_day_total_value,
                peak_day_total_subtitle,
                "#6b7280",
                value_font_size="1.4rem",
                value_wrap=True,
            )

    with row4_col1:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Hours Saved",
                f"{overview_avg_hours_saved:,.2f}",
                "Per day",
                "#6b7280",
            )
        else:
            render_kpi_card(
                "Total Hours Saved",
                f"{overview_total_hours_saved:,.2f}",
                "Across selected date range",
                "#6b7280",
            )

    with row4_col2:
        if overview_volume_mode == "Average per Day":
            render_kpi_card(
                "Avg Labor Value",
                f"${overview_avg_labor_value:,.0f}",
                "Per day",
                "#6b7280",
            )
        else:
            render_kpi_card(
                "Total Labor Value",
                f"${overview_total_labor_value:,.0f}",
                "Across selected date range",
                "#6b7280",
            )

    with row4_col3:
        if st.session_state.get("roi_calculated", False):
            overview_roi_payload = build_roi_payload(
                df,
                start_date,
                end_date,
                hourly_cost=st.session_state.get("roi_hourly_cost", 18.0),
                upfront_cost=st.session_state.get("roi_upfront_cost", 200000.0),
                monthly_cost=st.session_state.get("roi_monthly_cost", 0.0),
                yearly_cost=st.session_state.get("roi_yearly_cost", 8000.0),
            )

            if overview_roi_payload:
                if overview_roi_payload["roi_mode"] == "Annualized Projection":
                    render_kpi_card(
                        "Yearly Savings After Cost",
                        f'${overview_roi_payload["net_roi_value"]:,.0f}',
                        "Projected yearly savings after recurring cost",
                        "#6b7280",
                        value_color="#059669" if overview_roi_payload["net_roi_value"] >= 0 else "#dc2626",
                    )
                else:
                    render_kpi_card(
                        "Observed Net Value",
                        f'${overview_roi_payload["observed_net_operating_value"]:,.0f}',
                        "Selected range value minus recurring cost",
                        "#6b7280",
                        value_color="#059669" if overview_roi_payload["observed_net_operating_value"] >= 0 else "#dc2626",
                    )
            else:
                render_kpi_card(
                    "ROI",
                    "N/A",
                    "No ROI data for current date range",
                    "#6b7280",
                )
        else:
            render_kpi_card(
                "ROI",
                "Not Calculated",
                "Go to Reports and click Calculate ROI",
                "#6b7280",
            )
