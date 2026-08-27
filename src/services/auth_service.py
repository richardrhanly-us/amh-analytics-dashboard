#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         auth_service.py
#
#  Description: Provides authentication and user account helpers for
#               the SortView dashboard. This file handles login
#               auditing, user lookup, failed-login tracking, account
#               lockouts, password changes, and user creation.
#
#***************************************************************

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_engine

#***************************************************************
# Authentication Settings
#
# Defines the failed-login threshold and temporary account lockout
# window used during authentication.
#***************************************************************

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


#***************************************************************
#
#  Function:     log_auth_event
#
#  Description: Writes an authentication-related event to the audit
#               log. This is used to track successful logins, failed
#               logins, lockouts, password changes, and user creation
#               events.
#
#  Parameters:  event_type - Type of authentication event being logged.
#               is_success - Boolean flag indicating whether the event
#                            was successful.
#               user_id - Optional user ID connected to the event.
#               email - Optional email address connected to the event.
#               message - Optional human-readable event message.
#               metadata - Optional dictionary with extra event details.
#
#  Returns:     None
#
#***************************************************************

def log_auth_event(
    event_type: str,
    is_success: bool,
    user_id: int | None = None,
    email: str | None = None,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    # Build the insert statement for the authentication audit log.
    sql = text("""
        INSERT INTO auth_audit_log (
            user_id,
            email,
            event_type,
            is_success,
            message,
            metadata
        )
        VALUES (
            :user_id,
            :email,
            :event_type,
            :is_success,
            :message,
            CAST(:metadata AS jsonb)
        )
    """)

    # Normalize the email address and store metadata as JSON text.
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "user_id": user_id,
                "email": email.strip().lower() if email else None,
                "event_type": event_type,
                "is_success": is_success,
                "message": message,
                "metadata": "{}" if metadata is None else __import__("json").dumps(metadata),
            },
        )


#***************************************************************
#
#  Function:     get_user_by_email
#
#  Description: Retrieves a user account by email address. The lookup
#               is case-insensitive and returns account status,
#               password, lockout, and login tracking fields.
#
#  Parameters:  email - Email address to search for.
#
#  Returns:     dict[str, Any] | None - User record if found;
#                                      otherwise None.
#
#***************************************************************

def get_user_by_email(email: str) -> dict[str, Any] | None:
    # Build the user lookup query using a case-insensitive email match.
    sql = text("""
        SELECT
            id,
            email,
            full_name,
            password_hash,
            is_active,
            failed_login_attempts,
            locked_until,
            last_login_at,
            last_failed_login_at,
            last_password_changed_at,
            created_at
        FROM app_users
        WHERE lower(email) = lower(:email)
        LIMIT 1
    """)

    # Execute the lookup and return the first matching user as a dictionary.
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"email": email.strip()}).mappings().first()
        return dict(row) if row else None


#***************************************************************
#
#  Function:     get_user_by_id
#
#  Description: Retrieves a user account by internal user ID. The
#               returned record includes account status, password,
#               lockout, and login tracking fields.
#
#  Parameters:  user_id - Internal user ID to search for.
#
#  Returns:     dict[str, Any] | None - User record if found;
#                                      otherwise None.
#
#***************************************************************

def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    # Build the user lookup query using the internal user ID.
    sql = text("""
        SELECT
            id,
            email,
            full_name,
            password_hash,
            is_active,
            failed_login_attempts,
            locked_until,
            last_login_at,
            last_failed_login_at,
            last_password_changed_at,
            created_at
        FROM app_users
        WHERE id = :user_id
        LIMIT 1
    """)

    # Execute the lookup and return the first matching user as a dictionary.
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"user_id": user_id}).mappings().first()
        return dict(row) if row else None


#***************************************************************
#
#  Function:     record_failed_login
#
#  Description: Records a failed login attempt for a user. The function
#               increments the failed-login count, stores the latest
#               failed-login timestamp, and applies a temporary lockout
#               when the failed-attempt limit is reached.
#
#  Parameters:  user_id - Internal user ID for the failed login.
#
#  Returns:     None
#
#***************************************************************

def record_failed_login(user_id: int) -> None:
    # Update failed-login counters and apply lockout when the limit is reached.
    # max_attempts / lockout_minutes are bound parameters (not f-string
    # interpolation) even though both values only ever come from the
    # MAX_FAILED_ATTEMPTS / LOCKOUT_MINUTES module constants above.
    sql = text("""
        UPDATE app_users
        SET
            failed_login_attempts = failed_login_attempts + 1,
            last_failed_login_at = now(),
            locked_until = CASE
                WHEN failed_login_attempts + 1 >= :max_attempts
                THEN now() + (:lockout_minutes * interval '1 minute')
                ELSE locked_until
            END
        WHERE id = :user_id
    """)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "user_id": user_id,
                "max_attempts": MAX_FAILED_ATTEMPTS,
                "lockout_minutes": LOCKOUT_MINUTES,
            },
        )


#***************************************************************
#
#  Function:     record_successful_login
#
#  Description: Records a successful login for a user. The function
#               clears failed-login tracking, removes any lockout, and
#               updates the last-login timestamp.
#
#  Parameters:  user_id - Internal user ID for the successful login.
#
#  Returns:     None
#
#***************************************************************

def record_successful_login(user_id: int) -> None:
    # Clear lockout state and update the successful login timestamp.
    sql = text("""
        UPDATE app_users
        SET
            failed_login_attempts = 0,
            locked_until = NULL,
            last_login_at = now()
        WHERE id = :user_id
    """)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(sql, {"user_id": user_id})


#***************************************************************
#
#  Function:     _minutes_remaining
#
#  Description: Calculates the number of whole minutes remaining in
#               an account lockout period. Naive datetime values are
#               treated as UTC before comparison.
#
#  Parameters:  locked_until - Datetime value indicating when the
#                              lockout expires.
#
#  Returns:     int - Minutes remaining in the lockout period.
#
#***************************************************************

def _minutes_remaining(locked_until) -> int:
    if locked_until is None:
        return 0

    # Treat timezone-naive lockout values as UTC.
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=UTC)

    # Round remaining seconds up to the next full minute.
    delta = locked_until - datetime.now(UTC)
    seconds = max(0, int(delta.total_seconds()))
    minutes = max(1, (seconds + 59) // 60)
    return minutes


#***************************************************************
#
#  Function:     authenticate_user
#
#  Description: Authenticates a user by email and password. The
#               function checks for valid credentials, active account
#               status, account lockout state, missing password hashes,
#               and failed-login limits. All important outcomes are
#               written to the authentication audit log.
#
#  Parameters:  email - Email address submitted by the user.
#               password - Password submitted by the user.
#
#  Returns:     dict[str, Any] - Authentication result containing an
#                                ok flag, status code/message, and
#                                user details on success.
#
#***************************************************************

def authenticate_user(email: str, password: str) -> dict[str, Any]:
    # Normalize the email before lookup.
    normalized_email = email.strip().lower()
    user = get_user_by_email(normalized_email)

    # Return a generic login error when the user is not found.
    if not user:
        log_auth_event(
            event_type="login_failed",
            is_success=False,
            email=normalized_email,
            message="Unknown email or invalid password.",
            metadata={"reason": "user_not_found"},
        )
        return {
            "ok": False,
            "code": "invalid_credentials",
            "message": "Invalid email or password.",
        }

    # Block login for inactive accounts.
    if not user.get("is_active"):
        log_auth_event(
            event_type="login_failed",
            is_success=False,
            user_id=user["id"],
            email=user["email"],
            message="Inactive account.",
            metadata={"reason": "inactive"},
        )
        return {
            "ok": False,
            "code": "inactive",
            "message": "This account is inactive.",
        }

    # Check whether the account is currently locked from prior failures.
    locked_until = user.get("locked_until")
    if locked_until is not None:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=UTC)

        if locked_until > datetime.now(UTC):
            minutes_remaining = _minutes_remaining(locked_until)
            log_auth_event(
                event_type="login_locked",
                is_success=False,
                user_id=user["id"],
                email=user["email"],
                message=f"Login blocked during lockout. {minutes_remaining} minute(s) remaining.",
                metadata={"minutes_remaining": minutes_remaining},
            )
            return {
                "ok": False,
                "code": "locked",
                "minutes_remaining": minutes_remaining,
                "message": f"Too many failed login attempts. Try again in {minutes_remaining} minute(s).",
            }

    # Require a stored password hash before validating the supplied password.
    password_hash = user.get("password_hash")
    if not password_hash:
        log_auth_event(
            event_type="login_failed",
            is_success=False,
            user_id=user["id"],
            email=user["email"],
            message="Missing password hash.",
            metadata={"reason": "missing_password_hash"},
        )
        return {
            "ok": False,
            "code": "invalid_credentials",
            "message": "Invalid email or password.",
        }

    # Check the supplied password against the stored password hash.
    if not check_password_hash(password_hash, password):
        record_failed_login(user["id"])

        # Reload the user after the failed attempt to see whether lockout was applied.
        refreshed_user = get_user_by_email(normalized_email)
        locked_until = refreshed_user.get("locked_until") if refreshed_user else None

        if locked_until is not None:
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=UTC)

            if locked_until > datetime.now(UTC):
                minutes_remaining = _minutes_remaining(locked_until)
                log_auth_event(
                    event_type="login_locked",
                    is_success=False,
                    user_id=user["id"],
                    email=user["email"],
                    message=f"Account locked after failed login attempts. {minutes_remaining} minute(s) remaining.",
                    metadata={"minutes_remaining": minutes_remaining},
                )
                return {
                    "ok": False,
                    "code": "locked",
                    "minutes_remaining": minutes_remaining,
                    "message": f"Too many failed login attempts. Try again in {minutes_remaining} minute(s).",
                }

        # Return a generic invalid-credentials response for a bad password.
        log_auth_event(
            event_type="login_failed",
            is_success=False,
            user_id=user["id"],
            email=user["email"],
            message="Invalid password.",
            metadata={"reason": "bad_password"},
        )
        return {
            "ok": False,
            "code": "invalid_credentials",
            "message": "Invalid email or password.",
        }

    # Successful login resets failure tracking and returns safe user details.
    record_successful_login(user["id"])
    log_auth_event(
        event_type="login_success",
        is_success=True,
        user_id=user["id"],
        email=user["email"],
        message="User logged in successfully.",
    )

    return {
        "ok": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
        },
    }


#***************************************************************
#
#  Function:     change_password
#
#  Description: Changes a user's password after validating the user,
#               current password, new password length, confirmation
#               match, and difference from the current password. The
#               function updates the password hash and records the
#               outcome in the authentication audit log.
#
#  Parameters:  user_id - Internal user ID requesting the password change.
#               current_password - User's current password.
#               new_password - Requested new password.
#               confirm_password - Confirmation value for the new password.
#
#  Returns:     dict[str, Any] - Password-change result containing an
#                                ok flag, status code, and message.
#
#***************************************************************

def change_password(
    user_id: int,
    current_password: str,
    new_password: str,
    confirm_password: str,
) -> dict[str, Any]:
    # Confirm that the user exists before attempting a password change.
    user = get_user_by_id(user_id)
    if not user:
        log_auth_event(
            event_type="password_change_failed",
            is_success=False,
            user_id=user_id,
            message="User not found.",
            metadata={"reason": "user_not_found"},
        )
        return {
            "ok": False,
            "code": "user_not_found",
            "message": "User account could not be found.",
        }

    # Do not allow password changes for inactive accounts.
    if not user.get("is_active"):
        log_auth_event(
            event_type="password_change_failed",
            is_success=False,
            user_id=user["id"],
            email=user["email"],
            message="Inactive account.",
            metadata={"reason": "inactive"},
        )
        return {
            "ok": False,
            "code": "inactive",
            "message": "This account is inactive.",
        }

    # Verify the current password before accepting a new password.
    password_hash = user.get("password_hash")
    if not password_hash or not check_password_hash(password_hash, current_password):
        log_auth_event(
            event_type="password_change_failed",
            is_success=False,
            user_id=user["id"],
            email=user["email"],
            message="Current password was incorrect.",
            metadata={"reason": "invalid_current_password"},
        )
        return {
            "ok": False,
            "code": "invalid_current_password",
            "message": "Your current password is incorrect.",
        }

    new_password = new_password or ""
    confirm_password = confirm_password or ""

    # Enforce the minimum password length.
    if len(new_password) < 8:
        log_auth_event(
            event_type="password_change_failed",
            is_success=False,
            user_id=user["id"],
            email=user["email"],
            message="New password too short.",
            metadata={"reason": "password_too_short"},
        )
        return {
            "ok": False,
            "code": "password_too_short",
            "message": "Your new password must be at least 8 characters long.",
        }

    # Require the new password and confirmation to match.
    if new_password != confirm_password:
        log_auth_event(
            event_type="password_change_failed",
            is_success=False,
            user_id=user["id"],
            email=user["email"],
            message="Password confirmation mismatch.",
            metadata={"reason": "password_mismatch"},
        )
        return {
            "ok": False,
            "code": "password_mismatch",
            "message": "New password and confirmation do not match.",
        }

    # Prevent users from reusing the same password.
    if check_password_hash(password_hash, new_password):
        log_auth_event(
            event_type="password_change_failed",
            is_success=False,
            user_id=user["id"],
            email=user["email"],
            message="New password matched current password.",
            metadata={"reason": "same_password"},
        )
        return {
            "ok": False,
            "code": "same_password",
            "message": "Your new password must be different from your current password.",
        }

    # Store the new password hash and clear any failed-login or lockout state.
    sql = text("""
        UPDATE app_users
        SET
            password_hash = :password_hash,
            failed_login_attempts = 0,
            locked_until = NULL,
            last_password_changed_at = now()
        WHERE id = :user_id
    """)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "user_id": user_id,
                "password_hash": generate_password_hash(new_password),
            },
        )

    log_auth_event(
        event_type="password_change_success",
        is_success=True,
        user_id=user["id"],
        email=user["email"],
        message="Password changed successfully.",
    )

    return {
        "ok": True,
        "code": "password_changed",
        "message": "Your password was updated successfully.",
    }


#***************************************************************
#
#  Function:     create_user
#
#  Description: Creates a new active application user with a hashed
#               password. The function prevents duplicate emails,
#               inserts the new user record, logs the creation event,
#               and returns the created user details.
#
#  Parameters:  email - Email address for the new user.
#               password - Initial password for the new user.
#               full_name - Optional full name for the new user.
#
#  Returns:     dict[str, Any] - Created user record.
#
#  Raises:      ValueError - If a user with the email already exists.
#
#***************************************************************

def create_user(email: str, password: str, full_name: str = "") -> dict[str, Any]:
    # Normalize email before checking for duplicates or inserting the user.
    normalized_email = email.strip().lower()

    existing = get_user_by_email(normalized_email)
    if existing:
        log_auth_event(
            event_type="user_create_failed",
            is_success=False,
            email=normalized_email,
            message="User already exists.",
            metadata={"reason": "duplicate_email"},
        )
        raise ValueError("A user with that email already exists.")

    # Insert the new user with an active status and hashed password.
    sql = text("""
        INSERT INTO app_users (
            email,
            full_name,
            password_hash,
            is_active,
            failed_login_attempts,
            locked_until
        )
        VALUES (
            :email,
            :full_name,
            :password_hash,
            TRUE,
            0,
            NULL
        )
        RETURNING id, email, full_name, is_active, created_at
    """)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sql,
            {
                "email": normalized_email,
                "full_name": full_name.strip(),
                "password_hash": generate_password_hash(password),
            },
        ).mappings().first()

    created_user = dict(row)

    log_auth_event(
        event_type="user_create_success",
        is_success=True,
        user_id=created_user["id"],
        email=created_user["email"],
        message="User created successfully.",
    )

    return created_user
