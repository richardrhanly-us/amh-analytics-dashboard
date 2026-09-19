"""Tests for platform_admin_service.list_libraries_with_status.

Two properties are pinned here:

1. pipeline_status is keyed by OPERATIONAL (customer_id, branch_id). The Super
Admin listing must reach it through organizations.operational_customer_id /
branches.operational_branch_id, never through the SaaS-facing
organizations.id / branches.id (the two ID domains only coincide by accident
for some tenants, e.g. NBPL at 1/1).

2. An organization with several subscription rows is joined to only its most
recent one (newest created_at, id as tie-breaker), so it yields one row.

The behavioral tests below run the real query against an in-memory SQLite
database (the SQL is plain ANSI: left joins, lower(), a boolean literal), so
they prove what the join actually matches rather than just what the SQL text
says. One FakeEngine-based test additionally pins the SQL text.
"""

import pytest
from db_fakes import FakeEngine, FakeQueryResult
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from src.services import platform_admin_service

_SCHEMA = [
    """CREATE TABLE organizations (
        id INTEGER PRIMARY KEY, name TEXT, slug TEXT, status TEXT,
        operational_customer_id INTEGER)""",
    """CREATE TABLE branches (
        id INTEGER PRIMARY KEY, organization_id INTEGER, name TEXT, slug TEXT,
        is_primary BOOLEAN, operational_branch_id INTEGER)""",
    "CREATE TABLE plans (id INTEGER PRIMARY KEY, code TEXT, name TEXT)",
    """CREATE TABLE subscriptions (
        id INTEGER PRIMARY KEY, organization_id INTEGER, plan_id INTEGER,
        status TEXT, created_at TEXT)""",
    """CREATE TABLE pipeline_status (
        customer_id INTEGER, branch_id INTEGER, status TEXT,
        last_run TEXT, last_attempt TEXT)""",
]


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as conn:
        for ddl in _SCHEMA:
            conn.execute(text(ddl))
    monkeypatch.setattr(platform_admin_service, "get_engine", lambda: engine)
    return engine


def _add_library(engine, *, org_id, branch_id, operational_customer_id, operational_branch_id):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations VALUES (:id, :name, :slug, 'active', :ocid)"),
            {"id": org_id, "name": f"Org {org_id}", "slug": f"org-{org_id}",
             "ocid": operational_customer_id},
        )
        conn.execute(
            text("INSERT INTO branches VALUES (:id, :org, :name, :slug, 1, :obid)"),
            {"id": branch_id, "org": org_id, "name": f"Branch {branch_id}",
             "slug": f"branch-{branch_id}", "obid": operational_branch_id},
        )


def _add_pipeline_status(engine, *, customer_id, branch_id, status):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO pipeline_status VALUES (:c, :b, :s, '2026-09-18 08:00:00', "
                 "'2026-09-18 08:05:00')"),
            {"c": customer_id, "b": branch_id, "s": status},
        )


def _add_plan(engine, *, plan_id, code):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO plans VALUES (:id, :code, :name)"),
            {"id": plan_id, "code": code, "name": code.title()},
        )


def _add_subscription(engine, *, subscription_id, org_id, plan_id, status, created_at):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO subscriptions VALUES (:id, :org, :plan, :status, :created)"),
            {"id": subscription_id, "org": org_id, "plan": plan_id,
             "status": status, "created": created_at},
        )


def _by_org(rows):
    return {row["organization_id"]: row for row in rows}


def test_pipeline_status_is_joined_through_operational_ids(db):
    # SaaS IDs (10/20) differ from operational IDs (100/200). The decoy row
    # sits at the SaaS IDs and must NOT be picked up; the row at the
    # operational IDs is the one that belongs to this tenant.
    _add_library(db, org_id=10, branch_id=20,
                 operational_customer_id=100, operational_branch_id=200)
    _add_pipeline_status(db, customer_id=10, branch_id=20, status="decoy-saas-ids")
    _add_pipeline_status(db, customer_id=100, branch_id=200, status="success")

    rows = platform_admin_service.list_libraries_with_status()

    assert len(rows) == 1
    assert rows[0]["organization_id"] == 10
    assert rows[0]["branch_id"] == 20
    assert rows[0]["pipeline_status"] == "success"
    assert rows[0]["last_run"] == "2026-09-18 08:00:00"
    assert rows[0]["last_attempt"] == "2026-09-18 08:05:00"


def test_saas_ids_alone_cannot_match_a_pipeline_status_row(db):
    # Operational mapping exists but points elsewhere; the only pipeline row
    # happens to sit at the SaaS IDs. Coincidence in the ID domains must not
    # count as a match.
    _add_library(db, org_id=2, branch_id=2,
                 operational_customer_id=7, operational_branch_id=8)
    _add_pipeline_status(db, customer_id=2, branch_id=2, status="historical")

    rows = platform_admin_service.list_libraries_with_status()

    assert rows[0]["pipeline_status"] is None
    assert rows[0]["last_run"] is None
    assert rows[0]["last_attempt"] is None


def test_null_operational_mapping_produces_no_pipeline_status(db):
    # The clean-install case: no operational mapping, but a stale historical
    # pipeline_status row exists at customer_id=2, branch_id=2 (the tenant's
    # SaaS IDs). No fallback to the SaaS IDs -- it must show as not reporting.
    _add_library(db, org_id=2, branch_id=2,
                 operational_customer_id=None, operational_branch_id=None)
    _add_pipeline_status(db, customer_id=2, branch_id=2, status="historical")

    rows = platform_admin_service.list_libraries_with_status()

    assert len(rows) == 1
    assert rows[0]["organization_id"] == 2
    assert rows[0]["pipeline_status"] is None
    assert rows[0]["last_run"] is None
    assert rows[0]["last_attempt"] is None


@pytest.mark.parametrize(
    ("operational_customer_id", "operational_branch_id"),
    [(2, None), (None, 2)],
)
def test_partial_operational_mapping_produces_no_pipeline_status(
    db, operational_customer_id, operational_branch_id,
):
    _add_library(db, org_id=2, branch_id=2,
                 operational_customer_id=operational_customer_id,
                 operational_branch_id=operational_branch_id)
    _add_pipeline_status(db, customer_id=2, branch_id=2, status="historical")

    rows = platform_admin_service.list_libraries_with_status()

    assert rows[0]["pipeline_status"] is None


def test_matching_id_domains_still_join_correctly(db):
    # NBPL-style: SaaS IDs and operational IDs are both 1/1. Joining through
    # the bridge must still find its pipeline_status row.
    _add_library(db, org_id=1, branch_id=1,
                 operational_customer_id=1, operational_branch_id=1)
    _add_pipeline_status(db, customer_id=1, branch_id=1, status="success")

    rows = platform_admin_service.list_libraries_with_status()

    assert rows[0]["pipeline_status"] == "success"


def test_mapped_and_unmapped_libraries_are_resolved_independently(db):
    # Reproduces the live situation: NBPL (mapped, 1/1) alongside a
    # clean-install tenant (2/2, unmapped) that has a stale pipeline row at
    # its SaaS IDs. Only NBPL may show as reporting.
    _add_library(db, org_id=1, branch_id=1,
                 operational_customer_id=1, operational_branch_id=1)
    _add_library(db, org_id=2, branch_id=2,
                 operational_customer_id=None, operational_branch_id=None)
    _add_pipeline_status(db, customer_id=1, branch_id=1, status="success")
    _add_pipeline_status(db, customer_id=2, branch_id=2, status="historical")

    rows = _by_org(platform_admin_service.list_libraries_with_status())

    assert rows[1]["pipeline_status"] == "success"
    assert rows[2]["pipeline_status"] is None


# --- operational identity columns --------------------------------------------

def test_listing_exposes_operational_identity_for_mapped_and_unmapped_libraries(db):
    # NBPL-style mapped 1/1 alongside a clean-install-style unmapped tenant
    # (SaaS 2/2, NULL bridges). The unmapped tenant must read as NOT
    # operationally provisioned even though a historical row sits at 2/2.
    _add_library(db, org_id=1, branch_id=1,
                 operational_customer_id=1, operational_branch_id=1)
    _add_library(db, org_id=2, branch_id=2,
                 operational_customer_id=None, operational_branch_id=None)
    _add_pipeline_status(db, customer_id=2, branch_id=2, status="historical")

    by_org = _by_org(platform_admin_service.list_libraries_with_status())

    assert by_org[1]["operational_customer_id"] == 1
    assert by_org[1]["operational_branch_id"] == 1
    assert by_org[2]["operational_customer_id"] is None
    assert by_org[2]["operational_branch_id"] is None
    assert by_org[2]["pipeline_status"] is None


def test_listing_reports_operational_ids_distinct_from_saas_ids(db):
    _add_library(db, org_id=2, branch_id=2,
                 operational_customer_id=50, operational_branch_id=2)

    row = platform_admin_service.list_libraries_with_status()[0]

    assert row["organization_id"] == 2
    assert row["operational_customer_id"] == 50
    assert row["branch_id"] == 2
    assert row["operational_branch_id"] == 2


# --- latest-subscription join ------------------------------------------------

def test_organization_with_multiple_subscriptions_returns_one_row(db):
    _add_library(db, org_id=1, branch_id=1,
                 operational_customer_id=1, operational_branch_id=1)
    _add_plan(db, plan_id=1, code="trial")
    _add_plan(db, plan_id=2, code="pro")
    _add_subscription(db, subscription_id=1, org_id=1, plan_id=1,
                      status="trial", created_at="2026-01-01 00:00:00")
    _add_subscription(db, subscription_id=2, org_id=1, plan_id=2,
                      status="active", created_at="2026-06-01 00:00:00")
    _add_subscription(db, subscription_id=3, org_id=1, plan_id=2,
                      status="cancelled", created_at="2026-03-01 00:00:00")

    rows = platform_admin_service.list_libraries_with_status()

    assert len(rows) == 1
    assert rows[0]["organization_id"] == 1


def test_newest_subscription_wins_by_created_at_not_id(db):
    # The newest-by-created_at row has the LOWEST id, so this fails if the
    # join were picking by id (or by insertion order) instead of created_at.
    _add_library(db, org_id=1, branch_id=1,
                 operational_customer_id=1, operational_branch_id=1)
    _add_plan(db, plan_id=1, code="trial")
    _add_plan(db, plan_id=2, code="pro")
    _add_subscription(db, subscription_id=1, org_id=1, plan_id=2,
                      status="active", created_at="2026-06-01 00:00:00")
    _add_subscription(db, subscription_id=2, org_id=1, plan_id=1,
                      status="trial", created_at="2026-01-01 00:00:00")

    rows = platform_admin_service.list_libraries_with_status()

    assert len(rows) == 1
    assert rows[0]["subscription_status"] == "active"
    assert rows[0]["plan_code"] == "pro"
    assert rows[0]["plan_name"] == "Pro"


def test_subscription_created_at_tie_is_broken_deterministically_by_highest_id(db):
    _add_library(db, org_id=1, branch_id=1,
                 operational_customer_id=1, operational_branch_id=1)
    _add_plan(db, plan_id=1, code="trial")
    _add_plan(db, plan_id=2, code="pro")
    _add_subscription(db, subscription_id=1, org_id=1, plan_id=1,
                      status="trial", created_at="2026-06-01 00:00:00")
    _add_subscription(db, subscription_id=2, org_id=1, plan_id=2,
                      status="active", created_at="2026-06-01 00:00:00")

    rows = platform_admin_service.list_libraries_with_status()

    assert len(rows) == 1
    assert rows[0]["subscription_status"] == "active"
    assert rows[0]["plan_code"] == "pro"


def test_organization_without_a_subscription_still_appears(db):
    _add_library(db, org_id=1, branch_id=1,
                 operational_customer_id=1, operational_branch_id=1)

    rows = platform_admin_service.list_libraries_with_status()

    assert len(rows) == 1
    assert rows[0]["organization_id"] == 1
    assert rows[0]["subscription_status"] is None
    assert rows[0]["plan_code"] is None
    assert rows[0]["plan_name"] is None


def test_subscriptions_are_resolved_per_organization(db):
    # Org 1 has two subscriptions, org 2 has one, org 3 has none: one row
    # each, and each org sees only its own newest subscription.
    for org_id in (1, 2, 3):
        _add_library(db, org_id=org_id, branch_id=org_id,
                     operational_customer_id=org_id, operational_branch_id=org_id)
    _add_plan(db, plan_id=1, code="trial")
    _add_plan(db, plan_id=2, code="pro")
    _add_subscription(db, subscription_id=1, org_id=1, plan_id=1,
                      status="trial", created_at="2026-01-01 00:00:00")
    _add_subscription(db, subscription_id=2, org_id=1, plan_id=2,
                      status="active", created_at="2026-06-01 00:00:00")
    _add_subscription(db, subscription_id=3, org_id=2, plan_id=1,
                      status="trial", created_at="2026-02-01 00:00:00")

    rows = platform_admin_service.list_libraries_with_status()
    by_org = _by_org(rows)

    assert len(rows) == 3
    assert by_org[1]["plan_code"] == "pro"
    assert by_org[2]["plan_code"] == "trial"
    assert by_org[3]["plan_code"] is None


def test_multiple_subscriptions_do_not_disturb_operational_pipeline_join(db):
    # Combines both fixes: a mapped org with several subscriptions yields one
    # row that still carries the pipeline_status found via the operational
    # bridge, while an unmapped org with a stale row at its SaaS IDs still
    # shows no pipeline status.
    _add_library(db, org_id=10, branch_id=20,
                 operational_customer_id=100, operational_branch_id=200)
    _add_library(db, org_id=2, branch_id=2,
                 operational_customer_id=None, operational_branch_id=None)
    _add_plan(db, plan_id=1, code="trial")
    _add_plan(db, plan_id=2, code="pro")
    _add_subscription(db, subscription_id=1, org_id=10, plan_id=1,
                      status="trial", created_at="2026-01-01 00:00:00")
    _add_subscription(db, subscription_id=2, org_id=10, plan_id=2,
                      status="active", created_at="2026-06-01 00:00:00")
    _add_pipeline_status(db, customer_id=10, branch_id=20, status="decoy-saas-ids")
    _add_pipeline_status(db, customer_id=100, branch_id=200, status="success")
    _add_pipeline_status(db, customer_id=2, branch_id=2, status="historical")

    rows = platform_admin_service.list_libraries_with_status()
    by_org = _by_org(rows)

    assert len(rows) == 2
    assert by_org[10]["pipeline_status"] == "success"
    assert by_org[10]["plan_code"] == "pro"
    assert by_org[2]["pipeline_status"] is None


def test_list_libraries_sql_joins_pipeline_status_on_operational_columns(monkeypatch):
    engine = FakeEngine([FakeQueryResult(all_rows=[])])
    monkeypatch.setattr(platform_admin_service, "get_engine", lambda: engine)

    platform_admin_service.list_libraries_with_status()

    sql = " ".join(engine.calls[0]["sql"].lower().split())
    assert "ps.customer_id = o.operational_customer_id" in sql
    assert "ps.branch_id = b.operational_branch_id" in sql
    assert "ps.customer_id = o.id" not in sql
    assert "ps.branch_id = b.id" not in sql
