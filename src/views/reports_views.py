import altair as alt
import pandas as pd
import streamlit as st

from metrics import build_roi_payload
from ui_components import (
    render_kpi_card,
    download_button,
    render_chart,
    build_hourly_bar_chart,
    build_category_bar_chart,
    build_hourly_line_chart,
    format_hour_plain,
)


def render_reports(
    df,
    df_history_raw,
    df_live_raw,
    rejects_df,
    start_date,
    end_date,
    today,
    overall_metrics,
    top_issue,
    attention_text,
    LIBRARY_NAME,
    BRANCH_NAME,
    SYSTEM_NAME,
):

st.header("Reports")
pdf_button_placeholder = st.empty()


# =========================================================
# LABOR & EFFICIENCY
# =========================================================
st.subheader("Labor & Efficiency")
st.caption("Estimates staff time saved by Automated Materials Handler processing.")


# =========================================================
# ROI CALCULATOR
# =========================================================
with st.expander("ROI Calculator", expanded=False):
    st.caption("Estimate operating value, annual ROI, payback period, and since-install return.")

    roi_help_col1, roi_help_col2 = st.columns([3, 2])

    with roi_help_col1:
        st.info(
            "Use this section to set labor cost, capital cost, recurring cost, ROI mode, and install date. "
            "The Overview tab will use these same ROI settings."
        )

    with roi_help_col2:
        st.markdown(
            """
            <div style="
                border-left: 4px solid #7c3aed;
                background-color: #f9fafb;
                padding: 14px 16px;
                border-radius: 8px;
                margin-top: 2px;
                margin-bottom: 12px;
            ">
                <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                    What this controls
                </div>
                <div style="color: #4b5563; line-height: 1.45;">
                    Labor value, observed net value, annual ROI, payback period,
                    and since-install ROI.
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

    labor_input_col, upfront_col, monthly_col, yearly_col = st.columns(4)

    with labor_input_col:
        HOURLY_COST = st.number_input(
            "Hourly labor rate ($/hour)",
            min_value=0.0,
            max_value=1000.0,
            value=st.session_state.get("roi_hourly_cost", 17.56),
            step=0.5,
            format="%.2f",
            key="roi_hourly_cost",
            help="Adjust the hourly labor cost used to estimate labor value."
        )

    with upfront_col:
        UPFRONT_COST = st.number_input(
            "Upfront cost ($)",
            min_value=0.0,
            max_value=10000000.0,
            value=st.session_state.get("roi_upfront_cost", 118003.92),
            step=100.0,
            format="%.2f",
            key="roi_upfront_cost",
            help="One-time purchase or implementation cost."
        )

    with monthly_col:
        MONTHLY_COST = st.number_input(
            "Monthly cost ($/month)",
            min_value=0.0,
            max_value=1000000.0,
            value=st.session_state.get("roi_monthly_cost", 0.0),
            step=10.0,
            format="%.2f",
            key="roi_monthly_cost",
            help="Recurring monthly maintenance, service, or lease cost."
        )

    with yearly_col:
        YEARLY_COST = st.number_input(
            "Yearly cost ($/year)",
            min_value=0.0,
            max_value=1000000.0,
            value=st.session_state.get("roi_yearly_cost", 8400.0),
            step=50.0,
            format="%.2f",
            key="roi_yearly_cost",
            help="Recurring annual support, licensing, or maintenance cost."
        )

    roi_mode_col1, roi_mode_col2 = st.columns([2, 2])

    with roi_mode_col1:
        roi_mode = st.radio(
            "Calculation Mode",
            ["Observed (Selected Range)", "Annualized Projection"],
            horizontal=True,
            key="roi_mode",
            help="Observed uses only the selected date range. Annualized Projection scales the observed labor value to a 12-month estimate."
        )

    with roi_mode_col2:
        INSTALL_DATE = st.date_input(
            "Installed on",
            value=st.session_state.get("roi_install_date", pd.to_datetime("2020-11-20").date()),
            key="roi_install_date",
            help="Used to estimate ROI since the AMH was put into service."
        )

    INCLUDE_UPFRONT_IN_SINCE_INSTALL = st.checkbox(
        "Include upfront cost in since-install ROI",
        value=st.session_state.get("roi_include_upfront_since_install", True),
        key="roi_include_upfront_since_install",
        help="Usually this should stay on, since purchase ROI should include the initial capital cost."
    )

    calc_col1, calc_col2 = st.columns([1, 5])

    with calc_col1:
        calculate_roi_clicked = st.button("Calculate ROI", type="primary")

    with calc_col2:
        if st.button("Clear ROI Results"):
            st.session_state["roi_calculated"] = False
            st.rerun()

    if calculate_roi_clicked:
        st.session_state["roi_calculated"] = True

    roi_payload = None

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
        roi_pct = roi_payload["roi_pct"]
        net_roi_value = roi_payload["net_roi_value"]
        total_roi_cost = roi_payload["total_roi_cost"]
        payback_months = roi_payload["payback_months"]
        since_install_roi_pct = roi_payload["since_install_roi_pct"]
        since_install_net_value = roi_payload["since_install_net_value"]
        annual_labor_value = roi_payload["annual_labor_value"]
        annual_operating_cost = roi_payload["annual_operating_cost"]
        labor_value_saved = roi_payload["labor_value_saved"]
        observed_operating_cost = roi_payload["observed_operating_cost"]
        observed_net_operating_value = roi_payload["observed_net_operating_value"]
        observed_hours_saved = labor_value_saved / HOURLY_COST if HOURLY_COST > 0 else 0

        months_in_range = max((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 1, 1) / 30.44
        years_in_range = max((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 1, 1) / 365.25
        days_in_range = max((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 1, 1)

        install_date_ts = pd.to_datetime(INSTALL_DATE)
        today_ts = pd.Timestamp.today().normalize()
        installed_days = max((today_ts - install_date_ts).days, 1)
        installed_years = installed_days / 365.25

        since_install_labor_value = annual_labor_value * installed_years
        since_install_operating_cost = annual_operating_cost * installed_years

        if INCLUDE_UPFRONT_IN_SINCE_INSTALL:
            since_install_total_cost = UPFRONT_COST + since_install_operating_cost
        else:
            since_install_total_cost = since_install_operating_cost

        def render_explainer_card(title, body, border_color):
            st.markdown(
                f"""
                <div style="
                    border-left: 4px solid {border_color};
                    background-color: #f9fafb;
                    padding: 14px 16px;
                    border-radius: 8px;
                    margin-top: 8px;
                    margin-bottom: 12px;
                ">
                    <div style="font-weight: 600; color: #1f2937; margin-bottom: 6px;">
                        {title}
                    </div>
                    <div style="color: #4b5563; line-height: 1.45;">
                        {body}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

        # ===============================
        # ROW HEADER (MODE DEPENDENT)
        # ===============================
        if roi_mode == "Annualized Projection":
            st.markdown("### Annualized Metrics")
        else:
            st.markdown("### Selected Range Metrics")
        
        # ===============================
        # FIRST ROW (MODE DEPENDENT KPIs)
        # ===============================
        roi1, roi2, roi3, roi4 = st.columns(4)

        if roi_mode == "Annualized Projection":

            with roi1:
                render_kpi_card(
                    "1. Yearly Cost (User provided)",
                    f"${total_roi_cost:,.0f}",
                    "Recurring annual cost only",
                    "#6b7280"
                )

            with roi2:
                render_kpi_card(
                    "2. Twelve-month projection of savings at current rate",
                    f"${net_roi_value:,.0f}",
                    f"Based on last {days_in_range:,} days of activity",
                    "#6b7280",
                    value_color="#059669" if net_roi_value >= 0 else "#dc2626"
                )

            with roi3:
                if payback_months is not None:
                    payback_years = payback_months / 12
                    years_after_payback = installed_years - payback_years

                    if years_after_payback >= 0:
                        break_even_value = "Paid Off"
                        break_even_subtitle = f"Recovered cost ~{years_after_payback:,.1f} years ago"
                        break_even_color = "#059669"
                    else:
                        break_even_value = f"{abs(years_after_payback):,.1f} yrs"
                        break_even_subtitle = "Estimated time remaining to recover upfront cost"
                        break_even_color = "#d97706"
                else:
                    break_even_value = "Not Reached"
                    break_even_subtitle = "Current annual run rate does not recover upfront cost"
                    break_even_color = "#dc2626"

                render_kpi_card(
                    "3. Break-even Status",
                    break_even_value,
                    break_even_subtitle,
                    "#6b7280",
                    value_color=break_even_color
                )

            with roi4:
                render_kpi_card(
                    "4. Lifetime Value Generated",
                    f"${since_install_labor_value:,.0f}",
                    f"Estimated value created over {installed_years:,.1f} years",
                    "#6b7280",
                    value_color="#059669" if since_install_labor_value >= 0 else "#dc2626"
                )

        else:

            with roi1:
                render_kpi_card(
                    "Range Length",
                    f"{days_in_range:,} days",
                    f"{months_in_range:,.2f} months",
                    "#6b7280",
                    value_font_size="1.8rem"
                )

            with roi2:
                render_kpi_card(
                    "Observed Labor Value",
                    f"${labor_value_saved:,.0f}",
                    "Staff time avoided value",
                    "#6b7280"
                )

            with roi3:
                render_kpi_card(
                    "Observed Operating Cost",
                    f"${observed_operating_cost:,.0f}",
                    "Prorated recurring cost only",
                    "#6b7280"
                )

            with roi4:
                render_kpi_card(
                    "Observed Net Value",
                    f"${observed_net_operating_value:,.0f}",
                    "Labor value minus operating cost",
                    "#6b7280",
                    value_color="#059669" if observed_net_operating_value >= 0 else "#dc2626"
                )

        # ===============================
        # LIFETIME HEADER (STATIC)
        # ===============================

        
        st.markdown("### Lifetime Performance")
        install_roi1, install_roi2, install_roi3, install_roi4 = st.columns(4)

        with install_roi1:
            render_kpi_card(
                "5. Years Since Install",
                f"{installed_years:,.1f}",
                pd.to_datetime(INSTALL_DATE).strftime("%b %d, %Y"),
                "#6b7280"
            )

        with install_roi2:
            render_kpi_card(
                "6. Since-Install Value",
                f"${since_install_labor_value:,.0f}",
                "Projected cumulative labor value",
                "#6b7280"
            )

        with install_roi3:
            render_kpi_card(
                "7. Since-Install Net",
                f"${since_install_net_value:,.0f}",
                "Value minus total cost",
                "#6b7280",
                value_color="#059669" if since_install_net_value >= 0 else "#dc2626"
            )

        with install_roi4:
            render_kpi_card(
                "8. Since-Install ROI",
                f"{since_install_roi_pct:,.1f}%" if since_install_roi_pct is not None else "N/A",
                "Estimated ROI since install",
                "#6b7280",
                value_color="#059669" if since_install_roi_pct is not None and since_install_roi_pct >= 0 else "#dc2626"
            )

        with st.expander("ROI Breakdown", expanded=False):
            st.markdown("### ROI Breakdown")
        
            if roi_mode == "Annualized Projection":
                payback_display = f"{payback_months:,.1f} months" if payback_months is not None else "Not available"
                roi_display = f"{roi_pct:,.1f}%" if roi_pct is not None else "N/A"
                since_install_roi_display = f"{since_install_roi_pct:,.1f}%" if since_install_roi_pct is not None else "N/A"
                
                st.markdown("### How to interpret Annualized metrics")
                render_explainer_card(
                    f"1. Annual Cost — ${total_roi_cost:,.0f}",
                    (
                        "This is the total recurring cost to operate the AMH for one full year."
                        f"<br><br>Inputs used:"
                        f"<br>• Yearly cost setting: <b>${YEARLY_COST:,.0f}</b>"
                        f"<br>• Monthly cost setting: <b>${MONTHLY_COST:,.0f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Annual cost = yearly cost + (monthly cost × 12)"
                        f"<br>• <b>${YEARLY_COST:,.0f} + (${MONTHLY_COST:,.0f} × 12) = ${total_roi_cost:,.0f}</b>"
                    ),
                    "#6b7280"
                )
                
                render_explainer_card(
                    f"2. Current Annual Run Rate — ${net_roi_value:,.0f}",
                    (
                        "This estimates how much net value the AMH is generating per year at its current usage level."
                        f"<br><br>What this represents:"
                        f"<br>• Based only on the selected date range"
                        f"<br>• Scaled up to a full 12-month equivalent"
                        f"<br>• Then reduced by annual operating cost"
                        f"<br><br>Inputs used:"
                        f"<br>• Selected range: <b>{days_in_range:,} days</b>"
                        f"<br>• Annual labor value (scaled): <b>${annual_labor_value:,.0f}</b>"
                        f"<br>• Annual operating cost: <b>${total_roi_cost:,.0f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Net run rate = annual labor value − annual operating cost"
                        f"<br>• <b>${annual_labor_value:,.0f} − ${total_roi_cost:,.0f} = ${net_roi_value:,.0f}</b>"
                        f"<br><br>This is a current performance estimate, not a guaranteed future outcome."
                    ),
                    "#10b981"
                )
                
                render_explainer_card(
                    f"3. Break-even Status — {break_even_value}",
                    (
                        "This shows whether the AMH has already recovered its upfront purchase cost."
                        f"<br><br>Inputs used:"
                        f"<br>• Upfront cost: <b>${UPFRONT_COST:,.0f}</b>"
                        f"<br>• Current annual run rate: <b>${net_roi_value:,.0f}</b>"
                        f"<br>• Years since install: <b>{installed_years:,.1f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Break-even years = upfront cost ÷ annual run rate"
                        f"<br>• Break-even point ≈ <b>{payback_months/12:,.1f} years</b>"
                        f"<br><br>Status:"
                        f"<br>• <b>{break_even_value}</b>"
                        f"<br>• {break_even_subtitle}"
                    ),
                    "#3b82f6"
                )
                
                render_explainer_card(
                    f"4. Lifetime Value Generated — ${since_install_labor_value:,.0f}",
                    (
                        "This estimates the total labor value created by the AMH over its full lifetime."
                        f"<br><br>Inputs used:"
                        f"<br>• Annual labor value (current pace): <b>${annual_labor_value:,.0f}</b>"
                        f"<br>• Years since install: <b>{installed_years:,.1f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Lifetime value = annual labor value × years since install"
                        f"<br>• <b>${annual_labor_value:,.0f} × {installed_years:,.1f} = ${since_install_labor_value:,.0f}</b>"
                        f"<br><br>This is an estimate using current performance applied across the system's lifespan."
                    ),
                    "#f59e0b"
                )
                st.markdown("### How to interpret Since Install metrics")
                render_explainer_card(
                    f"5. Years Since Install — {installed_years:,.1f}",
                    (
                        "This is the amount of time between the install date and today."
                        f"<br><br>Inputs used:"
                        f"<br>• Install date: <b>{pd.to_datetime(INSTALL_DATE).strftime('%b %d, %Y')}</b>"
                        f"<br>• Years in service: <b>{installed_years:,.1f}</b>"
                    ),
                    "#6b7280"
                )
        
                render_explainer_card(
                    f"6. Since-Install Value — ${since_install_labor_value:,.0f}",
                    (
                        "This estimates total labor value created over the machine's time in service."
                        f"<br><br>Inputs used:"
                        f"<br>• Annual labor value: <b>${annual_labor_value:,.0f}</b>"
                        f"<br>• Years since install: <b>{installed_years:,.1f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Since-install value = annual labor value × years since install"
                        f"<br>• <b>${since_install_labor_value:,.0f}</b>"
                    ),
                    "#3b82f6"
                )
        
                render_explainer_card(
                    f"7. Since-Install Net — ${since_install_net_value:,.0f}",
                    (
                        "This estimates total value since install after subtracting total cost since install."
                        f"<br><br>Inputs used:"
                        f"<br>• Since-install labor value: <b>${since_install_labor_value:,.0f}</b>"
                        f"<br>• Since-install total cost: <b>${since_install_total_cost:,.0f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Since-install net = since-install labor value − since-install total cost"
                        f"<br>• <b>${since_install_labor_value:,.0f} − ${since_install_total_cost:,.0f} = ${since_install_net_value:,.0f}</b>"
                    ),
                    "#10b981"
                )
        
                render_explainer_card(
                    f"8. Since-Install ROI — {since_install_roi_display}",
                    (
                        "This compares since-install net value against since-install total cost."
                        f"<br><br>Inputs used:"
                        f"<br>• Since-install net value: <b>${since_install_net_value:,.0f}</b>"
                        f"<br>• Since-install total cost: <b>${since_install_total_cost:,.0f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Since-install ROI = since-install net value ÷ since-install total cost × 100"
                        f"<br>• <b>{since_install_roi_display}</b>"
                    ),
                    "#7c3aed"
                )
        
            else:
                since_install_roi_display = f"{since_install_roi_pct:,.1f}%" if since_install_roi_pct is not None else "N/A"
        
                render_explainer_card(
                    f"Range Length — {days_in_range:,} days",
                    (
                        "This is the exact selected date range used for the observed calculation."
                        f"<br><br>Inputs used:"
                        f"<br>• Start date: <b>{pd.to_datetime(start_date).strftime('%b %d, %Y')}</b>"
                        f"<br>• End date: <b>{pd.to_datetime(end_date).strftime('%b %d, %Y')}</b>"
                        f"<br>• Total days: <b>{days_in_range:,}</b>"
                        f"<br>• Equivalent months: <b>{months_in_range:,.2f}</b>"
                        f"<br>• Equivalent years: <b>{years_in_range:,.4f}</b>"
                        "<br><br>All observed values below use only this selected period."
                    ),
                    "#6b7280"
                )
        
                render_explainer_card(
                    f"Observed Labor Value — ${labor_value_saved:,.0f}",
                    (
                        "This is the estimated dollar value of staff time saved during the selected date range."
                        f"<br><br>Inputs used:"
                        f"<br>• Hourly labor cost setting (User provided): <b>${HOURLY_COST:,.2f}</b> per hour"
                        f"<br>• Calculated observed labor value: <b>${labor_value_saved:,.0f}</b>"
                        f"<br><br>Meaning:"
                        f"<br>• This is the labor value created by the AMH during just this selected period."
                    ),
                    "#3b82f6"
                )
        
                render_explainer_card(
                    f"Observed Operating Cost — ${observed_operating_cost:,.0f}",
                    (
                        "This is the prorated recurring operating cost for only the selected date range."
                        f"<br><br>Inputs used:"
                        f"<br>• Yearly recurring cost setting (User provided): <b>${YEARLY_COST:,.0f}</b>"
                        f"<br>• Monthly recurring cost setting (User provided): <b>${MONTHLY_COST:,.0f}</b>"
                        f"<br>• Selected days: <b>{days_in_range:,}</b>"
                        f"<br>• Equivalent months: <b>{months_in_range:,.2f}</b>"
                        f"<br>• Equivalent years: <b>{years_in_range:,.4f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Monthly portion = ${MONTHLY_COST:,.0f} × {months_in_range:,.2f}"
                        f"<br>• Yearly portion = ${YEARLY_COST:,.0f} × {years_in_range:,.4f}"
                        f"<br><br>Final observed operating cost:"
                        f"<br>• <b>${observed_operating_cost:,.0f}</b>"
                        "<br><br>This does not include the original upfront purchase cost."
                    ),
                    "#f59e0b"
                )
        
                render_explainer_card(
                    f"Observed Net Value — ${observed_net_operating_value:,.0f}",
                    (
                        "This is the value left after subtracting observed operating cost from observed labor value."
                        f"<br><br>Inputs used:"
                        f"<br>• Observed labor value: <b>${labor_value_saved:,.0f}</b>"
                        f"<br>• Observed operating cost: <b>${observed_operating_cost:,.0f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Observed net value = observed labor value − observed operating cost"
                        f"<br>• <b>${labor_value_saved:,.0f} − ${observed_operating_cost:,.0f} = ${observed_net_operating_value:,.0f}</b>"
                    ),
                    "#10b981"
                )
        
                render_explainer_card(
                    f"Years Since Install — {installed_years:,.1f}",
                    (
                        "This is the amount of time between the install date and today."
                        f"<br><br>Inputs used:"
                        f"<br>• Install date: <b>{pd.to_datetime(INSTALL_DATE).strftime('%b %d, %Y')}</b>"
                        f"<br>• Years in service: <b>{installed_years:,.1f}</b>"
                    ),
                    "#6b7280"
                )
        
                render_explainer_card(
                    f"Since-Install Value — ${since_install_labor_value:,.0f}",
                    (
                        "This estimates total labor value created over the machine's time in service."
                        f"<br><br>Inputs used:"
                        f"<br>• Annual labor value: <b>${annual_labor_value:,.0f}</b>"
                        f"<br>• Years since install: <b>{installed_years:,.1f}</b>"
                        f"<br><br>Final estimated since-install labor value:"
                        f"<br>• <b>${since_install_labor_value:,.0f}</b>"
                    ),
                    "#3b82f6"
                )
        
                render_explainer_card(
                    f"Since-Install Net — ${since_install_net_value:,.0f}",
                    (
                        "This estimates total value since install after subtracting total cost since install."
                        f"<br><br>Inputs used:"
                        f"<br>• Since-install labor value: <b>${since_install_labor_value:,.0f}</b>"
                        f"<br>• Since-install total cost: <b>${since_install_total_cost:,.0f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Since-install net = since-install labor value − since-install total cost"
                        f"<br>• <b>${since_install_labor_value:,.0f} − ${since_install_total_cost:,.0f} = ${since_install_net_value:,.0f}</b>"
                    ),
                    "#10b981"
                )
        
                render_explainer_card(
                    f"Since-Install ROI — {since_install_roi_display}",
                    (
                        "This compares since-install net value against since-install total cost."
                        f"<br><br>Inputs used:"
                        f"<br>• Since-install net value: <b>${since_install_net_value:,.0f}</b>"
                        f"<br>• Since-install total cost: <b>${since_install_total_cost:,.0f}</b>"
                        f"<br><br>Math:"
                        f"<br>• Since-install ROI = since-install net value ÷ since-install total cost × 100"
                        f"<br>• <b>{since_install_roi_display}</b>"
                    ),
                    "#7c3aed"
                )


        with st.expander("How ROI is Calculated", expanded=False):           
            st.markdown(f"""
**The following formulas are used to calculate the ROI metrics shown above:**

- Observed labor value = Observed hours saved × Hourly labor cost  
- Observed operating cost = Observed monthly cost + Observed yearly cost  
- Observed net value = Observed labor value − Observed operating cost  

- Annual labor value = Observed labor value × (12 ÷ Equivalent months)  
- Annual cost = Yearly cost + (Monthly cost × 12)  
- Current annual run rate = Annual labor value − Annual cost  
- Break-even years = Upfront cost ÷ Current annual run rate  

- Since-install value = Annual labor value × Years since install  
- Since-install operating cost = Annual operating cost × Years since install  
- Since-install total cost = Upfront cost + Since-install operating cost  
- Since-install net = Since-install value − Since-install total cost  
- Since-install ROI = Since-install net ÷ Since-install total cost × 100  

The formulas above are built on a sequence of dependent calculations. Each value is derived from the values before it:

Observed Labor Value + Observed Operating Cost  
→ Observed Net Value  

Observed Labor Value  
→ Annual Labor Value  

Annual Labor Value + Annual Cost  
→ Current Annual Run Rate  
→ Break-even Status  

Annual Labor Value + Years Since Install  
→ Since-Install Value  

Since-Install Value + Since-Install Total Cost  
→ Since-Install Net  
→ Since-Install ROI  

---

#### 1. Selected date range
This ROI calculation uses data from the selected reporting window.

Selected date range = **{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}**

Total days in range = **{days_in_range:,} days**

Equivalent months = Total days in range ÷ 30.44 days/month  

Equivalent months = {days_in_range:,} days ÷ 30.44 days/month  

**Equivalent months = {months_in_range:,.2f} months**

Equivalent years = Total days in range ÷ 365.25 days/year  

Equivalent years = {days_in_range:,} days ÷ 365.25 days/year  

**Equivalent years = {years_in_range:,.4f} years**

---

#### 2. Observed labor value
This is the observed dollar value of staff time avoided during the selected range.

*Refer to How Staff Time Saved Is Calculated in Labor & Efficiency for the full calculation of observed hours saved.*

Observed hours saved = **{observed_hours_saved:,.2f} hours**

Hourly labor cost = **${HOURLY_COST:.2f}/hour**

Observed labor value = Observed hours saved × Hourly labor cost  

Observed labor value = {observed_hours_saved:,.2f} hours × ${HOURLY_COST:.2f}/hour  

**Observed labor value = ${labor_value_saved:,.0f}**

---

#### 3. Observed operating cost
This is the recurring operating cost assigned only to the selected date range.

Observed monthly cost = Monthly cost × Equivalent months  

Observed monthly cost = ${MONTHLY_COST:,.2f}/month × {months_in_range:,.2f} months  

**Observed monthly cost = ${MONTHLY_COST * months_in_range:,.0f}**

Observed yearly cost = Yearly cost × Equivalent years  

Observed yearly cost = ${YEARLY_COST:,.2f}/year × {years_in_range:,.4f} years  

**Observed yearly cost = ${YEARLY_COST * years_in_range:,.0f}**

Observed operating cost = Observed monthly cost + Observed yearly cost  

Observed operating cost = ${MONTHLY_COST * months_in_range:,.0f} + ${YEARLY_COST * years_in_range:,.0f}  

**Observed operating cost = ${observed_operating_cost:,.0f}**

---

#### 4. Observed net value
This is the remaining observed value after subtracting recurring operating cost for the selected range.

Observed net value = Observed labor value − Observed operating cost  

Observed net value = ${labor_value_saved:,.0f} − ${observed_operating_cost:,.0f}  

**Observed net value = ${observed_net_operating_value:,.0f}**

---

#### 5. Annual labor value
This scales the observed labor value from the selected range to a 12-month equivalent.

Annual labor value = Observed labor value × (12 ÷ Equivalent months)  

Annual labor value = ${labor_value_saved:,.0f} × (12 ÷ {months_in_range:,.2f})  

**Annual labor value = ${annual_labor_value:,.0f}/year**

---

#### 6. Annual cost
This is the full recurring operating cost for one year.

Annual monthly cost = Monthly cost × 12 months  

Annual monthly cost = ${MONTHLY_COST:,.2f}/month × 12 months  

**Annual monthly cost = ${MONTHLY_COST * 12:,.0f}**

Annual cost = Yearly cost + Annual monthly cost  

Annual cost = ${YEARLY_COST:,.0f} + ${MONTHLY_COST * 12:,.0f}  

**Annual cost = ${annual_operating_cost:,.0f}/year**

---

#### 7. Current annual run rate
This is the projected annual value left after subtracting annual recurring cost.

Current annual run rate = Annual labor value − Annual cost  

Current annual run rate = ${annual_labor_value:,.0f} − ${annual_operating_cost:,.0f}  

**Current annual run rate = ${net_roi_value:,.0f}/year**

---

#### 8. Break-even status
This checks whether the current annual run rate is enough to recover the upfront cost, and whether that break-even point has already passed.

Upfront cost = **${UPFRONT_COST:,.0f}**

Break-even years = Upfront cost ÷ Current annual run rate  

Break-even years = ${UPFRONT_COST:,.0f} ÷ ${net_roi_value:,.0f}
""")

            if payback_months is not None:
                st.markdown(f"""
**Break-even years = {payback_months / 12:,.1f} years**

Years since install = **{installed_years:,.1f} years**

Years past break-even = Years since install − Break-even years  

Years past break-even = {installed_years:,.1f} − {payback_months / 12:,.1f}  

**Break-even status = {break_even_value}**  
{break_even_subtitle}
""")
            else:
                st.markdown(f"""
Break-even years cannot be calculated because the current annual run rate is not positive.

Years since install = **{installed_years:,.1f} years**

**Break-even status = Not Reached**
""")

            st.markdown(f"""
---

#### 9. Years since install
This is the elapsed time from the install date to today.

Install date = **{pd.to_datetime(INSTALL_DATE).strftime('%b %d, %Y')}**

Years since install = **{installed_years:,.1f} years**

---

#### 10. Since-install value
This is the total projected labor value across the machine's time in service.

Since-install value = Annual labor value × Years since install  

Since-install value = ${annual_labor_value:,.0f}/year × {installed_years:,.1f} years  

**Since-install value = ${since_install_labor_value:,.0f}**

---

#### 11. Since-install operating cost
This is the recurring operating cost accumulated across the installed life.

Since-install operating cost = Annual operating cost × Years since install  

Since-install operating cost = ${annual_operating_cost:,.0f}/year × {installed_years:,.1f} years  

**Since-install operating cost = ${since_install_operating_cost:,.0f}**
""")

            if INCLUDE_UPFRONT_IN_SINCE_INSTALL:
                st.markdown(f"""
---

#### 12. Since-install total cost
This includes both recurring operating cost and the original upfront purchase cost.

Since-install total cost = Upfront cost + Since-install operating cost  

Since-install total cost = ${UPFRONT_COST:,.0f} + ${since_install_operating_cost:,.0f}  

**Since-install total cost = ${since_install_total_cost:,.0f}**
""")
            else:
                st.markdown(f"""
---

#### 12. Since-install total cost
This includes recurring operating cost only.

Since-install total cost = Since-install operating cost  

Since-install total cost = ${since_install_operating_cost:,.0f}  

**Since-install total cost = ${since_install_total_cost:,.0f}**
""")

            st.markdown(f"""
---

#### 13. Since-install net
This is the remaining value after subtracting total cost since install.

Since-install net = Since-install value − Since-install total cost  

Since-install net = ${since_install_labor_value:,.0f} − ${since_install_total_cost:,.0f}  

**Since-install net = ${since_install_net_value:,.0f}**

---

#### 14. Since-install ROI
This compares since-install net value against since-install total cost.

Since-install ROI = Since-install net ÷ Since-install total cost × 100  

Since-install ROI = ${since_install_net_value:,.0f} ÷ ${since_install_total_cost:,.0f} × 100  

**Since-install ROI = {since_install_roi_pct:,.1f}%**
""")

    

    else:
        if st.session_state.get("roi_calculated", False):
            st.info("No ROI data is available for the selected date range.")
        else:
            st.info("Enter your assumptions above, then click Calculate ROI.")

with st.expander("Staff Time Equivalent", expanded=False):
    st.caption("Estimates staff time saved by comparing manual processing time against observed AMH processing time.")

    MANUAL_RATE = 45

    if len(df) > 0 and len(df_history_raw) > 0:
        rate_df = df.copy()
        rate_df["date"] = rate_df["datetime"].dt.date
        rate_df["hour"] = rate_df["datetime"].dt.hour

        daily_hourly = (
            rate_df.groupby(["date", "hour"])
            .size()
            .reset_index(name="checkins")
        )

        avg_hourly = (
            daily_hourly.groupby("hour")["checkins"]
            .mean()
            .reset_index(name="avg_items_per_hour")
        )

        if len(avg_hourly) > 0:
            peak_row = avg_hourly.loc[avg_hourly["avg_items_per_hour"].idxmax()]
            threshold = peak_row["avg_items_per_hour"] * 0.75
            peak_hours = avg_hourly[avg_hourly["avg_items_per_hour"] >= threshold].copy()

            AMH_RATE = (
                peak_hours["avg_items_per_hour"].mean()
                if len(peak_hours) > 0
                else peak_row["avg_items_per_hour"]
            )
        else:
            AMH_RATE = 130.0

        daily_counts = df["datetime"].dt.date.value_counts().sort_index()
        staff_df = daily_counts.reset_index()
        staff_df.columns = ["date", "checkins"]

        staff_df["manual_hours"] = staff_df["checkins"] / MANUAL_RATE
        staff_df["amh_hours"] = staff_df["checkins"] / AMH_RATE
        staff_df["hours_saved"] = (staff_df["manual_hours"] - staff_df["amh_hours"]).clip(lower=0)

        avg_daily_checkins = staff_df["checkins"].mean()
        avg_daily_manual_hours = staff_df["manual_hours"].mean()
        avg_daily_amh_hours = staff_df["amh_hours"].mean()
        avg_saved = staff_df["hours_saved"].mean()
        total_saved = staff_df["hours_saved"].sum()
        peak_day = staff_df.loc[staff_df["hours_saved"].idxmax()]
        labor_value_saved = total_saved * HOURLY_COST

        try:
            from report_export import build_director_report_pdf

            director_pdf = build_director_report_pdf(
                start_date=start_date,
                end_date=end_date,
                df=df,
                rejects_df=rejects_df,
                overall_metrics=overall_metrics,
                top_issue=top_issue,
                attention_text=attention_text,
                avg_hours_saved=avg_saved,
                total_hours_saved=total_saved,
                peak_day_saved=float(peak_day["hours_saved"]),
                peak_day_saved_date=pd.to_datetime(peak_day["date"]).strftime("%b %d, %Y"),
                manual_rate=MANUAL_RATE,
                amh_rate=AMH_RATE,
                library_name=LIBRARY_NAME,
                branch_name=BRANCH_NAME,
                system_name=SYSTEM_NAME,
                report_title="AMH Director Report",
                hourly_cost=HOURLY_COST,
                roi_mode=roi_mode,
                annual_cost=annual_operating_cost if roi_payload else None,
                yearly_savings_after_cost=net_roi_value if roi_payload and roi_mode == "Annualized Projection" else None,
                payback_months=payback_months if roi_payload and roi_mode == "Annualized Projection" else None,
                since_install_net_value=since_install_net_value if roi_payload else None,
                install_date=pd.to_datetime(INSTALL_DATE).strftime("%b %d, %Y") if roi_payload else None,
            )

            pdf_button_placeholder.download_button(
                label="Download Director PDF",
                data=director_pdf,
                file_name=f"amh_director_report_{pd.to_datetime(start_date).strftime('%Y%m%d')}_{pd.to_datetime(end_date).strftime('%Y%m%d')}.pdf",
                mime="application/pdf",
                key="director_pdf_download"
            )
        except Exception as e:
            pdf_button_placeholder.warning(f"Director PDF export is temporarily unavailable: {e}")

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
                    Using a manual processing rate of {MANUAL_RATE:.0f} items/hour and an observed AMH rate of
                    {AMH_RATE:.1f} items/hour, the average daily staff time saved in the selected range was
                    {avg_saved:,.2f} hours.
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
        
        k1, k2, k3 = st.columns(3)
        
        with k1:
            render_kpi_card(
                "1. Avg Hours Saved",
                f"{avg_saved:,.2f}",
                "Per day Across selected date range",
                "#6b7280"
            )
        
        with k2:
            render_kpi_card(
                "2. Total Hours Saved",
                f"{total_saved:,.2f}",
                "Across selected date range",
                "#6b7280"
            )
        
        with k3:
            render_kpi_card(
                "3. Estimated Labor Value for selected date range",
                f"${labor_value_saved:,.0f}",
                "Staff time avoided value",
                "#6b7280"
            )

if len(df) > 0 and len(df_history_raw) > 0:
    with st.expander("How the Staff Time Equivalent KPIs are calculated", expanded=False):
        st.info(f"""
##### 1. Average Hours Saved {avg_saved:,.2f}

###### Average Daily Check-ins = Total check-ins / Total days

- Average Daily Check-ins
= {int(staff_df["checkins"].sum()):,} / {staff_df["date"].nunique():,}

- Average Daily Check-ins
= {avg_daily_checkins:,.1f} items/day

###### Average Daily Manual Time = Average Daily Check-ins / Manual processing rate

- Average Daily Manual Time
= {avg_daily_checkins:,.1f} / {MANUAL_RATE:.1f}

- Average Daily Manual Time
= {avg_daily_manual_hours:,.2f} staff hours/day

###### Average Daily AMH Time = Average Daily Check-ins / AMH processing rate

- Average Daily AMH Time
= {avg_daily_checkins:,.1f} / {AMH_RATE:,.1f}

- Average Daily AMH Time
= {avg_daily_amh_hours:,.2f} machine hours/day

###### Average Hours Saved per Day = Average Daily Manual Time - Average Daily AMH Time

- Average Hours Saved per Day
= {avg_daily_manual_hours:,.2f} - {avg_daily_amh_hours:,.2f}

- **Average Hours Saved per Day
= {avg_saved:,.2f} staff hours/day**

##### 2. Total Hours Saved

- Total Hours Saved
= Average Hours Saved per Day * Total days

- Total Hours Saved
= {avg_saved:,.2f} * {staff_df["date"].nunique():,}

- Total Hours Saved
= {total_saved:,.2f} hours

##### 3. Estimated Labor Value

- Estimated Labor Value
= Total Hours Saved * Hourly labor cost

- Estimated Labor Value
= {total_saved:,.2f} * ${HOURLY_COST:.2f}

- Estimated Labor Value
= ${labor_value_saved:,.0f}
""")

    with st.expander("Processing rates and supporting methodology", expanded=False):
        st.info(f"""
### How Staff Time Saved Is Calculated

The calculation flow is:

Average Daily Check-ins
-> Manual Processing Rate and AMH Processing Rate
-> Manual Processing Time and AMH Processing Time
-> Average Hours Saved per Day
-> Total Hours Saved
-> Estimated Labor Value

#### Manual Processing Rate

The manual processing rate is calculated using Westside circulation activity reports in TLC from four months:

- March 2026
- June 2025
- August 2025
- September 2025

Each monthly sheet was processed using the same method.

##### Step 1: Group transactions into hourly activity

For each sheet, every check-in transaction timestamp was converted into:

- a calendar date
- an hour of the day

Transactions were then grouped by date and hour so that each day had an hourly check-in count.

##### Step 2: Find each day's peak operating threshold

For each individual day in a monthly sheet:

- the highest hourly check-in count for that day was identified
- a peak threshold was calculated as 75% of that day's maximum hourly count

Peak threshold = Daily maximum hourly check-ins * 0.75

Only hours meeting or exceeding that threshold were counted as peak manual operating hours for that day.

##### Step 3: Sum peak-hour counts within each month

The results by month were:

March 2026
- Peak manual check-ins = 2,343 items
- Peak manual hours = 51 hours
- Monthly manual rate = 2,343 / 51
- Monthly manual rate = 45.94 items/hour

June 2025
- Peak manual check-ins = 2,000 items
- Peak manual hours = 45 hours
- Monthly manual rate = 2,000 / 45
- Monthly manual rate = 44.44 items/hour

August 2025
- Peak manual check-ins = 3,058 items
- Peak manual hours = 60 hours
- Monthly manual rate = 3,058 / 60
- Monthly manual rate = 50.97 items/hour

September 2025
- Peak manual check-ins = 2,627 items
- Peak manual hours = 57 hours
- Monthly manual rate = 2,627 / 57
- Monthly manual rate = 46.09 items/hour

##### Step 4: Combine all monthly peak-hour data

Combined peak manual check-ins
= 2,343 + 2,000 + 3,058 + 2,627
= 10,028 items

Combined peak manual hours
= 51 + 45 + 60 + 57
= 213 hours

Manual processing rate
= Combined peak manual check-ins / Combined peak manual hours
= 10,028 / 213
= {MANUAL_RATE:.1f} items/hour

#### AMH Processing Rate

The AMH processing rate is calculated from AMH check-in history within the currently selected date range shown in the report.

##### Step 1: Group AMH activity into hourly throughput

AMH check-ins are grouped by:

- date
- hour

This creates an hourly item count for each day in the selected range.

##### Step 2: Build the AMH hourly average profile

Those daily hourly counts are then averaged by hour of day to estimate the machine's typical throughput at each hour.

##### Step 3: Identify peak machine operating hours

From that hourly AMH profile:

- the highest observed hourly average is identified
- a peak threshold is calculated at 75% of that maximum

Highest observed AMH hourly average = {peak_row["avg_items_per_hour"]:,.1f} items/hour

Peak AMH threshold
= {peak_row["avg_items_per_hour"]:,.1f} * 0.75
= {threshold:,.1f} items/hour

Only AMH hours meeting or exceeding that threshold are used in the final AMH rate.

##### Step 4: Compute AMH processing rate

AMH processing rate = {AMH_RATE:,.1f} items/hour

#### Supporting Daily Inputs

Average Daily Check-ins
= Total check-ins / Total days
= {int(staff_df["checkins"].sum()):,} / {staff_df["date"].nunique():,}
= {avg_daily_checkins:,.1f} items/day

Daily Manual Time
= Average Daily Check-ins / Manual processing rate
= {avg_daily_checkins:,.1f} / {MANUAL_RATE:.1f}
= {avg_daily_manual_hours:,.2f} staff hours/day

Daily AMH Time
= Average Daily Check-ins / AMH processing rate
= {avg_daily_checkins:,.1f} / {AMH_RATE:,.1f}
= {avg_daily_amh_hours:,.2f} machine hours/day
""")
else:
    st.info("No labor data is available for the selected date range.")





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
            download_button(dow_display, "weekday_volume.csv")
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
            download_button(
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
        download_button(display_df, "throughput_report.csv")

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
        download_button(weekday_display, "throughput_by_weekday_report.csv")

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
        download_button(display_df, "today_vs_typical_hourly_pattern.csv")
    else:
        st.info("Not enough data available to compare today versus the typical hourly pattern.")



# -----------------------------
# Routing & Destinations
# -----------------------------
st.subheader("Routing & Destinations")
st.caption("Shows where items are being sent after check-in and highlights routing concentration.")

with st.expander("Destination Breakdown", expanded=False):
    
    destination_counts = (
        df["destination_report"]
        .value_counts()
        .reset_index()
    )
    destination_counts.columns = ["destination", "count"]
    destination_counts = destination_counts.sort_values("count", ascending=False)
    if len(destination_counts) > 0:
        top_destination_row = destination_counts.loc[destination_counts["count"].idxmax()]
        top_destination_pct = (top_destination_row["count"] / destination_counts["count"].sum()) * 100

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
                    Top destination: {top_destination_row["destination"]} with {int(top_destination_row["count"]):,} items
                    ({top_destination_pct:.1f}% of all checkins).
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

        destination_chart = (
            alt.Chart(destination_counts)
            .mark_bar()
            .encode(
                x=alt.X(
                    "destination:N",
                    sort=destination_counts["destination"].tolist(),
                    title="Destination",
                    axis=alt.Axis(labelAngle=0)
                ),
                y=alt.Y("count:Q", title="Items"),
                tooltip=["destination", "count"]
            )
            .properties(height=350)
        )

        render_chart(destination_chart)

        st.dataframe(destination_counts, use_container_width=True)
        download_button(destination_counts, "destination_breakdown.csv")
    else:
        st.info("No destination data available for the selected date range.")




with st.expander("Bin Volume", expanded=False):
    if "bin" not in df.columns:
        st.warning("No bin column found in the current dataset. Add bin parsing to your cleaned checkins file first.")
    else:
        bin_df = df.copy()
        bin_df = bin_df[bin_df["bin"].notna()].copy()
        bin_df["bin"] = bin_df["bin"].astype(str)

        bin_summary = (
            bin_df["bin"]
            .value_counts()
            .sort_index()
            .reset_index()
        )
        bin_summary.columns = ["bin", "checkins"]
        bin_summary["pct_of_total"] = (bin_summary["checkins"] / bin_summary["checkins"].sum() * 100).round(2)

        top_bin_row = bin_summary.loc[bin_summary["checkins"].idxmax()]
        low_bin_row = bin_summary.loc[bin_summary["checkins"].idxmin()]
        bin_discrepancy = int(top_bin_row["checkins"] - low_bin_row["checkins"])

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
                    Most-used bin: {top_bin_row["bin"]} with {int(top_bin_row["checkins"]):,} items
                    ({top_bin_row["pct_of_total"]:.2f}% of all binned checkins).
                    Lowest-volume bin: {low_bin_row["bin"]} with {int(low_bin_row["checkins"]):,} items.
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
        
        k1, k2, k3, k4 = st.columns(4)
        
        with k1:
            render_kpi_card(
                "Binned Checkins",
                f"{int(bin_summary['checkins'].sum()):,}",
                "Items with a detected bin",
                "#6b7280"
            )
        
        with k2:
            render_kpi_card(
                "Top Bin",
                f"Bin {top_bin_row['bin']}",
                f"{int(top_bin_row['checkins']):,} items",
                "#6b7280",
                value_font_size="1.4rem"
            )
        
        with k3:
            render_kpi_card(
                "Top Bin Share",
                f"{top_bin_row['pct_of_total']:.2f}%",
                "Of all binned checkins",
                "#6b7280",
                value_font_size="1.4rem"
            )
        
        with k4:
            render_kpi_card(
                "Bin Discrepancy",
                f"{top_bin_row['pct_of_total'] - low_bin_row['pct_of_total']:.2f}% gap",
                f"Between highest and lowest bin share<br>"
                f"Bin {top_bin_row['bin']} leads ({top_bin_row['pct_of_total']:.2f}%) •"
                f"Bin {low_bin_row['bin']} lowest ({low_bin_row['pct_of_total']:.2f}%)",
                "#6b7280",
                value_font_size="1.4rem"
            )

        bin_volume_display = bin_summary.rename(columns={
            "bin": "Bin",
            "checkins": "Checkins",
            "pct_of_total": "% of Total"
        })

        st.dataframe(bin_volume_display, use_container_width=True)
        download_button(bin_volume_display, "bin_volume_report.csv")

        hour_range = list(range(7, 21))

        bin_hourly_source = bin_df.copy()
        bin_hourly_source["date"] = bin_hourly_source["datetime"].dt.date
        bin_hourly_source["hour"] = bin_hourly_source["datetime"].dt.hour

        daily_bin_hour = (
            bin_hourly_source.groupby(["date", "hour", "bin"])
            .size()
            .reset_index(name="daily_checkins")
        )

        avg_bin_hour = (
            daily_bin_hour.groupby(["hour", "bin"])["daily_checkins"]
            .mean()
            .reset_index(name="avg_checkins")
        )

        total_bin_hour = (
            bin_hourly_source.groupby(["hour", "bin"])
            .size()
            .reset_index(name="total_checkins")
        )

        hourly_bin_summary = total_bin_hour.merge(
            avg_bin_hour,
            on=["hour", "bin"],
            how="left"
        )

        if len(hourly_bin_summary) > 0:
            st.subheader("Bin Volume by Hour")

            hourly_bin_summary = hourly_bin_summary[
                (hourly_bin_summary["hour"] >= 7) & (hourly_bin_summary["hour"] <= 20)
            ].copy()

            hourly_bin_summary["hour_label"] = hourly_bin_summary["hour"].apply(format_hour_plain)
            hourly_bin_summary["bin_label"] = hourly_bin_summary["bin"].apply(lambda b: f"Bin {b}")

            bin_chart = (
                alt.Chart(hourly_bin_summary)
                .mark_line(point=False)
                .encode(
                    x=alt.X(
                        "hour_label:N",
                        sort=[format_hour_plain(h) for h in hour_range],
                        title="Hour",
                        axis=alt.Axis(labelAngle=0)
                    ),
                    y=alt.Y("avg_checkins:Q", title="Avg Checkins Per Hour"),
                    color=alt.Color("bin_label:N", title="Bin"),
                    tooltip=[
                        "hour_label",
                        "bin_label",
                        alt.Tooltip("avg_checkins:Q", title="Avg Checkins", format=".1f"),
                        alt.Tooltip("total_checkins:Q", title="Total Checkins")
                    ]
                )
                .properties(height=350)
                .interactive(False)
            )

            st.altair_chart(bin_chart, use_container_width=True)

            hourly_bin_display = hourly_bin_summary.pivot_table(
                index="hour_label",
                columns="bin_label",
                values=["total_checkins", "avg_checkins"],
                fill_value=0
            )

            hourly_bin_display.columns = [
                f"{bin_name} Total" if metric == "total_checkins" else f"{bin_name} Avg/Day"
                for metric, bin_name in hourly_bin_display.columns
            ]

            hourly_bin_display = hourly_bin_display.reset_index().rename(columns={"hour_label": "Hour"})

            avg_cols = [col for col in hourly_bin_display.columns if col.endswith("Avg/Day")]
            for col in avg_cols:
                hourly_bin_display[col] = hourly_bin_display[col].round(1)

            st.dataframe(hourly_bin_display, use_container_width=True)
            download_button(hourly_bin_display, "bin_volume_by_hour_report.csv")

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
        download_button(reject_counts, "reject_reasons.csv")
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

        annual_col1, annual_col2 = st.columns(2)
        
        annual_roi_payload = build_roi_payload(
            df,
            start_date,
            end_date,
            hourly_cost=st.session_state.get("roi_hourly_cost", 18.0),
            upfront_cost=st.session_state.get("roi_upfront_cost", 200000.0),
            monthly_cost=st.session_state.get("roi_monthly_cost", 0.0),
            yearly_cost=st.session_state.get("roi_yearly_cost", 8000.0),
        )
        
        with annual_col1:
            annual_labor_value_text = "N/A"
            if annual_roi_payload and annual_roi_payload.get("annual_labor_value") is not None:
                annual_labor_value_text = f"${annual_roi_payload['annual_labor_value']:,.0f}"
        
            render_kpi_card(
                "Annual Labor Value",
                annual_labor_value_text,
                "Projected yearly labor value",
                "#6b7280"
            )
        
        with annual_col2:
            annual_operating_cost_text = "N/A"
            if annual_roi_payload and annual_roi_payload.get("annual_operating_cost") is not None:
                annual_operating_cost_text = f"${annual_roi_payload['annual_operating_cost']:,.0f}"
        
            render_kpi_card(
                "Annual Operating Cost",
                annual_operating_cost_text,
                "Monthly + yearly recurring cost",
                "#6b7280"
            )
        
        if len(overflow_daily) > 0:
            st.subheader("Exception Bin Rate by Day")
            chart_df = overflow_daily["exception_rate_pct"]
            st.line_chart(chart_df)

            overflow_daily_display = overflow_daily.reset_index().rename(columns={"index": "date"})
            st.dataframe(overflow_daily_display, use_container_width=True)
            download_button(
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
                download_button(
                    hourly_exception_display,
                    "exception_bin_volume_by_hour_report.csv",
                    key="exception_bin_volume_by_hour_report_download"
                )
            else:
                st.info("No exception-bin items found for the selected date range.")
        else:
            st.info("No exception-bin items found for the selected date range.")
