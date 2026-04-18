from __future__ import annotations

from sqlalchemy import text

from database import get_engine


DEFAULT_MESSAGES = {
    "pending": "This branch is still being onboarded.",
    "mapping": "This branch is waiting for operational tenant mapping.",
    "settings": "This branch is waiting for runtime settings to be completed.",
    "agent": "This branch is waiting for the SortView Agent to be connected.",
    "initial_sync": "This branch is waiting for its first successful data sync.",
    "ready": "Ready",
    "paused": "This branch has been paused.",
}


def get_branch_readiness(org_slug: str, branch_slug: str) -> dict:
    engine = get_engine()

    with engine.connect() as conn:
        row = conn.execute(
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
                    b.operational_branch_id AS branch_id,
                    b.onboarding_status,
                    b.onboarding_message,
                    b.onboarding_updated_at
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

    if row is None:
        return {
            "is_ready": False,
            "code": "org_not_found",
            "message": "This organization could not be found.",
        }

    if row["branch_slug"] is None:
        return {
            "is_ready": False,
            "code": "branch_not_found",
            "message": "This branch could not be found for the selected organization.",
        }

    if row["branch_status"] != "active":
        return {
            "is_ready": False,
            "code": "branch_inactive",
            "message": "This branch is not active.",
        }

    customer_id = row["customer_id"] if row["customer_id"] is not None else row["organization_id"]
    branch_id = row["branch_id"] if row["branch_id"] is not None else row["app_branch_id"]

    onboarding_status = row["onboarding_status"] or "pending"
    onboarding_message = row["onboarding_message"] or DEFAULT_MESSAGES.get(
        onboarding_status,
        "This branch is not ready yet.",
    )

    if onboarding_status != "ready":
        return {
            "is_ready": False,
            "code": onboarding_status,
            "message": onboarding_message,
            "customer_id": customer_id,
            "branch_id": branch_id,
        }

    return {
        "is_ready": True,
        "code": "ready",
        "message": onboarding_message,
        "customer_id": customer_id,
        "branch_id": branch_id,
    }
