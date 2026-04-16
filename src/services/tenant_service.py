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

    effective_settings = {
        **org_settings,
        **branch_settings,
    }

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
