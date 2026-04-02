import pandas as pd


def get_date_filtered_df(df, start_date, end_date):
    return df[
        (df["datetime"].dt.date >= start_date) &
        (df["datetime"].dt.date <= end_date)
    ].copy()


def get_today_metrics(df, rejects_df, today):
    today_df = df[
        df["datetime"].dt.tz_localize(None).dt.date == today
    ].copy()
    today_rejects_df = rejects_df[rejects_df["datetime"].dt.date == today].copy()

    today_checkins = len(today_df)
    # --- Throughput metrics ---
    if len(today_df) > 0:
        hourly_counts = today_df["datetime"].dt.hour.value_counts().sort_index()
        from zoneinfo import ZoneInfo
        from datetime import datetime
        
        current_hour = datetime.now(ZoneInfo("America/Chicago")).hour

        current_speed = int(hourly_counts.get(current_hour, 0))
        peak_speed = int(hourly_counts.max())
    else:
        current_speed = 0
        peak_speed = 0
    
    
    today_rejects = len(today_rejects_df)

    MANUAL_RATE = 120  # items per hour (conservative estimate)
    staff_hours_saved = today_checkins / MANUAL_RATE if today_checkins > 0 else 0

    today_westside = today_df["destination"].astype(str).str.upper().str.contains("WESTSIDE", na=False).sum()
    today_library_express = today_df["destination"].astype(str).str.upper().str.contains("LIBRARY EXPRESS", na=False).sum()
    today_total_transit = today_westside + today_library_express

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
        "staff_hours_saved": staff_hours_saved,
        "current_speed": current_speed,
        "peak_speed": peak_speed,
        "today_total_transit": int(today_total_transit),
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

    westside_count = df["destination"].astype(str).str.upper().str.contains("WESTSIDE", na=False).sum() if len(df) > 0 else 0
    westside_pct = (westside_count / len(df) * 100) if len(df) > 0 else 0

    library_express_count = df["destination"].astype(str).str.upper().str.contains("LIBRARY EXPRESS", na=False).sum() if len(df) > 0 else 0
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
