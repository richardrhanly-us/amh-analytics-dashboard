import altair as alt
import pandas as pd
import streamlit as st

from ui_components import (
    format_hour,
    format_hour_plain,
    format_ill_branch_subtitle,
    render_chart,
    render_kpi_card,
)


def render_live_today(
    today,
    refresh_count,
    pipeline_status_label,
    pipeline_status_color,
    pipeline_status_bg,
    pipeline_expanded,
    app_refreshed_str,
    latest_checkin_str,
    latest_checkin_ago,
    pipeline_status_written_str,
    pipeline_status_written_ago,
    pipeline_last_attempt_str,
    pipeline_last_attempt_ago,
    pipeline_last_run_str,
    pipeline_last_run_ago,
    pipeline_result_text,
    status_code_text,
    checkins_rows,
    rejects_rows,
    uploaded_checkins_rows,
    uploaded_rejects_rows,
    checkins_bad_datetime_rows,
    rejects_bad_datetime_rows,
    transit_items,
    problem_items,
    destination_breakdown_text,
    today_metrics,
    today_checkins,
    today_rejects,
    today_total_transit,
    today_transit_counts_map,
    today_transit_pct_map,
    today_peak_hour,
    today_peak_hour_count,
    today_peak_hour_pct,
    today_reject_rate,
    historical_daily_avg_reject,
    live_reject_deviation,
    live_reject_subtitle_color,
    live_reject_value_color,
    TRANSIT_LABELS,
    TRANSIT_HOME_LABEL,
    today_holds,
    today_ill,
    today_ill_main,
    today_ill_by_branch,
    today_programming,
    today_collection_services,
    today_public_holds_df,
    today_ill_items_df,
    today_programming_df,
    today_collection_services_df,
    info_alerts,
    show_live_alert,
    live_alert_title,
    live_alert_text,
    info_border,
    info_bg,
    info_title,
    info_text,
    danger_border,
    danger_bg,
    danger_title,
    danger_text,
    today_df,
    today_rejects_df,
    today_hourly_checkins,
    live_hour_range,
    can_view_transits=True,
    can_view_internal_workflow=True,
):
    col1, col2 = st.columns([4, 2])

    with col1:
        st.header(f"{today.strftime('%A, %b %d')}")

        if st.button("Refresh Live Data"):
            # Pre-existing, manual, global cache clear -- wipes every
            # tenant/session's cached data on the server, not just this
            # one. Out of scope for Continuous Ingestion Phase 4 (which
            # only touches the automatic refresh_count-driven cadence in
            # app.py/data_loader.py), but worth a note now that the
            # automatic refresh is much faster (as low as 10s): this
            # button is a much bigger hammer than the interval users can
            # already just wait out, and is a candidate to revisit if it
            # turns out to be needed/used often post-Phase-4.
            st.cache_data.clear()
            st.session_state["last_refresh_count"] = refresh_count
            st.rerun()

    with col2:
        expander_label = f"● {pipeline_status_label}"

        st.markdown(
            f"""
            <style>
            div[data-testid="stExpander"] details {{
                border: 1px solid rgba(148, 163, 184, 0.28);
                border-radius: 10px;
                overflow: hidden;
                background-color: var(--secondary-background-color);
            }}

            div[data-testid="stExpander"] summary {{
                font-weight: 700;
                color: {pipeline_status_color};
                background-color: {pipeline_status_bg};
                padding-top: 0.2rem;
                padding-bottom: 0.2rem;
            }}

            div[data-testid="stExpander"] details[open] > div {{
                background-color: var(--secondary-background-color);
                color: var(--text-color);
            }}
            </style>
            """,
            unsafe_allow_html=True,
        )

        with st.expander(expander_label, expanded=pipeline_expanded):
            st.markdown(
                "##### Pipeline Status"
                f"""
App Last Refreshed: {app_refreshed_str}  
Latest Checkin in DB: {latest_checkin_str} ({latest_checkin_ago})  
Latest Status Row Written: {pipeline_status_written_str} ({pipeline_status_written_ago})  
Last Pipeline Attempt: {pipeline_last_attempt_str} ({pipeline_last_attempt_ago})  
Last Successful Upload Run: {pipeline_last_run_str} ({pipeline_last_run_ago})  
Latest Result: {pipeline_result_text}  
Status Code: `{status_code_text}`
                """
            )

            st.markdown("##### Run Summary")
            s1, s2 = st.columns(2)

            with s1:
                st.markdown(
                    f"""
New Checkins This Run: {checkins_rows:,}  
New Rejects This Run: {rejects_rows:,}  
Uploaded Checkins This Run: {uploaded_checkins_rows:,}  
Uploaded Rejects This Run: {uploaded_rejects_rows:,}
                    """
                )
            with s2:
                st.markdown(
                    f"""
Bad Checkin Datetimes: {checkins_bad_datetime_rows:,}  
Bad Reject Datetimes: {rejects_bad_datetime_rows:,}  
Transit Items: {transit_items:,}  
Problem Items: {problem_items:,}
                    """
                )

            st.markdown("##### Destination Breakdown")
            st.caption(destination_breakdown_text)

    if can_view_transits:
        live_group1, live_group2, live_group3 = st.columns(3)
    else:
        live_group1, live_group3 = st.columns(2)
        live_group2 = None

    with live_group1:
        st.markdown(
            """
            <div style="
                border: 2px solid #60a5fa;
                border-radius: 14px;
                padding: 12px 14px;
                background: #60a5fa;
                margin-bottom: 8px;
            ">
                <div style="
                    font-size: 0.95rem;
                    font-weight: 700;
                    color: #ffffff;
                    line-height: 1.2;
                ">
                    Operations
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        ops1, ops2, ops3 = st.columns(3)

        with ops1:
            render_kpi_card(
                "Checkins",
                f"{today_checkins:,}",
                "Processed today",
                "#6b7280",
                value_font_size="2.2rem",
                border_color="#93c5fd",
            )

        with ops2:
            pct = today_metrics.get("current_speed_fill_pct", 0)

            render_kpi_card(
                "Current Throughput",
                f"{today_metrics['current_speed']}",
                f"""
                Items this hour
                <div style="margin-top:6px; width:100%; padding:0 2px;">
                    <div style="
                        position:relative;
                        height:5px;
                        border-radius:999px;
                        background:linear-gradient(to right, #60a5fa, #f59e0b, #ef4444);
                        width:100%;
                        margin:0 auto;
                    ">
                        <div style="
                            position:absolute;
                            left:calc({pct * 100:.1f}% - 1px);
                            top:-3px;
                            width:3px;
                            height:11px;
                            border-radius:2px;
                            background:#111827;
                        "></div>
                    </div>
                    <div style="
                        display:flex;
                        justify-content:space-between;
                        align-items:center;
                        font-size:0.72rem;
                        color:#6b7280;
                        margin-top:3px;
                        line-height:1.1;
                    ">
                        <span>Slow</span>
                        <span>Busy</span>
                    </div>
                </div>
                """,
                "#6b7280",
                value_font_size="2.2rem",
                border_color="#93c5fd",
            )

        with ops3:
            if today_peak_hour is not None:
                render_kpi_card(
                    "Busiest Hour",
                    format_hour(today_peak_hour),
                    f"{today_peak_hour_count:,} items ({today_peak_hour_pct:.1f}%)",
                    "#6b7280",
                    value_font_size="1.7rem",
                    border_color="#93c5fd",
                )
            else:
                render_kpi_card(
                    "Busiest Hour",
                    "N/A",
                    "No activity yet",
                    "#6b7280",
                    value_font_size="1.7rem",
                    border_color="#93c5fd",
                )

    if can_view_transits and live_group2 is not None:
        with live_group2:
            st.markdown(
                """
                <div style="
                    border: 2px solid #34d399;
                    border-radius: 14px;
                    padding: 12px 14px;
                    background: #34d399;
                    margin-bottom: 8px;
                ">
                    <div style="
                        font-size: 0.95rem;
                        font-weight: 700;
                        color: #ffffff;
                        line-height: 1.2;
                    ">
                        Routing
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    
            routing_card_count = 1 + len(TRANSIT_LABELS)
            routing_cols = st.columns(routing_card_count if routing_card_count > 0 else 1)
    
            total_transit_pct = (today_total_transit / today_checkins * 100) if today_checkins > 0 else 0
    
            with routing_cols[0]:
                render_kpi_card(
                    "Total Transit",
                    f"{today_total_transit:,}",
                    f"{total_transit_pct:.1f}% of today",
                    "#6b7280",
                    value_font_size="2.2rem",
                    border_color="#34d399",
                )
    
            for idx, transit_label in enumerate(TRANSIT_LABELS, start=1):
                with routing_cols[idx]:
                    render_kpi_card(
                        transit_label,
                        f"{today_transit_counts_map.get(transit_label, 0):,}",
                        f"{today_transit_pct_map.get(transit_label, 0):.1f}% of today",
                        "#6b7280",
                        value_font_size="1.9rem",
                        border_color="#34d399",
                    )


    with live_group3:
        st.markdown(
            """
            <div style="
                border: 2px solid #a78bfa;
                border-radius: 14px;
                padding: 12px 14px;
                background: #a78bfa;
                margin-bottom: 8px;
            ">
                <div style="
                    font-size: 0.95rem;
                    font-weight: 700;
                    color: #ffffff;
                    line-height: 1.2;
                ">
                    Rejects
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        quality1, quality2 = st.columns(2)

        with quality1:
            render_kpi_card(
                "Rejects",
                f"{today_rejects:,}",
                "Failures today",
                "#6b7280",
                value_font_size="2.2rem",
                border_color="#c4b5fd",
            )

        with quality2:
            reject_rate_subtitle = "Of checkins today"
            if historical_daily_avg_reject > 0:
                reject_rate_subtitle = (
                    f"{live_reject_deviation:+.2f}% vs avg daily "
                    f"rate ({historical_daily_avg_reject:.2f}%)"
                )

            render_kpi_card(
                "Reject Rate",
                f"{today_reject_rate:.2f}%",
                reject_rate_subtitle,
                live_reject_subtitle_color,
                value_font_size="1.8rem",
                border_color="#c4b5fd",
                value_color=live_reject_value_color,
            )

    if can_view_internal_workflow:
        st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)
    
        st.markdown(
            """
            <div style="
                border: 2px solid #14b8a6;
                border-radius: 14px;
                padding: 12px 14px;
                background: linear-gradient(90deg, #14b8a6, #0ea5e9);
                margin-bottom: 8px;
            ">
                <div style="
                    font-size: 0.95rem;
                    font-weight: 700;
                    color: #ffffff;
                    line-height: 1.2;
                ">
                    Internal Workflow
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
    
        internal1, internal2, internal3, internal4 = st.columns(4)
    
        internal_pct_base = today_checkins if today_checkins > 0 else 1
    
        with internal1:
            render_kpi_card(
                "Holds",
                f"{today_holds:,}",
                f"Public holds after internal workflow subtraction ({(today_holds / internal_pct_base) * 100:.1f}% of checkins)",
                "#6b7280",
                value_font_size="2.0rem",
                border_color="#34d399"
            )
    
        with internal2:
            render_kpi_card(
                "ILL",
                f"{today_ill:,}",
                format_ill_branch_subtitle(
                    today_ill_main,
                    today_ill_by_branch,
                    TRANSIT_HOME_LABEL,
                    TRANSIT_LABELS,
                ),
                "#6b7280",
                value_font_size="1.85rem",
                border_color="#34d399"
            )
    
        with internal3:
            render_kpi_card(
                "Branch Services",
                f"{today_programming:,}",
                f"{(today_programming / internal_pct_base) * 100:.1f}% of checkins today",
                "#6b7280",
                value_font_size="1.85rem",
                border_color="#34d399"
            )
    
        with internal4:
            render_kpi_card(
                "Collection Services",
                f"{today_collection_services:,}",
                f"{(today_collection_services / internal_pct_base) * 100:.1f}% of checkins today",
                "#6b7280",
                value_font_size="1.7rem",
                border_color="#34d399"
            )
    
        with st.expander("Internal workflow audit", expanded=False):
            st.write("Public Holds:", today_holds)
            st.write("ILL:", today_ill)
            st.write("Programming:", today_programming)
            st.write("Collection Services:", today_collection_services)


    if info_alerts:
        st.markdown(
            f"""
            <div style="
                border-left: 5px solid {info_border};
                background-color: {info_bg};
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 18px;
                margin-bottom: 12px;
            ">
                <div style="font-weight: 600; color: {info_title}; margin-bottom: 6px;">
                    Trends / Info
                </div>
                <ul style="margin: 0; padding-left: 18px; color: {info_text};">
                    {''.join(f"<li><b>{a['level'].upper()}</b>: {a['text']}</li>" for a in info_alerts)}
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if show_live_alert:
        st.markdown(
            f"""
            <div style="
                border-left: 4px solid {danger_border};
                background-color: {danger_bg};
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 18px;
                margin-bottom: 8px;
            ">
                <div style="font-weight: 600; color: {danger_title}; margin-bottom: 6px;">
                    {live_alert_title}
                </div>
                <div style="color: {danger_text}; line-height: 1.4;">
                    {live_alert_text}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.divider()
    st.subheader("Checkins by Hour")

    checkins_hour_base = pd.DataFrame({"hour": live_hour_range})
    checkins_hour_base["hour_label"] = checkins_hour_base["hour"].apply(format_hour_plain)

    if len(today_hourly_checkins) > 0:
        checkins_hour_df = today_hourly_checkins.reset_index()
        checkins_hour_df.columns = ["hour", "checkins"]
    else:
        checkins_hour_df = pd.DataFrame(columns=["hour", "checkins"])

    checkins_hour_df = checkins_hour_base.merge(checkins_hour_df, on="hour", how="left").fillna(0)
    checkins_hour_df["checkins"] = checkins_hour_df["checkins"].astype(int)

    checkins_hour_chart = (
        alt.Chart(checkins_hour_df)
        .mark_line(point=True, strokeWidth=3)
        .encode(
            x=alt.X(
                "hour_label:N",
                sort=checkins_hour_df["hour_label"].tolist(),
                title="Hour",
                axis=alt.Axis(labelAngle=0),
            ),
            y=alt.Y("checkins:Q", title="Checkins"),
            tooltip=["hour_label", "checkins"],
        )
        .properties(height=250)
    )

    render_chart(checkins_hour_chart)

    st.subheader("Bin Volume")
    st.caption("Distribution of items across sort bins for today.")

    if "bin" in today_df.columns:
        today_bin_kpi_df = today_df.copy()
        today_bin_kpi_df = today_bin_kpi_df[today_bin_kpi_df["bin"].notna()].copy()
        today_bin_kpi_df["bin"] = today_bin_kpi_df["bin"].astype(str)

        if len(today_bin_kpi_df) > 0:
            today_bin_summary = (
                today_bin_kpi_df["bin"].value_counts().sort_index().reset_index()
            )
            today_bin_summary.columns = ["bin", "checkins"]

            bin_bar_df = today_bin_summary.copy()
            bin_bar_df["bin_label"] = bin_bar_df["bin"].apply(lambda b: f"Bin {b}")

            today_bin_bar_chart = (
                alt.Chart(bin_bar_df)
                .mark_bar()
                .encode(
                    x=alt.X("bin_label:N", title="Bin", axis=alt.Axis(labelAngle=0)),
                    y=alt.Y("checkins:Q", title="Checkins"),
                    tooltip=["bin_label", "checkins"],
                )
                .properties(height=350)
            )

            render_chart(today_bin_bar_chart)
        else:
            st.info("No binned checkins found for today.")
    else:
        st.info("No bin column found in today's checkins data.")

    st.subheader("Bin Volume by Hour")

    if "bin" in today_df.columns:
        today_bin_df = today_df.copy()
        today_bin_df = today_bin_df[today_bin_df["bin"].notna()].copy()
        today_bin_df["bin"] = today_bin_df["bin"].astype(str)

        if len(today_bin_df) > 0:
            today_hourly_bin = (
                today_bin_df.groupby([today_bin_df["datetime"].dt.hour, "bin"])
                .size()
                .unstack(fill_value=0)
            )

            today_hourly_bin = today_hourly_bin.reindex(live_hour_range, fill_value=0)

            today_hourly_bin_chart = today_hourly_bin.copy()
            today_hourly_bin_chart.columns = [f"Bin {col}" for col in today_hourly_bin_chart.columns]

            today_hourly_bin_display = today_hourly_bin_chart.copy().reset_index()
            today_hourly_bin_display.columns = ["hour"] + list(today_hourly_bin_display.columns[1:])
            today_hourly_bin_display["hour_label"] = today_hourly_bin_display["hour"].apply(format_hour_plain)

            today_hourly_bin_long = today_hourly_bin_display.melt(
                id_vars=["hour", "hour_label"],
                var_name="bin",
                value_name="checkins",
            )

            live_bin_chart = (
                alt.Chart(today_hourly_bin_long)
                .mark_line(point=False)
                .encode(
                    x=alt.X(
                        "hour_label:N",
                        sort=today_hourly_bin_display["hour_label"].tolist(),
                        title="Hour",
                        axis=alt.Axis(labelAngle=0),
                    ),
                    y=alt.Y("checkins:Q", title="Checkins"),
                    color=alt.Color("bin:N", title="Bin"),
                    tooltip=["hour_label", "bin", "checkins"],
                )
                .properties(height=350)
            )

            render_chart(live_bin_chart)
        else:
            st.info("No binned checkins found for today.")
    else:
        st.info("No bin column found in today's checkins data.")

    st.subheader("Top Issues Today")

    if len(today_rejects_df) > 0:
        today_issue_counts = today_rejects_df["error_simple"].value_counts()
        top_issue = today_issue_counts.index[0]
        top_issue_count = int(today_issue_counts.iloc[0])

        if len(today_issue_counts) > 1:
            second_issue = today_issue_counts.index[1]
            second_issue_count = int(today_issue_counts.iloc[1])
            issue_text = (
                f"Top issue today: {top_issue} ({top_issue_count}). "
                f"Next: {second_issue} ({second_issue_count})."
            )
        else:
            issue_text = f"Top issue today: {top_issue} ({top_issue_count})."

        st.markdown(
            f"""
            <div style="
                border-left: 4px solid #d97706;
                background-color: #f9fafb;
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 8px;
                margin-bottom: 8px;
            ">
                <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                    Issue Snapshot
                </div>
                <div style="color: #4b5563; line-height: 1.4;">
                    {issue_text}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.info("No reject issues found for today.")
