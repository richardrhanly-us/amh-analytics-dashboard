#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         entitlement_service.py
#
#  Description: Provides subscription and entitlement lookup helpers
#               for the SortView dashboard. This file retrieves a
#               user's organization role, loads the organization's
#               current subscription, loads plan-level feature
#               entitlements, and builds a combined entitlement context
#               used by permission checks.
#
#***************************************************************

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

from database import get_engine

logger = logging.getLogger("sortview.entitlements")

# Every function in this module is UNCACHED and framework-neutral: each
# lookup queries the database, and nothing here imports Streamlit. The
# dashboard's cached copies of get_org_subscription/get_plan_entitlements
# (and the build_entitlement_context that uses them) live in
# services.streamlit_entitlement_adapter; any non-Streamlit caller (e.g. an
# API route) must use this module directly, never that adapter.
#
# Role is never cached anywhere: it is the input to every permission
# check (has_role/can_manage_settings/etc.), so every entitlement context
# -- built here or through the adapter -- calls get_org_role_for_user
# fresh, and a role change takes effect on the very next call. (Account
# deactivation is a separate mechanism, enforced by
# auth_service.enforce_active_session -- it does not change a user's
# role.)

#***************************************************************
#
#  Function:     get_org_role_for_user
#
#  Description: Retrieves the role assigned to a user within a
#               specific organization.
#
#  Parameters:  user_id - Internal user ID.
#               org_slug - Organization slug being checked.
#
#  Returns:     str | None - User role if a membership exists;
#                            otherwise None.
#
#***************************************************************

def get_org_role_for_user(user_id: int, org_slug: str) -> str | None:
    # Build the query used to find the user's role for the organization. A membership that was removed
    # (memberships.removed_at) gives no role at all: the role left on its row is history.
    sql = text("""
        SELECT m.role
        FROM memberships m
        JOIN organizations o
          ON o.id = m.organization_id
        WHERE m.user_id = :user_id
          AND m.removed_at IS NULL
          AND o.slug = :org_slug
        LIMIT 1
    """)

    # Execute the lookup and return the role value when found.
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            sql,
            {"user_id": user_id, "org_slug": org_slug},
        ).first()
        return row[0] if row else None


#***************************************************************
#
#  Function:     get_org_subscription
#
#  Description: Retrieves the most recent subscription record for a
#               specific organization, including the related plan
#               information.
#
#  Parameters:  org_slug - Organization slug being checked.
#
#  Returns:     dict[str, Any] | None - Subscription and plan details
#                                      if found; otherwise None.
#
#***************************************************************

def get_org_subscription(org_slug: str) -> dict[str, Any] | None:
    # Build the query used to load the organization's latest subscription.
    sql = text("""
        SELECT
            s.id,
            s.status,
            s.started_at,
            s.ends_at,
            p.id AS plan_id,
            p.code AS plan_code,
            p.name AS plan_name
        FROM subscriptions s
        JOIN organizations o
          ON o.id = s.organization_id
        JOIN plans p
          ON p.id = s.plan_id
        WHERE o.slug = :org_slug
        ORDER BY s.created_at DESC
        LIMIT 1
    """)

    # Execute the query and return the subscription record as a dictionary.
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"org_slug": org_slug}).mappings().first()
        return dict(row) if row else None


#***************************************************************
#
#  Function:     get_plan_entitlements
#
#  Description: Loads all feature entitlements attached to a specific
#               subscription plan. Each feature includes whether it is
#               enabled and any configured limit value.
#
#  Parameters:  plan_id - Internal plan ID.
#
#  Returns:     dict[str, dict[str, Any]] - Dictionary keyed by feature
#                                           key with enabled and limit
#                                           details for each feature.
#
#***************************************************************

def get_plan_entitlements(plan_id: int) -> dict[str, dict[str, Any]]:
    # Build the query used to load feature entitlements for the plan.
    sql = text("""
        SELECT feature_key, enabled, limit_value
        FROM feature_entitlements
        WHERE plan_id = :plan_id
        ORDER BY feature_key
    """)

    # Execute the query and retrieve all entitlement rows.
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"plan_id": plan_id}).mappings().all()

    # Convert the entitlement rows into a feature-keyed dictionary.
    return {
        row["feature_key"]: {
            "enabled": bool(row["enabled"]),
            "limit_value": row["limit_value"],
        }
        for row in rows
    }


#***************************************************************
#
#  Function:     build_entitlement_context
#
#  Description: Builds the combined entitlement context for a user and
#               organization. The context includes the user's role,
#               the organization's current subscription, and any
#               feature entitlements connected to the subscription plan.
#
#  Parameters:  user_id - Internal user ID.
#               org_slug - Organization slug.
#
#  Returns:     dict[str, Any] - Entitlement context used by permission
#                                checks and dashboard feature gates.
#
#***************************************************************

def build_entitlement_context(user_id: int, org_slug: str) -> dict[str, Any]:
    return build_entitlement_context_with(
        user_id,
        org_slug,
        load_subscription=get_org_subscription,
        load_plan_entitlements=get_plan_entitlements,
    )


#***************************************************************
#
#  Function:     build_entitlement_context_with
#
#  Description: The single place an entitlement context is assembled.
#               The subscription and plan-entitlement lookups are
#               supplied by the caller, so the Streamlit adapter can
#               pass its cached copies while build_entitlement_context
#               passes this module's uncached ones. The role lookup is
#               deliberately NOT supplied by the caller: it is always
#               this module's uncached get_org_role_for_user.
#
#  Parameters:  user_id - Internal user ID.
#               org_slug - Organization slug.
#               load_subscription - Called as load_subscription(org_slug=...).
#               load_plan_entitlements - Called as load_plan_entitlements(plan_id).
#
#  Returns:     dict[str, Any] - Entitlement context used by permission
#                                checks and dashboard feature gates.
#
#***************************************************************

def build_entitlement_context_with(
    user_id: int,
    org_slug: str,
    *,
    load_subscription: Callable[..., dict[str, Any] | None],
    load_plan_entitlements: Callable[..., dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    # Load the user's role and the organization's subscription.
    role = get_org_role_for_user(user_id=user_id, org_slug=org_slug)
    subscription = load_subscription(org_slug=org_slug)

    # Load plan entitlements only when a subscription and plan ID exist.
    entitlements = {}
    if subscription and subscription.get("plan_id"):
        entitlements = load_plan_entitlements(subscription["plan_id"])

    return {
        "role": role,
        "subscription": subscription,
        "entitlements": entitlements,
    }


#***************************************************************
#
#  Function:     feature_enabled
#
#  Description: Checks whether a feature is enabled in the supplied
#               entitlement context.
#
#  Parameters:  entitlement_context - Combined entitlement context.
#               feature_key - Feature key being checked.
#
#  Returns:     bool - True if the feature is enabled; otherwise False.
#
#***************************************************************

def feature_enabled(entitlement_context: dict[str, Any], feature_key: str) -> bool:
    # Look up the feature entry from the context's entitlement dictionary.
    feature = entitlement_context.get("entitlements", {}).get(feature_key)
    if not feature:
        return False
    return bool(feature.get("enabled", False))


#***************************************************************
#
#  Function:     feature_limit
#
#  Description: Retrieves the configured limit value for a feature in
#               the supplied entitlement context.
#
#  Parameters:  entitlement_context - Combined entitlement context.
#               feature_key - Feature key being checked.
#
#  Returns:     Any | None - Feature limit value if present; otherwise
#                            None.
#
#***************************************************************

def feature_limit(entitlement_context: dict[str, Any], feature_key: str):
    # Look up the feature entry from the context's entitlement dictionary.
    feature = entitlement_context.get("entitlements", {}).get(feature_key)
    if not feature:
        return None
    return feature.get("limit_value")



#***************************************************************
#
#  Capabilities: what a plan's features mean
#
#  Description: The one reading of the plan features that decide what a
#               customer may see, by feature key and never by plan name.
#               Each one FAILS CLOSED: a feature that is missing, switched
#               off or holds a value that is not a positive whole number
#               gives the least the product offers, never the most.
#
#               transits      enabled: transit routing is shown
#               history_days  enabled, N: a report may start no earlier
#                             than N - 1 days before today
#                             enabled, no limit: no earliest date
#                             otherwise: DEFAULT_HISTORY_DAYS
#               max_sorters   enabled, N: at most N sorter sites
#                             enabled, no limit: no maximum
#                             otherwise: DEFAULT_MAX_SORTERS
#
#***************************************************************

TRANSITS_FEATURE = "transits"
HISTORY_DAYS_FEATURE = "history_days"
MAX_SORTERS_FEATURE = "max_sorters"

DEFAULT_HISTORY_DAYS = 30
DEFAULT_MAX_SORTERS = 1

# A sorter site counts while one of its installations can report. An inactive or retired one is history only.
SORTER_INSTALLATION_STATUSES = frozenset({"provisioning", "active"})


def _limit_or_unlimited(entitlement_context: dict[str, Any], feature_key: str, default: int) -> int | None:
    # The feature's positive limit, None for "no limit", or `default` when the feature does not grant one.
    feature = entitlement_context.get("entitlements", {}).get(feature_key)
    if not feature or not feature.get("enabled", False):
        return default
    limit = feature.get("limit_value")
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        # The key only: a stored value is not repeated in a log.
        logger.warning("Unusable plan feature limit; the default applies | feature=%s", feature_key)
        return default
    return limit


def transits_enabled(entitlement_context: dict[str, Any]) -> bool:
    return feature_enabled(entitlement_context, TRANSITS_FEATURE)


def history_days_limit(entitlement_context: dict[str, Any]) -> int | None:
    """How many calendar days back, today included, a report may reach. None: no limit."""
    return _limit_or_unlimited(entitlement_context, HISTORY_DAYS_FEATURE, DEFAULT_HISTORY_DAYS)


def earliest_report_date(entitlement_context: dict[str, Any], today: date) -> date | None:
    """The first date a report may start on, given the product's `today`. None: no earliest date."""
    days = history_days_limit(entitlement_context)
    return None if days is None else today - timedelta(days=days - 1)


def max_sorters_limit(entitlement_context: dict[str, Any]) -> int | None:
    """How many sorter sites the organization may have. None: no limit."""
    return _limit_or_unlimited(entitlement_context, MAX_SORTERS_FEATURE, DEFAULT_MAX_SORTERS)


def count_sorter_sites(installations: Iterable[Mapping[str, Any]]) -> int:
    """The sorter sites among an organization's collector installations (each with `branch_id` and `status`):
    the distinct host branches with at least one installation that can report. Two installations at one branch are
    one site. A routing destination is not an installation and is never counted."""
    return len({row["branch_id"] for row in installations if row["status"] in SORTER_INSTALLATION_STATUSES})
