"""Block 3d: the operational tenant resolver.

resolve_operational_tenant(user_id, org_slug, branch_slug) is a security
boundary: it is the only place a SaaS organization/branch is turned into the
operational (customer_id, branch_id) that later becomes the RLS tenant
context. These tests run its REAL SQL against an in-memory SQLite database
holding the four tables it joins, so the joins themselves -- not a fake's
canned answer -- are what decide every case below.

The tables here carry no CHECK or UNIQUE constraints on purpose: that lets a
test insert rows the production schema forbids (an unrecognised status, a
duplicate membership) and prove the resolver still fails closed. The
PostgreSQL schema and RLS interplay are covered in
tests/test_rls_phase1_postgres.py.

Imported the "flat" way (services.tenant_resolution_service), the identity
the API process uses.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from services import access_service, tenant_resolution_service
from services.tenant_resolution_service import (
    ResolvedOperationalTenant,
    resolve_operational_tenant,
)

_DDL = (
    "CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT, is_active BOOLEAN)",
    (
        "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, name TEXT, status TEXT, "
        "operational_customer_id INTEGER)"
    ),
    (
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, name TEXT, "
        "is_primary BOOLEAN, status TEXT, operational_branch_id INTEGER)"
    ),
    "CREATE TABLE memberships (id INTEGER PRIMARY KEY, organization_id INTEGER, user_id INTEGER, role TEXT)",
)

# Org A ("acme") and Org B ("beta"). Alice belongs to A only, Bob to B only,
# Carol to both. Both organizations have a branch called "main" -- the same
# slug, two different branches -- and each has one branch the other lacks.
ALICE, BOB, CAROL = 1, 2, 3
ACME_CUSTOMER, BETA_CUSTOMER = 8101, 8202
ACME_MAIN, ACME_EAST, BETA_MAIN, BETA_NORTH = 11, 12, 21, 22

_SEED = (
    (
        "INSERT INTO app_users (id, email, is_active) VALUES (1, 'alice@example.invalid', 1), "
        "(2, 'bob@example.invalid', 1), (3, 'carol@example.invalid', 1)"
    ),
    (
        f"INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES "
        f"(1, 'acme', 'Acme', 'active', {ACME_CUSTOMER}), (2, 'beta', 'Beta', 'active', {BETA_CUSTOMER})"
    ),
    (
        f"INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) VALUES "
        f"({ACME_MAIN}, 1, 'main', 'Main', 1, 'active', {ACME_MAIN}), "
        f"({ACME_EAST}, 1, 'east', 'East', 0, 'active', {ACME_EAST}), "
        f"({BETA_MAIN}, 2, 'main', 'Main', 1, 'active', {BETA_MAIN}), "
        f"({BETA_NORTH}, 2, 'north', 'North', 0, 'active', {BETA_NORTH})"
    ),
    (
        "INSERT INTO memberships (organization_id, user_id, role) VALUES "
        "(1, 1, 'admin'), (2, 2, 'viewer'), (1, 3, 'viewer'), (2, 3, 'owner')"
    ),
)


class Database:
    def __init__(self, engine):
        self.engine = engine
        self.statements: list[str] = []
        event.listen(engine, "before_cursor_execute", self._record)

    def _record(self, _conn, _cursor, statement, _parameters, _context, _executemany):
        self.statements.append(statement)

    def run(self, sql: str, **params) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(sql), params)

    def resolver_queries(self) -> int:
        return sum(1 for statement in self.statements if "FROM memberships m" in statement)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in (*_DDL, *_SEED):
            conn.execute(text(statement))

    monkeypatch.setattr(tenant_resolution_service, "get_engine", lambda: engine)
    monkeypatch.setattr(access_service, "get_engine", lambda: engine)
    yield Database(engine)
    engine.dispose()


def _ids(resolved: ResolvedOperationalTenant | None) -> tuple[int, int] | None:
    return None if resolved is None else (resolved.operational_customer_id, resolved.operational_branch_id)


# =====================================================================================================================
# Success
# =====================================================================================================================

def test_a_member_resolves_an_active_branch_of_their_organization(db):
    resolved = resolve_operational_tenant(ALICE, "acme", "main")

    assert resolved == ResolvedOperationalTenant(
        org_slug="acme",
        branch_slug="main",
        access_mode="full",
        operational_customer_id=ACME_CUSTOMER,
        operational_branch_id=ACME_MAIN,
    )


def test_each_branch_of_the_organization_resolves_to_its_own_operational_branch(db):
    assert _ids(resolve_operational_tenant(ALICE, "acme", "main")) == (ACME_CUSTOMER, ACME_MAIN)
    assert _ids(resolve_operational_tenant(ALICE, "acme", "east")) == (ACME_CUSTOMER, ACME_EAST)


def test_a_member_of_two_organizations_resolves_each_one_separately(db):
    # Both organizations have a branch whose slug is "main".
    assert _ids(resolve_operational_tenant(CAROL, "acme", "main")) == (ACME_CUSTOMER, ACME_MAIN)
    assert _ids(resolve_operational_tenant(CAROL, "beta", "main")) == (BETA_CUSTOMER, BETA_MAIN)


def test_a_trial_organization_resolves_with_full_access(db):
    db.run("UPDATE organizations SET status = 'trial' WHERE slug = 'acme'")

    assert resolve_operational_tenant(ALICE, "acme", "main").access_mode == "full"


def test_a_suspended_organization_resolves_for_reading_as_read_only(db):
    db.run("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'")

    resolved = resolve_operational_tenant(ALICE, "acme", "main")

    assert resolved.access_mode == "read_only"
    assert _ids(resolved) == (ACME_CUSTOMER, ACME_MAIN)


def test_operational_ids_of_zero_are_valid(db):
    # tenant_db treats 0 as a valid id; only NULL means "not mapped".
    db.run("UPDATE organizations SET operational_customer_id = 0 WHERE slug = 'acme'")
    db.run("UPDATE branches SET operational_branch_id = 0 WHERE id = :id", id=ACME_MAIN)

    assert _ids(resolve_operational_tenant(ALICE, "acme", "main")) == (0, 0)


def test_the_result_carries_the_slugs_of_the_rows_that_were_resolved(db):
    resolved = resolve_operational_tenant(ALICE, "acme", "east")

    assert (resolved.org_slug, resolved.branch_slug) == ("acme", "east")


# =====================================================================================================================
# Organization failures
# =====================================================================================================================

def test_an_unknown_organization_does_not_resolve(db):
    assert resolve_operational_tenant(ALICE, "no-such-org", "main") is None


def test_a_non_member_does_not_resolve_an_organization_that_exists(db):
    assert resolve_operational_tenant(ALICE, "beta", "main") is None
    assert resolve_operational_tenant(BOB, "acme", "main") is None


def test_an_unknown_user_does_not_resolve(db):
    assert resolve_operational_tenant(999, "acme", "main") is None


def test_a_removed_membership_stops_resolving(db):
    assert resolve_operational_tenant(ALICE, "acme", "main") is not None

    db.run("DELETE FROM memberships WHERE user_id = :u AND organization_id = 1", u=ALICE)

    assert resolve_operational_tenant(ALICE, "acme", "main") is None


def test_an_inactive_user_does_not_resolve(db):
    db.run("UPDATE app_users SET is_active = 0 WHERE id = :u", u=ALICE)

    assert resolve_operational_tenant(ALICE, "acme", "main") is None


@pytest.mark.parametrize("status", ["cancelled", "inactive", "ACTIVE", "", None])
def test_a_cancelled_or_unrecognised_organization_status_does_not_resolve(db, status):
    db.run("UPDATE organizations SET status = :s WHERE slug = 'acme'", s=status)

    assert resolve_operational_tenant(ALICE, "acme", "main") is None


def test_an_organization_without_an_operational_customer_id_does_not_resolve(db):
    db.run("UPDATE organizations SET operational_customer_id = NULL WHERE slug = 'acme'")

    assert resolve_operational_tenant(ALICE, "acme", "main") is None


@pytest.mark.parametrize("status", ["active", "trial", "suspended", "cancelled", "inactive", ""])
def test_the_access_mode_rule_agrees_with_access_service_for_every_status(db, status):
    db.run("UPDATE organizations SET status = :s WHERE slug = 'acme'", s=status)

    mode = access_service.get_org_access_mode("acme")
    resolved = resolve_operational_tenant(ALICE, "acme", "main")

    if mode == "blocked":
        assert resolved is None
    else:
        assert resolved.access_mode == mode


# =====================================================================================================================
# Branch failures
# =====================================================================================================================

def test_an_unknown_branch_does_not_resolve(db):
    assert resolve_operational_tenant(ALICE, "acme", "no-such-branch") is None


def test_a_branch_of_another_organization_does_not_resolve(db):
    # "north" exists, is active and is mapped -- in Org B.
    assert resolve_operational_tenant(ALICE, "acme", "north") is None
    # ...and "east" exists only in Org A.
    assert resolve_operational_tenant(BOB, "beta", "east") is None


def test_a_member_of_both_organizations_still_cannot_mix_them(db):
    assert resolve_operational_tenant(CAROL, "acme", "north") is None
    assert resolve_operational_tenant(CAROL, "beta", "east") is None


@pytest.mark.parametrize("status", ["inactive", "ACTIVE", "", None])
def test_an_inactive_or_unrecognised_branch_status_does_not_resolve(db, status):
    db.run("UPDATE branches SET status = :s WHERE id = :id", s=status, id=ACME_MAIN)

    assert resolve_operational_tenant(ALICE, "acme", "main") is None
    # No substitution: the organization's other active branch is not offered instead.
    assert _ids(resolve_operational_tenant(ALICE, "acme", "east")) == (ACME_CUSTOMER, ACME_EAST)


def test_a_branch_without_an_operational_branch_id_does_not_resolve(db):
    db.run("UPDATE branches SET operational_branch_id = NULL WHERE id = :id", id=ACME_MAIN)

    assert resolve_operational_tenant(ALICE, "acme", "main") is None


def test_there_is_no_fallback_to_the_primary_branch(db):
    for branch_slug in ("", "no-such-branch", "primary", "MAIN", " main", "main "):
        assert resolve_operational_tenant(ALICE, "acme", branch_slug) is None, branch_slug


def test_slugs_are_matched_exactly(db):
    for org_slug in ("ACME", "Acme", " acme", "acme ", "acm", "acme%", "%"):
        assert resolve_operational_tenant(ALICE, org_slug, "main") is None, org_slug


# =====================================================================================================================
# The tenant boundary
# =====================================================================================================================

def test_changing_only_the_branch_slug_never_leaves_the_organization(db):
    every_branch_slug = ("main", "east", "north", "no-such-branch")

    resolved = [resolve_operational_tenant(ALICE, "acme", slug) for slug in every_branch_slug]

    assert {r.operational_customer_id for r in resolved if r is not None} == {ACME_CUSTOMER}
    assert {r.operational_branch_id for r in resolved if r is not None} == {ACME_MAIN, ACME_EAST}


def test_no_combination_of_slugs_gives_a_user_another_organizations_tenant(db):
    slugs = ("acme", "beta", "main", "east", "north")
    reachable = {
        _ids(resolve_operational_tenant(ALICE, org, branch)) for org in slugs for branch in slugs
    } - {None}

    assert reachable == {(ACME_CUSTOMER, ACME_MAIN), (ACME_CUSTOMER, ACME_EAST)}


def test_the_customer_id_comes_from_the_organization_row_not_from_the_branch(db):
    # A branch row that (wrongly) points at Org A while carrying Org B's
    # operational branch id still yields Org A's customer id: the customer is
    # always the organization reached through the user's own membership.
    db.run(
        "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) "
        "VALUES (99, 1, 'odd', 'Odd', 0, 'active', :b)",
        b=BETA_NORTH,
    )

    resolved = resolve_operational_tenant(ALICE, "acme", "odd")

    assert resolved.operational_customer_id == ACME_CUSTOMER


def test_the_resolver_accepts_no_operational_identifier():
    parameters = inspect.signature(resolve_operational_tenant).parameters

    assert list(parameters) == ["user_id", "org_slug", "branch_slug"]
    assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in parameters.values())


# =====================================================================================================================
# Ambiguity, errors, caching
# =====================================================================================================================

def test_a_duplicate_membership_row_fails_closed(db):
    db.run("INSERT INTO memberships (organization_id, user_id, role) VALUES (1, :u, 'owner')", u=ALICE)

    assert resolve_operational_tenant(ALICE, "acme", "main") is None


def test_a_duplicate_branch_slug_in_one_organization_fails_closed(db):
    db.run(
        "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) "
        "VALUES (98, 1, 'main', 'Main again', 0, 'active', 98)"
    )

    assert resolve_operational_tenant(ALICE, "acme", "main") is None


def test_a_duplicate_organization_slug_fails_closed(db):
    db.run("INSERT INTO organizations (id, slug, name, status, operational_customer_id) "
           "VALUES (3, 'acme', 'Acme again', 'active', 8303)")
    db.run("INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) "
           "VALUES (31, 3, 'main', 'Main', 1, 'active', 31)")
    db.run("INSERT INTO memberships (organization_id, user_id, role) VALUES (3, :u, 'admin')", u=ALICE)

    assert resolve_operational_tenant(ALICE, "acme", "main") is None


def test_a_database_failure_propagates_instead_of_resolving_to_none(monkeypatch):
    class BrokenEngine:
        def connect(self):
            raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(tenant_resolution_service, "get_engine", lambda: BrokenEngine())

    with pytest.raises(RuntimeError, match="synthetic database failure"):
        resolve_operational_tenant(ALICE, "acme", "main")


def test_a_failing_query_propagates_instead_of_resolving_to_none(db):
    db.run("DROP TABLE memberships")

    with pytest.raises(Exception, match="memberships"):
        resolve_operational_tenant(ALICE, "acme", "main")


def test_every_call_queries_again_with_exactly_one_statement(db):
    resolve_operational_tenant(ALICE, "acme", "main")
    assert db.resolver_queries() == 1
    assert len(db.statements) == 1  # the whole decision is one statement

    resolve_operational_tenant(ALICE, "acme", "main")
    assert db.resolver_queries() == 2


def test_a_change_takes_effect_on_the_very_next_call(db):
    assert resolve_operational_tenant(ALICE, "acme", "main").access_mode == "full"

    db.run("UPDATE organizations SET status = 'suspended' WHERE slug = 'acme'")
    assert resolve_operational_tenant(ALICE, "acme", "main").access_mode == "read_only"

    db.run("UPDATE organizations SET status = 'cancelled' WHERE slug = 'acme'")
    assert resolve_operational_tenant(ALICE, "acme", "main") is None


def test_the_resolver_has_no_cache_and_holds_no_state():
    assert not hasattr(resolve_operational_tenant, "clear")
    assert not hasattr(resolve_operational_tenant, "cache_clear")
    assert not hasattr(resolve_operational_tenant, "__wrapped__")


# =====================================================================================================================
# The result object
# =====================================================================================================================

def test_the_result_is_immutable(db):
    resolved = resolve_operational_tenant(ALICE, "acme", "main")

    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.operational_customer_id = BETA_CUSTOMER


def test_the_result_holds_only_what_downstream_code_needs():
    assert [f.name for f in dataclasses.fields(ResolvedOperationalTenant)] == [
        "org_slug", "branch_slug", "access_mode", "operational_customer_id", "operational_branch_id",
    ]


def test_the_repr_does_not_carry_the_operational_ids(db):
    resolved = resolve_operational_tenant(ALICE, "acme", "main")

    assert repr(resolved) == "ResolvedOperationalTenant(org_slug='acme', branch_slug='main', access_mode='full')"
    assert str(ACME_CUSTOMER) not in f"{resolved} {resolved!r}"


def test_the_ids_are_plain_integers(db):
    resolved = resolve_operational_tenant(ALICE, "acme", "main")

    assert type(resolved.operational_customer_id) is int
    assert type(resolved.operational_branch_id) is int


# =====================================================================================================================
# Module boundaries
# =====================================================================================================================

def test_the_module_is_framework_neutral():
    source = inspect.getsource(tenant_resolution_service)

    for forbidden in ("import streamlit", "from streamlit", "fastapi", "starlette", "cache_data", "lru_cache",
                      "tenant_connection", "set_config"):
        assert forbidden not in source, forbidden
