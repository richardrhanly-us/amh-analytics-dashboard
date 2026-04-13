from __future__ import annotations

from typing import Any

from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_engine


def get_user_by_email(email: str) -> dict[str, Any] | None:
    sql = text("""
        SELECT id, email, full_name, password_hash, is_active, created_at
        FROM app_users
        WHERE lower(email) = lower(:email)
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"email": email}).mappings().first()
        return dict(row) if row else None


def authenticate_user(email: str, password: str) -> dict[str, Any] | None:
    user = get_user_by_email(email)
    if not user:
        return None

    if not user.get("is_active"):
        return None

    password_hash = user.get("password_hash")
    if not password_hash:
        return None

    if not check_password_hash(password_hash, password):
        return None

    return {
        "id": user["id"],
        "email": user["email"],
        "full_name": user["full_name"],
    }


def create_user(email: str, password: str, full_name: str = "") -> dict[str, Any]:
    sql = text("""
        INSERT INTO app_users (email, full_name, password_hash, is_active)
        VALUES (:email, :full_name, :password_hash, TRUE)
        RETURNING id, email, full_name, is_active, created_at
    """)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sql,
            {
                "email": email,
                "full_name": full_name,
                "password_hash": generate_password_hash(password),
            },
        ).mappings().first()

    return dict(row)
