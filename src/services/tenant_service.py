from __future__ import annotations

from typing import Any

from sqlalchemy import text

from database import get_engine


def create_organization_with_primary_branch(
    org_name: str,
    org_slug: str,
    branch_name: str,
    branch_slug: str,
    plan_code: str,
    org_settings: dict[str, Any] | None = None,
    branch_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    org_settings = org_settings or {}
    branch_settings = branch_settings or {}

    sql_insert_org = text("""
        INSERT INTO organizations (name, slug, status)
        VALUES (:name, :slug, 'active')
        RETURNING id, name, slug, status, created_at
    """)

    sql_insert_branch = text("""
        INSERT INTO branches (organization_id, name, slug, is_primary, status)
        VALUES (:organization_id, :name, :slug, TRUE, 'active')
        RETURNING id, organization_id, name, slug, is_primary, status, created_at
    """)

    sql_find_plan = text("""
        SELECT id, code, name
        FROM plans
        WHERE code = :plan_code
          AND is_active = TRUE
        LIMIT 1
    """)

    sql_insert_subscription = text("""
        INSERT INTO subscriptions (organization_id, plan_id, status)
        VALUES (:organization_id, :plan_id, 'trial')
        RETURNING id, organization_id, plan_id, status, started_at
    """)

    sql_insert_org_settings = text("""
        INSERT INTO organization_settings (organization_id, settings_json)
        VALUES (:organization_id, CAST(:settings_json AS JSONB))
        RETURNING id
    """)

    sql_insert_branch_settings = text("""
        INSERT INTO branch_settings (branch_id, settings_json)
        VALUES (:branch_id, CAST(:settings_json AS JSONB))
        RETURNING id
    """)

    import json

    engine = get_engine()
    with engine.begin() as conn:
        plan = conn.execute(sql_find_plan, {"plan_code": plan_code}).mappings().first()
        if not plan:
            raise RuntimeError(f"Plan not found: {plan_code}")

        org = conn.execute(
            sql_insert_org,
            {"name": org_name, "slug": org_slug},
        ).mappings().first()

        branch = conn.execute(
            sql_insert_branch,
            {
                "organization_id": org["id"],
                "name": branch_name,
                "slug": branch_slug,
            },
        ).mappings().first()

        subscription = conn.execute(
            sql_insert_subscription,
            {
                "organization_id": org["id"],
                "plan_id": plan["id"],
            },
        ).mappings().first()

        conn.execute(
            sql_insert_org_settings,
            {
                "organization_id": org["id"],
                "settings_json": json.dumps(org_settings),
            },
        )

        conn.execute(
            sql_insert_branch_settings,
            {
                "branch_id": branch["id"],
                "settings_json": json.dumps(branch_settings),
            },
        )

    return {
        "organization": dict(org),
        "branch": dict(branch),
        "plan": dict(plan),
        "subscription": dict(subscription),
    }


def get_organization_by_slug(org_slug: str) -> dict[str, Any] | None:
    sql = text("""
        SELECT id, name, slug, status, created_at, updated_at
        FROM organizations
        WHERE slug = :org_slug
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"org_slug": org_slug}).mappings().first()
        return dict(row) if row else None


def get_branch_by_slug(org_slug: str, branch_slug: str) -> dict[str, Any] | None:
    sql = text("""
        SELECT
            b.id,
            b.organization_id,
            b.name,
            b.slug,
            b.is_primary,
            b.status
        FROM branches b
        JOIN organizations o
          ON o.id = b.organization_id
        WHERE o.slug = :org_slug
          AND b.slug = :branch_slug
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            sql,
            {"org_slug": org_slug, "branch_slug": branch_slug},
        ).mappings().first()
        return dict(row) if row else None


# --- collector installations -------------------------------------------------
#
# collector_installations is server-side bookkeeping only: one row per
# deployed Collector instance/machine. It holds no credentials, and it is not
# a health signal -- pipeline_status remains the source of branch/ingestion
# health. last_seen_at is not populated by any of these functions.

COLLECTOR_INSTALLATION_STATUSES = ("provisioning", "active", "inactive", "retired")


def _clean_optional(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _validate_installation_fields(name: str, status: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Installation name is required")
    if status not in COLLECTOR_INSTALLATION_STATUSES:
        raise ValueError(
            f"Invalid installation status: {status!r} "
            f"(expected one of {', '.join(COLLECTOR_INSTALLATION_STATUSES)})"
        )
    return name


def create_collector_installation(
    organization_id: int,
    branch_id: int,
    name: str,
    hostname: str | None = None,
    collector_version: str | None = None,
    status: str = "provisioning",
) -> dict[str, Any]:
    name = _validate_installation_fields(name, status)

    sql_find_branch = text("""
        SELECT id
        FROM branches
        WHERE id = :branch_id
          AND organization_id = :organization_id
        LIMIT 1
    """)

    sql_insert = text("""
        INSERT INTO collector_installations
            (organization_id, branch_id, name, hostname, collector_version, status)
        VALUES
            (:organization_id, :branch_id, :name, :hostname, :collector_version, :status)
        RETURNING
            id, organization_id, branch_id, name, hostname, collector_version,
            status, installed_at, last_seen_at, created_at, updated_at
    """)

    engine = get_engine()
    with engine.begin() as conn:
        branch = conn.execute(
            sql_find_branch,
            {"branch_id": branch_id, "organization_id": organization_id},
        ).mappings().first()
        if not branch:
            raise RuntimeError(
                f"Branch {branch_id} not found for organization {organization_id}"
            )

        row = conn.execute(
            sql_insert,
            {
                "organization_id": organization_id,
                "branch_id": branch_id,
                "name": name,
                "hostname": _clean_optional(hostname),
                "collector_version": _clean_optional(collector_version),
                "status": status,
            },
        ).mappings().first()

    return dict(row) if row else {}


def list_collector_installations_for_branch(branch_id: int) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            ci.id,
            ci.organization_id,
            ci.branch_id,
            b.name AS branch_name,
            b.slug AS branch_slug,
            ci.name,
            ci.hostname,
            ci.collector_version,
            ci.status,
            ci.installed_at,
            ci.last_seen_at,
            ci.created_at,
            ci.updated_at
        FROM collector_installations ci
        JOIN branches b
          ON b.id = ci.branch_id
        WHERE ci.branch_id = :branch_id
        ORDER BY ci.id
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"branch_id": branch_id}).mappings().all()
        return [dict(row) for row in rows]


def list_collector_installations_for_organization(organization_id: int) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            ci.id,
            ci.organization_id,
            ci.branch_id,
            b.name AS branch_name,
            b.slug AS branch_slug,
            ci.name,
            ci.hostname,
            ci.collector_version,
            ci.status,
            ci.installed_at,
            ci.last_seen_at,
            ci.created_at,
            ci.updated_at
        FROM collector_installations ci
        JOIN branches b
          ON b.id = ci.branch_id
        WHERE ci.organization_id = :organization_id
        ORDER BY b.is_primary DESC, b.id, ci.id
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"organization_id": organization_id}).mappings().all()
        return [dict(row) for row in rows]


def update_collector_installation(
    installation_id: int,
    organization_id: int,
    name: str,
    hostname: str | None,
    collector_version: str | None,
    status: str,
) -> dict[str, Any]:
    """Set an installation's name/hostname/collector_version/status.

    All four fields are written; an empty hostname or collector_version is
    stored as NULL. organization_id scopes the update so one tenant's
    installation can never be edited through another tenant's context.
    installed_at is stamped once, the first time an installation becomes
    'active'; last_seen_at is never touched here.
    """
    name = _validate_installation_fields(name, status)

    sql = text("""
        UPDATE collector_installations
        SET name = :name,
            hostname = :hostname,
            collector_version = :collector_version,
            status = :status,
            installed_at = CASE
                WHEN :status = 'active' AND installed_at IS NULL THEN NOW()
                ELSE installed_at
            END,
            updated_at = NOW()
        WHERE id = :installation_id
          AND organization_id = :organization_id
        RETURNING
            id, organization_id, branch_id, name, hostname, collector_version,
            status, installed_at, last_seen_at, created_at, updated_at
    """)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sql,
            {
                "installation_id": installation_id,
                "organization_id": organization_id,
                "name": name,
                "hostname": _clean_optional(hostname),
                "collector_version": _clean_optional(collector_version),
                "status": status,
            },
        ).mappings().first()

    if not row:
        raise RuntimeError(
            f"Collector installation {installation_id} not found for organization {organization_id}"
        )
    return dict(row)


def _deep_merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_settings(result[key], value)
        else:
            result[key] = value

    return result


def get_effective_settings(org_slug: str, branch_slug: str | None = None) -> dict[str, Any]:
    sql_org = text("""
        SELECT
            o.id,
            o.name,
            o.slug,
            os.settings_json AS org_settings
        FROM organizations o
        LEFT JOIN organization_settings os
          ON os.organization_id = o.id
        WHERE o.slug = :org_slug
        LIMIT 1
    """)

    sql_branch = text("""
        SELECT
            b.id,
            b.name,
            b.slug,
            b.is_primary,
            bs.settings_json AS branch_settings
        FROM branches b
        LEFT JOIN branch_settings bs
          ON bs.branch_id = b.id
        WHERE b.organization_id = :organization_id
          AND (
                (:branch_slug IS NOT NULL AND b.slug = :branch_slug)
                OR (:branch_slug IS NULL AND b.is_primary = TRUE)
              )
        ORDER BY b.is_primary DESC, b.id ASC
        LIMIT 1
    """)

    sql_subscription = text("""
        SELECT
            s.id,
            s.status,
            p.code AS plan_code,
            p.name AS plan_name
        FROM subscriptions s
        JOIN plans p
          ON p.id = s.plan_id
        WHERE s.organization_id = :organization_id
        ORDER BY s.created_at DESC
        LIMIT 1
    """)

    sql_entitlements = text("""
        SELECT feature_key, enabled, limit_value
        FROM feature_entitlements
        WHERE plan_id = (
            SELECT s.plan_id
            FROM subscriptions s
            WHERE s.organization_id = :organization_id
            ORDER BY s.created_at DESC
            LIMIT 1
        )
    """)

    engine = get_engine()
    with engine.connect() as conn:
        org = conn.execute(sql_org, {"org_slug": org_slug}).mappings().first()
        if not org:
            raise RuntimeError(f"Organization not found: {org_slug}")

        branch = conn.execute(
            sql_branch,
            {
                "organization_id": org["id"],
                "branch_slug": branch_slug,
            },
        ).mappings().first()

        if not branch:
            raise RuntimeError(f"Branch not found for organization: {org_slug}")

        subscription = conn.execute(
            sql_subscription,
            {"organization_id": org["id"]},
        ).mappings().first()

        entitlement_rows = conn.execute(
            sql_entitlements,
            {"organization_id": org["id"]},
        ).mappings().all()

    org_settings = dict(org["org_settings"] or {})
    branch_settings = dict(branch["branch_settings"] or {})

    effective_settings = _deep_merge_settings(org_settings, branch_settings)

    entitlements = {
        row["feature_key"]: {
            "enabled": bool(row["enabled"]),
            "limit_value": row["limit_value"],
        }
        for row in entitlement_rows
    }

    return {
        "organization": {
            "id": org["id"],
            "name": org["name"],
            "slug": org["slug"],
        },
        "branch": {
            "id": branch["id"],
            "name": branch["name"],
            "slug": branch["slug"],
            "is_primary": branch["is_primary"],
        },
        "subscription": dict(subscription) if subscription else None,
        "settings": effective_settings,
        "entitlements": entitlements,
    }
