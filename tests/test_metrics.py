import pandas as pd
from src.metrics import (
    get_date_filtered_df,
    get_today_metrics,
    get_overall_metrics,
    get_historical_reject_baseline,
)


def build_checkins_df():
    return pd.DataFrame({
        "datetime": pd.to_datetime([
            "2026-03-29 09:00",
            "2026-03-29 10:00",
            "2026-03-30 09:00",
            "2026-03-30 09:30",
            "2026-03-30 11:00",
        ]),
        "destination": [
            "Main",
            "Westside",
            "Westside",
            "Library Express",
            "Main",
        ]
    })


def build_rejects_df():
    return pd.DataFrame({
        "datetime": pd.to_datetime([
            "2026-03-29 09:15",
            "2026-03-30 09:10",
        ])
    })


def test_get_date_filtered_df():
    df = build_checkins_df()

    result = get_date_filtered_df(
        df,
        start_date=pd.to_datetime("2026-03-30").date(),
        end_date=pd.to_datetime("2026-03-30").date(),
    )

    assert len(result) == 3
    assert result["datetime"].dt.date.min() == pd.to_datetime("2026-03-30").date()
    assert result["datetime"].dt.date.max() == pd.to_datetime("2026-03-30").date()


def test_get_today_metrics():
    df = build_checkins_df()
    rejects_df = build_rejects_df()
    today = pd.to_datetime("2026-03-30").date()

    result = get_today_metrics(df, rejects_df, today)

    assert result["today_checkins"] == 3
    assert result["today_rejects"] == 1
    assert result["today_westside"] == 1
    assert result["today_library_express"] == 1
    assert result["today_peak_hour"] == 9
    assert result["today_peak_hour_count"] == 2
    assert round(result["today_peak_hour_pct"], 2) == round((2 / 3) * 100, 2)
    assert round(result["today_reject_rate"], 2) == round((1 / 3) * 100, 2)


def test_get_today_metrics_when_no_checkins():
    df = build_checkins_df()
    rejects_df = build_rejects_df()
    today = pd.to_datetime("2026-04-01").date()

    result = get_today_metrics(df, rejects_df, today)

    assert result["today_checkins"] == 0
    assert result["today_rejects"] == 0
    assert result["today_westside"] == 0
    assert result["today_library_express"] == 0
    assert result["today_peak_hour"] is None
    assert result["today_peak_hour_count"] == 0
    assert result["today_peak_hour_pct"] == 0
    assert result["today_reject_rate"] == 0


def test_get_overall_metrics():
    df = build_checkins_df()
    rejects_df = build_rejects_df()

    result = get_overall_metrics(df, rejects_df)

    assert result["peak_hour"] == 9
    assert result["peak_hour_count"] == 3
    assert round(result["peak_hour_pct"], 2) == 60.00
    assert result["reject_count"] == 2
    assert round(result["reject_pct"], 2) == 40.00
    assert result["westside_count"] == 2
    assert round(result["westside_pct"], 2) == 40.00
    assert result["library_express_count"] == 1
    assert round(result["library_express_pct"], 2) == 20.00


def test_get_overall_metrics_when_empty():
    df = pd.DataFrame({
        "datetime": pd.to_datetime([]),
        "destination": pd.Series(dtype="object"),
    })
    rejects_df = pd.DataFrame({
        "datetime": pd.to_datetime([]),
    })

    result = get_overall_metrics(df, rejects_df)

    assert result["peak_hour"] is None
    assert result["peak_hour_count"] == 0
    assert result["peak_hour_pct"] == 0
    assert result["reject_count"] == 0
    assert result["reject_pct"] == 0
    assert result["westside_count"] == 0
    assert result["westside_pct"] == 0
    assert result["library_express_count"] == 0
    assert result["library_express_pct"] == 0


def test_get_historical_reject_baseline():
    df = build_checkins_df()
    rejects_df = build_rejects_df()
    today = pd.to_datetime("2026-03-30").date()

    result = get_historical_reject_baseline(df, rejects_df, today)

    assert round(result["historical_daily_avg_reject"], 2) == 50.00
    assert "historical_combined" in result
    assert len(result["historical_combined"]) == 1


def test_get_historical_reject_baseline_when_no_history():
    df = build_checkins_df()
    rejects_df = build_rejects_df()
    today = pd.to_datetime("2026-03-29").date()

    result = get_historical_reject_baseline(df, rejects_df, today)

    assert result["historical_daily_avg_reject"] == 0
    assert result["live_reject_deviation"] == 0
