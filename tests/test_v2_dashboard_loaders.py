"""Tests for the Contract v2 dashboard loaders added to src/data_loader.py (government-readiness audit, Part 1/7).

_read_table is monkeypatched with a call-recording stand-in, matching tests/test_data_loader_refresh.py's convention, so
these run without a real database connection. The Postgres-backed migration/RLS/service-layer behavior (v2_cutovers,
record_v2_cutover, get_effective_v2_cutover) is covered separately in tests/test_v2_cutover_postgres.py.
"""

import pandas as pd
import pytest

import data_loader as dl

ORG = 10
BRANCH = 1


@pytest.fixture(autouse=True)
def _clear_v2_loader_caches():
    loaders = [
        dl.load_checkin_events_df, dl.load_checkin_events_history_df,
        dl.load_reject_events_df, dl.load_reject_events_history_df,
        dl.load_acs_item_events_df, dl.load_acs_item_events_history_df,
        dl.load_v2_cutover, dl.load_v2_ingest_status,
    ]
    for loader in loaders:
        loader.clear()
    yield
    for loader in loaders:
        loader.clear()


@pytest.fixture
def recording_read_table(monkeypatch):
    calls = []

    def fake_read_table(query, params=None, *, customer_id=None, branch_id=None):
        calls.append({"query": query, "params": params, "customer_id": customer_id, "branch_id": branch_id})
        return pd.DataFrame()

    monkeypatch.setattr(dl, "_read_table", fake_read_table)
    return calls


# --- privacy: the v2 column lists can never include a prohibited legacy column --------------------------------------

@pytest.mark.parametrize("columns", [
    dl.CHECKIN_EVENTS_LOAD_COLUMNS,
    dl.REJECT_EVENTS_LOAD_COLUMNS,
    dl.ACS_ITEM_EVENTS_LOAD_COLUMNS,
])
def test_v2_load_columns_never_include_a_prohibited_legacy_column(columns):
    prohibited = {"barcode", "title", "patron_id", "raw_message", "source_file", "message_code", "message"}
    assert prohibited.isdisjoint(columns)


# --- item 1/2/3: checkins/rejects/acs events load and scope correctly -----------------------------------------------

def test_checkin_events_history_loader_scopes_by_customer_and_branch(recording_read_table):
    dl.load_checkin_events_history_df(ORG, BRANCH)
    assert len(recording_read_table) == 1
    call = recording_read_table[0]
    assert call["customer_id"] == ORG
    assert call["branch_id"] == BRANCH
    assert call["params"] == {"org_slug": ORG, "branch_slug": BRANCH}
    assert "checkin_events" in call["query"]
    assert "customer_id" in call["query"]
    assert "branch_id" in call["query"]
    for column in dl.CHECKIN_EVENTS_LOAD_COLUMNS:
        assert column in call["query"]


def test_checkin_events_live_loader_filters_to_today(recording_read_table):
    dl.load_checkin_events_df(ORG, BRANCH)
    call = recording_read_table[0]
    assert "event_time::date" in call["query"]


def test_reject_events_history_loader_scopes_by_customer_and_branch(recording_read_table):
    dl.load_reject_events_history_df(ORG, BRANCH)
    call = recording_read_table[0]
    assert "reject_events" in call["query"]
    for column in dl.REJECT_EVENTS_LOAD_COLUMNS:
        assert column in call["query"]


def test_acs_item_events_history_loader_scopes_by_customer_and_branch(recording_read_table):
    dl.load_acs_item_events_history_df(ORG, BRANCH)
    call = recording_read_table[0]
    assert "acs_item_events" in call["query"]
    for column in dl.ACS_ITEM_EVENTS_LOAD_COLUMNS:
        assert column in call["query"]


# --- item 4: tenant/branch scope is still required, exactly like the v1 loaders -------------------------------------

@pytest.mark.parametrize("loader", [
    dl.load_checkin_events_history_df, dl.load_checkin_events_df,
    dl.load_reject_events_history_df, dl.load_reject_events_df,
    dl.load_acs_item_events_history_df, dl.load_acs_item_events_df,
])
def test_v2_loaders_refuse_a_missing_org_or_branch(loader, recording_read_table, monkeypatch):
    monkeypatch.setattr(dl.st, "error", lambda *_args, **_kwargs: None)
    result = loader(None, BRANCH)
    assert isinstance(result, pd.DataFrame) and result.empty
    assert len(recording_read_table) == 0  # refused before ever touching the database


def test_load_v2_cutover_scopes_by_customer_and_branch(recording_read_table):
    dl.load_v2_cutover(ORG, BRANCH)
    call = recording_read_table[0]
    assert "v2_cutovers" in call["query"]
    assert call["params"] == {"org_slug": ORG, "branch_slug": BRANCH}


def test_load_v2_cutover_returns_none_when_no_row_exists(recording_read_table):
    assert dl.load_v2_cutover(ORG, BRANCH) is None


def test_load_v2_cutover_returns_the_stored_instant(monkeypatch):
    cutover = pd.Timestamp("2026-10-01T00:00:00Z")
    monkeypatch.setattr(dl, "_read_table", lambda *a, **k: pd.DataFrame({"cutover_at": [cutover]}))
    assert dl.load_v2_cutover(ORG, BRANCH) == cutover


def test_load_v2_cutover_treats_a_null_cutover_row_as_v1_only(monkeypatch):
    monkeypatch.setattr(dl, "_read_table", lambda *a, **k: pd.DataFrame({"cutover_at": [None]}))
    assert dl.load_v2_cutover(ORG, BRANCH) is None


def test_load_v2_ingest_status_scopes_by_customer_and_branch(recording_read_table):
    dl.load_v2_ingest_status(ORG, BRANCH)
    call = recording_read_table[0]
    assert "ingest_key_ids" in call["query"]
    assert "status = 'active'" in call["query"]


def test_load_v2_ingest_status_returns_none_when_no_active_key_has_ever_reported(recording_read_table):
    assert dl.load_v2_ingest_status(ORG, BRANCH) is None


# --- normalization -----------------------------------------------------------------------------------------------

def test_normalize_checkin_events_df_adds_a_none_barcode_column_never_reusing_item_key():
    df = pd.DataFrame({
        "event_time": ["2026-10-01T00:00:00Z"],
        "item_key": ["a" * 64],
        "destination": ["westside"],
        "bin": ["3"],
    })
    result = dl._normalize_checkin_events_df(df)
    assert result["barcode"].iloc[0] is None
    assert result["item_key"].iloc[0] == "a" * 64  # untouched, still present for internal use
    assert pd.notna(result["datetime"].iloc[0])


def test_normalize_reject_events_df_uses_error_class_as_error_message_fallback():
    df = pd.DataFrame({
        "event_time": ["2026-10-01T00:00:00Z"],
        "item_key": [None],
        "error_class": ["item_not_found"],
    })
    result = dl._normalize_reject_events_df(df)
    assert result["error_message"].iloc[0] == "item_not_found"


def test_normalize_acs_item_events_df_derives_is_hold_from_state():
    df = pd.DataFrame({
        "event_time": ["2026-10-01T00:00:00Z", "2026-10-01T00:01:00Z"],
        "item_key": ["a" * 64, "b" * 64],
        "state": ["hold", "non_hold_101"],
        "destination": ["main", "main"],
        "is_ill": [False, None],
        "is_branch_services": [False, None],
        "is_collection_services": [False, None],
    })
    result = dl._normalize_acs_item_events_df(df)
    assert result["is_hold"].tolist() == [True, False]
