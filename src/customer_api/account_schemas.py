"""Request and response models for the customer API's account routes.

Every request model refuses a field it does not name, so a request cannot
carry an email to change, a user id, a role or anything else these routes do
not take. Passwords and the reset token are SecretStr: masked in reprs, and so
in any error-tracker frame locals.

The length limits here only bound what is read. What makes a name or a
password acceptable is decided by services.auth_service, which answers with a
stable code the routes turn into the 422 problem list.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# The most a password field or token is read: generous, and far past anything real.
_SECRET_MAX_LENGTH = 1024


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountResponse(BaseModel):
    """The signed-in user's own account, as the browser may see it.

    `email` is how they sign in and is read-only here. `full_name` is the
    name they have given (it may be an empty string for an account that was
    created without one). The two timestamps are instants written in UTC
    (the route normalizes them, whatever zone the database session is in),
    or null when it has never happened: `last_login_at` is the last successful
    sign-in in either application, and `last_password_changed_at` the last
    time the password was changed or reset.

    No id, role, organization, password hash, lockout state or administrator
    flag has a field here."""

    model_config = ConfigDict(extra="forbid")

    email: str
    full_name: str
    last_login_at: dt.datetime | None
    last_password_changed_at: dt.datetime | None


class ProfileUpdateRequest(_Request):
    """Body of PUT /api/account/profile. The name is the only thing that can
    be changed."""

    full_name: str = Field(max_length=2000)


class ChangePasswordRequest(_Request):
    """Body of POST /api/account/change-password."""

    current_password: SecretStr = Field(max_length=_SECRET_MAX_LENGTH)
    new_password: SecretStr = Field(max_length=_SECRET_MAX_LENGTH)
    confirm_password: SecretStr = Field(max_length=_SECRET_MAX_LENGTH)


class PasswordResetRequest(_Request):
    """Body of POST /api/auth/password-reset/request."""

    email: str = Field(min_length=1, max_length=320)


class PasswordResetCompleteRequest(_Request):
    """Body of POST /api/auth/password-reset/complete. `token` is the value
    from the emailed link."""

    token: SecretStr = Field(min_length=1, max_length=512)
    new_password: SecretStr = Field(max_length=_SECRET_MAX_LENGTH)
    confirm_password: SecretStr = Field(max_length=_SECRET_MAX_LENGTH)
