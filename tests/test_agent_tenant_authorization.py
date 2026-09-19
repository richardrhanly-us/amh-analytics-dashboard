"""Tests for tenant-state enforcement in main.authenticate_agent.

A request may ingest only when:
    token valid AND token active AND request scope matches the token
    AND organization status in (active, trial) AND branch status = active

Token state and tenant state are independent gates. A valid, active token whose
tenant is suspended/cancelled (or whose branch is inactive) is AUTHENTICATED
but NOT AUTHORIZED: 403, not the 401 used for an unknown token. The 403 body is
generic and never reveals why (the reason is logged server-side only).

The tenant is resolved through the OPERATIONAL bridge -- organizations.
operational_customer_id = agent_tokens.customer_id and branches.
operational_branch_id = agent_tokens.branch_id, with the branch required to
belong to that organization -- never through the SaaS organizations.id /
branches.id.

Three layers:
  * authenticate_agent against fake token rows (gate ordering, status codes);
  * the real lookup SQL run against SQLite (join semantics: bridge, mismatch);
  * the HTTP endpoints via TestClient (nothing is written on rejection).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import main

GENERIC_DETAIL = "Agent is not currently authorized to upload data"


def _row(*, is_active=True, organization_status="active", branch_status="active",
         customer_id=1, branch_id=1):
    return {
        "id": 7, "customer_id": customer_id, "branch_id": branch_id,
        "is_active": is_active, "description": "test",
        "organization_status": organization_status, "branch_status": branch_status,
    }


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Conn:
    def __init__(self, token_row) -> None:
        self.token_row = token_row
        self.executed: list[str] = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append(sql)
        return _Result(self.token_row if "FROM agent_tokens" in sql else None)

    def used_marked(self):
        return any("UPDATE agent_tokens" in s for s in self.executed)


def _authenticate(row, customer_id=1, branch_id=1):
    conn = _Conn(row)
    result = main.authenticate_agent(conn, "Bearer some-token", customer_id, branch_id)
    return result, conn


def _rejection(row, customer_id=1, branch_id=1):
    conn = _Conn(row)
    with pytest.raises(HTTPException) as excinfo:
        main.authenticate_agent(conn, "Bearer some-token", customer_id, branch_id)
    return excinfo.value, conn


# --- accepted -----------------------------------------------------------------

@pytest.mark.parametrize("organization_status", ["active", "trial"])
def test_usable_tenant_is_accepted_and_marks_the_token_used(organization_status):
    _, conn = _authenticate(_row(organization_status=organization_status))

    assert conn.used_marked()


def test_nbpl_style_production_mapping_authenticates():
    # NBPL: operational customer 1 / branch 1, both active.
    result, _ = _authenticate(_row(customer_id=1, branch_id=1), customer_id=1, branch_id=1)

    assert (result["customer_id"], result["branch_id"]) == (1, 1)


# --- rejected: authenticated but not authorized (403) --------------------------

@pytest.mark.parametrize("organization_status", ["suspended", "cancelled"])
def test_active_token_for_a_suspended_or_cancelled_organization_is_rejected(organization_status):
    error, conn = _rejection(_row(organization_status=organization_status))

    assert error.status_code == 403
    assert error.detail == GENERIC_DETAIL
    assert not conn.used_marked()


def test_active_token_for_an_inactive_branch_is_rejected():
    error, conn = _rejection(_row(branch_status="inactive"))

    assert error.status_code == 403
    assert error.detail == GENERIC_DETAIL
    assert not conn.used_marked()


@pytest.mark.parametrize(
    ("organization_status", "branch_status"),
    [(None, None), ("active", None), (None, "active")],
)
def test_unmapped_or_mismatched_tenant_is_rejected_fail_closed(organization_status, branch_status):
    # NULL statuses come from the LEFT JOINs finding no organization for the
    # token's customer_id, or no branch of THAT organization for its branch_id.
    error, _ = _rejection(_row(organization_status=organization_status,
                               branch_status=branch_status))

    assert error.status_code == 403
    assert error.detail == GENERIC_DETAIL


def test_token_row_without_tenant_columns_is_rejected_not_crashed():
    row = _row()
    del row["organization_status"]
    del row["branch_status"]

    error, _ = _rejection(row)

    assert error.status_code == 403


@pytest.mark.parametrize("word", ["suspend", "cancel", "inactive", "trial", "branch", "organization"])
def test_tenant_rejection_reveals_nothing_about_tenant_state(word):
    for row in (_row(organization_status="suspended"), _row(branch_status="inactive")):
        error, _ = _rejection(row)
        assert word not in str(error.detail).lower()


# --- gate ordering / independence ----------------------------------------------

def test_unknown_token_is_still_401():
    error, _ = _rejection(None)

    assert error.status_code == 401
    assert error.detail == "Invalid agent token"


@pytest.mark.parametrize("organization_status", ["active", "trial", "suspended", "cancelled"])
def test_inactive_token_is_rejected_whatever_the_tenant_state(organization_status):
    # Reactivating a library must never make a deactivated token work.
    error, conn = _rejection(_row(is_active=False, organization_status=organization_status))

    assert error.status_code == 403
    assert error.detail == "Agent token is inactive"
    assert not conn.used_marked()


def test_scope_mismatch_is_still_reported_as_a_scope_error():
    error, _ = _rejection(_row(customer_id=1, branch_id=1), customer_id=2, branch_id=1)

    assert error.status_code == 403
    assert error.detail == "Token scope does not match customer_id / branch_id"


def test_scope_mismatch_takes_precedence_over_tenant_state():
    error, _ = _rejection(
        _row(organization_status="suspended", customer_id=1, branch_id=1),
        customer_id=1, branch_id=99,
    )

    assert error.detail == "Token scope does not match customer_id / branch_id"


# --- the real lookup SQL: join semantics against SQLite --------------------------
#
# The only Postgres-specific part of the query is the token hash expression;
# it is swapped for a plain comparison so the JOIN logic runs unmodified.

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"


@pytest.fixture
def lookup_db():
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as conn:
        for ddl in (
            """CREATE TABLE organizations (
                id INTEGER PRIMARY KEY, status TEXT, operational_customer_id INTEGER)""",
            """CREATE TABLE branches (
                id INTEGER PRIMARY KEY, organization_id INTEGER, status TEXT,
                operational_branch_id INTEGER)""",
            """CREATE TABLE agent_tokens (
                id INTEGER PRIMARY KEY, token_hash TEXT, customer_id INTEGER,
                branch_id INTEGER, is_active BOOLEAN, description TEXT)""",
        ):
            conn.execute(text(ddl))
    return engine


def _seed(engine, *, orgs=(), branches=(), tokens=()):
    with engine.begin() as conn:
        for org_id, status, op_customer in orgs:
            conn.execute(text("INSERT INTO organizations VALUES (:i, :s, :c)"),
                         {"i": org_id, "s": status, "c": op_customer})
        for branch_id, org_id, status, op_branch in branches:
            conn.execute(text("INSERT INTO branches VALUES (:i, :o, :s, :b)"),
                         {"i": branch_id, "o": org_id, "s": status, "b": op_branch})
        for token, customer_id, branch_id in tokens:
            conn.execute(
                text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, is_active, "
                     "description) VALUES (:t, :c, :b, 1, 'x')"),
                {"t": token, "c": customer_id, "b": branch_id},
            )


def _lookup(engine, token):
    sql = main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, ":token")
    with engine.connect() as conn:
        return conn.execute(text(sql), {"token": token}).mappings().first()


def test_lookup_nbpl_style_mapping_is_usable(lookup_db):
    _seed(lookup_db, orgs=[(1, "active", 1)], branches=[(1, 1, "active", 1)],
          tokens=[("nbpl", 1, 1)])

    row = _lookup(lookup_db, "nbpl")

    assert (row["organization_status"], row["branch_status"]) == ("active", "active")
    assert main.tenant_unusable_reason(row) is None


def test_lookup_resolves_through_operational_ids_not_saas_ids(lookup_db):
    # SaaS org 2 / branch 2 <-> operational customer 50 / branch 2.
    _seed(lookup_db, orgs=[(2, "active", 50)], branches=[(2, 2, "active", 2)],
          tokens=[("mapped", 50, 2), ("saas-ids", 2, 2)])

    mapped = _lookup(lookup_db, "mapped")
    assert main.tenant_unusable_reason(mapped) is None

    # A (historical) token at the SaaS ids 2/2 must NOT be treated as this
    # tenant: customer_id 2 is not any organization's operational customer.
    saas = _lookup(lookup_db, "saas-ids")
    assert saas["organization_status"] is None
    assert main.tenant_unusable_reason(saas) == "no mapped organization"


def test_lookup_unmapped_tenant_with_null_bridges_is_unusable(lookup_db):
    _seed(lookup_db, orgs=[(2, "active", None)], branches=[(2, 2, "active", None)],
          tokens=[("clean-install", 2, 2)])

    row = _lookup(lookup_db, "clean-install")

    assert main.tenant_unusable_reason(row) == "no mapped organization"


@pytest.mark.parametrize("status", ["suspended", "cancelled"])
def test_lookup_suspended_or_cancelled_organization_is_unusable(lookup_db, status):
    _seed(lookup_db, orgs=[(1, status, 1)], branches=[(1, 1, "active", 1)],
          tokens=[("t", 1, 1)])

    assert "organization status" in main.tenant_unusable_reason(_lookup(lookup_db, "t"))


def test_lookup_trial_organization_with_active_branch_is_usable(lookup_db):
    _seed(lookup_db, orgs=[(1, "trial", 1)], branches=[(1, 1, "active", 1)],
          tokens=[("t", 1, 1)])

    assert main.tenant_unusable_reason(_lookup(lookup_db, "t")) is None


def test_lookup_inactive_branch_is_unusable_even_when_organization_is_active(lookup_db):
    _seed(lookup_db, orgs=[(1, "active", 1)], branches=[(1, 1, "inactive", 1)],
          tokens=[("t", 1, 1)])

    assert "branch status" in main.tenant_unusable_reason(_lookup(lookup_db, "t"))


def test_lookup_branch_belonging_to_another_organization_is_unusable(lookup_db):
    # Token pairs customer 1 (org 1) with branch 5, which belongs to org 2 and
    # is itself active/mapped: the pair spans two tenants and must be rejected.
    _seed(
        lookup_db,
        orgs=[(1, "active", 1), (2, "active", 2)],
        branches=[(1, 1, "active", 1), (5, 2, "active", 5)],
        tokens=[("cross", 1, 5)],
    )

    row = _lookup(lookup_db, "cross")

    assert row["organization_status"] == "active"
    assert row["branch_status"] is None
    assert main.tenant_unusable_reason(row) == "no mapped branch for the organization"


def test_lookup_unknown_token_finds_no_row(lookup_db):
    _seed(lookup_db, orgs=[(1, "active", 1)], branches=[(1, 1, "active", 1)],
          tokens=[("t", 1, 1)])

    assert _lookup(lookup_db, "nope") is None


def test_lookup_sql_uses_the_operational_bridge():
    sql = " ".join(main._AGENT_TOKEN_LOOKUP_SQL.split())

    assert "o.operational_customer_id = t.customer_id" in sql
    assert "b.operational_branch_id = t.branch_id" in sql
    assert "b.organization_id = o.id" in sql
    assert "o.id = t.customer_id" not in sql
    assert "b.id = t.branch_id" not in sql


# --- HTTP endpoints ----------------------------------------------------------------

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


class _EndpointConn:
    def __init__(self, token_row) -> None:
        self.token_row = token_row
        self.executed: list[str] = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append(sql)
        if "FROM agent_tokens" in sql:
            return _Result(self.token_row)

        class _Written:
            rowcount = 1

        return _Written()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _EndpointEngine:
    def __init__(self, token_row) -> None:
        self.conn = _EndpointConn(token_row)

    def begin(self):
        return self.conn


_UPLOAD = {"checkins": [{"customer_id": 1, "branch_id": 1,
                         "event_time": "2026-09-19T09:00:00", "barcode": "1"}]}
_STATUS = {"customer_id": 1, "branch_id": 1, "status": "completed"}
_AUTH = {"Authorization": "Bearer good-token"}


def _inserts(engine):
    return [s for s in engine.conn.executed if "INSERT INTO" in s]


@pytest.mark.parametrize(
    ("path", "payload"),
    [("/upload", _UPLOAD), ("/upload-pipeline-status", _STATUS)],
)
def test_suspended_tenant_is_rejected_with_403_and_nothing_is_written(monkeypatch, path, payload):
    engine = _EndpointEngine(_row(organization_status="suspended"))
    monkeypatch.setattr(main, "engine", engine)

    response = client.post(path, json=payload, headers=_AUTH)

    assert response.status_code == 403
    assert response.json()["detail"] == GENERIC_DETAIL
    assert _inserts(engine) == []


@pytest.mark.parametrize(
    ("path", "payload"),
    [("/upload", _UPLOAD), ("/upload-pipeline-status", _STATUS)],
)
def test_usable_tenant_can_still_ingest(monkeypatch, path, payload):
    engine = _EndpointEngine(_row(organization_status="trial"))
    monkeypatch.setattr(main, "engine", engine)

    response = client.post(path, json=payload, headers=_AUTH)

    assert response.status_code == 200
    assert _inserts(engine) != []


def test_unknown_token_is_401_at_the_endpoint(monkeypatch):
    monkeypatch.setattr(main, "engine", _EndpointEngine(None))

    response = client.post("/upload", json=_UPLOAD, headers=_AUTH)

    assert response.status_code == 401
