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
                no cutover is touched.
    code        a one-time enrollment code, by services.collector_enrollment_service.create_enrollment_code -- the one
                implementation: it revokes the installation's earlier unused codes, stores only the code's hash,
                snapshots the contract and returns the raw code ONCE. For a contract-2 installation whose branch
                already has an active ingest key or any v2 cutover, nothing is written and BranchAlreadyEnrolled is
                raised: such a code could never be redeemed (replacing a v2 machine is C6).

NOTHING HERE HOLDS OR RETURNS a hostname, a Collector version, a token or its hash, a code's hash, a key id or an
operational or internal organization/branch id. The raw code leaves only in issue_enrollment_code's return value and is
never logged, audited or put in an exception. Audit rows name the library as `target_org_slug` -- never `org_slug`,
which marks an event as one the organization's own members may see (user_admin_service).
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
    tenant_service,
)
from .collector_enrollment_service import EnrollmentError
from .ingest_v2_service import has_active_ingest_key, has_any_v2_cutover

MAX_NAME_LENGTH = 120

# Which status each status may become here. Retired is final; nothing is made active by hand.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "provisioning": ("inactive", "retired"),
    "active": ("inactive", "retired"),
    "inactive": ("provisioning", "retired"),
    "retired": (),
}

# What stops an installation being added (a fixed code each).
LIBRARY_CANCELLED = "library_cancelled"
NO_ACTIVE_PRIMARY_BRANCH = "no_active_primary_branch"
COLLECTOR_SETTINGS_NOT_READY = "collector_settings_not_ready"

INSTALLATION_CREATED = "admin_installation_created"
INSTALLATION_RENAMED = "admin_installation_renamed"
INSTALLATION_STATUS_CHANGED = "admin_installation_status_changed"
ENROLLMENT_CODE_ISSUED = "admin_enrollment_code_issued"


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
    SELECT status FROM collector_installations WHERE id = :installation_id AND organization_id = :organization_id
"""

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


def change_status(library: Library, installation_id: int, *, status: str, actor: dict[str, Any]) -> dict[str, Any]:
    """Moves the installation to `status` if TRANSITIONS allows it from its current one; transition_not_allowed if
    not, or if it changed meanwhile."""
    _refuse_cancelled(library)
    with get_engine().begin() as conn:
        current = conn.execute(
            text(_CURRENT_STATUS_SQL), {"installation_id": installation_id, "organization_id": library.id}
        ).scalar()
        if current is None:
            raise InstallationAdminError("installation_not_found")
        if not transition_allowed(current, status):
            raise InstallationAdminError("transition_not_allowed")
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
    of a usable tenant, and (contract 2) its branch is not already enrolled -- is made before anything is written."""
    _refuse_cancelled(library)
    with get_engine().begin() as conn:
        try:
            scope = collector_enrollment_service.resolve_installation_scope(
                conn, installation_id, lock=True, expected_organization_id=library.id
            )
        except EnrollmentError as refused:
            raise InstallationAdminError(_public_refusal(refused.reason)) from None

        if scope.required_contract == collector_enrollment_service.CONTRACT_V2:
            # ingest_key_ids is read under row level security: the branch's own tenant, as the enrollment reads it.
            collector_enrollment_service._set_tenant_context(conn, scope)
            if has_active_ingest_key(conn, scope.customer_id, scope.operational_branch_id) or has_any_v2_cutover(
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
