from __future__ import annotations

from typing import Any

from sqlalchemy import text

from database import get_engine


def get_org_branches(org_slug: str) -> list[dict]:
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

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"org_slug": org_slug}).mappings().all()
        return [dict(row) for row in rows]


def get_user_memberships(user_id: int) -> list[dict[str, Any]]:
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

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"user_id": user_id}).mappings().all()
        return [dict(row) for row in rows]


def user_can_access_org(user_id: int, org_slug: str) -> bool:
    sql = text("""
        SELECT 1
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
        return row is not None
