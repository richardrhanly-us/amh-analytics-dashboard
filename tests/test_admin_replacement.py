"""C6a R1: machine replacement authorization and cancellation in the administration API, without a database.

    POST /api/admin/libraries/{org_slug}/installations/{installation_id}/replacements         authorize replacing it
    POST /api/admin/libraries/{org_slug}/installations/{installation_id}/cancel-replacement   cancel a pending one

Driven through TestClient(main.app) with services.admin_installation_service replaced, as
tests/test_admin_installations.py does: who may call, the exact shapes, and every refusal as its fixed code. The rules
themselves, their statements, the trigger and the concurrency are proved on a real server in
tests/test_admin_replacement_postgres.py. The migration's revision chain and DDL are checked here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from test_admin_installations import (
    ADMIN_ORIGIN,
    COOKIE,
    CUSTOMER_ORIGIN,
    FORBIDDEN,
    INSTALLATION,
    INSTALLATION_JSON,
    LIBRARY_NOT_FOUND,
    NOT_AUTHENTICATED,
    World,
    _send,
    api,  # noqa: F401 -- a pytest fixture, requested by name
)

from services import admin_installation_service as service
from services.admin_installation_service import InstallationAdminError

ROOT = Path(__file__).resolve().parent.parent
BASE = "/api/admin/libraries/acme/installations"
AUTHORIZE = f"{BASE}/101/replacements"
CANCEL = f"{BASE}/123/cancel-replacement"
REVISION, PREVIOUS_HEAD = "b8e2d4f61a07", "c1e7a94f2b60"


class ReplacementWorld(World):
    """World, plus the two replacement operations, recording their calls."""

    def __init__(self, monkeypatch, **kwargs):
        super().__init__(monkeypatch, **kwargs)
        monkeypatch.setattr(service, "authorize_replacement", self._authorize)
        monkeypatch.setattr(service, "cancel_replacement", self._cancel)

    def _authorize(self, library, source_installation_id, *, name, confirm_lost_response, actor):
        if self.refuse:
            raise InstallationAdminError(self.refuse)
        self.calls.append(("authorize", library.id, source_installation_id, name, confirm_lost_response, actor["id"]))
        return {"installation": {**INSTALLATION, "status": "provisioning", "name": name}, "replaces_installation_id": 101}

    def _cancel(self, library, installation_id, *, actor):
        if self.refuse:
            raise InstallationAdminError(self.refuse)
        self.calls.append(("cancel", library.id, installation_id, actor["id"]))
        return {"installation": {**INSTALLATION, "status": "retired"}, "replaces_installation_id": 101}


WRITES = [("post", AUTHORIZE, {"name": "Replacement Sorter"}), ("post", CANCEL, None)]


# =====================================================================================================================
# who may call
# =====================================================================================================================

@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_no_session_no_platform_admin_or_an_unknown_library_writes_nothing(api, monkeypatch, method, path, body):  # noqa: F811
    for kwargs, expected in (({"user": None}, (401, NOT_AUTHENTICATED)), ({"admin": False}, (403, FORBIDDEN)),
                             ({"libraries": {}}, (404, LIBRARY_NOT_FOUND))):
        world = ReplacementWorld(monkeypatch, **kwargs)
        response = _send(api, method, path, body)
        assert (response.status_code, response.json()) == expected
        assert world.calls == []


@pytest.mark.parametrize("origin", [None, "https://evil.example", CUSTOMER_ORIGIN])
@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_a_write_without_the_admin_origin_is_refused(api, monkeypatch, method, path, body, origin):  # noqa: F811
    world = ReplacementWorld(monkeypatch)

    response = _send(api, method, path, body, origin=origin)

    assert response.status_code == 403 and world.calls == []


@pytest.mark.parametrize(("method", "path"), [
    ("get", AUTHORIZE), ("put", AUTHORIZE), ("delete", AUTHORIZE), ("get", CANCEL), ("delete", CANCEL),
])
def test_only_post_is_a_route(api, monkeypatch, method, path):  # noqa: F811
    ReplacementWorld(monkeypatch)

    assert getattr(api, method)(path, headers={**COOKIE, "Origin": ADMIN_ORIGIN}).status_code == 405


# =====================================================================================================================
# shapes
# =====================================================================================================================

@pytest.mark.parametrize(("body", "confirm"), [
    ({"name": "Replacement Sorter"}, False),
    ({"name": "Replacement Sorter", "confirm_lost_response": True}, True),
    ({"name": "Replacement Sorter", "confirm_lost_response": False}, False),
])
def test_authorizing_sends_the_source_name_and_confirmation_and_answers_201(api, monkeypatch, body, confirm):  # noqa: F811
    world = ReplacementWorld(monkeypatch)

    response = _send(api, "post", AUTHORIZE, body)

    assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "installation": {**INSTALLATION_JSON, "status": "provisioning", "name": "Replacement Sorter"},
        "replaces_installation_id": 101,
    }
    assert world.calls == [("authorize", 90417, 101, "Replacement Sorter", confirm, 7)]


@pytest.mark.parametrize("body", [
    {}, {"name": 5}, {"name": "x", "confirm_lost_response": "yes"}, {"name": "x", "confirm_lost_response": 1},
    {"name": "x", "branch": "east"}, {"name": "x", "required_contract": 1}, {"name": "x", "status": "active"},
    {"name": "x", "replaces_installation_id": 7},
])
def test_an_authorization_with_any_other_field_or_type_is_422(api, monkeypatch, body):  # noqa: F811
    world = ReplacementWorld(monkeypatch)

    assert _send(api, "post", AUTHORIZE, body).status_code == 422
    assert world.calls == []


@pytest.mark.parametrize(("name", "code"), [("   ", "required"), ("x" * 121, "too_long")])
def test_a_blank_or_long_replacement_name_is_422_naming_the_field(api, monkeypatch, name, code):  # noqa: F811
    world = ReplacementWorld(monkeypatch)

    response = _send(api, "post", AUTHORIZE, {"name": name})

    assert response.status_code == 422 and response.json()["problems"] == [{"field": "name", "code": code}]
    assert world.calls == []


def test_cancelling_sends_the_installation_and_answers_200(api, monkeypatch):  # noqa: F811
    world = ReplacementWorld(monkeypatch)

    response = _send(api, "post", CANCEL, None)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"installation": {**INSTALLATION_JSON, "status": "retired"}, "replaces_installation_id": 101}
    assert world.calls == [("cancel", 90417, 123, 7)]


# =====================================================================================================================
# refusals
# =====================================================================================================================

AUTHORIZE_REFUSALS = [
    ("installation_not_found", 404), ("library_cancelled", 409), ("no_active_primary_branch", 409),
    ("collector_settings_not_ready", 409), ("operational_identity_incomplete", 409),
    ("replacement_source_retired", 409), ("replacement_source_not_contract_2", 409),
    ("replacement_source_not_at_primary_branch", 409), ("replacement_source_not_key_owner", 409),
    ("replacement_branch_not_enrolled", 409), ("unbound_tokens_active", 409), ("replacement_already_pending", 409),
    ("lost_response_confirmation_required", 409), ("replacement_disabled", 409),
]
CANCEL_REFUSALS = [("installation_not_found", 404), ("library_cancelled", 409), ("not_a_pending_replacement", 409),
                   ("transition_not_allowed", 409)]


@pytest.mark.parametrize(("path", "body", "code", "status"),
                         [(AUTHORIZE, {"name": "R"}, code, status) for code, status in AUTHORIZE_REFUSALS]
                         + [(CANCEL, None, code, status) for code, status in CANCEL_REFUSALS])
def test_every_refusal_is_its_fixed_code_and_message(api, monkeypatch, path, body, code, status):  # noqa: F811
    world = ReplacementWorld(monkeypatch)
    world.refuse = code

    response = _send(api, "post", path, body)

    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == code and set(response.json()) == {"code", "message"}


@pytest.mark.parametrize("code", ["use_cancel_replacement", "replacement_source_not_key_owner", "unbound_tokens_active",
                                  "replacement_branch_not_enrolled", "replacement_disabled"])
def test_the_status_change_and_code_issue_routes_answer_the_new_refusals(api, monkeypatch, code):  # noqa: F811
    world = ReplacementWorld(monkeypatch)
    world.refuse = code
    path, body = ((f"{BASE}/123/status", {"status": "retired"}) if code == "use_cancel_replacement"
                  else (f"{BASE}/123/enrollment-codes", None))

    response = _send(api, "put" if body else "post", path, body)

    assert (response.status_code, response.json()["code"]) == (409, code)


# =====================================================================================================================
# the migration
# =====================================================================================================================

def _script_directory():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_the_migration_is_the_single_head_directly_after_m1():
    script = _script_directory()

    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD


def _statements(direction):
    revision = _script_directory().get_revision(REVISION)
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", revision.path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    executed: list[str] = []

    class FakeOp:
        @staticmethod
        def execute(sql):
            executed.append(" ".join(str(sql).split()))

    module.op = FakeOp
    getattr(module, direction)()
    return executed


def test_the_upgrade_adds_exactly_the_approved_columns_constraints_and_trigger():
    executed = " | ".join(_statements("upgrade"))

    assert executed.startswith("SET LOCAL lock_timeout")
    for expected in (
        "ADD COLUMN replaces_installation_id BIGINT", "ADD COLUMN replacement_authorized_by_user_id BIGINT",
        "ADD COLUMN replacement_authorized_at TIMESTAMPTZ",
        "FOREIGN KEY (replaces_installation_id, branch_id) REFERENCES collector_installations (id, branch_id)",
        "ck_collector_installations_replacement_all_or_none", "ck_collector_installations_replaces_not_self",
        "BEFORE UPDATE OF replaces_installation_id, replacement_authorized_by_user_id, replacement_authorized_at",
    ):
        assert expected in executed, expected
    # No foreign key on the authorizing user, no default, no backfill, no grant: existing rows are untouched.
    for absent in ("REFERENCES app_users", "DEFAULT", "UPDATE collector_installations SET", "GRANT"):
        assert absent not in executed, absent


def test_the_downgrade_removes_exactly_what_the_upgrade_added():
    executed = " | ".join(_statements("downgrade"))

    for expected in ("DROP TRIGGER IF EXISTS trg_collector_installations_replacement_write_once",
                     "DROP FUNCTION IF EXISTS collector_installations_replacement_write_once()",
                     "DROP CONSTRAINT ck_collector_installations_replaces_not_self",
                     "DROP CONSTRAINT ck_collector_installations_replacement_all_or_none",
                     "DROP CONSTRAINT fk_collector_installations_replaces_same_branch",
                     "DROP COLUMN replacement_authorized_at", "DROP COLUMN replacement_authorized_by_user_id",
                     "DROP COLUMN replaces_installation_id"):
        assert expected in executed, expected
    assert "DROP TABLE" not in executed and "uq_collector_installations_id_branch_id" not in executed
