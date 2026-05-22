#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         entitlement_service.py
#
#  Description: Provides subscription and entitlement lookup helpers
#               for the SortView dashboard. This file retrieves a
#               user's organization role, loads the organization's
#               current subscription, loads plan-level feature
#               entitlements, and builds a combined entitlement context
#               used by permission checks.
#
#***************************************************************

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from database import get_engine


#***************************************************************
#
#  Function:     get_org_role_for_user
#
#  Description: Retrieves the role assigned to a user within a
#               specific organization.
#
#  Parameters:  user_id - Internal user ID.
#               org_slug - Organization slug being checked.
#
#  Returns:     str | None - User role if a membership exists;
#                            otherwise None.
#
#***************************************************************

def get_org_role_for_user(user_id: int, org_slug: str) -> str | None:
    # Build the query used to find the user's role for the organization.
    sql = text("""
        SELECT m.role
        FROM memberships m
        JOIN organizations o
          ON o.id = m.organization_id
        WHERE m.user_id = :user_id
          AND o.slug = :org_slug
        LIMIT 1
    """)

    # Execute the lookup and return the role value when found.
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            sql,
            {"user_id": user_id, "org_slug": org_slug},
        ).first()
        return row[0] if row else None


#***************************************************************
#
#  Function:     get_org_subscription
#
#  Description: Retrieves the most recent subscription record for a
#               specific organization, including the related plan
#               information.
#
#  Parameters:  org_slug - Organization slug being checked.
#
#  Returns:     dict[str, Any] | None - Subscription and plan details
#                                      if found; otherwise None.
#
#***************************************************************

def get_org_subscription(org_slug: str) -> dict[str, Any] | None:
    # Build the query used to load the organization's latest subscription.
    sql = text("""
        SELECT
            s.id,
            s.status,
            s.started_at,
            s.ends_at,
            p.id AS plan_id,
            p.code AS plan_code,
            p.name AS plan_name
        FROM subscriptions s
        JOIN organizations o
          ON o.id = s.organization_id
        JOIN plans p
          ON p.id = s.plan_id
        WHERE o.slug = :org_slug
        ORDER BY s.created_at DESC
        LIMIT 1
    """)

    # Execute the query and return the subscription record as a dictionary.
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"org_slug": org_slug}).mappings().first()
        return dict(row) if row else None


#***************************************************************
#
#  Function:     get_plan_entitlements
#
#  Description: Loads all feature entitlements attached to a specific
#               subscription plan. Each feature includes whether it is
#               enabled and any configured limit value.
#
#  Parameters:  plan_id - Internal plan ID.
#
#  Returns:     dict[str, dict[str, Any]] - Dictionary keyed by feature
#                                           key with enabled and limit
#                                           details for each feature.
#
#***************************************************************

def get_plan_entitlements(plan_id: int) -> dict[str, dict[str, Any]]:
    # Build the query used to load feature entitlements for the plan.
    sql = text("""
        SELECT feature_key, enabled, limit_value
        FROM feature_entitlements
        WHERE plan_id = :plan_id
        ORDER BY feature_key
    """)

    # Execute the query and retrieve all entitlement rows.
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"plan_id": plan_id}).mappings().all()

    # Convert the entitlement rows into a feature-keyed dictionary.
    return {
        row["feature_key"]: {
            "enabled": bool(row["enabled"]),
            "limit_value": row["limit_value"],
        }
        for row in rows
    }


#***************************************************************
#
#  Function:     build_entitlement_context
#
#  Description: Builds the combined entitlement context for a user and
#               organization. The context includes the user's role,
#               the organization's current subscription, and any
#               feature entitlements connected to the subscription plan.
#
#  Parameters:  user_id - Internal user ID.
#               org_slug - Organization slug.
#
#  Returns:     dict[str, Any] - Entitlement context used by permission
#                                checks and dashboard feature gates.
#
#***************************************************************

def build_entitlement_context(user_id: int, org_slug: str) -> dict[str, Any]:
    # Load the user's role and the organization's subscription.
    role = get_org_role_for_user(user_id=user_id, org_slug=org_slug)
    subscription = get_org_subscription(org_slug=org_slug)

    # Load plan entitlements only when a subscription and plan ID exist.
    entitlements = {}
    if subscription and subscription.get("plan_id"):
        entitlements = get_plan_entitlements(subscription["plan_id"])

    return {
        "role": role,
        "subscription": subscription,
        "entitlements": entitlements,
    }


#***************************************************************
#
#  Function:     feature_enabled
#
#  Description: Checks whether a feature is enabled in the supplied
#               entitlement context.
#
#  Parameters:  entitlement_context - Combined entitlement context.
#               feature_key - Feature key being checked.
#
#  Returns:     bool - True if the feature is enabled; otherwise False.
#
#***************************************************************

def feature_enabled(entitlement_context: dict[str, Any], feature_key: str) -> bool:
    # Look up the feature entry from the context's entitlement dictionary.
    feature = entitlement_context.get("entitlements", {}).get(feature_key)
    if not feature:
        return False
    return bool(feature.get("enabled", False))


#***************************************************************
#
#  Function:     feature_limit
#
#  Description: Retrieves the configured limit value for a feature in
#               the supplied entitlement context.
#
#  Parameters:  entitlement_context - Combined entitlement context.
#               feature_key - Feature key being checked.
#
#  Returns:     Any | None - Feature limit value if present; otherwise
#                            None.
#
#***************************************************************

def feature_limit(entitlement_context: dict[str, Any], feature_key: str):
    # Look up the feature entry from the context's entitlement dictionary.
    feature = entitlement_context.get("entitlements", {}).get(feature_key)
    if not feature:
        return None
    return feature.get("limit_value")
