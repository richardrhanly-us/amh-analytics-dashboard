"""Tests for this round's dashboard wiring: Live Today / Overview / Transits / Reports consuming the mixed-era read
model, the coexistence-aware pipeline status, manual-refresh cache-busting through the new loaders, and tenant
isolation. Government-readiness audit, dashboard-wiring round.

streamlit.testing.v1.AppTest is used for the two view-layer tests (render_overview, render_transits) that render real
Streamlit widgets, matching tests/test_dashboard_privacy_containment.py's existing convention. Everything else is a
direct, Streamlit-free unit test of the service functions.
"""

from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd
from streamlit.testing.v1 import AppTest

import data_loader as dl
from services import filter_context_service, live_context_service, mixed_era_service
from services.pipeline_context_service import build_v2_aware_pipeline_context

APP_TZ = ZoneInfo("America/Chicago")
ORG, BRANCH = 10, 1


def _acs_item_summary(holds_total=0, ill_total=0, programming_total=0, collection_services_total=0,
                       ill_main=0, ill_by_branch=None):
    empty = pd.DataFrame()
    return {
        "holds_total": holds_total, "ill_total": ill_total, "programming_total": programming_total,
        "collection_services_total": collection_services_total, "ill_main": ill_main,
        "ill_by_branch": ill_by_branch or {}, "items_df": empty, "holds_df": empty, "ill_df": empty,
        "programming_df": empty, "collection_services_df": empty,
    }


# =====================================================================================================================
# item 2: mixed-era Live Today -- build_live_context consumes a pre-computed acs_item_summary, never computes it itself
# =====================================================================================================================

def test_build_live_context_uses_the_precomputed_acs_item_summary_directly():
    checkins = pd.DataFrame({"datetime": pd.to_datetime(["2026-10-05 09:00"]), "destination": ["Main"]})
    rejects = pd.DataFrame({"datetime": pd.to_datetime([]), "error_message": []})
    summary = _acs_item_summary(holds_total=4, ill_total=1, ill_by_branch={"Westside": 1})
    palette = {k: "#000000" for k in ("danger_bg", "danger_border", "danger_text", "danger_title",
                                      "info_bg", "info_border", "info_text", "info_title")}

    context = live_context_service.build_live_context(
        df_live_raw=checkins, df_history_raw=checkins, rejects_live_raw=rejects, rejects_history_raw=rejects,
        acs_item_summary=summary, pipeline_status={}, refresh_count=0, today=date(2026, 10, 5),
        now_ct=pd.Timestamp("2026-10-05T12:00:00", tz=APP_TZ), transit_labels=["Westside"], transit_home_label="Main",
        theme_palette=palette,
    )

    live_today_args = context["live_today_args"]
    assert live_today_args["today_holds"] == 4
    assert live_today_args["today_ill"] == 1
    assert live_today_args["today_ill_by_branch"] == {"Westside": 1}


def test_build_live_context_never_imports_or_calls_build_acs_item_summary_itself():
    # Government-readiness audit: the whole point of threading a pre-computed summary through is that this module
    # never needs to know whether the branch is v1-only or mixed-era. Prove it structurally: the function name never
    # appears as a call target in this module's source.
    import ast
    from pathlib import Path

    source = Path(live_context_service.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {getattr(node.func, "id", None) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "build_acs_item_summary" not in calls


# =====================================================================================================================
# item 3: mixed-era Overview -- render_overview consumes acs_item_summary, shows combined-era totals
# =====================================================================================================================

def _overview_wiring_script():
    from datetime import date

    import pandas as pd
    import streamlit as st

    import views.overview_view as overview

    kpis = []
    original = overview.render_kpi_card

    def spy(*args, **kwargs):
        kpis.append((args[0], args[1]))
        return original(*args, **kwargs)

    overview.render_kpi_card = spy
    try:
        overview.render_overview(
            pd.DataFrame({"datetime": pd.to_datetime([])}),
            pd.DataFrame({"datetime": pd.to_datetime([]), "error_simple": []}),
            st.session_state["acs_item_summary"], date(2026, 1, 1), date(2026, 1, 31), "Jan 2026",
            ["Westside", "Library Express"], "Main", "Attention", "text", "#6b7280", {}, {},
            can_view_internal_workflow=True,
        )
    finally:
        overview.render_kpi_card = original
    st.session_state["kpis"] = kpis


def test_overview_shows_the_combined_mixed_era_totals_it_was_given():
    # A v1+v2 combined summary (2 v1 holds + 1 v2 hold = 3) -- render_overview must show exactly what it was handed,
    # never recompute anything itself (it has no raw ACS dataframe available to recompute from any more).
    at = AppTest.from_function(_overview_wiring_script, default_timeout=60)
    at.session_state["acs_item_summary"] = _acs_item_summary(
        holds_total=3, ill_total=2, programming_total=1, collection_services_total=0,
        ill_by_branch={"Westside": 1, "Library Express": 1},
    )
    at.run()
    assert not at.exception, [e.value for e in at.exception]

    kpis = dict(at.session_state["kpis"])
    assert kpis["Holds"] == "3"
    assert kpis["ILL"] == "2"
    assert kpis["Branch Services"] == "1"


# =====================================================================================================================
# item 4: mixed-era transits/routing -- the privacy-safe representation (no item_key, degraded title/barcode for v2)
# =====================================================================================================================

def _mixed_checkins_for_transits():
    """A tiny mixed-era-shaped checkins dataframe: one v1 "No Agency Destination" row (real title/barcode) and one
    v2 "No Agency Destination" row (no title at all; barcode is None; only item_group_key carries an identity)."""
    return pd.DataFrame({
        "datetime": pd.to_datetime(["2026-10-01 09:00", "2026-10-05 09:00"]),
        "destination": ["No Agency Destination", "NO AGENCY DESTINATION"],
        "title": ["Real Book Title", None],
        "barcode": ["REAL-BARCODE-1", None],
        "item_group_key": ["REAL-BARCODE-1", "f" * 64],
        "source_era": ["v1", "v2"],
    })


def _transits_wiring_script():
    from datetime import date

    import pandas as pd
    import streamlit as st

    from views import transits_view

    mixed_df = st.session_state["mixed_df"]
    empty = pd.DataFrame(columns=["datetime"])
    transits_view.render_transits(
        df=mixed_df, rejects_df=empty, today_df=empty, today_rejects_df=empty,
        df_history_raw=mixed_df, today=date(2026, 10, 6), start_date=date(2026, 9, 1), end_date=date(2026, 10, 6),
    )


def test_transits_no_agency_export_shows_real_v1_fields_and_discloses_the_v2_gap():
    at = AppTest.from_function(_transits_wiring_script, default_timeout=60)
    at.session_state["mixed_df"] = _mixed_checkins_for_transits()
    at.run()
    assert not at.exception, [e.value for e in at.exception]

    # Find the "Problem Items Deep Dive" no-agency-destination table among the rendered dataframes.
    no_agency_frames = [d.value for d in at.dataframe if "Title" in d.value.columns and "Barcode" in d.value.columns]
    assert len(no_agency_frames) == 1
    frame = no_agency_frames[0]
    assert len(frame) == 2

    v1_row = frame[frame["Barcode"] == "REAL-BARCODE-1"].iloc[0]
    assert v1_row["Title"] == "Real Book Title"

    v2_row = frame[frame["Barcode"] == "Not available (Contract v2)"].iloc[0]
    assert v2_row["Title"] == "Not available (Contract v2)"

    # item_key/item_group_key must never reach the rendered page, in this table or anywhere else on it.
    rendered_text = "\n".join(
        d.value.to_csv() for d in at.dataframe
    )
    assert "f" * 64 not in rendered_text


# =====================================================================================================================
# item 5: mixed-era Reports -- filter_context_service (which Reports/Overview share) tolerates the mixed-era shape
# =====================================================================================================================

def test_filtered_context_correctly_buckets_v2_normalized_destinations():
    # After mixed_era_service's rewrite, a v2 row's destination is already v1-compatible raw text ("LIBRARY EXPRESS"),
    # so filter_context_service's existing exact-text transit matching must keep working unchanged for it.
    df_history_raw = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-09-15 09:00", "2026-10-05 09:00", "2026-10-06 09:00"]),
        "destination": ["Westside", "LIBRARY EXPRESS", "Main"],
        "source_era": ["v1", "v2", "v2"],
        "item_group_key": ["b1", "x" * 64, "y" * 64],
    })
    rejects_history_raw = pd.DataFrame({"datetime": pd.to_datetime([]), "error_message": []})

    result = filter_context_service.build_filtered_context(
        df_history_raw=df_history_raw, rejects_history_raw=rejects_history_raw,
        start_date=date(2026, 9, 1), end_date=date(2026, 10, 31),
        transit_labels=["Westside", "Library Express"], transit_home_label="Main",
    )

    assert result["overview_transit_counts_map"] == {"Westside": 1, "Library Express": 1}


# =====================================================================================================================
# item 6: v2-aware pipeline health -- re-confirmed at the dashboard_context.py wiring boundary (see
# tests/test_v2_coexistence_status.py for the exhaustive build_v2_aware_pipeline_context coverage)
# =====================================================================================================================

def test_dashboard_context_calls_the_v2_aware_pipeline_context_not_the_legacy_one():
    import ast
    from pathlib import Path

    import dashboard_context
    source = Path(dashboard_context.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {getattr(node.func, "id", None) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "build_v2_aware_pipeline_context" in calls
    assert "build_pipeline_context" not in calls


def test_v1_only_branch_pipeline_status_is_untouched_through_the_real_wiring():
    # No v2 heartbeat at all (v2_ingest_status=None) -- must be indistinguishable from pre-v2 behavior.
    pipeline_status = {"status": "completed", "updated_at": "2026-10-05T12:00:00Z"}
    ctx = build_v2_aware_pipeline_context(
        pipeline_status, None, pd.DataFrame(columns=["datetime"]),
        pd.Timestamp("2026-10-05T12:05:00", tz=APP_TZ), APP_TZ, "light",
    )
    assert ctx["pipeline_status_label"] == "Pipeline Healthy"
    assert "pipeline_source" not in ctx  # only set on the v2-aware branch, per its own docstring


# =====================================================================================================================
# item 11: manual "Refresh now" still works -- refresh_count is forwarded to the underlying v1 AND v2 live loaders
# =====================================================================================================================

def test_mixed_checkins_live_forwards_refresh_count_to_both_eras_loaders(monkeypatch):
    seen = {}

    def fake_v1(*_a, **k):
        seen["v1"] = k.get("refresh_count")
        return pd.DataFrame()

    def fake_v2(*_a, **k):
        seen["v2"] = k.get("refresh_count")
        return pd.DataFrame()

    monkeypatch.setattr(mixed_era_service, "get_effective_cutover", lambda *_a: pd.Timestamp("2026-10-01", tz="UTC"))
    monkeypatch.setattr(dl, "load_checkins_df", fake_v1)
    monkeypatch.setattr(dl, "load_checkin_events_df", fake_v2)

    mixed_era_service.build_mixed_checkins_live_df(ORG, BRANCH, refresh_count=42)

    assert seen == {"v1": 42, "v2": 42}


def test_mixed_acs_summary_live_forwards_refresh_count_to_both_eras_loaders(monkeypatch):
    seen = {}

    def fake_v1(*_a, **k):
        seen["v1"] = k.get("refresh_count")
        return pd.DataFrame()

    def fake_v2(*_a, **k):
        seen["v2"] = k.get("refresh_count")
        return pd.DataFrame()

    monkeypatch.setattr(mixed_era_service, "get_effective_cutover", lambda *_a: pd.Timestamp("2026-10-01", tz="UTC"))
    monkeypatch.setattr(dl, "load_acs_df", fake_v1)
    monkeypatch.setattr(dl, "load_acs_item_events_df", fake_v2)

    mixed_era_service.build_mixed_acs_item_summary_live(ORG, BRANCH, [], set(), set(), [], [], refresh_count=99)

    assert seen == {"v1": 99, "v2": 99}


def test_a_new_manual_refresh_count_still_busts_the_v2_live_loaders_cache():
    # data_loader.load_checkin_events_df/load_reject_events_df/load_acs_item_events_df must each accept
    # refresh_count as part of their st.cache_data key, mirroring load_checkins_df/load_rejects_df/load_acs_df
    # exactly -- otherwise a manual "Refresh now" click would keep serving stale v2 data for up to 900s.
    import inspect
    for loader in (dl.load_checkin_events_df, dl.load_reject_events_df, dl.load_acs_item_events_df):
        params = inspect.signature(loader.__wrapped__).parameters
        assert "refresh_count" in params, loader.__name__


# =====================================================================================================================
# item 13: tenant/branch isolation for the new live wiring
# =====================================================================================================================

def test_mixed_checkins_live_scopes_both_eras_loaders_to_the_requested_tenant(monkeypatch):
    seen = []
    monkeypatch.setattr(mixed_era_service, "get_effective_cutover", lambda *_a: pd.Timestamp("2026-10-01", tz="UTC"))
    monkeypatch.setattr(dl, "load_checkins_df", lambda org, branch, **_k: seen.append(("v1", org, branch)) or pd.DataFrame())
    monkeypatch.setattr(dl, "load_checkin_events_df", lambda org, branch, **_k: seen.append(("v2", org, branch)) or pd.DataFrame())

    mixed_era_service.build_mixed_checkins_live_df(555, 7, refresh_count=1)

    assert seen == [("v1", 555, 7), ("v2", 555, 7)]


def test_effective_cutover_lookup_never_crosses_tenants(monkeypatch):
    calls = []
    monkeypatch.setattr(dl, "load_v2_cutover", lambda org, branch: calls.append((org, branch)) or None)

    mixed_era_service.get_effective_cutover(111, 2)
    mixed_era_service.get_effective_cutover(222, 3)

    assert calls == [(111, 2), (222, 3)]
