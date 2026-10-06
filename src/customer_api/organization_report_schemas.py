"""Response models for the customer API's organization-level range reports.

The rules of customer_api.report_schemas hold here unchanged: every figure is
an INTEGER COUNT and nothing derived has a field -- no rate, average, share
or ranking -- and every list is FULL LENGTH with explicit zeros.

An organization's counts are PROCESSING EVENTS added up across its sorter
sites. They are not distinct items: an item two of the organization's sorters
each processed is counted at both.

A rate for the organization is one total over another -- `reject_count` over
`checkin_count`, both from `totals` -- and never the average of the sorters'
own rates. That is why the sorters' counts are here and their rates are not.

No tenant, customer, branch or installation id, no stored destination value
and no reject message has a field here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from customer_api.operational_schemas import RoutingDestination, RoutingHome
from customer_api.organization_schemas import SorterHostBranch, SorterStatus
from customer_api.report_schemas import (
    OverviewDay,
    ReliabilityDay,
    ReliabilityReason,
    ReportRange,
)


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SorterIdentity(_ResponseModel):
    """Which of the organization's sorter sites a row is about: the `slug`,
    `name` and `host_branch` the organization's own `sorters` list gives."""

    slug: str
    name: str
    host_branch: SorterHostBranch


# --- overview --------------------------------------------------------------------------------------------------------

class OrganizationOverviewTotals(_ResponseModel):
    """The sorters' counts added up. `home_count + transit_count +
    other_count` is exactly `checkin_count`."""

    checkin_count: int
    home_count: int
    transit_count: int
    other_count: int
    reject_count: int


class OrganizationSorterOverview(_ResponseModel):
    """One sorter site's headline counts, beside what the organization's
    `sorters` list says about it. `available` is false for a sorter that is
    registered but has no operational data scope yet: its counts are then
    zero and it adds nothing to the totals. `active_days` is the number of
    dates in the range on which THIS sorter had a check-in."""

    slug: str
    name: str
    host_branch: SorterHostBranch
    status: SorterStatus
    collector_count: int
    available: bool
    checkin_count: int
    active_days: int
    transit_count: int
    reject_count: int


class OrganizationOverviewResponse(_ResponseModel):
    """An organization's headline counts over a range. `sorters` is the
    organization's sorter sites in the order its `sorters` list has them.
    `days` has one entry per date in the range, in order, each the sum of the
    sorters' counts for that date."""

    range: ReportRange
    totals: OrganizationOverviewTotals
    sorters: list[OrganizationSorterOverview]
    days: list[OverviewDay]


# --- routing network -------------------------------------------------------------------------------------------------

class RoutingNetworkTotals(_ResponseModel):
    checkin_count: int
    transit_count: int


class RoutingNetworkSource(_ResponseModel):
    """One sorter site and where it routed its check-ins: `home`, each of ITS
    configured destinations in its own configured order (zero where there
    were none), and everything else as `other_count`. The four add up to
    `checkin_count`."""

    sorter: SorterIdentity
    checkin_count: int
    home: RoutingHome
    transit_count: int
    other_count: int
    transit: list[RoutingDestination]


class RoutingNetworkDestination(_ResponseModel):
    """Check-ins routed to one destination `key`, added up across the sources
    that have it configured. `source_count` is how many sources those are,
    whether or not they routed anything there. `label` is the first such
    source's label for it.

    Two sources share an entry because their keys are equal, and that is all
    it means. A destination is a routing outcome: nothing here says a branch,
    a site or a sorter exists by that key, and it is never one of `sources`."""

    key: str
    label: str
    checkin_count: int
    source_count: int


class RoutingNetworkResponse(_ResponseModel):
    """Where an organization's sorter sites routed their check-ins over a
    range. `sources` has one entry per sorter site that has data to read, in
    the organization's order; each source's `transit` is a row of the
    source-by-destination picture. `destinations` adds those rows up by key
    and its counts add up to `totals.transit_count`."""

    range: ReportRange
    totals: RoutingNetworkTotals
    sources: list[RoutingNetworkSource]
    destinations: list[RoutingNetworkDestination]


# --- reliability -----------------------------------------------------------------------------------------------------

class OrganizationReliabilityTotals(_ResponseModel):
    """The sorters' counts added up. `reasons` always has the eight public
    reason codes in their fixed order and adds up to `reject_count`."""

    checkin_count: int
    reject_count: int
    reasons: list[ReliabilityReason]


class OrganizationSorterReliability(_ResponseModel):
    """One sorter site's rejects beside its check-ins, by reason. `available`
    is false, and every count zero, for a sorter with no operational data
    scope yet."""

    sorter: SorterIdentity
    available: bool
    checkin_count: int
    reject_count: int
    reasons: list[ReliabilityReason]


class OrganizationReliabilityResponse(_ResponseModel):
    """An organization's rejects over a range. `days` has one entry per date
    in the range, in order, each the sum of the sorters' check-ins and
    rejects for that date. Rejects are not a subset of check-ins."""

    range: ReportRange
    totals: OrganizationReliabilityTotals
    sorters: list[OrganizationSorterReliability]
    days: list[ReliabilityDay]
