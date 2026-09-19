"""Tests for scripts/create_agent_token.py scope validation.

(customer_id, branch_id) passed to the script are the OPERATIONAL pair. Before
inserting an agent token the script must prove the pair is a fully mapped,
single-tenant, usable organization/branch -- and must not display a raw token
for a pair it rejects.
"""

from __future__ import annotations

import sys

import pytest
from db_fakes import FakeEngine, FakeQueryResult

from scripts import create_agent_token as cat


def _customer(customer_id=1):
    return FakeQueryResult(first={"id": customer_id})


def _branch(branch_id=1, organization_id=1, status="active", operational_branch_id="same"):
    if operational_branch_id == "same":
        operational_branch_id = branch_id
    return FakeQueryResult(first={
        "id": branch_id, "organization_id": organization_id, "status": status,
        "operational_branch_id": operational_branch_id,
    })


def _orgs(*orgs):
    return FakeQueryResult(all_rows=[
        {"id": o[0], "slug": o[1], "status": o[2] if len(o) > 2 else "active"} for o in orgs
    ])


def _validate(*results, customer_id=1, branch_id=1):
    engine = FakeEngine(list(results))
    with engine.connect() as conn:
        return cat.validate_token_scope(conn, customer_id, branch_id), engine


# --- validate_token_scope ----------------------------------------------------

def test_nbpl_style_existing_mapping_is_accepted():
    scope, engine = _validate(_customer(1), _branch(1, 1), _orgs((1, "nbpl")))

    assert scope == {
        "organization_id": 1, "organization_slug": "nbpl", "customer_id": 1, "branch_id": 1,
    }
    # Read-only: three SELECTs, nothing else.
    assert len(engine.calls) == 3
    assert all("SELECT" in c["sql"] for c in engine.calls)


def test_mapped_tenant_with_distinct_operational_customer_is_accepted():
    # SaaS org 2 -> operational customer 50, branch 2 -> 2.
    scope, _ = _validate(
        _customer(50), _branch(2, 2), _orgs((2, "clean-install")),
        customer_id=50, branch_id=2,
    )

    assert scope["organization_id"] == 2
    assert scope["customer_id"] == 50


def test_trial_organization_is_accepted():
    scope, _ = _validate(_customer(), _branch(), _orgs((1, "acme", "trial")))

    assert scope["organization_slug"] == "acme"


def test_nonexistent_customer_is_rejected():
    with pytest.raises(cat.ScopeError, match=r"customer_id 9 does not exist"):
        _validate(FakeQueryResult(first=None), customer_id=9)


def test_nonexistent_branch_is_rejected():
    with pytest.raises(cat.ScopeError, match=r"branch_id 9 does not exist"):
        _validate(_customer(), FakeQueryResult(first=None), branch_id=9)


def test_unmapped_customer_with_no_organization_is_rejected():
    # The customers row exists, but no organization maps to it.
    with pytest.raises(cat.ScopeError, match="not mapped to any organization"):
        _validate(_customer(2), _branch(2, 2), _orgs(), customer_id=2, branch_id=2)


def test_clean_install_saas_pair_with_null_bridges_is_rejected():
    # SaaS 2/2, NULL bridges, no customers row 2: historical tokens/status rows
    # at 2/2 do not make it a valid operational scope.
    with pytest.raises(cat.ScopeError, match=r"customer_id 2 does not exist"):
        _validate(FakeQueryResult(first=None), customer_id=2, branch_id=2)


def test_unmapped_branch_is_rejected():
    with pytest.raises(cat.ScopeError, match="unmapped branch"):
        _validate(_customer(), _branch(operational_branch_id=None), _orgs((1, "nbpl")))


def test_branch_with_inconsistent_operational_id_is_rejected():
    with pytest.raises(cat.ScopeError, match="inconsistent operational_branch_id"):
        _validate(_customer(), _branch(1, 1, operational_branch_id=77), _orgs((1, "nbpl")))


def test_pair_spanning_different_tenants_is_rejected():
    # Customer 1 belongs to organization 1; branch 5 belongs to organization 2.
    with pytest.raises(cat.ScopeError, match="spans different tenants"):
        _validate(
            _customer(1), _branch(5, organization_id=2), _orgs((1, "nbpl")),
            customer_id=1, branch_id=5,
        )


def test_customer_mapped_to_more_than_one_organization_is_rejected():
    with pytest.raises(cat.ScopeError, match="2 organizations"):
        _validate(_customer(), _branch(), _orgs((1, "a"), (2, "b")))


@pytest.mark.parametrize("status", ["suspended", "cancelled", "inactive"])
def test_unusable_organization_status_is_rejected(status):
    with pytest.raises(cat.ScopeError, match="organization 1 status"):
        _validate(_customer(), _branch(), _orgs((1, "nbpl", status)))


def test_inactive_branch_is_rejected():
    with pytest.raises(cat.ScopeError, match="branch 1 status"):
        _validate(_customer(), _branch(status="inactive"), _orgs((1, "nbpl")))


# --- main(): nothing shown or written for a rejected pair ---------------------

_INSERT_MARKER = "INSERT INTO agent_tokens"


def _run_main(monkeypatch, engine, *extra_args):
    monkeypatch.setattr(cat, "generate_token", lambda: ("RAW-TOKEN-VALUE", "HASHVALUE"))
    monkeypatch.setattr(cat, "create_engine", lambda *a, **k: engine)
    monkeypatch.setattr(
        sys, "argv",
        ["create_agent_token.py", "--customer-id", "2", "--branch-id", "2",
         "--description", "test", *extra_args],
    )
    cat.main()


def test_execute_with_an_unmapped_pair_writes_nothing_and_prints_no_token(monkeypatch, capsys):
    # customers row 2 does not exist (clean-install style).
    engine = FakeEngine([FakeQueryResult(first=None)])

    with pytest.raises(SystemExit) as excinfo:
        _run_main(monkeypatch, engine, "--execute", "postgresql://unused")

    assert "REFUSED" in str(excinfo.value)
    assert not any(_INSERT_MARKER in c["sql"] for c in engine.calls)
    output = capsys.readouterr().out
    assert "RAW-TOKEN-VALUE" not in output


def test_execute_with_a_valid_pair_inserts_the_hash_and_shows_the_token_once(monkeypatch, capsys):
    engine = FakeEngine([
        _customer(2), _branch(2, 2), _orgs((2, "clean-install")),
        FakeQueryResult(),  # INSERT
    ])

    _run_main(monkeypatch, engine, "--execute", "postgresql://unused")

    inserts = [c for c in engine.calls if _INSERT_MARKER in c["sql"]]
    assert len(inserts) == 1
    assert inserts[0]["params"]["token_hash"] == "HASHVALUE"
    assert inserts[0]["params"]["customer_id"] == 2
    assert "RAW-TOKEN-VALUE" not in str(inserts[0]["params"])
    output = capsys.readouterr().out
    assert output.count("RAW-TOKEN-VALUE") == 1
    assert "Verified scope" in output


def test_dry_run_touches_no_database_and_says_it_did_not_validate(monkeypatch, capsys):
    def _no_engine(*args, **kwargs):
        raise AssertionError("dry run must not create a database engine")

    monkeypatch.setattr(cat, "generate_token", lambda: ("RAW-TOKEN-VALUE", "HASHVALUE"))
    monkeypatch.setattr(cat, "create_engine", _no_engine)
    monkeypatch.setattr(
        sys, "argv",
        ["create_agent_token.py", "--customer-id", "2", "--branch-id", "2",
         "--description", "test"],
    )

    cat.main()

    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "NOT validated" in output
