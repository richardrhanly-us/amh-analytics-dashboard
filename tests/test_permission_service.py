from src.services import permission_service


def ctx(role=None, entitlements=None):
    return {"role": role, "entitlements": entitlements or {}}


def entitlement(enabled, limit_value=None):
    return {"enabled": enabled, "limit_value": limit_value}


# --- get_role / has_role --------------------------------------------------

def test_get_role_returns_none_when_missing():
    assert permission_service.get_role({}) is None


def test_has_role_true_for_allowed_role():
    assert permission_service.has_role(ctx(role="admin"), {"owner", "admin"}) is True


def test_has_role_false_for_disallowed_role():
    assert permission_service.has_role(ctx(role="viewer"), {"owner", "admin"}) is False


def test_has_role_false_when_role_missing():
    assert permission_service.has_role(ctx(role=None), {"owner", "admin"}) is False


# --- role-gated permissions ------------------------------------------------

def test_can_view_app_allows_every_membership_role():
    for role in ("owner", "admin", "manager", "viewer"):
        assert permission_service.can_view_app(ctx(role=role)) is True


def test_can_view_app_denies_role_with_no_membership():
    assert permission_service.can_view_app(ctx(role=None)) is False


def test_can_manage_settings_restricted_to_admin_roles():
    assert permission_service.can_manage_settings(ctx(role="owner")) is True
    assert permission_service.can_manage_settings(ctx(role="admin")) is True
    assert permission_service.can_manage_settings(ctx(role="manager")) is False
    assert permission_service.can_manage_settings(ctx(role="viewer")) is False


def test_can_manage_users_restricted_to_admin_roles():
    assert permission_service.can_manage_users(ctx(role="admin")) is True
    assert permission_service.can_manage_users(ctx(role="viewer")) is False


# --- feature-flag gated permissions -----------------------------------------

def test_feature_enabled_true_when_flag_on():
    context = ctx(entitlements={"exports": entitlement(True)})
    assert permission_service.feature_enabled(context, "exports") is True


def test_feature_enabled_false_when_flag_off():
    context = ctx(entitlements={"exports": entitlement(False)})
    assert permission_service.feature_enabled(context, "exports") is False


def test_feature_enabled_false_when_feature_missing_entirely():
    context = ctx(entitlements={})
    assert permission_service.feature_enabled(context, "exports") is False


def test_can_export_reflects_exports_entitlement():
    assert permission_service.can_export(ctx(entitlements={"exports": entitlement(True)})) is True
    assert permission_service.can_export(ctx(entitlements={"exports": entitlement(False)})) is False
    assert permission_service.can_export(ctx(entitlements={})) is False


def test_can_use_alerts_reflects_alerts_entitlement():
    assert permission_service.can_use_alerts(ctx(entitlements={"alerts": entitlement(True)})) is True
    assert permission_service.can_use_alerts(ctx(entitlements={})) is False


def test_can_view_advanced_reports_reflects_entitlement():
    context = ctx(entitlements={"advanced_reports": entitlement(True)})
    assert permission_service.can_view_advanced_reports(context) is True
    assert permission_service.can_view_advanced_reports(ctx(entitlements={})) is False


def test_can_view_transits_reflects_entitlement():
    context = ctx(entitlements={"transits": entitlement(True)})
    assert permission_service.can_view_transits(context) is True
    assert permission_service.can_view_transits(ctx(entitlements={})) is False


def test_can_view_internal_workflow_reflects_entitlement():
    context = ctx(entitlements={"internal_workflow": entitlement(True)})
    assert permission_service.can_view_internal_workflow(context) is True
    assert permission_service.can_view_internal_workflow(ctx(entitlements={})) is False


# --- limit-based entitlements ------------------------------------------------

def test_feature_limit_returns_configured_value():
    context = ctx(entitlements={"max_branches": entitlement(True, limit_value=3)})
    assert permission_service.feature_limit(context, "max_branches") == 3


def test_feature_limit_returns_none_when_feature_missing():
    assert permission_service.feature_limit(ctx(entitlements={}), "max_branches") is None


def test_get_history_days_limit_reads_history_days_feature():
    context = ctx(entitlements={"history_days": entitlement(True, limit_value=90)})
    assert permission_service.get_history_days_limit(context) == 90


def test_can_access_branch_count_true_when_no_limit_configured():
    context = ctx(entitlements={})
    assert permission_service.can_access_branch_count(context, branch_count=50) is True


def test_can_access_branch_count_enforces_configured_limit():
    context = ctx(entitlements={"max_branches": entitlement(True, limit_value=3)})
    assert permission_service.can_access_branch_count(context, branch_count=3) is True
    assert permission_service.can_access_branch_count(context, branch_count=4) is False
