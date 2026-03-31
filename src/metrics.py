import pandas as pd


def get_date_filtered_df(df, start_date, end_date):
    return df[
        (df["datetime"].dt.date >= start_date) &
        (df["datetime"].dt.date <= end_date)
    ].copy()


def get_today_metrics(df, rejects_df, today):
    today_df = df[df["datetime"].dt.date == today].copy()
    today_rejects_df = rejects_df[rejects_df["datetime"].dt.date == today].copy()

    today_checkins = len(today_df)
    today_rejects = len(today_rejects_df)

    today_westside = (today_df["destination"] == "Westside").sum()
    today_library_express = (today_df["destination"] == "Library Express").sum()

    if today_checkins > 0:
        today_peak_hour_counts = today_df["datetime"].dt.hour.value_counts().sort_index()
        today_peak_hour = int(today_peak_hour_counts.idxmax())
        today_peak_hour_count = int(today_peak_hour_counts.max())
        today_peak_hour_pct = (today_peak_hour_count / today_checkins) * 100
        today_reject_rate = (today_rejects / today_checkins) * 100
    else:
        today_peak_hour = None
        today_peak_hour_count = 0
        today_peak_hour_pct = 0
        today_reject_rate = 0

    return {
        "today_df": today_df,
        "today_rejects_df": today_rejects_df,
        "today_checkins": today_checkins,
        "today_rejects": today_rejects,
        "today_westside": int(today_westside),
        "today_library_express": int(today_library_express),
        "today_peak_hour": today_peak_hour,
        "today_peak_hour_count": today_peak_hour_count,
        "today_peak_hour_pct": today_peak_hour_pct,
        "today_reject_rate": today_reject_rate,
    }


def get_overall_metrics(df, rejects_df):
    if len(df) > 0:
        peak_hour_counts = df["datetime"].dt.hour.value_counts().sort_index()
        peak_hour = int(peak_hour_counts.idxmax())
        peak_hour_count = int(peak_hour_counts.max())
        peak_hour_pct = (peak_hour_count / len(df)) * 100
    else:
        peak_hour = None
        peak_hour_count = 0
        peak_hour_pct = 0

    reject_count = len(rejects_df)
    reject_pct = (reject_count / len(df) * 100) if len(df) > 0 else 0

    westside_count = (df["destination"] == "Westside").sum() if len(df) > 0 else 0
    westside_pct = (westside_count / len(df) * 100) if len(df) > 0 else 0

    library_express_count = (df["destination"] == "Library Express").sum() if len(df) > 0 else 0
    library_express_pct = (library_express_count / len(df) * 100) if len(df) > 0 else 0

    return {
        "peak_hour": peak_hour,
        "peak_hour_count": peak_hour_count,
        "peak_hour_pct": peak_hour_pct,
        "reject_count": reject_count,
        "reject_pct": reject_pct,
        "westside_count": int(westside_count),
        "westside_pct": westside_pct,
        "library_express_count": int(library_express_count),
        "library_express_pct": library_express_pct,
    }


def get_historical_reject_baseline(df, rejects_df, today):
    historical_df = df[df["datetime"].dt.date < today].copy()
    historical_rejects_df = rejects_df[rejects_df["datetime"].dt.date < today].copy()

    if len(historical_df) == 0:
        return {
            "historical_daily_avg_reject": 0,
            "live_reject_deviation": 0,
        }

    historical_checkins_daily = historical_df["datetime"].dt.date.value_counts().sort_index()
    historical_rejects_daily = historical_rejects_df["datetime"].dt.date.value_counts().sort_index()

    historical_combined = pd.DataFrame({
        "checkins": historical_checkins_daily,
        "rejects": historical_rejects_daily
    }).fillna(0)

    historical_combined = historical_combined[historical_combined["checkins"] > 0]

    if len(historical_combined) == 0:
        return {
            "historical_daily_avg_reject": 0,
            "live_reject_deviation": 0,
        }

    historical_combined["reject_rate"] = (
        historical_combined["rejects"] / historical_combined["checkins"]
    ) * 100

    return {
        "historical_daily_avg_reject": historical_combined["reject_rate"].mean(),
        "historical_combined": historical_combined,
    }