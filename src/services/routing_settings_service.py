"""Reads and replaces an organization's stored routing settings, for the customer API.

    read_organization_routing(org_slug, user_id=...)                -> RoutingSettings | None
    replace_organization_routing(org_slug, settings, user_id=...)   -> RoutingSettings | None

What the settings are, and every rule about their values, is
services.routing_settings' business. This module only fetches one key of one
settings document and writes that key.

THE ORGANIZATION'S OWN BLOCK, AND ONLY THAT. What is read and written is
organization_settings.settings_json["transit"]. It is NOT a sorter site's
effective routing: the reports merge the `transit` of the site's
branch_settings over this block (services.routing_config_service), and
nothing here looks at branch_settings at all. An organization whose site has
a block of its own will see, in a report, something this module did not
return -- deliberately: this is what an administrator edits, not what a
report resolved.

EVERY STATEMENT SCOPES ITSELF, as in services.efficiency_settings_service.
The settings table is not under row level security, so a statement's own
WHERE is all that decides whose row it touches. Each one starts from the
organization's slug AND the acting user's ACTIVE membership of it as an owner
or admin. No id is taken from a caller. A scope that does not resolve -- no
such organization, not an owner or admin of it, a membership that was
removed, an organization that is cancelled -- is None, one answer for all of
them. Writing also needs the organization to be active or on trial: a
suspended organization's settings can be read and cannot be changed.

ONE KEY, NEVER THE DOCUMENT. The same document holds other things -- the
Efficiency settings, the internal routing lists, the admin lock -- that other
code writes. Only the `transit` key is ever selected, so nothing else in the
document reaches this process's answer. A replacement is a single statement
that sets (jsonb_set) that key of the stored document as it is at that
moment: the document is never read out, changed and written back, so a
change to any other key at the same time is not lost. A row that does not
exist yet is created holding that key alone. The block is always stored
whole; it is never merged with what was there.

A REPLACEMENT TAKES EFFECT IN EVERY REPORT AT ONCE, for every day the report
covers (see services.routing_settings): classification happens when a report
is read. Nothing here keeps the previous settings.

The write statement is PostgreSQL's (JSONB). Framework-neutral: no Streamlit,
no FastAPI. Database errors propagate.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping

from database import get_engine
from services.routing_settings import (
    ROUTING_SETTINGS_KEY,
    RoutingSettings,
    parse_stored_routing_settings,
    serialize_routing_settings,
)

# The `transit` key of the organization in the path, for a user who is an owner or admin of it. `for_write` is 1 or
# 0: a suspended organization is found for a read and not for a write. LIMIT 2, not 1: the schema makes a second
# row impossible, and one would be refused rather than resolved to whichever came first.
_ORGANIZATION_SQL = text("""
    SELECT os.settings_json -> 'transit' AS routing
    FROM organizations o
    JOIN memberships m
      ON m.organization_id = o.id
    LEFT JOIN organization_settings os
      ON os.organization_id = o.id
    WHERE o.slug = :org_slug
      AND m.user_id = :user_id
      AND m.role IN ('owner', 'admin')
      AND m.removed_at IS NULL
      AND (o.status IN ('active', 'trial') OR (o.status = 'suspended' AND :for_write = 0))
    LIMIT 2
""")

# Sets the `transit` key of the organization's settings document, creating the row if there is none. The
# organization's id is found by the statement itself, under the same rule as _ORGANIZATION_SQL for a write.
_SET_ORGANIZATION_SQL = text("""
    INSERT INTO organization_settings (organization_id, settings_json)
    SELECT o.id, jsonb_build_object('transit', CAST(:block AS JSONB))
    FROM organizations o
    JOIN memberships m
      ON m.organization_id = o.id
    WHERE o.slug = :org_slug
      AND m.user_id = :user_id
      AND m.role IN ('owner', 'admin')
      AND m.removed_at IS NULL
      AND o.status IN ('active', 'trial')
    ON CONFLICT (organization_id) DO UPDATE
    SET settings_json = jsonb_set(organization_settings.settings_json, '{transit}', CAST(:block AS JSONB), true),
        updated_at = NOW()
""")


def _one(conn: Connection, parameters: dict[str, Any], *, for_write: bool) -> RowMapping | None:
    """The one row the scope resolves to, or None. Two rows is not a scope."""
    rows = conn.execute(_ORGANIZATION_SQL, {**parameters, "for_write": int(for_write)}).mappings().all()
    return rows[0] if len(rows) == 1 else None


def _routing(conn: Connection, parameters: dict[str, Any], *, for_write: bool) -> RoutingSettings | None:
    row = _one(conn, parameters, for_write=for_write)
    if row is None:
        return None
    # PostgreSQL hands the JSONB value back already parsed, whatever it is. It is passed on as it is: a stored
    # value that is text, a number or a list is not a block, and is read as no settings -- never parsed again.
    return parse_stored_routing_settings(row["routing"])


def read_organization_routing(org_slug: str, *, user_id: int) -> RoutingSettings | None:
    """The organization's own stored routing settings, for a user who is an
    owner or admin of it; None if that scope does not resolve. Whatever is
    stored is read leniently and never refused."""
    engine = get_engine()
    with engine.connect() as conn:
        return _routing(conn, {"org_slug": org_slug, "user_id": user_id}, for_write=False)


def replace_organization_routing(org_slug: str, settings: RoutingSettings, *, user_id: int) -> RoutingSettings | None:
    """Makes `settings` the whole of the organization's routing block and
    returns what is then stored; None, with nothing written, if the scope
    does not resolve for a write. One transaction."""
    parameters = {"org_slug": org_slug, "user_id": user_id}
    block = serialize_routing_settings(settings)

    engine = get_engine()
    with engine.begin() as conn:
        # Whether there is anything to write to. What is stored now is about to be replaced.
        if _one(conn, parameters, for_write=True) is None:
            return None
        conn.execute(_SET_ORGANIZATION_SQL, {**parameters, "block": json.dumps(block)})
        return _routing(conn, parameters, for_write=True)


__all__ = [
    "ROUTING_SETTINGS_KEY",
    "read_organization_routing",
    "replace_organization_routing",
]
