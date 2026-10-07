"""R8C: removing someone from an organization takes effect on their very next Streamlit run -- whatever the dashboard
has cached.

The Streamlit adapters cache a user's list of organizations for 120 seconds (services.streamlit_access_adapter). That
list only fills the organization picker. What AUTHORIZES is read from the database on every run and is cached nowhere:

    src/app.py                       access_service.user_can_access_org
    src/pages/1_admin_settings.py    the role, via entitlement_service.get_org_role_for_user
    src/pages/2_admin_users.py       the same

So these tests hold the cache STALE on purpose -- it goes on listing the organization, exactly as it would for up to two
minutes after a removal -- and run the real role lookup against a real (in-memory) database in which the membership has
been removed. The page must refuse, and the service behind it must not be reached.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from membership_db import MembershipDatabase, record_audit
from streamlit.testing.v1 import AppTest

from services import (
    access_service,
    auth_service,
    entitlement_service,
    sidebar_service,
    streamlit_access_adapter,
    streamlit_entitlement_adapter,
    tenant_service,
    user_admin_service,
)

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "src" / "app.py"
SETTINGS_PAGE = ROOT / "src" / "pages" / "1_admin_settings.py"
USERS_PAGE = ROOT / "src" / "pages" / "2_admin_users.py"

ACME, ADMIN, OWNER, VIEWER = 1, 11, 12, 13
SESSION = {"auth_user": {"id": ADMIN, "email": "user11@example.invalid"}}
NO_PERMISSION = {USERS_PAGE: "You do not have permission to manage users.",
                 SETTINGS_PAGE: "You do not have permission to manage settings."}
STALE_MEMBERSHIPS = [{"organization_id": ACME, "customer_id": 1, "role": "admin", "organization_slug": "acme", "organization_name": "Acme"}]


@pytest.fixture
def db(monkeypatch):
    database = MembershipDatabase()
    database.organization(ACME, "acme")
    for user_id, role in ((ADMIN, "admin"), (OWNER, "owner"), (VIEWER, "viewer")):
        database.user(user_id)
        database.member(ACME, user_id, role)

    # THE STALE CACHE: the adapter keeps answering as it did before the removal, role and all.
    monkeypatch.setattr(streamlit_access_adapter, "get_user_memberships", lambda user_id: STALE_MEMBERSHIPS)
    monkeypatch.setattr(streamlit_access_adapter, "get_org_branches",
                        lambda org_slug: [{"branch_slug": "main", "branch_name": "Main", "is_primary": True}])
    monkeypatch.setattr(streamlit_entitlement_adapter, "get_org_subscription", lambda org_slug: None)
    monkeypatch.setattr(access_service, "get_org_access_mode", lambda org_slug: "full")
    monkeypatch.setattr(sidebar_service, "render_main_sidebar", lambda **_kwargs: None)
    monkeypatch.setattr(auth_service, "is_user_active", lambda user_id: True)
    # THE REAL GATE and the real service, on the real database.
    for module in (entitlement_service, access_service, user_admin_service):
        monkeypatch.setattr(module, "get_engine", lambda: database.engine)
    monkeypatch.setattr(auth_service, "log_auth_event_with_connection", record_audit)
    monkeypatch.setattr(user_admin_service, "list_recent_org_auth_events", lambda org_slug, limit=25: [])
    return database


def _run(path: Path) -> AppTest:
    at = AppTest.from_file(str(path), default_timeout=60)
    for key, value in SESSION.items():
        at.session_state[key] = value
    at.run()
    return at


def _unreachable(name):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be reached by someone who is not an administrator of the organization")
    return fail


def _remove(db, user_id=ADMIN) -> None:
    db.run("UPDATE memberships SET removed_at = '2026-01-01 00:00:00+00:00' WHERE organization_id = :o AND user_id = :u", o=ACME, u=user_id)


def test_control_an_active_admin_reaches_the_users_page(db):
    at = _run(USERS_PAGE)

    assert not at.exception, [e.value for e in at.exception]
    assert [t.value for t in at.title] == ["Admin / Users"] and not at.error
    assert [s.value for s in at.subheader][-2:] == ["Remove from organization", "Recent user management activity"]


@pytest.mark.parametrize("page", [USERS_PAGE, SETTINGS_PAGE], ids=["users", "settings"])
@pytest.mark.parametrize("change", ["removed", "demoted"])
def test_a_removed_or_demoted_admin_is_refused_while_the_cache_still_lists_the_organization(db, monkeypatch, page, change):
    if change == "removed":
        _remove(db)
    else:
        db.run("UPDATE memberships SET role = 'viewer' WHERE organization_id = :o AND user_id = :u", o=ACME, u=ADMIN)
    monkeypatch.setattr(user_admin_service, "list_org_users", _unreachable("list_org_users"))
    monkeypatch.setattr(tenant_service, "get_effective_settings", _unreachable("get_effective_settings"))

    at = _run(page)

    assert not at.exception, [e.value for e in at.exception]
    assert [e.value for e in at.error] == [NO_PERMISSION[page]]
    assert at.title == [] and at.button == []  # nothing of the page itself was rendered


def test_a_form_left_open_by_someone_since_removed_changes_nothing_when_it_is_submitted(db):
    at = _run(USERS_PAGE)  # rendered while still an admin
    assert [t.value for t in at.title] == ["Admin / Users"]
    _remove(db)            # ... removed by the owner, in another session
    before = db.snapshot()

    [confirm] = [c for c in at.checkbox if c.label == "Remove this user from this organization"]
    confirm.check()
    [submit] = [b for b in at.button if b.label == "Remove from organization"]
    submit.click()
    at.run()

    assert not at.exception, [e.value for e in at.exception]
    assert [e.value for e in at.error] == [NO_PERMISSION[USERS_PAGE]]
    assert db.snapshot() == before


def test_removing_a_member_from_the_page_marks_the_membership_and_leaves_the_account_alone(db):
    at = _run(USERS_PAGE)
    [who] = [s for s in at.selectbox if s.label == "User to remove"]
    who.select(VIEWER)
    [confirm] = [c for c in at.checkbox if c.label == "Remove this user from this organization"]
    confirm.check()
    [submit] = [b for b in at.button if b.label == "Remove from organization"]
    submit.click()
    at.run()

    assert not at.exception, [e.value for e in at.exception]
    assert not at.error
    assert db.active_role(ACME, VIEWER) is None and db.account_active(VIEWER)
    assert [e["event_type"] for e in db.audit()] == ["membership_removed"]


def test_removal_needs_the_confirmation_box(db):
    at = _run(USERS_PAGE)
    before = db.snapshot()
    [submit] = [b for b in at.button if b.label == "Remove from organization"]
    submit.click()
    at.run()

    assert [e.value for e in at.error] == ["Tick the box to confirm before removing a user."]
    assert db.snapshot() == before


def test_an_admin_is_not_offered_the_owner_role_and_an_owner_is(db):
    at = _run(USERS_PAGE)
    assert [list(s.options) for s in at.selectbox if s.label in ("Role", "New role")] == [["admin", "manager", "viewer"]] * 2

    at = AppTest.from_file(str(USERS_PAGE), default_timeout=60)
    at.session_state["auth_user"] = {"id": OWNER, "email": "user12@example.invalid"}
    at.run()
    assert [list(s.options) for s in at.selectbox if s.label in ("Role", "New role")] == [["owner", "admin", "manager", "viewer"]] * 2


@pytest.mark.parametrize("role", ["manager", "viewer", None, "", "superuser"])
def test_someone_who_may_not_administer_is_offered_no_roles_and_no_actions_even_if_the_page_gate_let_them_in(db, monkeypatch, role):
    # The page's own gate (can_manage_settings) already stops a manager or a viewer. Suppose it did not: the role
    # list must still be empty -- never the full list of roles as a fallback -- and the page must stop.
    from services import permission_service

    monkeypatch.setattr(permission_service, "can_manage_settings", lambda context: True)
    monkeypatch.setattr(streamlit_entitlement_adapter, "build_entitlement_context", lambda user_id, org_slug: {"role": role})
    monkeypatch.setattr(user_admin_service, "list_org_users", _unreachable("list_org_users"))

    at = _run(USERS_PAGE)

    assert not at.exception, [e.value for e in at.exception]
    assert [e.value for e in at.error] == [NO_PERMISSION[USERS_PAGE]]
    assert at.title == [] and at.button == [] and at.selectbox == []  # no role is offered, and nothing to submit


def test_the_page_has_no_fallback_role_list():
    source = USERS_PAGE.read_text(encoding="utf-8")

    assert "ALLOWED_MEMBERSHIP_ROLES" not in source
    assert "role_choices = list(assignable_roles(entitlement_context.get(\"role\")))\n" in source.replace("\r\n", "\n")
    assert "if not show_admin_button or not role_choices:" in source


def test_the_users_page_offers_no_way_to_switch_an_account_off():
    source = USERS_PAGE.read_text(encoding="utf-8")

    for forbidden in ("set_global_account_active", "set_user_active", "is_active=", "Activate / deactivate", "deactivate"):
        assert forbidden not in source, forbidden
    assert "remove_organization_member(" in source and "actor_email" not in source


def test_the_dashboards_own_gate_is_the_uncached_one_and_a_removed_member_fails_it(db):
    source = APP.read_text(encoding="utf-8")
    import_block = source[source.index("from services.access_service import ("):]
    assert "user_can_access_org" in import_block[:import_block.index(")")]
    assert 'if not user_can_access_org(auth_user["id"], selected_org_slug):' in source
    # Nothing between that name and the database caches: no st.cache_data, no adapter.
    assert "cache" not in inspect.getsource(access_service.user_can_access_org)
    assert not hasattr(streamlit_access_adapter, "user_can_access_org")

    assert access_service.user_can_access_org(ADMIN, "acme") is True
    _remove(db)
    assert access_service.user_can_access_org(ADMIN, "acme") is False
    assert entitlement_service.get_org_role_for_user(ADMIN, "acme") is None
    assert streamlit_entitlement_adapter.build_entitlement_context(ADMIN, "acme")["role"] is None
