from __future__ import annotations

from typing import Any

from sqlalchemy import text

from database import get_engine
from services.data_lifecycle_policy import (
    EVENT_ACCESS_CUTOFF,
    record_tenant_lifecycle_event,
)


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


# --- permanent offboarding: access cutoff ------------------------------------------------------------------------------
#
# NOT suspension, and deliberately a separate function from set_library_active_status, which it never calls and which
# never calls it. Suspension is reversible and writes one column. Offboarding is the PERMANENT administrative cutoff
# (docs/data-lifecycle-offboarding.md): organizations.status becomes 'cancelled' -- the terminal state every gate already
# fails closed on (main.authenticate_agent, collector enrollment, access_service, and set_library_active_status itself,
# which refuses to change it) -- and every access artifact the tenant holds is revoked in the SAME transaction.
#
# NOTHING IS DELETED. Tokens, installations, codes and keys are revoked/retired in place, so the action is auditable and
# an accidental cutoff can still be undone by hand before any purge. Deleting the tenant's data is a different,
# separately authorized operation that the runtime role cannot perform (scripts/purge_tenant_data.py).
#
# Subscriptions are left exactly as they are: 'cancelled' on the organization is the authoritative service state, and
# billing semantics are not coupled to it.

def _is_postgresql(conn) -> bool:
    # Row locks and set_config() are PostgreSQL-only; SQLite (unit tests) has neither and serializes writers itself.
    return getattr(getattr(conn, "dialect", None), "name", "") == "postgresql"


_OFFBOARD_LOCK_ORGANIZATION_SQL = """
    SELECT id, slug, status, operational_customer_id
    FROM organizations
    WHERE id = :organization_id
"""

_OFFBOARD_CANCEL_ORGANIZATION_SQL = """
    UPDATE organizations
    SET status = 'cancelled',
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :organization_id
      AND status <> 'cancelled'
"""

# Every token of the tenant: one bound to any of its installations, AND a legacy token (installation_id IS NULL), which
# is reachable only through the operational customer id.
_OFFBOARD_REVOKE_TOKENS_SQL = """
    UPDATE agent_tokens
    SET is_active = FALSE
    WHERE is_active = TRUE
      AND (
            customer_id = :customer_id
            OR installation_id IN (
                SELECT id FROM collector_installations WHERE organization_id = :organization_id
            )
          )
    RETURNING id
"""

_OFFBOARD_LIVE_INSTALLATIONS_SQL = """
    SELECT id, status
    FROM collector_installations
    WHERE organization_id = :organization_id
      AND status <> 'retired'
    ORDER BY id
"""

_OFFBOARD_RETIRE_INSTALLATIONS_SQL = """
    UPDATE collector_installations
    SET status = 'retired',
        updated_at = CURRENT_TIMESTAMP
    WHERE organization_id = :organization_id
      AND status <> 'retired'
    RETURNING id
"""

_OFFBOARD_REVOKE_CODES_SQL = """
    UPDATE collector_enrollment_codes
    SET revoked_at = CURRENT_TIMESTAMP
    WHERE used_at IS NULL
      AND revoked_at IS NULL
      AND installation_id IN (
          SELECT id FROM collector_installations WHERE organization_id = :organization_id
      )
    RETURNING id
"""

_OFFBOARD_MAPPED_BRANCHES_SQL = """
    SELECT operational_branch_id
    FROM branches
    WHERE organization_id = :organization_id
      AND operational_branch_id IS NOT NULL
    ORDER BY id
"""

_OFFBOARD_RETIRE_INGEST_KEYS_SQL = """
    UPDATE ingest_key_ids
    SET status = 'retired',
        retired_at = CURRENT_TIMESTAMP
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND status = 'active'
    RETURNING id
"""

# A session belongs to a USER, and a user may belong to several organizations. Only users this cutoff leaves with no
# usable organization at all lose their sessions: a member of another organization that is not cancelled (a suspended
# one still grants read-only access), or a platform admin, keeps theirs -- access_service already blocks the cancelled
# organization for them on every rerun. (The organization is already 'cancelled' in this transaction, so "no membership
# in a non-cancelled organization" needs no special case for it.)
_OFFBOARD_REVOKE_SESSIONS_SQL = """
    UPDATE auth_sessions
    SET revoked_at = CURRENT_TIMESTAMP
    WHERE revoked_at IS NULL
      AND user_id IN (
          SELECT m.user_id
          FROM memberships m
          JOIN app_users u
            ON u.id = m.user_id
          WHERE m.organization_id = :organization_id
            AND u.is_platform_admin = FALSE
            AND NOT EXISTS (
                SELECT 1
                FROM memberships m2
                JOIN organizations o2
                  ON o2.id = m2.organization_id
                WHERE m2.user_id = m.user_id
                  AND o2.status <> 'cancelled'
            )
      )
    RETURNING id
"""


def _returned_ids(result) -> list[int]:
    return sorted(int(row[0]) for row in result.fetchall())


def offboard_library(
    organization_id: int,
    confirm_slug: str,
    *,
    actor_user_id: int | None,
    actor_label: str,
) -> dict[str, Any]:
    """Permanently cuts off a library's access to SortView. One transaction; fails closed (nothing is written) if the
    organization does not exist, has an unrecognised status, or `confirm_slug` is not exactly its slug.

    Sets organizations.status = 'cancelled'; deactivates every agent token of the tenant (installation-bound and legacy
    unbound); retires every collector installation; revokes every unused enrollment code; retires every active ingest
    key; revokes the persistent sessions of users left with no usable organization; and appends one
    tenant_lifecycle_events row naming the actor and the ids of exactly the rows this call changed.

    Idempotent: on an already-cancelled organization the same sweeps run again and change nothing that is already
    revoked. A token, installation, code, key or session that was ALREADY revoked is never touched, so it does not appear
    in the evidence and can never be mistaken for something a reversal should switch back on.

    Returns {organization_id, status, changed, status_before, counts}.
    """
    params = {"organization_id": organization_id}

    engine = get_engine()
    with engine.begin() as conn:
        postgresql = _is_postgresql(conn)

        lock_sql = _OFFBOARD_LOCK_ORGANIZATION_SQL + (" FOR UPDATE" if postgresql else "")
        organization = conn.execute(text(lock_sql), params).mappings().first()
        if not organization:
            raise RuntimeError(f"Organization {organization_id} not found")

        status_before = organization["status"]
        if status_before not in _ORGANIZATION_STATUSES:
            raise RuntimeError(
                f"Organization {organization_id} has unrecognised status {status_before!r}"
            )
        if (confirm_slug or "").strip() != organization["slug"]:
            raise ValueError("Confirmation does not match the organization slug")

        customer_id = organization["operational_customer_id"]
        scope = {"organization_id": organization_id, "customer_id": customer_id}

        # The organization first: from here on, inside this transaction, the tenant is cancelled.
        conn.execute(text(_OFFBOARD_CANCEL_ORGANIZATION_SQL), params)

        token_ids = _returned_ids(conn.execute(text(_OFFBOARD_REVOKE_TOKENS_SQL), scope))

        installations_before = [
            {"id": int(row["id"]), "status_before": row["status"]}
            for row in conn.execute(text(_OFFBOARD_LIVE_INSTALLATIONS_SQL), params).mappings().all()
        ]
        installation_ids = _returned_ids(conn.execute(text(_OFFBOARD_RETIRE_INSTALLATIONS_SQL), params))
        # A heartbeat may have moved one provisioning -> active between the two statements; the ids are what count.
        installations_retired = [row for row in installations_before if row["id"] in installation_ids]

        code_ids = _returned_ids(conn.execute(text(_OFFBOARD_REVOKE_CODES_SQL), params))

        # ingest_key_ids is under row level security for the runtime role: an UPDATE only sees rows of the tenant named
        # by these two transaction-local settings (see migration 0acba192bf69). Without them it would match nothing and
        # report nothing, so the context is set per branch before each UPDATE.
        key_ids: list[int] = []
        if customer_id is not None:
            branches = conn.execute(text(_OFFBOARD_MAPPED_BRANCHES_SQL), params).fetchall()
            for (branch_id,) in branches:
                if postgresql:
                    conn.execute(
                        text("SELECT set_config('app.operational_customer_id', :v, true)"),
                        {"v": str(customer_id)},
                    )
                    conn.execute(
                        text("SELECT set_config('app.operational_branch_id', :v, true)"),
                        {"v": str(branch_id)},
                    )
                key_ids += _returned_ids(
                    conn.execute(
                        text(_OFFBOARD_RETIRE_INGEST_KEYS_SQL),
                        {"customer_id": customer_id, "branch_id": branch_id},
                    )
                )

        session_ids = _returned_ids(conn.execute(text(_OFFBOARD_REVOKE_SESSIONS_SQL), params))

        counts = {
            "agent_tokens_deactivated": len(token_ids),
            "installations_retired": len(installation_ids),
            "enrollment_codes_revoked": len(code_ids),
            "ingest_keys_retired": len(key_ids),
            "sessions_revoked": len(session_ids),
        }
        changed = status_before != "cancelled" or any(counts.values())

        # Ids and counts only -- never a token, a hash, a code or a hostname. Written on every call, so a repeat run is
        # itself on the record, and told apart from the first by `repeat_sweep` / `changed`: the first cutoff has
        # status_before active|trial|suspended and changed true; a repeat has status_before 'cancelled', and changed
        # false with every count zero unless something had become live again in between.
        record_tenant_lifecycle_event(
            conn,
            event_type=EVENT_ACCESS_CUTOFF,
            organization_id=organization_id,
            organization_slug=organization["slug"],
            operational_customer_id=customer_id,
            actor_user_id=actor_user_id,
            actor_label=actor_label,
            details={
                "status_before": status_before,
                "status_after": "cancelled",
                "repeat_sweep": status_before == "cancelled",
                "changed": changed,
                "counts": counts,
                "agent_token_ids": token_ids,
                "installations": installations_retired,
                "enrollment_code_ids": code_ids,
                "ingest_key_row_ids": sorted(key_ids),
                "session_ids": session_ids,
            },
        )

    return {
        "organization_id": organization_id,
        "status": "cancelled",
        "changed": changed,
        "status_before": status_before,
        "counts": counts,
    }
