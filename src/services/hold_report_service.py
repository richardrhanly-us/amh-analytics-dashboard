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

They are SUMS OF DAILY COUNTS (UI2 v7). Each local calendar day is classified on its own -- each item counted at most
once that day, by its latest record that day -- and the days' counts are added. So a range's total is exactly the sum
of its days' one-day reports, and a one-day range is what Live Today's "Holds today" shows. A multi-day total is
summed daily hold classifications, NOT the number of distinct holds placed or distinct items: an item held on three
days counts three times. On a cutover day each era's part is classified alone. A legacy day pairs its holds with the
patron records of that day and of the V1_PATRON_LOOKBACK before it (a fixed window per day), so it is classified the
same in every range that contains it. That lookback is how far back a patron's identity is found, not a report limit.

Memory is bounded: rows are read DAYS_PER_READ days at a time, each day classified and its rows dropped, so a long
range never holds more than one block of days (and, in the legacy era, one patron record per patron). No row is read
twice: the patron records before a range are read once, and later ones are carried forward from the blocks read.

THE ERAS are the other range reports' (services.operational_report_service.ReportWindow): a legacy row (naive local
wall-clock time) counts strictly before the cutover, a current row (an instant) at or after it. An era that owns none
of the range is not read at all -- and the legacy hold rules are read only when the legacy era owns part of it.

WHAT IS READ, and why it never leaves this module. The legacy classifier works from each legacy row's raw message
and patron id, and from the legacy rules: lists of patron ACCOUNT names, some of which can be a person's own. Those
are read here, on the tenant's scoped connection, handed to the classifier, and dropped. What this module returns is
two integers. The classifier's supporting frames, the rows, the rules and the internal category totals (Branch
Services, Collection Services) are not returned, kept or logged. The transit destinations are not read: neither
number depends on them.

CONFIRMED, PARTIAL AND UNCERTAIN (docs/hold-count-accuracy.md, phase B). The two counts are CONFIRMED holds only.
Collectors before 1.0.16 sent a Westside transit as a Westside hold, so on a v2 day before the branch's
`hold_classification.corrected_from` date (branch settings; absent or unreadable = no day is corrected) a public hold at
`westside` is not counted, and a day that had one is counted in `public_holds_partial_days`: the Westside part of that
day is UNAVAILABLE, never estimated and never zero. Such a day with any hold is also in `ill_uncertain_days` (the ILL
flag then followed the item record's patron, not the hold patron). An item-day whose latest record is an unknown alert
type is in `unknown_alert_count`, in neither count, in either era. Legacy days are always complete (corrected from the
stored raw messages), and so is a v2 day from `corrected_from`.

Framework-neutral: no Streamlit, no FastAPI, no caching, no engine. Every read takes a connection the caller
supplies -- already scoped to the tenant by row level security -- and database errors propagate: one era's count is
never returned as the whole.
"""

from __future__ import annotations

import bisect
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import Connection

import metrics
import metrics_v2
from services.hold_classification import HOLD_CLASSIFICATION_KEY, parse_corrected_from
from services.hold_rules import INTERNAL_ROUTING_KEY, V1HoldRules, v1_hold_rules
from services.operational_report_service import ReportWindow
from services.settings_merge import merge_settings
from services.tenant_resolution_service import ResolvedOperationalTenant

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

# The legacy PATRON records alone, same columns: read once, for the lookback before a range's first legacy day.
_V1_PATRONS = text("""
    SELECT event_time, message_code, barcode, destination, patron_id, raw_message
    FROM acs_events
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :span_start
      AND event_time < :span_end
      AND btrim(message_code) = '64'
    ORDER BY event_time
""").bindparams(bindparam("span_start", type_=DateTime(timezone=False)), bindparam("span_end", type_=DateTime(timezone=False)))

# The current rows, with the columns the current summary reads. They hold no patron data at all. `id` is read only to
# break ties: the latest record of an item is its greatest (event_time, id), and a correction reuses its hold's time.
_V2_COLUMNS = ("datetime", "item_key", "state", "destination", "is_ill", "is_branch_services", "is_collection_services", "id")
_V2_ROWS = text("""
    SELECT event_time, item_key, state, destination, is_ill, is_branch_services, is_collection_services, id
    FROM acs_item_events
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :span_start
      AND event_time < :span_end
    ORDER BY event_time, id
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


# The site's hold-classification marker (services.hold_classification), read only when the current era owns part of
# the range. The branch's own document only: the marker is per branch, never inherited from the organization.
_HOLD_CLASSIFICATION = text("""
    SELECT bs.settings_json -> 'hold_classification' AS hold_classification
    FROM organizations o
    JOIN branches b
      ON b.organization_id = o.id
    LEFT JOIN branch_settings bs
      ON bs.branch_id = b.id
    WHERE o.operational_customer_id = :customer_id
      AND b.operational_branch_id = :branch_id
    LIMIT 2
""")

# Before a branch's corrected-from date, a public v2 hold at these destinations may be a transit (decision D1: Westside
# only; Library Express holds are confirmed).
AMBIGUOUS_V2_DESTINATIONS = frozenset({"westside"})


@dataclass(frozen=True, slots=True)
class HoldsReport:
    """A sorter site's two CONFIRMED hold counts over a range, on how many of its days there was anything to count, and
    how many days or item-days are incomplete or uncertain (module docstring). Nothing else of what was read."""

    public_hold_count: int
    ill_hold_count: int
    days_with_data: int
    public_holds_partial_days: int = 0
    ill_uncertain_days: int = 0
    unknown_alert_count: int = 0


# At most this many days' rows are read at once. A day is classified on its own and its rows are dropped, so memory is
# bounded by one block of days however long the range: a ten-year report reads about 120 blocks, never all ten years.
DAYS_PER_READ = 31

# A legacy hold names its patron only by id; who the patron is (an ILL library, a service account) is in a patron
# record (message 64) that need not be on the same day. A legacy day is therefore classified with the patron records of
# that day AND of this long before it -- a fixed window per day, so a day's count never depends on the range around it.
# Only patron records are taken from before the day; items are always the day's own. This is how far back a patron's
# identity is looked for, NOT how far back a report may go (the plan's history window says that).
# 180 days: in NBPL's production data (2026-11-16) the longest gap between a legacy hold and its patron's earlier record
# was 175 days, across a legacy era of 182 days; 31 days would have left 1,313 of 35,605 such holds unclassified.
V1_PATRON_LOOKBACK = timedelta(days=180)


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
    effective: Mapping[str, Any] = merge_settings(organization, branch)
    return v1_hold_rules(effective.get(INTERNAL_ROUTING_KEY))


def hold_corrected_from(conn: Connection, tenant: ResolvedOperationalTenant) -> date | None:
    """The site's corrected-from date, or None (every v2 day uncorrected) when there is no valid marker. A resolved
    tenant has exactly one such row; anything else is an internal fault, never "no marker"."""
    rows = conn.execute(
        _HOLD_CLASSIFICATION, {"customer_id": tenant.operational_customer_id, "branch_id": tenant.operational_branch_id}
    ).mappings().all()
    if len(rows) != 1:
        raise RuntimeError("The hold classification marker of the resolved tenant could not be read as exactly one row.")
    stored = rows[0][HOLD_CLASSIFICATION_KEY]
    if isinstance(stored, (str, bytes)):                     # PostgreSQL hands JSONB back parsed; a text driver does not
        try:
            stored = json.loads(stored)
        except ValueError:
            return None
    return parse_corrected_from(stored)


def _days_of(span: tuple[Any, Any], boundaries: tuple[Any, ...]) -> list[tuple[int, Any, Any]]:
    """The local days of the range, each cut to the era's span: (the day's index in the range, start, end), in order,
    each [start, end). A day the span does not reach is left out; the cutover's day is cut at the cutover, its other
    part being the other era's."""
    days = []
    for index, (start, end) in enumerate(itertools.pairwise(boundaries)):
        cut_start, cut_end = max(start, span[0]), min(end, span[1])
        if cut_start < cut_end:
            days.append((index, cut_start, cut_end))
    return days


def _is_v1_patron(row: Any) -> bool:
    """A legacy patron record (message 64): who a patron is, not what happened to an item."""
    return str(row[1] or "").strip() == "64"


# What each day contributes to a report: confirmed public and ILL holds, unknown-alert item-days, and whether the day is
# partial (an unavailable Westside part) or ILL-uncertain (0 or 1 each).
_DAY_TOTALS = ("public", "ill", "unknown_alerts", "partial_day", "ill_uncertain_day")


def _count_days(
    conn: Connection,
    tenant: ResolvedOperationalTenant,
    statement: Any,
    columns: tuple[str, ...],
    days: list[tuple[int, Any, Any]],
    classify: Any,
    counted: set[int],
    *,
    patron_lookback: timedelta | None = None,
) -> dict[str, int]:
    """The day totals (_DAY_TOTALS) summed over `days`, each day classified alone by `classify(frame, index)`, which
    returns them for that day. Rows are read a block of
    DAYS_PER_READ days at a time and split at the day boundaries, so no more than one block is held at once and no row
    is read twice. The range index of each day that had any row of its own is added to `counted`.

    `patron_lookback` (the legacy era): each day is classified with, besides its own rows, the latest earlier patron
    record of each patron its items name, if that record is within the lookback before the day -- the same patron
    record a one-day report of that day would find, so a day is classified alike in every range. The records before the
    first day are read ONCE (patron records only); after that, each patron's latest record is carried forward from the
    days already read. Only one record per patron is kept, and none is returned."""
    totals = dict.fromkeys(_DAY_TOTALS, 0)
    latest: dict[Any, Any] = {}                              # patron id -> that patron's latest patron record so far
    if patron_lookback is not None and days:
        first_start = days[0][1]
        for row in conn.execute(_V1_PATRONS, _scope(tenant, (first_start - patron_lookback, first_start))).all():
            latest[row[4]] = row                             # in event_time order: the last one is the latest
    for first in range(0, len(days), DAYS_PER_READ):
        block = days[first:first + DAYS_PER_READ]
        rows = conn.execute(statement, _scope(tenant, (block[0][1], block[-1][2]))).all()
        times = [row[0] for row in rows]                     # in event_time order, as each statement orders them
        for index, start, end in block:
            today = list(rows[bisect.bisect_left(times, start):bisect.bisect_left(times, end)])
            if not today:
                continue
            counted.add(index)
            before = []
            if patron_lookback is not None:
                # The patron an item names is its HOLD patron (CY), else its own record's patron (AA): the one the
                # classifier looks up (metrics.hold_patron_id).
                named = {metrics.hold_patron_id(row[5], row[4]) for row in today if not _is_v1_patron(row)}
                before = [latest[patron] for patron in named
                          if patron in latest and latest[patron][0] >= start - patron_lookback]
            day = classify(_frame(before + today, columns), index)
            for name in _DAY_TOTALS:
                totals[name] += int(day[name])
            if patron_lookback is not None:
                for row in today:
                    if _is_v1_patron(row):
                        latest[row[4]] = row
    return totals


def get_holds_report(conn: Connection, tenant: ResolvedOperationalTenant, window: ReportWindow) -> HoldsReport:
    """The site's CONFIRMED public and ILL holds over the window: the SUM OF ITS DAYS, with how many of those days are
    partial or ILL-uncertain and how many item-days had an unknown alert type (module docstring). Each local calendar
    day is classified on its own, exactly as a one-day report of that day is (Live Today's "Holds today" is one), and
    the days' figures are added. An item held on several days counts on each; corrections within a day still count once.

    A day of the cutover is two parts, each its own era's, classified alone. `days_with_data` is how many of the range's
    days had at least one record of either era to count."""
    totals = dict.fromkeys(_DAY_TOTALS, 0)
    counted: set[int] = set()
    local = window.local_range

    if window.v1_span is not None:
        rules = legacy_hold_rules(conn, tenant)

        def legacy(frame: pd.DataFrame, index: int) -> Mapping[str, int]:
            summary = metrics.build_acs_item_summary(
                frame,
                [],  # transit labels: they only split ILL by destination, which this report does not give
                rules.branch_services_names,
                rules.collection_services_names,
                rules.branch_services_da_patterns,
                rules.collection_services_da_patterns,
            )
            # Legacy days are corrected from the stored raw messages: never partial, never ILL-uncertain.
            return {"public": summary["holds_total"], "ill": summary["ill_total"],
                    "unknown_alerts": summary["unknown_alert_total"], "partial_day": 0, "ill_uncertain_day": 0}

        days = _days_of(window.v1_span, local.v1_day_boundaries_local)
        era = _count_days(conn, tenant, _V1_ROWS, _V1_COLUMNS, days, legacy, counted, patron_lookback=V1_PATRON_LOOKBACK)
        totals = {name: totals[name] + era[name] for name in _DAY_TOTALS}

    if window.v2_span is not None:
        corrected_from = hold_corrected_from(conn, tenant)

        def current(frame: pd.DataFrame, index: int) -> Mapping[str, int]:
            # A day is corrected only from the marker's date on; a day with data of an older collector (the upgrade day,
            # a rollback) is before it by definition (decision D5), so it is uncorrected as a whole.
            corrected = corrected_from is not None and local.dates[index] >= corrected_from
            summary = metrics_v2.build_acs_item_summary_v2(
                frame, [], frozenset() if corrected else AMBIGUOUS_V2_DESTINATIONS)
            return {"public": summary["holds_total"], "ill": summary["ill_total"],
                    "unknown_alerts": summary["unknown_alert_total"],
                    "partial_day": int(summary["ambiguous_hold_total"] > 0),
                    "ill_uncertain_day": int(not corrected and summary["hold_items_total"] > 0)}

        days = _days_of(window.v2_span, local.v2_day_boundaries_utc)
        era = _count_days(conn, tenant, _V2_ROWS, _V2_COLUMNS, days, current, counted)
        totals = {name: totals[name] + era[name] for name in _DAY_TOTALS}

    # A cutover day's two parts are one day: its partial / ILL-uncertain flag comes from its v2 part only, so it is
    # counted at most once.
    return HoldsReport(
        public_hold_count=totals["public"],
        ill_hold_count=totals["ill"],
        days_with_data=len(counted),
        public_holds_partial_days=totals["partial_day"],
        ill_uncertain_days=totals["ill_uncertain_day"],
        unknown_alert_count=totals["unknown_alerts"],
    )
