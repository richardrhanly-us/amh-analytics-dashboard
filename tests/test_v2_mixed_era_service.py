"""Tests for src/services/mixed_era_service.py (government-readiness audit, Parts 1-2 of the dashboard-wiring round).

data_loader's public loaders are monkeypatched to return synthetic dataframes, so the actual v1/v2 partition logic --
the part that matters for "no double-counting" -- is exercised deterministically without a database. The cutover
boundary itself (data_loader.load_v2_cutover) is also monkeypatched here; its own SQL-scoping is covered in
tests/test_v2_dashboard_loaders.py, and the underlying v2_cutovers table/service functions in
tests/test_v2_cutover_postgres.py.
"""

import pandas as pd

import data_loader as dl
import metrics
import metrics_v2
from services import mixed_era_service as mixed

ORG = 10
BRANCH = 1
CUTOVER = pd.Timestamp("2026-10-01T00:00:00+00:00")


def _checkins_df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["datetime", "barcode", "destination"])


def _checkin_events_df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["datetime", "item_key", "destination", "bin"])


def _rejects_df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["datetime", "barcode", "error_message"])


def _reject_events_df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["datetime", "item_key", "error_class"])


# =====================================================================================================================
# item 1: a v1-only branch (no cutover) is unchanged -- checkins, rejects, and ACS (history and live)
# =====================================================================================================================

def test_no_cutover_checkins_history_is_unchanged_and_never_touches_the_v2_loader(monkeypatch):
    v1_df = _checkins_df([
        {"datetime": pd.Timestamp("2026-09-01T08:00:00+00:00"), "barcode": "111", "destination": "Main"},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: None)
    monkeypatch.setattr(dl, "load_checkins_history_df", lambda *_a: v1_df)

    def boom(*_a, **_k):
        raise AssertionError("the v2 loader must not be called when the branch has no cutover")
    monkeypatch.setattr(dl, "load_checkin_events_history_df", boom)

    result = mixed.build_mixed_checkins_df(ORG, BRANCH)
    assert len(result) == 1
    assert (result["source_era"] == "v1").all()
    assert result["barcode"].tolist() == ["111"]
    assert result["destination"].tolist() == ["Main"]  # untouched -- no v2 text rewrite ever applied to a v1 row


def test_no_cutover_checkins_live_is_unchanged_and_never_touches_the_v2_loader(monkeypatch):
    v1_df = _checkins_df([
        {"datetime": pd.Timestamp("2026-09-01T08:00:00+00:00"), "barcode": "111", "destination": "Main"},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: None)
    monkeypatch.setattr(dl, "load_checkins_df", lambda *_a, **_k: v1_df)

    def boom(*_a, **_k):
        raise AssertionError("the v2 live loader must not be called when the branch has no cutover")
    monkeypatch.setattr(dl, "load_checkin_events_df", boom)

    result = mixed.build_mixed_checkins_live_df(ORG, BRANCH, refresh_count=7)
    assert len(result) == 1
    assert (result["source_era"] == "v1").all()


def test_no_cutover_rejects_history_is_unchanged(monkeypatch):
    v1_df = _rejects_df([{"datetime": pd.Timestamp("2026-09-01T08:00:00+00:00"), "barcode": "111", "error_message": "Item Not Found"}])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: None)
    monkeypatch.setattr(dl, "load_rejects_history_df", lambda *_a: v1_df)

    def boom(*_a, **_k):
        raise AssertionError("the v2 loader must not be called when the branch has no cutover")
    monkeypatch.setattr(dl, "load_reject_events_history_df", boom)

    result = mixed.build_mixed_rejects_df(ORG, BRANCH)
    assert len(result) == 1 and result.iloc[0]["source_era"] == "v1"


def test_no_cutover_delegates_history_acs_summary_entirely_to_v1(monkeypatch):
    called = {}

    def fake_v1_summary(acs_df, *_a, **_k):
        called["acs_df"] = acs_df
        return {"holds_total": 3}
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: None)
    monkeypatch.setattr(dl, "load_acs_history_df", lambda *_a: pd.DataFrame({
        "datetime": [pd.Timestamp("2026-01-15T00:00:00+00:00")],
    }))
    monkeypatch.setattr(metrics, "build_acs_item_summary", fake_v1_summary)

    def boom(*_a, **_k):
        raise AssertionError("v2 ACS metrics must not run when the branch has no cutover")
    monkeypatch.setattr(metrics_v2, "build_acs_item_summary_v2", boom)

    result = mixed.build_mixed_acs_item_summary(
        ORG, BRANCH, pd.Timestamp("2026-01-01").date(), pd.Timestamp("2026-01-31").date(),
        ["Westside"], set(), set(), [], [],
    )
    assert result == {"holds_total": 3}
    assert len(called["acs_df"]) == 1  # the one in-range row survived the date filter


def test_no_cutover_delegates_live_acs_summary_entirely_to_v1(monkeypatch):
    called = {}

    def fake_v1_summary(acs_df, *_a, **_k):
        called["acs_df"] = acs_df
        return {"holds_total": 2}
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: None)
    monkeypatch.setattr(dl, "load_acs_df", lambda *_a, **_k: pd.DataFrame({
        "datetime": [pd.Timestamp("2026-01-15T00:00:00+00:00")],
        "message_code": ["10"], "barcode": ["b1"], "raw_message": ["101YNY"],
    }))
    monkeypatch.setattr(metrics, "build_acs_item_summary", fake_v1_summary)

    def boom(*_a, **_k):
        raise AssertionError("v2 ACS metrics must not run when the branch has no cutover")
    monkeypatch.setattr(metrics_v2, "build_acs_item_summary_v2", boom)

    result = mixed.build_mixed_acs_item_summary_live(
        ORG, BRANCH, ["Westside"], set(), set(), [], [], refresh_count=5,
    )
    assert result == {"holds_total": 2}
    assert len(called["acs_df"]) == 1


# =====================================================================================================================
# item 7/8: exact cutover-boundary partition, no double-counting, no gaps (checkins)
# =====================================================================================================================

def test_cutover_partitions_checkins_with_no_overlap_and_no_gap(monkeypatch):
    v1_df = _checkins_df([
        {"datetime": pd.Timestamp("2026-09-30T23:59:59+00:00"), "barcode": "before-cutover", "destination": "Main"},
        {"datetime": CUTOVER, "barcode": "at-cutover-still-v1-table", "destination": "Main"},
    ])
    v2_df = _checkin_events_df([
        {"datetime": CUTOVER, "item_key": "at-cutover-v2", "destination": "main", "bin": "1"},
        {"datetime": pd.Timestamp("2026-09-30T23:00:00+00:00"), "item_key": "before-cutover-in-v2-table",
         "destination": "main", "bin": "1"},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: CUTOVER)
    monkeypatch.setattr(dl, "load_checkins_history_df", lambda *_a: v1_df)
    monkeypatch.setattr(dl, "load_checkin_events_history_df", lambda *_a: v2_df)

    result = mixed.build_mixed_checkins_df(ORG, BRANCH)

    # v1's row exactly AT the cutover instant is excluded (v1 is strictly BEFORE); v2's row exactly at the cutover
    # is included; a stray v2 row timestamped BEFORE the cutover (e.g. a dry-run artifact) is excluded even though
    # it physically exists in the v2 table -- the boundary is never inferred from what's actually present.
    assert len(result) == 2
    assert set(result["source_era"]) == {"v1", "v2"}
    v1_rows = result[result["source_era"] == "v1"]
    v2_rows = result[result["source_era"] == "v2"]
    assert v1_rows["barcode"].tolist() == ["before-cutover"]
    assert v2_rows["item_key"].tolist() == ["at-cutover-v2"]


def test_v2_only_current_data_works_when_v1_has_nothing_after_cutover(monkeypatch):
    v1_df = _checkins_df([])
    v2_df = _checkin_events_df([
        {"datetime": pd.Timestamp("2026-10-05T00:00:00+00:00"), "item_key": "1" * 64, "destination": "main", "bin": "1"},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: CUTOVER)
    monkeypatch.setattr(dl, "load_checkins_history_df", lambda *_a: v1_df)
    monkeypatch.setattr(dl, "load_checkin_events_history_df", lambda *_a: v2_df)

    result = mixed.build_mixed_checkins_df(ORG, BRANCH)
    assert len(result) == 1
    assert result.iloc[0]["source_era"] == "v2"


def test_cutover_partitions_rejects_with_no_overlap_and_no_gap(monkeypatch):
    v1_df = _rejects_df([
        {"datetime": pd.Timestamp("2026-09-15T00:00:00+00:00"), "barcode": "b1", "error_message": "Item Not Found"},
    ])
    v2_df = _reject_events_df([
        {"datetime": pd.Timestamp("2026-10-05T00:00:00+00:00"), "item_key": "a" * 64, "error_class": "item_not_found"},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: CUTOVER)
    monkeypatch.setattr(dl, "load_rejects_history_df", lambda *_a: v1_df)
    monkeypatch.setattr(dl, "load_reject_events_history_df", lambda *_a: v2_df)

    result = mixed.build_mixed_rejects_df(ORG, BRANCH)
    assert len(result) == 2
    assert set(result["source_era"]) == {"v1", "v2"}


# =====================================================================================================================
# item: the destination-text mismatch this round fixed (Library Express / No Agency Destination)
# =====================================================================================================================

def test_v2_checkin_destination_is_rewritten_into_v1_compatible_raw_text(monkeypatch):
    v1_df = _checkins_df([])
    v2_df = _checkin_events_df([
        {"datetime": pd.Timestamp("2026-10-05T00:00:00+00:00"), "item_key": "1" * 64,
         "destination": "library_express", "bin": "1"},
        {"datetime": pd.Timestamp("2026-10-05T00:01:00+00:00"), "item_key": "2" * 64,
         "destination": "no_agency_destination", "bin": "1"},
        {"datetime": pd.Timestamp("2026-10-05T00:02:00+00:00"), "item_key": "3" * 64,
         "destination": "westside", "bin": "1"},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: CUTOVER)
    monkeypatch.setattr(dl, "load_checkins_history_df", lambda *_a: v1_df)
    monkeypatch.setattr(dl, "load_checkin_events_history_df", lambda *_a: v2_df)

    result = mixed.build_mixed_checkins_df(ORG, BRANCH).sort_values("item_group_key")

    # These are exactly the raw-text substrings v1's own destination-matching functions (metrics.py,
    # live_context_service.py, filter_context_service.py, transits_view.py) already look for -- naively reusing
    # v2's underscore-separated slug as-is would have silently broken all of them for Library Express and
    # "No Agency Destination" specifically (an underscore never matches a space).
    destinations = result.set_index("item_group_key")["destination"].to_dict()
    assert destinations["1" * 64] == "LIBRARY EXPRESS"
    assert destinations["2" * 64] == "NO AGENCY DESTINATION"
    assert destinations["3" * 64] == "WESTSIDE"


def test_mixed_checkins_item_group_key_is_barcode_for_v1_and_item_key_for_v2(monkeypatch):
    v1_df = _checkins_df([
        {"datetime": pd.Timestamp("2026-09-01T00:00:00+00:00"), "barcode": "REAL-BARCODE-1", "destination": "Westside"},
    ])
    v2_df = _checkin_events_df([
        {"datetime": pd.Timestamp("2026-10-02T00:00:00+00:00"), "item_key": "f" * 64, "destination": "westside", "bin": "2"},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: CUTOVER)
    monkeypatch.setattr(dl, "load_checkins_history_df", lambda *_a: v1_df)
    monkeypatch.setattr(dl, "load_checkin_events_history_df", lambda *_a: v2_df)

    result = mixed.build_mixed_checkins_df(ORG, BRANCH)

    v1_row = result[result["source_era"] == "v1"].iloc[0]
    v2_row = result[result["source_era"] == "v2"].iloc[0]
    assert v1_row["item_group_key"] == "REAL-BARCODE-1"
    assert v2_row["item_group_key"] == "f" * 64


# =====================================================================================================================
# item 9: item_key never appears rendered anywhere -- only item_group_key (internal) and, for v2 rows, the
# HMAC value lives ONLY in that internal column, never copied into "barcode"
# =====================================================================================================================

def test_v2_checkin_rows_never_populate_a_real_barcode_value(monkeypatch):
    v1_df = _checkins_df([])
    v2_df = _checkin_events_df([
        {"datetime": pd.Timestamp("2026-10-05T00:00:00+00:00"), "item_key": "a" * 64, "destination": "main", "bin": "1"},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: CUTOVER)
    monkeypatch.setattr(dl, "load_checkins_history_df", lambda *_a: v1_df)
    monkeypatch.setattr(dl, "load_checkin_events_history_df", lambda *_a: v2_df)

    result = mixed.build_mixed_checkins_df(ORG, BRANCH)
    # v2 rows have no "barcode" column contribution at all (v2 tables physically lack one) -- only item_group_key
    # (internal-only, never rendered/exported -- see tests/test_v2_pilot_prep_safety.py and transits_view.py) ever
    # carries the item_key value.
    assert "barcode" not in result.columns or result.loc[result["source_era"] == "v2", "barcode"].isna().all()


# =====================================================================================================================
# item: mixed ACS summary -- totals summed, ill_by_branch summed per label, date-range filter applied to both eras
# =====================================================================================================================

def test_mixed_acs_summary_sums_totals_and_ill_by_branch_across_eras(monkeypatch):
    v1_acs = pd.DataFrame([
        {"datetime": pd.Timestamp("2026-09-15T00:00:00+00:00"), "raw_message": "101YNY0001AB111AJTitle",
         "message_code": "10", "barcode": "111", "destination": "Westside", "patron_id": ""},
    ])
    v2_acs = pd.DataFrame([
        {"datetime": pd.Timestamp("2026-10-05T00:00:00+00:00"), "item_key": "2" * 64, "state": "hold",
         "destination": "westside", "is_ill": False, "is_branch_services": False, "is_collection_services": False},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: CUTOVER)
    monkeypatch.setattr(dl, "load_acs_history_df", lambda *_a: v1_acs)
    monkeypatch.setattr(dl, "load_acs_item_events_history_df", lambda *_a: v2_acs)

    result = mixed.build_mixed_acs_item_summary(
        ORG, BRANCH, pd.Timestamp("2026-09-01").date(), pd.Timestamp("2026-10-31").date(), ["Westside"],
        branch_services_names=set(), collection_services_names=set(),
        branch_services_da_patterns=[], collection_services_da_patterns=[],
    )
    assert result["holds_total"] == 2
    assert len(result["holds_df"]) == 2
    assert set(result["holds_df"]["source_era"]) == {"v1", "v2"}
    # item_key is dropped internally by metrics_v2 before it ever reaches a returned frame (see test_metrics_v2_parity.py)
    assert "item_key" not in result["holds_df"].columns


def test_mixed_acs_summary_date_range_excludes_out_of_range_rows_in_both_eras(monkeypatch):
    v1_acs = pd.DataFrame([
        {"datetime": pd.Timestamp("2026-08-01T00:00:00+00:00"), "raw_message": "101YNY", "message_code": "10",
         "barcode": "out-of-range", "destination": "Main", "patron_id": ""},
    ])
    v2_acs = pd.DataFrame([
        {"datetime": pd.Timestamp("2026-12-01T00:00:00+00:00"), "item_key": "3" * 64, "state": "hold",
         "destination": "main", "is_ill": False, "is_branch_services": False, "is_collection_services": False},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: CUTOVER)
    monkeypatch.setattr(dl, "load_acs_history_df", lambda *_a: v1_acs)
    monkeypatch.setattr(dl, "load_acs_item_events_history_df", lambda *_a: v2_acs)

    result = mixed.build_mixed_acs_item_summary(
        ORG, BRANCH, pd.Timestamp("2026-09-01").date(), pd.Timestamp("2026-10-31").date(), [],
        branch_services_names=set(), collection_services_names=set(),
        branch_services_da_patterns=[], collection_services_da_patterns=[],
    )
    # both rows fall outside [Sep 1, Oct 31] -- neither era should contribute anything
    assert result["holds_total"] == 0


def test_mixed_acs_summary_live_uses_prepare_todays_acs_snapshot_for_the_v1_portion(monkeypatch):
    # Two records for the same barcode today: an earlier hold, then a later retraction (message_code "10" but not a
    # "101" prefix) -- prepare_todays_acs_snapshot's "keep latest per barcode among today's rows" must still apply
    # to the v1 portion of a mixed-era branch's live ACS summary, exactly as it always has for a v1-only branch.
    v1_acs_live = pd.DataFrame([
        {"datetime": pd.Timestamp("2026-10-05T09:00:00+00:00"), "message_code": "10", "barcode": "B1",
         "raw_message": "101YNY", "destination": "Main", "patron_id": ""},
        {"datetime": pd.Timestamp("2026-10-05T09:05:00+00:00"), "message_code": "10", "barcode": "B1",
         "raw_message": "101YNN", "destination": "Main", "patron_id": ""},
    ])
    monkeypatch.setattr(mixed, "get_effective_cutover", lambda *_a: CUTOVER)
    monkeypatch.setattr(dl, "load_acs_df", lambda *_a, **_k: v1_acs_live)
    monkeypatch.setattr(dl, "load_acs_item_events_df", lambda *_a, **_k: pd.DataFrame())

    result = mixed.build_mixed_acs_item_summary_live(
        ORG, BRANCH, [], set(), set(), [], [], refresh_count=3,
    )
    # The latest record for B1 is the retraction (101YNN, not a hold) -- so it must NOT be counted, exactly as
    # metrics.prepare_todays_acs_snapshot's dedup would produce for a v1-only branch.
    assert result["holds_total"] == 0
