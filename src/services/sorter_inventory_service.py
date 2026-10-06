"""An organization's SortView sorting machines, for the customer API.

    list_sorter_sites(org_slug) -> [SorterSite(slug, name, host_branch_slug, host_branch_name, status, collector_count)]

WHAT IS LISTED. The machines an organization actually runs SortView on --
taken from collector_installations, the one place a deployed Collector is
registered -- and nothing else. A branch with no installation is not a
sorter. A routing destination is not a sorter: it is a value on another
sorter's check-ins (services.routing_destination), whether or not the place
it names also hosts a sorter of its own.

ONE ENTRY PER HOST BRANCH. Every operational table, the pipeline status and
the v1 -> v2 cutover are keyed by (customer, branch): the data of two
Collectors registered at one branch cannot be told apart. So the unit that
can honestly be shown, and opened as a dashboard, is the SORTER SITE -- the
installations of one host branch, together:

    slug             the host branch's slug. Unique within the organization, stable when a machine is renamed,
                     and exactly the scope the operational reads take.
    name             the name of the site's lead installation (see below), as an administrator wrote it
    status           that installation's status
    collector_count  how many of the site's installations can currently report (provisioning or active).
                     More than one means their figures are combined and cannot be separated.

Telling two machines at one branch apart needs the events themselves to say
which installation produced them. That is deliberately not attempted here.

WHICH INSTALLATIONS COUNT.

    active         shown: the machine is reporting
    provisioning   shown: registered, not yet heard from
    inactive       shown: switched off by an administrator; what it reported is still the site's history
    retired        never shown: decommissioned

and only when the host branch is itself active -- the same rule as the
organization's branch list -- and belongs to the installation's own
organization. A site's LEAD installation is its most alive one (active, then
provisioning, then inactive), the earliest registered among equals, so a
replaced machine left "inactive" beside its "active" replacement does not
name the site.

WHAT IS NOT RETURNED. No installation id, hostname or Collector version, no
token or enrollment data, and no organization, customer or branch id: none is
selected.

The caller has already established that the user may see the organization;
this module makes no access decision. Framework-neutral: no Streamlit, no
FastAPI. Database errors propagate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from database import get_engine

# Most alive first: the order a site's lead installation is chosen in.
VISIBLE_STATUSES = ("active", "provisioning", "inactive")
# The statuses in which an installation can report at all (main.INSTALLATION_HEARTBEAT_STATUSES).
REPORTING_STATUSES = ("provisioning", "active")

_STATUS_RANK = {status: rank for rank, status in enumerate(VISIBLE_STATUSES)}


@dataclass(frozen=True, slots=True)
class SorterSite:
    """One sorter site of an organization: see the module docstring."""

    slug: str
    name: str
    host_branch_slug: str
    host_branch_name: str
    status: str
    collector_count: int


def sorter_sites(installations: Iterable[Mapping[str, Any]]) -> list[SorterSite]:
    """The sorter sites in `installations` -- rows with name, status,
    branch_slug and branch_name, in the order the sites should be listed.

    Rows of one branch become one site, placed where that branch first
    appears. A row whose status is not a visible one is ignored, wherever it
    came from. Pure: nothing is read.
    """
    by_branch: dict[str, list[Mapping[str, Any]]] = {}
    for row in installations:
        if row["status"] in _STATUS_RANK:
            by_branch.setdefault(row["branch_slug"], []).append(row)

    sites = []
    for branch_slug, rows in by_branch.items():
        # min() keeps the first of equals, and the rows arrive earliest-registered first.
        lead = min(rows, key=lambda row: _STATUS_RANK[row["status"]])
        sites.append(SorterSite(
            slug=branch_slug,
            name=(lead["name"] or "").strip() or lead["branch_name"],
            host_branch_slug=branch_slug,
            host_branch_name=lead["branch_name"],
            status=lead["status"],
            collector_count=sum(1 for row in rows if row["status"] in REPORTING_STATUSES),
        ))
    return sites


# The organization is named by slug; the branch must be the installation's own
# organization's (a row pointing at another organization's branch matches
# nothing) and active. Ordered like the organization's branch list -- primary
# branch first, then by name -- and, within a branch, earliest registered first.
_INSTALLATIONS_SQL = text("""
    SELECT
        ci.name AS name,
        ci.status AS status,
        b.slug AS branch_slug,
        b.name AS branch_name
    FROM collector_installations ci
    JOIN organizations o
      ON o.id = ci.organization_id
    JOIN branches b
      ON b.id = ci.branch_id
     AND b.organization_id = o.id
    WHERE o.slug = :org_slug
      AND b.status = 'active'
      AND ci.status IN ('active', 'provisioning', 'inactive')
    ORDER BY b.is_primary DESC, b.name ASC, b.id ASC, ci.id ASC
""")


def list_sorter_sites(org_slug: str) -> list[SorterSite]:
    """The sorter sites of the organization with this slug, or an empty list
    if it has none (or does not exist). One statement runs."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(_INSTALLATIONS_SQL, {"org_slug": org_slug}).mappings().all()
    return sorter_sites(rows)
