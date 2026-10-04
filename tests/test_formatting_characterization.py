"""Characterization tests for the pure formatting helpers in src/ui_components.py:
format_hour, format_hour_plain and format_relative_time.

These pin the CURRENT output exactly -- including quirks -- so a later move
of these helpers out of ui_components (Phase 0, Block 1c) can prove it was
behavior-preserving. They are not a statement that every output below is
desirable: a deliberate behavior change belongs in its own change, with
these expectations updated alongside it.

Pure functions; no Streamlit runtime or database access needed.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import ui_components as ui

# The exact suffix markup format_hour appends. Pinned verbatim: the Live
# Today / Overview KPI cards and the filtered-context attention text render
# this string as trusted HTML.
AM = "<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>AM</span>"
PM = "<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>PM</span>"

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # freshness: allow FRESH004 -- passed as now_value at every call


# --- format_hour ------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (None, "N/A"),
        (0, f"12:00{AM}"),
        (7, f"7:00{AM}"),
        (11, f"11:00{AM}"),
        (12, f"12:00{PM}"),
        (13, f"1:00{PM}"),
        (23, f"11:00{PM}"),
        (np.int64(9), f"9:00{AM}"),  # a pandas groupby/idxmax result
    ],
)
def test_format_hour_output(hour, expected):
    assert ui.format_hour(hour) == expected


def test_format_hour_renders_a_float_hour_verbatim():
    # Quirk, pinned as-is: the hour is interpolated with an f-string, not cast to int.
    assert ui.format_hour(7.0) == f"7.0:00{AM}"


# --- format_hour_plain ------------------------------------------------------


@pytest.mark.parametrize("missing", [None, float("nan"), pd.NA])
def test_format_hour_plain_missing_values(missing):
    assert ui.format_hour_plain(missing) == "N/A"


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, "12:00 AM"),
        (7, "07:00 AM"),  # zero-padded (strftime %I), unlike format_hour
        (12, "12:00 PM"),
        (13, "01:00 PM"),
        (23, "11:00 PM"),
        (7.0, "07:00 AM"),  # cast to int first, unlike format_hour
        (np.int64(9), "09:00 AM"),
    ],
)
def test_format_hour_plain_output(hour, expected):
    assert ui.format_hour_plain(hour) == expected


# --- format_relative_time ---------------------------------------------------


def test_format_relative_time_missing_value():
    assert ui.format_relative_time(None, NOW) == "N/A"


@pytest.mark.parametrize(
    ("seconds_ago", "expected"),
    [
        (0, "just now"),
        (59, "just now"),
        (60, "1 min ago"),
        (119, "1 min ago"),
        (120, "2 min ago"),
        (3599, "59 min ago"),
        (3600, "1 hr ago"),
        (7199, "1 hr ago"),
        (7200, "2 hrs ago"),
        (86399, "23 hrs ago"),
        (86400, "1 day ago"),
        (172799, "1 day ago"),
        (172800, "2 days ago"),
    ],
)
def test_format_relative_time_buckets(seconds_ago, expected):
    assert ui.format_relative_time(NOW - timedelta(seconds=seconds_ago), NOW) == expected


def test_format_relative_time_reports_a_future_time_as_just_now():
    # Quirk, pinned as-is: a negative age falls into the "< 1 minute" bucket.
    assert ui.format_relative_time(NOW + timedelta(minutes=5), NOW) == "just now"


def test_format_relative_time_raises_on_nat():
    # Quirk, pinned as-is: only None is treated as missing; NaT reaches int(NaN).
    with pytest.raises(ValueError):
        ui.format_relative_time(pd.NaT, NOW)
