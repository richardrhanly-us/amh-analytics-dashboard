"""One-time Collector enrollment: code generation, redemption and POST /collector/enroll.

Runs the real service SQL and the real endpoint against SQLite (only the token
hash expression in the auth lookup is swapped, exactly as in
tests/test_agent_tenant_authorization.py), so the joins, the guarded UPDATE and
the transaction/rollback behavior are genuinely exercised. Behavior that depends
on PostgreSQL row locking and true concurrency lives in
tests/test_collector_enrollment_postgres.py.

Operational ids deliberately differ from SaaS ids, and one scenario swaps them,
so any confusion of the two fails a test.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

import main
from scripts import create_agent_token as create_agent_token_script
from src.services import collector_enrollment_service as enrollment

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
PUBLIC = {"detail": "Invalid or expired enrollment code"}
_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
CODE_PATTERN = re.compile(r"^SV(-[A-HJKMNP-Z2-9]{4}){4}$")

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


# --- database ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _register_sha256(dbapi_connection, _record):
        # Stands in for Postgres's encode(digest(:token, 'sha256'), 'hex'), so a
        # token stored as its SHA-256 (as enrollment stores it) authenticates.
        dbapi_connection.create_function(
            "sha256hex", 1, lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
        )

    ddl = [
        "CREATE TABLE customers (id INTEGER PRIMARY KEY)",
        """CREATE TABLE organizations (
            id INTEGER PRIMARY KEY, slug TEXT, status TEXT, operational_customer_id INTEGER)""",
        """CREATE TABLE branches (
            id INTEGER PRIMARY KEY, organization_id INTEGER, status TEXT, operational_branch_id INTEGER)""",
        """CREATE TABLE collector_installations (
            id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, name TEXT,
            hostname TEXT, collector_version TEXT, status TEXT, installed_at TEXT,
            last_seen_at TEXT, created_at TEXT, updated_at TEXT)""",
        """CREATE TABLE agent_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
            customer_id INTEGER NOT NULL, branch_id INTEGER NOT NULL, description TEXT,
            is_active BOOLEAN NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT, installation_id INTEGER)""",
        """CREATE TABLE collector_enrollment_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, installation_id INTEGER NOT NULL,
            code_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, revoked_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, created_by_user_id INTEGER)""",
        "CREATE TABLE pipeline_status (customer_id INTEGER, branch_id INTEGER, "
        + ", ".join(f"{c} TEXT" for c in _PIPELINE_STATUS_COLUMNS)
        + ", updated_at TEXT, UNIQUE (customer_id, branch_id))",
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.execute(text(statement))
    monkeypatch.setattr(main, "engine", engine)
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    monkeypatch.setattr(
        main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, "sha256hex(:token)")
    )
    return engine


def _run(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _tenant(engine, org_id, customer_id, branch_id, operational_branch_id, *, org_status="active",
            branch_status="active", customer_row=True):
    with engine.begin() as conn:
        if customer_row and customer_id is not None:
            conn.execute(text("INSERT OR IGNORE INTO customers VALUES (:c)"), {"c": customer_id})
        conn.execute(text("INSERT INTO organizations (id, slug, status, operational_customer_id) "
                          "VALUES (:i, :slug, :s, :c)"),
                     {"i": org_id, "slug": f"org-{org_id}", "s": org_status, "c": customer_id})
        conn.execute(text("INSERT INTO branches VALUES (:i, :o, :s, :b)"),
                     {"i": branch_id, "o": org_id, "s": branch_status, "b": operational_branch_id})


def _installation(engine, installation_id, org_id, branch_id, *, status="provisioning",
                  name=None, hostname="AMH-PC", version="1.0.2"):
    _run(
        engine,
        "INSERT INTO collector_installations (id, organization_id, branch_id, name, hostname, "
        "collector_version, status, installed_at, last_seen_at, created_at, updated_at) "
        "VALUES (:id, :o, :b, :n, :h, :v, :s, NULL, NULL, '2020-01-01 00:00:00', '2020-01-01 00:00:00')",
        id=installation_id, o=org_id, b=branch_id, n=name or f"Sorter {installation_id}",
        h=hostname, v=version, s=status,
    )


@pytest.fixture
def world(db):
    """Two tenants; the OPERATIONAL customer ids (10, 11) never equal the SaaS
    organization ids (1, 2). (A branch's operational id must equal its own id --
    a database CHECK -- so only the customer id can differ.)

      org 1 (active), customer 10, branch 1: installations 101 provisioning,
          102 active, 103 inactive, 104 retired -- three of them sharing one branch
      org 2 (trial), customer 11, branch 2: installation 201
    """
    _tenant(db, 1, 10, 1, 1)
    _tenant(db, 2, 11, 2, 2, org_status="trial")
    _installation(db, 101, 1, 1, name="Main AMH Sorter")
    _installation(db, 102, 1, 1, status="active", hostname="AMH-PC-2")
    _installation(db, 103, 1, 1, status="inactive")
    _installation(db, 104, 1, 1, status="retired")
    _installation(db, 201, 2, 2, hostname="OTHER-PC")
    return db


# --- helpers -------------------------------------------------------------------------------

def _generate(engine, installation_id, **kwargs):
    kwargs.setdefault("now", NOW)
    with engine.begin() as conn:
        return enrollment.create_enrollment_code(conn, installation_id, **kwargs)


def _redeem(engine, code, **kwargs):
    kwargs.setdefault("now", NOW + timedelta(minutes=1))
    with engine.begin() as conn:
        return enrollment.redeem_enrollment_code(conn, code, **kwargs)


def _insert_code(engine, installation_id, *, expires_at=None, used=False, revoked=False):
    """Writes a code row directly (bypassing generation's validation) so a test
    can create states generation would refuse. Returns the raw code."""
    raw = enrollment.new_enrollment_code()
    with engine.begin() as conn:
        conn.execute(
            enrollment._timestamped(
                "INSERT INTO collector_enrollment_codes (installation_id, code_hash, expires_at, used_at, "
                "revoked_at) VALUES (:installation_id, :code_hash, :expires_at, :used_at, :revoked_at)",
                installation_id=installation_id,
                code_hash=enrollment.hash_enrollment_code(raw),
                expires_at=expires_at or NOW + timedelta(minutes=30),
                used_at="2026-09-20 11:00:00" if used else None,
                revoked_at="2026-09-20 11:00:00" if revoked else None,
            )
        )
    return raw


def _table(engine, name):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {name} ORDER BY 1")).mappings().all()]  # nosec B608


def _installations(engine):
    return _table(engine, "collector_installations")


def _tokens(engine):
    return _table(engine, "agent_tokens")


def _codes(engine):
    return _table(engine, "collector_enrollment_codes")


def _everything_stored(engine) -> str:
    """Every value in every table, as one string -- for 'never persisted' checks."""
    parts = []
    for table in ("collector_enrollment_codes", "agent_tokens", "collector_installations", "pipeline_status"):
        parts.append(json.dumps(_table(engine, table), default=str))
    return "\n".join(parts)


def _reason(excinfo) -> str:
    return excinfo.value.reason


# ======================================================================================
# code format and primitives
# ======================================================================================

def test_generated_codes_use_the_documented_display_format():
    for _ in range(50):
        assert CODE_PATTERN.match(enrollment.new_enrollment_code())


def test_code_alphabet_has_no_lookalike_characters():
    assert not set("01OIL") & set(enrollment.CODE_ALPHABET)
    assert len(set(enrollment.CODE_ALPHABET)) == len(enrollment.CODE_ALPHABET) == 31


def test_codes_are_unique_and_come_from_a_cryptographic_source(monkeypatch):
    assert len({enrollment.new_enrollment_code() for _ in range(500)}) == 500

    calls = []
    real_choice = enrollment.secrets.choice
    monkeypatch.setattr(enrollment.secrets, "choice", lambda alphabet: calls.append(1) or real_choice(alphabet))
    enrollment.new_enrollment_code()
    assert len(calls) == enrollment.CODE_CHARACTER_COUNT  # secrets.choice, sixteen times


@pytest.mark.parametrize("typed", [
    "SV-ABCD-EFGH-JKMN-PQRS", "sv-abcd-efgh-jkmn-pqrs", "SVABCDEFGHJKMNPQRS", "  sv abcd efgh jkmn pqrs ",
    "ABCD-EFGH-JKMN-PQRS", "SV_ABCD_EFGH_JKMN_PQRS",
])
def test_entry_ignores_case_spacing_separators_and_an_omitted_prefix(typed):
    assert enrollment.normalize_enrollment_code(typed) == "SV-ABCD-EFGH-JKMN-PQRS"


@pytest.mark.parametrize("bad", [
    None, "", "   ", "SV-ABCD", "SV-ABCD-EFGH-JKMN-PQRS-TUVW", "SV-ABCD-EFGH-JKMN-PQR0", "SV-ABCD-EFGH-JKMN-PQRI",
    "x" * 500, 12345, "SV-ABCD-EFGH-JKMN-PQR!",
])
def test_anything_that_cannot_be_a_code_normalizes_to_none(bad):
    assert enrollment.normalize_enrollment_code(bad) is None


def test_agent_token_scheme_matches_create_agent_token_script():
    raw, digest = enrollment.new_agent_token()
    script_raw, script_digest = create_agent_token_script.generate_token()

    assert digest == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert script_digest == hashlib.sha256(script_raw.encode("utf-8")).hexdigest()
    assert len(raw) == len(script_raw) and len(digest) == len(script_digest) == 64
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", raw)
    assert enrollment.new_agent_token()[0] != raw


def test_enrollment_policy_constants_match_the_api_and_the_token_script():
    assert enrollment.ALLOWED_ORGANIZATION_STATUSES == main.ALLOWED_ORGANIZATION_STATUSES
    assert enrollment.ALLOWED_BRANCH_STATUS == main.ALLOWED_BRANCH_STATUS
    assert enrollment.ALLOWED_INSTALLATION_STATUSES == main.INSTALLATION_HEARTBEAT_STATUSES
    assert enrollment.ALLOWED_ORGANIZATION_STATUSES == create_agent_token_script._ALLOWED_ORGANIZATION_STATUSES
    assert enrollment.ALLOWED_BRANCH_STATUS == create_agent_token_script._ALLOWED_BRANCH_STATUS


# ======================================================================================
# generation
# ======================================================================================

def test_generation_returns_the_raw_code_once_with_installation_and_expiry(world):
    result = _generate(world, 101, created_by_user_id=7)

    assert CODE_PATTERN.match(result["enrollment_code"])
    assert result["installation_id"] == 101
    assert result["installation_name"] == "Main AMH Sorter"
    assert result["expires_at"] == NOW + timedelta(minutes=30)  # default 30 minutes
    assert result["ttl_minutes"] == 30
    assert result["revoked_previous_count"] == 0


def test_generation_stores_only_the_sha256_hash_and_never_the_raw_code(world):
    result = _generate(world, 101, created_by_user_id=7)
    raw = result["enrollment_code"]

    (row,) = _codes(world)
    assert row["code_hash"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert row["installation_id"] == 101 and row["created_by_user_id"] == 7
    assert row["used_at"] is None and row["revoked_at"] is None
    stored = _everything_stored(world).lower()
    for variant in (raw, raw.replace("-", ""), raw[3:], raw.replace("-", "")[2:]):
        assert variant.lower() not in stored


def test_generation_honours_a_custom_ttl(world):
    result = _generate(world, 101, ttl=timedelta(minutes=5))

    assert result["expires_at"] == NOW + timedelta(minutes=5) and result["ttl_minutes"] == 5


@pytest.mark.parametrize("installation_id", [101, 102], ids=["provisioning", "active"])
def test_generation_is_allowed_for_provisioning_and_active_installations(world, installation_id):
    assert _generate(world, installation_id)["installation_id"] == installation_id


def test_generation_for_a_trial_organization_is_allowed(world):
    assert _generate(world, 201)["installation_id"] == 201


def test_generation_for_a_missing_installation_is_refused(world):
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _generate(world, 99999)

    assert _reason(excinfo) == "installation_not_found"
    assert _codes(world) == []


@pytest.mark.parametrize(("installation_id", "reason"), [(103, "installation_status_inactive"),
                                                          (104, "installation_status_retired")])
def test_generation_for_an_inactive_or_retired_installation_is_refused(world, installation_id, reason):
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _generate(world, installation_id)

    assert _reason(excinfo) == reason
    assert _codes(world) == []


def test_generation_is_scoped_to_the_expected_organization(world):
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _generate(world, 201, expected_organization_id=1)  # org 2's installation, org 1's page

    assert _reason(excinfo) == "installation_organization_mismatch"
    assert _codes(world) == []
    assert _generate(world, 201, expected_organization_id=2)["installation_id"] == 201


def test_generating_a_new_code_invalidates_the_previous_unused_code_of_that_installation(world):
    first = _generate(world, 101)
    second = _generate(world, 101)

    first_row, second_row = _codes(world)
    assert first_row["revoked_at"] is not None  # invalidated
    assert second_row["revoked_at"] is None and second_row["used_at"] is None
    assert second["revoked_previous_count"] == 1
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _redeem(world, first["enrollment_code"])
    assert _reason(excinfo) == "revoked"
    assert _redeem(world, second["enrollment_code"])["installation_id"] == 101


def test_generation_only_invalidates_codes_of_the_same_installation(world):
    other = _generate(world, 102)

    _generate(world, 101)
    _generate(world, 101)

    other_row = next(r for r in _codes(world) if r["installation_id"] == 102)
    assert other_row["revoked_at"] is None  # untouched
    assert _redeem(world, other["enrollment_code"])["installation_id"] == 102


def test_generation_leaves_an_already_used_code_alone(world):
    first = _generate(world, 101)
    _redeem(world, first["enrollment_code"])

    second = _generate(world, 101)

    used_row, new_row = _codes(world)
    assert used_row["used_at"] is not None and used_row["revoked_at"] is None
    assert new_row["revoked_at"] is None
    assert second["revoked_previous_count"] == 0


def test_generation_never_touches_the_installation_or_any_token(world):
    _run(world, "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, installation_id) "
                "VALUES ('legacy-hash', 10, 1, NULL)")
    installations_before, tokens_before = _installations(world), _tokens(world)

    _generate(world, 101)

    assert _installations(world) == installations_before
    assert _tokens(world) == tokens_before


def test_generation_uses_the_installation_row_lock_only_on_postgresql(world):
    with world.connect() as conn:
        assert enrollment._lock_suffix(conn, "ci") == ""  # SQLite: no FOR UPDATE syntax

    class _Postgres:
        class dialect:
            name = "postgresql"

    assert enrollment._lock_suffix(_Postgres, "ci") == " FOR UPDATE OF ci"
    assert enrollment._lock_suffix(_Postgres) == " FOR UPDATE"
    assert "FOR UPDATE" in (enrollment._LOOKUP_CODE_SQL + enrollment._lock_suffix(_Postgres))


# ======================================================================================
# redemption: success
# ======================================================================================

def test_redeeming_a_provisioning_installations_code_returns_exactly_the_four_fields(world):
    code = _generate(world, 101)["enrollment_code"]

    result = _redeem(world, code, hostname="AMH-PC", collector_version="1.0.4")

    assert set(result) == {"customer_id", "branch_id", "installation_id", "agent_token"}
    assert result["customer_id"] == 10  # OPERATIONAL customer, not organizations.id (1)
    assert result["branch_id"] == 1
    assert result["installation_id"] == 101
    assert isinstance(result["agent_token"], str) and len(result["agent_token"]) >= 40


def test_redeeming_an_active_installations_code_works_for_a_reinstall(world):
    code = _generate(world, 102)["enrollment_code"]

    result = _redeem(world, code)

    assert result["installation_id"] == 102
    assert result["customer_id"] == 10


def test_the_token_row_has_the_right_scope_binding_hash_and_description(world):
    code = _generate(world, 101)["enrollment_code"]

    result = _redeem(world, code, hostname="AMH-PC", collector_version="1.0.4")

    (token,) = _tokens(world)
    assert (token["customer_id"], token["branch_id"], token["installation_id"]) == (10, 1, 101)
    assert token["is_active"] in (1, True)
    assert token["token_hash"] == hashlib.sha256(result["agent_token"].encode("utf-8")).hexdigest()
    assert "installation 101" in token["description"] and "Main AMH Sorter" in token["description"]
    assert "AMH-PC" in token["description"] and "1.0.4" in token["description"]
    assert result["agent_token"] not in token["description"]
    assert token["last_used_at"] is None


def test_the_operational_customer_is_used_even_when_saas_ids_are_swapped(db):
    # SaaS org 1 has OPERATIONAL customer 2, and SaaS org 2 has operational customer 1.
    _tenant(db, 1, 2, 1, 1)
    _tenant(db, 2, 1, 2, 2)
    _installation(db, 11, 1, 1)
    _installation(db, 12, 2, 2)

    a = _redeem(db, _generate(db, 11)["enrollment_code"])
    b = _redeem(db, _generate(db, 12)["enrollment_code"])

    assert (a["customer_id"], a["installation_id"]) == (2, 11)
    assert (b["customer_id"], b["installation_id"]) == (1, 12)


def test_enrollment_never_changes_the_installation_row(world):
    code = _generate(world, 101)["enrollment_code"]
    before = _installations(world)

    _redeem(world, code, hostname="SOMETHING-ELSE", collector_version="9.9.9")

    after = _installations(world)
    assert after == before  # status, installed_at, last_seen_at, collector_version, hostname, updated_at
    row = next(r for r in after if r["id"] == 101)
    assert row["installed_at"] is None and row["last_seen_at"] is None
    assert row["collector_version"] == "1.0.2" and row["hostname"] == "AMH-PC"


def test_enrollment_does_not_touch_existing_tokens(world):
    _run(world, "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, installation_id) "
                "VALUES ('legacy-hash', 10, 1, 'NBPL-style legacy token', NULL)")
    code = _generate(world, 101)["enrollment_code"]

    _redeem(world, code)

    legacy, enrolled = _tokens(world)
    assert legacy["token_hash"] == "legacy-hash" and legacy["is_active"] in (1, True)
    assert legacy["installation_id"] is None and legacy["description"] == "NBPL-style legacy token"
    assert enrolled["installation_id"] == 101


def test_redemption_marks_the_code_used_and_stores_neither_raw_secret(world):
    code = _generate(world, 101)["enrollment_code"]

    result = _redeem(world, code)

    (row,) = _codes(world)
    assert row["used_at"] is not None and row["revoked_at"] is None
    stored = _everything_stored(world)
    assert result["agent_token"] not in stored
    assert code not in stored and code.replace("-", "") not in stored


def test_hostname_and_version_are_optional_sanitized_metadata_only(world):
    code = _generate(world, 101)["enrollment_code"]

    result = _redeem(world, code, hostname="bad\r\nhost\x00name" + "x" * 500, collector_version=None)

    assert result["installation_id"] == 101
    (token,) = _tokens(world)
    assert "\r" not in token["description"] and "\n" not in token["description"] and "\x00" not in token["description"]
    assert len(token["description"]) < 400


def test_a_hostname_matching_another_installation_never_selects_it(world):
    code = _generate(world, 101)["enrollment_code"]  # 101's hostname is AMH-PC

    result = _redeem(world, code, hostname="AMH-PC-2")  # 102's hostname

    assert result["installation_id"] == 101


def test_the_code_selects_the_installation_not_the_branch(world):
    # Two installations share branch 1: each code yields a token for its own installation.
    a = _redeem(world, _generate(world, 101)["enrollment_code"])
    b = _redeem(world, _generate(world, 102)["enrollment_code"])

    assert (a["installation_id"], b["installation_id"]) == (101, 102)
    assert [t["installation_id"] for t in _tokens(world)] == [101, 102]


@pytest.mark.parametrize("typed", ["upper", "lower-no-dashes", "spaced"])
def test_a_typed_code_is_accepted_however_it_is_formatted(world, typed):
    code = _generate(world, 101)["enrollment_code"]
    entry = {"upper": code, "lower-no-dashes": code.replace("-", "").lower(),
             "spaced": code.replace("-", " ")}[typed]

    assert _redeem(world, entry)["installation_id"] == 101


# ======================================================================================
# redemption: refusals -- nothing is issued, the code stays as it was
# ======================================================================================

def _assert_refused(engine, code, reason, *, code_still_unused=True, tokens_before=0):
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _redeem(engine, code)
    assert _reason(excinfo) == reason
    assert len(_tokens(engine)) == tokens_before
    if code_still_unused:
        assert all(r["used_at"] is None for r in _codes(engine))


def test_an_unknown_code_is_refused(world):
    _assert_refused(world, enrollment.new_enrollment_code(), "unknown_code")


@pytest.mark.parametrize("bad", ["", "garbage", "SV-1111-1111-1111-1111", "x" * 200, "SV-ABCD-EFGH"])
def test_a_malformed_code_is_refused_without_reaching_the_database(world, bad):
    _assert_refused(world, bad, "malformed_code")


def test_an_expired_code_is_refused_and_the_boundary_is_exclusive(world):
    code = _generate(world, 101)["enrollment_code"]  # expires NOW + 30m

    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _redeem(world, code, now=NOW + timedelta(minutes=31))
    assert _reason(excinfo) == "expired"
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _redeem(world, code, now=NOW + timedelta(minutes=30))  # exactly at expires_at
    assert _reason(excinfo) == "expired"
    assert _tokens(world) == []
    assert _redeem(world, code, now=NOW + timedelta(minutes=29, seconds=59))["installation_id"] == 101


def test_a_used_code_cannot_be_redeemed_again(world):
    code = _generate(world, 101)["enrollment_code"]
    _redeem(world, code)

    _assert_refused(world, code, "already_used", code_still_unused=False, tokens_before=1)


def test_a_revoked_code_is_refused(world):
    code = _generate(world, 101)["enrollment_code"]
    _run(world, "UPDATE collector_enrollment_codes SET revoked_at = CURRENT_TIMESTAMP")

    _assert_refused(world, code, "revoked", code_still_unused=False)
    assert _tokens(world) == []


def test_a_code_for_an_installation_deactivated_after_generation_is_refused(world):
    code = _generate(world, 101)["enrollment_code"]
    _run(world, "UPDATE collector_installations SET status = 'inactive' WHERE id = 101")

    _assert_refused(world, code, "installation_status_inactive")


def test_a_code_for_an_installation_retired_after_generation_is_refused(world):
    code = _generate(world, 102)["enrollment_code"]
    _run(world, "UPDATE collector_installations SET status = 'retired' WHERE id = 102")

    _assert_refused(world, code, "installation_status_retired")


def test_a_code_whose_installation_was_deleted_is_refused(world):
    code = _generate(world, 101)["enrollment_code"]
    _run(world, "DELETE FROM collector_installations WHERE id = 101")

    _assert_refused(world, code, "installation_not_found")


@pytest.mark.parametrize("status", ["suspended", "cancelled"])
def test_a_code_for_a_suspended_or_cancelled_organization_is_refused(world, status):
    code = _generate(world, 101)["enrollment_code"]
    _run(world, "UPDATE organizations SET status = :s WHERE id = 1", s=status)

    _assert_refused(world, code, f"organization_status_{status}")


def test_a_code_for_an_inactive_branch_is_refused(world):
    code = _generate(world, 101)["enrollment_code"]
    _run(world, "UPDATE branches SET status = 'inactive' WHERE id = 1")

    _assert_refused(world, code, "branch_status_inactive")


def test_generation_refuses_the_same_unusable_tenants(world):
    _run(world, "UPDATE organizations SET status = 'suspended' WHERE id = 1")
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _generate(world, 101)
    assert _reason(excinfo) == "organization_status_suspended"

    _run(world, "UPDATE organizations SET status = 'active' WHERE id = 1")
    _run(world, "UPDATE branches SET status = 'inactive' WHERE id = 1")
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _generate(world, 101)
    assert _reason(excinfo) == "branch_status_inactive"
    assert _codes(world) == []


# --- bad / mismatched operational identity ---------------------------------------------

_IDENTITY_BREAKS = {
    # scenario -> (SQL that breaks the mapping AFTER the code was generated, expected reason)
    "organization_unmapped": ("UPDATE organizations SET operational_customer_id = NULL WHERE id = 1",
                              "organization_not_operationally_mapped"),
    "branch_unmapped": ("UPDATE branches SET operational_branch_id = NULL WHERE id = 1",
                        "branch_not_operationally_mapped"),
    "branch_mapping_inconsistent": ("UPDATE branches SET operational_branch_id = 99 WHERE id = 1",
                                    "branch_operational_mapping_inconsistent"),
    "branch_belongs_to_another_org": ("UPDATE branches SET organization_id = 2 WHERE id = 1",
                                      "branch_belongs_to_another_organization"),
    "operational_customer_missing": ("DELETE FROM customers WHERE id = 10", "operational_customer_missing"),
    "operational_customer_ambiguous": ("UPDATE organizations SET operational_customer_id = 10 WHERE id = 2",
                                       "operational_customer_ambiguous"),
    "branch_missing": ("DELETE FROM branches WHERE id = 1", "branch_missing"),
}


@pytest.mark.parametrize("scenario", list(_IDENTITY_BREAKS))
def test_a_bad_or_mismatched_operational_identity_never_yields_a_token(world, scenario):
    sql, reason = _IDENTITY_BREAKS[scenario]
    code = _generate(world, 101)["enrollment_code"]
    _run(world, sql)

    _assert_refused(world, code, reason)


@pytest.mark.parametrize("scenario", list(_IDENTITY_BREAKS))
def test_generation_refuses_the_same_broken_identities(world, scenario):
    sql, reason = _IDENTITY_BREAKS[scenario]
    _run(world, sql)

    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _generate(world, 101)

    assert _reason(excinfo) == reason
    assert _codes(world) == []


@pytest.mark.parametrize("scenario", ["organization_unmapped", "branch_unmapped", "branch_mapping_inconsistent",
                                      "branch_belongs_to_another_org", "operational_customer_missing",
                                      "operational_customer_ambiguous"])
def test_scope_validation_agrees_with_the_agent_token_script(world, scenario):
    """Whatever create_agent_token.py refuses for the operational pair, enrollment refuses too."""
    sql, _reason_text = _IDENTITY_BREAKS[scenario]
    _run(world, sql)

    customer, operational_branch = 10, 1  # what a token for installation 101 would be scoped to
    with world.connect() as conn, pytest.raises(create_agent_token_script.ScopeError):
        create_agent_token_script.validate_token_scope(conn, customer, operational_branch)
    with pytest.raises(enrollment.EnrollmentError):
        _generate(world, 101)


def test_the_clean_install_style_saas_only_tenant_is_refused(db):
    # SaaS ids exist, but the operational bridge columns are NULL: not provisioned.
    _tenant(db, 2, None, 2, None)
    _installation(db, 21, 2, 2)

    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _generate(db, 21)

    assert _reason(excinfo) == "organization_not_operationally_mapped"


# --- atomicity ---------------------------------------------------------------------------------

def test_a_failure_while_issuing_the_token_leaves_the_code_unused(world, monkeypatch):
    code = _generate(world, 101)["enrollment_code"]
    _run(world, "INSERT INTO agent_tokens (token_hash, customer_id, branch_id) VALUES ('taken', 10, 1)")
    monkeypatch.setattr(enrollment, "new_agent_token", lambda: ("raw-token", "taken"))  # unique-hash clash

    with pytest.raises(Exception, match="UNIQUE|unique|constraint"):
        _redeem(world, code)

    assert all(r["used_at"] is None for r in _codes(world))  # rolled back with the token insert
    assert len(_tokens(world)) == 1
    monkeypatch.undo()
    assert _redeem(world, code)["installation_id"] == 101  # and the code is still good


class _HookedEngine:
    """Runs `hook(sql, conn)` before each statement -- to stand in for another
    session committing between two of the redemption's own statements."""

    def __init__(self, engine, hook):
        self._engine, self._hook = engine, hook

    def begin(self):
        return _HookedContext(self._engine.begin(), self._hook)


class _HookedContext:
    def __init__(self, context, hook):
        self._context, self._hook = context, hook

    def __enter__(self):
        return _HookedConnection(self._context.__enter__(), self._hook)

    def __exit__(self, *exc):
        return self._context.__exit__(*exc)


class _HookedConnection:
    def __init__(self, conn, hook):
        self._conn, self._hook = conn, hook
        self.dialect = conn.dialect

    def execute(self, statement, params=None):
        self._hook(str(statement), self._conn)
        return self._conn.execute(statement) if params is None else self._conn.execute(statement, params)


def test_a_code_claimed_between_the_lookup_and_the_update_issues_no_token(world):
    """The guarded UPDATE is the last line of defence when a second redemption
    slips in between the lookup and the claim (on PostgreSQL the row lock stops
    that; the guard must hold without it)."""
    code = _generate(world, 101)["enrollment_code"]

    def steal_the_code(sql, conn):
        if "SET used_at" in sql:
            conn.execute(text("UPDATE collector_enrollment_codes SET used_at = CURRENT_TIMESTAMP"))

    with pytest.raises(enrollment.EnrollmentError) as excinfo, \
            _HookedEngine(world, steal_the_code).begin() as conn:
        enrollment.redeem_enrollment_code(conn, code, now=NOW + timedelta(minutes=1))

    assert _reason(excinfo) == "lost_redemption_race"
    assert _tokens(world) == []  # and the claimed-by-someone-else state was rolled back with it


def test_double_redemption_yields_exactly_one_token(world):
    code = _generate(world, 101)["enrollment_code"]
    outcomes = []
    for _ in range(3):
        try:
            outcomes.append(_redeem(world, code)["installation_id"])
        except enrollment.EnrollmentError as exc:
            outcomes.append(exc.reason)

    assert outcomes == [101, "already_used", "already_used"]
    assert len(_tokens(world)) == 1


# ======================================================================================
# secrets never logged
# ======================================================================================

def test_neither_the_code_nor_the_token_is_ever_logged(world, caplog):
    caplog.set_level("DEBUG")
    generated = _generate(world, 101, created_by_user_id=3)
    code = generated["enrollment_code"]
    with pytest.raises(enrollment.EnrollmentError):
        _redeem(world, "SV-ZZZZ-ZZZZ-ZZZZ-ZZZZ")
    issued = _redeem(world, code, hostname="AMH-PC")
    with pytest.raises(enrollment.EnrollmentError):
        _redeem(world, code)

    logged = caplog.text
    assert "Collector enrollment code generated" in logged and "Collector enrollment redeemed" in logged
    for secret in (code, code.replace("-", ""), issued["agent_token"], "ZZZZ-ZZZZ"):
        assert secret not in logged


# ======================================================================================
# POST /collector/enroll
# ======================================================================================

# The endpoint takes no `now`: it reads the wall clock (redeem_enrollment_code(now=None)). _generate, though,
# stamps codes at the fixed NOW, so under the real clock every HTTP test silently expired 30 minutes after NOW in
# real time. _enroll therefore pins the service's clock to the instant _redeem uses for direct calls -- the
# same relationship (generated at NOW, redeemed a minute later) on both paths, whatever day the suite runs.
ENDPOINT_NOW = NOW + timedelta(minutes=1)


class _InstanceOfRealDatetime(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, datetime)  # the service still sees its real, timezone-aware datetimes as datetimes


class _FrozenDatetime(datetime, metaclass=_InstanceOfRealDatetime):
    """`datetime` as the service module sees it, with only now() fixed; everything else is the real class."""

    @classmethod
    def now(cls, tz=None):
        return ENDPOINT_NOW.astimezone(tz) if tz else ENDPOINT_NOW.replace(tzinfo=None)


def _enroll(code, **extra):
    with patch.object(enrollment, "datetime", _FrozenDatetime):
        return client.post("/collector/enroll", json={"enrollment_code": code, **extra})


def test_the_endpoint_clock_is_pinned_so_a_fresh_code_is_unexpired_and_the_expiry_boundary_is_exact(world):
    fresh = _generate(world, 101)["enrollment_code"]  # expires NOW + 30m; the endpoint judges at NOW + 1m
    assert _enroll(fresh).status_code == 200

    at_the_boundary = _insert_code(world, 102, expires_at=ENDPOINT_NOW)  # expires exactly when the endpoint checks
    assert _enroll(at_the_boundary).status_code == 400

    just_valid = _insert_code(world, 201, expires_at=ENDPOINT_NOW + timedelta(seconds=1))
    assert _enroll(just_valid).status_code == 200


def test_the_pinned_clock_changes_nothing_else_the_service_does_with_datetimes(world):
    with patch.object(enrollment, "datetime", _FrozenDatetime):
        assert enrollment.datetime.now(UTC) == ENDPOINT_NOW
        assert isinstance(NOW, enrollment.datetime) and isinstance(datetime.now(UTC), enrollment.datetime)
    assert enrollment.datetime is datetime  # restored afterwards: the super-admin tests below use the real clock


def test_the_endpoint_needs_no_token_and_returns_exactly_the_four_fields(world):
    code = _generate(world, 101)["enrollment_code"]

    response = _enroll(code, hostname="AMH-PC", collector_version="1.0.4")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"customer_id", "branch_id", "installation_id", "agent_token"}
    assert (body["customer_id"], body["branch_id"], body["installation_id"]) == (10, 1, 101)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    (token,) = _tokens(world)
    assert token["token_hash"] == hashlib.sha256(body["agent_token"].encode("utf-8")).hexdigest()
    assert body["agent_token"] not in _everything_stored(world)


def test_hostname_and_version_are_optional_in_the_request(world):
    code = _generate(world, 101)["enrollment_code"]

    assert _enroll(code).status_code == 200


def test_the_enrolled_token_authenticates_a_heartbeat_and_that_is_what_confirms_contact(world):
    body = _enroll(_generate(world, 101)["enrollment_code"]).json()
    installation = next(r for r in _installations(world) if r["id"] == 101)
    assert installation["installed_at"] is None and installation["last_seen_at"] is None  # enrollment alone

    heartbeat = client.post(
        "/upload-pipeline-status",
        json={"customer_id": body["customer_id"], "branch_id": body["branch_id"], "status": "completed",
              "installation_id": body["installation_id"], "collector_version": "1.0.4"},
        headers={"Authorization": f"Bearer {body['agent_token']}"},
    )

    assert heartbeat.status_code == 200
    installation = next(r for r in _installations(world) if r["id"] == 101)
    assert installation["status"] == "active"
    assert installation["installed_at"] is not None and installation["last_seen_at"] is not None
    assert installation["collector_version"] == "1.0.4"


def _used_code(engine):
    code = _generate(engine, 101)["enrollment_code"]
    _redeem(engine, code)
    return code


_REFUSAL_SETUPS = {
    "unknown": lambda db: enrollment.new_enrollment_code(),
    "malformed": lambda db: "not-a-code",
    "used": lambda db: _used_code(db),
    "expired": lambda db: _insert_code(db, 101, expires_at=datetime(2020, 1, 1, tzinfo=UTC)),
    "revoked": lambda db: _insert_code(db, 101, revoked=True),
    "inactive_installation": lambda db: _insert_code(db, 103),
    "retired_installation": lambda db: _insert_code(db, 104),
    "suspended_org": lambda db: (_run(db, "UPDATE organizations SET status='suspended' WHERE id=1"),
                                 _insert_code(db, 101))[1],
    "cancelled_org": lambda db: (_run(db, "UPDATE organizations SET status='cancelled' WHERE id=1"),
                                 _insert_code(db, 101))[1],
    "inactive_branch": lambda db: (_run(db, "UPDATE branches SET status='inactive' WHERE id=1"),
                                   _insert_code(db, 101))[1],
    "unmapped_org": lambda db: (_run(db, "UPDATE organizations SET operational_customer_id=NULL WHERE id=1"),
                                _insert_code(db, 101))[1],
    "unmapped_branch": lambda db: (_run(db, "UPDATE branches SET operational_branch_id=NULL WHERE id=1"),
                                   _insert_code(db, 101))[1],
}


@pytest.mark.parametrize("scenario", list(_REFUSAL_SETUPS))
def test_every_refusal_is_the_same_generic_400_that_reveals_nothing(world, scenario):
    code = _REFUSAL_SETUPS[scenario](world)
    tokens_before = len(_tokens(world))

    response = _enroll(code)

    assert response.status_code == 400
    assert response.json() == PUBLIC
    assert len(_tokens(world)) == tokens_before  # nothing issued by THIS request


def test_refusal_bodies_are_byte_identical_across_every_reason(world):
    bodies = set()
    for scenario, setup in _REFUSAL_SETUPS.items():
        main.limiter.reset()
        # Each scenario starts from a fresh, unbroken world.
        with world.begin() as conn:
            for table in ("agent_tokens", "collector_enrollment_codes", "collector_installations",
                          "branches", "organizations", "customers"):
                conn.execute(text(f"DELETE FROM {table}"))  # nosec B608
        _tenant(world, 1, 10, 1, 1)
        _tenant(world, 2, 11, 2, 2, org_status="trial")
        _installation(world, 101, 1, 1)
        _installation(world, 103, 1, 1, status="inactive")
        _installation(world, 104, 1, 1, status="retired")
        response = _enroll(setup(world))
        assert response.status_code == 400, scenario
        bodies.add((response.status_code, response.text))

    assert len(bodies) == 1
    text_lower = next(iter(bodies))[1].lower()
    for word in ("suspend", "cancel", "inactive", "retired", "organization", "branch", "installation",
                 "expired", "used", "revoked", "unknown", "mapped"):
        assert word not in text_lower.replace("invalid or expired enrollment code", "")


def test_the_response_never_confirms_a_code_exists_for_a_missing_vs_present_installation(world):
    known_but_dead = _insert_code(world, 103)

    assert _enroll(known_but_dead).text == _enroll(enrollment.new_enrollment_code()).text


def test_a_missing_code_field_is_a_422_and_nothing_is_issued(world):
    assert client.post("/collector/enroll", json={"hostname": "AMH-PC"}).status_code == 422
    assert _tokens(world) == []


@pytest.mark.parametrize("payload", [
    {"enrollment_code": "x" * 65},
    {"enrollment_code": "SV-ABCD-EFGH-JKMN-PQRS", "hostname": "h" * 256},
    {"enrollment_code": "SV-ABCD-EFGH-JKMN-PQRS", "collector_version": "v" * 65},
])
def test_oversized_fields_are_a_422(world, payload):
    assert client.post("/collector/enroll", json=payload).status_code == 422


def test_the_request_model_masks_the_code_in_its_repr():
    request = main.CollectorEnrollRequest(enrollment_code="SV-ABCD-EFGH-JKMN-PQRS", hostname="h")

    assert "ABCD" not in repr(request) and "ABCD" not in str(request)
    assert request.enrollment_code.get_secret_value() == "SV-ABCD-EFGH-JKMN-PQRS"


def test_no_secret_reaches_the_logs_through_the_endpoint(world, caplog):
    caplog.set_level("DEBUG")
    code = _generate(world, 101)["enrollment_code"]
    body = _enroll(code).json()
    _enroll(code)  # already used -> logged as a rejection
    _enroll("SV-ZZZZ-ZZZZ-ZZZZ-ZZZZ")

    assert "Collector enrollment rejected" in caplog.text
    for secret in (code, code.replace("-", ""), body["agent_token"]):
        assert secret not in caplog.text


def test_an_unexpected_failure_is_a_generic_500_with_no_detail(world, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("database exploded: secret-internal-detail")

    monkeypatch.setattr(main, "redeem_enrollment_code", boom)

    response = _enroll(enrollment.new_enrollment_code())

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "secret-internal-detail" not in response.text


def test_concurrent_looking_double_submission_over_http_issues_one_token(world):
    code = _generate(world, 101)["enrollment_code"]

    first, second = _enroll(code), _enroll(code)

    assert (first.status_code, second.status_code) == (200, 400)
    assert len(_tokens(world)) == 1


def test_the_endpoint_is_rate_limited_per_client(world):
    if main.ENROLL_RATE_LIMIT != "10/minute":
        pytest.skip("SORTVIEW_ENROLL_RATE_LIMIT is overridden in this environment")

    statuses = [_enroll(enrollment.new_enrollment_code()).status_code for _ in range(11)]

    assert statuses[:10] == [400] * 10
    assert statuses[10] == 429


# ======================================================================================
# token <-> installation binding on the heartbeat path
# ======================================================================================

def _heartbeat(token, *, customer_id=10, branch_id=1, **extra):
    return client.post(
        "/upload-pipeline-status",
        json={"customer_id": customer_id, "branch_id": branch_id, "status": "completed", **extra},
        headers={"Authorization": f"Bearer {token}"},
    )


def _legacy_token(engine, raw="legacy-raw-token", *, installation_id=None):
    """An existing-style token: stored as its SHA-256, installation_id NULL."""
    _run(engine, "INSERT INTO agent_tokens (token_hash, customer_id, branch_id, is_active, installation_id) "
                 "VALUES (:h, 10, 1, 1, :i)", h=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
         i=installation_id)
    return raw


def test_a_legacy_null_installation_token_still_works_for_any_installation_of_its_scope(world):
    token = _legacy_token(world)

    assert _heartbeat(token, installation_id=101).status_code == 200
    assert _heartbeat(token, installation_id=102).status_code == 200
    assert _heartbeat(token).status_code == 200  # and the no-installation legacy heartbeat
    assert {r["id"]: r["status"] for r in _installations(world)}[101] == "active"


def test_an_enrolled_token_cannot_claim_a_different_explicit_installation(world):
    body = _enroll(_generate(world, 101)["enrollment_code"]).json()  # bound to 101
    before = _installations(world)

    response = _heartbeat(body["agent_token"], installation_id=102)  # same branch, provisioning/active

    assert response.status_code == 403
    assert response.json() == {"detail": "Collector installation is not authorized to report status"}
    assert _installations(world) == before  # 102 untouched
    assert _table(world, "pipeline_status") == []  # whole heartbeat rolled back


def test_an_enrolled_token_may_report_its_own_installation_and_omit_the_id(world):
    body = _enroll(_generate(world, 101)["enrollment_code"]).json()
    token = body["agent_token"]

    assert _heartbeat(token, installation_id=101, collector_version="1.0.4").status_code == 200
    assert _heartbeat(token).status_code == 200  # a legacy-shaped heartbeat is not rejected


def test_the_binding_rejection_is_indistinguishable_from_other_installation_rejections(world):
    body = _enroll(_generate(world, 101)["enrollment_code"]).json()

    mismatch = _heartbeat(body["agent_token"], installation_id=102)
    unknown = _heartbeat(_legacy_token(world), installation_id=9999)

    assert (mismatch.status_code, mismatch.text) == (unknown.status_code, unknown.text)


def test_an_enrolled_token_bound_to_a_now_inactive_installation_is_still_refused_on_heartbeat(world):
    body = _enroll(_generate(world, 101)["enrollment_code"]).json()
    _run(world, "UPDATE collector_installations SET status = 'inactive' WHERE id = 101")

    response = _heartbeat(body["agent_token"], installation_id=101)

    assert response.status_code == 403  # fail closed
    assert response.json() == {"detail": "Agent is not currently authorized to upload data"}
    assert next(r for r in _installations(world) if r["id"] == 101)["status"] == "inactive"


def test_the_auth_lookup_resolves_the_bound_installation_by_its_explicit_id_only():
    sql = " ".join(main._AGENT_TOKEN_LOOKUP_SQL.split())

    assert "t.installation_id" in sql
    assert "LEFT JOIN collector_installations ci ON ci.id = t.installation_id" in sql
    assert "hostname" not in sql.lower()  # never a hostname/branch lookup
    assert "ci.status AS installation_status" in sql
    # Bound-installation checks compare against the SAME tenant the token's scope resolves to.
    assert "o.id AS resolved_organization_id" in sql and "b.id AS resolved_branch_id" in sql


def test_upload_never_touches_the_enrollment_tables_or_writes_installations(monkeypatch):
    executed = []

    class _Result:
        rowcount = 1

        def mappings(self):
            return self

        def first(self):
            return {"id": 1, "customer_id": 1, "branch_id": 1, "is_active": True, "description": "t",
                    "organization_status": "active", "branch_status": "active"}

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
        json={"checkins": [{"customer_id": 1, "branch_id": 1, "barcode": "1", "event_time": "2026-09-20T09:00:00"}]},
        headers={"Authorization": "Bearer t"},
    )

    assert response.status_code == 200
    assert not any("collector_enrollment_codes" in s for s in executed)
    assert not any("UPDATE collector_installations" in s or "INSERT INTO collector_installations" in s
                   for s in executed)


# ======================================================================================
# Super Admin
# ======================================================================================

@pytest.fixture
def super_admin_engine(world, monkeypatch):
    """The Super Admin entry point runs through the service's own engine hook."""
    monkeypatch.setattr(enrollment, "_get_engine", lambda: world)
    return world


def test_super_admin_generation_returns_the_code_once_and_records_the_admin(super_admin_engine):
    result = enrollment.generate_enrollment_code_for_installation(101, created_by_user_id=5)

    assert CODE_PATTERN.match(result["enrollment_code"])
    assert result["installation_id"] == 101 and result["installation_name"] == "Main AMH Sorter"
    assert result["ttl_minutes"] == 30 and result["expires_at"] > datetime.now(UTC)
    (row,) = _codes(super_admin_engine)
    assert row["created_by_user_id"] == 5
    assert result["enrollment_code"] not in _everything_stored(super_admin_engine)


def test_super_admin_generation_invalidates_the_previous_unused_code_for_that_installation(super_admin_engine):
    first = enrollment.generate_enrollment_code_for_installation(101)
    second = enrollment.generate_enrollment_code_for_installation(101)

    assert second["revoked_previous_count"] == 1
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        _redeem(super_admin_engine, first["enrollment_code"], now=datetime.now(UTC))
    assert _reason(excinfo) == "revoked"
    assert _redeem(super_admin_engine, second["enrollment_code"], now=datetime.now(UTC))["installation_id"] == 101


def test_super_admin_generation_is_scoped_to_the_selected_installation_and_organization(super_admin_engine):
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        enrollment.generate_enrollment_code_for_installation(201, expected_organization_id=1)
    assert _reason(excinfo) == "installation_organization_mismatch"

    enrollment.generate_enrollment_code_for_installation(101, expected_organization_id=1)
    other = enrollment.generate_enrollment_code_for_installation(102, expected_organization_id=1)

    # Generating for 101 twice never disturbed 102's code.
    enrollment.generate_enrollment_code_for_installation(101, expected_organization_id=1)
    assert _redeem(super_admin_engine, other["enrollment_code"], now=datetime.now(UTC))["installation_id"] == 102


def test_super_admin_generation_refuses_an_unenrollable_installation(super_admin_engine):
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        enrollment.generate_enrollment_code_for_installation(104)  # retired

    assert _reason(excinfo) == "installation_status_retired"
    assert _codes(super_admin_engine) == []


_MANAGE_PAGE = "super_admin/pages/Manage_Libraries.py"


def _manage_source() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / _MANAGE_PAGE).read_text(encoding="utf-8")


def test_manage_libraries_offers_generate_enrollment_code_for_the_selected_installation_only():
    source = _manage_source()
    action = source[source.index('"Generate Enrollment Code"'):]
    action = action[: action.index("else:\n            # Shown once")]

    assert "installation_id=selected_installation_id" in action
    assert "expected_organization_id=organization_id" in action
    assert 'created_by_user_id=int(auth_user["id"])' in action
    assert "key=f\"generate_enrollment_code_{selected_installation_id}\"" in source


def test_manage_libraries_shows_the_code_name_id_expiry_and_a_single_use_warning():
    source = _manage_source()
    shown = source[source.index("# Shown once, in this run only"):]
    shown = shown[: shown.index("if generated[\"revoked_previous_count\"]")]

    assert 'st.code(generated["enrollment_code"]' in shown
    assert "installation_name" in shown and "installation_id" in shown
    assert "expires_at" in shown and "UTC" in shown
    assert "SINGLE USE" in shown and "cannot be displayed again" in shown


def test_manage_libraries_never_keeps_the_code_and_never_exposes_a_permanent_token():
    source = _manage_source()

    assert "st.session_state" not in source[source.index("Generate Enrollment Code"):
                                            source.index("Collector Installer Values")]
    assert "agent_token" not in source
    # No rerun after generating: the code stays on screen only for this run.
    assert "st.rerun()" not in source[source.index("# Shown once, in this run only"):
                                      source.index('st.info("No collector installations')]


def test_manage_libraries_only_offers_enrollment_for_provisioning_or_active_installations():
    source = _manage_source()

    assert "ALLOWED_INSTALLATION_STATUSES as ENROLLABLE_INSTALLATION_STATUSES" in source
    assert 'installation["status"] not in ENROLLABLE_INSTALLATION_STATUSES' in source
    assert enrollment.ALLOWED_INSTALLATION_STATUSES == ("provisioning", "active")


def test_manage_libraries_keeps_the_manual_operational_id_display_and_the_legacy_config():
    source = _manage_source()

    for expected in ("Operational Customer ID", "Operational Branch ID", "Installation ID",
                     "Collector Installer Values", "Download agent_config.json",
                     "format_collector_install_parameters"):
        assert expected in source, expected


# ======================================================================================
# authorization tightening: a BOUND token dies with its installation, on every endpoint
# ======================================================================================

GENERIC_403 = {"detail": "Agent is not currently authorized to upload data"}


def _enrolled(world, installation_id=101):
    return _enroll(_generate(world, installation_id)["enrollment_code"]).json()


def _token_last_used(engine, token):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT last_used_at FROM agent_tokens WHERE token_hash = :h"),
            {"h": hashlib.sha256(token.encode("utf-8")).hexdigest()},
        ).scalar()


def _upload(token, *, customer_id=10, branch_id=1):
    return client.post(
        "/upload",
        json={"checkins": [{"customer_id": customer_id, "branch_id": branch_id, "barcode": "1",
                            "event_time": "2026-09-20T09:00:00"}]},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize("status", ["inactive", "retired"])
def test_a_bound_token_stops_authenticating_when_its_installation_is_inactive_or_retired(world, status):
    token = _enrolled(world)["agent_token"]
    _run(world, "UPDATE collector_installations SET status = :s WHERE id = 101", s=status)
    installations_before = _installations(world)

    for extra in ({}, {"installation_id": 101}, {"installation_id": 102}):
        response = _heartbeat(token, **extra)
        assert (response.status_code, response.json()) == (403, GENERIC_403), extra

    assert _token_last_used(world, token) is None  # never marked used
    assert _installations(world) == installations_before  # nothing revived or touched
    assert _table(world, "pipeline_status") == []


@pytest.mark.parametrize("status", ["inactive", "retired"])
def test_a_bound_token_cannot_upload_when_its_installation_is_inactive_or_retired(world, status):
    token = _enrolled(world)["agent_token"]
    _run(world, "UPDATE collector_installations SET status = :s WHERE id = 101", s=status)

    response = _upload(token)

    # Rejected by authentication, before any row is written (the SQLite fixture has no
    # checkins table, so reaching the INSERT would have been a 500, not a 403).
    assert (response.status_code, response.json()) == (403, GENERIC_403)


@pytest.mark.parametrize("status", ["provisioning", "active"])
def test_a_bound_token_authenticates_while_its_installation_is_provisioning_or_active(world, status):
    token = _enrolled(world, 101)["agent_token"]
    _run(world, "UPDATE collector_installations SET status = :s WHERE id = 101", s=status)

    assert _heartbeat(token, installation_id=101).status_code == 200
    assert _heartbeat(token).status_code == 200
    assert _token_last_used(world, token) is not None


def test_the_gate_is_live_reactivating_the_installation_makes_the_same_token_work_again(world):
    token = _enrolled(world)["agent_token"]
    _run(world, "UPDATE collector_installations SET status = 'retired' WHERE id = 101")
    assert _heartbeat(token).status_code == 403

    _run(world, "UPDATE collector_installations SET status = 'active' WHERE id = 101")

    assert _heartbeat(token).status_code == 200  # no token was revoked or re-issued in between
    assert len(_tokens(world)) == 1


def test_legacy_tokens_are_unaffected_by_installation_status(world):
    token = _legacy_token(world)
    _run(world, "UPDATE collector_installations SET status = 'retired' WHERE organization_id = 1")

    assert _heartbeat(token).status_code == 200
    assert _heartbeat(token, installation_id=102).status_code == 403  # retired: existing heartbeat rule
    assert _heartbeat(token).status_code == 200
    (row,) = [t for t in _tokens(world) if t["installation_id"] is None]
    assert row["is_active"] in (1, True)


def test_a_bound_token_whose_installation_belongs_to_another_organization_is_refused(world):
    token = _enrolled(world)["agent_token"]  # bound to 101 (org 1 / branch 1)
    _run(world, "UPDATE collector_installations SET organization_id = 2, branch_id = 2 WHERE id = 101")

    assert (_heartbeat(token).status_code, _heartbeat(token).json()) == (403, GENERIC_403)


def test_a_bound_token_whose_installation_moved_to_another_branch_is_refused(world):
    _tenant(world, 3, 12, 3, 3)  # a second branch is needed under org 1
    _run(world, "INSERT INTO branches VALUES (4, 1, 'active', 4)")
    token = _enrolled(world)["agent_token"]
    _run(world, "UPDATE collector_installations SET branch_id = 4 WHERE id = 101")

    assert _heartbeat(token).status_code == 403


def test_a_bound_token_whose_installation_row_no_longer_exists_is_refused(world):
    token = _enrolled(world)["agent_token"]
    _run(world, "DELETE FROM collector_installations WHERE id = 101")  # (PostgreSQL would cascade the token too)

    assert (_heartbeat(token).status_code, _heartbeat(token).json()) == (403, GENERIC_403)


def test_the_bound_installation_gate_reveals_nothing_the_tenant_gate_does_not(world):
    bound = _enrolled(world)["agent_token"]
    _run(world, "UPDATE collector_installations SET status = 'retired' WHERE id = 101")
    installation_refusal = _heartbeat(bound)
    _run(world, "UPDATE collector_installations SET status = 'active' WHERE id = 101")
    _run(world, "UPDATE organizations SET status = 'suspended' WHERE id = 1")
    tenant_refusal = _heartbeat(bound)

    assert (installation_refusal.status_code, installation_refusal.text) == \
           (tenant_refusal.status_code, tenant_refusal.text)
    for word in ("installation", "retired", "inactive", "suspend", "bound"):
        assert word not in installation_refusal.text.lower()


def test_earlier_gates_keep_their_own_messages_and_precedence(world):
    token = _enrolled(world)["agent_token"]
    _run(world, "UPDATE collector_installations SET status = 'retired' WHERE id = 101")

    assert _heartbeat(token, customer_id=10, branch_id=99).json() == \
        {"detail": "Token scope does not match customer_id / branch_id"}  # scope gate first
    assert _heartbeat("not-a-token").status_code == 401
    _run(world, "UPDATE agent_tokens SET is_active = 0")
    assert _heartbeat(token).json() == {"detail": "Agent token is inactive"}  # inactive-token gate first


def test_an_unusable_bound_installation_is_logged_with_a_reason_but_never_a_secret(world, caplog):
    caplog.set_level("DEBUG")
    token = _enrolled(world)["agent_token"]
    _run(world, "UPDATE collector_installations SET status = 'retired' WHERE id = 101")

    _heartbeat(token)

    assert "bound installation 101 status 'retired'" in caplog.text
    assert token not in caplog.text


def test_bound_installation_unusable_reason_covers_each_case():
    def row(**overrides):
        base = {"installation_id": 5, "installation_status": "active", "installation_organization_id": 1,
                "resolved_organization_id": 1, "installation_branch_id": 2, "resolved_branch_id": 2}
        base.update(overrides)
        return base

    assert main.bound_installation_unusable_reason(row()) is None
    assert main.bound_installation_unusable_reason(row(installation_status="provisioning")) is None
    assert main.bound_installation_unusable_reason({}) is None  # legacy token: no binding at all
    assert main.bound_installation_unusable_reason(row(installation_id=None, installation_status="retired")) is None
    assert "status 'inactive'" in main.bound_installation_unusable_reason(row(installation_status="inactive"))
    assert "status 'retired'" in main.bound_installation_unusable_reason(row(installation_status="retired"))
    assert "not found" in main.bound_installation_unusable_reason(row(installation_status=None))
    assert "another organization" in main.bound_installation_unusable_reason(row(installation_organization_id=9))
    assert "another branch" in main.bound_installation_unusable_reason(row(installation_branch_id=9))
