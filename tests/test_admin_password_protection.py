"""The Admin Settings lock is stored, cached and shown without its plaintext (security/privacy post-containment).

The Admin Settings page asks an owner/admin for an extra "admin password" before showing the settings form. It used to be
kept in PLAINTEXT in `organization_settings.settings_json` (`security.admin_password`), cached process-wide for every user
of the organization, pre-filled into the browser after unlocking, compared with `==`, and unlocked by one global boolean.

What this guarantees now:

  * only a salted one-way hash is stored (`security.admin_password_hash`); the form never pre-fills it;
  * the `security` block is not part of the cached dashboard settings, and not in the tracked settings file;
  * wrong passwords fail, correct ones work, and a LEGACY plaintext value is still accepted (so no organization is
    locked out at deploy time) and is converted to a hash the next time settings are saved;
  * unlocking is scoped to one user in one organization;
  * the password and its hash are never rendered, logged or shown in the DB preview.

Every value is a SYNTHETIC canary. The page is a Streamlit script, so it runs for real under `AppTest`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from services import admin_lock_service as lock

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "src" / "pages" / "1_admin_settings.py"

PW = "CANARY-ADMIN-PASSWORD-7101"
OTHER_PW = "CANARY-ADMIN-PASSWORD-7102"


# =====================================================================================================================
# 1. The credential functions
# =====================================================================================================================

def test_the_stored_hash_is_not_the_password_and_cannot_be_read_back():
    stored = lock.hash_admin_password(PW)

    assert stored != PW and PW not in stored
    assert lock.hash_admin_password(PW) != stored  # salted: two hashes of one password differ
    assert lock.verify_admin_password(PW, {lock.HASH_KEY: stored}) is True


def test_a_wrong_or_empty_password_fails_and_a_correct_one_works():
    security = {lock.HASH_KEY: lock.hash_admin_password(PW)}

    assert lock.verify_admin_password(PW, security) is True
    assert lock.verify_admin_password(OTHER_PW, security) is False
    assert lock.verify_admin_password(PW + " ", security) is False
    assert lock.verify_admin_password("", security) is False


def test_nothing_verifies_when_no_password_is_set_or_the_hash_is_malformed():
    assert lock.verify_admin_password(PW, {}) is False
    assert lock.verify_admin_password(PW, None) is False
    assert lock.verify_admin_password("", {"admin_password": ""}) is False
    assert lock.verify_admin_password(PW, {lock.HASH_KEY: "not-a-real-hash"}) is False
    assert lock.has_admin_password({}) is False and lock.has_admin_password({"admin_password": ""}) is False


def test_a_legacy_plaintext_password_is_still_accepted_so_nobody_is_locked_out():
    legacy = {"admin_enabled": True, "admin_password": PW}

    assert lock.has_admin_password(legacy) and lock.is_legacy_plaintext(legacy)
    assert lock.verify_admin_password(PW, legacy) is True
    assert lock.verify_admin_password(OTHER_PW, legacy) is False


def test_a_stored_hash_takes_precedence_over_a_stale_legacy_value():
    security = {lock.HASH_KEY: lock.hash_admin_password(OTHER_PW), "admin_password": PW}

    assert lock.is_legacy_plaintext(security) is False
    assert lock.verify_admin_password(OTHER_PW, security) is True
    assert lock.verify_admin_password(PW, security) is False


def _build(**kwargs):
    defaults = {"enabled": True, "new_password": "", "remove_password": False, "current": None}
    return lock.build_security_settings(**{**defaults, **kwargs})


def test_a_new_password_is_stored_only_as_a_hash():
    block = _build(new_password=PW)

    assert set(block) == {"admin_enabled", lock.HASH_KEY}
    assert PW not in json.dumps(block) and "admin_password" not in block
    assert lock.verify_admin_password(PW, block) is True


def test_a_blank_password_keeps_the_current_hash_unchanged():
    current = {"admin_enabled": True, lock.HASH_KEY: lock.hash_admin_password(PW)}

    block = _build(current=current)

    assert block[lock.HASH_KEY] == current[lock.HASH_KEY]


def test_saving_with_a_blank_password_converts_a_legacy_value_to_a_hash_of_the_same_password():
    block = _build(current={"admin_enabled": True, "admin_password": PW})

    assert "admin_password" not in block and PW not in json.dumps(block)  # the plaintext is gone...
    assert lock.verify_admin_password(PW, block) is True  # ...and the same password still works


def test_a_new_password_replaces_a_legacy_one_and_removing_clears_it():
    replaced = _build(new_password=OTHER_PW, current={"admin_password": PW})
    assert lock.verify_admin_password(OTHER_PW, replaced) and not lock.verify_admin_password(PW, replaced)

    removed = _build(remove_password=True, new_password=OTHER_PW, current={lock.HASH_KEY: lock.hash_admin_password(PW)})
    assert removed == {"admin_enabled": True}


@pytest.mark.parametrize("current", [
    None, {}, {"admin_password": PW}, {lock.HASH_KEY: "x"}, {"admin_password": PW, lock.HASH_KEY: "x"},
])
def test_the_saved_block_never_carries_a_plaintext_password_key(current):
    for kwargs in ({}, {"new_password": PW}, {"remove_password": True}, {"enabled": False}):
        block = _build(current=current, **kwargs)
        assert "admin_password" not in block and PW not in json.dumps(block)


def test_the_displayable_view_never_holds_the_password_or_its_hash():
    stored = lock.hash_admin_password(PW)

    for security in ({lock.HASH_KEY: stored}, {"admin_password": PW}):
        view = lock.public_security_view(security)
        assert view == {"admin_enabled": True, "admin_password_set": True}
        assert PW not in json.dumps(view) and stored not in json.dumps(view)
    assert lock.public_security_view({})["admin_password_set"] is False


def test_without_security_drops_only_the_security_block():
    assert lock.without_security({"security": {"admin_password": PW}, "transit": {"a": 1}}) == {"transit": {"a": 1}}
    assert lock.without_security(None) == {}


# =====================================================================================================================
# 2. Nothing credential-like is cached, and none is committed
# =====================================================================================================================

@pytest.fixture
def settings_service():
    from services import settings_service as module

    module.load_runtime_settings.clear()
    yield module
    module.load_runtime_settings.clear()


def _effective(org_slug, branch_slug=None, security=None):
    return {
        "organization": {"id": 1, "name": "Acme", "slug": org_slug},
        "branch": {"id": 2, "name": "Main", "slug": branch_slug or "main", "is_primary": True},
        "subscription": None,
        "settings": {"security": security or {}, "transit": {"home_branch_label": "Home"}},
        "entitlements": {},
    }


@pytest.mark.parametrize("security", [
    {"admin_enabled": True, "admin_password": PW},
    {"admin_enabled": True, lock.HASH_KEY: "scrypt:32768:8:1$salt$" + "ab" * 32},
])
def test_the_cached_settings_hold_no_security_block_password_or_hash(settings_service, monkeypatch, tmp_path, security):
    monkeypatch.setattr(settings_service, "get_effective_settings", lambda org_slug, branch_slug=None: _effective(org_slug, branch_slug, security))

    result = settings_service.load_runtime_settings(tmp_path / "unused.json", org_slug="acme", branch_slug="main")

    assert result["source"] == "database"
    assert PW not in repr(result) and "ab" * 32 not in repr(result)
    assert "security" not in result["branch_settings"] and "security" not in result["tenant"]["settings"]
    assert result["TRANSIT_HOME_LABEL"] == "Home"  # everything the dashboard actually reads is intact


def test_the_file_fallback_is_stripped_too_even_if_the_file_carries_a_security_block(settings_service, monkeypatch, tmp_path):
    settings_file = tmp_path / "branch_settings.json"
    settings_file.write_text(json.dumps({"library": {"library_name": "Fallback Library"},
                                         "security": {"admin_enabled": True, "admin_password": PW}}), encoding="utf-8")

    from_file = settings_service.load_runtime_settings(settings_file, org_slug=None)
    assert PW not in repr(from_file) and "security" not in from_file["branch_settings"]

    def unavailable(org_slug, branch_slug=None):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(settings_service, "get_effective_settings", unavailable)
    settings_service.load_runtime_settings.clear()
    fallback = settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")
    assert fallback["source"] == "file_fallback" and fallback["LIBRARY_NAME"] == "Fallback Library"
    assert PW not in repr(fallback) and "security" not in fallback["branch_settings"]


def test_no_tracked_settings_file_carries_an_admin_password():
    offenders = []
    for base in (ROOT / "src", ROOT / "super_admin"):
        for path in base.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if "admin_password" in text or (isinstance(data, dict) and "security" in data):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_neither_page_writes_or_defaults_a_plaintext_password_key():
    for path in (PAGE, ROOT / "super_admin" / "pages" / "Provision_Library.py"):
        source = path.read_text(encoding="utf-8")
        assert '"admin_password"' not in source and "'admin_password'" not in source, path.name


# =====================================================================================================================
# 3. The Admin Settings page
# =====================================================================================================================

class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Conn:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self._engine.executed.append((sql, dict(params or {})))
        if "FROM organizations o" in sql:
            return _Result({"organization_id": 1, "organization_name": "Acme", "org_settings_row_id": 11,
                            "org_settings_json": self._engine.org_settings})
        if "FROM branches b" in sql:
            return _Result({"branch_id": 2, "branch_name": "Main", "branch_settings_row_id": None,
                            "branch_settings_json": {}})
        return _Result(None)


class _Engine:
    """Records what the page writes. `org_settings` is the organization's stored settings_json."""

    def __init__(self, org_settings=None):
        self.executed: list[tuple[str, dict]] = []
        self.org_settings = org_settings or {}

    def connect(self):
        return _Conn(self)

    begin = connect

    def saved_org_settings(self) -> dict:
        (params,) = [p for sql, p in self.executed if sql.startswith(("INSERT INTO organization_settings", "UPDATE organization_settings"))]
        return json.loads(params["settings_json"])


ORGS = {"acme": {"organization_slug": "acme", "organization_name": "Acme"},
        "beta": {"organization_slug": "beta", "organization_name": "Beta"}}
USER = {"id": 1, "email": "admin@example.invalid"}


def _patch_page(monkeypatch, *, security_by_org: dict, engine: _Engine | None = None):
    import database
    import services.access_service as access
    import services.entitlement_service as entitlement
    import services.permission_service as permission
    import services.sidebar_service as sidebar
    import services.tenant_service as tenant
    from services import auth_service

    monkeypatch.setattr(access, "get_user_memberships", lambda user_id: list(ORGS.values()))
    monkeypatch.setattr(access, "get_org_branches",
                        lambda org_slug: [{"branch_slug": "main", "branch_name": "Main", "is_primary": True}])
    monkeypatch.setattr(entitlement, "build_entitlement_context", lambda user_id, org_slug: {})
    monkeypatch.setattr(permission, "can_manage_settings", lambda context: True)
    monkeypatch.setattr(sidebar, "render_main_sidebar", lambda **_kwargs: None)
    monkeypatch.setattr(tenant, "get_effective_settings",
                        lambda org_slug, branch_slug=None: _effective(org_slug, branch_slug, security_by_org.get(org_slug)))
    monkeypatch.setattr(database, "get_engine", lambda: engine if engine is not None else _Engine())
    # PRE-PILOT active-session guard: the settings page now calls
    # auth_service.enforce_active_session() right after reading auth_user
    # from session state, which queries auth_service's own get_engine()
    # (a separate name binding from database.get_engine above, so that
    # patch alone doesn't reach it). This test's synthetic user is active.
    monkeypatch.setattr(auth_service, "is_user_active", lambda user_id: True)


def _run(*, org="acme", user=USER, unlocked_for=None) -> AppTest:
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.session_state["auth_user"] = user
    at.session_state["selected_org_slug"] = org
    if unlocked_for is not None:
        at.session_state["admin_unlocked_scope"] = unlocked_for
    at.run()
    return at


def _button(at, label):
    (match,) = [b for b in at.button if b.label == label]
    return match


def _field(at, label):
    (match,) = [t for t in at.text_input if t.label == label]
    return match


def _everything(at: AppTest) -> str:
    """Every element's text/labels AND its full protobuf (so a value sent to the browser but not visible is caught)."""
    parts: list[str] = []

    def walk(block):
        for element in block:
            parts.append(str(getattr(element, "proto", "")))
            for attribute in ("value", "label", "body"):
                value = getattr(element, attribute, None)
                if value is not None and not callable(value):
                    parts.append(str(value))
            children = getattr(element, "children", None)
            if children:
                walk(children.values() if isinstance(children, dict) else children)

    walk(at.main)
    return "\n".join(parts)


def _unlock(at: AppTest, password: str) -> AppTest:
    _field(at, "Admin password").input(password)
    _button(at, "Unlock").click()
    return at.run()


def _no_leak(at: AppTest, caplog, *secrets: str) -> None:
    rendered = _everything(at)
    assert [s for s in secrets if s in rendered] == []
    assert [s for s in secrets if s in caplog.text] == []
    assert [s for s in secrets if s in json.dumps({k: at.session_state[k] for k in at.session_state.filtered_state}, default=str)] == []


HASHED = {"admin_enabled": True, lock.HASH_KEY: lock.hash_admin_password(PW)}
LEGACY = {"admin_enabled": True, "admin_password": PW}


def test_with_a_stored_hash_a_wrong_password_is_refused_and_the_correct_one_unlocks(monkeypatch, caplog):
    _patch_page(monkeypatch, security_by_org={"acme": HASHED})

    with caplog.at_level(logging.DEBUG):
        at = _run()
        assert not [b for b in at.button if b.label == "Save Settings"]  # locked: no form
        wrong = _unlock(at, OTHER_PW)
        assert [e.value for e in wrong.error] == ["Incorrect password."]
        assert not [b for b in wrong.button if b.label == "Save Settings"]
        assert not wrong.warning  # an already-hashed password gets no "outdated format" notice

        unlocked = _unlock(_run(), PW)

    assert not unlocked.exception and _button(unlocked, "Save Settings")  # the form is now shown
    _no_leak(unlocked, caplog, PW, OTHER_PW, HASHED[lock.HASH_KEY])


def test_the_password_field_is_never_pre_filled_and_the_hash_is_never_sent_to_the_browser(monkeypatch, caplog):
    _patch_page(monkeypatch, security_by_org={"acme": HASHED})

    with caplog.at_level(logging.DEBUG):
        at = _run(unlocked_for=(USER["id"], "acme"))

    assert _field(at, "New admin password").value == ""
    _no_leak(at, caplog, PW, HASHED[lock.HASH_KEY])
    assert [j for j in at.json if "admin_password_set" in j.value]  # the preview says only that a password is set
    assert json.loads(at.json[0].value)["security"] == {"admin_enabled": True, "admin_password_set": True}


def test_a_legacy_plaintext_password_still_unlocks_and_the_admin_is_told_to_save_once(monkeypatch, caplog):
    _patch_page(monkeypatch, security_by_org={"acme": LEGACY})

    with caplog.at_level(logging.DEBUG):
        at = _run()
        assert [e.value for e in _unlock(at, OTHER_PW).error] == ["Incorrect password."]
        unlocked = _unlock(_run(), PW)

    assert _button(unlocked, "Save Settings")
    assert any("outdated, unprotected format" in w.value for w in unlocked.warning)
    assert _field(unlocked, "New admin password").value == ""  # the legacy plaintext is not pre-filled either
    assert PW not in _everything(unlocked) and PW not in caplog.text


def test_saving_a_new_password_persists_only_a_hash(monkeypatch, caplog):
    engine = _Engine(org_settings={"security": {"admin_enabled": True}})
    _patch_page(monkeypatch, security_by_org={"acme": {"admin_enabled": True}}, engine=engine)

    with caplog.at_level(logging.DEBUG):
        at = _run()  # no password set yet, so the form is open
        _field(at, "New admin password").input(PW)
        _button(at, "Save Settings").click()
        at.run()

    saved = engine.saved_org_settings()["security"]
    assert set(saved) == {"admin_enabled", lock.HASH_KEY}
    assert saved[lock.HASH_KEY] != PW and lock.verify_admin_password(PW, saved) and not lock.verify_admin_password(OTHER_PW, saved)
    assert PW not in json.dumps(engine.executed, default=str) and PW not in caplog.text  # not in any SQL or bound value


def test_saving_with_the_password_field_blank_keeps_the_existing_password(monkeypatch):
    engine = _Engine(org_settings={"security": HASHED})
    _patch_page(monkeypatch, security_by_org={"acme": HASHED}, engine=engine)

    at = _run(unlocked_for=(USER["id"], "acme"))
    _button(at, "Save Settings").click()
    at.run()

    assert engine.saved_org_settings()["security"] == HASHED


def test_saving_settings_converts_a_legacy_plaintext_password_to_a_hash_of_the_same_password(monkeypatch, caplog):
    engine = _Engine(org_settings={"security": dict(LEGACY)})
    _patch_page(monkeypatch, security_by_org={"acme": LEGACY}, engine=engine)

    with caplog.at_level(logging.DEBUG):
        at = _run(unlocked_for=(USER["id"], "acme"))
        _button(at, "Save Settings").click()
        at.run()

    saved = engine.saved_org_settings()["security"]
    assert "admin_password" not in saved and PW not in json.dumps(engine.executed, default=str)  # plaintext gone from the row
    assert lock.verify_admin_password(PW, saved) is True  # ...and the organization keeps the same password
    assert PW not in caplog.text


def test_the_remove_option_clears_the_password(monkeypatch):
    engine = _Engine(org_settings={"security": HASHED})
    _patch_page(monkeypatch, security_by_org={"acme": HASHED}, engine=engine)

    at = _run(unlocked_for=(USER["id"], "acme"))
    (remove,) = [c for c in at.checkbox if c.label == "Remove the admin password"]
    remove.check()
    _button(at, "Save Settings").click()
    at.run()

    assert engine.saved_org_settings()["security"] == {"admin_enabled": True}


def test_with_no_password_set_the_settings_form_opens_without_a_prompt_as_before(monkeypatch):
    _patch_page(monkeypatch, security_by_org={})

    at = _run()

    assert _button(at, "Save Settings") and not at.error
    assert any("no password is currently set" in w.value for w in at.warning)
    (remove,) = [c for c in at.checkbox if c.label == "Remove the admin password"]
    assert remove.disabled is True


def test_unlocking_is_scoped_to_one_user_in_one_organization(monkeypatch):
    _patch_page(monkeypatch, security_by_org={"acme": HASHED, "beta": {"admin_enabled": True, lock.HASH_KEY: lock.hash_admin_password(OTHER_PW)}})

    acme = _unlock(_run(org="acme"), PW)
    assert _button(acme, "Save Settings")  # unlocked for this user in acme

    acme.session_state["selected_org_slug"] = "beta"
    beta = acme.run()
    assert not [b for b in beta.button if b.label == "Save Settings"]  # acme's unlock does not open beta (it used to)
    assert [b.label for b in beta.button] == ["Unlock"]

    beta.session_state["selected_org_slug"] = "acme"
    beta.session_state["auth_user"] = {"id": 2, "email": "someone-else@example.invalid"}
    other_user = beta.run()
    assert not [b for b in other_user.button if b.label == "Save Settings"]  # nor does it open for a different user

    beta.session_state["auth_user"] = USER
    assert _button(beta.run(), "Save Settings")  # the same user in the same organization is still unlocked


def test_the_lock_button_locks_again(monkeypatch):
    _patch_page(monkeypatch, security_by_org={"acme": HASHED})

    at = _run(unlocked_for=(USER["id"], "acme"))
    _button(at, "Lock").click()
    at.run()

    assert not [b for b in at.button if b.label == "Save Settings"] and _button(at, "Unlock")
