import altair as alt
import streamlit as st

from ui_components import format_hour_plain, render_chart, render_kpi_card


def render_routing_destinations_section(df, gated_csv_download):
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
            gated_csv_download(destination_counts, "destination_breakdown.csv")
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
            gated_csv_download(bin_volume_display, "bin_volume_report.csv")
    
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
                gated_csv_download(hourly_bin_display, "bin_volume_by_hour_report.csv")
    
