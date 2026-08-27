from __future__ import annotations

ADMIN_ROLES = {"owner", "admin"}
STAFF_ROLES = {"owner", "admin", "manager"}
VIEW_ROLES = {"owner", "admin", "manager", "viewer"}


def get_role(entitlement_context: dict) -> str | None:
    return entitlement_context.get("role")


def has_role(entitlement_context: dict, allowed_roles: set[str]) -> bool:
    role = get_role(entitlement_context)
    return role in allowed_roles if role else False


def feature_enabled(entitlement_context: dict, feature_key: str) -> bool:
    feature = entitlement_context.get("entitlements", {}).get(feature_key)
    if not feature:
        return False
    return bool(feature.get("enabled", False))


def feature_limit(entitlement_context: dict, feature_key: str):
    feature = entitlement_context.get("entitlements", {}).get(feature_key)
    if not feature:
        return None
    return feature.get("limit_value")


def can_view_app(entitlement_context: dict) -> bool:
    return has_role(entitlement_context, VIEW_ROLES)


def can_manage_settings(entitlement_context: dict) -> bool:
    return has_role(entitlement_context, ADMIN_ROLES)


def can_manage_users(entitlement_context: dict) -> bool:
    return has_role(entitlement_context, ADMIN_ROLES)


def can_export(entitlement_context: dict) -> bool:
    return feature_enabled(entitlement_context, "exports")


def can_use_alerts(entitlement_context: dict) -> bool:
    return feature_enabled(entitlement_context, "alerts")


def can_view_advanced_reports(entitlement_context: dict) -> bool:
    return feature_enabled(entitlement_context, "advanced_reports")


def can_access_branch_count(entitlement_context: dict, branch_count: int) -> bool:
    limit_value = feature_limit(entitlement_context, "max_branches")
    if limit_value is None:
        return True
    return branch_count <= limit_value

def can_view_transits(entitlement_context: dict) -> bool:
    return feature_enabled(entitlement_context, "transits")


def can_view_internal_workflow(entitlement_context: dict) -> bool:
    return feature_enabled(entitlement_context, "internal_workflow")
def get_history_days_limit(entitlement_context: dict):
    return feature_limit(entitlement_context, "history_days")
