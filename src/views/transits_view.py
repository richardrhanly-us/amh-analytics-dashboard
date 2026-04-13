import pandas as pd
import streamlit as st

from transit_logic import (
    normalize_transit_destination,
    get_transit_summary,
    get_transit_time_summary,
    get_peak_transit_day_summary,
    get_transit_weekday_comparison,
    get_destination_weekday_mix,
    get_transit_reject_insight,
    get_destination_reject_summary,
    get_destination_driver_summary,
)

from ui_components import (
    render_kpi_card,
    download_button,
    render_chart,
    build_hourly_bar_chart,
    build_category_bar_chart,
    build_date_line_chart,
    format_hour_plain,
)

def render_transits(
    df,
    rejects_df,
    today_df,
    today_rejects_df,
    df_history_raw,
    today,
    start_date,
    end_date,
    can_view_transits=True,
):
    if not can_view_transits:
        st.info("Transit analytics are not available on this plan.")
        return
    st.header("Transit Routing")
    st.caption("Tracks items routed to transit destinations such as Westside and Library Express.")

    transit_mode = st.radio(
        "Transit view",
        ["Selected Range", "Today"],
        horizontal=True,
        key="transit_view_mode"
    )

    if transit_mode == "Today":
        base_df = today_df.copy()
        base_rejects_df = today_rejects_df.copy()
        date_label = today.strftime("%b %d, %Y")
    else:
        base_df = df.copy()
        base_rejects_df = rejects_df.copy()
        date_label = f"{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}"
        
        base_df["destination_clean"] = base_df["destination"].astype(str).str.strip()
        base_df["transit_destination"] = base_df["destination"].apply(normalize_transit_destination)
        
        base_df["destination_report"] = base_df["destination_clean"].copy()
        base_df.loc[base_df["destination_report"] == "1", "destination_report"] = "Main"
        base_df.loc[base_df["transit_destination"] == "Westside", "destination_report"] = "Westside"
        base_df.loc[base_df["transit_destination"] == "Library Express", "destination_report"] = "Library Express"
        
        base_df["destination_clean"] = base_df["destination_report"]

    st.caption(f"Showing: {date_label}")

    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    
    valid_transit_destinations = [
        "Westside",
        "Library Express",
    ]
    
    if len(base_df) > 0 and "datetime" in base_df.columns:
        base_df = base_df.copy()
        base_df["date"] = base_df["datetime"].dt.date
        base_df["day_of_week"] = base_df["datetime"].dt.day_name()
        base_df["destination_clean"] = base_df["destination"].astype(str).str.strip()
        base_df["transit_destination"] = base_df["destination"].apply(normalize_transit_destination)
    
        base_df["destination_report"] = base_df["destination_clean"].copy()
        base_df.loc[base_df["destination_report"] == "1", "destination_report"] = "Main"
        base_df.loc[base_df["transit_destination"] == "Westside", "destination_report"] = "Westside"
        base_df.loc[base_df["transit_destination"] == "Library Express", "destination_report"] = "Library Express"
    
        base_df["destination_clean"] = base_df["destination_report"]
    else:
        base_df = pd.DataFrame({
            "datetime": pd.Series(dtype="datetime64[ns]"),
            "date": pd.Series(dtype="object"),
            "day_of_week": pd.Series(dtype="object"),
            "destination_clean": pd.Series(dtype="object"),
            "transit_destination": pd.Series(dtype="object"),
            "destination_report": pd.Series(dtype="object"),
        })


    if len(base_rejects_df) > 0 and "datetime" in base_rejects_df.columns:
        base_rejects_df = base_rejects_df.copy()
        base_rejects_df["date"] = base_rejects_df["datetime"].dt.date
        base_rejects_df["day_of_week"] = base_rejects_df["datetime"].dt.day_name()
    else:
        base_rejects_df = pd.DataFrame({
            "datetime": pd.Series(dtype="datetime64[ns]"),
            "date": pd.Series(dtype="object"),
            "day_of_week": pd.Series(dtype="object"),
        })
        
    transit_df = base_df[
        base_df["transit_destination"].isin(valid_transit_destinations)
    ].copy()
    
    transit_summary = get_transit_summary(base_df)
    transit_time_summary = get_transit_time_summary(transit_df)

    total_transit_items = len(transit_df)
    total_transit_pct = (total_transit_items / len(base_df) * 100) if len(base_df) > 0 else 0
    transit_destination_count = transit_summary["destination"].nunique() if len(transit_summary) > 0 else 0

    top_transit_destination = "N/A"
    top_transit_subtitle = ""

    if len(transit_summary) > 0:
        top_row = transit_summary.iloc[0]
        top_transit_destination = top_row["destination"]
        top_transit_subtitle = (
            f"{int(top_row['transit_items']):,} items "
            f"({float(top_row['pct_of_total_items']):.2f}% of total)"
        )

    westside_count = len(
        base_df[base_df["transit_destination"] == "Westside"]
    )
    westside_pct = (westside_count / len(base_df) * 100) if len(base_df) > 0 else 0

    library_express_count = len(
        base_df[base_df["transit_destination"] == "Library Express"]
    )
    library_express_pct = (library_express_count / len(base_df) * 100) if len(base_df) > 0 else 0

    no_agency_dest_count = int(
        base_df["destination_clean"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False).sum()
    )

    peak_transit_day = get_peak_transit_day_summary(transit_df, weekday_order)
    peak_transit_day_label = peak_transit_day["peak_transit_day_label"]
    peak_transit_day_subtitle = peak_transit_day["peak_transit_day_subtitle"]

    transit_weekday_comparison = get_transit_weekday_comparison(base_df, base_rejects_df, weekday_order)
    destination_weekday_mix = get_destination_weekday_mix(transit_df, weekday_order)

    transit_insight = get_transit_reject_insight(transit_weekday_comparison)
    transit_reject_insight_title = transit_insight["title"]
    transit_reject_insight_text = transit_insight["text"]
    transit_reject_insight_color = transit_insight["color"]

    destination_reject_summary = get_destination_reject_summary(
        base_df,
        base_rejects_df,
        transit_summary,
        valid_transit_destinations
    )

    destination_driver_summary = get_destination_driver_summary(destination_reject_summary)
    destination_transit_summary_text = destination_driver_summary["text"]
    destination_transit_summary_color = destination_driver_summary["color"]

    transit1, transit2, transit3, transit4 = st.columns(4)
    
    with transit1:
        render_kpi_card(
            "Total Transit Items",
            f"{total_transit_items:,}",
            f"{total_transit_pct:.2f}% of all checkins",
            "#6b7280"
        )
    
    with transit2:
        render_kpi_card(
            "Transit to Westside",
            f"{westside_count:,}",
            f"{westside_pct:.2f}% of all checkins",
            "#6b7280"
        )
    
    with transit3:
        render_kpi_card(
            "Transit to Library Express",
            f"{library_express_count:,}",
            f"{library_express_pct:.2f}% of all checkins",
            "#6b7280"
        )
    
    with transit4:
        render_kpi_card(
            "Peak Avg Transit Day",
            peak_transit_day_label,
            peak_transit_day_subtitle,
            "#6b7280"
        )

    transit_pattern_title = "Transit Volume vs Reject Pattern"

    if transit_reject_insight_title == "No Clear Relationship":
        transit_pattern_text = (
            "High-transit days do not line up with high-reject days in the selected range. "
            "This suggests rejects are not mainly being driven by transit volume alone."
        )
        transit_pattern_color = "#6b7280"
    elif transit_reject_insight_title == "Strong Correlation":
        transit_pattern_text = (
            "High-transit days and high-reject days line up in the selected range. "
            "This suggests transit load may be contributing to failures."
        )
        transit_pattern_color = "#d97706"
    else:
        transit_pattern_text = transit_reject_insight_text
        transit_pattern_color = transit_reject_insight_color

    st.markdown(
        f"""
        <div style="
            border-left: 4px solid {transit_pattern_color};
            background-color: #f9fafb;
            padding: 14px 16px;
            border-radius: 8px;
            margin-top: 18px;
            margin-bottom: 4px;
        ">
            <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                {transit_pattern_title}
            </div>
            <div style="color: #4b5563; line-height: 1.4;">
                {transit_pattern_text}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.divider()

    st.subheader("Transit Reports")
    st.caption("Additional transit reports organized by data type.")

    with st.expander("Volume & Activity", expanded=False):
        st.subheader("Daily Transfer Summary")
        if len(base_df) > 0:
            daily_total = base_df.groupby(base_df["datetime"].dt.date).size()
            daily_ws = base_df[base_df["transit_destination"] == "Westside"].groupby(
                base_df[base_df["transit_destination"] == "Westside"]["datetime"].dt.date
            ).size()
            daily_le = base_df[base_df["transit_destination"] == "Library Express"].groupby(
                base_df[base_df["transit_destination"] == "Library Express"]["datetime"].dt.date
            ).size()
            daily_no_agency = base_df[
                base_df["destination_clean"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
            ].groupby(
                base_df[
                    base_df["destination_clean"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
                ]["datetime"].dt.date
            ).size()

            daily_transfer_summary = pd.DataFrame({
                "Date": pd.to_datetime(daily_total.index),
                "Total Checkins": daily_total.values
            })

            daily_transfer_summary["Transit to Westside"] = (
                daily_transfer_summary["Date"].dt.date.map(daily_ws).fillna(0).astype(int)
            )
            daily_transfer_summary["Transit to Library Express"] = (
                daily_transfer_summary["Date"].dt.date.map(daily_le).fillna(0).astype(int)
            )
            daily_transfer_summary["To No Agency Destination"] = (
                daily_transfer_summary["Date"].dt.date.map(daily_no_agency).fillna(0).astype(int)
            )
            daily_transfer_summary["Total Transit Items"] = (
                daily_transfer_summary["Transit to Westside"] +
                daily_transfer_summary["Transit to Library Express"]
            )
            daily_transfer_summary["Transit % of Total"] = (
                daily_transfer_summary["Total Transit Items"] / daily_transfer_summary["Total Checkins"] * 100
            ).round(2)

            daily_transfer_summary["Date"] = daily_transfer_summary["Date"].dt.strftime("%Y-%m-%d")

            st.dataframe(daily_transfer_summary, use_container_width=True)
            download_button(
                daily_transfer_summary,
                "daily_transfer_summary_report.csv",
                key="transit_reports_volume_activity_daily_transfer_summary_download"
            )
        else:
            st.info("No daily transfer summary data available for the selected date range.")
            
        st.subheader("Transit By Hour")
        st.caption("Shows how item transfers are distributed throughout the day to reveal peak routing times.")

        if len(transit_df) > 0:
            if transit_mode == "Today":
                transit_hourly = transit_df["datetime"].dt.hour.value_counts().sort_index().reset_index()
                transit_hourly.columns = ["hour", "transit_items"]
                transit_hourly["hour_label"] = transit_hourly["hour"].apply(format_hour_plain)
                transit_hourly = transit_hourly[
                    (transit_hourly["hour"] >= 7) & (transit_hourly["hour"] <= 20)
                ].copy()

                transit_hourly_chart = build_hourly_bar_chart(
                    transit_hourly,
                    "transit_items",
                    "Transit Items"
                )
                render_chart(transit_hourly_chart)

                transit_hourly_display = transit_hourly.rename(columns={
                    "hour_label": "Hour",
                    "transit_items": "Transit Items"
                })[["Hour", "Transit Items"]]

            else:
                transit_hourly_source = transit_df.copy()
                transit_hourly_source["date"] = transit_hourly_source["datetime"].dt.date
                transit_hourly_source["hour"] = transit_hourly_source["datetime"].dt.hour

                transit_hour_totals = (
                    transit_hourly_source.groupby("hour")
                    .size()
                    .reset_index(name="Total Transit Items")
                )

                transit_hour_daily = (
                    transit_hourly_source.groupby(["date", "hour"])
                    .size()
                    .reset_index(name="daily_transit_items")
                )

                transit_hour_avg = (
                    transit_hour_daily.groupby("hour")["daily_transit_items"]
                    .mean()
                    .reset_index(name="Avg Transit Items Per Day")
                )

                transit_hourly = transit_hour_totals.merge(
                    transit_hour_avg,
                    on="hour",
                    how="left"
                )

                transit_hourly["hour_label"] = transit_hourly["hour"].apply(format_hour_plain)
                transit_hourly = transit_hourly[
                    (transit_hourly["hour"] >= 7) & (transit_hourly["hour"] <= 20)
                ].copy()

                transit_hourly_chart = build_hourly_bar_chart(
                    transit_hourly.rename(columns={"Avg Transit Items Per Day": "avg_transit_items"}),
                    "avg_transit_items",
                    "Avg Transit Items Per Hour"
                )
                render_chart(transit_hourly_chart)

                transit_hourly_display = transit_hourly.rename(columns={
                    "hour_label": "Hour"
                })[["Hour", "Total Transit Items", "Avg Transit Items Per Day"]]

                transit_hourly_display["Avg Transit Items Per Day"] = (
                    transit_hourly_display["Avg Transit Items Per Day"].round(1)
                )

            st.dataframe(transit_hourly_display, use_container_width=True)
            download_button(
                transit_hourly_display,
                "transit_by_hour_report.csv",
                key="transit_by_hour_report_download"
            )
        else:
            st.info("No hourly transit data available for the selected date range.")

        st.subheader("Transit Trends Over Time")
        st.caption("Tells how transits to different branches change over time. Helps identify patterns in routing.")
        if len(transit_df) > 0:
            transit_mix = (
                transit_df.groupby([transit_df["datetime"].dt.date, "transit_destination"])
                .size()
                .reset_index(name="transit_items")
            )
            transit_mix.columns = ["date", "transit_destination", "transit_items"]
            transit_mix["date"] = pd.to_datetime(transit_mix["date"])

            transit_mix_chart = build_date_line_chart(
                transit_mix,
                "date",
                "transit_items",
                "Transit Items",
                series_col="transit_destination"
            )
            render_chart(transit_mix_chart)

            transit_mix_display = transit_mix.copy()
            transit_mix_display["date"] = pd.to_datetime(transit_mix_display["date"]).dt.strftime("%Y-%m-%d")
            transit_mix_display = transit_mix_display.rename(columns={
                "date": "Date",
                "transit_destination": "Destination",
                "transit_items": "Transit Items"
            })

            st.dataframe(transit_mix_display, use_container_width=True)
            download_button(
                transit_mix_display,
                "transit_trends_over_time_report.csv",
                key="transit_reports_volume_activity_transit_trends_over_time_download"
            )
        else:
            st.info("No transit trends data available for the selected date range.")

    with st.expander("Distribution & Flow", expanded=False):
        st.subheader("Routing Distribution")
        st.caption("Displays how items are proportionally distributed across all destinations.")
        if len(transit_summary) > 0:
            routing_distribution = transit_summary.copy()
            routing_distribution["pct_of_total_items"] = routing_distribution["pct_of_total_items"].round(2)

            routing_distribution_chart = build_category_bar_chart(
                routing_distribution.rename(columns={"pct_of_total_items": "routing_pct"}),
                "destination",
                "routing_pct",
                "% of Total Items",
                "Destination"
            )
            render_chart(routing_distribution_chart)

            routing_distribution_display = routing_distribution.rename(columns={
                "destination": "Destination",
                "transit_items": "Transit Items",
                "pct_of_total_items": "% of Total Items"
            })

            st.dataframe(routing_distribution_display, use_container_width=True)
            download_button(
                routing_distribution_display,
                "routing_distribution_report.csv",
                key="transit_reports_distribution_flow_routing_distribution_download"
            )
        else:
            st.info("No routing distribution data available for the selected date range.")

        st.subheader("Percentage Routing Over Time")
        st.caption("Gives the percentage of total items sent to each location over time.")
        if len(base_df) > 0 and len(transit_df) > 0:
            daily_total = base_df.groupby(base_df["datetime"].dt.date).size().rename("total_checkins")
            daily_routing = (
                transit_df.groupby([transit_df["datetime"].dt.date, "transit_destination"])
                .size()
                .reset_index(name="transit_items")
            )
            daily_routing.columns = ["date", "destination", "transit_items"]
            daily_routing["date"] = pd.to_datetime(daily_routing["date"])

            daily_routing["total_checkins"] = daily_routing["date"].dt.date.map(daily_total)
            daily_routing["routing_pct"] = (
                daily_routing["transit_items"] / daily_routing["total_checkins"] * 100
            ).round(2)

            routing_pct_chart = build_date_line_chart(
                daily_routing,
                "date",
                "routing_pct",
                "% of Total Items",
                series_col="destination"
            )
            render_chart(routing_pct_chart)

            routing_pct_display = daily_routing.copy()
            routing_pct_display["date"] = routing_pct_display["date"].dt.strftime("%Y-%m-%d")
            routing_pct_display = routing_pct_display.rename(columns={
                "date": "Date",
                "destination": "Destination",
                "transit_items": "Transit Items",
                "total_checkins": "Total Checkins",
                "routing_pct": "Routing %"
            })

            st.dataframe(routing_pct_display, use_container_width=True)
            download_button(
                routing_pct_display,
                "percentage_routing_over_time_report.csv",
                key="transit_reports_distribution_flow_percentage_routing_over_time_download"
            )
        else:
            st.info("No percentage routing data available for the selected date range.")

    with st.expander("Exceptions & Failures", expanded=False):
        st.subheader("Exception Report")
        if len(destination_reject_summary) > 0:
            diagnostics_display = destination_reject_summary.copy()
            diagnostics_display = diagnostics_display.rename(columns={
                "destination": "Destination",
                "transit_items": "Transit Items",
                "pct_of_total_items": "% of Total Items",
                "reject_count": "Transit-Linked Rejects",
                "reject_rate_pct": "Reject Rate %",
                "top_reject_reason": "Top Reject Reason",
                "reason_count": "Top Reason Count",
                "top_reason_pct_of_destination_rejects": "Top Reason % of Destination Rejects"
            })
            st.dataframe(diagnostics_display, use_container_width=True)
            download_button(
                diagnostics_display,
                "exception_report.csv",
                key="transit_reports_exceptions_failures_exception_report_download"
            )
        else:
            st.info("No destination-level transit reject data available for the selected date range.")

        st.subheader("Problem Items Deep Dive")
        st.caption("Looks at items with missing destination routing to highlight system or configuration issues.")
        no_agency_df = base_df[
            base_df["destination_clean"].astype(str).str.upper().str.contains("NO AGENCY DESTINATION", na=False)
        ].copy()

        if len(no_agency_df) > 0:
            no_agency_total = len(no_agency_df)

            no_agency_daily = (
                no_agency_df["datetime"]
                .dt.date
                .value_counts()
                .sort_index()
                .reset_index()
            )
            no_agency_daily.columns = ["date", "count"]
            no_agency_daily["date"] = pd.to_datetime(no_agency_daily["date"])

            st.subheader("No Agency Destination Items by Day")

            no_agency_daily_chart = build_date_line_chart(
                no_agency_daily,
                "date",
                "count",
                "No Agency Destination Items"
            )
            render_chart(no_agency_daily_chart)

            no_agency_hourly = (
                no_agency_df["datetime"]
                .dt.hour
                .value_counts()
                .sort_index()
                .reset_index()
            )
            no_agency_hourly.columns = ["hour", "count"]
            no_agency_hourly["hour_label"] = no_agency_hourly["hour"].apply(format_hour_plain)
            no_agency_hourly = no_agency_hourly[
                (no_agency_hourly["hour"] >= 7) & (no_agency_hourly["hour"] <= 20)
            ].copy()

            if len(no_agency_hourly) > 0:
                st.subheader("No Agency Destination Items by Hour")

                no_agency_hourly_chart = build_hourly_bar_chart(
                    no_agency_hourly,
                    "count",
                    "No Agency Destination Items"
                )
                render_chart(no_agency_hourly_chart)

            no_agency_display = no_agency_df[["datetime", "title", "barcode", "destination"]].copy()
            no_agency_display["datetime"] = pd.to_datetime(no_agency_display["datetime"]).dt.strftime("%Y-%m-%d %I:%M %p")
            no_agency_display = no_agency_display.rename(columns={
                "datetime": "Datetime",
                "title": "Title",
                "barcode": "Barcode",
                "destination": "Destination"
            })

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
                        {no_agency_total:,} items were routed to No Agency Destination in the selected date range.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            st.dataframe(no_agency_display, use_container_width=True)
            download_button(
                no_agency_display,
                "no_agency_destination_deep_dive_report.csv",
                key="transit_reports_exceptions_failures_no_agency_deep_dive_download"
            )
        else:
            st.info("No No Agency Destination items found for the selected date range.")

    with st.expander("Diagnostics & Insights", expanded=False):
        st.subheader("Baseline Comparison")
        st.caption("Compares recent data against historical data to detect anomalous activity.")

        historical_df = df_history_raw[df_history_raw["datetime"].dt.date < today].copy()
        
        if len(base_df) > 0 and len(historical_df) > 0:
            current_total_transit = len(transit_df)
            current_total_items = len(base_df)
            current_ws_pct = westside_pct
            current_le_pct = library_express_pct
        
            current_days = max(1, base_df["datetime"].dt.date.nunique())
        
            historical_df["destination_clean"] = historical_df["destination"].astype(str).str.strip()
            historical_df["transit_destination"] = historical_df["destination_clean"].apply(normalize_transit_destination)
        
            historical_df["destination_report"] = historical_df["destination_clean"].copy()
            historical_df.loc[historical_df["destination_report"] == "1", "destination_report"] = "Main"
            historical_df.loc[historical_df["transit_destination"] == "Westside", "destination_report"] = "Westside"
            historical_df.loc[historical_df["transit_destination"] == "Library Express", "destination_report"] = "Library Express"
        
            historical_df["destination_clean"] = historical_df["destination_report"]
        
            historical_transit_df = historical_df[
                historical_df["transit_destination"].isin(valid_transit_destinations)
            ].copy()

            historical_total_transit = len(historical_transit_df)
            historical_total_items = len(historical_df)
            historical_days = max(1, historical_df["datetime"].dt.date.nunique())

            current_avg_daily_transit = current_total_transit / current_days
            historical_avg_daily_transit = historical_total_transit / historical_days

            historical_ws_pct = (
                len(historical_df[historical_df["transit_destination"] == "Westside"]) / historical_total_items * 100
            ) if historical_total_items > 0 else 0

            historical_le_pct = (
                len(historical_df[historical_df["transit_destination"] == "Library Express"]) / historical_total_items * 100
            ) if historical_total_items > 0 else 0

            baseline_df = pd.DataFrame({
                "Metric": [
                    "Avg Daily Transit Items",
                    "Westside %",
                    "Library Express %"
                ],
                "Current": [
                    round(current_avg_daily_transit, 1),
                    round(current_ws_pct, 2),
                    round(current_le_pct, 2)
                ],
                "Historical Baseline": [
                    round(historical_avg_daily_transit, 1),
                    round(historical_ws_pct, 2),
                    round(historical_le_pct, 2)
                ]
            })

            st.dataframe(baseline_df, use_container_width=True)
            download_button(
                baseline_df,
                "transit_baseline_comparison_report.csv",
                key="transit_reports_diagnostics_baseline_comparison_download"
            )
        else:
            st.info("Not enough data available for baseline comparison.")

        st.subheader("Destination Diagnostics")
        st.caption("Shows which transit destinations are driving the most volume, rejects, and failure patterns.")

        if len(destination_reject_summary) > 0:
            diagnostics_df = destination_reject_summary.copy()

            diagnostics_df["transit_items"] = diagnostics_df["transit_items"].fillna(0).astype(int)
            diagnostics_df["pct_of_total_items"] = diagnostics_df["pct_of_total_items"].fillna(0).round(2)
            diagnostics_df["reject_count"] = diagnostics_df["reject_count"].fillna(0).astype(int)
            diagnostics_df["reject_rate_pct"] = diagnostics_df["reject_rate_pct"].fillna(0).round(2)
            diagnostics_df["reason_count"] = diagnostics_df["reason_count"].fillna(0).astype(int)
            diagnostics_df["top_reason_pct_of_destination_rejects"] = (
                diagnostics_df["top_reason_pct_of_destination_rejects"].fillna(0).round(2)
            )

            diagnostics_display = diagnostics_df.rename(columns={
                "destination": "Destination",
                "transit_items": "Transit Items",
                "pct_of_total_items": "% of Total Items",
                "reject_count": "Transit-Linked Rejects",
                "reject_rate_pct": "Reject Rate %",
                "top_reject_reason": "Top Reject Reason",
                "reason_count": "Top Reason Count",
                "top_reason_pct_of_destination_rejects": "Top Reason % of Destination Rejects"
            })

            if len(diagnostics_df) > 0:
                top_problem_row = diagnostics_df.sort_values(
                    ["reject_count", "reject_rate_pct", "transit_items"],
                    ascending=False
                ).iloc[0]

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
                            {top_problem_row['destination']} currently shows the strongest operational impact:
                            {int(top_problem_row['transit_items']):,} transit items,
                            {int(top_problem_row['reject_count']):,} transit-linked rejects,
                            and a {float(top_problem_row['reject_rate_pct']):.2f}% reject rate.
                            Top issue: {top_problem_row['top_reject_reason']}.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            st.dataframe(diagnostics_display, use_container_width=True)
            download_button(
                diagnostics_display,
                "destination_diagnostics_report.csv",
                key="transit_reports_diagnostics_destination_diagnostics_download"
            )
        else:
            st.info("No destination diagnostics available for the selected date range.")
