"""Tests for the Phase 4 cache-key decoupling in src/data_loader.py:

- live/status loaders keep refresh_count (and mtime) as part of their
  st.cache_data cache key
- the three historical loaders no longer accept refresh_count or mtime at
  all, and rely solely on their own ttl=900

_read_table is monkeypatched with a call-counting stand-in so these run
without a real database connection or a running Streamlit app --
st.cache_data itself works standalone (falls back to an in-memory cache
manager outside a real session), which is what actually exercises the
caching behavior here rather than just the loader bodies.
"""

import inspect

import pandas as pd
import pytest

import data_loader as dl

ORG = "test-org"
BRANCH = "test-branch"


@pytest.fixture(autouse=True)
def _clear_loader_caches():
    # st.cache_data's cache is process-global; clear every loader touched
    # by this file before and after each test so tests can't leak state
    # into each other or into other test modules.
    loaders = [
        dl.load_checkins_df, dl.load_rejects_df, dl.load_acs_df,
        dl.load_checkins_history_df, dl.load_rejects_history_df, dl.load_acs_history_df,
        dl.load_pipeline_status,
    ]
    for loader in loaders:
        loader.clear()
    yield
    for loader in loaders:
        loader.clear()


@pytest.fixture
def counting_read_table(monkeypatch):
    calls = []

    def fake_read_table(query, params=None):
        calls.append((query, params))
        return pd.DataFrame()

    monkeypatch.setattr(dl, "_read_table", fake_read_table)
    return calls


# --- signatures: exact call contract ----------------------------------------


def test_live_checkins_loader_keeps_mtime_and_refresh_count():
    params = inspect.signature(dl.load_checkins_df).parameters
    assert "mtime" in params
    assert "refresh_count" in params


def test_live_rejects_loader_keeps_mtime_and_refresh_count():
    params = inspect.signature(dl.load_rejects_df).parameters
    assert "mtime" in params
    assert "refresh_count" in params


def test_live_acs_loader_keeps_mtime_and_refresh_count():
    params = inspect.signature(dl.load_acs_df).parameters
    assert "mtime" in params
    assert "refresh_count" in params


def test_pipeline_status_loader_keeps_mtime_and_refresh_count():
    params = inspect.signature(dl.load_pipeline_status).parameters
    assert "mtime" in params
    assert "refresh_count" in params


def test_checkins_history_loader_has_no_mtime_or_refresh_count():
    params = inspect.signature(dl.load_checkins_history_df).parameters
    assert "mtime" not in params
    assert "refresh_count" not in params
    assert set(params) == {"org_slug", "branch_slug"}


def test_rejects_history_loader_has_no_mtime_or_refresh_count():
    params = inspect.signature(dl.load_rejects_history_df).parameters
    assert "mtime" not in params
    assert "refresh_count" not in params
    assert set(params) == {"org_slug", "branch_slug"}


def test_acs_history_loader_has_no_mtime_or_refresh_count():
    params = inspect.signature(dl.load_acs_history_df).parameters
    assert "mtime" not in params
    assert "refresh_count" not in params
    assert set(params) == {"org_slug", "branch_slug"}


# --- behavioral: live loaders actually change cache key with refresh_count -


def test_live_checkins_loader_misses_cache_on_new_refresh_count(counting_read_table):
    dl.load_checkins_df(org_slug=ORG, branch_slug=BRANCH, mtime="m1", refresh_count=0)
    dl.load_checkins_df(org_slug=ORG, branch_slug=BRANCH, mtime="m1", refresh_count=1)

    assert len(counting_read_table) == 2


def test_live_checkins_loader_hits_cache_on_same_args(counting_read_table):
    dl.load_checkins_df(org_slug=ORG, branch_slug=BRANCH, mtime="m1", refresh_count=0)
    dl.load_checkins_df(org_slug=ORG, branch_slug=BRANCH, mtime="m1", refresh_count=0)

    assert len(counting_read_table) == 1


def test_pipeline_status_loader_misses_cache_on_new_refresh_count(counting_read_table):
    dl.load_pipeline_status(org_slug=ORG, branch_slug=BRANCH, refresh_count=0)
    dl.load_pipeline_status(org_slug=ORG, branch_slug=BRANCH, refresh_count=1)

    assert len(counting_read_table) == 2


# --- behavioral: historical loaders are NOT tied to the live cadence -------


def test_checkins_history_loader_ignores_repeated_calls_within_ttl(counting_read_table):
    # No refresh_count parameter exists at all -- simulating "many refresh
    # ticks" is simply calling the loader repeatedly with its only real
    # arguments unchanged, which is exactly what happens across ticks in
    # app.py now that refresh_count has been removed from its call site.
    for _ in range(5):
        dl.load_checkins_history_df(org_slug=ORG, branch_slug=BRANCH)

    assert len(counting_read_table) == 1


def test_rejects_history_loader_ignores_repeated_calls_within_ttl(counting_read_table):
    for _ in range(5):
        dl.load_rejects_history_df(org_slug=ORG, branch_slug=BRANCH)

    assert len(counting_read_table) == 1


def test_acs_history_loader_ignores_repeated_calls_within_ttl(counting_read_table):
    for _ in range(5):
        dl.load_acs_history_df(org_slug=ORG, branch_slug=BRANCH)

    assert len(counting_read_table) == 1


def test_history_loaders_still_scoped_per_tenant(counting_read_table):
    # Different org/branch must still be a genuine cache miss -- decoupling
    # from refresh_count/mtime must not accidentally collapse tenant
    # scoping in the cache key.
    dl.load_checkins_history_df(org_slug=ORG, branch_slug=BRANCH)
    dl.load_checkins_history_df(org_slug="other-org", branch_slug=BRANCH)

    assert len(counting_read_table) == 2


# --- no new global cache clear ----------------------------------------------


def test_no_new_cache_data_clear_call_sites_in_data_loader():
    source = inspect.getsource(dl)
    assert "cache_data.clear()" not in source


def test_cache_data_clear_still_limited_to_one_pre_existing_call_site():
    from views import live_today_view

    source = inspect.getsource(live_today_view)
    assert source.count("st.cache_data.clear()") == 1
