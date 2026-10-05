"""Response models for the customer API's organization routes.

Every model lists exactly the fields a browser may see. The service rows
these are built from also carry internal and OPERATIONAL identifiers
(organization_id, customer_id, the branch's database id and its operational
branch_id, subscription and plan ids); none of them has a field here, so none
can be returned. The browser identifies an organization and a branch by slug
only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# "blocked" is deliberately not a value: a blocked organization is never
# returned at all.
AccessMode = Literal["full", "read_only"]


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrganizationSummary(_ResponseModel):
    slug: str
    name: str
    role: str
    access_mode: AccessMode


class BranchSummary(_ResponseModel):
    slug: str
    name: str
    is_primary: bool


class SubscriptionSummary(_ResponseModel):
    plan_code: str
    plan_name: str
    status: str


class FeatureEntitlement(_ResponseModel):
    enabled: bool
    limit_value: int | None


class OrganizationDetail(OrganizationSummary):
    branches: list[BranchSummary]
    subscription: SubscriptionSummary | None
    entitlements: dict[str, FeatureEntitlement]
