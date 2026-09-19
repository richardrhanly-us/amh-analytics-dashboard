"""Tests for the collector_installations service functions in tenant_service
and for the migration that creates the table.
"""

import os

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from db_fakes import FakeEngine, FakeQueryResult

from src.services import tenant_service

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _installation_row(**overrides):
    row = {
        "id": 7,
        "organization_id": 1,
        "branch_id": 10,
        "name": "Main AMH Sorter",
        "hostname": "AMH-PC",
        "collector_version": "1.0.2",
        "status": "provisioning",
        "installed_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


# --- create_collector_installation ------------------------------------------

def test_create_collector_installation_happy_path(monkeypatch):
    inserted = _installation_row()
    engine = FakeEngine([
        FakeQueryResult(first={"id": 10}),  # branch belongs to organization
        FakeQueryResult(first=inserted),    # INSERT ... RETURNING
    ])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.create_collector_installation(
        organization_id=1,
        branch_id=10,
        name="  Main AMH Sorter  ",
        hostname=" AMH-PC ",
        collector_version="1.0.2",
    )

    assert result == inserted
    assert "INSERT INTO collector_installations" in engine.calls[1]["sql"]
    assert engine.calls[1]["params"] == {
        "organization_id": 1,
        "branch_id": 10,
        "name": "Main AMH Sorter",
        "hostname": "AMH-PC",
        "collector_version": "1.0.2",
        "status": "provisioning",
    }


def test_create_collector_installation_stores_blank_optionals_as_null(monkeypatch):
    engine = FakeEngine([
        FakeQueryResult(first={"id": 10}),
        FakeQueryResult(first=_installation_row(hostname=None, collector_version=None)),
    ])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    tenant_service.create_collector_installation(
        organization_id=1, branch_id=10, name="Sorter", hostname="   ", collector_version="",
    )

    assert engine.calls[1]["params"]["hostname"] is None
    assert engine.calls[1]["params"]["collector_version"] is None


def test_create_collector_installation_rejects_branch_from_another_organization(monkeypatch):
    # Branch lookup filters on BOTH id and organization_id, so a branch that
    # exists under a different org finds nothing and no INSERT is attempted.
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="Branch 10 not found for organization 2"):
        tenant_service.create_collector_installation(
            organization_id=2, branch_id=10, name="Sorter",
        )

    assert len(engine.calls) == 1
    assert engine.calls[0]["params"] == {"branch_id": 10, "organization_id": 2}


def test_create_collector_installation_requires_a_name(monkeypatch):
    engine = FakeEngine([])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(ValueError, match="name is required"):
        tenant_service.create_collector_installation(
            organization_id=1, branch_id=10, name="   ",
        )

    assert engine.calls == []


def test_create_collector_installation_rejects_unknown_status(monkeypatch):
    engine = FakeEngine([])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(ValueError, match="Invalid installation status"):
        tenant_service.create_collector_installation(
            organization_id=1, branch_id=10, name="Sorter", status="exploded",
        )

    assert engine.calls == []


# --- list_collector_installations_* -----------------------------------------

def test_list_collector_installations_for_branch(monkeypatch):
    rows = [_installation_row(id=7), _installation_row(id=8, name="Second Sorter")]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.list_collector_installations_for_branch(10)

    assert result == rows
    assert engine.calls[0]["params"] == {"branch_id": 10}
    assert "WHERE ci.branch_id = :branch_id" in engine.calls[0]["sql"]


def test_list_collector_installations_for_branch_empty(monkeypatch):
    engine = FakeEngine([FakeQueryResult(all_rows=[])])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    assert tenant_service.list_collector_installations_for_branch(10) == []


def test_list_collector_installations_for_organization(monkeypatch):
    rows = [_installation_row(id=7, branch_id=10), _installation_row(id=9, branch_id=11)]
    engine = FakeEngine([FakeQueryResult(all_rows=rows)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.list_collector_installations_for_organization(1)

    assert result == rows
    assert engine.calls[0]["params"] == {"organization_id": 1}
    assert "WHERE ci.organization_id = :organization_id" in engine.calls[0]["sql"]


# --- update_collector_installation ------------------------------------------

def test_update_collector_installation_happy_path(monkeypatch):
    updated = _installation_row(name="Renamed", hostname=None, status="active")
    engine = FakeEngine([FakeQueryResult(first=updated)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.update_collector_installation(
        installation_id=7,
        organization_id=1,
        name=" Renamed ",
        hostname="",
        collector_version="1.0.2",
        status="active",
    )

    assert result == updated
    assert engine.calls[0]["params"] == {
        "installation_id": 7,
        "organization_id": 1,
        "name": "Renamed",
        "hostname": None,
        "collector_version": "1.0.2",
        "status": "active",
    }
    # Scoped to the organization, and never writes last_seen_at.
    assert "organization_id = :organization_id" in engine.calls[0]["sql"]
    assert "last_seen_at =" not in engine.calls[0]["sql"]


def test_update_collector_installation_raises_when_not_found_for_organization(monkeypatch):
    engine = FakeEngine([FakeQueryResult(first=None)])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="Collector installation 7 not found for organization 2"):
        tenant_service.update_collector_installation(
            installation_id=7,
            organization_id=2,
            name="Sorter",
            hostname=None,
            collector_version=None,
            status="active",
        )


def test_update_collector_installation_rejects_unknown_status(monkeypatch):
    engine = FakeEngine([])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    with pytest.raises(ValueError, match="Invalid installation status"):
        tenant_service.update_collector_installation(
            installation_id=7,
            organization_id=1,
            name="Sorter",
            hostname=None,
            collector_version=None,
            status="decommissioned",
        )

    assert engine.calls == []


@pytest.mark.parametrize("status", ["provisioning", "active", "inactive", "retired"])
def test_update_collector_installation_accepts_every_documented_status(monkeypatch, status):
    engine = FakeEngine([FakeQueryResult(first=_installation_row(status=status))])
    monkeypatch.setattr(tenant_service, "get_engine", lambda: engine)

    result = tenant_service.update_collector_installation(
        installation_id=7, organization_id=1, name="Sorter",
        hostname=None, collector_version=None, status=status,
    )

    assert result["status"] == status


# --- migration ---------------------------------------------------------------

def _script_directory():
    cfg = Config(os.path.join(ROOT_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_collector_installations_migration_chains_from_previous_head():
    script = _script_directory()

    revision = script.get_revision("fb984ee6c56c")

    assert revision is not None
    assert revision.down_revision == "c53c1b536c71"
    assert script.get_heads() == ["fb984ee6c56c"]


def test_collector_installations_migration_has_no_secret_or_agent_token_columns():
    # Guards the "bookkeeping only" contract: this table must not grow
    # credential columns or a link into agent_tokens.
    script = _script_directory()
    revision = script.get_revision("fb984ee6c56c")
    assert revision is not None
    with open(revision.path, encoding="utf-8") as f:
        source = f.read()

    for forbidden in ("token", "password", "database_url", "ip_address", "username"):
        assert f'sa.Column("{forbidden}' not in source
    assert "agent_tokens" not in source.split('"""', 2)[2]
