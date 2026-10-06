"""The authenticated user's organization and branch context.

These routes describe what the user may reach, in SaaS terms only: an
organization and a branch are identified by slug. They read the uncached core
services (services.access_service, services.entitlement_service) -- never the
Streamlit adapters -- and services.sorter_inventory_service, so membership,
role, organization status and the organization's sorting machines are looked
up fresh on every request. They run no SQL of their own, set no tenant
context and read no operational table.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from customer_api.auth_dependencies import require_current_user
from customer_api.errors import NO_STORE_HEADERS, CustomerApiError, CustomerApiRoute
from customer_api.organization_schemas import (
    BranchSummary,
    FeatureEntitlement,
    OrganizationDetail,
    OrganizationSummary,
    SorterHostBranch,
    SorterSummary,
    SubscriptionSummary,
)
from services import access_service, entitlement_service, sorter_inventory_service

CurrentUser = Annotated[dict[str, Any], Depends(require_current_user)]

# The access modes under which an organization is visible to its members.
# Anything else -- "blocked", or a value this code does not know -- is treated
# as not visible (fail closed).
_VISIBLE_ACCESS_MODES = ("full", "read_only")


def _organization_not_found() -> CustomerApiError:
    """The one answer for an organization that does not exist, that the user
    is not a member of, or that is cancelled or otherwise blocked. Identical
    in every case, so a slug cannot be probed."""
    return CustomerApiError(404, "organization_not_found", "Organization not found.")


def _visible_access_mode(org_slug: str) -> str | None:
    mode = access_service.get_org_access_mode(org_slug)
    return mode if mode in _VISIBLE_ACCESS_MODES else None


def require_organization_member(org_slug: str, user: CurrentUser) -> dict[str, Any]:
    """The authenticated user, once it is established that they may see the
    organization in the path: they are a member of it, and it is visible
    ("full" or "read_only" -- a suspended organization stays readable). Any
    other case is the one organization_not_found 404, exactly as for the
    organization detail below.

    For routes about an organization as a whole. It makes no role or
    entitlement decision and opens no operational scope: a route that reads
    operational data still resolves, site by site, each scope it reads. A
    database failure propagates and is answered as a server error, never as
    "not found"."""
    # get_user_memberships already leaves out cancelled organizations.
    if not any(m["organization_slug"] == org_slug for m in access_service.get_user_memberships(user["id"])):
        raise _organization_not_found()
    if _visible_access_mode(org_slug) is None:
        raise _organization_not_found()
    return user


def create_organization_router() -> APIRouter:
    router = APIRouter(prefix="/organizations", route_class=CustomerApiRoute)

    @router.get("")
    def list_organizations(user: CurrentUser) -> Response:
        # get_user_memberships already leaves out cancelled organizations and
        # orders by name; that order is kept.
        organizations = []
        for membership in access_service.get_user_memberships(user["id"]):
            access_mode = _visible_access_mode(membership["organization_slug"])
            if access_mode is None:
                continue
            organizations.append(
                OrganizationSummary(
                    slug=membership["organization_slug"],
                    name=membership["organization_name"],
                    role=membership["role"],
                    access_mode=access_mode,
                ).model_dump()
            )

        return JSONResponse(content=organizations, headers=NO_STORE_HEADERS)

    @router.get("/{org_slug}")
    def get_organization(org_slug: str, user: CurrentUser) -> Response:
        membership = next(
            (m for m in access_service.get_user_memberships(user["id"]) if m["organization_slug"] == org_slug),
            None,
        )
        if membership is None:
            raise _organization_not_found()

        access_mode = _visible_access_mode(org_slug)
        if access_mode is None:
            raise _organization_not_found()

        # The role comes from the entitlement context's own uncached lookup
        # -- the one every permission check is built on -- not from the
        # membership row above. No role means no membership any more.
        context = entitlement_service.build_entitlement_context(user["id"], org_slug)
        if not context["role"]:
            raise _organization_not_found()

        subscription = context["subscription"]
        detail = OrganizationDetail(
            slug=membership["organization_slug"],
            name=membership["organization_name"],
            role=context["role"],
            access_mode=access_mode,
            # get_org_branches returns the organization's ACTIVE branches,
            # primary first -- the same set the dashboard offers.
            branches=[
                BranchSummary(slug=branch["branch_slug"], name=branch["branch_name"], is_primary=branch["is_primary"])
                for branch in access_service.get_org_branches(org_slug)
            ],
            # The machines the organization runs SortView on: registered
            # installations, one entry per host branch. Never derived from the
            # branch list above or from routing destinations.
            sorters=[
                SorterSummary(
                    slug=site.slug,
                    name=site.name,
                    host_branch=SorterHostBranch(slug=site.host_branch_slug, name=site.host_branch_name),
                    status=site.status,
                    collector_count=site.collector_count,
                )
                for site in sorter_inventory_service.list_sorter_sites(org_slug)
            ],
            subscription=None if subscription is None else SubscriptionSummary(
                plan_code=subscription["plan_code"],
                plan_name=subscription["plan_name"],
                status=subscription["status"],
            ),
            entitlements={
                feature_key: FeatureEntitlement(enabled=feature["enabled"], limit_value=feature["limit_value"])
                for feature_key, feature in context["entitlements"].items()
            },
        )

        return JSONResponse(content=detail.model_dump(), headers=NO_STORE_HEADERS)

    return router
