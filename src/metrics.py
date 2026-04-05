import pandas as pd


def get_date_filtered_df(df, start_date, end_date):
    return df[
        (df["datetime"].dt.date >= start_date) &
        (df["datetime"].dt.date <= end_date)
    ].copy()



def get_today_metrics(df, rejects_df, today):
    import pandas as pd

    # safe empty fallbacks
    empty_today_df = pd.DataFrame()
    empty_today_rejects_df = pd.DataFrame()

    if df is None or not isinstance(df, pd.DataFrame) or "datetime" not in df.columns:
        df = empty_today_df.copy()
    else:
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df[df["datetime"].notna()].copy()

    if rejects_df is None or not isinstance(rejects_df, pd.DataFrame) or "datetime" not in rejects_df.columns:
        rejects_df = empty_today_rejects_df.copy()
    else:
        rejects_df = rejects_df.copy()
        rejects_df["datetime"] = pd.to_datetime(rejects_df["datetime"], errors="coerce")
        rejects_df = rejects_df[rejects_df["datetime"].notna()].copy()

    if len(df) > 0:
        try:
            if getattr(df["datetime"].dt, "tz", None) is not None:
                today_df = df[df["datetime"].dt.tz_localize(None).dt.date == today].copy()
            else:
                today_df = df[df["datetime"].dt.date == today].copy()
        except Exception:
            today_df = pd.DataFrame(columns=df.columns)
    else:
        today_df = pd.DataFrame(columns=df.columns if len(df.columns) > 0 else [])

    if len(rejects_df) > 0:
        try:
            if getattr(rejects_df["datetime"].dt, "tz", None) is not None:
                today_rejects_df = rejects_df[rejects_df["datetime"].dt.tz_localize(None).dt.date == today].copy()
            else:
                today_rejects_df = rejects_df[rejects_df["datetime"].dt.date == today].copy()
        except Exception:
            today_rejects_df = pd.DataFrame(columns=rejects_df.columns)
    else:
        today_rejects_df = pd.DataFrame(columns=rejects_df.columns if len(rejects_df.columns) > 0 else [])

    if "destination" in today_df.columns:
        today_dest_upper = today_df["destination"].astype(str).str.upper()
        today_westside = int(today_dest_upper.str.contains("WESTSIDE", na=False).sum())
        today_library_express = int(today_dest_upper.str.contains("LIBRARY EXPRESS", na=False).sum())
    else:
        today_westside = 0
        today_library_express = 0

    today_checkins = len(today_df)
    today_rejects = len(today_rejects_df)
    today_total_transit = today_westside + today_library_express

    if today_checkins > 0 and "datetime" in today_df.columns:
        hourly_counts = today_df["datetime"].dt.hour.value_counts().sort_index()
        today_peak_hour = int(hourly_counts.idxmax()) if len(hourly_counts) > 0 else None
        today_peak_hour_count = int(hourly_counts.max()) if len(hourly_counts) > 0 else 0
        today_peak_hour_pct = (today_peak_hour_count / today_checkins) * 100 if today_checkins > 0 else 0
    else:
        today_peak_hour = None
        today_peak_hour_count = 0
        today_peak_hour_pct = 0

    current_speed = 0
    if today_checkins > 0 and "datetime" in today_df.columns:
        current_hour = pd.Timestamp.now().hour
        current_speed = int((today_df["datetime"].dt.hour == current_hour).sum())

    today_reject_rate = (today_rejects / today_checkins * 100) if today_checkins > 0 else 0

    return {
        "today_df": today_df,
        "today_rejects_df": today_rejects_df,
        "today_checkins": today_checkins,
        "today_rejects": today_rejects,
        "today_total_transit": today_total_transit,
        "today_westside": today_westside,
        "today_library_express": today_library_express,
        "today_peak_hour": today_peak_hour,
        "today_peak_hour_count": today_peak_hour_count,
        "today_peak_hour_pct": today_peak_hour_pct,
        "today_reject_rate": today_reject_rate,
        "current_speed": current_speed,
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
