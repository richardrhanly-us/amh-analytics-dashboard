"""Request and response models for a library's lifecycle in the administration API (A2 item 6).

Closed (extra="forbid") both ways. The answer names the library by slug and name, its status before and after, and --
for an offboarding -- how many access artifacts were revoked. Never an id of any kind, an e-mail address, a token, a
code or a key: the rows an offboarding changed are named in its lifecycle evidence, not here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr

LibraryStatus = Literal["active", "trial", "suspended", "cancelled"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LifecycleLibrary(_Model):
    slug: str
    name: str
    status: LibraryStatus


class LifecycleResponse(_Model):
    """`changed` is false when the library was already in the state asked for; nothing was written then."""

    library: LifecycleLibrary
    previous_status: LibraryStatus
    changed: bool


class OffboardCounts(_Model):
    agent_tokens_deactivated: int
    installations_retired: int
    enrollment_codes_revoked: int
    ingest_keys_retired: int
    sessions_revoked: int


class OffboardResponse(LifecycleResponse):
    counts: OffboardCounts


class OffboardRequest(_Model):
    """The library's slug, typed again as confirmation."""

    confirm_slug: StrictStr
