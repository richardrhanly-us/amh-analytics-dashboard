"""Range reports for a whole organization: its sorter sites' reports, added up.

    get_organization_overview(org_slug, local_range, resolve_site=..., open_site=...)
    get_organization_routing_network(...)
    get_organization_reliability(...)

WHAT IS ADDED UP. An organization's figures are PROCESSING EVENTS across its
sorter sites -- the sites services.sorter_inventory_service lists. An item
one sorter processes and another sorter processes later is two events, at two
sites, and is counted twice here on purpose: nothing in this module is a
count of distinct items. A site is one host branch, whatever number of
collectors report for it, so it is read once and counted once.

HOW IT IS READ: ONE SITE AT A TIME, EACH UNDER ITS OWN SCOPE. For every
listed site this module asks `resolve_site` for the operational tenant the
user may read, has `open_site` open a connection scoped to exactly that
tenant by row level security, and runs services.operational_report_service's
own site reads on it. The integers are then added up in Python. There is no
statement here that spans two sites, no scope wider than one site is ever
set, and no report statement of this module's own: every count is one the
site's own report gives.

ALL OR NOTHING. A report is built only after every site has been read. A
read that fails -- a database error, a scope that could not be verified, a
site whose answer does not have the shape of the range -- propagates, and
there is then no report at all: never the sum of the sites that happened to
work.

A SITE THAT IS LISTED BUT HAS NO OPERATIONAL SCOPE is the one exception, and
it is not a failure: a sorter can be registered at a branch before the
organization or that branch has been given the operational id its data is
kept under. Such a site has no data to read and nothing to add. It is
reported as NOT AVAILABLE with zeros. It is told apart from everything else
by looking, not by guessing: when a listed site does not resolve, one
statement reads whether the organization and that branch each have an
operational id. Only "one such active branch, and an id is missing" makes a
site unavailable. Any other reason a listed site does not resolve is
SiteNotResolvedError, and the report fails.

ONE RANGE FOR EVERY SITE. The caller supplies one LocalRange -- the dates and
the product's zone -- and every site is read over exactly that range, so the
sites' days line up by construction; each answer's length is checked against
it all the same. Which era owns which part of the range is each site's own
business (operational_report_service.report_window); nothing about time is
decided here.

Framework-neutral: no Streamlit, no FastAPI, no pandas, no caching. Counts
only: no rate, average or share is computed.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy import text
from sqlalchemy.engine import Connection

from database import get_engine
from services.operational_report_service import (
    DestinationCounts,
    LocalRange,
    get_overview_report,
    get_reliability_report,
    get_routing_report,
    report_window,
)
from services.reject_reason import REJECT_REASONS
from services.routing_config_service import get_routing_config
from services.routing_destination import TransitDestination
from services.sorter_inventory_service import SorterSite, list_sorter_sites
from services.tenant_resolution_service import ResolvedOperationalTenant

# The operational tenant the user may read for the site hosted at this branch slug, or None.
SiteResolver = Callable[[str], ResolvedOperationalTenant | None]
# A connection scoped to exactly that tenant, with the scope verified.
SiteOpener = Callable[[ResolvedOperationalTenant], AbstractContextManager[Connection]]

T = TypeVar("T")


class OrganizationReportError(RuntimeError):
    """An organization report could not be built whole. An internal fault,
    never a client error. Its message is fixed and carries no id or slug."""


class SiteNotResolvedError(OrganizationReportError):
    """A listed sorter site that should have an operational scope did not
    resolve to one. Never answered as "unavailable"."""

    def __init__(self) -> None:
        super().__init__("A listed sorter site did not resolve to an operational scope.")


def _require(condition: bool, problem: str) -> None:
    if not condition:
        raise OrganizationReportError(problem)


# =====================================================================================================================
# Reading every site, each under its own scope
# =====================================================================================================================

# Whether the organization and one of its active branches each have an
# operational id. Two booleans: no id is selected. LIMIT 2, not 1: the schema
# makes a second row impossible, and one would not be read as "unmapped".
_SITE_MAPPING_SQL = text("""
    SELECT
        (o.operational_customer_id IS NOT NULL) AS organization_is_mapped,
        (b.operational_branch_id IS NOT NULL) AS branch_is_mapped
    FROM organizations o
    JOIN branches b
      ON b.organization_id = o.id
    WHERE o.slug = :org_slug
      AND b.slug = :branch_slug
      AND b.status = 'active'
    LIMIT 2
""")


def site_has_no_operational_scope(org_slug: str, branch_slug: str) -> bool:
    """True only when the organization has exactly one active branch with
    this slug and the organization or that branch has no operational id: the
    one case in which a listed sorter site legitimately has nothing to read.
    One statement runs; database errors propagate."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(_SITE_MAPPING_SQL, {"org_slug": org_slug, "branch_slug": branch_slug}).mappings().all()

    if len(rows) != 1:
        return False
    return not (bool(rows[0]["organization_is_mapped"]) and bool(rows[0]["branch_is_mapped"]))


@dataclass(frozen=True, slots=True)
class SiteRead(Generic[T]):
    """One listed sorter site and what was read for it. `report` is None for
    a site with no operational scope -- and for no other reason."""

    site: SorterSite
    report: T | None

    @property
    def available(self) -> bool:
        return self.report is not None


def read_sorter_sites(
    org_slug: str,
    read_site: Callable[[Connection, ResolvedOperationalTenant], T],
    *,
    resolve_site: SiteResolver,
    open_site: SiteOpener,
) -> tuple[SiteRead[T], ...]:
    """`read_site`'s answer for every sorter site of the organization, in
    services.sorter_inventory_service's order. Each site is resolved, opened
    and read on its own; the first failure ends the whole read.

    The caller has already established that the user may see the
    organization. `resolve_site` still decides, site by site, what that user
    may read -- this function never obtains a scope any other way.
    """
    reads: list[SiteRead[T]] = []
    scopes_read: set[tuple[int, int]] = set()

    for site in list_sorter_sites(org_slug):
        tenant = resolve_site(site.host_branch_slug)
        if tenant is None:
            if not site_has_no_operational_scope(org_slug, site.host_branch_slug):
                raise SiteNotResolvedError()
            reads.append(SiteRead(site=site, report=None))
            continue

        # Two listed sites are two host branches. If both resolved to one operational scope, the same rows
        # would be added up twice: refuse rather than report a total that is too large.
        scope = (tenant.operational_customer_id, tenant.operational_branch_id)
        _require(scope not in scopes_read, "Two listed sorter sites resolved to the same operational scope.")
        scopes_read.add(scope)

        with open_site(tenant) as conn:
            report = read_site(conn, tenant)
        reads.append(SiteRead(site=site, report=report))

    return tuple(reads)


def _add(totals: list[int], counts: tuple[int, ...]) -> None:
    """Adds `counts` to `totals`, entry by entry. Both must have the same length."""
    _require(len(counts) == len(totals), "A sorter site's report does not have the shape that was asked for.")
    for index, count in enumerate(counts):
        totals[index] += count


# =====================================================================================================================
# Overview
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class SorterOverview:
    """One sorter site's headline counts. All zero when not `available`."""

    site: SorterSite
    available: bool
    checkin_count: int
    active_days: int
    transit_count: int
    reject_count: int


@dataclass(frozen=True, slots=True)
class OrganizationOverview:
    """An organization's headline counts over a range: each sorter site's,
    and their sums. `checkin_days` and `reject_days` have one entry per day
    of the range. `home_count + transit_count + other_count` is
    `checkin_count`."""

    sorters: tuple[SorterOverview, ...]
    home_count: int
    transit_count: int
    other_count: int
    reject_count: int
    checkin_days: tuple[int, ...]
    reject_days: tuple[int, ...]

    @property
    def checkin_count(self) -> int:
        return self.home_count + self.transit_count + self.other_count


def get_organization_overview(
    org_slug: str, local_range: LocalRange, *, resolve_site: SiteResolver, open_site: SiteOpener
) -> OrganizationOverview:
    """The organization's overview over `local_range`: every site's own
    overview report (operational_report_service.get_overview_report), added
    up."""

    def read_site(conn: Connection, tenant: ResolvedOperationalTenant):
        routing = get_routing_config(conn, tenant)
        return get_overview_report(conn, tenant, report_window(conn, tenant, local_range), routing)

    reads = read_sorter_sites(org_slug, read_site, resolve_site=resolve_site, open_site=open_site)

    sorters = []
    home_count = transit_count = other_count = 0
    checkin_days = [0] * local_range.days
    reject_days = [0] * local_range.days
    for read in reads:
        report = read.report
        if report is None:
            sorters.append(SorterOverview(read.site, False, 0, 0, 0, 0))
            continue
        _add(checkin_days, report.checkin_days)
        _add(reject_days, report.reject_days)
        home_count += report.routing.home_count
        transit_count += report.routing.transit_count
        other_count += report.routing.other_count
        sorters.append(SorterOverview(
            site=read.site,
            available=True,
            checkin_count=report.checkin_count,
            active_days=report.active_days,
            transit_count=report.routing.transit_count,
            reject_count=report.reject_count,
        ))

    overview = OrganizationOverview(
        sorters=tuple(sorters),
        home_count=home_count,
        transit_count=transit_count,
        other_count=other_count,
        reject_count=sum(reject_days),
        checkin_days=tuple(checkin_days),
        reject_days=tuple(reject_days),
    )
    _require(
        overview.checkin_count == sum(checkin_days) == sum(sorter.checkin_count for sorter in sorters)
        and overview.reject_count == sum(sorter.reject_count for sorter in sorters),
        "An organization overview's parts do not add up to its totals.",
    )
    return overview


# =====================================================================================================================
# Routing network
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class RoutingSource:
    """One sorter site's check-ins by where IT routed them. `transit` is that
    site's own configured destinations, in its own order, and
    `counts.transit_counts` lines up with it."""

    site: SorterSite
    home_label: str
    transit: tuple[TransitDestination, ...]
    counts: DestinationCounts


@dataclass(frozen=True, slots=True)
class NetworkDestination:
    """Check-ins routed to one destination KEY, across every source that has
    it configured. Sources share an entry because their keys are equal --
    which says only that. It does not say the sources mean the same place,
    and it never says a branch, a site or a sorter exists by that key.

    `label` is the label of the first source, in listed order, that has the
    key. `source_count` is how many sources have the key among their
    configured destinations, whether or not they routed anything to it."""

    key: str
    label: str
    checkin_count: int
    source_count: int


@dataclass(frozen=True, slots=True)
class OrganizationRoutingNetwork:
    """Where an organization's sorter sites routed their check-ins. `sources`
    has one entry per site that has an operational scope, in listed order;
    `destinations` has one entry per destination key any of them has
    configured, in the order the keys are first met."""

    sources: tuple[RoutingSource, ...]
    destinations: tuple[NetworkDestination, ...]

    @property
    def checkin_count(self) -> int:
        return sum(source.counts.total for source in self.sources)

    @property
    def transit_count(self) -> int:
        return sum(source.counts.transit_count for source in self.sources)


def get_organization_routing_network(
    org_slug: str, local_range: LocalRange, *, resolve_site: SiteResolver, open_site: SiteOpener
) -> OrganizationRoutingNetwork:
    """The organization's routing network over `local_range`: every site's
    own routing report (operational_report_service.get_routing_report) as a
    source, and the sources' destinations added up by key.

    A site with no operational scope has no configuration and no check-ins to
    read, and is not a source. "Other" stays each source's own `other_count`:
    it is never given a key or a place among the destinations.
    """

    def read_site(conn: Connection, tenant: ResolvedOperationalTenant):
        routing = get_routing_config(conn, tenant)
        return routing, get_routing_report(conn, tenant, report_window(conn, tenant, local_range), routing)

    reads = read_sorter_sites(org_slug, read_site, resolve_site=resolve_site, open_site=open_site)

    sources = []
    # key -> [label of the first source that has it, check-ins, sources that have it]. A dict keeps first-met order.
    by_key: dict[str, list] = {}
    for read in reads:
        if read.report is None:
            continue
        routing, report = read.report
        _require(
            len(report.days) == local_range.days and len(report.total.transit_counts) == len(routing.transit),
            "A sorter site's report does not have the shape that was asked for.",
        )
        sources.append(RoutingSource(
            site=read.site, home_label=routing.home_label, transit=routing.transit, counts=report.total,
        ))
        for destination, count in zip(routing.transit, report.total.transit_counts, strict=True):
            entry = by_key.setdefault(destination.key, [destination.label, 0, 0])
            entry[1] += count
            entry[2] += 1

    network = OrganizationRoutingNetwork(
        sources=tuple(sources),
        destinations=tuple(
            NetworkDestination(key=key, label=label, checkin_count=count, source_count=source_count)
            for key, (label, count, source_count) in by_key.items()
        ),
    )
    _require(
        sum(destination.checkin_count for destination in network.destinations) == network.transit_count,
        "An organization routing network's destinations do not add up to its transit total.",
    )
    return network


# =====================================================================================================================
# Reliability
# =====================================================================================================================

@dataclass(frozen=True, slots=True)
class SorterReliability:
    """One sorter site's rejects beside its check-ins. `reason_counts` has
    one entry per reason, in services.reject_reason.REJECT_REASONS' order,
    and adds up to `reject_count`. All zero when not `available`."""

    site: SorterSite
    available: bool
    checkin_count: int
    reject_count: int
    reason_counts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class OrganizationReliability:
    """An organization's rejects over a range: each sorter site's, and their
    sums, by reason and by day. Rejects are not a subset of check-ins."""

    sorters: tuple[SorterReliability, ...]
    reason_counts: tuple[int, ...]
    checkin_days: tuple[int, ...]
    reject_days: tuple[int, ...]

    @property
    def checkin_count(self) -> int:
        return sum(self.checkin_days)

    @property
    def reject_count(self) -> int:
        return sum(self.reason_counts)


def get_organization_reliability(
    org_slug: str, local_range: LocalRange, *, resolve_site: SiteResolver, open_site: SiteOpener
) -> OrganizationReliability:
    """The organization's reliability report over `local_range`: every
    site's own (operational_report_service.get_reliability_report), added up
    reason by reason and day by day."""

    def read_site(conn: Connection, tenant: ResolvedOperationalTenant):
        return get_reliability_report(conn, tenant, report_window(conn, tenant, local_range))

    reads = read_sorter_sites(org_slug, read_site, resolve_site=resolve_site, open_site=open_site)

    sorters = []
    reason_counts = [0] * len(REJECT_REASONS)
    checkin_days = [0] * local_range.days
    reject_days = [0] * local_range.days
    for read in reads:
        report = read.report
        if report is None:
            sorters.append(SorterReliability(read.site, False, 0, 0, (0,) * len(REJECT_REASONS)))
            continue
        _add(reason_counts, report.rejects.reason_counts)
        _add(checkin_days, report.checkin_days)
        _add(reject_days, report.rejects.day_counts)
        sorters.append(SorterReliability(
            site=read.site,
            available=True,
            checkin_count=report.checkin_count,
            reject_count=report.reject_count,
            reason_counts=report.rejects.reason_counts,
        ))

    reliability = OrganizationReliability(
        sorters=tuple(sorters),
        reason_counts=tuple(reason_counts),
        checkin_days=tuple(checkin_days),
        reject_days=tuple(reject_days),
    )
    _require(
        reliability.reject_count == sum(reject_days) == sum(sorter.reject_count for sorter in sorters)
        and reliability.checkin_count == sum(sorter.checkin_count for sorter in sorters),
        "An organization reliability report's parts do not add up to its totals.",
    )
    return reliability
