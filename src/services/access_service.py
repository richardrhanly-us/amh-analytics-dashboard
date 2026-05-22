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
