"""The dashboard's st.cache_data layer over access_service's two lookups.

Moved here from tests/test_access_service.py when the caching itself moved
out of access_service (Block 2c): access_service is now the uncached,
framework-neutral core, and services.streamlit_access_adapter holds the
cached copies the Streamlit entry scripts import.

Imported the "flat" way (services.streamlit_access_adapter), matching how
Streamlit loads it with src/ as the script root. The adapter reaches the core
as services.access_service, so that -- not src.services.access_service, a
separate module object -- is the module whose get_engine these tests patch.
"""

import pytest
from db_fakes import FakeEngine, FakeQueryResult

from services import access_service, streamlit_access_adapter


@pytest.fixture(autouse=True)
def _clear_access_adapter_caches():
    # st.cache_data's cache is process-global, so clear it before/after every
    # test to keep these tests isolated from each other and from other test
    # modules, matching the existing convention in
    # tests/test_data_loader_refresh.py.
    streamlit_access_adapter.get_org_branches.clear()
    streamlit_access_adapter.get_user_memberships.clear()
    yield
    streamlit_access_adapter.get_org_branches.clear()
    streamlit_access_adapter.get_user_memberships.clear()


def test_cache_ttl_is_120_seconds():
    assert streamlit_access_adapter._CHROME_CACHE_TTL_SECONDS == 120


# --- results are the core's results, unchanged --------------------------------

def test_get_org_branches_returns_the_core_rows_for_the_requested_org(monkeypatch):
    rows = [
        {"id": 1, "branch_id": 100, "branch_slug": "main", "branch_name": "Main",
         "is_primary": True, "status": "active"},
        {"id": 2, "branch_id": 101, "branch_slug": "east", "branch_name": "East",
         "is_primary": False, "status": "active"},
    ]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert streamlit_access_adapter.get_org_branches("acme") == rows
    assert engine.calls[0]["params"] == {"org_slug": "acme"}


def test_get_user_memberships_returns_the_core_rows_for_the_requested_user(monkeypatch):
    rows = [
        {"organization_id": 1, "customer_id": 10, "role": "owner",
         "organization_slug": "acme", "organization_name": "Acme"},
    ]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    assert streamlit_access_adapter.get_user_memberships(1) == rows
    assert engine.calls[0]["params"] == {"user_id": 1}


# --- cache scoping (dashboard performance pass) ------------------------------

def test_get_org_branches_cache_hits_on_repeated_call_same_org(monkeypatch):
    engine = FakeEngine([FakeQueryResult(all_rows=[{"id": 1, "branch_id": 100, "branch_slug": "main",
                                                      "branch_name": "Main", "is_primary": True, "status": "active"}])])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    for _ in range(5):
        streamlit_access_adapter.get_org_branches(org_slug="acme")

    assert len(engine.calls) == 1


def test_get_org_branches_cache_is_scoped_per_org(monkeypatch):
    # A different org_slug must be a genuine cache miss -- caching must
    # never collapse two different tenants' branch lists together.
    engine = FakeEngine([
        FakeQueryResult(all_rows=[{"id": 1, "branch_id": 100, "branch_slug": "main",
                                    "branch_name": "Main", "is_primary": True, "status": "active"}]),
        FakeQueryResult(all_rows=[{"id": 2, "branch_id": 200, "branch_slug": "north",
                                    "branch_name": "North", "is_primary": True, "status": "active"}]),
    ])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    acme_branches = streamlit_access_adapter.get_org_branches(org_slug="acme")
    other_branches = streamlit_access_adapter.get_org_branches(org_slug="other-org")

    assert len(engine.calls) == 2
    assert acme_branches != other_branches


def test_get_user_memberships_cache_hits_on_repeated_call_same_user(monkeypatch):
    engine = FakeEngine([FakeQueryResult(all_rows=[{"organization_id": 1, "customer_id": 10, "role": "owner",
                                                     "organization_slug": "acme", "organization_name": "Acme"}])])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    for _ in range(5):
        streamlit_access_adapter.get_user_memberships(user_id=1)

    assert len(engine.calls) == 1


def test_get_user_memberships_cache_is_scoped_per_user(monkeypatch):
    # A different user_id must be a genuine cache miss -- caching must
    # never leak one user's memberships into another user's lookup.
    engine = FakeEngine([
        FakeQueryResult(all_rows=[{"organization_id": 1, "customer_id": 10, "role": "owner",
                                    "organization_slug": "acme", "organization_name": "Acme"}]),
        FakeQueryResult(all_rows=[]),
    ])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    user_1_memberships = streamlit_access_adapter.get_user_memberships(user_id=1)
    user_2_memberships = streamlit_access_adapter.get_user_memberships(user_id=2)

    assert len(engine.calls) == 2
    assert user_1_memberships != user_2_memberships


def test_clear_forces_the_next_call_back_to_the_core(monkeypatch):
    engine = FakeEngine([FakeQueryResult(all_rows=[]), FakeQueryResult(all_rows=[])])
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)

    streamlit_access_adapter.get_user_memberships(user_id=1)
    streamlit_access_adapter.get_user_memberships.clear()
    streamlit_access_adapter.get_user_memberships(user_id=1)

    assert len(engine.calls) == 2


# --- the security gates are not wrapped ---------------------------------------

def test_the_adapter_exposes_no_cached_copy_of_the_security_gates():
    assert not hasattr(streamlit_access_adapter, "user_can_access_org")
    assert not hasattr(streamlit_access_adapter, "get_org_access_mode")
