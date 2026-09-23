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

    def fake_read_table(query, params=None, *, customer_id=None, branch_id=None):
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


# --- behavioral: Collector-cadence live-data reload/no-reload matrix -------
#
# app.py no longer passes a raw ever-incrementing tick as refresh_count to
# the three live loaders -- it passes
# dashboard_refresh_service.resolve_live_data_cache_key(last_run,
# manual_refresh_count), and mtime is always None (never derived from
# pipeline_status["updated_at"], which continuous-agent heartbeat writes
# also bump). These tests drive the REAL cache key builder against the
# REAL loaders to prove the exact reload/no-reload matrix the refresh
# redesign requires.

from services.dashboard_refresh_service import resolve_live_data_cache_key

LIVE_LOADERS = [dl.load_checkins_df, dl.load_rejects_df, dl.load_acs_df]


@pytest.mark.parametrize("loader", LIVE_LOADERS)
def test_unchanged_last_run_does_not_force_live_reload(loader, counting_read_table):
    # Two automatic polls that both observe the same last_run (no new
    # successful Collector run) must be a single real DB read, not two.
    key = resolve_live_data_cache_key(last_run="2026-09-22T10:00:00", manual_refresh_count=0)
    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key)
    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key)

    assert len(counting_read_table) == 1


@pytest.mark.parametrize("loader", LIVE_LOADERS)
def test_changed_last_run_forces_live_reload(loader, counting_read_table):
    # A new successful Collector run (last_run advanced) must force a real
    # reload of checkins/rejects/ACS.
    key_1 = resolve_live_data_cache_key(last_run="2026-09-22T10:00:00", manual_refresh_count=0)
    key_2 = resolve_live_data_cache_key(last_run="2026-09-22T10:15:00", manual_refresh_count=0)
    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_1)
    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_2)

    assert len(counting_read_table) == 2


@pytest.mark.parametrize("loader", LIVE_LOADERS)
def test_heartbeat_only_updated_at_change_does_not_force_live_reload(loader, counting_read_table):
    # Simulates two pipeline_status reads where only updated_at changed
    # (a continuous-agent heartbeat write) -- last_run is identical both
    # times, so only last_run (never updated_at) feeds the cache key.
    status_poll_1 = {"last_run": "2026-09-22T10:00:00", "updated_at": "2026-09-22T10:00:05"}
    status_poll_2 = {"last_run": "2026-09-22T10:00:00", "updated_at": "2026-09-22T10:01:03"}

    key_1 = resolve_live_data_cache_key(last_run=status_poll_1["last_run"], manual_refresh_count=0)
    key_2 = resolve_live_data_cache_key(last_run=status_poll_2["last_run"], manual_refresh_count=0)
    assert key_1 == key_2

    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_1)
    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_2)

    assert len(counting_read_table) == 1


@pytest.mark.parametrize("loader", LIVE_LOADERS)
def test_changed_last_attempt_alone_does_not_force_live_reload(loader, counting_read_table):
    # A failed scheduled-Collector attempt advances last_attempt but never
    # last_run -- must not force a live reload either.
    status_poll_1 = {"last_run": "2026-09-22T10:00:00", "last_attempt": "2026-09-22T10:00:00"}
    status_poll_2 = {"last_run": "2026-09-22T10:00:00", "last_attempt": "2026-09-22T10:15:00"}

    key_1 = resolve_live_data_cache_key(last_run=status_poll_1["last_run"], manual_refresh_count=0)
    key_2 = resolve_live_data_cache_key(last_run=status_poll_2["last_run"], manual_refresh_count=0)
    assert key_1 == key_2

    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_1)
    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_2)

    assert len(counting_read_table) == 1


@pytest.mark.parametrize("loader", LIVE_LOADERS)
def test_manual_refresh_forces_live_reload_even_with_unchanged_last_run(loader, counting_read_table):
    # "Refresh now" must force a real reload even when the Collector has
    # not produced a new successful run since the last poll.
    key_before = resolve_live_data_cache_key(last_run="2026-09-22T10:00:00", manual_refresh_count=0)
    key_after_manual_refresh = resolve_live_data_cache_key(last_run="2026-09-22T10:00:00", manual_refresh_count=1)

    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_before)
    loader(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_after_manual_refresh)

    assert len(counting_read_table) == 2


def test_missing_pipeline_status_produces_a_stable_key_and_still_loads(counting_read_table):
    # No pipeline_status row yet (brand-new tenant) or a failed status
    # read: last_run is None both times -- must not crash, and repeated
    # polls in that state must still hit cache (not hammer the DB forever
    # while waiting for the very first successful Collector run).
    key_1 = resolve_live_data_cache_key(last_run=None, manual_refresh_count=0)
    key_2 = resolve_live_data_cache_key(last_run=None, manual_refresh_count=0)

    df_1 = dl.load_checkins_df(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_1)
    df_2 = dl.load_checkins_df(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key_2)

    assert len(counting_read_table) == 1
    assert isinstance(df_1, pd.DataFrame)
    assert isinstance(df_2, pd.DataFrame)


def test_first_render_key_loads_live_data_correctly(counting_read_table):
    # First-ever render for a tenant that already has a completed run:
    # the very first cache key call must be a genuine read (empty cache),
    # not silently skipped/served stale.
    key = resolve_live_data_cache_key(last_run="2026-09-22T10:00:00", manual_refresh_count=0)
    dl.load_checkins_df(org_slug=ORG, branch_slug=BRANCH, mtime=None, refresh_count=key)

    assert len(counting_read_table) == 1


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


def test_no_global_cache_data_clear_call_sites_in_live_today_view():
    # The pre-existing "Refresh Live Data" button used to call
    # st.cache_data.clear() directly (a global, cross-tenant cache wipe).
    # The Collector-cadence refresh redesign replaced it with a
    # tenant-scoped manual-refresh counter (app.py's on_refresh_now
    # callback + dashboard_refresh_service.resolve_live_data_cache_key),
    # so no call site here should ever clear the whole cache again.
    from views import live_today_view

    source = inspect.getsource(live_today_view)
    assert "st.cache_data.clear()" not in source
