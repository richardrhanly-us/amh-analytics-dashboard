"""One-time Collector enrollment: code generation and redemption.

FLOW. A Super Admin generates a short-lived, single-use enrollment code for ONE
explicit collector_installations row (create_enrollment_code). On the library's
machine the Collector redeems it over HTTPS (POST /collector/enroll ->
redeem_enrollment_code) and receives a long-lived agent token scoped to the
installation's OPERATIONAL customer/branch and bound to that installation
(agent_tokens.installation_id). The Collector never touches the database; this
module runs server-side only.

THIS MODULE IS DELIBERATELY FREE OF STREAMLIT AND OF ANY ENGINE. Both public
operations take an open SQLAlchemy connection, so the API (main.py) and Super
Admin (via generate_enrollment_code_for_installation) share one implementation
and tests can drive it against any database with the right tables.

IDENTITY. The tenant is resolved from the installation row, never from a
hostname, a branch guess, or a SaaS id posing as an operational one:
    collector_installations.id -> its organization and branch
    organizations.operational_customer_id  = the token's customer_id
    branches.operational_branch_id         = the token's branch_id
The pair must be fully mapped, unambiguous (exactly one organization maps to
that customer; the branch's operational id equals its own id and the branch
belongs to that organization), and the organization (active/trial) and branch
(active) must be usable -- the same rules main.authenticate_agent enforces on
every request and scripts/create_agent_token.py enforces when issuing a token.

SECRETS. The raw enrollment code exists only in the string returned to the
generating caller (shown once) and in the redeeming request; only its SHA-256
hex digest is stored. The raw agent token is returned once in the redemption
response; only its SHA-256 hex digest is stored (the scheme
scripts/create_agent_token.py and main.authenticate_agent already use). Neither
raw value is ever passed to a SQL statement or a log call here.

CONCURRENCY. Redemption locks the enrollment-code row (SELECT ... FOR UPDATE on
PostgreSQL) so two simultaneous redemptions serialize, and then claims it with a
guarded UPDATE (used_at IS NULL AND revoked_at IS NULL AND unexpired) whose
row count must be exactly 1 -- the guard alone already guarantees a single
winner on any database, the lock makes the loser wait and see the winner's
commit instead of racing it. Generation locks the installation row so two
concurrent generations for one installation cannot both leave an unused code.

ERRORS. Every redemption failure raises EnrollmentError carrying a specific
INTERNAL reason (for server logs and tests); the HTTP layer only ever shows
PUBLIC_ENROLLMENT_ERROR, so a caller cannot tell an unknown code from an
expired one, or learn anything about tenants, installations or their state.

This module never modifies collector_installations (installed_at, last_seen_at,
collector_version, hostname, status are all untouched): confirmed-contact
lifecycle belongs to the authenticated heartbeat/preflight.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, bindparam, text

logger = logging.getLogger("sortview.enrollment")

# --- enrollment code format ---------------------------------------------------
#
# SV-XXXX-XXXX-XXXX-XXXX: sixteen characters from a 31-character alphabet that
# leaves out the look-alikes 0/O and 1/I/L (about 79 bits of entropy from
# secrets.choice). Easy to read aloud and type; case and separators are ignored
# on entry.
CODE_PREFIX = "SV"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_GROUP_COUNT = 4
CODE_GROUP_LENGTH = 4
CODE_CHARACTER_COUNT = CODE_GROUP_COUNT * CODE_GROUP_LENGTH
_MAX_RAW_CODE_INPUT_LENGTH = 64
_CODE_SEPARATORS = re.compile(r"[\s\-_]+")

DEFAULT_ENROLLMENT_CODE_TTL = timedelta(minutes=30)

# The only enrollment failure text that ever leaves the server.
PUBLIC_ENROLLMENT_ERROR = "Invalid or expired enrollment code"

# Same rules as main.authenticate_agent / scripts/create_agent_token.py.
ALLOWED_INSTALLATION_STATUSES = ("provisioning", "active")
ALLOWED_ORGANIZATION_STATUSES = ("active", "trial")
ALLOWED_BRANCH_STATUS = "active"

_MAX_HOSTNAME_LENGTH = 128
_MAX_VERSION_LENGTH = 64
_MAX_INSTALLATION_NAME_IN_DESCRIPTION = 80
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class EnrollmentError(Exception):
    """An enrollment operation was refused. `reason` is INTERNAL (server logs,
    tests); it must never be sent to a client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- code and token primitives ----------------------------------------------------


def format_enrollment_code(characters: str) -> str:
    """'ABCD...' (16 code characters) -> 'SV-ABCD-EFGH-JKMN-PQRS'."""
    groups = [
        characters[i : i + CODE_GROUP_LENGTH] for i in range(0, CODE_CHARACTER_COUNT, CODE_GROUP_LENGTH)
    ]
    return "-".join([CODE_PREFIX, *groups])


def new_enrollment_code() -> str:
    """A fresh cryptographically random code in display format."""
    characters = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_CHARACTER_COUNT))
    return format_enrollment_code(characters)


def normalize_enrollment_code(value: str | None) -> str | None:
    """Canonical display form of whatever a person typed, or None if it cannot
    be a code: case, spaces, dashes and underscores are ignored, the SV prefix
    is optional. Anything of the wrong length or with characters outside the
    alphabet is not a code."""
    if not isinstance(value, str) or len(value) > _MAX_RAW_CODE_INPUT_LENGTH:
        return None
    compact = _CODE_SEPARATORS.sub("", value).upper()
    if compact.startswith(CODE_PREFIX) and len(compact) == len(CODE_PREFIX) + CODE_CHARACTER_COUNT:
        compact = compact[len(CODE_PREFIX) :]
    if len(compact) != CODE_CHARACTER_COUNT or any(ch not in CODE_ALPHABET for ch in compact):
        return None
    return format_enrollment_code(compact)


def hash_enrollment_code(canonical_code: str) -> str:
    return hashlib.sha256(canonical_code.encode("utf-8")).hexdigest()


def new_agent_token() -> tuple[str, str]:
    """(raw_token, sha256_hex_digest) -- the exact scheme of
    scripts/create_agent_token.generate_token, which main.authenticate_agent's
    `encode(digest(:token, 'sha256'), 'hex')` lookup matches."""
    raw_token = secrets.token_urlsafe(32)
    return raw_token, hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# --- helpers ----------------------------------------------------------------------------


def _clean_metadata(value: str | None, max_length: int) -> str | None:
    """Untrusted, informational text: control characters removed, trimmed,
    length-capped. Blank -> None."""
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARACTERS.sub("", value).strip()[:max_length].strip()
    return cleaned or None


def _lock_suffix(conn: Any, target: str = "") -> str:
    """Row locking where the database has it. SQLite (used by unit tests) has
    no FOR UPDATE and serializes writers itself; PostgreSQL is the only real
    target."""
    if getattr(getattr(conn, "dialect", None), "name", "") != "postgresql":
        return ""
    return f" FOR UPDATE OF {target}" if target else " FOR UPDATE"


def _timestamped(sql: str, **values: Any):
    """A text() statement whose `now`/`expires_at` binds are timezone-aware
    datetimes on every dialect."""
    statement = text(sql)
    typed = [
        bindparam(name, value=value, type_=DateTime(timezone=True))
        if isinstance(value, datetime)
        else bindparam(name, value=value)
        for name, value in values.items()
    ]
    return statement.bindparams(*typed)


# --- tenant resolution --------------------------------------------------------------------


@dataclass(frozen=True)
class InstallationScope:
    """A validated installation and the OPERATIONAL scope a token for it gets."""

    installation_id: int
    installation_name: str
    organization_id: int
    branch_id: int
    customer_id: int  # organizations.operational_customer_id
    operational_branch_id: int  # branches.operational_branch_id


_INSTALLATION_SCOPE_SQL = """
    SELECT
        ci.id AS installation_id,
        ci.name AS installation_name,
        ci.status AS installation_status,
        ci.organization_id AS organization_id,
        ci.branch_id AS branch_id,
        o.status AS organization_status,
        o.operational_customer_id AS operational_customer_id,
        b.id AS found_branch_id,
        b.organization_id AS branch_organization_id,
        b.status AS branch_status,
        b.operational_branch_id AS operational_branch_id
    FROM collector_installations ci
    JOIN organizations o
      ON o.id = ci.organization_id
    LEFT JOIN branches b
      ON b.id = ci.branch_id
    WHERE ci.id = :installation_id
"""


def resolve_installation_scope(
    conn: Any,
    installation_id: int,
    *,
    lock: bool = False,
    expected_organization_id: int | None = None,
) -> InstallationScope:
    """Validates that `installation_id` is an existing, provisioning/active
    installation of a usable tenant with a valid, unambiguous operational
    mapping, and returns the scope. Raises EnrollmentError(reason) otherwise.
    `lock` takes a row lock on the installation (PostgreSQL) for the caller's
    transaction. `expected_organization_id`, when given, additionally requires
    the installation to belong to that organization."""
    sql = _INSTALLATION_SCOPE_SQL + _lock_suffix(conn, "ci") if lock else _INSTALLATION_SCOPE_SQL
    row = conn.execute(text(sql), {"installation_id": installation_id}).mappings().first()

    if row is None:
        raise EnrollmentError("installation_not_found")
    if expected_organization_id is not None and int(row["organization_id"]) != int(expected_organization_id):
        raise EnrollmentError("installation_organization_mismatch")
    if row["installation_status"] not in ALLOWED_INSTALLATION_STATUSES:
        raise EnrollmentError(f"installation_status_{row['installation_status']}")
    if row["organization_status"] not in ALLOWED_ORGANIZATION_STATUSES:
        raise EnrollmentError(f"organization_status_{row['organization_status']}")
    if row["found_branch_id"] is None:
        raise EnrollmentError("branch_missing")
    if int(row["branch_organization_id"]) != int(row["organization_id"]):
        raise EnrollmentError("branch_belongs_to_another_organization")
    if row["branch_status"] != ALLOWED_BRANCH_STATUS:
        raise EnrollmentError(f"branch_status_{row['branch_status']}")

    customer_id = row["operational_customer_id"]
    operational_branch_id = row["operational_branch_id"]
    if customer_id is None:
        raise EnrollmentError("organization_not_operationally_mapped")
    if operational_branch_id is None:
        raise EnrollmentError("branch_not_operationally_mapped")
    if int(operational_branch_id) != int(row["found_branch_id"]):
        raise EnrollmentError("branch_operational_mapping_inconsistent")

    if conn.execute(
        text("SELECT 1 FROM customers WHERE id = :customer_id"), {"customer_id": customer_id}
    ).first() is None:
        raise EnrollmentError("operational_customer_missing")
    mapped_organizations = conn.execute(
        text("SELECT COUNT(*) FROM organizations WHERE operational_customer_id = :customer_id"),
        {"customer_id": customer_id},
    ).scalar_one()
    if int(mapped_organizations) != 1:
        raise EnrollmentError("operational_customer_ambiguous")

    return InstallationScope(
        installation_id=int(row["installation_id"]),
        installation_name=str(row["installation_name"]),
        organization_id=int(row["organization_id"]),
        branch_id=int(row["branch_id"]),
        customer_id=int(customer_id),
        operational_branch_id=int(operational_branch_id),
    )


# --- generation (Super Admin) --------------------------------------------------------------

_REVOKE_UNUSED_SQL = """
    UPDATE collector_enrollment_codes
    SET revoked_at = CURRENT_TIMESTAMP
    WHERE installation_id = :installation_id
      AND used_at IS NULL
      AND revoked_at IS NULL
"""

_INSERT_CODE_SQL = """
    INSERT INTO collector_enrollment_codes
        (installation_id, code_hash, expires_at, created_by_user_id)
    VALUES
        (:installation_id, :code_hash, :expires_at, :created_by_user_id)
    RETURNING id
"""


def create_enrollment_code(
    conn: Any,
    installation_id: int,
    *,
    created_by_user_id: int | None = None,
    expected_organization_id: int | None = None,
    ttl: timedelta = DEFAULT_ENROLLMENT_CODE_TTL,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generates a one-time enrollment code for `installation_id` inside the
    caller's transaction: validates the installation and its tenant, revokes the
    installation's earlier unused codes, stores the new code's SHA-256 hash, and
    returns the RAW code exactly once. Raises EnrollmentError if the
    installation cannot be enrolled."""
    scope = resolve_installation_scope(
        conn, installation_id, lock=True, expected_organization_id=expected_organization_id
    )
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + ttl

    revoked_previous = conn.execute(
        text(_REVOKE_UNUSED_SQL), {"installation_id": scope.installation_id}
    ).rowcount

    raw_code = new_enrollment_code()
    code_id = conn.execute(
        _timestamped(
            _INSERT_CODE_SQL,
            installation_id=scope.installation_id,
            code_hash=hash_enrollment_code(raw_code),
            expires_at=expires_at,
            created_by_user_id=created_by_user_id,
        )
    ).scalar_one()

    # Never the code itself -- only ids and timing.
    logger.info(
        "Collector enrollment code generated | code_id=%s installation_id=%s organization_id=%s "
        "created_by_user_id=%s expires_at=%s revoked_previous=%s",
        code_id, scope.installation_id, scope.organization_id, created_by_user_id,
        expires_at.isoformat(), revoked_previous,
    )
    return {
        "enrollment_code": raw_code,
        "enrollment_code_id": int(code_id),
        "installation_id": scope.installation_id,
        "installation_name": scope.installation_name,
        "organization_id": scope.organization_id,
        "expires_at": expires_at,
        "ttl_minutes": int(ttl.total_seconds() // 60),
        "revoked_previous_count": int(revoked_previous or 0),
    }


def _get_engine():
    # Imported here, not at module load: `database` pulls in Streamlit, and the
    # API (main.py) imports this module without wanting either.
    from database import get_engine

    return get_engine()


def generate_enrollment_code_for_installation(
    installation_id: int,
    *,
    created_by_user_id: int | None = None,
    expected_organization_id: int | None = None,
) -> dict[str, Any]:
    """Super Admin entry point: create_enrollment_code in its own transaction."""
    with _get_engine().begin() as conn:
        return create_enrollment_code(
            conn,
            installation_id,
            created_by_user_id=created_by_user_id,
            expected_organization_id=expected_organization_id,
        )


# --- redemption (Collector, via POST /collector/enroll) ----------------------------------------

_LOOKUP_CODE_SQL = """
    SELECT
        id,
        installation_id,
        used_at,
        revoked_at,
        expires_at <= :now AS is_expired
    FROM collector_enrollment_codes
    WHERE code_hash = :code_hash
"""

_CLAIM_CODE_SQL = """
    UPDATE collector_enrollment_codes
    SET used_at = CURRENT_TIMESTAMP
    WHERE id = :code_id
      AND used_at IS NULL
      AND revoked_at IS NULL
      AND expires_at > :now
"""

# (Bandit B105 flags the constant's name -- it holds SQL with bound parameters, not a
# credential; the token hash is a bind value, never interpolated.)
_INSERT_TOKEN_SQL = (  # nosec B105
    """
    INSERT INTO agent_tokens
        (token_hash, customer_id, branch_id, description, is_active, installation_id)
    VALUES
        (:token_hash, :customer_id, :branch_id, :description, TRUE, :installation_id)
    RETURNING id
"""
)


def _token_description(scope: InstallationScope, hostname: str | None, collector_version: str | None) -> str:
    parts = [
        "Collector enrollment",
        (
            f"installation {scope.installation_id} "
            f"({scope.installation_name[:_MAX_INSTALLATION_NAME_IN_DESCRIPTION]!r})"
        ),
    ]
    if hostname:
        parts.append(f"host {hostname}")
    if collector_version:
        parts.append(f"collector {collector_version}")
    return _CONTROL_CHARACTERS.sub("", " | ".join(parts))


def redeem_enrollment_code(
    conn: Any,
    raw_code: str,
    *,
    hostname: str | None = None,
    collector_version: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Redeems a one-time enrollment code inside the caller's transaction and
    returns exactly {customer_id, branch_id, installation_id, agent_token}. The
    caller commits; on any EnrollmentError (or other exception) it must roll
    back, which leaves the code unused and no token created.

    `hostname` and `collector_version` are untrusted, informational metadata:
    they only appear (sanitized) in the new token's description and never
    identify or select anything."""
    canonical = normalize_enrollment_code(raw_code)
    if canonical is None:
        raise EnrollmentError("malformed_code")
    checked_at = now or datetime.now(UTC)

    lookup_sql = _LOOKUP_CODE_SQL + _lock_suffix(conn)
    code_row = conn.execute(
        _timestamped(lookup_sql, code_hash=hash_enrollment_code(canonical), now=checked_at)
    ).mappings().first()

    if code_row is None:
        raise EnrollmentError("unknown_code")
    if code_row["used_at"] is not None:
        raise EnrollmentError("already_used")
    if code_row["revoked_at"] is not None:
        raise EnrollmentError("revoked")
    if bool(code_row["is_expired"]):
        raise EnrollmentError("expired")

    scope = resolve_installation_scope(conn, int(code_row["installation_id"]))

    claimed = conn.execute(
        _timestamped(_CLAIM_CODE_SQL, code_id=code_row["id"], now=checked_at)
    ).rowcount
    if claimed != 1:
        raise EnrollmentError("lost_redemption_race")

    agent_token, token_hash = new_agent_token()
    token_id = conn.execute(
        text(_INSERT_TOKEN_SQL),
        {
            "token_hash": token_hash,
            "customer_id": scope.customer_id,
            "branch_id": scope.operational_branch_id,
            "description": _token_description(
                scope,
                _clean_metadata(hostname, _MAX_HOSTNAME_LENGTH),
                _clean_metadata(collector_version, _MAX_VERSION_LENGTH),
            ),
            "installation_id": scope.installation_id,
        },
    ).scalar_one()

    # Ids only -- never the code or the token.
    logger.info(
        "Collector enrollment redeemed | code_id=%s token_id=%s installation_id=%s "
        "customer_id=%s branch_id=%s",
        code_row["id"], token_id, scope.installation_id, scope.customer_id, scope.operational_branch_id,
    )
    return {
        "customer_id": scope.customer_id,
        "branch_id": scope.operational_branch_id,
        "installation_id": scope.installation_id,
        "agent_token": agent_token,
    }
