import pandas as pd
import streamlit as st

from metrics import build_roi_payload
from ui_components import render_kpi_card


def fmt_money(value, decimals=0):
    if value is None:
        return "N/A"
    return f"${value:,.{decimals}f}"


def fmt_pct(value, decimals=1):
    if value is None:
        return "N/A"
    return f"{value:,.{decimals}f}%"


def render_labor_efficiency_section(
    df,
    df_history_raw,
    rejects_df,
    start_date,
    end_date,
    overall_metrics,
    top_issue,
    attention_text,
    LIBRARY_NAME,
    BRANCH_NAME,
    SYSTEM_NAME,
    pdf_button_placeholder,
    gated_pdf_download,
):
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
                install_date=INSTALL_DATE,
                include_upfront_in_since_install=INCLUDE_UPFRONT_IN_SINCE_INSTALL,
            )
                
        if roi_payload:
            net_roi_value = roi_payload["net_roi_value"]
            total_roi_cost = roi_payload["total_roi_cost"]
            payback_months = roi_payload["payback_months"]
        
            annual_labor_value = roi_payload["annual_labor_value"]
            annual_operating_cost = roi_payload["annual_operating_cost"]
        
            labor_value_saved = roi_payload["labor_value_saved"]
            observed_operating_cost = roi_payload["observed_operating_cost"]
            observed_net_operating_value = roi_payload["observed_net_operating_value"]
            observed_hours_saved = roi_payload["total_saved_hours"]
        
            days_in_range = roi_payload["days_in_range"]
            months_in_range = roi_payload["months_in_range"]
            years_in_range = roi_payload["years_in_range"]
        
            installed_years = roi_payload["installed_years"]
            since_install_labor_value = roi_payload["since_install_labor_value"]
            since_install_operating_cost = roi_payload["since_install_operating_cost"]
            since_install_total_cost = roi_payload["since_install_total_cost"]
            since_install_net_value = roi_payload["since_install_net_value"]
            since_install_roi_pct = roi_payload["since_install_roi_pct"]
        
            break_even_value = "Not Reached"
            break_even_subtitle = "Current annual run rate does not recover upfront cost"
            break_even_color = "#dc2626"
        
            if payback_months is not None and installed_years is not None:
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
        
            since_install_roi_display = fmt_pct(since_install_roi_pct)
        
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
                        fmt_money(net_roi_value),
                        f"Based on last {days_in_range:,} days of activity",
                        "#6b7280",
                        value_color="#059669" if net_roi_value >= 0 else "#dc2626"
                    )
    
                with roi3:
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
                    fmt_money(since_install_net_value),
                    "Value minus total cost",
                    "#6b7280",
                    value_color="#059669" if since_install_net_value >= 0 else "#dc2626"
                )
    
            with install_roi4:
                render_kpi_card(
                    "8. Since-Install ROI",
                    fmt_pct(since_install_roi_pct) if since_install_roi_pct is not None else "N/A",
                    "Estimated ROI since install",
                    "#6b7280",
                    value_color="#059669" if since_install_roi_pct is not None and since_install_roi_pct >= 0 else "#dc2626"
                )
    
            with st.expander("ROI Breakdown", expanded=False):
                st.markdown("### ROI Breakdown")
            
                if roi_mode == "Annualized Projection":
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
                    payback_years_display = f"{payback_months / 12:,.1f} years" if payback_months is not None else "Not available"
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
                            f"<br>• Break-even point ≈ <b>{payback_years_display}</b>"
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
    
    **Since-install ROI = {since_install_roi_display}**
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
    
                gated_pdf_download(
                    director_pdf,
                    f"amh_director_report_{pd.to_datetime(start_date).strftime('%Y%m%d')}_{pd.to_datetime(end_date).strftime('%Y%m%d')}.pdf",
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
    
    
    
    
    
