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
            b.status as branch_status,
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


# Library lifecycle. organizations.status is constrained to
# active / trial / suspended / cancelled ('inactive' is NOT a valid value).
#
#   Deactivate = reversible administrative SUSPENSION: organizations.status
#   becomes 'suspended'. Nothing else changes -- historical data, operational
#   identity mappings, collector_installations, subscriptions and agent tokens
#   are all preserved, and branch statuses are left alone (a branch that was
#   individually inactive must not become active on reactivation). The API
#   rejects ingestion for a suspended organization even though its tokens stay
#   active (see main.authenticate_agent).
#
#   Reactivate = 'suspended' -> 'active'. It never touches tokens: token state
#   and tenant state are independent gates.
#
#   'cancelled' is terminal here: neither action will change it. Cancellation
#   semantics are out of scope for this function.
_ORGANIZATION_STATUSES = ("active", "trial", "suspended", "cancelled")


def set_library_active_status(organization_id: int, is_active: bool) -> dict[str, Any]:
    """Suspend (is_active=False) or reactivate (is_active=True) a library.

    Only organizations.status is written, and only when it actually needs to
    change: suspending an already-suspended library and reactivating an
    active/trial one are no-ops (a trial library is not promoted to 'active').
    Raises RuntimeError, writing nothing, if the organization does not exist,
    is cancelled, or has an unrecognised status.

    Returns {organization_id, status, changed}.
    """
    sql_lock = text("""
        select status
        from organizations
        where id = :organization_id
        for update
    """)

    sql_update = text("""
        update organizations
        set status = :new_status,
            updated_at = now()
        where id = :organization_id
    """)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(sql_lock, {"organization_id": organization_id}).mappings().first()
        if not row:
            raise RuntimeError(f"Organization {organization_id} not found")

        current = row["status"]
        if current not in _ORGANIZATION_STATUSES:
            raise RuntimeError(
                f"Organization {organization_id} has unrecognised status {current!r}"
            )
        if current == "cancelled":
            raise RuntimeError(
                f"Organization {organization_id} is cancelled; it cannot be "
                f"{'reactivated' if is_active else 'suspended'} from here"
            )

        if is_active:
            if current in ("active", "trial"):
                return {"organization_id": organization_id, "status": current, "changed": False}
            new_status = "active"
        else:
            if current == "suspended":
                return {"organization_id": organization_id, "status": current, "changed": False}
            new_status = "suspended"

        conn.execute(
            sql_update,
            {"organization_id": organization_id, "new_status": new_status},
        )

    return {"organization_id": organization_id, "status": new_status, "changed": True}
