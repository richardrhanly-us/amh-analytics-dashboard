"""FastAPI dependencies for customer routes: the Origin guard and the
current-user lookup.

Neither knows anything about organizations, branches or tenant context.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from customer_api import settings
from customer_api.errors import CustomerApiError, not_authenticated
from services import session_service


def require_allowed_origin(request: Request) -> None:
    """CSRF defence for state-changing routes: the request must carry exactly
    one Origin header, and it must be one of the configured browser origins.
    A missing, repeated, malformed ("null" included) or unlisted Origin is
    refused. This is independent of CORS, which only governs what a browser
    lets a page read."""
    values = request.headers.getlist("origin")
    origin = settings.canonical_origin(values[0]) if len(values) == 1 else None

    if origin is None or origin not in settings.allowed_origins():
        raise CustomerApiError(403, "origin_not_allowed", "Request origin is not allowed.")


def session_token_from_request(request: Request) -> str | None:
    """The raw session token from the customer API's own cookie -- never the
    Streamlit dashboard's cookie, which this API does not read."""
    return request.cookies.get(settings.session_cookie_name()) or None


def require_current_user(request: Request) -> dict[str, Any]:
    """The authenticated user ({"id", "email", "full_name"}) for this request.

    Fails closed with the same 401 whether there is no cookie or the token is
    unknown, expired, revoked or belongs to a user who is no longer active --
    session_service.validate_session collapses those into None. A cookie that
    did not validate is cleared. A database failure is NOT an authentication
    failure: it propagates and is answered as a server error.
    """
    token = session_token_from_request(request)
    if token is None:
        raise not_authenticated()

    user = session_service.validate_session(token)
    if user is None:
        raise not_authenticated(clear_session_cookie=True)

    return user
