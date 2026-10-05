"""The customer API's router.

Every customer-facing route is registered through create_customer_router and
therefore lives under /api. /v1 and /v2 are deliberately not used: those
names belong to the collector ingestion contracts in root main.py.
"""

from __future__ import annotations

from fastapi import APIRouter
from slowapi import Limiter

from customer_api.auth_routes import create_auth_router
from customer_api.errors import CustomerApiRoute
from customer_api.organization_routes import create_organization_router

API_PREFIX = "/api"


def create_customer_router(limiter: Limiter) -> APIRouter:
    """Builds the customer router around the application's rate limiter.

    The limiter is passed in rather than imported so this package never
    depends on root main.py. Call once per application.
    """
    router = APIRouter(prefix=API_PREFIX, route_class=CustomerApiRoute)
    router.include_router(create_auth_router(limiter))
    router.include_router(create_organization_router())
    return router
