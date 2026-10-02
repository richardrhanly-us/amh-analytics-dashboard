"""Tenant offboarding and purge on a REAL PostgreSQL: the migrated schema, real foreign keys, real row level security, a real
non-owning runtime role, and the purge tool's own command line end to end.

What only a real server can prove:
  * every table of the migrated schema is classified by the lifecycle policy and the privilege baseline;
  * the purge order satisfies the real foreign-key topology, and nothing retained hangs off anything purged;
  * a role provisioned from the baseline can do everything the cutoff needs and CANNOT delete anything;
  * the cutoff retires ingest keys through row level security as that role;
  * tenant_lifecycle_events is append-only for every role, and survives the purge of its tenant;
  * the purge tool's dry run is read-only, and it refuses the runtime role, an un-offboarded tenant, a stale schema and
    unattributable acs_events.

OPT-IN AND SAFE BY CONSTRUCTION -- the same convention as tests/test_rls_phase1_postgres.py: runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local server. The module creates its
own throwaway database (migrated with the project's real Alembic chain) and its own throwaway runtime role, and drops
both afterward. Production is never touched.
"""

from __future__ import annotations

import importlib.util
import io
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from scripts import runtime_role_privileges as privileges
from src.services import data_lifecycle_policy as policy
from src.services import ingest_v2_service, platform_admin_service

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "purge_tenant_data.py"
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL lifecycle tests)"
)

# org 1 "lib-a": customer 10, branches 1 and 4 -- offboarded and purged.  org 2 "lib-b": customer 20, branch 2 -- untouched.
ORG_A, CUSTOMER_A, BRANCHES_A = 1, 10, (1, 4)
ORG_B, CUSTOMER_B, BRANCHES_B = 2, 20, (2,)

CANARY_PATRON = "CANARY-PATRON-21234000111"
CANARY_BARCODE = "CANARY-BARCODE-31234000999"
CANARY_TITLE = "CANARY-TITLE The Secret Garden"
CANARY_RAW = "CANARY-RAW-64Y20261001"
CANARY_HOSTNAME = "CANARY-HOSTNAME-AMH-PC"
CANARY_EMAIL = "canary.staff@example.invalid"
_CANARIES = (CANARY_PATRON, CANARY_BARCODE, CANARY_TITLE, CANARY_RAW, CANARY_HOSTNAME, CANARY_EMAIL)

RUNTIME_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only
ACTOR = {"actor_user_id": 900, "actor_label": "operator"}


def _guard(url) -> None:
    host = url.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(
            f"refusing to run against non-local PostgreSQL host {host!r}; set "
            "SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 only for a dedicated non-production test server"
        )


def _alembic(url, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    return subprocess.run(  # nosec B603
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )


# --- a throwaway migrated database and a throwaway runtime role -----------------------------------------------------

@pytest.fixture(scope="module")
def pg_url():
    admin = make_url(ADMIN_URL)
    _guard(admin)
    name = f"sortview_lifecycle_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    try:
        migrated = _alembic(url, "upgrade", "head")
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        yield url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


@pytest.fixture(scope="module")
def owner(pg_url):
    engine = create_engine(pg_url, hide_parameters=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def role(pg_url, owner):
    """A non-owning role provisioned from the baseline's OWN provisioning SQL -- so the tests exercise exactly what a
    fresh environment would be given, not a hand-written copy of it."""
    name = f"sortview_rt_test_{secrets.token_hex(4)}"
    with owner.begin() as conn:
        conn.execute(text(
            f"CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT "  # nosec B608
            f"PASSWORD '{RUNTIME_ROLE_PASSWORD}'"
        ))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{pg_url.database}" TO {name}'))  # nosec B608
        for line in privileges.provisioning_sql(name).splitlines():
            if line and not line.startswith("--") and line not in ("BEGIN;", "COMMIT;"):
                conn.execute(text(line))
    yield name
    with owner.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {name}"))  # nosec B608
        conn.execute(text(f"DROP ROLE IF EXISTS {name}"))  # nosec B608


@pytest.fixture(scope="module")
def runtime(pg_url, role):
    engine = create_engine(pg_url.set(username=role, password=RUNTIME_ROLE_PASSWORD), hide_parameters=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("purge_tenant_data_pg_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["purge_tenant_data_pg_under_test"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("purge_tenant_data_pg_under_test", None)


# --- data ---------------------------------------------------------------------------------------------------------------

_SEEDED_TABLES = (
    "tenant_lifecycle_events", "checkin_events", "reject_events", "acs_item_events", "ingest_key_ids", "v2_cutovers",
    "checkins", "rejects", "acs_events", "checkins_clean", "rejects_clean", "pipeline_status", "agent_tokens",
    "collector_enrollment_codes", "collector_installations", "auth_sessions", "password_reset_tokens", "auth_audit_log",
    "memberships", "subscriptions", "organization_settings", "branch_settings", "app_users", "branches",
    "organizations", "customers",
)


def _hex(seed: str) -> str:
    return (seed.encode().hex() * 64)[:64]


def _seed(owner) -> None:
    with owner.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_SEEDED_TABLES)} RESTART IDENTITY CASCADE"))  # nosec B608
        plan_id = conn.execute(text("SELECT id FROM plans ORDER BY id LIMIT 1")).scalar_one()
        for org, slug, customer, branches in ((ORG_A, "lib-a", CUSTOMER_A, BRANCHES_A), (ORG_B, "lib-b", CUSTOMER_B, BRANCHES_B)):
            conn.execute(text("INSERT INTO customers (id, name) VALUES (:c, :s)"), {"c": customer, "s": slug})
            conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) "
                              "VALUES (:o, :s, :s, 'active', :c)"), {"o": org, "s": slug, "c": customer})
            conn.execute(text("INSERT INTO organization_settings (organization_id) VALUES (:o)"), {"o": org})
            conn.execute(text("INSERT INTO subscriptions (organization_id, plan_id, status) VALUES (:o, :p, 'active')"),
                         {"o": org, "p": plan_id})
            installation = org * 100 + 1
            for index, branch in enumerate(branches):
                scope = {"c": customer, "b": branch}
                conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
                                  "VALUES (:b, :o, :slug, 'Branch', 'active', :b)"),
                             {"b": branch, "o": org, "slug": f"branch-{branch}"})
                conn.execute(text("INSERT INTO branch_settings (branch_id) VALUES (:b)"), {"b": branch})
                conn.execute(text("INSERT INTO checkins (customer_id, branch_id, event_time, barcode, title) "
                                  "VALUES (:c, :b, '2026-01-01 10:00', :x, :t)"),
                             {**scope, "x": f"{CANARY_BARCODE}-{branch}", "t": CANARY_TITLE})
                conn.execute(text("INSERT INTO rejects (customer_id, branch_id, event_time, barcode, error_message) "
                                  "VALUES (:c, :b, '2026-01-01 10:00', :x, 'jam')"),
                             {**scope, "x": f"{CANARY_BARCODE}-{branch}"})
                conn.execute(text("INSERT INTO acs_events (customer_id, branch_id, event_time, message_code, barcode, "
                                  "barcode_key, patron_id, raw_message) VALUES (:c, :b, '2026-01-01 10:00', '64', :x, :x, :p, :r)"),
                             {**scope, "x": f"{CANARY_BARCODE}-{branch}", "p": CANARY_PATRON, "r": CANARY_RAW})
                key_id = f"{branch:08x}-0000-4000-8000-{customer:012x}"
                conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:k, :c, :b)"),
                             {**scope, "k": key_id})
                keyed = {**scope, "k": key_id, "e": _hex(f"e{branch}"), "i": _hex(f"i{branch}")}
                conn.execute(text("INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, "
                                  "item_key, destination, bin) VALUES (:c, :b, :k, :e, now(), :i, 'main', '1')"), keyed)
                conn.execute(text("INSERT INTO reject_events (customer_id, branch_id, key_id, event_key, event_time, "
                                  "error_class) VALUES (:c, :b, :k, :e, now(), 'other')"), keyed)
                conn.execute(text("INSERT INTO acs_item_events (customer_id, branch_id, key_id, event_key, event_time, "
                                  "item_key, state) VALUES (:c, :b, :k, :e, now(), :i, 'other_code10')"), keyed)
                conn.execute(text("INSERT INTO v2_cutovers (customer_id, branch_id, set_by) VALUES (:c, :b, 'operator')"), scope)
                conn.execute(text("INSERT INTO pipeline_status (customer_id, branch_id, status) VALUES (:c, :b, 'ok')"), scope)
                if index == 0:
                    conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, name, hostname, "
                                      "status) VALUES (:i, :o, :b, 'Sorter', :h, 'active')"),
                                 {"i": installation, "o": org, "b": branch, "h": CANARY_HOSTNAME})
                    conn.execute(text("INSERT INTO collector_enrollment_codes (installation_id, code_hash, expires_at) "
                                      "VALUES (:i, :h, now() + interval '1 hour')"),
                                 {"i": installation, "h": _hex(f"code{org}")})
                    for bound in (installation, None):  # an enrollment-issued token, and a legacy unbound one
                        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, installation_id) "
                                          "VALUES (:h, :c, :b, :i)"),
                                     {**scope, "h": _hex(f"token{org}{bound}"), "i": bound})
        # Users are global. 1: only lib-a. 2: both tenants.
        for user, email, orgs in ((1, CANARY_EMAIL, (ORG_A,)), (2, "both@example.invalid", (ORG_A, ORG_B))):
            conn.execute(text("INSERT INTO app_users (id, email, full_name) VALUES (:u, :e, 'Staff')"), {"u": user, "e": email})
            conn.execute(text("INSERT INTO auth_sessions (user_id, token_hash, expires_at) "
                              "VALUES (:u, :h, now() + interval '1 day')"), {"u": user, "h": _hex(f"session{user}")})
            conn.execute(text("INSERT INTO auth_audit_log (user_id, email, event_type, is_success) "
                              "VALUES (:u, :e, 'login_success', TRUE)"), {"u": user, "e": email})
            for org in orgs:
                conn.execute(text("INSERT INTO memberships (organization_id, user_id, role) VALUES (:o, :u, 'viewer')"),
                             {"o": org, "u": user})


@pytest.fixture
def seeded(owner):
    _seed(owner)
    return owner


def _counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {table: int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())  # nosec B608
                for table in _SEEDED_TABLES}


def _scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _offboard(monkeypatch, engine, organization_id=ORG_A, slug="lib-a"):
    monkeypatch.setattr(platform_admin_service, "get_engine", lambda: engine)
    return platform_admin_service.offboard_library(organization_id, slug, **ACTOR)


def _repository_head() -> str:
    """The Alembic head of this checkout (the throwaway database is migrated to it), never a pinned revision id."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def _purge_cli(tool, url, *extra, organization_id=ORG_A, slug="lib-a"):
    out = io.StringIO()
    code = tool.main(["--organization-id", str(organization_id), "--confirm-slug", slug, "--database-url",
                      url.render_as_string(hide_password=False), *extra], out=out)
    return code, out.getvalue()


def _execute_args(slug="lib-a"):
    return ("--execute", "--confirm-purge", slug, "--operator", "test-operator")


# ======================================================================================================================
# the registries against the really migrated schema
# ======================================================================================================================

def test_every_table_in_the_migrated_schema_is_classified_by_the_lifecycle_policy(owner):
    with owner.connect() as conn:
        relations = dict(conn.execute(text(
            "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'public'"
        )).all())

    tables = {name for name, kind in relations.items() if kind == "BASE TABLE"}
    views = {name for name, kind in relations.items() if kind == "VIEW"}
    assert tables == {entry.table for entry in policy.DATABASE_SURFACES}
    assert views == set(policy.DERIVED_VIEWS)
    assert tables | views == set(privileges.BASELINE)


def test_the_purge_order_satisfies_every_real_foreign_key_and_nothing_retained_hangs_off_a_purged_table(owner):
    with owner.connect() as conn:
        foreign_keys = conn.execute(text("""
            SELECT child.relname, parent.relname
            FROM pg_constraint c
            JOIN pg_class child ON child.oid = c.conrelid
            JOIN pg_class parent ON parent.oid = c.confrelid
            JOIN pg_namespace n ON n.oid = child.relnamespace
            WHERE c.contype = 'f' AND n.nspname = 'public'
        """)).all()
    order = {entry.table: entry.purge_order for entry in policy.purge_plan()}

    assert len(foreign_keys) >= 20
    for child, parent in foreign_keys:
        if parent in order:
            # A purged parent's children must be purged too, and first -- otherwise the DELETE fails or cascades silently.
            assert child in order, f"{child} references purged table {parent} but is retained"
            assert order[child] < order[parent], f"{child} must be deleted before {parent}"


def test_the_nullable_tenant_keys_in_the_registry_match_the_real_columns(owner):
    with owner.connect() as conn:
        nullable = {
            (table, column) for table, column in conn.execute(text(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                "AND column_name IN ('customer_id', 'branch_id') AND is_nullable = 'YES'"
            )).all()
            if table not in policy.DERIVED_VIEWS
        }

    declared = {(e.table, column) for e in policy.DATABASE_SURFACES for column in e.nullable_tenant_keys}
    assert nullable == declared


# ======================================================================================================================
# the runtime role: provisioned from the baseline, verified against it, and unable to delete
# ======================================================================================================================

def test_a_role_provisioned_from_the_baseline_verifies_clean(owner, role):
    with owner.connect() as conn:
        assert privileges.differences(conn, role) == []


def test_verify_reports_an_extra_grant_a_missing_grant_and_an_unclassified_table(owner, role):
    with owner.begin() as conn:
        conn.execute(text(f"GRANT DELETE ON TABLE public.checkins TO {role}"))  # nosec B608
        conn.execute(text(f"REVOKE UPDATE ON TABLE public.agent_tokens FROM {role}"))  # nosec B608
        conn.execute(text("CREATE TABLE public.unreviewed_new_table (id INTEGER)"))
    try:
        with owner.connect() as conn:
            found = privileges.differences(conn, role)
    finally:
        with owner.begin() as conn:
            conn.execute(text(f"REVOKE DELETE ON TABLE public.checkins FROM {role}"))  # nosec B608
            conn.execute(text(f"GRANT UPDATE ON TABLE public.agent_tokens TO {role}"))  # nosec B608
            conn.execute(text("DROP TABLE public.unreviewed_new_table"))

    assert "checkins: DELETE is granted; the baseline does not allow it" in found
    assert "agent_tokens: UPDATE is missing" in found
    assert any(d.startswith("unreviewed_new_table: not in the baseline") for d in found)
    with owner.connect() as conn:
        assert privileges.differences(conn, role) == []  # and it is clean again once put right


def test_the_runtime_role_cannot_delete_or_truncate_any_table(seeded, runtime):
    before = _counts(seeded)

    for table in sorted(entry.table for entry in policy.DATABASE_SURFACES):
        for statement in (f"DELETE FROM {table}", f"TRUNCATE {table} CASCADE"):  # nosec B608
            with runtime.connect() as conn, pytest.raises(Exception, match="permission denied"):
                conn.execute(text(statement))

    assert _counts(seeded) == before


def test_row_level_security_is_enabled_and_not_forced_on_the_operational_tables(owner):
    with owner.connect() as conn:
        rows = conn.execute(text(
            "SELECT relname, relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND relrowsecurity"
        )).all()

    assert {name for name, _forced in rows} == set(privileges.RLS_TABLES)
    assert not any(forced for _name, forced in rows)  # the owner must keep bypassing it: migrations, and the purge


# ======================================================================================================================
# lifecycle evidence: append-only for everyone
# ======================================================================================================================

def _record(conn, **overrides):
    policy.record_tenant_lifecycle_event(conn, **{
        "event_type": policy.EVENT_ACCESS_CUTOFF, "organization_id": ORG_A, "organization_slug": "lib-a",
        "operational_customer_id": CUSTOMER_A, "actor_user_id": None, "actor_label": "operator",
        "details": {"counts": {"agent_tokens_deactivated": 2}}, **overrides,
    })


def test_the_runtime_role_can_append_evidence_but_not_read_change_or_remove_it(seeded, runtime):
    with runtime.begin() as conn:
        _record(conn)
    assert _scalar(seeded, "SELECT details -> 'counts' ->> 'agent_tokens_deactivated' FROM tenant_lifecycle_events") == "2"

    for statement in ("SELECT * FROM tenant_lifecycle_events", "UPDATE tenant_lifecycle_events SET actor_label = 'x'",
                      "DELETE FROM tenant_lifecycle_events"):
        with runtime.connect() as conn, pytest.raises(Exception, match="permission denied"):
            conn.execute(text(statement))
    assert _scalar(seeded, "SELECT COUNT(*) FROM tenant_lifecycle_events") == 1


def test_even_the_table_owner_cannot_update_or_delete_evidence(seeded):
    with seeded.begin() as conn:
        _record(conn)

    for statement in ("UPDATE tenant_lifecycle_events SET actor_label = 'someone else'",
                      "DELETE FROM tenant_lifecycle_events"):
        with seeded.connect() as conn, pytest.raises(Exception, match="append-only"):
            conn.execute(text(statement))
    assert _scalar(seeded, "SELECT actor_label FROM tenant_lifecycle_events") == "operator"


def test_the_database_rejects_an_unknown_event_type_a_blank_actor_and_non_object_details(seeded):
    insert = "INSERT INTO tenant_lifecycle_events (event_type, organization_id, organization_slug, actor_label, details) "
    for values in (
        "VALUES ('deleted_everything', 1, 'lib-a', 'operator', '{}'::jsonb)",
        "VALUES ('access_cutoff', 1, 'lib-a', '   ', '{}'::jsonb)",
        "VALUES ('access_cutoff', 1, 'lib-a', 'operator', '[1, 2]'::jsonb)",
    ):
        with seeded.connect() as conn, pytest.raises(Exception, match="violates check constraint"):
            conn.execute(text(insert + values))


# ======================================================================================================================
# the cutoff, run AS the runtime role
# ======================================================================================================================

def test_without_tenant_context_the_runtime_role_cannot_even_see_an_ingest_key(seeded, runtime):
    # Why offboard_library sets the row-level-security context before each UPDATE: without it this matches nothing.
    with runtime.begin() as conn:
        updated = conn.execute(text(
            "UPDATE ingest_key_ids SET status = 'retired', retired_at = now() WHERE customer_id = :c"
        ), {"c": CUSTOMER_A}).rowcount

    assert updated == 0
    assert _scalar(seeded, "SELECT COUNT(*) FROM ingest_key_ids WHERE status = 'active'") == 3


def test_the_runtime_role_can_perform_the_whole_cutoff(seeded, runtime, monkeypatch):
    result = _offboard(monkeypatch, runtime)

    assert result["counts"] == {
        "agent_tokens_deactivated": 2, "installations_retired": 1, "enrollment_codes_revoked": 1,
        "ingest_keys_retired": 2,  # one per branch, through row level security
        "sessions_revoked": 1,     # user 1; user 2 still belongs to lib-b
    }
    assert _scalar(seeded, "SELECT status FROM organizations WHERE id = :o", o=ORG_A) == "cancelled"
    assert _scalar(seeded, "SELECT COUNT(*) FROM agent_tokens WHERE customer_id = :c AND is_active", c=CUSTOMER_A) == 0
    assert _scalar(seeded, "SELECT COUNT(*) FROM ingest_key_ids WHERE customer_id = :c AND status = 'active'", c=CUSTOMER_A) == 0
    assert _scalar(seeded, "SELECT COUNT(*) FROM auth_sessions WHERE revoked_at IS NULL") == 1
    assert _scalar(seeded, "SELECT event_type FROM tenant_lifecycle_events") == "access_cutoff"
    # The other tenant: every artifact still live.
    assert _scalar(seeded, "SELECT status FROM organizations WHERE id = :o", o=ORG_B) == "active"
    assert _scalar(seeded, "SELECT COUNT(*) FROM agent_tokens WHERE customer_id = :c AND is_active", c=CUSTOMER_B) == 2
    assert _scalar(seeded, "SELECT status FROM ingest_key_ids WHERE customer_id = :c", c=CUSTOMER_B) == "active"
    assert _scalar(seeded, "SELECT status FROM collector_installations WHERE organization_id = :o", o=ORG_B) == "active"


def test_the_cutoff_deletes_nothing(seeded, runtime, monkeypatch):
    before = _counts(seeded)

    _offboard(monkeypatch, runtime)

    assert _counts(seeded) == {**before, "tenant_lifecycle_events": 1}


def test_a_cancelled_tenant_cannot_be_re_provisioned_on_the_real_schema(seeded, monkeypatch):
    _offboard(monkeypatch, seeded)

    with pytest.raises(ValueError, match="cancelled"), seeded.begin() as conn:
        ingest_v2_service.issue_ingest_key(conn, CUSTOMER_A, BRANCHES_A[0])
    with pytest.raises(ValueError, match="cancelled"), seeded.begin() as conn:
        ingest_v2_service.record_v2_cutover(conn, CUSTOMER_A, BRANCHES_A[0], None, "operator")
    with seeded.begin() as conn:  # the other tenant is unaffected
        assert ingest_v2_service.issue_ingest_key(conn, CUSTOMER_B, BRANCHES_B[0])


# ======================================================================================================================
# the purge tool, end to end
# ======================================================================================================================

def test_the_dry_run_is_a_read_only_transaction_that_writes_nothing(seeded, tool, pg_url, monkeypatch):
    _offboard(monkeypatch, seeded)
    before = _counts(seeded)
    statements: list[str] = []
    real_create_engine = tool.create_engine

    def recording_create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        event.listen(engine, "before_cursor_execute",
                     lambda _c, _cur, statement, *_rest: statements.append(" ".join(statement.split()).lower()))
        return engine

    monkeypatch.setattr(tool, "create_engine", recording_create_engine)

    code, out = _purge_cli(tool, pg_url)

    assert code == 0 and "INVENTORY (read-only, nothing is written)" in out and "WOULD BE ACCEPTED" in out
    assert "Nothing was written." in out and "LIVE DATABASE PURGE COMPLETE" not in out
    assert statements[0] == "set transaction read only"
    assert not any(s.startswith(("delete", "insert", "update", "truncate")) for s in statements)
    assert _counts(seeded) == before
    for line in ("acs_events                   2", "customers                    1", "TOTAL                        36"):
        assert line in out


def test_a_tenant_that_has_not_been_offboarded_is_refused(seeded, tool, pg_url):
    before = _counts(seeded)

    code, out = _purge_cli(tool, pg_url, *_execute_args("lib-b"), organization_id=ORG_B, slug="lib-b")

    assert code == 2 and "REFUSED: organization status is 'active', not 'cancelled'" in out
    assert "Nothing was written." in out and _counts(seeded) == before


def test_the_purge_refuses_to_run_as_the_runtime_role(seeded, tool, pg_url, role, monkeypatch):
    _offboard(monkeypatch, seeded)
    before = _counts(seeded)
    runtime_url = pg_url.set(username=role, password=RUNTIME_ROLE_PASSWORD)

    # A role with the runtime baseline owns nothing, so it is refused whatever it is called ...
    for extra in ((), _execute_args()):
        code, out = _purge_cli(tool, runtime_url, *extra)
        assert code == 2 and f"REFUSED: role '{role}' is not the owner of: " in out
        assert "Nothing was written." in out and "WOULD BE ACCEPTED" not in out and "Rows in scope" not in out

    # ... and the production role is refused by name, before anything else is looked at.
    monkeypatch.setattr(tool, "RUNTIME_ROLE", role)
    code, out = _purge_cli(tool, runtime_url, *_execute_args())
    assert code == 2 and f"REFUSED: connected as the runtime role '{role}'" in out

    assert "LIVE DATABASE PURGE COMPLETE" not in out and _counts(seeded) == before


# ======================================================================================================================
# the owner-only contract (final-review defect A1)
#
# The defect: a role that is NOT the table owner, but had been granted DELETE on everything, saw the row-level-security
# tables as EMPTY. For a tenant whose only rows in those tables were acs_events (which has no foreign key to stop the
# organization being deleted around it), the tool printed "WOULD BE ACCEPTED", then "LIVE DATABASE PURGE COMPLETE", and
# wrote a purge_executed evidence row -- with the tenant's acs_events, and a NULL-keyed one, still in the table.
# ======================================================================================================================

BROAD_ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only


@pytest.fixture(scope="module")
def broad_role(pg_url, owner):
    """NOT the owner and NOT sortview_app, but granted everything grantable: ALL on every table and sequence. It is
    still subject to row level security (NOBYPASSRLS, not a superuser) -- exactly the role that exposed the defect."""
    name = f"sortview_broad_test_{secrets.token_hex(4)}"
    with owner.begin() as conn:
        conn.execute(text(f"CREATE ROLE {name} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{BROAD_ROLE_PASSWORD}'"))  # nosec B608
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{pg_url.database}" TO {name}'))  # nosec B608
        conn.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {name}"))  # nosec B608
        conn.execute(text(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {name}"))  # nosec B608
    yield name
    with owner.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {name}"))  # nosec B608
        conn.execute(text(f"DROP ROLE IF EXISTS {name}"))  # nosec B608


def _acs_only_tenant(owner, monkeypatch, *, null_keyed_row: bool) -> None:
    """lib-a, offboarded, whose ONLY rows in a row-level-security table are its two acs_events."""
    _offboard(monkeypatch, owner)
    with owner.begin() as conn:
        for table in ("checkins", "rejects", "checkin_events", "reject_events", "acs_item_events", "ingest_key_ids"):
            conn.execute(text(f"DELETE FROM {table} WHERE customer_id = :c"), {"c": CUSTOMER_A})  # nosec B608
        if null_keyed_row:
            conn.execute(text("INSERT INTO acs_events (customer_id, branch_id, event_time, message_code, barcode_key, "
                              "patron_id) VALUES (NULL, NULL, '2026-02-02 10:00', '64', 'orphan', :p)"), {"p": CANARY_PATRON})


def _record_statements(tool, monkeypatch) -> list[str]:
    statements: list[str] = []
    real_create_engine = tool.create_engine

    def recording_create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        event.listen(engine, "before_cursor_execute",
                     lambda _c, _cur, statement, *_rest: statements.append(" ".join(statement.split()).lower()))
        return engine

    monkeypatch.setattr(tool, "create_engine", recording_create_engine)
    return statements


def test_the_owner_sees_the_tenants_acs_events_and_refuses_on_the_null_keyed_one(seeded, tool, pg_url, monkeypatch):
    _acs_only_tenant(seeded, monkeypatch, null_keyed_row=True)
    before = _counts(seeded)

    for extra in ((), _execute_args()):
        code, out = _purge_cli(tool, pg_url, *extra)

        assert code == 2
        assert "acs_events                   2" in out  # the owner really sees the tenant's rows ...
        assert "REFUSED: acs_events: 1 row(s) have a NULL tenant key" in out  # ... and the unattributable one
        assert "WOULD BE ACCEPTED" not in out and "LIVE DATABASE PURGE COMPLETE" not in out
    assert _counts(seeded) == before


@pytest.mark.parametrize("null_keyed_row", [True, False])
def test_a_non_owner_role_with_every_privilege_is_refused_before_it_can_inventory_or_purge(
        seeded, tool, pg_url, broad_role, monkeypatch, null_keyed_row):
    _acs_only_tenant(seeded, monkeypatch, null_keyed_row=null_keyed_row)
    before = _counts(seeded)
    broad_url = pg_url.set(username=broad_role, password=BROAD_ROLE_PASSWORD)

    # The precondition of the defect, shown directly: the role may DELETE, and row level security shows it nothing.
    broad = create_engine(broad_url)
    try:
        with broad.connect() as conn:
            assert conn.execute(text("SELECT has_table_privilege(current_user, 'public.acs_events', 'DELETE')")).scalar()
            assert conn.execute(text("SELECT COUNT(*) FROM acs_events")).scalar() == 0
    finally:
        broad.dispose()
    assert before["acs_events"] == (4 if null_keyed_row else 3)  # lib-a 2, lib-b 1 (+ the NULL-keyed one): really there

    statements = _record_statements(tool, monkeypatch)
    for extra in ((), _execute_args()):
        code, out = _purge_cli(tool, broad_url, *extra)

        assert code == 2, out
        assert f"REFUSED: role '{broad_role}' is not the owner of: " in out
        assert "A purge requires the table-owner role" in out and "row level security cannot hide" in out
        assert "Nothing was written." in out
        # No eligibility verdict and no count was produced from what this role can see.
        assert "WOULD BE ACCEPTED" not in out and "Rows in scope" not in out and "TOTAL" not in out
        assert "LIVE DATABASE PURGE COMPLETE" not in out and "EVIDENCE SUMMARY" not in out

    # It stopped at the catalog: the tenant was never looked up, nothing was counted, nothing was deleted or recorded.
    assert statements and not any("from organizations" in s or "count(*)" in s for s in statements)
    assert not any(s.startswith(("delete", "insert", "update")) for s in statements)

    assert _counts(seeded) == before
    assert _scalar(seeded, "SELECT status FROM organizations WHERE id = :o", o=ORG_A) == "cancelled"  # still there
    assert _scalar(seeded, "SELECT COUNT(*) FROM acs_events WHERE customer_id = :c", c=CUSTOMER_A) == 2
    assert _scalar(seeded, "SELECT COUNT(*) FROM tenant_lifecycle_events WHERE event_type = 'purge_executed'") == 0


def test_ownership_is_required_of_every_table_the_tool_uses_and_superuser_is_not_a_substitute(seeded, tool, pg_url, monkeypatch):
    _offboard(monkeypatch, seeded)
    before = _counts(seeded)
    required = tool.ownership_tables()
    assert set(required) == {entry.table for entry in policy.purge_plan()} | {"tenant_lifecycle_events", "alembic_version"}
    original_owner = _scalar(seeded, "SELECT current_user")
    assert _scalar(seeded, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")  # the test connection IS a superuser
    other = f"sortview_other_owner_{secrets.token_hex(4)}"
    with seeded.begin() as conn:
        conn.execute(text(f"CREATE ROLE {other} NOLOGIN"))  # nosec B608
    try:
        assert _purge_cli(tool, pg_url)[0] == 0  # owning all of them: accepted
        for table in required:  # ... and giving away ANY single one is enough to be refused
            with seeded.begin() as conn:
                conn.execute(text(f"ALTER TABLE public.{table} OWNER TO {other}"))  # nosec B608
            try:
                for extra in ((), _execute_args()):
                    code, out = _purge_cli(tool, pg_url, *extra)
                    assert code == 2, table
                    assert f"REFUSED: role '{original_owner}' is not the owner of: {table}. " in out, table
                    assert "WOULD BE ACCEPTED" not in out and "Rows in scope" not in out, table
            finally:
                with seeded.begin() as conn:
                    conn.execute(text(f"ALTER TABLE public.{table} OWNER TO {original_owner}"))  # nosec B608
    finally:
        with seeded.begin() as conn:
            conn.execute(text(f"DROP ROLE IF EXISTS {other}"))  # nosec B608

    assert _counts(seeded) == before
    assert _purge_cli(tool, pg_url)[0] == 0  # ownership restored: accepted again


@pytest.mark.parametrize("table", ["acs_events", "checkins", "organizations"])
def test_forced_row_level_security_is_refused_because_it_would_filter_the_owner_too(seeded, tool, pg_url, monkeypatch, table):
    _offboard(monkeypatch, seeded)
    before = _counts(seeded)
    with seeded.begin() as conn:
        conn.execute(text(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY"))  # nosec B608
    try:
        for extra in ((), _execute_args()):
            code, out = _purge_cli(tool, pg_url, *extra)
            assert code == 2 and f"REFUSED: row level security is FORCED on: {table}. " in out
            assert "WOULD BE ACCEPTED" not in out and "Rows in scope" not in out and "LIVE DATABASE PURGE COMPLETE" not in out
    finally:
        with seeded.begin() as conn:
            conn.execute(text(f"ALTER TABLE public.{table} NO FORCE ROW LEVEL SECURITY"))  # nosec B608

    assert _counts(seeded) == before
    assert _purge_cli(tool, pg_url)[0] == 0


def test_the_purge_refuses_a_schema_that_is_not_at_head(seeded, tool, pg_url, monkeypatch):
    _offboard(monkeypatch, seeded)
    before = _counts(seeded)
    monkeypatch.setattr(tool, "expected_schema_revision", lambda: "ffffffffffff")

    code, out = _purge_cli(tool, pg_url, *_execute_args())

    head = _repository_head()
    assert code == 2 and f"REFUSED: schema revision is '{head}', expected this repository's head 'ffffffffffff'" in out
    assert _counts(seeded) == before


@pytest.mark.parametrize(("customer_id", "branch_id"), [(None, None), (CUSTOMER_A, None), (None, BRANCHES_A[0])])
def test_the_purge_refuses_while_an_unattributable_acs_event_exists(seeded, tool, pg_url, monkeypatch, customer_id, branch_id):
    _offboard(monkeypatch, seeded)
    with seeded.begin() as conn:
        conn.execute(text("INSERT INTO acs_events (customer_id, branch_id, event_time, message_code, barcode_key, patron_id) "
                          "VALUES (:c, :b, '2026-02-02 10:00', '64', 'orphan', :p)"),
                     {"c": customer_id, "b": branch_id, "p": CANARY_PATRON})
    before = _counts(seeded)

    code, out = _purge_cli(tool, pg_url, *_execute_args())

    assert code == 2 and "REFUSED: acs_events: 1 row(s) have a NULL tenant key" in out
    assert "classified or remediated by hand" in out and "LIVE DATABASE PURGE COMPLETE" not in out
    assert not any(canary in out for canary in _CANARIES)  # the count, never the row
    assert _counts(seeded) == before  # nothing deleted: not a partial purge reported as complete


def test_the_purge_deletes_the_tenant_keeps_the_evidence_and_does_not_overclaim(seeded, tool, pg_url, monkeypatch):
    _offboard(monkeypatch, seeded)
    before = _counts(seeded)

    code, out = _purge_cli(tool, pg_url, *_execute_args())

    assert code == 0, out
    # --- what it says ---
    assert "LIVE DATABASE PURGE COMPLETE" in out and "EVIDENCE SUMMARY" in out
    assert "PROVIDER HISTORY AGED OUT: NOT VERIFIED" in out and "NOT yet physically erased" in out
    assert out.index("LIVE DATABASE PURGE COMPLETE") < out.index("PROVIDER HISTORY AGED OUT")
    assert "operator=test-operator" in out and f"schema_revision={_repository_head()}" in out
    assert not any(canary in out for canary in _CANARIES)
    assert pg_url.password not in out and "postgresql://" not in out

    # --- what is gone: every row of lib-a in every purge-plan table ---
    scope = {"organization_id": ORG_A, "customer_id": CUSTOMER_A}
    with seeded.connect() as conn:
        for entry in policy.purge_plan():
            remaining = conn.execute(text(f"SELECT COUNT(*) FROM {entry.table} WHERE {entry.purge_where}"), scope).scalar()  # nosec B608
            assert remaining == 0, entry.table
        assert conn.execute(text("SELECT COUNT(*) FROM branches WHERE id IN (1, 4)")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM checkins_routed WHERE customer_id = :c"), {"c": CUSTOMER_A}).scalar() == 0

    # --- what is left: lib-b whole, the global accounts and audit log, and the evidence ---
    after = _counts(seeded)
    assert after["organizations"] == 1 and after["customers"] == 1 and after["branches"] == 1
    for table in ("checkins", "rejects", "acs_events", "checkins_clean", "rejects_clean", "checkin_events", "reject_events",
                  "acs_item_events", "ingest_key_ids", "v2_cutovers", "pipeline_status", "collector_installations",
                  "collector_enrollment_codes"):
        assert after[table] == 1, table
    assert after["agent_tokens"] == 2 and after["memberships"] == 1
    for table in ("app_users", "auth_sessions", "password_reset_tokens", "auth_audit_log"):
        assert after[table] == before[table], table  # a tenant purge is not deletion of a person's account or audit trail
    assert _scalar(seeded, "SELECT status FROM organizations WHERE id = :o", o=ORG_B) == "active"

    with seeded.connect() as conn:
        events = conn.execute(text(
            "SELECT event_type, organization_id, organization_slug, operational_customer_id, actor_label, details "
            "FROM tenant_lifecycle_events ORDER BY id"
        )).mappings().all()
    assert [e["event_type"] for e in events] == ["access_cutoff", "purge_executed"]  # both outlive the tenant
    purge = events[1]
    assert (purge["organization_id"], purge["organization_slug"], purge["operational_customer_id"]) == (ORG_A, "lib-a", CUSTOMER_A)
    assert purge["actor_label"] == "test-operator" and purge["details"]["rows_deleted"]["acs_events"] == 2
    assert not any(canary in str(dict(e)) for e in events for canary in _CANARIES)

    # --- and it cannot be run twice ---
    code, out = _purge_cli(tool, pg_url, *_execute_args())
    assert code == 2 and "REFUSED: organization 1 does not exist" in out
    assert _counts(seeded) == after


# ======================================================================================================================
# search_path: the objects whose ownership is checked are exactly the objects queried and deleted from
#
# The ownership gate validates public.<table>; the tool's statements name tables unqualified. A search path that puts
# another schema ahead of public (a role or database default, or a connection option) must not be able to redirect the
# inventory, the checks or the DELETEs to same-named tables there.
# ======================================================================================================================

SHADOW_SCHEMA = "shadow_ahead_of_public"
_SHADOW_ACS_ROWS = 6  # 5 keyed to lib-a's customer, 1 with NULL keys
_SHADOW_ORGANIZATIONS = 1


@pytest.fixture
def shadowed(seeded, pg_url):
    """A schema holding same-named tables, owned by the same (valid) owner, and a URL whose search_path puts it FIRST.
    Only connections made from that URL see the altered path; the other fixtures' connections are untouched."""
    with seeded.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SHADOW_SCHEMA} CASCADE"))  # nosec B608
        conn.execute(text(f"CREATE SCHEMA {SHADOW_SCHEMA}"))  # nosec B608
        for table in ("acs_events", "checkins_clean", "organizations"):
            conn.execute(text(
                f"CREATE TABLE {SHADOW_SCHEMA}.{table} (LIKE public.{table} INCLUDING DEFAULTS)"))  # nosec B608
        for index in range(5):
            conn.execute(text(f"INSERT INTO {SHADOW_SCHEMA}.acs_events (id, customer_id, branch_id, patron_id) "  # nosec B608
                              "VALUES (:i, :c, :b, :p)"),
                         {"i": 9000 + index, "c": CUSTOMER_A, "b": BRANCHES_A[0], "p": CANARY_PATRON})
        # An unattributable row: if the tool read THIS table, it would refuse (and if it deleted from it, this would go).
        conn.execute(text(f"INSERT INTO {SHADOW_SCHEMA}.acs_events (id, customer_id, branch_id, patron_id) "  # nosec B608
                          "VALUES (9999, NULL, NULL, :p)"), {"p": CANARY_PATRON})
        conn.execute(text(f"INSERT INTO {SHADOW_SCHEMA}.checkins_clean (id, customer_id, branch_id) VALUES (1, :c, :b)"),  # nosec B608
                     {"c": CUSTOMER_A, "b": BRANCHES_A[0]})
        # A shadow organization with the SAME id and slug but still 'active': read instead of the real one, it would
        # make the tool refuse "not cancelled"; deleted instead of the real one, the real tenant would survive.
        conn.execute(text(f"INSERT INTO {SHADOW_SCHEMA}.organizations (id, slug, name, status, operational_customer_id) "  # nosec B608
                          "VALUES (:o, 'lib-a', 'shadow', 'active', :c)"), {"o": ORG_A, "c": CUSTOMER_A})
    yield pg_url.set(query={"options": f"-csearch_path={SHADOW_SCHEMA},public"})
    with seeded.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SHADOW_SCHEMA} CASCADE"))  # nosec B608


def _shadow_state(owner) -> dict[str, int]:
    with owner.connect() as conn:
        return {
            "acs_events": conn.execute(text(f"SELECT COUNT(*) FROM {SHADOW_SCHEMA}.acs_events")).scalar(),  # nosec B608
            "acs_events_null_keyed": conn.execute(text(
                f"SELECT COUNT(*) FROM {SHADOW_SCHEMA}.acs_events WHERE customer_id IS NULL")).scalar(),  # nosec B608
            "checkins_clean": conn.execute(text(f"SELECT COUNT(*) FROM {SHADOW_SCHEMA}.checkins_clean")).scalar(),  # nosec B608
            "organizations": conn.execute(text(f"SELECT COUNT(*) FROM {SHADOW_SCHEMA}.organizations")).scalar(),  # nosec B608
        }


_SHADOW_UNTOUCHED = {"acs_events": _SHADOW_ACS_ROWS, "acs_events_null_keyed": 1, "checkins_clean": 1,
                     "organizations": _SHADOW_ORGANIZATIONS}


def test_the_shadowing_search_path_is_real_for_an_ordinary_connection(seeded, shadowed):
    # Guards the regression tests below: an ordinary connection from this URL really does resolve the unqualified names
    # to the shadow tables first. Without that, "the tool ignored the shadow" would prove nothing.
    engine = create_engine(shadowed)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SHOW search_path")).scalar().replace(" ", "") == f"{SHADOW_SCHEMA},public"
            assert conn.execute(text("SELECT COUNT(*) FROM acs_events")).scalar() == _SHADOW_ACS_ROWS
            assert conn.execute(text("SELECT status FROM organizations WHERE id = :o"), {"o": ORG_A}).scalar() == "active"
            assert conn.execute(text(
                f"SELECT to_regclass('acs_events') = to_regclass('{SHADOW_SCHEMA}.acs_events') "  # nosec B608
                "AND to_regclass('acs_events') <> to_regclass('public.acs_events')")).scalar()
    finally:
        engine.dispose()


def test_a_schema_ahead_of_public_cannot_redirect_the_inventory_or_the_purge(seeded, tool, shadowed, monkeypatch):
    _offboard(monkeypatch, seeded)
    public_before = _counts(seeded)
    statements = _record_statements(tool, monkeypatch)

    # --- inventory: counts are public's, and the shadow's NULL-keyed row and 'active' organization are never seen ---
    code, out = _purge_cli(tool, shadowed)

    assert code == 0, out
    assert "WOULD BE ACCEPTED" in out and "REFUSED" not in out
    assert "acs_events                   2" in out and "checkins_clean               2" in out  # public: 2 and 2; shadow: 6 and 1
    assert "status=cancelled" in out  # the public organization, not the shadow one
    assert statements[1] == "set local search_path = public, pg_temp"  # straight after SET TRANSACTION READ ONLY
    assert _counts(seeded) == public_before and _shadow_state(seeded) == _SHADOW_UNTOUCHED

    # --- the purge itself: public is purged, the shadow schema is not touched ---
    code, out = _purge_cli(tool, shadowed, *_execute_args())

    assert code == 0, out
    assert "LIVE DATABASE PURGE COMPLETE" in out
    assert "  acs_events                   2" in out  # rows DELETED: public's two
    assert _shadow_state(seeded) == _SHADOW_UNTOUCHED  # every shadow row, the NULL-keyed one included, is still there
    assert _scalar(seeded, f"SELECT status FROM {SHADOW_SCHEMA}.organizations WHERE id = :o", o=ORG_A) == "active"  # nosec B608
    # public really was purged (schema-qualified, so this does not depend on any search path either)
    assert _scalar(seeded, "SELECT COUNT(*) FROM public.organizations WHERE id = :o", o=ORG_A) == 0
    assert _scalar(seeded, "SELECT COUNT(*) FROM public.acs_events WHERE customer_id = :c", c=CUSTOMER_A) == 0
    assert _scalar(seeded, "SELECT COUNT(*) FROM public.checkins_clean WHERE customer_id = :c", c=CUSTOMER_A) == 0
    assert _scalar(seeded, "SELECT COUNT(*) FROM public.customers WHERE id = :c", c=CUSTOMER_A) == 0
    assert _scalar(seeded, "SELECT COUNT(*) FROM public.organizations WHERE id = :o", o=ORG_B) == 1
    assert [row[0] for row in _rows_of(seeded, "SELECT event_type FROM public.tenant_lifecycle_events ORDER BY id")] == [
        "access_cutoff", "purge_executed"]
    # No statement the tool issued named the shadow schema, and none ran before the path was pinned.
    assert not any(SHADOW_SCHEMA in s for s in statements)


def test_if_the_search_path_were_not_pinned_the_resolution_check_refuses(seeded, tool, shadowed, monkeypatch):
    # The second line of defence, exercised by disabling the first: with the caller's path left in force, the tool must
    # notice that the names no longer resolve to the objects whose ownership it proved -- not read or delete from them.
    _offboard(monkeypatch, seeded)
    public_before = _counts(seeded)
    monkeypatch.setattr(tool, "pin_search_path", lambda conn: None)

    for extra in ((), _execute_args()):
        code, out = _purge_cli(tool, shadowed, *extra)

        assert code == 2, out
        assert ("REFUSED: the search path does not resolve these tables to schema public: "
                "acs_events, checkins_clean, organizations. ") in out
        assert "WOULD BE ACCEPTED" not in out and "Rows in scope" not in out and "LIVE DATABASE PURGE COMPLETE" not in out

    assert _counts(seeded) == public_before and _shadow_state(seeded) == _SHADOW_UNTOUCHED


def test_a_temporary_table_cannot_shadow_a_real_one_either(seeded, tool, pg_url):
    # pg_temp is searched FIRST unless it is named in the path; the pin names it last.
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn, conn.begin():
            conn.execute(text("CREATE TEMP TABLE acs_events (id INTEGER)"))
            resolves_to_temp = text("SELECT to_regclass('acs_events') = to_regclass('pg_temp.acs_events')")
            resolves_to_public = text("SELECT to_regclass('acs_events') = to_regclass('public.acs_events')")
            assert conn.execute(resolves_to_temp).scalar() and not conn.execute(resolves_to_public).scalar()
            assert tool.unresolved_tables(conn, tool.ownership_tables()) == ["acs_events"]

            tool.pin_search_path(conn)

            assert conn.execute(resolves_to_public).scalar() and not conn.execute(resolves_to_temp).scalar()
            assert tool.unresolved_tables(conn, tool.ownership_tables()) == []
    finally:
        engine.dispose()


def _rows_of(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).all()


# ======================================================================================================================
# the append-only guard: against the purge, and across a downgrade
# ======================================================================================================================

def test_the_purge_cannot_delete_lifecycle_rows_even_if_its_plan_were_wrong(seeded, tool, pg_url, monkeypatch):
    # The registry keeps tenant_lifecycle_events out of the purge plan. This proves the second line of defence: a plan
    # that wrongly included it is stopped by the trigger, and the whole purge rolls back.
    _offboard(monkeypatch, seeded)
    before = _counts(seeded)
    wrong_plan = (*policy.purge_plan(), policy.SurfacePolicy(
        "tenant_lifecycle_events", policy.PURGE, "evidence", "n/a", "organization_id = :organization_id", 99))
    monkeypatch.setattr(tool, "purge_plan", lambda: wrong_plan)

    with pytest.raises(Exception, match="tenant_lifecycle_events is append-only: DELETE is not permitted"):
        _purge_cli(tool, pg_url, *_execute_args())

    assert _counts(seeded) == before  # not one row of anything was deleted
    assert _scalar(seeded, "SELECT status FROM organizations WHERE id = :o", o=ORG_A) == "cancelled"


def test_downgrade_drops_the_table_with_its_trigger_and_function_and_upgrade_restores_them(seeded, pg_url, role):
    # Runs last in this module: the downgrade drops the table (rows and grants with it).
    with seeded.begin() as conn:
        _record(conn)  # a row present: the row trigger must not get in the way of DROP TABLE

    def objects():
        with seeded.connect() as conn:
            return (
                conn.execute(text("SELECT to_regclass('public.tenant_lifecycle_events') IS NOT NULL")).scalar(),
                conn.execute(text("SELECT COUNT(*) FROM pg_trigger WHERE tgname = 'trg_tenant_lifecycle_events_append_only'")).scalar(),
                conn.execute(text("SELECT COUNT(*) FROM pg_proc WHERE proname = 'tenant_lifecycle_events_append_only'")).scalar(),
                conn.execute(text("SELECT to_regclass('public.tenant_lifecycle_events_id_seq') IS NOT NULL")).scalar(),
            )

    assert objects() == (True, 1, 1, True)
    try:
        down = _alembic(pg_url, "downgrade", "0d1dcae29e32")
        assert down.returncode == 0, down.stderr[-2000:]
        assert objects() == (False, 0, 0, False)  # nothing left behind: no orphaned trigger function or sequence
    finally:
        up = _alembic(pg_url, "upgrade", "head")
        assert up.returncode == 0, up.stderr[-2000:]
    assert objects() == (True, 1, 1, True)
    assert _scalar(seeded, "SELECT COUNT(*) FROM tenant_lifecycle_events") == 0
    with seeded.connect() as conn, pytest.raises(Exception, match="append-only"), conn.begin():
        _record(conn)
        conn.execute(text("DELETE FROM tenant_lifecycle_events"))  # the restored trigger works
    # The throwaway role is not the literal sortview_app, so the migration granted it nothing: after the round trip it
    # holds no privilege at all on the new table -- a fresh table is closed to the runtime role until granted.
    with seeded.connect() as conn:
        assert [d for d in privileges.differences(conn, role) if "tenant_lifecycle_events" in d] == [
            "tenant_lifecycle_events: INSERT is missing", "sequence tenant_lifecycle_events_id_seq: USAGE is missing"]
