"""Request and response models for the customer API's auth routes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class LoginRequest(BaseModel):
    """Body of POST /api/auth/login. An unexpected field is a validation
    error, never silently dropped -- a request cannot carry a tenant
    (customer_id / branch_id) or any other value this route does not name.
    The password is a SecretStr, so it is masked in reprs and therefore in
    any error-tracker frame locals."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=1024)


class CurrentUserResponse(BaseModel):
    """The authenticated user, as the browser may see them. app_users.full_name
    is NOT NULL (it defaults to an empty string), so it is always a string."""

    model_config = ConfigDict(extra="forbid")

    id: int
    email: str
    full_name: str
