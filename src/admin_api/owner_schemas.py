"""Request model for assigning a library's first owner in the administration API (G6).

Closed (extra="forbid"). There is NO password field: an account created by this request gets a random password nobody
is told, and the person sets their own through password reset. The address is sent twice, as the admin form asks for
it twice, and the route refuses the request unless both are the same address.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class AssignFirstOwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: StrictStr = Field(min_length=1, max_length=320)
    email_confirmation: StrictStr = Field(min_length=1, max_length=320)
    full_name: StrictStr = Field(default="", max_length=120)
