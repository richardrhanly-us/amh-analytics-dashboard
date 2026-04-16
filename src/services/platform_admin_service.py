from __future__ import annotations

from typing import Any

from sqlalchemy import text

from database import get_engine


def is_platform_admin(user_id: int) -> bool:
    sql = text("""
        select is_platform_admin
        from app_users
        where id = :user_id
        limit 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"user_id": user_id}).mappings().first()
        if not row:
            return False
        return bool(row["is_platform_admin"])


def list_platform_libraries() -> list[dict[str, Any]]:
    sql = text("""
        select
            o.id as organization_id,
            o.name as organization_name,
            o.slug as organization_slug,
            o.status as organization_status,
            b.id as branch_id,
            b.name as branch_name,
            b.slug as branch_slug,
            b.is_primary,
            s.status as subscription_status,
            p.code as plan_code,
            p.name as plan_name
        from organizations o
        left join branches b
          on b.organization_id = o.id
         and b.is_primary = true
        left join subscriptions s
          on s.organization_id = o.id
        left join plans p
          on p.id = s.plan_id
        order by lower(o.name), b.id
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
        return [dict(row) for row in rows]
