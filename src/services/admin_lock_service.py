"""The Admin Settings "lock": a per-organization password an owner/admin must enter before the settings form.

It is NOT the login system (app_users has its own hashed passwords, roles and lockout) -- it is an extra prompt on
the Admin Settings page, kept in the organization's `settings_json` under `security`. It used to be stored, cached and
sent to the browser in PLAINTEXT (`security.admin_password`). Now:

  * only a salted one-way hash (`security.admin_password_hash`, werkzeug -- the same scheme as app_users) is stored;
  * nothing here can produce the password from what is stored, and the settings form never pre-fills it;
  * a legacy plaintext value is still ACCEPTED for verification, so no organization is locked out at deploy time, and
    it is replaced by a hash of the same value the next time an admin saves settings;
  * `without_security` keeps the whole block out of the process-wide settings cache.

Pure functions, no Streamlit and no database: the page and the settings service call them.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

HASH_KEY = "admin_password_hash"
# Written by older versions of the settings page; read only, and only to keep existing organizations working.
LEGACY_PLAINTEXT_KEY = "admin_password"


def hash_admin_password(password: str) -> str:
    """Salted one-way hash (werkzeug's default scheme, as for app_users). Not reversible."""
    return generate_password_hash(password)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def has_password_hash(security: Mapping[str, Any] | None) -> bool:
    return bool(_text((security or {}).get(HASH_KEY)))


def is_legacy_plaintext(security: Mapping[str, Any] | None) -> bool:
    """True when a plaintext password is stored and no hash has replaced it yet."""
    security = security or {}
    return bool(_text(security.get(LEGACY_PLAINTEXT_KEY))) and not has_password_hash(security)


def has_admin_password(security: Mapping[str, Any] | None) -> bool:
    return has_password_hash(security) or is_legacy_plaintext(security)


def verify_admin_password(entered: str, security: Mapping[str, Any] | None) -> bool:
    """Whether `entered` matches the organization's admin password. False when none is set (the caller decides what
    "no password" means) or when `entered` is empty."""
    security = security or {}
    if not entered:
        return False
    stored_hash = _text(security.get(HASH_KEY))
    if stored_hash:
        try:
            return check_password_hash(stored_hash, entered)
        except (ValueError, TypeError):
            return False  # a malformed hash never authenticates anyone
    legacy = _text(security.get(LEGACY_PLAINTEXT_KEY))
    if legacy:
        return hmac.compare_digest(entered.encode("utf-8"), legacy.encode("utf-8"))
    return False


def build_security_settings(
    *,
    enabled: bool,
    new_password: str,
    remove_password: bool,
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The `security` block to store. It never contains a plaintext password.

    - `remove_password`: no password is stored.
    - a non-empty `new_password`: replaced by a hash of it.
    - otherwise the current password is kept: an existing hash as it is, and a legacy plaintext value converted to a
      hash of the same value, so saving settings never silently removes the protection.
    """
    current = current or {}
    stored_hash = ""
    if not remove_password:
        if new_password:
            stored_hash = hash_admin_password(new_password)
        elif has_password_hash(current):
            stored_hash = _text(current.get(HASH_KEY))
        elif is_legacy_plaintext(current):
            stored_hash = hash_admin_password(_text(current.get(LEGACY_PLAINTEXT_KEY)))

    block: dict[str, Any] = {"admin_enabled": bool(enabled)}
    if stored_hash:
        block[HASH_KEY] = stored_hash
    return block


def public_security_view(security: Mapping[str, Any] | None) -> dict[str, Any]:
    """What may be displayed: whether the lock is on and whether a password is set -- never the password or its hash."""
    security = security or {}
    return {
        "admin_enabled": bool(security.get("admin_enabled", True)),
        "admin_password_set": has_admin_password(security),
    }


def without_security(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """A copy of `settings` without the `security` block, for anything that is cached or handed to the dashboard."""
    return {key: value for key, value in (settings or {}).items() if key != "security"}
