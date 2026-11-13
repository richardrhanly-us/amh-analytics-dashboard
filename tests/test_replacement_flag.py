"""The machine replacement flag (C6a R3, docs/c6a-replacement-machine-design.md 14.11), without a database.

SORTVIEW_MACHINE_REPLACEMENT_ENABLED is off unless it is `true`, trimmed and in any case. It is read on each call, by one
function, and the module that holds it is imported under two names (src.services and services): both must agree.
The guards themselves, and that everything else is unaffected, are tested with the database in
tests/test_replacement_redemption.py, tests/test_admin_replacement_postgres.py and
tests/test_replacement_redemption_postgres.py.
"""

from __future__ import annotations

import pytest

import services.replacement_eligibility as flat
import src.services.replacement_eligibility as packaged
from services import admin_installation_service as service
from services.admin_installation_service import InstallationAdminError, Library

VARIABLE = "SORTVIEW_MACHINE_REPLACEMENT_ENABLED"


@pytest.mark.parametrize(("value", "enabled"), [
    ("true", True), ("TRUE", True), ("True", True), (" true ", True), ("\ttrue\n", True),
    ("", False), ("false", False), ("1", False), ("yes", False), ("on", False), ("enabled", False),
    ("true1", False), ("t rue", False), ("'true'", False),
])
def test_only_true_in_any_case_turns_replacement_on(monkeypatch, value, enabled):
    monkeypatch.setenv(VARIABLE, value)

    assert flat.replacement_enabled() is enabled
    assert packaged.replacement_enabled() is enabled


def test_unset_is_off(monkeypatch):
    monkeypatch.delenv(VARIABLE, raising=False)

    assert flat.replacement_enabled() is False and packaged.replacement_enabled() is False


def test_it_is_read_on_each_call_not_once(monkeypatch):
    monkeypatch.delenv(VARIABLE, raising=False)
    assert flat.replacement_enabled() is False
    monkeypatch.setenv(VARIABLE, "true")
    assert flat.replacement_enabled() is True
    monkeypatch.setenv(VARIABLE, "false")
    assert flat.replacement_enabled() is False


def test_the_variable_and_the_refusal_code_are_the_documented_ones():
    assert flat.ENABLED_VARIABLE == packaged.ENABLED_VARIABLE == VARIABLE
    assert flat.REPLACEMENT_DISABLED == service.REPLACEMENT_DISABLED == "replacement_disabled"


def test_authorization_while_off_is_refused_before_the_database_is_touched(monkeypatch):
    monkeypatch.delenv(VARIABLE, raising=False)

    def no_database():
        raise AssertionError("the database was touched")

    monkeypatch.setattr(service, "get_engine", no_database)
    library = Library(id=1, slug="acme", name="Acme Library", status="active")

    with pytest.raises(InstallationAdminError) as refused:
        service.authorize_replacement(library, 101, name="Replacement", confirm_lost_response=False, actor={"id": 7})

    assert refused.value.code == "replacement_disabled"
