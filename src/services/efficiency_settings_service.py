"""Reads and replaces stored Efficiency settings, for the customer API.

    read_organization_efficiency(org_slug, user_id=...)                       -> OrganizationEfficiencySettings | None
    replace_organization_efficiency(org_slug, settings, user_id=...)          -> OrganizationEfficiencySettings | None
    read_sorter_efficiency(org_slug, branch_slug, user_id=...)                -> SorterEfficiency | None
    replace_sorter_efficiency(org_slug, branch_slug, settings, user_id=...)   -> SorterEfficiency | None

What the settings are, and every rule about their values, is
services.efficiency_settings' business. This module only fetches the two
settings documents and writes one key of one of them.

WHOSE SETTINGS. An organization's are in organization_settings; a sorter
site's are in the branch_settings of its host branch -- a sorter site is one
host branch (services.sorter_inventory_service), named by that branch's slug.
A site is found by the same rule the inventory lists it by: an active branch
of the organization with an installation that is not retired. It does NOT
need an operational data scope: a sorter that is registered and has reported
nothing yet can still be told what it cost.

EVERY STATEMENT SCOPES ITSELF. Neither settings table is under row level
security, so a statement's own WHERE is all that decides whose row it
touches. Each one below starts from the organization's slug AND the acting
user's membership of it as an owner or admin, and reaches a settings row only
through that organization's own ids. No id is ever taken from a caller: the
slugs and the user id are the whole of what comes in. A scope that does not
resolve -- no such organization, not an owner or admin of it, an organization
that is cancelled, no such sorter site -- is None, one answer for all of
them. Writing also needs the organization to be active or on trial: a
suspended organization's settings can be read and cannot be changed, as on
the dashboard's settings page.

ONE KEY, NEVER THE DOCUMENT. A settings document holds other things -- the
routing configuration, the admin lock -- that other code writes. A
replacement here is a single UPDATE that sets (jsonb_set) or removes the
`efficiency` key of the stored document as it is at that moment; the document
is never read out, changed and written back, so a change to any other key at
the same time is not lost. A row that does not exist yet is created holding
that key alone. Replacing the settings with none removes the key rather than
storing an empty block: "nothing configured" has one stored form.

The write statements are PostgreSQL's (JSONB). Framework-neutral: no
Streamlit, no FastAPI. Database errors propagate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping

from database import get_engine
from services.efficiency_settings import (
    EFFICIENCY_SETTINGS_KEY,
    OrganizationEfficiencySettings,
    SorterEfficiencySettings,
    parse_organization_efficiency_settings,
    parse_sorter_efficiency_settings,
    serialize_organization_efficiency_settings,
    serialize_sorter_efficiency_settings,
)


@dataclass(frozen=True, slots=True)
class SorterEfficiency:
    """What applies to one sorter site comes from two places, kept apart:
    the organization's defaults and the site's own settings. Resolving them
    is services.efficiency_settings.resolve_efficiency_settings."""

    organization: OrganizationEfficiencySettings
    sorter: SorterEfficiencySettings


# The organization in the path, for a user who is an owner or admin of it. `for_write` is 1 or 0: a suspended
# organization is found for a read and not for a write. LIMIT 2, not 1: the schema makes a second row impossible,
# and one would be refused rather than resolved to whichever came first.
_ORGANIZATION_SQL = text("""
    SELECT os.settings_json AS organization_settings
    FROM organizations o
    JOIN memberships m
      ON m.organization_id = o.id
    LEFT JOIN organization_settings os
      ON os.organization_id = o.id
    WHERE o.slug = :org_slug
      AND m.user_id = :user_id
      AND m.role IN ('owner', 'admin')
      AND (o.status IN ('active', 'trial') OR (o.status = 'suspended' AND :for_write = 0))
    LIMIT 2
""")

# The same, and one of that organization's sorter sites: an active branch of it that hosts an installation
# sorter_inventory_service would list.
_SORTER_SQL = text("""
    SELECT
        os.settings_json AS organization_settings,
        bs.settings_json AS sorter_settings
    FROM organizations o
    JOIN memberships m
      ON m.organization_id = o.id
    JOIN branches b
      ON b.organization_id = o.id
    LEFT JOIN organization_settings os
      ON os.organization_id = o.id
    LEFT JOIN branch_settings bs
      ON bs.branch_id = b.id
    WHERE o.slug = :org_slug
      AND m.user_id = :user_id
      AND m.role IN ('owner', 'admin')
      AND (o.status IN ('active', 'trial') OR (o.status = 'suspended' AND :for_write = 0))
      AND b.slug = :branch_slug
      AND b.status = 'active'
      AND EXISTS (
          SELECT 1
          FROM collector_installations ci
          WHERE ci.organization_id = o.id
            AND ci.branch_id = b.id
            AND ci.status IN ('active', 'provisioning', 'inactive')
      )
    LIMIT 2
""")

# Sets the `efficiency` key of the organization's settings document, creating the row if there is none. The
# organization's id is found by the statement itself, under the same rule as _ORGANIZATION_SQL for a write.
_SET_ORGANIZATION_SQL = text("""
    INSERT INTO organization_settings (organization_id, settings_json)
    SELECT o.id, jsonb_build_object('efficiency', CAST(:block AS JSONB))
    FROM organizations o
    JOIN memberships m
      ON m.organization_id = o.id
    WHERE o.slug = :org_slug
      AND m.user_id = :user_id
      AND m.role IN ('owner', 'admin')
      AND o.status IN ('active', 'trial')
    ON CONFLICT (organization_id) DO UPDATE
    SET settings_json = jsonb_set(organization_settings.settings_json, '{efficiency}', CAST(:block AS JSONB), true),
        updated_at = NOW()
""")

# Removes the key. A row that does not exist has no key to remove, and none is created.
_CLEAR_ORGANIZATION_SQL = text("""
    UPDATE organization_settings
    SET settings_json = settings_json - 'efficiency',
        updated_at = NOW()
    WHERE organization_id IN (
        SELECT o.id
        FROM organizations o
        JOIN memberships m
          ON m.organization_id = o.id
        WHERE o.slug = :org_slug
          AND m.user_id = :user_id
          AND m.role IN ('owner', 'admin')
          AND o.status IN ('active', 'trial')
    )
""")

_SET_SORTER_SQL = text("""
    INSERT INTO branch_settings (branch_id, settings_json)
    SELECT b.id, jsonb_build_object('efficiency', CAST(:block AS JSONB))
    FROM organizations o
    JOIN memberships m
      ON m.organization_id = o.id
    JOIN branches b
      ON b.organization_id = o.id
    WHERE o.slug = :org_slug
      AND m.user_id = :user_id
      AND m.role IN ('owner', 'admin')
      AND o.status IN ('active', 'trial')
      AND b.slug = :branch_slug
      AND b.status = 'active'
      AND EXISTS (
          SELECT 1
          FROM collector_installations ci
          WHERE ci.organization_id = o.id
            AND ci.branch_id = b.id
            AND ci.status IN ('active', 'provisioning', 'inactive')
      )
    ON CONFLICT (branch_id) DO UPDATE
    SET settings_json = jsonb_set(branch_settings.settings_json, '{efficiency}', CAST(:block AS JSONB), true),
        updated_at = NOW()
""")

_CLEAR_SORTER_SQL = text("""
    UPDATE branch_settings
    SET settings_json = settings_json - 'efficiency',
        updated_at = NOW()
    WHERE branch_id IN (
        SELECT b.id
        FROM organizations o
        JOIN memberships m
          ON m.organization_id = o.id
        JOIN branches b
          ON b.organization_id = o.id
        WHERE o.slug = :org_slug
          AND m.user_id = :user_id
          AND m.role IN ('owner', 'admin')
          AND o.status IN ('active', 'trial')
          AND b.slug = :branch_slug
          AND b.status = 'active'
          AND EXISTS (
              SELECT 1
              FROM collector_installations ci
              WHERE ci.organization_id = o.id
                AND ci.branch_id = b.id
                AND ci.status IN ('active', 'provisioning', 'inactive')
          )
    )
""")


def _settings_document(value: object) -> dict[str, Any]:
    """A stored settings_json as a dict. PostgreSQL hands JSONB back parsed; a
    driver that hands back text is parsed here. Nothing stored, or anything
    that is not an object, is an empty document."""
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return dict(value) if isinstance(value, Mapping) else {}


def _one(conn: Connection, statement, parameters: dict[str, Any]) -> RowMapping | None:
    """The one row the scope resolves to, or None. Two rows is not a scope."""
    rows = conn.execute(statement, parameters).mappings().all()
    return rows[0] if len(rows) == 1 else None


def _organization(conn: Connection, parameters: dict[str, Any], *, for_write: bool) -> OrganizationEfficiencySettings | None:
    row = _one(conn, _ORGANIZATION_SQL, {**parameters, "for_write": int(for_write)})
    if row is None:
        return None
    return parse_organization_efficiency_settings(_settings_document(row["organization_settings"]))


def _sorter(conn: Connection, parameters: dict[str, Any], *, for_write: bool) -> SorterEfficiency | None:
    row = _one(conn, _SORTER_SQL, {**parameters, "for_write": int(for_write)})
    if row is None:
        return None
    return SorterEfficiency(
        organization=parse_organization_efficiency_settings(_settings_document(row["organization_settings"])),
        sorter=parse_sorter_efficiency_settings(_settings_document(row["sorter_settings"])),
    )


def read_organization_efficiency(org_slug: str, *, user_id: int) -> OrganizationEfficiencySettings | None:
    """The organization's stored Efficiency defaults, for a user who is an
    owner or admin of it; None if that scope does not resolve. Raises
    EfficiencySettingsError if what is stored is malformed."""
    engine = get_engine()
    with engine.connect() as conn:
        return _organization(conn, {"org_slug": org_slug, "user_id": user_id}, for_write=False)


def replace_organization_efficiency(
    org_slug: str, settings: OrganizationEfficiencySettings, *, user_id: int
) -> OrganizationEfficiencySettings | None:
    """Makes `settings` the whole of the organization's Efficiency block and
    returns what is then stored; None, with nothing written, if the scope
    does not resolve for a write. One transaction."""
    parameters = {"org_slug": org_slug, "user_id": user_id}
    block = serialize_organization_efficiency_settings(settings)

    engine = get_engine()
    with engine.begin() as conn:
        # Whether there is anything to write to. Not parsed: what is stored now is about to be replaced.
        if _one(conn, _ORGANIZATION_SQL, {**parameters, "for_write": 1}) is None:
            return None
        if block:
            conn.execute(_SET_ORGANIZATION_SQL, {**parameters, "block": json.dumps(block)})
        else:
            conn.execute(_CLEAR_ORGANIZATION_SQL, parameters)
        return _organization(conn, parameters, for_write=True)


def read_sorter_efficiency(org_slug: str, branch_slug: str, *, user_id: int) -> SorterEfficiency | None:
    """The organization's defaults and the sorter site's own settings, for a
    user who is an owner or admin of the organization; None if that scope
    does not resolve. Raises EfficiencySettingsError if either stored block
    is malformed."""
    engine = get_engine()
    with engine.connect() as conn:
        return _sorter(conn, {"org_slug": org_slug, "branch_slug": branch_slug, "user_id": user_id}, for_write=False)


def replace_sorter_efficiency(
    org_slug: str, branch_slug: str, settings: SorterEfficiencySettings, *, user_id: int
) -> SorterEfficiency | None:
    """Makes `settings` the whole of the sorter site's Efficiency block and
    returns the organization's defaults with what is then stored for the
    site; None, with nothing written, if the scope does not resolve for a
    write. One transaction.

    The organization's own block is read first: if it is malformed this
    raises EfficiencySettingsError before anything is written, since the
    answer -- which says what each rate resolves to -- could not be given.
    """
    parameters = {"org_slug": org_slug, "branch_slug": branch_slug, "user_id": user_id}
    block = serialize_sorter_efficiency_settings(settings)

    engine = get_engine()
    with engine.begin() as conn:
        row = _one(conn, _SORTER_SQL, {**parameters, "for_write": 1})
        if row is None:
            return None
        # The site's own stored block is not parsed: it is about to be replaced.
        parse_organization_efficiency_settings(_settings_document(row["organization_settings"]))
        if block:
            conn.execute(_SET_SORTER_SQL, {**parameters, "block": json.dumps(block)})
        else:
            conn.execute(_CLEAR_SORTER_SQL, parameters)
        return _sorter(conn, parameters, for_write=True)


__all__ = [
    "EFFICIENCY_SETTINGS_KEY",
    "SorterEfficiency",
    "read_organization_efficiency",
    "read_sorter_efficiency",
    "replace_organization_efficiency",
    "replace_sorter_efficiency",
]
