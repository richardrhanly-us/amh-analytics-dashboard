"""The organization's plan features, for the customer API's routes.

    OrganizationEntitlements   the entitlement context of the organization in the path, for the signed-in user
    Transits                   403 unless the organization's plan includes transit routing

The context is read once per request (FastAPI keeps a dependency's answer for the rest of the request), by the same
uncached lookup the organization detail is built from, and only after the route's own checks of who the user is and
what they may see. What a feature MEANS is services.entitlement_service's business; nothing here knows a plan's name.
A database failure propagates as a server error.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends

from customer_api.auth_dependencies import require_current_user
from customer_api.errors import CustomerApiError
from services import entitlement_service


def organization_entitlements(org_slug: str, user: Annotated[dict[str, Any], Depends(require_current_user)]) -> dict[str, Any]:
    return entitlement_service.build_entitlement_context(user["id"], org_slug)


OrganizationEntitlements = Annotated[dict[str, Any], Depends(organization_entitlements)]


def require_transits(entitlements: OrganizationEntitlements) -> None:
    if not entitlement_service.transits_enabled(entitlements):
        raise CustomerApiError(403, "feature_not_available", "This feature is not available for this organization.")


# Declared after the route's tenant or membership check and before anything the request asks for: 401, then 404,
# then 403, then 422.
Transits = Annotated[None, Depends(require_transits)]
