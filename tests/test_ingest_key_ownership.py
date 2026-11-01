"""C6a M1: an ingest key's optional owning installation, through the real /v2/upload and /v2/status, and the migration rendered
as offline PostgreSQL SQL (no database is touched).

The endpoints run on the SQLite harness of tests/test_ingest_v2_api.py. What needs a real server (the composite foreign key,
upgrade and downgrade, row level security, the runtime role) is in tests/test_ingest_key_ownership_postgres.py.

Every value is SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import logging
import os
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from test_ingest_v2_api import (  # noqa: F401 -- the autouse limiter reset and the database are fixtures this module reuses
    BOUND_TOKEN,
    BRANCH,
    CUSTOMER,
    KEY,
    OTHER_TENANT_KEY,
    RETIRED_KEY,
    TOKEN,
    UNKNOWN_KEY,
    _reset_rate_limiter,
    client,
    db,
    status,
    upload,
    v1_payload,
)

import main
from scripts import runtime_role_privileges as privileges

ROOT = Path(__file__).resolve().parent.parent
REVISION = "c1e7a94f2b60"
PREVIOUS_HEAD = "f7b3d2c81a46"

OWNER, OTHER_INSTALLATION = 7, 8  # both at this tenant's branch; Db.bind_installation makes 7 and BOUND_TOKEN
OTHER_BOUND_TOKEN = "CANARY-V2-OTHER-INSTALLATION-TOKEN-3004"
KEY_REFUSAL = {"detail": main.V2_KEY_NOT_AUTHORIZED_DETAIL}
AGENT_REFUSAL = {"detail": "Agent is not currently authorized to upload data"}

ENDPOINTS = [pytest.param("/v2/upload", upload, id="upload"), pytest.param("/v2/status", status, id="status")]


def _insert_token(conn, token, installation_id):
    conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id)"
                      " VALUES (:h, :c, :b, 'test token', 1, :i)"),
                 {"h": hashlib.sha256(token.encode("utf-8")).hexdigest(), "c": CUSTOMER, "b": BRANCH, "i": installation_id})


@pytest.fixture
def site(db):  # noqa: F811 -- the imported fixture, requested by name
    """Installation 7 (BOUND_TOKEN) and installation 8 (OTHER_BOUND_TOKEN) at one branch, plus the legacy unbound TOKEN."""
    db.bind_installation("active")
    with db.engine.begin() as conn:
        conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, status) VALUES (:i, 1, 1, 'active')"),
                     {"i": OTHER_INSTALLATION})
        _insert_token(conn, OTHER_BOUND_TOKEN, OTHER_INSTALLATION)
    return db


def _own(site, key, installation_id=OWNER):
    with site.engine.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET installation_id = :i WHERE key_id = :k"), {"i": installation_id, "k": key})


def _post(path, body, token):
    return client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


def _everything(site) -> dict:
    """Every table a v2 request can write."""
    tables = ("checkin_events", "reject_events", "acs_item_events", "ingest_key_ids", "agent_tokens", "collector_installations",
              "branches", "checkins", "rejects", "acs_events", "pipeline_status")
    return {table: site.rows(table) for table in tables}


def _assert_accepted(site, path, key, response):
    assert response.status_code == 200, response.text
    if path == "/v2/upload":
        assert response.json()["checkins_inserted"] == 1
        assert site.count("checkin_events") == 1
    else:
        assert site.rows("ingest_key_ids", "last_heartbeat_at", f"WHERE key_id = '{key}'")[0][0] is not None


# =====================================================================================================================
# a key with no owner behaves exactly as it did
# =====================================================================================================================

@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
@pytest.mark.parametrize("token", [pytest.param(TOKEN, id="unbound"), pytest.param(BOUND_TOKEN, id="bound")])
def test_a_key_with_no_owner_accepts_an_unbound_and_a_bound_token(site, path, body, token):
    _assert_accepted(site, path, KEY, _post(path, body(KEY), token))


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
def test_a_retired_key_with_no_owner_is_still_refused(site, path, body):
    response = _post(path, body(RETIRED_KEY), TOKEN)

    assert (response.status_code, response.json()) == (403, KEY_REFUSAL)


# =====================================================================================================================
# a key with an owner accepts only a token bound to that owner
# =====================================================================================================================

@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
def test_the_owners_token_is_accepted(site, path, body):
    _own(site, KEY)

    _assert_accepted(site, path, KEY, _post(path, body(KEY), BOUND_TOKEN))


REFUSED_FOR_THE_OWNED_KEY = [pytest.param(OTHER_BOUND_TOKEN, id="another-installation"), pytest.param(TOKEN, id="unbound")]


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
@pytest.mark.parametrize("token", REFUSED_FOR_THE_OWNED_KEY)
def test_any_other_token_of_the_branch_is_refused_and_nothing_is_written(site, path, body, token):
    _own(site, KEY)
    before = _everything(site)

    response = _post(path, body(KEY), token)

    assert (response.status_code, response.json()) == (403, KEY_REFUSAL)
    assert _everything(site) == before


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
@pytest.mark.parametrize("installation_status", ["inactive", "retired"])
def test_the_owners_token_stops_with_its_installation_and_nothing_is_written(site, path, body, installation_status):
    _own(site, KEY)
    with site.engine.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = :s WHERE id = :i"), {"s": installation_status, "i": OWNER})
    before = _everything(site)

    response = _post(path, body(KEY), BOUND_TOKEN)

    assert (response.status_code, response.json()) == (403, AGENT_REFUSAL)
    assert _everything(site) == before


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
def test_the_owners_inactive_token_is_refused_and_nothing_is_written(site, path, body):
    _own(site, KEY)
    with site.engine.begin() as conn:
        conn.execute(text("UPDATE agent_tokens SET is_active = 0 WHERE installation_id = :i"), {"i": OWNER})
    before = _everything(site)

    response = _post(path, body(KEY), BOUND_TOKEN)

    assert (response.status_code, response.json()) == (403, {"detail": "Agent token is inactive"})
    assert _everything(site) == before


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
def test_a_retired_owned_key_is_refused_even_for_its_owner(site, path, body):
    _own(site, RETIRED_KEY)
    before = _everything(site)

    response = _post(path, body(RETIRED_KEY), BOUND_TOKEN)

    assert (response.status_code, response.json()) == (403, KEY_REFUSAL)
    assert _everything(site) == before


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
@pytest.mark.parametrize("owner", [pytest.param(None, id="no-owner"), pytest.param(9, id="owned")])
@pytest.mark.parametrize("token", [pytest.param(TOKEN, id="unbound"), pytest.param(BOUND_TOKEN, id="bound")])
def test_another_tenants_key_is_refused_whoever_owns_it(site, path, body, owner, token):
    _own(site, OTHER_TENANT_KEY, owner)
    before = _everything(site)

    response = _post(path, body(OTHER_TENANT_KEY), token)

    assert (response.status_code, response.json()) == (403, KEY_REFUSAL)
    assert _everything(site) == before


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
def test_an_owner_on_another_tenants_key_does_not_admit_a_token_bound_to_that_owner(site, path, body):
    # Even a key wrongly naming this tenant's installation stays that other tenant's: the tenant check comes first.
    _own(site, OTHER_TENANT_KEY, OWNER)

    response = _post(path, body(OTHER_TENANT_KEY), BOUND_TOKEN)

    assert (response.status_code, response.json()) == (403, KEY_REFUSAL)


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
def test_every_key_refusal_is_the_same_answer_and_only_the_log_says_why(site, path, body, caplog):
    _own(site, KEY)
    _own(site, RETIRED_KEY)

    with caplog.at_level(logging.WARNING):
        wrong_owner = _post(path, body(KEY), OTHER_BOUND_TOKEN)
    answers = [wrong_owner, _post(path, body(KEY), TOKEN), _post(path, body(RETIRED_KEY), BOUND_TOKEN),
               _post(path, body(UNKNOWN_KEY), BOUND_TOKEN), _post(path, body(OTHER_TENANT_KEY), BOUND_TOKEN)]

    assert {(answer.status_code, answer.text) for answer in answers} == {(403, wrong_owner.text)}
    assert "key is owned by another installation" in caplog.text
    assert "owned" not in wrong_owner.text and OTHER_BOUND_TOKEN not in caplog.text


def test_a_provisioning_owners_heartbeat_still_makes_it_active(site):
    _own(site, KEY)
    with site.engine.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = 'provisioning' WHERE id = :i"), {"i": OWNER})

    assert _post("/v2/status", status(KEY), BOUND_TOKEN).status_code == 200

    assert site.installation()[0] == "active"


def test_another_key_of_the_branch_is_not_affected_by_one_being_owned(site):
    _own(site, KEY)
    second = "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"

    assert _post("/v2/upload", upload(second), TOKEN).status_code == 200


# =====================================================================================================================
# one enforcement point, and Contract v1 is outside it
# =====================================================================================================================

def test_both_v2_endpoints_use_the_one_key_check_and_nothing_else_does():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    upload_route, status_route = source.split('@v2_status_router.post("/v2/status")')

    assert source.count("ingest_key_problem(") == 1
    assert source.count("_require_ingest_key(conn, token_row, data.key_id)") == 2
    assert upload_route.count("_require_ingest_key(conn, token_row, data.key_id)") == 1
    assert status_route.count("_require_ingest_key(conn, token_row, data.key_id)") == 1


def test_a_v1_upload_is_not_affected_by_the_branchs_key_having_an_owner(site):
    _own(site, KEY)

    response = client.post("/upload", json=v1_payload(), headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200 and response.json()["checkins_inserted"] == 1


# =====================================================================================================================
# the migration, rendered offline
# =====================================================================================================================

def _script_directory() -> ScriptDirectory:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _module():
    revision = _script_directory().get_revision(REVISION)
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", revision.path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(function_name: str) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(_module(), function_name)()
    return " ".join(buffer.getvalue().lower().split())


def test_the_migration_extends_the_one_linear_history():
    script = _script_directory()

    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    assert len(script.get_heads()) == 1
    assert [r.revision for r in script.walk_revisions() if r.down_revision == PREVIOUS_HEAD] == [REVISION]


def test_it_adds_the_nullable_column_and_the_composite_foreign_key_and_writes_no_row():
    sql = _render("upgrade")

    assert "alter table ingest_key_ids add column installation_id bigint;" in sql
    assert "add constraint uq_collector_installations_id_branch_id unique (id, branch_id)" in sql
    assert ("add constraint fk_ingest_key_ids_installation_branch foreign key (installation_id, branch_id) "
            "references collector_installations (id, branch_id)") in sql
    # The unique constraint comes first: the foreign key needs it.
    assert sql.index("uq_collector_installations_id_branch_id") < sql.index("fk_ingest_key_ids_installation_branch")
    for forbidden in ("update ", "insert into", "delete from", "truncate", "create index", "create unique", "on delete", "on update",
                      "cascade", "not null", "default", "grant ", "revoke ", "create policy", "row level security", "create trigger",
                      "agent_tokens", "branches"):
        assert forbidden not in sql, forbidden


def test_both_directions_are_bounded_by_a_transaction_local_lock_timeout():
    for direction in ("upgrade", "downgrade"):
        sql = _render(direction)
        assert sql.startswith("set local lock_timeout = '5s';"), direction


def test_the_downgrade_removes_exactly_what_was_added():
    sql = _render("downgrade")

    assert "alter table ingest_key_ids drop constraint fk_ingest_key_ids_installation_branch" in sql
    assert "alter table ingest_key_ids drop column installation_id" in sql
    assert "alter table collector_installations drop constraint uq_collector_installations_id_branch_id" in sql
    assert sql.index("fk_ingest_key_ids_installation_branch") < sql.index("uq_collector_installations_id_branch_id")
    assert sql.count("drop ") == 3
    for forbidden in ("cascade", "delete from", "update ", "truncate", "drop table"):
        assert forbidden not in sql, forbidden


def test_the_migration_says_what_it_means_and_in_what_order_to_deploy():
    docstring = " ".join((_module().__doc__ or "").split())

    assert "NO BACKFILL." in docstring and "NO GRANT, NO POLICY." in docstring and "NO OTHER INDEX." in docstring
    assert "apply this migration" in docstring and "Step 2 must never come first" in docstring
    assert "lock_timeout" in docstring


def test_the_runtime_role_needs_no_new_privilege():
    assert privileges.BASELINE["ingest_key_ids"] == {"SELECT", "INSERT", "UPDATE"} and "ingest_key_ids" in privileges.RLS_TABLES
    assert privileges.BASELINE["collector_installations"] == {"SELECT", "INSERT", "UPDATE"}
