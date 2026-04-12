import streamlit as st
import pandas as pd

from ui_components import render_kpi_card, format_ill_branch_subtitle
from metrics import build_acs_item_summary, build_roi_payload


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
):
    st.subheader("Summary")
    st.caption("Get a historical summary by choosing a date range.")

    # -------------------------------
    # ACS / INTERNAL WORKFLOW
    # -------------------------------
    overview_acs_df = acs_history_raw.copy()

    if len(overview_acs_df) > 0 and "datetime" in overview_acs_df.columns:
        overview_acs_df["datetime"] = pd.to_datetime(overview_acs_df["datetime"], errors="coerce")
        overview_acs_df = overview_acs_df.dropna(subset=["datetime"])
        overview_acs_df = overview_acs_df[
            (overview_acs_df["datetime"].dt.date >= start_date) &
            (overview_acs_df["datetime"].dt.date <= end_date)
        ]

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

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        render_kpi_card("Holds", f"{overview_holds:,}", date_range_text, "#6b7280")

    with col2:
        render_kpi_card(
            "ILL",
            f"{overview_ill:,}",
            format_ill_branch_subtitle(
                overview_ill_main,
                overview_ill_by_branch,
                TRANSIT_HOME_LABEL,
                TRANSIT_LABELS,
            ),
            "#6b7280"
        )

    with col3:
        render_kpi_card(
            "Programming",
            f"{overview_acs_summary['programming_total']:,}",
            date_range_text,
            "#6b7280"
        )

    with col4:
        render_kpi_card(
            "Collection Services",
            f"{overview_acs_summary['collection_services_total']:,}",
            date_range_text,
            "#6b7280"
        )

    # -------------------------------
    # ATTENTION BOX
    # -------------------------------
    st.markdown(
        f"""
        <div style="
            border-left: 4px solid {attention_color};
            background-color: #f9fafb;
            padding: 14px;
            border-radius: 8px;
            margin-top: 10px;
        ">
            <b>{attention_title}</b><br>
            {attention_text}
        </div>
        """,
        unsafe_allow_html=True
    )

    # -------------------------------
    # BASIC METRICS
    # -------------------------------
    total_checkins = len(df)
    reject_count = len(rejects_df)

    reject_pct = (reject_count / total_checkins * 100) if total_checkins > 0 else 0

    m1, m2, m3 = st.columns(3)

    with m1:
        render_kpi_card("Total Checkins", f"{total_checkins:,}", date_range_text, "#6b7280")

    with m2:
        render_kpi_card("Reject Count", f"{reject_count:,}", "Total failures", "#6b7280")

    with m3:
        render_kpi_card("Reject %", f"{reject_pct:.2f}%", date_range_text, "#6b7280")

    # -------------------------------
    # ROI (SIMPLE VERSION FOR NOW)
    # -------------------------------
    if st.session_state.get("roi_calculated", False):

        roi_payload = build_roi_payload(
            df,
            start_date,
            end_date,
            hourly_cost=st.session_state.get("roi_hourly_cost", 18.0),
            upfront_cost=st.session_state.get("roi_upfront_cost", 200000.0),
            monthly_cost=st.session_state.get("roi_monthly_cost", 0.0),
            yearly_cost=st.session_state.get("roi_yearly_cost", 8000.0),
        )

        if roi_payload:
            st.markdown("### ROI")

            r1, r2 = st.columns(2)

            with r1:
                render_kpi_card(
                    "Net Value",
                    f"${roi_payload['observed_net_operating_value']:,.0f}",
                    "Selected range",
                    "#6b7280"
                )

            with r2:
                render_kpi_card(
                    "Annual Projection",
                    f"${roi_payload['net_roi_value']:,.0f}",
                    "Projected yearly",
                    "#6b7280"
                )
