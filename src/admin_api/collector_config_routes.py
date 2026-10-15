"""A library's collector configuration in the administration API (A2 item 4).

    GET /api/admin/libraries/{org_slug}/collector-settings   the Collector time zone, the review record, readiness
    PUT /api/admin/libraries/{org_slug}/collector-settings   sets the time zone and records the review
    GET /api/admin/libraries/{org_slug}/internal-routing     the four Internal Routing lists
    PUT /api/admin/libraries/{org_slug}/internal-routing     replaces them

Included in the admin router (admin_api.router), so every route inherits its platform-administrator check and the
admin Origin guard on the two PUTs. Every answer is no-store.

THE LIBRARY'S OWN SETTINGS, at organization level: what a Contract v2 enrollment reads (services.collector_rules) and
what the v1 hold classifier reads (services.hold_rules). Never a branch's settings, and never `transit` -- the library
edits its home label and transit destinations itself; they are shown here for context only.

SAVED TO SORTVIEW, NOT TO A COLLECTOR. A change is used by the v1 hold reports from their next read and by the next
Contract v2 enrollment. A Collector already installed keeps the rules and time zone it received at enrollment: nothing
here reaches it.

THE INTERNAL ROUTING LISTS CAN HOLD A PERSON'S OWN ACCOUNT NAME. They are returned by the internal-routing GET and PUT
alone. A refused value is answered as fields and codes, never echoed; the audit record and the log line hold how many
entries each list has, never one.

    unknown library                               404 library_not_found
    a cancelled library's settings changed        409 library_cancelled   (they can still be read)
    a value refused                               422 with the fields at fault and why
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse, Response

from admin_api.collector_config_schemas import (
    CollectorSettingsRequest,
    CollectorSettingsResponse,
    EnrollmentProblem,
    InternalRoutingCounts,
    InternalRoutingRequest,
    InternalRoutingResponse,
)
from admin_api.dependencies import require_platform_admin
from admin_api.diagnostics_schemas import LibraryRef
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    logger,
)
from services import collector_rules, internal_routing, tenant_service

PlatformAdmin = Annotated[dict[str, Any], Depends(require_platform_admin)]


def _library_not_found() -> CustomerApiError:
    return CustomerApiError(404, "library_not_found", "Library not found.")


def _library_cancelled() -> CustomerApiError:
    return CustomerApiError(409, "library_cancelled", "This library is cancelled; its settings cannot be changed.")


def _organization(org_slug: str) -> dict[str, Any]:
    organization = tenant_service.get_organization_by_slug(org_slug)
    if organization is None:
        raise _library_not_found()
    return organization


def _library(organization: dict[str, Any]) -> LibraryRef:
    return LibraryRef(slug=organization["slug"], name=organization["name"])


def _invalid(code: str, message: str, problems: list[tuple[str, str]]) -> Response:
    """422: every field at fault and a fixed code for why. Nothing that was sent is repeated."""
    return JSONResponse(
        status_code=422,
        content={"code": code, "message": message, "problems": [{"field": field, "code": why} for field, why in problems]},
        headers=NO_STORE_HEADERS,
    )


def _enrollment_problems(site: dict[str, Any]) -> list[EnrollmentProblem]:
    problems: list[EnrollmentProblem] = []
    try:
        collector_rules.collector_timezone(site)
    except collector_rules.CollectorSettingsError as exc:
        problems.append("timezone_invalid" if exc.reason == collector_rules.TIMEZONE_INVALID else "timezone_not_configured")
    try:
        collector_rules.require_rules_reviewed(site)
    except collector_rules.CollectorSettingsError:
        problems.append("rules_not_reviewed")
    return problems


def _rules_reviewed_at(site: dict[str, Any]) -> datetime | None:
    try:
        collector_rules.require_rules_reviewed(site)
    except collector_rules.CollectorSettingsError:
        return None
    return datetime.fromisoformat(site[collector_rules.RULES_REVIEW_KEY]["reviewed_at"])


def _collector_settings_body(organization: dict[str, Any]) -> Response:
    site = tenant_service.get_collector_site_settings(int(organization["id"]))
    stored_zone = site.get(collector_rules.TIMEZONE_KEY)
    counts = site["internal_routing_counts"]
    body = CollectorSettingsResponse(
        library=_library(organization),
        collector_timezone=stored_zone if isinstance(stored_zone, str) else None,
        rules_reviewed_at=_rules_reviewed_at(site),
        home_branch_label=site["home_branch_label"],
        transit_destination_labels=list(site["transit_destination_labels"]),
        internal_routing_counts=InternalRoutingCounts(**{name: int(counts.get(name, 0)) for name in internal_routing.LISTS}),
        enrollment_problems=_enrollment_problems(site),
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def _internal_routing_body(organization: dict[str, Any], lists: dict[str, list[str]]) -> Response:
    body = InternalRoutingResponse(library=_library(organization), **{name: lists[name] for name in internal_routing.LISTS})
    return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def create_collector_config_router() -> APIRouter:
    router = APIRouter(prefix="/libraries/{org_slug}", route_class=CustomerApiRoute)

    @router.get("/collector-settings")
    def get_collector_settings(org_slug: str) -> Response:
        return _collector_settings_body(_organization(org_slug))

    @router.put("/collector-settings")
    def put_collector_settings(org_slug: str, user: PlatformAdmin, body: CollectorSettingsRequest) -> Response:
        organization = _organization(org_slug)
        try:
            tenant_service.set_collector_enrollment_settings(
                int(organization["id"]), timezone=body.collector_timezone, target_org_slug=organization["slug"],
                actor={"id": user["id"], "email": user["email"]},
            )
        except collector_rules.CollectorSettingsError as exc:
            return _invalid(
                "invalid_collector_settings", "The collector settings are not valid.",
                [("collector_timezone", "timezone_invalid" if exc.reason == collector_rules.TIMEZONE_INVALID else "required")],
            )
        except tenant_service.OrganizationCancelledError:
            raise _library_cancelled() from None
        logger.info("Admin collector settings updated | org=%s user_id=%s", organization["slug"], user["id"])
        return _collector_settings_body(organization)

    @router.get("/internal-routing")
    def get_internal_routing(org_slug: str) -> Response:
        organization = _organization(org_slug)
        return _internal_routing_body(organization, tenant_service.get_internal_routing(int(organization["id"])))

    @router.put("/internal-routing")
    def put_internal_routing(org_slug: str, user: PlatformAdmin, body: InternalRoutingRequest) -> Response:
        organization = _organization(org_slug)
        try:
            wanted = internal_routing.validate_internal_routing(body.model_dump())
        except internal_routing.InternalRoutingError as error:
            return _invalid(
                "invalid_internal_routing", "The Internal Routing lists are not valid.",
                [(problem.field, problem.code) for problem in error.problems],
            )
        try:
            stored = tenant_service.replace_internal_routing(
                int(organization["id"]), wanted, target_org_slug=organization["slug"],
                actor={"id": user["id"], "email": user["email"]},
            )
        except tenant_service.OrganizationCancelledError:
            raise _library_cancelled() from None
        # Who replaced which library's lists, and how many entries each now has. Never an entry.
        logger.info(
            "Admin Internal Routing replaced | org=%s user_id=%s counts=%s",
            organization["slug"], user["id"], internal_routing.entry_counts(stored),
        )
        return _internal_routing_body(organization, stored)

    return router
