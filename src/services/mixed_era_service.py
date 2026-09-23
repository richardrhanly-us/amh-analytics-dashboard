#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         mixed_era_service.py
#
#  Description: Government-readiness audit, Part 2: the mixed-era read
#               model. Combines a branch's v1 (legacy) and Contract v2
#               event history into one logical dashboard history, using
#               the branch's explicit, operator-recorded v2_cutovers
#               boundary (never inferred from the data itself -- see
#               alembic revision f2a91c7d4e83 and
#               src/data_loader.py::load_v2_cutover).
#
#               A branch with no cutover on record (the overwhelming
#               majority today) gets its v1 history back completely
#               unchanged -- these functions add nothing and filter
#               nothing for that branch. Only a branch an operator has
#               explicitly cut over sees any v2 rows at all.
#
#               DESTINATION TEXT: a v2 checkin/reject's destination is a
#               closed lower_snake_case slug (collector/v2_normalize.py),
#               e.g. "library_express" -- NOT the same text v1's raw AMH
#               destination field contains ("Library Express" /
#               "LIBRARY EXPRESS"). Every existing v1 dashboard function
#               that recognizes a transit/no-agency destination (metrics.py,
#               live_context_service.py, filter_context_service.py,
#               transits_view.py) does so by matching raw TEXT against
#               "WESTSIDE"/"LIBRARY EXPRESS"/"NO AGENCY DESTINATION" --
#               never against a slug. Rather than teach every one of those
#               call sites about v2's slug form, this module rewrites a v2
#               row's destination into the SAME raw-text shape those
#               functions already expect ("library_express" ->
#               "LIBRARY EXPRESS") at the single loader boundary here, so
#               v1 destination-matching logic works UNCHANGED for both
#               eras. (A caveat found and fixed by this rewrite: naively
#               reusing v2's slug as-is would have silently broken Library
#               Express and "No Agency Destination" detection for v2 rows,
#               since "library_express".upper() never contains "LIBRARY
#               EXPRESS" -- the underscore never matches the space.)
#
#***************************************************************

import pandas as pd

import data_loader
import metrics
import metrics_v2

#***************************************************************
#
#  Function:     _as_utc
#
#  Description: Normalizes a datetime Series or scalar Timestamp to
#               timezone-aware UTC, treating a naive value as already
#               being UTC (matching how this codebase's TIMESTAMPTZ
#               columns round-trip through pandas/psycopg2 elsewhere).
#
#  Parameters:  value - Series or scalar to normalize.
#
#  Returns:     Series | Timestamp - The same value, tz-aware UTC.
#
#***************************************************************

def _as_utc(value):
    if isinstance(value, pd.Series):
        value = pd.to_datetime(value, errors="coerce")
        if value.dt.tz is None:
            return value.dt.tz_localize("UTC")
        return value.dt.tz_convert("UTC")

    value = pd.Timestamp(value)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


def get_effective_cutover(org_slug, branch_slug):
    """Thin, testable wrapper around data_loader.load_v2_cutover -- the branch's current mixed-era boundary, or None
    if the branch is v1-only (never cut over, or its latest record is a rollback)."""
    return data_loader.load_v2_cutover(org_slug, branch_slug)


def is_v2_active(org_slug, branch_slug):
    """True if the branch has a non-None effective cutover on record -- used by the coexistence-aware health surface
    to decide whether a v2 status is even relevant for this branch."""
    return get_effective_cutover(org_slug, branch_slug) is not None


def _concat_nonempty(frames):
    """pd.concat, skipping any empty frame -- avoids pandas' FutureWarning/dtype churn on concatenating an
    empty-or-all-NA frame, and returns a clean empty DataFrame (not an error) if every frame is empty."""
    nonempty = [frame for frame in frames if frame is not None and not frame.empty]
    if not nonempty:
        return pd.DataFrame()
    return pd.concat(nonempty, ignore_index=True, sort=False)


def _split_by_cutover(df, cutover_at, *, before):
    if df is None or df.empty or "datetime" not in df.columns:
        return df.iloc[0:0].copy() if df is not None else pd.DataFrame()

    datetimes = _as_utc(df["datetime"])
    cutover_utc = _as_utc(cutover_at)
    mask = datetimes < cutover_utc if before else datetimes >= cutover_utc
    return df[mask].copy()


def _filter_date_range(df, start_date, end_date):
    """The exact date-range filter Overview used to apply inline to acs_history_raw before classifying it (see
    views/overview_view.py's prior implementation) -- extracted here so both the v1 and v2 portions of a mixed-era
    ACS summary are filtered identically before classification."""
    if df is None or df.empty or "datetime" not in df.columns:
        return df.iloc[0:0].copy() if df is not None else pd.DataFrame()

    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    work = work.dropna(subset=["datetime"])
    return work[(work["datetime"].dt.date >= start_date) & (work["datetime"].dt.date <= end_date)].copy()


def _v2_destination_to_v1_text(slug):
    """A Contract v2 destination slug, rewritten into the raw-text shape v1's destination-matching functions
    already expect (see this module's docstring). Slug-agnostic: works for the built-in slugs
    (collector/v2_normalize.py) and for any future collector-local custom slug alike."""
    if slug is None or (isinstance(slug, float) and pd.isna(slug)):
        return ""
    return str(slug).strip().replace("_", " ").upper()


def _normalize_v2_checkin_like_df(df, *, item_key_source):
    """Shared v2-side preparation for both checkins and rejects: rewrites destination text (checkins only -- rejects
    have no destination column) and adds item_group_key, the per-item identity used internally for the same
    latest-record/cross-referencing logic v1 already does with a real barcode (see transit_logic.py's
    group_column parameter). Never adds or reads patron_id/raw_message/barcode/title -- none exist on a v2 table."""
    if df.empty:
        return df

    df = df.copy()
    if "destination" in df.columns:
        df["destination"] = df["destination"].apply(_v2_destination_to_v1_text)
    df["item_group_key"] = df[item_key_source] if item_key_source in df.columns else None
    return df


#***************************************************************
#
#  Function:     build_mixed_checkins_df / build_mixed_checkins_live_df
#               build_mixed_rejects_df / build_mixed_rejects_live_df
#
#  Description: Builds one continuous checkins/rejects dataframe for a
#               branch: v1 rows strictly before the branch's recorded
#               cutover, v2 rows at or after it (no cutover on record
#               means v1 rows only, entirely unchanged from
#               data_loader's own loaders). Adds "source_era" ("v1"/"v2")
#               and "item_group_key" (barcode for v1, a Contract v2
#               item_key HMAC for v2 -- internal use only, see this
#               module's docstring; never rendered or exported, verified
#               by tests/test_v2_dashboard_wiring.py) so downstream code
#               can distinguish and cross-reference rows without needing
#               to know which era wrote them. Never double-counts: the
#               split is a strict partition of the requested cutover
#               instant. The *_live_df variants scope to today only,
#               mirroring data_loader.load_checkins_df/load_rejects_df's
#               refresh_count-based cache-busting exactly, so a manual
#               "Refresh now" click or a new Collector run still forces a
#               real reload for a mixed-era branch, same as a v1-only one.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#               refresh_count - (live variants only) Cache-busting key,
#                               forwarded unchanged to the v1 and v2 live
#                               loaders.
#
#  Returns:     DataFrame - Combined, datetime-sorted history.
#
#***************************************************************

def _build_mixed_checkins(v1_df, v2_df, cutover_at):
    if cutover_at is None:
        result = v1_df.copy()
        if not result.empty:
            result["source_era"] = "v1"
            result["item_group_key"] = result["barcode"] if "barcode" in result.columns else None
        return result

    v1_part = _split_by_cutover(v1_df, cutover_at, before=True)
    if not v1_part.empty:
        v1_part["source_era"] = "v1"
        v1_part["item_group_key"] = v1_part["barcode"] if "barcode" in v1_part.columns else None

    v2_part = _split_by_cutover(v2_df, cutover_at, before=False)
    v2_part = _normalize_v2_checkin_like_df(v2_part, item_key_source="item_key")
    if not v2_part.empty:
        v2_part["source_era"] = "v2"

    combined = _concat_nonempty([v1_part, v2_part])
    if not combined.empty and "datetime" in combined.columns:
        combined = combined.sort_values("datetime").reset_index(drop=True)
    return combined


def build_mixed_checkins_df(org_slug, branch_slug):
    cutover_at = get_effective_cutover(org_slug, branch_slug)
    v1_df = data_loader.load_checkins_history_df(org_slug, branch_slug)
    v2_df = (
        data_loader.load_checkin_events_history_df(org_slug, branch_slug)
        if cutover_at is not None else pd.DataFrame()
    )
    return _build_mixed_checkins(v1_df, v2_df, cutover_at)


def build_mixed_checkins_live_df(org_slug, branch_slug, refresh_count=0):
    cutover_at = get_effective_cutover(org_slug, branch_slug)
    v1_df = data_loader.load_checkins_df(org_slug, branch_slug, mtime=None, refresh_count=refresh_count)
    v2_df = (
        data_loader.load_checkin_events_df(org_slug, branch_slug, refresh_count=refresh_count)
        if cutover_at is not None else pd.DataFrame()
    )
    return _build_mixed_checkins(v1_df, v2_df, cutover_at)


def _build_mixed_rejects(v1_df, v2_df, cutover_at):
    if cutover_at is None:
        result = v1_df.copy()
        if not result.empty:
            result["source_era"] = "v1"
            result["item_group_key"] = result["barcode"] if "barcode" in result.columns else None
        return result

    v1_part = _split_by_cutover(v1_df, cutover_at, before=True)
    if not v1_part.empty:
        v1_part["source_era"] = "v1"
        v1_part["item_group_key"] = v1_part["barcode"] if "barcode" in v1_part.columns else None

    v2_part = _split_by_cutover(v2_df, cutover_at, before=False)
    v2_part = _normalize_v2_checkin_like_df(v2_part, item_key_source="item_key")
    if not v2_part.empty:
        v2_part["source_era"] = "v2"

    combined = _concat_nonempty([v1_part, v2_part])
    if not combined.empty and "datetime" in combined.columns:
        combined = combined.sort_values("datetime").reset_index(drop=True)
    return combined


def build_mixed_rejects_df(org_slug, branch_slug):
    cutover_at = get_effective_cutover(org_slug, branch_slug)
    v1_df = data_loader.load_rejects_history_df(org_slug, branch_slug)
    v2_df = (
        data_loader.load_reject_events_history_df(org_slug, branch_slug)
        if cutover_at is not None else pd.DataFrame()
    )
    return _build_mixed_rejects(v1_df, v2_df, cutover_at)


def build_mixed_rejects_live_df(org_slug, branch_slug, refresh_count=0):
    cutover_at = get_effective_cutover(org_slug, branch_slug)
    v1_df = data_loader.load_rejects_df(org_slug, branch_slug, mtime=None, refresh_count=refresh_count)
    v2_df = (
        data_loader.load_reject_events_df(org_slug, branch_slug, refresh_count=refresh_count)
        if cutover_at is not None else pd.DataFrame()
    )
    return _build_mixed_rejects(v1_df, v2_df, cutover_at)


#***************************************************************
#
#  Function:     build_mixed_acs_item_summary / build_mixed_acs_item_summary_live
#
#  Description: The mixed-era equivalent of
#               metrics.py::build_acs_item_summary. Computes v1's summary
#               over the pre-cutover portion (unchanged v1 classification
#               logic, from raw_message) and v2's summary
#               (metrics_v2.build_acs_item_summary_v2) over the
#               at/after-cutover portion (already-classified v2 fields,
#               never raw_message), then combines the two: totals are
#               summed, ill_by_branch counts are summed per label, and
#               the supporting detail frames are concatenated with a
#               source_era tag. A branch with no cutover on record
#               delegates entirely to build_acs_item_summary, unchanged.
#               The _live variant scopes to today only and mirrors
#               metrics.prepare_todays_acs_snapshot's v1 preparation
#               (the same "latest record per item" pass Live Today has
#               always done) before classifying the v1 portion; the v2
#               portion needs no such preparation (metrics_v2's own
#               "latest per item_key" dedup already covers it).
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#               start_date / end_date - (history variant only) Date range
#                   to filter to, mirroring Overview's own filter.
#               transit_labels / branch_services_names /
#               collection_services_names / branch_services_da_patterns /
#               collection_services_da_patterns - Same configuration
#                   build_acs_item_summary already takes.
#               refresh_count - (live variant only) Cache-busting key,
#                   forwarded unchanged to the v1 and v2 live loaders.
#
#  Returns:     dict - Same key shape as build_acs_item_summary.
#
#***************************************************************

def _combine_acs_summaries(v1_summary, v2_summary, transit_labels):
    ill_by_branch = {
        label: int(v1_summary["ill_by_branch"].get(label, 0)) + int(v2_summary["ill_by_branch"].get(label, 0))
        for label in transit_labels
    }

    def _combined_frame(key):
        v1_frame = v1_summary[key].copy()
        if not v1_frame.empty:
            v1_frame["source_era"] = "v1"
        v2_frame = v2_summary[key].copy()
        if not v2_frame.empty:
            v2_frame["source_era"] = "v2"
        return _concat_nonempty([v1_frame, v2_frame])

    return {
        "holds_total": v1_summary["holds_total"] + v2_summary["holds_total"],
        "ill_total": v1_summary["ill_total"] + v2_summary["ill_total"],
        "programming_total": v1_summary["programming_total"] + v2_summary["programming_total"],
        "collection_services_total": (
            v1_summary["collection_services_total"] + v2_summary["collection_services_total"]
        ),
        "ill_main": v1_summary["ill_main"] + v2_summary["ill_main"],
        "ill_by_branch": ill_by_branch,
        "items_df": _combined_frame("items_df"),
        "holds_df": _combined_frame("holds_df"),
        "ill_df": _combined_frame("ill_df"),
        "programming_df": _combined_frame("programming_df"),
        "collection_services_df": _combined_frame("collection_services_df"),
    }


def build_mixed_acs_item_summary(
    org_slug,
    branch_slug,
    start_date,
    end_date,
    transit_labels,
    branch_services_names,
    collection_services_names,
    branch_services_da_patterns,
    collection_services_da_patterns,
):
    cutover_at = get_effective_cutover(org_slug, branch_slug)
    v1_acs_df = _filter_date_range(
        data_loader.load_acs_history_df(org_slug, branch_slug), start_date, end_date
    )

    if cutover_at is None:
        return metrics.build_acs_item_summary(
            v1_acs_df,
            transit_labels,
            branch_services_names,
            collection_services_names,
            branch_services_da_patterns,
            collection_services_da_patterns,
        )

    v1_part = _split_by_cutover(v1_acs_df, cutover_at, before=True)
    v1_summary = metrics.build_acs_item_summary(
        v1_part,
        transit_labels,
        branch_services_names,
        collection_services_names,
        branch_services_da_patterns,
        collection_services_da_patterns,
    )

    v2_acs_df = _filter_date_range(
        data_loader.load_acs_item_events_history_df(org_slug, branch_slug), start_date, end_date
    )
    v2_part = _split_by_cutover(v2_acs_df, cutover_at, before=False)
    v2_summary = metrics_v2.build_acs_item_summary_v2(v2_part, transit_labels)

    return _combine_acs_summaries(v1_summary, v2_summary, transit_labels)


def build_mixed_acs_item_summary_live(
    org_slug,
    branch_slug,
    transit_labels,
    branch_services_names,
    collection_services_names,
    branch_services_da_patterns,
    collection_services_da_patterns,
    refresh_count=0,
):
    cutover_at = get_effective_cutover(org_slug, branch_slug)
    v1_acs_live = data_loader.load_acs_df(org_slug, branch_slug, mtime=None, refresh_count=refresh_count)
    v1_acs_prepared = metrics.prepare_todays_acs_snapshot(v1_acs_live)

    if cutover_at is None:
        return metrics.build_acs_item_summary(
            v1_acs_prepared,
            transit_labels,
            branch_services_names,
            collection_services_names,
            branch_services_da_patterns,
            collection_services_da_patterns,
        )

    v1_part = _split_by_cutover(v1_acs_prepared, cutover_at, before=True)
    v1_summary = metrics.build_acs_item_summary(
        v1_part,
        transit_labels,
        branch_services_names,
        collection_services_names,
        branch_services_da_patterns,
        collection_services_da_patterns,
    )

    v2_acs_live = data_loader.load_acs_item_events_df(org_slug, branch_slug, refresh_count=refresh_count)
    v2_part = _split_by_cutover(v2_acs_live, cutover_at, before=False)
    v2_summary = metrics_v2.build_acs_item_summary_v2(v2_part, transit_labels)

    return _combine_acs_summaries(v1_summary, v2_summary, transit_labels)
