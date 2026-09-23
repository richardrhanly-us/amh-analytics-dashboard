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
    resolve_run_every_seconds,
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


# --- resolve_run_every_seconds (WCAG 2.2.2 Pause, Stop, Hide) --------------
#
# Pure function: src/app.py computes is_operating_hours(now_ct) and reads
# the user's pause choice from st.session_state itself, then calls this
# to decide the single value it passes as st.fragment's run_every. Kept
# here, tested the same way as is_operating_hours/
# resolve_refresh_interval_seconds above, so the pause/resume decision
# never needs a live Streamlit runtime to verify.


def test_run_every_enabled_by_default_during_operating_hours():
    # Default behavior (is_paused=False, the value a fresh session starts
    # with) must be unchanged from before this control existed: auto
    # refresh runs at the configured interval during operating hours.
    assert resolve_run_every_seconds(
        is_operating_hours_now=True, is_paused=False, interval_seconds=10
    ) == 10


def test_run_every_none_when_paused_during_operating_hours():
    assert resolve_run_every_seconds(
        is_operating_hours_now=True, is_paused=True, interval_seconds=10
    ) is None


def test_run_every_restores_configured_interval_when_resumed():
    # Simulates the pause -> resume transition as two independent calls
    # (the function is pure/stateless; app.py re-evaluates it every
    # rerun) -- resuming must restore the exact configured cadence, not
    # some other value.
    paused = resolve_run_every_seconds(is_operating_hours_now=True, is_paused=True, interval_seconds=25)
    resumed = resolve_run_every_seconds(is_operating_hours_now=True, is_paused=False, interval_seconds=25)
    assert paused is None
    assert resumed == 25


def test_run_every_none_outside_operating_hours_when_not_paused():
    # The pre-existing operating-hours gate must still work unmodified
    # when the user has NOT paused anything.
    assert resolve_run_every_seconds(
        is_operating_hours_now=False, is_paused=False, interval_seconds=10
    ) is None


def test_run_every_none_outside_operating_hours_when_also_paused():
    # Both reasons to be off at once must still just be off, not raise or
    # produce a contradictory value.
    assert resolve_run_every_seconds(
        is_operating_hours_now=False, is_paused=True, interval_seconds=10
    ) is None


def test_run_every_uses_the_actual_configured_interval_not_a_hardcoded_one():
    assert resolve_run_every_seconds(
        is_operating_hours_now=True, is_paused=False, interval_seconds=45
    ) == 45


# --- session-state persistence of the pause choice (src/app.py) -----------
#
# The pattern below is copied verbatim (key name, button labels, call
# shape) from src/app.py's own Live Today pause control, so this proves
# the actual mechanism -- a button click flipping st.session_state and
# surviving a rerun -- works, using the real resolve_run_every_seconds,
# without needing to stand up the full authenticated dashboard (login,
# database, entitlements) just to reach one button.

_PAUSE_CONTROL_SCRIPT = """
import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
from services.dashboard_refresh_service import is_operating_hours, resolve_run_every_seconds

LIVE_TODAY_PAUSE_KEY = "_live_today_auto_refresh_paused"
if LIVE_TODAY_PAUSE_KEY not in st.session_state:
    st.session_state[LIVE_TODAY_PAUSE_KEY] = False

live_today_paused = st.session_state[LIVE_TODAY_PAUSE_KEY]

if live_today_paused:
    if st.button("Resume live updates"):
        st.session_state[LIVE_TODAY_PAUSE_KEY] = False
        st.rerun()
else:
    if st.button("Pause live updates"):
        st.session_state[LIVE_TODAY_PAUSE_KEY] = True
        st.rerun()

# Fixed instant inside the 6am-9pm operating window, so the only thing
# under test is the pause choice, not the operating-hours gate.
now_ct = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
run_every = resolve_run_every_seconds(
    is_operating_hours_now=is_operating_hours(now_ct),
    is_paused=live_today_paused,
    interval_seconds=10,
)
st.text(f"run_every={run_every}")
"""


def _button_labeled(at, label):
    matches = [b for b in at.button if b.label == label]
    assert matches, f"no button labeled {label!r} found; buttons present: {[b.label for b in at.button]}"
    return matches[0]


def test_pause_choice_persists_in_session_state_across_reruns():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_PAUSE_CONTROL_SCRIPT)
    at.run()

    # Default (fresh session): not paused, control offers to pause, and
    # the real refresh interval is what would be scheduled.
    assert at.session_state["_live_today_auto_refresh_paused"] is False
    assert at.text[0].value == "run_every=10"
    _button_labeled(at, "Pause live updates").click().run()

    # After clicking Pause: the choice survived the rerun via
    # st.session_state, the control now offers to resume, and the
    # resolved run_every is None -- no scheduled refresh while paused.
    assert at.session_state["_live_today_auto_refresh_paused"] is True
    assert at.text[0].value == "run_every=None"
    _button_labeled(at, "Resume live updates").click().run()

    # After clicking Resume: back to the original state, and the
    # configured cadence is restored exactly.
    assert at.session_state["_live_today_auto_refresh_paused"] is False
    assert at.text[0].value == "run_every=10"
