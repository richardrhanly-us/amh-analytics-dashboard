"""Tests for operational identity provisioning in tenant_service:
assign_operational_identity and build_collector_agent_config.

Identity model under test:
    organizations.id                      SaaS organization ID
    customers.id                          operational ingestion customer ID
    organizations.operational_customer_id -> customers.id
    branches.id                           SaaS branch row AND operational branch identity
    branches.operational_branch_id        == branches.id once provisioned

Collector/API scope is the operational pair. The SaaS organizations.id is never
a valid customer_id.

assign_operational_identity is exercised against _FakeIdentityDb, a small
stateful stand-in for the four tables involved. It dispatches on the SQL the
service issues and honours transactions (an exception rolls every change back),
so retry, idempotency and atomicity are tested across calls rather than only
per statement. IDs are deliberately chosen so the operational customer_id
(allocated from customers) never coincides with the SaaS organization id.
"""

from __future__ import annotations

import copy

import pytest
from db_fakes import FakeQueryResult

from src.services import tenant_service


class _FakeIdentityDb:
    def __init__(self, orgs, branches, customers, next_customer_id) -> None:
        self.orgs = {o["id"]: dict(o) for o in orgs}
        self.branches = {b["id"]: dict(b) for b in branches}
        self.customers = {c["id"]: dict(c) for c in customers}
        self.next_customer_id = next_customer_id
        self.calls: list[str] = []
        self.fail_on: str | None = None

    # engine interface used by the service
    def begin(self):
        return _FakeTx(self)

    def connect(self):
        return _FakeTx(self)

    # statement dispatch
    def execute(self, sql, params):
        q = " ".join(str(sql).split())
        self.calls.append(q)

        if self.fail_on and self.fail_on in q:
            self.fail_on = None  # fail once, so a retry can succeed
            raise RuntimeError("simulated database failure")

        if "FROM organizations" in q and "FOR UPDATE" in q:
            return FakeQueryResult(first=self.orgs.get(params["organization_id"]))

        if "FROM branches" in q and "FOR UPDATE" in q:
            branch = self.branches.get(params["branch_id"])
            if branch is None or branch["organization_id"] != params["organization_id"]:
                return FakeQueryResult(first=None)
            return FakeQueryResult(first=branch)

        if "FROM customers" in q:
            return FakeQueryResult(first=self.customers.get(params["customer_id"]))

        if "FROM organizations" in q and "id <> :organization_id" in q:
            for org in self.orgs.values():
                if (
                    org["operational_customer_id"] == params["customer_id"]
                    and org["id"] != params["organization_id"]
                ):
                    return FakeQueryResult(first={"id": org["id"]})
            return FakeQueryResult(first=None)

        if "INSERT INTO customers" in q:
            new_id = self.next_customer_id
            self.next_customer_id += 1
            self.customers[new_id] = {"id": new_id, "name": params["name"]}
            return FakeQueryResult(first={"id": new_id})

        if "UPDATE organizations" in q:
            org = self.orgs[params["organization_id"]]
            if org["operational_customer_id"] is None:
                org["operational_customer_id"] = params["customer_id"]
            return FakeQueryResult()

        if "UPDATE branches" in q:
            branch = self.branches[params["branch_id"]]
            if branch["operational_branch_id"] is None:
                branch["operational_branch_id"] = branch["id"]
            return FakeQueryResult()

        raise AssertionError(f"unexpected SQL: {q}")

    def writes(self):
        return [c for c in self.calls if c.startswith(("INSERT", "UPDATE"))]

    def snapshot(self):
        return copy.deepcopy((self.orgs, self.branches, self.customers, self.next_customer_id))

    def restore(self, snap):
        self.orgs, self.branches, self.customers, self.next_customer_id = snap


class _FakeTx:
    def __init__(self, db):
        self._db = db
        self._snap = None

    def __enter__(self):
        self._snap = self._db.snapshot()
        return self._db

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self._db.restore(self._snap)  # transaction rollback
        return False


def _org(org_id, name, customer_id=None):
    return {"id": org_id, "name": name, "operational_customer_id": customer_id}


def _branch(branch_id, org_id, operational_branch_id=None):
    return {"id": branch_id, "organization_id": org_id,
            "operational_branch_id": operational_branch_id}


def _clean_install_db():
    # Live clean-install test tenant: SaaS org 2 / branch 2, NULL bridges, no
    # customers row 2. NBPL (1/1 -> 1/1) is present alongside it. The next
    # customers id is 50 so an allocated customer never equals the SaaS id.
    return _FakeIdentityDb(
        orgs=[_org(1, "NBPL", customer_id=1), _org(2, "Clean Install Test")],
        branches=[_branch(1, 1, operational_branch_id=1), _branch(2, 2)],
        customers=[{"id": 1, "name": "NBPL"}],
        next_customer_id=50,
    )


@pytest.fixture
def db(monkeypatch):
    fake = _clean_install_db()
    monkeypatch.setattr(tenant_service, "get_engine", lambda: fake)
    return fake


# --- allocation and self-mapping ---------------------------------------------

def test_allocates_a_new_operational_customer_for_an_unmapped_tenant(db):
    result = tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert result["operational_customer_id"] == 50
    assert result["created_customer"] is True
    assert result["changed"] is True
    # A distinct customers row was created from the organization's name, and the
    # organization now points at it.
    assert db.customers[50] == {"id": 50, "name": "Clean Install Test"}
    assert db.orgs[2]["operational_customer_id"] == 50


def test_operational_customer_id_is_never_the_saas_organization_id(db):
    result = tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert result["organization_id"] == 2
    assert result["operational_customer_id"] != result["organization_id"]


def test_branch_is_mapped_to_its_own_id(db):
    result = tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert result["operational_branch_id"] == 2
    assert db.branches[2]["operational_branch_id"] == 2


def test_assignment_uses_row_locks_and_never_touches_tokens(db):
    tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert any("FROM organizations" in c and "FOR UPDATE" in c for c in db.calls)
    assert any("FROM branches" in c and "FOR UPDATE" in c for c in db.calls)
    assert not any("agent_tokens" in c for c in db.calls)


# --- idempotency and retry ---------------------------------------------------

def test_rerunning_an_assigned_tenant_is_idempotent(db):
    first = tenant_service.assign_operational_identity(organization_id=2, branch_id=2)
    customers_after_first = dict(db.customers)
    db.calls.clear()

    second = tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert second["operational_customer_id"] == first["operational_customer_id"]
    assert second["operational_branch_id"] == first["operational_branch_id"]
    assert second["created_customer"] is False
    assert second["changed"] is False
    assert db.customers == customers_after_first
    assert db.writes() == []


def test_retry_after_a_failed_run_creates_exactly_one_customer(db):
    # The branch update fails AFTER the customer was inserted and the org
    # updated. Because assignment is one transaction, everything rolls back...
    db.fail_on = "UPDATE branches"
    with pytest.raises(RuntimeError, match="simulated database failure"):
        tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert set(db.customers) == {1}
    assert db.orgs[2]["operational_customer_id"] is None
    assert db.branches[2]["operational_branch_id"] is None

    # ...so the retry allocates one customer, not two, and reuses no orphan.
    result = tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert set(db.customers) == {1, 50}
    assert result["operational_customer_id"] == 50
    assert db.orgs[2]["operational_customer_id"] == 50


def test_mapped_organization_with_a_new_unmapped_branch_only_maps_the_branch(db):
    db.branches[9] = _branch(9, 1)  # second NBPL branch, not yet mapped

    result = tenant_service.assign_operational_identity(organization_id=1, branch_id=9)

    assert result["operational_customer_id"] == 1
    assert result["operational_branch_id"] == 9
    assert result["created_customer"] is False
    assert result["changed"] is True
    assert set(db.customers) == {1}


# --- NBPL-style existing mapping ----------------------------------------------

def test_nbpl_style_existing_mapping_remains_valid_and_untouched(db):
    result = tenant_service.assign_operational_identity(organization_id=1, branch_id=1)

    assert result["operational_customer_id"] == 1
    assert result["operational_branch_id"] == 1
    assert result["created_customer"] is False
    assert result["changed"] is False
    assert db.writes() == []
    assert set(db.customers) == {1}


# --- fail-closed cases -------------------------------------------------------

def test_branch_of_another_organization_is_rejected_without_writes(db):
    # Branch 1 exists but belongs to org 1, not org 2.
    with pytest.raises(RuntimeError, match="Branch 1 not found for organization 2"):
        tenant_service.assign_operational_identity(organization_id=2, branch_id=1)

    assert db.writes() == []
    assert set(db.customers) == {1}
    assert db.orgs[2]["operational_customer_id"] is None


def test_unknown_organization_is_rejected(db):
    with pytest.raises(RuntimeError, match="Organization 999 not found"):
        tenant_service.assign_operational_identity(organization_id=999, branch_id=2)

    assert db.writes() == []


def test_unknown_branch_is_rejected(db):
    with pytest.raises(RuntimeError, match="Branch 999 not found for organization 2"):
        tenant_service.assign_operational_identity(organization_id=2, branch_id=999)

    assert db.writes() == []


def test_branch_operational_id_that_is_not_its_own_id_is_rejected(db):
    db.branches[2]["operational_branch_id"] = 77

    with pytest.raises(RuntimeError, match="Inconsistent operational identity"):
        tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    # Failed closed BEFORE allocating anything.
    assert db.writes() == []
    assert set(db.customers) == {1}
    assert db.orgs[2]["operational_customer_id"] is None


def test_mapping_to_a_nonexistent_customer_is_rejected(db):
    db.orgs[2]["operational_customer_id"] = 42  # dangling: no customers row 42

    with pytest.raises(RuntimeError, match="customer 42, which does not exist"):
        tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert db.writes() == []
    assert set(db.customers) == {1}


def test_operational_customer_already_mapped_to_another_organization_is_refused(db):
    # Both organizations claim customer 1. Neither may proceed (the DB's
    # partial unique index forbids this state; the service also fails closed).
    db.orgs[2]["operational_customer_id"] = 1

    with pytest.raises(RuntimeError, match="already mapped to organization 1"):
        tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert db.writes() == []
    with pytest.raises(RuntimeError, match="already mapped to organization 2"):
        tenant_service.assign_operational_identity(organization_id=1, branch_id=1)


def test_a_new_allocation_never_reuses_another_organizations_customer(db):
    tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    assert db.orgs[2]["operational_customer_id"] != db.orgs[1]["operational_customer_id"]


# --- Collector config generation ---------------------------------------------

_LEGACY_CONFIG_KEYS = {
    "database_url", "customer_id", "branch_id", "raw_checkins_file", "raw_rejects_file",
    "processed_checkins_file", "processed_rejects_file", "checkins_history_file",
    "rejects_history_file", "status_file", "api_url", "raw_acs_file",
    "processed_acs_file", "acs_history_file",
}


def test_mapped_tenant_config_uses_operational_ids_not_saas_ids():
    # SaaS org 2 / branch 2 mapped to operational customer 50 / branch 2.
    config = tenant_service.build_collector_agent_config(
        operational_customer_id=50,
        operational_branch_id=2,
        api_url="https://api.example.test/",
    )

    assert config["customer_id"] == 50
    assert config["branch_id"] == 2
    assert config["api_url"] == "https://api.example.test"


def test_config_schema_is_unchanged_and_never_carries_a_database_url():
    config = tenant_service.build_collector_agent_config(
        operational_customer_id=1, operational_branch_id=1, api_url="https://x.test",
    )

    assert set(config) == _LEGACY_CONFIG_KEYS
    assert config["database_url"] == ""


def test_config_generation_end_to_end_after_assignment_uses_the_mapped_pair(db):
    identity = tenant_service.assign_operational_identity(organization_id=2, branch_id=2)

    config = tenant_service.build_collector_agent_config(
        operational_customer_id=identity["operational_customer_id"],
        operational_branch_id=identity["operational_branch_id"],
        api_url="https://x.test",
    )

    assert (config["customer_id"], config["branch_id"]) == (50, 2)
    assert config["customer_id"] != 2  # the SaaS organization id


@pytest.mark.parametrize(
    ("customer_id", "branch_id"),
    [(None, None), (None, 2), (50, None)],
)
def test_unmapped_tenant_cannot_produce_a_collector_config(customer_id, branch_id):
    with pytest.raises(ValueError, match="Operational identity is not assigned"):
        tenant_service.build_collector_agent_config(
            operational_customer_id=customer_id,
            operational_branch_id=branch_id,
            api_url="https://x.test",
        )


def test_clean_install_style_saas_ids_with_null_bridges_are_not_provisioned(db):
    # SaaS 2/2 with NULL bridges: the ids exist in the SaaS domain, and (per
    # live data) historical rows sit at 2/2, but that is NOT an operational
    # mapping. No config may be built from it, and assignment must allocate a
    # genuine operational customer rather than adopting 2.
    org = db.orgs[2]
    branch = db.branches[2]
    assert org["operational_customer_id"] is None
    assert branch["operational_branch_id"] is None

    with pytest.raises(ValueError, match="not assigned"):
        tenant_service.build_collector_agent_config(
            operational_customer_id=org["operational_customer_id"],
            operational_branch_id=branch["operational_branch_id"],
            api_url="https://x.test",
        )

    assigned = tenant_service.assign_operational_identity(organization_id=2, branch_id=2)
    assert assigned["operational_customer_id"] == 50
    assert 2 not in db.customers
