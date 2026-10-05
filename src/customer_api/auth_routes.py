"""Browser session routes: login, logout and the current session.

The credential is the opaque token session_service issues. It travels only
in an HttpOnly cookie: it is never in a response body, never logged and never
handed to browser script.
"""

from __future__ import annotations

from typing import Annotated, Any

import sentry_sdk
from fastapi import APIRouter, Depends, Request
from slowapi import Limiter
from starlette.responses import JSONResponse, Response

from customer_api import settings
from customer_api.auth_dependencies import (
    require_allowed_origin,
    require_current_user,
    session_token_from_request,
)
from customer_api.auth_schemas import CurrentUserResponse, LoginRequest
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    clear_session_cookie,
    logger,
    set_session_cookie,
)
from services import auth_service, session_service
from services.privacy_hardening import log_safe_exception


def _current_user_body(user: dict[str, Any]) -> dict[str, Any]:
    # Built field by field through the response model, so nothing else a
    # service might one day put in the user dict can reach the browser.
    return CurrentUserResponse(id=user["id"], email=user["email"], full_name=user["full_name"]).model_dump()


def create_auth_router(limiter: Limiter) -> APIRouter:
    """The auth routes, rate-limited by the application's own limiter (handed
    in so this package never imports main.py). Build once per limiter: the
    limiter records each limited route when it is decorated."""
    router = APIRouter(prefix="/auth", route_class=CustomerApiRoute)

    @router.post("/login", dependencies=[Depends(require_allowed_origin)])
    @limiter.limit(settings.login_rate_limit)
    def login(request: Request, data: LoginRequest) -> Response:
        result = auth_service.authenticate_user(email=data.email, password=data.password.get_secret_value())

        if not result["ok"]:
            # One answer for an unknown email, a wrong password, an inactive
            # account and a locked account: the response must not reveal
            # which accounts exist or what state they are in. auth_service
            # has already done its own failure tracking, lockout and audit.
            raise CustomerApiError(401, "invalid_credentials", "Invalid email or password.")

        user = result["user"]
        # A failure here propagates: no cookie is set and the answer is the
        # generic server error, not an authentication failure.
        session = session_service.create_session(user["id"])

        response = JSONResponse(content=_current_user_body(user), headers=NO_STORE_HEADERS)
        set_session_cookie(response, session["token"])
        return response

    @router.get("/session")
    def current_session(user: Annotated[dict[str, Any], Depends(require_current_user)]) -> Response:
        return JSONResponse(content=_current_user_body(user), headers=NO_STORE_HEADERS)

    @router.post("/logout", dependencies=[Depends(require_allowed_origin)])
    def logout(request: Request) -> Response:
        token = session_token_from_request(request)

        if token is not None:
            _end_session(token)

        # The browser's credential is cleared whatever happened above.
        response = Response(status_code=204, headers=NO_STORE_HEADERS)
        clear_session_cookie(response)
        return response

    return router


def _end_session(token: str) -> None:
    """Best-effort server-side logout. Nothing here may stop the browser's
    cookie from being cleared, so every step is contained and a failure is
    logged as a safe summary (never the token, never the exception's text)."""
    user = None
    try:
        # Only to learn whose session this is, for the logout audit event.
        user = session_service.validate_session(token)
    except Exception as exc:
        _report("Customer logout could not identify the session", exc)

    try:
        # Writes its own session_revoked audit event. Unknown and
        # already-revoked tokens are a no-op.
        session_service.revoke_session(token)
    except Exception as exc:
        _report("Customer logout could not revoke the session", exc)

    if user is None:
        return

    try:
        # The same event the dashboard's logout writes.
        auth_service.log_auth_event(
            event_type="logout",
            is_success=True,
            user_id=user["id"],
            email=user["email"],
            message="User logged out.",
            metadata={"source": "customer_api"},
        )
    except Exception as exc:
        _report("Failed to write logout audit event", exc)


def _report(message: str, exc: Exception) -> None:
    log_safe_exception(logger, message, exc)
    sentry_sdk.capture_exception(exc)
