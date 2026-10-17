"""The legacy Admin Settings lock credential functions (services.admin_lock_service), and no tracked settings file
carries an admin password.

The lock was an extra "admin password" prompt on the customer Streamlit Admin Settings page, which A5 retired. The
service and its migration tooling stay until the legacy `security` blocks are removed from the database; until then
these pin what they do: only a salted one-way hash is stored, wrong passwords fail and correct ones work, a legacy
plaintext value is still recognised, and the displayable view never holds the password or its hash.

Every value is a SYNTHETIC canary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services import admin_lock_service as lock

ROOT = Path(__file__).resolve().parent.parent

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
# 2. No tracked settings file carries an admin password
# =====================================================================================================================

def test_no_tracked_settings_file_carries_an_admin_password():
    offenders = []
    for base in (ROOT / "src",):
        for path in base.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if "admin_password" in text or (isinstance(data, dict) and "security" in data):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
