"""The holds report for one sorter site, for the customer API: how many PUBLIC holds, and how many INTERLIBRARY LOAN
holds, over a range of local calendar days.

    get_holds_report(conn, tenant, window) -> HoldsReport(public_hold_count, ill_hold_count)

WHAT IS COUNTED is exactly what the dashboard counts, by the dashboard's own classifiers -- not a copy of them:

    v1 (before the site's cutover)   metrics.build_acs_item_summary, from the legacy ACS rows, classified when this
                                     report is read, with the legacy hold rules (services.hold_rules) of the site
    v2 (from the cutover on)         metrics_v2.build_acs_item_summary_v2, from the flags the collector sent with each
                                     hold, classified on the collector by its own local rules

A PUBLIC hold is a hold -- the latest record of its item, within that era's part of the range -- that is not ILL and
is not for one of the library's own service accounts. ILL holds are the holds flagged ILL. A hold can be both ILL and
for a service account; it then counts as ILL and is still not public. Both numbers are each classifier's own output,
taken as it gives them: nothing is subtracted from anything here.

They are RANGE TOTALS. Each era keeps only the latest record of each item inside its own part of the range, so the
total for a range is not the sum of its days' totals, and no per-day figure is given. An item with records on both
sides of the cutover is counted once in each era: a legacy barcode and a current item key cannot be connected.

THE ERAS are the other range reports' (services.operational_report_service.ReportWindow): a legacy row (naive local
wall-clock time) counts strictly before the cutover, a current row (an instant) at or after it. An era that owns none
of the range is not read at all -- and the legacy hold rules are read only when the legacy era owns part of it.

WHAT IS READ, and why it never leaves this module. The legacy classifier works from each legacy row's raw message
and patron id, and from the legacy rules: lists of patron ACCOUNT names, some of which can be a person's own. Those
are read here, on the tenant's scoped connection, handed to the classifier, and dropped. What this module returns is
two integers. The classifier's supporting frames, the rows, the rules and the internal category totals (Branch
Services, Collection Services) are not returned, kept or logged. The transit destinations are not read: neither
number depends on them.

Framework-neutral: no Streamlit, no FastAPI, no caching, no engine. Every read takes a connection the caller
supplies -- already scoped to the tenant by row level security -- and database errors propagate: one era's count is
never returned as the whole.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import Connection

import metrics
import metrics_v2
from services.hold_rules import INTERNAL_ROUTING_KEY, V1HoldRules, v1_hold_rules
from services.operational_report_service import ReportWindow
from services.tenant_resolution_service import ResolvedOperationalTenant
from services.tenant_service import _deep_merge_settings

# The legacy rows the legacy classifier reads, and only those: its item records (a raw message starting `101`) and
# its patron records (message 64), with the columns it uses. Both conditions are the classifier's own.
_V1_COLUMNS = ("datetime", "message_code", "barcode", "destination", "patron_id", "raw_message")
_V1_ROWS = text("""
    SELECT event_time, message_code, barcode, destination, patron_id, raw_message
    FROM acs_events
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :span_start
      AND event_time < :span_end
      AND (raw_message LIKE '101%' OR btrim(message_code) = '64')
    ORDER BY event_time
""").bindparams(bindparam("span_start", type_=DateTime(timezone=False)), bindparam("span_end", type_=DateTime(timezone=False)))

# The current rows, with the columns the current summary reads. They hold no patron data at all.
_V2_COLUMNS = ("datetime", "item_key", "state", "destination", "is_ill", "is_branch_services", "is_collection_services")
_V2_ROWS = text("""
    SELECT event_time, item_key, state, destination, is_ill, is_branch_services, is_collection_services
    FROM acs_item_events
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :span_start
      AND event_time < :span_end
    ORDER BY event_time
""").bindparams(bindparam("span_start", type_=DateTime(timezone=True)), bindparam("span_end", type_=DateTime(timezone=True)))

# The legacy hold rules of the tenant's site: the `internal_routing` key of the organization's settings and of the
# site's branch settings, and nothing else of either document. Neither table is under row level security, so this
# statement's own filter is the only thing scoping it: both ids are bound from the tenant that was resolved for the
# request. LIMIT 2, not 1: a second row would be refused rather than resolved to whichever came first.
_V1_RULES = text("""
    SELECT
        os.settings_json -> 'internal_routing' AS organization_rules,
        COALESCE(jsonb_exists(os.settings_json, 'internal_routing'), FALSE) AS organization_has_rules,
        bs.settings_json -> 'internal_routing' AS branch_rules,
        COALESCE(jsonb_exists(bs.settings_json, 'internal_routing'), FALSE) AS branch_has_rules
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


@dataclass(frozen=True, slots=True)
class HoldsReport:
    """A sorter site's two hold counts over a range. Nothing else of what was read."""

    public_hold_count: int
    ill_hold_count: int


def _scope(tenant: ResolvedOperationalTenant, span: tuple[Any, Any]) -> dict[str, Any]:
    return {
        "customer_id": tenant.operational_customer_id,
        "branch_id": tenant.operational_branch_id,
        "span_start": span[0],
        "span_end": span[1],
    }


def _frame(rows, columns: tuple[str, ...]) -> pd.DataFrame:
    # Each statement's first column is event_time, which the classifiers call `datetime`.
    return pd.DataFrame([tuple(row) for row in rows], columns=list(columns))


def legacy_hold_rules(conn: Connection, tenant: ResolvedOperationalTenant) -> V1HoldRules:
    """The legacy hold rules that apply at the tenant's site: the organization's `internal_routing` block with the
    site's own merged over it, exactly as the dashboard's effective settings are built
    (services.tenant_service.get_effective_settings). A tenant that was just resolved always has exactly one such
    row; anything else is an internal fault, never an empty set of rules."""
    rows = conn.execute(
        _V1_RULES, {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id}
    ).mappings().all()
    if len(rows) != 1:
        raise RuntimeError("The hold rules of the resolved tenant could not be read as exactly one row.")
    row = rows[0]

    # The merge is the dashboard's own, applied to the one key: a key the branch's document has replaces or is merged
    # into the organization's, and a key it does not have leaves the organization's as it is.
    organization: dict[str, Any] = {INTERNAL_ROUTING_KEY: row["organization_rules"]} if row["organization_has_rules"] else {}
    branch: dict[str, Any] = {INTERNAL_ROUTING_KEY: row["branch_rules"]} if row["branch_has_rules"] else {}
    effective: Mapping[str, Any] = _deep_merge_settings(organization, branch)
    return v1_hold_rules(effective.get(INTERNAL_ROUTING_KEY))


def get_holds_report(conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow) -> HoldsReport:
    """The site's public and ILL hold counts over the window: each era by its own classifier, then the two added."""
    public = ill = 0

    if window.v1_span is not None:
        rules = legacy_hold_rules(conn, tenant)
        rows = conn.execute(_V1_ROWS, _scope(tenant, window.v1_span)).all()
        legacy = metrics.build_acs_item_summary(
            _frame(rows, _V1_COLUMNS),
            [],  # transit labels: they only split ILL by destination, which this report does not give
            rules.branch_services_names,
            rules.collection_services_names,
            rules.branch_services_da_patterns,
            rules.collection_services_da_patterns,
        )
        public, ill = public + int(legacy["holds_total"]), ill + int(legacy["ill_total"])

    if window.v2_span is not None:
        rows = conn.execute(_V2_ROWS, _scope(tenant, window.v2_span)).all()
        current = metrics_v2.build_acs_item_summary_v2(_frame(rows, _V2_COLUMNS), [])
        public, ill = public + int(current["holds_total"]), ill + int(current["ill_total"])

    return HoldsReport(public_hold_count=public, ill_hold_count=ill)
