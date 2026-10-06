"""services.sort_bin: the one key a sort bin has, however it was stored."""

from __future__ import annotations

import inspect

import pytest

from collector import v2_normalize
from services import sort_bin
from services.sort_bin import bin_key, bin_order


@pytest.mark.parametrize(("stored", "key"), [
    ("0", "0"),
    ("1", "1"),
    ("4", "4"),
    ("7", "7"),
    ("10", "10"),
    ("12", "12"),
    ("20", "20"),
    ("999", "999"),
    ("9999", "9999"),
])
def test_a_number_is_its_own_bin(stored, key):
    assert bin_key(stored) == key


@pytest.mark.parametrize(("stored", "key"), [
    ("00", "0"),
    ("000", "0"),
    ("0000", "0"),
    ("04", "4"),
    ("004", "4"),
    ("0004", "4"),
    ("0010", "10"),
    ("010", "10"),
    ("0100", "100"),
    ("0999", "999"),
])
def test_leading_zeros_do_not_make_another_bin(stored, key):
    assert bin_key(stored) == key


@pytest.mark.parametrize("stored", ["0", "00", "000", "0000", " 0 "])
def test_zero_is_a_bin_like_any_other(stored):
    assert bin_key(stored) == "0"


@pytest.mark.parametrize(("stored", "key"), [(" 4", "4"), ("4 ", "4"), ("  04\t", "4"), ("\n10\r\n", "10")])
def test_whitespace_around_a_number_is_not_part_of_it(stored, key):
    assert bin_key(stored) == key


@pytest.mark.parametrize("stored", [None, "", " ", "   ", "\t", "\n", "\r\n"])
def test_nothing_stored_or_blank_text_is_unknown(stored):
    assert bin_key(stored) is None


@pytest.mark.parametrize("stored", ["unknown", "UNKNOWN", "Unknown", " unknown ", "n/a", "N/A", "none", "null", "NULL", "-", "?"])
def test_the_word_unknown_and_its_like_are_unknown(stored):
    assert bin_key(stored) is None


@pytest.mark.parametrize("stored", [
    "Westside", "Main", "bin1", "Bin 4", "BIN4", "b4", "4b", "four", "exception", "overflow", "holds", "reject",
    "Library Express", "x", "bin", "Bin",
])
def test_text_that_is_not_a_number_is_unknown(stored):
    assert bin_key(stored) is None


@pytest.mark.parametrize("stored", [
    "-1", "+1", "-0", "1.0", "1.5", "4.", ".4", "1,000", "1 0", "1-2", "1/2", "#4", "4!", "(4)", "4;", "0x4", "1e3",
    "4'; DROP TABLE checkins; --", "4\x00", "4_0", "٤", "４", "²", "Ⅳ", "一",
])
def test_signs_points_punctuation_and_digits_that_are_not_ascii_are_unknown(stored):
    assert bin_key(stored) is None


@pytest.mark.parametrize("stored", [
    "12345", "00004", "00000", "31234000123456", "9" * 50, "0" * 50, "4" * 10_000, "x" * 10_000,
    "Westside " * 2_000, "4" + " " * 10_000 + "4",
])
def test_a_value_too_long_to_be_a_bin_is_unknown(stored):
    # Five digits or more is not a bin under the collector contract -- with or without leading zeros.
    assert bin_key(stored) is None


@pytest.mark.parametrize("stored", [0, 4, 4.0, True, False, b"4", ["4"], ("4",), {"bin": "4"}, object()])
def test_anything_that_is_not_text_is_unknown(stored):
    assert bin_key(stored) is None


def test_a_key_is_always_a_plain_number_of_one_to_four_digits_with_no_leading_zero():
    for number in range(10_000):
        for stored in (str(number), str(number).zfill(4)):
            assert bin_key(stored) == str(number)
    assert {bin_key(str(number)) for number in range(10_000)} == {str(number) for number in range(10_000)}


def test_a_key_is_a_key_already():
    for stored in ("0", "04", "0010", "9999", " 7 "):
        key = bin_key(stored)
        assert key is not None and bin_key(key) == key


def test_the_stored_value_is_not_changed():
    stored = " 04 "

    assert bin_key(stored) == "4"
    assert stored == " 04 "


def test_bins_sort_by_their_number_not_their_text():
    keys = ["10", "2", "0", "1", "20", "12", "7", "100", "9"]

    assert sorted(keys, key=bin_order) == ["0", "1", "2", "7", "9", "10", "12", "20", "100"]
    assert sorted(keys) != sorted(keys, key=bin_order)       # text order would put 10 before 2


# --- one rule for both eras ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "0", "00", "4", "04", "0010", "9999", " 7 ", "12345", "00004", "", " ", None, "unknown", "Westside", "-1", "1.0",
    "bin1", "٤", "４", 4, "4 4", "1,000", "0000", "\t3\n",
])
def test_a_value_is_a_known_bin_exactly_when_the_collector_contract_says_so(raw):
    # What the collector stores for a raw bin is that text or "unknown". A legacy row holding the same raw text
    # must be known or unknown the same way, or one bin would be two things on either side of a cutover.
    stored_by_the_collector = v2_normalize.normalize_bin(raw)

    assert (bin_key(raw) is None) == (stored_by_the_collector == v2_normalize.UNKNOWN)
    # And the two stored forms of one bin have one key.
    assert bin_key(stored_by_the_collector) == bin_key(raw)


def test_the_rule_is_the_collector_contracts_own_pattern():
    assert sort_bin._NUMERIC_BIN.pattern == v2_normalize._NUMERIC_BIN.pattern == r"[0-9]{1,4}"


# --- what the module is, and is not ---------------------------------------------------------------------------------

def test_no_bin_number_is_named_and_nothing_is_assumed_about_which_bins_exist():
    code = inspect.getsource(sort_bin).split('"""', 2)[2]

    # No bin is special, there is no list or count of bins, and nothing is said about what a bin is for.
    for forbidden in ("range(", "== \"0\"", "== '0'", "7", "exception", "overflow", "hold", "reject", "capacity",
                      "fullness", "destination", "label", "utiliz"):
        assert forbidden not in code, forbidden


def test_the_module_is_pure():
    imports = [line.strip() for line in inspect.getsource(sort_bin).splitlines() if line.startswith(("import ", "from "))]

    assert imports == ["from __future__ import annotations", "import re"]
    assert sorted(name for name in vars(sort_bin) if not name.startswith("_") and name not in ("annotations", "re")) == [
        "bin_key", "bin_order",
    ]
