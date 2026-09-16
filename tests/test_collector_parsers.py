"""Tests for collector/parsers.py -- the thin adapter between the
canonical, UNCHANGED agent/parser/{checkins,rejects,acs}.py modules and
the exact dict shape collector/uploader.py sends to POST /upload.

These tests deliberately do NOT re-test parsing algorithm correctness
(delimiter splitting, destination normalization rules, error-message
categorization rules, ACS tag extraction) -- that's already covered by
tests/test_parser_canonical.py against agent/parser/* directly, and is
explicitly out of scope for this adapter to re-verify. What's tested
here is the adapter boundary itself: field mapping, field dropping,
customer_id/branch_id injection, event_time serialization, and the
malformed-timestamp behavior -- now UNIFORM across all three sources
(drop the row entirely), corrected from an earlier assumption that
Checkins/Rejects should send event_time=null like the legacy uploader
did. That assumption was proven unsafe against the current backend
schema (checkins.event_time / rejects.event_time are NOT NULL) in the
backend-contract verification that preceded this change -- see
collector/parsers.py's own module docstring for the full reasoning.

FIXTURE PROVENANCE, stated explicitly per source:
  - Checkins: reuses the exact real-production-row line from
    tests/test_parser_canonical.py's
    test_checkins_real_production_row_from_checkins_clean_csv (itself
    reconstructed from a real row in the archived deployed agent's
    checkins_clean.csv, 2026-08-31). This is the one source with a
    genuine production-grounded fixture.
  - Rejects and ACS: the lines below are SYNTHETIC, hand-constructed to
    match the documented format (pipe-delimited for rejects; \\x01/\\x02
    control characters and SIP-style tags for ACS). They are
    representative, not production-grounded -- real raw Rejects.txt and
    ACS Log.txt samples remain an explicit, unmet validation requirement
    before controlled AMH rollout (see this phase's own report).
"""

from __future__ import annotations

from collector import parsers

CUSTOMER_ID = 100
BRANCH_ID = 5

# Real production row, same line used in
# tests/test_parser_canonical.py::test_checkins_real_production_row_from_checkins_clean_csv.
REAL_CHECKIN_LINE = (
    "Sunny days /|33472004192508|MLEPB|E KERBEL NATURE|000|1|False||4|N|N|N|8/31/2026|4:18:39 PM"
)


def _checkins(lines):
    return parsers.build_production_parse_fns(customer_id=CUSTOMER_ID, branch_id=BRANCH_ID)["checkins"](lines)


def _rejects(lines):
    return parsers.build_production_parse_fns(customer_id=CUSTOMER_ID, branch_id=BRANCH_ID)["rejects"](lines)


def _acs(lines):
    return parsers.build_production_parse_fns(customer_id=CUSTOMER_ID, branch_id=BRANCH_ID)["acs"](lines)


# --- build_production_parse_fns ----------------------------------------


def test_build_production_parse_fns_returns_exactly_the_three_known_sources():
    fns = parsers.build_production_parse_fns(customer_id=CUSTOMER_ID, branch_id=BRANCH_ID)
    assert set(fns.keys()) == {"checkins", "rejects", "acs"}


# --- checkins: valid row (production-grounded) --------------------------


def test_valid_checkin_maps_to_exact_backend_field_names():
    records = _checkins([REAL_CHECKIN_LINE])

    assert len(records) == 1
    record = records[0]

    assert record == {
        "customer_id": CUSTOMER_ID,
        "branch_id": BRANCH_ID,
        "event_time": "2026-08-31 16:18:39",
        "title": "Sunny days /",
        "barcode": "33472004192508",
        "collection_code": "MLEPB",
        "call_number": "E KERBEL NATURE",
        "shelf_code": "000",
        "destination": "Main",
        "bin": "4",
        "is_problem": False,
        "message": "",
        "flag_1": "N",
        "flag_2": "N",
        "flag_3": "N",
        "source_file": "Checkins.txt",
    }


def test_checkins_output_has_no_dropped_or_invented_fields():
    # date_only/hour/day_of_week/is_transit/destination_raw/date/time are
    # all real columns agent.parser.checkins produces, but none of them
    # are accepted by main.py's CheckinRow -- the adapter must not leak
    # them through. source_event_id must not be invented either (see
    # collector/parsers.py's module docstring).
    record = _checkins([REAL_CHECKIN_LINE])[0]

    expected_keys = {
        "customer_id", "branch_id", "event_time", "title", "barcode",
        "collection_code", "call_number", "shelf_code", "destination",
        "bin", "is_problem", "message", "flag_1", "flag_2", "flag_3",
        "source_file",
    }
    assert set(record.keys()) == expected_keys


def test_checkins_destination_normalization_survives_into_output():
    line = "Book A|111|MLFIC|FIC A|000|WESTSIDE (2910 IH35)|False||1|N|N|N|1/31/2026|8:00:00 AM"
    record = _checkins([line])[0]
    assert record["destination"] == "Westside"


def test_checkins_short_row_is_dropped_not_crashed():
    lines = [REAL_CHECKIN_LINE, "Broken row with too few fields|123"]
    records = _checkins(lines)

    assert len(records) == 1
    assert records[0]["barcode"] == "33472004192508"


def test_checkins_extra_fields_beyond_the_expected_columns_are_truncated_not_appended():
    # agent.parser.checkins truncates a line with MORE than 14 fields to
    # the first 14 (does not reject it, does not carry the extra field
    # through) -- the adapter must not add an extra output key for it.
    line = "Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM|EXTRA_FIELD"
    record = _checkins([line])[0]

    assert record["barcode"] == "111"
    assert "EXTRA_FIELD" not in record.values()


def test_checkins_malformed_timestamp_row_is_dropped_not_sent_with_null_event_time():
    # CORRECTED behavior (backend-contract verification phase):
    # checkins.event_time is NOT NULL in the database, so a row with no
    # usable timestamp must never be uploaded at all -- see
    # collector/parsers.py's module docstring's MALFORMED TIMESTAMP
    # BEHAVIOR section. This replaces the old
    # test_checkins_bad_timestamp_keeps_the_row_with_null_event_time,
    # which asserted the now-corrected legacy behavior.
    line = "Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|not-a-date|not-a-time"
    records = _checkins([line])

    assert records == []


def test_checkins_malformed_timestamp_row_does_not_block_valid_neighboring_rows():
    lines = [
        "Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|not-a-date|not-a-time",
        REAL_CHECKIN_LINE,
    ]
    records = _checkins(lines)

    assert len(records) == 1
    assert records[0]["barcode"] == "33472004192508"
    assert records[0]["event_time"] is not None


def test_checkins_empty_input_returns_empty_list():
    assert _checkins([]) == []


# --- rejects: valid row (synthetic, representative -- not production-grounded) --


def test_valid_reject_maps_to_exact_backend_field_names():
    # SYNTHETIC line -- see module docstring's FIXTURE PROVENANCE.
    line = "111|Item Not Found in ACS|1/31/2026|8:00:00 AM"
    records = _rejects([line])

    assert len(records) == 1
    assert records[0] == {
        "customer_id": CUSTOMER_ID,
        "branch_id": BRANCH_ID,
        "event_time": "2026-01-31 08:00:00",
        "barcode": "111",
        "message": "Item Not Found in ACS",
        "source_file": "Rejects.txt",
    }


def test_rejects_output_has_no_dropped_or_invented_fields():
    line = "111|Item Not Found in ACS|1/31/2026|8:00:00 AM"
    record = _rejects([line])[0]

    expected_keys = {"customer_id", "branch_id", "event_time", "barcode", "message", "source_file"}
    assert set(record.keys()) == expected_keys


def test_reject_error_simplification_does_not_survive_into_the_upload_row():
    # error_simple is a real column agent.parser.rejects produces (used
    # for dashboard display), but RejectRow has no field for it and the
    # legacy uploader never sent it -- only the raw error_message (mapped
    # to "message") is sent. Confirms it neither replaces "message" nor
    # appears under any key.
    line = "111|Multiple RFID tags detected|1/31/2026|8:01:00 AM"
    record = _rejects([line])[0]

    assert record["message"] == "Multiple RFID tags detected"
    assert "error_simple" not in record
    assert "RFID Collision" not in record.values()


def test_rejects_short_row_is_dropped_not_crashed():
    lines = ["111|Item Not Found|1/31/2026|8:00:00 AM", "not enough fields"]
    records = _rejects(lines)

    assert len(records) == 1
    assert records[0]["barcode"] == "111"


def test_rejects_malformed_timestamp_row_is_dropped_not_sent_with_null_event_time():
    # CORRECTED behavior -- see the checkins equivalent above and
    # collector/parsers.py's module docstring. rejects.event_time is
    # also NOT NULL in the database.
    line = "111|Item Not Found|not-a-date|not-a-time"
    records = _rejects([line])

    assert records == []


def test_rejects_malformed_timestamp_row_does_not_block_valid_neighboring_rows():
    lines = [
        "111|Item Not Found|not-a-date|not-a-time",
        "222|Item Not Found in ACS|1/31/2026|8:00:00 AM",
    ]
    records = _rejects(lines)

    assert len(records) == 1
    assert records[0]["barcode"] == "222"
    assert records[0]["event_time"] is not None


def test_rejects_empty_input_returns_empty_list():
    assert _rejects([]) == []


# --- acs: valid row (synthetic, representative -- not production-grounded) --


def test_valid_acs_maps_to_exact_backend_field_names():
    # SYNTHETIC line -- see module docstring's FIXTURE PROVENANCE. Same
    # \x01/\x02 control-character shape and AB/AJ/AA/CT tag scheme
    # already proven against agent.parser.acs in test_parser_canonical.py.
    line = "\x011/31/2026\x028:00:00 AM\x02CK|AB12345|AJSome Title|AApatron1|CTMain\x01"
    records = _acs([line])

    assert len(records) == 1
    assert records[0] == {
        "customer_id": CUSTOMER_ID,
        "branch_id": BRANCH_ID,
        "event_time": "2026-01-31 08:00:00",
        "message_code": "CK",
        "barcode": "12345",
        "title": "Some Title",
        "patron_id": "patron1",
        "destination": "Main",
        "raw_message": "CK|AB12345|AJSome Title|AApatron1|CTMain",
        "source_file": "ACS Log.txt",
    }


def test_acs_output_has_no_dropped_or_invented_fields():
    line = "\x011/31/2026\x028:00:00 AM\x02CK|AB12345|AJSome Title|AApatron1|CTMain\x01"
    record = _acs([line])[0]

    expected_keys = {
        "customer_id", "branch_id", "event_time", "message_code", "barcode",
        "title", "patron_id", "destination", "raw_message", "source_file",
    }
    assert set(record.keys()) == expected_keys


def test_acs_control_character_and_tag_parsing_survives_into_output():
    line = "\x011/31/2026\x028:05:00 AM\x02CK|AB67890|AJAnother Title|AApatron2|CTWestside\x01"
    record = _acs([line])[0]

    assert record["barcode"] == "67890"
    assert record["title"] == "Another Title"
    assert record["patron_id"] == "patron2"
    assert record["destination"] == "Westside"


def test_acs_short_line_without_enough_segments_is_dropped_not_crashed():
    records = _acs(["\x01only-one-segment\x01"])
    assert records == []


def test_acs_bad_timestamp_drops_the_row_entirely():
    # ACS's already-established behavior, UNCHANGED by the backend-
    # contract correction (checkins/rejects were brought in line with
    # THIS behavior, not the other way around). See collector/parsers.py's
    # module docstring's MALFORMED TIMESTAMP BEHAVIOR section.
    line = "\x01bad-date\x02bad-time\x02CK|AB123\x01"
    records = _acs([line])

    assert records == []


def test_acs_malformed_timestamp_row_does_not_block_valid_neighboring_rows():
    lines = [
        "\x01bad-date\x02bad-time\x02CK|AB123\x01",
        "\x011/31/2026\x028:00:00 AM\x02CK|AB12345|AJSome Title|AApatron1|CTMain\x01",
    ]
    records = _acs(lines)

    assert len(records) == 1
    assert records[0]["barcode"] == "12345"
    assert records[0]["event_time"] is not None


def test_acs_empty_input_returns_empty_list():
    assert _acs([]) == []


# --- source_event_id is never introduced --------------------------------


def test_no_adapter_ever_sets_source_event_id():
    checkin_record = _checkins([REAL_CHECKIN_LINE])[0]
    reject_record = _rejects(["111|Item Not Found|1/31/2026|8:00:00 AM"])[0]
    acs_record = _acs(["\x011/31/2026\x028:00:00 AM\x02CK|AB12345\x01"])[0]

    assert "source_event_id" not in checkin_record
    assert "source_event_id" not in reject_record
    assert "source_event_id" not in acs_record


# --- mixed valid + malformed batches: adapter call never fails ----------


def test_mixed_checkins_batch_yields_only_valid_rows_never_raises():
    lines = [
        REAL_CHECKIN_LINE,
        "Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|not-a-date|not-a-time",
        "Broken row with too few fields|123",
        "",
        "Book B|222|MLFIC|FIC B|000|1|False||1|N|N|N|1/31/2026|8:00:00 AM",
    ]
    records = _checkins(lines)  # must not raise

    assert sorted(r["barcode"] for r in records) == ["222", "33472004192508"]
    assert all(r["event_time"] is not None for r in records)


def test_mixed_rejects_batch_yields_only_valid_rows_never_raises():
    lines = [
        "111|Item Not Found|not-a-date|not-a-time",
        "not enough fields",
        "",
        "222|Item Not Found in ACS|1/31/2026|8:00:00 AM",
    ]
    records = _rejects(lines)  # must not raise

    assert [r["barcode"] for r in records] == ["222"]
    assert all(r["event_time"] is not None for r in records)


def test_mixed_acs_batch_yields_only_valid_rows_never_raises():
    lines = [
        "\x01bad-date\x02bad-time\x02CK|AB123\x01",
        "\x01only-one-segment\x01",
        "\x011/31/2026\x028:00:00 AM\x02CK|AB12345|AJSome Title|AApatron1|CTMain\x01",
    ]
    records = _acs(lines)  # must not raise

    assert [r["barcode"] for r in records] == ["12345"]
    assert all(r["event_time"] is not None for r in records)


# --- full UploadRequest can never contain a null event_time for -------------
# --- checkins/rejects (requirement D) ----------------------------------


def test_upload_request_from_adapter_output_never_contains_null_event_time_for_checkins_or_rejects():
    # Imports the REAL backend models from main.py, exactly as the
    # backend-contract verification did -- local validation only, no
    # network call (main.py's create_engine is lazy; tests/conftest.py
    # already sets a placeholder DATABASE_URL for exactly this reason).
    import main

    checkin_lines = [
        REAL_CHECKIN_LINE,
        "Book A|111|MLFIC|FIC A|000|1|False||1|N|N|N|not-a-date|not-a-time",
    ]
    reject_lines = [
        "111|Item Not Found|not-a-date|not-a-time",
        "222|Item Not Found in ACS|1/31/2026|8:00:00 AM",
    ]
    acs_lines = [
        "\x01bad-date\x02bad-time\x02CK|AB123\x01",
        "\x011/31/2026\x028:00:00 AM\x02CK|AB12345|AJSome Title|AApatron1|CTMain\x01",
    ]

    request = main.UploadRequest(
        checkins=_checkins(checkin_lines),
        rejects=_rejects(reject_lines),
        acs=_acs(acs_lines),
    )

    assert all(row.event_time is not None for row in request.checkins)
    assert all(row.event_time is not None for row in request.rejects)
    assert all(row.event_time is not None for row in request.acs)
    # And the valid rows still made it through, proving this isn't
    # trivially true because everything got dropped.
    assert len(request.checkins) == 1
    assert len(request.rejects) == 1
    assert len(request.acs) == 1
