"""A small real database for tests about organization memberships: the account tables as the migrated schema has
them (memberships.removed_at included), in an in-memory SQLite, with helpers to say who belongs where.

Every query a service runs against it runs for real. Two things differ from PostgreSQL and are handled here:

  * the audit log's INSERT casts its metadata to jsonb, which SQLite cannot do -- `record_audit` stands in for
    auth_service.log_auth_event_with_connection and stores the same row with the metadata as JSON text;
  * there are no row locks -- the services skip FOR UPDATE on SQLite, as the repository's convention has it. Locking is
    exercised against a real server in tests/test_user_admin_postgres.py.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

SCHEMA = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, status TEXT, operational_customer_id INTEGER)",
    (
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, name TEXT, is_primary BOOLEAN, "
        "status TEXT, operational_branch_id INTEGER)"
    ),
    (
        "CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT UNIQUE, full_name TEXT DEFAULT '', password_hash TEXT, "
        "is_active BOOLEAN NOT NULL DEFAULT 1, is_platform_admin BOOLEAN NOT NULL DEFAULT 0, "
        "failed_login_attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT, last_login_at TEXT, "
        "last_password_changed_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    ),
    (
        "CREATE TABLE memberships (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
        "role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'manager', 'viewer')), created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
        "removed_at TEXT, UNIQUE (organization_id, user_id))"
    ),
    (
        "CREATE TABLE auth_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, email TEXT, event_type TEXT NOT NULL, "
        "is_success BOOLEAN NOT NULL, message TEXT, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    ),
    (
        "CREATE TABLE auth_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL, revoked_at TEXT, last_seen_at TEXT)"
    ),
    "CREATE TABLE organization_settings (id INTEGER PRIMARY KEY, organization_id INTEGER UNIQUE, settings_json TEXT)",
    "CREATE TABLE branch_settings (id INTEGER PRIMARY KEY, branch_id INTEGER UNIQUE, settings_json TEXT)",
    "CREATE TABLE collector_installations (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, name TEXT, status TEXT)",
)


def record_audit(conn: Any, event_type: str, is_success: bool, user_id: int | None = None, email: str | None = None,
                 message: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    """auth_service.log_auth_event_with_connection, for SQLite: the same row, on the same connection."""
    conn.execute(
        text("INSERT INTO auth_audit_log (user_id, email, event_type, is_success, message, metadata) VALUES (:u, :e, :t, :s, :m, :md)"),
        {"u": user_id, "e": email, "t": event_type, "s": is_success, "m": message, "md": json.dumps(metadata or {})},
    )


class MembershipDatabase:
    """The database and the vocabulary of the tests that use it. Ids are whatever a test gives: they mean nothing."""

    def __init__(self) -> None:
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        with self.engine.begin() as conn:
            for statement in SCHEMA:
                conn.execute(text(statement))

    def run(self, sql: str, **parameters: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(sql), parameters)

    def rows(self, sql: str, **parameters: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(sql), parameters).mappings()]

    def organization(self, organization_id: int, slug: str, status: str = "active", customer: int | None = None) -> int:
        self.run("INSERT INTO organizations (id, slug, name, status, operational_customer_id) VALUES (:i, :s, :n, :st, :c)",
                 i=organization_id, s=slug, n=f"{slug.title()} Library", st=status, c=customer)
        return organization_id

    def user(self, user_id: int, email: str | None = None, *, active: bool = True, platform_admin: bool = False) -> int:
        self.run("INSERT INTO app_users (id, email, password_hash, is_active, is_platform_admin) VALUES (:i, :e, 'CANARY-HASH', :a, :p)",
                 i=user_id, e=email or f"user{user_id}@example.invalid", a=active, p=platform_admin)
        return user_id

    def member(self, organization_id: int, user_id: int, role: str, *, removed: bool = False) -> None:
        self.run("INSERT INTO memberships (organization_id, user_id, role, removed_at) VALUES (:o, :u, :r, :x)",
                 o=organization_id, u=user_id, r=role, x="2026-01-01 00:00:00+00:00" if removed else None)

    def session(self, user_id: int, token_hash: str) -> None:
        self.run("INSERT INTO auth_sessions (user_id, token_hash, expires_at) VALUES (:u, :t, '2999-01-01 00:00:00+00:00')", u=user_id, t=token_hash)

    # --- what a test then looks at -----------------------------------------------------------------------------

    def membership(self, organization_id: int, user_id: int) -> dict[str, Any] | None:
        found = self.rows("SELECT role, removed_at FROM memberships WHERE organization_id = :o AND user_id = :u", o=organization_id, u=user_id)
        assert len(found) <= 1, "a person has at most one membership row per organization"
        return found[0] if found else None

    def active_role(self, organization_id: int, user_id: int) -> str | None:
        membership = self.membership(organization_id, user_id)
        return None if membership is None or membership["removed_at"] is not None else membership["role"]

    def account_active(self, user_id: int) -> bool:
        return bool(self.rows("SELECT is_active FROM app_users WHERE id = :u", u=user_id)[0]["is_active"])

    def live_sessions(self, user_id: int) -> int:
        return len(self.rows("SELECT 1 FROM auth_sessions WHERE user_id = :u AND revoked_at IS NULL", u=user_id))

    def audit(self) -> list[dict[str, Any]]:
        events = self.rows("SELECT user_id, email, event_type, is_success, message, metadata FROM auth_audit_log ORDER BY id")
        return [{**event, "metadata": json.loads(event["metadata"])} for event in events]

    def snapshot(self) -> tuple:
        """Everything a refused change must leave exactly as it was."""
        return (
            self.rows("SELECT * FROM memberships ORDER BY id"),
            self.rows("SELECT id, email, is_active, is_platform_admin, password_hash FROM app_users ORDER BY id"),
            self.rows("SELECT * FROM auth_sessions ORDER BY id"),
            self.rows("SELECT * FROM auth_audit_log ORDER BY id"),
        )
