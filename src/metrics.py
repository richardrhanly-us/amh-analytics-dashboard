import re
import pandas as pd

def get_date_filtered_df(df, start_date, end_date):
    if df is None or not isinstance(df, pd.DataFrame) or "datetime" not in df.columns:
        return pd.DataFrame()

    work_df = df.copy()
    work_df["datetime"] = pd.to_datetime(work_df["datetime"], errors="coerce")
    work_df = work_df[work_df["datetime"].notna()].copy()

    return work_df[
        (work_df["datetime"].dt.date >= start_date)
        & (work_df["datetime"].dt.date <= end_date)
    ].copy()




def get_today_metrics(df, rejects_df, today):
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
                today_rejects_df = rejects_df[
                    rejects_df["datetime"].dt.tz_localize(None).dt.date == today
                ].copy()
            else:
                today_rejects_df = rejects_df[rejects_df["datetime"].dt.date == today].copy()
        except Exception:
            today_rejects_df = pd.DataFrame(columns=rejects_df.columns)
    else:
        today_rejects_df = pd.DataFrame(columns=rejects_df.columns if len(rejects_df.columns) > 0 else [])

    if "destination" in today_df.columns:
        today_dest_upper = today_df["destination"].fillna("").astype(str).str.upper()
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
    if df is None or not isinstance(df, pd.DataFrame):
        df = pd.DataFrame()

    if rejects_df is None or not isinstance(rejects_df, pd.DataFrame):
        rejects_df = pd.DataFrame()

    if len(df) > 0 and "datetime" in df.columns:
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

    if len(df) > 0 and "destination" in df.columns:
        dest_upper = df["destination"].fillna("").astype(str).str.upper()
        westside_count = dest_upper.str.contains("WESTSIDE", na=False).sum()
        westside_pct = (westside_count / len(df) * 100) if len(df) > 0 else 0

        library_express_count = dest_upper.str.contains("LIBRARY EXPRESS", na=False).sum()
        library_express_pct = (library_express_count / len(df) * 100) if len(df) > 0 else 0
    else:
        westside_count = 0
        westside_pct = 0
        library_express_count = 0
        library_express_pct = 0

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
    if df is None or not isinstance(df, pd.DataFrame) or "datetime" not in df.columns:
        return {
            "historical_daily_avg_reject": 0,
            "historical_combined": pd.DataFrame(),
        }

    if rejects_df is None or not isinstance(rejects_df, pd.DataFrame) or "datetime" not in rejects_df.columns:
        rejects_df = pd.DataFrame(columns=["datetime"])

    historical_df = df[df["datetime"].dt.date < today].copy()
    historical_rejects_df = rejects_df[rejects_df["datetime"].dt.date < today].copy()

    if len(historical_df) == 0:
        return {
            "historical_daily_avg_reject": 0,
            "historical_combined": pd.DataFrame(),
        }

    historical_checkins_daily = historical_df["datetime"].dt.date.value_counts().sort_index()
    historical_rejects_daily = historical_rejects_df["datetime"].dt.date.value_counts().sort_index()

    historical_combined = pd.DataFrame({
        "checkins": historical_checkins_daily,
        "rejects": historical_rejects_daily,
    }).fillna(0)

    historical_combined = historical_combined[historical_combined["checkins"] > 0]

    if len(historical_combined) == 0:
        return {
            "historical_daily_avg_reject": 0,
            "historical_combined": pd.DataFrame(),
        }

    historical_combined["reject_rate"] = (
        historical_combined["rejects"] / historical_combined["checkins"]
    ) * 100

    return {
        "historical_daily_avg_reject": historical_combined["reject_rate"].mean(),
        "historical_combined": historical_combined,
    }

def build_roi_payload(
    df,
    start_date,
    end_date,
    hourly_cost=18.0,
    upfront_cost=200000.0,
    monthly_cost=0.0,
    yearly_cost=8000.0,
    install_date=None,
    include_upfront_in_since_install=True,
):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) == 0 or "datetime" not in df.columns:
        return None

    work_df = df.copy()
    work_df["datetime"] = pd.to_datetime(work_df["datetime"], errors="coerce")
    work_df = work_df[work_df["datetime"].notna()].copy()

    if len(work_df) == 0:
        return None

    manual_rate = 45

    work_df["date"] = work_df["datetime"].dt.date
    work_df["hour"] = work_df["datetime"].dt.hour

    daily_hourly = (
        work_df.groupby(["date", "hour"])
        .size()
        .reset_index(name="checkins")
    )

    avg_hourly = (
        daily_hourly.groupby("hour")["checkins"]
        .mean()
        .reset_index(name="avg_items_per_hour")
    )

    if len(avg_hourly) > 0:
        peak_row = avg_hourly.loc[avg_hourly["avg_items_per_hour"].idxmax()]
        threshold = peak_row["avg_items_per_hour"] * 0.75
        peak_hours = avg_hourly[avg_hourly["avg_items_per_hour"] >= threshold].copy()

        amh_rate = (
            peak_hours["avg_items_per_hour"].mean()
            if len(peak_hours) > 0
            else peak_row["avg_items_per_hour"]
        )
    else:
        amh_rate = 130.0

    daily_counts = work_df["datetime"].dt.date.value_counts().sort_index()
    staff_df = daily_counts.reset_index()
    staff_df.columns = ["date", "checkins"]

    staff_df["manual_hours"] = staff_df["checkins"] / manual_rate
    staff_df["amh_hours"] = staff_df["checkins"] / amh_rate
    staff_df["hours_saved"] = (staff_df["manual_hours"] - staff_df["amh_hours"]).clip(lower=0)

    total_saved_hours = float(staff_df["hours_saved"].sum())
    labor_value_saved = total_saved_hours * hourly_cost

    days_in_range = max((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 1, 1)
    months_in_range = days_in_range / 30.44
    years_in_range = days_in_range / 365.25

    observed_operating_cost = (monthly_cost * months_in_range) + (yearly_cost * years_in_range)
    observed_total_cost = upfront_cost + observed_operating_cost

    # Selected-range "observed net" should subtract recurring operating cost only
    observed_net_operating_value = labor_value_saved - observed_operating_cost
    observed_roi_pct = (
        (observed_net_operating_value / observed_operating_cost) * 100
        if observed_operating_cost > 0
        else None
    )

    annual_labor_value = labor_value_saved * (12 / months_in_range) if months_in_range > 0 else 0.0
    annual_operating_cost = (monthly_cost * 12) + yearly_cost
    annual_net_value = annual_labor_value - annual_operating_cost
    annual_roi_pct = (
        (annual_net_value / annual_operating_cost) * 100
        if annual_operating_cost > 0
        else None
    )

    payback_months = None
    if annual_net_value > 0:
        payback_months = (upfront_cost / annual_net_value) * 12

    installed_years = None
    since_install_labor_value = None
    since_install_operating_cost = None
    since_install_total_cost = None
    since_install_net_value = None
    since_install_roi_pct = None

    if install_date is not None:
        install_date_ts = pd.to_datetime(install_date)
        today_ts = pd.Timestamp.today().normalize()
        installed_days = max((today_ts - install_date_ts).days, 1)
        installed_years = installed_days / 365.25

        since_install_labor_value = annual_labor_value * installed_years
        since_install_operating_cost = annual_operating_cost * installed_years

        if include_upfront_in_since_install:
            since_install_total_cost = upfront_cost + since_install_operating_cost
        else:
            since_install_total_cost = since_install_operating_cost

        since_install_net_value = since_install_labor_value - since_install_total_cost
        since_install_roi_pct = (
            (since_install_net_value / since_install_total_cost) * 100
            if since_install_total_cost and since_install_total_cost > 0
            else None
        )

    return {
        "manual_rate": manual_rate,
        "amh_rate": amh_rate,
        "total_saved_hours": total_saved_hours,
        "labor_value_saved": labor_value_saved,
        "hourly_cost": hourly_cost,
        "upfront_cost": upfront_cost,
        "monthly_cost": monthly_cost,
        "yearly_cost": yearly_cost,

        "days_in_range": days_in_range,
        "months_in_range": months_in_range,
        "years_in_range": years_in_range,

        "observed_operating_cost": observed_operating_cost,
        "observed_total_cost": observed_total_cost,
        "observed_net_operating_value": observed_net_operating_value,
        "observed_net_value": observed_net_operating_value,
        "observed_roi_pct": observed_roi_pct,

        "annual_labor_value": annual_labor_value,
        "annual_operating_cost": annual_operating_cost,
        "annual_net_value": annual_net_value,
        "annual_roi_pct": annual_roi_pct,

        # backward-compatible names expected by reports/overview
        "net_roi_value": annual_net_value,
        "total_roi_cost": annual_operating_cost,
        "roi_pct": annual_roi_pct,

        "payback_months": payback_months,

        "installed_years": installed_years,
        "since_install_labor_value": since_install_labor_value,
        "since_install_operating_cost": since_install_operating_cost,
        "since_install_total_cost": since_install_total_cost,
        "since_install_net_value": since_install_net_value,
        "since_install_roi_pct": since_install_roi_pct,
    }


def build_acs_item_summary(
    acs_df,
    transit_labels,
    branch_services_names,
    collection_services_names,
    branch_services_da_patterns,
    collection_services_da_patterns,
):
    if acs_df is None or len(acs_df) == 0:
        return {
            "holds_total": 0,
            "ill_total": 0,
            "programming_total": 0,
            "collection_services_total": 0,
            "ill_main": 0,
            "ill_by_branch": {},
            "items_df": pd.DataFrame(),
            "holds_df": pd.DataFrame(),
            "ill_df": pd.DataFrame(),
            "programming_df": pd.DataFrame(),
            "collection_services_df": pd.DataFrame(),
        }

    df = acs_df.copy()
    df["raw_message"] = df["raw_message"].fillna("").astype(str)
    df["message_code"] = df["message_code"].astype(str).str.strip()

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    items = df[df["raw_message"].str.startswith("101", na=False)].copy()

    if len(items) == 0:
        return {
            "holds_total": 0,
            "ill_total": 0,
            "programming_total": 0,
            "collection_services_total": 0,
            "ill_main": 0,
            "ill_by_branch": {},
            "items_df": pd.DataFrame(),
            "holds_df": pd.DataFrame(),
            "ill_df": pd.DataFrame(),
            "programming_df": pd.DataFrame(),
            "collection_services_df": pd.DataFrame(),
        }

    items = items.sort_values("datetime")
    items = items.drop_duplicates(subset=["barcode"], keep="last")

    items["is_hold"] = items["raw_message"].str.startswith("101YNY", na=False)

    patrons = df[df["message_code"] == "64"].copy()

    if len(patrons) > 0:
        patrons = patrons.sort_values("datetime")
        patrons = patrons.drop_duplicates(subset=["patron_id"], keep="last")
        patrons["patron_name"] = (
            patrons["raw_message"]
            .str.extract(r"\|AE([^|]*)", expand=False)
            .fillna("")
            .astype(str)
            .str.strip()
        )
        patrons["patron_type"] = (
            patrons["raw_message"]
            .str.extract(r"\|PT([^|]*)", expand=False)
            .fillna("")
            .astype(str)
            .str.strip()
        )
    else:
        patrons = pd.DataFrame(columns=["patron_id", "patron_name", "patron_type"])

    items = items.merge(
        patrons[["patron_id", "patron_name", "patron_type"]],
        on="patron_id",
        how="left"
    )

    items["patron_name"] = items["patron_name"].fillna("").astype(str).str.strip()
    items["patron_type"] = items["patron_type"].fillna("").astype(str).str.strip()

    items["patron_name_upper"] = items["patron_name"].fillna("").astype(str).str.upper()
    items["raw_upper"] = items["raw_message"].fillna("").astype(str).str.upper()
    items["destination_upper"] = items["destination"].fillna("").astype(str).str.upper()

    items["is_ill"] = (
        items["patron_type"].str.upper().eq("ILL")
        | items["destination_upper"].str.contains(r"\bILL\b|INTERLIBRARY", regex=True, na=False)
    )

    items["is_collection_services"] = items["patron_name_upper"].isin(collection_services_names)

    for pattern in collection_services_da_patterns:
        escaped_pattern = re.escape(f"|{pattern}|")
        items["is_collection_services"] = (
            items["is_collection_services"]
            | items["raw_upper"].str.contains(escaped_pattern, na=False)
        )

    items["is_programming"] = items["patron_name_upper"].isin(branch_services_names)

    for pattern in branch_services_da_patterns:
        escaped_pattern = re.escape(f"|{pattern}|")
        items["is_programming"] = (
            items["is_programming"]
            | items["raw_upper"].str.contains(escaped_pattern, na=False)
        )

    holds_df = items[items["is_hold"]].copy()
    ill_df = holds_df[holds_df["is_ill"]].copy()
    programming_df = holds_df[holds_df["is_programming"]].copy()
    collection_services_df = holds_df[holds_df["is_collection_services"]].copy()

    internal_mask = (
        holds_df["is_ill"]
        | holds_df["is_programming"]
        | holds_df["is_collection_services"]
    )

    public_holds_df = holds_df[~internal_mask].copy()

    ill_dest_upper = ill_df["destination"].fillna("").astype(str).str.strip().str.upper()

    ill_by_branch = {}
    for transit_label in transit_labels:
        ill_by_branch[transit_label] = int((ill_dest_upper == transit_label.upper()).sum())

    ill_main_count = int(
        (~ill_dest_upper.isin([label.upper() for label in transit_labels])).sum()
    )

    return {
        "holds_total": int(len(public_holds_df)),
        "ill_total": int(len(ill_df)),
        "programming_total": int(len(programming_df)),
        "collection_services_total": int(len(collection_services_df)),
        "ill_main": ill_main_count,
        "ill_by_branch": ill_by_branch,
        "items_df": items,
        "holds_df": public_holds_df,
        "ill_df": ill_df,
        "programming_df": programming_df,
        "collection_services_df": collection_services_df,
    }
