"""The libraries a SortView platform administrator sees, for the administration API (A2 item 2).

    list_library_rows()                  -> [LibraryRow]          every organization, cancelled included
    get_library_row(org_slug)            -> LibraryRow | None
    site_tenant(org_slug, branch_slug)   -> ResolvedOperationalTenant | None

WHAT A ROW HOLDS. The organization (name, slug, status), its primary branch (name, slug, status), whether its
operational identity is assigned -- as a word, never the ids -- and its newest subscription and that subscription's
plan. The newest subscription is the one with the latest created_at, the highest id breaking a tie: the ordering
platform_admin_service.list_libraries_with_status uses. No numeric id, settings, contact, notes or pipeline data is in
a row.

    operational_identity   "assigned"  the organization's operational customer AND its primary branch's operational
                                       branch are both set
                           "partial"   exactly one of them is
                           "missing"   neither is (or there is no primary branch and no operational customer)

WHY THERE IS A TENANT LOOKUP HERE. The customer API resolves an operational tenant only for a MEMBER of the
organization (services.tenant_resolution_service). A platform administrator is not one, and is not made one.
site_tenant maps an organization slug and one of its branch slugs to the operational ids its status is stored under,
so the caller can read that status through a connection scoped to exactly that tenant. The ids stay inside the
returned object and are never part of anything this module, or its caller, sends to a browser. It refuses (None) a
branch of another organization, more than one matching row, and a branch or organization with no operational id.

Framework-neutral: no Streamlit, no FastAPI. Every statement is plain ANSI SQL. Database errors propagate. This module
makes no access decision: its caller has established that the user is a platform administrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text

from database import get_engine
from services.access_service import access_mode_for_organization_status
from services.tenant_resolution_service import ResolvedOperationalTenant

OperationalIdentity = Literal["assigned", "partial", "missing"]


@dataclass(frozen=True, slots=True)
class LibraryRow:
    """One organization as a platform administrator sees it: see the module docstring."""

    slug: str
    name: str
    status: str
    primary_branch_slug: str | None
    primary_branch_name: str | None
    primary_branch_status: str | None
    operational_identity: OperationalIdentity
    plan_code: str | None
    plan_name: str | None
    subscription_status: str | None


_LIBRARIES_SQL = """
    SELECT
        o.slug AS slug,
        o.name AS name,
        o.status AS status,
        (o.operational_customer_id IS NOT NULL) AS has_operational_customer,
        b.slug AS primary_branch_slug,
        b.name AS primary_branch_name,
        b.status AS primary_branch_status,
        (b.operational_branch_id IS NOT NULL) AS has_operational_branch,
        s.status AS subscription_status,
        p.code AS plan_code,
        p.name AS plan_name
    FROM organizations o
    LEFT JOIN branches b
      ON b.organization_id = o.id
     AND b.is_primary = TRUE
    LEFT JOIN subscriptions s
      ON s.id = (
          SELECT s2.id
          FROM subscriptions s2
          WHERE s2.organization_id = o.id
          ORDER BY s2.created_at DESC, s2.id DESC
          LIMIT 1
      )
    LEFT JOIN plans p
      ON p.id = s.plan_id
    {where}
    ORDER BY lower(o.name), o.slug, b.id
"""
_LIST_SQL = text(_LIBRARIES_SQL.format(where=""))
_ONE_SQL = text(_LIBRARIES_SQL.format(where="WHERE o.slug = :org_slug"))

# LIMIT 2, not 1: (organization_id, branch slug) is unique, so a second row would mean that guarantee is gone -- and
# that is refused, not resolved to whichever row came first.
_SITE_TENANT_SQL = text("""
    SELECT
        o.slug AS org_slug,
        o.status AS organization_status,
        o.operational_customer_id AS operational_customer_id,
        b.slug AS branch_slug,
        b.operational_branch_id AS operational_branch_id
    FROM organizations o
    JOIN branches b
      ON b.organization_id = o.id
    WHERE o.slug = :org_slug
      AND b.slug = :branch_slug
    LIMIT 2
""")


def _identity(has_customer: object, has_branch: object) -> OperationalIdentity:
    assigned = int(bool(has_customer)) + int(bool(has_branch))
    return "assigned" if assigned == 2 else "partial" if assigned == 1 else "missing"


def _rows(result) -> list[LibraryRow]:
    libraries: dict[str, LibraryRow] = {}
    for row in result.mappings():
        # One row per organization: should a second primary branch ever exist, the first (lowest id) is kept.
        if row["slug"] in libraries:
            continue
        libraries[row["slug"]] = LibraryRow(
            slug=row["slug"],
            name=row["name"],
            status=row["status"],
            primary_branch_slug=row["primary_branch_slug"],
            primary_branch_name=row["primary_branch_name"],
            primary_branch_status=row["primary_branch_status"],
            operational_identity=_identity(row["has_operational_customer"], row["has_operational_branch"]),
            plan_code=row["plan_code"],
            plan_name=row["plan_name"],
            subscription_status=row["subscription_status"],
        )
    return list(libraries.values())


def list_library_rows() -> list[LibraryRow]:
    """Every organization, cancelled ones included, ordered by name. One statement runs."""
    with get_engine().connect() as conn:
        return _rows(conn.execute(_LIST_SQL))


def get_library_row(org_slug: str) -> LibraryRow | None:
    """The organization with this slug, or None. One statement runs."""
    with get_engine().connect() as conn:
        rows = _rows(conn.execute(_ONE_SQL, {"org_slug": org_slug}))
    return rows[0] if rows else None


def site_tenant(org_slug: str, branch_slug: str) -> ResolvedOperationalTenant | None:
    """The operational tenant behind one branch of the organization, or None (see the module docstring). Unlike
    the customer resolver it requires no membership and refuses no organization status: a suspended or cancelled
    library's last report is still what it last reported."""
    with get_engine().connect() as conn:
        rows = conn.execute(_SITE_TENANT_SQL, {"org_slug": org_slug, "branch_slug": branch_slug}).mappings().all()
    if len(rows) != 1:
        return None
    row = rows[0]
    if row["operational_customer_id"] is None or row["operational_branch_id"] is None:
        return None
    return ResolvedOperationalTenant(
        org_slug=row["org_slug"],
        branch_slug=row["branch_slug"],
        access_mode=access_mode_for_organization_status(row["organization_status"]),
        operational_customer_id=int(row["operational_customer_id"]),
        operational_branch_id=int(row["operational_branch_id"]),
    )
