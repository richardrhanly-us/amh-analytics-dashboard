"""R8J: what the dashboard's v1 hold classifier (src/metrics.py::build_acs_item_summary) actually does -- pinned as it is.

These are characterization tests. They do not say the behaviour is right; they say what it is, so that nothing changes it
by accident. The number they protect is the PUBLIC HOLDS count: every hold, less the ones the library's own accounts hold
(Branch Services, Collection Services) and the ILL ones. The two internal totals are pinned only because they decide
that number.

Every name, account and title here is synthetic. A "patron record" is a SIP2 message 64 (`|AE` name, `|PT` type) joined
to an item by patron_id; an "item record" is a message 10 whose raw text starts `101`, and a HOLD is one that starts
`101YNY`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from metrics import build_acs_item_summary
from services.settings_service import v1_hold_rules

ACCOUNT = "EXAMPLE (ST)STAFF ACCOUNT A"
DEPARTMENT = "(XX) TS(XX)-CATALOGING"
PATRON = "SAMPLE PUBLIC PATRON"

_clock = iter(range(10_000))


def _when(day: int = 5) -> pd.Timestamp:
    return pd.Timestamp(f"2026-09-{day:02d}T12:00:00") + pd.Timedelta(seconds=next(_clock))


def item(barcode: str, patron: str = "", *, hold: bool = True, extra: str = "", destination: str = "Main", when=None) -> dict:
    """A message-10 item record. `extra` is more SIP2 fields, e.g. "|DA<name>|"."""
    prefix = "101YNY" if hold else "101YNN"
    return {
        "datetime": when if when is not None else _when(),
        "message_code": "10",
        "barcode": barcode,
        "destination": destination,
        "patron_id": patron,
        "raw_message": f"{prefix}20260905    120000|AO1|AB{barcode}|AJA Synthetic Title|AQMain{extra}|",
    }


def patron(patron_id: str, name: str, patron_type: str = "ADULT", when=None) -> dict:
    """A message-64 patron record."""
    return {
        "datetime": when if when is not None else _when(),
        "message_code": "64",
        "barcode": "",
        "destination": "",
        "patron_id": patron_id,
        "raw_message": f"64              00120260905    120000|AO1|AA{patron_id}|AE{name}|PT{patron_type}|",
    }


def summary(rows, *, branch=(), collection=(), branch_da=(), collection_da=(), transit=()):
    return build_acs_item_summary(pd.DataFrame(rows), list(transit), set(branch), set(collection), list(branch_da), list(collection_da))


def counts(result) -> tuple[int, int, int, int]:
    """(public holds, ILL, Branch Services, Collection Services)."""
    return result["holds_total"], result["ill_total"], result["programming_total"], result["collection_services_total"]


# =====================================================================================================================
# With nothing configured
# =====================================================================================================================

def test_with_no_lists_every_hold_that_is_not_ill_is_public():
    rows = [patron("p1", PATRON), patron("p2", ACCOUNT), item("b1", "p1"), item("b2", "p2"), item("b3")]

    assert counts(summary(rows)) == (3, 0, 0, 0)


def test_no_rows_and_no_item_records_are_zero_everywhere():
    assert counts(summary([])) == (0, 0, 0, 0)
    assert counts(summary([patron("p1", PATRON)])) == (0, 0, 0, 0)


# =====================================================================================================================
# What a hold is
# =====================================================================================================================

def test_only_a_101YNY_item_record_is_a_hold():
    rows = [patron("p1", ACCOUNT), item("b1", "p1", hold=False), item("b2", "p1", hold=True)]

    result = summary(rows, branch=[ACCOUNT])
    # The non-hold 101 record is an item, never a hold of either kind -- even for a configured account.
    assert counts(result) == (0, 0, 1, 0)
    assert len(result["items_df"]) == 2


def test_the_latest_record_of_an_item_decides_whether_it_is_a_hold():
    first, later = _when(1), _when(2)
    rows = [item("b1", when=first, hold=True), item("b1", when=later, hold=False)]

    assert counts(summary(rows)) == (0, 0, 0, 0)
    assert counts(summary([item("b2", when=first, hold=False), item("b2", when=later, hold=True)])) == (1, 0, 0, 0)


def test_a_message_10_that_is_not_a_101_record_is_not_an_item():
    rows = [{**item("b1"), "raw_message": "100YNY20260905    120000|AB b1|"}]

    assert counts(summary(rows)) == (0, 0, 0, 0)


# =====================================================================================================================
# Names: the hold's patron's name, from the patron record
# =====================================================================================================================

@pytest.mark.parametrize("configured", [ACCOUNT, ACCOUNT.lower(), f"  {ACCOUNT}  ", ACCOUNT.title()])
def test_a_name_matches_whatever_its_case_and_surrounding_space(configured):
    rows = [patron("p1", ACCOUNT), item("b1", "p1")]

    assert counts(summary(rows, branch=[configured])) == (0, 0, 1, 0)
    assert counts(summary(rows, collection=[configured])) == (0, 0, 0, 1)


@pytest.mark.parametrize("configured", ["EXAMPLE (ST)STAFF", "STAFF ACCOUNT A", "EXAMPLE STAFF ACCOUNT A", ACCOUNT + "S"])
def test_a_name_must_match_the_whole_name_and_its_punctuation(configured):
    rows = [patron("p1", ACCOUNT), item("b1", "p1")]

    assert counts(summary(rows, branch=[configured])) == (1, 0, 0, 0)


def test_a_name_is_matched_through_the_patron_record_joined_by_patron_id_only():
    # The item's own text says nothing of the name; the patron record does, and only for that patron id.
    rows = [patron("p1", ACCOUNT), patron("p2", PATRON), item("b1", "p1"), item("b2", "p2"), item("b3", "p9")]

    assert counts(summary(rows, collection=[ACCOUNT])) == (2, 0, 0, 1)


def test_the_latest_patron_record_for_a_patron_id_is_the_one_used_whichever_side_of_the_hold_it_falls():
    rows = [patron("p1", PATRON, when=_when(1)), item("b1", "p1", when=_when(2)), patron("p1", ACCOUNT, when=_when(3))]

    assert counts(summary(rows, branch=[ACCOUNT])) == (0, 0, 1, 0)


def test_blank_and_repeated_names_change_nothing():
    rows = [patron("p1", ACCOUNT), patron("p2", PATRON), item("b1", "p1"), item("b2", "p2")]

    assert counts(summary(rows, branch=[ACCOUNT, ACCOUNT.lower(), "", "   "])) == (1, 0, 1, 0)
    # A blank name is not "no name": a hold with no patron record is not matched by a blank entry.
    assert counts(summary([item("b3")], branch=["", "  "])) == (1, 0, 0, 0)


# =====================================================================================================================
# |DA...| patterns: a literal marker in the item's own raw message
# =====================================================================================================================

def test_a_da_pattern_classifies_a_hold_with_no_patron_record_at_all():
    rows = [item("b1", extra=f"|DA{DEPARTMENT}")]

    assert counts(summary(rows, collection_da=[f"DA{DEPARTMENT}"])) == (0, 0, 0, 1)
    assert counts(summary(rows, branch_da=[f"DA{DEPARTMENT}"])) == (0, 0, 1, 0)


@pytest.mark.parametrize("configured", [f"da{DEPARTMENT.lower()}", f"  DA{DEPARTMENT} "])
def test_a_da_pattern_matches_whatever_its_case_and_surrounding_space(configured):
    assert counts(summary([item("b1", extra=f"|DA{DEPARTMENT}")], collection_da=[configured])) == (0, 0, 0, 1)


def test_a_da_pattern_must_be_a_whole_field_with_a_bar_on_both_sides():
    whole = item("b1", extra=f"|DA{DEPARTMENT}")
    longer = item("b2", extra=f"|DA{DEPARTMENT} ANNEX")
    shorter = item("b3", extra="|DA(XX) TS(XX)")

    assert counts(summary([whole, longer, shorter], collection_da=[f"DA{DEPARTMENT}"])) == (2, 0, 0, 1)


def test_a_da_pattern_is_literal_text_not_a_regular_expression():
    # Brackets, dots and stars are matched as themselves.
    literal = item("b1", extra="|DA(X.X)*-ACCT")
    lookalike = item("b2", extra="|DA(XYX)XX-ACCT")

    assert counts(summary([literal, lookalike], branch_da=["DA(X.X)*-ACCT"])) == (1, 0, 1, 0)


def test_a_blank_da_pattern_is_ignored_and_does_not_match_every_message():
    assert counts(summary([item("b1"), item("b2", extra="||")], branch_da=["", "   "])) == (2, 0, 0, 0)


# =====================================================================================================================
# Overlap: the categories are not exclusive, and nothing decides between them
# =====================================================================================================================

def test_a_name_in_both_lists_counts_in_both_totals_and_once_out_of_public_holds():
    rows = [patron("p1", ACCOUNT), item("b1", "p1"), item("b2")]

    assert counts(summary(rows, branch=[ACCOUNT], collection=[ACCOUNT])) == (1, 0, 1, 1)


def test_a_name_and_a_marker_for_the_same_hold_count_it_once_per_category():
    rows = [patron("p1", ACCOUNT), item("b1", "p1", extra=f"|DA{ACCOUNT}")]

    assert counts(summary(rows, branch=[ACCOUNT], branch_da=[f"DA{ACCOUNT}"])) == (0, 0, 1, 0)


def test_an_ill_hold_for_a_configured_account_counts_as_both_and_is_not_public():
    rows = [patron("p1", ACCOUNT, patron_type="ILL"), item("b1", "p1"), item("b2")]

    assert counts(summary(rows, collection=[ACCOUNT])) == (1, 1, 0, 1)


@pytest.mark.parametrize("where", ["patron-type", "destination", "patron-name", "title"])
def test_ill_is_decided_by_fixed_rules_and_not_by_the_lists(where):
    rows = {
        "patron-type": [patron("p1", PATRON, patron_type="ILL"), item("b1", "p1")],
        "destination": [item("b1", destination="ILL Office")],
        "patron-name": [patron("p1", "INTERLIBRARY LOAN DESK"), item("b1", "p1")],
        # The known quirk: the whole raw message is searched, so a title with the word "Ill" in it is ILL.
        "title": [{**item("b1"), "raw_message": "101YNY20260905    120000|AO1|ABb1|AJThe Ill-Made Synthetic Knight|"}],
    }[where]

    assert counts(summary(rows)) == (0, 1, 0, 0)


# =====================================================================================================================
# What comes out
# =====================================================================================================================

def test_the_summary_frames_carry_no_patron_or_raw_record_columns():
    result = summary([patron("p1", ACCOUNT), item("b1", "p1"), item("b2")], branch=[ACCOUNT])

    for key in ("items_df", "holds_df", "ill_df", "programming_df", "collection_services_df"):
        assert set(result[key].columns) <= {"datetime", "message_code", "barcode", "destination", "is_hold", "is_ill",
                                            "is_programming", "is_collection_services"}, key
    assert ACCOUNT not in repr(result)


def test_the_settings_object_the_dashboard_passes_classifies_exactly_as_plain_lists_do():
    # What app.py hands the classifier now: services.settings_service.V1HoldRules, built from the stored block.
    stored = {"branch_services_names": [f" {ACCOUNT.lower()} "], "collection_services_names": [],
              "branch_services_da_patterns": [], "collection_services_da_patterns": [f"DA{DEPARTMENT}"]}
    rules = v1_hold_rules(stored)
    rows = [patron("p1", ACCOUNT), item("b1", "p1"), item("b2", extra=f"|DA{DEPARTMENT}"), item("b3")]

    via_rules = build_acs_item_summary(pd.DataFrame(rows), [], rules.branch_services_names, rules.collection_services_names,
                                       rules.branch_services_da_patterns, rules.collection_services_da_patterns)
    assert counts(via_rules) == counts(summary(rows, branch=[ACCOUNT], collection_da=[f"DA{DEPARTMENT}"])) == (1, 0, 1, 1)
