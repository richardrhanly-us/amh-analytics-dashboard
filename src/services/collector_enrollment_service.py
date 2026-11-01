"""One-time Collector enrollment: code generation and redemption.

FLOW. A platform administrator generates a short-lived, single-use enrollment code for ONE
explicit collector_installations row (create_enrollment_code). On the library's
machine the Collector redeems it over HTTPS (POST /collector/enroll ->
redeem_enrollment_code) and receives a long-lived agent token scoped to the
installation's OPERATIONAL customer/branch and bound to that installation
(agent_tokens.installation_id). The Collector never touches the database; this
module runs server-side only.

THIS MODULE IS DELIBERATELY FREE OF STREAMLIT AND OF ANY ENGINE. Both public
operations take an open SQLAlchemy connection, so the API (main.py, the admin
API) and the standalone generate_enrollment_code_for_installation share one
implementation and tests can drive it against any database with the right tables.

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

Redemption locks the code's organization FOR SHARE BEFORE the code row. Offboarding,
recovery and revocation hold that row FOR UPDATE, so each either waits for this
enrollment and then sees its token, or has committed and this sees the outcome (for
offboarding, a revoked code). The order is organization, then code, as in offboarding:
taking the organization after the code could deadlock with offboarding's code sweep.

ERRORS. Every redemption failure raises EnrollmentError carrying a specific
INTERNAL reason (for server logs and tests); the HTTP layer only ever shows
PUBLIC_ENROLLMENT_ERROR, so a caller cannot tell an unknown code from an
expired one, or learn anything about tenants, installations or their state.

This module never modifies collector_installations (installed_at, last_seen_at,
collector_version, hostname, status are all untouched): confirmed-contact
lifecycle belongs to the authenticated heartbeat/preflight.

THE CONTRACT IS THE SERVER'S DECISION (onboarding stage A1). An installation
carries the ingest contract it must be enrolled on
(collector_installations.required_contract: 1 = the legacy contract, 2 =
Contract v2), a code SNAPSHOTS that value when it is generated, and redemption
enforces the snapshot against what the installer says it can do
(`contract_version` in the request; absent means 1):

    code 1, request absent/1   the legacy enrollment, exactly as before: the four fields
    code 1, request 2          refused -- contract_not_permitted
    code 2, request absent/1   refused -- collector_upgrade_required (an installer that cannot do v2
                               never gets a token it could only use for v1)
    code 2, request 2          the Contract v2 enrollment (below)
    request not 1 or 2         refused -- unsupported_contract_version

Those refusals raise EnrollmentRefused, which the HTTP layer answers with a 409
and a fixed code. They are reached only by a caller who presented a code that
is known, unused, unrevoked, unexpired and belongs to a usable tenant -- who
already holds the credential -- so they disclose nothing the generic refusal
protects. NO REFUSAL CONSUMES THE CODE.

A CONTRACT-2 ENROLLMENT also, in the SAME transaction: issues the branch's
ingest key, owned by the enrolling installation (a non-secret key_id; the HMAC
secret behind it is generated on the Collector's machine and never exists here),
records the branch's initial v2 cutover -- which is also what closes the v1
/upload route for the branch -- and returns the site's Collector time zone and
its classification rules document (services.collector_rules). Everything it
needs is read and validated BEFORE
the first write, and it is refused (enrollment_not_ready) if the site's
settings are not ready, or if the branch already has an active ingest key or
any cutover record: a second v2 identity for a branch is never created
silently, and an existing cutover is never moved. Which of those it was is
logged as a fixed reason code and never sent to the caller.

A NEW INSTALLATION IS STILL CONTRACT 1 UNLESS IT IS CREATED AS 2, explicitly
(services.tenant_service.create_collector_installation). Making 2 the normal
choice waits for an installer that can enroll on it; until then a contract-2
installation could not be enrolled by any released bundle.

A RECOVERY TOKEN (issue_recovery_token) replaces the agent token of a Contract 2
installation that owns its branch's only active key, for a Collector that could
not store the token its enrollment returned. Only an authenticated platform
administrator reaches it (services.admin_installation_service).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, bindparam, text

from . import collector_rules
from .ingest_v2_service import (
    active_ingest_key_owners,
    has_active_ingest_key,
    has_any_v2_cutover,
    issue_ingest_key,
    record_initial_v2_cutover,
)

logger = logging.getLogger("sortview.enrollment")

# --- ingest contracts --------------------------------------------------------------
#
# What an installation must be enrolled on, and what an installer may ask for.
# The numbers are the wire contract's own (`contract_version` on the v2 routes).
CONTRACT_LEGACY = 1
CONTRACT_V2 = 2
SUPPORTED_CONTRACTS = (CONTRACT_LEGACY, CONTRACT_V2)
CONTRACT_MODE_V2 = "v2"  # the Collector config's `contract_mode`

# The refusals a caller holding a VALID code can be told about. Each is a fixed
# public code with one fixed sentence; nothing else about the refusal is sent.
COLLECTOR_UPGRADE_REQUIRED = "collector_upgrade_required"
CONTRACT_NOT_PERMITTED = "contract_not_permitted"
UNSUPPORTED_CONTRACT_VERSION = "unsupported_contract_version"
ENROLLMENT_NOT_READY = "enrollment_not_ready"
REFUSAL_DETAILS = {
    COLLECTOR_UPGRADE_REQUIRED: "This enrollment code requires a newer SortView Collector.",
    CONTRACT_NOT_PERMITTED: "This enrollment code does not permit the requested contract.",
    UNSUPPORTED_CONTRACT_VERSION: "The requested contract version is not supported.",
    ENROLLMENT_NOT_READY: "This installation is not ready to enroll. Contact SortView support.",
}

# Who recorded an enrollment's cutover. Fixed text; the ids are in the row's note.
INITIAL_CUTOVER_SET_BY = "collector-enrollment"

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


class EnrollmentRefused(Exception):
    """A VALID code was presented and the enrollment was still refused, for a
    reason the caller may be told: `code` is one of REFUSAL_DETAILS' keys (the
    whole of what is sent, with its fixed sentence). `reason` is INTERNAL -- a
    fixed code for server logs and tests, never a settings value -- and must
    never be sent to a client. The enrollment code is not consumed."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason

    @property
    def detail(self) -> str:
        return REFUSAL_DETAILS[self.code]


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


def is_postgresql(conn: Any) -> bool:
    return getattr(getattr(conn, "dialect", None), "name", "") == "postgresql"


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
    required_contract: int = CONTRACT_LEGACY  # collector_installations.required_contract


_INSTALLATION_SCOPE_SQL = """
    SELECT
        ci.id AS installation_id,
        ci.name AS installation_name,
        ci.status AS installation_status,
        ci.required_contract AS required_contract,
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
        required_contract=int(row["required_contract"]),
    )


# --- generation (platform admin) -----------------------------------------------------------

_REVOKE_UNUSED_SQL = """
    UPDATE collector_enrollment_codes
    SET revoked_at = CURRENT_TIMESTAMP
    WHERE installation_id = :installation_id
      AND used_at IS NULL
      AND revoked_at IS NULL
"""

_INSERT_CODE_SQL = """
    INSERT INTO collector_enrollment_codes
        (installation_id, code_hash, expires_at, created_by_user_id, required_contract)
    VALUES
        (:installation_id, :code_hash, :expires_at, :created_by_user_id, :required_contract)
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
    installation cannot be enrolled.

    The code SNAPSHOTS the installation's required contract: what it permits is
    fixed here and does not follow a later change to the installation."""
    scope = resolve_installation_scope(
        conn, installation_id, lock=True, expected_organization_id=expected_organization_id
    )
    if scope.required_contract not in SUPPORTED_CONTRACTS:
        raise EnrollmentError("installation_contract_unsupported")
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
            required_contract=scope.required_contract,
        )
    ).scalar_one()

    # Never the code itself -- only ids and timing.
    logger.info(
        "Collector enrollment code generated | code_id=%s installation_id=%s organization_id=%s "
        "created_by_user_id=%s expires_at=%s revoked_previous=%s required_contract=%s",
        code_id, scope.installation_id, scope.organization_id, created_by_user_id,
        expires_at.isoformat(), revoked_previous, scope.required_contract,
    )
    return {
        "enrollment_code": raw_code,
        "enrollment_code_id": int(code_id),
        "installation_id": scope.installation_id,
        "installation_name": scope.installation_name,
        "organization_id": scope.organization_id,
        "required_contract": scope.required_contract,
        "expires_at": expires_at,
        "ttl_minutes": int(ttl.total_seconds() // 60),
        "revoked_previous_count": int(revoked_previous or 0),
    }


def _get_engine():
    # Imported here, not at module load, so importing this module needs no
    # database configuration: the API (main.py) imports it at startup.
    from database import get_engine

    return get_engine()


def generate_enrollment_code_for_installation(
    installation_id: int,
    *,
    created_by_user_id: int | None = None,
    expected_organization_id: int | None = None,
) -> dict[str, Any]:
    """Standalone entry point (used by the retired Super Admin Streamlit app): create_enrollment_code in its own
    transaction."""
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
        required_contract,
        expires_at <= :now AS is_expired
    FROM collector_enrollment_codes
    WHERE code_hash = :code_hash
"""

_CODE_ORGANIZATION_SQL = """
    SELECT ci.organization_id
    FROM collector_enrollment_codes c
    JOIN collector_installations ci
      ON ci.id = c.installation_id
    WHERE c.code_hash = :code_hash
"""

_LOCK_ORGANIZATION_SHARED_SQL = "SELECT id FROM organizations WHERE id = :organization_id FOR SHARE"

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


# --- what a Contract v2 enrollment needs, read and checked before anything is written ----------

# Neither settings table is under row level security, so this statement's own
# filter is the only thing scoping the read: both ids come from the validated
# installation scope, never from the request.
_V2_SITE_SQL = """
    SELECT
        o.created_at AS organization_created_at,
        os.settings_json AS organization_settings,
        bs.settings_json AS branch_settings
    FROM organizations o
    LEFT JOIN organization_settings os
      ON os.organization_id = o.id
    LEFT JOIN branch_settings bs
      ON bs.branch_id = :branch_id
    WHERE o.id = :organization_id
"""

_LOCK_BRANCH_SQL = "SELECT id FROM branches WHERE id = :branch_id"

_INSTALLATION_STATUS_SQL = "SELECT status FROM collector_installations WHERE id = :installation_id"


@dataclass(frozen=True)
class _V2Site:
    """What a contract-2 enrollment returns and records about its site. `repr` shows no rule: the rules document
    holds patron account names."""

    timezone: str
    cutover_at: datetime
    rules: dict[str, Any] = field(repr=False)


def _set_tenant_context(conn: Any, scope: InstallationScope) -> None:
    """The row level security context for the enrolling tenant, transaction-local -- what ingest_key_ids' policies
    test. Set from the validated installation scope, never from the request (the same rule as
    main._authenticate_agent). PostgreSQL only: SQLite (unit tests) has neither RLS nor set_config()."""
    set_tenant_context(conn, scope.customer_id, scope.operational_branch_id)


def set_tenant_context(conn: Any, customer_id: int, operational_branch_id: int) -> None:
    if getattr(getattr(conn, "dialect", None), "name", "") != "postgresql":
        return
    conn.execute(text("SELECT set_config('app.operational_customer_id', :v, true)"), {"v": str(customer_id)})
    conn.execute(text("SELECT set_config('app.operational_branch_id', :v, true)"), {"v": str(operational_branch_id)})


def _settings_document(value: object) -> dict[str, Any]:
    """A stored settings_json as a dict. PostgreSQL hands JSONB back parsed; a driver that hands back text is parsed
    here. Nothing stored is an empty document; text that is not JSON is refused."""
    if isinstance(value, (str, bytes)):
        unreadable = False
        try:
            value = json.loads(value)
        except ValueError:  # the message quotes the document: it must not survive as an exception context
            unreadable = True
        if unreadable:
            raise EnrollmentRefused(ENROLLMENT_NOT_READY, "settings_unreadable")
    return dict(value) if isinstance(value, dict) else {}


def _aware_instant(value: object) -> datetime | None:
    """A stored TIMESTAMPTZ as an aware datetime, or None if it is not one. A value with no offset is not guessed at."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value


def _prepare_v2_enrollment(conn: Any, scope: InstallationScope) -> _V2Site:
    """Everything a contract-2 enrollment has to know and be sure of, BEFORE its first write. Reads only; raises
    EnrollmentRefused(ENROLLMENT_NOT_READY, <fixed reason>) if the site cannot be enrolled on Contract v2 now.

    Takes a row lock on the branch first (PostgreSQL), so two enrollments for one branch -- two installations of it --
    cannot both pass the "no key, no cutover" checks below and each create an identity.

    A retirement takes the same lock, so the installation's status is read again under it: one that committed while this
    waited must not end up owning the key it is about to be given. Raises EnrollmentError: the installation is no longer
    enrollable, exactly as if the code had been presented a moment later."""
    _set_tenant_context(conn, scope)
    conn.execute(text(_LOCK_BRANCH_SQL + _lock_suffix(conn)), {"branch_id": scope.branch_id})
    installation_status = conn.execute(
        text(_INSTALLATION_STATUS_SQL), {"installation_id": scope.installation_id}
    ).scalar()
    if installation_status not in ALLOWED_INSTALLATION_STATUSES:
        raise EnrollmentError(f"installation_status_{installation_status}")

    row = conn.execute(
        text(_V2_SITE_SQL), {"organization_id": scope.organization_id, "branch_id": scope.branch_id}
    ).mappings().first()
    if row is None:
        raise EnrollmentRefused(ENROLLMENT_NOT_READY, "organization_missing")

    organization_settings = _settings_document(row["organization_settings"])
    effective = collector_rules.effective_settings(organization_settings, _settings_document(row["branch_settings"]))

    # The reason is taken out of the except block and raised from outside it: a settings value must never ride along
    # as an exception's context. (CollectorSettingsError itself carries only a fixed code.)
    problem: str | None = None
    timezone = ""
    rules: dict[str, Any] = {}
    try:
        timezone = collector_rules.collector_timezone(organization_settings)
        collector_rules.require_rules_reviewed(organization_settings)
        rules = collector_rules.build_classification_rules(effective)
    except collector_rules.CollectorSettingsError as exc:
        problem = exc.reason
    if problem is not None:
        raise EnrollmentRefused(ENROLLMENT_NOT_READY, problem)

    # The branch's v1 -> v2 boundary is the moment its organization came to exist: before any event the Collector can
    # send, whatever the sorter's clock says. (organizations.created_at is TIMESTAMPTZ NOT NULL; branches.created_at
    # is a nullable TIMESTAMP with no zone and is deliberately not used.)
    cutover_at = _aware_instant(row["organization_created_at"])
    if cutover_at is None:
        raise EnrollmentRefused(ENROLLMENT_NOT_READY, "organization_created_at_unusable")

    # One v2 identity per branch, and never a second cutover, whoever owns the key: refused, not resolved.
    if has_active_ingest_key(conn, scope.customer_id, scope.operational_branch_id):
        raise EnrollmentRefused(ENROLLMENT_NOT_READY, "active_ingest_key_exists")
    if has_any_v2_cutover(conn, scope.customer_id, scope.operational_branch_id):
        raise EnrollmentRefused(ENROLLMENT_NOT_READY, "cutover_exists")

    return _V2Site(timezone=timezone, cutover_at=cutover_at, rules=rules)


def _require_permitted_contract(required: int, requested: int | None) -> int:
    """The contract this redemption enrolls on, or EnrollmentRefused. `required` is the code's snapshot; `requested`
    is what the installer said it supports (None: it said nothing, which is the legacy contract)."""
    asked = CONTRACT_LEGACY if requested is None else requested
    if isinstance(asked, bool) or not isinstance(asked, int) or asked not in SUPPORTED_CONTRACTS:
        raise EnrollmentRefused(UNSUPPORTED_CONTRACT_VERSION, "requested_contract_unsupported")
    if required not in SUPPORTED_CONTRACTS:
        # A requirement this server does not implement: whatever the installer is, it is not new enough.
        raise EnrollmentRefused(COLLECTOR_UPGRADE_REQUIRED, "code_requires_unsupported_contract")
    if required == CONTRACT_V2 and asked != CONTRACT_V2:
        raise EnrollmentRefused(COLLECTOR_UPGRADE_REQUIRED, "code_requires_contract_2")
    if required == CONTRACT_LEGACY and asked != CONTRACT_LEGACY:
        raise EnrollmentRefused(CONTRACT_NOT_PERMITTED, "code_permits_contract_1_only")
    return required


def _lock_code_organization(conn: Any, code_hash: str) -> None:
    """The organization of the code's installation, FOR SHARE, to the end of the caller's transaction (see CONCURRENCY in the
    module docstring). PostgreSQL only. An unknown code locks nothing, and the caller then refuses it."""
    if not is_postgresql(conn):
        return
    organization_id = conn.execute(text(_CODE_ORGANIZATION_SQL), {"code_hash": code_hash}).scalar()
    if organization_id is not None:
        conn.execute(text(_LOCK_ORGANIZATION_SHARED_SQL), {"organization_id": organization_id})


def redeem_enrollment_code(
    conn: Any,
    raw_code: str,
    *,
    hostname: str | None = None,
    collector_version: str | None = None,
    contract_version: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Redeems a one-time enrollment code inside the caller's transaction. The
    caller commits; on any EnrollmentError, EnrollmentRefused or other
    exception it must roll back, which leaves the code unused and nothing
    created.

    A LEGACY enrollment (a contract-1 code, no `contract_version` or 1) returns
    exactly {customer_id, branch_id, installation_id, agent_token}, as it
    always has. A CONTRACT-2 enrollment (a contract-2 code and
    `contract_version` 2) returns those four and contract_version,
    contract_mode, key_id, timezone and classification_rules, having issued the
    branch's ingest key and recorded its initial cutover in this transaction.
    Any other pairing raises EnrollmentRefused (see the module docstring).

    `hostname` and `collector_version` are untrusted, informational metadata:
    they only appear (sanitized) in the new token's description and never
    identify or select anything."""
    canonical = normalize_enrollment_code(raw_code)
    if canonical is None:
        raise EnrollmentError("malformed_code")
    checked_at = now or datetime.now(UTC)
    code_hash = hash_enrollment_code(canonical)
    _lock_code_organization(conn, code_hash)

    lookup_sql = _LOOKUP_CODE_SQL + _lock_suffix(conn)
    code_row = conn.execute(
        _timestamped(lookup_sql, code_hash=code_hash, now=checked_at)
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

    # The code is valid and its tenant usable: from here a refusal may say why (EnrollmentRefused), and still
    # consumes nothing. The CODE's snapshot decides, not the installation's current value.
    contract = _require_permitted_contract(int(code_row["required_contract"]), contract_version)
    site = _prepare_v2_enrollment(conn, scope) if contract == CONTRACT_V2 else None

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

    result: dict[str, Any] = {
        "customer_id": scope.customer_id,
        "branch_id": scope.operational_branch_id,
        "installation_id": scope.installation_id,
        "agent_token": agent_token,
    }
    if site is None:
        # Ids only -- never the code or the token.
        logger.info(
            "Collector enrollment redeemed | code_id=%s token_id=%s installation_id=%s "
            "customer_id=%s branch_id=%s",
            code_row["id"], token_id, scope.installation_id, scope.customer_id, scope.operational_branch_id,
        )
        return result

    # Contract v2: the branch's key, owned by this installation, and its cutover, in this same transaction. The key is a
    # non-secret id -- the secret behind it is generated on the Collector's machine and never exists here.
    key_id = issue_ingest_key(conn, scope.customer_id, scope.operational_branch_id, installation_id=scope.installation_id)
    cutover_id = record_initial_v2_cutover(
        conn,
        scope.customer_id,
        scope.operational_branch_id,
        site.cutover_at,
        INITIAL_CUTOVER_SET_BY,
        f"initial v2 cutover | enrollment_code_id={int(code_row['id'])} installation_id={scope.installation_id}",
    )
    result.update({
        "contract_version": CONTRACT_V2,
        "contract_mode": CONTRACT_MODE_V2,
        "key_id": key_id,
        "timezone": site.timezone,
        "classification_rules": site.rules,
    })

    # Ids and counts only -- never the code, the token, a rule or a settings value.
    logger.info(
        "Collector enrollment redeemed | code_id=%s token_id=%s installation_id=%s customer_id=%s branch_id=%s "
        "contract=%s key_id=%s cutover_id=%s rule_destinations=%s rule_entries=%s",
        code_row["id"], token_id, scope.installation_id, scope.customer_id, scope.operational_branch_id,
        CONTRACT_V2, key_id, cutover_id, len(site.rules["destinations"]),
        sum(len(site.rules[name]) for name in collector_rules.NAME_LISTS),
    )
    return result


# --- recovery (platform admin) -----------------------------------------------------------------------------------------

_REVOKE_INSTALLATION_TOKENS_SQL = """
    UPDATE agent_tokens
    SET is_active = FALSE
    WHERE installation_id = :installation_id
      AND is_active = TRUE
    RETURNING id
"""


def issue_recovery_token(conn: Any, installation_id: int, *, expected_organization_id: int) -> dict[str, Any]:
    """Replaces an installation's agent tokens with one new bound token inside the caller's transaction, and returns the RAW
    token exactly once. Raises EnrollmentError(reason) unless the installation is a provisioning or active Contract 2
    installation of a usable tenant that owns its branch's only active key; the caller then rolls everything back. A token
    not bound to this installation is not touched. The caller holds the organization's row lock.

    The tokens come first, as on every authenticated request: a heartbeat holds its token and then updates the installation,
    so locking the installation first could deadlock with it. A request already past the token check when this commits may
    still finish: this replaces a credential, it is not an upload fence."""
    revoked = len(conn.execute(text(_REVOKE_INSTALLATION_TOKENS_SQL), {"installation_id": installation_id}).fetchall())
    scope = resolve_installation_scope(conn, installation_id, lock=True, expected_organization_id=expected_organization_id)
    if scope.required_contract != CONTRACT_V2:
        raise EnrollmentError("installation_contract_not_2")
    _set_tenant_context(conn, scope)
    if active_ingest_key_owners(conn, scope.customer_id, scope.operational_branch_id) != [scope.installation_id]:
        raise EnrollmentError("installation_does_not_own_the_only_active_key")

    agent_token, token_hash = new_agent_token()
    token_id = conn.execute(
        text(_INSERT_TOKEN_SQL),
        {
            "token_hash": token_hash,
            "customer_id": scope.customer_id,
            "branch_id": scope.operational_branch_id,
            "description": _CONTROL_CHARACTERS.sub(
                "",
                f"Collector recovery | installation {scope.installation_id} "
                f"({scope.installation_name[:_MAX_INSTALLATION_NAME_IN_DESCRIPTION]!r})",
            ),
            "installation_id": scope.installation_id,
        },
    ).scalar_one()

    # Ids and counts only -- never the token.
    logger.info(
        "Collector recovery token issued | token_id=%s installation_id=%s customer_id=%s branch_id=%s revoked_previous=%s",
        token_id, scope.installation_id, scope.customer_id, scope.operational_branch_id, revoked,
    )
    return {
        "agent_token": agent_token,
        "token_id": int(token_id),
        "installation_id": scope.installation_id,
        "installation_name": scope.installation_name,
        "revoked_previous_count": revoked,
    }
