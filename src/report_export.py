from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd


def safe_int(value, default=0):
    try:
        if pd.isna(value):
            return default
        return int(value)
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def format_hour_plain(hour_value):
    if hour_value is None or pd.isna(hour_value):
        return "N/A"
    return pd.to_datetime(f"{int(hour_value):02d}:00").strftime("%I:%M %p")


def format_date_range(start_date, end_date):
    return f"{pd.to_datetime(start_date).strftime('%b %d, %Y')} to {pd.to_datetime(end_date).strftime('%b %d, %Y')}"


def build_executive_summary(report_data: Dict[str, Any]) -> str:
    total_checkins = report_data["total_checkins"]
    total_hours_saved = report_data["total_hours_saved"]
    reject_pct = report_data["reject_pct"]
    top_issue = report_data["top_issue"]
    peak_hour = report_data["peak_hour"]
    westside_pct = report_data["westside_pct"]
    library_express_pct = report_data["library_express_pct"]
    labor_value_saved = report_data.get("labor_value_saved")

    labor_value_text = ""
    if labor_value_saved is not None:
        labor_value_text = f" This represents approximately ${labor_value_saved:,.0f} in estimated staff time avoided."

    return (
        f"During the selected period, the AMH processed {total_checkins:,} items and reduced manual workload "
        f"by an estimated {total_hours_saved:,.1f} staff hours.{labor_value_text} "
        f"The overall reject rate was {reject_pct:.2f}%, with {top_issue} as the leading issue category. "
        f"Peak operational load occurred around {peak_hour}. "
        f"Transit routing remained stable, with {westside_pct:.2f}% of items routed to Westside and "
        f"{library_express_pct:.2f}% routed to Library Express."
    )


def build_director_report_data(
    *,
    start_date,
    end_date,
    df: pd.DataFrame,
    rejects_df: pd.DataFrame,
    overall_metrics: Dict[str, Any],
    top_issue: str,
    attention_text: str,
    avg_hours_saved: float,
    total_hours_saved: float,
    peak_day_saved: Optional[float] = None,
    peak_day_saved_date: Optional[str] = None,
    manual_rate: Optional[float] = None,
    amh_rate: Optional[float] = None,
    hourly_cost: Optional[float] = None,
    library_name: str = "New Braunfels Public Library",
    branch_name: str = "Main Branch",
    system_name: str = "Tech Logic UltraSort",
    report_title: str = "AMH Director Report - Cole Johnson",
) -> Dict[str, Any]:
    days_in_range = df["datetime"].dt.date.nunique() if len(df) > 0 and "datetime" in df.columns else 0
    total_checkins = len(df)
    avg_daily_checkins = (total_checkins / days_in_range) if days_in_range > 0 else 0.0

    labor_value_saved = None
    if hourly_cost is not None and total_hours_saved is not None:
        labor_value_saved = total_hours_saved * hourly_cost

    busiest_weekday_avg = "N/A"
    if len(df) > 0 and "datetime" in df.columns:
        weekday_avg = (
            df.assign(day_of_week=df["datetime"].dt.day_name())
            .groupby("day_of_week")
            .size()
            .div(df["datetime"].dt.date.nunique())
            .reindex([
                "Monday", "Tuesday", "Wednesday",
                "Thursday", "Friday", "Saturday", "Sunday"
            ])
        )

        if len(weekday_avg.dropna()) > 0:
            busiest_weekday_avg = weekday_avg.idxmax()

    reject_count = safe_int(overall_metrics.get("reject_count", len(rejects_df)))
    reject_pct = safe_float(overall_metrics.get("reject_pct", 0.0))
    westside_count = safe_int(overall_metrics.get("westside_count", 0))
    westside_pct = safe_float(overall_metrics.get("westside_pct", 0.0))
    library_express_count = safe_int(overall_metrics.get("library_express_count", 0))
    library_express_pct = safe_float(overall_metrics.get("library_express_pct", 0.0))

    peak_hour_raw = overall_metrics.get("peak_hour")
    peak_hour = format_hour_plain(peak_hour_raw)

    report_data = {
        "report_title": report_title,
        "generated_at": datetime.now().strftime("%b %d, %Y %I:%M %p"),
        "library_name": library_name,
        "branch_name": branch_name,
        "system_name": system_name,
        "date_range": format_date_range(start_date, end_date),
        "days_in_range": safe_int(days_in_range),
        "total_checkins": safe_int(total_checkins),
        "avg_daily_checkins": safe_float(avg_daily_checkins),
        "reject_count": reject_count,
        "reject_pct": reject_pct,
        "top_issue": top_issue or "N/A",
        "westside_count": westside_count,
        "westside_pct": westside_pct,
        "library_express_count": library_express_count,
        "library_express_pct": library_express_pct,
        "peak_hour": peak_hour,
        "avg_hours_saved": safe_float(avg_hours_saved),
        "total_hours_saved": safe_float(total_hours_saved),
        "peak_day_saved": safe_float(peak_day_saved) if peak_day_saved is not None else None,
        "peak_day_saved_date": peak_day_saved_date,
        "manual_rate": safe_float(manual_rate) if manual_rate is not None else None,
        "amh_rate": safe_float(amh_rate) if amh_rate is not None else None,
        "attention_text": attention_text or "No major issues stand out in the selected date range.",
        "busiest_weekday_avg": busiest_weekday_avg,
        "labor_value_saved": safe_float(labor_value_saved) if labor_value_saved is not None else None,
        "hourly_cost": safe_float(hourly_cost) if hourly_cost is not None else None,
        "hourly_cost_display": safe_float(hourly_cost) if hourly_cost is not None else None,
        "manual_total_hours": safe_float(total_checkins / manual_rate) if manual_rate not in (None, 0) else None,
        "amh_total_hours": safe_float(total_checkins / amh_rate) if amh_rate not in (None, 0) else None,
    }

    report_data["executive_summary"] = build_executive_summary(report_data)
    return report_data


def render_director_report_html(report_data: Dict[str, Any]) -> str:
    labor_value_html = ""
    labor_value_method_html = ""

    if report_data.get("labor_value_saved") is not None:
        labor_value_html = f"""
        <div class="section">
            <h2>Labor Value Snapshot</h2>
            <div class="summary-box">
                Estimated labor value for the selected range is
                <strong>${report_data['labor_value_saved']:,.0f}</strong>,
                based on <strong>{report_data['total_hours_saved']:.1f}</strong> total staff hours saved
                and an hourly labor cost of
                <strong>${report_data['hourly_cost_display']:.2f}</strong>.
            </div>
        </div>
        """

        if (
            report_data.get("labor_value_saved") is not None
            and report_data.get("hourly_cost_display") is not None
            and report_data.get("manual_rate") not in (None, 0)
            and report_data.get("amh_rate") not in (None, 0)
            and report_data.get("manual_total_hours") is not None
            and report_data.get("amh_total_hours") is not None
        ):
            labor_value_method_html = f"""
            <div class="section">
                <h2>Labor Value Method</h2>
                <div class="summary-box">
                    <div style="margin-bottom:10px;">
                        Estimated labor value is based on the selected date range and calculated in two steps:
                    </div>

                    <div style="margin-bottom:10px;">
                        <strong>1. Total hours saved</strong><br>
                        Total hours saved = (Total checkins ÷ Manual rate) − (Total checkins ÷ Observed AMH rate)
                    </div>

                    <div style="margin-bottom:10px;">
                        Total hours saved = ({report_data['total_checkins']:,} items ÷ {report_data['manual_rate']:.1f} items/hour)
                        − ({report_data['total_checkins']:,} items ÷ {report_data['amh_rate']:.1f} items/hour)
                    </div>

                    <div style="margin-bottom:10px;">
                        Total hours saved = {report_data['manual_total_hours']:.1f} staff hours
                        − {report_data['amh_total_hours']:.1f} machine hours
                        = <strong>{report_data['total_hours_saved']:.1f} staff hours</strong>
                    </div>

                    <div style="margin-bottom:10px;">
                        <strong>2. Estimated labor value</strong><br>
                        Estimated labor value = Total hours saved × Hourly labor cost
                    </div>

                    <div>
                        Estimated labor value = {report_data['total_hours_saved']:.1f} staff hours
                        × ${report_data['hourly_cost_display']:.2f}/hour
                        = <strong>${report_data['labor_value_saved']:,.0f}</strong>
                    </div>
                </div>
            </div>
            """

    rates_html = ""
    if report_data.get("manual_rate") is not None and report_data.get("amh_rate") is not None:
        rates_html = f"""
        <div class="section">
            <h2>Processing Rate Assumptions</h2>
            <div class="two-col">
                <div class="info-box">
                    <div class="info-label">Manual Rate</div>
                    <div class="info-value">{report_data['manual_rate']:.1f} items/hour</div>
                </div>
                <div class="info-box">
                    <div class="info-label">Observed AMH Rate</div>
                    <div class="info-value">{report_data['amh_rate']:.1f} items/hour</div>
                </div>
            </div>
            <div style="margin-top:10px; color:#4b5563; font-size:11px; line-height:1.5;">
                Manual rate reflects the observed item-processing speed for staff at Westside performing check-in manually during peak hours in the selected date range.
                Observed AMH rate reflects the average machine throughput during the busiest operating hours in the selected date range.
            </div>
        </div>
        """

    roi_html = ""
    if (
        report_data.get("yearly_savings_after_cost") is not None
        or report_data.get("since_install_net_value") is not None
    ):
        yearly_savings_text = (
            f"${report_data['yearly_savings_after_cost']:,.0f}"
            if report_data.get("yearly_savings_after_cost") is not None
            else "N/A"
        )

        annual_cost_text = (
            f"${report_data['annual_cost']:,.0f}"
            if report_data.get("annual_cost") is not None
            else "N/A"
        )

        payback_text = (
            f"{report_data['payback_months']:,.1f} mo"
            if report_data.get("payback_months") is not None
            else "N/A"
        )

        since_install_net_text = (
            f"${report_data['since_install_net_value']:,.0f}"
            if report_data.get("since_install_net_value") is not None
            else "N/A"
        )

        install_date_sub = (
            f"Install date: {report_data['install_date']}"
            if report_data.get("install_date")
            else "Estimated cumulative net value"
        )

        roi_html = f"""
        <div class="section">
            <h2>ROI Snapshot</h2>
            <div class="kpi-grid">
                <div class="kpi-card">
                    <div class="kpi-label">Yearly Savings After Cost</div>
                    <div class="kpi-value">{yearly_savings_text}</div>
                    <div class="kpi-sub">Projected annual net value</div>
                </div>

                <div class="kpi-card">
                    <div class="kpi-label">Annual Cost</div>
                    <div class="kpi-value">{annual_cost_text}</div>
                    <div class="kpi-sub">Recurring annual cost only</div>
                </div>

                <div class="kpi-card">
                    <div class="kpi-label">Time to Recover Cost</div>
                    <div class="kpi-value">{payback_text}</div>
                    <div class="kpi-sub">Based on annual net savings</div>
                </div>

                <div class="kpi-card">
                    <div class="kpi-label">Since-Install Net Value</div>
                    <div class="kpi-value">{since_install_net_text}</div>
                    <div class="kpi-sub">{install_date_sub}</div>
                </div>
            </div>
        </div>
        """

    labor_value_display = (
        f"${report_data['labor_value_saved']:,.0f}"
        if report_data.get("labor_value_saved") is not None
        else "N/A"
    )

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{report_data['report_title']}</title>
        <style>
            @page {{
                size: Letter;
                margin: 0.5in;
            }}

            body {{
                font-family: Arial, Helvetica, sans-serif;
                color: #1f2937;
                margin: 0;
                padding: 0;
                font-size: 12px;
                line-height: 1.45;
            }}

            .page {{
                width: 100%;
            }}

            .header {{
                border-bottom: 3px solid #60a5fa;
                padding-bottom: 14px;
                margin-bottom: 18px;
            }}

            .eyebrow {{
                color: #6b7280;
                font-size: 11px;
                margin-bottom: 6px;
            }}

            .title {{
                font-size: 24px;
                font-weight: 700;
                color: #111827;
                margin: 0 0 6px 0;
            }}

            .subtitle {{
                color: #4b5563;
                font-size: 12px;
                margin: 0;
            }}

            .meta {{
                margin-top: 8px;
                color: #6b7280;
                font-size: 11px;
            }}

            .section {{
                margin-bottom: 18px;
            }}

            .section h2 {{
                font-size: 15px;
                margin: 0 0 8px 0;
                color: #111827;
                border-left: 4px solid #a78bfa;
                padding-left: 8px;
            }}

            .summary-box {{
                background: #f8fafc;
                border: 1px solid #e5e7eb;
                border-radius: 10px;
                padding: 12px 14px;
            }}

            .kpi-grid {{
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 10px;
            }}

            .kpi-card {{
                border: 1px solid #e5e7eb;
                border-radius: 10px;
                padding: 12px;
                background: #ffffff;
            }}

            .kpi-label {{
                font-size: 11px;
                color: #6b7280;
                margin-bottom: 6px;
            }}

            .kpi-value {{
                font-size: 20px;
                font-weight: 700;
                color: #111827;
                margin-bottom: 4px;
            }}

            .kpi-sub {{
                font-size: 11px;
                color: #6b7280;
            }}

            .two-col {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 10px;
            }}

            .info-box {{
                border: 1px solid #e5e7eb;
                border-radius: 10px;
                padding: 12px;
                background: #ffffff;
            }}

            .info-label {{
                color: #6b7280;
                font-size: 11px;
                margin-bottom: 6px;
            }}

            .info-value {{
                font-size: 16px;
                font-weight: 700;
                color: #111827;
            }}

            .info-sub {{
                font-size: 11px;
                color: #6b7280;
                margin-top: 6px;
            }}

            .recommendation-box {{
                background: #fff7ed;
                border-left: 4px solid #d97706;
                padding: 12px 14px;
                border-radius: 8px;
            }}

            .footer {{
                margin-top: 20px;
                padding-top: 10px;
                border-top: 1px solid #e5e7eb;
                color: #6b7280;
                font-size: 10px;
                text-align: right;
            }}
        </style>
    </head>
    <body>
        <div class="page">
            <div class="header">
                <div class="eyebrow">Hanly Analytics • SortView</div>
                <h1 class="title">{report_data['report_title']}</h1>
                <p class="subtitle">
                    {report_data['library_name']} • {report_data['branch_name']} • {report_data['system_name']}
                </p>
                <div class="meta">
                    Reporting period: {report_data['date_range']}<br>
                    Generated: {report_data['generated_at']}
                </div>
            </div>

            <div class="section">
                <h2>Executive Summary</h2>
                <div class="summary-box">
                    {report_data['executive_summary']}
                </div>
            </div>

            <div class="section">
                <h2>Key Performance Indicators</h2>
                <div class="kpi-grid">
                    <div class="kpi-card">
                        <div class="kpi-label">Total Checkins</div>
                        <div class="kpi-value">{report_data['total_checkins']:,}</div>
                        <div class="kpi-sub">{report_data['days_in_range']} day(s) in range</div>
                    </div>

                    <div class="kpi-card">
                        <div class="kpi-label">Avg Daily Checkins</div>
                        <div class="kpi-value">{report_data['avg_daily_checkins']:.1f}</div>
                        <div class="kpi-sub">Average per day</div>
                    </div>

                    <div class="kpi-card">
                        <div class="kpi-label">Reject Rate</div>
                        <div class="kpi-value">{report_data['reject_pct']:.2f}%</div>
                        <div class="kpi-sub">{report_data['reject_count']:,} total rejects</div>
                    </div>

                    <div class="kpi-card">
                        <div class="kpi-label">Top Issue</div>
                        <div class="kpi-value" style="font-size:16px;">{report_data['top_issue']}</div>
                        <div class="kpi-sub">Leading reject category</div>
                    </div>

                    <div class="kpi-card">
                        <div class="kpi-label">Westside Transit</div>
                        <div class="kpi-value">{report_data['westside_pct']:.2f}%</div>
                        <div class="kpi-sub">{report_data['westside_count']:,} items</div>
                    </div>

                    <div class="kpi-card">
                        <div class="kpi-label">Library Express Transit</div>
                        <div class="kpi-value">{report_data['library_express_pct']:.2f}%</div>
                        <div class="kpi-sub">{report_data['library_express_count']:,} items</div>
                    </div>

                    <div class="kpi-card">
                        <div class="kpi-label">Avg Hours Saved</div>
                        <div class="kpi-value">{report_data['avg_hours_saved']:.2f}</div>
                        <div class="kpi-sub">Staff hours saved per day</div>
                    </div>

                    <div class="kpi-card">
                        <div class="kpi-label">Total Hours Saved</div>
                        <div class="kpi-value">{report_data['total_hours_saved']:.2f}</div>
                        <div class="kpi-sub">Across selected range</div>
                    </div>

                    <div class="kpi-card">
                        <div class="kpi-label">Estimated Labor Value</div>
                        <div class="kpi-value">{labor_value_display}</div>
                        <div class="kpi-sub">Estimated staff time avoided</div>
                    </div>
                </div>
            </div>

            {roi_html}

            {labor_value_html}

            {labor_value_method_html}

            {rates_html}

            <div class="section">
                <h2>Recommended Attention</h2>
                <div class="recommendation-box">
                    {report_data['attention_text']}
                </div>
            </div>

            <div class="footer">
                SortView Director Report
            </div>
        </div>
    </body>
    </html>
    """
    return html


def html_to_pdf_bytes(html: str) -> bytes:
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise ImportError(
            "weasyprint is not installed. Install it with: pip install weasyprint"
        ) from exc

    pdf_bytes = HTML(string=html).write_pdf()
    return pdf_bytes


def build_director_report_pdf(
    *,
    start_date,
    end_date,
    df: pd.DataFrame,
    rejects_df: pd.DataFrame,
    overall_metrics: Dict[str, Any],
    top_issue: str,
    attention_text: str,
    avg_hours_saved: float,
    total_hours_saved: float,
    peak_day_saved: Optional[float] = None,
    peak_day_saved_date: Optional[str] = None,
    manual_rate: Optional[float] = None,
    amh_rate: Optional[float] = None,
    hourly_cost: Optional[float] = None,
    roi_mode: Optional[str] = None,
    yearly_savings_after_cost: Optional[float] = None,
    annual_cost: Optional[float] = None,
    payback_months: Optional[float] = None,
    since_install_net_value: Optional[float] = None,
    install_date: Optional[str] = None,
    library_name: str = "New Braunfels Public Library",
    branch_name: str = "Main Branch",
    system_name: str = "Tech Logic UltraSort",
    report_title: str = "AMH Director Report - Cole Johnson",
) -> bytes:
    report_data = build_director_report_data(
        start_date=start_date,
        end_date=end_date,
        df=df,
        rejects_df=rejects_df,
        overall_metrics=overall_metrics,
        top_issue=top_issue,
        attention_text=attention_text,
        avg_hours_saved=avg_hours_saved,
        total_hours_saved=total_hours_saved,
        peak_day_saved=peak_day_saved,
        peak_day_saved_date=peak_day_saved_date,
        manual_rate=manual_rate,
        amh_rate=amh_rate,
        hourly_cost=hourly_cost,
        library_name=library_name,
        branch_name=branch_name,
        system_name=system_name,
        report_title=report_title,
        "roi_mode": roi_mode,
        "yearly_savings_after_cost": safe_float(yearly_savings_after_cost) if yearly_savings_after_cost is not None else None,
        "annual_cost": safe_float(annual_cost) if annual_cost is not None else None,
        "payback_months": safe_float(payback_months) if payback_months is not None else None,
        "since_install_net_value": safe_float(since_install_net_value) if since_install_net_value is not None else None,
        "install_date": install_date,
    )

    html = render_director_report_html(report_data)
    return html_to_pdf_bytes(html)
