"""Server-side persistent login sessions for the Streamlit dashboard.

FLOW. A successful password (or guest) login calls create_session(user_id),
which generates a cryptographically random opaque token, stores only its
SHA-256 hex digest in auth_sessions, and returns the raw token once. The
caller (a later step: app.py) puts that raw token in a browser cookie --
this module has no browser/cookie logic at all. On a later page load, the
caller reads the cookie back and calls validate_session(raw_token) to
restore st.session_state["auth_user"] in the same minimal shape
auth_service.authenticate_user already returns.

Only hashlib.sha256(...).hexdigest() of a token ever reaches auth_sessions.
Neither the raw token nor its hash is included in audit or application log
calls -- the identical discipline already used for password_reset_tokens
(auth_service.py) and agent tokens (collector_enrollment_service.py).

NO MODULE-LEVEL auth_service IMPORT. A later, separately-approved step will
likely need auth_service.enforce_active_session() to call INTO this module
(to revoke a deactivated user's persistent sessions), which would create an
auth_service <-> session_service import cycle if this module imported
auth_service at module scope. _log_auth_event() below imports auth_service
inside the function body instead, so this module has no top-level
dependency on auth_service at all.

DATABASE ERRORS PROPAGATE, ON PURPOSE. None of the functions below catch
their own SQL exceptions -- matching auth_service.py's own convention
exactly (its only log_safe_exception call wraps a best-effort AUDIT write,
never its core authenticate_user/get_user_by_email/etc. queries). A real
DB/connectivity failure here is a genuine operational error, not an
invalid-session result, and must never be silently collapsed into a
None/False return -- that would hide an outage behind what looks like an
ordinary expired cookie. The caller (a later step, in app.py) is the actual
boundary layer and can wrap a call here in log_safe_exception if it wants a
graceful fallback, exactly as src/pages/1_admin_settings.py already does
around its own service calls. Bound values (including token_hash) can never
leak through a propagated exception regardless of which layer eventually
logs it: database.get_engine() sets hide_parameters=True on the engine
itself, which strips bound parameter values from SQLAlchemy's own
exception text before any log line is ever formatted.

REVOCATION IS ALWAYS A SOFT UPDATE (revoked_at = now()), never a DELETE --
matching password_reset_tokens.used_at and agent_tokens.is_active. No
automatic cleanup of expired/revoked rows: correctness never depends on
any cleanup running, only on validate_session's own WHERE clause.

CONCURRENCY. No row locking (FOR UPDATE) anywhere in this module, unlike
collector_enrollment_service's enrollment-code redemption. Sessions are
independent by design (multiple concurrent sessions per user are allowed)
and every mutation here is a single, self-contained UPDATE guarded by its
own WHERE clause -- there is no "exactly one winner" race to defend
against the way a one-time code claim has.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from database import get_engine

logger = logging.getLogger("sortview.session")

# --- session settings ---------------------------------------------------------

SESSION_TOKEN_BYTES = 32  # matches PASSWORD_RESET_TOKEN_BYTES / new_agent_token's 32 bytes
SESSION_LIFETIME = timedelta(days=14)
LAST_SEEN_THROTTLE = timedelta(minutes=15)

_MAX_RAW_TOKEN_INPUT_LENGTH = 512  # defensive cap before hashing; a real token is ~43 chars


# --- token primitives -----------------------------------------------------------

def _generate_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def _hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# --- audit logging (lazy import -- see module docstring) ------------------------

def _log_auth_event(**kwargs: Any) -> None:
    from services import auth_service

    auth_service.log_auth_event(**kwargs)


# --- SQL --------------------------------------------------------------------------

_CREATE_SESSION_SQL = """
    INSERT INTO auth_sessions (user_id, token_hash, expires_at, last_seen_at)
    VALUES (:user_id, :token_hash, :expires_at, :last_seen_at)
    RETURNING id
"""

_VALIDATE_SESSION_SQL = """
    SELECT
        s.id AS session_id,
        u.id AS id,
        u.email AS email,
        u.full_name AS full_name
    FROM auth_sessions s
    JOIN app_users u ON u.id = s.user_id
    WHERE s.token_hash = :token_hash
      AND s.revoked_at IS NULL
      AND s.expires_at > :now
      AND u.is_active = TRUE
"""

# Hardened: a session that became revoked or expired between the validation
# SELECT and this UPDATE must not have its last_seen_at touched -- the
# throttle predicate alone doesn't guard against that window.
_TOUCH_LAST_SEEN_SQL = """
    UPDATE auth_sessions
    SET last_seen_at = :now
    WHERE id = :session_id
      AND revoked_at IS NULL
      AND expires_at > :now
      AND (last_seen_at IS NULL OR last_seen_at < :stale_before)
"""

_REVOKE_SESSION_SQL = """
    UPDATE auth_sessions
    SET revoked_at = :now
    WHERE token_hash = :token_hash
      AND revoked_at IS NULL
    RETURNING id, user_id
"""

_REVOKE_ALL_SQL = """
    UPDATE auth_sessions
    SET revoked_at = :now
    WHERE user_id = :user_id
      AND revoked_at IS NULL
"""


# --- create -----------------------------------------------------------------------

def create_session(
    user_id: int,
    *,
    lifetime: timedelta = SESSION_LIFETIME,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Creates a new persistent session for user_id and returns the RAW token
    once: {"token": raw_token, "expires_at": expires_at}. Never stores or
    logs the raw token -- only its SHA-256 hex digest reaches auth_sessions."""
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + lifetime
    raw_token = _generate_session_token()
    token_hash = _hash_session_token(raw_token)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(_CREATE_SESSION_SQL),
            {
                "user_id": user_id,
                "token_hash": token_hash,
                "expires_at": expires_at,
                "last_seen_at": issued_at,
            },
        ).mappings().first()

    session_id = row["id"]

    _log_auth_event(
        event_type="session_created",
        is_success=True,
        user_id=user_id,
        message="Persistent session created.",
        metadata={"session_id": int(session_id), "expires_at": expires_at.isoformat()},
    )

    return {"token": raw_token, "expires_at": expires_at}


# --- validate ---------------------------------------------------------------------

def validate_session(raw_token: str | None, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Validates a raw session token and returns the same minimal auth-user
    shape auth_service.authenticate_user returns on success:
        {"id": ..., "email": ..., "full_name": ...}
    or None for any invalid state (malformed input, unknown/expired/revoked
    token, or an inactive user) -- collapsed into one clean result, never an
    exception. A real database/connectivity failure is NOT caught here and
    propagates -- see the module docstring's DATABASE ERRORS PROPAGATE note."""
    if not raw_token or not isinstance(raw_token, str) or len(raw_token) > _MAX_RAW_TOKEN_INPUT_LENGTH:
        return None

    checked_at = now or datetime.now(UTC)
    token_hash = _hash_session_token(raw_token)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(_VALIDATE_SESSION_SQL),
            {"token_hash": token_hash, "now": checked_at},
        ).mappings().first()

    if row is None:
        return None

    _touch_last_seen(row["session_id"], checked_at)

    return {
        "id": row["id"],
        "email": row["email"],
        "full_name": row["full_name"],
    }


def _touch_last_seen(session_id: int, checked_at: datetime) -> None:
    """Updates last_seen_at only when the session is still valid (not
    revoked/expired as of `checked_at`) AND last_seen_at is missing or
    older than LAST_SEEN_THROTTLE -- a single guarded UPDATE, so a
    validate_session call inside the throttle window, or one that raced a
    revoke/expiry, is a no-op write rather than a Python-side branch. A
    failure here is a genuine DB error and is allowed to propagate like
    any other in this module."""
    stale_before = checked_at - LAST_SEEN_THROTTLE
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(_TOUCH_LAST_SEEN_SQL),
            {"session_id": session_id, "now": checked_at, "stale_before": stale_before},
        )


# --- revoke -----------------------------------------------------------------------

def revoke_session(raw_token: str | None, *, now: datetime | None = None) -> bool:
    """Revokes the session for raw_token. Idempotent: revoking an unknown,
    already-revoked, or malformed token is a no-op returning False, never
    an exception. Never deletes the row."""
    if not raw_token or not isinstance(raw_token, str) or len(raw_token) > _MAX_RAW_TOKEN_INPUT_LENGTH:
        return False

    token_hash = _hash_session_token(raw_token)
    checked_at = now or datetime.now(UTC)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(_REVOKE_SESSION_SQL),
            {"token_hash": token_hash, "now": checked_at},
        ).mappings().first()

    if row is None:
        return False

    _log_auth_event(
        event_type="session_revoked",
        is_success=True,
        user_id=row["user_id"],
        message="Persistent session revoked.",
        metadata={"session_id": int(row["id"])},
    )
    return True


def revoke_all_sessions_for_user(user_id: int, *, now: datetime | None = None) -> int:
    """Revokes every currently-unrevoked session for user_id (account
    deactivation, or a future "log out everywhere"). Returns the number of
    sessions revoked. Never deletes any row."""
    checked_at = now or datetime.now(UTC)

    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(_REVOKE_ALL_SQL),
            {"user_id": user_id, "now": checked_at},
        )
        revoked_count = result.rowcount

    _log_auth_event(
        event_type="session_revoked_all",
        is_success=True,
        user_id=user_id,
        message="All persistent sessions revoked for user.",
        metadata={"revoked_count": int(revoked_count)},
    )
    return int(revoked_count)
