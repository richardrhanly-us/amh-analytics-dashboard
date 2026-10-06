"""Reads a sorter site's routing configuration for the customer API.

    get_routing_config(conn, tenant) -> RoutingConfig(home_label, home_keys, transit)

The configuration is in the same settings the dashboard reads
(services.tenant_service.get_effective_settings): organization_settings
.settings_json with the site's branch_settings.settings_json merged over it.
What the `transit` block in them means is services.routing_destination's
business; this module only fetches the two documents for the tenant that was
resolved for the request and merges them the way the dashboard does.

Framework-neutral: no Streamlit, no FastAPI, no pandas, no caching, no engine.
The read takes a connection the caller supplies; database errors propagate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from services.routing_destination import RoutingConfig, build_routing_config
from services.tenant_resolution_service import ResolvedOperationalTenant
from services.tenant_service import _deep_merge_settings

# Neither settings table is under row level security, so this statement's own
# filter is the only thing scoping the read: both ids are bound from the
# tenant that was resolved for the request, never from request input. LIMIT 2,
# not 1: the schema makes a second row impossible, and one would be refused
# rather than resolved to whichever came first.
_SITE_SETTINGS_SQL = text("""
    SELECT
        b.name AS site_name,
        os.settings_json AS organization_settings,
        bs.settings_json AS branch_settings
    FROM organizations o
    JOIN branches b
      ON b.organization_id = o.id
    LEFT JOIN organization_settings os
      ON os.organization_id = o.id
    LEFT JOIN branch_settings bs
      ON bs.branch_id = b.id
    WHERE o.operational_customer_id = :customer_id
      AND b.operational_branch_id = :branch_id
    LIMIT 2
""")


def _settings_document(value: object) -> dict[str, Any]:
    """A stored settings_json as a dict. PostgreSQL hands JSONB back parsed; a
    driver that hands back text is parsed here. Nothing stored, or anything
    that is not an object, is an empty document."""
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return dict(value) if isinstance(value, Mapping) else {}


def get_routing_config(conn: Connection, tenant: ResolvedOperationalTenant) -> RoutingConfig:
    """The routing configuration of the tenant's sorter site: the
    organization's settings with the site's own merged over them, exactly as
    the dashboard's effective settings are built.

    One statement runs. A tenant that was just resolved always has exactly
    one such row; anything else is an internal fault (RuntimeError), never an
    empty configuration.
    """
    rows = conn.execute(
        _SITE_SETTINGS_SQL,
        {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id},
    ).mappings().all()

    if len(rows) != 1:
        raise RuntimeError("The settings of the resolved tenant could not be read as exactly one row.")
    row = rows[0]

    effective = _deep_merge_settings(
        _settings_document(row["organization_settings"]),
        _settings_document(row["branch_settings"]),
    )
    return build_routing_config(effective, site_name=row["site_name"] or "")
