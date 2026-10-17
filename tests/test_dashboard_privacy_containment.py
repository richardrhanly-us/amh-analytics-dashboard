"""The v1 ACS hold classifier's privacy containment (security/privacy Step 2).

Under the v1 Collector contract the cloud classifier (metrics.build_acs_item_summary) still reads `patron_id` and
`raw_message`. What this guarantees is that the classifier's aggregates are unchanged and that its supporting frames
carry no patron column, raw message, title or barcode out of the metrics layer.

Every value is a SYNTHETIC canary.
"""

from __future__ import annotations

import pandas as pd

import metrics

P_ADULT, P_ILL, P_PROG, P_COLL = "CANARY-PATRON-A", "CANARY-PATRON-B", "CANARY-PATRON-C", "CANARY-PATRON-D"
NAME_PROG, NAME_COLL = "CANARY PROGRAMMING ACCOUNT", "CANARY CATALOGING ACCOUNT"
PATTERN_COLL = "DACANARY DEPARTMENT PATTERN"
CANARIES = ("CANARY-PATRON", "CANARY-NAME", "CANARY-ADDR", "CANARY-MAIL", "CANARY-TITLE", "CANARY-BARCODE",
            NAME_PROG, NAME_COLL, PATTERN_COLL, "CANARY-RAW")


def _patron(pid, name, patron_type):
    return {"datetime": "2026-01-05 09:00:00", "message_code": "64", "barcode": None, "patron_id": pid, "destination": None,
            "raw_message": f"64 |AA{pid}|AE{name}|PT{patron_type}|BDCANARY-ADDR-1 MAIN ST|BECANARY-MAIL@example.invalid"}


def _item(barcode, pid, destination, prefix="101YNY", extra=""):
    return {"datetime": "2026-01-05 10:00:00", "message_code": "10", "barcode": barcode, "patron_id": pid,
            "destination": destination,
            "raw_message": f"{prefix}|AB{barcode}|AJCANARY-TITLE {barcode}|AA{pid or ''}|CT{destination or ''}{extra}"}


def synthetic_acs_rows() -> list[dict]:
    """Five hold items -- one public, one ILL (patron type), one programming (service-account name), one collection
    services (service-account name), one collection services (delimited pattern) -- plus a non-hold check-in."""
    return [
        _patron(P_ADULT, "CANARY-NAME ONE", "ADULT"),
        _patron(P_ILL, "CANARY-NAME TWO", "ILL"),
        _patron(P_PROG, NAME_PROG, "STAFF"),
        _patron(P_COLL, NAME_COLL, "STAFF"),
        _item("CANARY-BARCODE-A", P_ADULT, "Main"),
        _item("CANARY-BARCODE-B", P_ILL, "Westside"),
        _item("CANARY-BARCODE-C", P_PROG, "Main"),
        _item("CANARY-BARCODE-D", P_COLL, "Main"),
        _item("CANARY-BARCODE-E", None, "Main", extra=f"|{PATTERN_COLL}|"),
        _item("CANARY-BARCODE-F", P_ADULT, "Main", prefix="101YNN"),  # not a hold: never counted
    ]


CLASSIFIER_CONFIG = {
    "transit_labels": ["Westside", "Library Express"],
    "branch_services_names": [NAME_PROG],
    "collection_services_names": [NAME_COLL],
    "branch_services_da_patterns": [],
    "collection_services_da_patterns": [PATTERN_COLL],
}
EXPECTED_COUNTS = {"holds_total": 1, "ill_total": 1, "programming_total": 1, "collection_services_total": 2}


def _frame(rows):
    frame = pd.DataFrame(rows)
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    return frame


# --- the classifier: same aggregates, no patron columns leaving the metrics layer -----------------------------------------

def test_the_classifier_still_produces_exactly_the_same_aggregate_counts():
    summary = metrics.build_acs_item_summary(_frame(synthetic_acs_rows()), **CLASSIFIER_CONFIG)

    assert {k: summary[k] for k in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert summary["ill_by_branch"] == {"Westside": 1, "Library Express": 0} and summary["ill_main"] == 0


def test_the_classifiers_supporting_frames_carry_no_patron_columns_or_raw_text():
    summary = metrics.build_acs_item_summary(_frame(synthetic_acs_rows()), **CLASSIFIER_CONFIG)

    for key in ("items_df", "holds_df", "ill_df", "programming_df", "collection_services_df"):
        frame = summary[key]
        assert set(frame.columns) <= set(metrics._SUMMARY_FRAME_COLUMNS), key
        assert not {"patron_id", "patron_name", "patron_type", "raw_message", "raw_upper", "title"} & set(frame.columns), key
    # the counts and the flags are still there
    assert len(summary["holds_df"]) == 1 and len(summary["ill_df"]) == 1
    assert {"is_hold", "is_ill", "is_programming", "is_collection_services"} <= set(summary["items_df"].columns)


def test_the_supporting_frames_do_not_contain_any_patron_or_message_canary():
    summary = metrics.build_acs_item_summary(_frame(synthetic_acs_rows()), **CLASSIFIER_CONFIG)
    text_of_frames = "\n".join(summary[k].to_csv() for k in
                               ("items_df", "holds_df", "ill_df", "programming_df", "collection_services_df"))

    for canary in ("CANARY-PATRON", "CANARY-NAME", "CANARY-ADDR", "CANARY-MAIL", "CANARY-TITLE", "CANARY-RAW", NAME_PROG, NAME_COLL):
        assert canary not in text_of_frames


def test_an_empty_acs_frame_still_summarises_to_zero():
    for empty in (None, pd.DataFrame()):
        summary = metrics.build_acs_item_summary(empty, **CLASSIFIER_CONFIG)

        assert {k: summary[k] for k in EXPECTED_COUNTS} == dict.fromkeys(EXPECTED_COUNTS, 0)
