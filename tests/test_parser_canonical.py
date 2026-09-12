"""Regression tests for agent/parser/ -- the canonical Tech Logic line
parsers (Continuous Ingestion Phase B).

Two things are being proven here, not just "does it parse":

1. The moved logic is byte-for-byte the same as what was deployed and
   proven in production for 5 months (agent/SortViewAgent - What is
   currently sitting on the AMH computer/). One fixture in each parser's
   tests is reconstructed directly from real production output
   (data/processed/checkins_clean.csv, dated 2026-08-31, from that
   folder) rather than hand-invented, specifically to ground at least one
   case in real data.
2. The existing edge-case behavior (short-row skipping, bad-datetime
   coercion, destination normalization) is preserved exactly.

agent.parse_checkins/parse_rejects/parse_acs now just re-export these
functions -- tests/test_parse_checkins.py and tests/test_parse_rejects.py
(pre-existing) continue to exercise the same logic through the old import
paths and must keep passing unchanged.
"""

import math

import pandas as pd

from agent.parser import acs, checkins, rejects

# --- checkins ----------------------------------------------------------


def test_checkins_real_production_row_from_checkins_clean_csv():
    # Reconstructed directly from a real row in
    # `agent/SortViewAgent - What is currently sitting on the AMH computer/
    # data/processed/checkins_clean.csv` (2026-08-31 16:18:39, destination_raw
    # "1", is_problem "False", empty message, bin "4"). Expected fields
    # below are exactly what that CSV row already shows.
    line = "Sunny days /|33472004192508|MLEPB|E KERBEL NATURE|000|1|False||4|N|N|N|8/31/2026|4:18:39 PM"

    df = checkins.parse_lines([line])

    assert len(df) == 1
    row = df.iloc[0]
    assert row["title"] == "Sunny days /"
    assert row["barcode"] == "33472004192508"
    assert row["destination"] == "Main"
    assert row["is_problem"] == False
    assert row["is_transit"] == False
    assert row["message"] == ""
    assert row["bin"] == "4"
    assert str(row["datetime"]) == "2026-08-31 16:18:39"


def test_checkins_normalize_destination():
    assert checkins.normalize_destination("1") == "Main"
    assert checkins.normalize_destination("LOCAL") == "Main"
    assert checkins.normalize_destination("MAIN") == "Main"
    assert checkins.normalize_destination("WESTSIDE (2910 IH35)") == "Westside"
    assert checkins.normalize_destination("LIBRARY EXPRESS") == "Library Express"
    assert checkins.normalize_destination("No Agency Destination") == "No Agency Destination"
    assert checkins.normalize_destination("") == ""
    assert checkins.normalize_destination(None) == ""


def test_checkins_transit_flag_for_westside_and_library_express():
    lines = [
        "Book A|111|MLFIC|FIC A|000|WESTSIDE (2910 IH35)|False||1|N|N|N|1/31/2026|8:00:00 AM",
        "Book B|222|MLFIC|FIC B|000|LIBRARY EXPRESS|False||1|N|N|N|1/31/2026|8:00:00 AM",
        "Book C|333|MLFIC|FIC C|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM",
    ]
    df = checkins.parse_lines(lines)

    assert df["is_transit"].tolist() == [True, True, False]


def test_checkins_short_row_is_skipped_not_crashed():
    lines = [
        "Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM",
        "Broken row with too few fields|123",
    ]
    df = checkins.parse_lines(lines)

    assert len(df) == 1
    assert df.iloc[0]["barcode"] == "111"


def test_checkins_bad_datetime_coerced_to_nat_not_raised():
    lines = ["Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|not-a-date|not-a-time"]
    df = checkins.parse_lines(lines)

    assert len(df) == 1
    assert pd.isna(df.iloc[0]["datetime"])


def test_checkins_empty_input_returns_typed_empty_frame():
    df = checkins.parse_lines([])

    assert len(df) == 0
    assert "destination" in df.columns
    assert "is_transit" in df.columns


def test_checkins_blank_lines_are_ignored():
    lines = ["", "   ", "Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM", ""]
    df = checkins.parse_lines(lines)

    assert len(df) == 1


# --- rejects -------------------------------------------------------------


def test_rejects_basic_parse_and_error_simplification():
    lines = [
        "111|Item Not Found in ACS|1/31/2026|8:00:00 AM",
        "222|Multiple RFID tags detected|1/31/2026|8:01:00 AM",
        "333|Some other weird message|1/31/2026|8:02:00 AM",
    ]
    df = rejects.parse_lines(lines)

    assert df["error_simple"].tolist() == ["Item Not Found", "RFID Collision", "Other"]


def test_rejects_short_row_is_skipped():
    lines = ["111|Item Not Found|1/31/2026|8:00:00 AM", "not enough fields"]
    df = rejects.parse_lines(lines)

    assert len(df) == 1


def test_rejects_empty_input_returns_typed_empty_frame():
    df = rejects.parse_lines([])

    assert len(df) == 0
    assert "error_simple" in df.columns


# --- acs -------------------------------------------------------------------


def test_acs_basic_tag_extraction():
    # \x01/\x02 are the real ACS control characters seen in the deployed
    # parser (agent/parse_acs.py); AB=barcode, AJ=title, AA=patron_id,
    # CT=destination per extract_fields' tag scheme. TAG_PATTERN
    # (`[A-Z]{2}[^|]*`) requires "|" between tag segments within the
    # message -- confirmed by reading the regex, not assumed.
    line = "\x011/31/2026\x028:00:00 AM\x02CK|AB12345|AJSome Title|AApatron1|CTMain\x01"

    df = acs.parse_lines([line])

    assert len(df) == 1
    row = df.iloc[0]
    assert row["barcode"] == "12345"
    assert row["title"] == "Some Title"
    assert row["patron_id"] == "patron1"
    assert row["destination"] == "Main"
    assert row["message_code"] == "CK"


def test_acs_short_line_without_enough_segments_is_skipped():
    df = acs.parse_lines(["\x01only-one-segment\x01"])

    assert len(df) == 0


def test_acs_empty_input_returns_typed_empty_frame():
    df = acs.parse_lines([])

    assert len(df) == 0
    assert list(df.columns) == acs.EMPTY_COLUMNS


def test_acs_bad_datetime_coerced_not_raised():
    line = "\x01bad-date\x02bad-time\x02CK|AB123\x01"
    df = acs.parse_lines([line])

    assert len(df) == 1
    assert pd.isna(df.iloc[0]["datetime"]) or math.isnan(float("nan"))


# --- parse_lines_with_offsets: offset survives skipped lines (Phase E) ----
#
# Phase E needs the exact source-record offset behind each surviving
# output row (for deterministic event identity), not just a row's
# position in the output DataFrame -- these prove that correspondence
# holds even when blank/short/malformed lines are silently dropped in
# between kept ones, which would desync a naive positional zip.


def test_checkins_parse_lines_with_offsets_skips_short_row_offset_too():
    numbered_lines = [
        (1000, "Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM"),
        (1100, "Broken row with too few fields|123"),
        (1200, "Book B|222|MLFIC|FIC B|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM"),
    ]

    df, kept_offsets = checkins.parse_lines_with_offsets(numbered_lines)

    assert len(df) == 2
    assert kept_offsets == [1000, 1200]
    assert df.iloc[0]["barcode"] == "111"
    assert df.iloc[1]["barcode"] == "222"


def test_checkins_parse_lines_with_offsets_skips_blank_line_offset_too():
    numbered_lines = [
        (500, ""),
        (600, "Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM"),
    ]

    df, kept_offsets = checkins.parse_lines_with_offsets(numbered_lines)

    assert len(df) == 1
    assert kept_offsets == [600]


def test_checkins_parse_lines_is_a_thin_wrapper_around_with_offsets():
    lines = ["Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM"]

    via_wrapper = checkins.parse_lines(lines)
    via_offsets, _ = checkins.parse_lines_with_offsets(list(enumerate(lines)))

    pd.testing.assert_frame_equal(via_wrapper, via_offsets)


def test_rejects_parse_lines_with_offsets_skips_short_row_offset_too():
    numbered_lines = [
        (2000, "111|Item Not Found|1/31/2026|8:00:00 AM"),
        (2100, "not enough fields"),
        (2200, "222|Multiple RFID tags detected|1/31/2026|8:01:00 AM"),
    ]

    df, kept_offsets = rejects.parse_lines_with_offsets(numbered_lines)

    assert len(df) == 2
    assert kept_offsets == [2000, 2200]


def test_acs_parse_lines_with_offsets_skips_short_line_offset_too():
    numbered_lines = [
        (3000, "\x011/31/2026\x028:00:00 AM\x02CK|AB12345|AJSome Title|AApatron1|CTMain\x01"),
        (3100, "\x01only-one-segment\x01"),
        (3200, "\x011/31/2026\x028:05:00 AM\x02CK|AB67890\x01"),
    ]

    df, kept_offsets = acs.parse_lines_with_offsets(numbered_lines)

    assert len(df) == 2
    assert kept_offsets == [3000, 3200]


# --- re-export parity: old import paths still work identically -----------


def test_old_import_path_reexports_same_function_object():
    from agent.parse_checkins import normalize_destination as old_normalize
    from agent.parse_rejects import simplify_error_message as old_simplify

    assert old_normalize is checkins.normalize_destination
    assert old_simplify is rejects.simplify_error_message
