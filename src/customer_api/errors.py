"""The customer API's own error shape and the route class that produces it.

Collector routes answer errors as {"detail": "..."} through FastAPI's global
handlers. The customer API answers {"code": "...", "message": "..."} instead,
and does so through a route class scoped to customer routes -- the same
mechanism root main.py uses for its v2 routes -- so no global exception
handler is added or changed and the two contracts can evolve independently.

Left to the application's existing handlers, unchanged: request validation
(the hardened 422), rate limiting (429) and any HTTPException.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import sentry_sdk
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from slowapi.errors import RateLimitExceeded
from starlette.responses import JSONResponse, Response

from customer_api import settings
from services.privacy_hardening import log_safe_exception

logger = logging.getLogger("sortview.customer_api")

# Auth responses are per-user and must never be stored by a browser or proxy.
NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class CustomerApiError(Exception):
    """An expected, client-visible failure. `code` is stable and machine
    readable; `message` is fixed text, never an exception's own message."""

    def __init__(self, status_code: int, code: str, message: str, *, clear_session_cookie: bool = False) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.clear_session_cookie = clear_session_cookie


def not_authenticated(*, clear_session_cookie: bool = False) -> CustomerApiError:
    """The one answer for every missing, unknown, expired or revoked session
    and for a session whose user is no longer active."""
    return CustomerApiError(
        401, "not_authenticated", "Authentication is required.", clear_session_cookie=clear_session_cookie
    )


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name(),
        value=token,
        max_age=settings.session_cookie_max_age_seconds(),
        path="/",
        secure=settings.cookie_secure(),
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    # Same name, Path and Secure as when it was set: a __Host- cookie is only
    # replaced (and so only cleared) by one carrying the same attributes.
    response.delete_cookie(
        key=settings.session_cookie_name(),
        path="/",
        secure=settings.cookie_secure(),
        httponly=True,
        samesite="lax",
    )


def error_response(error: CustomerApiError) -> JSONResponse:
    response = JSONResponse(
        status_code=error.status_code,
        content={"code": error.code, "message": error.message},
        headers=NO_STORE_HEADERS,
    )
    if error.clear_session_cookie:
        clear_session_cookie(response)
    return response


class CustomerApiRoute(APIRoute):
    """Route class for every customer route. An expected CustomerApiError
    becomes its {"code", "message"} response. Anything unexpected -- a
    database failure included -- becomes one generic 500, logged as a safe
    summary (never the exception's own text): it is never reported as an
    authentication failure."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except CustomerApiError as error:
                return error_response(error)
            except (HTTPException, RequestValidationError, RateLimitExceeded):
                raise
            except Exception as exc:
                log_safe_exception(logger, "Customer API request failed", exc)
                sentry_sdk.capture_exception(exc)
                return error_response(CustomerApiError(500, "internal_error", "Internal server error."))

        return handler
