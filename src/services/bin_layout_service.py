"""The bin layouts of an organization's sorter sites, for the customer API.

    read_bin_layouts(org_slug) -> {sorter site slug: BinLayout}

What a layout is, and where it is stored, is services.bin_layout. Only the
`bin_layout` key of each branch's own settings document is selected: no
other setting is read, and the organization's document is not read at all.

A SITE WITHOUT A VALID LAYOUT IS LEFT OUT. A layout that is stored but not
valid is logged -- the sorter site and a code, never the stored value -- and
is left out exactly as if none were stored: the sorter is then drawn from
its check-ins alone, and nothing else about the organization fails.

Keyed by the host branch's slug, which is the sorter site's slug. A branch
with a layout but no sorter is never looked up by the caller. Read-only;
writing a layout is an operator's job (scripts/set_sorter_bin_layout.py).

The caller has already established that the user may see the organization;
this module makes no access decision. Framework-neutral: no Streamlit, no
FastAPI. Database errors propagate.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

from database import get_engine
from services.bin_layout import BinLayout, BinLayoutError, parse_bin_layout

logger = logging.getLogger("sortview.bin_layouts")

# `->` on JSON: PostgreSQL hands back the parsed value; SQLite (unit tests) hands back its JSON text.
_LAYOUTS_SQL = text("""
    SELECT
        b.slug AS branch_slug,
        bs.settings_json -> 'bin_layout' AS bin_layout
    FROM organizations o
    JOIN branches b
      ON b.organization_id = o.id
    JOIN branch_settings bs
      ON bs.branch_id = b.id
    WHERE o.slug = :org_slug
      AND b.status = 'active'
""")


def read_bin_layouts(org_slug: str) -> dict[str, BinLayout]:
    """The valid layouts of the organization with this slug, by sorter site
    slug. One statement runs."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(_LAYOUTS_SQL, {"org_slug": org_slug}).mappings().all()

    layouts = {}
    for row in rows:
        stored = row["bin_layout"]
        if stored is None:
            continue
        try:
            layouts[row["branch_slug"]] = parse_bin_layout(json.loads(stored) if isinstance(stored, str) else stored)
        except (BinLayoutError, ValueError) as error:
            code = error.code if isinstance(error, BinLayoutError) else "unreadable"
            logger.warning("bin layout ignored: org=%s sorter=%s reason=%s", org_slug, row["branch_slug"], code)
    return layouts
