"""Tests for src/services/dashboard_refresh_service.py -- the Phase 4
live refresh interval resolver and operating-hours gate. Pure functions,
no Streamlit runtime or DB access needed.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from services.dashboard_refresh_service import (
    DEFAULT_REFRESH_SECONDS,
    is_operating_hours,
    resolve_refresh_interval_seconds,
)

APP_TZ = ZoneInfo("America/Chicago")


def test_default_is_ten_seconds():
    assert DEFAULT_REFRESH_SECONDS == 10


def test_missing_env_value_falls_back_to_default():
    seconds, warning = resolve_refresh_interval_seconds(None)
    assert seconds == 10
    assert warning is None


def test_empty_string_env_value_falls_back_to_default():
    seconds, warning = resolve_refresh_interval_seconds("")
    assert seconds == 10
    assert warning is None


def test_whitespace_only_env_value_falls_back_to_default():
    seconds, warning = resolve_refresh_interval_seconds("   ")
    assert seconds == 10
    assert warning is None


def test_valid_positive_integer_overrides_default():
    seconds, warning = resolve_refresh_interval_seconds("15")
    assert seconds == 15
    assert warning is None


def test_valid_value_with_surrounding_whitespace_is_trimmed():
    seconds, warning = resolve_refresh_interval_seconds("  20  ")
    assert seconds == 20
    assert warning is None


def test_zero_falls_back_safely_with_warning():
    seconds, warning = resolve_refresh_interval_seconds("0")
    assert seconds == 10
    assert warning is not None
    assert "0" in warning


def test_negative_falls_back_safely_with_warning():
    seconds, warning = resolve_refresh_interval_seconds("-5")
    assert seconds == 10
    assert warning is not None


def test_non_integer_falls_back_safely_with_warning():
    seconds, warning = resolve_refresh_interval_seconds("not-a-number")
    assert seconds == 10
    assert warning is not None


def test_float_string_falls_back_safely_with_warning():
    # int("10.5") raises ValueError -- must not crash, must fall back.
    seconds, warning = resolve_refresh_interval_seconds("10.5")
    assert seconds == 10
    assert warning is not None


def test_resolve_never_raises_on_arbitrary_garbage():
    for garbage in ["NaN", "Infinity", "--5", "1e10", "  ", "\t\n", "0x10"]:
        seconds, _warning = resolve_refresh_interval_seconds(garbage)
        assert seconds > 0


# --- is_operating_hours -----------------------------------------------------


def test_operating_hours_true_at_start_of_window():
    assert is_operating_hours(datetime(2026, 8, 28, 6, 0, tzinfo=APP_TZ)) is True


def test_operating_hours_true_mid_day():
    assert is_operating_hours(datetime(2026, 8, 28, 12, 30, tzinfo=APP_TZ)) is True


def test_operating_hours_false_just_before_window():
    assert is_operating_hours(datetime(2026, 8, 28, 5, 59, tzinfo=APP_TZ)) is False


def test_operating_hours_false_at_end_of_window():
    # 21:00 (9pm) is exclusive -- window is 6am <= hour < 21.
    assert is_operating_hours(datetime(2026, 8, 28, 21, 0, tzinfo=APP_TZ)) is False


def test_operating_hours_true_one_minute_before_close():
    assert is_operating_hours(datetime(2026, 8, 28, 20, 59, tzinfo=APP_TZ)) is True


def test_operating_hours_false_overnight():
    assert is_operating_hours(datetime(2026, 8, 28, 2, 0, tzinfo=APP_TZ)) is False
