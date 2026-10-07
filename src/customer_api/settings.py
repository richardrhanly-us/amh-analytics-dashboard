"""Configuration for the customer API, read from the environment.

Every value is read when it is asked for, not at import time, so nothing here
depends on import order and a test can set an environment variable for one
test. No domain, origin or hostname is hardcoded: the deployment supplies
them.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from limits import parse_many

from services import session_service

# --- session cookie ---------------------------------------------------------------

_SECURE_COOKIE_NAME = "__Host-sortview_api_session"
_INSECURE_COOKIE_NAME = "sortview_api_session"


def cookie_secure() -> bool:
    """Whether the session cookie is sent with Secure. On unless
    SORTVIEW_CUSTOMER_COOKIE_SECURE is exactly "false" (local HTTP
    development): any other value, including a typo, keeps it on."""
    return os.getenv("SORTVIEW_CUSTOMER_COOKIE_SECURE", "true").strip().lower() != "false"


def session_cookie_name() -> str:
    # __Host- is a browser-ENFORCED prefix: such a cookie is only accepted
    # when it is Secure, Path=/ and has no Domain. It therefore cannot be
    # used over plain HTTP, so the name follows the secure setting rather
    # than being something a developer has to get right separately.
    return _SECURE_COOKIE_NAME if cookie_secure() else _INSECURE_COOKIE_NAME


def session_cookie_max_age_seconds() -> int:
    """The cookie lives exactly as long as the server-side session it carries."""
    return int(session_service.SESSION_LIFETIME.total_seconds())


# --- allowed browser origins (CSRF defence for state-changing routes) --------------

def canonical_origin(value: str | None) -> str | None:
    """`scheme://host[:port]` for a well-formed http(s) origin, else None.

    Only the scheme and host are lower-cased (both are case-insensitive).
    Nothing else is normalised: a path, query, fragment, credentials or a
    trailing slash makes the value not an origin at all, and an explicit port
    is kept as written, so two distinct origins never compare equal.
    """
    # No whitespace or control character anywhere: urlsplit would silently
    # drop some of them and make a malformed value look like a clean one.
    if not value or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value):
        return None

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None

    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        return None

    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" if port is None else f"{parts.scheme}://{host}:{port}"


def allowed_origins() -> frozenset[str]:
    """SORTVIEW_CUSTOMER_ALLOWED_ORIGINS, comma-separated. Empty by default,
    so every state-changing customer route refuses until a deployment names
    its own browser origin(s). An entry that is not a well-formed origin is
    dropped rather than loosely matched."""
    raw = os.getenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", "")
    return frozenset(
        origin for origin in (canonical_origin(entry.strip()) for entry in raw.split(",")) if origin is not None
    )


# --- login rate limit ---------------------------------------------------------------

# POST /api/auth/login takes no credential the limiter could key on, so --
# like POST /collector/enroll -- it is limited per client address. This is in
# addition to auth_service's own per-account lockout.
DEFAULT_LOGIN_RATE_LIMIT = "10/minute"


def _rate_limit(variable: str, default: str) -> str:
    value = os.getenv(variable, default).strip()
    try:
        parse_many(value)
    except ValueError:
        return default
    return value


def login_rate_limit() -> str:
    """SORTVIEW_LOGIN_RATE_LIMIT in slowapi format. A value that does not
    parse falls back to the default instead of leaving login unlimited."""
    return _rate_limit("SORTVIEW_LOGIN_RATE_LIMIT", DEFAULT_LOGIN_RATE_LIMIT)


# --- account rate limits ------------------------------------------------------------

# Each of these takes a secret a caller could otherwise guess at without end: a current password, or a reset
# token. Like login, each is limited per client address, in its own bucket.
DEFAULT_PASSWORD_ATTEMPT_RATE_LIMIT = "10/minute"  # nosec B105 - a rate, not a password
# Asking for a reset sends an email. Fewer of those: nobody needs more than a few a minute.
DEFAULT_PASSWORD_RESET_REQUEST_RATE_LIMIT = "5/minute"  # nosec B105 - a rate, not a password


def password_attempt_rate_limit() -> str:
    """SORTVIEW_PASSWORD_ATTEMPT_RATE_LIMIT: changing a password, and completing
    a reset. A value that does not parse falls back to the default."""
    return _rate_limit("SORTVIEW_PASSWORD_ATTEMPT_RATE_LIMIT", DEFAULT_PASSWORD_ATTEMPT_RATE_LIMIT)


def password_reset_request_rate_limit() -> str:
    """SORTVIEW_PASSWORD_RESET_REQUEST_RATE_LIMIT: asking for a reset email. A
    value that does not parse falls back to the default."""
    return _rate_limit("SORTVIEW_PASSWORD_RESET_REQUEST_RATE_LIMIT", DEFAULT_PASSWORD_RESET_REQUEST_RATE_LIMIT)


# --- where the customer application is ----------------------------------------------

# The page of the customer (React) application that completes a password reset, and the name the token travels
# under -- in the link's FRAGMENT (#token=...), not its query string. A browser never sends a fragment to a
# server, so the token is not in the request for the page, in a web server's or proxy's access log, or in a
# Referer header; the page reads it and submits it in the body of POST /api/auth/password-reset/complete.
# The dashboard's own reset link (SORTVIEW_APP_URL/?reset_token=...) is a different address and is not built here.
PASSWORD_RESET_PATH = "/reset-password"  # nosec B105 - a URL path
PASSWORD_RESET_TOKEN_PARAMETER = "token"  # nosec B105 - a fragment parameter's name


def customer_app_origin() -> str | None:
    """SORTVIEW_CUSTOMER_APP_URL: the origin people open the customer
    application at, for links sent by email. None unless it is a well-formed
    origin AND one of the allowed browser origins -- a link is never built to
    somewhere this API would then refuse requests from. Nothing is assumed
    when it is unset: there is no default host."""
    origin = canonical_origin(os.getenv("SORTVIEW_CUSTOMER_APP_URL", "").strip())
    return origin if origin is not None and origin in allowed_origins() else None


def password_reset_url(token: str) -> str | None:
    """The link that completes a reset in the customer application, or None
    when that application's address is not configured."""
    origin = customer_app_origin()
    if origin is None:
        return None
    return f"{origin}{PASSWORD_RESET_PATH}#{urlencode({PASSWORD_RESET_TOKEN_PARAMETER: token})}"


# --- product time zone ---------------------------------------------------------------

# The one time zone in which "a day" is understood. It is the dashboard's
# existing setting, SORTVIEW_LIVE_TIMEZONE, read here directly so nothing
# Streamlit-coupled is imported; there is no per-organization zone.
DEFAULT_PRODUCT_TIMEZONE = "America/Chicago"


def product_timezone() -> ZoneInfo:
    """The configured zone. Only an UNSET variable means the default: a value
    that is set but is not a known IANA zone raises (ZoneInfoNotFoundError or
    ValueError) and is answered as a server error. A wrong zone would move
    every day boundary silently, so it is never guessed at or substituted."""
    return ZoneInfo(os.getenv("SORTVIEW_LIVE_TIMEZONE", DEFAULT_PRODUCT_TIMEZONE).strip())
