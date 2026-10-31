"""Tests for src/metrics_v2.py and src/services/destination_mapping.py (government-readiness audit, Parts 3 and 4).

Part 4's regression test (test_destination_mapping_matches_the_deployed_v2_normalize_module) imports
collector/v2_normalize.py directly to prove the two built-in mappings never drift apart -- a test-only cross-reference,
never a runtime dependency (src/ still never imports collector/ code at runtime; see destination_mapping.py's docstring).
"""

import itertools
import random
import sys
from pathlib import Path

import pandas as pd
import pytest

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


# --- the latest record of an item is its greatest (event_time, id): docs/contract-v2-design.md section 9.4 -----------

ITEM = "1" * 64


def _in_insert_order(*records):
    """The records with the ids the database gave them, in the order they are written here."""
    return [{**record, "id": row_id} for row_id, record in enumerate(records, start=1)]


def _totals(rows):
    result = build_acs_item_summary_v2(_acs_items_df(rows), [])
    return result["holds_total"], result["ill_total"]


@pytest.mark.parametrize(("records", "expected"), [
    pytest.param([_hold(ITEM), _hold(ITEM, is_ill=True)], (0, 1), id="public-corrected-to-ill"),
    pytest.param([_hold(ITEM, is_ill=True), _hold(ITEM)], (1, 0), id="ill-corrected-to-public"),
    pytest.param([_hold(ITEM), _hold(ITEM, is_ill=True), _hold(ITEM)], (1, 0), id="public-ill-public"),
    pytest.param([_hold(ITEM, is_ill=True), _hold(ITEM), _hold(ITEM, is_ill=True)], (0, 1), id="ill-public-ill"),
    pytest.param([_hold(ITEM), _non_hold(ITEM)], (0, 0), id="public-retracted-in-its-second"),
    pytest.param([_hold(ITEM, is_ill=True), _non_hold(ITEM)], (0, 0), id="ill-retracted-in-its-second"),
    pytest.param([_non_hold(ITEM), _hold(ITEM)], (1, 0), id="held-again-in-the-retractions-second"),
])
def test_a_tie_is_decided_by_the_greatest_id_whichever_way_the_rows_arrive(records, expected):
    for arrival in itertools.permutations(_in_insert_order(*records)):
        assert _totals(list(arrival)) == expected


def test_a_later_event_time_outranks_a_greater_id():
    later, earlier = _hold(ITEM, when="2026-10-05T00:00:01+00:00"), _non_hold(ITEM, when="2026-10-05T00:00:00+00:00")

    assert _totals(_in_insert_order(later, earlier)) == (1, 0)


@pytest.mark.parametrize("arrival", ["id-order", "reversed", "shuffled"])
def test_dense_ties_across_many_items_match_a_reduction_by_event_time_and_id(arrival):
    instants = [f"2026-10-05T00:00:0{second}+00:00" for second in range(3)]
    records = []
    for n in range(60):
        item, when, is_ill = f"{n:064x}", instants[n % 3], n % 2 == 0
        records += [_hold(item, is_ill=is_ill, when=when), _hold(item, is_ill=not is_ill, when=when)]
        records += [_non_hold(item, when=when)] if n % 5 == 0 else []
    rows = _in_insert_order(*records)

    latest = {}
    for row in sorted(rows, key=lambda row: (row["datetime"], row["id"])):
        latest[row["item_key"]] = row
    holds = [row for row in latest.values() if row["state"] == "hold"]
    expected = (sum(not row["is_ill"] for row in holds), sum(row["is_ill"] for row in holds))

    if arrival == "reversed":
        rows.reverse()
    elif arrival == "shuffled":
        random.Random(20261031).shuffle(rows)  # nosec B311 - a fixed order for a test

    assert expected == (24, 24)
    assert _totals(rows) == expected


def test_rows_without_an_id_are_still_summarized_by_their_time_and_gain_no_id():
    # A caller without database ids gets the latest record by time. A tie has no contract order without an id, so none is asserted.
    df = _acs_items_df([
        _non_hold(ITEM, when="2026-10-05T00:05:00+00:00"),
        _hold(ITEM, when="2026-10-05T00:00:00+00:00"),
        _hold("2" * 64, is_ill=True),
        _hold("3" * 64),
    ])

    result = build_acs_item_summary_v2(df, [])

    assert (result["holds_total"], result["ill_total"]) == (1, 1)
    assert "id" not in result["items_df"].columns


def test_the_database_id_never_appears_in_any_returned_frame():
    result = build_acs_item_summary_v2(_acs_items_df(_in_insert_order(_hold(ITEM), _hold("2" * 64, is_ill=True))), [])

    for key in ("items_df", "holds_df", "ill_df", "programming_df", "collection_services_df"):
        assert "id" not in result[key].columns, key


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


# --- R8J: a v2 hold's flags are what the collector decided, and decide public holds as v1's do ----------------------

def test_overlapping_v2_flags_count_in_each_total_and_take_the_hold_out_of_public_holds_once():
    df = _acs_items_df([
        _hold("1" * 64, is_ill=True, is_collection_services=True),
        _hold("2" * 64, is_branch_services=True, is_collection_services=True),
        _hold("3" * 64),
    ])

    result = build_acs_item_summary_v2(df, [])

    assert (result["holds_total"], result["ill_total"], result["programming_total"], result["collection_services_total"]) == (1, 1, 1, 2)


def test_a_v2_holds_flags_are_taken_as_sent_and_a_missing_flag_is_false():
    # Nothing is reclassified on the server: there is no list to classify with. A flag that did not arrive is false.
    df = _acs_items_df([_hold("1" * 64, is_branch_services=None, is_collection_services=None), _hold("2" * 64, is_collection_services=True)])

    result = build_acs_item_summary_v2(df, [])

    assert (result["holds_total"], result["programming_total"], result["collection_services_total"]) == (1, 0, 1)
