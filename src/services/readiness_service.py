from __future__ import annotations

from sqlalchemy import text

from database import get_engine


def get_branch_readiness(org_slug: str, branch_slug: str) -> dict:
    engine = get_engine()

    with engine.connect() as conn:
        tenant_row = conn.execute(
            text("""
                SELECT
                    o.id AS organization_id,
                    o.slug AS organization_slug,
                    o.name AS organization_name,
                    o.operational_customer_id AS customer_id,
                    b.id AS app_branch_id,
                    b.slug AS branch_slug,
                    b.name AS branch_name,
                    b.status AS branch_status,
                    b.operational_branch_id AS branch_id
                FROM organizations o
                LEFT JOIN branches b
                  ON b.organization_id = o.id
                 AND b.slug = :branch_slug
                WHERE o.slug = :org_slug
                LIMIT 1
            """),
            {
                "org_slug": org_slug,
                "branch_slug": branch_slug,
            },
        ).mappings().first()

        if tenant_row is None:
            return {
                "is_ready": False,
                "code": "org_not_found",
                "message": "This organization could not be found.",
            }

        if tenant_row["branch_slug"] is None:
            return {
                "is_ready": False,
                "code": "branch_not_found",
                "message": "This branch could not be found for the selected organization.",
            }

        if tenant_row["branch_status"] != "active":
            return {
                "is_ready": False,
                "code": "branch_inactive",
                "message": "This branch is not active yet.",
            }

        customer_id = tenant_row["customer_id"]
        branch_id = tenant_row["branch_id"]

        if customer_id is None or branch_id is None:
            return {
                "is_ready": False,
                "code": "missing_operational_mapping",
                "message": "This library is missing its operational tenant mapping.",
            }

        pipeline_row = conn.execute(
            text("""
                SELECT
                    status,
                    last_run,
                    updated_at,
                    checkins_rows,
                    checkins_history_rows,
                    uploaded_checkins_rows
                FROM pipeline_status
                WHERE customer_id = :customer_id
                  AND branch_id = :branch_id
                ORDER BY updated_at DESC
                LIMIT 1
            """),
            {
                "customer_id": customer_id,
                "branch_id": branch_id,
            },
        ).mappings().first()

        if pipeline_row is None:
            return {
                "is_ready": False,
                "code": "waiting_for_pipeline",
                "message": "Waiting for SortView Agent to send the first pipeline run for this branch.",
            }

        data_row = conn.execute(
            text("""
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM checkins_routed
                        WHERE customer_id = :customer_id
                          AND branch_id = :branch_id
                        LIMIT 1
                    ) AS has_routed,
                    EXISTS (
                        SELECT 1
                        FROM checkins_clean
                        WHERE customer_id = :customer_id
                          AND branch_id = :branch_id
                        LIMIT 1
                    ) AS has_clean
            """),
            {
                "customer_id": customer_id,
                "branch_id": branch_id,
            },
        ).mappings().first()

        has_data = bool(data_row["has_routed"]) or bool(data_row["has_clean"])

        if not has_data:
            return {
                "is_ready": False,
                "code": "waiting_for_history",
                "message": "The pipeline is connected, but this branch does not have historical checkin data yet.",
            }

        return {
            "is_ready": True,
            "code": "ready",
            "message": "Ready",
            "customer_id": customer_id,
            "branch_id": branch_id,
        }
