"""C6a M3a on a REAL PostgreSQL: scripts/reconcile_ingest_keys.py inventory, against real redemptions and real row level security.

OPT-IN AND SAFE BY CONSTRUCTION, the same convention as tests/test_key_stamping_and_recovery_postgres.py: it runs only when
SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION server it may create and drop databases on. A
throwaway database and roles are created and dropped. The host must be local unless SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE=1 is also
set. The API's own DATABASE_URL is never read. Every value is SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from scripts import reconcile_ingest_keys as tool
from scripts import runtime_role_privileges as privileges
from services import collector_enrollment_service as enrollment
from services import collector_rules, ingest_v2_service

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("SORTVIEW_TEST_POSTGRES_URL")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ROLE_PASSWORD = secrets.token_urlsafe(24)  # throwaway, this session only

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")

READY = {"collector_timezone": "America/Chicago", "collector_rules": collector_rules.rules_review_marker(
    reviewed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), reviewed_by_user_id=5)}
# org 1 "acme": customer 10, branches 1 and 3. org 2 "other": customer 20, branch 2.
OWNER, SECOND, AT_OTHER_BRANCH = 101, 102, 301
KEY = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
SECOND_KEY = "8d9e0f1a-2b3c-4d4e-9f5a-6b7c8d9e0f1a"
INSTANT = datetime(2026, 10, 12, 15, 30, 1, 123456, tzinfo=UTC)
LATER = INSTANT + timedelta(minutes=5)
PRE_M1_REVISION = "f7b3d2c81a46"


# --- a throwaway database and roles ------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cluster():
    admin = make_url(ADMIN_URL)
    host = admin.host or ""
    if host not in LOCAL_HOSTS and os.environ.get("SORTVIEW_TEST_POSTGRES_ALLOW_REMOTE") != "1":
        pytest.fail(f"refusing to run against non-local PostgreSQL host {host!r}")
    name = f"sortview_m3a_test_{secrets.token_hex(4)}"
    suffix = secrets.token_hex(4)
    roles = {kind: f"sortview_m3a_{kind}_{suffix}" for kind in ("runtime", "reader", "partial")}
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # nosec B608 - generated name, no user input
    url = admin.set(database=name)
    owner = create_engine(url, hide_parameters=True)
    created = False
    try:
        _alembic(url, "upgrade", "head")
        with owner.begin() as conn:
            conn.execute(text(f"CREATE ROLE {roles['runtime']} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{ROLE_PASSWORD}'"))  # nosec B608
            # What an operator might be given: reads everything, bypasses row level security, and can write nothing.
            conn.execute(text(f"CREATE ROLE {roles['reader']} LOGIN NOSUPERUSER BYPASSRLS PASSWORD '{ROLE_PASSWORD}'"))  # nosec B608
            conn.execute(text(f"CREATE ROLE {roles['partial']} LOGIN NOSUPERUSER BYPASSRLS PASSWORD '{ROLE_PASSWORD}'"))  # nosec B608
            created = True
            for role in roles.values():
                conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO {role}'))  # nosec B608
            for line in privileges.provisioning_sql(roles["runtime"]).splitlines():
                if line and not line.startswith("--") and line not in ("BEGIN;", "COMMIT;"):
                    conn.execute(text(line))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {roles['reader']}, {roles['partial']}"))  # nosec B608
            conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {roles['reader']}"))  # nosec B608
            conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {roles['partial']}"))  # nosec B608
            conn.execute(text(f"REVOKE SELECT ON agent_tokens FROM {roles['partial']}"))  # nosec B608
        role_urls = {kind: url.set(username=role, password=ROLE_PASSWORD).render_as_string(hide_password=False)
                     for kind, role in roles.items()}
        yield {"owner": owner, "url": url, "owner_url": url.render_as_string(hide_password=False), **role_urls}
    finally:
        if created:
            with owner.begin() as conn:
                for role in roles.values():
                    conn.execute(text(f"DROP OWNED BY {role}"))  # nosec B608
                    conn.execute(text(f"DROP ROLE IF EXISTS {role}"))  # nosec B608
        owner.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # nosec B608
        admin_engine.dispose()


def _alembic(url, *arguments):
    done = subprocess.run(  # nosec B603
        [sys.executable, "-m", "alembic", *arguments], cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]


@pytest.fixture
def db(cluster):
    owner = cluster["owner"]
    with owner.begin() as conn:
        conn.execute(text(
            "TRUNCATE auth_audit_log, tenant_lifecycle_events, v2_cutovers, ingest_key_ids, checkin_events, reject_events, "
            "acs_item_events, checkins, collector_enrollment_codes, agent_tokens, collector_installations, organization_settings, "
            "branch_settings, pipeline_status, branches, organizations, customers, app_users RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("INSERT INTO app_users (id, email, full_name, is_platform_admin) "
                          "VALUES (7, 'admin@example.invalid', 'Test Admin', TRUE)"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Acme'), (20, 'Other')"))
        conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id, created_at) VALUES "
                          "(1, 'acme', 'Acme', 'active', 10, '2026-01-01T00:00:00+00:00'), "
                          "(2, 'other', 'Other', 'active', 20, '2026-01-01T00:00:00+00:00')"))
        conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id, "
                          "onboarding_status) VALUES (1, 1, 'main', 'Main', TRUE, 'active', 1, 'ready'), "
                          "(3, 1, 'north', 'North', FALSE, 'active', 3, 'ready'), (2, 2, 'main', 'Other Main', TRUE, 'active', 2, 'ready')"))
        conn.execute(text("INSERT INTO organization_settings (organization_id, settings_json) VALUES "
                          "(1, CAST(:s AS JSONB)), (2, CAST(:s AS JSONB))"), {"s": json.dumps(READY)})
        conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) VALUES "
                          "(101, 1, 1, 'Owner', 'active', 2), (102, 1, 1, 'Second', 'active', 2), "
                          "(301, 1, 3, 'North', 'active', 2), (201, 2, 2, 'Other', 'active', 2)"))
    return owner


def _token(conn, installation_id, *, customer=10, branch=1, is_active=True, secret="synthetic"):
    conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id) "
                      "VALUES (:h, :c, :b, 'synthetic', :a, :i)"),
                 {"h": hashlib.sha256(f"{secret}{secrets.token_hex(8)}".encode()).hexdigest(), "c": customer, "b": branch,
                  "a": is_active, "i": installation_id})


def _key(conn, key=KEY, *, created_at=INSTANT, status="active", owner=None, customer=10, branch=1):
    conn.execute(text("INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, status, retired_at, created_at, installation_id) "
                      "VALUES (:k, :c, :b, :s, CASE WHEN :s = 'retired' THEN now() END, :t, :i)"),
                 {"k": key, "c": customer, "b": branch, "s": status, "t": created_at, "i": owner})


def _code(conn, code_id=900, *, installation=OWNER, used_at=INSTANT, contract=2):
    conn.execute(text("INSERT INTO collector_enrollment_codes (id, installation_id, code_hash, expires_at, used_at, required_contract) "
                      "VALUES (:id, :i, :h, now() + interval '1 day', :u, :r)"),
                 {"id": code_id, "i": installation, "h": hashlib.sha256(secrets.token_bytes(8)).hexdigest(), "u": used_at, "r": contract})


def _cutover(conn, *, set_at=INSTANT, set_by="collector-enrollment",
             note="initial v2 cutover | enrollment_code_id=900 installation_id=101", customer=10, branch=1):
    conn.execute(text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, set_at, note) "
                      "VALUES (:c, :b, :t, :s, :a, :n)"), {"c": customer, "b": branch, "t": INSTANT, "s": set_by, "a": set_at, "n": note})


def _redeem(owner, installation_id=OWNER, *, clear_owner=False):
    """A REAL enrollment. With clear_owner, the key is as a redemption before C6a M2 left it: everything but the owner."""
    with owner.begin() as conn:
        code = enrollment.create_enrollment_code(conn, installation_id, created_by_user_id=7, expected_organization_id=1)
    with owner.begin() as conn:
        key_id = enrollment.redeem_enrollment_code(conn, code["enrollment_code"], contract_version=2)["key_id"]
        if clear_owner:
            conn.execute(text("UPDATE ingest_key_ids SET installation_id = NULL WHERE key_id = :k"), {"k": key_id})
    return key_id


def _run(url, *arguments):
    out = io.StringIO()
    code = tool.main(["inventory", "--database-url", url, *arguments], out=out)
    return code, out.getvalue()


def _report(cluster, url_kind="owner_url"):
    code, out = _run(cluster[url_kind], "--format", "json")
    assert code == 0, out
    return json.loads(out)


def _one_key(report, key_id=None):
    return next(row for row in report["keys"] if key_id is None or row["key_id"] == key_id)


def _branch(report, customer=10, branch=1):
    return next(row for row in report["branches"] if row["customer_id"] == customer and row["branch_id"] == branch)


def _fingerprint(owner):
    """Every public table's every row and row version. A write, or a row lock (xmax), to any of them changes this."""
    with owner.connect() as conn:
        tables = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")).scalars().all()
        assert len(tables) > 20
        return {table: conn.execute(text(  # nosec B608 - names from the catalog
            f"SELECT md5(COALESCE(string_agg(t::text || t.xmin::text || t.xmax::text, ',' ORDER BY t.ctid), '')) FROM public.{table} t"
        )).scalar() for table in tables}


# --- a real redemption is what E1 describes ------------------------------------------------------------------------------------

def test_a_real_redemption_writes_the_three_equal_instants_e1_reads(db):
    key_id = _redeem(db, clear_owner=True)

    with db.connect() as conn:
        row = conn.execute(text(
            "SELECT k.created_at AS key_at, c.set_at AS cutover_at, e.used_at AS code_at, c.set_by, c.note "
            "FROM ingest_key_ids k JOIN v2_cutovers c ON c.branch_id = k.branch_id "
            "JOIN collector_enrollment_codes e ON e.installation_id = :i WHERE k.key_id = :k"
        ), {"k": key_id, "i": OWNER}).mappings().one()

    assert row["key_at"] == row["cutover_at"] == row["code_at"]
    assert row["set_by"] == tool.ENROLLMENT_SET_BY and tool._ENROLLMENT_NOTE.fullmatch(row["note"])


def test_a_real_enrollment_before_m2_is_a_candidate_with_an_unknown_owner_and_no_decision(db, cluster):
    key_id = _redeem(db, clear_owner=True)

    key = _one_key(_report(cluster), key_id)

    assert key["owner_state"] == "unknown" and key["owner_installation_id"] is None
    assert key["e1"]["status"] == "candidate" and key["e1"]["candidate_installation_id"] == OWNER and key["e1"]["conclusive"] is False
    with db.connect() as conn:
        assert conn.execute(text("SELECT installation_id FROM ingest_key_ids WHERE key_id = :k"), {"k": key_id}).scalar() is None


def test_a_real_enrollment_after_m2_has_its_owner_recorded_and_the_evidence_agrees(db, cluster):
    key_id = _redeem(db)

    key = _one_key(_report(cluster), key_id)

    assert key["owner_state"] == "recorded" and key["owner_installation_id"] == OWNER
    assert key["e1"]["status"] == "candidate" and "owner_differs_from_e1_candidate" not in key["flags"]


def test_a_heartbeat_writing_both_rows_in_one_transaction_coincides_with_its_installation(db, cluster):
    key_id = _redeem(db, clear_owner=True)
    with db.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET last_heartbeat_at = CURRENT_TIMESTAMP WHERE key_id = :k"), {"k": key_id})
        conn.execute(text("UPDATE collector_installations SET last_seen_at = CURRENT_TIMESTAMP WHERE id = :i"), {"i": OWNER})

    key = _one_key(_report(cluster), key_id)

    assert key["heartbeat_coincides_with_installation_ids"] == [OWNER] and key["e1"]["status"] == "candidate"


# --- unknown ownership and duplicates --------------------------------------------------------------------------------------------

def test_a_key_issued_by_the_real_script_path_has_no_owner_and_no_evidence(db, cluster):
    with db.begin() as conn:
        key_id = ingest_v2_service.issue_ingest_key(conn, 10, 1, installation_id=None)

    key = _one_key(_report(cluster), key_id)

    assert key["owner_state"] == "unknown" and key["e1"]["status"] == "none" and key["e1"]["reasons"] == ["no_enrollment_cutover"]


def test_active_and_retired_keys_with_no_owner_are_both_reported_and_a_retired_one_is_not_attributed(db, cluster):
    with db.begin() as conn:
        retired = ingest_v2_service.issue_ingest_key(conn, 10, 1, installation_id=None)
        ingest_v2_service.retire_ingest_key(conn, retired)
        active = ingest_v2_service.issue_ingest_key(conn, 10, 3, installation_id=None)
        _token(conn, OWNER)

    report = _report(cluster)

    assert _one_key(report, retired)["status"] == "retired" and _one_key(report, retired)["owner_installation_id"] is None
    assert _one_key(report, retired)["e1"]["status"] == "none"
    assert _one_key(report, active)["status"] == "active"
    assert report["summary"]["keys_owner_unknown_active"] == report["summary"]["keys_owner_unknown_retired"] == 1


def test_a_second_active_key_and_a_branch_with_no_active_key_are_reported_and_nothing_is_chosen(db, cluster):
    with db.begin() as conn:
        first, second = KEY, SECOND_KEY  # only by hand now: issue_ingest_key refuses a second active key
        _key(conn, first)
        _key(conn, second)
        retired = ingest_v2_service.issue_ingest_key(conn, 10, 3, installation_id=None)
        ingest_v2_service.retire_ingest_key(conn, retired)

    report = _report(cluster)

    assert _branch(report)["key_state"] == "multiple_active" and set(_branch(report)["active_key_ids"]) == {first, second}
    assert "multiple_active_keys" in _branch(report)["flags"]
    assert all("several_active_keys_at_branch" in _one_key(report, key)["flags"] for key in (first, second))
    assert _branch(report, 10, 3)["key_state"] == "retired_only" and "no_active_key_after_retirement" in _branch(report, 10, 3)["flags"]
    assert _branch(report, 20, 2)["key_state"] == "none_issued"
    assert report["summary"]["branches_multiple_active_keys"] == 1
    assert report["summary"]["branches_no_active_key_after_retirement"] == 1
    assert report["summary"]["branches_no_key_ever_issued"] == 1


# --- misleading timestamps and forged notes ------------------------------------------------------------------------------------

def test_an_operator_cutover_at_the_keys_exact_instant_is_not_a_candidate(db, cluster):
    with db.begin() as conn:
        _key(conn)
        _token(conn, OWNER)
        _code(conn)
        _cutover(conn, set_by="an operator", note="pilot cutover")

    e1 = _one_key(_report(cluster))["e1"]

    assert e1["status"] == "incomplete" and e1["reasons"] == ["cutover_instant_matches_without_enrollment_record"]
    assert e1["candidate_installation_id"] is None


def test_equal_timestamps_at_another_branch_mean_nothing_here(db, cluster):
    with db.begin() as conn:
        _key(conn)
        _token(conn, OWNER)
        _code(conn)
        _cutover(conn, branch=3)

    assert _one_key(_report(cluster))["e1"]["status"] == "none"


def test_a_later_key_at_an_enrolled_branch_gets_none_of_its_evidence(db, cluster):
    enrolled = _redeem(db, clear_owner=True)
    later = KEY
    with db.begin() as conn:
        ingest_v2_service.retire_ingest_key(conn, enrolled)
        _key(conn, later, created_at=LATER)  # only by hand now: issue_ingest_key refuses an enrolled branch

    report = _report(cluster)

    assert _one_key(report, enrolled)["e1"]["status"] == "candidate"
    assert _one_key(report, later)["e1"]["status"] == "incomplete"
    assert _one_key(report, later)["e1"]["reasons"] == ["enrollment_cutover_instant_differs"]


@pytest.mark.parametrize(("note", "status", "reason"), [
    pytest.param("initial v2 cutover | enrollment_code_id=999 installation_id=101", "incomplete", "enrollment_code_missing", id="no-such-code"),
    pytest.param("initial v2 cutover | enrollment_code_id=900 installation_id=102", "conflicting", "code_names_another_installation",
                 id="another-installation"),
    pytest.param("initial v2 cutover | enrollment_code_id=901 installation_id=301", "conflicting", "installation_at_another_branch",
                 id="installation-at-another-branch"),
    pytest.param("initial v2 cutover | enrollment_code_id=902 installation_id=101", "conflicting", "code_used_at_differs", id="code-at-another-instant"),
    pytest.param("initial v2 cutover | enrollment_code_id=903 installation_id=101", "conflicting", "code_is_not_contract_2", id="contract-1-code"),
    pytest.param("initial v2 cutover | enrollment_code_id=900 installation_id=999", "conflicting", "code_names_another_installation", id="no-such-installation"),
    pytest.param("initial v2 cutover | enrollment_code_id=900 installation_id=101; DROP TABLE x", "incomplete",
                 "cutover_instant_matches_without_enrollment_record", id="trailing-text"),
])
def test_a_forged_enrollment_note_is_refused_as_a_candidate(db, cluster, note, status, reason):
    with db.begin() as conn:
        _key(conn)
        _token(conn, OWNER)
        _code(conn)
        _code(conn, 901, installation=AT_OTHER_BRANCH)
        _code(conn, 902, used_at=LATER)
        _code(conn, 903, contract=1)
        _cutover(conn, note=note)

    e1 = _one_key(_report(cluster))["e1"]

    assert e1["status"] == status and reason in e1["reasons"] and e1["candidate_installation_id"] is None


def test_the_real_cutover_tool_can_forge_the_note_but_not_the_instant(db, cluster):
    with db.begin() as conn:
        _key(conn)
        _token(conn, OWNER)
        _code(conn)
        ingest_v2_service.record_v2_cutover(conn, 10, 1, INSTANT, "collector-enrollment",
                                            note="initial v2 cutover | enrollment_code_id=900 installation_id=101")

    e1 = _one_key(_report(cluster))["e1"]

    assert e1["status"] == "incomplete" and e1["reasons"] == ["enrollment_cutover_instant_differs"]


def test_a_forgery_of_every_row_is_still_only_a_candidate_for_review(db, cluster):
    """The limit E1 is honest about: someone who can write all of these can pass it, so it is never reported as proof."""
    with db.begin() as conn:
        _key(conn)
        _token(conn, OWNER)
        _code(conn)
        _cutover(conn)

    report = _report(cluster)
    e1 = _one_key(report)["e1"]

    assert e1["status"] == "candidate" and e1["conclusive"] is False and "not proof" in report["notice"]
    assert _one_key(report)["owner_installation_id"] is None


# --- ambiguous relationships -----------------------------------------------------------------------------------------------------

def test_one_record_fitting_two_keys_identifies_neither(db, cluster):
    with db.begin() as conn:
        _key(conn)
        _key(conn, SECOND_KEY)
        _token(conn, OWNER)
        _code(conn)
        _cutover(conn)

    report = _report(cluster)

    for key_id in (KEY, SECOND_KEY):
        assert _one_key(report, key_id)["e1"]["status"] == "ambiguous"
        assert _one_key(report, key_id)["e1"]["candidate_installation_id"] is None


def test_two_enrollment_cutovers_at_a_branch_are_ambiguous(db, cluster):
    with db.begin() as conn:
        _key(conn)
        _token(conn, OWNER)
        _code(conn)
        _cutover(conn)
        _cutover(conn, set_at=LATER)

    e1 = _one_key(_report(cluster))["e1"]

    assert e1["status"] == "ambiguous" and e1["reasons"] == ["several_enrollment_cutovers_at_this_branch"]


def test_a_heartbeat_written_with_another_installation_makes_a_real_candidate_ambiguous(db, cluster):
    key_id = _redeem(db, clear_owner=True)
    with db.begin() as conn:
        conn.execute(text("UPDATE ingest_key_ids SET last_heartbeat_at = CURRENT_TIMESTAMP WHERE key_id = :k"), {"k": key_id})
        conn.execute(text("UPDATE collector_installations SET last_seen_at = CURRENT_TIMESTAMP WHERE id = :i"), {"i": SECOND})

    e1 = _one_key(_report(cluster), key_id)["e1"]

    assert e1["status"] == "ambiguous" and e1["reasons"] == ["heartbeat_coincides_with_another_installation"]


def test_several_installations_with_tokens_and_no_evidence_stay_unknown_and_flagged(db, cluster):
    with db.begin() as conn:
        _key(conn)
        _token(conn, OWNER)
        _token(conn, SECOND)
        _token(conn, None)

    report = _report(cluster)
    key = _one_key(report)

    assert key["owner_state"] == "unknown" and key["e1"]["candidate_installation_id"] is None
    assert {"several_live_installations_at_branch", "active_unbound_tokens_at_branch"} <= set(key["flags"])
    assert {"active_tokens_bound_to_several_installations", "several_live_installations", "active_unbound_tokens"} <= set(_branch(report)["flags"])
    assert [(row["binding"], row["installation_id"]) for row in _branch(report)["tokens"]] == [("bound", OWNER), ("bound", SECOND), ("unbound", None)]


def test_an_active_key_owned_by_a_retired_installation_is_flagged(db, cluster):
    with db.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET status = 'retired' WHERE id = :i"), {"i": OWNER})
        _key(conn, owner=OWNER)

    assert "owner_installation_not_usable" in _one_key(_report(cluster))["flags"]


def test_a_recorded_owner_disagreeing_with_a_candidate_is_flagged_not_changed(db, cluster):
    with db.begin() as conn:
        _key(conn, owner=SECOND)
        _token(conn, OWNER)
        _code(conn)
        _cutover(conn)

    key = _one_key(_report(cluster))

    assert key["owner_installation_id"] == SECOND and "owner_differs_from_e1_candidate" in key["flags"]


def test_a_rollback_and_a_cancelled_organization_are_flagged(db, cluster):
    with db.begin() as conn:
        _key(conn)
        conn.execute(text("INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by) VALUES (10, 1, NULL, 'an operator')"))
        conn.execute(text("UPDATE organizations SET status = 'cancelled' WHERE id = 1"))

    report = _report(cluster)

    assert {"latest_cutover_is_a_rollback", "organization_cancelled"} <= set(_branch(report)["flags"])
    assert "organization_cancelled" in _one_key(report)["flags"]


def test_event_counts_are_per_key_with_no_event_content(db, cluster):
    with db.begin() as conn:
        _key(conn)
        conn.execute(text("INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, destination, bin) VALUES "
                          "(10, 1, :k, :e, now(), 'westside', '3')"), {"k": KEY, "e": "ab" * 32})

    events = _one_key(_report(cluster))["events"]

    assert events["count"] == 1 and events["count_under_another_scope"] == 0 and set(events) == {
        "count", "last_received_at", "count_under_another_scope"}


# --- no data changes ----------------------------------------------------------------------------------------------------------------

def test_the_inventory_changes_no_row_and_locks_no_row_in_either_format(db, cluster):
    key_id = _redeem(db, clear_owner=True)
    with db.begin() as conn:
        _token(conn, None)
    before = _fingerprint(db)

    for output_format in ("text", "json"):
        code, _out = _run(cluster["owner_url"], "--format", output_format)
        assert code == 0
    _report(cluster, "reader")

    assert _fingerprint(db) == before
    with db.connect() as conn:
        assert conn.execute(text("SELECT installation_id FROM ingest_key_ids WHERE key_id = :k"), {"k": key_id}).scalar() is None


def test_the_transaction_really_refuses_writes_and_row_locks(db, cluster):
    for statement in ("UPDATE ingest_key_ids SET installation_id = 101", "SELECT id FROM ingest_key_ids FOR UPDATE",
                      "INSERT INTO customers (id, name) VALUES (99, 'x')"):
        with create_engine(cluster["owner_url"]).connect() as conn:
            tool.open_snapshot(conn)
            with pytest.raises(DBAPIError) as refused:
                conn.execute(text(statement))
            assert refused.value.orig.pgcode == "25006"
            conn.rollback()


def test_the_settings_end_with_the_transaction(db, cluster):
    engine = create_engine(cluster["owner_url"], pool_size=1, max_overflow=0)
    with engine.connect() as conn:
        tool.open_snapshot(conn)
        conn.rollback()
        assert conn.execute(text("SHOW row_security")).scalar() == "on"
        assert conn.execute(text("SHOW transaction_read_only")).scalar() == "off"
    engine.dispose()


def test_the_report_is_one_snapshot(db, cluster, monkeypatch):
    with db.begin() as conn:
        _key(conn)
    real = tool._rows
    calls = []

    def commits_a_token_during_the_inventory(conn, sql, **params):
        rows = real(conn, sql, **params)
        if not calls:  # after the first read of data, a token commits on another connection
            calls.append(sql)
            with db.begin() as other:
                _token(other, None)
        return rows

    monkeypatch.setattr(tool, "_rows", commits_a_token_during_the_inventory)
    code, out = _run(cluster["owner_url"], "--format", "json")
    monkeypatch.setattr(tool, "_rows", real)

    assert code == 0 and _branch(json.loads(out))["tokens"] == []
    assert len(_branch(_report(cluster))["tokens"]) == 1


# --- incomplete visibility ------------------------------------------------------------------------------------------------------------

def test_a_role_under_row_level_security_is_refused_and_never_reports_an_empty_registry(db, cluster):
    with db.begin() as conn:
        _key(conn)

    code, out = _run(cluster["runtime"], "--format", "json")
    document = json.loads(out)

    assert code == 2 and document["complete"] is False and document["error"]["code"] == "incomplete_visibility"
    assert "keys" not in document and "summary" not in document


def test_a_role_that_cannot_read_a_table_is_refused_and_named(db, cluster):
    code, out = _run(cluster["partial"])

    assert code == 2 and "incomplete_visibility" in out and "agent_tokens" in out and "SUMMARY" not in out


def test_a_role_with_full_visibility_sees_what_the_owner_sees(db, cluster):
    key_id = _redeem(db, clear_owner=True)
    with db.begin() as conn:
        _key(conn, SECOND_KEY, branch=3)

    reader, owner = _report(cluster, "reader"), _report(cluster)

    assert reader["database"]["superuser"] is False and reader["database"]["bypass_rls"] is True
    assert reader["keys"] == owner["keys"] and reader["branches"] == owner["branches"]
    assert {row["key_id"] for row in reader["keys"]} == {key_id, SECOND_KEY}


# --- schema -------------------------------------------------------------------------------------------------------------------------------

def test_a_migrated_database_has_no_schema_differences(db, cluster):
    report = _report(cluster)

    assert report["schema"]["differences"] == [] and report["summary"]["schema_differences"] == 0
    assert report["database"]["alembic_revisions"]
    names = {row["name"] for row in report["schema"]["indexes"]}
    assert "ingest_key_ids_key_id_uidx" in names
    assert {row["name"] for row in report["schema"]["policies"] if row["table_name"] == "ingest_key_ids"} == {
        "tenant_isolation_select", "tenant_isolation_insert", "tenant_isolation_update"}


@pytest.mark.parametrize(("change", "undo", "difference"), [
    pytest.param("CREATE UNIQUE INDEX m3a_extra_uidx ON ingest_key_ids (customer_id, branch_id) WHERE status = 'active'",
                 "DROP INDEX m3a_extra_uidx", "ingest_key_ids: indexes unexpected: m3a_extra_uidx", id="extra-index"),
    pytest.param("ALTER TABLE ingest_key_ids DROP CONSTRAINT fk_ingest_key_ids_installation_branch",
                 "ALTER TABLE ingest_key_ids ADD CONSTRAINT fk_ingest_key_ids_installation_branch "
                 "FOREIGN KEY (installation_id, branch_id) REFERENCES collector_installations (id, branch_id)",
                 "ingest_key_ids: constraints missing: fk_ingest_key_ids_installation_branch", id="m1-foreign-key-missing"),
    pytest.param("CREATE TRIGGER m3a_extra BEFORE UPDATE ON ingest_key_ids FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
                 "DROP TRIGGER m3a_extra ON ingest_key_ids", "ingest_key_ids: triggers unexpected: m3a_extra", id="extra-trigger"),
    pytest.param("ALTER POLICY tenant_isolation_update ON ingest_key_ids RENAME TO m3a_renamed",
                 "ALTER POLICY m3a_renamed ON ingest_key_ids RENAME TO tenant_isolation_update",
                 "ingest_key_ids: policies missing: tenant_isolation_update", id="policy-gone"),
    pytest.param("ALTER TABLE ingest_key_ids DISABLE ROW LEVEL SECURITY", "ALTER TABLE ingest_key_ids ENABLE ROW LEVEL SECURITY",
                 "ingest_key_ids: row level security is not enabled", id="rls-off"),
    pytest.param("ALTER TABLE ingest_key_ids FORCE ROW LEVEL SECURITY", "ALTER TABLE ingest_key_ids NO FORCE ROW LEVEL SECURITY",
                 "ingest_key_ids: row level security is forced", id="rls-forced"),
])
def test_a_schema_difference_is_reported_not_repaired(db, cluster, change, undo, difference):
    with db.begin() as conn:
        conn.execute(text(change))
    try:
        report = _report(cluster)
        code, out = _run(cluster["owner_url"])
    finally:
        with db.begin() as conn:
            conn.execute(text(undo))

    assert difference in report["schema"]["differences"]
    assert code == 0 and f"difference: {difference}" in out


def test_the_inventory_refuses_a_database_without_m1(db, cluster):
    _alembic(cluster["url"], "downgrade", PRE_M1_REVISION)
    try:
        code, out = _run(cluster["owner_url"], "--format", "json")
    finally:
        _alembic(cluster["url"], "upgrade", "head")
    document = json.loads(out)

    assert code == 2 and document["error"]["code"] == "m1_schema_unavailable" and "keys" not in document


def test_a_missing_column_it_reads_is_refused_and_named(db, cluster):
    with db.begin() as conn:
        conn.execute(text("ALTER TABLE v2_cutovers RENAME COLUMN note TO m3a_note"))
    try:
        code, out = _run(cluster["owner_url"])
    finally:
        with db.begin() as conn:
            conn.execute(text("ALTER TABLE v2_cutovers RENAME COLUMN m3a_note TO note"))

    assert code == 2 and "schema_unsupported" in out and "v2_cutovers" in out and "note" in out and "SUMMARY" not in out


# --- errors fail closed --------------------------------------------------------------------------------------------------------------------

def test_an_error_mid_inventory_produces_no_report_and_a_fixed_message(db, cluster, monkeypatch):
    with db.begin() as conn:
        _key(conn)
    monkeypatch.setattr(tool, "_BRANCHES_SQL", "SELECT * FROM m3a_no_such_table")

    code, out = _run(cluster["owner_url"], "--format", "json")
    document = json.loads(out)

    assert code == 2 and document["error"] == {"code": "inventory_failed", "message": tool._REFUSALS["inventory_failed"], "sqlstate": "42P01"}
    assert "keys" not in document and "m3a_no_such_table" not in out and KEY not in out


def test_a_bad_password_reveals_nothing(db, cluster):
    url = make_url(cluster["reader"]).set(username="m3a_no_such_role", password="CANARY-bad-password")

    code, out = _run(url.render_as_string(hide_password=False))

    assert code == 2 and "connection_failed" in out and "CANARY" not in out and "m3a_no_such_role" not in out


# --- no secret, identifier or raw content ------------------------------------------------------------------------------------------------------

def test_no_credential_hostname_patron_or_raw_value_reaches_either_output(db, cluster):
    token_hash = hashlib.sha256(b"CANARY-raw-agent-token").hexdigest()
    code_hash = hashlib.sha256(b"CANARY-raw-enrollment-code").hexdigest()
    event_key, item_key = "ce" * 32, "cf" * 32
    canaries = [
        token_hash, code_hash, event_key, item_key, "CANARY-raw-agent-token", "CANARY-raw-enrollment-code",
        "CANARY-token-description", "CANARY-hostname.example.invalid", "CANARY-installation-name", "9.9.9-CANARY",
        "CANARY-operator", "CANARY-cutover-note", "CANARY-patron", "CANARY-barcode", "CANARY-title", "CANARY-raw-message",
        "westsidecanary", ROLE_PASSWORD,
    ]
    with db.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET name = 'CANARY-installation-name', "
                          "hostname = 'CANARY-hostname.example.invalid', collector_version = '9.9.9-CANARY' WHERE id = :i"), {"i": OWNER})
        conn.execute(text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id) "
                          "VALUES (:h, 10, 1, 'CANARY-token-description host CANARY-hostname.example.invalid', TRUE, :i)"),
                     {"h": token_hash, "i": OWNER})
        conn.execute(text("INSERT INTO collector_enrollment_codes (id, installation_id, code_hash, expires_at, used_at, required_contract) "
                          "VALUES (900, :i, :h, now(), :u, 2)"), {"i": OWNER, "h": code_hash, "u": INSTANT})
        _key(conn)
        _cutover(conn)
        _cutover(conn, set_by="CANARY-operator", note="CANARY-cutover-note", set_at=LATER)
        conn.execute(text("INSERT INTO checkin_events (customer_id, branch_id, key_id, event_key, event_time, item_key, destination, bin) "
                          "VALUES (10, 1, :k, :e, now(), :i, 'westsidecanary', '3')"), {"k": KEY, "e": event_key, "i": item_key})
        conn.execute(text("INSERT INTO acs_events (customer_id, branch_id, event_time, message_code, barcode, barcode_key, title, patron_id, raw_message) "
                          "VALUES (10, 1, now(), 'x', 'CANARY-barcode', 'k', 'CANARY-title', 'CANARY-patron', 'CANARY-raw-message')"))

    for kind, output_format in (("owner_url", "json"), ("owner_url", "text"), ("reader", "json"), ("runtime", "json"), ("partial", "text")):
        _exit_code, out = _run(cluster[kind], "--format", output_format)
        for canary in canaries:
            assert canary not in out, canary
    assert KEY in _run(cluster["owner_url"], "--format", "json")[1]  # the key_id itself is not a secret


def test_the_report_keys_name_no_credential_or_identifier(db, cluster):
    with db.begin() as conn:
        _key(conn)
        _token(conn, OWNER)

    def field_names(value):
        if isinstance(value, dict):
            for name, inner in value.items():
                yield name
                yield from field_names(inner)
        elif isinstance(value, list):
            for inner in value:
                yield from field_names(inner)

    report = _report(cluster)
    names = {name.lower() for name in field_names({"keys": report["keys"], "branches": report["branches"]})}

    assert not {name for name in names if any(word in name for word in (
        "hash", "secret", "password", "description", "hostname", "patron", "barcode", "note", "set_by", "name"))}


def test_the_text_output_is_readable(db, cluster):
    key_id = _redeem(db, clear_owner=True)

    code, out = _run(cluster["owner_url"])

    assert code == 0 and out.startswith("C6a INGEST KEY INVENTORY (read-only; nothing was changed)")
    assert key_id in out and f"E1 candidate (installation {OWNER}, requires review)" in out and "no differences" in out
