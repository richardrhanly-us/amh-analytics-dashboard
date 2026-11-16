"""UI2 v6: a sorter's bin layout (services.bin_layout) -- what is a layout, and what is refused."""

from __future__ import annotations

import pytest

from services.bin_layout import (
    BIN_INVALID,
    COLUMN_INVALID,
    COLUMNS_INVALID,
    DUPLICATE_BIN,
    END_INVALID,
    MAX_COLUMNS,
    NOT_AN_OBJECT,
    TOO_MANY_COLUMNS,
    WRONG_FIELDS,
    BinLayout,
    BinLayoutError,
    parse_bin_layout,
    serialize_bin_layout,
)
from services.sort_bin import bin_key

SEVEN_BINS = {"end": "0", "columns": [["6", "5"], ["4", "3"], ["2", "1"]]}


def _refused(value) -> str:
    with pytest.raises(BinLayoutError) as refusal:
        parse_bin_layout(value)
    return refusal.value.code


def test_a_layout_is_its_end_bin_and_its_columns_in_order_along_the_belt():
    assert parse_bin_layout(SEVEN_BINS) == BinLayout(end="0", columns=(("6", "5"), ("4", "3"), ("2", "1")))


def test_a_layout_is_stored_and_sent_as_it_was_read():
    assert serialize_bin_layout(parse_bin_layout(SEVEN_BINS)) == SEVEN_BINS


def test_the_order_given_is_kept_never_sorted():
    layout = parse_bin_layout({"end": "9", "columns": [["1", "12"], ["3", "2"]]})

    assert layout.columns == (("1", "12"), ("3", "2"))


@pytest.mark.parametrize("value", [None, [], "layout", 7, ["0", [["1", "2"]]]])
def test_anything_but_an_object_is_refused(value):
    assert _refused(value) == NOT_AN_OBJECT


@pytest.mark.parametrize("value", [
    {},
    {"end": "0"},
    {"columns": [["1", "2"]]},
    {"end": "0", "columns": [["1", "2"]], "rows": []},
    {"End": "0", "columns": [["1", "2"]]},
])
def test_a_missing_or_unknown_field_is_refused(value):
    assert _refused(value) == WRONG_FIELDS


@pytest.mark.parametrize("end", [None, 0, ["0"], {"bin": "0"}])
def test_an_end_bin_that_is_not_text_is_refused(end):
    assert _refused({"end": end, "columns": [["1", "2"]]}) == END_INVALID


@pytest.mark.parametrize("columns", [None, [], "6,5", {"0": ["1", "2"]}])
def test_columns_that_are_not_a_non_empty_list_are_refused(columns):
    assert _refused({"end": "0", "columns": columns}) == COLUMNS_INVALID


@pytest.mark.parametrize("column", [["1"], ["1", "2", "3"], [], "12", {"upper": "1", "lower": "2"}, None])
def test_a_column_that_is_not_exactly_a_pair_is_refused(column):
    assert _refused({"end": "0", "columns": [["5", "6"], column]}) == COLUMN_INVALID


@pytest.mark.parametrize("key", ["04", "00", "", " 4", "4 ", "-1", "+1", "1.0", "12345", "four", "unknown", "٣", 4, None])
def test_a_bin_not_written_as_the_reports_write_it_is_refused(key):
    """"04" names bin 4 in a check-in, but a layout must use the key the reports count it under."""
    assert _refused({"end": "0", "columns": [["1", key]]}) == BIN_INVALID
    assert _refused({"end": key, "columns": [["1", "2"]]}) in (END_INVALID, BIN_INVALID)


@pytest.mark.parametrize("key", ["0", "7", "10", "9999"])
def test_every_bin_key_a_report_can_produce_is_accepted(key):
    assert bin_key(key) == key
    assert parse_bin_layout({"end": "1", "columns": [["2", key if key != "2" else "3"]]})


@pytest.mark.parametrize("value", [
    {"end": "0", "columns": [["0", "1"]]},
    {"end": "0", "columns": [["1", "1"]]},
    {"end": "0", "columns": [["1", "2"], ["3", "2"]]},
])
def test_a_bin_named_twice_is_refused(value):
    assert _refused(value) == DUPLICATE_BIN


def test_a_layout_may_have_up_to_the_column_limit_and_no_more():
    columns = [[str(2 * index + 1), str(2 * index + 2)] for index in range(MAX_COLUMNS + 1)]

    assert len(parse_bin_layout({"end": "0", "columns": columns[:MAX_COLUMNS]}).columns) == MAX_COLUMNS
    assert _refused({"end": "0", "columns": columns}) == TOO_MANY_COLUMNS


def test_a_refusal_quotes_nothing_stored():
    with pytest.raises(BinLayoutError) as refusal:
        parse_bin_layout({"end": "0", "columns": [["SECRET-CANARY", "1"]]})

    assert "SECRET-CANARY" not in str(refusal.value)
    assert str(refusal.value) == BIN_INVALID
