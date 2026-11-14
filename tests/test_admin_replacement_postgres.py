"""C6a R1 on a REAL PostgreSQL: machine replacement authorization and cancellation.

docs/c6a-replacement-machine-design.md, Amendment A (14.2-14.5, 14.9 R1). services.admin_installation_service runs as a
NON-OWNING, NOBYPASSRLS role holding exactly the recorded runtime baseline, so ingest_key_ids' row level security really
applies to the key-ownership checks. What only a real server can prove:

  * authorization adds one linked `provisioning` Contract 2 installation with its write-once attribution, audited, and
    changes nothing else -- no token, key, cutover or source status;
  * every eligibility refusal (14.3, 14.3a), each writing nothing;
  * at most one pending replacement per branch (D1), including under concurrency and the deactivate-authorize-reactivate
    sequence, against cancellation and against offboarding;
  * Cancel replacement retires it, revokes its unused codes and is audited; the general status change refuses to;
  * a code can be issued for a pending replacement, ordinary enrollment restrictions are unchanged, and the code R1
    issues is the one R2 redeems (tests/test_replacement_redemption*.py test the redemption itself);
  * the write-once trigger, the all-or-none and not-self CHECKs and the same-branch foreign key;
  * the general installation update can no longer move an installation out of retired;
  * a failure after the audit insert rolls back the event with its authorization or cancellation;
  * the migration round-trips from c1e7a94f2b60 on its own throwaway database, and its downgrade removes exactly what
    its upgrade added.

OPT-IN AND SAFE BY CONSTRUCTION, as tests/test_admin_installations_postgres.py, whose throwaway-database fixture this
module reuses: runs only when SORTVIEW_TEST_POSTGRES_URL points at a maintenance database on a NON-PRODUCTION, local
server. Every value is SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from test_admin_installations_postgres import (  # noqa: F401 -- `cluster` is a fixture
    ADMIN_URL,
    READY,
    cluster,
)

import database
from services import admin_installation_service as service
from services import (
    collector_enrollment_service,
    platform_admin_service,
    replacement_eligibility,
    tenant_service,
)
from services.admin_installation_service import InstallationAdminError
from services.collector_enrollment_service import EnrollmentError

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SORTVIEW_TEST_POSTGRES_URL is not set (opt-in PostgreSQL tests)")

ACTOR = {"id": 7, "email": "admin@example.invalid"}
OTHER_ADMIN = {"id": 8, "email": "second@example.invalid"}
SOURCE_TOKEN = "synthetic-agent-token-for-source-101"
UNBOUND_TOKEN = "synthetic-legacy-unbound-token-acme"
ACME_KEY = "1b2c3d4e-5f60-4718-9a2b-3c4d5e6f7a81"
OTHER_KEY = "2c3d4e5f-6071-4829-8b3c-4d5e6f7a8b92"
UNOWNED_KEY = "3d4e5f60-7182-493a-9c4d-5e6f7a8b9ca3"


@pytest.fixture
def db(cluster, monkeypatch):  # noqa: F811 -- the imported fixture, requested by name
    monkeypatch.setenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED", "true")  # 14.11: these tests replace machines
    owner, runtime = cluster
    with owner.begin() as conn:
        conn.execute(text(
            "TRUNCATE auth_audit_log, tenant_lifecycle_events, v2_cutovers, ingest_key_ids, collector_enrollment_codes, "
            "agent_tokens, collector_installations, organization_settings, branch_settings, branches, organizations, "
            "customers, app_users RESTART IDENTITY CASCADE"
        ))
        conn.execute(text(
            "INSERT INTO app_users (id, email, full_name, is_platform_admin) VALUES "
            "(7, 'admin@example.invalid', 'Test Admin', TRUE), (8, 'second@example.invalid', 'Second Admin', TRUE)"
        ))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Acme'), (11, 'Other'), (12, 'Fresh'), "
                          "(14, 'Legacy')"))
        conn.execute(text(
            "INSERT INTO organizations (id, slug, name, status, operational_customer_id, created_at) VALUES "
            "(1, 'acme', 'Acme Library', 'active', 10, '2026-01-01T00:00:00+00:00'), "
            "(2, 'other', 'Other Library', 'active', 11, '2026-01-01T00:00:00+00:00'), "
            "(3, 'fresh', 'Fresh Library', 'active', 12, '2026-01-01T00:00:00+00:00'), "
            "(4, 'legacy', 'Legacy Library', 'active', 14, '2026-01-01T00:00:00+00:00'), "
            "(6, 'unmapped', 'Unmapped Library', 'active', NULL, '2026-01-01T00:00:00+00:00')"
        ))
        conn.execute(text(
            "INSERT INTO branches (id, organization_id, slug, name, is_primary, status, operational_branch_id) VALUES "
            "(1, 1, 'main', 'Acme Main', TRUE, 'active', 1), (5, 1, 'east', 'Acme East', FALSE, 'active', 5), "
            "(2, 2, 'main', 'Other Main', TRUE, 'active', 2), (3, 3, 'main', 'Fresh Main', TRUE, 'active', 3), "
            "(4, 4, 'main', 'Legacy Main', TRUE, 'active', 4), (6, 6, 'main', 'Unmapped Main', TRUE, 'active', NULL)"
        ))
        # Every library but `fresh` has its Collector settings ready, so authorization reaches the rules it tests.
        conn.execute(text(
            "INSERT INTO organization_settings (organization_id, settings_json) VALUES "
            "(1, CAST(:ready AS JSONB)), (2, CAST(:ready AS JSONB)), (4, CAST(:ready AS JSONB)), (6, CAST(:ready AS JSONB))"
        ), {"ready": json.dumps(READY)})
        conn.execute(text(
            "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract) VALUES "
            "(101, 1, 1, 'Acme Sorter', 'active', 2), (102, 1, 1, 'Acme Legacy', 'inactive', 1), "
            "(103, 1, 1, 'Acme Retired', 'retired', 2), (105, 1, 5, 'Acme East Sorter', 'active', 2), "
            "(201, 2, 2, 'Other Sorter', 'active', 2), (301, 3, 3, 'Fresh Sorter', 'provisioning', 2), "
            "(401, 4, 4, 'Legacy Sorter', 'active', 2), (601, 6, 6, 'Unmapped Sorter', 'active', 2)"
        ))
        conn.execute(text("SELECT setval('collector_installations_id_seq', 1000)"))
        conn.execute(text(
            "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active, installation_id) "
            "VALUES (:hash, 10, 1, 'synthetic', TRUE, 101)"
        ), {"hash": hashlib.sha256(SOURCE_TOKEN.encode()).hexdigest()})
        # Acme and Other are enrolled on Contract v2: an active key owned by their Collector, and the cutover.
        # Legacy is enrolled with a key that has no recorded owner (issued before M1).
        conn.execute(text(
            "INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, installation_id) VALUES "
            "(:acme, 10, 1, 101), (:other, 11, 2, 201), (:unowned, 14, 4, NULL)"
        ), {"acme": ACME_KEY, "other": OTHER_KEY, "unowned": UNOWNED_KEY})
        conn.execute(text(
            "INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, note) VALUES "
            "(10, 1, '2026-01-01T00:00:00+00:00', 'collector-enrollment', 'enrolled'), "
            "(11, 2, '2026-01-01T00:00:00+00:00', 'collector-enrollment', 'enrolled'), "
            "(14, 4, '2026-01-01T00:00:00+00:00', 'collector-enrollment', 'enrolled')"
        ))
    monkeypatch.setattr(database, "_engine", runtime)  # the one flat engine every service uses
    return owner, runtime


# --- helpers -----------------------------------------------------------------------------------------------------------

def _library(slug):
    library = service.get_library(slug)
    assert library is not None
    return library


def _rows(owner, sql, **params):
    with owner.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _run(owner, sql, **params):
    with owner.begin() as conn:
        conn.execute(text(sql), params)


def _installation(owner, installation_id):
    (row,) = _rows(owner, "SELECT * FROM collector_installations WHERE id = :id", id=installation_id)
    return row


def _credentials(owner):
    """Everything R1 must never change: tokens, keys, cutovers and every pre-existing installation's status."""
    return (
        _rows(owner, "SELECT id, is_active, installation_id FROM agent_tokens ORDER BY id"),
        _rows(owner, "SELECT key_id, status, installation_id, retired_at FROM ingest_key_ids ORDER BY id"),
        _rows(owner, "SELECT * FROM v2_cutovers ORDER BY id"),
        _rows(owner, "SELECT id, status FROM collector_installations WHERE id < 1000 ORDER BY id"),
    )


def _audit(owner):
    return _rows(owner, "SELECT user_id, email, event_type, message, metadata FROM auth_audit_log ORDER BY id")


def _pending(owner, branch_id=1):
    return [row["id"] for row in _rows(owner, """
        SELECT ci.id FROM collector_installations ci
        WHERE ci.branch_id = :b AND ci.replaces_installation_id IS NOT NULL AND ci.status <> 'retired'
          AND NOT EXISTS (SELECT 1 FROM ingest_key_ids k WHERE k.installation_id = ci.id)
        ORDER BY ci.id""", b=branch_id)]


def _authorize(source=101, slug="acme", *, name="Replacement Sorter", confirm=False, actor=ACTOR):
    return service.authorize_replacement(_library(slug), source, name=name, confirm_lost_response=confirm, actor=actor)


def _refused(code, call):
    with pytest.raises(InstallationAdminError) as refused:
        call()
    assert refused.value.code == code


# =====================================================================================================================
# authorization
# =====================================================================================================================

def test_authorization_adds_one_linked_provisioning_installation_and_changes_nothing_else(db):
    owner, _ = db
    before = _credentials(owner)

    authorized = _authorize(name="  Replacement Sorter ")

    new_id = authorized["installation"]["id"]
    assert authorized["replaces_installation_id"] == 101
    assert authorized["installation"]["status"] == "provisioning" and authorized["installation"]["required_contract"] == 2
    row = _installation(owner, new_id)
    assert (row["organization_id"], row["branch_id"], row["name"], row["status"], row["required_contract"]) == (
        1, 1, "Replacement Sorter", "provisioning", 2)
    assert (row["replaces_installation_id"], row["replacement_authorized_by_user_id"]) == (101, 7)
    assert row["replacement_authorized_at"] is not None and row["installed_at"] is None and row["hostname"] is None
    assert _credentials(owner) == before  # no token, key, cutover or source status touched
    assert _audit(owner) == [{
        "user_id": 7, "email": "admin@example.invalid", "event_type": "admin_replacement_authorized",
        "message": "Collector replacement authorized.",
        "metadata": {"target_org_slug": "acme", "installation_id": new_id, "replaces_installation_id": 101,
                     "source_status": "active", "branch_slug": "main", "actor_user_id": 7},
    }]


def test_an_inactive_source_can_be_replaced(db):
    owner, _ = db
    _run(owner, "UPDATE collector_installations SET status = 'inactive' WHERE id = 101")

    authorized = _authorize()

    assert _audit(owner)[-1]["metadata"]["source_status"] == "inactive"
    assert _installation(owner, authorized["installation"]["id"])["replaces_installation_id"] == 101


def test_a_provisioning_source_needs_the_lost_response_confirmation_which_is_audited(db):
    owner, _ = db
    _run(owner, "UPDATE collector_installations SET status = 'provisioning' WHERE id = 101")

    _refused("lost_response_confirmation_required", lambda: _authorize(confirm=False))
    assert _pending(owner) == [] and _audit(owner) == []

    authorized = _authorize(confirm=True)

    metadata = _audit(owner)[-1]["metadata"]
    assert metadata["source_status"] == "provisioning" and metadata["lost_response_confirmed"] is True
    assert _pending(owner) == [authorized["installation"]["id"]]


def test_the_confirmation_is_not_recorded_for_an_active_source(db):
    owner, _ = db

    _authorize(confirm=True)

    assert "lost_response_confirmed" not in _audit(owner)[-1]["metadata"]


@pytest.mark.parametrize(("slug", "source", "code"), [
    ("acme", 103, "replacement_source_retired"),
    ("acme", 102, "replacement_source_not_contract_2"),
    ("acme", 105, "replacement_source_not_at_primary_branch"),
    ("acme", 201, "installation_not_found"),                 # another library's installation
    ("acme", 999, "installation_not_found"),
    ("fresh", 301, "collector_settings_not_ready"),
    ("legacy", 401, "replacement_source_not_key_owner"),     # the branch's key has no recorded owner
    ("unmapped", 601, "operational_identity_incomplete"),
])
def test_an_ineligible_source_is_refused_and_nothing_is_written(db, slug, source, code):
    owner, _ = db
    before = (_credentials(owner), _rows(owner, "SELECT count(*) AS n FROM collector_installations"))

    _refused(code, lambda: _authorize(source, slug))

    assert (_credentials(owner), _rows(owner, "SELECT count(*) AS n FROM collector_installations")) == before
    assert _audit(owner) == []


def test_a_source_that_owns_no_key_is_refused(db):
    owner, _ = db
    _run(owner, "UPDATE ingest_key_ids SET status = 'retired', retired_at = now() WHERE key_id = :k", k=ACME_KEY)

    _refused("replacement_source_not_key_owner", _authorize)


def test_a_second_active_key_at_the_branch_is_refused(db):
    owner, _ = db
    _run(owner, "INSERT INTO ingest_key_ids (key_id, customer_id, branch_id) VALUES (:k, 10, 1)",
         k="4e5f6071-8293-4a4b-8d5e-6f7a8b9cadb4")

    _refused("replacement_source_not_key_owner", _authorize)


def test_a_branch_with_no_cutover_is_refused(db):
    owner, _ = db
    _run(owner, "DELETE FROM v2_cutovers WHERE customer_id = 10")

    _refused("replacement_branch_not_enrolled", _authorize)


def test_an_active_unbound_token_at_the_branch_is_refused_until_it_is_revoked(db):
    owner, _ = db
    _run(owner, "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active) "
                "VALUES (:h, 10, 1, 'legacy', TRUE)", h=hashlib.sha256(UNBOUND_TOKEN.encode()).hexdigest())

    _refused("unbound_tokens_active", _authorize)

    (token,) = service.list_unbound_tokens(_library("acme"))
    service.revoke_unbound_token(_library("acme"), token["id"], actor=ACTOR)
    assert _authorize()["replaces_installation_id"] == 101


def test_a_cancelled_library_is_refused(db):
    owner, _ = db
    _run(owner, "UPDATE organizations SET status = 'cancelled' WHERE id = 1")

    _refused("library_cancelled", _authorize)


# =====================================================================================================================
# one pending replacement per branch (D1)
# =====================================================================================================================

def test_a_second_authorization_is_refused_while_one_is_pending_and_allowed_after_cancel(db):
    owner, _ = db
    first = _authorize()["installation"]["id"]

    _refused("replacement_already_pending", lambda: _authorize(name="Another", actor=OTHER_ADMIN))

    service.cancel_replacement(_library("acme"), first, actor=ACTOR)
    second = _authorize(name="Another", actor=OTHER_ADMIN)["installation"]["id"]
    assert _pending(owner) == [second]
    assert _installation(owner, second)["replacement_authorized_by_user_id"] == 8


def test_deactivating_a_pending_replacement_does_not_allow_a_second(db):
    owner, _ = db
    first = _authorize()["installation"]["id"]

    service.change_status(_library("acme"), first, status="inactive", actor=ACTOR)
    _refused("replacement_already_pending", lambda: _authorize(name="Another"))
    service.change_status(_library("acme"), first, status="provisioning", actor=ACTOR)

    assert _pending(owner) == [first]


def test_concurrent_authorizations_leave_exactly_one_pending_replacement(db):
    owner, _ = db
    barrier = threading.Barrier(4)

    def attempt(index):
        barrier.wait()
        try:
            return _authorize(name=f"Concurrent {index}")["installation"]["id"]
        except InstallationAdminError as refused:
            return refused.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(attempt, range(4)))

    created = [outcome for outcome in outcomes if isinstance(outcome, int)]
    assert len(created) == 1 and sorted(o for o in outcomes if isinstance(o, str)) == ["replacement_already_pending"] * 3
    assert _pending(owner) == created
    assert [row["event_type"] for row in _audit(owner)] == ["admin_replacement_authorized"]


def test_authorization_racing_cancellation_never_leaves_two_pending(db):
    owner, _ = db
    first = _authorize()["installation"]["id"]
    barrier = threading.Barrier(2)

    def cancel():
        barrier.wait()
        return service.cancel_replacement(_library("acme"), first, actor=ACTOR)["installation"]["status"]

    def authorize():
        barrier.wait()
        try:
            return _authorize(name="Racing")["installation"]["id"]
        except InstallationAdminError as refused:
            return refused.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled, authorized = pool.submit(cancel), pool.submit(authorize)
        assert cancelled.result() == "retired"
        outcome = authorized.result()

    assert outcome == "replacement_already_pending" or isinstance(outcome, int)
    assert _pending(owner) == ([] if isinstance(outcome, str) else [outcome])


def test_authorization_racing_offboarding_leaves_nothing_pending(db):
    owner, _ = db
    barrier = threading.Barrier(2)

    def offboard():
        barrier.wait()
        platform_admin_service.offboard_library(1, "acme", actor_user_id=7, actor_label="test", admin_actor=ACTOR)

    def authorize():
        barrier.wait()
        try:
            return _authorize(name="Racing")["installation"]["id"]
        except InstallationAdminError as refused:
            return refused.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        offboarded, authorized = pool.submit(offboard), pool.submit(authorize)
        offboarded.result()
        outcome = authorized.result()

    # Either it was authorized first and the offboarding retired it, or the offboarding came first and it was refused.
    assert outcome == "library_cancelled" or isinstance(outcome, int)
    assert _pending(owner) == []
    assert _rows(owner, "SELECT count(*) AS n FROM collector_installations WHERE organization_id = 1 "
                        "AND status <> 'retired'") == [{"n": 0}]


# =====================================================================================================================
# cancellation (D7) and the general status change
# =====================================================================================================================

def test_cancel_retires_the_replacement_revokes_its_unused_codes_and_is_audited(db):
    owner, _ = db
    replacement = _authorize()["installation"]["id"]
    service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR)
    before = _credentials(owner)

    cancelled = service.cancel_replacement(_library("acme"), replacement, actor=OTHER_ADMIN)

    assert cancelled["installation"]["status"] == "retired" and cancelled["replaces_installation_id"] == 101
    assert _rows(owner, "SELECT count(*) AS n FROM collector_enrollment_codes WHERE installation_id = :i "
                        "AND used_at IS NULL AND revoked_at IS NULL", i=replacement) == [{"n": 0}]
    assert _credentials(owner) == before  # the source runs on, untouched
    row = _installation(owner, replacement)
    assert (row["replaces_installation_id"], row["replacement_authorized_by_user_id"]) == (101, 7)  # kept as written
    assert _audit(owner)[-1] == {
        "user_id": 8, "email": "second@example.invalid", "event_type": "admin_replacement_cancelled",
        "message": "Collector replacement cancelled.",
        "metadata": {"target_org_slug": "acme", "installation_id": replacement, "replaces_installation_id": 101,
                     "previous_status": "provisioning", "revoked_code_count": 1, "actor_user_id": 8},
    }


@pytest.mark.parametrize("target", ["source", "cancelled", "unlinked"])
def test_cancel_refuses_what_is_not_a_pending_replacement(db, target):
    owner, _ = db
    replacement = _authorize()["installation"]["id"]
    if target == "cancelled":
        service.cancel_replacement(_library("acme"), replacement, actor=ACTOR)
    installation_id = {"source": 101, "cancelled": replacement,
                       "unlinked": service.create_installation(_library("acme"), name="Plain", required_contract=2,
                                                               actor=ACTOR)["id"]}[target]
    audit_before = _audit(owner)

    _refused("not_a_pending_replacement", lambda: service.cancel_replacement(_library("acme"), installation_id, actor=ACTOR))

    assert _audit(owner) == audit_before


def test_cancel_refuses_another_librarys_installation(db):
    _refused("installation_not_found", lambda: service.cancel_replacement(_library("other"), 101, actor=ACTOR))


def test_the_general_status_change_refuses_to_retire_a_pending_replacement(db):
    owner, _ = db
    replacement = _authorize()["installation"]["id"]

    _refused("use_cancel_replacement",
             lambda: service.change_status(_library("acme"), replacement, status="retired", actor=ACTOR))

    assert _installation(owner, replacement)["status"] == "provisioning" and _pending(owner) == [replacement]


def test_an_ordinary_installation_is_still_retired_by_the_general_status_change(db):
    owner, _ = db

    service.change_status(_library("acme"), 102, status="retired", actor=ACTOR)

    assert _installation(owner, 102)["status"] == "retired"


def test_the_source_still_cannot_be_retired_while_it_owns_the_active_key(db):
    _authorize()

    _refused("installation_owns_active_key",
             lambda: service.change_status(_library("acme"), 101, status="retired", actor=ACTOR))


# =====================================================================================================================
# enrollment codes, and their redemption (R2)
# =====================================================================================================================

def test_a_pending_replacement_can_be_issued_a_code(db):
    owner, _ = db
    replacement = _authorize()["installation"]["id"]

    issued = service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR)

    assert issued["required_contract"] == 2 and issued["enrollment_code"].startswith("SV-")
    assert _audit(owner)[-1]["event_type"] == "admin_enrollment_code_issued"
    assert issued["enrollment_code"] not in json.dumps(_audit(owner))


def test_ordinary_code_restrictions_are_unchanged(db):
    plain = service.create_installation(_library("acme"), name="Plain", required_contract=2, actor=ACTOR)["id"]

    _refused("branch_already_enrolled", lambda: service.issue_enrollment_code(_library("acme"), plain, actor=ACTOR))
    _refused("branch_already_enrolled", lambda: service.issue_enrollment_code(_library("acme"), 101, actor=ACTOR))
    assert service.issue_enrollment_code(_library("fresh"), 301, actor=ACTOR)["required_contract"] == 2


def test_a_code_is_refused_once_the_source_is_no_longer_eligible(db):
    owner, _ = db
    replacement = _authorize()["installation"]["id"]
    _run(owner, "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active) "
                "VALUES (:h, 10, 1, 'legacy', TRUE)", h=hashlib.sha256(UNBOUND_TOKEN.encode()).hexdigest())

    _refused("unbound_tokens_active", lambda: service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR))


def test_a_cancelled_replacement_cannot_be_issued_a_code(db):
    replacement = _authorize()["installation"]["id"]
    service.cancel_replacement(_library("acme"), replacement, actor=ACTOR)

    _refused("installation_not_enrollable",
             lambda: service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR))


def test_a_code_issued_by_r1_is_redeemed_by_r2_and_the_replacement_is_then_no_longer_pending(db):
    """Until R2 this code was refused (`active_ingest_key_exists`). R2 redeems it (tests/test_replacement_redemption*.py
    cover the cutover itself); here, only the handoff from R1's state: the pending replacement now owns the branch's only
    active key, the source is retired, and Cancel replacement no longer applies."""
    owner, runtime = db
    replacement = _authorize()["installation"]["id"]
    code = service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR)["enrollment_code"]

    with runtime.begin() as conn:
        result = collector_enrollment_service.redeem_enrollment_code(
            conn, code, hostname="replacement-pc", collector_version="1.0.14", contract_version=2)

    assert result["installation_id"] == replacement
    assert _installation(owner, 101)["status"] == "retired" and _pending(owner) == []
    assert _rows(owner, "SELECT installation_id FROM ingest_key_ids WHERE customer_id = 10 AND status = 'active'") == [
        {"installation_id": replacement}]
    _refused("not_a_pending_replacement",
             lambda: service.cancel_replacement(_library("acme"), replacement, actor=ACTOR))


def test_redeeming_a_cancelled_replacements_code_is_refused(db):
    _, runtime = db
    replacement = _authorize()["installation"]["id"]
    code = service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR)["enrollment_code"]
    service.cancel_replacement(_library("acme"), replacement, actor=ACTOR)

    with pytest.raises(EnrollmentError), runtime.begin() as conn:
        collector_enrollment_service.redeem_enrollment_code(conn, code, contract_version=2)


# =====================================================================================================================
# the schema: write-once trigger, CHECKs, same-branch foreign key
# =====================================================================================================================

@pytest.mark.parametrize("column, value", [
    ("replaces_installation_id", "NULL"), ("replaces_installation_id", "201"),
    ("replacement_authorized_by_user_id", "8"), ("replacement_authorized_at", "now() - interval '1 day'"),
])
@pytest.mark.parametrize("role", ["runtime", "owner"])
def test_the_replacement_columns_are_write_once_for_every_role(db, column, value, role):
    owner, runtime = db
    replacement = _authorize()["installation"]["id"]
    engine = runtime if role == "runtime" else owner

    with pytest.raises(DBAPIError, match="write-once"), engine.begin() as conn:
        conn.execute(text(f"UPDATE collector_installations SET {column} = {value} WHERE id = :i"),  # nosec B608 - fixed
                     {"i": replacement})

    row = _installation(owner, replacement)
    assert (row["replaces_installation_id"], row["replacement_authorized_by_user_id"]) == (101, 7)


def test_every_other_column_stays_updatable_and_naming_a_column_unchanged_is_allowed(db):
    owner, runtime = db
    replacement = _authorize()["installation"]["id"]

    with runtime.begin() as conn:
        conn.execute(text("UPDATE collector_installations SET name = 'Renamed', status = 'inactive', "
                          "replaces_installation_id = replaces_installation_id WHERE id = :i"), {"i": replacement})

    row = _installation(owner, replacement)
    assert (row["name"], row["status"], row["replaces_installation_id"]) == ("Renamed", "inactive", 101)


@pytest.mark.parametrize("columns, values", [
    ("replaces_installation_id", "101"),
    ("replaces_installation_id, replacement_authorized_by_user_id", "101, 7"),
    ("replacement_authorized_by_user_id, replacement_authorized_at", "7, now()"),
])
def test_the_replacement_columns_are_all_or_none(db, columns, values):
    owner, _ = db

    with pytest.raises(DBAPIError, match="ck_collector_installations_replacement_all_or_none"), owner.begin() as conn:
        conn.execute(text(
            f"INSERT INTO collector_installations (organization_id, branch_id, name, status, required_contract, {columns}) "  # nosec B608
            f"VALUES (1, 1, 'Partial', 'provisioning', 2, {values})"
        ))


def test_an_installation_cannot_replace_itself(db):
    owner, _ = db

    with pytest.raises(DBAPIError, match="ck_collector_installations_replaces_not_self"), owner.begin() as conn:
        conn.execute(text(
            "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract, "
            "replaces_installation_id, replacement_authorized_by_user_id, replacement_authorized_at) "
            "VALUES (900, 1, 1, 'Self', 'provisioning', 2, 900, 7, now())"
        ))


def test_a_replacement_cannot_name_an_installation_at_another_branch(db):
    owner, _ = db

    with pytest.raises(DBAPIError, match="fk_collector_installations_replaces_same_branch"), owner.begin() as conn:
        conn.execute(text(
            "INSERT INTO collector_installations (organization_id, branch_id, name, status, required_contract, "
            "replaces_installation_id, replacement_authorized_by_user_id, replacement_authorized_at) "
            "VALUES (1, 1, 'Cross', 'provisioning', 2, 201, 7, now())"
        ))


def test_existing_installations_have_no_replacement_columns_set(db):
    owner, _ = db

    assert _rows(owner, "SELECT count(*) AS n FROM collector_installations WHERE replaces_installation_id IS NOT NULL "
                        "OR replacement_authorized_by_user_id IS NOT NULL OR replacement_authorized_at IS NOT NULL") == [
        {"n": 0}]


# =====================================================================================================================
# the general installation update: retired is final (14.2)
# =====================================================================================================================

@pytest.mark.parametrize("status", ["provisioning", "active", "inactive"])
def test_the_general_update_cannot_move_an_installation_out_of_retired(db, status):
    owner, _ = db

    with pytest.raises(RuntimeError):
        tenant_service.update_collector_installation(
            installation_id=103, organization_id=1, name="Revived", hostname=None, collector_version=None, status=status)

    row = _installation(owner, 103)
    assert (row["name"], row["status"]) == ("Acme Retired", "retired")


def test_the_general_update_still_renames_a_retired_installation_that_stays_retired(db):
    owner, _ = db

    tenant_service.update_collector_installation(
        installation_id=103, organization_id=1, name="Renamed", hostname=None, collector_version=None, status="retired")

    assert (_installation(owner, 103)["name"], _installation(owner, 103)["status"]) == ("Renamed", "retired")


# =====================================================================================================================
# an audit event commits or rolls back with its change (14.2a)
# =====================================================================================================================

def _everything(owner):
    """Every row authorization or cancellation could write, the audit log included."""
    return (
        _credentials(owner),
        _rows(owner, "SELECT * FROM collector_installations ORDER BY id"),
        _rows(owner, "SELECT id, installation_id, used_at, revoked_at FROM collector_enrollment_codes ORDER BY id"),
        _audit(owner),
    )


def _fail_after_the_audit_insert(monkeypatch):
    """Both writes end by reading the installation back (`_one`) after `_audit`, inside the same transaction. Fail
    there, recording the event the transaction itself can see, so the event is proved written before the failure."""
    seen = []

    def failing_one(conn, library, installation_id):
        seen.append(conn.execute(text("SELECT event_type FROM auth_audit_log ORDER BY id DESC LIMIT 1")).scalar())
        raise RuntimeError("injected failure after the audit insert")

    monkeypatch.setattr(service, "_one", failing_one)
    return seen


def test_a_failure_after_the_authorization_audit_rolls_back_the_event_and_the_installation(db, monkeypatch):
    owner, _ = db
    before = _everything(owner)
    seen = _fail_after_the_audit_insert(monkeypatch)

    with pytest.raises(RuntimeError, match="injected"):
        _authorize()

    assert seen == [service.REPLACEMENT_AUTHORIZED]  # the event was inserted inside the transaction ...
    assert _everything(owner) == before              # ... and went back with the new installation
    assert _pending(owner) == []


def test_a_failure_after_the_cancellation_audit_rolls_back_the_event_the_retirement_and_the_code_revocation(
        db, monkeypatch):
    owner, _ = db
    replacement = _authorize()["installation"]["id"]
    service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR)
    before = _everything(owner)
    seen = _fail_after_the_audit_insert(monkeypatch)

    with pytest.raises(RuntimeError, match="injected"):
        service.cancel_replacement(_library("acme"), replacement, actor=ACTOR)

    assert seen == [service.REPLACEMENT_CANCELLED]
    assert _everything(owner) == before
    assert _installation(owner, replacement)["status"] == "provisioning" and _pending(owner) == [replacement]


# =====================================================================================================================
# the migration b8e2d4f61a07: a round trip on its own throwaway database
# =====================================================================================================================

PREVIOUS_REVISION = "c1e7a94f2b60"
R1_ADDITIONS = {
    ("column", "collector_installations", "replaces_installation_id", "bigint", "YES", None),
    ("column", "collector_installations", "replacement_authorized_by_user_id", "bigint", "YES", None),
    ("column", "collector_installations", "replacement_authorized_at", "timestamp with time zone", "YES", None),
    ("constraint", "collector_installations", "fk_collector_installations_replaces_same_branch",
     "FOREIGN KEY (replaces_installation_id, branch_id) REFERENCES collector_installations(id, branch_id)"),
    ("constraint", "collector_installations", "ck_collector_installations_replaces_not_self",
     "CHECK ((replaces_installation_id <> id))"),
    ("function", "collector_installations_replacement_write_once", ""),
}
# Their definitions are long; these two are matched by name and checked separately.
R1_NAMED = {"ck_collector_installations_replacement_all_or_none", "trg_collector_installations_replacement_write_once"}


def _schema(engine):
    """Every column, constraint, index, user trigger and function in the public schema, as comparable tuples."""
    queries = (
        ("SELECT 'column', table_name::text, column_name::text, data_type::text, is_nullable::text, "
         "column_default::text FROM information_schema.columns WHERE table_schema = 'public'"),
        ("SELECT 'constraint', conrelid::regclass::text, conname::text, pg_get_constraintdef(oid) "
         "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"),
        "SELECT 'index', tablename::text, indexname::text, indexdef FROM pg_indexes WHERE schemaname = 'public'",
        ("SELECT 'trigger', tgrelid::regclass::text, tgname::text, pg_get_triggerdef(oid) FROM pg_trigger "
         "WHERE NOT tgisinternal"),
        ("SELECT 'function', proname::text, pg_get_function_identity_arguments(oid) FROM pg_proc "
         "WHERE pronamespace = 'public'::regnamespace"),
    )
    with engine.connect() as conn:
        return {tuple(row) for sql in queries for row in conn.execute(text(sql)).all()}


_REPLACEMENT_INSERT = (
    "INSERT INTO collector_installations (id, organization_id, branch_id, name, status, required_contract, "
    "replaces_installation_id, replacement_authorized_by_user_id, replacement_authorized_at) "
    "VALUES (:id, 1, 1, 'Replacement', 'provisioning', 2, :replaces, :by, now())"
)


def test_the_migration_round_trips_and_its_downgrade_removes_exactly_its_additions():
    from test_collector_enrollment_v2_postgres import Throwaway, _alembic

    with Throwaway(PREVIOUS_REVISION) as db:
        engine = create_engine(db.url, hide_parameters=True)
        try:
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO customers (id, name) VALUES (10, 'Lib A')"))
                conn.execute(text("INSERT INTO organizations (id, slug, name, status, operational_customer_id) "
                                  "VALUES (1, 'lib-a', 'Lib A', 'active', 10)"))
                conn.execute(text("INSERT INTO branches (id, organization_id, slug, name, status, operational_branch_id) "
                                  "VALUES (1, 1, 'main', 'Main', 'active', 1), (2, 1, 'east', 'East', 'active', 2)"))
                conn.execute(text("INSERT INTO collector_installations (id, organization_id, branch_id, name, status, "
                                  "required_contract) VALUES (101, 1, 1, 'Sorter', 'active', 2), "
                                  "(201, 1, 2, 'East Sorter', 'active', 2)"))
            schema_before = _schema(engine)
            rows_before = _rows(engine, "SELECT * FROM collector_installations ORDER BY id")

            upgraded = _alembic(db.url, "upgrade", "b8e2d4f61a07")
            assert upgraded.returncode == 0, upgraded.stderr[-2000:]

            # The upgrade adds exactly R1's schema, changes nothing already there, and leaves existing rows unset.
            schema_after = _schema(engine)
            assert schema_before <= schema_after
            added = schema_after - schema_before
            named = {item for item in added if item[2] in R1_NAMED}
            assert added - named == R1_ADDITIONS
            assert {item[2] for item in named} == R1_NAMED and len(named) == 2
            (trigger,) = [item for item in named if item[0] == "trigger"]
            assert ("BEFORE UPDATE OF replaces_installation_id, replacement_authorized_by_user_id, "
                    "replacement_authorized_at ON ") in trigger[3]
            assert "collector_installations FOR EACH ROW EXECUTE FUNCTION " \
                   "collector_installations_replacement_write_once()" in trigger[3]
            assert _rows(engine, "SELECT count(*) AS n FROM collector_installations WHERE replaces_installation_id "
                                 "IS NOT NULL OR replacement_authorized_by_user_id IS NOT NULL "
                                 "OR replacement_authorized_at IS NOT NULL") == [{"n": 0}]

            # The constraints and the trigger are enforced.
            for row, constraint in (
                ({"id": 301, "replaces": 101, "by": None}, "replacement_all_or_none"),
                ({"id": 302, "replaces": 302, "by": 7}, "replaces_not_self"),
                ({"id": 303, "replaces": 201, "by": 7}, "replaces_same_branch"),  # 201 is at another branch
            ):
                with pytest.raises(DBAPIError, match=constraint), engine.begin() as conn:
                    conn.execute(text(_REPLACEMENT_INSERT), row)
            with engine.begin() as conn:
                conn.execute(text(_REPLACEMENT_INSERT), {"id": 304, "replaces": 101, "by": 7})
            with pytest.raises(DBAPIError, match="write-once"), engine.begin() as conn:
                conn.execute(text("UPDATE collector_installations SET replacement_authorized_by_user_id = 8 "
                                  "WHERE id = 304"))
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM collector_installations WHERE id = 304"))

            downgraded = _alembic(db.url, "downgrade", PREVIOUS_REVISION)
            assert downgraded.returncode == 0, downgraded.stderr[-2000:]

            # Only R1's additions are gone: every other column, constraint, index, trigger and function, and every row.
            assert _schema(engine) == schema_before
            assert _rows(engine, "SELECT * FROM collector_installations ORDER BY id") == rows_before

            assert _alembic(db.url, "upgrade", "head").returncode == 0  # and it goes forward again
        finally:
            engine.dispose()


# =====================================================================================================================
# machine replacement turned off (14.11)
# =====================================================================================================================

def _installations(owner):
    return _rows(owner, "SELECT * FROM collector_installations ORDER BY id")


def _codes(owner):
    return _rows(owner, "SELECT * FROM collector_enrollment_codes ORDER BY id")


def test_while_replacement_is_off_authorization_is_refused_and_writes_nothing(db, monkeypatch):
    owner, _ = db
    before = (_credentials(owner), _installations(owner), _audit(owner))
    monkeypatch.delenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED")

    _refused("replacement_disabled", _authorize)
    _refused("replacement_disabled", lambda: _authorize(confirm=True))

    assert (_credentials(owner), _installations(owner), _audit(owner)) == before


def test_while_replacement_is_off_a_pending_replacement_gets_no_code_and_keeps_its_unused_one(db, monkeypatch):
    owner, _ = db
    replacement = _authorize()["installation"]["id"]
    service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR)
    before = (_codes(owner), _audit(owner))
    monkeypatch.delenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED")

    _refused("replacement_disabled", lambda: service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR))

    assert (_codes(owner), _audit(owner)) == before


def test_while_replacement_is_off_a_pending_replacement_can_still_be_cancelled_or_deactivated(db, monkeypatch):
    owner, _ = db
    replacement = _authorize()["installation"]["id"]
    service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR)
    before = _credentials(owner)
    monkeypatch.delenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED")

    assert service.change_status(_library("acme"), replacement, status="inactive", actor=ACTOR)["status"] == "inactive"
    assert service.change_status(_library("acme"), replacement, status="provisioning", actor=ACTOR)["status"] == (
        "provisioning")
    _refused("use_cancel_replacement",
             lambda: service.change_status(_library("acme"), replacement, status="retired", actor=ACTOR))
    cancelled = service.cancel_replacement(_library("acme"), replacement, actor=ACTOR)

    assert cancelled["installation"]["status"] == "retired" and _pending(owner) == []
    assert _rows(owner, "SELECT count(*) AS n FROM collector_enrollment_codes WHERE installation_id = :i "
                        "AND used_at IS NULL AND revoked_at IS NULL", i=replacement) == [{"n": 0}]
    assert _credentials(owner) == before  # the source runs on, untouched
    assert _audit(owner)[-1]["event_type"] == "admin_replacement_cancelled"


def test_while_replacement_is_off_ordinary_administration_is_unaffected(db, monkeypatch):
    owner, _ = db
    _run(owner, "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active) "
                "VALUES (:h, 10, 1, 'legacy', TRUE)", h=hashlib.sha256(UNBOUND_TOKEN.encode()).hexdigest())
    monkeypatch.delenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED")

    assert service.issue_enrollment_code(_library("fresh"), 301, actor=ACTOR)["required_contract"] == 2
    plain = service.create_installation(_library("acme"), name="Plain", required_contract=2, actor=ACTOR)["id"]
    _refused("branch_already_enrolled", lambda: service.issue_enrollment_code(_library("acme"), plain, actor=ACTOR))
    (token,) = service.list_unbound_tokens(_library("acme"))
    service.revoke_unbound_token(_library("acme"), token["id"], actor=ACTOR)
    recovered = service.issue_recovery_token(_library("acme"), 101, actor=ACTOR)

    assert recovered["agent_token"] and recovered["revoked_previous_count"] == 1
    assert [event["event_type"] for event in _audit(owner)] == [
        "admin_enrollment_code_issued", "admin_installation_created", "admin_unbound_token_revoked",
        "admin_recovery_token_issued",
    ]


# =====================================================================================================================
# the installation list's replacement fields (14.11, decision 1), as the runtime role under row level security
# =====================================================================================================================

def _replaces(slug="acme"):
    return {i["id"]: i["replaces_installation_id"] for i in service.list_installations(_library(slug))}


def test_the_list_has_no_pending_replacement_until_one_is_authorized(db):
    assert service.pending_replacement_id(_library("acme")) is None
    assert set(_replaces().values()) == {None}

    replacement = _authorize()["installation"]["id"]

    assert service.pending_replacement_id(_library("acme")) == replacement
    assert _replaces()[replacement] == 101 and _replaces()[101] is None


def test_a_cancelled_replacement_is_no_longer_pending_and_still_names_its_source(db):
    replacement = _authorize()["installation"]["id"]

    service.cancel_replacement(_library("acme"), replacement, actor=ACTOR)

    assert service.pending_replacement_id(_library("acme")) is None
    assert _replaces()[replacement] == 101


def test_a_redeemed_replacement_is_no_longer_pending_because_its_key_is_read_under_the_tenant_context(db):
    """Without the branch's tenant context, row level security would hide the replacement's new key from the runtime
    role, and the redeemed replacement would still look pending."""
    _, runtime = db
    replacement = _authorize()["installation"]["id"]
    code = service.issue_enrollment_code(_library("acme"), replacement, actor=ACTOR)["enrollment_code"]
    with runtime.begin() as conn:
        collector_enrollment_service.redeem_enrollment_code(conn, code, contract_version=2)

    assert service.pending_replacement_id(_library("acme")) is None
    assert _replaces()[replacement] == 101
    with runtime.connect() as conn:  # the same read with no tenant context: the key is hidden, so it would look pending
        assert replacement_eligibility.pending_replacement_at_branch(conn, 1) == replacement


def test_the_pending_replacement_is_the_librarys_own_and_never_another_tenants(db):
    other = _authorize(201, "other")["installation"]["id"]

    assert service.pending_replacement_id(_library("other")) == other
    assert service.pending_replacement_id(_library("acme")) is None
    assert other not in _replaces("acme")

    acme = _authorize()["installation"]["id"]
    assert (service.pending_replacement_id(_library("acme")), service.pending_replacement_id(_library("other"))) == (
        acme, other)


def test_a_library_with_no_single_primary_branch_reports_no_pending_replacement(db):
    owner, _ = db
    _authorize()
    _run(owner, "UPDATE branches SET is_primary = FALSE WHERE id = 1")

    assert service.pending_replacement_id(_library("acme")) is None


def test_the_pending_replacement_is_reported_whatever_the_flag(db, monkeypatch):
    replacement = _authorize()["installation"]["id"]
    monkeypatch.delenv("SORTVIEW_MACHINE_REPLACEMENT_ENABLED")

    assert service.pending_replacement_id(_library("acme")) == replacement  # so it can still be found and cancelled
