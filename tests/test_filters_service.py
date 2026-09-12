"""Tests for src/services/filters_service.py's date-range default and
persistence behavior (dashboard date-range performance pass).

resolve_date_filters renders a real Streamlit sidebar radio (and, in
"Single Day" mode, a date_input), so these use Streamlit's own AppTest
harness (streamlit.testing.v1) rather than calling the function directly
-- session_state/widget-key behavior (what survives a rerun, what gets
cleared when a widget doesn't render) can't be exercised any other way.
This is exactly how a real regression was caught during development: a
first-draft fix using only a widget `key` looked correct by inspection,
but AppTest revealed Streamlit clears a keyed widget's session_state
entry on any run where that widget isn't instantiated at all -- which
happens here whenever Live Today is active, since it never renders this
radio. The two-slot design in filters_service.py (a durable non-widget
slot mirroring the widget's own key) is what actually fixes that.

The default mode itself changed twice during this work: All Time (never
measured against any budget) -> Last 30 Days (still not the lightest
practical default) -> Single Day, i.e. "Today" (the current, intended
default -- reporting views are secondary/on-demand relative to Live
Today, so their first-visit cost should be as small as practical). Only
the DEFAULT changed each time; the persistence mechanism itself has not
changed since it was first added.
"""

import pandas as pd
from streamlit.testing.v1 import AppTest

TEST_MIN_DATE = pd.Timestamp("2020-01-01").date()
TEST_TODAY = pd.Timestamp("2026-03-30").date()


def _resolve_script():
    import pandas as pd
    import streamlit as st

    from services.filters_service import resolve_date_filters

    selected_view = st.session_state.get("_test_selected_view", "Overview")
    start, end = resolve_date_filters(
        selected_view=selected_view,
        min_date=pd.Timestamp("2020-01-01").date(),
        max_date=pd.Timestamp("2026-03-30").date(),
        local_today=pd.Timestamp("2026-03-30").date(),
    )
    st.session_state["_test_result"] = (start, end)


def _run(at, selected_view):
    at.session_state["_test_selected_view"] = selected_view
    at.run()
    return at.session_state["_test_result"]


# --- 1. first reporting view in a new session defaults to Today -------------


def test_default_on_first_visit_is_today():
    at = AppTest.from_function(_resolve_script)
    start, end = _run(at, "Overview")

    assert at.sidebar.radio[0].value == "Single Day"
    assert len(at.sidebar.date_input) == 1
    assert at.sidebar.date_input[0].value == TEST_TODAY
    assert start == TEST_TODAY
    assert end == TEST_TODAY


# --- 2. switching Overview -> Reports -> Transits preserves Today initially -


def test_switching_between_reporting_views_preserves_today_default():
    at = AppTest.from_function(_resolve_script)

    for selected_view in ["Overview", "Reports", "Transits"]:
        start, end = _run(at, selected_view)
        assert at.sidebar.radio[0].value == "Single Day"
        assert start == TEST_TODAY
        assert end == TEST_TODAY


# --- 3. choosing Last 30 Days persists across those views -------------------


def test_choosing_last_30_days_persists_across_views():
    at = AppTest.from_function(_resolve_script)
    _run(at, "Overview")

    at.sidebar.radio[0].set_value("Last 30 Days").run()
    start, end = at.session_state["_test_result"]
    assert end == TEST_TODAY
    assert (end - start).days == 29

    for selected_view in ["Transits", "Reports"]:
        start, end = _run(at, selected_view)
        assert at.sidebar.radio[0].value == "Last 30 Days"
        assert (end - start).days == 29


# --- 4. choosing All Time persists across those views ------------------------


def test_choosing_all_time_persists_across_views():
    at = AppTest.from_function(_resolve_script)
    _run(at, "Overview")

    at.sidebar.radio[0].set_value("All Time").run()
    start, end = at.session_state["_test_result"]
    assert start == TEST_MIN_DATE
    assert end == TEST_TODAY

    for selected_view in ["Transits", "Reports"]:
        start, end = _run(at, selected_view)
        assert at.sidebar.radio[0].value == "All Time"
        assert start == TEST_MIN_DATE


# --- 5. explicit selection survives a detour through Live Today -------------


def test_explicit_choice_survives_a_detour_through_live_today():
    # Live Today never renders this radio at all. A naive
    # key-only-on-first-use implementation loses the user's choice here,
    # because Streamlit clears a keyed widget's session_state entry on
    # any run where the widget isn't instantiated -- this is the
    # regression this test guards against.
    at = AppTest.from_function(_resolve_script)
    _run(at, "Overview")
    at.sidebar.radio[0].set_value("All Time").run()

    _run(at, "Live Today")
    _run(at, "Reports")

    assert at.sidebar.radio[0].value == "All Time"
    start, _ = at.session_state["_test_result"]
    assert start == TEST_MIN_DATE


# --- 6. returning to a reporting view does not silently reset to Today ------


def test_returning_to_reporting_view_does_not_reset_to_today():
    # Same shape as #5 but with a different, non-All-Time mode, to prove
    # the fix isn't specific to one particular selection.
    at = AppTest.from_function(_resolve_script)
    _run(at, "Transits")
    at.sidebar.radio[0].set_value("Last 7 Days").run()

    _run(at, "Live Today")
    start, end = _run(at, "Transits")

    assert at.sidebar.radio[0].value == "Last 7 Days"
    assert at.sidebar.radio[0].value != "Single Day"
    assert (end - start).days == 6


def test_live_today_never_renders_the_date_range_radio():
    at = AppTest.from_function(_resolve_script)
    _run(at, "Live Today")

    assert len(at.sidebar.radio) == 0
