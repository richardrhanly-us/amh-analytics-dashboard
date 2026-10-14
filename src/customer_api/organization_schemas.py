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


class SorterHostBranch(_ResponseModel):
    """The branch a sorter is installed at. Where the machine is -- not what it sorts for."""

    slug: str
    name: str


# "retired" is deliberately not a value: a retired machine is never returned at all.
SorterStatus = Literal["active", "provisioning", "inactive"]

# The host branch's setup progress, as services.branch_readiness.normalize_onboarding_status presents it: "ready"
# once its first data has arrived, "pending" before. Whatever else is stored is "pending"; nothing is passed through.
OnboardingStatus = Literal["pending", "ready"]


class SorterSummary(_ResponseModel):
    """One SortView sorter site of the organization (services.sorter_inventory_service).

    `slug` identifies it within the organization and is what its dashboard's
    reads are addressed by; today it is the host branch's slug. `name` is the
    machine's registered name. `collector_count` is how many collectors can
    report for the site: above one, their figures are combined and cannot be
    separated. `onboarding_status` is the host branch's setup progress: it is
    information, and the site's reads are open whichever it is.

    No installation id, hostname, collector version, credential or onboarding
    message has a field here. A place a sorter merely routes items TO is not a
    sorter and is never in this list."""

    slug: str
    name: str
    host_branch: SorterHostBranch
    status: SorterStatus
    collector_count: int
    onboarding_status: OnboardingStatus


class SubscriptionSummary(_ResponseModel):
    plan_code: str
    plan_name: str
    status: str


class FeatureEntitlement(_ResponseModel):
    enabled: bool
    limit_value: int | None


class OrganizationDetail(OrganizationSummary):
    branches: list[BranchSummary]
    sorters: list[SorterSummary]
    subscription: SubscriptionSummary | None
    entitlements: dict[str, FeatureEntitlement]
