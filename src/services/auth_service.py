from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_engine

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


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
            created_at
        FROM app_users
        WHERE lower(email) = lower(:email)
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"email": email.strip()}).mappings().first()
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
    user = get_user_by_email(email)

    if not user:
        return {
            "ok": False,
            "code": "invalid_credentials",
            "message": "Invalid email or password.",
        }

    if not user.get("is_active"):
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
            return {
                "ok": False,
                "code": "locked",
                "minutes_remaining": minutes_remaining,
                "message": f"Too many failed login attempts. Try again in {minutes_remaining} minute(s).",
            }

    password_hash = user.get("password_hash")
    if not password_hash:
        return {
            "ok": False,
            "code": "invalid_credentials",
            "message": "Invalid email or password.",
        }

    if not check_password_hash(password_hash, password):
        record_failed_login(user["id"])

        refreshed_user = get_user_by_email(email)
        locked_until = refreshed_user.get("locked_until") if refreshed_user else None

        if locked_until is not None:
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)

            if locked_until > datetime.now(timezone.utc):
                minutes_remaining = _minutes_remaining(locked_until)
                return {
                    "ok": False,
                    "code": "locked",
                    "minutes_remaining": minutes_remaining,
                    "message": f"Too many failed login attempts. Try again in {minutes_remaining} minute(s).",
                }

        return {
            "ok": False,
            "code": "invalid_credentials",
            "message": "Invalid email or password.",
        }

    record_successful_login(user["id"])

    return {
        "ok": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
        },
    }


def create_user(email: str, password: str, full_name: str = "") -> dict[str, Any]:
    normalized_email = email.strip().lower()

    existing = get_user_by_email(normalized_email)
    if existing:
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

    return dict(row)
