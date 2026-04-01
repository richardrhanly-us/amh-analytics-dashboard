import pandas as pd


def normalize_transit_destination(value):
    if pd.isna(value):
        return ""

    text = str(value).strip()
    upper_text = text.upper()

    if not text or upper_text in {"1", "LOCAL", "MAIN"}:
        return ""

    if "WESTSIDE" in upper_text:
        return "Westside"

    if "LIBRARY EXPRESS" in upper_text:
        return "Library Express"

    return text


def get_transit_summary(df):
    if len(df) == 0:
        return pd.DataFrame(columns=["destination", "transit_items", "pct_of_total_items"])

    transit_df = df[df["transit_destination"].isin(["Westside", "Library Express"])].copy()

    if len(transit_df) == 0:
        return pd.DataFrame(columns=["destination", "transit_items", "pct_of_total_items"])

    summary = (
        transit_df["transit_destination"]
        .value_counts()
        .rename_axis("destination")
        .reset_index(name="transit_items")
    )
    summary["pct_of_total_items"] = (summary["transit_items"] / len(df) * 100).round(2)
    return summary


def compute_transit_times(df):
    if "barcode" not in df.columns or len(df) == 0:
        return pd.DataFrame()

    df = df.sort_values("datetime")
    results = []

    grouped = df.groupby("barcode")

    for _, group in grouped:
        group = group.sort_values("datetime")
        last_time = None

        for _, row in group.iterrows():
            current_time = row["datetime"]
            current_dest = row.get("transit_destination")

            if last_time is not None and current_dest:
                delta_minutes = (current_time - last_time).total_seconds() / 60

                if 0 < delta_minutes < 1440:
                    results.append({
                        "destination": current_dest,
                        "transit_time_min": delta_minutes
                    })

            last_time = current_time

    if not results:
        return pd.DataFrame()

    return pd.DataFrame(results)


def get_transit_time_summary(df):
    transit_times_df = compute_transit_times(df)

    if len(transit_times_df) == 0:
        return pd.DataFrame(columns=["destination", "avg_minutes"])

    summary_df = (
        transit_times_df.groupby("destination")["transit_time_min"]
        .mean()
        .reset_index()
    )
    summary_df["avg_minutes"] = summary_df["transit_time_min"].round(1)
    return summary_df[["destination", "avg_minutes"]]


def get_peak_transit_day_summary(transit_df, weekday_order):
    if len(transit_df) == 0:
        return {
            "peak_transit_day_label": "N/A",
            "peak_transit_day_subtitle": "",
        }

    daily_transit_counts = transit_df.groupby("date").size().reset_index(name="transit_count")
    daily_transit_counts["day_of_week"] = pd.to_datetime(daily_transit_counts["date"]).dt.day_name()

    transit_weekday_avg = daily_transit_counts.groupby("day_of_week")["transit_count"].mean()
    transit_weekday_avg = transit_weekday_avg.reindex(
        [d for d in weekday_order if d in transit_weekday_avg.index]
    )

    if len(transit_weekday_avg) == 0:
        return {
            "peak_transit_day_label": "N/A",
            "peak_transit_day_subtitle": "",
        }

    peak_day = transit_weekday_avg.idxmax()
    peak_avg = transit_weekday_avg.max()
    overall_transit_daily_avg = daily_transit_counts["transit_count"].mean()
    delta = peak_avg - overall_transit_daily_avg

    return {
        "peak_transit_day_label": peak_day,
        "peak_transit_day_subtitle": f"Avg {peak_avg:.0f} items/day ({delta:+.0f} vs overall daily avg)",
    }


def get_transit_weekday_comparison(df, rejects_df, weekday_order):
    if len(df) == 0:
        return pd.DataFrame()

    checkins_daily_for_corr = df.groupby("date").size().rename("checkins")
    rejects_daily_for_corr = rejects_df.groupby("date").size().rename("rejects")

    daily_ops = pd.concat([checkins_daily_for_corr, rejects_daily_for_corr], axis=1).fillna(0)
    daily_ops = daily_ops[daily_ops["checkins"] > 0].reset_index()

    if len(daily_ops) == 0:
        return pd.DataFrame()

    daily_ops["day_of_week"] = pd.to_datetime(daily_ops["date"]).dt.day_name()
    daily_ops["reject_rate"] = (daily_ops["rejects"] / daily_ops["checkins"]) * 100

    transit_daily_counts = df[df["transit_destination"].isin(["Westside", "Library Express"])] \
        .groupby("date").size().reset_index(name="transit_count")

    if len(transit_daily_counts) == 0:
        return pd.DataFrame()

    transit_daily_counts["day_of_week"] = pd.to_datetime(transit_daily_counts["date"]).dt.day_name()

    transit_weekday_avg = transit_daily_counts.groupby("day_of_week")["transit_count"].mean()
    transit_weekday_avg = transit_weekday_avg.reindex(
        [d for d in weekday_order if d in transit_weekday_avg.index]
    )

    reject_weekday_avg = daily_ops.groupby("day_of_week")["reject_rate"].mean()
    reject_weekday_avg = reject_weekday_avg.reindex(
        [d for d in weekday_order if d in reject_weekday_avg.index]
    )

    return pd.DataFrame({
        "Avg Transit Items / Day": transit_weekday_avg,
        "Avg Reject Rate %": reject_weekday_avg
    }).dropna(how="all")


def get_destination_weekday_mix(transit_df, weekday_order):
    if len(transit_df) == 0:
        return pd.DataFrame()

    daily_dest_counts = (
        transit_df.groupby(["date", "transit_destination"])
        .size()
        .reset_index(name="transit_count")
    )
    daily_dest_counts["day_of_week"] = pd.to_datetime(daily_dest_counts["date"]).dt.day_name()

    destination_weekday_mix = (
        daily_dest_counts.groupby(["day_of_week", "transit_destination"])["transit_count"]
        .mean()
        .unstack(fill_value=0)
    )

    destination_weekday_mix = destination_weekday_mix.reindex(
        [d for d in weekday_order if d in destination_weekday_mix.index]
    )

    return destination_weekday_mix


def get_destination_reject_summary(df, rejects_df, transit_summary, valid_transit_destinations):
    if len(df) == 0 or len(transit_summary) == 0:
        return pd.DataFrame()

    barcode_map = (
        df.sort_values("datetime")
        .drop_duplicates(subset=["barcode"], keep="last")
        [["barcode", "destination", "transit_destination"]]
    )

    rejects_with_destination = rejects_df.merge(
        barcode_map,
        on="barcode",
        how="left"
    )

    transit_rejects = rejects_with_destination[
        rejects_with_destination["transit_destination"].isin(valid_transit_destinations)
    ].copy()

    if len(transit_rejects) == 0:
        summary = transit_summary.copy()
        summary["reject_count"] = 0
        summary["reject_rate_pct"] = 0.0
        summary["top_reject_reason"] = "None"
        summary["reason_count"] = 0
        summary["top_reason_pct_of_destination_rejects"] = 0.0
        return summary

    destination_reject_counts = (
        transit_rejects["transit_destination"]
        .value_counts()
        .rename_axis("destination")
        .reset_index(name="reject_count")
    )

    destination_top_issue_count = (
        transit_rejects.groupby(["transit_destination", "error_simple"])
        .size()
        .reset_index(name="reason_count")
        .sort_values(["transit_destination", "reason_count"], ascending=[True, False])
        .drop_duplicates(subset=["transit_destination"])
        .rename(columns={
            "transit_destination": "destination",
            "error_simple": "top_reject_reason"
        })
    )

    destination_reject_summary = transit_summary.merge(
        destination_reject_counts,
        on="destination",
        how="left"
    )

    destination_reject_summary["reject_count"] = destination_reject_summary["reject_count"].fillna(0).astype(int)
    destination_reject_summary["reject_rate_pct"] = (
        destination_reject_summary["reject_count"] / destination_reject_summary["transit_items"] * 100
    ).round(2)

    destination_reject_summary = destination_reject_summary.merge(
        destination_top_issue_count[["destination", "top_reject_reason", "reason_count"]],
        on="destination",
        how="left"
    )

    destination_reject_summary["top_reject_reason"] = destination_reject_summary["top_reject_reason"].fillna("None")
    destination_reject_summary["reason_count"] = destination_reject_summary["reason_count"].fillna(0).astype(int)

    destination_reject_summary["top_reason_pct_of_destination_rejects"] = destination_reject_summary.apply(
        lambda row: round((row["reason_count"] / row["reject_count"]) * 100, 1)
        if row["reject_count"] > 0 else 0.0,
        axis=1
    )

    return destination_reject_summary


def get_transit_reject_insight(transit_weekday_comparison):
    title = "Transit / Reject Pattern"
    text = "Not enough data to compare transit load and reject patterns yet."
    color = "#6b7280"

    if len(transit_weekday_comparison) == 0:
        return {
            "title": title,
            "text": text,
            "color": color,
        }

    transit_peak_day = transit_weekday_comparison["Avg Transit Items / Day"].idxmax()
    reject_peak_day = transit_weekday_comparison["Avg Reject Rate %"].idxmax()

    transit_peak_value = transit_weekday_comparison.loc[transit_peak_day, "Avg Transit Items / Day"]
    reject_peak_value = transit_weekday_comparison.loc[reject_peak_day, "Avg Reject Rate %"]

    correlation = None
    if len(transit_weekday_comparison.dropna()) > 1:
        correlation = transit_weekday_comparison["Avg Transit Items / Day"].corr(
            transit_weekday_comparison["Avg Reject Rate %"]
        )

    if transit_peak_day == reject_peak_day:
        title = "Strong Correlation"
        text = (
            f"{transit_peak_day} has both the highest average transit load "
            f"({transit_peak_value:.0f} items/day) and highest average reject rate "
            f"({reject_peak_value:.2f}%)."
        )
        color = "#b91c1c"

    elif correlation is not None and correlation > 0.5:
        title = "Moderate Correlation"
        text = (
            f"Transit load and reject rate move somewhat together "
            f"(corr={correlation:.2f}), but the peak days do not match."
        )
        color = "#d97706"

    else:
        title = "No Clear Relationship"
        text = (
            f"Transit load peaks on {transit_peak_day} "
            f"({transit_peak_value:.0f} items/day), while reject rate peaks on "
            f"{reject_peak_day} ({reject_peak_value:.2f}%). "
            f"This does not suggest a strong load-driven pattern."
        )
        color = "#6b7280"

    return {
        "title": title,
        "text": text,
        "color": color,
    }


def get_destination_driver_summary(destination_reject_summary):
    text = "No transit destination diagnostics available for the selected date range."
    color = "#6b7280"

    if len(destination_reject_summary) == 0:
        return {
            "text": text,
            "color": color,
        }

    top_volume_row = destination_reject_summary.sort_values("transit_items", ascending=False).iloc[0]
    top_reject_row = destination_reject_summary.sort_values("reject_count", ascending=False).iloc[0]

    if top_reject_row["reject_count"] == 0:
        text = (
            f"{top_volume_row['destination']} drives most transit volume "
            f"({int(top_volume_row['transit_items']):,} items), but no transit-linked rejects "
            f"were found for the selected range."
        )
        color = "#059669"

    elif top_volume_row["destination"] == top_reject_row["destination"]:
        text = (
            f"{top_volume_row['destination']} leads both transit volume and transit-linked rejects: "
            f"{int(top_volume_row['transit_items']):,} items and "
            f"{int(top_reject_row['reject_count']):,} rejects. "
            f"Top issue: {top_reject_row['top_reject_reason']}."
        )
        color = "#b91c1c"

    else:
        text = (
            f"{top_volume_row['destination']} has the most transit volume "
            f"({int(top_volume_row['transit_items']):,} items), while "
            f"{top_reject_row['destination']} has the most transit-linked rejects "
            f"({int(top_reject_row['reject_count']):,})."
        )
        color = "#92400e"

    return {
        "text": text,
        "color": color,
    }
