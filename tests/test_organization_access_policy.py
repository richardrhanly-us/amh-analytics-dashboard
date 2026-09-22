"""Organization lifecycle access policy, exercised as real Streamlit script
runs (the admin pages are Streamlit multipage entry points, independent of
app.py -- see test_admin_error_containment.py for the same convention).

Chosen policy (approved):
    active / trial -> "full"       normal customer access
    suspended       -> "read_only"  dashboard/history reads allowed;
                                     admin mutations blocked
    cancelled       -> "blocked"    no customer access at all

This file proves the PAGE-LEVEL gate on both admin pages: a suspended or
cancelled organization's admin page is blocked entirely, with a clear
message, before any settings/user-management content renders. The
service-level enforcement (the real security boundary, independent of
whether this page-level gate is ever bypassed) is covered separately in
tests/test_user_admin_service.py and inline in 1_admin_settings.save_settings.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PAGE = ROOT / "src" / "pages" / "1_admin_settings.py"
USERS_PAGE = ROOT / "src" / "pages" / "2_admin_users.py"

ADMIN_SESSION = {"auth_user": {"id": 1, "email": "admin@example.invalid"}}


def _patch_common(monkeypatch, *, access_mode: str):
    import services.access_service as access
    import services.entitlement_service as entitlement
    import services.permission_service as permission
    import services.sidebar_service as sidebar
    from services import auth_service

    monkeypatch.setattr(access, "get_user_memberships",
                        lambda user_id: [{"organization_slug": "acme", "organization_name": "Acme"}])
    monkeypatch.setattr(access, "get_org_branches",
                        lambda org_slug: [{"branch_slug": "main", "branch_name": "Main", "is_primary": True}])
    monkeypatch.setattr(access, "get_org_access_mode", lambda org_slug: access_mode)
    monkeypatch.setattr(entitlement, "build_entitlement_context", lambda user_id, org_slug: {})
    monkeypatch.setattr(permission, "can_manage_settings", lambda context: True)
    monkeypatch.setattr(sidebar, "render_main_sidebar", lambda **_kwargs: None)
    monkeypatch.setattr(auth_service, "is_user_active", lambda user_id: True)


def _patch_settings_page(monkeypatch, *, access_mode: str):
    import services.tenant_service as tenant

    _patch_common(monkeypatch, access_mode=access_mode)
    # If the page-level gate fails to stop the script, execution would
    # reach this and fail with an AssertionError rather than a DB error --
    # a clearer signal than a stray real-engine call.
    monkeypatch.setattr(
        tenant, "get_effective_settings",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("get_effective_settings must not be reached when access mode is not full")
        ),
    )


def _patch_users_page(monkeypatch, *, access_mode: str):
    import services.user_admin_service as user_admin

    _patch_common(monkeypatch, access_mode=access_mode)
    monkeypatch.setattr(
        user_admin, "list_org_users",
        lambda org_slug: (_ for _ in ()).throw(
            AssertionError("list_org_users must not be reached when access mode is not full")
        ),
    )


def _run(path: Path) -> AppTest:
    at = AppTest.from_file(str(path), default_timeout=60)
    for key, value in ADMIN_SESSION.items():
        at.session_state[key] = value
    at.run()
    return at


def _rendered_errors(at: AppTest) -> list[str]:
    return [e.value for e in at.error]


# --- Admin Settings page ------------------------------------------------------

def test_settings_page_blocks_whole_page_when_suspended(monkeypatch):
    _patch_settings_page(monkeypatch, access_mode="read_only")

    at = _run(SETTINGS_PAGE)

    assert not at.exception, [e.value for e in at.exception]
    errors = _rendered_errors(at)
    assert any("suspended" in e for e in errors)
    assert at.title == []  # the settings page's own title never rendered


def test_settings_page_blocks_whole_page_when_cancelled(monkeypatch):
    _patch_settings_page(monkeypatch, access_mode="blocked")

    at = _run(SETTINGS_PAGE)

    assert not at.exception, [e.value for e in at.exception]
    errors = _rendered_errors(at)
    assert any("no longer available" in e for e in errors)
    assert at.title == []


def test_settings_page_renders_normally_when_full(monkeypatch):
    import services.tenant_service as tenant

    _patch_common(monkeypatch, access_mode="full")
    monkeypatch.setattr(
        tenant, "get_effective_settings",
        lambda org_slug, branch_slug=None: {
            "organization": {"id": 1, "name": "Acme", "slug": org_slug},
            "branch": {"id": 2, "name": "Main", "slug": branch_slug or "main", "is_primary": True},
            "subscription": None,
            "settings": {},
            "entitlements": {},
        },
    )

    at = _run(SETTINGS_PAGE)

    assert not at.exception, [e.value for e in at.exception]
    assert _rendered_errors(at) == []


# --- Admin Users page ----------------------------------------------------------

def test_users_page_blocks_whole_page_when_suspended(monkeypatch):
    _patch_users_page(monkeypatch, access_mode="read_only")

    at = _run(USERS_PAGE)

    assert not at.exception, [e.value for e in at.exception]
    errors = _rendered_errors(at)
    assert any("suspended" in e for e in errors)
    assert at.title == []


def test_users_page_blocks_whole_page_when_cancelled(monkeypatch):
    _patch_users_page(monkeypatch, access_mode="blocked")

    at = _run(USERS_PAGE)

    assert not at.exception, [e.value for e in at.exception]
    errors = _rendered_errors(at)
    assert any("no longer available" in e for e in errors)
    assert at.title == []


def test_users_page_renders_normally_when_full(monkeypatch):
    import services.user_admin_service as user_admin

    _patch_common(monkeypatch, access_mode="full")
    monkeypatch.setattr(user_admin, "list_org_users", lambda org_slug: [])
    monkeypatch.setattr(user_admin, "list_recent_org_auth_events", lambda org_slug, limit=25: [])

    at = _run(USERS_PAGE)

    assert not at.exception, [e.value for e in at.exception]
    assert _rendered_errors(at) == []
    assert [t.value for t in at.title] == ["Admin / Users"]
