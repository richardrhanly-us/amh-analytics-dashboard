"""Response models for the administration API's libraries (A2 item 2).

Purpose-built and closed (extra="forbid"): only the fields named here can reach the admin app. Never a numeric id of
any kind, an operational id, settings, contact or notes, a hostname or Collector version, or anything of a heartbeat
beyond its state and when it was received -- diagnostics are A2 item 3.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from customer_api.organization_schemas import SubscriptionSummary
from services.admin_library_service import OperationalIdentity
from services.branch_readiness import OnboardingStatus
from services.pipeline_state import PipelineState


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PipelineSummary(_ResponseModel):
    """What a sorter site's collection pipeline last reported (services.pipeline_status_service): the v2 heartbeat
    for a site cut over to Contract v2, the legacy report otherwise. `unknown` with no time: nothing reported, or the
    site has no operational identity to read it under."""

    state: PipelineState
    last_reported_at: datetime | None


class BranchSummary(_ResponseModel):
    slug: str
    name: str
    status: str


class HostBranch(_ResponseModel):
    slug: str
    name: str


class SorterSiteSummary(_ResponseModel):
    """One sorter site, as services.sorter_inventory_service lists it, with its own pipeline status."""

    slug: str
    name: str
    host_branch: HostBranch
    status: str
    collector_count: int
    onboarding_status: OnboardingStatus
    pipeline: PipelineSummary


class PrimarySorter(_ResponseModel):
    """The sorter site at the library's primary branch: the one whose state the list shows. Never a merge of
    several sites' states."""

    onboarding_status: OnboardingStatus
    pipeline: PipelineSummary


class LibrarySummary(_ResponseModel):
    slug: str
    name: str
    status: str
    primary_branch: BranchSummary | None
    subscription: SubscriptionSummary | None
    sorter_count: int
    primary_sorter: PrimarySorter | None


class LibraryListResponse(_ResponseModel):
    """`timezone` is the IANA zone the product shows times in. Every time here is an aware UTC instant."""

    timezone: str
    libraries: list[LibrarySummary]


class LibraryDetailResponse(_ResponseModel):
    timezone: str
    slug: str
    name: str
    status: str
    primary_branch: BranchSummary | None
    operational_identity: OperationalIdentity
    subscription: SubscriptionSummary | None
    sorters: list[SorterSiteSummary]
