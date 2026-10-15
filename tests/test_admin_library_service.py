"""A2 item 2: services.admin_library_service, the libraries a platform administrator sees.

The real statements run against an in-memory SQLite database holding the columns they touch -- plus the ones they must
never return -- as tests/test_platform_admin_service.py does for the Super Admin listing. Imported the "flat" way, the
identity the API process uses.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from services import admin_library_service
from services.admin_library_service import LibraryRow

_SCHEMA = [
    """CREATE TABLE organizations (
        id INTEGER PRIMARY KEY, name TEXT, slug TEXT, status TEXT, operational_customer_id INTEGER)""",
    """CREATE TABLE branches (
        id INTEGER PRIMARY KEY, organization_id INTEGER, name TEXT, slug TEXT, is_primary BOOLEAN,
        operational_branch_id INTEGER, status TEXT)""",
    "CREATE TABLE plans (id INTEGER PRIMARY KEY, code TEXT, name TEXT)",
    """CREATE TABLE subscriptions (
        id INTEGER PRIMARY KEY, organization_id INTEGER, plan_id INTEGER, status TEXT, created_at TEXT)""",
]


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as conn:
        for ddl in _SCHEMA:
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO plans (id, code, name) VALUES (1, 'starter', 'Starter'), (3, 'enterprise', 'Enterprise')"))
    monkeypatch.setattr(admin_library_service, "get_engine", lambda: engine)
    return engine


def _org(engine, org_id, slug, name, *, status="active", customer=None):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, name, slug, status, operational_customer_id) VALUES (:i, :n, :s, :st, :c)"),
            {"i": org_id, "n": name, "s": slug, "st": status, "c": customer},
        )


def _branch(engine, branch_id, org_id, slug, *, name=None, primary=True, operational=None, status="active"):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO branches (id, organization_id, name, slug, is_primary, operational_branch_id, status) "
                "VALUES (:i, :o, :n, :s, :p, :ob, :st)"
            ),
            {"i": branch_id, "o": org_id, "n": name or slug.title(), "s": slug, "p": primary, "ob": operational, "st": status},
        )


def _subscription(engine, sub_id, org_id, plan_id, status, created_at):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO subscriptions (id, organization_id, plan_id, status, created_at) VALUES (:i, :o, :p, :s, :c)"),
            {"i": sub_id, "o": org_id, "p": plan_id, "s": status, "c": created_at},
        )


# --- library rows ------------------------------------------------------------------------------------------------

def test_a_library_row_holds_the_organization_its_primary_branch_and_its_plan_and_no_id(db):
    _org(db, 1, "nbpl", "New Braunfels Public Library", customer=1)
    _branch(db, 1, 1, "main", name="Main Branch", operational=1)
    _subscription(db, 1, 1, 3, "trial", "2026-01-01")

    assert admin_library_service.list_library_rows() == [
        LibraryRow(
            slug="nbpl",
            name="New Braunfels Public Library",
            status="active",
            primary_branch_slug="main",
            primary_branch_name="Main Branch",
            primary_branch_status="active",
            operational_identity="assigned",
            plan_code="enterprise",
            plan_name="Enterprise",
            subscription_status="trial",
        )
    ]
    assert not any(field.name.endswith("id") for field in dataclasses.fields(LibraryRow))


def test_every_library_is_listed_by_name_cancelled_and_suspended_included(db):
    _org(db, 1, "zeta", "Zeta Library")
    _org(db, 2, "alpha", "alpha Library", status="cancelled")
    _org(db, 3, "mid", "Mid Library", status="suspended")

    assert [(row.slug, row.status) for row in admin_library_service.list_library_rows()] == [
        ("alpha", "cancelled"),
        ("mid", "suspended"),
        ("zeta", "active"),
    ]


def test_only_the_primary_branch_is_the_libraries_branch(db):
    _org(db, 1, "acme", "Acme")
    _branch(db, 1, 1, "east", primary=False)
    _branch(db, 2, 1, "central", primary=True)

    (row,) = admin_library_service.list_library_rows()
    assert row.primary_branch_slug == "central"


def test_a_library_without_a_primary_branch_or_subscription_has_none(db):
    _org(db, 1, "bare", "Bare Library")
    _branch(db, 1, 1, "only", primary=False)

    (row,) = admin_library_service.list_library_rows()
    assert (row.primary_branch_slug, row.primary_branch_name, row.primary_branch_status) == (None, None, None)
    assert (row.plan_code, row.plan_name, row.subscription_status) == (None, None, None)
    assert row.operational_identity == "missing"


@pytest.mark.parametrize(
    ("customer", "operational_branch", "expected"),
    [(7, 3, "assigned"), (7, None, "partial"), (None, 3, "partial"), (None, None, "missing")],
)
def test_operational_identity_is_a_word_never_the_ids(db, customer, operational_branch, expected):
    _org(db, 1, "acme", "Acme", customer=customer)
    _branch(db, 3, 1, "main", operational=operational_branch)

    (row,) = admin_library_service.list_library_rows()
    assert row.operational_identity == expected


def test_the_newest_subscription_wins_by_created_at_then_highest_id(db):
    _org(db, 1, "acme", "Acme")
    _subscription(db, 1, 1, 1, "cancelled", "2026-01-01")
    _subscription(db, 2, 1, 3, "active", "2026-06-01")
    _subscription(db, 3, 1, 1, "trial", "2026-03-01")
    _org(db, 2, "tie", "Tie")
    _subscription(db, 4, 2, 3, "trial", "2026-05-05")
    _subscription(db, 5, 2, 1, "active", "2026-05-05")

    rows = {row.slug: row for row in admin_library_service.list_library_rows()}
    assert (rows["acme"].plan_code, rows["acme"].subscription_status) == ("enterprise", "active")
    assert (rows["tie"].plan_code, rows["tie"].subscription_status) == ("starter", "active")


def test_a_second_primary_branch_never_makes_a_second_row(db):
    _org(db, 1, "acme", "Acme")
    _branch(db, 2, 1, "second", primary=True)
    _branch(db, 1, 1, "first", primary=True)

    rows = admin_library_service.list_library_rows()
    assert [(row.slug, row.primary_branch_slug) for row in rows] == [("acme", "first")]


def test_one_library_by_slug_or_none(db):
    _org(db, 1, "acme", "Acme")
    _org(db, 2, "other", "Other")

    assert admin_library_service.get_library_row("acme").slug == "acme"
    assert admin_library_service.get_library_row("missing") is None


# --- the tenant behind one site ------------------------------------------------------------------------------------

def test_a_site_resolves_to_its_operational_tenant_without_any_membership(db):
    _org(db, 1, "nbpl", "NBPL", customer=11, status="suspended")
    _branch(db, 4, 1, "main", operational=4)

    tenant = admin_library_service.site_tenant("nbpl", "main")

    assert (tenant.org_slug, tenant.branch_slug, tenant.access_mode) == ("nbpl", "main", "read_only")
    assert (tenant.operational_customer_id, tenant.operational_branch_id) == (11, 4)


def test_a_cancelled_librarys_site_still_resolves(db):
    _org(db, 1, "gone", "Gone", customer=5, status="cancelled")
    _branch(db, 2, 1, "main", operational=2)

    assert admin_library_service.site_tenant("gone", "main").access_mode == "blocked"


def test_a_branch_of_another_organization_does_not_resolve(db):
    _org(db, 1, "acme", "Acme", customer=1)
    _org(db, 2, "other", "Other", customer=2)
    _branch(db, 5, 2, "theirs", operational=5)

    assert admin_library_service.site_tenant("acme", "theirs") is None


@pytest.mark.parametrize(("customer", "operational_branch"), [(None, 3), (7, None), (None, None)])
def test_a_site_without_both_operational_ids_does_not_resolve(db, customer, operational_branch):
    _org(db, 1, "acme", "Acme", customer=customer)
    _branch(db, 3, 1, "main", operational=operational_branch)

    assert admin_library_service.site_tenant("acme", "main") is None


def test_an_unknown_site_does_not_resolve(db):
    _org(db, 1, "acme", "Acme", customer=1)

    assert admin_library_service.site_tenant("acme", "nowhere") is None
    assert admin_library_service.site_tenant("nobody", "main") is None


def test_more_than_one_matching_branch_is_refused_not_guessed(db):
    _org(db, 1, "acme", "Acme", customer=1)
    _branch(db, 3, 1, "main", operational=3)
    _branch(db, 4, 1, "main", operational=4, primary=False)

    assert admin_library_service.site_tenant("acme", "main") is None
