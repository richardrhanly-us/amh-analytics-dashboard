"""FastAPI dependencies for admin routes: the platform-administrator check and the admin Origin guard.

Both are attached to the admin router itself (admin_api.router), so every admin route has them.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from admin_api import settings
from customer_api.auth_dependencies import require_current_user
from customer_api.errors import CustomerApiError
from services import platform_admin_service

# Methods that change nothing. Every other method is a write and must come from the admin app's origin.
_SAFE_METHODS = frozenset({"GET", "HEAD"})


def require_platform_admin(user: Annotated[dict[str, Any], Depends(require_current_user)]) -> dict[str, Any]:
    """The signed-in user, once the database says -- now, on this request -- that they are a platform administrator.

    No session, an invalid one, or one whose user is no longer active: require_current_user's own 401. Signed in but
    not a platform administrator, whatever roles they hold in any organization: 403. The flag is read uncached every
    time, so a revoked administrator is refused on their next request. A database failure is not an answer about
    the user: it propagates and is a server error, never a 401 or 403."""
    if not platform_admin_service.is_platform_admin(user["id"]):
        raise CustomerApiError(403, "forbidden", "You do not have access to SortView administration.")
    return user


def require_admin_origin(request: Request) -> None:
    """CSRF defence for state-changing admin routes. A GET or HEAD passes without an Origin. Any other request must
    carry exactly one Origin header, listed in SORTVIEW_ADMIN_ALLOWED_ORIGINS; a missing, repeated, malformed or
    unlisted one -- the customer app's origin included -- is refused."""
    if request.method in _SAFE_METHODS:
        return
    values = request.headers.getlist("origin")
    origin = settings.canonical_origin(values[0]) if len(values) == 1 else None
    if origin is None or origin not in settings.allowed_origins():
        raise CustomerApiError(403, "origin_not_allowed", "Request origin is not allowed.")
