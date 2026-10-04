#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         access_service.py
#
#  Description: Provides organization and branch access helpers for
#               the SortView dashboard. This file loads the active
#               branches for an organization, retrieves the
#               organizations assigned to a user, and verifies whether
#               a user has access to a selected organization.
#
#***************************************************************

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from database import get_engine

# Every function in this module is UNCACHED and framework-neutral: each call
# queries the database, and nothing here imports Streamlit. The dashboard's
# cached copies of get_org_branches/get_user_memberships live in
# services.streamlit_access_adapter; any non-Streamlit caller (e.g. an API
# route) must use this module directly, never that adapter.
#
# user_can_access_org is the tenant-isolation gate checked before any
# org-scoped data loads, and get_org_access_mode is the organization
# lifecycle gate. Neither may ever be served from a cache -- a just-revoked
# user (or a just-cancelled organization) must fail on the very next call.

#***************************************************************
#
#  Function:     get_org_branches
#
#  Description: Loads all active branches for a selected organization.
#               The primary branch is listed first, followed by the
#               remaining branches in alphabetical order.
#
#  Parameters:  org_slug - Organization slug used to identify the
#                          selected organization.
#
#  Returns:     list[dict] - List of active branch records for the
#                            organization.
#
#***************************************************************

def get_org_branches(org_slug: str) -> list[dict]:
    # Build the SQL query used to load active branches for the organization.
    sql = text("""
        SELECT
            b.id,
            b.operational_branch_id AS branch_id,
            b.slug AS branch_slug,
            b.name AS branch_name,
            b.is_primary,
            b.status
        FROM branches b
        JOIN organizations o
          ON o.id = b.organization_id
        WHERE o.slug = :org_slug
          AND b.status = 'active'
        ORDER BY b.is_primary DESC, b.name ASC
    """)

    # Execute the query and convert each result row into a dictionary.
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"org_slug": org_slug}).mappings().all()
        return [dict(row) for row in rows]


#***************************************************************
#
#  Function:     get_user_memberships
#
#  Description: Loads the organizations assigned to a user. Each
#               membership includes the user's role, organization
#               display information, and operational customer ID used
#               by the dashboard data layer.
#
#  Parameters:  user_id - Internal user ID for the authenticated user.
#
#  Returns:     list[dict[str, Any]] - List of organization membership
#                                      records for the user.
#
#***************************************************************

def get_user_memberships(user_id: int) -> list[dict[str, Any]]:
    # Build the SQL query used to load the user's organization memberships.
    # A cancelled organization is excluded here so it simply disappears from
    # the user's org list (reusing app.py's existing "selected org not in
    # the allowed list" self-healing clamp and its "no organizations" empty
    # state) rather than needing new UI to represent a visible-but-blocked
    # option. A suspended organization is deliberately still included --
    # suspended customers retain read-only access, enforced by
    # get_org_access_mode below, not by hiding the org from this list.
    sql = text("""
        SELECT
            m.organization_id,
            o.operational_customer_id AS customer_id,
            m.role,
            o.slug AS organization_slug,
            o.name AS organization_name
        FROM memberships m
        JOIN organizations o
          ON o.id = m.organization_id
        WHERE m.user_id = :user_id
          AND o.status != 'cancelled'
        ORDER BY o.name
    """)

    # Execute the query and convert each result row into a dictionary.
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"user_id": user_id}).mappings().all()
        return [dict(row) for row in rows]


#***************************************************************
#
#  Function:     user_can_access_org
#
#  Description: Checks whether a user has membership access to a
#               specific organization.
#
#  Parameters:  user_id - Internal user ID for the authenticated user.
#               org_slug - Organization slug being checked.
#
#  Returns:     bool - True if the user has access to the organization;
#                      otherwise False.
#
#***************************************************************

def user_can_access_org(user_id: int, org_slug: str) -> bool:
    # Build a minimal query that only checks whether a matching membership exists.
    sql = text("""
        SELECT 1
        FROM memberships m
        JOIN organizations o
          ON o.id = m.organization_id
        WHERE m.user_id = :user_id
          AND o.slug = :org_slug
        LIMIT 1
    """)

    # If the query returns a row, the user has access to the organization.
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            sql,
            {"user_id": user_id, "org_slug": org_slug},
        ).first()
        return row is not None


#***************************************************************
#
#  Function:     get_org_access_mode
#
#  Description: Maps an organization's lifecycle status to the level of
#               customer access it currently allows:
#                   active / trial -> "full"      normal customer access
#                   suspended      -> "read_only"  dashboard/history reads
#                                                  allowed; admin mutations
#                                                  blocked
#                   cancelled, an unknown org, or any unrecognised status
#                                  -> "blocked"    no customer access
#               Deliberately uncached, matching user_can_access_org's
#               reasoning: this is a security gate (it is what makes a
#               cancelled organization fail closed for an already-open
#               session), not a display value.
#
#  Parameters:  org_slug - Organization slug being checked.
#
#  Returns:     str - "full", "read_only", or "blocked".
#
#***************************************************************

def get_org_access_mode(org_slug: str) -> str:
    sql = text("""
        SELECT status
        FROM organizations
        WHERE slug = :org_slug
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"org_slug": org_slug}).first()

    if row is None:
        return "blocked"

    status = row[0]
    if status in ("active", "trial"):
        return "full"
    if status == "suspended":
        return "read_only"
    return "blocked"
