import pytest
from db_fakes import FakeEngine, FakeQueryResult

from src.services import readiness_service


@pytest.fixture(autouse=True)
def _clear_readiness_cache():
    # get_branch_readiness is st.cache_data-wrapped (dashboard performance
    # pass); its cache is process-global, so clear it before/after every
    # test to keep these tests isolated from each other and from other
    # test modules, matching the existing convention in
    # tests/test_data_loader_refresh.py.
    readiness_service.get_branch_readiness.clear()
    yield
    readiness_service.get_branch_readiness.clear()


def _ready_row(**overrides):
    row = {
        "organization_id": 1,
        "organization_slug": "acme",
        "organization_name": "Acme",
        "customer_id": 10,
        "app_branch_id": 1,
        "branch_slug": "main",
        "branch_name": "Main",
        "branch_status": "active",
        "branch_id": 100,
        "onboarding_status": "ready",
        "onboarding_message": None,
        "onboarding_updated_at": None,
    }
    row.update(overrides)
    return row


def test_ready_branch_reports_is_ready(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=_ready_row())])
    monkeypatch.setattr(readiness_service, "get_engine", lambda: engine)

    result = readiness_service.get_branch_readiness(org_slug="acme", branch_slug="main")

    assert result["is_ready"] is True
    assert result["code"] == "ready"


def test_missing_org_reports_org_not_found(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(readiness_service, "get_engine", lambda: engine)

    result = readiness_service.get_branch_readiness(org_slug="ghost-org", branch_slug="main")

    assert result["is_ready"] is False
    assert result["code"] == "org_not_found"


def test_missing_branch_reports_branch_not_found(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=_ready_row(branch_slug=None, branch_status=None, branch_id=None))])
    monkeypatch.setattr(readiness_service, "get_engine", lambda: engine)

    result = readiness_service.get_branch_readiness(org_slug="acme", branch_slug="does-not-exist")

    assert result["is_ready"] is False
    assert result["code"] == "branch_not_found"


def test_inactive_branch_reports_branch_inactive(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=_ready_row(branch_status="inactive"))])
    monkeypatch.setattr(readiness_service, "get_engine", lambda: engine)

    result = readiness_service.get_branch_readiness(org_slug="acme", branch_slug="main")

    assert result["is_ready"] is False
    assert result["code"] == "branch_inactive"


def test_missing_operational_mapping_reported(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=_ready_row(customer_id=None))])
    monkeypatch.setattr(readiness_service, "get_engine", lambda: engine)

    result = readiness_service.get_branch_readiness(org_slug="acme", branch_slug="main")

    assert result["is_ready"] is False
    assert result["code"] == "missing_operational_mapping"


def test_non_ready_onboarding_status_reported(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=_ready_row(onboarding_status="pending"))])
    monkeypatch.setattr(readiness_service, "get_engine", lambda: engine)

    result = readiness_service.get_branch_readiness(org_slug="acme", branch_slug="main")

    assert result["is_ready"] is False
    assert result["code"] == "pending"


def test_every_readiness_answer_is_exactly_what_the_dashboard_shows(monkeypatch):
    ids = {"customer_id": 10, "branch_id": 100}
    cases = [
        # (row, result)
        (None, {"is_ready": False, "code": "org_not_found", "message": "This organization could not be found."}),
        (_ready_row(branch_slug=None, branch_status=None, branch_id=None),
         {"is_ready": False, "code": "branch_not_found", "message": "This branch could not be found for the selected organization."}),
        (_ready_row(branch_status="inactive"), {"is_ready": False, "code": "branch_inactive", "message": "This branch is not active."}),
        (_ready_row(branch_id=None),
         {"is_ready": False, "code": "missing_operational_mapping", "message": "Operational tenant mapping is missing for this branch."}),
        (_ready_row(), {"is_ready": True, "code": "ready", "message": "Ready", **ids}),
        (_ready_row(onboarding_message="Live since Monday"), {"is_ready": True, "code": "ready", "message": "Live since Monday", **ids}),
        (_ready_row(onboarding_status="pending"), {"is_ready": False, "code": "pending", "message": "This branch is still being onboarded.", **ids}),
        (_ready_row(onboarding_status=None), {"is_ready": False, "code": "pending", "message": "This branch is still being onboarded.", **ids}),
        (_ready_row(onboarding_status="paused"), {"is_ready": False, "code": "paused", "message": "This branch has been paused.", **ids}),
        (_ready_row(onboarding_status="paused", onboarding_message="Closed for renovation"),
         {"is_ready": False, "code": "paused", "message": "Closed for renovation", **ids}),
        (_ready_row(onboarding_status="something_new"),
         {"is_ready": False, "code": "something_new", "message": "This branch is not ready yet.", **ids}),
        (_ready_row(onboarding_status="READY"), {"is_ready": False, "code": "READY", "message": "This branch is not ready yet.", **ids}),
        (_ready_row(onboarding_status="branch_inactive"),
         {"is_ready": False, "code": "branch_inactive", "message": "This branch is not ready yet.", **ids}),
    ]
    for row, result in cases:
        readiness_service.get_branch_readiness.clear()
        monkeypatch.setattr(readiness_service, "get_engine", lambda row=row: FakeEngine([FakeQueryResult(first=row)]))

        assert readiness_service.get_branch_readiness(org_slug="acme", branch_slug="main") == result, row


# --- cache scoping (dashboard performance pass) ------------------------------


def test_cache_hits_on_repeated_call_same_org_and_branch(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=_ready_row())])
    monkeypatch.setattr(readiness_service, "get_engine", lambda: engine)

    for _ in range(5):
        readiness_service.get_branch_readiness(org_slug="acme", branch_slug="main")

    assert len(engine.calls) == 1


def test_cache_is_scoped_per_org_and_branch(monkeypatch):
    # A different (org_slug, branch_slug) pair must never share a cache
    # entry -- one branch's readiness can never leak into another
    # tenant's or another branch's result.
    engine = FakeEngine([
        FakeQueryResult(first=_ready_row(onboarding_status="ready")),
        FakeQueryResult(first=_ready_row(onboarding_status="pending", branch_slug="north", branch_id=200)),
    ])
    monkeypatch.setattr(readiness_service, "get_engine", lambda: engine)

    main_result = readiness_service.get_branch_readiness(org_slug="acme", branch_slug="main")
    north_result = readiness_service.get_branch_readiness(org_slug="acme", branch_slug="north")

    assert len(engine.calls) == 2
    assert main_result["is_ready"] is True
    assert north_result["is_ready"] is False
