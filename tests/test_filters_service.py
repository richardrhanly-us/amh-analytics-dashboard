"""Tests for src/services/filters_service.py's date-range default and
persistence behavior (dashboard date-range performance pass).

resolve_date_filters renders a real Streamlit sidebar radio, so these use
Streamlit's own AppTest harness (streamlit.testing.v1) rather than
calling the function directly -- session_state/widget-key behavior
(what survives a rerun, what gets cleared when a widget doesn't render)
can't be exercised any other way. This is exactly how a real regression
was caught during development: a first-draft fix using only a widget
`key` looked correct by inspection, but AppTest revealed Streamlit clears
a keyed widget's session_state entry on any run where that widget isn't
instantiated at all -- which happens here whenever Live Today is active,
since it never renders this radio. The two-slot design below (a durable
non-widget slot mirroring the widget's own key) is what actually fixes
that.
"""

import pandas as pd
from streamlit.testing.v1 import AppTest


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


def test_default_on_first_visit_is_last_30_days():
    at = AppTest.from_function(_resolve_script)
    start, end = _run(at, "Overview")

    assert at.sidebar.radio[0].value == "Last 30 Days"
    assert end == pd.Timestamp("2026-03-30").date()
    assert (end - start).days == 29


def test_all_time_still_available_and_spans_full_history():
    at = AppTest.from_function(_resolve_script)
    _run(at, "Overview")

    at.sidebar.radio[0].set_value("All Time").run()
    start, end = at.session_state["_test_result"]

    assert start == pd.Timestamp("2020-01-01").date()
    assert end == pd.Timestamp("2026-03-30").date()


def test_explicit_choice_survives_switching_between_reporting_views():
    # Overview -> Transits -> Reports all render the SAME shared widget
    # (resolve_date_filters is called from one call site regardless of
    # selected_view) -- an explicit choice must not reset just because
    # the active reporting view changed.
    at = AppTest.from_function(_resolve_script)
    _run(at, "Overview")
    at.sidebar.radio[0].set_value("All Time").run()

    _run(at, "Transits")
    assert at.sidebar.radio[0].value == "All Time"

    _run(at, "Reports")
    assert at.sidebar.radio[0].value == "All Time"


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
    assert start == pd.Timestamp("2020-01-01").date()


def test_live_today_never_renders_the_date_range_radio():
    at = AppTest.from_function(_resolve_script)
    _run(at, "Live Today")

    assert len(at.sidebar.radio) == 0
