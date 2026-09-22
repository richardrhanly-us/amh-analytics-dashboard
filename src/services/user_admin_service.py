from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from database import get_engine
from services import access_service, auth_service
from services.privacy_hardening import log_safe_exception

logger = logging.getLogger("sortview.user_admin")

ALLOWED_MEMBERSHIP_ROLES = ["owner", "admin", "manager", "viewer"]

# Shown by the admin Users page; fixed text, never an exception's own message.
USER_CREATE_FAILED_MESSAGE = (
    "The user could not be created. Please check the details and try again. "
    "If this keeps happening, contact SortView support."
)


def _get_org_row(org_slug: str) -> dict[str, Any] | None:
    sql = text("""
        SELECT id, slug, name
        FROM organizations
        WHERE slug = :org_slug
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"org_slug": org_slug}).mappings().first()
        return dict(row) if row else None


def list_org_users(org_slug: str) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            u.id AS user_id,
            u.email,
            u.full_name,
            u.is_active,
            m.role,
            u.last_login_at,
            u.last_password_changed_at,
            u.created_at
        FROM memberships m
        JOIN organizations o
          ON o.id = m.organization_id
        JOIN app_users u
          ON u.id = m.user_id
        WHERE o.slug = :org_slug
        ORDER BY lower(u.email)
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"org_slug": org_slug}).mappings().all()
        return [dict(row) for row in rows]


def create_or_add_org_user(
    org_slug: str,
    email: str,
    password: str,
    full_name: str,
    role: str,
) -> dict[str, Any]:
    # Service-level enforcement, independent of the calling page's own
    # gate: a suspended or cancelled organization can never gain a new
    # user or membership here, even if this function is ever reached
    # some other way.
    if access_service.get_org_access_mode(org_slug) != "full":
        return {
            "ok": False,
            "message": "This organization does not currently allow administrative changes.",
        }

    org = _get_org_row(org_slug)
    if not org:
        return {
            "ok": False,
            "message": "Organization not found.",
        }

    normalized_email = email.strip().lower()
    existing_user = auth_service.get_user_by_email(normalized_email)

    engine = get_engine()

    if existing_user:
        membership_check_sql = text("""
            SELECT 1
            FROM memberships m
            WHERE m.organization_id = :organization_id
              AND m.user_id = :user_id
            LIMIT 1
        """)

        with engine.connect() as conn:
            exists = conn.execute(
                membership_check_sql,
                {
                    "organization_id": org["id"],
                    "user_id": existing_user["id"],
                },
            ).first()

        if exists:
            return {
                "ok": False,
                "message": "That user already belongs to this organization.",
            }

        insert_membership_sql = text("""
            INSERT INTO memberships (organization_id, user_id, role)
            VALUES (:organization_id, :user_id, :role)
        """)

        with engine.begin() as conn:
            conn.execute(
                insert_membership_sql,
                {
                    "organization_id": org["id"],
                    "user_id": existing_user["id"],
                    "role": role,
                },
            )

        auth_service.log_auth_event(
            event_type="membership_added",
            is_success=True,
            user_id=existing_user["id"],
            email=existing_user["email"],
            message="Existing user added to organization.",
            metadata={
                "org_slug": org_slug,
                "role": role,
            },
        )

        return {
            "ok": True,
            "message": "Existing user was added to this organization.",
        }

    try:
        created_user = auth_service.create_user(
            email=normalized_email,
            password=password,
            full_name=full_name,
        )
    except auth_service.UserAlreadyExistsError:
        return {
            "ok": False,
            "message": auth_service.UserAlreadyExistsError.MESSAGE,
        }
    except ValueError as exc:
        log_safe_exception(logger, "User creation refused", exc)
        return {
            "ok": False,
            "message": USER_CREATE_FAILED_MESSAGE,
        }

    insert_membership_sql = text("""
        INSERT INTO memberships (organization_id, user_id, role)
        VALUES (:organization_id, :user_id, :role)
    """)

    with engine.begin() as conn:
        conn.execute(
            insert_membership_sql,
            {
                "organization_id": org["id"],
                "user_id": created_user["id"],
                "role": role,
            },
        )

    auth_service.log_auth_event(
        event_type="membership_added",
        is_success=True,
        user_id=created_user["id"],
        email=created_user["email"],
        message="New user created and added to organization.",
        metadata={
            "org_slug": org_slug,
            "role": role,
        },
    )

    return {
        "ok": True,
        "message": "User created and added to this organization.",
    }


def update_org_user_role(org_slug: str, user_id: int, role: str) -> dict[str, Any]:
    # Service-level enforcement, independent of the calling page's own
    # gate: a suspended or cancelled organization's roles can never be
    # changed here, even if this function is ever reached some other way.
    if access_service.get_org_access_mode(org_slug) != "full":
        return {
            "ok": False,
            "message": "This organization does not currently allow administrative changes.",
        }

    sql = text("""
        UPDATE memberships m
        SET role = :role
        FROM organizations o
        WHERE m.organization_id = o.id
          AND o.slug = :org_slug
          AND m.user_id = :user_id
    """)

    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            sql,
            {
                "org_slug": org_slug,
                "user_id": user_id,
                "role": role,
            },
        )

    if result.rowcount == 0:
        return {
            "ok": False,
            "message": "Membership not found.",
        }

    user = auth_service.get_user_by_id(user_id)
    auth_service.log_auth_event(
        event_type="membership_role_updated",
        is_success=True,
        user_id=user_id,
        email=user["email"] if user else None,
        message="Organization role updated.",
        metadata={
            "org_slug": org_slug,
            "role": role,
        },
    )

    return {
        "ok": True,
        "message": "User role updated.",
    }


def set_user_active(org_slug: str, user_id: int, is_active: bool) -> dict[str, Any]:
    """Activates/deactivates a user's account from an organization's admin page.

    org_slug scopes WHO may be targeted from this admin surface: the
    calling organization must currently have full access, and the target
    user must actually be a member of that organization (added here so a
    suspended/cancelled org can no longer reach this action, and so this
    admin surface can no longer target a user outside the org being
    administered). app_users.is_active itself remains a GLOBAL account
    flag, exactly as before this change -- deactivating a user here
    deactivates their account everywhere, not just in this organization.
    Whether account access should instead be scoped per-organization
    (rather than one global flag) is a separate, not-yet-made product/
    security decision; this function does not redesign that.
    """
    # Service-level enforcement, independent of the calling page's own
    # gate: a suspended or cancelled organization can never activate or
    # deactivate a user here, even if this function is ever reached some
    # other way.
    if access_service.get_org_access_mode(org_slug) != "full":
        return {
            "ok": False,
            "message": "This organization does not currently allow administrative changes.",
        }

    membership_check_sql = text("""
        SELECT 1
        FROM memberships m
        JOIN organizations o
          ON o.id = m.organization_id
        WHERE o.slug = :org_slug
          AND m.user_id = :user_id
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        is_member = conn.execute(
            membership_check_sql,
            {"org_slug": org_slug, "user_id": user_id},
        ).first()

    if is_member is None:
        return {
            "ok": False,
            "message": "That user is not a member of this organization.",
        }

    sql = text("""
        UPDATE app_users
        SET is_active = :is_active
        WHERE id = :user_id
    """)

    with engine.begin() as conn:
        result = conn.execute(
            sql,
            {
                "user_id": user_id,
                "is_active": is_active,
            },
        )

    if result.rowcount == 0:
        return {
            "ok": False,
            "message": "User not found.",
        }

    user = auth_service.get_user_by_id(user_id)
    auth_service.log_auth_event(
        event_type="user_status_updated",
        is_success=True,
        user_id=user_id,
        email=user["email"] if user else None,
        message="User status updated.",
        metadata={
            "org_slug": org_slug,
            "is_active": is_active,
        },
    )

    return {
        "ok": True,
        "message": "User status updated.",
    }


def list_recent_org_auth_events(org_slug: str, limit: int = 25) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            aal.id,
            aal.created_at,
            aal.email,
            aal.event_type,
            aal.is_success,
            aal.message,
            aal.metadata
        FROM auth_audit_log aal
        JOIN memberships m
          ON m.user_id = aal.user_id
        JOIN organizations o
          ON o.id = m.organization_id
        WHERE o.slug = :org_slug
        ORDER BY aal.created_at DESC
        LIMIT :limit
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {
                "org_slug": org_slug,
                "limit": limit,
            },
        ).mappings().all()
        return [dict(row) for row in rows]
