"""services.branch_readiness: the dashboard's decision whether a branch is ready, from the row
services.readiness_service already reads. Pure: no database, no Streamlit, no FastAPI, no environment.
"""

from __future__ import annotations

import copy
import inspect

from src.services import branch_readiness as module
from src.services.branch_readiness import READY, BranchReadiness, branch_readiness

READY_ROW = {
    "branch_slug": "main",
    "branch_status": "active",
    "customer_id": 10,
    "branch_id": 100,
    "onboarding_status": "ready",
}


def _row(**overrides):
    return {**READY_ROW, **overrides}


def test_each_row_gives_the_answer_the_dashboard_has_always_given():
    setup, onboarding = False, True
    cases = [
        # (row, code, from onboarding_status, ready)
        (None, "org_not_found", setup, False),
        (_row(branch_slug=None, branch_status=None, branch_id=None, onboarding_status=None), "branch_not_found", setup, False),
        (_row(branch_slug=None), "branch_not_found", setup, False),
        (_row(branch_status="inactive"), "branch_inactive", setup, False),
        (_row(branch_status="Active"), "branch_inactive", setup, False),
        (_row(branch_status=None), "branch_inactive", setup, False),
        (_row(customer_id=None), "missing_operational_mapping", setup, False),
        (_row(branch_id=None), "missing_operational_mapping", setup, False),
        (_row(customer_id=None, branch_id=None), "missing_operational_mapping", setup, False),
        (_row(customer_id=0, branch_id=0), "ready", onboarding, True),
        (_row(onboarding_status="ready"), "ready", onboarding, True),
        (_row(onboarding_status="pending"), "pending", onboarding, False),
        (_row(onboarding_status="paused"), "paused", onboarding, False),
        (_row(onboarding_status=None), "pending", onboarding, False),
        (_row(onboarding_status=""), "pending", onboarding, False),
        (_row(onboarding_status="initial_sync"), "initial_sync", onboarding, False),
        (_row(onboarding_status="something_new"), "something_new", onboarding, False),
        (_row(onboarding_status="READY"), "READY", onboarding, False),
        (_row(onboarding_status=" ready"), " ready", onboarding, False),
        # A stored status that spells a setup code is still a status.
        (_row(onboarding_status="branch_inactive"), "branch_inactive", onboarding, False),
    ]
    for row, code, from_onboarding, ready in cases:
        result = branch_readiness(row)
        assert result == BranchReadiness(code, onboarding=from_onboarding), row
        assert result.is_ready is ready, row


def test_the_checks_are_made_in_the_dashboards_order():
    # Each earlier reason wins over every later one.
    assert branch_readiness(_row(branch_slug=None, branch_status="inactive", customer_id=None, onboarding_status="paused")).code == "branch_not_found"
    assert branch_readiness(_row(branch_status="inactive", customer_id=None, onboarding_status="paused")).code == "branch_inactive"
    assert branch_readiness(_row(customer_id=None, onboarding_status="paused")).code == "missing_operational_mapping"


def test_only_exactly_ready_is_ready():
    assert READY == "ready"
    assert [status for status in ("ready", "pending", "paused", None, "", "READY", "unexpected")
            if branch_readiness(_row(onboarding_status=status)).is_ready] == ["ready"]


def test_the_row_is_not_changed():
    row = _row(onboarding_status=None)
    before = copy.deepcopy(row)

    branch_readiness(row)

    assert row == before


def test_the_module_is_pure():
    code = inspect.getsource(module).split('"""', 2)[2]

    for forbidden in ("streamlit", "fastapi", "sqlalchemy", "database", "get_engine", "os.getenv", "os.environ", "logging", "print("):
        assert forbidden not in code, forbidden
