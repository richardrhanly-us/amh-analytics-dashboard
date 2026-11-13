"""A library's Collector installations and their enrollment codes, for the administration API (A2 item 5).

Framework-neutral: no FastAPI, no Streamlit. Every write runs in ONE transaction with its audit row in
auth_audit_log, so there is no change without its record and no record of a change that did not happen.

WHAT AN ADMINISTRATOR MAY DO, AND NOTHING MORE:

    list        every installation of the library -- whatever its status or its branch's -- with a safe summary of
                its enrollment codes: when an unused one expires, and when one was last redeemed
    add         at the library's ACTIVE PRIMARY branch only (no branch is ever chosen: multi-branch is C8), starting
                `provisioning`, on contract 1 or 2. Contract 2 needs the library's Collector time zone and review record
                (A2 item 4); contract 1 does not.
    rename      the name alone. The branch, contract, hostname, Collector version and status are untouched.
    status      only these transitions (TRANSITIONS), each guarded against a concurrent change:

                    provisioning -> inactive      active -> inactive
                    inactive -> provisioning      (the next heartbeat makes it active; installed_at is not stamped)
                    provisioning | active | inactive -> retired     (final here)

                An installation's bound agent token authenticates only while it is provisioning or active
                (main.bound_installation_unusable_reason), so inactive and retired stop it at once. No ingest key and
                no cutover is touched, so an installation that owns an active key cannot be retired here: that would
                strand the key. It can be deactivated; offboarding the library retires both together.
    code        a one-time enrollment code, by services.collector_enrollment_service.create_enrollment_code -- the one
                implementation: it revokes the installation's earlier unused codes, stores only the code's hash,
                snapshots the contract and returns the raw code ONCE. For a contract-2 installation whose branch
                already has an active ingest key or any v2 cutover, nothing is written and BranchAlreadyEnrolled is
                raised: such a code could never be redeemed. The one exception is a PENDING REPLACEMENT (below): it may
                have a code while its source is still an eligible source. Redeeming that code is the replacement
                itself (C6a R2, collector_enrollment_service._redeem_replacement).
    replace     C6a R1 (docs/c6a-replacement-machine-design.md, Amendment A, 14.2-14.5): authorize the replacement of an
                ENROLLED Contract 2 installation -- the owner of its branch's only active ingest key, provisioning, active or
                inactive, never retired, at the library's active primary branch, with a v2 cutover and no active legacy
                unbound token at the branch. A provisioning source also needs the administrator's confirmation that its
                enrollment response was lost (14.3a). At most one pending replacement per branch. It adds one
                `provisioning` Contract 2 installation whose write-once columns name the source, the authorizing
                administrator and the time, and changes nothing else: no token, key, cutover or status.
    cancel      a pending replacement only, by its own operation and audit event: it is retired and its unused codes
                revoked. The general status change refuses to retire a pending replacement.

    A PENDING REPLACEMENT is an installation that names the installation it replaces, is not retired, and owns no
    ingest key, active or retired. Inactive counts: deactivating one, authorizing another and reactivating the first
    must not leave two.
    recovery    one new bound agent token for a provisioning or active Contract 2 installation that owns its branch's only
                active key, replacing the installation's earlier tokens, by the enrollment service's issue_recovery_token.
                The raw token is returned ONCE.
    unbound     list and revoke a library's legacy unbound tokens, one at a time. Deactivating an installation does not stop
                them. Revoking is not an upload fence: a request already past the token check may still finish.

LOCK ORDER. Offboarding holds the organization, then tokens, installations, codes and keys; an authenticated request holds
its token and then updates the installation. So recovery and revocation take the organization first and tokens before the
installation. An enrollment holds the organization FOR SHARE before its code row, so they wait for it and then see its token. A
retirement takes the branch FOR NO KEY UPDATE, against an enrollment's lock on it, and never the organization
or a token; it does not block an event insert's FOR KEY SHARE. Replacement authorization, cancellation and code issuance
take the organization FOR UPDATE first, then the installation or branch: so they serialize with each other, with
offboarding and with an enrollment, and a pending replacement can only appear under that lock (14.5).

NOTHING HERE HOLDS OR RETURNS a hostname, a Collector version, a token's hash, a code's hash, a key id or an operational or
internal organization/branch id. The raw code and the raw recovery token leave only in the return value of the call that
made them and are never logged, audited or put in an exception. A token's own row id and an unbound token's operator-written
description are not secrets. Audit rows name the library as `target_org_slug` -- never `org_slug`, which marks an event as
one the organization's own members may see (user_admin_service).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from database import get_engine

from . import (
    auth_service,
    collector_enrollment_service,
    collector_rules,
    replacement_eligibility,
    tenant_service,
)
from .collector_enrollment_service import EnrollmentError
from .ingest_v2_service import (
    active_ingest_key_owners,
    has_active_ingest_key,
    has_any_v2_cutover,
)

MAX_NAME_LENGTH = 120

# Which status each status may become here. Retired is final; nothing is made active by hand.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "provisioning": ("inactive", "retired"),
    "active": ("inactive", "retired"),
    "inactive": ("provisioning", "retired"),
    "retired": (),
}

INSTALLATION_OWNS_ACTIVE_KEY = "installation_owns_active_key"

# What stops an installation being added (a fixed code each).
LIBRARY_CANCELLED = "library_cancelled"
NO_ACTIVE_PRIMARY_BRANCH = replacement_eligibility.NO_ACTIVE_PRIMARY_BRANCH
COLLECTOR_SETTINGS_NOT_READY = "collector_settings_not_ready"

INSTALLATION_CREATED = "admin_installation_created"
INSTALLATION_RENAMED = "admin_installation_renamed"
INSTALLATION_STATUS_CHANGED = "admin_installation_status_changed"
ENROLLMENT_CODE_ISSUED = "admin_enrollment_code_issued"
RECOVERY_CREDENTIAL_ISSUED = "admin_recovery_token_issued"
UNBOUND_CREDENTIAL_REVOKED = "admin_unbound_token_revoked"
REPLACEMENT_AUTHORIZED = "admin_replacement_authorized"
REPLACEMENT_CANCELLED = "admin_replacement_cancelled"

# Why a replacement cannot be authorized, a code issued for one, or one cancelled (a fixed code each). The source rules
# are shared with redemption (services.replacement_eligibility); their codes are the same values.
OPERATIONAL_IDENTITY_INCOMPLETE = replacement_eligibility.OPERATIONAL_IDENTITY_INCOMPLETE
REPLACEMENT_SOURCE_RETIRED = replacement_eligibility.REPLACEMENT_SOURCE_RETIRED
REPLACEMENT_SOURCE_NOT_CONTRACT_2 = replacement_eligibility.REPLACEMENT_SOURCE_NOT_CONTRACT_2
REPLACEMENT_SOURCE_NOT_AT_PRIMARY_BRANCH = replacement_eligibility.REPLACEMENT_SOURCE_NOT_AT_PRIMARY_BRANCH
REPLACEMENT_SOURCE_NOT_KEY_OWNER = replacement_eligibility.REPLACEMENT_SOURCE_NOT_KEY_OWNER
REPLACEMENT_BRANCH_NOT_ENROLLED = replacement_eligibility.REPLACEMENT_BRANCH_NOT_ENROLLED
UNBOUND_TOKENS_ACTIVE = replacement_eligibility.UNBOUND_TOKENS_ACTIVE
REPLACEMENT_ALREADY_PENDING = "replacement_already_pending"
LOST_RESPONSE_CONFIRMATION_REQUIRED = "lost_response_confirmation_required"
NOT_A_PENDING_REPLACEMENT = "not_a_pending_replacement"
USE_CANCEL_REPLACEMENT = "use_cancel_replacement"
REPLACEMENT_DISABLED = replacement_eligibility.REPLACEMENT_DISABLED


class InstallationAdminError(Exception):
    """A refusal the API may answer with: `code` is a fixed public code, and the only text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Library:
    """The library an administrator is working on. `id` is internal: it scopes statements and is never returned."""

    id: int
    slug: str
    name: str
    status: str


@dataclass(frozen=True)
class Branch:
    slug: str
    name: str
    status: str
    is_primary: bool


@dataclass(frozen=True)
class PrimaryBranch:
    id: int  # internal, never returned
    slug: str
    name: str
    status: str


def validate_name(name: object) -> str:
    """The name, trimmed. ValueError("required" | "too_long" | "not_text") otherwise -- a code, never the value."""
    if not isinstance(name, str):
        raise ValueError("not_text")
    trimmed = name.strip()
    if not trimmed:
        raise ValueError("required")
    if len(trimmed) > MAX_NAME_LENGTH:
        raise ValueError("too_long")
    return trimmed


def transition_allowed(current: str, wanted: str) -> bool:
    return wanted in TRANSITIONS.get(current, ())


# --- reads -------------------------------------------------------------------------------------------------------------

_LIBRARY_SQL = "SELECT id, slug, name, status FROM organizations WHERE slug = :org_slug"

# LIMIT 2: a library with two primary branches is not one this can add to.
_PRIMARY_BRANCH_SQL = """
    SELECT id, slug, name, status
    FROM branches
    WHERE organization_id = :organization_id
      AND is_primary = TRUE
    ORDER BY id
    LIMIT 2
"""

# Every installation of the organization, with its branch and a summary of its codes that names no code.
_INSTALLATIONS_SQL = """
    SELECT
        ci.id, ci.name, ci.status, ci.required_contract, ci.installed_at, ci.last_seen_at, ci.created_at,
        b.slug AS branch_slug, b.name AS branch_name, b.status AS branch_status, b.is_primary AS branch_is_primary,
        (SELECT MAX(c.expires_at) FROM collector_enrollment_codes c
          WHERE c.installation_id = ci.id AND c.used_at IS NULL AND c.revoked_at IS NULL AND c.expires_at > :now)
            AS pending_code_expires_at,
        (SELECT MAX(c.used_at) FROM collector_enrollment_codes c WHERE c.installation_id = ci.id)
            AS last_code_redeemed_at
    FROM collector_installations ci
    JOIN branches b
      ON b.id = ci.branch_id
    WHERE ci.organization_id = :organization_id
"""


def get_library(org_slug: str) -> Library | None:
    with get_engine().connect() as conn:
        row = conn.execute(text(_LIBRARY_SQL), {"org_slug": org_slug}).mappings().first()
    return None if row is None else Library(id=int(row["id"]), slug=row["slug"], name=row["name"], status=row["status"])


def _primary_branch(conn: Any, organization_id: int) -> PrimaryBranch | None:
    rows = conn.execute(text(_PRIMARY_BRANCH_SQL), {"organization_id": organization_id}).mappings().all()
    if len(rows) != 1:
        return None
    row = rows[0]
    return PrimaryBranch(id=int(row["id"]), slug=row["slug"], name=row["name"], status=row["status"])


def get_primary_branch(library: Library) -> PrimaryBranch | None:
    with get_engine().connect() as conn:
        return _primary_branch(conn, library.id)


def _installation(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "branch": Branch(
            slug=row["branch_slug"], name=row["branch_name"], status=row["branch_status"],
            is_primary=bool(row["branch_is_primary"]),
        ),
        "status": row["status"],
        "required_contract": int(row["required_contract"]),
        "installed_at": row["installed_at"],
        "last_seen_at": row["last_seen_at"],
        "created_at": row["created_at"],
        "pending_code_expires_at": row["pending_code_expires_at"],
        "last_code_redeemed_at": row["last_code_redeemed_at"],
    }


def _installations(conn: Any, organization_id: int, *, installation_id: int | None = None) -> list[dict[str, Any]]:
    sql = _INSTALLATIONS_SQL + (" AND ci.id = :installation_id" if installation_id is not None else "")
    sql += " ORDER BY b.is_primary DESC, b.id, ci.id"
    rows = conn.execute(
        text(sql),
        {"organization_id": organization_id, "installation_id": installation_id, "now": datetime.now(UTC)},
    ).mappings().all()
    return [_installation(row) for row in rows]


def list_installations(library: Library) -> list[dict[str, Any]]:
    with get_engine().connect() as conn:
        return _installations(conn, library.id)


def _one(conn: Any, library: Library, installation_id: int) -> dict[str, Any]:
    found = _installations(conn, library.id, installation_id=installation_id)
    if not found:
        raise InstallationAdminError("installation_not_found")
    return found[0]


def v2_settings_ready(library: Library) -> bool:
    """Whether a contract-2 installation may be added: the library's Collector time zone and review record (A2 item 4),
    read as the enrollment reads them."""
    site = tenant_service.get_collector_site_settings(library.id)
    try:
        collector_rules.collector_timezone(site)
        collector_rules.require_rules_reviewed(site)
    except collector_rules.CollectorSettingsError:
        return False
    return True


def add_problems(library: Library, primary: PrimaryBranch | None) -> list[str]:
    """What stops an installation being added now. COLLECTOR_SETTINGS_NOT_READY concerns contract 2 alone."""
    problems = []
    if library.status == "cancelled":
        problems.append(LIBRARY_CANCELLED)
    if primary is None or primary.status != "active":
        problems.append(NO_ACTIVE_PRIMARY_BRANCH)
    if not v2_settings_ready(library):
        problems.append(COLLECTOR_SETTINGS_NOT_READY)
    return problems


# --- writes ------------------------------------------------------------------------------------------------------------

def _audit(conn: Any, event_type: str, message: str, library: Library, actor: dict[str, Any], **details: Any) -> None:
    auth_service.log_auth_event_with_connection(
        conn,
        event_type=event_type,
        is_success=True,
        user_id=int(actor["id"]),
        email=actor.get("email"),
        message=message,
        metadata={"target_org_slug": library.slug, **details, "actor_user_id": int(actor["id"])},
    )


def _refuse_cancelled(library: Library) -> None:
    if library.status == "cancelled":
        raise InstallationAdminError(LIBRARY_CANCELLED)


def create_installation(library: Library, *, name: str, required_contract: int, actor: dict[str, Any]) -> dict[str, Any]:
    """Adds an installation at the library's active primary branch, `provisioning`, on `required_contract`."""
    name = validate_name(name)
    if required_contract not in tenant_service.COLLECTOR_CONTRACTS:
        raise ValueError("invalid_contract")
    _refuse_cancelled(library)
    if required_contract == tenant_service.COLLECTOR_CONTRACT_V2 and not v2_settings_ready(library):
        raise InstallationAdminError(COLLECTOR_SETTINGS_NOT_READY)

    with get_engine().begin() as conn:
        primary = _primary_branch(conn, library.id)
        if primary is None or primary.status != "active":
            raise InstallationAdminError(NO_ACTIVE_PRIMARY_BRANCH)
        try:
            row = tenant_service.insert_collector_installation(
                conn, organization_id=library.id, branch_id=primary.id, name=name,
                status="provisioning", required_contract=required_contract,
            )
        except RuntimeError:
            # Cancelled since it was read: the insert's own check refused it.
            raise InstallationAdminError(LIBRARY_CANCELLED) from None
        installation_id = int(row["id"])
        _audit(conn, INSTALLATION_CREATED, "Collector installation added.", library, actor,
               installation_id=installation_id, branch_slug=primary.slug, required_contract=required_contract)
        return _one(conn, library, installation_id)


_RENAME_SQL = """
    UPDATE collector_installations
    SET name = :name,
        updated_at = NOW()
    WHERE id = :installation_id
      AND organization_id = :organization_id
      AND NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = :organization_id AND o.status = 'cancelled')
    RETURNING id
"""


def rename_installation(library: Library, installation_id: int, *, name: str, actor: dict[str, Any]) -> dict[str, Any]:
    """Sets the name and nothing else."""
    name = validate_name(name)
    _refuse_cancelled(library)
    with get_engine().begin() as conn:
        renamed = conn.execute(
            text(_RENAME_SQL), {"installation_id": installation_id, "organization_id": library.id, "name": name}
        ).first()
        if renamed is None:
            _one(conn, library, installation_id)  # not this library's: installation_not_found
            raise InstallationAdminError(LIBRARY_CANCELLED)
        _audit(conn, INSTALLATION_RENAMED, "Collector installation renamed.", library, actor,
               installation_id=installation_id)
        return _one(conn, library, installation_id)


_CURRENT_STATUS_SQL = """
    SELECT status, replaces_installation_id
    FROM collector_installations
    WHERE id = :installation_id AND organization_id = :organization_id
"""

# Whether the installation owns any ingest key, active or retired: a replacement that owns one has been redeemed and is no
# longer pending. ingest_key_ids is read under row level security: the caller has set the branch's tenant context.
_OWNS_ANY_KEY_SQL = "SELECT 1 FROM ingest_key_ids WHERE installation_id = :installation_id LIMIT 1"


def _owns_any_key(conn: Any, installation_id: int) -> bool:
    return conn.execute(text(_OWNS_ANY_KEY_SQL), {"installation_id": installation_id}).first() is not None

# Guarded on the status that was read and allowed, so a concurrent change is refused rather than overwritten. Status
# alone: installed_at is the heartbeat's to stamp.
_SET_STATUS_SQL = """
    UPDATE collector_installations
    SET status = :status,
        updated_at = NOW()
    WHERE id = :installation_id
      AND organization_id = :organization_id
      AND status = :previous_status
      AND NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = :organization_id AND o.status = 'cancelled')
    RETURNING id
"""


_INSTALLATION_TENANT_SQL = """
    SELECT b.id AS branch_id, b.operational_branch_id, o.operational_customer_id
    FROM collector_installations ci
    JOIN organizations o
      ON o.id = ci.organization_id
    JOIN branches b
      ON b.id = ci.branch_id
    WHERE ci.id = :installation_id
      AND ci.organization_id = :organization_id
"""

_LOCK_BRANCH_SQL = "SELECT id FROM branches WHERE id = :branch_id FOR NO KEY UPDATE"


def _refuse_retiring_a_key_owner(conn: Any, library: Library, installation_id: int) -> None:
    """installation_owns_active_key if retiring would strand an active key. Under the branch's lock, held to commit: an
    enrollment holds the same one while it gives the installation a key, and reads the status again once it has it."""
    tenant = conn.execute(
        text(_INSTALLATION_TENANT_SQL), {"installation_id": installation_id, "organization_id": library.id}
    ).mappings().first()
    if tenant is None:
        raise InstallationAdminError("installation_not_found")
    if collector_enrollment_service.is_postgresql(conn):
        conn.execute(text(_LOCK_BRANCH_SQL), {"branch_id": tenant["branch_id"]})
    customer_id, operational_branch_id = tenant["operational_customer_id"], tenant["operational_branch_id"]
    if customer_id is None or operational_branch_id is None:
        return  # a key is issued only to a fully mapped tenant
    # ingest_key_ids is read under row level security: the branch's own tenant.
    collector_enrollment_service.set_tenant_context(conn, customer_id, operational_branch_id)
    if installation_id in active_ingest_key_owners(conn, customer_id, operational_branch_id):
        raise InstallationAdminError(INSTALLATION_OWNS_ACTIVE_KEY)


def change_status(library: Library, installation_id: int, *, status: str, actor: dict[str, Any]) -> dict[str, Any]:
    """Moves the installation to `status` if TRANSITIONS allows it from its current one; transition_not_allowed if
    not, or if it changed meanwhile. installation_owns_active_key if `status` is retired and it owns an active key.
    use_cancel_replacement if `status` is retired and it is a pending replacement: that has its own operation and audit
    event (cancel_replacement)."""
    _refuse_cancelled(library)
    with get_engine().begin() as conn:
        found = conn.execute(
            text(_CURRENT_STATUS_SQL), {"installation_id": installation_id, "organization_id": library.id}
        ).mappings().first()
        if found is None:
            raise InstallationAdminError("installation_not_found")
        current = found["status"]
        if not transition_allowed(current, status):
            raise InstallationAdminError("transition_not_allowed")
        if status == "retired":
            # Takes the branch lock and sets its tenant context, so the key read below is the one a redemption would see.
            _refuse_retiring_a_key_owner(conn, library, installation_id)
            if found["replaces_installation_id"] is not None and not _owns_any_key(conn, installation_id):
                raise InstallationAdminError(USE_CANCEL_REPLACEMENT)
        changed = conn.execute(text(_SET_STATUS_SQL), {
            "installation_id": installation_id, "organization_id": library.id,
            "status": status, "previous_status": current,
        }).first()
        if changed is None:
            raise InstallationAdminError("transition_not_allowed")
        _audit(conn, INSTALLATION_STATUS_CHANGED, "Collector installation status changed.", library, actor,
               installation_id=installation_id, previous_status=current, status=status)
        return _one(conn, library, installation_id)


def _public_refusal(reason: str) -> str:
    """The fixed public code for an enrollment service refusal. The service's reason is internal and never sent."""
    if reason in ("installation_not_found", "installation_organization_mismatch"):
        return "installation_not_found"
    if reason.startswith("installation_"):
        return "installation_not_enrollable"
    if reason.startswith("organization_status_"):
        return "library_not_enrollable"
    return "operational_identity_incomplete"


def issue_enrollment_code(library: Library, installation_id: int, *, actor: dict[str, Any]) -> dict[str, Any]:
    """A one-time enrollment code for the installation, by the enrollment service's own create_enrollment_code. The
    raw code is in the return value and nowhere else. Every check -- the installation is this library's, enrollable,
    of a usable tenant, and (contract 2) its branch is not already enrolled -- is made before anything is written.

    A pending replacement (C6a R1) is the one Contract 2 installation that may have a code on an enrolled branch, and
    only while its source is still an eligible source (_require_eligible_source, under the same locks as authorization).
    Redeemed by R2. With machine replacement off (14.11), such a code is refused (`replacement_disabled`) before anything
    is written, and the replacement's earlier codes are left as they are."""
    _refuse_cancelled(library)
    with get_engine().begin() as conn:
        # The organization first, as authorization and cancellation take it, so neither can interleave with this.
        _lock_library(conn, library)
        try:
            scope = collector_enrollment_service.resolve_installation_scope(
                conn, installation_id, lock=True, expected_organization_id=library.id
            )
        except EnrollmentError as refused:
            raise InstallationAdminError(_public_refusal(refused.reason)) from None

        if scope.required_contract == collector_enrollment_service.CONTRACT_V2:
            # ingest_key_ids is read under row level security: the branch's own tenant, as the enrollment reads it.
            collector_enrollment_service._set_tenant_context(conn, scope)
            replaces = conn.execute(
                text(_CURRENT_STATUS_SQL), {"installation_id": installation_id, "organization_id": library.id}
            ).mappings().one()["replaces_installation_id"]
            if replaces is not None and not _owns_any_key(conn, installation_id):
                if not replacement_eligibility.replacement_enabled():
                    raise InstallationAdminError(REPLACEMENT_DISABLED)
                _require_eligible_source(conn, library, int(replaces))
            elif has_active_ingest_key(conn, scope.customer_id, scope.operational_branch_id) or has_any_v2_cutover(
                conn, scope.customer_id, scope.operational_branch_id
            ):
                raise InstallationAdminError("branch_already_enrolled")

        try:
            issued = collector_enrollment_service.create_enrollment_code(
                conn, installation_id, created_by_user_id=int(actor["id"]), expected_organization_id=library.id
            )
        except EnrollmentError as refused:
            raise InstallationAdminError(_public_refusal(refused.reason)) from None

        _audit(conn, ENROLLMENT_CODE_ISSUED, "Collector enrollment code issued.", library, actor,
               installation_id=installation_id, enrollment_code_id=issued["enrollment_code_id"],
               required_contract=issued["required_contract"], expires_at=issued["expires_at"].isoformat(),
               revoked_previous_count=issued["revoked_previous_count"])
        return {
            "installation": {"id": issued["installation_id"], "name": issued["installation_name"]},
            "required_contract": issued["required_contract"],
            "enrollment_code": issued["enrollment_code"],
            "expires_at": issued["expires_at"],
            "ttl_minutes": issued["ttl_minutes"],
            "revoked_previous_count": issued["revoked_previous_count"],
        }


# --- credential recovery and legacy unbound tokens -----------------------------------------------------------------------

_LOCK_LIBRARY_SQL = "SELECT status FROM organizations WHERE id = :organization_id"


def _lock_library(conn: Any, library: Library) -> None:
    """The organization's row, as offboarding locks it, so neither can miss what the other does. library_cancelled if
    offboarding committed first: the caller's Library may have been read before it did."""
    status = conn.execute(
        text(_LOCK_LIBRARY_SQL + collector_enrollment_service._lock_suffix(conn)), {"organization_id": library.id}
    ).scalar()
    if status == "cancelled":
        raise InstallationAdminError(LIBRARY_CANCELLED)


def issue_recovery_token(library: Library, installation_id: int, *, actor: dict[str, Any]) -> dict[str, Any]:
    """One new bound agent token for the installation, replacing its earlier ones, audited, all in one transaction. The raw
    token is in the return value and nowhere else; any failure leaves the earlier tokens as they were."""
    _refuse_cancelled(library)
    with get_engine().begin() as conn:
        _lock_library(conn, library)
        if conn.execute(
            text(_CURRENT_STATUS_SQL), {"installation_id": installation_id, "organization_id": library.id}
        ).first() is None:
            raise InstallationAdminError("installation_not_found")
        try:
            issued = collector_enrollment_service.issue_recovery_token(
                conn, installation_id, expected_organization_id=library.id
            )
        except EnrollmentError:
            raise InstallationAdminError("installation_not_recoverable") from None
        _audit(conn, RECOVERY_CREDENTIAL_ISSUED, "Collector recovery token issued.", library, actor,
               installation_id=installation_id, token_id=issued["token_id"],
               revoked_previous_count=issued["revoked_previous_count"])
        return {
            "installation": {"id": issued["installation_id"], "name": issued["installation_name"]},
            "agent_token": issued["agent_token"],
            "revoked_previous_count": issued["revoked_previous_count"],
        }


# Bound tokens are not here: their installation's status and the recovery token already govern them, and only a bound token's
# description can carry a hostname. Both columns are TIMESTAMP with no zone, written as the session's CURRENT_TIMESTAMP.
_UNBOUND_TOKENS_SQL = """
    SELECT
        t.id, t.description, b.name AS branch_name,
        t.created_at AT TIME ZONE current_setting('TimeZone') AS created_at,
        t.last_used_at AT TIME ZONE current_setting('TimeZone') AS last_used_at
    FROM agent_tokens t
    JOIN organizations o
      ON o.operational_customer_id = t.customer_id
    LEFT JOIN branches b
      ON b.operational_branch_id = t.branch_id
     AND b.organization_id = o.id
    WHERE o.id = :organization_id
      AND t.installation_id IS NULL
      AND t.is_active = TRUE
    ORDER BY t.id
"""

_REVOKE_UNBOUND_CREDENTIAL_SQL = """
    UPDATE agent_tokens
    SET is_active = FALSE
    WHERE id = :token_id
      AND installation_id IS NULL
      AND is_active = TRUE
      AND customer_id = (SELECT operational_customer_id FROM organizations WHERE id = :organization_id)
    RETURNING id
"""


def list_unbound_tokens(library: Library) -> list[dict[str, Any]]:
    """The library's active legacy unbound tokens. Never a hash."""
    with get_engine().connect() as conn:
        rows = conn.execute(text(_UNBOUND_TOKENS_SQL), {"organization_id": library.id}).mappings().all()
    return [dict(row) for row in rows]


def revoke_unbound_token(library: Library, token_id: int, *, actor: dict[str, Any]) -> None:
    """Deactivates one active unbound token of this library, audited, in one transaction. token_not_found otherwise: a bound
    token, another library's and one already inactive are answered alike. Nothing here makes a token active again."""
    _refuse_cancelled(library)
    with get_engine().begin() as conn:
        _lock_library(conn, library)
        revoked = conn.execute(
            text(_REVOKE_UNBOUND_CREDENTIAL_SQL), {"token_id": token_id, "organization_id": library.id}
        ).first()
        if revoked is None:
            raise InstallationAdminError("token_not_found")
        _audit(conn, UNBOUND_CREDENTIAL_REVOKED, "Legacy unbound agent token revoked.", library, actor, token_id=token_id)


# --- machine replacement: authorization and cancellation (C6a R1) ------------------------------------------------------
#
# docs/c6a-replacement-machine-design.md, Amendment A (14.2-14.5, 14.9). Nothing here redeems a replacement, retires a
# credential, a key or a source, or writes a cutover: that is R2. The replacement installation's row is the durable
# authorization record (its write-once columns); auth_audit_log records each action in the same transaction.

# The only statement that writes the replacement columns (the general create path never names them). The trigger of
# migration b8e2d4f61a07 keeps them as written.
_INSERT_REPLACEMENT_SQL = """
    INSERT INTO collector_installations
        (organization_id, branch_id, name, status, required_contract,
         replaces_installation_id, replacement_authorized_by_user_id, replacement_authorized_at)
    VALUES
        (:organization_id, :branch_id, :name, 'provisioning', :required_contract,
         :replaces_installation_id, :authorized_by_user_id, CURRENT_TIMESTAMP)
    RETURNING id
"""

_CANCEL_REPLACEMENT_SQL = """
    UPDATE collector_installations
    SET status = 'retired',
        updated_at = NOW()
    WHERE id = :installation_id
      AND organization_id = :organization_id
      AND status = :previous_status
    RETURNING id
"""

_REVOKE_UNUSED_CODES_SQL = """
    UPDATE collector_enrollment_codes
    SET revoked_at = CURRENT_TIMESTAMP
    WHERE installation_id = :installation_id
      AND used_at IS NULL
      AND revoked_at IS NULL
    RETURNING id
"""


def _replacement_subject(conn: Any, library: Library, installation_id: int) -> Any:
    found = replacement_eligibility.replacement_subject(conn, library.id, installation_id)
    if found is None:
        raise InstallationAdminError("installation_not_found")
    return found


_lock_branch_for_tenant = replacement_eligibility.lock_branch_for_tenant


def _require_eligible_source(
    conn: Any, library: Library, source_id: int
) -> tuple[Any, replacement_eligibility.PrimarySite]:
    """The source and the primary branch, if `source_id` may be replaced now (services.replacement_eligibility, shared with
    redemption). The caller holds the organization FOR UPDATE."""
    try:
        return replacement_eligibility.require_eligible_source(conn, library.id, source_id)
    except replacement_eligibility.ReplacementIneligible as refused:
        raise InstallationAdminError(refused.code) from None


def authorize_replacement(
    library: Library, source_installation_id: int, *, name: str, confirm_lost_response: bool, actor: dict[str, Any]
) -> dict[str, Any]:
    """Authorizes the replacement of `source_installation_id` (14.3): one new `provisioning` Contract 2 installation at the
    source's branch, naming the source, the authorizing administrator and the time in its write-once columns, audited in
    the same transaction. Nothing else changes: the source keeps running, with its tokens, key, status and cutover.

    `confirm_lost_response` must be True for a `provisioning` source (14.3a): the administrator confirms its enrollment
    response was lost and setup cannot resume. Returns {"installation", "replaces_installation_id"}.

    With machine replacement off (14.11), refused (`replacement_disabled`) before anything is read or written."""
    if not replacement_eligibility.replacement_enabled():
        raise InstallationAdminError(REPLACEMENT_DISABLED)
    name = validate_name(name)
    _refuse_cancelled(library)
    if not v2_settings_ready(library):
        raise InstallationAdminError(COLLECTOR_SETTINGS_NOT_READY)
    with get_engine().begin() as conn:
        _lock_library(conn, library)
        source, primary = _require_eligible_source(conn, library, source_installation_id)
        if replacement_eligibility.pending_replacement_at_branch(conn, primary.id) is not None:
            raise InstallationAdminError(REPLACEMENT_ALREADY_PENDING)
        provisioning_source = source["status"] == "provisioning"
        if provisioning_source and confirm_lost_response is not True:
            raise InstallationAdminError(LOST_RESPONSE_CONFIRMATION_REQUIRED)

        installation_id = int(conn.execute(text(_INSERT_REPLACEMENT_SQL), {
            "organization_id": library.id, "branch_id": primary.id, "name": name,
            "required_contract": tenant_service.COLLECTOR_CONTRACT_V2,
            "replaces_installation_id": source_installation_id, "authorized_by_user_id": int(actor["id"]),
        }).scalar_one())
        details: dict[str, Any] = {
            "installation_id": installation_id, "replaces_installation_id": source_installation_id,
            "source_status": source["status"], "branch_slug": primary.slug,
        }
        if provisioning_source:
            details["lost_response_confirmed"] = True
        _audit(conn, REPLACEMENT_AUTHORIZED, "Collector replacement authorized.", library, actor, **details)
        return {"installation": _one(conn, library, installation_id), "replaces_installation_id": source_installation_id}


def cancel_replacement(library: Library, installation_id: int, *, actor: dict[str, Any]) -> dict[str, Any]:
    """Cancels a pending replacement (14.3, D7): it is retired, its unused enrollment codes revoked, and the cancellation
    audited in the same transaction. The source is untouched. not_a_pending_replacement for any other installation,
    including a replacement already redeemed. Returns {"installation", "replaces_installation_id"}."""
    _refuse_cancelled(library)
    with get_engine().begin() as conn:
        _lock_library(conn, library)
        subject = _replacement_subject(conn, library, installation_id)
        if subject["replaces_installation_id"] is None or subject["status"] == "retired":
            raise InstallationAdminError(NOT_A_PENDING_REPLACEMENT)
        _lock_branch_for_tenant(conn, subject)
        if _owns_any_key(conn, installation_id):
            raise InstallationAdminError(NOT_A_PENDING_REPLACEMENT)

        cancelled = conn.execute(text(_CANCEL_REPLACEMENT_SQL), {
            "installation_id": installation_id, "organization_id": library.id, "previous_status": subject["status"],
        }).first()
        if cancelled is None:
            raise InstallationAdminError("transition_not_allowed")  # changed meanwhile
        revoked = len(conn.execute(text(_REVOKE_UNUSED_CODES_SQL), {"installation_id": installation_id}).fetchall())
        replaces = int(subject["replaces_installation_id"])
        _audit(conn, REPLACEMENT_CANCELLED, "Collector replacement cancelled.", library, actor,
               installation_id=installation_id, replaces_installation_id=replaces,
               previous_status=subject["status"], revoked_code_count=revoked)
        return {"installation": _one(conn, library, installation_id), "replaces_installation_id": replaces}
