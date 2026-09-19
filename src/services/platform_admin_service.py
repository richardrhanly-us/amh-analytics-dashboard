from __future__ import annotations

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


def list_libraries_with_status():
    # pipeline_status is keyed by OPERATIONAL (customer_id, branch_id), not by
    # organizations.id / branches.id (the SaaS-facing IDs). It must be joined
    # through the operational_customer_id / operational_branch_id bridge, with
    # no fallback to the SaaS IDs: an organization or branch that has no
    # operational mapping (NULL) matches no pipeline_status row, which is the
    # correct "not reporting" answer. The two ID domains only coincide by
    # accident for some tenants (e.g. NBPL, 1/1).
    #
    # Each organization is joined to only its most recent subscription (newest
    # created_at, id as the tie-breaker -- the same "latest subscription"
    # ordering tenant_service and entitlement_service use), so an organization
    # with several subscription rows still yields one row.
    sql = text("""
        select
            o.id as organization_id,
            o.name as organization_name,
            o.slug as organization_slug,
            o.status as organization_status,
            o.operational_customer_id,
            b.id as branch_id,
            b.name as branch_name,
            b.slug as branch_slug,
            b.operational_branch_id,
            b.is_primary,
            s.status as subscription_status,
            p.code as plan_code,
            p.name as plan_name,
            ps.status as pipeline_status,
            ps.last_run,
            ps.last_attempt
        from organizations o
        left join branches b
            on b.organization_id = o.id
           and b.is_primary = true
        left join subscriptions s
            on s.id = (
                select s2.id
                from subscriptions s2
                where s2.organization_id = o.id
                order by s2.created_at desc, s2.id desc
                limit 1
            )
        left join plans p
            on p.id = s.plan_id
        left join pipeline_status ps
            on ps.customer_id = o.operational_customer_id
           and ps.branch_id = b.operational_branch_id
        order by lower(o.name), b.id
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
        return [dict(row) for row in rows]


def set_library_active_status(organization_id: int, branch_id: int, is_active: bool):
    new_status = "active" if is_active else "inactive"

    sql_update_org = text("""
        update organizations
        set status = :new_status
        where id = :organization_id
    """)

    sql_update_branch = text("""
        update branches
        set status = :new_status
        where id = :branch_id
    """)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sql_update_org,
            {
                "organization_id": organization_id,
                "new_status": new_status,
            },
        )
        conn.execute(
            sql_update_branch,
            {
                "branch_id": branch_id,
                "new_status": new_status,
            },
        )
