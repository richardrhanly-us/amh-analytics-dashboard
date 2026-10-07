"""The signed-in user's own account, and password reset.

    GET  /account                          who I am: email, name, last sign-in, last password change
    PUT  /account/profile                  change my name (the only thing that can be changed)
    POST /account/change-password          change my password, knowing the current one
    POST /auth/password-reset/request      ask for a reset link by email          (public)
    POST /auth/password-reset/complete     set a new password with that link's token   (public)

THESE ARE ABOUT A PERSON, NOT AN ORGANIZATION. The first three need a session
and nothing else: no organization, membership or role is looked at, so someone
who belongs to several organizations -- or, for the moment, to none -- manages
their account the same way. The user is always the session's: no route takes a
user id or an email to act on.

NOTHING ABOUT PASSWORDS OR TOKENS IS DECIDED HERE. services.auth_service
checks the current password, the new password's rules and the reset token,
writes the hash, revokes sessions and writes the audit log; a route turns its
answer into a response. No password or token is ever logged, echoed or put in
an error.

EVERY WRITE requires an allowed Origin (customer_api.auth_dependencies), like
login and logout.

SESSIONS. Changing a name leaves the session as it is. Changing or resetting
a password revokes EVERY session of that user, in this application and in the
dashboard -- the one that made the request included -- so a successful
password change also clears the browser's cookie, and the person signs in
again with the new password.

A RESET REQUEST NEVER SAYS WHETHER AN ACCOUNT EXISTS. The answer is the same
for an address that has an active account, one that has none, and one whose
account is inactive; the token never reaches the browser; and the email is
sent after the response, so neither a slow mail server nor a failed delivery
changes what the caller sees. The one thing that is reported, and identically
for every address, is that this deployment cannot send reset email at all
(503): the link's address or the mail settings are not configured.

The link goes to the customer application (settings.password_reset_url). The
dashboard's own reset emails are sent by the dashboard and still link to the
dashboard; the two share one kind of token, so either can complete a reset
the other began.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import sentry_sdk
from fastapi import APIRouter, Depends, Request
from slowapi import Limiter
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response

from customer_api import settings
from customer_api.account_schemas import (
    AccountResponse,
    ChangePasswordRequest,
    PasswordResetCompleteRequest,
    PasswordResetRequest,
    ProfileUpdateRequest,
)
from customer_api.auth_dependencies import require_allowed_origin, require_current_user
from customer_api.errors import (
    NO_STORE_HEADERS,
    CustomerApiError,
    CustomerApiRoute,
    clear_session_cookie,
    logger,
    not_authenticated,
)
from services import auth_service, email_service
from services.privacy_hardening import log_safe_exception

CurrentUser = Annotated[dict[str, Any], Depends(require_current_user)]

# What auth_service says is wrong, as the field it is about and a stable word for why.
_NAME_PROBLEMS = {
    "name_required": ("full_name", "required"),
    "name_too_long": ("full_name", "too_long"),
    "name_invalid": ("full_name", "invalid_characters"),
}
_PASSWORD_PROBLEMS = {
    "invalid_current_password": ("current_password", "incorrect"),
    "password_too_short": ("new_password", "too_short"),
    "password_mismatch": ("confirm_password", "mismatch"),
    "same_password": ("new_password", "same_as_current"),
}

RESET_REQUESTED = {
    "code": "password_reset_requested",
    "message": "If an active account exists for that email address, password reset instructions will be sent.",
}


def _problem(code: str, message: str, problem: tuple[str, str]) -> Response:
    """422 for a value that cannot be accepted: which field, and a stable
    word for why. The customer API's error shape with the problems beside it,
    as for settings. Nothing that was sent is repeated."""
    return JSONResponse(
        status_code=422,
        content={"code": code, "message": message, "problems": [{"field": problem[0], "code": problem[1]}]},
        headers=NO_STORE_HEADERS,
    )


def _utc(instant: datetime | None) -> datetime | None:
    """The same instant, written in UTC -- or None for something that has never happened.

    Both account timestamps are TIMESTAMPTZ columns. The driver hands such a value back as an aware datetime in the
    DATABASE SESSION's time zone, which is whatever the server or the connection happens to be set to; left as it
    is, the API would answer "...-05:00" from one deployment and "...+00:00" from another. The instant is the
    same either way; only how it is written changes here.

    A naive datetime is refused, not guessed at. A TIMESTAMPTZ never arrives without a zone, so one that does is a
    fault -- a column changed, or a row from somewhere else -- and there is no zone it can safely be assumed to
    be in."""
    if instant is None:
        return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("an account timestamp has no time zone")
    return instant.astimezone(UTC)


def _account_body(profile: dict[str, Any]) -> Response:
    # Built field by field through the response model, so nothing else in a user row can reach the browser.
    body = AccountResponse(
        email=profile["email"],
        full_name=profile["full_name"] or "",
        last_login_at=_utc(profile["last_login_at"]),
        last_password_changed_at=_utc(profile["last_password_changed_at"]),
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers=NO_STORE_HEADERS)


def _unexpected(result: dict[str, Any]) -> RuntimeError:
    # A code this module does not know how to answer: a server error, never a guess. The code only -- no message.
    return RuntimeError(f"auth_service answered with an unhandled code: {result.get('code')}")


def _reset_delivery_unavailable() -> CustomerApiError:
    return CustomerApiError(503, "password_reset_unavailable", "Password reset is not available right now.")


def _deliver_reset_email(recipient_email: str, reset_token: str, reset_url: str) -> None:
    """Sends the reset link. Runs after the response has gone, so nothing
    here can change it; a failure is logged as a safe summary and reported
    to the error tracker -- never the address, the token or the link."""
    try:
        email_service.send_password_reset_email(recipient_email, reset_token, reset_url=reset_url)
    except Exception as exc:
        log_safe_exception(logger, "Password reset email could not be sent", exc)
        sentry_sdk.capture_exception(exc)


def create_account_router(limiter: Limiter) -> APIRouter:
    """The account routes, rate-limited by the application's own limiter
    (handed in so this package never imports main.py). Build once per
    limiter."""
    router = APIRouter(route_class=CustomerApiRoute)

    @router.get("/account")
    def get_account(user: CurrentUser) -> Response:
        profile = auth_service.get_account_profile(user["id"])
        if profile is None:
            # Deactivated since the session was validated a moment ago.
            raise not_authenticated(clear_session_cookie=True)
        return _account_body(profile)

    @router.put("/account/profile", dependencies=[Depends(require_allowed_origin)])
    def update_profile(data: ProfileUpdateRequest, user: CurrentUser) -> Response:
        result = auth_service.update_profile_name(user["id"], data.full_name)

        if result["ok"]:
            return _account_body(result["profile"])
        if result["code"] in _NAME_PROBLEMS:
            return _problem("invalid_profile", "The profile is not valid.", _NAME_PROBLEMS[result["code"]])
        if result["code"] == "user_not_found":
            raise not_authenticated(clear_session_cookie=True)
        raise _unexpected(result)

    @router.post("/account/change-password", dependencies=[Depends(require_allowed_origin)])
    @limiter.limit(settings.password_attempt_rate_limit)
    def change_password(request: Request, data: ChangePasswordRequest, user: CurrentUser) -> Response:
        result = auth_service.change_password(
            user_id=user["id"],
            current_password=data.current_password.get_secret_value(),
            new_password=data.new_password.get_secret_value(),
            confirm_password=data.confirm_password.get_secret_value(),
        )

        if result["ok"]:
            # Every session of this user is now revoked, this one included: the cookie is no longer a credential.
            response = Response(status_code=204, headers=NO_STORE_HEADERS)
            clear_session_cookie(response)
            return response
        if result["code"] in _PASSWORD_PROBLEMS:
            # A wrong current password is a 422 like the others, deliberately not a 401: the session is still good.
            return _problem("invalid_password_change", "The password could not be changed.", _PASSWORD_PROBLEMS[result["code"]])
        if result["code"] in ("user_not_found", "inactive"):
            raise not_authenticated(clear_session_cookie=True)
        raise _unexpected(result)

    @router.post("/auth/password-reset/request", dependencies=[Depends(require_allowed_origin)])
    @limiter.limit(settings.password_reset_request_rate_limit)
    def request_password_reset(request: Request, data: PasswordResetRequest) -> Response:
        # Decided before the address is looked at, so the answer is the same whoever it belongs to.
        if settings.customer_app_origin() is None or not email_service.smtp_configured():
            raise _reset_delivery_unavailable()

        result = auth_service.request_password_reset(data.email)

        response = JSONResponse(status_code=202, content=RESET_REQUESTED, headers=NO_STORE_HEADERS)
        reset_token, recipient_email = result.get("reset_token"), result.get("reset_email")
        if reset_token and recipient_email:
            reset_url = settings.password_reset_url(reset_token)
            if reset_url is not None:
                # After the response: the caller cannot tell an address that gets an email from one that does not.
                response.background = BackgroundTask(_deliver_reset_email, recipient_email, reset_token, reset_url)
        return response

    @router.post("/auth/password-reset/complete", dependencies=[Depends(require_allowed_origin)])
    @limiter.limit(settings.password_attempt_rate_limit)
    def complete_password_reset(request: Request, data: PasswordResetCompleteRequest) -> Response:
        result = auth_service.reset_password_with_token(
            token=data.token.get_secret_value(),
            new_password=data.new_password.get_secret_value(),
            confirm_password=data.confirm_password.get_secret_value(),
        )

        if result["ok"]:
            # Every session of that user is revoked. Nobody is signed in by this: they sign in with the new password.
            return Response(status_code=204, headers=NO_STORE_HEADERS)
        if result["code"] == "invalid_reset_token":
            # One answer for a token that never existed, has expired, was already used or belongs to an inactive account.
            raise CustomerApiError(400, "invalid_reset_token", "This password reset link is invalid or has expired.")
        if result["code"] in _PASSWORD_PROBLEMS:
            return _problem("invalid_password_reset", "The password could not be reset.", _PASSWORD_PROBLEMS[result["code"]])
        raise _unexpected(result)

    return router
