import altair as alt
import pandas as pd
import streamlit as st

from ui_components import (
    build_hourly_bar_chart,
    format_hour_plain,
    render_chart,
    render_kpi_card,
)


def render_errors_exceptions_section(df, rejects_df, gated_csv_download):
    # -----------------------------
    # Errors & Exceptions
    # -----------------------------
    st.subheader("Errors & Exceptions")
    st.caption("Tracks failure types, exception routing, and patterns that may indicate operational issues.")
    
    with st.expander("Reject Reasons", expanded=False):
        reject_counts = rejects_df["error_simple"].value_counts().reset_index()
        reject_counts.columns = ["reason", "count"]
    
        if len(reject_counts) > 0:
            top_reason_row = reject_counts.loc[reject_counts["count"].idxmax()]
            top_reason_pct = (top_reason_row["count"] / reject_counts["count"].sum()) * 100
    
            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #dc2626;
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
                        Top reject reason: {top_reason_row["reason"]} with {int(top_reason_row["count"]):,} rejects
                        ({top_reason_pct:.1f}% of all failures).
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
    
            reject_chart = (
                alt.Chart(reject_counts)
                .mark_bar()
                .encode(
                    x=alt.X(
                        "reason:N",
                        sort=reject_counts["reason"].tolist(),
                        title="Reject Reason",
                        axis=alt.Axis(labelAngle=0)
                    ),
                    y=alt.Y("count:Q", title="Count"),
                    tooltip=["reason", "count"]
                )
                .properties(height=350)
            )
    
            render_chart(reject_chart)
    
            st.dataframe(reject_counts, use_container_width=True)
            gated_csv_download(reject_counts, "reject_reasons.csv")
        else:
            st.info("No reject reason data available for the selected date range.")
    
    with st.expander("Top Issues", expanded=False):
        
    
        st.markdown("### Top Issues Report")
    
        if len(rejects_df) > 0:
            top_issues_df = (
                rejects_df["error_simple"]
                .value_counts()
                .reset_index()
            )
            top_issues_df.columns = ["Issue", "Reject Count"]
            top_issues_df["% of Failures"] = (
                top_issues_df["Reject Count"] / top_issues_df["Reject Count"].sum() * 100
            ).round(1)
    
            issue_explanations = {
                "Item Not Found": "Barcode not recognized by ILS / missing item record",
                "ILS / ACS Failure": "Communication issue between AMH and ILS/ACS",
                "RFID Collision": "Multiple tags detected in bin",
                "Call Number / Config Error": "Item routing configuration mismatch",
                "Routing Error": "Destination not resolved correctly",
                "Other": "Uncategorized system failure"
            }
    
            top_issues_df["Explanation"] = top_issues_df["Issue"].map(issue_explanations).fillna("Operational issue requiring review")
    
            st.dataframe(top_issues_df, use_container_width=True)
        else:
            st.info("No reject issues found for the selected date range.")
    
    with st.expander("Exceptions / Overflow", expanded=False):
        if "bin" not in df.columns:
            st.warning("No bin column found in the current dataset. Add bin parsing to your cleaned checkins file first.")
        else:
            EXCEPTION_BIN = "0"
    
            bin_df = df.copy()
            bin_df = bin_df[bin_df["bin"].notna()].copy()
            bin_df["bin"] = bin_df["bin"].astype(str)
    
            exception_df = bin_df[bin_df["bin"] == EXCEPTION_BIN].copy()
            total_binned = len(bin_df)
            exception_count = len(exception_df)
            exception_pct = (exception_count / total_binned * 100) if total_binned > 0 else 0
            
            library_express_count = int(
                (df["destination_clean"] == "Library Express").sum()
            )
    
            estimated_holds = max(
                exception_count - len(rejects_df) - library_express_count,
                0
            )
    
            estimated_holds_pct = (estimated_holds / exception_count * 100) if exception_count > 0 else 0
    
            daily_exception = exception_df["datetime"].dt.date.value_counts().sort_index()
            daily_total = bin_df["datetime"].dt.date.value_counts().sort_index()
    
            overflow_daily = pd.DataFrame({
                "total_binned": daily_total,
                "exception_bin_items": daily_exception
            }).fillna(0)
    
            overflow_daily["exception_rate_pct"] = (
                overflow_daily["exception_bin_items"] / overflow_daily["total_binned"] * 100
            ).round(2)
    
            peak_exception_day_label = "N/A"
            peak_exception_rate = 0
            if len(overflow_daily) > 0:
                peak_exception_day = overflow_daily["exception_rate_pct"].idxmax()
                peak_exception_day_label = pd.to_datetime(peak_exception_day).strftime("%a, %b %d")
                peak_exception_rate = overflow_daily["exception_rate_pct"].max()
    
            hourly_exception = exception_df["datetime"].dt.hour.value_counts().sort_index()
            hourly_exception_df = hourly_exception.reset_index()
            hourly_exception_df.columns = ["hour", "exception_items"]
            if len(hourly_exception_df) > 0:
                hourly_exception_df["hour_label"] = hourly_exception_df["hour"].apply(format_hour_plain)
                peak_exception_hour_row = hourly_exception_df.loc[hourly_exception_df["exception_items"].idxmax()]
                peak_exception_hour_text = peak_exception_hour_row["hour_label"]
                peak_exception_hour_count = int(peak_exception_hour_row["exception_items"])
            else:
                peak_exception_hour_text = "N/A"
                peak_exception_hour_count = 0
    
            insight_text = (
                f"Exception bin {EXCEPTION_BIN} handled {exception_count:,} items "
                f"({exception_pct:.2f}% of all binned checkins). "
                f"Peak exception day: {peak_exception_day_label} at {peak_exception_rate:.2f}%. "
                f"Peak exception hour: {peak_exception_hour_text} with {peak_exception_hour_count:,} items."
            )
    
            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid #d97706;
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
                        {insight_text}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
    
            k1, k2, k3, k4 = st.columns(4)
            with k1:
                render_kpi_card(
                    "Exception Bin",
                    f"Bin {EXCEPTION_BIN}",
                    "Assumed overflow / exception lane",
                    "#6b7280",
                    value_font_size="1.4rem"
                )
            with k2:
                render_kpi_card(
                    "Exception Items",
                    f"{exception_count:,}",
                    "Routed to exception bin",
                    "#6b7280"
                )
            with k3:
                render_kpi_card(
                    "Exception Share",
                    f"{exception_pct:.2f}%",
                    "Of all binned checkins",
                    "#6b7280",
                    value_font_size="1.4rem"
                )
            with k4:
                render_kpi_card(
                    "Estimated Holds",
                    f"{estimated_holds:,}",
                    f"{estimated_holds_pct:.1f}% of Bin 0",
                    "#6b7280"
                )
    
            
            if len(overflow_daily) > 0:
                st.subheader("Exception Bin Rate by Day")
                chart_df = overflow_daily["exception_rate_pct"]
                st.line_chart(chart_df)
    
                overflow_daily_display = overflow_daily.reset_index().rename(columns={"index": "date"})
                st.dataframe(overflow_daily_display, use_container_width=True)
                gated_csv_download(
                    overflow_daily_display,
                    "exception_bin_rate_by_day_report.csv",
                    key="exception_bin_rate_by_day_report_download"
                )
            if len(exception_df) > 0:
                st.subheader("Exception Bin Volume by Hour")
    
                exception_hourly_source = exception_df.copy()
                exception_hourly_source["date"] = exception_hourly_source["datetime"].dt.date
                exception_hourly_source["hour"] = exception_hourly_source["datetime"].dt.hour
    
                days_in_range = exception_hourly_source["date"].nunique()
    
                exception_hour_totals = (
                    exception_hourly_source.groupby("hour")
                    .size()
                    .reset_index(name="exception_items")
                )
    
                exception_hour_daily = (
                    exception_hourly_source.groupby(["date", "hour"])
                    .size()
                    .reset_index(name="daily_exception_items")
                )
    
                exception_hour_avg = (
                    exception_hour_daily.groupby("hour")["daily_exception_items"]
                    .mean()
                    .reset_index(name="avg_exception_items")
                )
    
                hourly_exception_summary = exception_hour_totals.merge(
                    exception_hour_avg,
                    on="hour",
                    how="left"
                )
    
                hourly_exception_summary["hour_label"] = hourly_exception_summary["hour"].apply(format_hour_plain)
                hourly_exception_summary = hourly_exception_summary[
                    (hourly_exception_summary["hour"] >= 7) & (hourly_exception_summary["hour"] <= 20)
                ].copy()
    
                if len(hourly_exception_summary) > 0:
                    peak_exception_hour_row = hourly_exception_summary.loc[
                        hourly_exception_summary["avg_exception_items"].idxmax()
                    ]
    
                    st.markdown(
                        f"""
                        <div style="
                            border-left: 4px solid #d97706;
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
                                Highest average exception-bin hour: {peak_exception_hour_row["hour_label"]} with
                                {peak_exception_hour_row["avg_exception_items"]:,.1f} average items per day
                                across {days_in_range} day(s). Total exception-bin volume during that hour in the
                                selected range: {int(peak_exception_hour_row["exception_items"]):,} items.
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
    
                    exception_chart = build_hourly_bar_chart(
                        hourly_exception_summary.rename(columns={"avg_exception_items": "avg_items_per_hour"}),
                        "avg_items_per_hour",
                        "Avg Exception Items Per Hour"
                    )
                    render_chart(exception_chart)
    
                    hourly_exception_display = hourly_exception_summary.rename(columns={
                        "hour_label": "Hour",
                        "exception_items": "Total Exception Items",
                        "avg_exception_items": "Avg Exception Items Per Day"
                    })[["Hour", "Total Exception Items", "Avg Exception Items Per Day"]]
    
                    hourly_exception_display["Avg Exception Items Per Day"] = (
                        hourly_exception_display["Avg Exception Items Per Day"].round(1)
                    )
    
                    st.dataframe(hourly_exception_display, use_container_width=True)
                    gated_csv_download(
                        hourly_exception_display,
                        "exception_bin_volume_by_hour_report.csv",
                        key="exception_bin_volume_by_hour_report_download"
                    )
                else:
                    st.info("No exception-bin items found for the selected date range.")
            else:
                st.info("No exception-bin items found for the selected date range.")
