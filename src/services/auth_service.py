from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_engine

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 1


def log_auth_event(
    event_type: str,
    is_success: bool,
    user_id: int | None = None,
    email: str | None = None,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
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


def get_user_by_email(email: str) -> dict[str, Any] | None:
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

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"email": email.strip()}).mappings().first()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
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

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"user_id": user_id}).mappings().first()
        return dict(row) if row else None


def record_failed_login(user_id: int) -> None:
    sql = text(f"""
        UPDATE app_users
        SET
            failed_login_attempts = failed_login_attempts + 1,
            last_failed_login_at = now(),
            locked_until = CASE
                WHEN failed_login_attempts + 1 >= {MAX_FAILED_ATTEMPTS}
                THEN now() + interval '{LOCKOUT_MINUTES} minutes'
                ELSE locked_until
            END
        WHERE id = :user_id
    """)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(sql, {"user_id": user_id})


def record_successful_login(user_id: int) -> None:
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


def _minutes_remaining(locked_until) -> int:
    if locked_until is None:
        return 0

    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)

    delta = locked_until - datetime.now(timezone.utc)
    seconds = max(0, int(delta.total_seconds()))
    minutes = max(1, (seconds + 59) // 60)
    return minutes


def authenticate_user(email: str, password: str) -> dict[str, Any]:
    normalized_email = email.strip().lower()
    user = get_user_by_email(normalized_email)

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

    locked_until = user.get("locked_until")
    if locked_until is not None:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)

        if locked_until > datetime.now(timezone.utc):
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

    if not check_password_hash(password_hash, password):
        record_failed_login(user["id"])

        refreshed_user = get_user_by_email(normalized_email)
        locked_until = refreshed_user.get("locked_until") if refreshed_user else None

        if locked_until is not None:
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)

            if locked_until > datetime.now(timezone.utc):
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


def change_password(
    user_id: int,
    current_password: str,
    new_password: str,
    confirm_password: str,
) -> dict[str, Any]:
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


def create_user(email: str, password: str, full_name: str = "") -> dict[str, Any]:
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
