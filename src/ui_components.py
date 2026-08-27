#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         ui_components.py
#
#  Description: Provides shared Streamlit UI and chart helper
#               functions for the SortView dashboard. This file
#               renders KPI cards, formats dates and times, creates
#               CSV download buttons, applies theme-aware chart styling,
#               and builds reusable Altair chart objects.
#
#***************************************************************

import altair as alt
import pandas as pd
import streamlit as st

#***************************************************************
#
#  Function:     render_kpi_card
#
#  Description: Renders a styled KPI card in Streamlit using custom
#               HTML. The card can display a title, main value,
#               subtitle, optional fill background, and custom styling
#               options for dashboard metrics.
#
#  Parameters:  title - KPI card title.
#               value - Main value displayed in the card.
#               subtitle - Optional supporting text under the value.
#               subtitle_color - CSS color used for the subtitle.
#               value_font_size - CSS font size for the main value.
#               border_color - CSS border color for the card.
#               value_color - CSS color for the main value.
#               value_wrap - Boolean flag allowing the value to wrap.
#               fill_pct - Optional percentage used to fill the card
#                          background from the bottom.
#               fill_color - Optional CSS color for the fill area.
#
#  Returns:     None
#
#***************************************************************

def render_kpi_card(
    title,
    value,
    subtitle="",
    subtitle_color="var(--text-color)",
    value_font_size="2.35rem",
    border_color="rgba(148, 163, 184, 0.28)",
    value_color="var(--text-color)",
    value_wrap=False,
    fill_pct=None,
    fill_color=None,
):
    # Use the active Streamlit theme to choose a default fill color.
    theme_base = st.get_option("theme.base") or "light"

    if fill_color is None:
        if theme_base == "dark":
            fill_color = "rgba(96, 165, 250, 0.28)"
        else:
            fill_color = "rgba(59, 130, 246, 0.12)"

    # Control how the main value behaves when it is long.
    value_white_space = "normal" if value_wrap else "nowrap"
    value_word_break = "break-word" if value_wrap else "normal"

    # Clamp the fill percentage between 0 and 100.
    safe_fill_pct = 0
    if fill_pct is not None:
        safe_fill_pct = max(0, min(fill_pct, 1)) * 100

    # Build optional background fill HTML.
    fill_html = ""
    if fill_pct is not None:
        fill_html = (
            f'<div style="'
            f'position:absolute;'
            f'left:0;'
            f'bottom:0;'
            f'width:100%;'
            f'height:{safe_fill_pct:.1f}%;'
            f'background:{fill_color};'
            f'z-index:1;'
            f'transition:height 0.6s ease;'
            f'"></div>'
        )

    # Build optional subtitle HTML.
    subtitle_html = ""
    if subtitle:
        subtitle_html = (
            f'<div style="'
            f'font-size:0.98rem;'
            f'font-weight:500;'
            f'color:{subtitle_color};'
            f'margin-top:10px;'
            f'line-height:1.35;'
            f'overflow:visible;'
            f'position:relative;'
            f'z-index:2;'
            f'opacity:0.82;'
            f'width:100%;'
            f'">{subtitle}</div>'
        )

    # Build and render the full card HTML.
    card_html = (
        f'<div style="'
        f'position:relative;'
        f'overflow:hidden;'
        f'border:1px solid {border_color};'
        f'border-radius:12px;'
        f'padding:16px 18px;'
        f'background:var(--secondary-background-color);'
        f'min-height:185px;'
        f'height:185px;'
        f'display:flex;'
        f'flex-direction:column;'
        f'justify-content:center;'
        f'align-items:center;'
        f'text-align:center;'
        f'box-shadow:0 1px 2px rgba(0, 0, 0, 0.08);'
        f'">'
        f'{fill_html}'
        f'<div style="'
        f'font-size:1.08rem;'
        f'font-weight:600;'
        f'color:var(--text-color);'
        f'margin-bottom:10px;'
        f'position:relative;'
        f'z-index:2;'
        f'opacity:0.80;'
        f'">{title}</div>'
        f'<div style="'
        f'font-size:{value_font_size};'
        f'font-weight:700;'
        f'color:{value_color};'
        f'line-height:1.15;'
        f'margin-bottom:4px;'
        f'white-space:{value_white_space};'
        f'word-break:{value_word_break};'
        f'position:relative;'
        f'z-index:2;'
        f'">{value}</div>'
        f'{subtitle_html}'
        f'</div>'
    )

    st.markdown(card_html, unsafe_allow_html=True)


#***************************************************************
#
#  Function:     format_hour
#
#  Description: Formats a numeric hour value into dashboard HTML
#               using 12-hour time with a smaller AM/PM label.
#
#  Parameters:  hour - Numeric hour value using 24-hour time.
#
#  Returns:     str - HTML-formatted time label.
#
#***************************************************************

def format_hour(hour):
    if hour is None:
        return "N/A"

    if hour == 0:
        return "12:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>AM</span>"
    if hour < 12:
        return f"{hour}:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>AM</span>"
    if hour == 12:
        return "12:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>PM</span>"
    return f"{hour-12}:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>PM</span>"


#***************************************************************
#
#  Function:     format_hour_plain
#
#  Description: Formats a numeric hour value into plain 12-hour
#               clock text without HTML.
#
#  Parameters:  hour_value - Numeric hour value using 24-hour time.
#
#  Returns:     str - Plain formatted time label, or "N/A" if missing.
#
#***************************************************************

def format_hour_plain(hour_value):
    if hour_value is None or pd.isna(hour_value):
        return "N/A"

    return pd.to_datetime(f"{int(hour_value):02d}:00").strftime("%I:%M %p")


#***************************************************************
#
#  Function:     format_relative_time
#
#  Description: Converts a datetime value into a readable relative
#               time string such as "just now", "5 min ago", or
#               "2 days ago".
#
#  Parameters:  dt_value - Earlier datetime value.
#               now_value - Current datetime value used for comparison.
#
#  Returns:     str - Relative time label.
#
#***************************************************************

def format_relative_time(dt_value, now_value):
    if dt_value is None:
        return "N/A"

    minutes = int((now_value - dt_value).total_seconds() // 60)

    if minutes < 1:
        return "just now"
    if minutes == 1:
        return "1 min ago"
    if minutes < 60:
        return f"{minutes} min ago"

    hours = minutes // 60
    if hours == 1:
        return "1 hr ago"
    if hours < 24:
        return f"{hours} hrs ago"

    days = hours // 24
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


#***************************************************************
#
#  Function:     download_button
#
#  Description: Renders a Streamlit download button for exporting a
#               dataframe as a CSV file.
#
#  Parameters:  df - Dataframe to export.
#               filename - Name of the downloaded CSV file.
#               key - Optional Streamlit widget key.
#
#  Returns:     None
#
#***************************************************************

def download_button(df, filename, key=None):
    # Convert the dataframe to UTF-8 encoded CSV bytes.
    csv = df.to_csv(index=False).encode("utf-8")

    left, _ = st.columns([1, 10])

    with left:
        st.download_button(
            label="Download CSV",
            data=csv,
            file_name=filename,
            mime="text/csv",
            key=key or f"{filename}_download",
        )


#***************************************************************
#
#  Function:     format_ill_branch_subtitle
#
#  Description: Builds a compact subtitle showing ILL item counts by
#               home branch and configured transit branches.
#
#  Parameters:  main_count - ILL count for the home branch.
#               ill_by_branch - Dictionary of ILL counts by transit branch.
#               transit_home_label - Label for the home branch.
#               transit_labels - List of transit branch labels.
#
#  Returns:     str - Formatted ILL branch subtitle.
#
#***************************************************************

def format_ill_branch_subtitle(main_count, ill_by_branch, transit_home_label, transit_labels):
    parts = [f"{transit_home_label} {main_count:,}"]

    for transit_label in transit_labels:
        branch_count = int(ill_by_branch.get(transit_label, 0))
        parts.append(f"{transit_label} {branch_count:,}")

    return " • ".join(parts)


#***************************************************************
#
#  Function:     render_chart
#
#  Description: Applies theme-aware styling to an Altair chart and
#               renders it in Streamlit using the full container width.
#
#  Parameters:  chart - Altair chart object to render.
#
#  Returns:     None
#
#***************************************************************

def render_chart(chart):
    # Match chart colors to the active Streamlit theme.
    theme_base = st.get_option("theme.base") or "light"

    if theme_base == "dark":
        axis_label_color = "#cbd5e1"
        axis_title_color = "#e5e7eb"
        grid_color = "rgba(148, 163, 184, 0.18)"
        domain_color = "rgba(148, 163, 184, 0.28)"
        tick_color = "rgba(148, 163, 184, 0.28)"
        legend_label_color = "#cbd5e1"
        legend_title_color = "#e5e7eb"
        title_color = "#f8fafc"
        chart_background = "transparent"
        view_fill = "transparent"
    else:
        axis_label_color = "#6b7280"
        axis_title_color = "#6b7280"
        grid_color = "#e5e7eb"
        domain_color = "#d1d5db"
        tick_color = "#d1d5db"
        legend_label_color = "#6b7280"
        legend_title_color = "#6b7280"
        title_color = "#1f2937"
        chart_background = "transparent"
        view_fill = "transparent"

    # Apply shared styling across chart axes, legends, titles, and background.
    chart = (
        chart.configure_view(stroke=None, fill=view_fill)
        .configure_axis(
            labelColor=axis_label_color,
            titleColor=axis_title_color,
            gridColor=grid_color,
            domainColor=domain_color,
            tickColor=tick_color,
            labelFontSize=12,
            titleFontSize=13,
        )
        .configure_legend(
            labelColor=legend_label_color,
            titleColor=legend_title_color,
            labelFontSize=12,
            titleFontSize=13,
        )
        .configure_title(color=title_color, fontSize=16)
        .properties(background=chart_background)
    )

    st.altair_chart(chart, use_container_width=True)


#***************************************************************
#
#  Function:     get_hour_range_df
#
#  Description: Builds a dataframe containing a complete range of
#               hours and formatted hour labels. This helps charts
#               display all expected operating hours even when some
#               hours have no activity.
#
#  Parameters:  start_hour - First hour to include.
#               end_hour - Last hour to include.
#
#  Returns:     DataFrame - Hour range dataframe.
#
#***************************************************************

def get_hour_range_df(start_hour=7, end_hour=20):
    hour_df = pd.DataFrame({"hour": list(range(start_hour, end_hour + 1))})
    hour_df["hour_label"] = hour_df["hour"].apply(format_hour_plain)
    return hour_df


#***************************************************************
#
#  Function:     build_hourly_bar_chart
#
#  Description: Builds an Altair bar chart for hourly data. The chart
#               fills missing hours with zero values so the x-axis
#               remains consistent across dashboard views.
#
#  Parameters:  df - Source dataframe containing hourly values.
#               value_col - Numeric column to chart.
#               title_y - Y-axis title.
#               start_hour - First hour to display.
#               end_hour - Last hour to display.
#
#  Returns:     Chart - Altair bar chart object.
#
#***************************************************************

def build_hourly_bar_chart(df, value_col, title_y, start_hour=7, end_hour=20):
    # Merge data against a complete hour range to keep missing hours visible.
    hour_base = get_hour_range_df(start_hour, end_hour)
    merged = hour_base.merge(df, on=["hour", "hour_label"], how="left").fillna(0)

    chart = (
        alt.Chart(merged)
        .mark_bar()
        .encode(
            x=alt.X(
                "hour_label:N",
                sort=merged["hour_label"].tolist(),
                title="Hour",
                axis=alt.Axis(labelAngle=0),
            ),
            y=alt.Y(f"{value_col}:Q", title=title_y),
            tooltip=["hour_label", value_col],
        )
        .properties(height=350)
    )
    return chart


#***************************************************************
#
#  Function:     build_category_bar_chart
#
#  Description: Builds an Altair bar chart for category-based data.
#               The chart preserves the dataframe's category order
#               when rendering the x-axis.
#
#  Parameters:  df - Source dataframe.
#               category_col - Column containing category labels.
#               value_col - Numeric column to chart.
#               y_title - Y-axis title.
#               x_title - Optional x-axis title.
#
#  Returns:     Chart - Altair bar chart object.
#
#***************************************************************

def build_category_bar_chart(df, category_col, value_col, y_title, x_title=""):
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X(
                f"{category_col}:N",
                sort=df[category_col].tolist(),
                title=x_title,
                axis=alt.Axis(labelAngle=0),
            ),
            y=alt.Y(f"{value_col}:Q", title=y_title),
            tooltip=[category_col, value_col],
        )
        .properties(height=350)
    )
    return chart


#***************************************************************
#
#  Function:     build_date_line_chart
#
#  Description: Builds an Altair line chart for date-based trends.
#               The chart can optionally split the line by a series
#               column for grouped comparisons.
#
#  Parameters:  df - Source dataframe.
#               date_col - Date column used for the x-axis.
#               value_col - Numeric column used for the y-axis.
#               y_title - Y-axis title.
#               series_col - Optional column used to split the line
#                            into multiple series.
#
#  Returns:     Chart - Altair line chart object.
#
#***************************************************************

def build_date_line_chart(df, date_col, value_col, y_title, series_col=None):
    if series_col:
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    f"{date_col}:T",
                    title="Date",
                    axis=alt.Axis(labelAngle=0, format="%b %d"),
                ),
                y=alt.Y(f"{value_col}:Q", title=y_title),
                color=alt.Color(f"{series_col}:N"),
                tooltip=[date_col, series_col, value_col],
            )
            .properties(height=350)
        )
    else:
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    f"{date_col}:T",
                    title="Date",
                    axis=alt.Axis(labelAngle=0, format="%b %d"),
                ),
                y=alt.Y(f"{value_col}:Q", title=y_title),
                tooltip=[date_col, value_col],
            )
            .properties(height=350)
        )
    return chart


#***************************************************************
#
#  Function:     build_weekday_line_chart
#
#  Description: Builds an Altair line chart for weekday-based trends.
#               The chart uses a fixed Monday-through-Sunday order
#               and can optionally split the line by a series column.
#
#  Parameters:  df - Source dataframe.
#               weekday_col - Weekday label column used for the x-axis.
#               value_col - Numeric column used for the y-axis.
#               series_col - Optional column used to split the line
#                            into multiple series.
#
#  Returns:     Chart - Altair line chart object.
#
#***************************************************************

def build_weekday_line_chart(df, weekday_col, value_col, series_col=None):
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    if series_col:
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    f"{weekday_col}:N",
                    sort=weekday_order,
                    title="Weekday",
                    axis=alt.Axis(labelAngle=0),
                ),
                y=alt.Y(f"{value_col}:Q", title="Value"),
                color=alt.Color(f"{series_col}:N"),
                tooltip=[weekday_col, series_col, value_col],
            )
            .properties(height=350)
        )
    else:
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    f"{weekday_col}:N",
                    sort=weekday_order,
                    title="Weekday",
                    axis=alt.Axis(labelAngle=0),
                ),
                y=alt.Y(f"{value_col}:Q", title="Value"),
                tooltip=[weekday_col, value_col],
            )
            .properties(height=350)
        )
    return chart


#***************************************************************
#
#  Function:     build_hourly_line_chart
#
#  Description: Builds an Altair line chart for hourly data. The chart
#               fills missing hours with zero values and can optionally
#               split the line by a series column.
#
#  Parameters:  df - Source dataframe containing hourly values.
#               value_col - Numeric column used for the y-axis.
#               title_y - Y-axis title.
#               series_col - Optional column used to split the line
#                            into multiple series.
#               start_hour - First hour to display.
#               end_hour - Last hour to display.
#
#  Returns:     Chart - Altair line chart object.
#
#***************************************************************

def build_hourly_line_chart(df, value_col, title_y, series_col=None, start_hour=7, end_hour=20):
    hour_base = get_hour_range_df(start_hour, end_hour)

    if series_col:
        # Expand every series across the full hour range so missing
        # combinations appear as zero values instead of disappearing.
        series_values = df[series_col].dropna().unique().tolist()
        expanded = pd.MultiIndex.from_product(
            [hour_base["hour"].tolist(), series_values],
            names=["hour", series_col],
        ).to_frame(index=False)

        expanded = expanded.merge(hour_base, on="hour", how="left")
        merged = expanded.merge(df, on=["hour", "hour_label", series_col], how="left").fillna(0)

        chart = (
            alt.Chart(merged)
            .mark_line(point=False)
            .encode(
                x=alt.X(
                    "hour_label:N",
                    sort=hour_base["hour_label"].tolist(),
                    title="Hour",
                    axis=alt.Axis(labelAngle=0),
                ),
                y=alt.Y(f"{value_col}:Q", title=title_y),
                color=alt.Color(f"{series_col}:N"),
                tooltip=["hour_label", series_col, value_col],
            )
            .properties(height=350)
        )
    else:
        # Single-series version of the hourly line chart.
        merged = hour_base.merge(df, on=["hour", "hour_label"], how="left").fillna(0)

        chart = (
            alt.Chart(merged)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    "hour_label:N",
                    sort=merged["hour_label"].tolist(),
                    title="Hour",
                    axis=alt.Axis(labelAngle=0),
                ),
                y=alt.Y(f"{value_col}:Q", title=title_y),
                tooltip=["hour_label", value_col],
            )
            .properties(height=350)
        )

    return chart
