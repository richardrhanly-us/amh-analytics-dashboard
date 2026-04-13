from __future__ import annotations

from typing import Any

from sqlalchemy import text

from database import get_engine


def get_org_role_for_user(user_id: int, org_slug: str) -> str | None:
    sql = text("""
        SELECT m.role
        FROM memberships m
        JOIN organizations o
          ON o.id = m.organization_id
        WHERE m.user_id = :user_id
          AND o.slug = :org_slug
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            sql,
            {"user_id": user_id, "org_slug": org_slug},
        ).first()
        return row[0] if row else None


def get_org_subscription(org_slug: str) -> dict[str, Any] | None:
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

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"org_slug": org_slug}).mappings().first()
        return dict(row) if row else None


def get_plan_entitlements(plan_id: int) -> dict[str, dict[str, Any]]:
    sql = text("""
        SELECT feature_key, enabled, limit_value
        FROM feature_entitlements
        WHERE plan_id = :plan_id
        ORDER BY feature_key
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"plan_id": plan_id}).mappings().all()

    return {
        row["feature_key"]: {
            "enabled": bool(row["enabled"]),
            "limit_value": row["limit_value"],
        }
        for row in rows
    }


def build_entitlement_context(user_id: int, org_slug: str) -> dict[str, Any]:
    role = get_org_role_for_user(user_id=user_id, org_slug=org_slug)
    subscription = get_org_subscription(org_slug=org_slug)

    entitlements = {}
    if subscription and subscription.get("plan_id"):
        entitlements = get_plan_entitlements(subscription["plan_id"])

    return {
        "role": role,
        "subscription": subscription,
        "entitlements": entitlements,
    }


def feature_enabled(entitlement_context: dict[str, Any], feature_key: str) -> bool:
    feature = entitlement_context.get("entitlements", {}).get(feature_key)
    if not feature:
        return False
    return bool(feature.get("enabled", False))


def feature_limit(entitlement_context: dict[str, Any], feature_key: str):
    feature = entitlement_context.get("entitlements", {}).get(feature_key)
    if not feature:
        return None
    return feature.get("limit_value")
