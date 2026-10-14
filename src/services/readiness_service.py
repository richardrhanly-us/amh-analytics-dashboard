from __future__ import annotations

import streamlit as st
from sqlalchemy import text

from database import get_engine
from services.branch_readiness import branch_readiness

# get_branch_readiness previously ran on every Streamlit rerun -- every
# auto-refresh tick, every nav click -- to answer a question (is this
# branch onboarded/active/mapped) that only changes on an admin/
# onboarding action. ttl=120 keeps it feeling live for anyone actively
# completing onboarding while eliminating repeat round trips otherwise.
# Cache key is (org_slug, branch_slug), so this is naturally tenant- and
# branch-scoped.
_READINESS_CACHE_TTL_SECONDS = 120

DEFAULT_MESSAGES = {
    "pending": "This branch is still being onboarded.",
    "mapping": "This branch is waiting for operational tenant mapping.",
    "settings": "This branch is waiting for runtime settings to be completed.",
    "agent": "This branch is waiting for the SortView Agent to be connected.",
    "initial_sync": "This branch is waiting for its first successful data sync.",
    "ready": "Ready",
    "paused": "This branch has been paused.",
}

_SETUP_MESSAGES = {
    "org_not_found": "This organization could not be found.",
    "branch_not_found": "This branch could not be found for the selected organization.",
    "branch_inactive": "This branch is not active.",
    "missing_operational_mapping": "Operational tenant mapping is missing for this branch.",
}


@st.cache_data(ttl=_READINESS_CACHE_TTL_SECONDS, show_spinner=False)
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

    readiness = branch_readiness(row)

    if row is None or not readiness.onboarding:
        return {
            "is_ready": False,
            "code": readiness.code,
            "message": _SETUP_MESSAGES[readiness.code],
        }

    onboarding_message = row["onboarding_message"] or DEFAULT_MESSAGES.get(
        readiness.code,
        "This branch is not ready yet.",
    )

    return {
        "is_ready": readiness.is_ready,
        "code": readiness.code,
        "message": onboarding_message,
        "customer_id": row["customer_id"],
        "branch_id": row["branch_id"],
    }
