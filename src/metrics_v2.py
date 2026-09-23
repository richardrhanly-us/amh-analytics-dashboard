#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         metrics_v2.py
#
#  Description: Contract v2 equivalent of metrics.py::build_acs_item_summary.
#               v1 classifies holds/ILL/programming/collection-services by
#               scanning raw_message and joining a message-64 patron
#               record (see metrics.py). v2 classifies the SAME categories
#               on the collector, before upload (collector/v2_classify.py,
#               collector/v2_transform.py) -- this module only AGGREGATES
#               the already-classified, privacy-safe boolean/enum fields
#               v2 stores directly on acs_item_events. It never attempts
#               to reconstruct raw_message or any patron field, because
#               neither exists anywhere in a v2 payload or table.
#
#               This is a deliberately SEPARATE function from
#               metrics.py::build_acs_item_summary, not a shared
#               refactor: that function is live, tested v1 code and this
#               government-readiness round does not touch it.
#
#***************************************************************

import pandas as pd

from services.destination_mapping import build_transit_label_to_v2_slug_map

#***************************************************************
# Summary Frame Columns (privacy containment, matching metrics.py's
# _SUMMARY_FRAME_COLUMNS convention)
#
# item_key is deliberately NOT included: unlike v1's barcode (which the
# dashboard already treats as internal-only, never rendered), the
# instruction for v2 is stricter -- item_key must not reach any frame the
# view layer receives at all, even one nothing currently renders from.
#***************************************************************

_SUMMARY_FRAME_COLUMNS_V2 = (
    "datetime",
    "state",
    "destination",
    "is_hold",
    "is_ill",
    "is_programming",
    "is_collection_services",
)


def _summary_frame_v2(frame):
    return frame[[c for c in _SUMMARY_FRAME_COLUMNS_V2 if c in frame.columns]].copy()


def _empty_summary_v2():
    return {
        "holds_total": 0,
        "ill_total": 0,
        "programming_total": 0,
        "collection_services_total": 0,
        "ill_main": 0,
        "ill_by_branch": {},
        "items_df": pd.DataFrame(),
        "holds_df": pd.DataFrame(),
        "ill_df": pd.DataFrame(),
        "programming_df": pd.DataFrame(),
        "collection_services_df": pd.DataFrame(),
    }


#***************************************************************
#
#  Function:     build_acs_item_summary_v2
#
#  Description: Builds the v2 equivalent of build_acs_item_summary's
#               return shape from an already-privacy-safe,
#               already-classified acs_item_events dataframe (see
#               src/data_loader.py::_normalize_acs_item_events_df). Holds
#               parity for holds/ILL/ILL-by-branch/programming
#               (v2's "branch services")/collection-services totals.
#
#  Parameters:  acs_items_df - Normalized acs_item_events dataframe
#                              (columns: datetime, item_key, state,
#                              destination, is_ill, is_branch_services,
#                              is_collection_services, is_hold).
#               transit_labels - List of configured transit branch labels
#                                (the SAME dashboard configuration v1's
#                                build_acs_item_summary receives).
#
#  Returns:     dict - Same key shape as build_acs_item_summary.
#
#***************************************************************

def build_acs_item_summary_v2(acs_items_df, transit_labels):
    if acs_items_df is None or len(acs_items_df) == 0:
        return _empty_summary_v2()

    df = acs_items_df.copy()

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "item_key" not in df.columns:
        df["item_key"] = None

    if "destination" not in df.columns:
        df["destination"] = ""

    # Only hold and non-hold-101 records are "items" in v1's sense (raw_message
    # starting with "101"); other_code10 rows are excluded the same way v1
    # excludes any raw_message that does not start with "101".
    if "state" in df.columns:
        items = df[df["state"].isin(["hold", "non_hold_101"])].copy()
    else:
        items = df.iloc[0:0].copy()

    if len(items) == 0:
        return _empty_summary_v2()

    # Keep the latest record per item (the same real-world pattern v1 handles
    # with drop_duplicates(subset=["barcode"], keep="last") -- Tech Logic can
    # emit more than one status record for the same physical item over time,
    # e.g. a hold placed and later retracted). item_key is used only for this
    # internal grouping and is dropped before anything is returned.
    if "datetime" in items.columns:
        items = items.sort_values("datetime")
    items = items.drop_duplicates(subset=["item_key"], keep="last")

    items["is_hold"] = items["state"] == "hold"

    for col in ("is_ill", "is_branch_services", "is_collection_services"):
        if col not in items.columns:
            items[col] = False
        items[col] = items[col].map(lambda v: bool(v) if pd.notna(v) else False)

    items["is_programming"] = items["is_branch_services"]

    holds_df = items[items["is_hold"]].copy()
    ill_df = holds_df[holds_df["is_ill"]].copy()
    programming_df = holds_df[holds_df["is_programming"]].copy()
    collection_services_df = holds_df[holds_df["is_collection_services"]].copy()

    internal_mask = (
        holds_df["is_ill"]
        | holds_df["is_programming"]
        | holds_df["is_collection_services"]
    )
    public_holds_df = holds_df[~internal_mask].copy()

    # ILL-by-branch: map each configured transit_label to the v2 slug it
    # would normalize to (Part 4 of the government-readiness audit), then
    # bucket ill_df rows by that slug. A transit_label the built-in mapping
    # cannot resolve (a custom collector-side rule) simply gets 0 here, the
    # same way an unmatched v1 destination label already yields 0 --
    # ill_main below picks up anything not attributed to a mapped label,
    # never silently mis-attributing it to the wrong branch.
    label_to_slug = build_transit_label_to_v2_slug_map(transit_labels)
    ill_destination = ill_df["destination"].fillna("").astype(str)

    ill_by_branch = {}
    for transit_label in transit_labels:
        slug = label_to_slug.get(transit_label)
        ill_by_branch[transit_label] = int((ill_destination == slug).sum()) if slug else 0

    mapped_slugs = set(label_to_slug.values())
    ill_main_count = int((~ill_destination.isin(mapped_slugs)).sum())

    return {
        "holds_total": len(public_holds_df),
        "ill_total": len(ill_df),
        "programming_total": len(programming_df),
        "collection_services_total": len(collection_services_df),
        "ill_main": ill_main_count,
        "ill_by_branch": ill_by_branch,
        "items_df": _summary_frame_v2(items),
        "holds_df": _summary_frame_v2(public_holds_df),
        "ill_df": _summary_frame_v2(ill_df),
        "programming_df": _summary_frame_v2(programming_df),
        "collection_services_df": _summary_frame_v2(collection_services_df),
    }
