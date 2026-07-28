import streamlit as st

from ui_components import download_button
from views.reports_labor import render_labor_efficiency_section
from views.reports_volume import render_volume_capacity_section
from views.reports_routing import render_routing_destinations_section
from views.reports_errors import render_errors_exceptions_section


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
    can_export=False,
    can_advanced_reports=False,
):

    st.header("Reports")
    pdf_button_placeholder = st.empty()

    export_notice_shown = False

    def gated_csv_download(df_export, filename, key=None):
        nonlocal export_notice_shown
        if can_export:
            download_button(df_export, filename, key=key)
        elif not export_notice_shown:
            st.caption("Exports are not available on this plan.")
            export_notice_shown = True

    def gated_pdf_download(pdf_bytes, file_name, key="director_pdf_download"):
        nonlocal export_notice_shown
        if can_export:
            pdf_button_placeholder.download_button(
                label="Download Director PDF",
                data=pdf_bytes,
                file_name=file_name,
                mime="application/pdf",
                key=key
            )
        elif not export_notice_shown:
            pdf_button_placeholder.caption("Exports are not available on this plan.")
            export_notice_shown = True

    render_labor_efficiency_section(
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
    )

    render_volume_capacity_section(df, df_live_raw, df_history_raw, today, gated_csv_download)

    render_routing_destinations_section(df, gated_csv_download)

    render_errors_exceptions_section(df, rejects_df, gated_csv_download)
