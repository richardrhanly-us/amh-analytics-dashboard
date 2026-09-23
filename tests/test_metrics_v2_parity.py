"""Tests for src/metrics_v2.py and src/services/destination_mapping.py (government-readiness audit, Parts 3 and 4).

Part 4's regression test (test_destination_mapping_matches_the_deployed_v2_normalize_module) imports
collector/v2_normalize.py directly to prove the two built-in mappings never drift apart -- a test-only cross-reference,
never a runtime dependency (src/ still never imports collector/ code at runtime; see destination_mapping.py's docstring).
"""

import sys
from pathlib import Path

import pandas as pd

from services.destination_mapping import (
    build_transit_label_to_v2_slug_map,
    map_transit_label_to_v2_slug,
)
from src.metrics_v2 import build_acs_item_summary_v2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _acs_items_df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["datetime", "item_key", "state", "destination", "is_ill", "is_branch_services", "is_collection_services"]
    )


def _hold(item_key, destination="main", is_ill=False, is_branch_services=False, is_collection_services=False, when="2026-10-05T00:00:00+00:00"):
    return {
        "datetime": pd.Timestamp(when),
        "item_key": item_key,
        "state": "hold",
        "destination": destination,
        "is_ill": is_ill,
        "is_branch_services": is_branch_services,
        "is_collection_services": is_collection_services,
    }


def _non_hold(item_key, when="2026-10-05T00:00:00+00:00"):
    return {
        "datetime": pd.Timestamp(when),
        "item_key": item_key,
        "state": "non_hold_101",
        "destination": None,
        "is_ill": None,
        "is_branch_services": None,
        "is_collection_services": None,
    }


# --- empty input --------------------------------------------------------------------------------------------------

def test_empty_input_returns_the_same_zeroed_shape_as_v1():
    result = build_acs_item_summary_v2(pd.DataFrame(), ["Westside"])
    assert result["holds_total"] == 0
    assert result["ill_by_branch"] == {}
    assert list(result["items_df"].columns) == []


# --- item 8: holds parity ------------------------------------------------------------------------------------------

def test_holds_total_counts_only_public_holds():
    df = _acs_items_df([
        _hold("1" * 64),
        _hold("2" * 64, is_ill=True),
        _non_hold("3" * 64),  # not a hold at all -- excluded
    ])
    result = build_acs_item_summary_v2(df, [])
    assert result["holds_total"] == 1
    assert result["ill_total"] == 1


def test_other_code10_state_is_excluded_entirely_matching_v1s_101_prefix_filter():
    df = _acs_items_df([
        {**_hold("1" * 64), "state": "other_code10"},
    ])
    result = build_acs_item_summary_v2(df, [])
    assert result["holds_total"] == 0
    assert result["ill_total"] == 0


def test_latest_record_per_item_wins_like_v1s_barcode_dedup():
    df = _acs_items_df([
        _hold("1" * 64, when="2026-10-05T00:00:00+00:00"),
        {**_non_hold("1" * 64, when="2026-10-05T00:05:00+00:00")},  # same item, later, retracted
    ])
    result = build_acs_item_summary_v2(df, [])
    assert result["holds_total"] == 0  # the LATEST record for this item is not a hold


# --- item 10/11: branch/programming and collection services parity -------------------------------------------------

def test_programming_and_collection_services_totals():
    df = _acs_items_df([
        _hold("1" * 64, is_branch_services=True),
        _hold("2" * 64, is_collection_services=True),
        _hold("3" * 64),
    ])
    result = build_acs_item_summary_v2(df, [])
    assert result["programming_total"] == 1
    assert result["collection_services_total"] == 1
    assert result["holds_total"] == 1  # only the plain public hold


# --- item 18: item_key never reaches a returned summary frame -------------------------------------------------------

def test_item_key_never_appears_in_any_returned_frame():
    df = _acs_items_df([_hold("a" * 64, is_ill=True)])
    result = build_acs_item_summary_v2(df, ["Westside"])
    for key in ("items_df", "holds_df", "ill_df", "programming_df", "collection_services_df"):
        assert "item_key" not in result[key].columns, key


# --- item 12: ILL-by-branch mapping parity (Part 4) ------------------------------------------------------------------

def test_map_transit_label_to_v2_slug_covers_nbpls_configured_labels():
    assert map_transit_label_to_v2_slug("Westside") == "westside"
    assert map_transit_label_to_v2_slug("Library Express") == "library_express"
    assert map_transit_label_to_v2_slug("Main") == "main"
    assert map_transit_label_to_v2_slug("Some Future Custom Branch") == "unknown"


def test_ill_by_branch_buckets_v2_slugs_under_the_configured_label_and_falls_back_to_main():
    df = _acs_items_df([
        _hold("1" * 64, destination="westside", is_ill=True),
        _hold("2" * 64, destination="library_express", is_ill=True),
        _hold("3" * 64, destination="main", is_ill=True),
        _hold("4" * 64, destination="some_custom_slug", is_ill=True),  # unresolved by the built-in mapping
    ])
    result = build_acs_item_summary_v2(df, ["Westside", "Library Express"])
    assert result["ill_by_branch"] == {"Westside": 1, "Library Express": 1}
    # main + the unresolved custom slug both fall into ill_main, never mis-attributed to Westside/Library Express.
    assert result["ill_main"] == 2


def test_build_transit_label_to_v2_slug_map_omits_unresolvable_labels():
    mapping = build_transit_label_to_v2_slug_map(["Westside", "A Totally Custom Branch"])
    assert mapping == {"Westside": "westside"}


class _NoCustomRules:
    """A minimal stand-in for collector.v2_rules.Rules: normalize_destination reads only `.destinations`, an
    iterable of (contains, slug) pairs -- empty here, since destination_mapping.py deliberately has no access to a
    collector's local custom rules file (see its module docstring)."""
    destinations: tuple = ()


def test_destination_mapping_matches_the_deployed_v2_normalize_module():
    from collector import v2_normalize

    for raw in ("WESTSIDE BRANCH", "westside", "Library Express Annex", "library express", "1", "Local", "Main",
                "No Agency Destination", "something else entirely"):
        expected = v2_normalize.normalize_destination(raw, rules=_NoCustomRules())
        assert map_transit_label_to_v2_slug(raw) == expected, raw
