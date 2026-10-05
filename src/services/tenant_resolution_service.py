"""Server-side resolution of the OPERATIONAL tenant behind a SaaS organization
and branch.

A browser (or any other caller) names an organization and a branch by slug.
The operational tables under row level security are keyed by something else:
organizations.operational_customer_id and branches.operational_branch_id.
resolve_operational_tenant is the one place that mapping is made, and it is
made only for a user who is entitled to it. Operational ids are never taken
from a caller: this module has no parameter that accepts one.

ONE QUERY, ONE DECISION. Membership, organization status, branch ownership,
branch status and the presence of both operational ids are read by a single
statement on a single connection, so the decision is made from one coherent
view of the database rather than assembled from several separate lookups.

FAIL CLOSED. Anything short of "exactly one row, and every rule satisfied"
resolves to None: no such organization, not a member, an inactive user, a
cancelled or otherwise unusable organization, no such branch, a branch of a
different organization, an inactive branch, a missing operational id, or more
than one matching row. No default and no fallback (not even to a primary
branch) is ever substituted. The reason is deliberately not reported, so a
caller cannot be used to probe which of those is true.

DATABASE ERRORS PROPAGATE. A failed query is an operational error, not "no
access": it is never turned into None.

NOTHING IS CACHED and no transaction is held open for the caller. Each call
queries again. A caller that goes on to query operational data therefore does
so a moment after this decision was made; that is acceptable because the ids
returned here are then applied as the RLS tenant context (tenant_db), which
constrains every such query to exactly this tenant.

Framework-neutral: no Streamlit, no FastAPI, no HTTP response logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text

from database import get_engine

# How an organization's lifecycle status maps to what its members may do.
# This is the same rule as access_service.get_org_access_mode -- repeated here
# only because that function runs its own query and this decision must come
# from the single resolver query. tests/test_tenant_resolution_service.py
# pins that the two agree for every status.
#   active / trial -> "full"
#   suspended      -> "read_only"  (history stays readable; mutations do not)
#   anything else  -> not resolvable at all
_ACCESS_MODE_BY_ORGANIZATION_STATUS = {
    "active": "full",
    "trial": "full",
    "suspended": "read_only",
}
_USABLE_BRANCH_STATUS = "active"

# LIMIT 2, not 1: the schema makes a second row impossible (organizations.slug,
# (organization_id, branch slug) and (organization_id, user_id) are all
# unique), so a second row would mean that guarantee is gone -- and that must
# be refused, not resolved to whichever row happened to come first.
_RESOLVE_SQL = text("""
    SELECT
        u.is_active AS user_is_active,
        o.slug AS org_slug,
        o.status AS organization_status,
        o.operational_customer_id AS operational_customer_id,
        b.slug AS branch_slug,
        b.status AS branch_status,
        b.operational_branch_id AS operational_branch_id
    FROM memberships m
    JOIN app_users u
      ON u.id = m.user_id
    JOIN organizations o
      ON o.id = m.organization_id
    JOIN branches b
      ON b.organization_id = o.id
    WHERE m.user_id = :user_id
      AND o.slug = :org_slug
      AND b.slug = :branch_slug
    LIMIT 2
""")


@dataclass(frozen=True, slots=True)
class ResolvedOperationalTenant:
    """The operational tenant a user may read, as resolved from the database.

    org_slug and branch_slug are the values of the rows that were resolved,
    not an echo of what the caller asked for. access_mode is "full" or
    "read_only"; a caller that is about to change something must check it.

    The two operational ids are server-internal authorization context: they
    are what tenant_db applies as the RLS tenant. They are left out of the
    repr so an ordinary log line or traceback does not carry them, and they
    must never be put in a response.
    """

    org_slug: str
    branch_slug: str
    access_mode: str
    operational_customer_id: int = field(repr=False)
    operational_branch_id: int = field(repr=False)


def resolve_operational_tenant(user_id: int, org_slug: str, branch_slug: str) -> ResolvedOperationalTenant | None:
    """Resolves (user, organization slug, branch slug) to the operational
    tenant that user may read, or None. See the module docstring for the
    rules; database errors propagate."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            _RESOLVE_SQL,
            {"user_id": user_id, "org_slug": org_slug, "branch_slug": branch_slug},
        ).mappings().all()

    if len(rows) != 1:
        return None
    row = rows[0]

    if not row["user_is_active"]:
        return None

    access_mode = _ACCESS_MODE_BY_ORGANIZATION_STATUS.get(row["organization_status"])
    if access_mode is None:
        return None

    if row["branch_status"] != _USABLE_BRANCH_STATUS:
        return None

    if row["operational_customer_id"] is None or row["operational_branch_id"] is None:
        return None

    return ResolvedOperationalTenant(
        org_slug=row["org_slug"],
        branch_slug=row["branch_slug"],
        access_mode=access_mode,
        operational_customer_id=int(row["operational_customer_id"]),
        operational_branch_id=int(row["operational_branch_id"]),
    )
