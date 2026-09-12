"""Tests for the historical-baseline caching in
src/services/live_context_service.py (dashboard performance pass).

_build_historical_baseline wraps three full-history scans (highest
observed hourly throughput, historical transit percentages, historical
average daily reject rate) that build_live_context previously recomputed
from scratch on every single Live Today render -- including every ~10s
auto-refresh tick, regardless of whether df_history_raw had actually
changed since the last tick. These tests prove the cache actually avoids
repeat work for identical inputs, still recomputes when the underlying
history changes, and preserves the exact values the prior inline code
produced.
"""

import pandas as pd
import pytest

from services import live_context_service as lcs


@pytest.fixture(autouse=True)
def _clear_baseline_cache():
    # st.cache_data's cache is process-global; isolate this file's tests
    # from each other and from other test modules, matching the existing
    # convention in tests/test_data_loader_refresh.py.
    lcs._build_historical_baseline.clear()
    yield
    lcs._build_historical_baseline.clear()


def _history_df(dates, destination="Main"):
    return pd.DataFrame({
        "datetime": pd.to_datetime(dates),
        "destination": [destination] * len(dates),
    })


def _rejects_df(dates):
    return pd.DataFrame({
        "datetime": pd.to_datetime(dates),
        "error_message": ["Item Not Found"] * len(dates),
    })


def _counting_wrapper(monkeypatch, calls):
    original = lcs.get_historical_reject_baseline

    def counting(df, rejects_df, today):
        calls.append(1)
        return original(df, rejects_df, today)

    monkeypatch.setattr(lcs, "get_historical_reject_baseline", counting)


# --- cache behavior: identical inputs are not recomputed --------------------


def test_historical_baseline_not_recomputed_for_identical_inputs(monkeypatch):
    calls = []
    _counting_wrapper(monkeypatch, calls)

    df_history = _history_df(["2026-03-28 09:00", "2026-03-29 09:00"])
    rejects_history = _rejects_df(["2026-03-28 09:05"])
    today = pd.Timestamp("2026-03-30").date()

    for _ in range(5):
        lcs._build_historical_baseline(df_history, rejects_history, today, ("Westside",))

    assert len(calls) == 1


def test_historical_baseline_recomputes_when_history_changes(monkeypatch):
    calls = []
    _counting_wrapper(monkeypatch, calls)

    today = pd.Timestamp("2026-03-30").date()
    rejects_history = _rejects_df(["2026-03-28 09:05"])

    lcs._build_historical_baseline(_history_df(["2026-03-28 09:00"]), rejects_history, today, ("Westside",))
    lcs._build_historical_baseline(
        _history_df(["2026-03-28 09:00", "2026-03-29 09:00"]), rejects_history, today, ("Westside",)
    )

    assert len(calls) == 2


def test_historical_baseline_recomputes_when_transit_labels_change(monkeypatch):
    calls = []
    _counting_wrapper(monkeypatch, calls)

    df_history = _history_df(["2026-03-28 09:00"])
    rejects_history = _rejects_df([])
    today = pd.Timestamp("2026-03-30").date()

    lcs._build_historical_baseline(df_history, rejects_history, today, ("Westside",))
    lcs._build_historical_baseline(df_history, rejects_history, today, ("Library Express",))

    assert len(calls) == 2


# --- correctness: values match what the prior inline code computed ----------


def test_max_observed_hourly_throughput_matches_busiest_historical_hour():
    df_history = _history_df([
        "2026-03-28 09:00", "2026-03-28 09:15", "2026-03-28 09:30",
        "2026-03-29 10:00",
    ])

    result = lcs._build_historical_baseline(
        df_history, _rejects_df([]), pd.Timestamp("2026-03-30").date(), ()
    )

    assert result["max_observed_hourly_throughput"] == 3


def test_historical_transit_pct_map_excludes_todays_rows():
    df_history = pd.DataFrame({
        "datetime": pd.to_datetime([
            "2026-03-29 09:00", "2026-03-29 10:00", "2026-03-30 09:00",
        ]),
        "destination": ["Westside", "Main", "Westside"],
    })
    today = pd.Timestamp("2026-03-30").date()

    result = lcs._build_historical_baseline(df_history, _rejects_df([]), today, ("Westside",))

    # Only the two 2026-03-29 rows are historical (strictly before today);
    # one of them is Westside -> 50%. Today's Westside row must not count.
    assert result["historical_transit_pct_map"]["Westside"] == 50.0


def test_historical_daily_avg_reject_matches_get_historical_reject_baseline():
    df_history = _history_df(["2026-03-28 09:00", "2026-03-29 09:00"])
    rejects_history = _rejects_df(["2026-03-28 09:05"])
    today = pd.Timestamp("2026-03-30").date()

    expected = lcs.get_historical_reject_baseline(df_history, rejects_history, today)
    result = lcs._build_historical_baseline(df_history, rejects_history, today, ())

    assert result["historical_daily_avg_reject"] == expected["historical_daily_avg_reject"]
