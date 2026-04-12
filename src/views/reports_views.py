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
