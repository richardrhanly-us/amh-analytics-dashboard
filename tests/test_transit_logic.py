import pandas as pd

from src.transit_logic import (
    normalize_transit_destination,
    get_transit_summary,
    compute_transit_times,
    get_transit_time_summary,
    get_peak_transit_day_summary,
    get_transit_weekday_comparison,
    get_destination_weekday_mix,
    get_destination_reject_summary,
    get_transit_reject_insight,
    get_destination_driver_summary,
)


def build_checkins_df():
    df = pd.DataFrame({
        "barcode": [
            "A", "A",
            "B", "B",
            "C", "C",
            "D",
        ],
        "datetime": pd.to_datetime([
            "2026-03-30 09:00",
            "2026-03-30 10:00",
            "2026-03-31 09:00",
            "2026-03-31 11:00",
            "2026-04-01 08:00",
            "2026-04-01 08:30",
            "2026-04-01 12:00",
        ]),
        "date": [
            pd.to_datetime("2026-03-30").date(),
            pd.to_datetime("2026-03-30").date(),
            pd.to_datetime("2026-03-31").date(),
            pd.to_datetime("2026-03-31").date(),
            pd.to_datetime("2026-04-01").date(),
            pd.to_datetime("2026-04-01").date(),
            pd.to_datetime("2026-04-01").date(),
        ],
        "destination": [
            "Main",
            "Westside",
            "Main",
            "Library Express",
            "Main",
            "Westside",
            "Main",
        ],
    })

    df["transit_destination"] = df["destination"].apply(normalize_transit_destination)
    return df


def build_rejects_df():
    df = pd.DataFrame({
        "barcode": ["A", "B", "B"],
        "datetime": pd.to_datetime([
            "2026-03-30 10:05",
            "2026-03-31 11:05",
            "2026-03-31 11:10",
        ]),
        "error_simple": [
            "Routing Error",
            "Item Not Found",
            "Item Not Found",
        ],
    })

    df["date"] = df["datetime"].dt.date
    return df


def test_normalize_transit_destination():
    assert normalize_transit_destination("WESTSIDE (2910 IH35)") == "Westside"
    assert normalize_transit_destination("LIBRARY EXPRESS") == "Library Express"
    assert normalize_transit_destination("1") == ""
    assert normalize_transit_destination("MAIN") == ""
    assert normalize_transit_destination("") == ""
    assert normalize_transit_destination(None) == ""


def test_get_transit_summary():
    df = build_checkins_df()
    summary = get_transit_summary(df)

    assert len(summary) == 2
    assert set(summary["destination"]) == {"Westside", "Library Express"}

    westside_row = summary[summary["destination"] == "Westside"].iloc[0]
    library_row = summary[summary["destination"] == "Library Express"].iloc[0]

    assert int(westside_row["transit_items"]) == 2
    assert int(library_row["transit_items"]) == 1


def test_compute_transit_times():
    df = build_checkins_df()
    result = compute_transit_times(df)

    assert len(result) == 3
    assert set(result["destination"]) == {"Westside", "Library Express"}


def test_get_transit_time_summary():
    df = build_checkins_df()

    result = get_transit_time_summary(df)

    assert set(result["destination"]) == {"Westside", "Library Express"}

    westside_row = result[result["destination"] == "Westside"].iloc[0]
    library_row = result[result["destination"] == "Library Express"].iloc[0]

    assert float(westside_row["avg_minutes"]) == 45.0
    assert float(library_row["avg_minutes"]) == 120.0


def test_get_peak_transit_day_summary():
    df = build_checkins_df()
    transit_df = df[df["transit_destination"].isin(["Westside", "Library Express"])].copy()
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    result = get_peak_transit_day_summary(transit_df, weekday_order)

    assert result["peak_transit_day_label"] in {"Monday", "Tuesday", "Wednesday"}
    assert "Avg" in result["peak_transit_day_subtitle"]


def test_get_transit_weekday_comparison():
    df = build_checkins_df()
    rejects_df = build_rejects_df()
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    result = get_transit_weekday_comparison(df, rejects_df, weekday_order)

    assert not result.empty
    assert "Avg Transit Items / Day" in result.columns
    assert "Avg Reject Rate %" in result.columns


def test_get_destination_weekday_mix():
    df = build_checkins_df()
    transit_df = df[df["transit_destination"].isin(["Westside", "Library Express"])].copy()
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    result = get_destination_weekday_mix(transit_df, weekday_order)

    assert not result.empty
    assert "Westside" in result.columns or "Library Express" in result.columns


def test_get_destination_reject_summary():
    df = build_checkins_df()
    rejects_df = build_rejects_df()
    transit_summary = get_transit_summary(df)

    result = get_destination_reject_summary(
        df=df,
        rejects_df=rejects_df,
        transit_summary=transit_summary,
        valid_transit_destinations=["Westside", "Library Express"],
    )

    assert not result.empty
    assert "reject_count" in result.columns
    assert "top_reject_reason" in result.columns

    westside_row = result[result["destination"] == "Westside"].iloc[0]
    library_row = result[result["destination"] == "Library Express"].iloc[0]

    assert int(westside_row["reject_count"]) == 1
    assert int(library_row["reject_count"]) == 2
    assert library_row["top_reject_reason"] == "Item Not Found"


def test_get_transit_reject_insight():
    comparison = pd.DataFrame(
        {
            "Avg Transit Items / Day": [10.0, 5.0],
            "Avg Reject Rate %": [4.0, 2.0],
        },
        index=["Monday", "Tuesday"]
    )

    result = get_transit_reject_insight(comparison)

    assert "title" in result
    assert "text" in result
    assert "color" in result
    assert result["title"] in {"Strong Correlation", "Moderate Correlation", "No Clear Relationship"}


def test_get_destination_driver_summary():
    summary = pd.DataFrame({
        "destination": ["Westside", "Library Express"],
        "transit_items": [20, 10],
        "reject_count": [3, 1],
        "top_reject_reason": ["Routing Error", "Item Not Found"],
    })

    result = get_destination_driver_summary(summary)

    assert "text" in result
    assert "color" in result
    assert isinstance(result["text"], str)
