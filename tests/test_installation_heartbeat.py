"""Tests for linking a Collector heartbeat to its collector_installations row.

POST /upload-pipeline-status may carry an explicit installation_id (plus the
running Collector's version). After the normal token/scope/tenant gates, that
installation must be EXACTLY the row with that id AND belong to the tenant the
token resolves to through the OPERATIONAL bridge. A matching provisioning or
active installation records the heartbeat (provisioning -> active,
installed_at stamped once at the first such contact, last_seen_at + collector_version
updated); a preflight probe counts as a confirmed contact exactly like a scheduled
run's heartbeat; anything
else -- unknown, another organization's, another branch's, inactive, retired --
fails the WHOLE heartbeat closed with one generic 403.

These run the real endpoint and the real SQL against SQLite (the only
Postgres-specific piece, the token hash expression, is swapped for a plain
comparison exactly as tests/test_agent_tenant_authorization.py does), so the
joins are genuinely exercised. Operational ids deliberately differ from SaaS
ids everywhere, and one fixture swaps them, so any fallback to a SaaS id fails.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import main

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
GENERIC_INSTALLATION_DETAIL = "Collector installation is not authorized to report status"
OLD = "2020-01-01 00:00:00"

_PIPELINE_STATUS_COLUMNS = [
    "last_attempt", "last_run", "status", "checkins_rows", "rejects_rows", "acs_rows",
    "uploaded_checkins_rows", "uploaded_rejects_rows", "uploaded_acs_rows",
    "checkins_bad_datetime_rows", "rejects_bad_datetime_rows", "acs_bad_datetime_rows",
    "transit_items", "problem_items", "destination_breakdown", "health_status",
    "pending_outbox_count", "quarantined_count", "oldest_pending_event_at",
    "last_success_at", "last_failure_category", "last_error", "watcher_last_active_at",
]

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


@pytest.fixture
def db(monkeypatch):
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    ddl = [
        """CREATE TABLE organizations (
            id INTEGER PRIMARY KEY, status TEXT, operational_customer_id INTEGER)""",
        """CREATE TABLE branches (
            id INTEGER PRIMARY KEY, organization_id INTEGER, status TEXT,
            operational_branch_id INTEGER)""",
        """CREATE TABLE agent_tokens (
            id INTEGER PRIMARY KEY, token_hash TEXT, customer_id INTEGER,
            branch_id INTEGER, is_active BOOLEAN, description TEXT, last_used_at TEXT,
            installation_id INTEGER)""",
        """CREATE TABLE collector_installations (
            id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER,
            name TEXT, hostname TEXT, collector_version TEXT, status TEXT,
            installed_at TEXT, last_seen_at TEXT, created_at TEXT, updated_at TEXT)""",
        "CREATE TABLE pipeline_status (customer_id INTEGER, branch_id INTEGER, "
        + ", ".join(f"{c} TEXT" for c in _PIPELINE_STATUS_COLUMNS)
        + ", updated_at TEXT, UNIQUE (customer_id, branch_id))",
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.execute(text(statement))

    # main.engine is used by the endpoint; the token lookup's hash expression
    # is the one non-portable piece of SQL.
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(
        main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, ":token")
    )
    return engine


def _org(engine, org_id, operational_customer_id, status="active"):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations VALUES (:i, :s, :c)"),
            {"i": org_id, "s": status, "c": operational_customer_id},
        )


def _branch(engine, branch_id, org_id, operational_branch_id, status="active"):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO branches VALUES (:i, :o, :s, :b)"),
            {"i": branch_id, "o": org_id, "s": status, "b": operational_branch_id},
        )


def _token(engine, token, operational_customer_id, operational_branch_id):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, is_active, description) "
                "VALUES (:t, :c, :b, 1, 'x')"
            ),
            {"t": token, "c": operational_customer_id, "b": operational_branch_id},
        )


def _installation(
    engine, installation_id, org_id, branch_id, *, status="provisioning", hostname="AMH-PC",
    version="1.0.2", installed_at=None, last_seen_at=None,
):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO collector_installations (id, organization_id, branch_id, name, hostname, "
                "collector_version, status, installed_at, last_seen_at, created_at, updated_at) "
                "VALUES (:id, :o, :b, :n, :h, :v, :s, :ia, :ls, :old, :old)"
            ),
            {
                "id": installation_id, "o": org_id, "b": branch_id, "n": f"Sorter {installation_id}",
                "h": hostname, "v": version, "s": status, "ia": installed_at, "ls": last_seen_at,
                "old": OLD,
            },
        )


@pytest.fixture
def world(db):
    """Two tenants. SaaS ids (org 1/2, branch 1/2/3) and operational ids
    (customer 10/11, branch 20/21/22) never coincide.

      org 1 (customer 10): branch 1 (op 20), branch 3 (op 22)
      org 2 (customer 11): branch 2 (op 21)
    """
    _org(db, 1, 10)
    _org(db, 2, 11)
    _branch(db, 1, 1, 20)
    _branch(db, 3, 1, 22)
    _branch(db, 2, 2, 21)
    _token(db, "tok-1", 10, 20)
    _token(db, "tok-2", 11, 21)

    _installation(db, 101, 1, 1)                                   # branch 1, provisioning
    _installation(db, 102, 1, 1, hostname="AMH-PC-2")              # second, same branch
    _installation(db, 103, 1, 3)                                   # org 1's OTHER branch
    _installation(db, 201, 2, 2)                                   # org 2
    _installation(db, 104, 1, 1, status="inactive")
    _installation(db, 105, 1, 1, status="retired")
    _installation(db, 106, 1, 1, status="active", installed_at=OLD, last_seen_at=OLD, version="1.0.2")
    return db


def _row(engine, installation_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM collector_installations WHERE id = :i"), {"i": installation_id}
        ).mappings().first()
    return dict(row) if row else None


def _all_installations(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM collector_installations ORDER BY id")).mappings().all()
    return [dict(r) for r in rows]


def _pipeline_rows(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text("SELECT * FROM pipeline_status")).mappings().all()]


def _token_last_used(engine, token):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT last_used_at FROM agent_tokens WHERE token_hash = :t"), {"t": token}
        ).scalar()


def _heartbeat(token="tok-1", customer_id=10, branch_id=20, **extra):
    payload = {"customer_id": customer_id, "branch_id": branch_id, "status": "completed", **extra}
    return client.post(
        "/upload-pipeline-status", json=payload, headers={"Authorization": f"Bearer {token}"}
    )


def _assert_generic_rejection(response):
    assert response.status_code == 403
    assert response.json() == {"detail": GENERIC_INSTALLATION_DETAIL}


class _HookedEngine:
    """Wraps the real SQLite engine so a test can observe (and interfere with)
    each statement the endpoint runs inside its one transaction."""

    def __init__(self, engine, hook):
        self._engine = engine
        self._hook = hook

    def begin(self):
        return _HookedContext(self._engine.begin(), self._hook)


class _HookedContext:
    def __init__(self, context, hook):
        self._context = context
        self._hook = hook

    def __enter__(self):
        return _HookedConnection(self._context.__enter__(), self._hook)

    def __exit__(self, *exc):
        return self._context.__exit__(*exc)


class _HookedConnection:
    def __init__(self, conn, hook):
        self._conn = conn
        self._hook = hook

    def execute(self, statement, params=None):
        self._hook(str(statement), self._conn)
        return self._conn.execute(statement, params)


# --- request contract ---------------------------------------------------------

def test_installation_fields_are_not_pipeline_status_columns():
    for field in ("installation_id", "collector_version", "hostname"):
        assert field not in main._PIPELINE_STATUS_UPDATABLE_FIELDS

    data = main.PipelineStatusRequest(
        customer_id=1, branch_id=1, status="completed", installation_id=5, collector_version="1.0.3"
    )
    sql, params = main._build_pipeline_status_upsert(data)

    assert "installation_id" not in sql and "collector_version" not in sql
    assert "installation_id" not in params and "collector_version" not in params


def test_heartbeat_contract_has_no_hostname_identity_field():
    assert "hostname" not in main.PipelineStatusRequest.model_fields
    assert {"installation_id", "collector_version"} <= set(main.PipelineStatusRequest.model_fields)


def test_both_installation_fields_are_optional():
    data = main.PipelineStatusRequest(customer_id=1, branch_id=1)

    assert data.installation_id is None and data.collector_version is None


@pytest.mark.parametrize("bad", [0, -1, 2**63, "abc", 1.5])
def test_malformed_installation_id_is_a_422_and_writes_nothing(world, bad):
    before = _all_installations(world)

    response = _heartbeat(installation_id=bad)

    assert response.status_code == 422
    assert _all_installations(world) == before
    assert _pipeline_rows(world) == []


def test_oversized_collector_version_is_a_422_and_writes_nothing(world):
    response = _heartbeat(installation_id=101, collector_version="x" * 65)

    assert response.status_code == 422
    assert _row(world, 101)["status"] == "provisioning"


# --- legacy heartbeat (no installation_id) -------------------------------------

def test_legacy_heartbeat_updates_pipeline_status_and_never_touches_installations(world):
    before = _all_installations(world)

    response = _heartbeat()

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert [r["status"] for r in _pipeline_rows(world)] == ["completed"]
    assert _all_installations(world) == before


def test_legacy_heartbeat_with_matching_hostname_and_version_still_touches_nothing(world):
    # An old/odd client naming the machine and a version, but no installation_id,
    # must not be matched to an installation by hostname (or by its branch).
    before = _all_installations(world)

    response = _heartbeat(hostname="AMH-PC", collector_version="1.0.3")

    assert response.status_code == 200
    assert _all_installations(world) == before


def test_legacy_heartbeat_with_explicit_null_installation_id_is_legacy(world):
    before = _all_installations(world)

    response = _heartbeat(installation_id=None, collector_version="1.0.3")

    assert response.status_code == 200
    assert _all_installations(world) == before
    assert len(_pipeline_rows(world)) == 1


def test_legacy_heartbeat_never_runs_the_installation_lifecycle_query(world, monkeypatch):
    executed: list[str] = []
    monkeypatch.setattr(main, "engine", _HookedEngine(world, lambda sql, conn: executed.append(sql)))

    response = _heartbeat()

    assert response.status_code == 200
    assert any("agent_tokens" in sql for sql in executed)
    # The token lookup joins collector_installations read-only (to fail closed for a
    # BOUND token); a legacy heartbeat must never run the lifecycle lookup or write.
    assert not any("UPDATE collector_installations" in sql for sql in executed)
    assert not any("ci.id = :installation_id" in " ".join(sql.split()) for sql in executed)


# --- lifecycle: provisioning -> active, installed_at, last_seen_at, version -----

# A "confirmed installation contact" is ANY successful authenticated status
# request carrying the installation_id -- a scheduled run's heartbeat OR the
# install-time preflight probe (status "preflight_check"). The server does not
# distinguish them, so the first preflight is the first confirmed contact.

def test_a_preflight_probe_is_a_confirmed_contact_that_activates_and_stamps_installed_at(world):
    response = _heartbeat(
        installation_id=101, collector_version="1.0.3",
        status="preflight_check", checkins_rows=0, rejects_rows=0, acs_rows=0,
    )

    assert response.status_code == 200
    row = _row(world, 101)
    assert row["status"] == "active"
    assert row["installed_at"] is not None and row["installed_at"] > OLD
    assert row["last_seen_at"] == row["installed_at"]
    assert [r["status"] for r in _pipeline_rows(world)] == ["preflight_check"]


def test_installed_at_is_the_first_contact_the_first_scheduled_run_only_advances_last_seen_at(world):
    _heartbeat(installation_id=101, collector_version="1.0.3", status="preflight_check")  # install-time
    with world.begin() as conn:  # age the stamps so "advanced" is observable
        conn.execute(
            text("UPDATE collector_installations SET installed_at = :t, last_seen_at = :t WHERE id = 101"),
            {"t": OLD},
        )

    _heartbeat(installation_id=101, collector_version="1.0.3", status="completed")  # first scheduled run

    row = _row(world, 101)
    assert row["installed_at"] == OLD  # the preflight's stamp, not re-stamped by the scheduled run
    assert row["last_seen_at"] > OLD  # the most recent contact
    assert row["status"] == "active"


def test_provisioning_installation_with_correct_scope_becomes_active(world):
    response = _heartbeat(installation_id=101, collector_version="1.0.3")

    assert response.status_code == 200
    row = _row(world, 101)
    assert row["status"] == "active"
    assert row["installed_at"] is not None and row["installed_at"] > OLD
    assert row["last_seen_at"] is not None and row["last_seen_at"] > OLD
    assert row["updated_at"] > OLD
    # The pipeline_status upsert happened in the same request.
    assert [r["status"] for r in _pipeline_rows(world)] == ["completed"]


def test_first_heartbeat_stamps_installed_at_and_last_seen_at_together(world):
    _heartbeat(installation_id=101, collector_version="1.0.3")

    row = _row(world, 101)
    assert row["installed_at"] == row["last_seen_at"]  # one transaction timestamp


def test_later_heartbeats_preserve_installed_at_and_advance_last_seen_at(world):
    _heartbeat(installation_id=101, collector_version="1.0.3")
    first = _row(world, 101)
    # Age the stamps so "advanced" is observable regardless of clock resolution.
    with world.begin() as conn:
        conn.execute(
            text("UPDATE collector_installations SET installed_at = :t, last_seen_at = :t, updated_at = :t "
                 "WHERE id = 101"),
            {"t": OLD},
        )

    response = _heartbeat(installation_id=101, collector_version="1.0.3")

    assert response.status_code == 200
    later = _row(world, 101)
    assert later["installed_at"] == OLD  # preserved, never re-stamped
    assert later["last_seen_at"] > OLD
    assert later["updated_at"] > OLD
    assert first["installed_at"] is not None


def test_collector_version_is_updated_from_the_reported_runtime_version(world):
    assert _row(world, 101)["collector_version"] == "1.0.2"  # admin-entered

    _heartbeat(installation_id=101, collector_version="1.0.3")

    assert _row(world, 101)["collector_version"] == "1.0.3"


@pytest.mark.parametrize("reported", [None, "", "   "])
def test_missing_or_blank_version_keeps_the_existing_version(world, reported):
    extra = {} if reported is None else {"collector_version": reported}

    response = _heartbeat(installation_id=101, **extra)

    assert response.status_code == 200
    row = _row(world, 101)
    assert row["collector_version"] == "1.0.2"
    assert row["status"] == "active"


def test_reported_version_is_trimmed(world):
    _heartbeat(installation_id=101, collector_version="  1.0.3  ")

    assert _row(world, 101)["collector_version"] == "1.0.3"


def test_active_installation_stays_active_and_keeps_installed_at(world):
    response = _heartbeat(installation_id=106, collector_version="1.0.3")

    assert response.status_code == 200
    row = _row(world, 106)
    assert row["status"] == "active"
    assert row["installed_at"] == OLD
    assert row["last_seen_at"] > OLD
    assert row["collector_version"] == "1.0.3"


def test_active_installation_with_missing_installed_at_gets_it_stamped_once(world):
    with world.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET installed_at = NULL WHERE id = 106"))

    _heartbeat(installation_id=106)

    assert _row(world, 106)["installed_at"] > OLD


# --- inactive / retired are never revived; fail closed ---------------------------

@pytest.mark.parametrize("installation_id", [104, 105], ids=["inactive", "retired"])
def test_inactive_or_retired_installation_is_rejected_and_not_reactivated(world, installation_id):
    before = _all_installations(world)

    response = _heartbeat(installation_id=installation_id, collector_version="1.0.3")

    _assert_generic_rejection(response)
    assert _all_installations(world) == before
    assert _row(world, installation_id)["last_seen_at"] is None
    assert _row(world, installation_id)["collector_version"] == "1.0.2"


@pytest.mark.parametrize("installation_id", [104, 105], ids=["inactive", "retired"])
def test_rejected_installation_heartbeat_does_not_touch_pipeline_status(world, installation_id):
    _heartbeat(installation_id=installation_id)

    assert _pipeline_rows(world) == []


# --- wrong / mismatched installation ids ------------------------------------------

def test_unknown_installation_id_fails_closed(world):
    before = _all_installations(world)

    response = _heartbeat(installation_id=9999)

    _assert_generic_rejection(response)
    assert _all_installations(world) == before
    assert _pipeline_rows(world) == []


def test_installation_of_another_organization_fails_closed(world):
    before = _all_installations(world)

    response = _heartbeat(installation_id=201)  # org 2's installation, org 1's token

    _assert_generic_rejection(response)
    assert _all_installations(world) == before
    assert _pipeline_rows(world) == []


def test_installation_of_another_branch_fails_closed(world):
    before = _all_installations(world)

    # Installation 103 is org 1's, but on branch 3; the token is scoped to branch 1.
    response = _heartbeat(installation_id=103)

    _assert_generic_rejection(response)
    assert _all_installations(world) == before
    assert _pipeline_rows(world) == []


def test_installation_row_whose_branch_belongs_to_another_organization_fails_closed(world):
    # Corrupt/inconsistent data: an org-1 installation pointing at org 2's
    # branch. The branch-belongs-to-organization join must reject it for BOTH
    # tenants.
    _installation(world, 301, 1, 2)
    before = _all_installations(world)

    for token, customer_id, branch_id in (("tok-1", 10, 20), ("tok-2", 11, 21)):
        _assert_generic_rejection(
            _heartbeat(token=token, customer_id=customer_id, branch_id=branch_id, installation_id=301)
        )

    assert _all_installations(world) == before


def test_the_other_tenants_installation_is_reachable_only_with_its_own_token(world):
    # Sanity: the very row rejected above works for its rightful tenant.
    response = _heartbeat(token="tok-2", customer_id=11, branch_id=21, installation_id=201)

    assert response.status_code == 200
    assert _row(world, 201)["status"] == "active"
    assert _row(world, 101)["status"] == "provisioning"


def test_all_rejections_are_byte_identical_and_reveal_nothing(world):
    responses = [
        _heartbeat(installation_id=9999),   # does not exist
        _heartbeat(installation_id=201),    # another organization
        _heartbeat(installation_id=103),    # another branch
        _heartbeat(installation_id=104),    # inactive
        _heartbeat(installation_id=105),    # retired
    ]

    assert {r.status_code for r in responses} == {403}
    assert len({r.text for r in responses}) == 1
    body = responses[0].text.lower()
    for word in ("exist", "found", "organization", "branch", "tenant", "inactive", "retired", "9999", "201"):
        assert word not in body


# --- SaaS id vs operational id ---------------------------------------------------------

def test_a_saas_id_collision_cannot_make_the_wrong_installation_update(db):
    """SaaS org 1 has OPERATIONAL customer 2, and SaaS org 2 has operational
    customer 1 (likewise for branches): every SaaS id is also somebody else's
    operational id. A lookup that fell back to (or confused) SaaS ids would
    resolve the wrong tenant."""
    _org(db, 1, 2)
    _org(db, 2, 1)
    _branch(db, 1, 1, 2)   # SaaS branch 1 -> operational branch 2
    _branch(db, 2, 2, 1)   # SaaS branch 2 -> operational branch 1
    _token(db, "tok-a", 2, 2)   # tenant A: operational customer 2 / branch 2 (= SaaS org 1 / branch 1)
    _token(db, "tok-b", 1, 1)   # tenant B: operational customer 1 / branch 1 (= SaaS org 2 / branch 2)
    _installation(db, 11, 1, 1)  # tenant A's
    _installation(db, 12, 2, 2)  # tenant B's

    # Tenant B's token naming tenant A's installation -- even though 11 sits on
    # SaaS branch 1 == B's OPERATIONAL branch id, and org 1 == B's operational customer.
    _assert_generic_rejection(_heartbeat(token="tok-b", customer_id=1, branch_id=1, installation_id=11))
    assert _row(db, 11)["status"] == "provisioning"
    assert _row(db, 12)["status"] == "provisioning"

    # And each tenant reaches only its own.
    assert _heartbeat(token="tok-b", customer_id=1, branch_id=1, installation_id=12).status_code == 200
    assert _row(db, 12)["status"] == "active"
    assert _row(db, 11)["status"] == "provisioning"

    assert _heartbeat(token="tok-a", customer_id=2, branch_id=2, installation_id=11).status_code == 200
    assert _row(db, 11)["status"] == "active"


def test_installation_lookup_sql_uses_the_operational_bridge():
    sql = " ".join(main._INSTALLATION_LOOKUP_SQL.split())

    assert "ci.id = :installation_id" in sql
    assert "o.operational_customer_id = :customer_id" in sql
    assert "b.operational_branch_id = :branch_id" in sql
    assert "ci.organization_id" in sql and "b.organization_id = o.id" in sql
    assert "o.id = :customer_id" not in sql
    assert "b.id = :branch_id" not in sql
    assert "LIMIT" not in sql.upper()
    assert "hostname" not in sql.lower()


# --- one branch, several installations ---------------------------------------------------

def test_only_the_explicitly_identified_installation_is_touched(world):
    # 101, 102 (provisioning), 104 (inactive), 105 (retired) and 106 (active)
    # all sit on branch 1.
    before = {r["id"]: r for r in _all_installations(world)}

    response = _heartbeat(installation_id=102, collector_version="1.0.3")

    assert response.status_code == 200
    after = {r["id"]: r for r in _all_installations(world)}
    assert after[102]["status"] == "active"
    for installation_id, untouched in before.items():
        if installation_id != 102:
            assert after[installation_id] == untouched, installation_id


def test_hostname_never_selects_an_installation(world):
    # Two installations share hostname AMH-PC's branch; no installation_id and
    # even a hostname naming a specific one changes nothing.
    before = _all_installations(world)

    _heartbeat(hostname="AMH-PC-2", collector_version="1.0.3")
    _heartbeat(hostname="AMH-PC", collector_version="1.0.3")

    assert _all_installations(world) == before


def test_a_wrong_installation_id_is_never_replaced_by_another_of_the_same_branch(world):
    before = _all_installations(world)

    # 9999 does not exist; branch 1 has plenty of installations to "fall back" to.
    _assert_generic_rejection(_heartbeat(installation_id=9999, hostname="AMH-PC"))

    assert _all_installations(world) == before


# --- gate ordering & transactionality -----------------------------------------------------

def test_scope_mismatch_is_still_reported_as_a_scope_error(world):
    before = _all_installations(world)

    response = _heartbeat(customer_id=10, branch_id=99, installation_id=101)

    assert response.status_code == 403
    assert response.json()["detail"] == "Token scope does not match customer_id / branch_id"
    assert _all_installations(world) == before


def test_unknown_token_is_still_401_with_an_installation_id(world):
    response = _heartbeat(token="nope", installation_id=101)

    assert response.status_code == 401
    assert _row(world, 101)["status"] == "provisioning"


def test_suspended_tenant_is_rejected_by_the_tenant_gate_before_any_installation_update(world):
    with world.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE id = 1"))
    before = _all_installations(world)

    response = _heartbeat(installation_id=101)

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent is not currently authorized to upload data"
    assert _all_installations(world) == before
    assert _pipeline_rows(world) == []


def test_rejected_installation_rolls_back_the_whole_heartbeat_including_token_use(world):
    assert _token_last_used(world, "tok-1") is None

    _assert_generic_rejection(_heartbeat(installation_id=9999))

    assert _token_last_used(world, "tok-1") is None
    assert _pipeline_rows(world) == []


def test_accepted_heartbeat_commits_token_use_installation_and_pipeline_status_together(world):
    response = _heartbeat(installation_id=101, collector_version="1.0.3")

    assert response.status_code == 200
    assert _token_last_used(world, "tok-1") is not None
    assert _row(world, 101)["status"] == "active"
    assert len(_pipeline_rows(world)) == 1


def test_a_pipeline_status_failure_rolls_back_the_installation_update(world, monkeypatch):
    monkeypatch.setattr(
        main, "_build_pipeline_status_upsert",
        lambda data: ("INSERT INTO no_such_table VALUES (1)", {}),
    )

    response = _heartbeat(installation_id=101, collector_version="1.0.3")

    assert response.status_code == 500
    row = _row(world, 101)
    assert row["status"] == "provisioning"
    assert row["last_seen_at"] is None
    assert row["collector_version"] == "1.0.2"


def test_a_concurrent_deactivation_between_lookup_and_update_is_not_overwritten(world, monkeypatch, caplog):
    """The lookup saw 'provisioning', but the row is 'inactive' by the time the
    guarded UPDATE runs (the hook flips it inside the request's own transaction,
    standing in for another session's committed change): the status guard makes
    the UPDATE match nothing, so the heartbeat is rejected rather than reviving it."""

    def deactivate_just_before_the_heartbeat_update(sql, conn):
        if "UPDATE collector_installations" in sql:
            conn.execute(text("UPDATE collector_installations SET status = 'inactive' WHERE id = 101"))

    monkeypatch.setattr(
        main, "engine", _HookedEngine(world, deactivate_just_before_the_heartbeat_update)
    )

    with caplog.at_level("WARNING", logger="sortview.api"):
        response = _heartbeat(installation_id=101, collector_version="1.0.3")

    _assert_generic_rejection(response)
    assert "installation status changed during the heartbeat" in caplog.text
    row = _row(world, 101)
    # Never activated, stamped or re-versioned by this heartbeat (the simulated
    # flip itself rolled back with the rest of the request).
    assert row["status"] != "active"
    assert row["last_seen_at"] is None and row["installed_at"] is None
    assert row["collector_version"] == "1.0.2"
    assert _pipeline_rows(world) == []


# --- /upload is unchanged ------------------------------------------------------------------

def test_upload_never_writes_collector_installations(monkeypatch):
    executed: list[str] = []

    class _Result:
        rowcount = 1

        def mappings(self):
            return self

        def first(self):
            return {
                "id": 1, "customer_id": 1, "branch_id": 1, "is_active": True, "description": "t",
                "organization_status": "active", "branch_status": "active",
            }

    class _Conn:
        def execute(self, statement, params=None):
            executed.append(str(statement))
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Engine:
        def begin(self):
            return _Conn()

    monkeypatch.setattr(main, "engine", _Engine())

    response = client.post(
        "/upload",
        json={"checkins": [{"customer_id": 1, "branch_id": 1, "barcode": "1",
                            "event_time": "2026-09-19T09:00:00"}]},
        headers={"Authorization": "Bearer t"},
    )

    assert response.status_code == 200
    assert any("INSERT INTO checkins" in sql for sql in executed)
    assert not any("UPDATE collector_installations" in sql for sql in executed)
    assert not any("INSERT INTO collector_installations" in sql for sql in executed)


def test_upload_ignores_installation_fields_entirely():
    # /upload's request model has no installation_id: extra keys are dropped,
    # never interpreted.
    assert "installation_id" not in main.UploadRequest.model_fields
    for row_model in (main.CheckinRow, main.RejectRow, main.AcsRow):
        assert "installation_id" not in row_model.model_fields
