"""Request and response models for provisioning a library in the administration API (A2 item 7).

Closed (extra="forbid") both ways. A library, its primary branch and a plan are named by slug or code and name; no id
of any kind -- organization, branch, customer, operational, plan -- is a field here, and nothing about contacts, notes,
users, installations, codes, tokens or keys.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Plan(_Model):
    code: str
    name: str


class PlansResponse(_Model):
    """Active plans only."""

    plans: list[Plan]


class ProvisionLibraryRequest(_Model):
    """Names are trimmed; slugs are taken exactly as sent and must already be in slug form."""

    name: StrictStr
    slug: StrictStr
    branch_name: StrictStr
    branch_slug: StrictStr
    plan_code: StrictStr


class ProvisionedLibrary(_Model):
    slug: str
    name: str
    status: Literal["active"]


class ProvisionedBranch(_Model):
    slug: str
    name: str
    status: Literal["active"]


class ProvisionedSubscription(_Model):
    plan_code: str
    plan_name: str
    status: Literal["trial"]


class ProvisionLibraryResponse(_Model):
    library: ProvisionedLibrary
    primary_branch: ProvisionedBranch
    subscription: ProvisionedSubscription
    operational_identity: Literal["assigned"]


class OperationalIdentityResponse(_Model):
    """`changed` is false when the identity was already complete; nothing was written then."""

    operational_identity: Literal["assigned"]
    changed: bool
