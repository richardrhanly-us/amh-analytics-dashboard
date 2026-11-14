"""Which Collector installation may be replaced now: the rules shared by replacement authorization (C6a R1,
services.admin_installation_service) and replacement redemption (C6a R2, services.collector_enrollment_service).

docs/c6a-replacement-machine-design.md, 14.3, 14.3a, 14.5 and 14.4a decision D. Each refusal is a fixed code; the caller turns
it into its own error (an admin refusal, or `enrollment_not_ready` at redemption). The codes are R1's, unchanged.

Free of both callers on purpose: the enrollment service cannot import the admin service, which imports it. It reads only;
the one lock it takes is the branch FOR NO KEY UPDATE, as a retirement takes it. ingest_key_ids is under row level
security, so it sets the branch's tenant context before reading a key, from the installation row, never from a request.

THE REPLACEMENT FLAG (14.11). Machine replacement is off unless SORTVIEW_MACHINE_REPLACEMENT_ENABLED is `true` (trimmed, any
case). Off, authorization, a code for a pending replacement and the redemption of a replacement's code are refused;
cancellation, and everything that is not a replacement, are not affected. Read on each call, by replacement_enabled()
alone: this module is imported both as src.services and as services, and an import-time constant would exist twice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from .ingest_v2_service import active_ingest_key_owners, has_any_v2_cutover

CONTRACT_V2 = 2

NO_ACTIVE_PRIMARY_BRANCH = "no_active_primary_branch"
OPERATIONAL_IDENTITY_INCOMPLETE = "operational_identity_incomplete"
REPLACEMENT_SOURCE_RETIRED = "replacement_source_retired"
REPLACEMENT_SOURCE_NOT_CONTRACT_2 = "replacement_source_not_contract_2"
REPLACEMENT_SOURCE_NOT_AT_PRIMARY_BRANCH = "replacement_source_not_at_primary_branch"
REPLACEMENT_SOURCE_NOT_KEY_OWNER = "replacement_source_not_key_owner"
REPLACEMENT_BRANCH_NOT_ENROLLED = "replacement_branch_not_enrolled"
UNBOUND_TOKENS_ACTIVE = "unbound_tokens_active"
INSTALLATION_NOT_FOUND = "installation_not_found"
REPLACEMENT_DISABLED = "replacement_disabled"

ENABLED_VARIABLE = "SORTVIEW_MACHINE_REPLACEMENT_ENABLED"


def replacement_enabled() -> bool:
    """True only when SORTVIEW_MACHINE_REPLACEMENT_ENABLED is `true`, trimmed and in any case. Unset or anything else: off."""
    return os.getenv(ENABLED_VARIABLE, "").strip().lower() == "true"


class ReplacementIneligible(Exception):
    """`code` is a fixed refusal code, and the only text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PrimarySite:
    id: int  # internal, never returned
    slug: str
    status: str


# The installation with what the replacement rules need: its tenant mapping comes with it, so nothing is read twice.
_SUBJECT_SQL = """
    SELECT
        ci.id, ci.status, ci.required_contract, ci.branch_id, ci.replaces_installation_id,
        b.operational_branch_id, o.operational_customer_id
    FROM collector_installations ci
    JOIN organizations o
      ON o.id = ci.organization_id
    JOIN branches b
      ON b.id = ci.branch_id
    WHERE ci.id = :installation_id
      AND ci.organization_id = :organization_id
"""

# LIMIT 2: a library with two primary branches has no primary branch to replace at.
_PRIMARY_BRANCH_SQL = """
    SELECT id, slug, status
    FROM branches
    WHERE organization_id = :organization_id
      AND is_primary = TRUE
    ORDER BY id
    LIMIT 2
"""

_LOCK_BRANCH_SQL = "SELECT id FROM branches WHERE id = :branch_id FOR NO KEY UPDATE"

# A legacy unbound token at the branch could upload under the branch's key whatever an installation's status (14.3, D4).
_ACTIVE_UNBOUND_AT_BRANCH_SQL = """
    SELECT 1
    FROM agent_tokens
    WHERE installation_id IS NULL
      AND is_active = TRUE
      AND customer_id = :customer_id
      AND branch_id = :operational_branch_id
    LIMIT 1
"""

# A pending replacement at the branch (14.5): names a source, is not retired, owns no key. ingest_key_ids under row level
# security: the caller has set the branch's tenant context.
_PENDING_REPLACEMENT_AT_BRANCH_SQL = """
    SELECT ci.id
    FROM collector_installations ci
    WHERE ci.branch_id = :branch_id
      AND ci.replaces_installation_id IS NOT NULL
      AND ci.status <> 'retired'
      AND NOT EXISTS (SELECT 1 FROM ingest_key_ids k WHERE k.installation_id = ci.id)
"""


def _is_postgresql(conn: Any) -> bool:
    return getattr(getattr(conn, "dialect", None), "name", "") == "postgresql"


def _set_tenant_context(conn: Any, customer_id: int, operational_branch_id: int) -> None:
    if not _is_postgresql(conn):  # SQLite (unit tests) has neither RLS nor set_config()
        return
    conn.execute(text("SELECT set_config('app.operational_customer_id', :v, true)"), {"v": str(customer_id)})
    conn.execute(text("SELECT set_config('app.operational_branch_id', :v, true)"), {"v": str(operational_branch_id)})


def replacement_subject(conn: Any, organization_id: int, installation_id: int) -> Any | None:
    """The installation with its tenant mapping, if it is this organization's; None otherwise."""
    return conn.execute(
        text(_SUBJECT_SQL), {"installation_id": installation_id, "organization_id": organization_id}
    ).mappings().first()


def primary_site(conn: Any, organization_id: int) -> PrimarySite | None:
    rows = conn.execute(text(_PRIMARY_BRANCH_SQL), {"organization_id": organization_id}).mappings().all()
    if len(rows) != 1:
        return None
    row = rows[0]
    return PrimarySite(id=int(row["id"]), slug=row["slug"], status=row["status"])


def lock_branch_for_tenant(conn: Any, subject: Any) -> bool:
    """The subject's branch FOR NO KEY UPDATE (as a retirement takes it, against an enrollment's FOR UPDATE), then its
    tenant context for ingest_key_ids' row level security. False if the tenant is not fully mapped: it has no key then."""
    if _is_postgresql(conn):
        conn.execute(text(_LOCK_BRANCH_SQL), {"branch_id": subject["branch_id"]})
    if subject["operational_customer_id"] is None or subject["operational_branch_id"] is None:
        return False
    _set_tenant_context(conn, int(subject["operational_customer_id"]), int(subject["operational_branch_id"]))
    return True


def require_eligible_source(conn: Any, organization_id: int, source_id: int) -> tuple[Any, PrimarySite]:
    """The source and the primary branch, if `source_id` may be replaced now (14.3 checks 1-5, 14.3a): the organization's,
    Contract 2, not retired, at its active primary branch, the owner of the branch's ONLY active key, the branch enrolled
    (a v2 cutover) and no active legacy unbound token at it. ReplacementIneligible(code) otherwise. Takes the branch lock
    and leaves the branch's tenant context set. Whether the organization may act at all is the caller's to check."""
    source = replacement_subject(conn, organization_id, source_id)
    if source is None:
        raise ReplacementIneligible(INSTALLATION_NOT_FOUND)
    if source["status"] == "retired":
        raise ReplacementIneligible(REPLACEMENT_SOURCE_RETIRED)
    if int(source["required_contract"]) != CONTRACT_V2:
        raise ReplacementIneligible(REPLACEMENT_SOURCE_NOT_CONTRACT_2)
    primary = primary_site(conn, organization_id)
    if primary is None or primary.status != "active":
        raise ReplacementIneligible(NO_ACTIVE_PRIMARY_BRANCH)
    if int(source["branch_id"]) != primary.id:
        raise ReplacementIneligible(REPLACEMENT_SOURCE_NOT_AT_PRIMARY_BRANCH)
    if not lock_branch_for_tenant(conn, source):
        raise ReplacementIneligible(OPERATIONAL_IDENTITY_INCOMPLETE)
    customer_id, operational_branch_id = int(source["operational_customer_id"]), int(source["operational_branch_id"])
    # Key ownership, not status, decides (14.3a): no key, a key with no owner, two active keys or another owner all refuse.
    if active_ingest_key_owners(conn, customer_id, operational_branch_id) != [source_id]:
        raise ReplacementIneligible(REPLACEMENT_SOURCE_NOT_KEY_OWNER)
    if not has_any_v2_cutover(conn, customer_id, operational_branch_id):
        raise ReplacementIneligible(REPLACEMENT_BRANCH_NOT_ENROLLED)
    if conn.execute(
        text(_ACTIVE_UNBOUND_AT_BRANCH_SQL),
        {"customer_id": customer_id, "operational_branch_id": operational_branch_id},
    ).first() is not None:
        raise ReplacementIneligible(UNBOUND_TOKENS_ACTIVE)
    return source, primary


# The library's one primary branch with its tenant mapping, for the list's read of the pending replacement.
_PRIMARY_TENANT_SQL = """
    SELECT b.id, b.operational_branch_id, o.operational_customer_id
    FROM branches b
    JOIN organizations o
      ON o.id = b.organization_id
    WHERE b.organization_id = :organization_id
      AND b.is_primary = TRUE
    ORDER BY b.id
    LIMIT 2
"""


def pending_replacement_for_library(conn: Any, organization_id: int) -> int | None:
    """The organization's pending replacement, if it has one, for display (14.11, decision 1): read, never locked.

    A replacement is authorized only at the library's primary branch, so that is where it is looked for, by the same
    definition authorization and redemption use (pending_replacement_at_branch). ingest_key_ids is under row level
    security, so the branch's tenant context is set first: without it every key would be hidden and a redeemed
    replacement would look pending. A tenant that is not fully mapped has no key at all, so none is hidden from it."""
    rows = conn.execute(text(_PRIMARY_TENANT_SQL), {"organization_id": organization_id}).mappings().all()
    if len(rows) != 1:
        return None
    primary = rows[0]
    if primary["operational_customer_id"] is not None and primary["operational_branch_id"] is not None:
        _set_tenant_context(conn, int(primary["operational_customer_id"]), int(primary["operational_branch_id"]))
    return pending_replacement_at_branch(conn, int(primary["id"]))


def pending_replacement_at_branch(conn: Any, branch_id: int, *, excluding: int | None = None) -> int | None:
    """A pending replacement at the branch, other than `excluding`, or None (14.5; 14.4a decision B). The caller has set
    the branch's tenant context."""
    sql, params = _PENDING_REPLACEMENT_AT_BRANCH_SQL, {"branch_id": branch_id}
    if excluding is not None:
        sql, params = sql + "  AND ci.id <> :excluding", {**params, "excluding": excluding}
    found = conn.execute(text(sql + "    LIMIT 1"), params).scalar()
    return None if found is None else int(found)
