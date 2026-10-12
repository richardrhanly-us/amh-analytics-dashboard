"""Reads and replaces an organization's stored SortView Value settings, for the customer API.

    read_sortview_value(org_slug, user_id=...)               -> StoredSortViewValue | None
    replace_sortview_value(org_slug, settings, user_id=...)  -> StoredSortViewValue | None

What the settings are, every rule about their values, and every figure made
from them, is services.sortview_value's business. This module only fetches
two keys of one settings document and writes one of them. Nothing is worked
out here.

WHAT IS READ. organization_settings.settings_json["sortview_value"], and
beside it the organization's Efficiency block, for one thing only: the labor
rate that applies when the SortView Value block has none of its own
(services.sortview_value.resolve_labor_rate). Only those two keys are ever
selected, so nothing else of the document reaches this process's answer. No
branch_settings row is read at all: a sorter site's own Efficiency override
is not the organization's, and cannot become a SortView Value rate.

A STORED EFFICIENCY BLOCK THAT CANNOT BE READ is not this module's fault to
report: the SortView Value settings are still read, with no Efficiency rate
to fall back on (`efficiency_unreadable` says so). A stored SortView Value
block that cannot be read is: services.sortview_value.SortViewValueError.

EVERY STATEMENT SCOPES ITSELF, as in services.efficiency_settings_service.
The settings table is not under row level security, so a statement's own
WHERE is all that decides whose row it touches. Each one starts from the
organization's slug AND the acting user's ACTIVE membership of it as an owner
or admin. No id is ever taken from a caller: the slug and the user id are the
whole of what comes in. A scope that does not resolve -- no such
organization, not an owner or admin of it, a membership that was removed, an
organization that is cancelled -- is None, one answer for all of them.
Writing also needs the organization to be active or on trial: a suspended
organization's settings can be read and cannot be changed.

ONE KEY, NEVER THE DOCUMENT. The document holds other things -- the
Efficiency settings, the routing configuration, the admin lock -- that other
code writes. A replacement is a single statement that sets (jsonb_set) or
removes the `sortview_value` key of the stored document as it is at that
moment; the document is never read out, changed and written back, so a
change to any other key at the same time is not lost. A row that does not
exist yet is created holding that key alone. Replacing the settings with none
removes the key rather than storing an empty block: "nothing configured" has
one stored form. The block is always stored whole: the last write of it wins.

The write statements are PostgreSQL's (JSONB). Framework-neutral: no
Streamlit, no FastAPI. Database errors propagate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping

from database import get_engine
from services.efficiency_settings import (
    EFFICIENCY_SETTINGS_KEY,
    EfficiencySettingsError,
    OrganizationEfficiencySettings,
    parse_organization_efficiency_settings,
)
from services.sortview_value import (
    SORTVIEW_VALUE_SETTINGS_KEY,
    SortViewValueSettings,
    parse_sortview_value_settings,
    serialize_sortview_value_settings,
)


@dataclass(frozen=True, slots=True)
class StoredSortViewValue:
    """An organization's SortView Value settings as stored, and what its
    labor rate falls back on: the organization's Efficiency defaults.

    `efficiency_unreadable`: the stored Efficiency block is malformed, so
    `organization_efficiency` is empty -- there is no rate to fall back on.
    That is reported, never raised: the SortView Value settings are read
    regardless. Resolving the rate is services.sortview_value.resolve_labor_rate."""

    settings: SortViewValueSettings
    organization_efficiency: OrganizationEfficiencySettings
    efficiency_unreadable: bool = False


# The organization in the path, for a user who is an owner or admin of it, and the two keys of its settings
# document this module reads. `for_write` is 1 or 0: a suspended organization is found for a read and not for a
# write. LIMIT 2, not 1: the schema makes a second row impossible, and one would be refused rather than resolved
# to whichever came first.
_ORGANIZATION_SQL = text("""
    SELECT
        os.settings_json -> 'sortview_value' AS sortview_value_block,
        os.settings_json -> 'efficiency' AS efficiency_block
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

# Sets the `sortview_value` key of the organization's settings document, creating the row if there is none. The
# organization's id is found by the statement itself, under the same rule as _ORGANIZATION_SQL for a write.
_SET_SQL = text("""
    INSERT INTO organization_settings (organization_id, settings_json)
    SELECT o.id, jsonb_build_object('sortview_value', CAST(:block AS JSONB))
    FROM organizations o
    JOIN memberships m
      ON m.organization_id = o.id
    WHERE o.slug = :org_slug
      AND m.user_id = :user_id
      AND m.role IN ('owner', 'admin')
      AND m.removed_at IS NULL
      AND o.status IN ('active', 'trial')
    ON CONFLICT (organization_id) DO UPDATE
    SET settings_json = jsonb_set(organization_settings.settings_json, '{sortview_value}', CAST(:block AS JSONB), true),
        updated_at = NOW()
""")

# Removes the key. A row that does not exist has no key to remove, and none is created.
_CLEAR_SQL = text("""
    UPDATE organization_settings
    SET settings_json = settings_json - 'sortview_value',
        updated_at = NOW()
    WHERE organization_id IN (
        SELECT o.id
        FROM organizations o
        JOIN memberships m
          ON m.organization_id = o.id
        WHERE o.slug = :org_slug
          AND m.user_id = :user_id
          AND m.role IN ('owner', 'admin')
          AND m.removed_at IS NULL
          AND o.status IN ('active', 'trial')
    )
""")


def stored_sortview_value(sortview_value_block: object, efficiency_block: object) -> StoredSortViewValue:
    """What the two stored blocks say. Raises SortViewValueError if the
    SortView Value block is malformed. A malformed Efficiency block is not
    raised: it leaves no rate to fall back on, and says so."""
    settings = parse_sortview_value_settings({SORTVIEW_VALUE_SETTINGS_KEY: sortview_value_block})
    try:
        efficiency = parse_organization_efficiency_settings({EFFICIENCY_SETTINGS_KEY: efficiency_block})
    except EfficiencySettingsError:
        return StoredSortViewValue(settings, OrganizationEfficiencySettings(), efficiency_unreadable=True)
    return StoredSortViewValue(settings, efficiency)


def _one(conn: Connection, parameters: dict[str, Any], *, for_write: bool) -> RowMapping | None:
    """The one row the scope resolves to, or None. Two rows is not a scope."""
    rows = conn.execute(_ORGANIZATION_SQL, {**parameters, "for_write": int(for_write)}).mappings().all()
    return rows[0] if len(rows) == 1 else None


def _stored(row: RowMapping) -> StoredSortViewValue:
    # Each is one key of a JSONB document, which PostgreSQL hands back already decoded: an object as a dict, a JSON
    # string as a str. Neither is decoded again -- a stored string is a malformed block, whatever text it holds.
    return stored_sortview_value(row["sortview_value_block"], row["efficiency_block"])


def read_sortview_value(org_slug: str, *, user_id: int) -> StoredSortViewValue | None:
    """The organization's stored SortView Value settings and its Efficiency
    defaults, for a user who is an owner or admin of it; None if that scope
    does not resolve. Raises SortViewValueError if the stored SortView Value
    block is malformed."""
    engine = get_engine()
    with engine.connect() as conn:
        row = _one(conn, {"org_slug": org_slug, "user_id": user_id}, for_write=False)
        return None if row is None else _stored(row)


def replace_sortview_value(org_slug: str, settings: SortViewValueSettings, *, user_id: int) -> StoredSortViewValue | None:
    """Makes `settings` the whole of the organization's SortView Value block
    and returns what is then stored; None, with nothing written, if the scope
    does not resolve for a write. One transaction.

    What is stored now is not parsed first: it is about to be replaced, so a
    malformed block is repaired by replacing it."""
    parameters = {"org_slug": org_slug, "user_id": user_id}
    block = serialize_sortview_value_settings(settings)

    engine = get_engine()
    with engine.begin() as conn:
        if _one(conn, parameters, for_write=True) is None:
            return None
        if block:
            conn.execute(_SET_SQL, {**parameters, "block": json.dumps(block)})
        else:
            conn.execute(_CLEAR_SQL, parameters)
        row = _one(conn, parameters, for_write=True)
        return None if row is None else _stored(row)


__all__ = [
    "SORTVIEW_VALUE_SETTINGS_KEY",
    "StoredSortViewValue",
    "read_sortview_value",
    "replace_sortview_value",
    "stored_sortview_value",
]
