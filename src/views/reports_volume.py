import pandas as pd
import streamlit as st

from ui_components import (
    render_chart,
    build_hourly_bar_chart,
    build_category_bar_chart,
    build_hourly_line_chart,
    format_hour_plain,
)


def render_volume_capacity_section(df, df_live_raw, df_history_raw, today, gated_csv_download):
    # -----------------------------
    # Volume & Capacity
    # -----------------------------
    st.subheader("Volume & Capacity")
    st.caption("How much the AMH is processing, when demand peaks, and how current volume compares to normal patterns.")
    
    with st.expander("Weekday & Peak Analysis", expanded=False):
        st.caption("Shows volume trends by day of week and identifies peak operating times.")
    
        dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    
        if len(df) > 0:
            weekday_df = df.copy()
            weekday_df["date"] = weekday_df["datetime"].dt.date
            weekday_df["day_of_week"] = weekday_df["datetime"].dt.day_name()
    
            days_in_range = weekday_df["date"].nunique()
    
            dow_totals = (
                weekday_df.groupby("day_of_week")
                .size()
                .reindex(dow_order)
                .fillna(0)
                .reset_index(name="count")
            )
    
            daily_weekday = (
                weekday_df.groupby(["date", "day_of_week"])
                .size()
                .reset_index(name="daily_checkins")
            )
    
            dow_avg = (
                daily_weekday.groupby("day_of_week")["daily_checkins"]
                .mean()
                .reindex(dow_order)
                .fillna(0)
                .reset_index(name="avg_checkins")
            )
    
            dow_summary = dow_totals.merge(dow_avg, on="day_of_week", how="left")
    
            if len(dow_summary) > 0:
                busiest_day = dow_summary.loc[dow_summary["avg_checkins"].idxmax()]
    
                st.markdown(
                    f"""
                    <div style="
                        border-left: 4px solid #2563eb;
                        background-color: #f9fafb;
                        padding: 14px 16px;
                        border-radius: 8px;
                        margin-top: 8px;
                        margin-bottom: 16px;
                    ">
                        <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                            Report Summary
                        </div>
                        <div style="color: #4b5563; line-height: 1.4;">
                            Busiest average day: {busiest_day['day_of_week']} with
                            {busiest_day['avg_checkins']:,.1f} average checkins per day
                            across {days_in_range} day(s). Total volume for that weekday in the selected range:
                            {int(busiest_day['count']):,} checkins.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
    
                dow_chart = build_category_bar_chart(
                    dow_summary.rename(columns={"avg_checkins": "avg_items_per_day"}),
                    "day_of_week",
                    "avg_items_per_day",
                    "Avg Checkins Per Day",
                    "Day of Week"
                )
                render_chart(dow_chart)
    
                dow_display = dow_summary.rename(columns={
                    "day_of_week": "Day of Week",
                    "count": "Total Checkins",
                    "avg_checkins": "Avg Checkins Per Day"
                })[["Day of Week", "Total Checkins", "Avg Checkins Per Day"]]
    
                dow_display["Avg Checkins Per Day"] = dow_display["Avg Checkins Per Day"].round(1)
    
                st.dataframe(dow_display, use_container_width=True)
                gated_csv_download(dow_display, "weekday_volume.csv")
            else:
                st.info("No weekday data available for selected range.")
        else:
            st.info("No weekday data available for selected range.")
    
        st.subheader("Peak Hour Analysis")
    
        if len(df) > 0:
            peak_df = df.copy()
            peak_df["date"] = peak_df["datetime"].dt.date
            peak_df["hour"] = peak_df["datetime"].dt.hour
    
            days_in_range = peak_df["date"].nunique()
    
            hour_totals = (
                peak_df.groupby("hour")
                .size()
                .reset_index(name="count")
            )
    
            hour_daily = (
                peak_df.groupby(["date", "hour"])
                .size()
                .reset_index(name="hourly_checkins")
            )
    
            hour_avg = (
                hour_daily.groupby("hour")["hourly_checkins"]
                .mean()
                .reset_index(name="avg_checkins")
            )
    
            hour_summary = hour_totals.merge(hour_avg, on="hour", how="left")
            hour_summary["hour_label"] = hour_summary["hour"].apply(format_hour_plain)
            hour_summary = hour_summary[(hour_summary["hour"] >= 7) & (hour_summary["hour"] <= 20)].copy()
    
            if len(hour_summary) > 0:
                busiest_hour_row = hour_summary.loc[hour_summary["avg_checkins"].idxmax()]
            
                st.markdown(
                    f"""
                    <div style="
                        border-left: 4px solid #2563eb;
                        background-color: #f9fafb;
                        padding: 14px 16px;
                        border-radius: 8px;
                        margin-top: 8px;
                        margin-bottom: 16px;
                    ">
                        <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                            Report Summary
                        </div>
                        <div style="color: #4b5563; line-height: 1.4;">
                            Busiest average hour: {busiest_hour_row["hour_label"]} with
                            {busiest_hour_row["avg_checkins"]:,.1f} average checkins per day
                            across {days_in_range} day(s). Total volume during that hour in the selected range:
                            {int(busiest_hour_row["count"]):,} checkins.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            
                peak_hour_chart = build_hourly_bar_chart(
                    hour_summary.rename(columns={"avg_checkins": "avg_items_per_hour"}),
                    "avg_items_per_hour",
                    "Avg Checkins Per Hour"
                )
                render_chart(peak_hour_chart)
            
                peak_hour_display = hour_summary.rename(columns={
                    "hour_label": "Hour",
                    "count": "Total Checkins",
                    "avg_checkins": "Avg Checkins Per Day"
                })[["Hour", "Total Checkins", "Avg Checkins Per Day"]]
            
                peak_hour_display["Avg Checkins Per Day"] = peak_hour_display["Avg Checkins Per Day"].round(1)
            
                st.dataframe(peak_hour_display, use_container_width=True)
                gated_csv_download(
                    peak_hour_display,
                    "peak_hour_analysis.csv"
                )
            else:
                st.info("No hourly data available for selected range.")
        else:
            st.info("No hourly data available for selected range.")
    
    
    with st.expander("Throughput", expanded=False):
        st.caption("Shows average checkins per hour per day across the selected date range, so multi-day ranges do not overstate throughput.")
    
        if len(df) > 0:
            throughput_df = df.copy()
            throughput_df["date"] = throughput_df["datetime"].dt.date
            throughput_df["hour"] = throughput_df["datetime"].dt.hour
            throughput_df["day_of_week"] = throughput_df["datetime"].dt.day_name()
    
            # ===== BUILD HOURLY =====
            daily_hourly = (
                throughput_df.groupby(["date", "hour"])
                .size()
                .reset_index(name="checkins")
            )
    
            avg_hourly = (
                daily_hourly.groupby("hour")["checkins"]
                .mean()
                .reset_index(name="avg_items_per_hour")
            )
    
            avg_hourly["hour_label"] = avg_hourly["hour"].apply(format_hour_plain)
    
            avg_hourly_chart_df = avg_hourly[(avg_hourly["hour"] >= 7) & (avg_hourly["hour"] <= 20)].copy()
    
            if len(avg_hourly) > 0:
                busiest_hour_row = avg_hourly.loc[avg_hourly["avg_items_per_hour"].idxmax()]
                st.subheader("Average Checkins per Hour")
                st.markdown(
                    f"""
                    <div style="
                        border-left: 4px solid #2563eb;
                        background-color: #f9fafb;
                        padding: 14px 16px;
                        border-radius: 8px;
                        margin-top: 8px;
                        margin-bottom: 16px;
                    ">
                        <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                            Hourly Summary
                        </div>
                        <div style="color: #4b5563; line-height: 1.4;">
                            Busiest average hour: {busiest_hour_row["hour_label"]} at
                            {busiest_hour_row["avg_items_per_hour"]:,.1f} checkins per hour.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            
            throughput_chart = build_hourly_bar_chart(
                avg_hourly_chart_df,
                "avg_items_per_hour",
                "Avg Checkins Per Hour"
            )
            render_chart(throughput_chart)
            
            display_df = avg_hourly_chart_df.rename(columns={
                "hour_label": "Hour",
                "avg_items_per_hour": "Avg Checkins Per Hour"
            })[["Hour", "Avg Checkins Per Hour"]]
            
            display_df["Avg Checkins Per Hour"] = display_df["Avg Checkins Per Hour"].round(1)
            
            st.dataframe(display_df, use_container_width=True)
            gated_csv_download(display_df, "throughput_report.csv")
    
            # ===== WEEKDAY SECTION =====
            st.subheader("Average Checkins per Day by Weekday")
    
            weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    
            weekday_daily = (
                throughput_df.groupby(["date", "day_of_week"])
                .size()
                .reset_index(name="daily_checkins")
            )
    
            weekday_avg = (
                weekday_daily.groupby("day_of_week")["daily_checkins"]
                .mean()
                .reindex(weekday_order)
                .fillna(0)
                .reset_index(name="avg_checkins_per_day")
            )
    
            busiest_weekday_row = weekday_avg.loc[weekday_avg["avg_checkins_per_day"].idxmax()]
    
            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #2563eb;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Weekday Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Busiest average weekday: {busiest_weekday_row["day_of_week"]} at
                        {busiest_weekday_row["avg_checkins_per_day"]:,.1f} checkins per day.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
    
            weekday_chart = build_category_bar_chart(
                weekday_avg,
                "day_of_week",
                "avg_checkins_per_day",
                "Avg Checkins Per Day",
                "Day of Week"
            )
            render_chart(weekday_chart)
    
            weekday_display = weekday_avg.rename(columns={
                "day_of_week": "Day of Week",
                "avg_checkins_per_day": "Avg Checkins Per Day"
            })[["Day of Week", "Avg Checkins Per Day"]]
    
            weekday_display["Avg Checkins Per Day"] = weekday_display["Avg Checkins Per Day"].round(1)
    
            st.dataframe(weekday_display, use_container_width=True)
            gated_csv_download(weekday_display, "throughput_by_weekday_report.csv")
    
        else:
            st.info("No throughput data available for the selected date range.")
    
    with st.expander("Today vs Typical Hourly Pattern", expanded=False):
    
        if "datetime" in df_live_raw.columns:
            today_df_report = df_live_raw[df_live_raw["datetime"].dt.date == today].copy()
        else:
            today_df_report = pd.DataFrame()
    
        if "datetime" in df_history_raw.columns:
            historical_df_report = df_history_raw[df_history_raw["datetime"].dt.date < today].copy()
        else:
            historical_df_report = pd.DataFrame()
    
        if "datetime" in today_df_report.columns and len(today_df_report) > 0:
            today_hourly = today_df_report["datetime"].dt.hour.value_counts().sort_index()
        else:
            today_hourly = pd.Series(dtype=float)
    
        if "datetime" in historical_df_report.columns and len(historical_df_report) > 0 and historical_df_report["datetime"].dt.date.nunique() > 0:
            typical_hourly = (
                historical_df_report.groupby(historical_df_report["datetime"].dt.hour).size()
                / historical_df_report["datetime"].dt.date.nunique()
            )
        else:
            typical_hourly = pd.Series(dtype=float)
    
        all_hours = sorted(set(today_hourly.index).union(set(typical_hourly.index)))
    
        compare_df = pd.DataFrame({"hour": all_hours})
        compare_df["today"] = compare_df["hour"].map(today_hourly).fillna(0)
        compare_df["typical"] = compare_df["hour"].map(typical_hourly).fillna(0).round(1)
        compare_df["delta"] = compare_df["today"] - compare_df["typical"]
        compare_df["hour_label"] = compare_df["hour"].apply(format_hour_plain)
    
        if len(compare_df) > 0:
            max_delta_row = compare_df.loc[compare_df["delta"].idxmax()]
    
            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #059669;
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 16px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        Report Summary
                    </div>
                    <div style="color: #4b5563; line-height: 1.4;">
                        Biggest deviation at {max_delta_row["hour_label"]} —
                        {max_delta_row["delta"]:+.1f} items versus the typical hourly pattern.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
    
            compare_df = compare_df[(compare_df["hour"] >= 7) & (compare_df["hour"] <= 20)].copy()
    
            compare_long = compare_df.melt(
                id_vars=["hour", "hour_label"],
                value_vars=["today", "typical"],
                var_name="series",
                value_name="items"
            )
    
            compare_chart = build_hourly_line_chart(compare_long, "items", "Items", series_col="series")
            render_chart(compare_chart)
    
            display_df = compare_df[["hour_label", "today", "typical", "delta"]].rename(
                columns={"hour_label": "hour"}
            )
            st.dataframe(display_df, use_container_width=True)
            gated_csv_download(display_df, "today_vs_typical_hourly_pattern.csv")
        else:
            st.info("Not enough data available to compare today versus the typical hourly pattern.")
    
    
    
