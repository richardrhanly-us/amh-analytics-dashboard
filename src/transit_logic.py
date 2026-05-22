#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         transit_logic.py
#
#  Description: Provides transit and internal routing logic for the
#               SortView dashboard. This file normalizes transit
#               destinations, identifies internal routing categories,
#               summarizes transit activity, calculates transit times,
#               compares transit and reject patterns, and builds
#               diagnostic summaries for transit destinations.
#
#***************************************************************

import re
import pandas as pd


#***************************************************************
#
#  Function:     normalize_transit_destination
#
#  Description: Converts raw destination values into standardized
#               transit destination names used by the dashboard.
#               Local, Main, blank, and invalid values are treated
#               as non-transit destinations.
#
#  Parameters:  value - Raw destination value from checkin data.
#
#  Returns:     str - Normalized transit destination name, original
#                     text, or an empty string for non-transit items.
#
#***************************************************************

def normalize_transit_destination(value):
    # Treat missing values as non-transit destinations.
    if pd.isna(value):
        return ""

    text = str(value).strip()
    upper_text = text.upper()

    # Local/Main routing is not counted as transit activity.
    if not text or upper_text in {"1", "LOCAL", "MAIN"}:
        return ""

    # Standardize known transit branch names.
    if "WESTSIDE" in upper_text:
        return "Westside"

    if "LIBRARY EXPRESS" in upper_text:
        return "Library Express"

    return text


#***************************************************************
#
#  Function:     normalize_internal_destination
#
#  Description: Classifies non-public routing destinations into
#               internal workflow categories such as ILL, Collection
#               Services, Repair/Mending, Staff Review, or Other
#               Internal. Public transit destinations and invalid
#               routing values are ignored.
#
#  Parameters:  destination - Destination value from ACS or routing data.
#               raw_message - Raw ACS message text.
#               message_code - ACS message code.
#
#  Returns:     str | None - Internal routing category, or None if the
#                            row should not be counted as internal routing.
#
#***************************************************************

def normalize_internal_destination(destination, raw_message="", message_code=""):
    # Normalize inputs so text checks do not fail on None values.
    destination = "" if destination is None else str(destination).strip()
    raw_message = "" if raw_message is None else str(raw_message)
    message_code = "" if message_code is None else str(message_code).strip()

    combined = f"{destination} {raw_message}".upper()

    # Ignore rows that do not provide any destination or message text.
    if not destination and not raw_message:
        return None

    # Ignore normal public transit destinations.
    if "WESTSIDE" in combined or "LIBRARY EXPRESS" in combined:
        return None

    # Ignore missing or invalid agency destinations.
    if "NO AGENCY DESTINATION" in combined or destination == "":
        return None

    # Identify interlibrary loan routing.
    if re.search(r"\bILL\b", combined) or "INTERLIBRARY" in combined:
        return "ILL"

    # Identify collection services or processing-related routing.
    if (
        "COLLECTION SERVICES" in combined
        or "COLLECTION" in combined
        or "CATALOG" in combined
        or "PROCESSING" in combined
    ):
        return "Collection Services"

    # Identify repair or mending workflows.
    if "REPAIR" in combined or "MENDING" in combined or "MEND" in combined:
        return "Repair / Mending"

    # Identify staff review workflows.
    if "STAFF" in combined or "REVIEW" in combined:
        return "Staff Review"

    # Exclude message codes that should not be treated as internal routing.
    if message_code in {"09", "10", "11", "12", "13", "14", "15", "16", "17", "18"}:
        return None

    return "Other Internal"


#***************************************************************
#
#  Function:     build_internal_routing_summary
#
#  Description: Builds a summary dataframe of internal routing
#               categories from ACS event data. Each ACS row is
#               classified with normalize_internal_destination, then
#               grouped by category.
#
#  Parameters:  acs_df - ACS event dataframe.
#
#  Returns:     DataFrame - Internal routing category counts.
#
#***************************************************************

def build_internal_routing_summary(acs_df):
    if acs_df is None or len(acs_df) == 0:
        return pd.DataFrame(columns=["internal_category", "count"])

    work_df = acs_df.copy()

    # Normalize datetime values when available.
    if "datetime" in work_df.columns:
        work_df["datetime"] = pd.to_datetime(work_df["datetime"], errors="coerce")

    # Classify each row into an internal routing category.
    work_df["internal_category"] = work_df.apply(
        lambda row: normalize_internal_destination(
            row.get("destination"),
            row.get("raw_message"),
            row.get("message_code"),
        ),
        axis=1,
    )

    # Keep only rows that were classified as internal routing.
    work_df = work_df[work_df["internal_category"].notna()].copy()

    if len(work_df) == 0:
        return pd.DataFrame(columns=["internal_category", "count"])

    summary = (
        work_df["internal_category"]
        .value_counts()
        .rename_axis("internal_category")
        .reset_index(name="count")
    )

    return summary


#***************************************************************
#
#  Function:     get_internal_count
#
#  Description: Retrieves the count for one internal routing category
#               from an internal routing summary dataframe.
#
#  Parameters:  summary_df - Internal routing summary dataframe.
#               category_name - Category name to look up.
#
#  Returns:     int - Count for the requested category, or 0 if missing.
#
#***************************************************************

def get_internal_count(summary_df, category_name):
    if summary_df is None or len(summary_df) == 0:
        return 0

    match = summary_df.loc[summary_df["internal_category"] == category_name, "count"]
    if len(match) == 0:
        return 0

    return int(match.iloc[0])


#***************************************************************
#
#  Function:     get_transit_summary
#
#  Description: Builds a destination-level summary of public transit
#               activity for Westside and Library Express. The summary
#               includes item counts and percent of total checkins.
#
#  Parameters:  df - Checkin dataframe containing transit_destination.
#
#  Returns:     DataFrame - Transit item counts and percentages.
#
#***************************************************************

def get_transit_summary(df):
    if len(df) == 0:
        return pd.DataFrame(columns=["destination", "transit_items", "pct_of_total_items"])

    # Keep only destinations counted as public transit branches.
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


#***************************************************************
#
#  Function:     compute_transit_times
#
#  Description: Estimates transit timing by comparing repeated scans
#               for the same barcode. When an item has a later scan
#               with a transit destination, the elapsed time from the
#               previous scan is recorded in minutes.
#
#  Parameters:  df - Checkin dataframe containing barcode, datetime,
#                    and transit_destination columns.
#
#  Returns:     DataFrame - Destination-level transit time observations.
#
#***************************************************************

def compute_transit_times(df):
    if "barcode" not in df.columns or len(df) == 0:
        return pd.DataFrame()

    work_df = df.sort_values("datetime").copy()
    results = []

    grouped = work_df.groupby("barcode")

    # Review each barcode's scan history in chronological order.
    for _, group in grouped:
        group = group.sort_values("datetime")
        last_time = None

        for _, row in group.iterrows():
            current_time = row["datetime"]
            current_dest = row.get("transit_destination")

            # Record a transit time only when there is a previous scan and
            # the current row has a transit destination.
            if last_time is not None and current_dest:
                delta_minutes = (current_time - last_time).total_seconds() / 60

                # Ignore impossible or extreme values over one day.
                if 0 < delta_minutes < 1440:
                    results.append({
                        "destination": current_dest,
                        "transit_time_min": delta_minutes,
                    })

            last_time = current_time

    if not results:
        return pd.DataFrame()

    return pd.DataFrame(results)


#***************************************************************
#
#  Function:     get_transit_time_summary
#
#  Description: Calculates average transit time by destination using
#               the transit time observations created by
#               compute_transit_times.
#
#  Parameters:  df - Checkin dataframe.
#
#  Returns:     DataFrame - Destination and average transit time in minutes.
#
#***************************************************************

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


#***************************************************************
#
#  Function:     get_peak_transit_day_summary
#
#  Description: Determines which weekday has the highest average
#               transit volume and builds a short summary label and
#               subtitle for dashboard display.
#
#  Parameters:  transit_df - Dataframe containing transit rows.
#               weekday_order - Ordered list of weekdays for display.
#
#  Returns:     dict - Peak transit day label and subtitle text.
#
#***************************************************************

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


#***************************************************************
#
#  Function:     get_transit_weekday_comparison
#
#  Description: Compares average transit volume and average reject
#               rate by weekday. This helps determine whether heavier
#               transit days also show higher reject rates.
#
#  Parameters:  df - Checkin dataframe containing date and transit data.
#               rejects_df - Reject dataframe containing date values.
#               weekday_order - Ordered list of weekdays for display.
#
#  Returns:     DataFrame - Weekday comparison of transit load and
#                           reject rate.
#
#***************************************************************

def get_transit_weekday_comparison(df, rejects_df, weekday_order):
    if len(df) == 0:
        return pd.DataFrame()

    # Build daily checkin and reject counts for reject-rate comparison.
    checkins_daily_for_corr = df.groupby("date").size().rename("checkins")
    rejects_daily_for_corr = rejects_df.groupby("date").size().rename("rejects")

    daily_ops = pd.concat([checkins_daily_for_corr, rejects_daily_for_corr], axis=1).fillna(0)
    daily_ops = daily_ops[daily_ops["checkins"] > 0].reset_index()

    if len(daily_ops) == 0:
        return pd.DataFrame()

    daily_ops["day_of_week"] = pd.to_datetime(daily_ops["date"]).dt.day_name()
    daily_ops["reject_rate"] = (daily_ops["rejects"] / daily_ops["checkins"]) * 100

    # Build daily transit counts for public transit destinations.
    transit_daily_counts = (
        df[df["transit_destination"].isin(["Westside", "Library Express"])]
        .groupby("date")
        .size()
        .reset_index(name="transit_count")
    )

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
        "Avg Reject Rate %": reject_weekday_avg,
    }).dropna(how="all")


#***************************************************************
#
#  Function:     get_destination_weekday_mix
#
#  Description: Calculates average transit volume by weekday and
#               destination. This supports charts that show whether
#               certain transit destinations are busier on specific
#               days of the week.
#
#  Parameters:  transit_df - Dataframe containing transit rows.
#               weekday_order - Ordered list of weekdays for display.
#
#  Returns:     DataFrame - Weekday-by-destination transit averages.
#
#***************************************************************

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


#***************************************************************
#
#  Function:     get_destination_reject_summary
#
#  Description: Builds a destination-level reject diagnostic summary
#               for public transit destinations. This links reject
#               records back to the most recent checkin destination
#               for the same barcode, then calculates reject counts,
#               reject rates, top reject reasons, and top reason
#               percentages by destination.
#
#  Parameters:  df - Checkin dataframe.
#               rejects_df - Reject dataframe.
#               transit_summary - Transit summary dataframe.
#               valid_transit_destinations - List of destinations to include.
#
#  Returns:     DataFrame - Transit destination reject diagnostics.
#
#***************************************************************

def get_destination_reject_summary(df, rejects_df, transit_summary, valid_transit_destinations):
    if len(df) == 0 or len(transit_summary) == 0:
        return pd.DataFrame()

    if "barcode" not in df.columns:
        return pd.DataFrame()

    summary = transit_summary.copy()

    # If reject data is unavailable, return the transit summary with
    # zeroed reject diagnostic fields.
    if len(rejects_df) == 0 or "barcode" not in rejects_df.columns:
        summary["reject_count"] = 0
        summary["reject_rate_pct"] = 0.0
        summary["top_reject_reason"] = "None"
        summary["reason_count"] = 0
        summary["top_reason_pct_of_destination_rejects"] = 0.0
        return summary

    # Map each barcode to its most recent known transit destination.
    barcode_map = (
        df.sort_values("datetime")
        .drop_duplicates(subset=["barcode"], keep="last")
        [["barcode", "destination", "transit_destination"]]
    )

    # Attach the most recent destination information to reject records.
    rejects_with_destination = rejects_df.merge(
        barcode_map,
        on="barcode",
        how="left",
    )

    transit_rejects = rejects_with_destination[
        rejects_with_destination["transit_destination"].isin(valid_transit_destinations)
    ].copy()

    if len(transit_rejects) == 0:
        summary["reject_count"] = 0
        summary["reject_rate_pct"] = 0.0
        summary["top_reject_reason"] = "None"
        summary["reason_count"] = 0
        summary["top_reason_pct_of_destination_rejects"] = 0.0
        return summary

    if "error_simple" not in transit_rejects.columns:
        transit_rejects["error_simple"] = "Unknown"

    # Count transit-linked rejects by destination.
    destination_reject_counts = (
        transit_rejects["transit_destination"]
        .value_counts()
        .rename_axis("destination")
        .reset_index(name="reject_count")
    )

    # Identify the most common reject reason for each transit destination.
    destination_top_issue_count = (
        transit_rejects.groupby(["transit_destination", "error_simple"])
        .size()
        .reset_index(name="reason_count")
        .sort_values(["transit_destination", "reason_count"], ascending=[True, False])
        .drop_duplicates(subset=["transit_destination"])
        .rename(columns={
            "transit_destination": "destination",
            "error_simple": "top_reject_reason",
        })
    )

    destination_reject_summary = summary.merge(
        destination_reject_counts,
        on="destination",
        how="left",
    )

    destination_reject_summary["reject_count"] = destination_reject_summary["reject_count"].fillna(0).astype(int)
    destination_reject_summary["reject_rate_pct"] = (
        destination_reject_summary["reject_count"] / destination_reject_summary["transit_items"] * 100
    ).round(2)

    destination_reject_summary = destination_reject_summary.merge(
        destination_top_issue_count[["destination", "top_reject_reason", "reason_count"]],
        on="destination",
        how="left",
    )

    destination_reject_summary["top_reject_reason"] = destination_reject_summary["top_reject_reason"].fillna("None")
    destination_reject_summary["reason_count"] = destination_reject_summary["reason_count"].fillna(0).astype(int)

    destination_reject_summary["top_reason_pct_of_destination_rejects"] = destination_reject_summary.apply(
        lambda row: round((row["reason_count"] / row["reject_count"]) * 100, 1)
        if row["reject_count"] > 0 else 0.0,
        axis=1,
    )

    return destination_reject_summary


#***************************************************************
#
#  Function:     get_transit_reject_insight
#
#  Description: Builds a short dashboard insight comparing weekday
#               transit volume and reject-rate patterns. The function
#               identifies whether the highest transit day and highest
#               reject-rate day match or whether the relationship is
#               weak.
#
#  Parameters:  transit_weekday_comparison - Dataframe containing
#                                            average transit items and
#                                            reject rates by weekday.
#
#  Returns:     dict - Insight title, text, and display color.
#
#***************************************************************

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

    # Strongest signal: transit volume and reject rate peak on the same day.
    if transit_peak_day == reject_peak_day:
        title = "Strong Correlation"
        text = (
            f"{transit_peak_day} has both the highest average transit load "
            f"({transit_peak_value:.0f} items/day) and highest average reject rate "
            f"({reject_peak_value:.2f}%)."
        )
        color = "#b91c1c"

    # Moderate signal: transit and reject patterns trend together.
    elif correlation is not None and correlation > 0.5:
        title = "Moderate Correlation"
        text = (
            f"Transit load and reject rate move somewhat together "
            f"(corr={correlation:.2f}), but the peak days do not match."
        )
        color = "#d97706"

    # Weak signal: peak transit and reject patterns do not align.
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


#***************************************************************
#
#  Function:     get_destination_driver_summary
#
#  Description: Builds a short diagnostic summary explaining whether
#               the destination with the highest transit volume is also
#               the destination driving the most transit-linked rejects.
#
#  Parameters:  destination_reject_summary - Destination-level transit
#                                            and reject diagnostic dataframe.
#
#  Returns:     dict - Summary text and display color.
#
#***************************************************************

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

    # Good outcome: transit volume exists, but no linked rejects were found.
    if top_reject_row["reject_count"] == 0:
        text = (
            f"{top_volume_row['destination']} drives most transit volume "
            f"({int(top_volume_row['transit_items']):,} items), but no transit-linked rejects "
            f"were found for the selected range."
        )
        color = "#059669"

    # Warning outcome: the same destination has the most volume and rejects.
    elif top_volume_row["destination"] == top_reject_row["destination"]:
        text = (
            f"{top_volume_row['destination']} leads both transit volume and transit-linked rejects: "
            f"{int(top_volume_row['transit_items']):,} items and "
            f"{int(top_reject_row['reject_count']):,} rejects. "
            f"Top issue: {top_reject_row['top_reject_reason']}."
        )
        color = "#b91c1c"

    # Mixed outcome: one destination drives volume, another drives rejects.
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
